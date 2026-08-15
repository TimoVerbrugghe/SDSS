# SDSS — agent instructions

SDSS turns a Steam Deck into the second screen (DS/3DS bottom screen, Wii U GamePad) for
emulators running on a Steam Machine in the gamescope session.

Read [docs/PLAN.md](../docs/PLAN.md) before changing architecture, and
[docs/hardware-recon.md](../docs/hardware-recon.md) before assuming anything about the
target system — it holds verified facts gathered from the real devices.

## Architecture invariants

Do not break these without updating `docs/PLAN.md` first:

1. The emulator runs inside a **nested sway** with `WLR_BACKENDS=wayland,headless`.
   `WL-1` is the TV (main screen), `HEADLESS-1` is the streamed second screen.
2. **Sunshine captures `HEADLESS-1`** via `capture = wlr`, with `stream_audio = disabled`.
   Audio stays on the TV.
3. Deck touch arrives as uinput devices created by Sunshine. `sdss-inputd` grabs them
   exclusively (`EVIOCGRAB`) so gamescope cannot see them, then re-injects into sway via
   `zwlr_virtual_pointer_manager_v1` bound to `HEADLESS-1`.
4. The SteamOS rootfs is **read-only and stays that way**. No `pacman`, no
   `steamos-readonly disable`. Everything lives under `$HOME`.
5. Emulator config edits always go through the journal in `host/src/sdss/patch.py`, so a
   restore is byte-identical. Never write a config without a backup.

## Conventions

- Host tooling is **Python 3.11+, standard library only**. No third-party runtime
  dependencies — the target is SteamOS where installing wheels is awkward.
  `python-evdev` / `pywayland` are allowed **only** inside `sdss.inputbridge`, and must be
  imported lazily so the rest of the CLI works without them.
- Tests use `unittest` (`python3 -m unittest discover -s host/tests`). No pytest.
- Emulator knowledge belongs in `host/src/sdss/profiles.py` as data, never as branching
  logic scattered through the session code.
- Anything learned from the real hardware goes into `docs/hardware-recon.md` or
  `docs/spikes/`, with the exact command that produced it.

## Working with the hardware

Both devices are reachable over SSH on the LAN (user `deck`). Host addresses and the SSH
helper live in `packaging/`; never commit passwords.

The gamescope session's Wayland socket is `gamescope-0` and `XDG_RUNTIME_DIR=/run/user/1000`.
An SSH shell has no session env — source `/run/user/1000/gamescope-environment` first.

## Style

- Prefer small, data-driven modules over frameworks.
- Comments explain *why*, not *what*, and only when the code cannot show it.
- Do not add documentation files unless asked.
