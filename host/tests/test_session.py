import fcntl
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sdss import runtime
from sdss.profiles import AZAHAR
from sdss.session import Session


class WriteArtifactsTests(unittest.TestCase):
    """Regression coverage for the actual `sdss run` entry point, not just its helpers.

    Every real hardware launch crashed with
    "'PosixPath' object has no attribute 'outer_gamescope_resolution'" because a local
    variable in write_artifacts() was named `runtime`, shadowing the `runtime` module
    import used two lines later. The standalone runtime.outer_gamescope_resolution()
    tests never caught this — they call the module function directly and never go
    through Session.write_artifacts() at all.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        env = {
            "XDG_RUNTIME_DIR": str(root / "runtime"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_DATA_HOME": str(root / "data"),
        }
        patcher = mock.patch.dict("os.environ", env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_detects_outer_gamescope_resolution_without_crashing(self):
        with mock.patch.object(
            runtime, "outer_gamescope_resolution", return_value=(1280, 800)
        ), mock.patch.object(runtime, "parent_display", return_value=("wayland", "gamescope-0")):
            session = Session(profile=AZAHAR, command=["azahar"])
            artifacts = session.write_artifacts()

        config = artifacts["sway_config"].read_text()
        self.assertIn("output WL-1 mode 1280x800@60Hz position 0 0", config)

    def test_headless_output_uses_the_profiles_native_second_size(self):
        # Regression test: write_artifacts() used to hardcode a 1280x800 HEADLESS-1
        # output for every profile, silently ignoring each Profile's own
        # `second_size` (e.g. Azahar's 320x240 native second-screen resolution).
        with mock.patch.object(
            runtime, "outer_gamescope_resolution", return_value=(1280, 800)
        ), mock.patch.object(runtime, "parent_display", return_value=("wayland", "gamescope-0")):
            session = Session(profile=AZAHAR, command=["azahar"])
            artifacts = session.write_artifacts()

        config = artifacts["sway_config"].read_text()
        width, height = AZAHAR.second_size
        self.assertIn(f"output HEADLESS-1 mode {width}x{height}@60Hz", config)

    def test_falls_back_to_1920x1080_when_no_outer_gamescope_found(self):
        with mock.patch.object(
            runtime, "outer_gamescope_resolution", return_value=None
        ), mock.patch.object(runtime, "parent_display", return_value=("wayland", "gamescope-0")):
            session = Session(profile=AZAHAR, command=["azahar"])
            artifacts = session.write_artifacts()

        config = artifacts["sway_config"].read_text()
        self.assertIn("output WL-1 mode 1920x1080@60Hz position 0 0", config)

    def test_uses_x11_output_name_when_steam_provides_a_per_game_display(self):
        # Steam hands the launched game DISPLAY=:1 (its own per-game Xwayland) but no
        # WAYLAND_DISPLAY. Generating a sway.conf with an "output WL-1 ..." line in that
        # case is exactly the bug that made every real launch spin forever: sway would
        # dutifully create WL-1 on the shared gamescope-0 Wayland socket, correctly
        # rendered but invisible to steamcompmgr's per-game readiness walk. The config
        # must target X11-1 whenever runtime.parent_display() picks the x11 backend —
        # and must NOT force a "mode" on it: wlr_x11_backend rejects any mode that
        # isn't the per-game Xwayland's own root window size ("Requested backend
        # configuration failed"), leaving the output power:false and the spinner stuck
        # exactly as before, just for a different reason.
        with mock.patch.object(
            runtime, "outer_gamescope_resolution", return_value=(1280, 800)
        ), mock.patch.object(runtime, "parent_display", return_value=("x11", ":1")):
            session = Session(profile=AZAHAR, command=["azahar"])
            artifacts = session.write_artifacts()

        config = artifacts["sway_config"].read_text()
        self.assertIn("output X11-1 position 0 0", config)
        self.assertNotIn("output X11-1 mode", config)
        self.assertNotIn("WL-1", config)

    def test_x11_backend_gets_a_resize_script_gating_readiness(self):
        # X11-1 stays unsized in sway.conf (see above), so on its own it would render
        # at wlroots' hardcoded 1024x768 default forever — a real, correctly-composited,
        # but pillarboxed picture inside the actual, larger TV output. The dump-env.sh
        # this test inspects is what session._await_nested_display() waits on before
        # launching the emulator, so the xdotool resize must run — and complete —
        # *inside* that same script, ahead of the readiness printf, or the emulator can
        # still win the race and render its first frame at the wrong size regardless.
        with mock.patch.object(
            runtime, "outer_gamescope_resolution", return_value=(1280, 800)
        ), mock.patch.object(runtime, "parent_display", return_value=("x11", ":1")):
            session = Session(profile=AZAHAR, command=["azahar"])
            artifacts = session.write_artifacts()

        script = artifacts["sway_env"].parent.joinpath("dump-env.sh").read_text()
        self.assertIn("xdotool", script)
        self.assertIn("DISPLAY=:1", script)
        resize_pos = script.index("xdotool windowsize")
        printf_pos = script.index("printf ")
        self.assertLess(resize_pos, printf_pos)

    def test_wayland_backend_gets_no_resize_script(self):
        # The native-Wayland fallback path uses a real "mode" line in sway.conf (WL-1
        # accepts commit-time modes fine), so it needs none of the X11 workaround.
        with mock.patch.object(
            runtime, "outer_gamescope_resolution", return_value=(1280, 800)
        ), mock.patch.object(runtime, "parent_display", return_value=("wayland", "gamescope-0")):
            session = Session(profile=AZAHAR, command=["azahar"])
            artifacts = session.write_artifacts()

        script = artifacts["sway_env"].parent.joinpath("dump-env.sh").read_text()
        self.assertNotIn("xdotool", script)


class PatchConfigsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        env = {
            "HOME": str(self.root),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
        }
        patcher = mock.patch.dict("os.environ", env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_creates_the_cemu_gamepad_profile_before_pointing_settings_xml_at_it(self):
        from sdss.profiles import CEMU

        cemu_config = self.root / ".config" / "Cemu"
        cemu_config.mkdir(parents=True)
        (cemu_config / "settings.xml").write_text(
            "<content><open_pad>false</open_pad><controllerProfile></controllerProfile>"
            "</content>"
        )

        with mock.patch.dict(os.environ, {"SDSS_CEMU_GAMEPAD_PROFILE": "DeckGamePad"}):
            session = Session(profile=CEMU, command=["cemu"])
            changed = session.patch_configs()

        profile_file = cemu_config / "controllerProfiles" / "DeckGamePad.txt"
        self.assertIn(profile_file, changed)
        self.assertIn("emulate = Wii U GamePad", profile_file.read_text())
        settings = (cemu_config / "settings.xml").read_text()
        self.assertIn("DeckGamePad", settings)

        session.journal.restore_snapshots()
        self.assertFalse(profile_file.exists())

    def test_does_not_overwrite_an_existing_gamepad_profile(self):
        from sdss.profiles import CEMU

        cemu_config = self.root / ".config" / "Cemu"
        (cemu_config / "controllerProfiles").mkdir(parents=True)
        (cemu_config / "settings.xml").write_text(
            "<content><open_pad>false</open_pad></content>"
        )
        profile_file = cemu_config / "controllerProfiles" / "DeckGamePad.txt"
        profile_file.write_text("user's own mapping")

        with mock.patch.dict(os.environ, {"SDSS_CEMU_GAMEPAD_PROFILE": "DeckGamePad"}):
            session = Session(profile=CEMU, command=["cemu"])
            changed = session.patch_configs()

        self.assertNotIn(profile_file, changed)
        self.assertEqual(profile_file.read_text(), "user's own mapping")

        session.journal.restore_snapshots()
        self.assertEqual(profile_file.read_text(), "user's own mapping")


class SunshinePinFifoTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        env = {
            "XDG_RUNTIME_DIR": str(root / "runtime"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_DATA_HOME": str(root / "data"),
        }
        patcher = mock.patch.dict("os.environ", env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_pin_fifo_keeps_child_stdin_blocking(self):
        session = Session(profile=AZAHAR, command=["azahar"])
        keepalive_fd, child_fd = session._pin_fifo()
        self.addCleanup(lambda: os.close(keepalive_fd))
        self.addCleanup(lambda: os.close(child_fd))
        self.addCleanup(lambda: (session.runtime / "pin").unlink(missing_ok=True))

        keepalive_flags = fcntl.fcntl(keepalive_fd, fcntl.F_GETFL)
        child_flags = fcntl.fcntl(child_fd, fcntl.F_GETFL)
        self.assertTrue(keepalive_flags & os.O_NONBLOCK)
        self.assertFalse(child_flags & os.O_NONBLOCK)

    def test_start_keeps_fifo_writer_until_cleanup(self):
        session = Session(profile=AZAHAR, command=["azahar"])
        fake_proc = mock.Mock()
        fake_proc.poll.return_value = 0
        with mock.patch("sdss.session.stream.default_spec"), mock.patch(
            "sdss.session.stream.launch_command", return_value=["sunshine"]
        ), mock.patch("sdss.session.subprocess.Popen", return_value=fake_proc) as popen:
            session._start_sunshine("wayland-1")

        keepalive_fd = session._pin_keepalive_fd
        self.assertIsNotNone(keepalive_fd)
        child_fd = popen.call_args.kwargs["stdin"]
        with self.assertRaises(OSError):
            fcntl.fcntl(child_fd, fcntl.F_GETFL)
        self.assertIsNotNone(fcntl.fcntl(keepalive_fd, fcntl.F_GETFL))
        session.cleanup()
        with self.assertRaises(OSError):
            fcntl.fcntl(keepalive_fd, fcntl.F_GETFL)


if __name__ == "__main__":
    unittest.main()
