import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sdss import patch
from sdss.patch import INI, TOML, XML, Edit

CEMU_XML = """<?xml version="1.0" encoding="UTF-8"?>
<content>
    <fullscreen>false</fullscreen>
    <open_pad>false</open_pad>
    <pad_maximized>false</pad_maximized>
</content>
"""

AZAHAR_INI = """[Core]
use_cpu_jit=true

[Layout]
large_screen_proportion=4
layout_option=5
layout_option\\default=false
swap_screen=false

[UI]
theme=default
"""

MELONDS_TOML = """[Instance0]
JoystickID = 0

[Instance0.Window0]
ScreenLayout = 3
ScreenSizing = 3
"""


class TestXmlEdits(unittest.TestCase):
    def test_sets_tag_value(self):
        out = patch.apply_edits(CEMU_XML, XML, (Edit(key="open_pad", value="true"),))
        self.assertIn("<open_pad>true</open_pad>", out)
        self.assertIn("<pad_maximized>false</pad_maximized>", out)

    def test_missing_tag_raises(self):
        with self.assertRaises(patch.PatchError):
            patch.apply_edits(CEMU_XML, XML, (Edit(key="nope", value="1"),))

    def test_missing_optional_tag_is_ignored(self):
        out = patch.apply_edits(CEMU_XML, XML, (Edit(key="nope", value="1", required=False),))
        self.assertEqual(out, CEMU_XML)


class TestIniEdits(unittest.TestCase):
    def test_replaces_existing_key_in_section(self):
        out = patch.apply_edits(
            AZAHAR_INI, INI, (Edit(section="Layout", key="layout_option", value="4"),)
        )
        self.assertIn("layout_option=4", out)
        self.assertIn("layout_option\\default=false", out)
        self.assertIn("use_cpu_jit=true", out)

    def test_appends_missing_key_to_section(self):
        out = patch.apply_edits(
            AZAHAR_INI,
            INI,
            (Edit(section="Layout", key="secondary_display_layout", value="2"),),
        )
        layout = out.split("[Layout]")[1].split("[UI]")[0]
        self.assertIn("secondary_display_layout=2", layout)

    def test_creates_missing_section(self):
        out = patch.apply_edits(
            AZAHAR_INI, INI, (Edit(section="Extra", key="foo", value="bar"),)
        )
        self.assertIn("[Extra]", out)
        self.assertIn("foo=bar", out)

    def test_key_prefix_is_not_matched(self):
        out = patch.apply_edits(
            AZAHAR_INI, INI, (Edit(section="Layout", key="layout_option", value="4"),)
        )
        self.assertIn("layout_option\\default=false", out)
        self.assertNotIn("layout_option\\default=4", out)


class TestTomlEdits(unittest.TestCase):
    def test_creates_dotted_table(self):
        out = patch.apply_edits(
            MELONDS_TOML,
            TOML,
            (Edit(section="Instance0.Window1", key="ScreenSizing", value="5"),),
        )
        self.assertIn("[Instance0.Window1]", out)
        self.assertIn("ScreenSizing = 5", out)
        window0 = out.split("[Instance0.Window0]")[1].split("[Instance0.Window1]")[0]
        self.assertIn("ScreenSizing = 3", window0)


