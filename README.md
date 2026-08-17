<p align="center">
  <img src="assets/logo-wide.svg" alt="SDSS — Steam Deck Second Screen" width="620">
</p>

<p align="center">
  Use a <b>Steam Deck as the second screen</b> for DS / 3DS / Wii U emulation running on a
  <b>Steam Machine</b> (SteamOS, gamescope session).
</p>

The TV shows the main screen. The Deck shows the DS bottom screen / 3DS bottom screen /
Wii U GamePad — and its touchscreen acts as the stylus.

```
Steam Machine (SteamOS / gamescope session)
└── sdss run -- <emulator>
    └── nested sway compositor
        ├── output X11-1       → per-game Xwayland     → TV        (main screen)
        │   └── WL-1 fallback  → gamescope Wayland     → TV        (native/off-Steam)
        └── output HEADLESS-1  → captured by Sunshine  → Moonlight (second screen)
                                                           ↑ touch ↓
                                               sdss-inputd (stylus injection)
```

## Why it works this way

gamescope is a single-window micro-compositor. It cannot show or stream two toplevel
windows of one emulator independently, and a single process cannot split its windows across
two compositors. So SDSS runs the emulator inside a **nested compositor with two outputs** —
one visible on the TV, one headless and streamed.

Rejected alternatives:

| Approach | Why not |
| --- | --- |
| Apollo virtual display | Windows-only |
| Steam Remote Play | Streams only the focused game window |
| Two gamescope instances | One process cannot split windows across compositors |
| RetroArch DS cores | Both screens share one framebuffer; no second window exists |

## Supported emulators

| System | Emulator | Second-screen mechanism | Verified |
| --- | --- | --- | --- |
| Wii U | Cemu (AppImage) | `settings.xml` → `<open_pad>true</open_pad>`, window `GamePad View` | yes (needs Xwayland) |
| 3DS | Azahar (AppImage) | `qt-config.ini` → `layout_option=4` + `secondary_display_layout=2`, window `… \| Secondary Window` | yes |
| DS | melonDS (Flatpak) | `melonDS.toml` → `Instance0.Window1.ScreenSizing` | no |

RetroArch DS cores are **not** supported — they render both screens into one framebuffer.

## Status

Early development. See [docs/PLAN.md](docs/PLAN.md) for the phased plan and
[docs/hardware-recon.md](docs/hardware-recon.md) for verified facts about the target hardware.

| Phase | State |
| --- | --- |
| P0 spikes | dual output, capture, Cemu/Azahar second windows, and an end-to-end stream to the Deck all proven; see [docs/spikes](docs/spikes) |
| P1 `sdss run` | implemented; every piece verified individually |
| P2 emulator profiles | Cemu and Azahar verified; melonDS open |
| P3 touch bridge | `sdss-inputd` implemented; streams verified end to end |
| P4 Decky plugin | implemented — toggles second screen mode and restores configs |
| P5 Deck helper | `deck/install.sh` + `deck/sdss-connect.sh` |
| P6 packaging | one AppImage: installer on first run, management app afterwards |

## Repo layout

```
host/       Python package `sdss` — launch wrapper, compositor + Sunshine config, CLI
plugin/     Decky plugin (Steam Machine UI)
deck/       Steam Deck side helper (Moonlight auto-connect)
runtime/    Build recipes for the bundled sway/wlroots compositor
packaging/  Host installer, uninstaller, udev rule, desktop entry, AppImage recipe
app/        The SDSS desktop app (install, status dashboard, update, uninstall)
docs/       Plan, architecture, spike results
assets/     Logo and other artwork
```

## Install

Download **`SDSS.AppImage`** from the [latest release](../../releases/latest), make it
executable, and run it — on the Steam Machine, on the Deck, or both:

```bash
chmod +x SDSS.AppImage
./SDSS.AppImage
```

A download or a copy from a USB stick usually loses the executable bit. In Dolphin:
right-click → **Properties** → **Permissions** → tick **Is executable**. If the AppImage
refuses to start because FUSE is missing, run it as
`./SDSS.AppImage --appimage-extract-and-run`.

The app detects whether it is on a Steam Machine (`Fremont`) or a Steam Deck
(`Jupiter` / `Galileo`) and preselects it; a Deck install also asks for the Steam Machine's
address. Everything the installer does is streamed into a log pane — nothing is hidden.

The AppImage carries the whole SDSS tree, so it is the installer *and* the payload: there is
no clone and no unzip step. On success it copies itself to `~/Applications/SDSS.AppImage`
and adds a single **SDSS** entry to the application menu.

| | Steam Machine (host) | Steam Deck (client) |
| --- | --- | --- |
| Flatpaks | Sunshine | Moonlight |
| Binaries | `~/.local/bin/sdss` | `~/.local/bin/sdss-connect` |
| System changes | one udev rule (asks for sudo once) | none |
| Extras | compositor image, Decky plugin | Steam shortcut + library artwork + controller template |

