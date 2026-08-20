"""Sunshine instance dedicated to the second screen.

It captures only sway's headless output and never streams audio — sound stays on the TV.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import launch, paths
from .compositor import HEADLESS_OUTPUT

APP_NAME = "Second Screen"
FLATPAK_ID = "dev.lizardbyte.app.Sunshine"
# Moonlight's CLI takes only a host, with no way to address a custom port, so SDSS owns the
# default port on the Steam Machine rather than running alongside another Sunshine.
DEFAULT_PORT = 47989


@dataclass(frozen=True)
class SunshineSpec:
    config_dir: Path
    port: int = DEFAULT_PORT
    name: str = "SDSS Second Screen"
    output: str = HEADLESS_OUTPUT


def default_spec() -> SunshineSpec:
    """The one Sunshine config location every caller shares."""
    return SunshineSpec(config_dir=paths.config_dir() / "sunshine")


def render_conf(spec: SunshineSpec) -> str:
    settings = {
        "sunshine_name": spec.name,
        "port": spec.port,
        "capture": "wlr",
        "output_name": spec.output,
        "stream_audio": "disabled",
        # Every "info"-level line (resolution, codec/vaapi details, bitrate, interface
        # discovery -- dozens of lines per launch) is stdout that gets relayed through
        # Steam's own log-capture pipeline (srt-logger) alongside everything else SDSS's
        # session produces, on top of every other process SDSS launches. "warning" still
        # surfaces anything Sunshine considers an actual problem (warning/error/fatal);
        # only the routine per-launch diagnostic chatter is cut.
        "min_log_level": "warning",
        "origin_web_ui_allowed": "lan",
        # There is no desktop shell in this headless bwrap sandbox for a tray icon to
        # attach to. Left at its "enabled" default, Sunshine still tries to create one on
        # every single launch, and its GTK/DBus teardown fails loudly on every single
        # session exit -- verified on hardware: "GLib-GIO-CRITICAL: Error while sending
        # AddMatch()/GetNameOwner() message: The connection is closed", a
        # Gtk-CRITICAL assertion failure, and a libayatana-appindicator-WARNING, on
        # every single teardown, all relayed through Steam's own log-capture pipeline
        # (srt-logger) alongside everything else SDSS's session produces. Disabling it
        # is strictly correct for this setup regardless of any other effect.
        "system_tray": "disabled",
        "file_apps": str(spec.config_dir / "apps.json"),
        "log_path": str(spec.config_dir / "sunshine.log"),
        "credentials_file": str(spec.config_dir / "credentials.json"),
        "file_state": str(spec.config_dir / "state.json"),
    }
    return "".join(f"{key} = {value}\n" for key, value in settings.items())


def render_apps() -> str:
    apps = {
        "env": {},
        "apps": [
            {
                "name": APP_NAME,
                "auto-detach": "true",
                "exclude-global-prep-cmd": "false",
                "prep-cmd": [],
            }
        ],
    }
    return json.dumps(apps, indent=4) + "\n"


def write_config(spec: SunshineSpec) -> Path:
    spec.config_dir.mkdir(parents=True, exist_ok=True)
    conf = spec.config_dir / "sunshine.conf"
    conf.write_text(render_conf(spec))
    (spec.config_dir / "apps.json").write_text(render_apps())
    return conf


def launch_command(spec: SunshineSpec, wayland_display: str, runtime_dir: str) -> list[str]:
    """Flatpak Sunshine, pointed at the nested sway socket instead of the gamescope one.

    `--socket=wayland` alone binds the wrong display name, so the socket is exposed
    explicitly. `-0` makes Sunshine read a pairing PIN from stdin, which is what lets the
    Deck pair without anyone touching the web UI.
    """
    return [
        "flatpak",
        "run",
        *launch.flatpak_socket_args(wayland_display),
        f"--env=XDG_RUNTIME_DIR={runtime_dir}",
        f"--filesystem={spec.config_dir}",
        "--device=all",
        FLATPAK_ID,
        str(spec.config_dir / "sunshine.conf"),
        "-0",
    ]
