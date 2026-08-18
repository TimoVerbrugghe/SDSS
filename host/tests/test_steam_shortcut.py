"""Tests for the binary shortcuts.vdf writer (deck/add-steam-shortcut.py)."""

import importlib.util
import os
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path

DECK = Path(__file__).resolve().parents[2] / "deck"
_spec = importlib.util.spec_from_file_location(
    "sdss_steam_shortcut", DECK / "add-steam-shortcut.py"
)
assert _spec and _spec.loader
shortcut = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = shortcut
_spec.loader.exec_module(shortcut)


class AppIdTest(unittest.TestCase):
    def test_hashes_the_exe_field_as_stored(self):
        # Steam CRCs the Exe field including its literal quotes; hashing the bare path
        # produces an id Steam never looks up.
        exe = "/home/deck/.local/bin/sdss-connect"
        entry = shortcut.build_entry(exe, "Second Screen", "10.0.0.5", "/home/deck")
        expected = shortcut.shortcut_appid(shortcut.quoted(exe), "Second Screen")
        self.assertEqual(entry["appid"] & 0xFFFFFFFF, expected & 0xFFFFFFFF)
        self.assertNotEqual(expected, shortcut.shortcut_appid(exe, "Second Screen"))

    def test_appid_has_the_high_bit_set(self):
        self.assertTrue(shortcut.shortcut_appid('"/bin/true"', "X") & 0x80000000)

    def test_appid_fits_a_signed_int32_field(self):
        entry = shortcut.build_entry("/bin/true", "X", "", "/")
        self.assertGreaterEqual(entry["appid"], -(2**31))
        self.assertLess(entry["appid"], 2**31)


class RoundTripTest(unittest.TestCase):
    def _entries(self):
        return [
            shortcut.build_entry("/bin/a", "Alpha", "host-a", "/bin"),
            shortcut.build_entry("/bin/b", "Beta", "host-b", "/bin"),
        ]

    def test_serialize_parse_round_trip(self):
        entries = self._entries()
        self.assertEqual(shortcut.parse(shortcut.serialize(entries)), entries)

    def test_empty_shortcuts_file_parses_as_no_entries(self):
        self.assertEqual(shortcut.parse(b""), [])
        self.assertEqual(shortcut.parse(shortcut.serialize([])), [])

    def test_nested_tags_survive(self):
        parsed = shortcut.parse(shortcut.serialize(self._entries()))
        self.assertEqual(parsed[0]["tags"], {"0": "SDSS"})

    def test_launch_options_carry_the_host(self):
        parsed = shortcut.parse(shortcut.serialize(self._entries()))
        self.assertEqual(parsed[0]["LaunchOptions"], "host-a")

    def test_rungameid_shape(self):
        appid = shortcut.shortcut_appid('"/bin/true"', "X")
        self.assertEqual(shortcut.rungameid(appid), (appid << 32) | 0x02000000)

    def test_rungameid_masks_the_signed_appid_stored_in_the_vdf(self):
        """A gameid must not go negative just because the VDF holds appids signed.

        build_entry writes the signed form, so anything read back out of shortcuts.vdf is
        routinely negative; shifting that left by 32 produces a gameid Steam ignores
        without error. Hard-coded value observed working on hardware.
        """
        unsigned = shortcut.shortcut_appid('"/bin/true"', "X")
        signed = struct.unpack("<i", struct.pack("<I", unsigned))[0]
        self.assertLess(signed, 0, "fixture must exercise the negative case")
        self.assertEqual(shortcut.rungameid(signed), shortcut.rungameid(unsigned))
        self.assertGreater(shortcut.rungameid(signed), 0)
        self.assertEqual(shortcut.rungameid(-1257812039), 13044482501723029504)


class TestByteFidelity(unittest.TestCase):
    """A rewrite must not mangle shortcuts this tool did not create."""

    def test_non_utf8_names_round_trip_byte_for_byte(self):
        entry = shortcut.build_entry("/bin/a", "Alpha", "host", "/bin")
        # A latin-1 name, as older Steam versions and hand-added shortcuts contain.
        entry["AppName"] = b"Caf\xe9".decode("utf-8", "surrogateescape")
        data = shortcut.serialize([entry])
        self.assertIn(b"Caf\xe9\x00", data)
        self.assertEqual(shortcut.serialize(shortcut.parse(data)), data)

    def test_round_trip_is_stable_for_ordinary_entries(self):
        data = shortcut.serialize(
            [
                shortcut.build_entry("/bin/a", "Alpha", "host-a", "/bin"),
                shortcut.build_entry("/bin/b", "Beta", "host-b", "/bin"),
            ]
        )
        self.assertEqual(shortcut.serialize(shortcut.parse(data)), data)

    def test_serialize_rejects_unknown_value_types(self):
        with self.assertRaises(shortcut.ShortcutsError):
            shortcut.serialize([{"weird": 1.5}])


