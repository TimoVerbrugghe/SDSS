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
        "min_log_level": "info",
        "origin_web_ui_allowed": "lan",
        "file_apps": str(spec.config_dir / "apps.json"),
        "log_path": str(spec.config_dir / "sunshine.log"),
        "credentials_file": str(spec.config_dir / "credentials.json"),
        "file_state": str(spec.config_dir / "state.json"),
    }
    return "".join(f"{key} = {value}\n" for key, value in settings.items())


def app_name() -> str:
    """Sunshine app name shown in `moonlight list`.

    `HEADLESS-1` is always sized to `compositor.DECK_PANEL_RESOLUTION` regardless of
    profile (emulators like Azahar render their second screen at a fixed native aspect
    ratio and letterbox internally to fit any larger output, so matching the Deck's own
    panel size avoids an extra, squashing rescale on top of that). Since that size never
    varies today, `deck/sdss-connect.sh` just hardcodes the same constant rather than
    parsing it out of the app name, so this stays a plain, stable name.
    """
    return APP_NAME


def render_apps() -> str:
    apps = {
        "env": {},
        "apps": [
            {
                "name": app_name(),
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
