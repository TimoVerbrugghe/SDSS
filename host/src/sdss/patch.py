"""Surgical config editing with a byte-exact restore journal.

Emulator configs belong to the user, so SDSS never rewrites a file wholesale: it edits the
minimum number of lines and keeps the original bytes so a restore is byte-identical.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

INI = "ini"
XML = "xml"
TOML = "toml"

_SEPARATOR = {INI: "=", TOML: " = "}


@dataclass(frozen=True)
class Edit:
    """One key to force. `section` is the INI section or dotted TOML table; None for XML."""

    key: str
    value: str
    section: str | None = None


class PatchError(Exception):
    pass


def apply_edits(text: str, fmt: str, edits: tuple[Edit, ...]) -> str:
    for edit in edits:
        if fmt == XML:
            text = _set_xml_tag(text, edit.key, edit.value)
        elif fmt in (INI, TOML):
            if edit.section is None:
                raise PatchError(f"{fmt} edit for {edit.key!r} needs a section")
            text = _set_section_key(text, fmt, edit.section, edit.key, edit.value)
        else:
            raise PatchError(f"unsupported config format: {fmt}")
    return text


def _set_xml_tag(text: str, tag: str, value: str) -> str:
    pattern = re.compile(rf"(<{re.escape(tag)}>)(.*?)(</{re.escape(tag)}>)", re.DOTALL)
    if not pattern.search(text):
        raise PatchError(f"<{tag}> not found")
    return pattern.sub(lambda m: f"{m.group(1)}{value}{m.group(3)}", text, count=1)


def _set_section_key(text: str, fmt: str, section: str, key: str, value: str) -> str:
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    header = f"[{section}]"
    sep = _SEPARATOR[fmt]
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")

    start = next((i for i, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        tail = [] if lines and lines[-1] == "" else [""]
        return newline.join(lines + tail + [header, f"{key}{sep}{value}", ""])

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].lstrip().startswith("["):
            end = i
            break

    for i in range(start + 1, end):
        if key_re.match(lines[i]):
            lines[i] = f"{key}{sep}{value}"
            return newline.join(lines)

    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines.insert(insert_at, f"{key}{sep}{value}")
    return newline.join(lines)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Journal:
    """Records original file contents so a session can be undone exactly."""

    def __init__(self, root: Path, name: str) -> None:
        self.dir = root / name
        self.manifest = self.dir / "manifest.json"
        self.files = self.dir / "files"

    @property
    def exists(self) -> bool:
        return self.manifest.is_file()

    def _load(self) -> list[dict]:
        if not self.exists:
            return []
        return json.loads(self.manifest.read_text())

    def _save(self, entries: list[dict]) -> None:
        self.manifest.write_text(json.dumps(entries, indent=2))

    def record(self, path: Path) -> None:
        """Snapshot `path` before it is modified. Recording twice keeps the first snapshot."""
        entries = self._load()
        if any(e["path"] == str(path) for e in entries):
            return
        self.files.mkdir(parents=True, exist_ok=True)
        entry: dict = {"path": str(path), "existed": path.is_file()}
        if entry["existed"]:
            data = path.read_bytes()
            backup = self.files / f"{len(entries):03d}-{path.name}"
            backup.write_bytes(data)
            entry["backup"] = backup.name
            entry["sha256"] = _digest(data)
        entries.append(entry)
        self._save(entries)

    def restore(self) -> list[Path]:
        restored: list[Path] = []
        for entry in self._load():
            path = Path(entry["path"])
            if entry["existed"]:
                backup = self.files / entry["backup"]
                shutil.copyfile(backup, path)
            elif path.exists():
                path.unlink()
            restored.append(path)
        self.discard()
        return restored

    def discard(self) -> None:
        if self.dir.exists():
            shutil.rmtree(self.dir)


def patch_file(path: Path, fmt: str, edits: tuple[Edit, ...], journal: Journal) -> bool:
    """Apply `edits` to `path`, backing it up first. Returns True when bytes changed."""
    if not path.is_file():
        raise PatchError(f"config not found: {path}")
    original = path.read_text()
    patched = apply_edits(original, fmt, edits)
    journal.record(path)
    if patched == original:
        return False
    path.write_text(patched)
    return True
