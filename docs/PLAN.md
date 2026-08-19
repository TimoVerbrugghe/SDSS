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

`sdss run` starts a nested **sway** whose main output connects back to whichever surface
`runtime.parent_display()` picks — `WLR_BACKENDS=x11,headless` against gamescope's
per-game Xwayland `DISPLAY` when Steam provides one, else `WLR_BACKENDS=wayland,headless`
against the shared `gamescope-0` Wayland socket for native/off-Steam testing:

- `X11-1` (Steam launch) or `WL-1` (native/off-Steam fallback) — a window inside the
  session gamescope, fullscreen on the TV. Holds the emulator's **main** window.
  Gamescope's per-game Xwayland (`STEAM_MULTIPLE_XWAYLANDS=1`) exists specifically so
  steamcompmgr can watch one isolated display per launched app for readiness; a window
  on the shared `gamescope-0` Wayland socket renders correctly but is invisible to that
  check, so Steam's loading spinner never clears. `X11-1` must be preferred whenever
  Steam hands the process a `DISPLAY`.
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

`/dev/uinput` already carries an ACL granting `user:deck:rw`, so *creating* virtual devices
needs no root. Reading Sunshine's devices does need a udev rule, though: systemd-logind only
tags devices it recognises as a keyboard/mouse seat device with `uaccess`, so the touch, pen
and absolute-pointer nodes Sunshine creates land as `0660 root:input` with no ACL for the
desktop user, and `EVIOCGRAB` fails. `packaging/60-sdss-input.rules` tags them explicitly;
it is installed once, with sudo, by `packaging/install-host.sh`. `sdss-inputd` itself still
runs unprivileged. The rule matches on device name, `KERNEL=="event[0-9]*"` and
`DEVPATH=="/devices/virtual/*"` — note that `DRIVERS=="uinput"` looks right but never
matches, because the parent input device reports an empty `DRIVERS`; see
[S8](spikes/S8-udev-uaccess.md) for the A/B test behind both conditions.

Rejected alternative: sway's own `input <id> map_to_output HEADLESS-1` would do the mapping
for free, but only for devices sway owns through a libinput backend. Adding libinput to a
nested sway would make it open *every* device and fight gamescope for them.

### The frame rule

Everything the bridge emits is decided while decoding a packet but **emitted at
`SYN_REPORT`**, in a fixed order: `release` (when a handoff moved the pointer) → `motion` →
`release` → `press`. This is not stylistic. evdev delivers a whole gesture as one packet —
`ABS_X`, `ABS_Y`, `BTN_TOUCH`, `SYN_REPORT` all arrive together — so acting on a button the
moment it is decoded uses the *previous* frame's coordinates. The visible symptom is a tap
landing wherever the cursor happened to be, which looks like a rescaling bug and is not one.

So: **never emit to the pointer from inside the per-event decode loop.** Set a `pending_*`
flag and let the `SYN_REPORT` branch emit it. A code review of this file should treat a new
`self.pointer.<x>()` call in the decode path as a defect unless it is one of the two
existing non-gesture exits — `_release()` on device teardown, and the emergency release on
`SYN_DROPPED` — both of which drop a stuck button rather than describe a gesture. The
ordering within the branch matters too: a lift and a re-touch can share one frame, and
emitting the press before the release leaves the pointer released at the end of it.

Related invariants in the same loop, each of which has already been a bug:

- Multitouch positions are recorded for **every** slot, not just the primary one; a
  promoted finger needs its own coordinates, and pressing before it has reported any fires
  the tap at the departing finger's location.
- Slot state must be dropped on lift (`_forget_slot`). The kernel reuses slot indices, so a
  stale entry is later adopted verbatim by whatever finger takes that index.
- `SYN_DROPPED` means the rest of the packet is a truncated fragment: discard through the
  next `SYN_REPORT`, then re-read the device state. Applying it is worse than dropping it.
