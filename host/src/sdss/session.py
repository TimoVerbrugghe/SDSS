"""Session lifecycle: patch configs, start sway + Sunshine, run the emulator, restore."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import launch, paths, patch, runtime, stream
from .compositor import CompositorSpec, OutputMode, environment, render_config
from .profiles import Profile

log = logging.getLogger("sdss.session")

JOURNAL_NAME = "session"
ENV_DUMP_TIMEOUT = 15.0
DECK_RESOLUTION = (1280, 800)


class SessionError(Exception):
    pass


@dataclass
class Session:
    profile: Profile
    command: list[str]
    tv: OutputMode = OutputMode(1920, 1080)
    dry_run: bool = False
    _processes: list[subprocess.Popen] = field(default_factory=list, repr=False)

    @property
    def runtime(self) -> Path:
        return paths.runtime_dir() / "session"

    @property
    def journal(self) -> patch.Journal:
        return patch.Journal(paths.backup_dir(), JOURNAL_NAME)

    # --- preparation -----------------------------------------------------------------

    def write_artifacts(self) -> dict[str, Path]:
        """Generate the sway config and env-dump helper. Safe to call in dry-run mode."""
        runtime = paths.ensure(self.runtime)
        env_file = runtime / "sway-env"
        dump = runtime / "dump-env.sh"
        dump.write_text(
            "#!/bin/sh\n"
            f'printf "WAYLAND_DISPLAY=%s\\nSWAYSOCK=%s\\n" "$WAYLAND_DISPLAY" "$SWAYSOCK" > {env_file}\n'
        )
        dump.chmod(0o755)

        spec = CompositorSpec(
            profile=self.profile,
            env_dump=str(dump),
            tv=self.tv,
            second=OutputMode(*DECK_RESOLUTION),
        )
        config = runtime / "sway.conf"
        config.write_text(render_config(spec))

        sunshine = stream.SunshineSpec(config_dir=paths.config_dir() / "sunshine")
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
            journal.restore()
        changed: list[Path] = []
        for target in self.profile.configs:
            path = target.resolve()
            if patch.patch_file(path, target.format, target.edits, journal):
                changed.append(path)
        return changed

    # --- run -------------------------------------------------------------------------

    def run(self) -> int:
        artifacts = self.write_artifacts()
        if self.dry_run:
            return 0

        self.patch_configs()
        try:
            compositor = self._start_sway(artifacts["sway_config"])
            nested_display = self._await_nested_display(artifacts["sway_env"])
            self._start_sunshine(nested_display)
            emulator = self._start_emulator(nested_display)
            code = emulator.wait()
            log.info("emulator exited with %s — tearing down the session", code)
            if compositor.poll() is None:
                compositor.terminate()
            return code
        finally:
            self.cleanup()

    def _start_emulator(self, nested_display: str) -> subprocess.Popen:
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", str(paths.runtime_dir()))
        command = launch.build_command(self.command, nested_display)
        env = launch.build_env(dict(os.environ), nested_display, runtime_dir)
        log.info("launching emulator on %s", nested_display)
        proc = subprocess.Popen(command, env=env)
        self._processes.append(proc)
        return proc

    def _start_sway(self, config: Path) -> subprocess.Popen:
        parent = os.environ.get("WAYLAND_DISPLAY") or os.environ.get(
            "GAMESCOPE_WAYLAND_DISPLAY", "gamescope-0"
        )
        env = {**os.environ, **environment(parent), **self.profile.extra_env}
        try:
            command = runtime.compositor_command(config, paths.runtime_dir().parent)
        except RuntimeError as exc:
            raise SessionError(str(exc)) from exc
        log.info("starting nested compositor on parent display %s", parent)
        proc = subprocess.Popen(command, env=env)
        self._processes.append(proc)
        return proc

    def _await_nested_display(self, env_file: Path) -> str:
        env_file.unlink(missing_ok=True)
        deadline = time.monotonic() + ENV_DUMP_TIMEOUT
        while time.monotonic() < deadline:
            if env_file.is_file():
                values = dict(
                    line.split("=", 1)
                    for line in env_file.read_text().splitlines()
                    if "=" in line
                )
                display = values.get("WAYLAND_DISPLAY")
                if display:
                    log.info("nested compositor is on %s", display)
                    return display
            time.sleep(0.2)
        raise SessionError("nested compositor did not report its Wayland socket")

    def _start_sunshine(self, nested_display: str) -> subprocess.Popen:
        spec = stream.SunshineSpec(config_dir=paths.config_dir() / "sunshine")
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", str(paths.runtime_dir()))
        command = stream.launch_command(spec, nested_display, runtime_dir)
        log.info("starting Sunshine on port %s", spec.port)
        proc = subprocess.Popen(command)
        self._processes.append(proc)
        return proc

    def cleanup(self) -> None:
        for proc in reversed(self._processes):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self._processes.clear()
        journal = self.journal
        if journal.exists:
            restored = journal.restore()
            log.info("restored %d config file(s)", len(restored))
