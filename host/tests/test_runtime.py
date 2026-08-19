import os
import sys
import tempfile
import threading
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

    def test_container_does_not_share_the_host_ipc_namespace(self):
        # Every other namespace-sharing flag on this podman run has a comment tying it to a
        # specific, verified hardware bug (spinner dismissal, the X11 bridge). --ipc=host had
        # none, and unlike those, it puts sway/the nested Xwayland into the same SysV/POSIX
        # IPC namespace Steam's own client (and its overlay IPC) lives in — the broadest,
        # least-justified grant of shared kernel state SDSS was making. Verified on hardware
        # (docs/redesign-plan.md, Phase 0) that Cemu/Azahar still render, the second screen
        # still streams, and audio still reaches the TV without it.
        with mock.patch.object(runtime, "native_sway", return_value=None), mock.patch.object(
            runtime, "podman_available", return_value=True
        ), mock.patch.object(runtime, "own_cgroup", return_value=None):
            command = runtime.compositor_command(
                Path("/run/user/1000/sdss/session/sway.conf"),
                Path("/run/user/1000"),
                home=Path("/home/deck"),
            )
        self.assertNotIn("--ipc=host", command)


class ParentLifecycleTests(unittest.TestCase):
    def test_parent_death_signal_is_registered_with_prctl(self):
        libc = mock.Mock()
        libc.prctl.return_value = 0
        with mock.patch.object(runtime.sys, "platform", "linux"), mock.patch.object(
            runtime.ctypes, "CDLL", return_value=libc
        ), mock.patch.object(runtime.os, "getppid", return_value=1234), mock.patch.object(
            runtime, "_arm_parent_lineage_watch"
        ):
            runtime.arm_parent_death_signal()
        libc.prctl.assert_called_once_with(
            runtime.PR_SET_PDEATHSIG, runtime.signal.SIGTERM, 0, 0, 0
        )

    def test_parent_watch_tracks_reaper_and_steam_client(self):
        parents = {1234: 2000, 2000: 3000, 3000: 1632, 1632: 1}
        names = {2000: "reaper", 3000: "steam", 1632: "systemd"}
        with mock.patch.object(
            runtime, "_parent_pid", side_effect=lambda pid: parents.get(pid)
        ), mock.patch.object(
            runtime, "_process_name", side_effect=lambda pid: names.get(pid)
        ):
            watched = runtime._watched_parent_pids(1234)
        self.assertEqual(watched, (2000, 3000))

    def test_parent_watch_falls_back_to_reaper_off_steam(self):
        parents = {1234: 2000, 2000: 1632, 1632: 1}
        with mock.patch.object(
            runtime, "_parent_pid", side_effect=lambda pid: parents.get(pid)
        ), mock.patch.object(runtime, "_process_name", return_value=None):
            watched = runtime._watched_parent_pids(1234)
        self.assertEqual(watched, (2000,))

    def test_watch_thread_survives_disarm_racing_its_own_wait(self):
        """Regression: the watch loop re-read the *module-global* `_parent_watch_stop` on
        every iteration. disarm_parent_death_watch() reassigns that global to None (after
        signalling the event it still held a reference to) -- if that happened between one
        iteration's wait() returning and the loop's next while-check re-reading the global,
        the thread crashed with "AttributeError: 'NoneType' object has no attribute
        'is_set'". Verified on hardware during a real teardown. The fix binds the loop to a
        local reference captured once at thread start, immune to the global being reassigned
        later. This test forces disarm to fire in exactly that window, deterministically,
        rather than hoping to hit the real race by timing.
        """

        class RacingEvent(threading.Event):
            def wait(self, timeout=None):
                runtime.disarm_parent_death_watch()
                return super().wait(timeout)

        created_threads: list[threading.Thread] = []
        real_thread_cls = threading.Thread

        class CapturingThread(real_thread_cls):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                created_threads.append(self)

        captured: list[object] = []
        original_hook = threading.excepthook
        threading.excepthook = captured.append
        try:
            with mock.patch.object(runtime.threading, "Event", RacingEvent), mock.patch.object(
                runtime.threading, "Thread", CapturingThread
            ), mock.patch.object(runtime, "_watched_parent_pids", return_value=(os.getpid(),)):
                runtime._arm_parent_lineage_watch(1234)
            self.assertEqual(len(created_threads), 1)
            thread = created_threads[0]
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive(), "watch thread should have stopped cleanly")
            self.assertEqual(captured, [], "watch thread must not raise on disarm")
        finally:
            threading.excepthook = original_hook
            runtime._parent_watch_stop = None


