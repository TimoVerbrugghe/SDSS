"""Tests for the machine-readable surfaces the desktop app consumes.

`sdss doctor --json` and the fields added to `sdss status --json` are a contract: the app
must never have to parse prose written for a human, and the Decky plugin ships prebuilt,
so the pre-existing `status --json` shape may only ever be extended.
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sdss import cli, doctor, paths, patch, profiles, release


class _IsolatedHome(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for var in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
            self._set(var, str(self.root / var.lower()))

    def _set(self, name: str, value: str | None) -> None:
        previous = os.environ.get(name)

        def restore() -> None:
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous

        self.addCleanup(restore)
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

    def _run(self, *argv: str) -> tuple[int, str]:
        parser = cli.build_parser()
        args = parser.parse_args(list(argv))
        args.command = []
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = args.func(args)
        return code, buffer.getvalue()


class DoctorJsonTest(_IsolatedHome):
    def test_json_and_text_describe_the_same_checks(self):
        report = doctor.run()
        _, text = self._run("doctor")
        payload = json.loads(self._run("doctor", "--json")[1])
        self.assertEqual(
            [check["id"] for check in payload["checks"]],
            [check.id for check in report.checks],
        )
        self.assertEqual(payload["problems"], report.problems)
        self.assertIn(f"{report.problems} problem(s)", text)

    def test_exit_code_tracks_problem_count(self):
        code, output = self._run("doctor", "--json")
        payload = json.loads(output)
        self.assertEqual(code, 1 if payload["problems"] else 0)

    def test_every_check_carries_the_fields_the_app_renders(self):
        payload = json.loads(self._run("doctor", "--json")[1])
        self.assertTrue(payload["checks"])
        for check in payload["checks"]:
            for field in ("id", "section", "label", "detail", "ok", "problem"):
                self.assertIn(field, check, check)
            self.assertIsInstance(check["ok"], bool)
            self.assertIsInstance(check["problem"], bool)

    def test_check_ids_are_unique(self):
        ids = [check.id for check in doctor.run().checks]
        self.assertEqual(len(ids), len(set(ids)))

    def test_informational_rows_are_never_counted_as_problems(self):
        # Session variables and missing emulator configs are reported, but an emulator the
        # user does not own must not make the app show a red install.
        for check in doctor.run().checks:
            if check.section == "emulator configs" or check.id.startswith("env:"):
                self.assertFalse(check.problem, check)

    def test_a_stale_journal_is_a_problem_with_a_fix(self):
        before = {check.id for check in doctor.run().checks}
        self.assertNotIn("stale-journal", before)
        journal = patch.Journal(paths.backup_dir(), "session")
        target = self.root / "config.ini"
        target.write_text("[Layout]\n")
        journal.record(target)
        stale = {check.id: check for check in doctor.run().checks}
        self.assertIn("stale-journal", stale)
        self.assertTrue(stale["stale-journal"].problem)
        self.assertEqual(stale["stale-journal"].fix, "restore")

    def test_plain_doctor_output_is_not_json(self):
        with self.assertRaises(json.JSONDecodeError):
            json.loads(self._run("doctor")[1])


class StatusReleaseTest(_IsolatedHome):
    def test_release_is_reported_absent_before_an_install(self):
        payload = json.loads(self._run("status", "--json")[1])
        self.assertEqual(payload["release"]["present"], "no")
        self.assertIsNone(payload["release"]["version"])
        self.assertIsNone(payload["release"]["role"])

    def test_release_marker_and_role_are_read_back(self):
        release_dir = release.release_dir()
        release_dir.mkdir(parents=True)
        (release_dir / release.MARKER_NAME).write_text(
            json.dumps({"version": "1.2.3", "installed_at": "2026-08-16T00:00:00Z"})
        )
        release.role_file().parent.mkdir(parents=True, exist_ok=True)
        release.role_file().write_text("steam-machine\n")
        payload = json.loads(self._run("status", "--json")[1])
        self.assertEqual(payload["release"]["version"], "1.2.3")
        self.assertEqual(payload["release"]["installed_at"], "2026-08-16T00:00:00Z")
        self.assertEqual(payload["release"]["role"], "steam-machine")
        self.assertEqual(payload["release"]["present"], "yes")

    def test_an_unparseable_marker_does_not_break_status(self):
        release_dir = release.release_dir()
        release_dir.mkdir(parents=True)
        (release_dir / release.MARKER_NAME).write_text("{ truncated")
        payload = json.loads(self._run("status", "--json")[1])
        self.assertIsNone(payload["release"]["version"])

    def test_an_unrecognised_role_is_reported_as_none(self):
        release.role_file().parent.mkdir(parents=True, exist_ok=True)
        release.role_file().write_text("laptop\n")
        payload = json.loads(self._run("status", "--json")[1])
        self.assertIsNone(payload["release"]["role"])

    def test_patched_configs_are_listed_from_the_journal(self):
        payload = json.loads(self._run("status", "--json")[1])
        self.assertEqual(payload["patched_configs"], [])
        target = self.root / "qt-config.ini"
        target.write_text("[Layout]\n")
        patch.Journal(paths.backup_dir(), "session").record(target)
        payload = json.loads(self._run("status", "--json")[1])
        self.assertEqual(payload["patched_configs"], [str(target)])

    def test_an_unreadable_journal_does_not_break_status(self):
        journal = patch.Journal(paths.backup_dir(), "session")
        journal.dir.mkdir(parents=True)
        journal.manifest.write_text("[ truncated")
        payload = json.loads(self._run("status", "--json")[1])
        self.assertEqual(payload["patched_configs"], [])

    def test_profiles_still_carry_the_keys_the_decky_bundle_reads(self):
        payload = json.loads(self._run("status", "--json")[1])
        self.assertEqual(
            [entry["id"] for entry in payload["profiles"]],
            [profile.id for profile in profiles.PROFILES],
        )
        for entry in payload["profiles"]:
            for field in ("id", "name", "system", "verified", "enabled", "hooked"):
                self.assertIn(field, entry)

    def test_source_version_reads_the_repo_version_file(self):
        root = Path(__file__).resolve().parents[2]
        self.assertNotEqual(release.source_version(root), "unknown")
        self.assertEqual(release.source_version(self.root), "unknown")


if __name__ == "__main__":
    unittest.main()
