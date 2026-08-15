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
| Steam shortcuts | `~/.steam/steam/userdata/95940292/config/shortcuts.vdf` |

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
