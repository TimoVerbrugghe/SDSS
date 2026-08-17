"""Host self-check, as data.

The text `sdss doctor` output and the `--json` payload are two renderings of the same
list, so the desktop app never has to screen-scrape prose that was written for a human.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import asdict, dataclass, field

from . import patch, paths, profiles, runtime, state

SESSION_VARS = ("WAYLAND_DISPLAY", "GAMESCOPE_WAYLAND_DISPLAY", "XDG_RUNTIME_DIR")


@dataclass
class Check:
    id: str
    section: str
    label: str
    detail: str
    ok: bool = True
    #: Counted in the "N problem(s)" total and shown red by the app. Informational rows
    #: (session variables, per-emulator config presence) are `ok` but never `problem`.
    problem: bool = False
    hint: str | None = None
    fix: str | None = None


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    @property
    def problems(self) -> int:
        return sum(1 for check in self.checks if check.problem)

    def to_json(self) -> dict:
        return {
            "problems": self.problems,
            "checks": [asdict(check) for check in self.checks],
        }


def run() -> Report:
    report = Report()

    compositor = runtime.describe()
    usable = bool(runtime.native_sway() or runtime.image_present())
    report.checks.append(
        Check(
            id="compositor",
            section="tooling",
            label="compositor",
            detail=compositor,
            ok=usable,
            problem=not usable,
            hint=None if usable else "run runtime/build.sh to build the compositor image",
            fix=None if usable else "compositor",
        )
    )

    flatpak = shutil.which("flatpak")
    report.checks.append(
        Check(
            id="flatpak",
            section="tooling",
            label="flatpak",
            detail=f"{flatpak or 'MISSING'}  (Sunshine runtime)",
            ok=bool(flatpak),
            problem=not flatpak,
            hint=None if flatpak else "flatpak is required for Sunshine",
        )
    )

    for var in SESSION_VARS:
        value = os.environ.get(var, "")
        report.checks.append(
            Check(
                id=f"env:{var}",
                section="session",
                label=var,
                detail=value or "(unset)",
            )
        )
    has_display = bool(
        os.environ.get("WAYLAND_DISPLAY") or os.environ.get("GAMESCOPE_WAYLAND_DISPLAY")
    )
    if not has_display:
        report.checks.append(
            Check(
                id="session-display",
                section="session",
                label="wayland display",
                detail="no Wayland display in the environment",
                ok=False,
                problem=True,
                hint="source /run/user/1000/gamescope-environment when running over SSH",
            )
        )

    for profile in profiles.PROFILES:
        for index, target in enumerate(profile.configs):
            resolved = target.resolve()
            present = resolved.is_file()
            report.checks.append(
                Check(
                    id=f"config:{profile.id}:{index}",
                    section="emulator configs",
                    label=profile.id,
                    detail=f"{'ok' if present else 'MISSING'}  {resolved}",
                    ok=present,
                )
            )

    current = state.load()
    report.checks.append(
        Check(
            id="second-screen",
            section="state",
            label="second screen mode",
            detail="enabled" if current.enabled else "disabled",
        )
    )
    journal = patch.Journal(paths.backup_dir(), "session")
    if journal.exists:
        report.checks.append(
            Check(
                id="stale-journal",
                section="state",
                label="config journal",
                detail="stale config journal from an interrupted session",
                ok=False,
                problem=True,
                hint="run `sdss restore`",
                fix="restore",
            )
        )
    return report
