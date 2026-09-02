"""Launching the emulator on the host, pointed at the nested compositor.

Verified on hardware: a native (AppImage) Wayland client connects to the nested sway by
setting `WAYLAND_DISPLAY` alone, but a Flatpak needs the socket explicitly exposed into its
sandbox — `--socket=wayland` on its own binds the wrong display name.
"""

from __future__ import annotations

import os
import re

FLATPAK = "flatpak"
SAVED_LD_PRELOAD = "SDSS_EMULATOR_LD_PRELOAD"
STEAM_OVERLAY_LIBRARY = "gameoverlayrenderer.so"

# SteamOS ships gamescope's "XWayland Bypass" Vulkan layer as an *implicit* layer, so the
# loader injects it into every Vulkan client under a gamescope session — including one SDSS
# has redirected into the nested compositor. The layer assumes the surface it is presenting
# to belongs to the outer gamescope and reaches back to it, which is no longer true here:
# verified on hardware, Cemu segfaults (signal 11) inside the layer at swapchain teardown,
# and the resulting stack-trace spam is large enough to exhaust Steam's own 32-bit allocator
# ("tier0/memstd.cpp: OUT OF MEMORY" -> "Fatal assert; application exiting"), taking the
# whole Steam client down with it. The layer publishes this opt-out in its own manifest
# (/usr/share/vulkan/implicit_layer.d/VkLayer_FROG_gamescope_wsi.*.json).
DISABLE_GAMESCOPE_WSI = "DISABLE_GAMESCOPE_WSI"


def is_flatpak(command: list[str]) -> bool:
    return bool(command) and os.path.basename(command[0]) == FLATPAK


def flatpak_socket_args(wayland_display: str) -> list[str]:
    return [
        "--socket=wayland",
        f"--filesystem=xdg-run/{wayland_display}",
        f"--env=WAYLAND_DISPLAY={wayland_display}",
        # A Flatpak gets a fresh environment, so the host-side export below does not reach
        # it — the opt-out has to cross the sandbox boundary explicitly.
        f"--env={DISABLE_GAMESCOPE_WSI}=1",
    ]


def build_command(
    command: list[str], wayland_display: str, extra_args: tuple[str, ...] = ()
) -> list[str]:
    """Return `command` adjusted so it renders into the nested compositor."""
    command = list(command)
    command.extend(arg for arg in extra_args if arg not in command)
    if not is_flatpak(command):
        return command

    try:
        run_at = command.index("run")
    except ValueError:
        return command

    injected = flatpak_socket_args(wayland_display)
    already = {arg.split("=", 1)[0] for arg in command}
    injected = [arg for arg in injected if arg.split("=", 1)[0] not in already]
    return command[: run_at + 1] + injected + command[run_at + 1 :]


def build_env(
    base: dict[str, str],
    wayland_display: str,
    runtime_dir: str,
    *,
    x11_display: str | None = None,
    prefer_x11: bool = False,
    steam_overlay: bool = True,
) -> dict[str, str]:
    env = restore_emulator_preload(base, enabled=steam_overlay)
    env["WAYLAND_DISPLAY"] = wayland_display
    env["XDG_RUNTIME_DIR"] = runtime_dir
    # Set for both backends: the layer is implicit, so it loads regardless of whether the
    # emulator ends up on the nested Wayland or the nested Xwayland.
    env[DISABLE_GAMESCOPE_WSI] = "1"

    if prefer_x11 and x11_display:
        # Reached through the abstract X socket, shared with the container via host networking.
        env["DISPLAY"] = x11_display
        env["GDK_BACKEND"] = "x11"
        env["QT_QPA_PLATFORM"] = "xcb"
        return env

    # Without this the toolkit falls back to xcb and lands on the desktop session instead.
    env.setdefault("QT_QPA_PLATFORM", "wayland")
    env.setdefault("GDK_BACKEND", "wayland")
    env.pop("DISPLAY", None)
    return env


def restore_emulator_preload(
    base: dict[str, str], *, enabled: bool = True
) -> dict[str, str]:
    """Restore Steam's overlay preload only for the emulator process."""
    env = dict(base)
    if SAVED_LD_PRELOAD not in env:
        return env if enabled else _strip_steam_overlay_preload(env)
    preload = env.pop(SAVED_LD_PRELOAD)
    if enabled and preload:
        env["LD_PRELOAD"] = preload
    else:
        env.pop("LD_PRELOAD", None)
    return env if enabled else _strip_steam_overlay_preload(env)


def helper_env(base: dict[str, str]) -> dict[str, str]:
    """Prevent SDSS helpers from registering as Steam overlay clients."""
    env = dict(base)
    env.pop(SAVED_LD_PRELOAD, None)
    return _strip_steam_overlay_preload(env)


def _strip_steam_overlay_preload(env: dict[str, str]) -> dict[str, str]:
    preload = env.get("LD_PRELOAD", "")
    entries = [entry for entry in re.split(r"[:\s]+", preload) if entry]
    entries = [
        entry
        for entry in entries
        if os.path.basename(entry) != STEAM_OVERLAY_LIBRARY
    ]
    if entries:
        env["LD_PRELOAD"] = ":".join(entries)
    else:
        env.pop("LD_PRELOAD", None)
    return env
