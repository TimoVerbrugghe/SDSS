"""Tests for the launcher-wrapper hook (auto-launch via a normal Steam library shortcut)."""

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sdss import hooks, profiles


class WrapperInstallTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.launcher = Path(self._tmp.name) / "azahar.AppImage"
        self.launcher.write_bytes(b"#!/bin/bash\necho real-appimage\n")
        self.launcher.chmod(0o755)
        self.profile = replace(profiles.AZAHAR, launcher_path=str(self.launcher))

    def test_install_shadows_the_real_binary_and_writes_a_wrapper(self):
        changed = hooks.install(self.profile)
        self.assertTrue(changed)
        shadow = self.launcher.with_name(self.launcher.name + ".sdss-real")
        self.assertTrue(shadow.is_file())
        self.assertEqual(shadow.read_bytes(), b"#!/bin/bash\necho real-appimage\n")
        self.assertIn("sdss run --profile azahar", self.launcher.read_text())
        self.assertTrue(hooks.is_installed(self.profile))

    def test_wrapper_keeps_steam_overlay_for_the_emulator_only(self):
        hooks.install(self.profile)
        wrapper = self.launcher.read_text()
        save = 'export SDSS_EMULATOR_LD_PRELOAD="${LD_PRELOAD-}"'
        self.assertIn(save, wrapper)
        self.assertIn("unset LD_PRELOAD", wrapper)
        self.assertLess(wrapper.index(save), wrapper.index("exec "))

    def test_install_is_idempotent(self):
        hooks.install(self.profile)
        wrapper_text = self.launcher.read_text()
        changed = hooks.install(self.profile)
        self.assertFalse(changed)
        self.assertEqual(self.launcher.read_text(), wrapper_text)

    def test_remove_restores_the_original_bytes_exactly(self):
        original = self.launcher.read_bytes()
        hooks.install(self.profile)
        changed = hooks.remove(self.profile)
        self.assertTrue(changed)
        self.assertEqual(self.launcher.read_bytes(), original)
        self.assertFalse(hooks.is_installed(self.profile))

    def test_remove_without_install_is_a_noop(self):
        self.assertFalse(hooks.remove(self.profile))
        self.assertEqual(self.launcher.read_bytes(), b"#!/bin/bash\necho real-appimage\n")

    def test_reconcile_drives_both_directions(self):
        self.assertTrue(hooks.reconcile(self.profile, True))
        self.assertTrue(hooks.is_installed(self.profile))
        self.assertTrue(hooks.reconcile(self.profile, False))
        self.assertFalse(hooks.is_installed(self.profile))

    def test_missing_launcher_path_is_a_noop(self):
        profile = replace(self.profile, launcher_path=None)
        self.assertFalse(hooks.install(profile))
        self.assertFalse(hooks.remove(profile))

    def test_emudeck_update_rewraps_the_new_binary(self):
        hooks.install(self.profile)
        self.launcher.write_bytes(b"#!/bin/bash\necho new-version\n")
        self.launcher.chmod(0o755)

        self.assertTrue(hooks.install(self.profile))
        self.assertTrue(hooks.is_installed(self.profile))
        shadow = self.launcher.with_name(self.launcher.name + ".sdss-real")
        self.assertEqual(shadow.read_bytes(), b"#!/bin/bash\necho new-version\n")
        self.assertFalse(
            self.launcher.with_name(f".{self.launcher.name}.sdss-previous").exists()
        )

    def test_handles_symlink_launcher_like_melonds_flatpak_export(self):
        real_target = Path(self._tmp.name) / "real-melonds"
        real_target.write_bytes(b"real melonds binary\n")
        link = Path(self._tmp.name) / "net.kuribo64.melonDS"
        link.symlink_to(real_target)
        profile = replace(profiles.MELONDS, launcher_path=str(link))

        self.assertTrue(hooks.install(profile))
        self.assertTrue(link.is_file())
        self.assertFalse(link.is_symlink())
        self.assertIn("sdss run --profile melonds", link.read_text())

        self.assertTrue(hooks.remove(profile))
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), real_target.resolve())

    def test_flatpak_export_wrapper_preserves_flatpak_command_form(self):
        profile = replace(
            profiles.MELONDS,
            launcher_path=str(self.launcher),
        )
        hooks.install(profile)
        wrapper = self.launcher.read_text()
        self.assertIn(
            "flatpak run net.kuribo64.melonDS",
            wrapper,
        )

    def test_shadow_missing_blocks_remove(self):
        hooks.install(self.profile)
        shadow = self.launcher.with_name(self.launcher.name + ".sdss-real")
        shadow.unlink()
        self.assertFalse(hooks.remove(self.profile))

    def test_stale_marker_never_shadows_our_own_wrapper(self):
        """A wrapper whose marker text differs (an older SDSS version) must be rewritten in
        place, never treated as a real binary. Shadowing it would unlink the shadow holding
        the only copy of the real AppImage and leave the wrapper exec'ing itself forever."""
        hooks.install(self.profile)
        shadow = self.launcher.with_name(self.launcher.name + ".sdss-real")
        real_bytes = shadow.read_bytes()
        self.launcher.write_text(
            "#!/bin/bash\n# sdss-wrapper: azahar — wording from an older version.\n"
            "exec /home/deck/.local/bin/sdss run --profile azahar -- something\n"
        )

        self.assertTrue(hooks.install(self.profile))
        self.assertTrue(hooks.is_installed(self.profile))
        # The real binary is still exactly where it was.
        self.assertEqual(shadow.read_bytes(), real_bytes)
        self.assertNotIn(".sdss-real.sdss-real", self.launcher.read_text())

    def test_wrapper_write_failure_restores_the_real_binary(self):
        original = self.launcher.read_bytes()

        def boom(*_args, **_kwargs):
            raise OSError("no space left on device")

        with mock.patch.object(hooks, "_wrapper_script", side_effect=boom):
            with self.assertRaises(OSError):
                hooks.install(self.profile)

        # The launcher path must never be left missing: install()/remove() both need a
        # file there, so an empty path is otherwise unrecoverable.
        self.assertTrue(self.launcher.is_file())
        self.assertEqual(self.launcher.read_bytes(), original)
        self.assertFalse(hooks.is_installed(self.profile))

    def test_update_rewrap_failure_restores_new_binary_and_old_shadow(self):
        hooks.install(self.profile)
        shadow = self.launcher.with_name(self.launcher.name + ".sdss-real")
        old_shadow = shadow.read_bytes()
        self.launcher.write_bytes(b"#!/bin/bash\necho new-version\n")

        with mock.patch.object(
            hooks, "_write_wrapper", side_effect=OSError("no space")
        ):
            with self.assertRaises(OSError):
                hooks.install(self.profile)

        self.assertEqual(
            self.launcher.read_bytes(),
            b"#!/bin/bash\necho new-version\n",
        )
        self.assertEqual(shadow.read_bytes(), old_shadow)

    def test_install_recovers_a_launcher_lost_mid_swap(self):
        hooks.install(self.profile)
        shadow = self.launcher.with_name(self.launcher.name + ".sdss-real")
        # Simulate a kill between shadowing and writing the wrapper.
        self.launcher.unlink()

        self.assertTrue(hooks.install(self.profile))
        self.assertTrue(self.launcher.is_file())
        self.assertTrue(hooks.is_installed(self.profile))
        self.assertTrue(shadow.is_file())

    def test_remove_recovers_a_launcher_lost_mid_swap(self):
        original = self.launcher.read_bytes()
        hooks.install(self.profile)
        self.launcher.unlink()

        self.assertTrue(hooks.remove(self.profile))
        self.assertEqual(self.launcher.read_bytes(), original)

    def test_remove_keeps_a_real_binary_that_replaced_the_wrapper(self):
        """An EmuDeck update can drop a fresh AppImage over the wrapper while SDSS is
        disabled. Restoring the older shadow over it would silently downgrade the user."""
        hooks.install(self.profile)
        self.launcher.write_bytes(b"#!/bin/bash\necho newer-version\n")

        self.assertFalse(hooks.remove(self.profile))
        self.assertEqual(self.launcher.read_bytes(), b"#!/bin/bash\necho newer-version\n")

    def test_is_wrapper_does_not_read_the_whole_file(self):
        """Checked on every profile on every `sdss status`, which the Decky plugin calls
        on each panel open — and unwrapped paths are >100 MB AppImages."""
        big = Path(self._tmp.name) / "cemu.AppImage"
        big.write_bytes(b"\x7fELF" + b"\0" * (4 * 1024 * 1024))
        profile = replace(profiles.CEMU, launcher_path=str(big))

        with mock.patch.object(Path, "read_text", side_effect=AssertionError("full read")):
            self.assertFalse(hooks.is_installed(profile))


if __name__ == "__main__":
    unittest.main()