class X11BridgeTests(unittest.TestCase):
    def test_bridge_dir_is_ours_and_links_back_to_the_host_sockets(self):
        # The host's /tmp/.X11-unix is root-owned. Under --userns=keep-id host uid 0 maps
        # to nobody, so wlroots rejects it ("not owned by root or us"), Xwayland never
        # starts, DISPLAY comes back empty and needs_x11 profiles get a black screen.
        # Verified on hardware. The bridge is a directory we own, with the host's sockets
        # symlinked to their path under the second mount so they still resolve outward.
        with tempfile.TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp) / "run"
            host_x11 = Path(tmp) / "host-x11"
            host_x11.mkdir()
            (host_x11 / "X0").touch()
            (host_x11 / "X1").touch()
            (host_x11 / "not-a-socket").touch()
            with mock.patch.object(runtime, "HOST_X11_DIR", host_x11):
                bridge = runtime.prepare_x11_bridge(runtime_dir)

            self.assertNotEqual(bridge, host_x11)
            self.assertEqual(sorted(p.name for p in bridge.iterdir()), ["X0", "X1"])
            self.assertEqual(
                os.readlink(bridge / "X0"),
                str(runtime.CONTAINER_HOST_X11_DIR / "X0"),
            )

            # Re-running must not accumulate or trip over the previous links.
            with mock.patch.object(runtime, "HOST_X11_DIR", host_x11):
                runtime.prepare_x11_bridge(runtime_dir)
            self.assertEqual(sorted(p.name for p in bridge.iterdir()), ["X0", "X1"])

    def test_command_mounts_the_bridge_over_the_x11_dir(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            runtime, "native_sway", return_value=None
        ), mock.patch.object(runtime, "podman_available", return_value=True), mock.patch.object(
            runtime, "own_cgroup", return_value=None
        ):
            command = runtime.compositor_command(
                Path("/run/user/1000/sdss/session/sway.conf"),
                Path(tmp),
                home=Path("/home/deck"),
            )
        # The straight passthrough is what broke Xwayland; it must not come back.
        self.assertNotIn("--volume=/tmp/.X11-unix:/tmp/.X11-unix", command)
        self.assertIn(f"--volume={tmp}/sdss/x11:/tmp/.X11-unix", command)
        # ...and the real directory still has to be reachable for the symlinks to resolve.
        self.assertIn(
            f"--volume={runtime.HOST_X11_DIR}:{runtime.CONTAINER_HOST_X11_DIR}", command
        )


