import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sdss import launch


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


if __name__ == "__main__":
    unittest.main()
