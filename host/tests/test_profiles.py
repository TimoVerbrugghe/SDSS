import unittest

from sdss import profiles
from sdss.compositor import CompositorSpec, OutputMode, render_config


class TestDetection(unittest.TestCase):
    def test_detects_appimage_launch(self):
        found = profiles.detect(["/home/deck/Applications/Cemu.AppImage", "game.wud"])
        self.assertIsNotNone(found)
        self.assertEqual(found.id, "cemu")

    def test_detects_flatpak_launch(self):
        found = profiles.detect(["flatpak", "run", "net.kuribo64.melonDS"])
        self.assertEqual(found.id, "melonds")

    def test_unknown_command_has_no_profile(self):
        self.assertIsNone(profiles.detect(["/usr/bin/retroarch", "-L", "core.so"]))

    def test_get_rejects_unknown_id(self):
        with self.assertRaises(KeyError):
            profiles.get("dolphin")


class TestWindowMatch(unittest.TestCase):
    def test_title_only_match_produces_one_rule(self):
        rules = profiles.CEMU.second_window.rules()
        self.assertEqual(rules, ('title="GamePad View"',))

    def test_app_id_match_covers_wayland_and_xwayland(self):
        rules = profiles.MELONDS.second_window.rules()
        self.assertIn('app_id="^melonDS$" title="melonDS.*2"', rules)
        self.assertIn('class="^melonDS$" title="melonDS.*2"', rules)

    def test_app_id_and_title_are_anded_so_the_main_window_is_not_matched(self):
        for rule in profiles.MELONDS.second_window.rules():
            self.assertIn("title=", rule)


class TestSwayConfig(unittest.TestCase):
    def render(self, profile):
        spec = CompositorSpec(
            profile=profile,
            env_dump="/run/user/1000/sdss/session/dump-env.sh",
            tv=OutputMode(1920, 1080),
            second=OutputMode(1280, 800),
        )
        return render_config(spec)

    def test_outputs_do_not_overlap(self):
        config = self.render(profiles.CEMU)
        self.assertIn("output WL-1 mode 1920x1080@60Hz position 0 0", config)
        self.assertIn("output HEADLESS-1 mode 1280x800@60Hz position 1920 0", config)

    def test_second_window_moves_to_headless_workspace(self):
        config = self.render(profiles.CEMU)
        self.assertIn(
            'for_window [title="GamePad View"] move container to workspace second, '
            "fullscreen enable",
            config,
        )

    def test_env_dump_runs_inside_the_compositor(self):
        self.assertIn("exec /run/user/1000/sdss/session/dump-env.sh", self.render(profiles.AZAHAR))

    def test_emulator_is_not_started_by_the_compositor(self):
        # sway runs in a container and cannot exec host binaries.
        config = self.render(profiles.AZAHAR)
        self.assertNotIn("AppImage", config)
        self.assertNotIn("flatpak", config)


if __name__ == "__main__":
    unittest.main()
