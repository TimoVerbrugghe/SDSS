"""Per-emulator knowledge. Data only — no behaviour lives here.

Values are sourced from `docs/hardware-recon.md`; window matchers marked UNVERIFIED still
need spike S4 on real hardware.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .patch import INI, TOML, XML, Edit


def _cemu_edits() -> tuple[Edit, ...]:
    edits: list[Edit] = [Edit(key="open_pad", value="true")]
    gamepad_profile = os.environ.get("SDSS_CEMU_GAMEPAD_PROFILE", "").strip()
    if gamepad_profile:
        # Cemu profile keys differ across versions/builds; try both without failing if absent.
        edits.append(Edit(key="controllerProfile", value=gamepad_profile, required=False))
        edits.append(Edit(key="controller_profile", value=gamepad_profile, required=False))
    return tuple(edits)


@dataclass(frozen=True)
class ConfigTarget:
    path: str
    format: str
    edits: tuple[Edit, ...]

    def resolve(self) -> Path:
        return Path(os.path.expanduser(self.path))


@dataclass(frozen=True)
class WindowMatch:
    """sway criteria for a window. At least one field must be set."""

    app_id: tuple[str, ...] = ()
    title_regex: str | None = None

    def rules(self) -> tuple[str, ...]:
        """Criteria strings to match this window, ORed by the caller.

        sway ANDs everything inside one bracket, so app_id and title combine into a single
        rule; the app_id/class pair is duplicated because Xwayland views expose `class`.
        """
        title = f'title="{self.title_regex}"' if self.title_regex else ""
        if not self.app_id:
            if not title:
                raise ValueError("empty window match")
            return (title,)

        out: list[str] = []
        for app_id in self.app_id:
            escaped = re.escape(app_id)
            for attribute in ("app_id", "class"):
                criteria = f'{attribute}="^{escaped}$"'
                out.append(f"{criteria} {title}" if title else criteria)
        return tuple(out)


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    system: str
    detect: tuple[str, ...]
    second_window: WindowMatch
    configs: tuple[ConfigTarget, ...] = ()
    second_size: tuple[int, int] = (1280, 800)
    notes: str = ""
    verified: bool = False
    # Cemu's AppImage GTK build cannot talk Wayland, so it needs the nested Xwayland.
    needs_x11: bool = False
    extra_env: dict[str, str] = field(default_factory=dict)
    # Path EmuDeck's own launcher script execs. SDSS never edits that script — it swaps
    # this file for a wrapper (see hooks.py) so `sdss run` still wraps a normal Steam launch.
    launcher_path: str | None = None
    # Command used when the launcher path is an export or other indirection rather than the
    # emulator command itself.
    launcher_command: tuple[str, ...] | None = None


CEMU = Profile(
    id="cemu",
    name="Cemu",
    system="Wii U",
    detect=("Cemu.AppImage", "cemu", "info.cemu.Cemu"),
    configs=(
        ConfigTarget(
            path="~/.config/Cemu/settings.xml",
            format=XML,
            edits=_cemu_edits(),
        ),
    ),
    # Verified on hardware: "GamePad View - FPS: 60.10", class Cemu.
    second_window=WindowMatch(app_id=("Cemu",), title_regex="^GamePad View"),
    second_size=(854, 480),
    verified=True,
    needs_x11=True,
    launcher_path="~/Applications/Cemu.AppImage",
    notes="Separate GamePad view renders the Wii U GamePad screen at 854x480. The official "
    "AppImage has no working GTK Wayland backend (cemu-project/Cemu#1809), so it runs on "
    "the nested Xwayland.",
)

AZAHAR = Profile(
    id="azahar",
    name="Azahar",
    system="3DS",
    detect=("azahar.AppImage", "azahar", "org.azahar_emu.Azahar"),
    configs=(
        ConfigTarget(
            path="~/.config/azahar-emu/qt-config.ini",
            format=INI,
            edits=(
                # LayoutOption::SeparateWindows == 4
                Edit(section="Layout", key="layout_option", value="4"),
                Edit(section="Layout", key="layout_option\\default", value="false"),
                # SecondaryDisplayLayout::BottomScreenOnly == 2
                Edit(section="Layout", key="secondary_display_layout", value="2"),
                Edit(section="Layout", key="secondary_display_layout\\default", value="false"),
            ),
        ),
    ),
    # Verified on hardware: "Azahar 2126.0 | <game> | Secondary Window".
    second_window=WindowMatch(
        app_id=("org.azahar_emu.Azahar",), title_regex="Secondary Window$"
    ),
    second_size=(320, 240),
    verified=True,
    launcher_path="~/Applications/azahar.AppImage",
    notes="Qt writes a `key\\default` marker next to every setting; it must be false or "
    "Azahar restores its own default on launch.",
)

MELONDS = Profile(
    id="melonds",
    name="melonDS",
    system="DS",
    detect=("melonDS", "net.kuribo64.melonDS"),
    configs=(
        ConfigTarget(
            path="~/.var/app/net.kuribo64.melonDS/config/melonDS/melonDS.toml",
            format=TOML,
            edits=(
                # screenSizing: 4 = TopOnly, 5 = BotOnly
                Edit(section="Instance0.Window0", key="ScreenSizing", value="4"),
                Edit(section="Instance0.Window1", key="Enabled", value="true"),
                Edit(section="Instance0.Window1", key="ScreenSizing", value="5"),
                Edit(section="Instance0.Window1", key="ScreenLayout", value="0"),
                Edit(section="Instance0.Window1", key="Width", value="256"),
                Edit(section="Instance0.Window1", key="Height", value="192"),
            ),
        ),
    ),
    # The second-window title matcher is not verified on hardware; matching the complete
    # title suffix is safer than moving both indistinguishable melonDS windows to the Deck.
    second_window=WindowMatch(app_id=("melonDS",), title_regex="melonDS.*2"),
    second_size=(256, 192),
    needs_x11=True,
    launcher_path="~/.local/share/flatpak/exports/bin/net.kuribo64.melonDS",
    launcher_command=("flatpak", "run", "net.kuribo64.melonDS"),
    notes="melonDS 1.x migrates melonDS.ini to melonDS.toml on first save. Like Cemu it "
    "only maps windows on Xwayland. The second-window title match remains unverified.",
)

PROFILES: tuple[Profile, ...] = (CEMU, AZAHAR, MELONDS)


def get(profile_id: str) -> Profile:
    for profile in PROFILES:
        if profile.id == profile_id:
            return profile
    known = ", ".join(p.id for p in PROFILES)
    raise KeyError(f"unknown profile {profile_id!r} (known: {known})")


def detect(command: list[str]) -> Profile | None:
    """Pick the profile whose marker appears in the launch command."""
    haystack = " ".join(command).lower()
    for profile in PROFILES:
        if any(marker.lower() in haystack for marker in profile.detect):
            return profile
    return None
