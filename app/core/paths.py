"""Where the app finds things.

Every location is derived from the environment rather than hardcoded, both because XDG
variables are honoured throughout SDSS and because the tests need to point the whole app
at a temporary directory.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Directory the AppImage runtime mounts itself at. Absent when running from a checkout.
APPDIR_VAR = "APPDIR"
#: Absolute path of the running AppImage file, set by the AppImage runtime.
APPIMAGE_VAR = "APPIMAGE"
#: Test/development override for the embedded SDSS tree.
PAYLOAD_VAR = "SDSS_APP_PAYLOAD"

APPIMAGE_NAME = "SDSS.AppImage"
DESKTOP_ENTRY_NAME = "sdss.desktop"
LEGACY_DESKTOP_ENTRY_NAME = "sdss-installer.desktop"


def home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def _xdg(var: str, default: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else home() / default


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "sdss"


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / "sdss"


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / "sdss"


def applications_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / "applications"


def install_root() -> Path:
    """The tree `install.sh` maintains. Must match INSTALL_ROOT in install.sh."""
    return data_dir() / "release"


def payload_root() -> Path:
    """The SDSS tree this app carries and installs from.

    Inside the AppImage that is the copy under `$APPDIR`; from a checkout it is the
    repository itself, which is what makes `app/sdss-app` runnable during development.
    """
    override = os.environ.get(PAYLOAD_VAR)
    if override:
        return Path(override)
    appdir = os.environ.get(APPDIR_VAR)
    if appdir:
        return Path(appdir) / "usr/share/sdss"
    return Path(__file__).resolve().parents[2]


def running_appimage() -> Path | None:
    """The AppImage file this process was started from, if any."""
    value = os.environ.get(APPIMAGE_VAR)
    return Path(value) if value else None


def applications_home() -> Path:
    """`~/Applications` — where EmuDeck keeps AppImages, so the user already looks there."""
    return home() / "Applications"


def installed_appimage() -> Path:
    return applications_home() / APPIMAGE_NAME


def desktop_entry() -> Path:
    return applications_dir() / DESKTOP_ENTRY_NAME


def legacy_desktop_entry() -> Path:
    return applications_dir() / LEGACY_DESKTOP_ENTRY_NAME


def log_file() -> Path:
    """Everything the app runs is appended here, so "Open log" has something to show."""
    return state_dir() / "app.log"


def sdss_bin() -> Path:
    return home() / ".local/bin/sdss"


def sdss_connect_bin() -> Path:
    return home() / ".local/bin/sdss-connect"


def ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
