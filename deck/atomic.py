"""Durable file replacement shared by the deck-side installers.

Steam re-reads both `shortcuts.vdf` and its controller templates while running, so a
half-written file would be parsed and rejected with the previous good copy already gone.
The containing directory is fsynced as well: the rename itself is not durable on every
filesystem until its directory entry is flushed, so a crash right afterwards could
otherwise resurrect the pre-replace file.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_atomically(path: Path, data: bytes | str, mode: int | None = None) -> None:
    """Replace `path` with `data`. `mode` defaults to the existing file's, else 0644."""
    if mode is None:
        mode = path.stat().st_mode if path.exists() else 0o644
    payload = data.encode("utf-8") if isinstance(data, str) else data
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.sdss-", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
        temporary = None
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
