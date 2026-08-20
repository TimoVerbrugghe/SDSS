"""How to start the nested compositor on a host that has no sway installed.

SteamOS ships no sway and the rootfs is read-only, so the compositor normally runs from a
container image. A native `sway` on PATH is preferred when present (development machines).
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

IMAGE = os.environ.get("SDSS_COMPOSITOR_IMAGE", "localhost/sdss-compositor:latest")

# Fixed name so teardown can reach the container even though `podman run`'s own child
# process is not what keeps it alive (conmon, fuse-overlayfs and the nested Xwayland are
# siblings that outlive it — see Session.cleanup).
CONTAINER_NAME = "sdss-compositor"
PR_SET_PDEATHSIG = 1
INPUTD_NAME = "sdss_inputd.py"
_parent_watch_stop: threading.Event | None = None
log = logging.getLogger("sdss.runtime")


def arm_parent_death_signal() -> None:
    """Make the session receive SIGTERM when its Steam launcher disappears.

    Steam stops the launcher process, not necessarily every descendant it spawned. Linux
    otherwise reparents SDSS to systemd, leaving the emulator and compositor alive.
    """
    if not sys.platform.startswith("linux"):
        return

    original_parent = os.getppid()
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))

    # The parent can exit between getppid() and prctl(). Deliver the same signal ourselves
    # rather than allowing a race to leave an orphaned session.
    if os.getppid() != original_parent:
        os.kill(os.getpid(), signal.SIGTERM)
    _arm_parent_lineage_watch(original_parent)


def _arm_parent_lineage_watch(parent_pid: int) -> None:
    """Notice either Steam or its reaper disappearing while wrappers survive."""
    global _parent_watch_stop
    watched_pids = _watched_parent_pids(parent_pid)
    if not watched_pids:
        return
    stop_event = threading.Event()
    _parent_watch_stop = stop_event

    def watch() -> None:
        # Bound to the local `stop_event`, not the module-global `_parent_watch_stop`: the
        # global is reassigned to a new Event (or None) by disarm_parent_death_watch() from
        # the main thread, and re-reading it here raced with that reassignment. Verified on
        # hardware: this thread crashed with "AttributeError: 'NoneType' object has no
        # attribute 'is_set'" when disarm ran between an iteration's wait() and its next
        # while-check. Harmless to the running session (the thread was already being told to
        # stop), but a real bug — an unhandled exception in a background thread — worth
        # closing regardless of whether it caused any particular observed crash.
        while not stop_event.is_set():
            for pid in watched_pids:
                if not Path(f"/proc/{pid}").exists():
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
            stop_event.wait(0.5)

    threading.Thread(target=watch, name="sdss-parent-watch", daemon=True).start()


def _watched_parent_pids(parent_pid: int) -> tuple[int, ...]:
    """Return the reaper and, when present, Steam itself from the launch ancestry."""
    ancestors = _ancestor_pids(parent_pid)
    if not ancestors:
        return ()
    watched = [ancestors[0]]
    steam = next((pid for pid in ancestors if _process_name(pid) == "steam"), None)
    if steam is not None and steam not in watched:
        watched.append(steam)
    return tuple(watched)


def _ancestor_pids(pid: int) -> tuple[int, ...]:
    """Return a process's live parent chain, nearest first."""
    ancestors: list[int] = []
    seen = {pid}
    current = pid
    while True:
        parent = _parent_pid(current)
        if parent is None or parent <= 1 or parent in seen:
            break
        ancestors.append(parent)
        seen.add(parent)
        current = parent
    return tuple(ancestors)


def _parent_pid(pid: int) -> int | None:
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return None
    for line in status.splitlines():
        if line.startswith("PPid:\t"):
            try:
                return int(line.split("\t", 1)[1])
            except ValueError:
                return None
    return None


