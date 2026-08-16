# S5 — `EVIOCGRAB` hides Sunshine virtual input from gamescope

Run: 2026-08-16 on the Steam Machine (<steam-machine>), SteamOS,
kernel `6.16.12-valve24.5-1-neptune-616`.

Sunshine was not streaming during this spike. The devices under test were created with
`uinput` using Sunshine's exact device names and an absolute `ABS_X`/`ABS_Y` axis pair,
which is what the bridge keys off — see [S8-udev-uaccess.md](S8-udev-uaccess.md) for the
evidence that those synthetic nodes are indistinguishable from Sunshine's at the udev
level.

## Verdict

| Question | Result |
| --- | --- |
| Does `EVIOCGRAB` hide events from a second reader? | **PASS** |
| Does the bridge attach to the right subset of devices? | **PASS** |
| Is a re-plugged device detectable? | **PASS** — see below, this found a real bug |

## `EVIOCGRAB` is exclusive

Two `InputDevice` handles were opened on the same node — standing in for `sdss-inputd`
and gamescope — and only the first called `grab()`.

```
events seen by GRABBER: 4
events seen by OTHER READER: 0 (0 => EVIOCGRAB hides input)
after ungrab, other reader sees: 2
```

The ungrab line is the control: the second reader is healthy and *does* receive events
once the grab is released, so the 0 above is the grab working, not a broken test.

## Device selection

Running the real `Bridge.discover()` from `runtime/inputd/sdss_inputd.py` against four
uinput nodes carrying Sunshine's names:

```
INFO:sdss-inputd:attached to Touch passthrough (/dev/input/event20)
INFO:sdss-inputd:attached to Pen passthrough (/dev/input/event21)
INFO:sdss-inputd:attached to Mouse passthrough (absolute) (/dev/input/event23)
discovered: [('/dev/input/event20', 'Touch passthrough'),
             ('/dev/input/event21', 'Pen passthrough'),
             ('/dev/input/event23', 'Mouse passthrough (absolute)')]
```

`Mouse passthrough` (event22) is correctly **not** attached: it is the relative pointer
and has no `ABS_X`/`ABS_Y`, so there is nothing to map onto the output.

## Re-plug detection — the finding

A Sunshine stream restart destroys and recreates the uinput nodes. The spike simulated
that by closing the `UInput` and immediately creating a new one with the same name:

```
stream 1 node: /dev/input/event20
stream 2 node: /dev/input/event20 (same path reused: True )
old fd poll flags: 24 POLLHUP set: True
identity changed: (3412, 1578) -> (3412, 1582) => True
```

Three facts, all of which the implementation depends on:

1. **The kernel reuses the same `/dev/input/eventN` path.** Identity by path alone leaves
   the bridge holding a dead fd that never becomes readable again — touch silently stops
   working after the first stream restart.
2. **`st_rdev` is unchanged (3412) but `st_ino` changes (1578 → 1582).** Identity must be
   the `(st_rdev, st_ino)` pair; either half alone is insufficient.
3. **The stale fd reports flags 24 = `POLLERR|POLLHUP`, not `POLLIN`.** So the poll loop
   has to treat a hangup as "drop and rescan"; waiting for readability would hang forever.

Reproduce with the script in the session log; the essential part is:

```python
p = select.poll(); p.register(dev.fileno(), select.POLLIN)
print(p.poll(500))          # [(fd, 24)] after the node is recreated
os.stat(path).st_ino        # differs from the value captured at attach time
```

## Not covered

Injection into sway via `zwlr_virtual_pointer_manager_v1` is [S6](S6-virtual-pointer.md),
which passed the same day; `pywayland` is deliberately absent from the host and only
present inside the container image, so that spike runs inside `podman`.

The Deck-side half of the chain — Moonlight delivering touch to Sunshine at all — is
covered by `../hardware-recon.md` ("Second screen, end to end"), which is where the
Steam Input `Touchscreen Native Support` requirement is recorded.
