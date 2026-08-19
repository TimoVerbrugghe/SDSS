"""Tests for `sdss status --json`, the contract the Decky plugin depends on."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sdss import cli, patch, profiles, state


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
        reconcile = mock.patch.object(
            cli.managed_config, "reconcile", return_value=[]
        )
        reconcile.start()
        self.addCleanup(reconcile.stop)
        hooks = mock.patch.object(cli.hooks, "reconcile", return_value=False)
        hooks.start()
        self.addCleanup(hooks.stop)

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

    def test_enable_reconciles_managed_configs_for_effective_profiles(self):
        self.assertEqual(self._enable("enable"), 0)
        cli.managed_config.reconcile.assert_called_once_with(
            {profile.id: True for profile in profiles.PROFILES}
        )

    def test_restore_disables_state_and_restores_all_managed_configs(self):
        state.save(state.State(enabled=True))
        with mock.patch.object(
            cli.managed_config, "restore_all", return_value=[]
        ) as restore:
            self.assertEqual(self._enable("restore"), 0)
        restore.assert_called_once_with()
        self.assertFalse(state.load().enabled)

    def test_failed_enable_rolls_configs_back_and_keeps_saved_state(self):
        disabled = {profile.id: False for profile in profiles.PROFILES}
        enabled = {profile.id: True for profile in profiles.PROFILES}
        cli.managed_config.reconcile.side_effect = [
            patch.PatchError("broken config"),
            [],
        ]

        self.assertEqual(self._enable("enable"), 1)

        self.assertFalse(state.load().enabled)
        self.assertEqual(
            cli.managed_config.reconcile.call_args_list,
            [mock.call(enabled), mock.call(disabled)],
        )

    def test_state_save_is_atomic(self):
        state.save(state.State(enabled=True))
        state_path = Path(self._tmp.name) / "sdss/state.json"
        self.assertTrue(state_path.is_file())
        self.assertEqual(list(state_path.parent.glob(".state.json.sdss-tmp")), [])


if __name__ == "__main__":
    unittest.main()
