"""How to start the nested compositor on a host that has no sway installed.

SteamOS ships no sway and the rootfs is read-only, so the compositor normally runs from a
container image. A native `sway` on PATH is preferred when present (development machines).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

IMAGE = os.environ.get("SDSS_COMPOSITOR_IMAGE", "localhost/sdss-compositor:latest")


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


X11_SOCKET_DIR = Path("/tmp/.X11-unix")


def prepare_x11_socket_dir(runtime_dir: Path, source: Path | None = None) -> Path:
    """Return a directory to bind onto /tmp/.X11-unix that Xwayland will accept.

    The host's /tmp/.X11-unix is root:root 1777. Under rootless podman with
    --userns=keep-id, only our own uid is mapped, so host root shows up inside the
    container as `nobody` (65534). wlroots refuses to start Xwayland unless the
    socket directory is owned by root *or* the current user (xwayland/sockets.c:83
    "not owned by root or us") and has the sticky bit (sockets.c:90), so mounting
    the host directory straight through breaks every needs_x11 profile (melonDS,
    Cemu) with "Failed to start Xwayland".

    Mirroring the host's sockets into a directory we own satisfies the ownership
    check while keeping the parent display reachable: the existing X0/X1 entries
    are hardlinked (copied if that fails across filesystems) so the nested sway's
    wlr_x11_backend can still connect out, and sway's own Xwayland creates its new
    socket here. Host-side emulators reach that new socket because the abstract
    socket namespace is shared via --network=host, which is what they actually
    connect over — the filesystem entry only has to exist somewhere writable.
    """
    target = runtime_dir / "sdss-x11"
    source = source if source is not None else X11_SOCKET_DIR
    target.mkdir(parents=True, exist_ok=True)
    try:
        for entry in source.iterdir():
            mirror = target / entry.name
            if mirror.exists():
                continue
            try:
                os.link(entry, mirror)
            except OSError:
                # Cross-device or a socket we may not link — the abstract namespace
                # still carries the connection, so a missing mirror is not fatal.
                pass
    except OSError:
        pass
    # Sticky bit last: mkdir honours the process umask, and wlroots checks for it
    # separately from ownership.
    os.chmod(target, 0o1777)
    return target


def compositor_command(config: Path, runtime_dir: Path, home: Path | None = None) -> list[str]:
    sway = native_sway()
    if sway:
        return [sway, "-c", str(config)]
    if not podman_available():
        raise RuntimeError("no sway on PATH and podman is unavailable")

    home = home or Path.home()
    parent = own_cgroup()
    x11_dir = prepare_x11_socket_dir(runtime_dir)
    return [
        "podman",
        # podman's default systemd cgroup manager refuses a --cgroup-parent that is a
        # *.scope (it only accepts systemd slices); cgroupfs accepts any cgroupfs path,
        # including the app-steam-app<id>-<pid>.scope Steam created, so the container
        # actually nests under it instead of erroring out with exit 125.
        *(["--cgroup-manager=cgroupfs"] if parent else []),
        "run",
        "--rm",
        "--userns=keep-id",
        # host networking shares the abstract socket namespace, which is how host X11
        # clients reach the nested Xwayland.
        "--network=host",
        "--ipc=host",
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
        # Shared (not private/empty) so the nested sway's wlr_x11_backend can connect out
        # to the host's per-game Xwayland socket. Must be read-write, not read-only:
        # sway's own nested Xwayland (`xwayland enable` in compositor.py) also creates a
        # new socket file here for needs_x11 profiles (Cemu, melonDS) — the host-side
        # emulator process connects to that new socket, which a ro mount would prevent
        # the container from creating in the first place.
        #
        # Sourced from prepare_x11_socket_dir() rather than /tmp/.X11-unix directly: the
        # host directory is root-owned, which --userns=keep-id maps to `nobody` inside
        # the container, and wlroots then refuses to start Xwayland at all.
        f"--volume={x11_dir}:/tmp/.X11-unix",
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
