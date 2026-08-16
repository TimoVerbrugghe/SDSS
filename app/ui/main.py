"""Window, pages and wiring for the SDSS app.

Two pages: an install page when nothing is installed yet, and a dashboard afterwards. Both
share the log pane, because "what did it just do?" is the same question in both states.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..core import actions, elevate, paths, probe, runner, selfinstall, update
from .logview import LogView
from .worker import Worker

ROLE_LABELS = probe.ROLE_LABELS
OK_MARK = "✓"
BAD_MARK = "✗"


_role_label = probe.role_label


class InstallPage(QWidget):
    """First run: pick an endpoint and install."""

    def __init__(self, window: "MainWindow") -> None:
        super().__init__()
        self.window = window
        layout = QVBoxLayout(self)

        title = QLabel("<h2>Install SDSS</h2>")
        layout.addWidget(title)
        layout.addWidget(
            QLabel(
                "SDSS turns a Steam Deck into the second screen for DS, 3DS and Wii U\n"
                "emulation running on a Steam Machine. Install it on both devices."
            )
        )

        form = QFormLayout()
        self.role = QComboBox()
        for role, label in ROLE_LABELS.items():
            self.role.addItem(label, role)
        detected = probe.detect_role()
        if detected:
            self.role.setCurrentIndex(self.role.findData(detected))
        form.addRow("This device is", self.role)

        self.host = QLineEdit()
        self.host.setPlaceholderText("Steam Machine IP address or hostname")
        self.host.setText(probe.host_address() or "")
        form.addRow("Steam Machine", self.host)
        layout.addLayout(form)

        self.detected_note = QLabel(
            f"Detected: {_role_label(detected)}" if detected else
            "This device did not identify itself; choose the endpoint yourself."
        )
        layout.addWidget(self.detected_note)

        self.install_button = QPushButton("Install")
        self.install_button.clicked.connect(self._install)
        layout.addWidget(self.install_button)
        layout.addStretch(1)

        self.role.currentIndexChanged.connect(self._sync_host)
        self._sync_host()

    def _sync_host(self) -> None:
        is_deck = self.role.currentData() == probe.STEAM_DECK
        self.host.setEnabled(is_deck)

    def _install(self) -> None:
        role = self.role.currentData()
        host = self.host.text().strip()
        if role == probe.STEAM_DECK and not host:
            QMessageBox.warning(
                self, "Address needed", "A Steam Machine address is required for a Deck install."
            )
            return
        self.window.install(role, host or None)


class HealthRow(QWidget):
    def __init__(self, check: probe.Check, window: "MainWindow") -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        mark = OK_MARK if check.ok else BAD_MARK
        label = QLabel(f"{mark}  <b>{check.label}</b> — {check.detail}")
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setWordWrap(True)
        layout.addWidget(label, 1)
        if not check.ok and check.fix:
            button = QPushButton("Fix")
            button.clicked.connect(lambda: window.fix(check))
            layout.addWidget(button)


class Dashboard(QWidget):
    """Everything about an installed SDSS, and the buttons that change it."""

    def __init__(self, window: "MainWindow") -> None:
        super().__init__()
        self.window = window
        self._layout = QVBoxLayout(self)
        self.summary = QLabel()
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        self._layout.addWidget(self.summary)

        self.health = QGroupBox("Health")
        self.health_layout = QVBoxLayout(self.health)
        self._layout.addWidget(self.health)

        self.emulators = QGroupBox("Second screen")
        self.emulators_layout = QVBoxLayout(self.emulators)
        self._layout.addWidget(self.emulators)

        buttons = QHBoxLayout()
        for text, slot in (
            ("Check for updates", window.check_for_updates),
            ("Update from file…", window.update_from_file),
            ("Repair", window.repair),
            ("Uninstall", window.uninstall),
            ("Open log", window.open_log),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        self._layout.addLayout(buttons)
        self._layout.addStretch(1)

    @staticmethod
    def _clear(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def refresh(self, status: probe.Status) -> None:
        version = status.installed_version or "unknown"
        installed_at = status.installed_at or "unknown"
        self.summary.setText(
            f"<h2>SDSS {version}</h2>"
            f"<p>{_role_label(status.role)}<br>"
            f"Installed at {status.install_path}<br>"
            f"Last install/update: {installed_at}<br>"
            f"This app: {status.app_version}</p>"
        )

        self._clear(self.health_layout)
        for check in status.checks:
            self.health_layout.addWidget(HealthRow(check, self.window))
        if not status.problems:
            self.health_layout.addWidget(QLabel(f"{OK_MARK} Everything SDSS needs is in place."))

        self._clear(self.emulators_layout)
        sdss = status.sdss or {}
        if status.role != probe.STEAM_MACHINE:
            self.emulators.setVisible(False)
        else:
            self.emulators.setVisible(True)
            master = QCheckBox("Second screen mode")
            master.setChecked(bool(sdss.get("enabled")))
            master.clicked.connect(lambda value: self.window.toggle(value, None))
            self.emulators_layout.addWidget(master)
            for entry in sdss.get("profiles", []):
                text = f"{entry['name']} ({entry['system']})"
                if not entry.get("verified"):
                    text += " — unverified"
                if entry.get("hooked"):
                    text += " — launcher wrapped"
                box = QCheckBox(text)
                box.setChecked(bool(entry.get("enabled")))
                box.clicked.connect(
                    lambda value, pid=entry["id"]: self.window.toggle(value, pid)
                )
                self.emulators_layout.addWidget(box)
            patched = sdss.get("patched_configs") or []
            if patched:
                row = QHBoxLayout()
                row.addWidget(QLabel(f"{len(patched)} emulator config(s) still patched"), 1)
                restore = QPushButton("Restore all")
                restore.clicked.connect(self.window.restore)
                row.addWidget(restore)
                container = QWidget()
                container.setLayout(row)
                self.emulators_layout.addWidget(container)


class UpdateDialog(QDialog):
    def __init__(self, parent, release: update.ReleaseInfo, current: str) -> None:
        super().__init__(parent)
        self.setWindowTitle("Update SDSS")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"<b>{release.version}</b> is available (you have {current})."))
        notes = QTextBrowser()
        notes.setPlainText(release.notes or "No release notes were published.")
        layout.addWidget(notes)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Download and install")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class MainWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("SDSS — Steam Deck Second Screen")
        # Fits the Deck's 1280x800 panel with room for SteamOS's default scaling.
        self.resize(980, 720)
        self.status = probe.probe()

        self.stack = QStackedWidget()
        self.install_page = InstallPage(self)
        self.dashboard = Dashboard(self)
        self.stack.addWidget(self.install_page)
        self.stack.addWidget(self.dashboard)

        self.log = LogView()
        self.busy = QLabel()
        layout = QVBoxLayout(self)
        layout.addWidget(self.stack, 1)
        layout.addWidget(self.busy)
        layout.addWidget(self.log, 1)

        self._worker: Worker | None = None
        self.refresh()

    # -- state ---------------------------------------------------------------

    def refresh(self) -> None:
        self.status = probe.probe()
        if self.status.installed:
            self.dashboard.refresh(self.status)
            self.stack.setCurrentWidget(self.dashboard)
        else:
            self.stack.setCurrentWidget(self.install_page)

    def _set_busy(self, message: str | None) -> None:
        self.busy.setText(message or "")
        self.stack.setEnabled(message is None)

    def _run(self, task, message: str) -> None:
        """Run `task(emit)` off the main thread and refresh when it finishes."""
        if self._worker is not None:
            QMessageBox.information(self, "Busy", "Something is already running.")
            return
        self._set_busy(message)
        worker = Worker(task)
        worker.line.connect(self.log.append_line)
        worker.finished.connect(self._finished)
        self._worker = worker
        worker.start()

    def _finished(self, code: int, message: str) -> None:
        self._worker = None
        self._set_busy(None)
        if message:
            self.log.append_line(message)
        if code != 0:
            QMessageBox.warning(self, "SDSS", message or "That did not work. See the log below.")
        self.refresh()

    def _command_task(self, command, done: str = ""):
        def task(emit):
            emit("$ " + " ".join(command))
            result = runner.run(command, emit)
            if result.ok:
                return 0, done
            return result.returncode, f"{command[0]} exited with {result.returncode}"

        return task

    # -- actions -------------------------------------------------------------

    def install(self, role: str, host: str | None) -> None:
        command = actions.install_command(role, host)

        def task(emit):
            emit("$ " + " ".join(command))
            result = runner.run(command, emit)
            if not result.ok:
                return result.returncode, f"the installer exited with {result.returncode}"
            installed = selfinstall.install_self()
            if installed:
                emit(f"installed {installed.target}")
                emit(f"installed {installed.desktop_entry}")
            return 0, "SDSS is installed."

        self._run(task, f"Installing SDSS for {_role_label(role)} …")

    def repair(self) -> None:
        role = self.status.role
        if role is None:
            QMessageBox.warning(self, "SDSS", "No installed role is recorded; reinstall instead.")
            return
        self._run(
            self._command_task(
                actions.repair_command(role, self.status.host_address), "Repair finished."
            ),
            "Re-running the installer …",
        )

    def fix(self, check: probe.Check) -> None:
        if check.fix == probe.FIX_UDEV:
            self.install_udev_rule()
        elif check.fix == probe.FIX_RESTORE:
            self.restore()
        else:
            self.repair()

    def install_udev_rule(self) -> None:
        plan = elevate.plan()
        if not plan.possible:
            QMessageBox.warning(self, "Administrator access needed", plan.reason or "")
            return
        password = None
        if plan.needs_password:
            password, accepted = QInputDialog.getText(
                self,
                "Administrator password",
                "SDSS needs permission to install one udev rule under /etc.\n"
                "Your password is used once and never stored.",
                QLineEdit.EchoMode.Password,
            )
            if not accepted:
                return
        command = actions.udev_command()

        def task(emit):
            emit("$ " + " ".join(elevate.build(command, plan.method)))
            result = elevate.run_elevated(command, plan, password, emit)
            if result.ok:
                return 0, "The udev rule is installed."
            return result.returncode, "Installing the udev rule failed; see the log."

        self._run(task, "Installing the udev rule …")

    def restore(self) -> None:
        self._run(
            self._command_task(actions.restore_command(), "Emulator configs restored."),
            "Restoring emulator configs …",
        )

    def toggle(self, enabled: bool, profile: str | None) -> None:
        self._run(
            self._command_task(actions.toggle_command(enabled, profile)),
            "Updating second screen mode …",
        )

    def uninstall(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Uninstall SDSS")
        layout = QVBoxLayout(dialog)
        layout.addWidget(
            QLabel(
                "Emulator configs are restored first, then the release, the sdss command,\n"
                "the Decky plugin and the compositor image are removed."
            )
        )
        keep = QCheckBox("Leave emulator configs patched")
        etc = QCheckBox("Also remove the udev rule under /etc (needs administrator access)")
        app = QCheckBox("Also remove this app from ~/Applications")
        layout.addWidget(keep)
        layout.addWidget(etc)
        layout.addWidget(app)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Uninstall")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        plan = elevate.plan() if etc.isChecked() else None
        password = None
        if plan is not None and plan.needs_password:
            password, accepted = QInputDialog.getText(
                self,
                "Administrator password",
                "Removing the files under /etc needs administrator access.",
                QLineEdit.EchoMode.Password,
            )
            if not accepted:
                return
        command = actions.uninstall_command(keep_configs=keep.isChecked())
        remove_app = app.isChecked()

        def task(emit):
            emit("$ " + " ".join(command))
            result = runner.run(command, emit)
            if not result.ok:
                return result.returncode, "Uninstall failed; see the log."
            if plan is not None and plan.possible:
                emit("removing the files under /etc")
                elevate.run_elevated(actions.remove_etc_command(), plan, password, emit)
            elif plan is not None:
                emit(f"skipped the files under /etc: {plan.reason}")
            if remove_app:
                # Last, and deliberately after everything else: this is the file the
                # running process was started from.
                paths.desktop_entry().unlink(missing_ok=True)
                paths.installed_appimage().unlink(missing_ok=True)
                emit(f"removed {paths.installed_appimage()}")
            return 0, "SDSS is uninstalled."

        self._run(task, "Uninstalling SDSS …")

    # -- updates -------------------------------------------------------------

    def check_for_updates(self) -> None:
        current = self.status.app_version
        try:
            release = update.latest_release()
        except update.UpdateError as exc:
            # Offline is normal on these devices; it is a log line, not a failure dialog.
            self.log.append_line(f"could not check for updates: {exc}")
            QMessageBox.information(self, "Updates", f"Could not check for updates.\n\n{exc}")
            return
        if not update.is_newer(release.version, current):
            QMessageBox.information(self, "Updates", f"SDSS {current} is up to date.")
            return
        if UpdateDialog(self, release, current).exec() != QDialog.DialogCode.Accepted:
            return

        target = paths.installed_appimage()

        def task(emit):
            emit(f"downloading {release.url}")
            staged = update.download(release, update.staging_path())
            emit("checksum verified")
            update.apply(staged, target)
            emit(f"updated {target}")
            selfinstall.write_desktop_entry(target)
            return 0, f"Updated to {release.version}. Close and reopen SDSS to run it."

        self._run(task, f"Updating to {release.version} …")

    def update_from_file(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Choose an SDSS AppImage", str(paths.applications_home()), "AppImage (*.AppImage)"
        )
        if not chosen:
            return
        target = paths.installed_appimage()

        def task(emit):
            update.install_local(Path(chosen), target)
            selfinstall.write_desktop_entry(target)
            emit(f"installed {target}")
            return 0, "Installed. Close and reopen SDSS to run the new version."

        self._run(task, "Installing from file …")

    def open_log(self) -> None:
        log_file = paths.log_file()
        if not log_file.is_file():
            QMessageBox.information(self, "Log", "Nothing has been logged yet.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_file)))


def run_gui(args: argparse.Namespace) -> int:
    application = QApplication(sys.argv[:1])
    application.setApplicationName("SDSS")
    window = MainWindow()
    window.show()
    return application.exec()


def self_test() -> None:
    """Construct the whole window offscreen. Used by CI to prove the bundled Qt works."""
    application = QApplication.instance() or QApplication(sys.argv[:1])
    window = MainWindow()
    window.show()
    application.processEvents()
    window.close()
