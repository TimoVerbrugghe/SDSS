import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
