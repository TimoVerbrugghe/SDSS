"""Making the app permanent: its own copy in ~/Applications and one desktop entry.

EmuDeck's convention is a single AppImage in `~/Applications`, and that is where users
already look on these devices. Copying happens *after* a successful install so a failed
first run leaves nothing behind.
"""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from . import paths

DESKTOP_TEMPLATE = """[Desktop Entry]
Name=SDSS
GenericName=Steam Deck Second Screen
Comment=Install, update and manage Steam Deck Second Screen
Exec={exec_line}
Icon=sdss
Terminal=false
Type=Application
Categories=Game;Utility;
StartupNotify=true
"""


@dataclass
class SelfInstall:
    """What `install_self` did, so the UI can say so without re-probing."""

    copied: bool
    target: Path
    desktop_entry: Path


def _quote(path: Path) -> str:
    """Quote a path for a .desktop Exec line (spaces are the only realistic case here)."""
    text = str(path)
    if any(ch in text for ch in ' \t"\'\\'):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def exec_line(appimage: Path, *, fuse: bool = True) -> str:
    """The Exec= value for the desktop entry.

    Without FUSE the AppImage runtime cannot mount itself and exits with a message no one
    sees when launched from a menu. `APPIMAGE_EXTRACT_AND_RUN=1` makes the same runtime
    unpack to a temp directory instead, which is slower but always works — so the entry is
    written for whichever mode actually works on this device.
    """
    quoted = _quote(appimage)
    if fuse:
        return quoted
    return f"env APPIMAGE_EXTRACT_AND_RUN=1 {quoted}"


def fuse_available() -> bool:
    """Whether libfuse2 is usable, which is what an AppImage needs to self-mount.

    Checked by looking for the device node and the library rather than by trying a mount:
    a failed mount attempt is exactly the confusing failure this exists to avoid.
    """
    if not Path("/dev/fuse").exists():
        return False
    for directory in ("/usr/lib", "/usr/lib64", "/usr/lib/x86_64-linux-gnu", "/lib64"):
        base = Path(directory)
        if not base.is_dir():
            continue
        try:
            if any(entry.name.startswith("libfuse.so.2") for entry in base.iterdir()):
                return True
        except OSError:
            continue
    return False


def write_desktop_entry(appimage: Path, *, fuse: bool | None = None) -> Path:
    entry = paths.desktop_entry()
    paths.ensure(entry.parent)
    if fuse is None:
        fuse = fuse_available()
    entry.write_text(DESKTOP_TEMPLATE.format(exec_line=exec_line(appimage, fuse=fuse)))
    entry.chmod(0o644)
    # The pre-app launcher ("Install or Update SDSS"). Left behind it would be a second
    # menu entry running a script that no longer describes how SDSS is installed.
    paths.legacy_desktop_entry().unlink(missing_ok=True)
    return entry


def install_self(source: Path | None = None) -> SelfInstall | None:
    """Copy the running AppImage to ~/Applications and (re)write the desktop entry.

    Returns None when there is no AppImage to copy — running from a checkout during
    development, where writing an entry pointing at a file that does not exist would be
    worse than doing nothing.
    """
    appimage = Path(source) if source else paths.running_appimage()
    if appimage is None or not appimage.is_file():
        return None
    target = paths.installed_appimage()
    paths.ensure(target.parent)
    copied = False
    same = target.exists() and appimage.samefile(target)
    if not same:
        # Same directory, then os.replace: a copy straight onto the target would leave a
        # truncated AppImage behind if it were interrupted, and the file being replaced
        # may be the one currently executing.
        tmp = target.with_name(f".{target.name}.new")
        try:
            shutil.copyfile(appimage, tmp)
            tmp.chmod(
                tmp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            )
            os.replace(tmp, target)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        copied = True
    entry = write_desktop_entry(target)
    return SelfInstall(copied=copied, target=target, desktop_entry=entry)
