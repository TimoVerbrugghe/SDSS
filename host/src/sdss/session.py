"""Session lifecycle: patch configs, start sway + Sunshine, run the emulator, restore."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import compositor, launch, paths, patch, runtime, stream
from .compositor import CompositorSpec, OutputMode, environment, render_config
from .profiles import Profile

log = logging.getLogger("sdss.session")

JOURNAL_NAME = "session"
ENV_DUMP_TIMEOUT = 15.0


class SessionError(Exception):
    pass


@dataclass
class Session:
    profile: Profile
    command: list[str]
    tv: OutputMode | None = None
    dry_run: bool = False
    _processes: list[subprocess.Popen] = field(default_factory=list, repr=False)
    _pin_keepalive_fd: int | None = field(default=None, repr=False)

    @property
    def runtime(self) -> Path:
        return paths.runtime_dir() / "session"

    @property
    def journal(self) -> patch.Journal:
        return patch.Journal(paths.backup_dir(), JOURNAL_NAME)

    @staticmethod
    def _session_runtime_dir() -> str:
        return os.environ.get("XDG_RUNTIME_DIR", str(paths.runtime_dir()))

    # --- preparation -----------------------------------------------------------------

    def write_artifacts(self) -> dict[str, Path]:
        """Generate the sway config and env-dump helper. Safe to call in dry-run mode."""
        # Named runtime_dir (not "runtime") so it doesn't shadow the `runtime` module
        # imported above — shadowing it here previously crashed every real launch with
        # "'PosixPath' object has no attribute 'outer_gamescope_resolution'" the moment
        # tv was undetermined, since the call below silently resolved to this local Path.
        runtime_dir = paths.ensure(self.runtime)
        env_file = runtime_dir / "sway-env"
        dump = runtime_dir / "dump-env.sh"

        backend, parent = runtime.parent_display()
        main_output = (
            compositor.MAIN_OUTPUT_X11 if backend == "x11" else compositor.MAIN_OUTPUT_WAYLAND
        )
        # The resize (when needed) must complete before this script signals readiness,
        # since that's what gates _start_emulator() — otherwise the emulator can still
        # render its first frame against the pre-resize output, same bug either way.
        fit_script = compositor.x11_fit_script(parent) if backend == "x11" else ""
        dump.write_text(
            "#!/bin/sh\n"
            f"{fit_script}"
            'printf "WAYLAND_DISPLAY=%s\\nDISPLAY=%s\\nSWAYSOCK=%s\\n" '
            f'"$WAYLAND_DISPLAY" "$DISPLAY" "$SWAYSOCK" > {env_file}\n'
        )
        dump.chmod(0o755)

        tv = self.tv
        if tv is None:
            detected = runtime.outer_gamescope_resolution()
            if detected:
                tv = OutputMode(*detected)
                log.info("detected outer gamescope resolution %s", tv)
            else:
                tv = OutputMode(1920, 1080)
                log.info("no outer gamescope found, defaulting TV output to %s", tv)

        spec = CompositorSpec(
            profile=self.profile,
            env_dump=str(dump),
            tv=tv,
            second=OutputMode(*self.profile.second_size),
            main_output=main_output,
        )
        config = runtime_dir / "sway.conf"
        config.write_text(render_config(spec))

        sunshine = stream.default_spec()
        stream.write_config(sunshine)
        return {
            "sway_config": config,
            "sway_env": env_file,
            "sunshine_conf": sunshine.config_dir / "sunshine.conf",
        }

    def patch_configs(self) -> list[Path]:
        journal = self.journal
        if journal.exists:
            log.warning("stale journal found — restoring before starting")
            journal.restore_snapshots()
        changed: list[Path] = []
        for target in self.profile.configs:
            path = target.resolve()
            if patch.patch_file(path, target.format, target.resolved_edits(), journal):
                changed.append(path)
        return changed

    # --- run -------------------------------------------------------------------------

    def run(self) -> int:
        artifacts = self.write_artifacts()
        if self.dry_run:
            return 0

        try:
            # Inside the try: a PatchError from the second of three config targets used to
            # leave the first one patched with a populated journal and no restore, because
            # this ran before the `finally` was armed.
            self.patch_configs()
            sway_proc = self._start_sway(artifacts["sway_config"])
            nested = self._await_nested_display(artifacts["sway_env"])
            self._start_sunshine(nested["WAYLAND_DISPLAY"])
            emulator = self._start_emulator(nested)
            code = emulator.wait()
            log.info("emulator exited with %s — tearing down the session", code)
            if sway_proc.poll() is None:
                sway_proc.terminate()
            return code
        finally:
            self.cleanup()

    def _start_emulator(self, nested: dict[str, str]) -> subprocess.Popen:
        wayland_display = nested["WAYLAND_DISPLAY"]
        x11_display = nested.get("DISPLAY") or None
        if self.profile.needs_x11 and not x11_display:
            raise SessionError(
                f"{self.profile.name} needs Xwayland but the compositor reported no DISPLAY"
            )
        runtime_dir = self._session_runtime_dir()
        command = launch.build_command(self.command, wayland_display)
        env = launch.build_env(
            dict(os.environ),
            wayland_display,
            runtime_dir,
            x11_display=x11_display,
            prefer_x11=self.profile.needs_x11,
        )
        log.info(
            "launching emulator on %s", x11_display if self.profile.needs_x11 else wayland_display
        )
        try:
            proc = subprocess.Popen(command, env=env)
        except OSError as exc:
            raise SessionError(f"could not launch emulator {command[0]!r}: {exc}") from exc
        self._processes.append(proc)
        return proc

    def _start_sway(self, config: Path) -> subprocess.Popen:
        backend, parent = runtime.parent_display()
        env = {**os.environ, **environment(backend, parent), **self.profile.extra_env}
        try:
            command = runtime.compositor_command(config, paths.runtime_dir().parent)
        except RuntimeError as exc:
            raise SessionError(str(exc)) from exc
        log.info("starting nested compositor on parent %s display %s", backend, parent)
        try:
            proc = subprocess.Popen(command, env=env)
        except OSError as exc:
            raise SessionError(f"could not start compositor {command[0]!r}: {exc}") from exc
        self._processes.append(proc)
        return proc

    def _await_nested_display(self, env_file: Path) -> dict[str, str]:
        env_file.unlink(missing_ok=True)
        deadline = time.monotonic() + ENV_DUMP_TIMEOUT
        while time.monotonic() < deadline:
            if env_file.is_file():
                values = dict(
                    line.split("=", 1)
                    for line in env_file.read_text().splitlines()
                    if "=" in line
                )
                if values.get("WAYLAND_DISPLAY"):
                    log.info(
                        "nested compositor on %s (xwayland %s)",
                        values["WAYLAND_DISPLAY"],
                        values.get("DISPLAY") or "none",
                    )
                    return values
            time.sleep(0.2)
        raise SessionError("nested compositor did not report its Wayland socket")

    def _start_sunshine(self, nested_display: str) -> subprocess.Popen:
        spec = stream.default_spec()
        command = stream.launch_command(spec, nested_display, self._session_runtime_dir())
        log.info("starting Sunshine on port %s", spec.port)
        keepalive_fd, child_fd = self._pin_fifo()
        started = False
        try:
            proc = subprocess.Popen(command, stdin=child_fd)
            started = True
        except OSError as exc:
            raise SessionError(f"could not start Sunshine {command[0]!r}: {exc}") from exc
        finally:
            # The child owns its duplicated stdin. The parent must retain the separate
            # keepalive writer until Sunshine exits, otherwise the FIFO reports EOF.
            os.close(child_fd)
            if not started:
                os.close(keepalive_fd)
        self._pin_keepalive_fd = keepalive_fd
        self._processes.append(proc)
        return proc

    def _pin_fifo(self) -> tuple[int, int]:
        """FIFO Sunshine reads pairing PINs from, so pairing needs no web UI.

        Returns (keepalive_fd, child_fd). keepalive_fd is opened O_NONBLOCK so
        opening it cannot block waiting for a reader/writer and it gives the
        FIFO a permanent writer so it never reports EOF; it is never handed to
        a child. child_fd is a separate, blocking read-only descriptor meant
        for the child's stdin -- O_NONBLOCK is a file-status flag shared by
        dup()'d descriptors, so reusing keepalive_fd as the child's stdin would
        make Sunshine's reads non-blocking too (EAGAIN on an empty FIFO
        instead of blocking for a PIN).
        """
        path = paths.ensure(self.runtime) / "pin"
        if path.exists():
            path.unlink()
        os.mkfifo(path, 0o600)
        keepalive_fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        child_fd = os.open(path, os.O_RDONLY)
        return keepalive_fd, child_fd

    def cleanup(self) -> None:
        for proc in reversed(self._processes):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self._processes.clear()
        if self._pin_keepalive_fd is not None:
            os.close(self._pin_keepalive_fd)
            self._pin_keepalive_fd = None
        pin_path = self.runtime / "pin"
        pin_path.unlink(missing_ok=True)
        journal = self.journal
        if journal.exists:
            try:
                restored = journal.restore_snapshots()
            except patch.PatchError as exc:
                # cleanup() runs from a `finally`; raising here would replace whatever
                # actually ended the session. The backups are still on disk either way.
                log.error("could not restore config files: %s", exc)
            else:
                log.info("restored %d config file(s)", len(restored))
