import contextlib
import fcntl
import os
import signal
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sdss import runtime
from sdss import session as session_module
from sdss.profiles import AZAHAR, CEMU
from sdss.session import Session, SessionError, SessionInterrupted


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

    def test_headless_output_is_always_the_decks_panel_resolution(self):
        # HEADLESS-1 must match Moonlight's requested Deck panel resolution. The emulator's
        # native second-screen size is not the streamed output size.
        with mock.patch.object(
            runtime, "outer_gamescope_resolution", return_value=(1280, 800)
        ), mock.patch.object(runtime, "parent_display", return_value=("wayland", "gamescope-0")):
            session = Session(profile=AZAHAR, command=["azahar"])
            artifacts = session.write_artifacts()

        config = artifacts["sway_config"].read_text()
        self.assertIn("output HEADLESS-1 mode 1280x800@60Hz", config)

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
        with mock.patch.dict(
            os.environ,
            {
                "LD_PRELOAD": "/steam/gameoverlayrenderer.so",
                "SDSS_EMULATOR_LD_PRELOAD": "/steam/gameoverlayrenderer.so",
            },
        ), mock.patch("sdss.session.stream.default_spec"), mock.patch(
            "sdss.session.stream.launch_command", return_value=["sunshine"]
        ), mock.patch("sdss.session.subprocess.Popen", return_value=fake_proc) as popen:
            session._start_sunshine("wayland-1")

        keepalive_fd = session._pin_keepalive_fd
        self.assertIsNotNone(keepalive_fd)
        helper_env = popen.call_args.kwargs["env"]
        self.assertNotIn("LD_PRELOAD", helper_env)
        self.assertNotIn("SDSS_EMULATOR_LD_PRELOAD", helper_env)
        child_fd = popen.call_args.kwargs["stdin"]
        with self.assertRaises(OSError):
            fcntl.fcntl(child_fd, fcntl.F_GETFL)
        self.assertIsNotNone(fcntl.fcntl(keepalive_fd, fcntl.F_GETFL))
        session.cleanup()
        with self.assertRaises(OSError):
            fcntl.fcntl(keepalive_fd, fcntl.F_GETFL)


