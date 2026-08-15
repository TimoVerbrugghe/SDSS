"""Sunshine instance dedicated to the second screen.

It captures only sway's headless output and never streams audio — sound stays on the TV.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .compositor import HEADLESS_OUTPUT

APP_NAME = "Second Screen"
FLATPAK_ID = "dev.lizardbyte.app.Sunshine"
DEFAULT_PORT = 47999


@dataclass(frozen=True)
class SunshineSpec:
    config_dir: Path
    port: int = DEFAULT_PORT
    name: str = "SDSS Second Screen"
    output: str = HEADLESS_OUTPUT


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
    """Flatpak Sunshine, pointed at the nested sway socket instead of the gamescope one."""
    return [
        "flatpak",
        "run",
        f"--env=WAYLAND_DISPLAY={wayland_display}",
        f"--env=XDG_RUNTIME_DIR={runtime_dir}",
        f"--filesystem={runtime_dir}",
        f"--filesystem={spec.config_dir}",
        "--device=all",
        FLATPAK_ID,
        str(spec.config_dir / "sunshine.conf"),
    ]
