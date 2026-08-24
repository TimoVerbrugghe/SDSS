# Hardware recon

Verified facts about the target devices. Everything here came from a real command — if you
change an assumption, re-run the command and update this file.

Collected: 2026-08-15.

## Steam Machine

| Fact | Value |
| --- | --- |
| OS | SteamOS (`holo`), kernel `6.16.12-valve24.5-1-neptune` |
| `steamos-readonly status` | `enabled` — must stay that way |
| GPU | AMD Navi 33, `/dev/dri/renderD128` |
| gamescope | `3.16.23.4` |
| Python | `3.13.5` at `/usr/bin/python3` |
| Session | `DESKTOP_SESSION=gamescope-wayland`, `GAMESCOPE_WAYLAND_DISPLAY=gamescope-0`, `DISPLAY=:0`, `STEAM_MULTIPLE_XWAYLANDS=1` |
| `XDG_RUNTIME_DIR` | `/run/user/1000` |
| Session env dump | `/run/user/1000/gamescope-environment` (source this over SSH) |
| Decky | installed, plugins in `~/homebrew/plugins` |
| Steam shortcuts | `~/.steam/steam/userdata/<steam-id>/config/shortcuts.vdf` |

### Present / missing tooling

| Present | Missing |
| --- | --- |
| `flatpak`, `podman`, `distrobox`, `git`, `python3` | `sway`, `swaymsg`, `weston`, `cage`, `wlr-randr`, `sunshine`, `moonlight`, `meson`, `ninja`, `gcc`, `flatpak-builder` |

Consequences:

- The nested compositor **must be shipped by SDSS** (Flatpak bundle, container, or
  self-contained tarball). Nothing usable is on the system.
- Sunshine is available on Flathub as `dev.lizardbyte.app.Sunshine`.
- There is no compiler on the device, so all building happens in CI or a container.

## Emulators (EmuDeck)

| Emulator | Install | Config path |
| --- | --- | --- |
| Cemu | `~/Applications/Cemu.AppImage` | `~/.config/Cemu/settings.xml` |
| Azahar | `~/Applications/azahar.AppImage` | `~/.config/azahar-emu/qt-config.ini` |
| melonDS | Flatpak `net.kuribo64.melonDS` 1.1 | `~/.var/app/net.kuribo64.melonDS/config/melonDS/` |

Other EmuDeck AppImages present: DuckStation, Eden, Shadps4-qt, pcsx2-Qt, rpcs3, Vita3K.
Other Flatpaks include RetroArch 1.22.2, Dolphin, PPSSPP.

### Cemu — separate GamePad view

`~/.config/Cemu/settings.xml` already contains the relevant keys:

```xml
<open_pad>false</open_pad>
<pad_position><x>0</x><y>6</y></pad_position>
<pad_size><x>328</x><y>207</y></pad_size>
<pad_maximized>false</pad_maximized>
```

`open_pad` is the "Separate GamePad view" toggle.

### Azahar — separate windows

`~/.config/azahar-emu/qt-config.ini`, section `[Layout]`. From
`azahar/src/common/settings.h`:

```
LayoutOption:            Default=0 SingleScreen=1 LargeScreen=2 SideScreen=3
                         SeparateWindows=4 HybridScreen=5 CustomLayout=6
SecondaryDisplayLayout:  None=0 TopScreenOnly=1 BottomScreenOnly=2 SideBySide=3
                         OppositeScreenOnly=4 Original=5 Hybrid=6 LargeScreen=7
```

So the second screen needs `layout_option=4` and `secondary_display_layout=2`.
Qt settings files also carry `key\default=true` markers that must be flipped to `false`,
otherwise Azahar rewrites the value from its default.

### melonDS — second window

melonDS 1.x uses `melonDS.toml` and migrates the legacy `melonDS.ini` on first save. From
`src/frontend/qt_sdl/Config.cpp`, per-window settings live under
`Instance<N>.Window<M>.*` (`ScreenLayout`, `ScreenSizing`, `ScreenSwap`, `ScreenRotation`,
`ScreenGap`, `ShowOSD`, `Width`, `Height`).