Nothing is written outside `$HOME` except two files under `/etc` — the udev rule and its
SteamOS atomic-update keep-list entry, which is what makes the rule survive OS updates. The
SteamOS rootfs is never modified: no `pacman`, no `steamos-readonly disable`.

If the password prompt fails, the install still completes everything else and the udev row
in the dashboard goes red with a **Fix** button. A Deck fresh out of the box often has no
password set at all; the app says so and points at `passwd`.

### The app after installing

Reopening the AppImage shows a status dashboard instead of the installer:

- installed version, endpoint role and install path;
- a health row per component (Sunshine/Moonlight, the `sdss` shim, the udev rule, the
  compositor image, the Decky plugin, `PATH`), each with a **Fix** button that runs exactly
  the same script the installer would;
- second screen state — the global toggle and each emulator profile, including whether its
  launcher wrapper is currently in place (an EmuDeck emulator update can silently overwrite
  it; opening the app re-wraps it);
- which emulator configs are currently patched, with **Restore all**;
- **Check for updates**, **Repair**, **Uninstall** and **Open log**.

### Command line

The GUI is never the only way to do anything. The AppImage takes the same flags, which is
what SSH and CI use:

```bash
./SDSS.AppImage --role steam-machine
./SDSS.AppImage --role steam-deck --host 10.0.0.5
./SDSS.AppImage --status          # the dashboard's data as JSON
./SDSS.AppImage --uninstall
```

From a checkout, the underlying scripts work exactly as before and remain the supported
advanced path:

```bash
./install.sh --role steam-machine
~/.local/share/sdss/release/packaging/uninstall.sh
```

### Updating

**Check for updates** in the app compares the installed version against the latest GitHub
release, downloads the new AppImage, verifies its published checksum, replaces
`~/Applications/SDSS.AppImage` atomically and re-runs the install from the new payload. An
update with no published checksum is refused. Being offline is not an error — the check
just reports that it could not run.

Developers can point the same flow at a locally built file, or install from a checkout:

```bash
./install.sh                       # from the new checkout, or
~/.local/share/sdss/release/install.sh --source /path/to/new/checkout
```

The release directory is swapped in atomically; if the swap fails the previous release is
put back.

### Uninstalling

**Uninstall** in the app, or:

```bash
~/.local/share/sdss/release/packaging/uninstall.sh
```

Either way the same script runs. It restores every emulator config SDSS patched *before*
removing anything, then deletes the release, the `sdss` shim, the desktop entry, the Decky
plugin and the compositor image. On a Deck install it also removes `sdss-connect`, the SDSS
Steam shortcut, and the SDSS controller template. Tick **keep emulator configs patched** to
skip the restore. The two `/etc` files are left in place (they are inert without SDSS); the
app offers to remove them, and `uninstall.sh --help` prints the command. The AppImage itself
is removed last, since it is the running process.

Development on the host uses the same package directly:

```bash
PYTHONPATH=host/src python3 -m sdss.cli doctor
cd host && python3 -m unittest discover -s tests
```

### Building the AppImage

```bash
packaging/appimage/build.sh          # writes dist/SDSS-x86_64.AppImage + .sha256
```

It downloads a pinned standalone CPython and PySide6 and packs them with the tracked tree,
so the result depends on nothing in the SteamOS rootfs. Never build on the target — CI does
it on `ubuntu-latest`.

### Decky plugin

The plugin toggles second screen mode (globally and per emulator) and can restore all
patched emulator configs. It shells out to `sdss`, so config edits always go through the
patch journal.

SteamOS has no node, so `plugin/dist/index.js` is committed prebuilt. After changing the
frontend:

```bash
cd plugin && npm install && npm run build
```

The bundle is only rebuilt by `plugin/install.sh` when `dist/index.js` is missing, so a
committed bundle always wins on SteamOS. Force a rebuild on a machine with node:

```bash
SDSS_REBUILD_PLUGIN=1 plugin/install.sh
```

## Deck controller template (required for 1:1 touch)

`deck/install.sh` installs a Steam Input template named **SDSS - Second Screen**. It is
derived on-device from Steam's own `Gamepad with Joystick Trackpad` template plus one
always-on `Touchscreen Native Support` command, so it never goes stale when Valve changes
the controller schema.

Select it in Game Mode: `Second Screen` -> Controller Settings -> current layout ->
Templates -> `SDSS - Second Screen`.

Without this, Steam Input can consume touchscreen events before Moonlight sees them.
With it, touch maps 1:1 to the streamed second screen.

For touch validation, prefer a ROM that needs frequent bottom-screen taps (for example,
`The Legend of Zelda: A Link Between Worlds`) rather than one that can progress mostly
without touch.

## Constraints

- The SteamOS root filesystem stays **read-only**. Everything installs under `$HOME`
  (Flatpaks + a bundled compositor runtime).
- Emulator configs are patched with a backup journal and restored byte-identically on exit.