- `motion_absolute` must be followed by `frame()`. Without it the pointer is completely
  dead while every other assertion still passes.
- Axis extents are `max - min + 1` (evdev ranges are inclusive). Using `max - min` scales
  everything slightly and makes the far edge exactly 1.0, which is outside the [0,1)
  surface range the protocol accepts — the rightmost column becomes unreachable.

Because none of this is observable without hardware, `host/tests/test_inputd.py` drives
`_handle` with scripted packets and a recording pointer. Two deliberate properties of that
harness: the X and Y test extents **differ** (1279 vs 799) so a swapped axis cannot hide
behind a shared range, and motions are asserted as raw `(x, x_extent, y, y_extent)` tuples
rather than ratios, so a wrong extent that preserves the quotient still fails.

## Emulator profiles

| Emulator | Install | Config | Second-screen edit |
| --- | --- | --- | --- |
| Cemu | `~/Applications/Cemu.AppImage` | `~/.config/Cemu/settings.xml` | `<open_pad>true</open_pad>` — **verified**, window `GamePad View` (X11 only) |
| Azahar | `~/Applications/azahar.AppImage` | `~/.config/azahar-emu/qt-config.ini` | `[Layout] layout_option=4` (SeparateWindows), `secondary_display_layout=2` (BottomScreenOnly), `[Renderer] graphics_api=1` (OpenGL while SDSS runs) — **verified**, window `… | Secondary Window`; Steam launches use nested Xwayland because native Wayland produced fatal protocol errors, while Vulkan under nested Xwayland triggers an unbounded Steam overlay memory leak |
| melonDS | Flatpak `net.kuribo64.melonDS` | `~/.var/app/net.kuribo64.melonDS/config/melonDS/melonDS.toml` | `Instance0.Window1.ScreenSizing` = bottom-only — unverified |
| RetroArch DS | — | — | **Not viable** — one framebuffer, no second toplevel |

The enabled state owns emulator configuration. `sdss enable` snapshots and applies only the
keys declared by each profile; per-profile and master disables selectively restore those keys
in the live file, preserving unrelated changes made while SDSS was enabled. Full-file,
checksum-verified backups remain available for missing/corrupt-file recovery. A game session
owns only processes and never restores configuration from Steam's fragile Exit Game path.

## Auto-launch from the Steam Library

SDSS must never edit EmuDeck's launcher scripts (`azahar.sh`, `cemu.sh`) or Steam's
`shortcuts.vdf` — both are regenerated by EmuDeck/Steam ROM Manager and would silently
drop a direct edit. Instead, each launcher script dynamically resolves the emulator
binary path at launch time (`find "$emufolder" -iname "azahar*.AppImage" | ... | tail -n1`)
and `exec`s it — that resolved *file*, never inspected by content, is the stable extension
point. `sdss enable`/`disable` swaps it for a thin wrapper (`host/src/sdss/hooks.py`) that
shadows the real AppImage/Flatpak-export symlink alongside it and calls
`sdss run --profile <id> -- <emulator-command> "$@"`; Flatpak exports use their `flatpak run`
command so sandbox access can be injected, while `disable` restores the original
file exactly. This makes a normal Steam Library launch transparently pick up the second
screen with no shortcut/script edits at all.

Known drift risk: an EmuDeck emulator version update re-downloads the AppImage under the
same resolved filename, silently overwriting the wrapper. Mitigated by making the wrapper
install idempotent and re-running it on every state read, not just on the enable/disable
transition: the Decky plugin's `get_state()` calls `sdss status --json` every time its
panel opens, and that command now reconciles every profile's wrapper against whatever
binary currently sits at the resolved path. So simply reopening the Decky panel (or
toggling the switch) after an update re-wraps the fresh binary — no systemd timer needed.

## Repo layout

