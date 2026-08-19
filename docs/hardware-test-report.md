# SDSS Hardware Test Report

## Executive summary

At the beginning of the hardware testing, SDSS worked end to end:

- The emulator ran on the Steam Machine TV.
- The Deck connected through Moonlight.
- The Deck showed the emulator's second screen.
- Touch and gamepad input reached the emulator.
- Azahar was confirmed working through its Steam shortcut.

The later failures were caused by several different issues that appeared at
different times. Some were SDSS bugs, some were deployment mistakes, and one
was a recovery mistake during testing. They made the system look unreliable
because a failure in one layer often prevented the next layer from starting.

The important conclusion is that the original working design was not lost.
The main failures were identified and fixes were added and tested. The current
working tree also contains the fix for the latest squashed second screen.
The latest complete release has now been deployed to both devices and tested
again through Steam Game Mode.

## How SDSS works, in simple terms

The Steam Machine runs the emulator inside a small, private display server.
That display server has two screens:

1. One screen goes back to gamescope and appears on the TV.
2. The other screen is a virtual `1280x800` screen.

Sunshine captures only the virtual screen. Moonlight on the Deck displays that
capture. A small input bridge takes Deck touch events and sends them to the
emulator's second window.

Steam must launch the emulator. Steam supplies a special per-game display that
gamescope watches. Starting the emulator directly over SSH is useful for
diagnostics, but it is not a valid end-to-end Steam test.

## What worked first

The first successful path was:

- Deploy SDSS to the Steam Machine and the Deck.
- Pair Moonlight with the Steam Machine.
- Launch Azahar through its generated Steam shortcut.
- Stream the `Second Screen` application to the Deck.

This proved that the overall architecture was sound. It also proved that
Sunshine capture, Moonlight streaming, Deck touch input, and the nested
compositor could work together.

## Why the first Cemu failures happened

Cemu is different from Azahar. Cemu 2.6 needs X11 through nested Xwayland,
whereas Azahar uses Wayland directly. That exposed problems that Azahar did
not exercise.

### 1. A partial deployment mixed incompatible files

At one point only some Python files were copied into the installed release.
The installed release was from a different branch and had a different
`Profile` definition. The new session code expected `second_size`, but the
old profile code did not provide it.

The result was a Python exception during a Steam launch. Because Steam
swallows most application output, this looked like a game or Steam crash.

**Fix:** deployment instructions now require replacing the complete
`host/src/sdss/` package, clearing `__pycache__`, and importing the installed
package before launching.

When installing on the Deck, Steam must be fully stopped first. If Steam is
running, the installer pauses before updating the generated shortcut and can
wait indefinitely for interactive confirmation. Switch the Deck to Desktop
Mode, run `steam -shutdown`, verify that no `steam` process remains, and only
then run `install.sh --role steam-deck`.

### 2. Xwayland could not use the container's X11 socket

The nested compositor runs in a rootless container. The host X11 socket
directory is root-owned, so the container could see the directory but could not
use it correctly. Cemu therefore received no usable X11 display and showed a
black screen.

**Fix:** SDSS now creates a user-owned X11 bridge. The bridge points to the
real host sockets, while the real socket directory is mounted separately for
the compositor. This keeps the SteamOS system untouched and works with the
rootless container.

After this fix, Cemu and its GamePad View were observed rendering correctly:
the main window appeared on the TV and the second window appeared on
`HEADLESS-1`.

### 3. Xwayland started too late

Sway was configured to start Xwayland lazily. SDSS captured its environment
before Xwayland had allocated a display number, so Cemu saw an empty
`DISPLAY`.

**Fix:** Xwayland is now forced to start during compositor startup, and SDSS
waits for the required display information instead of launching Cemu with a
blank display.

### 4. A crashed container was left behind

When a previous run died badly, Podman retained a broken container/conmon
record. `podman run --replace` did not reliably recover from that state. The
new compositor died almost immediately, so SDSS reported that no Wayland
socket appeared.

**Fix:** SDSS removes the named compositor container before starting a new
one, and also removes it during normal cleanup.