def _process_name(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return None


def disarm_parent_death_watch() -> None:
    global _parent_watch_stop
    if _parent_watch_stop is not None:
        _parent_watch_stop.set()
        _parent_watch_stop = None


def parent_display() -> tuple[str, str]:
    """Which surface the nested compositor should connect to, and how.

    Gamescope gives every launched Steam game its own isolated per-game Xwayland
    instance (`STEAM_MULTIPLE_XWAYLANDS=1`, `--xwayland-count 2`) and hands the game
    process `DISPLAY=:N` pointing at it — but no `WAYLAND_DISPLAY` at all. That per-game
    Xwayland is exactly what gamescope's steamcompmgr watches to decide the app is
    "ready" and dismiss its loading spinner; a window on the shared desktop
    `gamescope-0` Wayland socket renders fine but is invisible to that check and the
    spinner never clears. So `DISPLAY` must be preferred whenever Steam provides one;
    only fall back to Wayland for native/off-Steam testing where no X11 display exists.
    """
    display = os.environ.get("DISPLAY")
    if display:
        return "x11", display
    wayland = os.environ.get("WAYLAND_DISPLAY") or os.environ.get(
        "GAMESCOPE_WAYLAND_DISPLAY", "gamescope-0"
    )
    return "wayland", wayland


def native_sway() -> str | None:
    return shutil.which("sway")


def podman_available() -> bool:
    return shutil.which("podman") is not None


def image_present() -> bool:
    if not podman_available():
        return False
    result = subprocess.run(
        ["podman", "image", "exists", IMAGE], capture_output=True, check=False
    )
    return result.returncode == 0


def remove_container(name: str = CONTAINER_NAME) -> bool:
    """Force-kill and remove the compositor container.

    Terminating the `podman run` process is not enough: conmon supervises the container in
    its own process and, together with fuse-overlayfs and the nested Xwayland, keeps running
    inside the Steam per-game cgroup scope SDSS deliberately nests under. Steam treats a
    non-empty scope as the game still running, so the next launch is refused with "Game
    already running" until those are reaped. Verified on hardware.

    This used to try `--signal TERM` first, bounded by a short `podman wait`, on the theory
    that a plain SIGKILL never gives sway a chance to close its X11 connection to gamescope's
    per-game Xwayland cleanly, and that the abrupt severing was a plausible contributor to the
    Steam-side corruption documented in docs/architecture.md. That theory was never proven,
    and hardware testing found something worse than an unproven theory: sending the container
    `--signal TERM` occasionally (not reliably) makes sway itself crash with SIGBUS within a
    few hundred milliseconds, taking Xwayland and sdss_inputd down with it (a triple coredump,
    reproduced twice — once with the emulator's own SIGTERM handling also implicated, once
    with the emulator already reaped by SIGKILL beforehand, ruling that out as the cause). A
    crash is not a clean protocol disconnect either — the kernel closes the socket the same
    abrupt way SIGKILL would have — so the graceful attempt was buying, at best, an
    occasional clean disconnect at the cost of an occasional hard crash, not a reliable
    improvement over SIGKILL. Going straight to SIGKILL removes that crash mode entirely.

    Going straight to SIGKILL was *also* not the whole story: the exact same triple coredump
    reproduced again with this function unchanged from the above fix. The remaining cause was
    ordering, not signal choice — any Podman teardown command can race, because `podman kill`
    hits the container entrypoint through conmon and `podman rm --force` (or Podman's own
    `--rm` auto-cleanup once the killed entrypoint exits) tears down the fuse-overlayfs rootfs
    that sway, Xwayland and sdss_inputd all still demand-page their own code and shared
    libraries from. `reap_orphaned_helpers()` is what actually kills those clients directly,
    since `--pid=host` keeps Xwayland/sdss_inputd out of Podman's own view of "the
    container", so it must run before *any* Podman kill/remove path can make conmon or
    fuse-overlayfs disappear underneath them.
    """
    if not podman_available():
        return False
    # Must run before `podman kill` and `rm --force`: it kills sway/Xwayland/sdss_inputd and
    # confirms they are actually gone before Podman can remove the rootfs their code is still
    # demand-paged from.
    reap_orphaned_helpers(name)
    subprocess.run(
        ["podman", "kill", "--signal", "KILL", name],
        capture_output=True,
        check=False,
    )
    result = subprocess.run(
        ["podman", "rm", "--force", "--ignore", name],
        capture_output=True,
        check=False,
    )
    _repair_invalid_podman_pause()
    return result.returncode == 0


def reap_orphaned_helpers(name: str = CONTAINER_NAME) -> None:
    """Kill SDSS helper processes left behind after Podman loses its container record.

    Rootless Podman can reparent overlay mounts and nested Xwayland to the user manager when
    conmon or the session dies first. It can also create a persistent ``catatonit -P`` pause
    process and reparent it to Steam's reaper, making "Exit Game" wait forever. Pause cleanup
    requires both Podman's own pause PID file and a parent in this launch's ancestry, so
    unrelated rootless Podman sessions are not touched even when systemd chooses a different
    cgroup layout for the helper.

    sway, Xwayland and sdss_inputd keep demand-paging their own code and shared libraries
    (e.g. Mesa's libgallium) from the container's fuse-overlayfs-backed rootfs for as long as
    they run. conmon and fuse-overlayfs are that rootfs's supervisor and storage backend.
    Killing the two groups in a single unordered pass -- exactly what this function used to
    do, in whatever order ``os.listdir("/proc")`` happened to return -- let fuse-overlayfs or
    conmon die while sway/Xwayland/sdss_inputd were still executing. The next time one of them
    faulted in a not-yet-resident code page, the kernel found the backing rootfs gone and
    raised SIGBUS instead: reproduced on hardware as a triple coredump (sway itself, inside
    libgallium, plus Xwayland and sdss_inputd), and consistent with the failure being
    probabilistic rather than reliable -- it depended on enumeration order and scheduling
    timing. Killing the rootfs-dependent processes first and confirming they are actually
    gone -- not just signaled -- before touching conmon/fuse-overlayfs/the pause process
    removes the race instead of narrowing it.
    """
    reap_orphaned_appimage_mounts()
    launch_ancestors = set(_ancestor_pids(os.getpid()))
    launch_cgroup = own_cgroup()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return
    rootfs_clients: list[int] = []
    supervisors: list[int] = []
    for entry in entries:
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        pid = int(entry)
        try:
            command = Path(f"/proc/{entry}/cmdline").read_bytes().replace(b"\0", b" ")
        except OSError:
            continue
        is_named_container = f"-n {name}".encode() in command
        is_input_bridge = INPUTD_NAME.encode() in command
        is_launch_scoped = _belongs_to_cgroup(pid, launch_cgroup)
        is_nested_sway = is_launch_scoped and (
            b"/sway " in command and b"/sdss/session/sway.conf" in command
        )
        is_rootless_xwayland = is_launch_scoped and (
            b"Xwayland" in command and b"-rootless" in command and b"-wm" in command
        )
        is_sdss_overlay = is_launch_scoped and (
            b"fuse-overlayfs" in command
            and b"/.local/share/containers/storage/overlay/" in command
        )
        is_launch_pause = _is_launch_owned_podman_pause(pid, command, launch_ancestors)

        if is_nested_sway or is_input_bridge or is_rootless_xwayland:
            rootfs_clients.append(pid)
        elif is_named_container or is_sdss_overlay or is_launch_pause:
            supervisors.append(pid)

    for pid in rootfs_clients:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            continue
    _await_process_exit(rootfs_clients, timeout=2.0)

    for pid in supervisors:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            continue


def _await_process_exit(pids: list[int], timeout: float) -> None:
    """Poll until none of ``pids`` are still alive, or ``timeout`` elapses.

    A SIGKILL does not mean the target has already stopped running -- the kernel needs a
    scheduling opportunity to deliver it. See reap_orphaned_helpers for why the caller must
    not proceed to remove the container's storage until these PIDs are actually confirmed
    gone, not merely signaled.
    """
    deadline = time.monotonic() + timeout
    remaining = set(pids)
    while remaining and time.monotonic() < deadline:
        remaining = {pid for pid in remaining if Path(f"/proc/{pid}").exists()}
        if remaining:
            time.sleep(0.02)


def _belongs_to_cgroup(pid: int, parent: str | None) -> bool:
    if not parent:
        return False
    try:
        contents = Path(f"/proc/{pid}/cgroup").read_text()
    except OSError:
        return False
    for line in contents.splitlines():
        if not line.startswith("0::"):
            continue
        current = line[3:].lstrip("/")
        return current == parent or current.startswith(f"{parent}/")
    return False


def _is_launch_owned_podman_pause(
    pid: int, command: bytes, launch_ancestors: set[int]
) -> bool:
    args = command.split()
    if len(args) != 2 or Path(os.fsdecode(args[0])).name != "catatonit" or args[1] != b"-P":
        return False
    parent = _parent_pid(pid)
    if parent not in launch_ancestors:
        return False
    return _podman_pause_pid() == pid


def _podman_pause_pid() -> int | None:
    runtime_dir = Path(
        os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    )
    try:
        return int((runtime_dir / "libpod/tmp/pause.pid").read_text().strip())
    except (OSError, ValueError):
        return None


def _repair_invalid_podman_pause() -> None:
    result = subprocess.run(
        ["podman", "ps", "-a"], capture_output=True, text=True, check=False
    )
    if result.returncode == 0:
        return
    message = f"{result.stdout}\n{result.stderr}"
    if "invalid internal status" not in message or "podman system migrate" not in message:
        return
    repaired = subprocess.run(
        ["podman", "system", "migrate"], capture_output=True, text=True, check=False
    )
    if repaired.returncode != 0:
        log.warning(
            "could not repair rootless Podman pause status: %s", repaired.stderr.strip()
        )


def reap_orphaned_appimage_mounts() -> None:
    """Unmount detached FUSE mounts left by an interrupted SDSS AppImage launch.

    AppImage mounts are not processes, so killing the emulator and compositor cannot remove
    them. Restrict this to mounts under AppImage's temporary directory whose source is one of
    SDSS's shadowed launcher files.
    """
    try:
        mountinfo = Path("/proc/self/mountinfo").read_text()
    except OSError:
        return
    fusermount = shutil.which("fusermount3") or shutil.which("fusermount")
    if not fusermount:
        return
    for line in mountinfo.splitlines():
        fields = line.split(" - ", 1)
        if len(fields) != 2:
            continue
        mount_fields, filesystem_fields = fields
        mount_parts = mount_fields.split()
        filesystem_parts = filesystem_fields.split()
        if len(mount_parts) < 5 or len(filesystem_parts) < 2:
            continue
        mountpoint = re.sub(
            r"\\([0-7]{3})",
            lambda match: chr(int(match.group(1), 8)),
            mount_parts[4],
        )
        source = re.sub(
            r"\\([0-7]{3})",
            lambda match: chr(int(match.group(1), 8)),
            filesystem_parts[1],
        )
        if not mountpoint.startswith("/tmp/.mount_") or not source.endswith(
            ".AppImage.sdss-real"
        ):
            continue
        result = subprocess.run(
            [fusermount, "-u", "-z", mountpoint],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            log.warning("could not unmount stale AppImage mount %s", mountpoint)


HOST_X11_DIR = Path("/tmp/.X11-unix")
# Where the host's X sockets are re-exposed inside the container, so the bridge dir mounted
# over /tmp/.X11-unix can symlink back out to them.
CONTAINER_HOST_X11_DIR = Path("/run/host-x11")


def x11_bridge_dir(runtime_dir: Path) -> Path:
    return runtime_dir / "sdss" / "x11"


def prepare_x11_bridge(runtime_dir: Path) -> Path:
    """Build a user-owned /tmp/.X11-unix replacement and return it.

    Mounting the host's /tmp/.X11-unix straight through fails: it is root-owned (0:0), and
    under --userns=keep-id host uid 0 maps to nobody, so wlroots refuses it outright --
    "/tmp/.X11-unix not owned by root or us" -- and then "No display available in the first
    33". Xwayland never starts, sway reports an empty DISPLAY, and every needs_x11 profile
    (Cemu, melonDS) dies with a black second screen.

    A directory we own satisfies that check. The host's existing sockets are symlinked back
    in via a second, separate mount of the real directory, so the nested compositor can still
    reach the host's per-game Xwayland (WLR_BACKENDS=x11) while its own Xwayland creates a
    new socket here. Host-side clients reach that new display over the abstract socket
    namespace, which --network=host already shares (verified: xdpyinfo on :2 from the host).
    """
    bridge = x11_bridge_dir(runtime_dir)
    bridge.mkdir(parents=True, exist_ok=True)
    for existing in bridge.iterdir():
        if existing.is_symlink():
            existing.unlink()
    try:
        sockets = sorted(HOST_X11_DIR.iterdir())
    except OSError:
        return bridge
    for socket_path in sockets:
        if not socket_path.name.startswith("X"):
            continue
        (bridge / socket_path.name).symlink_to(CONTAINER_HOST_X11_DIR / socket_path.name)
    return bridge


def outer_gamescope_resolution() -> tuple[int, int] | None:
    """The `-w`/`-h` the *outer* gamescope session was launched with.

    `WL-1` in the nested sway is not a real display — it is a Wayland window inside the
    outer gamescope, which only ever accepts a mode that exactly matches its own -w/-h.
    SteamOS images differ (Deck panels are 1280x800, but a Steam Machine or an external-TV
    Deck session can be anything), so this must be read live rather than assumed. Requesting
    any other size makes sway mark the output `power:false` after a failed modeset, sway
    never commits a frame for it, and gamescope's spinner never dismisses even though the
    rest of the session is running fine.
    """
    try:
        entries = os.listdir("/proc")
    except OSError:
        return None
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            cmdline = Path(f"/proc/{entry}/cmdline").read_bytes()
        except OSError:
            continue
        args = [a.decode(errors="replace") for a in cmdline.split(b"\0") if a]
        if not args or Path(args[0]).name != "gamescope":
            continue
        width = height = None
        for i, arg in enumerate(args):
            if arg == "-w" and i + 1 < len(args):
                width = args[i + 1]
            elif arg == "-h" and i + 1 < len(args):
                height = args[i + 1]
        if width and height:
            try:
                return int(width), int(height)
            except ValueError:
                return None
    return None


def own_cgroup() -> str | None:
    """The cgroup this process (and therefore the whole `sdss run` chain) lives in.

    When Steam launches a game it puts the whole process tree in a per-app cgroup
    scope (`app-steam-app<id>-<pid>.scope`). Gamescope decides a launched game is
    "ready" — and dismisses its loading spinner — by walking that scope's process
    tree. A rootless podman container defaults to its own `libpod-*.scope`, which
    is a sibling, not a descendant, of Steam's scope, so gamescope never finds
    sway/the emulator inside it. Passing this back as `--cgroup-parent` nests the
    container's cgroup under Steam's scope instead, so gamescope's walk finds it.
    """
    try:
        contents = Path("/proc/self/cgroup").read_text()
    except OSError:
        return None
    for line in contents.splitlines():
        # cgroup v2 unified hierarchy: "0::/user.slice/.../app-steam-app123-456.scope"
        if line.startswith("0::"):
            path = line[3:]
            return path.lstrip("/") or None
    return None


def compositor_command(config: Path, runtime_dir: Path, home: Path | None = None) -> list[str]:
    sway = native_sway()
    if sway:
        return [sway, "-c", str(config)]
    if not podman_available():
        raise RuntimeError("no sway on PATH and podman is unavailable")

    home = home or Path.home()
    parent = own_cgroup()
    return [
        "podman",
        # podman's default systemd cgroup manager refuses a --cgroup-parent that is a
        # *.scope (it only accepts systemd slices); cgroupfs accepts any cgroupfs path,
        # including the app-steam-app<id>-<pid>.scope Steam created, so the container
        # actually nests under it instead of erroring out with exit 125.
        *(["--cgroup-manager=cgroupfs"] if parent else []),
        "run",
        "--rm",
        f"--name={CONTAINER_NAME}",
        # A crashed session can leave the old container behind and podman refuses to reuse
        # a name; without this every subsequent launch fails with exit 125.
        "--replace",
        "--userns=keep-id",
        # host networking shares the abstract socket namespace, which is how host X11
        # clients reach the nested Xwayland.
        "--network=host",
        # Gamescope decides a launched Steam game is "ready" (and dismisses its loading
        # spinner) by walking the process tree/cgroup it launched. A default podman run
        # puts sway in its own pid/cgroup namespace, orphaned from that tree, so gamescope
        # never sees azahar's window as belonging to the app it started and hangs on the
        # spinner forever even though the compositor and emulator are running fine.
        "--pid=host",
        "--cgroupns=host",
        *([f"--cgroup-parent=/{parent}"] if parent else []),
        f"--volume={runtime_dir}:{runtime_dir}",
        f"--volume={home}:{home}",
        # /tmp/.X11-unix cannot be mounted straight through: it is root-owned, which maps to
        # nobody under --userns=keep-id, and wlroots then refuses to create its Xwayland
        # socket there ("not owned by root or us"), leaving DISPLAY empty and every
        # needs_x11 profile with a black screen. Mount a directory we own in its place, and
        # expose the real one separately so the symlinks inside it still resolve outward.
        f"--volume={x11_bridge_dir(runtime_dir)}:/tmp/.X11-unix",
        f"--volume={HOST_X11_DIR}:{CONTAINER_HOST_X11_DIR}",
        # The touch bridge reads and grabs Sunshine's virtual input devices.
        "--volume=/dev/input:/dev/input",
        "--device=/dev/input",
        "--device=/dev/dri",
        f"--env=XDG_RUNTIME_DIR={runtime_dir}",
        f"--env=HOME={home}",
        # Passed through unset (no "=value") when unused by the chosen backend — podman
        # simply omits an --env flag whose name isn't set in this process's own env.
        "--env=DISPLAY",
        "--env=WAYLAND_DISPLAY",
        "--env=WLR_BACKENDS",
        "--env=WLR_X11_OUTPUTS",
        "--env=WLR_WL_OUTPUTS",
        "--env=WLR_HEADLESS_OUTPUTS",
        "--env=WLR_NO_HARDWARE_CURSORS",
        IMAGE,
        "-c",
        str(config),
    ]


def describe() -> str:
    sway = native_sway()
    if sway:
        return f"native sway ({sway})"
    if image_present():
        return f"container image {IMAGE}"
    if podman_available():
        return f"container image {IMAGE} (NOT BUILT — run runtime/build.sh)"
    return "unavailable (no sway, no podman)"
