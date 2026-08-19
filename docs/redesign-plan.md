# SDSS — redesign plan

Companion to [docs/architecture.md](architecture.md), which lays out the current shape and
the hardware evidence for why it destabilizes Steam. This document proposes what to change.

## The goal, verbatim

Three requirements, stated by the project owner, are the fixed target for this redesign —
everything below is judged against them, not against "does the crash stop happening today":

1. Seamless toggle between SDSS mode and non-SDSS mode via the Decky plugin.
2. In SDSS mode, launching any ROM shows the second screen on the Deck with touch and
   gamepad controls.
3. Outside of the second-screen behavior, the experience must be identical to running
   without SDSS at all.

Goal 3 is the one the current architecture violates hardest, per
[docs/architecture.md §2.1](architecture.md#21-what-sdss-adds-enumerated): eleven processes
spun up and torn down, signal-killed, on every single launch. It's also — per the evidence
in that document — the most likely source of the recurring Steam crash. Fixing goal 3
and fixing the crash point at the same change.

## Design principle

> Move everything that doesn't have to happen per-launch out of the per-launch path.

Concretely: the compositor, Sunshine, and the touch bridge do not need to be created and
destroyed for every game. They need to exist for exactly as long as SDSS mode is toggled
on — which is a Decky plugin action, already the seam goal 1 names. Only the emulator itself
needs to start and stop per launch, exactly like it does without SDSS.

This isn't a new mechanism invented for this document — it's the same seam the config
journal already uses successfully
([docs/architecture.md §2.3](architecture.md#23-whats-already-scoped-correctly-the-config-journal)).
Config patches moved from "every session" to "the Decky toggle" earlier in this project and
that fixed a real class of bugs (drift across long-running SDSS sessions, restores firing on
every Exit Game). The process lifecycle needs the same move.

## Target architecture

```mermaid
flowchart TD
    subgraph "Decky toggle ON  (starts once, lives until toggle OFF)"
        Sway["sway + its own Xwayland\n(persistent second-screen compositor)"]
        Sunshine["Sunshine\n(captures HEADLESS-1)"]
        InputD["sdss-inputd"]
        Sway --> Headless["HEADLESS-1 (1280x800)"]
        Sunshine -->|RTSP| Deck[Deck / Moonlight]
        InputD --> Headless
    end

    subgraph "Every Steam launch (thin, unchanged from vanilla otherwise)"
        Steam[Steam] --> Reaper[reaper] --> Wrapper["wrapper\n(hooks.py, thinner)"]
        Wrapper -->|"exec, LD_PRELOAD untouched"| Emulator["Cemu/Azahar\n(host process)"]
    end

    Emulator -->|"connects to the ALREADY-RUNNING\nsecond-screen compositor"| Sway
    Sway -.->|"one-time per launch: point its\nmain output at the new per-game Xwayland"| GamescopeXwayland["gamescope's per-game Xwayland\n(new every launch, not SDSS's doing)"]
```

The per-launch wrapper's job shrinks to: confirm the persistent service is up, tell it which
profile is launching (so it applies the right window-routing rules), and exec the real
emulator command essentially unchanged. No `podman run`, no fresh container, no fresh sway
process, no fresh Xwayland, no cgroup-parent nesting *per launch*. Those still exist, but
they're set up once per Decky toggle, not once per game.

### Why this targets the crash directly

[docs/architecture.md §3.4](architecture.md#34-leading-hypothesis-evidence-backed-not-proven)
identifies the strongest correlate found on hardware: Steam's overlay/session bookkeeping
staying healthy for exactly one compositor "generation" per Steam boot, and breaking when
that generation is torn down (mostly by signal) and a fresh one takes its place for the next
launch. A persistent compositor has, at most, **one** generation for the entire time SDSS is
toggled on — spanning any number of game launches — which is exactly the condition under
which every documented incident *didn't* happen. This is offered as a strong, evidence-aligned
bet, not a proven fix; §4 below defines how to prove it on hardware before relying on it.

## The central open question

Gamescope dismisses a launched game's loading spinner by walking **Steam's own per-launch
cgroup scope** for a client with a mapped window
([`own_cgroup()`](/Users/timo/Projects/SDSS/host/src/sdss/runtime.py:408), verified on
hardware). The X11 client gamescope is actually watching is **sway itself** — sway is what
connects to gamescope's per-game Xwayland as an X11 client
([`environment()`](/Users/timo/Projects/SDSS/host/src/sdss/compositor.py:146)); the emulator
never touches that display directly. If sway becomes long-lived and stops being recreated
inside each launch's own cgroup scope, does the spinner still dismiss?

This has to be answered on hardware before committing to the persistent-compositor design,
because getting it wrong silently reintroduces the exact spinner-hang bug that was already
found and fixed once this project. Two candidate answers, both requiring a hardware spike
(§4, Phase 1) rather than a guess:

- **Option A — move the process, not the display connection.** A running process's cgroup
  membership can change at runtime (write its PID into a new `cgroup.procs`). If sway (kept
  alive across launches) is moved into each new launch's Steam-assigned scope at the moment
  a new game starts, and wlroots' X11 backend can be told to reconnect its main output to a
  *new* `DISPLAY` value without restarting the process, this preserves the spinner mechanism
  exactly while keeping the compositor persistent. **Unverified**: whether wlroots' X11
  backend supports live output reconnection to a different X11 display at all — this may
  require sway/wlroots source inspection or an upstream feature that doesn't exist.
- **Option B — a thin per-launch relay, persistent compositor behind it.** Keep a
  lightweight, per-launch-scoped process whose only job is to be the X11 client gamescope's
  spinner check sees (satisfying the cgroup-walk requirement cheaply), while the actual
  compositor content is produced by the persistent sway and mirrored/relayed to it. More
  moving parts than Option A, but doesn't depend on wlroots supporting live reconnection.
  **Unverified**: what relay mechanism (if any) can forward composited output between two
  Wayland/X11 servers with acceptable latency for a game's primary display.

If hardware testing shows neither is feasible without disproportionate complexity, the
fallback is Phase 0 below — it does not depend on resolving this question and is worth doing
regardless of which way this resolves.

## Phased plan

### Phase 0 — immediate, low-risk hardening (do this regardless of Phase 1/2 outcome)

Targets the same evidence without waiting on the open question above:

1. **Graceful teardown before force.** [`_terminate_process`](/Users/timo/Projects/SDSS/host/src/sdss/session.py:168)
   already does SIGTERM-then-SIGKILL, but sway's shutdown on SIGTERM has never been verified
   to cleanly close its X11 connection to gamescope's per-game Xwayland before exiting.
   Verify this specifically (does wlroots flush/close the X11 backend connection on SIGTERM,
   or does the process just die mid-connection?); if it doesn't, send sway a mechanism that
   does (e.g. an IPC command to close outputs before signaling the process).
2. **Explicit settle/drain window between sessions.** Add a brief, verified-not-just-slept
   wait after teardown completes and before the next session's compositor starts — poll for
   the old X11 connection actually being gone (e.g. `xdotool`/`xwininfo` against gamescope's
   per-game Xwayland showing no stale client) rather than assuming process-exit timing is
   sufficient.
3. **Drop `--ipc=host`.** No comment in [runtime.py](/Users/timo/Projects/SDSS/host/src/sdss/runtime.py:457)
   justifies it, unlike every other namespace-sharing flag on that `podman run` line. Test
   removing it; if nothing breaks (compositor start, Sunshine capture, audio still routing to
   the TV via Pipewire, which passes file descriptors over its own protocol rather than
   relying on shared IPC namespaces), it comes out. If something *does* depend on it, that
   dependency gets its own comment explaining what and why, matching every other flag on that
   line.
4. **Re-run the exact alternating-cycle test** from
   [hardware-test-report.md](hardware-test-report.md:538) — Cemu, Cemu, Azahar, Cemu,
   Azahar, at least 5 cycles — after 1–3, and record whether the "first launch clean, second
   fatal" correlation from [docs/architecture.md §3.3](architecture.md#33-fresh-reproduction-2026-08-19-it-is-not-about-which-emulator-or-whether-its-overlay-is-even-enabled)
   still reproduces. This is the acceptance gate for Phase 0 — if it's gone, Phase 1/2 may
   not be needed at all; document that outcome either way rather than assuming.

### Phase 1 — feasibility spike for the persistent compositor

Answer the open question in isolation, without touching the shipped session lifecycle:

1. Spike sway/wlroots' ability to reconnect a running compositor's X11 output to a different
   `DISPLAY` at runtime (Option A). Check upstream wlroots issues/source before assuming;
   this is a fact to discover, not a design decision to make blind.
2. In parallel or as a fallback, spike whether moving a running process's cgroup membership
   at runtime actually satisfies gamescope's spinner-dismissal walk (independent of the
   output-reconnection question — this part can be tested today with a synthetic process).
3. If both fail, evaluate Option B's relay approach for latency/complexity before ruling out
   the persistent-compositor direction entirely; if that also fails, stop here — Phase 0
   remains the shipped mitigation, and this document's "necessary vs incidental" framing
   still stands as the record of what SDSS *should* look like if a future spike succeeds.

### Phase 2 — build the persistent second-screen service (contingent on Phase 1)

1. A long-lived service (systemd `--user` unit is the natural fit, matching how
   `sdss-memory-monitor.service` was already run as a transient unit successfully during this
   investigation) owns sway, Sunshine, and `sdss-inputd` for the entire time SDSS is enabled.
2. `sdss enable`/`disable` (already the Decky-driven seam, per
   [`cmd_enable`](/Users/timo/Projects/SDSS/host/src/sdss/cli.py:182)) starts/stops this
   service in addition to its existing config-journal and wrapper-reconciliation work. This
   is an additive change to an already-correct seam, not a new one — goal 1 is satisfied by
   construction.
3. Decide what the second screen shows when SDSS is on but no emulator is running (idle
   state) — a blank output, a static "waiting for a game" scene, or the last frame. Needs a
   product decision, not just an engineering one.
4. The per-launch wrapper (`hooks.py`-generated script → `sdss run`) shrinks to: confirm the
   service is healthy (a socket/health probe, not "assume it's there"), apply the Option
   A/B mechanism from Phase 1 to attach this launch's game to the persistent compositor, and
   exec the emulator. If the service is unexpectedly down, decide whether to start it
   on-demand (slower, but self-healing) or fail loudly rather than silently launching without
   a second screen.
5. Window-routing rules are already profile-specific data
   ([profiles.py](/Users/timo/Projects/SDSS/host/src/sdss/profiles.py:116)); with a
   persistent sway, these become a `swaymsg`-driven reload against the running compositor
   instead of baked into a config file at process start. Confirm sway supports reloading
   `for_window`/`assign` criteria without restarting (it does, via `swaymsg reload`) so this
   is a config regen + reload, not new infrastructure.

### Phase 3 — shrink the wrapper further

Once Phase 2 is live, re-examine every remaining per-launch deviation from vanilla against
goal 3:
- `DISABLE_GAMESCOPE_WSI=1` and the `LD_PRELOAD` save/restore dance around the emulator
  ([launch.py](/Users/timo/Projects/SDSS/host/src/sdss/launch.py)) still apply per-launch
  and should stay — they're targeted, verified fixes for the emulator's *own* process, not
  architectural overhead.
- The AppImage shadow-wrapper mechanism ([hooks.py](/Users/timo/Projects/SDSS/host/src/sdss/hooks.py))
  stays; it's how goal 1's "seamless" requirement is met for a normal Steam Library launch,
  and it's already minimal — one exec hop, no polling, no state left behind when SDSS is off.
- Re-audit `_start_emulator`'s environment build for anything that's a leftover assumption
  from the per-launch-compositor world (e.g. `WAYLAND_DISPLAY`/`DISPLAY` values computed
  fresh each session) and adjust it to address the persistent service instead.

## What does not change

- The config-patch journal ([managed_config.py](/Users/timo/Projects/SDSS/host/src/sdss/managed_config.py),
  [patch.py](/Users/timo/Projects/SDSS/host/src/sdss/patch.py)) is already scoped to the
  Decky toggle, not the game session. Nothing in this plan touches it.
  [docs/architecture.md §2.3](architecture.md#23-whats-already-scoped-correctly-the-config-journal)
  documents why it's already correct.
- Profile data staying data-only ([profiles.py](/Users/timo/Projects/SDSS/host/src/sdss/profiles.py))
  — the persistent compositor's window-routing reload in Phase 2 still reads from the same
  `Profile` dataclasses; only *how* the config reaches sway (reload vs. process start)
  changes.
- Touch routing ([`sdss-inputd`](/Users/timo/Projects/SDSS/runtime/inputd/sdss_inputd.py))
  keeps its job unchanged — it moves from "restarted per launch" to "long-lived," but its
  `EVIOCGRAB`/rescale/inject logic is unaffected.
- The read-only rootfs / `$HOME`-only constraint, and every other invariant in
  [docs/PLAN.md](PLAN.md), still hold.

## Acceptance criteria, tied back to the three goals

1. **Seamless toggle**: `sdss enable`/`disable` from Decky starts/stops the persistent
   service with no user-visible delay beyond what starting Sunshine/sway already takes
   today; toggling off leaves zero SDSS processes running, same as today's `sdss restore`.
2. **Second screen on any ROM launch**: unchanged behavior from the user's perspective —
   launch any profiled emulator, second screen appears with touch + gamepad. This is the
   regression risk of the whole plan; the alternating-cycle test from Phase 0.4 is the gate,
   re-run after Phase 2 lands.
3. **Identical outside the second screen**: measured directly against
   [docs/architecture.md §2.1](architecture.md#21-what-sdss-adds-enumerated)'s table — after
   Phase 2/3, the per-launch process count should drop from eleven entries to essentially
   two (the wrapper hop, the emulator itself), with the compositor/Sunshine/input-bridge
   trio no longer appearing in that table's "started by" column as "every launch."

Both architecture.md's evidence and this plan's phasing should be revisited once Phase 0's
re-run (or Phase 1's spike) produces a result — this is a living pair of documents, not a
one-time write-up.
