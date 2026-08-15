# S1–S4 — nested compositor, host clients, capture

Run: 2026-08-15, against the Steam Machine (10.10.10.161) in **Desktop Mode**
(Plasma/X11 — the gamescope session was not running, see "Not yet verified").

## Verdict

| Spike | Question | Result |
| --- | --- | --- |
| S1a | Can a nested wlroots compositor provide two outputs at once? | **PASS** |
| S1b | Can it attach to the *gamescope* session as a Wayland client? | **not tested** — needs Game Mode |
| S2a | Can the headless output be captured? | **PASS** (grim) |
| S2b | Sunshine `capture = wlr` against `HEADLESS-1` | **not tested** |
| S3a | Do host clients render into the containerized compositor? | **PASS** (native AppImage) |
| S3b | Does a Flatpak emulator render into it? | **FAIL so far** — melonDS connects but maps no window |
| S4 | Emulator second-window identity | **partial** — main window identity captured for Azahar |

## Compositor delivery

SteamOS has no sway, no compiler, and a read-only rootfs, so the compositor ships as a
container image built with the podman that SteamOS already includes
(`runtime/Containerfile`, `runtime/build.sh`). Nothing is installed outside `$HOME`.

Two problems had to be solved:

1. **`exec /usr/bin/sway: Operation not permitted`** — Arch's sway binary carries
   `cap_sys_nice=ep`, and rootless podman refuses to exec a file with capabilities.
   Fixed by `setcap -r /usr/bin/sway` in the image; a nested compositor does not need
   realtime scheduling.

   ```
   podman run --rm --entrypoint /usr/bin/getcap localhost/sdss-compositor:latest -v /usr/bin/sway
   → /usr/bin/sway cap_sys_nice=ep
   ```

2. The compositor runs in the container but the **emulator runs on the host**. sway cannot
   exec host binaries, so `sdss` starts the emulator itself once the compositor's socket
   exists. This changed the design: the sway config no longer contains the launch command.

## S1a — two outputs (PASS)

```
podman run -d --name sdss-s1 --userns=keep-id --network=host --ipc=host \
  --volume=/run/user/1000:/run/user/1000 --volume=/home/deck:/home/deck \
  --device=/dev/dri --env=XDG_RUNTIME_DIR=/run/user/1000 --env=HOME=/home/deck \
  --env=WLR_BACKENDS=headless --env=WLR_HEADLESS_OUTPUTS=2 --env=WLR_NO_HARDWARE_CURSORS=1 \
  localhost/sdss-compositor:latest -c ~/sdss-spikes/s1b.conf
```

```
HEADLESS-2 {'x': 1920, 'y': 0, 'width': 1280, 'height': 800} True
HEADLESS-1 {'x': 0, 'y': 0, 'width': 1920, 'height': 1080} True
```

sway 1.12 honours the `output ... mode ... position ...` lines from a generated config, and
each output gets its own workspace. `swaymsg` must be invoked with an explicit socket —
`podman exec` does not inherit `SWAYSOCK`:

```
podman exec --env SWAYSOCK=$(ls -t /run/user/1000/sway-ipc.* | head -1) sdss-s1 swaymsg -t get_tree
```

## S2a — capture (PASS)

`grim -o HEADLESS-2 h2.png` produced a valid 1280x800 PNG of the headless output, proving
wlroots screencopy works against an output that has no physical display. This is the same
protocol Sunshine's `capture = wlr` uses.

## S3a — host clients (PASS)

A native AppImage on the host maps into the containerized compositor with nothing but
`WAYLAND_DISPLAY` pointing at the shared socket:

```
setsid env WAYLAND_DISPLAY=wayland-1 XDG_RUNTIME_DIR=/run/user/1000 \
  QT_QPA_PLATFORM=wayland ~/Applications/azahar.AppImage
→ VIEW app_id='org.azahar_emu.Azahar' class=None title='Azahar 2126.0'
```

A client started *inside* the container (`foot`) also maps, which is what isolated the
problem to client delivery rather than the compositor.

Cemu's AppImage bundles GTK **without a Wayland backend**:

```
Error: Unable to initialize GTK+, is DISPLAY set properly?
```

so Cemu needs Xwayland. sway's Xwayland failed to start in the container for two reasons:
the abstract socket `@/tmp/.X11-unix/X0` collides with the host's Plasma X server (shared
because of `--network=host`), and bind-mounting the host `/tmp/.X11-unix` makes wlroots
refuse it (`not owned by root or us`). **Open item** — give the container a private
`/tmp/.X11-unix` it owns and expose the resulting display to host clients.

## S3b — Flatpak clients (FAIL so far)

melonDS connects to the compositor socket, runs the ROM, and creates an OpenGL context, but
never maps a window:

```
melonDS 1.1 / MP comm init OK / Created a OpenGL context / FW: WIFI CRC16 = GOOD
```

`flatpak run --socket=wayland` alone binds the wrong display name. Adding explicit access
did not fix the mapping either:

```
flatpak run --socket=wayland --filesystem=xdg-run/wayland-1 --env=WAYLAND_DISPLAY=wayland-1 \
  net.kuribo64.melonDS <rom>
```

The `--filesystem=xdg-run/<socket>` form is required and is now implemented in
`sdss.launch`; the remaining mapping failure is unexplained and still open.

Useful side effect: quitting melonDS migrated its config, so
`~/.var/app/net.kuribo64.melonDS/config/melonDS/melonDS.toml` now exists and the melonDS
profile has a real file to patch.

## Not yet verified

- Nested sway as a client of the **gamescope** session (needs Game Mode; the machine was in
  Desktop Mode and there was no `gamescope-0` socket in `/run/user/1000`).
- Sunshine capturing `HEADLESS-1` and a Moonlight client connecting.
- Any emulator's **second** window: Cemu needs Xwayland, Azahar needs a 3DS ROM (none on the
  device), melonDS does not map at all yet.
- Touch injection (S5, S6).