The device currently only has the legacy flat `melonDS.ini` (`ScreenLayout=3`,
`ScreenSizing=3`), which maps to `Instance0.Window0.*`. Whether melonDS re-creates
`Window1` at startup purely from config is **unverified** — spike S4.

## Open questions

- S1b: does nested sway attach to `gamescope-0` as a plain Wayland client? **Blocked**:
  gamescope refuses to start with no display connected (`HDMI-A-1` and `DP-1` both
  disconnected) and segfaults on `cannot find any connected connector`. Connect the TV or a
  dummy HDMI plug first. Note `steamos-session-select gamescope` *persists* the default
  login mode, so returning to the desktop needs
  `steamosctl set-default-login-mode desktop` as well.
- S3b: why does the melonDS Flatpak connect to the compositor but map no window?

## Process lifetime and environment on SteamOS

Two traps that cost a lot of debugging time; both bite anything launched over SSH.

- **`KillUserProcesses=True`.** SteamOS ships
  `/etc/systemd/logind.conf.d/killuserprocesses.conf`, and `deck` has `Linger=no`. When a
  session ends, logind SIGKILLs its entire session scope — `nohup`, `setsid` and `disown`
  do *not* help, because none of them move the process out of that cgroup. Symptom: the
  emulator dies a few minutes after launch with no exit status anywhere, taking any
  supervisor process with it. Launch long-lived processes under the user manager instead:

  ```
  systemd-run --user --unit=sdss-emulator --setenv=… <cmd>
  ```

- **The user manager's environment is not the shell's.** It imported
  `QT_QPA_PLATFORM=xcb` and `DISPLAY=:0` from the Plasma session, so a Qt emulator started
  as a user unit renders to the *host* X server and maps no window in the nested sway —
  while still happily emulating at ~10% CPU. Set the backend explicitly:
  `QT_QPA_PLATFORM=wayland`, `DISPLAY=`, `XDG_SESSION_TYPE=wayland`.

  Verified: with those set, both Azahar windows map, `Secondary Window` lands on
  `HEADLESS-1`, and the unit survives SSH logout.

### Steam shortcut launch procedure

For Steam Game Mode validation, start the Deck's generated `Second Screen`
shortcut from Steam. A direct `sdss-connect <host>` command over SSH is not an
equivalent test: it bypasses Steam's game scope and must be reserved for
pairing or isolated Moonlight diagnostics. When automation is necessary, send
the Steam URL to the already-running Steam client after sourcing the live
gamescope environment.

The reliable SSH-side launch procedure is:

```
export XDG_RUNTIME_DIR=/run/user/1000
set -a; source /run/user/1000/gamescope-environment; set +a
/home/deck/.local/share/Steam/steam.sh "steam://rungameid/<64-bit-gameid>"
```

Before launching, confirm Game Mode with Steam's `-steamdeck -gamepadui`
process and an active `wayland` user session. After launching, confirm the
Moonlight process is descended from Steam's `reaper SteamLaunch` process and
that `gameprocess_log.txt` records the shortcut's 64-bit gameid. Do not use
`nohup`, `setsid`, or a direct Moonlight/`sdss-connect` process to stand in for
the Steam shortcut when validating the end-to-end flow.

The 64-bit gameids currently verified in `shortcuts.vdf` are Azahar
`13816419520748716032`, Cemu Wind Waker HD `15768959151553642496`, and Deck Second
Screen `13044482501723029504`. The 32-bit shortcut appid does not launch the shortcut
through this path. Bare `steam <url>` and an unsourced SSH environment also silently
fail to hand the launch to gamescope.

During teardown testing, killing the Steam-managed emulator process tree with `pkill`
left Steam's client alive but its game state wedged; subsequent URLs appeared to do
nothing. Prefer stopping the game through Steam. If this occurs, restart the user
gamescope session, wait for Steam to return, source the newly generated
`gamescope-environment`, and retry. A known-good Azahar launch then produced:

```
sdss: detected outer gamescope resolution 1280x800@60Hz
sdss: starting nested compositor on parent x11 display :1
sdss: nested compositor on wayland-1 (xwayland :2)
sdss: starting Sunshine on port 47989
sdss: launching emulator on wayland-1
```

## Second screen, end to end (verified)