## Why Steam itself appeared to crash

There were two related but separate cleanup problems.

### Gamescope's Vulkan layer affected Cemu in an earlier failure

The gamescope session exports a Vulkan integration layer. That layer is useful
for normal games, but it also loaded inside SDSS's nested compositor. Cemu
could render briefly, then crash while tearing down or rebuilding its Vulkan
swapchain. This explains an earlier class of Cemu failures, but it is not the
root cause of the latest Steam UI failure described below.

**Fix:** SDSS explicitly disables the gamescope WSI layer for its processes.
The setting is also passed through Flatpak because Flatpak does not inherit all
host environment changes automatically.

### Cleanup only stopped the direct child process

SDSS started Podman, but Podman started other processes such as conmon,
fuse-overlayfs, and Xwayland. Stopping only the direct SDSS child left those
processes alive inside Steam's game scope.

Steam then believed the old game was still running and showed messages such as
“Wind Waker is already running”. In some tests, the remaining processes also
interfered with the next launch.

**Fix:** the compositor now has a known container name, and cleanup asks
Podman to remove that container so its descendants are removed too.

### The latest Cemu launch exhausted Steam's memory

The latest incident has a clearer cause than the earlier Vulkan hypothesis.
Cemu started successfully at `17:42:14`, and the Deck second screen continued
showing Cemu after the Steam UI disappeared. At `17:43:08`, Steam's own log
reported:

```text
OUT OF MEMORY! attempted allocation size: 65
cannot allocate memory for thread-local data: ABORT
```

The Steam launcher service then stopped the old Steam processes and restarted
Steam. Its systemd record shows a `6.8G` memory peak. The Cemu/SDSS processes
were in a separate Steam application scope, so they survived the Steam client
restart. That is why this looked like Cemu was still running in the
background: it really was, while Steam had already been restarted.

This means the latest failure was not caused by the `1280x800` output or by
Cemu crashing first. The exact reason Steam reached that memory peak is still
unknown; the evidence only proves that Steam aborted from memory exhaustion
during Cemu startup. The next test should record per-process memory before and
after launch and avoid treating a surviving application scope as proof that
Steam is healthy.

### Follow-up: stale compositor resources amplified the pressure

The post-restart process audit found several older SDSS application scopes still
owned by the user manager. They contained orphaned `fuse-overlayfs` mounts and
nested Xwayland servers on displays `:2` through `:5`; the active session was
separate and still had its expected emulator, Sway, Sunshine, input bridge, and
Podman processes. The active slice used about `1.38G`, while the stale scopes
retained more than `1G` combined. This does not prove that the stale scopes
alone triggered Steam's allocator abort, but it provides a concrete contributor
and explains why Steam could restart while the second screen remained visible.

Cleanup now reaps SDSS's rootless overlay helpers and nested Xwayland servers
after container removal. The matcher requires the Podman storage path for
`fuse-overlayfs` and the nested Xwayland `-rootless -wm` signature, leaving
gamescope's primary `:0` and `:1` Xwaylands untouched. The host suite covers
both positive matches and those protected gamescope/unrelated cases.

## Why later launches appeared to do nothing

During teardown testing, Steam-managed processes were killed directly. That
left Steam's internal launch state inconsistent: the Steam client remained
partly alive, but its main loop stalled and orphaned helper processes remained.
The visible symptoms included:

- A permanent “Delaying launch (0%)”.
- A spinner followed by a return to the Steam UI.
- A shortcut that appeared to do nothing.
- “Game already running” even after the window was gone.

This was not evidence that every SDSS launch was broken. Steam itself had
become wedged.

**Recovery:** restarting the gamescope session restored Steam. The known-good
Azahar Steam URL then launched again, proving that the Steam launch path and
SDSS could recover.

**New rule:** stop games through Steam whenever possible. Do not use broad
`pkill` commands on Steam-managed process trees.

## Why the second screen became squashed

The emulator profiles contain their own native second-screen sizes, for
example:

- Azahar: `320x240`
- Cemu: `854x480`
- melonDS: `256x192`

Those values describe the emulator window, not the Deck display. A regression
used the emulator size for `HEADLESS-1`. Azahar consequently produced:

```text
output HEADLESS-1 mode 320x240@60Hz
```

Moonlight then scaled that small image to the Deck's `1280x800` panel, causing
the visible squashing.

**Fix:** `HEADLESS-1` is now always `1280x800@60Hz`, independently of the
emulator's native window size. The generated configuration was verified on
hardware, and the full host test suite passed with 233 tests.

## What was changed

The current working changes include:

- Full-package deployment and stale-bytecode guidance.
- The reliable Steam shortcut launch procedure.
- Safer compositor cleanup and stale-container removal.
- A user-owned X11 bridge for Cemu.
- Forced Xwayland startup and display readiness checks.
- Gamescope WSI opt-out for SDSS processes and Flatpak.
- The fixed `1280x800` Deck output.
- Regression tests for the above behavior.
- Hardware findings in `docs/hardware-recon.md`.

## Current status

### Confirmed

- The architecture works end to end.
- Azahar worked through Steam and streamed its second screen.
- Deck touch and gamepad input worked in the verified session.
- Cemu rendered after the X11 socket ownership fix.
- The latest release was deployed to both the Steam Machine and the Deck.
- A controlled Steam-launched Azahar session was followed by a
  Steam-launched Cemu Wind Waker session. During a later verification, the
  Deck shortcut was no longer running; that was a test-state mistake, not a
  successful active stream. It has now been relaunched through Steam and
  verified as `reaper -> sdss-connect -> moonlight stream`.
- Cemu remained running on the Steam Machine; after relaunching the Deck
  shortcut, Moonlight is again connected to its second screen. The Steam
  Machine had about 10 GiB available memory and no new out-of-memory event.
- The deployed lifecycle files on both devices match the working tree.
- The latest Steam UI failure was confirmed as a Steam out-of-memory abort;
  Cemu and SDSS survived in their separate application scope.
- Steam recovered after restarting gamescope.
- The local implementation generates `HEADLESS-1` at `1280x800`.
- All 233 host tests pass.

### Not yet confirmed

- A usable `gamescopectl` screenshot could not be captured during this run:
  Gamescope reported that it had run out of screenshot images. This is a
  Gamescope capture limitation, not evidence that the second screen was absent.
- Steam's `steam://closeapp` request did not stop the Azahar wrapper and its
  descendants in this test, so direct targeted cleanup was required before
  launching Cemu. The Steam-native teardown path remains an open issue.

### Post-fix lifecycle validation

The parent-death teardown fix was redeployed to Fremont and Galileo, followed
by a targeted helper-reaping fix. The failure was narrower than the emulator
process itself: after the Steam launcher wrapper disappeared, Podman could
already have lost its container record while `conmon` and the container's
`sdss_inputd` process remained alive. Those two processes were enough to leave
the second screen connected and make the next Steam launch look like a
concurrent session.

The session also watches the Steam launcher lineage. Steam can terminate its
reaper while EmuDeck's shell wrapper remains alive; the direct parent-death
signal does not cover that case. The lineage watcher raises the same
interrupt when the original reaper disappears. `runtime.remove_container()`
then scans `/proc` after Podman cleanup and force-terminates only the named
`sdss-compositor` `conmon` helper and the SDSS input bridge. This covers both
normal cleanup and the stale-container state observed on hardware.

Validation after redeployment:

- Azahar launch, screenshot, Steam-launcher teardown: no SDSS artifacts.
- Cemu launch, screenshot, Steam-launcher teardown: no SDSS artifacts.
- Azahar/Cemu/Azahar repeated alternating cycles: no artifacts after each
  teardown.
- Killing the Steam reaper alone after a fresh Cemu launch: the lineage
  watchdog completed teardown with no artifacts.
- Steam remained running throughout (`steam` PID 605270).
- The host suite passed: 239 tests.