```
host/       Python package `sdss`
plugin/     Decky plugin (TS frontend + Python backend shelling out to `sdss`)
deck/       Deck-side Moonlight auto-connect helper
runtime/    Build recipe for the bundled sway/wlroots compositor
packaging/  host installer, uninstaller, udev rule, desktop launcher
docs/       this plan, recon notes, spike results
```

## Phases

| Phase | Content |
| --- | --- |
| P0 | Recon and spikes (blocking) |
| P1 | `sdss run` MVP: sway dual output + Sunshine + manual launch options |
| P2 | Emulator profiles + toggle-owned selective config patch/restore |
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
| S5 | `EVIOCGRAB` hides Sunshine virtual input from gamescope | **pass** ([S5](spikes/S5-input-grab.md)) |
| S6 | wlr virtual pointer bound to an output drives the headless window | **pass** ([S6](spikes/S6-virtual-pointer.md)) |
| S7 | End-to-end stream to the Deck, and Moonlight touch mode | **pass** ([S2/S7](spikes/S2-streaming.md), [recon](hardware-recon.md)) |
| S8 | udev `uaccess` for Sunshine's virtual input devices | **pass** ([S8](spikes/S8-udev-uaccess.md)) |

`/dev/uinput` on the Steam Machine already carries an ACL granting `user:deck:rw`, so the
touch bridge does not need root. It does need a udev rule to *read* Sunshine's devices —
see "Touch" above; `packaging/60-sdss-input.rules` is installed once with sudo.

S5–S7 were re-run on the real devices on 2026-08-16 and are written up in `docs/spikes/`.
They found four things worth carrying forward:

- Sunshine's uinput nodes **reuse the same `/dev/input/eventN` path** across a stream
  restart, with `st_rdev` unchanged and only `st_ino` differing, and the stale fd reports
  `POLLHUP` rather than becoming readable. Device identity is therefore `(st_rdev, st_ino)`.
  Take that identity **before** opening the device: a restart between the two makes the
  daemon record the new device under the old identity and never notice the swap.
- `DRIVERS=="uinput"` is **not** a usable udev match — the parent input device reports
  `DRIVERS==""`. The devpath (`/devices/virtual/*`) is what distinguishes a virtual node.
- `pywayland`'s `Display` must be disconnected explicitly; letting it be garbage-collected
  segfaults the process on exit.
- `create_virtual_pointer_with_output` needs manager **version 2**; against v1 the protocol
  error arrives asynchronously and looks like an unrelated display failure. See
  [S6](spikes/S6-virtual-pointer.md).

What remains open is not a spike but the two environment-blocked items: S1b (nested sway
as a gamescope client) and S3b (Flatpak melonDS mapping no window), both of which need a
display physically connected to the Steam Machine.

Moonlight touch mode is **not** open: `docs/hardware-recon.md` records the full chain
verified end to end — 1985 injected motions from ~74 discrete touches, all normalised
inside 0..1 — once `Touchscreen Native Support` is added as an always-on command to the
shortcut's controller layout. That layout step is the load-bearing part and is what
`deck/install-controller-template.py` automates.

The compositor is delivered as a **container image** built with the podman that SteamOS
already ships (`runtime/`), so nothing is installed outside `$HOME`. Because sway runs in a
container it cannot exec host binaries: `sdss` starts the compositor first and then launches
the emulator on the host against the compositor's Wayland socket.

## Verification

- One `docs/spikes/SN-*.md` per spike with the exact commands and pass/fail.
- Unit tests: managed keys restore while unrelated settings survive; full-backup recovery;
  profile matching.
- Manual E2E per emulator: TV shows the main screen, Moonlight shows only the second
  screen, Deck touch drives the stylus, quitting stops sway/Sunshine, and disabling SDSS
  restores managed config keys.
- Latency budget: the extra compositing hop must stay under roughly one frame on the TV path.

## Out of scope for v1

RetroArch DS support, audio on the second stream, HDR, and any write to the read-only rootfs.
