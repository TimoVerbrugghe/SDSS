"""`sdss` command line."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys

from . import paths, patch, profiles, runtime, state, stream
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
    import os

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
    journal = patch.Journal(paths.backup_dir(), "session")
    if journal.exists:
        print("  WARNING: stale config journal — run `sdss restore`")
        problems += 1

    print(f"\n{problems} problem(s)")
    return 1 if problems else 0


def cmd_run(args: argparse.Namespace) -> int:
    if not args.command:
        print("nothing to run: pass the emulator command after --", file=sys.stderr)
        return 2

    current = state.load()
    profile = profiles.get(args.profile) if args.profile else profiles.detect(args.command)
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
    except SessionError as exc:
        log.error("%s", exc)
        return 1


def _exec_passthrough(command: list[str]) -> int:
    import subprocess

    return subprocess.call(command)


def cmd_patch(args: argparse.Namespace) -> int:
    profile = profiles.get(args.profile)
    session = Session(profile=profile, command=[])
    changed = session.patch_configs()
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
    current.enabled = args.value
    if args.profile:
        current.profiles[args.profile] = args.value
    state.save(current)
    print(f"second screen mode: {'enabled' if current.enabled else 'disabled'}")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    current = state.load()
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
    sub.add_parser("status", help="show toggle state").set_defaults(func=cmd_status)
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