Deck touch → Moonlight → Sunshine `Touch passthrough` → `sdss-inputd` →
`zwlr_virtual_pointer` on `HEADLESS-1`: 1985 injected motions from ~74 discrete touches,
all normalised coordinates inside 0..1. Gamepad works over the same session, and the
emulator's second window is what Sunshine captures.

## Sunshine's input devices

Sunshine creates its passthrough devices with uinput when a client connects:

```
event11 Mouse passthrough              event14 Touch passthrough
event12 Mouse passthrough (absolute)   event15 Pen passthrough
event13 Keyboard passthrough           event16 Sunshine X-Box One (virtual) pad
```

- Only the keyboard and the gamepad get a `uaccess` ACL from logind; touch, pen and both
  mouse nodes land as `0660 root:input` with no ACL, so `sdss-inputd` cannot open them.
  Fixed by `packaging/60-sdss-input.rules` (`TAG+="uaccess"` on the three names the
  bridge actually claims — touch, pen, and the *absolute* mouse node; the relative
  `Mouse passthrough` is deliberately left alone, see S5).
- **Steam Input on the Deck swallows the touchscreen.** With the default controller layout
  for a non-Steam shortcut, touches never reach Moonlight and *nothing* arrives on any
  passthrough device — while the gamepad works perfectly. Changing the layout's touchscreen
  handling on the Deck fixes it. Moonlight's own `--touchscreen-trackpad` setting is not
  the cause. Reliable fix on the shortcut: Edit Layout -> Action Sets -> cog on `Default`
  -> `Add Always-On Command` -> `System` -> `Touchscreen Native Support`.
- Beware a measurement trap: the virtual pad emits `ABS_X/Y/RX/RY` continuously as idle
  controller state (~7k events/min), regardless of user input. Do not read that as activity.
- `EVIOCGRAB` still fails with `EBUSY` while the desktop **Xorg** session holds the
  devices. Not an issue in Game Mode, where gamescope is the only consumer.
- The virtual pad reports `045e:02ea` version `0408`, name `Xbox One S Controller`, SDL
  GUID `03008d205e040000ea02000008040000`. Emulators mapped to a *Steam Input* pad
  (`030079f6de280000ff11000001000000`, vendor `0x28de`) ignore it — the GUID must match.
  Probe it with `ctypes` against `/usr/lib/libSDL2-2.0.so.0` rather than guessing.

## Verified during spikes

- Native AppImages map into the containerized compositor with `WAYLAND_DISPLAY` alone.
- **Azahar** is a native Wayland client. `app_id=org.azahar_emu.Azahar`; the second window is
  titled `Azahar <ver> | <game> | Secondary Window`.
