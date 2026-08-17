"""The log pane: everything SDSS runs, verbatim.

An installer that hides what it is doing is impossible to support remotely, and every SDSS
script already prints exactly what it did — so the app shows that output rather than
inventing progress messages of its own.
"""

from __future__ import annotations

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QPlainTextEdit

#: Older output is dropped rather than kept forever: a container build prints tens of
#: thousands of lines, and the full transcript is on disk in the app log anyway.
MAX_BLOCKS = 5000


class LogView(QPlainTextEdit):
    def __init__(self) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setMaximumBlockCount(MAX_BLOCKS)
        font = QFont("monospace")
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(font)

    def append_line(self, line: str) -> None:
        self.appendPlainText(line)
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())
