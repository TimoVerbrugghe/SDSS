"""What this device looks like right now.

Everything here is a *read*: the app decides what to offer from these facts, and every
repair is performed by re-running the same shell entry points a terminal would.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import paths
from .runner import Result, run as _run

Runner = Callable[..., Result]

STEAM_MACHINE = "steam-machine"
STEAM_DECK = "steam-deck"
ROLES = (STEAM_MACHINE, STEAM_DECK)
ROLE_LABELS = {
    STEAM_MACHINE: "Steam Machine (host)",
    STEAM_DECK: "Steam Deck (client)",
}

SUNSHINE_ID = "dev.lizardbyte.app.Sunshine"
MOONLIGHT_ID = "com.moonlight_stream.Moonlight"
COMPOSITOR_IMAGE = os.environ.get("SDSS_COMPOSITOR_IMAGE", "localhost/sdss-compositor:latest")

DMI_PRODUCT = Path("/sys/devices/virtual/dmi/id/product_name")
UDEV_RULE = Path("/etc/udev/rules.d/60-sdss-input.rules")
ATOMIC_KEEP = Path("/etc/atomic-update.conf.d/sdss-atomic-update.conf")
ATOMIC_KEEP_DIR = Path("/etc/atomic-update.conf.d")
DECKY_PLUGIN = "homebrew/plugins/SDSS"

#: Fix actions a check can ask for. The UI only ever maps these to `actions` helpers.
FIX_REPAIR = "repair"
FIX_UDEV = "udev"
FIX_RESTORE = "restore"


@dataclass
class Check:
    id: str
    label: str
    ok: bool
    detail: str
    fix: str | None = None
    #: Worth showing, but never a reason to call the install broken (PATH, an optional
    #: Decky Loader). Kept out of `Status.problems` so one cosmetic row cannot turn the
    #: whole dashboard red and send the user re-running a repair that changes nothing.
    advisory: bool = False


@dataclass
class Status:
    detected_role: str | None = None
    installed_role: str | None = None
    installed: bool = False
    installed_version: str | None = None
    installed_at: str | None = None
    install_path: str = ""
    app_version: str = "unknown"
    host_address: str | None = None
    steamos: bool = False
    checks: list[Check] = field(default_factory=list)
    #: `sdss status --json`, when the shim is installed and runnable.
    sdss: dict | None = None

    @property
    def role(self) -> str | None:
        return self.installed_role or self.detected_role

    @property
    def problems(self) -> list[Check]:
        return [check for check in self.checks if not check.ok and not check.advisory]

    def to_json(self) -> dict:
        payload = asdict(self)
        payload["role"] = self.role
        return payload


def role_label(role: str | None) -> str:
    return ROLE_LABELS.get(role or "", "unknown")


def detect_role(product_file: Path = DMI_PRODUCT) -> str | None:
    """Same DMI mapping as `install.sh`, so both agree on what this device is."""
    try:
        product = product_file.read_text().strip()
    except OSError:
        return None
    if product in ("Jupiter", "Galileo"):
        return STEAM_DECK
    if product == "Fremont":
        return STEAM_MACHINE
    return None


def installed_role() -> str | None:
    try:
        value = (paths.config_dir() / "installed-role").read_text().strip()
    except OSError:
        return None
    return value if value in ROLES else None


def host_address() -> str | None:
    """The Steam Machine address a Deck install was pointed at, if one was recorded."""
    try:
        value = (paths.config_dir() / "host").read_text().strip()
    except OSError:
        return None
    return value or None


def is_steamos(os_release: Path = Path("/etc/os-release")) -> bool:
    try:
        return "steam" in os_release.read_text().lower()
    except OSError:
        return False


def flatpak_installed(app_id: str) -> bool:
    """Whether a Flatpak app is present, without shelling out.

    `flatpak list` on a cold cache is slow enough to be visible when the dashboard opens,
    and this runs on every refresh. Both the per-user and system installation roots are
    checked because `install_flatpak` uses `--user` but the app may already be there.
    """
    roots = [
        paths.data_dir().parent / "flatpak/app",
        Path("/var/lib/flatpak/app"),
    ]
    return any((root / app_id).is_dir() for root in roots)


def podman_image_present(runner: Runner = _run) -> bool:
    return runner(["podman", "image", "exists", COMPOSITOR_IMAGE]).ok


def on_path(directory: Path) -> bool:
    entries = (os.environ.get("PATH") or "").split(os.pathsep)
    return str(directory) in entries


def sdss_status(runner: Runner = _run) -> dict | None:
    """`sdss status --json`, or None when the shim is missing or unusable."""
    binary = paths.sdss_bin()
    if not binary.is_file():
        return None
    result = runner([str(binary), "status", "--json"])
    if not result.ok:
        return None
    try:
        payload = json.loads(result.output)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _release_marker() -> dict:
    try:
        raw = json.loads((paths.install_root() / ".sdss-release.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def app_version() -> str:
    try:
        text = (paths.payload_root() / "VERSION").read_text()
    except OSError:
        return "unknown"
    return text.strip() or "unknown"


def _host_checks(status: Status, runner: Runner) -> list[Check]:
    checks = [
        Check(
            id="sunshine",
            label="Sunshine (Flatpak)",
            ok=flatpak_installed(SUNSHINE_ID),
            detail=SUNSHINE_ID,
            fix=FIX_REPAIR,
        ),
        Check(
            id="shim",
            label="sdss command",
            ok=paths.sdss_bin().is_file(),
            detail=str(paths.sdss_bin()),
            fix=FIX_REPAIR,
        ),
        Check(
            id="udev-rule",
            label="touch device udev rule",
            ok=UDEV_RULE.is_file(),
            detail=str(UDEV_RULE),
            fix=FIX_UDEV,
        ),
    ]
    # Only meaningful on SteamOS 3.6+, where unknown /etc changes are dropped on update.
    if ATOMIC_KEEP_DIR.is_dir():
        checks.append(
            Check(
                id="atomic-keep",
                label="survives SteamOS updates",
                ok=ATOMIC_KEEP.is_file(),
                detail=str(ATOMIC_KEEP),
                fix=FIX_UDEV,
            )
        )
    image_ok = podman_image_present(runner)
    checks.append(
        Check(
            id="compositor-image",
            label="compositor image",
            ok=image_ok,
            detail=COMPOSITOR_IMAGE if image_ok else f"{COMPOSITOR_IMAGE} (not built)",
            fix=FIX_REPAIR,
        )
    )
    plugin = paths.home() / DECKY_PLUGIN
    decky_present = (paths.home() / "homebrew/plugins").is_dir()
    if plugin.is_dir():
        detail = str(plugin)
    elif decky_present:
        detail = "not installed"
    else:
        detail = "Decky Loader is not installed"
    checks.append(
        Check(
            id="decky-plugin",
            label="Decky plugin",
            ok=plugin.is_dir(),
            detail=detail,
            fix=FIX_REPAIR,
            # Decky Loader is optional: this app and `sdss enable`/`disable` both work
            # without it, so a missing plugin is never a failed install.
            advisory=not decky_present,
        )
    )
    if status.sdss and status.sdss.get("patched_configs"):
        count = len(status.sdss["patched_configs"])
        checks.append(
            Check(
                id="patched-configs",
                label="emulator configs",
                ok=False,
                detail=f"{count} config(s) still patched from an interrupted session",
                fix=FIX_RESTORE,
            )
        )
    return checks


def _deck_checks() -> list[Check]:
    steam_root = paths.home() / ".steam/steam"
    template = steam_root / "controller_base/templates/sdss_second_screen.vdf"
    address = host_address()
    return [
        Check(
            id="moonlight",
            label="Moonlight (Flatpak)",
            ok=flatpak_installed(MOONLIGHT_ID),
            detail=MOONLIGHT_ID,
            fix=FIX_REPAIR,
        ),
        Check(
            id="connect",
            label="sdss-connect launcher",
            ok=paths.sdss_connect_bin().is_file(),
            detail=str(paths.sdss_connect_bin()),
            fix=FIX_REPAIR,
        ),
        Check(
            id="controller-template",
            label="controller template",
            ok=template.is_file(),
            detail=str(template),
            fix=FIX_REPAIR,
        ),
        Check(
            id="host-address",
            label="Steam Machine address",
            ok=bool(address),
            detail=address or "not configured",
            fix=FIX_REPAIR,
        ),
    ]


def probe(runner: Runner = _run) -> Status:
    status = Status()
    status.detected_role = detect_role()
    status.installed_role = installed_role()
    status.install_path = str(paths.install_root())
    status.installed = paths.install_root().is_dir()
    status.steamos = is_steamos()
    status.app_version = app_version()
    status.host_address = host_address()

    marker = _release_marker()
    version = marker.get("version")
    installed_at = marker.get("installed_at")
    status.installed_version = version if isinstance(version, str) else None
    status.installed_at = installed_at if isinstance(installed_at, str) else None

    if not status.installed:
        return status

    status.sdss = sdss_status(runner) if status.role == STEAM_MACHINE else None
    if status.role == STEAM_MACHINE:
        status.checks = _host_checks(status, runner)
    elif status.role == STEAM_DECK:
        status.checks = _deck_checks()
    status.checks.append(
        Check(
            id="path",
            label="~/.local/bin on PATH",
            ok=on_path(paths.home() / ".local/bin"),
            detail="only needed to run sdss from a terminal",
            # SDSS is driven from this app, the Decky plugin and Steam, none of which
            # resolve through PATH.
            advisory=True,
        )
    )
    return status