- **Cemu 2.6** requires X11 (`class=Cemu`, second window `GamePad View - FPS: …`). Its
  AppImage bundles a Wayland-capable `libgdk-3.so.0` but still fails with
  `Unable to initialize GTK+, is DISPLAY set properly?` — see
  [cemu-project/Cemu#1809](https://github.com/cemu-project/Cemu/issues/1809).
- Xwayland runs in the container when it is given an X11 socket dir owned by `deck`; host
  clients reach it over the abstract socket with `DISPLAY=:1`.
- `melonDS.toml` now exists on the device (melonDS migrated it on a clean exit).
- ROMs: `~/Emulation/roms/n3ds` (`.cci`), `~/Emulation/roms/nds` (`.nds`),
  `~/Emulation/roms/wiiu/roms` (`.wua`).
- For touch verification, prefer ROMs that require bottom-screen input often (for example,
  `The Legend of Zelda: A Link Between Worlds`) over titles where touch is mostly optional.

## Steam Input and packaging (verified on device, SteamOS)

- DMI `product_name` identifies the endpoint: Steam Machine reports `Fremont`
  (`sys_vendor=Valve`), Steam Deck reports `Jupiter` (LCD) or `Galileo` (OLED).
  `cat /sys/devices/virtual/dmi/id/product_name`
- **SteamOS ships no pip**: `python3 -m pip` → `No module named pip`. The installer must
  not use it; `sdss` is installed as a shim that sets `PYTHONPATH` and runs
  `python3 -m sdss.cli`. Device Python is 3.13.5.
- Steam Input templates live in `~/.steam/steam/controller_base/templates/*.vdf`
  (text VDF). This is also where EmuDeck drops its custom templates.
- The always-on **Touchscreen Native Support** command is the binding
  `controller_action ts_n`, placed in an `always_on_action` input of the group whose
  `"mode"` is `switches`. Confirmed from Steam's own
  `controller_neptune_touchscreen.vdf` (which uses `ts_hover`/`ts_lc`/`ts_mc`/`ts_rc`)
  and from EmuDeck's `emudeck_cloud_controller_config.vdf` (which uses `ts_n`).
  Steam's UI maps these to `#ControllerActionKey_Change_TouchscreenMode_*`.
  `grep -rl "controller_action ts_hover" ~/.steam/steam/`
- A template needs `"export_type" "template"` and `"url" "template://<file>.vdf"` to show
  up in the Templates list.
- Decky Loader's `~/homebrew/plugins` is **root-owned** (`drwxr-xr-x root root`), so
  installing a plugin needs `sudo` even though everything else stays in `$HOME`.
  Installed plugin files themselves are owned by `deck`.
- Steam rewrites `shortcuts.vdf` from memory on exit, discarding edits made while it
  runs — the Deck installer warns before touching it.

## Sunshine virtual input, udev and pywayland (verified 2026-08-16, Steam Machine)

Gathered while closing spikes S5–S7 (`docs/spikes/`). Devices were reproduced with
`uinput` using Sunshine's exact device names; kernel
`6.16.12-valve24.5-1-neptune-616`.

- A uinput node appears at `/devices/virtual/input/inputN/eventN` and its parent input
  device reports **`DRIVERS==""`, not `uinput`**. A udev rule matching `DRIVERS=="uinput"`
  therefore never fires. Match `DEVPATH=="/devices/virtual/*"` instead; a physical device
  is under `/devices/pci*`.
  `udevadm info -a -n /dev/input/eventN | grep -E "looking at|DRIVERS"`
- Without `packaging/60-sdss-input.rules` the node is `0660 root:input` with no ACL. With
  it, `getfacl` shows `user:deck:rw-` and `CURRENT_TAGS=:uaccess:seat:`.
  `getfacl -p /dev/input/eventN`
- `evdev`'s `UInput.device` is **`None`** when udev has not granted access — the library
  cannot reopen its own node. Resolve paths from
  `/sys/devices/virtual/input/input*/event*` rather than relying on it.
- **A Sunshine stream restart reuses the same `/dev/input/eventN` path.** `st_rdev` is
  unchanged, only `st_ino` differs (observed 1578 → 1582 on `event20`), and the stale fd
  polls `POLLERR|POLLHUP` (24) instead of ever becoming readable. Hence device identity is
  the `(st_rdev, st_ino)` pair and the poll loop must treat a hangup as "drop and rescan".
- `EVIOCGRAB` is genuinely exclusive: with two readers on one node, the grabber saw all 4
  injected events and the second reader saw 0 — then 2 after `ungrab()`.
- Inside `localhost/sdss-compositor:latest`, nested sway with `WLR_BACKENDS=headless`
  names its output **`HEADLESS-1`** and offers `zwlr_virtual_pointer_manager_v1` v2,
  `wl_seat` v9, `wl_output` v4.
- `pywayland`'s `Display.dispatch(block=False)` is `wl_display_dispatch_pending` and, per
  its own docstring, "does not attempt to read the display fd". `Display.read()` is
  required, or a poll loop watching the fd spins at 100% CPU.
- **The `Display` must be disconnected explicitly.** Letting it be garbage-collected
  segfaults the interpreter at exit (exit 139 without `disconnect()`, exit 0 with it).

## Controller template generation (verified 2026-08-16, Steam Deck)

`deck/install-controller-template.py` derives the SDSS template from Steam's own
`controller_neptune_gamepad_joystick.vdf` rather than shipping a copy. Checked against the
real templates directory (78 files after install):

- The base template is present, is 11832 chars, contains a `switches` group, and has **no**
  `ts_n` binding of its own — so the derivation has something to attach to and is not
  silently relying on Steam having already done it.
- Generated output contains exactly **one** `controller_action ts_n` binding, in an
  `always_on_action` input of the `switches` group, plus `"export_type" "template"` and
  `"url" "template://sdss_second_screen.vdf"` (both required for it to appear in Steam's
  Templates list).
- Generation is **deterministic**: two runs produce byte-identical output.
- Derivation is **idempotent**: re-deriving from an already-patched base still yields one
  `ts_n` binding, not two.
- Installing twice leaves no `.tmp`/`.new` debris — the atomic write cleans up after
  itself — and all 78 templates still parse afterwards.

## Launching shortcuts, pairing and capture (verified 2026-08-18, both devices)

Full end-to-end run: an emulator launched from its Steam shortcut on the Steam Machine,
streamed to the Deck's `Second Screen` shortcut, both confirmed live.

### Launching a non-Steam shortcut must go through Steam, not SSH

Anything launched over plain SSH never inherits a per-game Xwayland `DISPLAY`, so
`runtime.parent_display()` picks a different branch than it does in the real session.
**Test evidence gathered by SSH-launching the emulator is void** — this invalidated a
day's worth of it. Hand the URL to the already-running Steam client instead, so gamescope
is the parent:

```sh
export XDG_RUNTIME_DIR=/run/user/1000
set -a; source /run/user/1000/gamescope-environment; set +a   # yields DISPLAY=:0
/home/deck/.local/share/Steam/steam.sh "steam://rungameid/<64-bit-gameid>"
```

Bare `steam <url>` without the sourced environment does nothing. A related artifact: Qt
apps aborting over SSH with **exit 134 (SIGABRT) on "could not connect to display"** do
not reproduce under a Steam launch.

### `rungameid` needs the 64-bit id, built from the *unsigned* appid

`((appid & 0xFFFFFFFF) << 32) | 0x02000000`. Passing the 32-bit appid, or a negative one,
is **silently ignored** — no error, nothing launches. `shortcuts.vdf` stores appids signed
(int32), so an id read back out is routinely negative and must be masked. Reading the
shortcuts requires the **binary** parser in `deck/add-steam-shortcut.py` (`parse()` /
`serialize()`); `deck/vdf.py` is for **text** VDF (controller templates) and cannot read
this file. There is no `sdss.deck` package, so load it by path:

```python
spec = importlib.util.spec_from_file_location("ass", "deck/add-steam-shortcut.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
entries = m.parse(path.read_bytes())
```

Steam rewrites `shortcuts.vdf` from memory on exit — restart Steam after editing it.

### Pairing is keyed to the host IP, and is two-sided

Moving the host to a new DHCP address invalidates the existing pairing. `moonlight list`
then returns empty, and `sdss-connect.sh` treats that as fatal — which presents as
**"spinner, then the shortcut closes"**. The script is behaving correctly; the host is
simply unpaired. Re-pair with both halves overlapping in time: the Deck runs
`moonlight pair <host> --pin <pin>` while the host writes the same PIN into the FIFO
`/run/user/1000/sdss/session/pin` (a real FIFO, `prw-------`, so the write blocks until
read). A ~12 s delay before writing worked.

`moonlight list` over SSH needs `QT_QPA_PLATFORM=offscreen` and
`XDG_RUNTIME_DIR=/run/user/1000`; it is slow, so wrap it in `timeout 60`.

### Verified process chains

Host, from the Steam shortcut:

```
reaper SteamLaunch AppId=… -> azahar.sh <rom>
  -> python3 -m sdss.cli run --profile azahar -- …azahar.AppImage.sdss-real <rom>
    -> podman run … --volume=/run/user/1000/sdss-x11:/tmp/.X11-unix …   (nested sway)
    -> bwrap -- sunshine …/sdss/sunshine/sunshine.conf -0
```

Deck, from its shortcut:

```
reaper SteamLaunch AppId=… -> sdss-connect <host>
  -> bwrap -- moonlight stream <host> Second Screen --resolution 1280x800 --fps 60 \
       --display-mode fullscreen --no-vsync --no-touchscreen-trackpad
```

### `wlgrab`'s log ordering is misleading

### Capturing the Steam Machine screen

Use Gamescope's control tool for screenshots rather than `spectacle` or an
`ffmpeg` display grab. The live gamescope environment is required:

```sh
export XDG_RUNTIME_DIR=/run/user/1000
set -a; source /run/user/1000/gamescope-environment; set +a
/usr/bin/gamescopectl screenshot /home/deck/sdss-debug/steam-machine.png
```

The command writes a PNG and captures the outer Steam Machine display. It is
useful for diagnosing Steam's spinner, library screen, or a visible emulator;
it does not capture SDSS's streamed `HEADLESS-1` output.

Sunshine enumerates every output, printing `Name:` / `Offset` / `Logical size` for each,
then prints `Selected monitor [...]` immediately after the **last one enumerated** — which
is not necessarily the one selected. A `Logical size: 2560x1440` line can therefore sit
directly above the selection line while the captured output really is 1280x800. Always
grep with context and match `Logical size` back to its own preceding `Name:`. Correct
capture reads `Name: HEADLESS-1` / `Offset: 8192x0` / `Logical size: 1280x800`.

### `/tmp/.X11-unix` cannot be bind-mounted from the host

It is `root:root`, and under `--userns=keep-id` host root maps to `nobody` inside, so
wlroots refuses it ("not owned by root or us") and Xwayland fails to start. SDSS mirrors
the sockets into a user-owned `$XDG_RUNTIME_DIR/sdss-x11` (sticky bit applied *after*
`mkdir`, since `mkdir` honours the umask) and mounts that instead.

### Debugging notes

- A diagnostic `podman run` needs `--entrypoint=/bin/sh`; the image's ENTRYPOINT is `sway`.
- Query the nested sway:
  `podman exec $(podman ps -q | head -1) sh -c "SWAYSOCK=/run/user/1000/sway-ipc.1000.<pid>.sock swaymsg -t get_outputs"`
  (glob for the socket with `ls /run/user/1000/sway-ipc.*`).
- **Emulator stderr is not captured** into the SDSS log. This has cost real time twice.
- Stale `sdss_inputd` / `sunshine` processes leak across failed runs and hold port 48010.
  Kill them and `podman rm -af` between test runs.
- A Steam UI OOM abort does not imply that the emulator scope stopped. On 2026-08-18,
  a failed Cemu close left a 1.1 GiB Cemu process alive while a new Azahar launch
  started another 1.1 GiB emulator; Steam then aborted with `cannot allocate memory
  for thread-local data`. Before a new Steam launch, verify the previous emulator
  process group is gone, not just that Steam has restarted.
- SDSS now holds a per-user session lock and rejects a second `sdss run` while one session
  is active. It also handles `SIGTERM`/`SIGINT` by entering the normal process-cleanup path.
  Emulator configs are no longer part of that path: enabled state owns per-profile selective
  journals, so Steam-native close only needs to reap the emulator, Sunshine, and compositor.
- Azahar's native Wayland path is not stable under Steam on this image. Animal Crossing:
  New Leaf reproduced `wl_display#1: error 1: invalid method 1` followed by Steam's
  allocator abort. Nested Xwayland is stable, preserves the Steam overlay, and exposes
  `WM_CLASS=Azahar`; the secondary-window rule must match that class as well as the native
  `org.azahar_emu.Azahar` app ID.
- Watching only the Steam reaper's lifetime is insufficient: Steam can abort and restart
  while the reaper and application scope survive. The SDSS lineage watcher must also track
  the actual `steam` ancestor so that a client restart triggers normal session cleanup.
- Never let SDSS helpers inherit Steam's `gameoverlayrenderer.so`. The wrapper saves the
  preload and removes it before the coordinator starts; only the emulator gets it back.
  Cemu then matches stock memory behavior instead of driving Steam above 2 GiB.
- Vulkan Azahar through nested Xwayland leaks roughly 430–480 MiB from Steam every five
  seconds when the overlay is present. A journaled OpenGL override keeps the overlay working
  without loading `steamoverlayvulkanlayer.so`.
- Podman's pause helper is identified by `$XDG_RUNTIME_DIR/libpod/tmp/pause.pid`, not by a
  stable cgroup layout. Kill it only when its parent belongs to the current launch ancestry.
- Emulator config journals belong to enabled state, not a game session. Their snapshots
  restore only declared keys; game exit is process teardown only.
- SDSS wraps the *emulator binary*, not the EmuDeck launcher. AppImage emulators get an
  in-place wrapper plus a `.sdss-real` shadow and are honoured; melonDS is **bypassed**
  because EmuDeck's `melonds.sh` hardcodes `/usr/bin/flatpak run`.
- SSH's first connection attempt sometimes fails with "Permission denied"; retry after ~3 s.

### Steam UI OOM crash on emulator switch — investigation, 2026-08-24/25 (unresolved)

A recurring, hard-to-reproduce-on-demand bug: switching between/exiting emulators
(Azahar ↔ Cemu) with SDSS active eventually crashes the Steam Machine's Steam UI (a
spinner, then a full Steam client restart). This is distinct from the older,
already-fixed leaks documented above (Cemu Vulkan-layer segfault spam,
overlay-through-Xwayland leak) — same fatal signature
(`tier0/memstd.cpp: OUT OF MEMORY` → `Fatal assert; application exiting` →
`cannot allocate memory for thread-local data: ABORT`), different root cause, not yet
fixed.

