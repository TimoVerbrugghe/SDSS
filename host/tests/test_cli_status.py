"""Tests for `sdss status --json`, the contract the Decky plugin depends on."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sdss import cli, profiles, state


class StatusJsonTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        previous = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = self._tmp.name
        self.addCleanup(
            lambda: os.environ.__setitem__("XDG_STATE_HOME", previous)
            if previous is not None
            else os.environ.pop("XDG_STATE_HOME", None)
        )

    def _status(self, *argv: str) -> str:
        parser = cli.build_parser()
        args = parser.parse_args(["status", *argv])
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(args.func(args), 0)
        return buffer.getvalue()

    def test_reports_every_known_profile(self):
        payload = json.loads(self._status("--json"))
        self.assertEqual(
            [entry["id"] for entry in payload["profiles"]],
            [profile.id for profile in profiles.PROFILES],
        )

    def test_profile_ids_are_not_guessable_abbreviations(self):
        # The plugin used to hardcode "melon"; the real id is "melonds".
        ids = {profile.id for profile in profiles.PROFILES}
        self.assertIn("melonds", ids)

    def test_reflects_saved_state(self):
        first = profiles.PROFILES[0].id
        state.save(state.State(enabled=True, profiles={first: False}))
        payload = json.loads(self._status("--json"))
        self.assertTrue(payload["enabled"])
        by_id = {entry["id"]: entry for entry in payload["profiles"]}
        self.assertFalse(by_id[first]["enabled"])
        self.assertTrue(by_id[profiles.PROFILES[1].id]["enabled"])

    def test_carries_the_sunshine_port(self):
        payload = json.loads(self._status("--json"))
        self.assertIsInstance(payload["sunshine_port"], int)

    def test_plain_output_is_not_json(self):
        with self.assertRaises(json.JSONDecodeError):
            json.loads(self._status())

    def test_state_file_lands_in_the_state_dir(self):
        state.save(state.State(enabled=True))
        self.assertTrue((Path(self._tmp.name) / "sdss/state.json").is_file())


class EnableTest(StatusJsonTest):
    def _enable(self, *argv: str) -> int:
        parser = cli.build_parser()
        args = parser.parse_args(list(argv))
        with redirect_stdout(io.StringIO()):
            return args.func(args)

    def test_unknown_profile_is_rejected(self):
        self.assertEqual(self._enable("enable", "--profile", "melon"), 2)
        self.assertEqual(json.loads(self._status("--json"))["enabled"], False)

    def test_profile_toggle_does_not_move_the_master_switch(self):
        first = profiles.PROFILES[0].id
        self.assertEqual(self._enable("enable"), 0)
        self.assertEqual(self._enable("disable", "--profile", first), 0)
        payload = json.loads(self._status("--json"))
        self.assertTrue(payload["enabled"])
        by_id = {entry["id"]: entry for entry in payload["profiles"]}
        self.assertFalse(by_id[first]["enabled"])

    def test_master_toggle_gates_every_profile(self):
        self.assertEqual(self._enable("disable"), 0)
        payload = json.loads(self._status("--json"))
        self.assertFalse(any(entry["enabled"] for entry in payload["profiles"]))


if __name__ == "__main__":
    unittest.main()
