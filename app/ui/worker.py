"""Background execution for the UI.

Every action the app performs is a subprocess that can take minutes (a container build, a
Flatpak download). Running one on the Qt main thread freezes the window, which on a Deck
looks exactly like a crash — so all of them go through this worker.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QThread, Signal


class Worker(QObject):
    """Runs one callable off the main thread, forwarding its output line by line.

    The callable is handed an `emit_line` function rather than being given the signal
    directly, so `app.core` never has to know Qt exists.
    """

    line = Signal(str)
    finished = Signal(int, str)

    def __init__(self, task: Callable[[Callable[[str], None]], tuple[int, str]]) -> None:
        super().__init__()
        self._task = task

    def start(self) -> QThread:
        thread = QThread()
        self.moveToThread(thread)
        thread.started.connect(self._run)
        # Keep a reference on the worker: a QThread that goes out of scope is destroyed
        # while still running, which aborts the process rather than the task.
        self._thread = thread
        thread.start()
        return thread

    def _run(self) -> None:
        try:
            code, message = self._task(self.line.emit)
        except Exception as exc:  # noqa: BLE001 - a crash here must not kill the window
            code, message = 1, str(exc)
        self.finished.emit(code, message)
        self._thread.quit()
