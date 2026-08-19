import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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
    def test_verified_profiles_match_on_app_id_and_title(self):
        rules = profiles.CEMU.second_window.rules()
        self.assertIn('app_id="^Cemu$" title="^GamePad View"', rules)
        self.assertIn('class="^Cemu$" title="^GamePad View"', rules)

    def test_azahar_matches_only_the_secondary_window(self):
        rules = profiles.AZAHAR.second_window.rules()
        self.assertTrue(all(rule.endswith('title="Secondary Window$"') for rule in rules))
        self.assertIn('class="^Azahar$" title="Secondary Window$"', rules)

    def test_app_id_match_covers_wayland_and_xwayland(self):
        rules = profiles.MELONDS.second_window.rules()
        self.assertIn('app_id="^melonDS$" title="melonDS.*2"', rules)
        self.assertIn('class="^melonDS$" title="melonDS.*2"', rules)

    def test_app_id_and_title_are_anded_so_the_main_window_is_not_matched(self):
        for rule in profiles.MELONDS.second_window.rules():
            self.assertIn("title=", rule)


class TestX11Requirement(unittest.TestCase):
    def test_steam_profiles_use_the_stable_xwayland_path(self):
        # Cemu and melonDS cannot map correctly on Wayland. Azahar can map there, but native
        # Wayland has failed under Steam with a protocol error and no usable Steam overlay.
        self.assertTrue(profiles.CEMU.needs_x11)
        self.assertTrue(profiles.MELONDS.needs_x11)
        self.assertTrue(profiles.AZAHAR.needs_x11)


