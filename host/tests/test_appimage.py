"""Cross-tree invariants for the AppImage recipe.

The AppImage is only ever built on a machine that is not the target, and the failure mode
that matters — a file the app needs at runtime that the recipe never copied — is invisible
until someone runs it on a Deck. These assertions are cheap and catch exactly that drift.
"""

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

IMPORTS_QT = re.compile(r"^\s*(?:from|import)\s+PySide6", re.MULTILINE)

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "packaging/appimage/build.sh"
APPRUN = ROOT / "packaging/appimage/AppRun"
WORKFLOW = ROOT / ".github/workflows/appimage.yml"


class AppImageRecipeTest(unittest.TestCase):
    def setUp(self):
        self.build = BUILD.read_text()
        self.apprun = APPRUN.read_text()

    def test_scripts_are_executable(self):
        for script in (BUILD, APPRUN, ROOT / "app/sdss-app"):
            self.assertTrue(script.stat().st_mode & 0o111, script)

    def test_shell_syntax(self):
        for script, shell in ((BUILD, "bash"), (APPRUN, "sh")):
            with self.subTest(script=script.name):
                self.assertEqual(
                    subprocess.run([shell, "-n", str(script)], capture_output=True).returncode,
                    0,
                )

    def test_every_entry_point_is_strict(self):
        self.assertIn("set -euo pipefail", self.build)
        self.assertIn("set -eu", self.apprun)

    def test_apprun_launches_the_payload_entry_point(self):
        self.assertIn("usr/share/sdss/app/sdss-app", self.apprun)
        # The bundled interpreter must win over anything in the user's environment.
        self.assertIn("PYTHONNOUSERSITE=1", self.apprun)
        self.assertIn("unset PYTHONPATH PYTHONHOME", self.apprun)

    def test_payload_root_matches_apprun(self):
        # AppRun execs $APPDIR/usr/share/sdss/app/sdss-app; paths.payload_root() has to
        # resolve to that same directory or the app installs from the wrong tree.
        sys.path.insert(0, str(ROOT))
        from app.core import paths

        with mock.patch.dict(os.environ, {"APPDIR": "/tmp/appdir"}, clear=False):
            os.environ.pop("SDSS_APP_PAYLOAD", None)
            self.assertEqual(paths.payload_root(), Path("/tmp/appdir/usr/share/sdss"))

    def test_the_payload_contents_are_asserted(self):
        # `git ls-files` means an untracked file is silently absent from the AppImage; the
        # build has to notice rather than ship a GUI with no app/ui in it.
        for required in ("app/ui/main.py", "app/sdss-app", "packaging/uninstall.sh"):
            self.assertIn(required, self.build)

    def test_the_prebuilt_decky_bundle_is_required(self):
        # SteamOS has no node. Shipping an AppImage whose payload cannot install the
        # plugin is a silent half-install, so the build refuses instead.
        self.assertIn("plugin/dist/index.js", self.build)

    def test_the_payload_is_the_tracked_tree(self):
        # `git ls-files`, not `cp -R .`: a stray dist/ or node_modules from a local build
        # would otherwise be shipped to every user.
        self.assertIn("git ls-files", self.build)

    def test_installer_scripts_stay_executable_in_the_payload(self):
        self.assertIn('chmod +x "$payload/install.sh"', self.build)
        self.assertIn("find \"$payload\" -name '*.sh' -exec chmod +x {} +", self.build)

    def test_checksum_is_published_next_to_the_appimage(self):
        # The app refuses an update it cannot verify, so a build with no .sha256 produces
        # an AppImage that can never be updated from.
        self.assertIn('"$OUT.sha256"', self.build)
        workflow = WORKFLOW.read_text()
        self.assertIn("SDSS-x86_64.AppImage.sha256", workflow)

    def test_pins_are_overridable_but_not_floating(self):
        for pin in ("SDSS_PYTHON_VERSION", "SDSS_PYSIDE_VERSION", "SDSS_APPIMAGETOOL_URL"):
            self.assertIn(pin, self.build)
        self.assertIn("PySide6-Essentials==$PYSIDE_VERSION", self.build)


class UiIsolationTest(unittest.TestCase):
    """`app/core` must stay importable without Qt; `sdss` must never see it at all."""

    def test_core_never_imports_qt(self):
        for module in sorted((ROOT / "app/core").glob("*.py")):
            self.assertIsNone(IMPORTS_QT.search(module.read_text()), module.name)
        # app/cli.py may import Qt, but only inside _gui(), so --no-gui works without it.
        head = (ROOT / "app/cli.py").read_text().split("def _gui")[0]
        self.assertIsNone(IMPORTS_QT.search(head))

    def test_the_host_package_never_imports_the_app(self):
        for module in sorted((ROOT / "host/src/sdss").glob("*.py")):
            text = module.read_text()
            self.assertIsNone(IMPORTS_QT.search(text), module.name)
            self.assertIsNone(re.search(r"^\s*(?:from|import)\s+app\b", text, re.M), module.name)

    def test_qt_lives_only_in_the_ui_package(self):
        users = {
            path.relative_to(ROOT).parts[1]
            for path in ROOT.glob("app/**/*.py")
            if IMPORTS_QT.search(path.read_text())
        }
        self.assertEqual(users, {"ui"})


if __name__ == "__main__":
    unittest.main()
