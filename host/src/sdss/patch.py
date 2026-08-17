"""Surgical config editing with a byte-exact restore journal.

Emulator configs belong to the user, so SDSS never rewrites a file wholesale: it edits the
minimum number of lines and keeps the original bytes so a restore is byte-identical.
"""

from __future__ import annotations

import hashlib
import json
import os
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
    required: bool = True


class PatchError(Exception):
    pass


def _extract_xml_tag(text: str, tag: str) -> tuple[bool, str]:
    pattern = re.compile(rf"<{re.escape(tag)}>(.*?)</{re.escape(tag)}>", re.DOTALL)
    match = pattern.search(text)
    if not match:
        return False, ""
    return True, match.group(1)


def _extract_section_key(text: str, section: str, key: str) -> tuple[bool, str]:
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    header = f"[{section}]"
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.*)$")

    start = next((i for i, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        return False, ""

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].lstrip().startswith("["):
            end = i
            break

    for i in range(start + 1, end):
        match = key_re.match(lines[i])
        if match:
            return True, match.group(1).strip()
    return False, ""


def _remove_section_key(text: str, section: str, key: str) -> str:
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    header = f"[{section}]"
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")

    start = next((i for i, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        return text

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].lstrip().startswith("["):
            end = i
            break

    for i in range(start + 1, end):
        if key_re.match(lines[i]):
            del lines[i]
            return newline.join(lines)
    return text


def _snapshot(text: str, fmt: str, edit: Edit) -> dict:
    if fmt == XML:
        present, value = _extract_xml_tag(text, edit.key)
        return {
            "format": fmt,
            "section": None,
            "key": edit.key,
            "present": present,
            "value": value,
            "required": edit.required,
        }
    if fmt in (INI, TOML):
        if edit.section is None:
            raise PatchError(f"{fmt} edit for {edit.key!r} needs a section")
        present, value = _extract_section_key(text, edit.section, edit.key)
        return {
            "format": fmt,
            "section": edit.section,
            "key": edit.key,
            "present": present,
            "value": value,
            "required": edit.required,
        }
    raise PatchError(f"unsupported config format: {fmt}")


def _restore_snapshot(text: str, snapshot: dict) -> str:
    fmt = snapshot["format"]
    section = snapshot.get("section")
    key = snapshot["key"]
    present = bool(snapshot.get("present"))
    value = str(snapshot.get("value", ""))
    required = bool(snapshot.get("required", True))
    if fmt == XML:
        if not present:
            if required:
                raise PatchError(f"cannot restore missing XML tag <{key}>")
            return text
        return _set_xml_tag(text, key, value)
    if fmt in (INI, TOML):
        if section is None:
            raise PatchError(f"{fmt} snapshot for {key!r} is missing section")
        if present:
            return _set_section_key(text, fmt, section, key, value)
        return _remove_section_key(text, section, key)
    raise PatchError(f"unsupported config format: {fmt}")


def apply_edits(text: str, fmt: str, edits: tuple[Edit, ...]) -> str:
    for edit in edits:
        if fmt == XML:
            text = _set_xml_tag(text, edit.key, edit.value, required=edit.required)
        elif fmt in (INI, TOML):
            if edit.section is None:
                raise PatchError(f"{fmt} edit for {edit.key!r} needs a section")
            text = _set_section_key(text, fmt, edit.section, edit.key, edit.value)
        else:
            raise PatchError(f"unsupported config format: {fmt}")
    return text


def _set_xml_tag(text: str, tag: str, value: str, *, required: bool = True) -> str:
    pattern = re.compile(rf"(<{re.escape(tag)}>)(.*?)(</{re.escape(tag)}>)", re.DOTALL)
    if not pattern.search(text):
        if required:
            raise PatchError(f"<{tag}> not found")
        return text
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


def _write_atomic(path: Path, data: bytes) -> None:
    """Write `data` to `path` via temp file + rename so a kill mid-write can't corrupt it."""
    tmp = path.with_name(f".{path.name}.sdss-tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


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
        try:
            entries = json.loads(self.manifest.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            # A kill mid-write (SteamOS SIGKILLs the whole session on logout) can leave the
            # manifest truncated. Treating that as "nothing recorded" would be worse than
            # failing: `restore` would report success having restored nothing, then
            # `discard` would delete the backup files that are still perfectly intact.
            raise PatchError(
                f"backup manifest {self.manifest} is unreadable ({exc}); "
                f"the backups in {self.files} are untouched — recover them by hand, "
                f"then remove {self.dir}"
            ) from exc
        if not isinstance(entries, list):
            raise PatchError(f"backup manifest {self.manifest} is not a list of entries")
        return entries

    def _save(self, entries: list[dict]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        _write_atomic(self.manifest, json.dumps(entries, indent=2).encode())

    def record(self, path: Path) -> None:
        """Snapshot `path` before it is modified. Recording twice keeps the first snapshot."""
        entries = self._load()
        if any(e["path"] == str(path) for e in entries):
            return
        self.files.mkdir(parents=True, exist_ok=True)
        entry: dict = {"path": str(path), "existed": path.is_file()}
        if entry["existed"]:
            data = path.read_bytes()
            # Named from the path, not the entry count: a count-derived name collides with
            # an existing backup whenever entries are ever re-numbered, silently
            # overwriting the only copy of an earlier file.
            backup = self.files / f"{_digest(str(path).encode())[:16]}-{path.name}"
            backup.write_bytes(data)
            entry["backup"] = backup.name
            entry["sha256"] = _digest(data)
        entries.append(entry)
        self._save(entries)

    def record_snapshots(self, path: Path, fmt: str, edits: tuple[Edit, ...], original: str) -> None:
        entries = self._load()
        for entry in entries:
            if entry["path"] != str(path):
                continue
            entry["snapshots"] = [_snapshot(original, fmt, edit) for edit in edits]
            self._save(entries)
            return
        raise PatchError(f"config {path} was not recorded before storing snapshots")

    def restore(self) -> list[Path]:
        restored: list[Path] = []
        for entry in self._load():
            path = Path(entry["path"])
            if entry["existed"]:
                backup = self.files / entry["backup"]
                try:
                    data = backup.read_bytes()
                except FileNotFoundError as exc:
                    raise PatchError(
                        f"backup for {path} is missing (expected at {backup}) — refusing to restore"
                    ) from exc
                expected = entry.get("sha256")
                if expected is not None and _digest(data) != expected:
                    raise PatchError(
                        f"backup for {path} is corrupted (sha256 mismatch) — refusing to restore"
                    )
                _write_atomic(path, data)
            elif path.exists():
                path.unlink()
            restored.append(path)
        self.discard()
        return restored

    def restore_snapshots(self) -> list[Path]:
        restored: list[Path] = []
        for entry in self._load():
            path = Path(entry["path"])
            snapshots = entry.get("snapshots")
            if isinstance(snapshots, list) and path.is_file():
                current = path.read_text()
                updated = current
                for snapshot in snapshots:
                    updated = _restore_snapshot(updated, snapshot)
                if updated != current:
                    _write_atomic(path, updated.encode())
                restored.append(path)
                continue

            # Compatibility path for journals created before snapshots existed.
            if entry["existed"]:
                backup = self.files / entry["backup"]
                try:
                    data = backup.read_bytes()
                except FileNotFoundError as exc:
                    raise PatchError(
                        f"backup for {path} is missing (expected at {backup}) — refusing to restore"
                    ) from exc
                expected = entry.get("sha256")
                if expected is not None and _digest(data) != expected:
                    raise PatchError(
                        f"backup for {path} is corrupted (sha256 mismatch) — refusing to restore"
                    )
                _write_atomic(path, data)
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
    journal.record_snapshots(path, fmt, edits, original)
    if patched == original:
        return False
    # Atomic for the same reason `restore` is: SteamOS SIGKILLs the whole session on
    # logout, and a truncated emulator config is exactly what the journal exists to avoid.
    _write_atomic(path, patched.encode())
    return True
