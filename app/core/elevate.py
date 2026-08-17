"""Getting root for the one step that needs it, from a process with no terminal.

Only `packaging/install-udev-rule.sh` (and removing what it installed) needs root. A GUI
has nowhere for `sudo` to print its prompt, so the strategy is chosen up front rather than
discovered by hanging on an invisible password prompt.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from .runner import Result, run as _run

Runner = Callable[..., Result]

#: Already root — nothing to do.
NONE = "none"
#: `sudo` is configured to not ask (SteamOS default for the `deck` user in some images).
SUDO_NOPASSWD = "sudo-nopasswd"
#: A polkit agent is running, so pkexec can put the prompt on screen itself.
PKEXEC = "pkexec"
#: Ask in our own dialog and feed `sudo -S`.
SUDO_PASSWORD = "sudo-password"
#: No way to elevate at all.
UNAVAILABLE = "unavailable"

#: `passwd -S` first field after the name.
PASSWORD_SET = "set"
PASSWORD_NONE = "none"
PASSWORD_LOCKED = "locked"
PASSWORD_UNKNOWN = "unknown"


@dataclass(frozen=True)
class Plan:
    method: str
    #: True when the caller must collect a password before running the command.
    needs_password: bool
    #: Why elevation is impossible, or what the user must do first.
    reason: str | None = None

    @property
    def possible(self) -> bool:
        return self.method != UNAVAILABLE


def is_root() -> bool:
    return os.geteuid() == 0


def polkit_agent_running() -> bool:
    """Whether some process looks like a polkit authentication agent.

    Plasma starts one, but SDSS also has to work in stripped sessions, and pkexec with no
    agent fails with a message that reads like a permissions bug. Reading `/proc/*/comm`
    keeps this free of a `ps` dependency.
    """
    try:
        entries = os.listdir("/proc")
    except OSError:
        return False
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            comm = Path(f"/proc/{entry}/comm").read_text().strip()
        except OSError:
            continue
        lowered = comm.lower()
        if "polkit" in lowered and "agent" in lowered:
            return True
    return False


def sudo_is_passwordless(runner: Runner = _run) -> bool:
    if shutil.which("sudo") is None:
        return False
    # -n never prompts: it either succeeds because no password is needed, or fails at once.
    return runner(["sudo", "-n", "true"]).ok


def password_status(runner: Runner = _run) -> str:
    """Whether this account even has a password, which `sudo` would otherwise reject.

    A fresh SteamOS install leaves the `deck` user with no password set, and every sudo
    attempt then fails authentication no matter what is typed. Telling the user to run
    `passwd` is the only useful response, so it is detected rather than looped on.
    """
    if shutil.which("passwd") is None:
        return PASSWORD_UNKNOWN
    user = os.environ.get("USER") or ""
    command = ["passwd", "-S"] + ([user] if user else [])
    result = runner(command)
    if not result.ok:
        return PASSWORD_UNKNOWN
    fields = result.output.split()
    if len(fields) < 2:
        return PASSWORD_UNKNOWN
    return {"P": PASSWORD_SET, "NP": PASSWORD_NONE, "L": PASSWORD_LOCKED}.get(
        fields[1], PASSWORD_UNKNOWN
    )


def plan(runner: Runner = _run) -> Plan:
    if is_root():
        return Plan(NONE, needs_password=False)
    if sudo_is_passwordless(runner):
        return Plan(SUDO_NOPASSWD, needs_password=False)
    if shutil.which("pkexec") and polkit_agent_running():
        return Plan(PKEXEC, needs_password=False)
    if shutil.which("sudo") is None:
        return Plan(UNAVAILABLE, needs_password=False, reason="sudo is not installed")
    status = password_status(runner)
    if status in (PASSWORD_NONE, PASSWORD_LOCKED):
        return Plan(
            UNAVAILABLE,
            needs_password=False,
            reason=(
                "this account has no password set, so sudo can never authenticate. "
                "Open a terminal, run `passwd` to set one, then try again."
            ),
        )
    return Plan(SUDO_PASSWORD, needs_password=True)


def build(command: Iterable[str], method: str) -> list[str]:
    """Wrap `command` so it runs as root under `method`."""
    argv = [str(part) for part in command]
    if method == NONE:
        return argv
    if method == PKEXEC:
        return ["pkexec", *argv]
    if method == SUDO_NOPASSWD:
        return ["sudo", "-n", *argv]
    if method == SUDO_PASSWORD:
        # -S reads the password from stdin, -p '' keeps the prompt out of the log pane.
        return ["sudo", "-S", "-p", "", *argv]
    raise ValueError(f"cannot elevate with method {method!r}")


def run_elevated(
    command: Iterable[str],
    plan_: Plan,
    password: str | None = None,
    on_line=None,
    runner: Runner = _run,
) -> Result:
    """Run `command` as root. The password, if any, goes to stdin and is never logged."""
    if not plan_.possible:
        raise ValueError(plan_.reason or "elevation is unavailable")
    argv = build(command, plan_.method)
    stdin_text = None
    if plan_.method == SUDO_PASSWORD:
        if password is None:
            raise ValueError("this elevation method needs a password")
        stdin_text = password + "\n"
    return runner(argv, on_line, stdin_text=stdin_text)
