import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sdss import runtime


class CompositorCommandTests(unittest.TestCase):
    def test_container_shares_host_pid_namespace(self):
        # Gamescope dismisses its loading spinner by walking the process tree/cgroup of the
        # game it launched. If sway runs in podman's own default pid namespace, it is
        # invisible to that walk and gamescope hangs on the spinner forever even though the
        # compositor and emulator are running correctly. --pid=host keeps sway (and
        # everything it spawns) inside the tree Steam launched.
        with mock.patch.object(runtime, "native_sway", return_value=None), mock.patch.object(
            runtime, "podman_available", return_value=True
        ), mock.patch.object(runtime, "own_cgroup", return_value=None):
            command = runtime.compositor_command(
                Path("/run/user/1000/sdss/session/sway.conf"),
                Path("/run/user/1000"),
                home=Path("/home/deck"),
            )
        self.assertIn("--pid=host", command)
        self.assertIn("--cgroupns=host", command)

    def test_container_nests_under_steams_cgroup_scope(self):
        # --cgroupns=host only shares the cgroup *namespace view*; podman still forks the
        # container into its own libpod-*.scope by default, which is a sibling (not a
        # descendant) of the app-steam-app<id>-<pid>.scope Steam creates. Gamescope's
        # readiness walk never finds it there, so --cgroup-parent must explicitly nest the
        # container under the scope this process (the whole sdss run chain) already lives in.
        with mock.patch.object(runtime, "native_sway", return_value=None), mock.patch.object(
            runtime, "podman_available", return_value=True
        ), mock.patch.object(
            runtime,
            "own_cgroup",
            return_value="user.slice/user-1000.slice/user@1000.service/app.slice/"
            "app-steam-app3216885850-80042.scope",
        ):
            command = runtime.compositor_command(
                Path("/run/user/1000/sdss/session/sway.conf"),
                Path("/run/user/1000"),
                home=Path("/home/deck"),
            )
        self.assertIn(
            "--cgroup-parent=/user.slice/user-1000.slice/user@1000.service/app.slice/"
            "app-steam-app3216885850-80042.scope",
            command,
        )
        # podman's default systemd cgroup manager rejects a --cgroup-parent that is a
        # *.scope with "did not receive systemd slice as cgroup parent" (exit 125), so the
        # cgroupfs manager must be selected — and as a global flag, before "run".
        self.assertIn("--cgroup-manager=cgroupfs", command)
        self.assertLess(command.index("--cgroup-manager=cgroupfs"), command.index("run"))


class OuterGamescopeResolutionTests(unittest.TestCase):
    def test_reads_width_and_height_from_gamescope_cmdline(self):
        # `WL-1` in the nested sway is a Wayland window inside the *outer* gamescope, not a
        # real display — it only ever accepts a mode matching that outer session's own
        # -w/-h. Some SteamOS images fix this at 1280x800 (a Deck panel resolution), not
        # 1920x1080; requesting the wrong size makes sway mark the output power:false after
        # a failed modeset, so it never commits a frame and gamescope's spinner never
        # dismisses even though the rest of the session is running fine.
        entries = ["1", "42", "not-a-pid"]
        cmdlines = {
            "1": b"/sbin/init\x00splash\x00",
            "42": b"gamescope\x00--generate-drm-mode\x00fixed\x00-w\x001280\x00-h\x00800\x00-e\x00",
        }

        def fake_read_bytes(self):
            return cmdlines[self.parts[-2]]

        with mock.patch("os.listdir", return_value=entries), mock.patch.object(
            Path, "read_bytes", fake_read_bytes
        ):
            self.assertEqual(runtime.outer_gamescope_resolution(), (1280, 800))

    def test_returns_none_when_no_gamescope_process_found(self):
        with mock.patch("os.listdir", return_value=["1"]), mock.patch.object(
            Path, "read_bytes", return_value=b"/sbin/init\x00splash\x00"
        ):
            self.assertIsNone(runtime.outer_gamescope_resolution())


class ParentDisplayTests(unittest.TestCase):
    def test_prefers_x11_display_steam_provides_per_game(self):
        # Gamescope hands the launched game DISPLAY=:N (its own isolated per-game
        # Xwayland, STEAM_MULTIPLE_XWAYLANDS=1) but never WAYLAND_DISPLAY. steamcompmgr's
        # spinner-dismissal walk is scoped to that display, so it must win whenever set,
        # even if some Wayland variable also happens to be present.
        with mock.patch.dict(
            "os.environ",
            {"DISPLAY": ":1", "GAMESCOPE_WAYLAND_DISPLAY": "gamescope-0"},
            clear=True,
        ):
            self.assertEqual(runtime.parent_display(), ("x11", ":1"))

    def test_falls_back_to_gamescope_wayland_display(self):
        with mock.patch.dict(
            "os.environ", {"GAMESCOPE_WAYLAND_DISPLAY": "gamescope-0"}, clear=True
        ):
            self.assertEqual(runtime.parent_display(), ("wayland", "gamescope-0"))

    def test_falls_back_to_wayland_display_over_the_gamescope_specific_one(self):
        with mock.patch.dict(
            "os.environ",
            {"WAYLAND_DISPLAY": "wayland-3", "GAMESCOPE_WAYLAND_DISPLAY": "gamescope-0"},
            clear=True,
        ):
            self.assertEqual(runtime.parent_display(), ("wayland", "wayland-3"))

    def test_defaults_to_gamescope_0_when_nothing_is_set(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(runtime.parent_display(), ("wayland", "gamescope-0"))


if __name__ == "__main__":
    unittest.main()
