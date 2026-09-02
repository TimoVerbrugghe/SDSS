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
5. Emulator config edits always go through the journal in `host/src/sdss/patch.py`.
   Enabled profiles own only their declared keys: disabling selectively restores those keys
   while preserving unrelated edits. Every journal also retains a checksum-verified full
   backup for recovery. Never write an emulator config without that journal.

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

### Launching Steam shortcuts over SSH

In **Steam Game Mode**, every end-to-end test must start from the Deck's generated
Steam shortcut (`Second Screen`) in the Steam UI. Do not launch
`sdss-connect` directly over SSH and call that an end-to-end test: it bypasses
Steam's game scope and is only valid for pairing or isolated Moonlight
diagnostics. An SSH-triggered Steam URL is the automation equivalent of
selecting the shortcut, but it must be sent to the already-running Steam client
with the live gamescope environment sourced.

Before testing on the Deck, verify that Game Mode is actually active:

```
ps -eo args | grep '[s]team .*-steamdeck.*-gamepadui'
loginctl show-session "$(loginctl list-sessions --no-legend | awk '$3=="deck" {print $1; exit}')" -p Type -p State
```

The expected session type is `wayland`, and Steam must include both
`-steamdeck` and `-gamepadui`. Never use `nohup`, `setsid`, or a direct
`sdss-connect <host>` launch to simulate the Game Mode shortcut.

Launch non-Steam shortcuts with the **64-bit gameid**, not the signed 32-bit appid, and
source the live gamescope environment first:

```
export XDG_RUNTIME_DIR=/run/user/1000
set -a; source /run/user/1000/gamescope-environment; set +a
/home/deck/.local/share/Steam/steam.sh "steam://rungameid/<64-bit-gameid>"
```

The known-good SDSS launch path is this exact command; bare `steam <url>` or a launch
without the sourced environment silently does nothing. Current shortcut IDs include:
Azahar `13816419520748716032`, Cemu Wind Waker HD `15768959151553642496`, and the
Deck Second Screen shortcut `13044482501723029504`.

After launching, verify that Steam owns the process before judging the test:

```
ps -eo pid,ppid,args | grep -E '[r]eaper SteamLaunch AppId=|[m]oonlight stream'
grep -E 'AppID 13044482501723029504' \
  ~/.local/share/Steam/logs/gameprocess_log.txt | tail
```

The Moonlight process should descend from Steam's `reaper SteamLaunch` process.
If the Deck shows no image, keep the Steam-launched session intact while
collecting Steam/Moonlight logs; do not replace it with a direct `sdss-connect`
launch, because that changes the test being diagnosed.

For a screenshot of the Steam Machine's outer display, use Gamescope's control
tool. Source the live environment first:

```
export XDG_RUNTIME_DIR=/run/user/1000
set -a; source /run/user/1000/gamescope-environment; set +a
/usr/bin/gamescopectl screenshot /home/deck/sdss-debug/steam-machine.png
```

This captures the Steam Machine display (including a spinner or visible
emulator), not the streamed `HEADLESS-1` screen.

Do not kill Steam-managed game process trees with `pkill` during teardown testing. It
can leave Steam's game state wedged while the Steam client remains alive, making later
URLs appear broken. Stop the emulator through Steam when possible. If Steam is wedged,
restart the user gamescope session, wait for the client to return, source the newly
generated environment file, and only then retry the URL.

### Deploying to a device

**Never `scp` individual changed files onto an installed release.** This has produced two
separate multi-hour debugging sessions, both of which looked like emulator bugs and were
not. The installed tree under `~/.local/share/sdss/release/` was built from *some* commit,
and that commit is usually not the branch you are on — it may contain modules your branch
does not have and lack attributes your branch's modules now require. Copying three files
into it yields a package that imports cleanly and then dies at runtime, deep inside a
Steam-launched process where the traceback is invisible. The symptom that finally exposed
it was `AttributeError: 'Profile' object has no attribute 'second_size'` — a stale
`profiles.py` under a freshly copied `session.py`.

So: **deploy the whole `host/src/sdss/` package**, then verify before launching anything.

```
scp host/src/sdss/*.py deck@<host>:~/.local/share/sdss/release/host/src/sdss/
ssh deck@<host> 'find ~/.local/share/sdss/release -name __pycache__ -type d -exec rm -rf {} +
  cd ~/.local/share/sdss/release/host/src && python3 -c "import sdss.cli"'
```

Note that `*.py` only overwrites; it never deletes. If the installed release has modules
your branch removed, they survive and may still be imported. Compare the two file lists
(`ls ... | xargs -n1 basename`) whenever the deployed version is of unknown provenance —
`~/.local/share/sdss/release/VERSION` tells you which release it came from, not which
branch. When the lists differ, re-run the installer instead of patching by hand.

Clearing `__pycache__` is not optional: SteamOS keeps writing it, and a stale `.pyc` will
happily shadow a file you just replaced.

### Finding out why a Steam-launched run failed

SDSS writes no log of its own, and an emulator started through Steam has its stderr
swallowed. When a launch "just crashes", the traceback is in the **journal**, attributed to
Steam rather than to SDSS:

```
journalctl --user --since "-6 min" | grep -iE "sdss|Traceback|Error|signal|Fatal"
```

Reach for that before reading Steam's own logs or hypothesising about the emulator. Every
"the emulator crashes" report in this project so far has turned out to be an SDSS-side
exception visible in exactly this way.

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

## Current investigation handoff

The August 2026 hardware-investigation changes are merged to `main`, but the intermittent
Steam crash is not considered fully explained. A follow-up session must read
`docs/hardware-recon.md` before proposing another experiment.

Established findings:

- The valid Game Mode path is Steam shortcut -> SDSS -> nested sway -> Sunshine/WLR capture ->
  Moonlight -> Deck. A direct `sdss-connect` invocation is not an equivalent end-to-end test.
- Sunshine's automatic gamepad probe creates and destroys multiple uinput pads on every startup.
  With a connected Moonlight client, Steam Input re-enumerates those devices and the 32-bit Steam
  client can exhaust its address space. Production config pins `gamepad = xone`; do not disable
  the controller entirely.
- The X11 backend into Steam's per-game Xwayland is a necessary observed trigger in failing runs,
  but the exact remaining intermittent failure mechanism is still open.
- The gamescope app-ID/cgroup mismatch is real but not causal by itself: it occurred on successful
  launches too. X11 root-property cleanup was also tested and disproved.
- Killing Steam-managed process trees can strand Steam's running-game bookkeeping. Stop through
  Steam, or restart the user gamescope session before treating a later launch as evidence.

Follow-up rules:

1. Keep the Steam-launched process intact while collecting logs, and identify which layer is being
   tested: Steam bookkeeping, compositor, emulator, Sunshine, Moonlight, or touch input.
2. Treat first-launch success and later-launch failure as separate observations. A new hypothesis
   must account for the timing/intermittency rather than assuming a deterministic failure.
3. Record verified commands and results in `docs/hardware-recon.md`; do not create a competing
   hardware narrative in another document.
4. Never commit real device addresses, Steam IDs, credentials, or raw SSH output. Redact them to
   `<steam-machine>`, `<deck>`, and `<steam-id>`.
5. Before changing teardown or capture code, inspect the lifecycle in
   `host/src/sdss/session.py` and `host/src/sdss/runtime.py`, and run the targeted host tests.
