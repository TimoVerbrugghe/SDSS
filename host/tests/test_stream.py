import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sdss import stream


class TestSunshineConfig(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.spec = stream.SunshineSpec(config_dir=Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def test_captures_only_the_headless_output(self):
        conf = stream.render_conf(self.spec)
        self.assertIn("capture = wlr", conf)
        self.assertIn("output_name = HEADLESS-1", conf)

    def test_audio_stays_on_the_tv(self):
        self.assertIn("stream_audio = disabled", stream.render_conf(self.spec))

    def test_uses_the_default_port_so_the_moonlight_cli_can_reach_it(self):
        # `moonlight stream <host>` accepts no port argument.
        self.assertEqual(stream.DEFAULT_PORT, 47989)
        self.assertIn("port = 47989", stream.render_conf(self.spec))

    def test_single_app_is_exposed(self):
        apps = json.loads(stream.render_apps())
        self.assertEqual(len(apps["apps"]), 1)

    def test_app_name_is_plain_second_screen(self):
        # HEADLESS-1's size never varies (always DECK_PANEL_RESOLUTION), so there's
        # nothing to smuggle into the name — deck/sdss-connect.sh hardcodes the same
        # constant instead of parsing it out of the app list.
        apps = json.loads(stream.render_apps())
        self.assertEqual(apps["apps"][0]["name"], "Second Screen")

    def test_app_name_helper_matches_render_apps(self):
        self.assertEqual(stream.app_name(), "Second Screen")


class TestLaunchCommand(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.spec = stream.SunshineSpec(config_dir=Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def command(self):
        return stream.launch_command(self.spec, "wayland-1", "/run/user/1000")

    def test_socket_is_exposed_explicitly(self):
        # --socket=wayland alone binds the wrong display name into the sandbox.
        command = self.command()
        self.assertIn("--filesystem=xdg-run/wayland-1", command)
        self.assertIn("--env=WAYLAND_DISPLAY=wayland-1", command)

    def test_reads_pairing_pin_from_stdin(self):
        self.assertEqual(self.command()[-1], "-0")

    def test_uinput_access_for_touch_injection(self):
        self.assertIn("--device=all", self.command())


if __name__ == "__main__":
    unittest.main()
