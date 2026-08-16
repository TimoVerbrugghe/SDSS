import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


DECK = Path(__file__).resolve().parents[2] / "deck"
_spec = importlib.util.spec_from_file_location(
    "sdss_steam_shortcut_user_selection", DECK / "add-steam-shortcut.py"
)
assert _spec and _spec.loader
shortcut = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = shortcut
_spec.loader.exec_module(shortcut)


class SteamUserSelectionTest(unittest.TestCase):
    def test_most_recent_loginuser_is_first(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "userdata/111/config").mkdir(parents=True)
            (root / "userdata/222/config").mkdir(parents=True)
            (root / "config").mkdir()
            # loginusers.vdf keys are SteamID64s, not the userdata/<N> 32-bit account
            # id — encode that here so this test actually exercises the conversion in
            # user_config_dirs()/_account_id() rather than comparing IDs that would
            # never match on a real Steam install.
            base = shortcut._STEAM_ACCOUNT_ID_BASE
            (root / "config/loginusers.vdf").write_text(
                '"users"\n'
                '{\n'
                f'\t"{base + 111}"\n'
                '\t{\n'
                '\t\t"MostRecent"\t\t"0"\n'
                '\t}\n'
                f'\t"{base + 222}"\n'
                '\t{\n'
                '\t\t"MostRecent"\t\t"1"\n'
                '\t}\n'
                '}\n',
                encoding="utf-8",
            )

            configs = shortcut.user_config_dirs(root)

            self.assertEqual(configs[0], root / "userdata/222/config")

    def test_falls_back_to_config_mtime_when_loginusers_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            older = root / "userdata/111/config"
            newer = root / "userdata/222/config"
            older.mkdir(parents=True)
            newer.mkdir(parents=True)
            older.touch()
            newer.touch()

            configs = shortcut.user_config_dirs(root)

            self.assertEqual(configs[0], newer)


if __name__ == "__main__":
    unittest.main()