class TestWriteFileIfAbsent(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_creates_missing_file_and_its_parent_dir(self):
        path = self.root / "controllerProfiles" / "DeckGamePad.txt"
        journal = patch.Journal(self.root / "journal", "session")

        written = patch.write_file_if_absent(path, "[General]\nemulate = Wii U GamePad\n", journal)

        self.assertTrue(written)
        self.assertEqual(path.read_text(), "[General]\nemulate = Wii U GamePad\n")

    def test_never_overwrites_an_existing_file(self):
        path = self.root / "DeckGamePad.txt"
        path.write_text("user's own mapping")
        journal = patch.Journal(self.root / "journal", "session")

        written = patch.write_file_if_absent(path, "template content", journal)

        self.assertFalse(written)
        self.assertEqual(path.read_text(), "user's own mapping")

    def test_teardown_removes_a_file_it_created(self):
        path = self.root / "DeckGamePad.txt"
        journal = patch.Journal(self.root / "journal", "session")

        patch.write_file_if_absent(path, "template content", journal)
        self.assertTrue(path.is_file())

        journal.restore_snapshots()
        self.assertFalse(path.exists())

    def test_teardown_leaves_a_preexisting_file_untouched(self):
        path = self.root / "DeckGamePad.txt"
        path.write_text("user's own mapping")
        journal = patch.Journal(self.root / "journal", "session")

        patch.write_file_if_absent(path, "template content", journal)
        journal.restore_snapshots()

        self.assertEqual(path.read_text(), "user's own mapping")


class TestJournal(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_restore_is_byte_identical(self):
        config = self.root / "qt-config.ini"
        config.write_text(AZAHAR_INI)
        original = config.read_bytes()
        journal = patch.Journal(self.root / "journal", "session")

        changed = patch.patch_file(
            config,
            INI,
            (
                Edit(section="Layout", key="layout_option", value="4"),
                Edit(section="Layout", key="secondary_display_layout", value="2"),
            ),
            journal,
        )
        self.assertTrue(changed)
        self.assertNotEqual(config.read_bytes(), original)

        journal.restore()
        self.assertEqual(config.read_bytes(), original)
        self.assertFalse(journal.exists)

    def test_restore_deletes_files_that_did_not_exist(self):
        missing = self.root / "new.conf"
        journal = patch.Journal(self.root / "journal", "session")
        journal.record(missing)
        missing.write_text("created during session")

        journal.restore()
        self.assertFalse(missing.exists())

    def test_recording_twice_keeps_the_first_snapshot(self):
        config = self.root / "a.ini"
        config.write_text("[S]\nk=1\n")
        journal = patch.Journal(self.root / "journal", "session")
        journal.record(config)
        config.write_text("[S]\nk=2\n")
        journal.record(config)

        journal.restore()
        self.assertEqual(config.read_text(), "[S]\nk=1\n")

    def test_corrupt_manifest_never_discards_the_intact_backups(self):
        # SteamOS SIGKILLs the whole session on logout (see docs/hardware-recon.md), which
        # can hit `sdss` mid-write and truncate manifest.json. Treating that as "nothing
        # recorded" is the dangerous reading: restore() would report success having
        # restored nothing, then discard() would rmtree the backups that are still fine.
        journal = patch.Journal(self.root / "journal", "session")
        config = self.root / "b.ini"
        config.write_text("[S]\nk=1\n")
        journal.record(config)
        backup = next(journal.files.iterdir())
        journal.manifest.write_text("{not valid json")

        with self.assertRaises(patch.PatchError):
            journal._load()
        with self.assertRaises(patch.PatchError):
            journal.restore()
        # The whole point: the backup bytes are still recoverable by hand.
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_text(), "[S]\nk=1\n")

    def test_backup_names_do_not_collide_across_entries(self):
        # Backup filenames used to be derived from len(entries), so any renumbering made a
        # later record() overwrite an earlier file's only copy.
        journal = patch.Journal(self.root / "journal", "session")
        first = self.root / "one.ini"
        second = self.root / "two.ini"
        first.write_text("[S]\nk=first\n")
        second.write_text("[S]\nk=second\n")
        journal.record(first)
        journal.record(second)

        self.assertEqual(len(list(journal.files.iterdir())), 2)
        first.write_text("clobbered")
        second.write_text("clobbered")
        journal.restore()
        self.assertEqual(first.read_text(), "[S]\nk=first\n")
        self.assertEqual(second.read_text(), "[S]\nk=second\n")

    def test_manifest_write_is_atomic(self):
        journal = patch.Journal(self.root / "journal", "session")
        config = self.root / "c.ini"
        config.write_text("[S]\nk=1\n")
        journal.record(config)

        # No leftover temp file, and the manifest itself is valid JSON.
        leftovers = list(journal.dir.glob(".manifest.json.sdss-tmp"))
        self.assertEqual(leftovers, [])
        self.assertTrue(journal.manifest.is_file())

    def test_restore_refuses_a_corrupted_backup(self):
        # The journal records a sha256 digest of each backup precisely so a truncated
        # or corrupted backup file can't be restored silently — restore() must verify
        # it instead of trusting the bytes on disk unconditionally.
        config = self.root / "d.ini"
        config.write_text("[S]\nk=1\n")
        journal = patch.Journal(self.root / "journal", "session")
        journal.record(config)
        config.write_text("[S]\nk=2\n")

        backup = journal.files / next(journal.files.iterdir()).name
        backup.write_bytes(b"corrupted")

        with self.assertRaises(patch.PatchError):
            journal.restore()
        # The live file must be left alone when the restore is refused.
        self.assertEqual(config.read_text(), "[S]\nk=2\n")

    def test_restore_snapshots_preserves_unrelated_user_changes(self):
        config = self.root / "qt-config.ini"
        config.write_text(AZAHAR_INI)
        journal = patch.Journal(self.root / "journal", "session")
        patch.patch_file(
            config,
            INI,
            (
                Edit(section="Layout", key="layout_option", value="4"),
                Edit(section="Layout", key="secondary_display_layout", value="2"),
            ),
            journal,
        )
        current = config.read_text()
        config.write_text(current.replace("theme=default", "theme=dark"))

        journal.restore_snapshots()
        restored = config.read_text()
        self.assertIn("layout_option=5", restored)
        self.assertNotIn("layout_option=4", restored)
        self.assertNotIn("secondary_display_layout=2", restored)
        self.assertIn("theme=dark", restored)

    def test_restore_snapshots_falls_back_to_full_restore_without_snapshots(self):
        config = self.root / "legacy.ini"
        config.write_text("[S]\nk=1\n")
        journal = patch.Journal(self.root / "journal", "session")
        journal.record(config)
        config.write_text("[S]\nk=2\n")

        journal.restore_snapshots()
        self.assertEqual(config.read_text(), "[S]\nk=1\n")


if __name__ == "__main__":
    unittest.main()