class TestCorruptInput(unittest.TestCase):
    """Truncated files must fail with one clear error, never IndexError."""

    def _truncations(self):
        data = shortcut.serialize(
            [shortcut.build_entry("/bin/a", "Alpha", "host-a", "/bin")]
        )
        # Every prefix except the empty one (which legitimately means "no shortcuts").
        return [data[:n] for n in range(1, len(data))]

    def test_every_truncation_raises_shortcuts_error(self):
        for truncated in self._truncations():
            with self.subTest(length=len(truncated)):
                # A bare try/except here would pass silently on the truncations that parse
                # "successfully" — exactly the cases worth knowing about.
                with self.assertRaises(shortcut.ShortcutsError):
                    shortcut.parse(truncated)

    def test_garbage_leading_byte_is_rejected(self):
        with self.assertRaises(shortcut.ShortcutsError):
            shortcut.parse(b"\x99garbage")

    def test_unterminated_string_is_rejected(self):
        with self.assertRaises(shortcut.ShortcutsError):
            shortcut.parse(b"\x00shortcuts")

    def test_header_only_file_is_not_read_as_zero_shortcuts(self):
        """The dangerous shape: a file cut short right after the header once parsed as an
        empty list, so rewriting it would have deleted every shortcut the user had."""
        with self.assertRaises(shortcut.ShortcutsError):
            shortcut.parse(b"\x00shortcuts\x00")


class TestOwnership(unittest.TestCase):
    def test_our_own_entry_is_replaced(self):
        exe = "/home/deck/.local/bin/sdss-connect"
        entry = shortcut.build_entry(exe, "Second Screen", "10.0.0.5", "/home/deck")
        self.assertTrue(shortcut.owned_by_sdss(entry, shortcut.quoted(exe)))

    def test_a_users_own_shortcut_named_second_screen_is_left_alone(self):
        """Someone else's Moonlight launcher may plausibly be called "Second Screen".
        Matching on the name alone would silently delete it."""
        theirs = shortcut.build_entry("/usr/bin/moonlight", "Second Screen", "", "/usr/bin")
        theirs["tags"] = {}
        self.assertFalse(
            shortcut.owned_by_sdss(theirs, shortcut.quoted("/home/deck/.local/bin/sdss-connect"))
        )

    def test_entry_without_our_tag_is_still_matched_by_exe(self):
        """Entries written before the tag existed, or with --name, must still dedupe."""
        exe = "/home/deck/.local/bin/sdss-connect"
        stale = shortcut.build_entry(exe, "Old Name", "1.2.3.4", "/home/deck")
        stale["tags"] = {}
        self.assertTrue(shortcut.owned_by_sdss(stale, shortcut.quoted(exe)))


class TestIdempotency(unittest.TestCase):
    """Exercises the real dedupe helper — reimplementing the filter here would let the
    tests keep passing after the shipped logic changed."""

    def _dedupe(self, entries, exe):
        quoted_exe = shortcut.quoted(exe)
        return [e for e in entries if not shortcut.owned_by_sdss(e, quoted_exe)]

    def test_rerunning_replaces_rather_than_duplicates(self):
        exe = "/home/deck/.local/bin/sdss-connect"
        entry = shortcut.build_entry(exe, "Second Screen", "10.0.0.5", "/home/deck")
        other = shortcut.build_entry("/bin/other", "Other", "", "/bin")
        other["tags"] = {}  # build_entry always tags; a foreign shortcut would not be.
        entries = [other, entry]

        kept = self._dedupe(entries, exe)
        kept.append(entry)

        self.assertEqual(len(kept), 2)
        self.assertEqual([e["AppName"] for e in kept], ["Other", "Second Screen"])

    def test_renamed_shortcut_is_still_deduped_by_exe(self):
        exe = "/home/deck/.local/bin/sdss-connect"
        stale = shortcut.build_entry(exe, "Old Name", "1.2.3.4", "/home/deck")
        self.assertEqual(self._dedupe([stale], exe), [])

    def test_an_unrelated_shortcut_survives(self):
        exe = "/home/deck/.local/bin/sdss-connect"
        theirs = shortcut.build_entry("/usr/bin/moonlight", "Second Screen", "", "/usr/bin")
        theirs["tags"] = {}
        self.assertEqual(self._dedupe([theirs], exe), [theirs])