The final process check was empty for `azahar`, `cemu`, `sdss_inputd`,
`conmon`, `sway`, `sunshine`, and `podman`, and the Deck had no lingering
`sdss-connect` or Moonlight process once the stream ended.

### Final alternating-cycle validation and Deck launch message

The Deck Flatpak cleanup fix was then deployed and exercised across the
correct Steam workflow: launch an emulator through its Steam shortcut, launch
the Deck Second Screen shortcut through Steam Game Mode, capture both outputs,
and exit through the Steam UI. The completed cycles were:

- Azahar / A Link Between Worlds
- Cemu / Wind Waker HD
- Azahar / Animal Crossing New Leaf
- Cemu / Twilight Princess HD

The completed cycles produced usable host and Deck screenshots and ended with
no emulator, SDSS, compositor, Sunshine, Podman, `conmon`, `sdss_inputd`,
`sdss-connect`, or Moonlight artifacts. The Twilight Princess launch was
verified from its process arguments rather than inferred from the shortcut
name. An earlier test accidentally launched an Eden/Switch `.nsp`; it was
stopped with targeted cleanup and was not counted as a Cemu cycle.

The later Deck message, “An error occurred while launching this game: Game
configuration unavailable”, was not reproduced as a launch failure in the
available Steam logs. At `20:47:09–20:47:10`, Steam recorded:

```text
ExecuteSteamURL: "steam://rungameid/13044482501723029504"
Loaded Config ... sdss_second_screen.vdf
Game process added ... sdss-connect <steam-machine>
LaunchApp changed task to Completed
```

The resulting process chain was `reaper -> sdss-connect -> Flatpak bwrap ->
Moonlight`, with the expected `1280x800` stream options. No error, failure,
configuration-unavailable, or timeout entry occurred in that timestamp
window. Therefore the message is currently classified as a transient Steam
UI/configuration-state warning (or a separate attempt without a matching
timestamp), not evidence that the deployed SDSS launch failed. A definitive
UI diagnosis requires the exact time of a future reproduction.

### Reproduced Steam UI abort at 18:15 on 2026-08-18

The next resilience attempt reproduced the same failure while launching
Majora's Mask 3D (Azahar) through Steam. The fresh Steam log contains:

```text
***** OUT OF MEMORY! attempted allocation size: 65 ****
cannot allocate memory for thread-local data: ABORT
Startup - Steam Client launched with: ... -steamdeck ... -gamepadui
```

The important state at capture time was that the previous Wind Waker HD Cemu
process had **not** been torn down. It was an orphaned process owned by the
gamescope session, alongside the newly launched Azahar process:

```text
Cemu.AppImage ... Wind The Wind Waker HD (EU).wua   RSS 1,156 MiB
azahar.AppImage ... Majora's Mask 3D ...            RSS 1,169 MiB
steamwebhelper                                             RSS 456 MiB
```

Azahar also had its SDSS compositor, Sunshine, and wrapper processes alive.
Steam restarted at `18:15:27`, one second after the abort at `18:15:26`;
the Azahar/SDSS scope survived. This makes the immediate trigger
**concurrent emulator processes after failed Steam-native teardown**, rather
than a new display-mode regression. It also explains why this failure appears
SDSS-specific: SDSS keeps the emulator session and streamed second screen
alive when Steam's close request fails, allowing the next Steam launch to
accumulate another large emulator/compositor workload.

The Cemu process was subsequently identified by its standalone process group
(`PGID 253428`) and must be stopped with targeted process-group cleanup before
the next alternating-launch run. Do not interpret a Steam UI restart as proof
that the emulator or SDSS session stopped.

The SDSS launch path is therefore working again on real hardware. The next
engineering task is specifically Steam shortcut teardown/reaping, not another
display or compositor redesign.

### Cleanup compatibility check

The compositor cleanup hardening initially used `podman kill --ignore`, but the
SteamOS Podman version supports `--ignore` for `rm`, not for `kill`. That made
the new kill step a no-op and left conmon behind. The command now omits
`--ignore` on `kill` and keeps it only on `rm --force --ignore`, so a missing
container is a non-fatal result there. The host suite passes with this
correction, and a fresh Azahar launch produced a valid `1920x1080` gamescope
screenshot before the test session was cleaned manually.