**Instrumentation technique that actually worked**: `nohup`/`setsid` background samplers
do not reliably survive either an SSH disconnect or Steam's own crash/restart (which
changes the PID being tracked). The only approach that survived both: a self-relaunching
bash loop that re-`pgrep`s for `ubuntu12_32/steam -srt-logger-opened` each time it dies,
deployed as a transient systemd unit — `systemd-run --user --unit=<name> <script>` —
which keeps running across SSH disconnects and Steam restarts. Tear down with
`systemctl --user stop <name>` before deploying a replacement.

**What was conclusively ruled out**, each via a controlled hardware experiment (not just log
correlation):
- **Not the crash-cascade/coredump volume.** A prior fix (`RLIMIT_CORE=0` via `preexec_fn`
  on the compositor and emulator `Popen` calls, still in `session.py` as a real, independent
  improvement) eliminates coredumps from the sway/Xwayland teardown crash cascade, but the
  Steam UI still crashes with zero coredumps generated in the failing window.
- **Not Sunshine, not `wlgrab`, not `HEADLESS-1`.** Direct memory sampling proved Steam's
  own 32-bit client process (`ubuntu12_32/steam -srt-logger-opened ...`) is what leaks —
  linear, ~56 MB/s, 100% anonymous/private-dirty pages (`smaps_rollup`), new small
  `rw-p`/guard/`rw-p` VMA triples appearing every ~2s at monotonically decreasing addresses
  (never reused) — climbing from ~236 MB to the 32-bit ~3-4 GB address-space ceiling in
  roughly 55-65 seconds, then a fatal `tier0` OOM abort and a fresh Steam PID.
  `wlgrab` (the string Steam's console log relays) turned out to be a string inside
  **Sunshine's own binary** (`strings` on the Flatpak's `files/bin/sunshine`), not Steam's —
  it is Sunshine's wlroots screencapture backend logging retries, relayed through Steam's
  own log-capture pipe (`srt-logger`) like every other subprocess SDSS launches, not
  evidence of anything inside Steam itself. Confirmed directly: launching Azahar with the
  `_start_sunshine()` call skipped entirely (Sunshine never started at all — no
  `HEADLESS-1` capture attempt possible) reproduces the identical leak, same rate, same
  crash. Game Recording was also independently confirmed off in Steam settings throughout
  all testing, and Remote Play was confirmed off/never enabled.
