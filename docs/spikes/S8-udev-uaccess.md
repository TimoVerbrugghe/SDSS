# S8 — udev `uaccess` for Sunshine's virtual input devices

Run: 2026-08-16 on the Steam Machine (<steam-machine>), SteamOS,
kernel `6.16.12-valve24.5-1-neptune-616`.

This spike exists to justify the match conditions in
[`packaging/60-sdss-input.rules`](../../packaging/60-sdss-input.rules). A wrong match here
fails in one of two silent ways: too narrow and touch input never works at all, too broad
and an unrelated USB device is handed to the desktop user.

## Verdict

| Question | Result |
| --- | --- |
| Do Sunshine-style uinput nodes get `uaccess` without our rule? | **No** — mode 0660 root:input |
| Does the rule grant the desktop user an ACL? | **PASS** |
| Is `DRIVERS=="uinput"` a valid narrowing? | **NO — would break the rule entirely** |
| Is `DEVPATH=="/devices/virtual/*"` a valid narrowing? | **PASS**, with a negative control |

## The device tree

A uinput node created with the name `Touch passthrough`:

```
looking at device '/devices/virtual/input/input32/event20':
looking at parent device '/devices/virtual/input/input32':
    KERNELS=="input32"
    SUBSYSTEMS=="input"
    DRIVERS==""
    ATTRS{name}=="Touch passthrough"
```

**`DRIVERS` is the empty string, not `uinput`.** The obvious-looking narrowing
`DRIVERS!="uinput", GOTO=...` would therefore skip every device and the rule would never
fire — no ACL, no touch, and nothing in any log to explain why. This was drafted and
discarded on the strength of this output.

What *is* distinctive is the devpath: uinput nodes live under `/devices/virtual/`, while a
physical device is under `/devices/pci*`, e.g. the Steam Controller puck:

```
looking at device '/devices/pci0000:00/.../usb1/1-3/1-3:1.2/0003:28DE:1305.0001/input/input2/event2'
```

## A/B test of the devpath match

The same rule was installed twice, differing only in the devpath condition, and a fresh
uinput node was created under each:

| Rule condition | `CURRENT_TAGS` | ACL for `deck` |
| --- | --- | --- |
| `DEVPATH!="/devices/virtual/*"` (candidate) | `:uaccess:seat:` | **GRANTED** |
| `DEVPATH!="/devices/pci*"` (negative control) | *(empty)* | denied |

The negative control is the point: it proves the condition actually discriminates rather
than passing everything through.

## The shipped rule, end to end

With `packaging/60-sdss-input.rules` installed verbatim, five uinput nodes were created —
Sunshine's four names plus a decoy:

```
Touch passthrough                GRANTED   /dev/input/event20
Pen passthrough                  GRANTED   /dev/input/event21
Mouse passthrough                denied    /dev/input/event22
Mouse passthrough (absolute)     GRANTED   /dev/input/event23
Some Other Device                denied    /dev/input/event24
```

`Mouse passthrough` (event22) is the *relative* pointer, which the bridge never attaches
to (S5). It was granted in an earlier run of this spike, when the rule still listed all
four names; it was then dropped from the rule so the ACL surface matches `TOUCH_DEVICES`
exactly. A test asserts the two lists stay in step.

and the ACL is the real thing, not just a tag:

```
crw-rw----+ 1 root input 13, 84 /dev/input/event20
user::rw-
user:deck:rw-
group::rw-
```

`sdss-inputd` then opens and `EVIOCGRAB`s it unprivileged — see
[S5-input-grab.md](S5-input-grab.md).

## Why the rule is needed at all

Without it the node is `0660 root:input` with no ACL. SteamOS's stock rules only add
`uaccess` to devices logind already recognises as seat keyboard/mouse devices
(`/usr/lib/udev/rules.d/73-seat-late.rules` runs the `uaccess` builtin for things already
tagged), and Sunshine's touch/pen/absolute nodes are not among them.

## Reproducing

```sh
sudo cp packaging/60-sdss-input.rules /etc/udev/rules.d/
sudo udevadm control --reload
python3 - <<'PY'
from evdev import UInput, AbsInfo, ecodes as e
import time
cap = {e.EV_KEY: [e.BTN_TOUCH],
       e.EV_ABS: [(e.ABS_X, AbsInfo(0, 0, 1919, 0, 0, 0)),
                  (e.ABS_Y, AbsInfo(0, 0, 1079, 0, 0, 0))]}
ui = UInput(cap, name="Touch passthrough", version=0x3)
time.sleep(60)
PY
# in another shell:
getfacl -p /dev/input/event20
```

Note that `UInput.device` is `None` when the rule has *not* granted access — evdev cannot
reopen its own node. That is a useful quick signal, but resolve the path from
`/sys/devices/virtual/input/input*/event*` rather than relying on it.
