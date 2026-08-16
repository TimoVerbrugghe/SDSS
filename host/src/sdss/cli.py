"""`sdss` command line."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import subprocess
import sys

from . import doctor, hooks, paths, patch, profiles, release, state, stream
from .session import Session, SessionError

log = logging.getLogger("sdss")


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


def cmd_doctor(args: argparse.Namespace) -> int:
    report = doctor.run()
    if getattr(args, "json", False):
        payload = report.to_json()
        payload["release"] = release.installed()
        print(json.dumps(payload, indent=2))
        return 1 if report.problems else 0

    section = None
    for check in report.checks:
        if check.section != section:
            section = check.section
            print(f"== {section} ==")
        print(f"  {check.label:<26} {check.detail}")
        if check.hint:
            print(f"  hint: {check.hint}")
    print(f"\n{report.problems} problem(s)")
    return 1 if report.problems else 0


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
    if not (current.enabled_for(profile.id) or args.force or args.dry_run):
        log.info("second screen mode is off — launching %s unchanged", profile.id)
        return _exec_passthrough(args.command)

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
    except (SessionError, patch.PatchError) as exc:
        log.error("%s", exc)
        return 1


def _exec_passthrough(command: list[str]) -> int:
    return subprocess.call(command)


def cmd_patch(args: argparse.Namespace) -> int:
    try:
        profile = profiles.get(args.profile)
    except KeyError as exc:
        print(exc.args[0], file=sys.stderr)
        return 2
    session = Session(profile=profile, command=[])
    try:
        changed = session.patch_configs()
    except patch.PatchError as exc:
        log.error("%s", exc)
        return 1
    for path in changed:
        print(f"patched {path}")
    if not changed:
        print("no changes needed")
    return 0


def cmd_restore(_: argparse.Namespace) -> int:
    journal = patch.Journal(paths.backup_dir(), "session")
    if not journal.exists:
        print("nothing to restore")
        return 0
    for path in journal.restore():
        print(f"restored {path}")
    return 0


def cmd_enable(args: argparse.Namespace) -> int:
    current = state.load()
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
    state.save(current)
    _reconcile_hooks(current)
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

    Held under an exclusive lock: each reconcile is a check-then-rename on a path Steam
    also launches from, so two overlapping invocations (a panel open racing an enable, or
    two panel opens) could otherwise interleave into shadowing an already-shadowed binary.
    """
    with _hooks_lock():
        for profile in profiles.PROFILES:
            if profile.launcher_path is None:
                continue
            try:
                hooks.reconcile(profile, current.enabled_for(profile.id))
            except OSError as exc:
                log.warning("could not update launcher for %s: %s", profile.id, exc)


@contextlib.contextmanager
def _hooks_lock():
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


def _patched_configs() -> list:
    """Config files the journal is currently holding backups for.

    Best effort on purpose: this feeds a status display, and an unreadable manifest must
    not take the whole payload down with it — `sdss restore` is where that error belongs.
    """
    journal = patch.Journal(paths.backup_dir(), "session")
    if not journal.exists:
        return []
    try:
        return journal.recorded_paths()
    except patch.PatchError as exc:
        log.warning("could not read the config journal: %s", exc)
        return []


def cmd_status(args: argparse.Namespace) -> int:
    current = state.load()
    _reconcile_hooks(current)
    if getattr(args, "json", False):
        # Consumed by the Decky plugin, so profile ids never have to be hardcoded there.
        # Keys are only ever *added* here: the plugin ships prebuilt and an older bundle
        # keeps working only as long as the existing shape is untouched.
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
                            "hooked": hooks.is_installed(profile),
                        }
                        for profile in profiles.PROFILES
                    ],
                    "release": release.installed(),
                    "patched_configs": [str(path) for path in _patched_configs()],
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

    doctor_cmd = sub.add_parser("doctor", help="check the host setup")
    doctor_cmd.add_argument("--json", action="store_true", help="machine-readable output")
    doctor_cmd.set_defaults(func=cmd_doctor)
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
    run.add_argument("--force", action="store_true", help="ignore the enabled toggle")
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
