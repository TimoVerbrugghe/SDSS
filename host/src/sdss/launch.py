"""Launching the emulator on the host, pointed at the nested compositor.

Verified on hardware: a native (AppImage) Wayland client connects to the nested sway by
setting `WAYLAND_DISPLAY` alone, but a Flatpak needs the socket explicitly exposed into its
sandbox — `--socket=wayland` on its own binds the wrong display name.
"""

from __future__ import annotations

import os

FLATPAK = "flatpak"


def is_flatpak(command: list[str]) -> bool:
    return bool(command) and os.path.basename(command[0]) == FLATPAK


def flatpak_socket_args(wayland_display: str) -> list[str]:
    return [
        "--socket=wayland",
        f"--filesystem=xdg-run/{wayland_display}",
        f"--env=WAYLAND_DISPLAY={wayland_display}",
    ]


def build_command(command: list[str], wayland_display: str) -> list[str]:
    """Return `command` adjusted so it renders into the nested compositor."""
    if not is_flatpak(command):
        return list(command)

    try:
        run_at = command.index("run")
    except ValueError:
        return list(command)

    injected = flatpak_socket_args(wayland_display)
    already = {arg.split("=", 1)[0] for arg in command}
    injected = [arg for arg in injected if arg.split("=", 1)[0] not in already]
    return command[: run_at + 1] + injected + command[run_at + 1 :]


def build_env(base: dict[str, str], wayland_display: str, runtime_dir: str) -> dict[str, str]:
    env = dict(base)
    env["WAYLAND_DISPLAY"] = wayland_display
    env["XDG_RUNTIME_DIR"] = runtime_dir
    # Qt would otherwise fall back to xcb and land on the desktop session instead.
    env.setdefault("QT_QPA_PLATFORM", "wayland")
    env.setdefault("GDK_BACKEND", "wayland")
    env.pop("DISPLAY", None)
    return env
