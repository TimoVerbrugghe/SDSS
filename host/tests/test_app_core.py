"""Tests for the desktop app's decision layer.

`app/core` is standard-library only precisely so it can be exercised here, on a runner
with no display, no SteamOS and no PySide6. What is asserted is the thing that actually
breaks in the field: the *command* that gets run, and the refusal paths (no elevation, no
network, a bad checksum) that a happy-path test would never reach.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app import cli as app_cli
from app.core import actions, elevate, paths, probe, runner, selfinstall, update


class FakeRunner:
    """Records commands and replays canned results, so nothing is ever executed."""

    def __init__(self, results=None):
        self.results = dict(results or {})
        self.calls = []

    def __call__(self, command, on_line=None, **kwargs):
        argv = [str(part) for part in command]
        self.calls.append({"argv": argv, "kwargs": kwargs})
        key = argv[0] if argv else ""
        result = self.results.get(" ".join(argv), self.results.get(key))
        if result is None:
            result = runner.Result(command=argv, returncode=0, lines=[])
        if on_line:
            for line in result.lines:
                on_line(line)
        return result


def result(returncode=0, output=""):
    return runner.Result(
        command=["fake"], returncode=returncode, lines=output.splitlines()
    )


class _Sandbox(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.payload = Path(self._tmp.name) / "payload"
        (self.payload / "packaging").mkdir(parents=True)
        (self.payload / "install.sh").write_text("#!/bin/sh\n")
        (self.payload / "packaging/uninstall.sh").write_text("#!/bin/sh\n")
        (self.payload / "packaging/install-udev-rule.sh").write_text("#!/bin/sh\n")
        (self.payload / "VERSION").write_text("9.9.9\n")
        self.home.mkdir()
        self._env = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "XDG_CONFIG_HOME": str(self.home / ".config"),
                "XDG_STATE_HOME": str(self.home / ".local/state"),
                "XDG_DATA_HOME": str(self.home / ".local/share"),
                paths.PAYLOAD_VAR: str(self.payload),
            },
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def install_root(self) -> Path:
        return paths.install_root()

    def make_installed(self, role="steam-machine", version="1.0.0"):
        root = self.install_root()
        (root / "packaging").mkdir(parents=True)
        (root / "packaging/uninstall.sh").write_text("#!/bin/sh\n")
        (root / "packaging/install-udev-rule.sh").write_text("#!/bin/sh\n")
        (root / "install.sh").write_text("#!/bin/sh\n")
        (root / ".sdss-release.json").write_text(
            json.dumps({"version": version, "installed_at": "2026-08-16T10:00:00Z"})
        )
        config = paths.config_dir()
        config.mkdir(parents=True, exist_ok=True)
        (config / "installed-role").write_text(role + "\n")
        return root


class PathsTest(_Sandbox):
    def test_locations_follow_the_environment(self):
        self.assertEqual(paths.install_root(), self.home / ".local/share/sdss/release")
        self.assertEqual(paths.payload_root(), self.payload)
        self.assertEqual(paths.desktop_entry().name, "sdss.desktop")

    def test_payload_comes_from_appdir_inside_an_appimage(self):
        with mock.patch.dict(os.environ, {paths.PAYLOAD_VAR: "", paths.APPDIR_VAR: "/tmp/mnt"}):
            os.environ.pop(paths.PAYLOAD_VAR)
            self.assertEqual(paths.payload_root(), Path("/tmp/mnt/usr/share/sdss"))

    def test_install_root_matches_the_installer(self):
        # install.sh hardcodes the same path; if they drift the app manages a directory
        # nothing else writes to.
        script = Path(__file__).resolve().parents[2] / "install.sh"
        self.assertIn(
            'INSTALL_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/sdss/release"',
            script.read_text(),
        )


class ActionsTest(_Sandbox):
    def test_install_runs_the_payload_installer(self):
        command = actions.install_command("steam-machine")
        self.assertEqual(command, [str(self.payload / "install.sh"), "--role", "steam-machine"])

    def test_install_from_the_payload_even_when_a_release_exists(self):
        # The whole point of the app is that a newer AppImage *is* newer code: an update
        # that re-ran the installed copy could never install anything new.
        self.make_installed()
        self.assertTrue(
            actions.install_command("steam-machine")[0].startswith(str(self.payload))
        )

    def test_deck_install_carries_the_host(self):
        self.assertIn("--host", actions.install_command("steam-deck", "10.0.0.5"))
        self.assertNotIn("--host", actions.install_command("steam-machine", "10.0.0.5"))

    def test_deck_install_without_a_host_omits_the_flag(self):
        self.assertEqual(
            actions.install_command("steam-deck"), [str(self.payload / "install.sh"), "--role", "steam-deck"]
        )

    def test_uninstall_never_prompts(self):
        command = actions.uninstall_command()
        self.assertIn("--yes", command)
        self.assertNotIn("--keep-configs", command)
        self.assertIn("--keep-configs", actions.uninstall_command(keep_configs=True))

    def test_uninstall_prefers_the_installed_copy(self):
        self.assertTrue(actions.uninstall_command()[0].startswith(str(self.payload)))
        self.make_installed()
        self.assertTrue(actions.uninstall_command()[0].startswith(str(self.install_root())))

    def test_sdss_command_uses_the_shim_when_present(self):
        self.assertEqual(actions.sdss_command("restore")[0], "python3")
        shim = paths.sdss_bin()
        shim.parent.mkdir(parents=True)
        shim.write_text("#!/bin/sh\n")
        self.assertEqual(actions.sdss_command("restore"), [str(shim), "restore"])

    def test_sdss_fallback_targets_the_package_source(self):
        command = actions.sdss_command("status", "--json")
        self.assertIn(str(self.payload / "host/src"), command[2])
        self.assertEqual(command[-2:], ["status", "--json"])

    def test_toggle_and_restore(self):
        self.assertEqual(actions.toggle_command(True)[-1], "enable")
        self.assertEqual(actions.toggle_command(False, "cemu")[-3:], ["disable", "--profile", "cemu"])
        self.assertEqual(actions.restore_command()[-1], "restore")

    def test_remove_etc_targets_exactly_the_two_documented_files(self):
        self.assertEqual(
            actions.remove_etc_command(),
            ["rm", "-f", str(probe.UDEV_RULE), str(probe.ATOMIC_KEEP)],
        )


class ProbeTest(_Sandbox):
    def test_role_detection_matches_the_dmi_names(self):
        product = Path(self._tmp.name) / "product_name"
        for name, expected in [
            ("Jupiter", probe.STEAM_DECK),
            ("Galileo", probe.STEAM_DECK),
            ("Fremont", probe.STEAM_MACHINE),
            ("ThinkPad", None),
        ]:
            product.write_text(name + "\n")
            self.assertEqual(probe.detect_role(product), expected, name)

    def test_missing_dmi_file_is_not_an_error(self):
        self.assertIsNone(probe.detect_role(Path(self._tmp.name) / "nope"))

    def test_detected_role_matches_installer_detection(self):
        script = (Path(__file__).resolve().parents[2] / "install.sh").read_text()
        self.assertIn("Jupiter|Galileo) echo \"steam-deck\"", script)
        self.assertIn("Fremont) echo \"steam-machine\"", script)

    def test_installed_role_rejects_junk(self):
        config = paths.config_dir()
        config.mkdir(parents=True)
        (config / "installed-role").write_text("laptop\n")
        self.assertIsNone(probe.installed_role())
        (config / "installed-role").write_text("steam-deck\n")
        self.assertEqual(probe.installed_role(), probe.STEAM_DECK)

    def test_uninstalled_device_reports_no_checks(self):
        status = probe.probe(FakeRunner())
        self.assertFalse(status.installed)
        self.assertEqual(status.checks, [])

    def test_installed_host_reports_the_marker_and_checks(self):
        self.make_installed(version="1.2.3")
        status = probe.probe(FakeRunner({"podman": result(1)}))
        self.assertTrue(status.installed)
        self.assertEqual(status.installed_version, "1.2.3")
        self.assertEqual(status.role, probe.STEAM_MACHINE)
        ids = [check.id for check in status.checks]
        for expected in ("sunshine", "shim", "udev-rule", "compositor-image", "decky-plugin"):
            self.assertIn(expected, ids)

    def test_deck_checks_replace_host_checks(self):
        self.make_installed(role="steam-deck")
        status = probe.probe(FakeRunner())
        ids = [check.id for check in status.checks]
        self.assertIn("moonlight", ids)
        self.assertIn("controller-template", ids)
        self.assertNotIn("udev-rule", ids)

    def test_missing_compositor_image_is_a_problem_with_a_repair(self):
        self.make_installed()
        status = probe.probe(FakeRunner({"podman": result(1)}))
        check = {c.id: c for c in status.checks}["compositor-image"]
        self.assertFalse(check.ok)
        self.assertEqual(check.fix, probe.FIX_REPAIR)
        self.assertIn(check, status.problems)

    def test_present_compositor_image_is_ok(self):
        self.make_installed()
        status = probe.probe(FakeRunner({"podman": result(0)}))
        self.assertTrue({c.id: c for c in status.checks}["compositor-image"].ok)

    def test_advisory_rows_never_make_the_install_look_broken(self):
        self.make_installed()
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin"}):
            status = probe.probe(FakeRunner({"podman": result(0)}))
        path_check = {c.id: c for c in status.checks}["path"]
        self.assertFalse(path_check.ok)
        self.assertTrue(path_check.advisory)
        self.assertNotIn(path_check, status.problems)

    def test_a_patched_config_is_surfaced_with_a_restore(self):
        self.make_installed()
        shim = paths.sdss_bin()
        shim.parent.mkdir(parents=True)
        shim.write_text("#!/bin/sh\n")
        payload = json.dumps({"enabled": True, "patched_configs": ["/home/deck/qt-config.ini"]})
        status = probe.probe(FakeRunner({str(shim): result(0, payload)}))
        check = {c.id: c for c in status.checks}["patched-configs"]
        self.assertFalse(check.ok)
        self.assertEqual(check.fix, probe.FIX_RESTORE)

    def test_unparseable_sdss_status_is_ignored(self):
        self.make_installed()
        shim = paths.sdss_bin()
        shim.parent.mkdir(parents=True)
        shim.write_text("#!/bin/sh\n")
        status = probe.probe(FakeRunner({str(shim): result(0, "not json")}))
        self.assertIsNone(status.sdss)

    def test_status_serialises_to_json(self):
        self.make_installed()
        payload = probe.probe(FakeRunner()).to_json()
        json.dumps(payload)
        self.assertEqual(payload["role"], probe.STEAM_MACHINE)


class ElevateTest(unittest.TestCase):
    def test_command_wrapping_per_method(self):
        self.assertEqual(elevate.build(["x"], elevate.NONE), ["x"])
        self.assertEqual(elevate.build(["x"], elevate.PKEXEC), ["pkexec", "x"])
        self.assertEqual(elevate.build(["x"], elevate.SUDO_NOPASSWD), ["sudo", "-n", "x"])
        self.assertEqual(
            elevate.build(["x"], elevate.SUDO_PASSWORD), ["sudo", "-S", "-p", "", "x"]
        )

    def test_unknown_method_is_rejected(self):
        with self.assertRaises(ValueError):
            elevate.build(["x"], "magic")

    def test_passwordless_sudo_wins(self):
        fake = FakeRunner({"sudo -n true": result(0)})
        with mock.patch("shutil.which", return_value="/usr/bin/sudo"):
            self.assertEqual(elevate.plan(fake).method, elevate.SUDO_NOPASSWD)

    def test_pkexec_is_only_used_with_a_running_agent(self):
        fake = FakeRunner({"sudo -n true": result(1), "passwd": result(0, "deck P 2026")})
        with mock.patch("shutil.which", return_value="/usr/bin/x"), mock.patch.object(
            elevate, "polkit_agent_running", return_value=True
        ):
            self.assertEqual(elevate.plan(fake).method, elevate.PKEXEC)
        with mock.patch("shutil.which", return_value="/usr/bin/x"), mock.patch.object(
            elevate, "polkit_agent_running", return_value=False
        ):
            plan = elevate.plan(fake)
        self.assertEqual(plan.method, elevate.SUDO_PASSWORD)
        self.assertTrue(plan.needs_password)

    def test_an_account_with_no_password_is_reported_not_looped_on(self):
        fake = FakeRunner({"sudo -n true": result(1), "passwd": result(0, "deck NP 2026 0 99999 7 -1")})
        with mock.patch("shutil.which", return_value="/usr/bin/x"), mock.patch.object(
            elevate, "polkit_agent_running", return_value=False
        ):
            plan = elevate.plan(fake)
        self.assertEqual(plan.method, elevate.UNAVAILABLE)
        self.assertFalse(plan.possible)
        self.assertIn("passwd", plan.reason)

    def test_password_status_parses_passwd_s(self):
        with mock.patch("shutil.which", return_value="/usr/bin/passwd"):
            self.assertEqual(
                elevate.password_status(FakeRunner({"passwd": result(0, "deck P 2026")})),
                elevate.PASSWORD_SET,
            )
            self.assertEqual(
                elevate.password_status(FakeRunner({"passwd": result(0, "deck L 2026")})),
                elevate.PASSWORD_LOCKED,
            )
            self.assertEqual(
                elevate.password_status(FakeRunner({"passwd": result(1, "")})),
                elevate.PASSWORD_UNKNOWN,
            )

    def test_password_goes_to_stdin_and_not_into_the_command(self):
        fake = FakeRunner()
        plan = elevate.Plan(elevate.SUDO_PASSWORD, needs_password=True)
        elevate.run_elevated(["/x.sh"], plan, "hunter2", runner=fake)
        call = fake.calls[0]
        self.assertNotIn("hunter2", " ".join(call["argv"]))
        self.assertEqual(call["kwargs"]["stdin_text"], "hunter2\n")

    def test_a_method_needing_a_password_refuses_to_run_without_one(self):
        with self.assertRaises(ValueError):
            elevate.run_elevated(
                ["/x.sh"], elevate.Plan(elevate.SUDO_PASSWORD, needs_password=True), runner=FakeRunner()
            )

    def test_an_impossible_plan_refuses_to_run(self):
        with self.assertRaises(ValueError):
            elevate.run_elevated(
                ["/x.sh"], elevate.Plan(elevate.UNAVAILABLE, needs_password=False, reason="no"),
                runner=FakeRunner(),
            )

    def test_no_password_is_sent_for_pkexec(self):
        fake = FakeRunner()
        elevate.run_elevated(
            ["/x.sh"], elevate.Plan(elevate.PKEXEC, needs_password=False), runner=fake
        )
        self.assertIsNone(fake.calls[0]["kwargs"]["stdin_text"])


class RunnerTest(_Sandbox):
    def test_output_is_streamed_and_returned(self):
        seen = []
        res = runner.run(["sh", "-c", "echo one; echo two >&2"], seen.append)
        self.assertTrue(res.ok)
        self.assertEqual(sorted(seen), ["one", "two"])

    def test_failure_is_reported_not_raised(self):
        self.assertEqual(runner.run(["sh", "-c", "exit 3"]).returncode, 3)

    def test_a_missing_program_is_reported_not_raised(self):
        res = runner.run(["definitely-not-a-program-xyz"])
        self.assertFalse(res.ok)
        self.assertIn("could not run", res.output)

    def test_stdin_is_closed_so_prompts_do_not_hang(self):
        # Every SDSS script guards its prompts with `read -r -p ... || answer=""`, which
        # only takes the default if stdin is at EOF. An inherited terminal here would hang
        # the app forever on a prompt no one can see.
        res = runner.run(["sh", "-c", 'read -r x || x=default; echo "got:$x"'])
        self.assertIn("got:default", res.output)

    def test_a_password_reaches_the_child_but_never_the_log(self):
        res = runner.run(["sh", "-c", "read -r p; echo len:${#p}"], stdin_text="hunter2\n")
        self.assertIn("len:7", res.output)
        self.assertNotIn("hunter2", paths.log_file().read_text())

    def test_commands_are_logged(self):
        runner.run(["sh", "-c", "echo hello"])
        self.assertIn("hello", paths.log_file().read_text())


class SelfInstallTest(_Sandbox):
    def _fake_appimage(self) -> Path:
        appimage = Path(self._tmp.name) / "SDSS-x86_64.AppImage"
        appimage.write_bytes(b"AI\x02payload")
        appimage.chmod(0o755)
        return appimage

    def test_copies_itself_into_applications_and_writes_one_entry(self):
        installed = selfinstall.install_self(self._fake_appimage())
        self.assertTrue(installed.copied)
        self.assertEqual(installed.target, self.home / "Applications/SDSS.AppImage")
        self.assertTrue(os.access(installed.target, os.X_OK))
        self.assertTrue(installed.desktop_entry.is_file())

    def test_running_from_applications_does_not_copy_over_itself(self):
        target = paths.installed_appimage()
        target.parent.mkdir(parents=True)
        target.write_bytes(b"AI\x02payload")
        installed = selfinstall.install_self(target)
        self.assertFalse(installed.copied)
        self.assertEqual(target.read_bytes(), b"AI\x02payload")

    def test_a_checkout_has_nothing_to_install(self):
        self.assertIsNone(selfinstall.install_self(None))

    def test_the_legacy_launcher_is_removed(self):
        legacy = paths.legacy_desktop_entry()
        legacy.parent.mkdir(parents=True)
        legacy.write_text("[Desktop Entry]\n")
        selfinstall.install_self(self._fake_appimage())
        self.assertFalse(legacy.exists())

    def test_entry_falls_back_to_extract_and_run_without_fuse(self):
        appimage = self._fake_appimage()
        selfinstall.write_desktop_entry(appimage, fuse=False)
        text = paths.desktop_entry().read_text()
        self.assertIn("APPIMAGE_EXTRACT_AND_RUN=1", text)
        selfinstall.write_desktop_entry(appimage, fuse=True)
        self.assertNotIn("APPIMAGE_EXTRACT_AND_RUN", paths.desktop_entry().read_text())

    def test_exec_line_quotes_paths_with_spaces(self):
        self.assertEqual(selfinstall.exec_line(Path("/a b/S.AppImage")), '"/a b/S.AppImage"')
        self.assertEqual(selfinstall.exec_line(Path("/opt/S.AppImage")), "/opt/S.AppImage")


class UpdateTest(_Sandbox):
    RELEASE = {
        "tag_name": "v1.5.0",
        "body": "notes",
        "assets": [
            {
                "name": "SDSS-x86_64.AppImage",
                "browser_download_url": "https://github.com/o/r/releases/download/v1.5.0/SDSS-x86_64.AppImage",
            },
            {
                "name": "SHA256SUMS",
                "browser_download_url": "https://github.com/o/r/releases/download/v1.5.0/SHA256SUMS",
            },
        ],
    }

    def _opener(self, bodies):
        class Response:
            def __init__(self, data):
                self._data = data

            def read(self, _size=None):
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def opener(request, timeout=None):
            url = request.full_url
            if url not in bodies:
                raise OSError(f"unexpected url {url}")
            return Response(bodies[url])

        return opener

    def test_version_comparison(self):
        self.assertTrue(update.is_newer("v1.5.0", "1.4.9"))
        self.assertTrue(update.is_newer("1.5.0", "unknown"))
        self.assertFalse(update.is_newer("1.5.0", "1.5.0"))
        self.assertFalse(update.is_newer("1.4.0", "1.5.0"))
        self.assertTrue(update.is_newer("0.10.0", "0.9.0"))

    def test_latest_release_picks_the_appimage_and_checksum(self):
        bodies = {
            "https://api.github.com/repos/o/r/releases/latest": json.dumps(self.RELEASE).encode()
        }
        info = update.latest_release("o/r", self._opener(bodies))
        self.assertEqual(info.version, "v1.5.0")
        self.assertTrue(info.url.endswith(".AppImage"))
        self.assertTrue(info.checksum_url.endswith("SHA256SUMS"))
        self.assertEqual(info.filename, "SDSS-x86_64.AppImage")

    def test_being_offline_is_an_update_error_not_a_crash(self):
        def opener(request, timeout=None):
            raise OSError("Network is unreachable")

        with self.assertRaises(update.UpdateError):
            update.latest_release("o/r", opener)

    def test_a_release_without_an_appimage_is_rejected(self):
        payload = dict(self.RELEASE, assets=[])
        bodies = {"https://api.github.com/repos/o/r/releases/latest": json.dumps(payload).encode()}
        with self.assertRaises(update.UpdateError):
            update.latest_release("o/r", self._opener(bodies))

    def test_non_github_urls_are_refused(self):
        payload = dict(
            self.RELEASE,
            assets=[{"name": "x.AppImage", "browser_download_url": "https://evil.example/x.AppImage"}],
        )
        bodies = {"https://api.github.com/repos/o/r/releases/latest": json.dumps(payload).encode()}
        with self.assertRaises(update.UpdateError):
            update.latest_release("o/r", self._opener(bodies))

    def test_plain_http_is_refused(self):
        with self.assertRaises(update.UpdateError):
            update._fetch("http://api.github.com/x")

    def test_checksum_parsing_prefers_the_matching_filename(self):
        text = (
            "0" * 64 + "  other.AppImage\n" + "a" * 64 + "  SDSS-x86_64.AppImage\n"
        )
        self.assertEqual(update.parse_checksum(text, "SDSS-x86_64.AppImage"), "a" * 64)
        self.assertEqual(update.parse_checksum("b" * 64 + "\n", "SDSS-x86_64.AppImage"), "b" * 64)
        self.assertIsNone(update.parse_checksum("nothing here\n", "x"))

    def test_download_verifies_the_checksum(self):
        data = b"appimage-bytes"
        digest = __import__("hashlib").sha256(data).hexdigest()
        info = update.ReleaseInfo(
            version="v1.5.0",
            notes="",
            url="https://github.com/o/r/releases/download/v1.5.0/SDSS-x86_64.AppImage",
            checksum_url="https://github.com/o/r/releases/download/v1.5.0/SHA256SUMS",
        )
        bodies = {
            info.url: data,
            info.checksum_url: f"{digest}  SDSS-x86_64.AppImage\n".encode(),
        }
        target = Path(self._tmp.name) / "dl/SDSS.AppImage"
        update.download(info, target, self._opener(bodies))
        self.assertEqual(target.read_bytes(), data)
        self.assertTrue(os.access(target, os.X_OK))

    def test_a_mismatched_checksum_leaves_nothing_behind(self):
        info = update.ReleaseInfo(
            version="v1.5.0",
            notes="",
            url="https://github.com/o/r/releases/download/v1.5.0/SDSS-x86_64.AppImage",
            checksum_url="https://github.com/o/r/releases/download/v1.5.0/SHA256SUMS",
        )
        bodies = {info.url: b"tampered", info.checksum_url: b"c" * 64 + b"  SDSS-x86_64.AppImage\n"}
        target = Path(self._tmp.name) / "dl/SDSS.AppImage"
        with self.assertRaises(update.UpdateError):
            update.download(info, target, self._opener(bodies))
        self.assertFalse(target.exists())

    def test_a_release_without_a_checksum_is_refused(self):
        info = update.ReleaseInfo(version="v1", notes="", url="https://github.com/a", checksum_url=None)
        with self.assertRaises(update.UpdateError):
            update.download(info, Path(self._tmp.name) / "x", self._opener({}))

    def test_apply_replaces_the_target_atomically(self):
        new = Path(self._tmp.name) / "new.AppImage"
        new.write_bytes(b"new")
        target = paths.installed_appimage()
        target.parent.mkdir(parents=True)
        target.write_bytes(b"old")
        update.apply(new, target)
        self.assertEqual(target.read_bytes(), b"new")
        self.assertTrue(os.access(target, os.X_OK))
        self.assertFalse(target.with_name(f".{target.name}.new").exists())

    def test_local_update_rejects_a_missing_file(self):
        with self.assertRaises(update.UpdateError):
            update.install_local(Path(self._tmp.name) / "nope", paths.installed_appimage())


class AppCliTest(_Sandbox):
    def test_status_is_json(self):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(app_cli.main(["--status"]), 0)
        self.assertIn("install_path", json.loads(buffer.getvalue()))

    def test_explicit_flags_never_open_a_window(self):
        with mock.patch.object(app_cli, "_headless", return_value=0) as headless, mock.patch.object(
            app_cli, "_gui", return_value=0
        ) as gui, mock.patch.object(app_cli, "has_display", return_value=True):
            app_cli.main(["--role", "steam-machine"])
        headless.assert_called_once()
        gui.assert_not_called()

    def test_no_display_and_no_flags_prints_a_summary_and_installs_nothing(self):
        # Opening the app over SSH, or on a machine with no display, must never start
        # rewriting the system just because it could not draw a window.
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with mock.patch.object(app_cli, "has_display", return_value=False), mock.patch.object(
            app_cli.runner, "run"
        ) as run, redirect_stdout(buffer):
            self.assertEqual(app_cli.main([]), 0)
        run.assert_not_called()
        self.assertIn("--role", buffer.getvalue())

    def test_no_gui_with_a_role_still_installs(self):
        with mock.patch.object(app_cli, "_headless", return_value=0) as headless, mock.patch.object(
            app_cli, "has_display", return_value=True
        ):
            app_cli.main(["--no-gui", "--role", "steam-machine"])
        headless.assert_called_once()

    def test_a_display_opens_the_gui(self):
        with mock.patch.object(app_cli, "_gui", return_value=0) as gui, mock.patch.object(
            app_cli, "has_display", return_value=True
        ):
            app_cli.main([])
        gui.assert_called_once()

    def test_stage_only_uses_the_gui_when_a_display_is_available(self):
        with mock.patch.object(app_cli, "_headless", return_value=0) as headless, mock.patch.object(
            app_cli, "_gui", return_value=0
        ) as gui, mock.patch.object(app_cli, "has_display", return_value=True):
            app_cli.main(["--stage-only"])
        headless.assert_not_called()
        gui.assert_called_once()
        self.assertTrue(gui.call_args.args[0].stage_only)

    def test_headless_install_forwards_the_role_and_host(self):
        recorded = {}

        def fake_run(command, on_line=None, **kwargs):
            recorded["argv"] = [str(part) for part in command]
            return runner.Result(command=[], returncode=0, lines=[])

        with mock.patch.object(app_cli.runner, "run", fake_run):
            self.assertEqual(app_cli.main(["--role", "steam-deck", "--host", "10.0.0.5"]), 0)
        self.assertEqual(recorded["argv"][-4:], ["--role", "steam-deck", "--host", "10.0.0.5"])

    def test_a_deck_install_without_a_host_is_refused_before_running_anything(self):
        with mock.patch.object(app_cli.runner, "run") as run:
            self.assertEqual(app_cli.main(["--role", "steam-deck"]), 2)
        run.assert_not_called()

    def test_uninstall_forwards_keep_configs(self):
        recorded = {}

        def fake_run(command, on_line=None, **kwargs):
            recorded["argv"] = [str(part) for part in command]
            return runner.Result(command=[], returncode=0, lines=[])

        with mock.patch.object(app_cli.runner, "run", fake_run):
            app_cli.main(["--uninstall", "--keep-configs"])
        self.assertIn("--keep-configs", recorded["argv"])
        self.assertIn("--yes", recorded["argv"])

    def test_a_failed_install_does_not_install_the_app(self):
        with mock.patch.object(
            app_cli.runner, "run", return_value=runner.Result(command=[], returncode=1)
        ), mock.patch.object(app_cli.selfinstall, "install_self") as install_self:
            self.assertEqual(app_cli.main(["--role", "steam-machine"]), 1)
        install_self.assert_not_called()

    def test_a_successful_install_makes_the_app_permanent(self):
        with mock.patch.object(
            app_cli.runner, "run", return_value=runner.Result(command=[], returncode=0)
        ), mock.patch.object(app_cli.selfinstall, "install_self") as install_self:
            install_self.return_value = None
            self.assertEqual(app_cli.main(["--role", "steam-machine"]), 0)
        install_self.assert_called_once()


if __name__ == "__main__":
    unittest.main()
