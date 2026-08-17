"""What is actually installed on this device, as opposed to what is running.

The desktop app runs from an AppImage that carries its own copy of the tree, so "my
version" and "the installed version" are different questions. `install.sh` writes the
marker read here at the moment it swaps a release in, which is the only point where both
answers are known at once.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import paths

VERSION_FILE = "VERSION"
MARKER_NAME = ".sdss-release.json"


def release_dir() -> Path:
    """Where `install.sh` puts the tree it installed."""
    return paths.data_dir() / "release"


def marker_file() -> Path:
    return release_dir() / MARKER_NAME


def role_file() -> Path:
    return paths.config_dir() / "installed-role"


def source_version(root: Path) -> str:
    """The version recorded in a checkout/payload, or "unknown"."""
    try:
        text = (root / VERSION_FILE).read_text()
    except OSError:
        return "unknown"
    return text.strip() or "unknown"


def installed() -> dict[str, str | None]:
    """Version, install timestamp and role of the installed release.

    Every field is optional: a release installed by an older installer has no marker, and
    the role is recorded separately (and by `deck/install.sh` too, which never writes a
    marker at all).
    """
    info: dict[str, str | None] = {
        "version": None,
        "installed_at": None,
        "role": None,
        "path": str(release_dir()),
        "present": None,
    }
    info["present"] = "yes" if release_dir().is_dir() else "no"
    try:
        raw = json.loads(marker_file().read_text())
    except (OSError, json.JSONDecodeError):
        raw = {}
    if isinstance(raw, dict):
        version = raw.get("version")
        installed_at = raw.get("installed_at")
        info["version"] = str(version) if isinstance(version, str) else None
        info["installed_at"] = str(installed_at) if isinstance(installed_at, str) else None
    try:
        role = role_file().read_text().strip()
    except OSError:
        role = ""
    info["role"] = role if role in ("steam-machine", "steam-deck") else None
    return info