class TestConfigTargets(unittest.TestCase):
    def test_cemu_edits_read_gamepad_profile_at_resolution_time(self):
        target = profiles.CEMU.configs[0]
        with mock.patch.dict(
            os.environ, {"SDSS_CEMU_GAMEPAD_PROFILE": "DeckGamePad"}
        ):
            edits = target.resolved_edits()
        values = {(edit.key, edit.value) for edit in edits}
        self.assertIn(("open_pad", "true"), values)
        self.assertIn(("controllerProfile", "DeckGamePad"), values)
        self.assertIn(("controller_profile", "DeckGamePad"), values)

    def test_no_gamepad_profile_means_no_optional_edits_or_files(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            edits = profiles.CEMU.configs[0].resolved_edits()
            files = profiles.CEMU.resolved_files()
        self.assertEqual({(edit.key, edit.value) for edit in edits}, {("open_pad", "true")})
        self.assertEqual(files, ())

    def test_cemu_default_profile_file_uses_safe_name(self):
        with mock.patch.dict(
            os.environ, {"SDSS_CEMU_GAMEPAD_PROFILE": "DeckGamePad"}
        ):
            target = profiles.CEMU.resolved_files()[0]
        self.assertEqual(
            target.resolve(),
            Path(os.path.expanduser("~/.config/Cemu/controllerProfiles/DeckGamePad.txt")),
        )
        self.assertIn("emulate = Wii U GamePad", target.content)

    def test_cemu_profile_path_traversal_is_rejected(self):
        with mock.patch.dict(
            os.environ, {"SDSS_CEMU_GAMEPAD_PROFILE": "../../etc/x"}
        ):
            with self.assertRaises(ValueError):
                profiles.CEMU.resolved_files()
            with self.assertRaises(ValueError):
                profiles.CEMU.configs[0].resolved_edits()


class TestSwayConfig(unittest.TestCase):
    def render(self, profile):
        spec = CompositorSpec(
            profile=profile,
            env_dump="/run/user/1000/sdss/session/dump-env.sh",
            tv=OutputMode(1920, 1080),
            second=OutputMode(1280, 800),
        )
        return render_config(spec)

    def test_xwayland_is_forced_not_lazy(self):
        # `xwayland enable` makes sway spawn Xwayland only when the first X11 client
        # connects. Nothing connects before the env-dump `exec` runs, so DISPLAY is
        # empty and every needs_x11 profile dies with "needs Xwayland but the compositor
        # reported no DISPLAY". `force` starts it during compositor init instead.
        config = self.render(profiles.CEMU)
        self.assertIn("xwayland force", config)
        self.assertNotIn("xwayland enable", config)

    def test_outputs_do_not_overlap(self):
        config = self.render(profiles.CEMU)
        self.assertIn("output WL-1 mode 1920x1080@60Hz position 0 0", config)
        self.assertIn("output HEADLESS-1 mode 1280x800@60Hz position 1920 0", config)

    def test_x11_main_output_gets_no_mode_directive(self):
        # wlr_x11_backend commits its output at whatever size the parent Xwayland's root
        # window already is and rejects any other "mode" sway config asks for
        # ("Requested backend configuration failed, searching for valid fallbacks"),
        # leaving the output power:false and gamescope's spinner stuck forever. X11-1
        # must be left unsized; only the native-Wayland fallback (WL-1) gets an exact
        # mode, since that backend *does* support arbitrary custom modes.
        from sdss.compositor import MAIN_OUTPUT_X11

        spec = CompositorSpec(
            profile=profiles.CEMU,
            env_dump="/run/user/1000/sdss/session/dump-env.sh",
            tv=OutputMode(1920, 1080),
            second=OutputMode(1280, 800),
            main_output=MAIN_OUTPUT_X11,
        )
        config = render_config(spec)
        self.assertIn("output X11-1 position 0 0", config)
        self.assertNotIn("output X11-1 mode", config)
        # X11-1's real size is only known at runtime, so HEADLESS-1 can't be positioned
        # relative to it like the Wayland path does — it must sit far enough away that
        # it can never overlap whatever size X11-1 actually ends up being.
        self.assertIn("output HEADLESS-1 mode 1280x800@60Hz position 8192 0", config)

    def test_second_window_moves_to_headless_workspace(self):
        config = self.render(profiles.CEMU)
        self.assertIn(
            'for_window [app_id="^Cemu$" title="^GamePad View"] move container to '
            "workspace second, fullscreen enable",
            config,
        )
        self.assertIn(
            'for_window [class="^Cemu$" title="^GamePad View"] move container to '
            "workspace second, fullscreen enable",
            config,
        )

    def test_env_dump_runs_inside_the_compositor(self):
        self.assertIn("exec /run/user/1000/sdss/session/dump-env.sh", self.render(profiles.AZAHAR))

    def test_azahar_requests_native_fullscreen(self):
        self.assertEqual(profiles.AZAHAR.launch_args, ("-f",))

    def test_azahar_uses_opengl_for_nested_steam_overlay(self):
        edits = {
            (edit.section, edit.key): edit.value
            for target in profiles.AZAHAR.configs
            for edit in target.edits
        }
        self.assertEqual(edits[("Renderer", "graphics_api")], "1")
        self.assertEqual(edits[("Renderer", "graphics_api\\default")], "false")
        self.assertFalse(profiles.AZAHAR.steam_overlay)
        self.assertTrue(profiles.CEMU.steam_overlay)

    def test_emulator_is_not_started_by_the_compositor(self):
        # sway runs in a container and cannot exec host binaries.
        config = self.render(profiles.AZAHAR)
        self.assertNotIn("AppImage", config)
        self.assertNotIn("flatpak", config)

    def test_no_titlebar_padding_directive(self):
        # sway's titlebar_padding requires value >= titlebar_border_thickness (default 2),
        # so "titlebar_padding 0" or "0 0" both fail config validation with "Invalid size
        # specified" and sway refuses to start. default_border/default_floating_border
        # none already remove titlebars entirely, so the directive is unnecessary.
        config = self.render(profiles.CEMU)
        self.assertNotIn("titlebar_padding", config)


class TestX11FitScript(unittest.TestCase):
    def test_targets_the_configured_display_and_output_title(self):
        from sdss.compositor import x11_fit_script

        script = x11_fit_script(":1")
        self.assertIn("DISPLAY=:1 xdotool getdisplaygeometry", script)
        self.assertIn("DISPLAY=:1 xwininfo -root -children", script)
        self.assertIn("DISPLAY=:1 xdotool windowsize", script)
        self.assertIn('"wlroots - X11-1"', script)

    def test_does_not_use_xdotool_search_by_name(self):
        # wlroots sets only _NET_WM_NAME (EWMH) on its own toplevel window, never the
        # classic ICCCM WM_NAME `xdotool search --name` actually matches against. Every
        # real Steam launch either matched nothing, or — worse — matched the *root*
        # window instead, since an empty/absent name still trivially satisfies a
        # wildcard search. xwininfo's own title lookup isn't fooled by this because it
        # reads whichever property backs the title, which is why its output is parsed
        # here instead.
        from sdss.compositor import x11_fit_script

        script = x11_fit_script(":1")
        self.assertNotIn("xdotool search", script)

    def test_quotes_a_display_value_with_shell_metacharacters(self):
        # DISPLAY comes from runtime.parent_display(), which reads it straight out of
        # the process environment Steam set — it must not be interpolated unquoted
        # into a shell script.
        from sdss.compositor import x11_fit_script

        script = x11_fit_script(":1; rm -rf /")
        self.assertIn("':1; rm -rf /'", script)

    def test_never_blocks_forever_if_the_window_never_appears(self):
        # This script gates dump-env.sh's readiness signal, which session.py's
        # _await_nested_display() waits on before launching the emulator — an
        # unbounded retry here would hang every launch if the window is never found,
        # rather than falling through to the existing 15s timeout.
        from sdss.compositor import x11_fit_script

        script = x11_fit_script(":1")
        self.assertIn('[ "$i" -lt 20 ]', script)


if __name__ == "__main__":
    unittest.main()
