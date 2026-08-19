import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sdss import cli, launch


class TestFlatpakArgs(unittest.TestCase):
    def test_flatpak_gets_socket_access(self):
        command = launch.build_command(
            ["flatpak", "run", "net.kuribo64.melonDS", "game.nds"], "wayland-1"
        )
        self.assertEqual(
            command,
            [
                "flatpak",
                "run",
                "--socket=wayland",
                "--filesystem=xdg-run/wayland-1",
                "--env=WAYLAND_DISPLAY=wayland-1",
                "--env=DISABLE_GAMESCOPE_WSI=1",
                "net.kuribo64.melonDS",
                "game.nds",
            ],
        )

    def test_existing_flags_are_not_duplicated(self):
        command = launch.build_command(
            ["flatpak", "run", "--socket=wayland", "net.kuribo64.melonDS"], "wayland-1"
        )
        self.assertEqual(command.count("--socket=wayland"), 1)

    def test_native_command_is_untouched(self):
        original = ["/home/deck/Applications/Cemu.AppImage", "game.wud"]
        self.assertEqual(launch.build_command(original, "wayland-1"), original)

    def test_profile_launch_args_are_appended_once(self):
        command = launch.build_command(
            ["/home/deck/Applications/azahar.AppImage", "game.3ds"],
            "wayland-1",
            ("-f",),
        )
        self.assertEqual(
            command,
            ["/home/deck/Applications/azahar.AppImage", "game.3ds", "-f"],
        )
        self.assertEqual(
            launch.build_command(command, "wayland-1", ("-f",)),
            command,
        )

    def test_flatpak_without_run_subcommand_is_untouched(self):
        original = ["flatpak", "list"]
        self.assertEqual(launch.build_command(original, "wayland-1"), original)


class TestEnv(unittest.TestCase):
    def test_points_toolkits_at_the_nested_compositor(self):
        env = launch.build_env({"DISPLAY": ":0"}, "wayland-1", "/run/user/1000")
        self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-1")
        self.assertEqual(env["QT_QPA_PLATFORM"], "wayland")
        self.assertEqual(env["GDK_BACKEND"], "wayland")

    def test_display_is_dropped_so_clients_cannot_escape_to_the_desktop(self):
        env = launch.build_env({"DISPLAY": ":0"}, "wayland-1", "/run/user/1000")
        self.assertNotIn("DISPLAY", env)

    def test_caller_overrides_are_respected(self):
        env = launch.build_env({"QT_QPA_PLATFORM": "xcb"}, "wayland-1", "/run/user/1000")
        self.assertEqual(env["QT_QPA_PLATFORM"], "xcb")

    def test_x11_clients_target_the_nested_xwayland(self):
        env = launch.build_env(
            {"DISPLAY": ":0"},
            "wayland-1",
            "/run/user/1000",
            x11_display=":1",
            prefer_x11=True,
        )
        self.assertEqual(env["DISPLAY"], ":1")
        self.assertEqual(env["GDK_BACKEND"], "x11")
        self.assertEqual(env["QT_QPA_PLATFORM"], "xcb")

    def test_x11_preference_without_a_display_falls_back_to_wayland(self):
        env = launch.build_env({"DISPLAY": ":0"}, "wayland-1", "/run/user/1000", prefer_x11=True)
        self.assertNotIn("DISPLAY", env)
        self.assertEqual(env["GDK_BACKEND"], "wayland")

    def test_saved_steam_overlay_is_restored_for_the_emulator(self):
        preload = (
            "/home/deck/.local/share/Steam/ubuntu12_32/gameoverlayrenderer.so:"
            "/home/deck/.local/share/Steam/ubuntu12_64/gameoverlayrenderer.so"
        )
        env = launch.build_env(
            {launch.SAVED_LD_PRELOAD: preload},
            "wayland-1",
            "/run/user/1000",
        )
        self.assertEqual(env["LD_PRELOAD"], preload)
        self.assertNotIn(launch.SAVED_LD_PRELOAD, env)

    def test_helper_env_strips_only_steam_overlay_preloads(self):
        env = launch.helper_env(
            {
                "LD_PRELOAD": (
                    "/tmp/keep.so:"
                    "/home/deck/.local/share/Steam/ubuntu12_64/gameoverlayrenderer.so "
                    "/tmp/also-keep.so"
                ),
                launch.SAVED_LD_PRELOAD: "/tmp/saved-overlay.so",
            }
        )
        self.assertEqual(env["LD_PRELOAD"], "/tmp/keep.so:/tmp/also-keep.so")
        self.assertNotIn(launch.SAVED_LD_PRELOAD, env)

    def test_helper_env_drops_ld_preload_when_only_overlay_remains(self):
        env = launch.helper_env(
            {"LD_PRELOAD": "/steam/gameoverlayrenderer.so"}
        )
        self.assertNotIn("LD_PRELOAD", env)

    def test_overlay_can_be_disabled_for_an_emulator(self):
        env = launch.build_env(
            {launch.SAVED_LD_PRELOAD: "/steam/gameoverlayrenderer.so"},
            "wayland-1",
            "/run/user/1000",
            steam_overlay=False,
        )
        self.assertNotIn("LD_PRELOAD", env)
        self.assertNotIn(launch.SAVED_LD_PRELOAD, env)


class TestGamescopeWsiOptOut(unittest.TestCase):
    """The layer crashes when its gamescope surface is really the nested compositor.

    Verified on hardware: Cemu segfaults inside the layer at swapchain teardown and the
    stack-trace spam exhausts Steam's 32-bit allocator, killing the Steam client itself.
    """

    def test_layer_is_disabled_on_the_nested_xwayland(self):
        env = launch.build_env(
            {}, "wayland-1", "/run/user/1000", x11_display=":1", prefer_x11=True
        )
        self.assertEqual(env["DISABLE_GAMESCOPE_WSI"], "1")

    def test_layer_is_disabled_on_the_nested_wayland(self):
        # It is an *implicit* layer, so it loads on the Wayland path too.
        env = launch.build_env({}, "wayland-1", "/run/user/1000")
        self.assertEqual(env["DISABLE_GAMESCOPE_WSI"], "1")

    def test_opt_out_is_not_left_to_the_inherited_environment(self):
        # A gamescope session exports ENABLE_GAMESCOPE_WSI=1; inheriting is not enough.
        env = launch.build_env(
            {"ENABLE_GAMESCOPE_WSI": "1"}, "wayland-1", "/run/user/1000"
        )
        self.assertEqual(env["DISABLE_GAMESCOPE_WSI"], "1")

    def test_opt_out_crosses_the_flatpak_sandbox(self):
        # A Flatpak gets a fresh environment, so build_env's export cannot reach it.
        command = launch.build_command(["flatpak", "run", "net.kuribo64.melonDS"], "wayland-1")
        self.assertIn("--env=DISABLE_GAMESCOPE_WSI=1", command)


class TestPassthroughEnv(unittest.TestCase):
    def test_passthrough_restores_saved_steam_overlay(self):
        with mock.patch.dict(
            "os.environ",
            {launch.SAVED_LD_PRELOAD: "/steam/gameoverlayrenderer.so"},
            clear=True,
        ), mock.patch.object(cli.subprocess, "call", return_value=0) as call:
            self.assertEqual(cli._exec_passthrough(["emulator"]), 0)
        call.assert_called_once_with(
            ["emulator"],
            env={"LD_PRELOAD": "/steam/gameoverlayrenderer.so"},
        )


if __name__ == "__main__":
    unittest.main()