class TestBackupPruning(unittest.TestCase):
    def test_only_the_newest_backups_survive(self):
        with tempfile.TemporaryDirectory() as raw:
            config = Path(raw)
            for index in range(6):
                backup = config / f"shortcuts.vdf.sdss-{1000 + index}.bak"
                backup.write_bytes(b"x")
                os.utime(backup, (1000 + index, 1000 + index))
            shortcut.prune_backups(config, keep=3)
            remaining = sorted(p.name for p in config.glob("*.bak"))
            self.assertEqual(
                remaining,
                [
                    "shortcuts.vdf.sdss-1003.bak",
                    "shortcuts.vdf.sdss-1004.bak",
                    "shortcuts.vdf.sdss-1005.bak",
                ],
            )


class TestRemoval(unittest.TestCase):
    """`--remove` is what the uninstaller calls; a shortcut left behind points Steam at a
    launcher that no longer exists."""

    def _steam_root(self, entries):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        config = root / "userdata" / "12345" / "config"
        config.mkdir(parents=True)
        (config / "shortcuts.vdf").write_bytes(shortcut.serialize(entries))
        return root, config / "shortcuts.vdf"

    def test_removes_our_entry_and_keeps_the_others(self):
        exe = "/home/deck/.local/bin/sdss-connect"
        mine = shortcut.build_entry(exe, "Second Screen", "10.0.0.5", "/home/deck")
        theirs = shortcut.build_entry("/usr/bin/other", "Other", "", "/usr/bin")
        theirs["tags"] = {}
        root, path = self._steam_root([theirs, mine])

        code = shortcut.main(
            ["--remove", "--force", "--exe", exe, "--steam-root", str(root)]
        )

        self.assertEqual(code, 0)
        self.assertEqual(
            [e["AppName"] for e in shortcut.parse(path.read_bytes())], ["Other"]
        )

    def test_removal_does_not_require_the_launcher_to_still_exist(self):
        """The uninstaller deletes the release first; refusing here would strand the
        shortcut in the user's library forever."""
        exe = "/nonexistent/sdss-connect"
        root, path = self._steam_root(
            [shortcut.build_entry(exe, "Second Screen", "10.0.0.5", "/tmp")]
        )

        code = shortcut.main(
            ["--remove", "--force", "--exe", exe, "--steam-root", str(root)]
        )

        self.assertEqual(code, 0)
        self.assertEqual(shortcut.parse(path.read_bytes()), [])

    def test_a_file_without_our_shortcut_is_left_byte_identical(self):
        theirs = shortcut.build_entry("/usr/bin/other", "Other", "", "/usr/bin")
        theirs["tags"] = {}
        root, path = self._steam_root([theirs])
        before = path.read_bytes()

        code = shortcut.main(
            [
                "--remove",
                "--force",
                "--exe",
                "/home/deck/.local/bin/sdss-connect",
                "--steam-root",
                str(root),
            ]
        )

        self.assertEqual(code, 0)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.glob("*.bak")), [])


class TestLibraryArtwork(unittest.TestCase):
    def _steam_root(self, entries):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        config = root / "userdata" / "12345" / "config"
        config.mkdir(parents=True)
        (config / "shortcuts.vdf").write_bytes(shortcut.serialize(entries))
        return root, config

    def _assets_dir(self):
        assets = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, assets, True)
        for _, name in shortcut.LIBRARY_ASSETS:
            (assets / name).write_bytes(name.encode("utf-8"))
        return assets

    def _grid_files(self, appid):
        file_id = appid & 0xFFFFFFFF
        return [f"{file_id}{suffix}.png" for suffix, _ in shortcut.LIBRARY_ASSETS]

    def test_add_writes_grid_assets_for_the_shortcut(self):
        root, config = self._steam_root([])
        assets = self._assets_dir()
        launcher = root / "sdss-connect"
        launcher.write_text("#!/bin/sh\n")
        appid = shortcut.shortcut_appid(shortcut.quoted(str(launcher)), "Second Screen")

        code = shortcut.main(
            [
                "--force",
                "--steam-root",
                str(root),
                "--exe",
                str(launcher),
                "--assets-dir",
                str(assets),
                "10.0.0.5",
            ]
        )

        self.assertEqual(code, 0)
        for name in self._grid_files(appid):
            self.assertTrue((config / "grid" / name).is_file(), name)

    def test_remove_deletes_grid_assets_for_removed_entries(self):
        exe = "/home/deck/.local/bin/sdss-connect"
        mine = shortcut.build_entry(exe, "Second Screen", "10.0.0.5", "/home/deck")
        root, config = self._steam_root([mine])
        appid = mine["appid"]
        grid = config / "grid"
        grid.mkdir(parents=True)
        for name in self._grid_files(appid):
            (grid / name).write_bytes(b"x")

        code = shortcut.main(
            ["--remove", "--force", "--steam-root", str(root), "--exe", exe]
        )

        self.assertEqual(code, 0)
        for name in self._grid_files(appid):
            self.assertFalse((grid / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
