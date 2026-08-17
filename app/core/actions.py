"""The commands the app runs, built as data so they can be asserted in tests.

Nothing here executes anything: the UI hands these to `runner.run` (or `elevate.run_elevated`)
so the log pane shows exactly the command a user could have typed themselves.
"""

from __future__ import annotations

from pathlib import Path

from . import paths, probe

INSTALL_SCRIPT = "install.sh"
UNINSTALL_SCRIPT = "packaging/uninstall.sh"
UDEV_SCRIPT = "packaging/install-udev-rule.sh"


def script_root(script: str) -> Path:
    """Where to run `script` from: the installed release if it has it, else our payload.

    Repairs prefer the installed tree so what runs is what is actually installed, but a
    first install (or a wrecked release directory) has to fall back to the copy the
    AppImage carries.
    """
    installed = paths.install_root() / script
    if installed.is_file():
        return paths.install_root()
    return paths.payload_root()


def install_command(
    role: str,
    host: str | None = None,
    *,
    stage_only: bool = False,
    source: Path | None = None,
) -> list[str]:
    """`install.sh` for `role`, run from the tree the app carries.

    Always run from the payload: the point of the app is that a newer AppImage *is* newer
    code, so an install or update must copy from here rather than re-run whatever version
    is already installed.
    """
    payload = Path(source) if source else paths.payload_root()
    command = [str(payload / INSTALL_SCRIPT), "--role", role]
    if role == probe.STEAM_DECK and host:
        command += ["--host", host]
    if stage_only:
        command.append("--stage-only")
    return command


def repair_command(role: str, host: str | None = None) -> list[str]:
    """Re-running the installer *is* the repair; every step of it is idempotent."""
    return install_command(role, host)


def uninstall_command(*, keep_configs: bool = False) -> list[str]:
    command = [str(script_root(UNINSTALL_SCRIPT) / UNINSTALL_SCRIPT), "--yes"]
    if keep_configs:
        command.append("--keep-configs")
    return command


def udev_command() -> list[str]:
    """Elevated. Installs the rule that lets the touch bridge grab Sunshine's devices."""
    return [str(script_root(UDEV_SCRIPT) / UDEV_SCRIPT)]


def remove_etc_command() -> list[str]:
    """Elevated. The two files `uninstall.sh` deliberately leaves behind."""
    return ["rm", "-f", str(probe.UDEV_RULE), str(probe.ATOMIC_KEEP)]


def sdss_command(*args: str) -> list[str]:
    """A `sdss` subcommand, run through the installed shim.

    Falls back to the payload's package with PYTHONPATH set, so the dashboard still works
    when the shim is the very thing that is missing.
    """
    shim = paths.sdss_bin()
    if shim.is_file():
        return [str(shim), *args]
    src = script_root(INSTALL_SCRIPT) / "host/src"
    code = (
        f"import sys; sys.path.insert(0, {str(src)!r}); "
        "from sdss.cli import main; raise SystemExit(main(sys.argv[1:]))"
    )
    return ["python3", "-c", code, *args]


def restore_command() -> list[str]:
    return sdss_command("restore")


def toggle_command(enabled: bool, profile: str | None = None) -> list[str]:
    command = sdss_command("enable" if enabled else "disable")
    if profile:
        command += ["--profile", profile]
    return command


def status_command() -> list[str]:
    return sdss_command("status", "--json")
