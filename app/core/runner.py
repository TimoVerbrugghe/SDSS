"""Run the SDSS shell entry points and stream their output.

The app never reimplements install logic — it runs `install.sh`, `uninstall.sh` and `sdss`
exactly as a terminal would — so this module is the only place a subprocess is started.
"""

from __future__ import annotations

import datetime
import os
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

OnLine = Callable[[str], None]


@dataclass
class Result:
    command: list[str]
    returncode: int
    lines: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        return "\n".join(self.lines)


def _log(handle, text: str) -> None:
    if handle is None:
        return
    try:
        handle.write(text + "\n")
        handle.flush()
    except OSError:
        pass


def _open_log():
    try:
        paths.ensure(paths.log_file().parent)
        return paths.log_file().open("a", encoding="utf-8")
    except OSError:
        # A log we cannot write must never stop an install; the UI shows the same lines.
        return None


def run(
    command: Iterable[str],
    on_line: OnLine | None = None,
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    stdin_text: str | None = None,
) -> Result:
    """Run `command`, streaming merged stdout/stderr to `on_line`, and return the result.

    `stdin_text` exists for `sudo -S`, and is deliberately never logged or echoed. When it
    is None the child gets `/dev/null` on stdin rather than an inherited terminal: every
    SDSS script guards its prompts with `read -r -p ... || answer=""`, so EOF makes them
    take the default instead of hanging on a prompt no one can see.
    """
    argv = [str(part) for part in command]
    lines: list[str] = []
    handle = _open_log()
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    _log(handle, f"\n=== {stamp} $ {' '.join(argv)}")

    merged = dict(os.environ)
    if env:
        merged.update(env)

    try:
        process = subprocess.Popen(  # noqa: S603 - argv list, never a shell string
            argv,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=merged,
            cwd=str(cwd) if cwd else None,
        )
    except OSError as exc:
        message = f"could not run {argv[0]}: {exc}"
        _log(handle, message)
        if handle is not None:
            handle.close()
        if on_line:
            on_line(message)
        return Result(command=argv, returncode=127, lines=[message])

    if stdin_text is not None and process.stdin is not None:
        try:
            process.stdin.write(stdin_text)
            process.stdin.close()
        except OSError:
            # The child exited before reading it (a wrong password on a previous prompt,
            # for example). The return code below is what the caller reacts to.
            pass

    assert process.stdout is not None
    for raw in process.stdout:
        line = raw.rstrip("\n")
        lines.append(line)
        _log(handle, line)
        if on_line:
            on_line(line)
    process.stdout.close()
    returncode = process.wait()
    _log(handle, f"=== exit {returncode}")
    if handle is not None:
        handle.close()
    return Result(command=argv, returncode=returncode, lines=lines)
