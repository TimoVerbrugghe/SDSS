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
