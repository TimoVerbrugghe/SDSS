# S4 — emulator second windows, Xwayland, and Game Mode

Run: 2026-08-15 on the Steam Machine (10.10.10.161). Follows
[S1-compositor.md](S1-compositor.md).

## Verdict

| Spike | Question | Result |
| --- | --- | --- |
| S4-azahar | Does Azahar produce a second window, and what is it called? | **PASS** |
| S4-cemu | Does Cemu produce a GamePad window, and what is it called? | **PASS** |
| S3c | Can Xwayland run in the container so Cemu works? | **PASS** |
| S1b | Nested sway as a client of the gamescope session | **BLOCKED** — no display connected |

## Azahar (3DS) — PASS

`sdss patch azahar` wrote exactly the intended values:

```
372:layout_option=4
373:layout_option\default=false
406:secondary_display_layout=2
407:secondary_display_layout\default=false
```

Launching a `.cci` from `/home/deck/Emulation/roms/n3ds` produced two windows:

```
[HEADLESS-2] app_id='org.azahar_emu.Azahar' title='Azahar 2126.0 | Animal Crossing: New Leaf | Secondary Window'
[HEADLESS-1] app_id='org.azahar_emu.Azahar' title='Azahar 2126.0 | Animal Crossing: New Leaf'
```

Matcher: `app_id="^org.azahar_emu.Azahar$" title="Secondary Window$"`. Both windows share
the `app_id`, so the title is what discriminates — matching on `app_id` alone would drag the
main window onto the second screen too.

Azahar is a **native Wayland** client; no Xwayland needed.

## Cemu (Wii U) — PASS, but requires Xwayland

`sdss patch cemu` set `<open_pad>true</open_pad>`, and launching a `.wua` produced:

```
[HEADLESS-2] class='Cemu' instance='AppRun.wrapped' title='GamePad View - FPS: 60.10'
[HEADLESS-2] class='Cemu' instance='AppRun.wrapped' title='Cemu 2.6 - FPS: 60.10 [Vulkan] [Generic] [TitleId: ...] NES REMIX PACK [US v2]'
```

Matcher: `class="^Cemu$" title="^GamePad View"` (Xwayland views expose `class`, not `app_id`).

### Cemu needs X11 — confirmed three ways

1. **Upstream**: [cemu-project/Cemu#1809](https://github.com/cemu-project/Cemu/issues/1809)
   — "Appimage using XWayland instead of native Wayland". Setting `GDK_BACKEND=wayland` did
   not help the reporter either. The `linux-bin-x64` build and the third-party
   [pkgforge Cemu-AppImage-Enhanced](https://github.com/pkgforge-dev/Cemu-AppImage-Enhanced)
   *do* open native Wayland windows.
2. **On device**: with no `DISPLAY` and `GDK_BACKEND=wayland`, Cemu 2.6 dies at startup:

   ```
   Error: Unable to initialize GTK+, is DISPLAY set properly?
   ```

3. **Binary inspection**: the AppImage links `libwayland-client/egl/cursor` *and* `libX11`,
   and its bundled `libgdk-3.so.0` does contain Wayland symbols (110 of them) — so the
   backend is compiled in but unusable in practice. The failure is not a missing library.

Conclusion: keep `needs_x11 = True` for Cemu. Switching to the pkgforge AppImage would
remove the Xwayland dependency and is worth revisiting if Xwayland causes trouble.

## Xwayland in the container — PASS

Two problems, both solved:

- **Abstract socket collision.** `--network=host` shares the abstract socket namespace, so
  `@/tmp/.X11-unix/X0` is already taken by the desktop's X server. wlroots simply moves to
  the next free number (`:1` here).
- **Socket directory ownership.** wlroots refuses a `/tmp/.X11-unix` it does not own
  (`not owned by root or us`), which rules out bind-mounting the host's root-owned one.
  Fix: mount a directory owned by `deck`:

  ```
  --volume=$HOME/.local/share/sdss/x11:/tmp/.X11-unix
  ```

Host X11 clients then reach the nested Xwayland over the **abstract** socket by setting
`DISPLAY=:1`, even though the filesystem socket lives outside the host's `/tmp/.X11-unix`.
`sdss` discovers the number from the compositor's env dump rather than guessing.

## Game Mode (S1b) — BLOCKED

`steamos-session-select gamescope` needed two workarounds — a stuck
`plasma-powerdevil.service` start job had to be cancelled first — but the session still
could not start:

```
drm: Connectors:
drm:   HDMI-A-1 (disconnected)
drm:   DP-1 (disconnected)
drm: cannot find any connected connector!
drm: Failed to find a primary plane
Failed to create backend.
gamescope-session.service: Main process exited, code=dumped, status=11/SEGV
```

`/sys/class/drm/card0-*/status` confirms both connectors are disconnected. **gamescope
requires a physically connected display** and segfaults without one, so it crash-looped.

The machine was returned to a clean state:

```
steamosctl set-default-login-mode desktop
steamosctl set-default-desktop-session plasmax11.desktop
steamosctl switch-to-desktop-mode plasmax11.desktop
```

Note that `steamos-session-select gamescope` **persists** the default login mode, so it must
be reset explicitly — switching back is not symmetric.

To finish S1b, connect the TV (or a dummy HDMI plug) and re-run with
`WLR_BACKENDS=wayland,headless` and `WAYLAND_DISPLAY=gamescope-0`.
