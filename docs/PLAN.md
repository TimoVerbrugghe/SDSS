# SDSS — Implementation Plan

## Goal

Steam Machine (SteamOS, gamescope session) runs Azahar / Cemu / melonDS. The Steam Deck
connects over Moonlight and shows **only** the second screen (3DS bottom / DS bottom / Wii U
GamePad), with the Deck touchscreen acting as stylus / GamePad touch. Toggled from a Decky
plugin on the Steam Machine.

## Constraints

- Touch/stylus from the Deck is required in v1.
- Emulators come from EmuDeck: Cemu and Azahar are AppImages in `~/Applications`,
  melonDS is a Flatpak.
- Python for host tooling, TypeScript for the Decky frontend.
- The SteamOS root filesystem stays read-only. No `pacman`, no `steamos-readonly disable`.
  Flatpaks plus bundled binaries under `$HOME` only.

## Core technical finding

gamescope is a single-window micro-compositor: two toplevel windows of one emulator cannot
be shown or streamed independently, and one process cannot split its windows across two
compositors. Apollo's virtual display is Windows-only. Steam Remote Play only streams the
focused game. Therefore Sunshine + Moonlight is mandatory, and the emulator must run inside
a nested compositor that owns two outputs.

## Architecture — nested dual-output sidecar compositor

`sdss run` starts a nested **sway** with `WLR_BACKENDS=wayland,headless`:

- `WL-1` — a Wayland window inside the session gamescope, fullscreen on the TV. Holds the
  emulator's **main** window.
- `HEADLESS-1` — 1280x800@60, holds the emulator's **second** window. Captured by a
  dedicated Sunshine instance (`capture = wlr`, `output_name = HEADLESS-1`,
  `stream_audio = disabled`).

sway is chosen over a custom wlroots compositor because it already provides Xwayland,
window rules by `app_id`/class/title, multi-seat, and the `swaymsg` IPC.

## Touch routing

Moonlight and Sunshine already carry input natively. While a stream is running, Sunshine
materialises these devices on the Steam Machine (verified):

```
Mouse passthrough          Keyboard passthrough      Touch passthrough
Mouse passthrough (absolute)   Pen passthrough       Sunshine X-Box One (virtual) pad
```

So SDSS writes no networking and no input protocol. Two categories fall out of that:

- **Gamepad and keyboard need nothing.** Emulators read gamepads through evdev globally, so
  the Deck can act as an extra controller with zero SDSS code.
- **Touch and stylus need a bridge**, for two reasons:
  1. *Ownership.* Those devices belong to the host seat. The nested compositor reports
     **0 input devices** (`swaymsg -t get_inputs`), because it runs on the wayland/headless
     backends and never opens libinput. In the gamescope session the events go to gamescope,
     which forwards them to its focused surface — sway's **TV** output. The headless output
     can never receive them.
  2. *Coordinate space.* The absolute devices are normalised across the whole output layout
     (TV `1920x1080` at `0,0` plus the second screen `1280x800` at `1920,0`). A tap at the
     top-left of the Deck would land at the top-left of the TV.

`sdss-inputd` therefore has a narrow job: take `Touch passthrough` (and the absolute mouse),
`EVIOCGRAB` it so gamescope cannot also act on it, rescale onto the second output, and inject
through `zwlr_virtual_pointer_manager_v1.create_virtual_pointer_with_output` bound to
`HEADLESS-1`. Every emulator here accepts an absolute pointer as touch/stylus.

`/dev/uinput` already carries an ACL granting `user:deck:rw`, so no root and no udev rule.

Rejected alternative: sway's own `input <id> map_to_output HEADLESS-1` would do the mapping
for free, but only for devices sway owns through a libinput backend. Adding libinput to a
nested sway would make it open *every* device and fight gamescope for them.

## Emulator profiles

| Emulator | Install | Config | Second-screen edit |
| --- | --- | --- | --- |
| Cemu | `~/Applications/Cemu.AppImage` | `~/.config/Cemu/settings.xml` | `<open_pad>true</open_pad>` — **verified**, window `GamePad View` (X11 only) |
| Azahar | `~/Applications/azahar.AppImage` | `~/.config/azahar-emu/qt-config.ini` | `[Layout] layout_option=4` (SeparateWindows), `secondary_display_layout=2` (BottomScreenOnly) — **verified**, window `… | Secondary Window` |
| melonDS | Flatpak `net.kuribo64.melonDS` | `~/.var/app/net.kuribo64.melonDS/config/melonDS/melonDS.toml` | `Instance0.Window1.ScreenSizing` = bottom-only — unverified |
| RetroArch DS | — | — | **Not viable** — one framebuffer, no second toplevel |

All config edits go through a backup journal so the user's config is restored
byte-identically when the session ends.

## Repo layout

```
host/       Python package `sdss`
plugin/     Decky plugin (TS frontend + Python backend shelling out to `sdss`)
deck/       Deck-side Moonlight auto-connect helper
runtime/    Build recipe for the bundled sway/wlroots compositor
packaging/  install script, systemd user units, flatpak overrides
docs/       this plan, recon notes, spike results
```

## Phases

| Phase | Content |
| --- | --- |
| P0 | Recon and spikes (blocking) |
| P1 | `sdss run` MVP: sway dual output + Sunshine + manual launch options |
| P2 | Emulator profiles + config patch/restore |
| P3 | Touch input bridge (`sdss-inputd`) |
| P4 | Decky plugin |
| P5 | Deck-side auto-connect helper |
| P6 | Packaging, install/update, docs |

## Spikes

Results so far: [spikes/S1-compositor.md](spikes/S1-compositor.md).

| ID | Question | State |
| --- | --- | --- |
| S1a | Nested wlroots compositor with two outputs | **pass** |
| S1b | Nested sway as a client of the gamescope session | **blocked** — gamescope needs a connected display |
| S2a | Headless output can be captured (wlroots screencopy) | **pass** (grim) |
| S2b | Sunshine `capture = wlr` against `HEADLESS-1` | **pass** (VA-API hardware encode) |
| S3a | Host clients render into the containerized compositor | **pass** (AppImage) |
| S3b | Flatpak emulator renders into it | open (melonDS connects, maps no window) |
| S3c | Xwayland inside the container (needed by Cemu) | **pass** |
| S4 | Exact second-window `app_id`/title per emulator | **pass** for Cemu and Azahar |
| S5 | `EVIOCGRAB` hides Sunshine virtual input from gamescope | open |
| S6 | wlr virtual pointer bound to an output drives the headless window | open |
| S7 | End-to-end stream to the Deck, and Moonlight touch mode | **pass** for streaming; touch mode open |

`/dev/uinput` on the Steam Machine already carries an ACL granting `user:deck:rw`, so the
touch bridge will not need root or a udev rule.

The compositor is delivered as a **container image** built with the podman that SteamOS
already ships (`runtime/`), so nothing is installed outside `$HOME`. Because sway runs in a
container it cannot exec host binaries: `sdss` starts the compositor first and then launches
the emulator on the host against the compositor's Wayland socket.

## Verification

- One `docs/spikes/SN-*.md` per spike with the exact commands and pass/fail.
- Unit tests: patch → restore round-trip is byte-identical; profile matching.
- Manual E2E per emulator: TV shows the main screen, Moonlight shows only the second
  screen, Deck touch drives the stylus, quitting restores configs and stops sway/Sunshine.
- Latency budget: the extra compositing hop must stay under roughly one frame on the TV path.

## Out of scope for v1

RetroArch DS support, audio on the second stream, HDR, and any write to the read-only rootfs.