- **Not excessive log volume.** The already-documented Cemu-Vulkan-layer-segfault OOM (see
  `DISABLE_GAMESCOPE_WSI` in `launch.py`) hit the same `tier0/memstd.cpp` signature via
  genuinely massive stack-trace log spam relayed through `srt-logger`. This is a different
  mechanism: the console log during the leak window is essentially quiet (no line repeats
  more than a handful of times), and Steam's open-fd count (sampled via `/proc/<pid>/fd`
  each time it changed) stayed flat at ~155-172 the entire time RSS climbed from 236 MB to
  over 3 GB — ruling out "too many open log handles/lines" as this leak's mechanism.
- **Not touch/pointer input volume.** With the touch bridge active but the Deck screen
  completely untouched after launch, Steam's RSS stayed essentially flat (~5 MB total
  drift over 7 minutes and several emulator relaunches) — no continuous climb. Conversely,
  deliberately dragging/touching heavily for ~20-30s after launch also did **not** trigger
  or accelerate a crash (RSS moved by only ~1-2 MB during a heavy-touch window). This rules
  out wlroots' X11 backend's broadcast XInput2 motion-event registration (see below) as the
  proportional trigger, even though it remains a structurally unusual thing that backend
  does.

**What was conclusively confirmed** as the necessary trigger, via a controlled A/B on the
exact same session shape:
- Patching `runtime.parent_display()` to unconditionally return `("wayland", ...)` (i.e.
  forcing `WLR_BACKENDS=wayland,headless`, so the nested compositor never touches Steam's
  per-game Xwayland at all) made the leak disappear completely across repeated launches —
  Steam's RSS stayed flat (~250-256 MB) for the full test. Restoring the normal
  `x11,headless` backend (the documented default whenever Steam hands SDSS a `DISPLAY`)
  reproduces the leak reliably again. **The X11-backend connection into gamescope's
  per-game Xwayland is necessary for the leak; the Wayland-backend connection is not
  sufficient to trigger it.**