`remove_container()` no longer relies on that ignore semantics as the sole
safety net, though: it unconditionally calls `reap_orphaned_helpers()`
afterwards (added during the lineage-watchdog hardening below), which walks
`/proc` and SIGKILLs any `conmon`/`sdss_inputd` process still tagged with the
container name — even if Podman itself has already lost/forgotten the
container record. So "ignoring" a missing container no longer means "assume
nothing is left running"; it means Podman's own bookkeeping isn't trusted as
the last word, and a process-table sweep backstops it.

## Latest deployment verification

The current working tree was deployed to the reachable Steam Machine
(`<steam-machine>`) and Steam Deck (`<deck>`). The host installation rebuilt
`localhost/sdss-compositor:latest` successfully and installed the current
runtime, including the corrected Podman cleanup and orphan-helper reaper. The
Deck installation updated `sdss-connect`, Moonlight integration, the Steam
shortcut, and the touch controller template.

A fresh Animal Crossing New Leaf launch through the Steam Machine shortcut
created the expected Azahar, SDSS compositor, Sway, Sunshine, Podman, conmon,
and nested Xwayland chain. A host `gamescopectl` screenshot was captured. With
that host session active, the Deck client connected and a second-screen
`gamescopectl` screenshot was captured. After teardown, a delayed audit found no
Azahar, Cemu, SDSS compositor, conmon, fuse-overlayfs, nested Xwayland,
Sunshine, Sway, or Podman container artifacts on the host, and no
`sdss-connect` or Moonlight artifacts on the Deck.

The SSH-launched Steam URL handler did not provide a reliable substitute for
the physical Game Mode “Exit Game” interaction in this run: Steam logged the
shortcut request, but the Deck process exited before a stable Moonlight
process was observable. The direct Deck client connection succeeded and was
used only to validate the stream path. Therefore this run is deployment and
cleanup evidence, not a replacement for a user-operated Game Mode
launch/exit cycle. The remaining acceptance test is to repeat the alternating
Azahar/Cemu cycles with the physical Steam UI and record the corresponding
Steam “Game process removed” events.

### AppImage mount cleanup follow-up

The post-test audit found one ownerless mount at
`/tmp/.mount_Cemu.AAOpDFl`, sourced from `Cemu.AppImage.sdss-real`. There was
no Cemu process, SDSS process, container, or process reference to the mount;
`fuser` reported only the kernel mount. This was residue from an earlier
interrupted Cemu session, not from the immediately preceding Azahar teardown.
It was safely removed with `fusermount3 -u`.

The cleanup gap is now fixed in `runtime.reap_orphaned_helpers()`. It parses
mountinfo and lazily unmounts only temporary AppImage mounts whose source ends
in `.AppImage.sdss-real`, so unrelated user FUSE mounts are not touched. The
reaper runs from session cleanup even when native sway is used.

The fix was deployed to the Steam Machine and verified with a fresh Steam
launch of Wind Waker HD (Cemu). The live mount appeared during gameplay.
After stopping the Steam reaper, the mount, compositor, Sunshine, Cemu,
conmon, and fuse-overlayfs were gone at 8 seconds; the remaining
`cemu.sh`/`rclone` save-sync wrapper also exited by 20 seconds. A second
20-second audit found no AppImage mount, SDSS process, or Podman container.

### Azahar Steam crash and overlay follow-up

Animal Crossing: New Leaf reproduced two linked failures on Azahar's native
Wayland path. Azahar exited with a fatal Wayland protocol error:

```text
wl_display#1: error 1: invalid method 1, object wl_registry#33
The Wayland connection experienced a fatal error: Invalid argument
```

Steam then aborted 13 seconds later with its recurring 32-bit allocator
failure, despite 12 GiB of system memory remaining:

```text
OUT OF MEMORY! attempted allocation size: 65
cannot allocate memory for thread-local data: ABORT
```

