import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sdss import managed_config, patch, profiles
from sdss.patch import INI, Edit


class ManagedConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        state = self.root / "state"
        self.env = mock.patch.dict(
            os.environ, {"XDG_STATE_HOME": str(state)}, clear=False
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = self.root / "emulator.ini"
        self.config.write_text("[Managed]\nlayout=single\n\n[User]\ntheme=light\n")
        self.profile = replace(
            profiles.AZAHAR,
            id="test",
            configs=(
                profiles.ConfigTarget(
                    path=str(self.config),
                    format=INI,
                    edits=(Edit(section="Managed", key="layout", value="separate"),),
                ),
            ),
            files=(),
        )

    def test_toggle_owns_managed_keys_and_preserves_unrelated_changes(self):
        managed_config.enable_profile(self.profile)
        self.assertIn("layout=separate", self.config.read_text())

        self.config.write_text(
            self.config.read_text().replace("theme=light", "theme=dark")
        )
        managed_config.disable_profile(self.profile)

        restored = self.config.read_text()
        self.assertIn("layout=single", restored)
        self.assertIn("theme=dark", restored)

    def test_repeated_enable_keeps_original_managed_value(self):
        managed_config.enable_profile(self.profile)
        managed_config.enable_profile(self.profile)
        managed_config.disable_profile(self.profile)
        self.assertIn("layout=single", self.config.read_text())

    def test_missing_config_can_be_deferred_until_launch(self):
        missing = replace(
            self.profile,
            configs=(
                profiles.ConfigTarget(
                    path=str(self.root / "missing.ini"),
                    format=INI,
                    edits=(Edit(section="Managed", key="layout", value="separate"),),
                ),
            ),
        )
        self.assertEqual(
            managed_config.enable_profile(missing, missing_ok=True),
            [],
        )
        with self.assertRaises(patch.PatchError):
            managed_config.enable_profile(missing)

    def test_legacy_session_journal_is_selectively_migrated(self):
        journal = managed_config.legacy_journal()
        patch.patch_file(
            self.config,
            INI,
            (Edit(section="Managed", key="layout", value="legacy"),),
            journal,
        )
        self.config.write_text(
            self.config.read_text().replace("theme=light", "theme=dark")
        )

        managed_config.migrate_legacy_journal()

        restored = self.config.read_text()
        self.assertIn("layout=single", restored)
        self.assertIn("theme=dark", restored)
        self.assertFalse(journal.exists)

    def test_restore_all_handles_profile_and_legacy_journals(self):
        with mock.patch.object(managed_config, "PROFILES", (self.profile,)):
            managed_config.enable_profile(self.profile)
            restored = managed_config.restore_all()
        self.assertEqual(restored, [self.config])
        self.assertIn("layout=single", self.config.read_text())


if __name__ == "__main__":
    unittest.main()