- However, the Wayland-backend build is **not usable as-is**: a `gamescopectl screenshot`
  taken while Azahar was running under the forced-Wayland build showed only Steam's
  spinner — the emulator's window, rendered as a plain client on the shared `gamescope-0`
  Wayland socket, is invisible to gamescope's own game-window compositing, not just to its
  spinner-dismissal heuristic. So the TV picture itself, not just spinner-clearing, depends
  on the X11-backend/per-game-Xwayland connection; there is no "render on Wayland, use a
  lightweight proxy purely for readiness" shortcut — the real picture has to go through
  that Xwayland one way or another.
- gamescope's own source (`ValveSoftware/gamescope:src/steamcompmgr.cpp`) was read in full:
  its readiness/spinner logic is purely event-driven (`add_win`/`map_win` on
  `CreateNotify`/`MapNotify`, `damage_win`'s "first damage from a window with a resolved
  `STEAM_GAME` appID" heuristic triggering a focus-atom recompute that the closed-source
  Steam client reads) and its two `XQueryTree` call sites free their buffers immediately
  and run only on new-window events, never in a continuous loop — gamescope's own C++ side
  is not the leak. wlroots' X11 backend
  (`wlroots/wlroots:backend/x11/{backend,output}.c`) uses DRI3/Present/XFixes/XInput2/
  XRender at connection time, does one `xcb_present_pixmap` + `xcb_flush` per rendered
  frame, and registers **broadcast** XInput2 events (`XCB_INPUT_DEVICE_ALL_MASTER`) on its
  own window — but contains **no RandR usage at all**, so it cannot be causing any kind of
  screen/CRTC-geometry-change storm. No wlroots GitLab issue or gamescope GitHub issue
  matching this exact scenario (a second X11 client, i.e. our nested compositor, causing a
  leak in a third, unrelated client's — Steam's — own process) was found in either
  tracker.
- The leak is **not reliably reproducible on demand**: several clean back-to-back
  Azahar/Cemu launches in a row can run fine, then a later one (with no obvious
  difference in usage pattern) crashes — consistent with a timing/ordering race at
  session-startup rather than something proportional to elapsed time, input volume, or
  log volume once running.

**Current state: root cause NOT yet identified.** The bug is real, reproduces on both
hardware devices, and is scoped specifically to "SDSS's nested `sway` connects its
`wlr_x11_backend` to Steam's per-game Xwayland" — but *why* that specific kind of X11
client triggers a leak inside Steam's own separate, closed-source client process, and why
it is racy rather than deterministic, is unresolved. Candidate next steps for a future
session: capture Steam's own X11 traffic on that Xwayland (e.g. `xtrace`/`Xephyr` shim, or
`WLR_XWAYLAND_DEBUG`-style logging) during both a clean and a crashing launch to diff what
Steam's client actually does differently; or instrument the exact moment (which frame /
which X event) RSS growth begins relative to sway's own startup sequence, to narrow the
race window. Do not re-attempt the previously-tried fixes (kill-ordering, coredump
suppression) for this bug — they are confirmed unrelated.