This rules out a kernel OOM or GPU reset. The EmuDeck shader-cache warning is
also not the primary cause: Azahar's shader files were normal-sized, and in an
earlier reproduction Azahar continued running after Steam had already died.

Azahar now uses the nested Xwayland path. Hardware verification with the same
ROM confirmed:

- `DISPLAY=:2`, `QT_QPA_PLATFORM=xcb`, and `GDK_BACKEND=x11`;
- the main window on `X11-1` at 2560x1440;
- the secondary window on `HEADLESS-1` at 1280x800;
- Sunshine capturing `HEADLESS-1` and the touch bridge attaching;
- Steam's overlay renderer and Vulkan overlay layer mapped into Azahar;
- the physical Steam button opening the overlay;
- the same Steam PID remaining healthy for more than five minutes, with no
  Wayland protocol or allocator failure.

The X11 window class is `Azahar`, not `org.azahar_emu.Azahar`, so the profile
matches both identifiers. The parent-lineage watcher also tracks the actual
Steam process in addition to its reaper, ensuring a future Steam restart
signals the SDSS coordinator and enters normal cleanup instead of stranding
the emulator scope.

### Root cause and architecture correction

Controlled A/B testing isolated two independent sources of Steam instability:

1. Every SDSS helper inherited Steam's `gameoverlayrenderer.so`. With SDSS
   enabled, Cemu drove the Steam launcher service to roughly 2 GiB within
   seconds and Steam aborted around 51 seconds. The same ROM with SDSS disabled
   remained below 1 GiB and exited normally.
2. After helper preload isolation, Vulkan Azahar on nested Xwayland still made
   Steam grow by roughly 430–480 MiB every five seconds. Stock Azahar was
   stable, and nested Azahar became stable when SDSS temporarily selected
   OpenGL. `gameoverlayrenderer.so` remained loaded, while
   `steamoverlayvulkanlayer.so` did not.

The wrapper now saves Steam's preload, removes it before starting the SDSS
coordinator, and restores it only for the emulator. Nested sway, Podman, and
Sunshine therefore never register as overlay clients. Azahar's profile uses a
journaled OpenGL override while SDSS is enabled.

Teardown exposed two additional lifecycle races:

- Rootless Podman creates `catatonit -P` and records it in
  `$XDG_RUNTIME_DIR/libpod/tmp/pause.pid`. When parented to Steam's reaper it
  keeps Exit Game pending after every other SDSS process has gone. Cleanup now
  requires both that authoritative PID and current launch ancestry before
  killing it.
- Steam can deliver SIGINT and then SIGTERM during one teardown. The second
  signal used to interrupt cleanup. Signals are now idempotent and the
  parent-watch thread is disarmed before teardown.

The emulator-config branch (`origin/copilot/update-emulator-configs`) also
contained the selective journal design that this branch had lost. It is now
integrated and moved to the correct ownership boundary:

- enabling SDSS snapshots and applies only profile-declared keys;
- each profile has a persistent `enabled-<profile>` journal;
- game sessions own processes only and never restore configs from Exit Game;
- disabling selectively restores managed keys in the current file, preserving
  unrelated edits;
- checksum-verified full backups remain the fallback for missing/corrupt files
  and legacy session journals.

### Alternating-session acceptance

After deploying the corrected architecture, one uninterrupted Steam client
completed this sequence:

1. Wind Waker HD (Cemu): overlay opened; Exit Game completed.
2. Twilight Princess HD (Cemu): held beyond the old crash interval; overlay
   opened; Exit Game completed.
3. Animal Crossing: New Leaf (Azahar/OpenGL): held beyond the old Vulkan leak
   interval; overlay opened; Exit Game completed.

Steam's PID remained unchanged and its service stayed around 0.7–1.1 GiB.
Every app scope was empty after each exit. Managed config values and their
per-profile journals remained active across game exits. A master disable then
restored Cemu `open_pad=false` and Azahar `graphics_api=2`, removed the
journals, and a re-enable reapplied `open_pad=true` and `graphics_api=1`.
