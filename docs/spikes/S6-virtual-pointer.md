# S6 — wlr virtual pointer bound to an output drives the headless window

Run: 2026-08-16 on the Steam Machine (<steam-machine>), inside
`localhost/sdss-compositor:latest` (the only place `pywayland` exists — the SteamOS host
deliberately has no third-party Python).

## Verdict

| Question | Result |
| --- | --- |
| Does nested sway come up headless and expose `zwlr_virtual_pointer_manager_v1`? | **PASS** |
| Can `VirtualPointer` bind to a named output and inject motion/buttons? | **PASS** |
| Does an unknown output name fail with a useful message? | **PASS** |
| Is `dispatch(block=False)` enough to service the display fd? | **NO** — see below |
| Does the process exit cleanly? | **NO** — segfault, now fixed |

## Nested sway, headless

```
WLR_BACKENDS=headless WLR_LIBINPUT_NO_DEVICES=1 sway -c /dev/null
```

comes up on its own socket and advertises:

```
  global: wl_output v4
  global: wl_seat v9
  global: zwlr_virtual_pointer_manager_v1 v2
VIRTUAL_POINTER_AVAILABLE: True
```

The default headless output is named `HEADLESS-1`, matching the architecture invariant in
[../PLAN.md](../PLAN.md). Two errors in `sway.log` are expected in this environment and
harmless: `drmGetDevices2 failed` (no GPU node passed in) and a missing `swaybg`.

## Binding and injecting

Driving the real `VirtualPointer` from `runtime/inputd/sdss_inputd.py`:

```
bound VirtualPointer to HEADLESS-1
display fd: 3
motion + button round trip OK
```

and the error path, which matters because the output name is user-visible configuration:

```
RuntimeError: output 'NOPE-9' not found (have: HEADLESS-1)
```

## Finding 1 — `dispatch(block=False)` never reads the fd

`pywayland`'s own docstring, read out of the container image:

> If block is `False`, it does not attempt to read the display fd or event queue and
> simply returns zero if the queue is empty.

That is `wl_display_dispatch_pending`. Since the bridge's poll loop registers the display
fd for `POLLIN`, dispatching without reading would leave the fd permanently readable and
spin the loop at 100% CPU. `Display.read()` is what drains it, so `VirtualPointer.dispatch()`
calls `read()` and *then* `dispatch(block=False)`.

Confirmed present in the image: `get_fd: True | read: True | flush: True`.

## Finding 2 — the daemon segfaulted on every clean exit

Letting the interpreter garbage-collect the `Display` frees the proxies and the connection
in an order libwayland does not accept. Isolated with a single-variable A/B:

```
== without disconnect ==
reached end of script
exit=139                      <- SIGSEGV, core dumped
== with disconnect ==
disconnected explicitly
reached end of script
exit=0
```

`sdss-inputd` shuts down through `Bridge.close()` on SIGTERM, so this fired on every
normal stop — a core dump each time, and any exit-status supervision would have seen a
crash rather than a clean stop.

Fixed by adding `VirtualPointer.close()` (explicit `display.disconnect()` after dropping
the proxy references) and calling it from `Bridge.close()` **last**, since `_drop()`
releases held buttons *through* the pointer and would otherwise lose those events. After
the fix:

```
bridge closed cleanly, twice
exit=0
```

Covered by `TestPointerTeardown` in `host/tests/test_inputd.py`, including the ordering
(`button(False)` strictly before `close`) and idempotency.

## Finding 3 — output binding needs manager version 2

`create_virtual_pointer_with_output` is a **v2** request. Sending it to a v1
`zwlr_virtual_pointer_manager_v1` is a protocol error, and Wayland reports those
*asynchronously* — so it does not raise at the call. It surfaces some time later as a bare
`wayland display error; stopping`, with nothing pointing at the real cause (a compositor too
old to bind a pointer to a specific output). Since binding to `HEADLESS-1` is the entire
point of this daemon, there is no fallback worth having; `VirtualPointer.__init__` checks
the bound version up front and fails with a message that names the requirement.

Worth knowing when reviewing: this class of bug is invisible to a test that calls the guard
directly. It only fails if the test drives the real `__init__` against a fake v1 manager and
asserts the v2 request is never sent.

## Reproducing

```sh
podman run --rm --entrypoint bash \
  -v /path/to/sdss_inputd.py:/tmp/sdss_inputd.py:ro \
  -e XDG_RUNTIME_DIR=/tmp/xdg localhost/sdss-compositor:latest -c '
mkdir -p /tmp/xdg && chmod 700 /tmp/xdg
export WLR_BACKENDS=headless WLR_LIBINPUT_NO_DEVICES=1
sway -c /dev/null >/tmp/sway.log 2>&1 & sleep 5
export WAYLAND_DISPLAY=$(ls /tmp/xdg | grep -m1 "^wayland-[0-9]*$")
python3 -c "
import sys; sys.path.insert(0, \"/tmp\")
import sdss_inputd as m
p = m.VirtualPointer(\"HEADLESS-1\")
p.motion(960, 1920, 540, 1080); p.button(True); p.dispatch()
p.button(False); p.close()
"; echo "exit=$?"'
```

## Not covered

Injecting into a sway that is itself nested inside the gamescope session (S1b) still needs
a connected display; this spike ran sway standalone in the container. The mapping of Deck
touch coordinates onto the emulator's second window is exercised by
[S5-input-grab.md](S5-input-grab.md) and the unit tests, not here.
