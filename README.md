# SDSS — Steam Deck Second Screen

Use a **Steam Deck as the second screen** for DS / 3DS / Wii U emulation running on a
**Steam Machine** (SteamOS, gamescope session).

The TV shows the main screen. The Deck shows the DS bottom screen / 3DS bottom screen /
Wii U GamePad — and its touchscreen acts as the stylus.

```
Steam Machine (SteamOS / gamescope session)
└── sdss run -- <emulator>
    └── nested sway compositor
        ├── output WL-1        → window inside gamescope → TV        (main screen)
        └── output HEADLESS-1  → captured by Sunshine    → Moonlight (second screen)
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
| P3 touch bridge | not started (`/dev/uinput` access confirmed available) |
| P4 Decky plugin | not started |
| P5 Deck helper | `deck/install.sh` + `deck/sdss-connect.sh` |
| P6 packaging | compositor image builds on device |

## Repo layout

```
host/       Python package `sdss` — launch wrapper, compositor + Sunshine config, CLI
plugin/     Decky plugin (Steam Machine UI)
deck/       Steam Deck side helper (Moonlight auto-connect)
runtime/    Build recipes for the bundled sway/wlroots compositor
packaging/  Install scripts and systemd user units
docs/       Plan, architecture, spike results
```

## Install

Not packaged yet. For development:

```bash
cd host && python3 -m pip install -e .
sdss doctor
```

## Constraints

- The SteamOS root filesystem stays **read-only**. Everything installs under `$HOME`
  (Flatpaks + a bundled compositor runtime).
- Emulator configs are patched with a backup journal and restored byte-identically on exit.
