# S2 / S7 — Sunshine capture and end-to-end streaming to the Deck

Run: 2026-08-15, Steam Machine (<steam-machine>) headless + Steam Deck (<deck>).
No display was attached to the Steam Machine for any of this.

## Verdict

| Spike | Question | Result |
| --- | --- | --- |
| S2b | Sunshine `capture = wlr` against the headless output | **PASS** |
| S7a | Pair the Deck with Sunshine unattended | **PASS** |
| S7b | Stream the second screen to the Deck end to end | **PASS** |

## S2b — capture

Sunshine (Flatpak `dev.lizardbyte.app.Sunshine`) attached to the nested compositor finds
the screencopy protocol and selects the output **by name**:

```
[wayland] Found interface: zwlr_screencopy_manager_v1(30) version 3
[wlgrab] Monitor 0 is HEADLESS-2 / Monitor 1 is HEADLESS-1
[wlgrab] Selected monitor [Headless output 2] for streaming
Found H.264 encoder: h264_vaapi [vaapi]
Found HEVC encoder: hevc_vaapi [vaapi]
Found AV1 encoder:  av1_vaapi  [vaapi]
```

So `output_name = HEADLESS-1` works as written, and the AMD GPU encodes in hardware with no
display attached. `nvenc` and `vulkan` probes fail first — that is normal and harmless.

The Flatpak needs the same explicit socket exposure the emulators need:
`--socket=wayland --filesystem=xdg-run/<socket> --env=WAYLAND_DISPLAY=<socket>`.

## S7a — unattended pairing

Sunshine's `-0` flag reads the pairing PIN from stdin, so no web UI and no web credentials
are needed. `sdss` gives Sunshine a FIFO as stdin (`$XDG_RUNTIME_DIR/sdss/session/pin`).

```
# Deck
moonlight pair <steam-machine> --pin 1234
# Steam Machine
echo 1234 > $XDG_RUNTIME_DIR/sdss/session/pin
```

Afterwards the Deck lists the host's apps:

```
$ moonlight list <steam-machine>
Second Screen
```

**Moonlight's CLI accepts only a host — there is no port option.** SDSS therefore uses the
default Sunshine port (47989) instead of a private one.

## S7b — the stream

With Azahar running a 3DS ROM and the window rules applied:

```
[HEADLESS-2] 1920x1080  Azahar 2126.0 | Animal Crossing: New Leaf
[HEADLESS-1] 1280x800   Azahar 2126.0 | Animal Crossing: New Leaf | Secondary Window
```

Host side:

```
New streaming session started [active sessions: 1]
CLIENT CONNECTED
[wayland] Resolution: 1280x800
Creating encoder [hevc_vaapi]
```

Deck side: `FFmpeg-based video decoder chosen`, `Direct mapping possible`,
`Frame pacing disabled: target 90 Hz with 60 FPS stream`.

## Why early captures looked black

Worth recording, because "black frame" had **three unrelated causes** and none of them were
the missing HDMI connection:

1. **The emulator had exited.** The first host capture was taken after Azahar quit, so the
   output was genuinely empty. Always assert the process is alive before judging a frame.
2. **The content really was black.** Animal Crossing's intro renders the train on the *top*
   screen while the bottom screen stays black. Setting `secondary_display_layout=1`
   (TopScreenOnly) proved the second window renders — the title screen appeared on
   `HEADLESS-1` as a 1.09 MB PNG.
3. **The Deck's screenshot tool returns black.** `spectacle` on Plasma Wayland produced a
   byte-identical 5975-byte black PNG *with no stream running at all*. Deck-side
   screenshots prove nothing here; use host-side `grim` plus the Moonlight log instead.

Useful signal: an empty 1280x800 output is exactly 3057 bytes as PNG, and an empty
1920x1080 output is 6121 bytes. Any capture of that size is blank.

Headless operation was never the problem — a CPU/shm client (`foot`) and a GPU client
(Azahar) both rendered and captured correctly.

## Known headless-only wart

```
[wlr] [types/wlr_linux_dmabuf_v1.c:1130] Failed to get backend DRM FD
```

The headless backend has no DRM FD to advertise through `linux-dmabuf`. It did not stop GPU
clients from rendering here. In the real setup the compositor also has the *wayland* backend
(`WLR_BACKENDS=wayland,headless`), which supplies a DRM FD, so this should disappear.

## Reproduce

```
runtime/build.sh                       # once, on the Steam Machine
sdss run -- ~/Applications/azahar.AppImage <rom>
deck/install.sh                        # once, on the Deck
sdss-connect <steam-machine-ip> --pair 1234
sdss-connect <steam-machine-ip>
```
