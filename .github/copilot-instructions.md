# SDSS — agent instructions

SDSS turns a Steam Deck into the second screen (DS/3DS bottom screen, Wii U GamePad) for
emulators running on a Steam Machine in the gamescope session.

Read [docs/PLAN.md](../docs/PLAN.md) before changing architecture, and
[docs/hardware-recon.md](../docs/hardware-recon.md) before assuming anything about the
target system — it holds verified facts gathered from the real devices.

## Architecture invariants

Do not break these without updating `docs/PLAN.md` first:

1. The emulator runs inside a **nested sway**. The main output connects back to whatever
   `runtime.parent_display()` picks: `X11-1` via `WLR_BACKENDS=x11,headless` against
   gamescope's per-game Xwayland `DISPLAY` when Steam provides one (this is what
   steamcompmgr actually watches to dismiss the loading spinner — a window on the shared
   `gamescope-0` Wayland socket renders fine but is invisible to that check), else `WL-1`
   via `WLR_BACKENDS=wayland,headless` for native/off-Steam testing. `HEADLESS-1` is the
   streamed second screen either way.
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
  `python-evdev` / `pywayland` are allowed **only** inside the touch bridge
  (`runtime/inputd/sdss_inputd.py`), which runs inside the container image where those
  packages are installed — never in the `sdss` package.
- Tests use `unittest` (`python3 -m unittest discover -s host/tests`). No pytest.
- Every shell entry point runs under `set -euo pipefail` (`packaging/common.sh` is the one
  exception — it is sourced, so it inherits its caller's). Two consequences bite
  constantly, and both have already shipped as bugs here:
  - `producer | grep -q x` **fails** when the producer is slow. `grep -q` exits on the
    first match, the producer gets `SIGPIPE`, and `pipefail` surfaces `141`. Capture first
    (`out="$(producer)"`), then match with `<<<"$out"`.
  - `read -r -p ... answer` returns non-zero at EOF, so any prompt in a non-interactive
    run (piped installer, CI, `ssh` without a tty) aborts the script instead of taking the
    default. Always write `read -r -p "..." answer || answer=""`.
  `bash -n` over every tracked script runs in CI, but it only catches syntax — neither of
  the above is a syntax error.
- Emulator knowledge belongs in `host/src/sdss/profiles.py` as data, never as branching
  logic scattered through the session code.
- Anything learned from the real hardware goes into `docs/hardware-recon.md` or
  `docs/spikes/`, with the exact command that produced it.
- **Steam's files are foreign formats we only ever round-trip.** `shortcuts.vdf` and the
  controller templates are parsed by hand (`deck/vdf.py`), so anything the tokenizer does
  not model is silently dropped on write — a template that merely *looks* fine can lose a
  binding. Text VDF carries `[$OSTYPE]`-style conditional suffixes after a value and after
  a block's closing brace, and those are load-bearing to Steam; a conditional used to be
  mistaken for the next key, shifting every following pair. Note the parser normalises
  whitespace, so a re-emitted file is *not* byte-identical to Steam's original — assert
  **stability** instead (`dumps(loads(dumps(x))) == dumps(x)`) plus the specific suffixes,
  which is what `host/tests/test_controller_template.py` does. Raise `VdfError` on anything
  unmodelled rather than guessing.
- `plugin/package-lock.json` is **git-ignored on purpose**. A lockfile generated here
  records an internal Microsoft npm proxy (`pkgs.visualstudio.com`) in every `resolved`
  URL, which leaks infrastructure and is unresolvable from a public CI runner. Do not
  commit one. Reproducibility comes from `plugin/package.json` pinning **exact** versions
  instead of caret ranges — keep it that way, or the CI bundle check starts failing on
  unrelated upstream releases. Building locally (`SDSS_REBUILD_PLUGIN=1`) works fine
  through the proxy; the committed `plugin/dist/index.js` is what SteamOS actually uses.

## Working with the hardware

Both devices are reachable over SSH on the LAN (user `deck`). There is no SSH helper or
host-address file in the repo — use plain `ssh`, and never commit passwords or addresses.

This applies to **docs and spike write-ups too**, which is where it has actually leaked:
paste real command output and the LAN IPs, hostnames and Steam IDs come with it. Redact to
`<steam-machine>` / `<deck>` / `<steam-id>` when writing a spike up. Grep before committing:
`grep -rnE '([0-9]{1,3}\.){3}[0-9]{1,3}' --exclude-dir=.git .`

The gamescope session's Wayland socket is `gamescope-0` and `XDG_RUNTIME_DIR=/run/user/1000`.
An SSH shell has no session env — source `/run/user/1000/gamescope-environment` first.

## Style

- Prefer small, data-driven modules over frameworks.
- Comments explain *why*, not *what*, and only when the code cannot show it.
- Do not add documentation files unless asked.

## Reviewing changes here

Most of this codebase is not observable from a Mac or a CI runner: the touch bridge needs
Sunshine's uinput devices, the compositor needs a display, and Steam's files are only
meaningful to Steam. That makes "the tests pass" a weak signal by default. Two habits
compensate:

- **Check that a test would fail without the fix.** Revert the change (or mutate the line)
  and re-run. Several fixes here initially passed a suite that never exercised them — one
  guard was tested directly but never at its call site, so deleting the call changed
  nothing. If a mutation is invisible, the test is asserting the wrong thing.
- **Reproduce a claimed bug before fixing it**, and re-check it after. `pipefail`/`SIGPIPE`,
  `read` at EOF, and `.gitignore` negation rules all behave differently from how they read.

Note for Macs: `sys.pycache_prefix` may be preset system-wide to
`~/Library/Caches/com.apple.python`, so stale bytecode survives deleting local
`__pycache__` and can produce failures that contradict the source. Run `python3 -B` when a
result stops making sense. Local `python3` is often 3.9 while the project targets 3.11+;
CI is the authority.