class CleanupContainerTests(unittest.TestCase):
    """cleanup() must reap the container, not just its own children.

    Verified on hardware: leftover conmon/fuse-overlayfs/Xwayland kept Steam's per-game
    cgroup scope populated, so the next launch failed with "Game already running".
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        patcher = mock.patch.dict(
            "os.environ",
            {
                "XDG_RUNTIME_DIR": str(root / "runtime"),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_DATA_HOME": str(root / "data"),
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_cleanup_removes_the_compositor_container(self):
        session = Session(profile=AZAHAR, command=["azahar"])
        with mock.patch("sdss.session.runtime.native_sway", return_value=None), mock.patch(
            "sdss.session.runtime.remove_container"
        ) as remove:
            session.cleanup()
        remove.assert_called_once()

    def test_cleanup_skips_podman_when_sway_is_native(self):
        session = Session(profile=AZAHAR, command=["azahar"])
        with mock.patch("sdss.session.runtime.native_sway", return_value="/usr/bin/sway"), (
            mock.patch("sdss.session.runtime.remove_container")
        ) as remove:
            session.cleanup()
        remove.assert_not_called()

    def test_cleanup_survives_a_failing_podman(self):
        # cleanup() runs from a `finally`; teardown must never mask the real error.
        session = Session(profile=AZAHAR, command=["azahar"])
        with mock.patch("sdss.session.runtime.native_sway", return_value=None), mock.patch(
            "sdss.session.runtime.remove_container", side_effect=OSError("no podman")
        ):
            session.cleanup()

    def test_cleanup_terminates_process_groups(self):
        session = Session(profile=AZAHAR, command=["azahar"])
        process = mock.Mock()
        process.poll.return_value = None
        process.pid = 1234
        session._processes.append(process)
        with mock.patch("sdss.session.os.killpg") as killpg, mock.patch(
            "sdss.session.runtime.native_sway", return_value="/usr/bin/sway"
        ):
            session.cleanup()
        killpg.assert_called_once_with(1234, signal.SIGTERM)
        process.wait.assert_called_once_with(timeout=5)

    def test_cleanup_sends_sigterm_first_to_the_emulator(self):
        """The emulator now gets a graceful SIGTERM, not an immediate SIGKILL.

        Reversed from an earlier hardware-driven hypothesis: an immediate SIGKILL to
        the emulator was implicated in a reproducible sway/Xwayland SIGBUS (it denies
        the emulator any chance to release its GPU/X11 resources first). A later,
        distinct hardware finding then confirmed that killing the compositor before the
        emulator instead makes the *emulator* (Azahar) SIGABRT when its Xwayland
        connection disappears while it's still alive. A graceful SIGTERM to the
        emulator, awaited before the compositor is touched at all, is the one
        combination not yet falsified — see cleanup()'s own docstring for the full
        history.
        """
        session = Session(profile=AZAHAR, command=["azahar"])
        emulator = mock.Mock()
        emulator.poll.return_value = None
        emulator.pid = 4321
        session._processes.append(emulator)
        session._emulator_proc = emulator
        with mock.patch("sdss.session.os.killpg") as killpg, mock.patch(
            "sdss.session.runtime.native_sway", return_value="/usr/bin/sway"
        ):
            session.cleanup()
        killpg.assert_any_call(4321, signal.SIGTERM)

    def test_cleanup_sends_sigterm_to_both_emulator_and_compositor(self):
        """Both the emulator and sway now get a graceful SIGTERM."""
        session = Session(profile=AZAHAR, command=["azahar"])
        sway_proc = mock.Mock()
        sway_proc.poll.return_value = None
        sway_proc.pid = 1111
        emulator = mock.Mock()
        emulator.poll.return_value = None
        emulator.pid = 2222
        session._processes.extend([sway_proc, emulator])
        session._emulator_proc = emulator
        with mock.patch("sdss.session.os.killpg") as killpg, mock.patch(
            "sdss.session.runtime.native_sway", return_value="/usr/bin/sway"
        ):
            session.cleanup()
        killpg.assert_any_call(1111, signal.SIGTERM)
        killpg.assert_any_call(2222, signal.SIGTERM)

    def test_cleanup_removes_container_before_touching_podman_wrapper(self):
        """The local podman child must not be signaled before rootfs clients are reaped.

        Verified on hardware: Steam overlay Exit Game sends SIGINT to SDSS, cleanup() used
        to SIGTERM the tracked `podman run` process before calling remove_container(). With
        `--rm`, that let conmon start tearing down fuse-overlayfs while sway, Xwayland and
        sdss_inputd were still demand-paging from it, reproducing the triple SIGBUS even
        though remove_container() itself had the correct internal ordering. The emulator is
        now terminated (gracefully) before any of this, per cleanup()'s current ordering.
        """
        session = Session(profile=AZAHAR, command=["azahar"])
        compositor = mock.Mock()
        compositor.poll.return_value = None
        compositor.pid = 1111
        emulator = mock.Mock()
        emulator.poll.return_value = None
        emulator.pid = 2222
        session._processes.extend([compositor, emulator])
        session._compositor_proc = compositor
        session._emulator_proc = emulator
        calls: list[tuple[str, int | None, bool | None]] = []

        def terminate(proc, *, graceful=True):
            calls.append(("terminate", proc.pid, graceful))
            proc.poll.return_value = 0

        with mock.patch("sdss.session.runtime.native_sway", return_value=None), mock.patch(
            "sdss.session.runtime.remove_container",
            side_effect=lambda: calls.append(("remove_container", None, None)),
        ), mock.patch.object(Session, "_terminate_process", side_effect=terminate):
            session.cleanup()

        self.assertEqual(
            calls,
            [
                ("terminate", 2222, True),
                ("remove_container", None, None),
                ("terminate", 1111, False),
            ],
        )

    def test_cleanup_kills_emulator_before_the_compositor(self):
        """The emulator must be dead *before* the compositor is torn down.

        Verified on hardware, 100%-reproducible: killing the compositor while the
        emulator is still alive makes the emulator itself (Azahar) SIGABRT when its
        Xwayland connection disappears out from under it — confirmed via coredump
        (signal 6, "X11 connection broke") at the exact moment reap_orphaned_helpers()
        kills Xwayland. That crashed exit is also what stalls Steam's own game-process
        reaper for tens of seconds afterward (the "long delay exiting a game" symptom).
        An earlier ordering (emulator killed first, but with an *immediate* SIGKILL)
        was tried and separately falsified: it does not give the emulator any chance to
        release its GPU/X11 resources first, and was implicated in a sway/Xwayland
        SIGBUS. The combination here — emulator first, but with a graceful SIGTERM the
        code waits out — is the one not yet falsified. Assert the order directly:
        emulator (graceful), then compositor, then Sunshine/anything else.
        """
        session = Session(profile=AZAHAR, command=["azahar"])
        compositor = mock.Mock()
        compositor.poll.return_value = None
        compositor.pid = 1111
        sunshine = mock.Mock()
        sunshine.poll.return_value = None
        sunshine.pid = 3333
        emulator = mock.Mock()
        emulator.poll.return_value = None
        emulator.pid = 2222
        # Startup order: compositor, sunshine, emulator (matches _start_sway/_start_sunshine/
        # _start_emulator's own append order).
        session._processes.extend([compositor, sunshine, emulator])
        session._compositor_proc = compositor
        session._emulator_proc = emulator
        calls: list[tuple[str, int | None, bool | None]] = []

        def terminate(proc, *, graceful=True):
            calls.append(("terminate", proc.pid, graceful))
            proc.poll.return_value = 0

        with mock.patch("sdss.session.runtime.native_sway", return_value=None), mock.patch(
            "sdss.session.runtime.remove_container",
            side_effect=lambda: calls.append(("remove_container", None, None)),
        ), mock.patch.object(Session, "_terminate_process", side_effect=terminate):
            session.cleanup()

        self.assertEqual(
            calls,
            [
                ("terminate", 2222, True),
                ("remove_container", None, None),
                ("terminate", 1111, False),
                ("terminate", 3333, True),
            ],
        )

    def test_terminate_process_kills_descendants_outside_process_group(self):
        process = mock.Mock()
        process.pid = 1234
        process.wait.return_value = None
        with mock.patch.object(Session, "_descendant_pids", return_value=[5678]), mock.patch(
            "sdss.session.os.kill"
        ) as kill, mock.patch("sdss.session.os.killpg"):
            Session._terminate_process(process)
        kill.assert_any_call(5678, signal.SIGTERM)
        kill.assert_any_call(5678, signal.SIGKILL)

    def test_terminate_process_graceful_false_never_sends_sigterm(self):
        """graceful=False must not give the target any chance to react to SIGTERM."""
        process = mock.Mock()
        process.pid = 1234
        process.wait.return_value = None
        with mock.patch.object(Session, "_descendant_pids", return_value=[5678]), mock.patch(
            "sdss.session.os.kill"
        ) as kill, mock.patch("sdss.session.os.killpg") as killpg:
            Session._terminate_process(process, graceful=False)
        kill.assert_any_call(5678, signal.SIGKILL)
        killpg.assert_called_once_with(1234, signal.SIGKILL)
        for call in kill.call_args_list:
            self.assertNotEqual(call.args[1], signal.SIGTERM)
        for call in killpg.call_args_list:
            self.assertNotEqual(call.args[1], signal.SIGTERM)

    def test_start_sway_force_removes_a_stale_container_first(self):
        """`--replace` alone is not enough when the previous conmon died unclean.

        Verified on hardware: podman reported "conmon exited prematurely" and the new
        container died ~16ms in, so sway never wrote its env dump and the launch failed
        with "nested compositor did not report its Wayland socket".
        """
        session = Session(profile=AZAHAR, command=["azahar"])
        order = []
        with mock.patch(
            "sdss.session.runtime.parent_display", return_value=("wayland", "wayland-0")
        ), mock.patch(
            "sdss.session.runtime.compositor_command", return_value=["podman", "run"]
        ), mock.patch(
            "sdss.session.runtime.remove_container",
            side_effect=lambda *a, **k: order.append("remove"),
        ), mock.patch(
            "sdss.session.subprocess.Popen",
            side_effect=lambda *a, **k: order.append("run") or mock.MagicMock(),
        ):
            session._start_sway(Path("/tmp/sway.conf"))
        self.assertEqual(order, ["remove", "run"])

    def test_start_sway_prepares_the_x11_bridge_before_running_the_container(self):
        """The bridge dir must exist, and be ours, before podman mounts it.

        Verified on hardware: with the host's root-owned /tmp/.X11-unix mounted straight
        through, wlroots logged "/tmp/.X11-unix not owned by root or us" for all 33 display
        slots and gave up ("Failed to start Xwayland"), so DISPLAY came back empty and Cemu
        rendered nothing.
        """
        session = Session(profile=AZAHAR, command=["azahar"])
        order = []
        with mock.patch(
            "sdss.session.runtime.parent_display", return_value=("wayland", "wayland-0")
        ), mock.patch(
            "sdss.session.runtime.compositor_command", return_value=["podman", "run"]
        ), mock.patch("sdss.session.runtime.remove_container"), mock.patch(
            "sdss.session.runtime.prepare_x11_bridge",
            side_effect=lambda *a, **k: order.append("bridge"),
        ), mock.patch(
            "sdss.session.subprocess.Popen",
            side_effect=lambda *a, **k: order.append("run") or mock.MagicMock(),
        ):
            session._start_sway(Path("/tmp/sway.conf"))
        self.assertEqual(order, ["bridge", "run"])

    def test_start_sway_does_not_inherit_steam_overlay(self):
        session = Session(profile=AZAHAR, command=["azahar"])
        process = mock.MagicMock()
        with mock.patch.dict(
            os.environ,
            {
                "LD_PRELOAD": "/steam/gameoverlayrenderer.so",
                "SDSS_EMULATOR_LD_PRELOAD": "/steam/gameoverlayrenderer.so",
            },
        ), mock.patch(
            "sdss.session.runtime.parent_display", return_value=("x11", ":1")
        ), mock.patch(
            "sdss.session.runtime.compositor_command", return_value=["podman", "run"]
        ), mock.patch(
            "sdss.session.runtime.remove_container"
        ), mock.patch(
            "sdss.session.runtime.prepare_x11_bridge"
        ), mock.patch(
            "sdss.session.subprocess.Popen", return_value=process
        ) as popen:
            session._start_sway(Path("/tmp/sway.conf"))

        helper_env = popen.call_args.kwargs["env"]
        self.assertNotIn("LD_PRELOAD", helper_env)
        self.assertNotIn("SDSS_EMULATOR_LD_PRELOAD", helper_env)

    def test_start_sway_disables_coredumps_in_the_child(self):
        """Sway execs Xwayland and sdss-inputd; RLIMIT_CORE=0 here covers both via
        inheritance, so every process in the crash cascade stops generating a
        systemd-coredump/minidump/DrKonqi journal burst on top of itself."""
        session = Session(profile=AZAHAR, command=["azahar"])
        process = mock.MagicMock()
        with mock.patch(
            "sdss.session.runtime.parent_display", return_value=("x11", ":1")
        ), mock.patch(
            "sdss.session.runtime.compositor_command", return_value=["podman", "run"]
        ), mock.patch(
            "sdss.session.runtime.remove_container"
        ), mock.patch(
            "sdss.session.runtime.prepare_x11_bridge"
        ), mock.patch(
            "sdss.session.subprocess.Popen", return_value=process
        ) as popen:
            session._start_sway(Path("/tmp/sway.conf"))

        self.assertIs(popen.call_args.kwargs["preexec_fn"], session_module._disable_coredumps)


class SessionLockTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        patcher = mock.patch.dict(
            "os.environ",
            {
                "XDG_RUNTIME_DIR": str(root / "runtime"),
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_DATA_HOME": str(root / "data"),
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_second_session_is_rejected(self):
        first = Session(profile=AZAHAR, command=["azahar"])
        second = Session(profile=CEMU, command=["cemu"])
        with first._session_lock():
            with self.assertRaisesRegex(SessionError, "another SDSS emulator session"):
                with second._session_lock():
                    pass

    def test_lock_is_released_after_session(self):
        first = Session(profile=AZAHAR, command=["azahar"])
        second = Session(profile=CEMU, command=["cemu"])
        with first._session_lock():
            pass
        with second._session_lock():
            pass

    def test_sigterm_enters_cleanup_path_and_handlers_are_restored(self):
        session = Session(profile=AZAHAR, command=["azahar"])
        original = signal.getsignal(signal.SIGTERM)
        session._install_signal_handlers()
        self.addCleanup(session._restore_signal_handlers)

        with self.assertRaises(SessionInterrupted):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        session._restore_signal_handlers()
        self.assertIs(signal.getsignal(signal.SIGTERM), original)

    def test_second_signal_does_not_interrupt_cleanup(self):
        session = Session(profile=AZAHAR, command=["azahar"])
        session._install_signal_handlers()
        self.addCleanup(session._restore_signal_handlers)
        handler = signal.getsignal(signal.SIGTERM)

        with self.assertRaises(SessionInterrupted):
            handler(signal.SIGTERM, None)
        handler(signal.SIGTERM, None)

    def test_run_disarms_parent_watch_before_cleanup(self):
        session = Session(profile=AZAHAR, command=["azahar"])
        order = []
        with mock.patch.object(
            session, "_session_lock", return_value=contextlib.nullcontext()
        ), mock.patch.object(
            session, "_install_signal_handlers"
        ), mock.patch.object(
            session, "_restore_signal_handlers"
        ), mock.patch.object(
            session,
            "write_artifacts",
            return_value={"sway_config": Path("/tmp/sway.conf")},
        ), mock.patch.object(
            session, "_start_sway", side_effect=SessionError("boom")
        ), mock.patch.object(
            session, "cleanup", side_effect=lambda: order.append("cleanup")
        ), mock.patch.object(
            runtime, "arm_parent_death_signal"
        ), mock.patch.object(
            runtime,
            "disarm_parent_death_watch",
            side_effect=lambda: order.append("disarm"),
        ):
            with self.assertRaisesRegex(SessionError, "boom"):
                session.run()

        self.assertEqual(order, ["disarm", "cleanup"])


class EmulatorLaunchBackendTests(unittest.TestCase):
    def test_azahar_is_started_on_the_nested_xwayland(self):
        session = Session(profile=AZAHAR, command=["azahar", "game.cci"])
        process = mock.Mock()
        with mock.patch.dict(
            os.environ,
            {
                "SDSS_EMULATOR_LD_PRELOAD": "/steam/gameoverlayrenderer.so",
            },
            clear=True,
        ), mock.patch("sdss.session.subprocess.Popen", return_value=process) as popen:
            session._start_emulator({"WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":2"})

        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["DISPLAY"], ":2")
        self.assertEqual(env["QT_QPA_PLATFORM"], "xcb")
        self.assertEqual(env["GDK_BACKEND"], "x11")
        # Verified on hardware: Azahar's Steam overlay is enabled again (profiles.py's
        # AZAHAR.notes) now that the OpenGL override and the graceful compositor teardown
        # in runtime.py address the reasons it was previously disabled.
        self.assertEqual(env["LD_PRELOAD"], "/steam/gameoverlayrenderer.so")
        self.assertNotIn("SDSS_EMULATOR_LD_PRELOAD", env)

    def test_emulator_disables_coredumps(self):
        session = Session(profile=AZAHAR, command=["azahar", "game.cci"])
        process = mock.Mock()
        with mock.patch("sdss.session.subprocess.Popen", return_value=process) as popen:
            session._start_emulator({"WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":2"})
        self.assertIs(popen.call_args.kwargs["preexec_fn"], session_module._disable_coredumps)

    def test_cemu_keeps_steam_overlay(self):
        session = Session(profile=CEMU, command=["cemu", "game.wua"])
        process = mock.Mock()
        with mock.patch.dict(
            os.environ,
            {"SDSS_EMULATOR_LD_PRELOAD": "/steam/gameoverlayrenderer.so"},
            clear=True,
        ), mock.patch("sdss.session.subprocess.Popen", return_value=process) as popen:
            session._start_emulator({"WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":2"})
        self.assertEqual(
            popen.call_args.kwargs["env"]["LD_PRELOAD"],
            "/steam/gameoverlayrenderer.so",
        )


class AwaitNestedDisplayTests(unittest.TestCase):
    """DISPLAY can lag WAYLAND_DISPLAY in the env dump; needs_x11 profiles must wait.

    Verified on hardware: sway wrote the dump with an empty DISPLAY and Cemu failed with
    "needs Xwayland but the compositor reported no DISPLAY" while sway and Sunshine were
    both healthy — a black second screen and no emulator.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.env_file = Path(self._tmp.name) / "sway-env"

    def test_waits_for_display_when_the_profile_needs_x11(self):
        self.assertTrue(CEMU.needs_x11)
        session = Session(profile=CEMU, command=["cemu"])
        # _await_nested_display() unlinks the file first, so the dump has to appear
        # during polling — same as sway writing it after the compositor comes up.
        writes = iter(
            [
                "WAYLAND_DISPLAY=wayland-1\nDISPLAY=\n",
                "WAYLAND_DISPLAY=wayland-1\nDISPLAY=:2\n",
            ]
        )

        def advance(_):
            self.env_file.write_text(next(writes))

        with mock.patch("sdss.session.time.sleep", side_effect=advance):
            values = session._await_nested_display(self.env_file)
        self.assertEqual(values["DISPLAY"], ":2")

    def test_returns_immediately_for_a_wayland_only_profile(self):
        profile = replace(AZAHAR, needs_x11=False)
        session = Session(profile=profile, command=["azahar"])
        calls = []

        def advance(_):
            calls.append(1)
            self.env_file.write_text("WAYLAND_DISPLAY=wayland-1\nDISPLAY=\n")

        with mock.patch("sdss.session.time.sleep", side_effect=advance):
            values = session._await_nested_display(self.env_file)
        self.assertEqual(values["WAYLAND_DISPLAY"], "wayland-1")
        # A blank DISPLAY must not hold up a profile that never asked for Xwayland.
        self.assertEqual(len(calls), 1)

    def test_times_out_when_xwayland_never_appears(self):
        session = Session(profile=CEMU, command=["cemu"])

        def advance(_):
            self.env_file.write_text("WAYLAND_DISPLAY=wayland-1\nDISPLAY=\n")

        with mock.patch("sdss.session.time.sleep", side_effect=advance), mock.patch(
            "sdss.session.time.monotonic", side_effect=[0.0, 1.0, 1e6]
        ), self.assertRaises(SessionError):
            session._await_nested_display(self.env_file)


if __name__ == "__main__":
    unittest.main()
