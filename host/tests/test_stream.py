import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        self.assertNotIn("encoder =", conf)

    def test_keeps_controller_input_but_skips_the_gamepad_probe(self):
        """Naming the pad type skips Sunshine's per-startup uinput probe, which Steam
        Input re-enumerates until the 32-bit Steam client exhausts its address space.
        Controller input itself must stay on, or the Deck loses buttons and triggers."""
        conf = stream.render_conf(self.spec)
        self.assertIn("gamepad = xone", conf)
        self.assertNotIn("controller = disabled", conf)

    def test_can_force_software_encoding_for_capture_diagnostics(self):
        spec = stream.SunshineSpec(
            config_dir=Path(self._tmp.name), encoder="software"
        )
        self.assertIn("encoder = software", stream.render_conf(spec))

    def test_rejects_unknown_encoder_diagnostic(self):
        with self.assertRaisesRegex(ValueError, "SDSS_SUNSHINE_ENCODER"):
            with mock.patch.dict(
                os.environ, {"SDSS_SUNSHINE_ENCODER": "unknown"}
            ):
                stream.default_spec()

    def test_audio_stays_on_the_tv(self):
        self.assertIn("stream_audio = disabled", stream.render_conf(self.spec))

    def test_uses_the_default_port_so_the_moonlight_cli_can_reach_it(self):
        # `moonlight stream <host>` accepts no port argument.
        self.assertEqual(stream.DEFAULT_PORT, 47989)
        self.assertIn("port = 47989", stream.render_conf(self.spec))

    def test_system_tray_is_disabled(self):
        """No desktop shell exists in this headless sandbox for a tray icon to attach to.

        Verified on hardware: left at Sunshine's "enabled" default, every single session
        teardown logged GLib-GIO-CRITICAL/Gtk-CRITICAL/libayatana-appindicator-WARNING
        failures from Sunshine's tray icon trying (and failing) to tear itself down,
        relayed through Steam's own log-capture pipeline alongside everything else.
        """
        self.assertIn("system_tray = disabled", stream.render_conf(self.spec))

    def test_log_level_suppresses_routine_info_noise(self):
        """Cut Sunshine's own per-launch "info" chatter, not just the tray icon's.

        Verified on hardware: dozens of "Info:" lines per launch (resolution, codec/
        vaapi details, bitrate, interface discovery) are stdout relayed through Steam's
        own log-capture pipeline (srt-logger) alongside everything else SDSS's session
        produces. "warning" still surfaces anything Sunshine considers an actual
        problem; only the routine diagnostic noise is cut.
        """
        self.assertIn("min_log_level = warning", stream.render_conf(self.spec))

    def test_single_app_is_exposed(self):
        apps = json.loads(stream.render_apps())
        self.assertEqual([app["name"] for app in apps["apps"]], [stream.APP_NAME])


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

    def test_libva_info_logging_is_suppressed(self):
        """libva has its own logging, entirely separate from Sunshine's min_log_level.

        Verified on hardware: left unset, libva defaults to level 2 ("info") and prints
        "libva info: ..." for every driver probe/open on every single launch -- a
        substantial, steady contributor to the subprocess output Steam's own log-capture
        pipeline (srt-logger) has to relay. Level 1 keeps genuine "libva error: ..."
        messages if something actually breaks.
        """
        self.assertIn("--env=LIBVA_MESSAGING_LEVEL=1", self.command())


if __name__ == "__main__":
    unittest.main()
