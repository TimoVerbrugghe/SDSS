"""Entry point for the SDSS app: a GUI by default, the same actions on the command line.

The GUI is never allowed to be the only way to do something — an SSH shell, CI and a
device with no display must all be able to drive the same install, so every window has a
flag equivalent here.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .core import actions, elevate, paths, probe, runner, selfinstall, update


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sdss-app",
        description="Install, update and manage Steam Deck Second Screen",
    )
    parser.add_argument("--role", choices=list(probe.ROLES), help="install for this endpoint")
    parser.add_argument("--host", help="Steam Machine address (Steam Deck installs)")
    parser.add_argument(
        "--stage-only", action="store_true", help="copy the release without setting anything up"
    )
    parser.add_argument("--uninstall", action="store_true", help="remove SDSS from this device")
    parser.add_argument(
        "--keep-configs", action="store_true", help="with --uninstall: leave emulator configs patched"
    )
    parser.add_argument("--status", action="store_true", help="print the status as JSON and exit")
    parser.add_argument("--version", action="store_true", help="print the app version and exit")
    parser.add_argument(
        "--no-gui", action="store_true", help="never open a window, even when a display exists"
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="build the window offscreen and exit; changes nothing on the system",
    )
    return parser


def has_display() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))


def _echo(line: str) -> None:
    print(line, flush=True)


def _install(role: str, host: str | None, stage_only: bool) -> int:
    command = actions.install_command(role, host, stage_only=stage_only)
    result = runner.run(command, _echo)
    if not result.ok:
        return result.returncode
    installed = selfinstall.install_self()
    if installed:
        _echo(f"installed {installed.target}")
        _echo(f"installed {installed.desktop_entry}")
    return 0


def _uninstall(keep_configs: bool) -> int:
    return runner.run(actions.uninstall_command(keep_configs=keep_configs), _echo).returncode


def _print_status() -> int:
    import json

    print(json.dumps(probe.probe().to_json(), indent=2))
    return 0


def _self_test() -> int:
    """Prove the bundled Qt loads and the window builds, without touching the system.

    The AppImage is built on a machine that is not the target and carries its own Qt, so
    "it packaged successfully" says nothing about whether it can draw. This is the cheapest
    check that would actually fail on a broken bundle, and CI runs it on every build.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from .ui.main import self_test

    self_test()
    print("self-test ok")
    return 0


def _summary() -> int:
    """What a bare run with no display prints.

    Never an install: an app opened by accident over SSH, or on a machine with no display,
    must not start rewriting the system because it could not draw a window.
    """
    status = probe.probe()
    print(f"SDSS app {status.app_version}")
    if not status.installed:
        role = status.detected_role
        suggestion = role or "steam-machine|steam-deck"
        print(f"SDSS is not installed ({status.install_path} is missing).")
        print(f"Install it with:  sdss-app --role {suggestion}")
        if role == probe.STEAM_DECK or role is None:
            print("A Steam Deck install also needs --host <steam-machine-address>.")
        return 0
    print(f"installed:  {status.installed_version or 'unknown'}")
    print(f"role:       {probe.role_label(status.role)}")
    print(f"path:       {status.install_path}")
    for check in status.checks:
        print(f"  {'ok  ' if check.ok else 'FAIL'}  {check.label}: {check.detail}")
    problems = len(status.problems)
    print(f"\n{problems} problem(s). Repair with:  sdss-app --role {status.role}")
    return 0


def _headless(args: argparse.Namespace) -> int:
    if args.uninstall:
        return _uninstall(args.keep_configs)
    role = args.role or probe.installed_role() or probe.detect_role()
    if role is None:
        print(
            "cannot tell what this device is; pass --role steam-machine or --role steam-deck",
            file=sys.stderr,
        )
        return 2
    host = args.host or probe.host_address()
    if role == probe.STEAM_DECK and not host and not args.stage_only:
        print("a Steam Machine address is required: pass --host", file=sys.stderr)
        return 2
    return _install(role, host, args.stage_only)


def _gui(args: argparse.Namespace) -> int:
    try:
        from .ui.main import run_gui
    except ImportError as exc:  # PySide6 missing: only ever true outside the AppImage
        print(f"the graphical interface is unavailable ({exc}); falling back to the terminal")
        return _headless(args)
    return run_gui(args)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print(probe.app_version())
        return 0
    if args.status:
        return _print_status()
    if args.self_test:
        return _self_test()
    explicit = bool(args.uninstall or args.role or args.stage_only)
    if explicit:
        return _headless(args)
    if args.no_gui or not has_display():
        return _summary()
    return _gui(args)


if __name__ == "__main__":
    raise SystemExit(main())
