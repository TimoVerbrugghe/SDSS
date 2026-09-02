"""Persistent ownership of the emulator settings SDSS manages while enabled."""

from __future__ import annotations

from pathlib import Path

from . import paths, patch
from .profiles import PROFILES, Profile

LEGACY_SESSION_JOURNAL = "session"
PROFILE_JOURNAL_PREFIX = "enabled-"


def journal_for(profile: Profile) -> patch.Journal:
    return patch.Journal(
        paths.backup_dir(), f"{PROFILE_JOURNAL_PREFIX}{profile.id}"
    )


def legacy_journal() -> patch.Journal:
    return patch.Journal(paths.backup_dir(), LEGACY_SESSION_JOURNAL)


def migrate_legacy_journal() -> list[Path]:
    journal = legacy_journal()
    return journal.restore_snapshots() if journal.exists else []


def enable_profile(profile: Profile, *, missing_ok: bool = False) -> list[Path]:
    journal = journal_for(profile)
    changed: list[Path] = []
    for target in profile.resolved_files():
        path = target.resolve()
        if patch.write_file_if_absent(path, target.content, journal):
            changed.append(path)
    for target in profile.configs:
        path = target.resolve()
        if missing_ok and not path.is_file():
            continue
        if patch.patch_file(path, target.format, target.resolved_edits(), journal):
            changed.append(path)
    return changed


def disable_profile(profile: Profile) -> list[Path]:
    journal = journal_for(profile)
    return journal.restore_snapshots() if journal.exists else []


def reconcile(enabled_by_profile: dict[str, bool]) -> list[Path]:
    changed = migrate_legacy_journal()
    for profile in PROFILES:
        if enabled_by_profile.get(profile.id, False):
            changed.extend(enable_profile(profile, missing_ok=True))
        else:
            changed.extend(disable_profile(profile))
    return changed


def restore_all() -> list[Path]:
    restored = migrate_legacy_journal()
    for profile in PROFILES:
        restored.extend(disable_profile(profile))
    return restored


def active_journals() -> tuple[str, ...]:
    names = [
        f"{PROFILE_JOURNAL_PREFIX}{profile.id}"
        for profile in PROFILES
        if journal_for(profile).exists
    ]
    if legacy_journal().exists:
        names.append(LEGACY_SESSION_JOURNAL)
    return tuple(names)
