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
    extra_env: dict[str, str] = field(default_factory=dict)


CEMU = Profile(
    id="cemu",
    name="Cemu",
    system="Wii U",
    detect=("Cemu.AppImage", "cemu", "info.cemu.Cemu"),
    configs=(
        ConfigTarget(
            path="~/.config/Cemu/settings.xml",
            format=XML,
            edits=(Edit(key="open_pad", value="true"),),
        ),
    ),
    # Cemu's separate GamePad window is titled "GamePad View". UNVERIFIED (S4).
    second_window=WindowMatch(title_regex="GamePad View"),
    second_size=(854, 480),
    notes="Separate GamePad view renders the Wii U GamePad screen at 854x480.",
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
    # UNVERIFIED (S4): Azahar's secondary render window title.
    second_window=WindowMatch(title_regex="Secondary"),
    second_size=(320, 240),
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
                # screenSizing_BotOnly == 5, screenLayout_Natural == 0
                Edit(section="Instance0.Window1", key="ScreenSizing", value="5"),
                Edit(section="Instance0.Window1", key="ScreenLayout", value="0"),
                Edit(section="Instance0.Window1", key="Width", value="256"),
                Edit(section="Instance0.Window1", key="Height", value="192"),
            ),
        ),
    ),
    # UNVERIFIED (S4): whether melonDS re-creates Window1 from config alone.
    second_window=WindowMatch(app_id=("melonDS",), title_regex="melonDS.*2"),
    second_size=(256, 192),
    notes="melonDS 1.x migrates melonDS.ini to melonDS.toml on first save; the toml must "
    "exist before SDSS can patch it.",
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
