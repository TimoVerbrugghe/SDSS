"""Filesystem locations. Everything lives under $HOME — the SteamOS rootfs is read-only."""

from __future__ import annotations

import os
from pathlib import Path


def _env_dir(var: str, default: Path) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else default


def config_dir() -> Path:
    return _env_dir("XDG_CONFIG_HOME", Path.home() / ".config") / "sdss"


def state_dir() -> Path:
    return _env_dir("XDG_STATE_HOME", Path.home() / ".local/state") / "sdss"


def data_dir() -> Path:
    return _env_dir("XDG_DATA_HOME", Path.home() / ".local/share") / "sdss"


def runtime_dir() -> Path:
    """Per-session scratch space. Falls back to the state dir when not in a user session."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    return Path(base) / "sdss" if base else state_dir() / "run"


def backup_dir() -> Path:
    return state_dir() / "backups"


def state_file() -> Path:
    return state_dir() / "state.json"


def ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
