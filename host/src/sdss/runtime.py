"""How to start the nested compositor on a host that has no sway installed.

SteamOS ships no sway and the rootfs is read-only, so the compositor normally runs from a
container image. A native `sway` on PATH is preferred when present (development machines).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

IMAGE = os.environ.get("SDSS_COMPOSITOR_IMAGE", "localhost/sdss-compositor:latest")


def native_sway() -> str | None:
    return shutil.which("sway")


def podman_available() -> bool:
    return shutil.which("podman") is not None


def image_present() -> bool:
    if not podman_available():
        return False
    import subprocess

    result = subprocess.run(
        ["podman", "image", "exists", IMAGE], capture_output=True, check=False
    )
    return result.returncode == 0


def compositor_command(config: Path, runtime_dir: Path, home: Path | None = None) -> list[str]:
    sway = native_sway()
    if sway:
        return [sway, "-c", str(config)]
    if not podman_available():
        raise RuntimeError("no sway on PATH and podman is unavailable")

    home = home or Path.home()
    return [
        "podman",
        "run",
        "--rm",
        "--userns=keep-id",
        "--network=host",
        "--ipc=host",
        f"--volume={runtime_dir}:{runtime_dir}",
        f"--volume={home}:{home}",
        "--device=/dev/dri",
        f"--env=XDG_RUNTIME_DIR={runtime_dir}",
        f"--env=HOME={home}",
        "--env=WAYLAND_DISPLAY",
        "--env=WLR_BACKENDS",
        "--env=WLR_WL_OUTPUTS",
        "--env=WLR_HEADLESS_OUTPUTS",
        "--env=WLR_NO_HARDWARE_CURSORS",
        IMAGE,
        "-c",
        str(config),
    ]


def describe() -> str:
    if native_sway():
        return f"native sway ({native_sway()})"
    if image_present():
        return f"container image {IMAGE}"
    if podman_available():
        return f"container image {IMAGE} (NOT BUILT — run runtime/build.sh)"
    return "unavailable (no sway, no podman)"