class ContainerTeardownTests(unittest.TestCase):
    """Steam refuses the next launch while anything survives in its per-game cgroup scope.

    Verified on hardware: after a session, conmon, fuse-overlayfs and the nested Xwayland
    were still in app-steam-app<id>-<pid>.scope and Steam reported "Game already running".
    Terminating the `podman run` child does not reap them -- they are not its children.
    """

    def test_container_is_named_so_teardown_can_reach_it(self):
        with mock.patch.object(runtime, "native_sway", return_value=None), mock.patch.object(
            runtime, "podman_available", return_value=True
        ), mock.patch.object(runtime, "own_cgroup", return_value=None):
            command = runtime.compositor_command(
                Path("/run/user/1000/sdss/session/sway.conf"),
                Path("/run/user/1000"),
                home=Path("/home/deck"),
            )
        self.assertIn(f"--name={runtime.CONTAINER_NAME}", command)
        # A crashed session leaves the container behind and podman refuses to reuse a name.
        self.assertIn("--replace", command)

    def test_remove_container_tries_graceful_term_before_force_kill(self):
        # A plain SIGKILL never lets sway close its X11 connection to gamescope's per-game
        # Xwayland cleanly — verified on hardware (docs/architecture.md) that the abrupt
        # severing is a plausible contributor to Steam-side corruption on the *next* launch.
        # TERM must be sent, and waited on, before the unconditional KILL/rm backstop.
        with mock.patch.object(runtime, "podman_available", return_value=True), mock.patch.object(
            runtime.subprocess, "run"
        ) as run:
            run.return_value = mock.Mock(returncode=0)
            self.assertTrue(runtime.remove_container())
        self.assertEqual(run.call_count, 4)

        term_argv = run.call_args_list[0][0][0]
        self.assertEqual(term_argv[:2], ["podman", "kill"])
        self.assertIn("--signal", term_argv)
        self.assertIn("TERM", term_argv)
        self.assertIn(runtime.CONTAINER_NAME, term_argv)

        wait_call = run.call_args_list[1]
        wait_argv = wait_call[0][0]
        self.assertEqual(wait_argv[:2], ["podman", "wait"])
        self.assertIn("--ignore", wait_argv)
        self.assertIn("stopped", wait_argv)
        self.assertIn(runtime.CONTAINER_NAME, wait_argv)
        # Must never block the caller indefinitely if the container refuses to stop.
        self.assertIn("timeout", wait_call[1])
        self.assertEqual(wait_call[1]["timeout"], runtime.GRACEFUL_STOP_TIMEOUT)

        kill_argv = run.call_args_list[2][0][0]
        self.assertEqual(kill_argv[:2], ["podman", "kill"])
        self.assertIn("KILL", kill_argv)

        rm_argv = run.call_args_list[3][0][0]
        self.assertEqual(rm_argv[:2], ["podman", "rm"])

    def test_remove_container_survives_a_graceful_wait_timeout(self):
        # A container that never stops on TERM must not hang the whole teardown — the
        # unconditional SIGKILL backstop still has to run.
        def run_side_effect(argv, **kwargs):
            if argv[:2] == ["podman", "wait"]:
                raise runtime.subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))
            return mock.Mock(returncode=0)

        with mock.patch.object(runtime, "podman_available", return_value=True), mock.patch.object(
            runtime.subprocess, "run", side_effect=run_side_effect
        ) as run:
            self.assertTrue(runtime.remove_container())
        argv_sequence = [call[0][0][:2] for call in run.call_args_list]
        self.assertEqual(
            argv_sequence,
            [["podman", "kill"], ["podman", "wait"], ["podman", "kill"], ["podman", "rm"]],
        )

    def test_remove_container_force_removes_by_name(self):
        with mock.patch.object(runtime, "podman_available", return_value=True), mock.patch.object(
            runtime.subprocess, "run"
        ) as run:
            run.return_value = mock.Mock(returncode=0)
            self.assertTrue(runtime.remove_container())
        self.assertEqual(run.call_count, 4)
        kill_argv = run.call_args_list[2][0][0]
        self.assertEqual(kill_argv[:2], ["podman", "kill"])
        self.assertIn("--signal", kill_argv)
        self.assertIn("KILL", kill_argv)
        self.assertIn(runtime.CONTAINER_NAME, kill_argv)
        self.assertNotIn("--ignore", kill_argv)
        argv = run.call_args_list[3][0][0]
        self.assertEqual(argv[:2], ["podman", "rm"])
        self.assertIn("--force", argv)
        # Removing an already-gone container must not be reported as a failure.
        self.assertIn("--ignore", argv)
        self.assertIn(runtime.CONTAINER_NAME, argv)

    def test_remove_container_is_a_noop_without_podman(self):
        with mock.patch.object(runtime, "podman_available", return_value=False), mock.patch.object(
            runtime.subprocess, "run"
        ) as run:
            self.assertFalse(runtime.remove_container())
        run.assert_not_called()

    def test_remove_container_reports_failure(self):
        with mock.patch.object(runtime, "podman_available", return_value=True), mock.patch.object(
            runtime.subprocess, "run"
        ) as run:
            run.return_value = mock.Mock(returncode=1)
            self.assertFalse(runtime.remove_container())

    def test_remove_container_reaps_orphaned_helpers(self):
        with mock.patch.object(runtime, "podman_available", return_value=True), mock.patch.object(
            runtime.subprocess, "run"
        ) as run, mock.patch.object(runtime, "reap_orphaned_helpers") as reap:
            run.return_value = mock.Mock(returncode=0)
            runtime.remove_container()
        reap.assert_called_once_with(runtime.CONTAINER_NAME)

    def test_reap_orphaned_helpers_kills_named_processes(self):
        with mock.patch.object(runtime.os, "listdir", return_value=["123", "456"]), mock.patch.object(
            runtime.os, "getpid", return_value=999
        ), mock.patch.object(
            runtime.Path,
            "read_bytes",
            side_effect=[b"/usr/bin/conmon\0-n\0sdss-compositor\0", b"python\0sdss_inputd.py\0"],
        ), mock.patch.object(runtime.os, "kill") as kill:
            runtime.reap_orphaned_helpers()
        self.assertEqual(
            kill.call_args_list,
            [mock.call(123, runtime.signal.SIGKILL), mock.call(456, runtime.signal.SIGKILL)],
        )

    def test_reap_orphaned_helpers_kills_stale_nested_graphics_helpers(self):
        commands = [
            b"/usr/bin/Xwayland\0:2\0-rootless\0-wm\041\0",
            b"/usr/bin/fuse-overlayfs\0upperdir=/home/deck/.local/share/containers/storage/overlay/abc/merged\0",
            b"/usr/bin/Xwayland\0:1\0-rootless\0",
            b"/usr/bin/fuse-overlayfs\0upperdir=/tmp/other-container\0",
        ]
        with mock.patch.object(runtime.os, "listdir", return_value=["123", "456", "789", "999"]), mock.patch.object(
            runtime.os, "getpid", return_value=1000
        ), mock.patch.object(runtime.Path, "read_bytes", side_effect=commands), mock.patch.object(
            runtime, "own_cgroup", return_value="user.slice/app-steam.scope"
        ), mock.patch.object(
            runtime,
            "_belongs_to_cgroup",
            side_effect=lambda pid, _parent: pid in (123, 456),
        ), mock.patch.object(
            runtime.os, "kill"
        ) as kill:
            runtime.reap_orphaned_helpers()
        self.assertEqual(
            kill.call_args_list,
            [mock.call(123, runtime.signal.SIGKILL), mock.call(456, runtime.signal.SIGKILL)],
        )

    def test_reap_orphaned_helpers_preserves_graphics_helpers_from_other_scope(self):
        commands = [
            b"/usr/bin/Xwayland\0:3\0-rootless\0-wm\041\0",
            b"/usr/bin/fuse-overlayfs\0upperdir=/home/deck/.local/share/containers/storage/overlay/other/merged\0",
        ]
        with mock.patch.object(
            runtime.os, "listdir", return_value=["123", "456"]
        ), mock.patch.object(
            runtime.os, "getpid", return_value=1000
        ), mock.patch.object(
            runtime.Path, "read_bytes", side_effect=commands
        ), mock.patch.object(
            runtime, "own_cgroup", return_value="user.slice/app-steam.scope"
        ), mock.patch.object(
            runtime, "_belongs_to_cgroup", return_value=False
        ), mock.patch.object(runtime.os, "kill") as kill:
            runtime.reap_orphaned_helpers()
        kill.assert_not_called()

    def test_reap_orphaned_helpers_kills_launch_owned_podman_pause(self):
        with mock.patch.object(runtime, "reap_orphaned_appimage_mounts"), mock.patch.object(
            runtime, "_ancestor_pids", return_value=(2000, 3000)
        ), mock.patch.object(
            runtime.os, "listdir", return_value=["123"]
        ), mock.patch.object(
            runtime.os, "getpid", return_value=1000
        ), mock.patch.object(
            runtime.Path, "read_bytes", return_value=b"catatonit\0-P\0"
        ), mock.patch.object(
            runtime, "_parent_pid", return_value=2000
        ), mock.patch.object(
            runtime, "_podman_pause_pid", return_value=123
        ), mock.patch.object(runtime.os, "kill") as kill:
            runtime.reap_orphaned_helpers()
        kill.assert_called_once_with(123, runtime.signal.SIGKILL)

    def test_reap_orphaned_helpers_preserves_unrelated_podman_pause(self):
        with mock.patch.object(runtime, "reap_orphaned_appimage_mounts"), mock.patch.object(
            runtime, "_ancestor_pids", return_value=(2000, 3000)
        ), mock.patch.object(
            runtime.os, "listdir", return_value=["123"]
        ), mock.patch.object(
            runtime.os, "getpid", return_value=1000
        ), mock.patch.object(
            runtime.Path, "read_bytes", return_value=b"catatonit\0-P\0"
        ), mock.patch.object(
            runtime, "_parent_pid", return_value=4000
        ), mock.patch.object(runtime, "_podman_pause_pid") as pause_pid, mock.patch.object(
            runtime.os, "kill"
        ) as kill:
            runtime.reap_orphaned_helpers()
        pause_pid.assert_not_called()
        kill.assert_not_called()

    def test_reap_orphaned_helpers_preserves_other_launch_catatonit(self):
        with mock.patch.object(runtime, "reap_orphaned_appimage_mounts"), mock.patch.object(
            runtime, "_ancestor_pids", return_value=(2000, 3000)
        ), mock.patch.object(
            runtime.os, "listdir", return_value=["123"]
        ), mock.patch.object(
            runtime.os, "getpid", return_value=1000
        ), mock.patch.object(
            runtime.Path, "read_bytes", return_value=b"catatonit\0-P\0"
        ), mock.patch.object(
            runtime, "_parent_pid", return_value=2000
        ), mock.patch.object(
            runtime, "_podman_pause_pid", return_value=456
        ), mock.patch.object(runtime.os, "kill") as kill:
            runtime.reap_orphaned_helpers()
        kill.assert_not_called()

    def test_reap_orphaned_helpers_unmounts_sdss_appimage_mounts(self):
        mountinfo = (
            "42 35 0:99 / /tmp/.mount_Cemu.ABC rw,nosuid - fuseblk "
            "/home/deck/Cemu.AppImage.sdss-real rw\n"
        )
        with mock.patch.object(runtime.Path, "read_text", return_value=mountinfo), mock.patch.object(
            runtime.shutil, "which", return_value="/usr/bin/fusermount3"
        ), mock.patch.object(runtime.subprocess, "run") as run:
            runtime.reap_orphaned_appimage_mounts()
        run.assert_called_once_with(
            ["/usr/bin/fusermount3", "-u", "-z", "/tmp/.mount_Cemu.ABC"],
            capture_output=True,
            check=False,
        )


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
