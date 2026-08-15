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

- S1b: does nested sway attach to `gamescope-0` as a plain Wayland client? The machine was
  in **Desktop Mode** (Plasma/X11, `plasmashell` running, no gamescope process and no
  `gamescope-0` socket) during the first round of spikes, so this is untested. Switch with
  `steamos-session-select gamescope` — disruptive, so ask first.
- S3b: why does the melonDS Flatpak connect to the compositor but map no window?
- S3c: Xwayland inside the container (needed by Cemu, whose bundled GTK has no Wayland
  backend) does not start yet.
- S4: exact `app_id` / window titles of each emulator's second window.

## Verified during spikes

- Native AppImages map into the containerized compositor with `WAYLAND_DISPLAY` alone;
  `app_id` for Azahar is `org.azahar_emu.Azahar`, main window title `Azahar 2126.0`.
- Cemu's AppImage GTK build has **no Wayland backend** — it needs Xwayland.
- `melonDS.toml` now exists on the device (melonDS migrated it on a clean exit).
