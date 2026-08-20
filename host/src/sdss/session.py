"""Session lifecycle: start sway + Sunshine, run the emulator, and tear processes down."""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import compositor, launch, paths, runtime, stream
from .compositor import DECK_PANEL_RESOLUTION, CompositorSpec, OutputMode, environment, render_config
from .profiles import Profile

log = logging.getLogger("sdss.session")

ENV_DUMP_TIMEOUT = 15.0


class SessionError(Exception):
    pass


class SessionInterrupted(SessionError):
    pass


@dataclass
class Session:
    profile: Profile
    command: list[str]
    tv: OutputMode | None = None
    dry_run: bool = False
    _processes: list[subprocess.Popen] = field(default_factory=list, repr=False)
    _emulator_proc: subprocess.Popen | None = field(default=None, repr=False)
    _compositor_proc: subprocess.Popen | None = field(default=None, repr=False)
    _pin_keepalive_fd: int | None = field(default=None, repr=False)
    _signal_handlers: dict[int, object] = field(default_factory=dict, repr=False)
    _stopping: bool = field(default=False, repr=False)

    @property
    def runtime(self) -> Path:
        return paths.runtime_dir() / "session"

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
            second=OutputMode(*DECK_PANEL_RESOLUTION),
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

    # --- run -------------------------------------------------------------------------

    def run(self) -> int:
        if self.dry_run:
            self.write_artifacts()
            return 0

        with self._session_lock():
            self._install_signal_handlers()
            try:
                runtime.arm_parent_death_signal()
                artifacts = self.write_artifacts()
                sway_proc = self._start_sway(artifacts["sway_config"])
                nested = self._await_nested_display(artifacts["sway_env"])
                self._start_sunshine(nested["WAYLAND_DISPLAY"])
                emulator = self._start_emulator(nested)
                code = emulator.wait()
                log.info("emulator exited with %s — tearing down the session", code)
                return code
            finally:
                self._stopping = True
                runtime.disarm_parent_death_watch()
                try:
                    self.cleanup()
                finally:
                    self._restore_signal_handlers()

    @contextlib.contextmanager
    def _session_lock(self):
        lock_path = paths.session_lock_file()
        handle = paths.ensure(lock_path.parent).joinpath(lock_path.name).open("w")
        try:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SessionError("another SDSS emulator session is already running") from exc
            yield
        finally:
            handle.close()

    def _install_signal_handlers(self) -> None:
        def interrupted(signum, _frame):
            name = signal.Signals(signum).name
            if self._stopping:
                log.warning("received %s while already stopping — ignoring", name)
                return
            self._stopping = True
            log.warning("received %s — stopping the SDSS session", name)
            raise SessionInterrupted(f"session interrupted by {name}")

        for signum in (signal.SIGTERM, signal.SIGINT):
            self._signal_handlers[signum] = signal.signal(signum, interrupted)

    def _restore_signal_handlers(self) -> None:
        for signum, handler in self._signal_handlers.items():
            signal.signal(signum, handler)
        self._signal_handlers.clear()

    @staticmethod
    def _terminate_process(proc: subprocess.Popen, *, graceful: bool = True) -> None:
        """Stop a launched process and the wrappers it may have spawned.

        ``graceful=False`` skips SIGTERM and sends SIGKILL immediately. The emulator
        needs this: Azahar does not shut down reliably on SIGTERM. Usually it just
        ignores it for the full 5s timeout below (harmless — the SIGKILL escalation
        handles that), but verified on hardware that it occasionally reacts within
        milliseconds instead, and that fast reaction correlates with sway, Xwayland and
        sdss_inputd then crashing with SIGBUS moments later.

        SIGKILL to the emulator does *not* eliminate that crash on its own, though —
        verified on hardware again later (a fresh, 100%-reproducible test: launch, wait
        ~20s, exit via the Steam overlay): sway/Xwayland still crash with SIGBUS even
        though the emulator already gets SIGKILL immediately here. The remaining exposure
        is *how long Xwayland stays alive after* the emulator (its GPU-rendering client)
        disappears abruptly — see cleanup()'s ordering, which now tears the compositor
        down immediately after the emulator instead of after Sunshine's own shutdown.
        """
        first_signal = signal.SIGTERM if graceful else signal.SIGKILL
        descendants = Session._descendant_pids(proc.pid)
        for pid in descendants:
            try:
                os.kill(pid, first_signal)
            except (ProcessLookupError, PermissionError):
                continue
        try:
            os.killpg(proc.pid, first_signal)
        except ProcessLookupError:
            return
        except PermissionError as exc:
            log.warning("could not terminate process group %s: %s", proc.pid, exc)
            return
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                log.warning("could not kill process group %s: %s", proc.pid, exc)
            for pid in descendants:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    continue
            proc.wait()
        else:
            # Flatpak and container runtimes can move their init process into a
            # separate process group; do not leave it holding Steam's reaper.
            for pid in descendants:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    continue

    @staticmethod
    def _descendant_pids(root_pid: int) -> list[int]:
        """Return a snapshot of all descendants, including escaped process groups."""
        try:
            entries = os.listdir("/proc")
        except OSError:
            return []
        parents: dict[int, list[int]] = {}
        for entry in entries:
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                status = Path(f"/proc/{pid}/status").read_text()
            except OSError:
                continue
            parent = next(
                (
                    int(line.split("\t", 1)[1])
                    for line in status.splitlines()
                    if line.startswith("PPid:\t")
                ),
                None,
            )
            if parent is not None:
                parents.setdefault(parent, []).append(pid)

        descendants: list[int] = []
        pending = [root_pid]
        while pending:
            parent = pending.pop()
            children = parents.get(parent, [])
            descendants.extend(children)
            pending.extend(children)
        return descendants

    def _start_emulator(self, nested: dict[str, str]) -> subprocess.Popen:
        wayland_display = nested["WAYLAND_DISPLAY"]
        x11_display = nested.get("DISPLAY") or None
        if self.profile.needs_x11 and not x11_display:
            raise SessionError(
                f"{self.profile.name} needs Xwayland but the compositor reported no DISPLAY"
            )
        runtime_dir = self._session_runtime_dir()
        command = launch.build_command(
            self.command, wayland_display, self.profile.launch_args
        )
        env = launch.build_env(
            dict(os.environ),
            wayland_display,
            runtime_dir,
            x11_display=x11_display,
            prefer_x11=self.profile.needs_x11,
            steam_overlay=self.profile.steam_overlay,
        )
        log.info(
            "launching emulator on %s", x11_display if self.profile.needs_x11 else wayland_display
        )
        try:
            proc = subprocess.Popen(command, env=env, start_new_session=True)
        except OSError as exc:
            raise SessionError(f"could not launch emulator {command[0]!r}: {exc}") from exc
        self._processes.append(proc)
        self._emulator_proc = proc
        return proc

    def _start_sway(self, config: Path) -> subprocess.Popen:
        backend, parent = runtime.parent_display()
        env = launch.helper_env(
            {**os.environ, **environment(backend, parent), **self.profile.extra_env}
        )
        try:
            command = runtime.compositor_command(config, paths.runtime_dir().parent)
        except RuntimeError as exc:
            raise SessionError(str(exc)) from exc
        log.info("starting nested compositor on parent %s display %s", backend, parent)
        # Must exist and be owned by us before the container mounts it over /tmp/.X11-unix,
        # or wlroots refuses to start Xwayland and needs_x11 profiles get a black screen.
        runtime.prepare_x11_bridge(paths.runtime_dir().parent)
        # `podman run --replace` refuses to take over a container whose conmon died
        # without cleaning up (a hard kill, or a crashed previous run): it reports
        # "conmon exited prematurely" and the new container dies immediately. A forced
        # remove first is the only thing that reliably clears that state.
        runtime.remove_container()
        try:
            proc = subprocess.Popen(command, env=env, start_new_session=True)
        except OSError as exc:
            raise SessionError(f"could not start compositor {command[0]!r}: {exc}") from exc
        self._processes.append(proc)
        self._compositor_proc = proc
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
                # `xwayland force` starts Xwayland at compositor init, so DISPLAY is
                # normally already set by the time sway runs the env dump. Keep waiting
                # rather than accepting a blank one, so a slow start surfaces as a timeout
                # instead of "needs Xwayland but the compositor reported no DISPLAY".
                if values.get("WAYLAND_DISPLAY") and not (
                    self.profile.needs_x11 and not values.get("DISPLAY")
                ):
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
            proc = subprocess.Popen(
                command,
                env=launch.helper_env(dict(os.environ)),
                stdin=child_fd,
                start_new_session=True,
            )
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
        """Tear the session down: emulator, then compositor, then everything else.

        Verified on hardware, 100%-reproducible: launch a needs_x11 profile (Cemu or
        Azahar), let it run ~20s, exit via the Steam overlay. sway and Xwayland crash
        with SIGBUS every time — signal 7, confirmed via systemd-coredump's own log line
        (though the dump itself never becomes readable through coredumpctl, so this does
        not show up as a listed coredump; Steam's own minidump generator also fails on
        it). The crash correlates with the compositor staying alive after the emulator,
        its GPU-rendering client, disappears — not with *how* the emulator is killed
        (SIGKILL here already, per _terminate_process's docstring) but with what happens
        *after*: the previous ordering ran Sunshine's own shutdown between "emulator
        dead" and "compositor dead", stretching that window every time. The compositor
        (sway/Xwayland/sdss_inputd) is now torn down immediately after the emulator,
        before Sunshine or anything else, to close that window as much as SDSS's own
        code can. Not yet re-verified crash-free on hardware after this change — treat
        the ordering as the current best hypothesis, not a confirmed fix.
        """
        compositor_proc = self._compositor_proc
        emulator_proc = self._emulator_proc
        uses_native_sway = runtime.native_sway() is not None

        if emulator_proc is not None and emulator_proc.poll() is None:
            self._terminate_process(emulator_proc, graceful=False)

        if uses_native_sway:
            if compositor_proc is not None and compositor_proc.poll() is None:
                self._terminate_process(compositor_proc, graceful=True)
            runtime.reap_orphaned_helpers()
        else:
            # Do not terminate the local `podman run` child first. With `--rm`, even that can
            # make conmon begin container teardown before sway/Xwayland/sdss_inputd are gone.
            # remove_container() owns that ordering: direct rootfs clients first, then Podman.
            try:
                runtime.remove_container()
            except OSError as exc:
                # cleanup() runs from a `finally`; never let teardown replace the real error.
                log.warning("could not remove compositor container: %s", exc)
            if compositor_proc is not None and compositor_proc.poll() is None:
                self._terminate_process(compositor_proc, graceful=False)

        # Everything else (currently just Sunshine) gets its ordinary graceful shutdown
        # only now — after the emulator/compositor pair that must die back-to-back.
        for proc in reversed(self._processes):
            if proc is emulator_proc or proc is compositor_proc:
                continue
            if proc.poll() is None:
                self._terminate_process(proc, graceful=True)

        self._processes.clear()
        self._emulator_proc = None
        self._compositor_proc = None
        if self._pin_keepalive_fd is not None:
            os.close(self._pin_keepalive_fd)
            self._pin_keepalive_fd = None
        pin_path = self.runtime / "pin"
        pin_path.unlink(missing_ok=True)
