"""`sdss` command line."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import os
import shutil
import subprocess
import sys

from . import (
    hooks,
    launch,
    managed_config,
    paths,
    patch,
    profiles,
    runtime,
    state,
    stream,
)
from .session import Session, SessionError

log = logging.getLogger("sdss")
RECONCILE_ERRORS = (patch.PatchError, OSError, ValueError)


def _split_command(argv: list[str]) -> tuple[list[str], list[str]]:
    if "--" in argv:
        index = argv.index("--")
        return argv[:index], argv[index + 1 :]
    return argv, []


def cmd_profiles(_: argparse.Namespace) -> int:
    for profile in profiles.PROFILES:
        mark = "ok" if profile.verified else "unverified"
        print(f"{profile.id:<10} {profile.system:<6} {profile.name:<10} [{mark}]")
        for target in profile.configs:
            resolved = target.resolve()
            present = "present" if resolved.is_file() else "MISSING"
            print(f"  {target.format:<4} {resolved} ({present})")
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    problems = 0
    print("== tooling ==")
    print(f"  compositor  {runtime.describe()}")
    if not (runtime.native_sway() or runtime.image_present()):
        problems += 1
    sunshine = shutil.which("flatpak")
    print(f"  flatpak     {sunshine or 'MISSING'}  (Sunshine runtime)")
    problems += 0 if sunshine else 1

    print("== session ==")
    for var in ("WAYLAND_DISPLAY", "GAMESCOPE_WAYLAND_DISPLAY", "XDG_RUNTIME_DIR"):
        value = os.environ.get(var, "")
        print(f"  {var:<26} {value or '(unset)'}")
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("GAMESCOPE_WAYLAND_DISPLAY")):
        print("  hint: source /run/user/1000/gamescope-environment when running over SSH")
        problems += 1

    print("== emulator configs ==")
    for profile in profiles.PROFILES:
        for target in profile.configs:
            resolved = target.resolve()
            status = "ok" if resolved.is_file() else "MISSING"
            print(f"  {profile.id:<10} {status:<8} {resolved}")

    print("== state ==")
    current = state.load()
    print(f"  second screen mode: {'enabled' if current.enabled else 'disabled'}")
    journals = managed_config.active_journals()
    if managed_config.LEGACY_SESSION_JOURNAL in journals:
        print("  WARNING: legacy session config journal — run `sdss restore`")
        problems += 1

    print(f"\n{problems} problem(s)")
    return 1 if problems else 0


def cmd_run(args: argparse.Namespace) -> int:
    if not args.command:
        print("nothing to run: pass the emulator command after --", file=sys.stderr)
        return 2

    current = state.load()
    if args.profile:
        try:
            profile = profiles.get(args.profile)
        except KeyError as exc:
            # This runs from the Steam launch wrapper, so never fail the game launch over
            # a bad profile name — say why and hand the emulator through untouched.
            log.error("%s — launching unchanged", exc.args[0])
            return _exec_passthrough(args.command)
    else:
        profile = profiles.detect(args.command)
    if profile is None:
        log.info("no SDSS profile matches this command — launching unchanged")
        return _exec_passthrough(args.command)
    if not (current.enabled_for(profile.id) or args.dry_run):
        log.info("second screen mode is off — launching %s unchanged", profile.id)
        return _exec_passthrough(args.command)

    if not args.dry_run:
        try:
            with _reconcile_lock():
                managed_config.migrate_legacy_journal()
                managed_config.enable_profile(profile)
        except RECONCILE_ERRORS as exc:
            log.error("%s", exc)
            return 1

    session = Session(profile=profile, command=args.command, dry_run=args.dry_run)
    try:
        if args.dry_run:
            artifacts = session.write_artifacts()
            for name, path in artifacts.items():
                if not path.is_file():
                    print(f"== {name}: {path} (created at runtime) ==")
                    continue
                print(f"== {name}: {path} ==")
                print(path.read_text())
            return 0
        return session.run()
    except (SessionError, patch.PatchError, OSError, ValueError) as exc:
        log.error("%s", exc)
        return 1


def _exec_passthrough(command: list[str]) -> int:
    return subprocess.call(
        command, env=launch.restore_emulator_preload(dict(os.environ))
    )


def cmd_patch(args: argparse.Namespace) -> int:
    try:
        profile = profiles.get(args.profile)
    except KeyError as exc:
        print(exc.args[0], file=sys.stderr)
        return 2
    try:
        with _reconcile_lock():
            managed_config.migrate_legacy_journal()
            changed = managed_config.enable_profile(profile)
    except RECONCILE_ERRORS as exc:
        log.error("%s", exc)
        return 1
    for path in changed:
        print(f"patched {path}")
    if not changed:
        print("no changes needed")
    return 0


def cmd_restore(_: argparse.Namespace) -> int:
    previous = state.load()
    current = _copy_state(previous)
    current.enabled = False
    try:
        with _reconcile_lock():
            restored = managed_config.restore_all()
            _reconcile_hooks(current)
            state.save(current)
    except RECONCILE_ERRORS as exc:
        _rollback_reconcile(previous)
        log.error("%s", exc)
        return 1
    if not restored:
        print("nothing to restore")
    for path in restored:
        print(f"restored {path}")
    return 0


def cmd_enable(args: argparse.Namespace) -> int:
    previous = state.load()
    current = _copy_state(previous)
    if args.profile:
        try:
            profiles.get(args.profile)
        except KeyError as exc:
            print(exc.args[0], file=sys.stderr)
            return 2
        # A per-profile toggle must not move the master switch.
        current.profiles[args.profile] = args.value
    else:
        current.enabled = args.value
    try:
        with _reconcile_lock():
            managed_config.reconcile(_effective_profiles(current))
            _reconcile_hooks(current)
            state.save(current)
    except RECONCILE_ERRORS as exc:
        _rollback_reconcile(previous)
        log.error("%s", exc)
        return 1
    if args.profile:
        print(f"{args.profile}: {'enabled' if args.value else 'disabled'}")
    else:
        print(f"second screen mode: {'enabled' if current.enabled else 'disabled'}")
    return 0


def _reconcile_hooks(current: state.State) -> None:
    """Swap each profile's launcher for our wrapper (or restore it) to match its toggle.

    Called on every `enable`/`disable` and every `status` read — the latter is what the
    Decky plugin calls whenever its panel opens, which is what makes this self-heal an
    EmuDeck update that silently overwrote a wrapper while SDSS was already enabled.

    The caller holds `_reconcile_lock`, since config ownership and wrappers must move together.
    """
    errors: list[str] = []
    for profile in profiles.PROFILES:
        if profile.launcher_path is None:
            continue
        try:
            hooks.reconcile(profile, current.enabled_for(profile.id))
        except OSError as exc:
            errors.append(f"{profile.id}: {exc}")
    if errors:
        raise OSError("could not update launcher(s): " + "; ".join(errors))


def _effective_profiles(current: state.State) -> dict[str, bool]:
    return {
        profile.id: current.enabled_for(profile.id)
        for profile in profiles.PROFILES
    }


def _copy_state(current: state.State) -> state.State:
    return state.State(
        enabled=current.enabled,
        profiles=dict(current.profiles),
    )


def _rollback_reconcile(previous: state.State) -> None:
    try:
        with _reconcile_lock():
            managed_config.reconcile(_effective_profiles(previous))
            _reconcile_hooks(previous)
    except RECONCILE_ERRORS as exc:
        log.error("could not roll back failed toggle reconciliation: %s", exc)


@contextlib.contextmanager
def _reconcile_lock():
    lock_path = paths.hooks_lock_file()
    try:
        paths.ensure(lock_path.parent)
        handle = lock_path.open("w")
    except OSError as exc:
        # A lock we cannot create must not stop the toggle from working; the race it
        # guards against needs two concurrent invocations, which is the rarer case.
        log.warning("could not open %s (%s); proceeding without a lock", lock_path, exc)
        yield
        return
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        handle.close()


def cmd_status(args: argparse.Namespace) -> int:
    current = state.load()
    try:
        with _reconcile_lock():
            _reconcile_hooks(current)
    except OSError as exc:
        log.error("%s", exc)
        return 1
    if getattr(args, "json", False):
        # Consumed by the Decky plugin, so profile ids never have to be hardcoded there.
        print(
            json.dumps(
                {
                    "enabled": current.enabled,
                    "sunshine_port": stream.DEFAULT_PORT,
                    "profiles": [
                        {
                            "id": profile.id,
                            "name": profile.name,
                            "system": profile.system,
                            "verified": profile.verified,
                            "enabled": current.enabled_for(profile.id),
                        }
                        for profile in profiles.PROFILES
                    ],
                },
                indent=2,
            )
        )
        return 0
    print(f"enabled: {current.enabled}")
    for profile in profiles.PROFILES:
        print(f"  {profile.id:<10} {current.enabled_for(profile.id)}")
    print(f"sunshine port: {stream.DEFAULT_PORT}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sdss", description="Steam Deck Second Screen")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="check the host setup").set_defaults(func=cmd_doctor)
    sub.add_parser("profiles", help="list emulator profiles").set_defaults(func=cmd_profiles)
    status = sub.add_parser("status", help="show toggle state")
    status.add_argument("--json", action="store_true", help="machine-readable output")
    status.set_defaults(func=cmd_status)
    sub.add_parser("restore", help="restore emulator configs from the journal").set_defaults(
        func=cmd_restore
    )

    run = sub.add_parser("run", help="launch an emulator with the second screen")
    run.add_argument("--profile", help="force a profile instead of detecting one")
    run.add_argument("--dry-run", action="store_true", help="print generated config and stop")
    run.set_defaults(func=cmd_run)

    patch_cmd = sub.add_parser("patch", help="apply a profile's config edits")
    patch_cmd.add_argument("profile")
    patch_cmd.set_defaults(func=cmd_patch)

    enable = sub.add_parser("enable", help="turn second screen mode on")
    enable.add_argument("--profile")
    enable.set_defaults(func=cmd_enable, value=True)

    disable = sub.add_parser("disable", help="turn second screen mode off")
    disable.add_argument("--profile")
    disable.set_defaults(func=cmd_enable, value=False)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    own, command = _split_command(argv)
    args = build_parser().parse_args(own)
    args.command = command
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="sdss: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
