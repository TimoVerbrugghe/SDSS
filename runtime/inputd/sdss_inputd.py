#!/usr/bin/env python
"""Route Sunshine's touch/pen input onto the streamed output.

Moonlight and Sunshine already deliver the input: while a stream is running Sunshine
creates `Touch passthrough`, `Pen passthrough` and `Mouse passthrough (absolute)` as host
uinput devices. Two things are still wrong for SDSS:

* those devices belong to the host seat, and the nested compositor has no input devices of
  its own, so the events would go to the session compositor instead;
* their absolute axes span the whole output layout, so a tap meant for the second screen
  lands on the TV.

This daemon grabs the devices exclusively, rescales their coordinates onto one output, and
replays them through `zwlr_virtual_pointer_v1`, which every emulator here accepts as a
stylus. Gamepad and keyboard are deliberately left alone — they already work.

Runs inside the compositor container, where evdev and pywayland are available.
"""

from __future__ import annotations

import argparse
import errno
import logging
import os
import signal
import time

# Names Sunshine gives the devices that carry absolute pointing input.
TOUCH_DEVICES = (
    "Touch passthrough",
    "Pen passthrough",
    "Mouse passthrough (absolute)",
)

BTN_LEFT = 0x110
BTN_TOUCH = 0x14A

log = logging.getLogger("sdss-inputd")


def wanted(device_name: str) -> bool:
    return device_name in TOUCH_DEVICES


def normalize(value: int, minimum: int, maximum: int) -> tuple[int, int]:
    """Map a raw absolute axis value to the (position, extent) pair the protocol wants.

    evdev absinfo ranges are *inclusive*, so an axis of 0..1919 has 1920 positions. Using
    `max - min` as the extent both scaled everything by 1919/1920 and let the far edge
    produce exactly 1.0, which is outside the [0,1) surface range the virtual-pointer
    protocol accepts — the rightmost column of the DS bottom screen was unreachable.
    """
    extent = maximum - minimum + 1
    if extent <= 1:
        return 0, 1
    return max(0, min(extent - 1, value - minimum)), extent


class VirtualPointer:
    """A wlr virtual pointer bound to one output."""

    def __init__(self, output_name: str) -> None:
        from pywayland.client import Display
        from pywayland.protocol.wayland import WlOutput, WlSeat
        from pywayland.protocol.wlr_virtual_pointer_unstable_v1 import (
            ZwlrVirtualPointerManagerV1,
        )

        self.display = Display()
        self.display.connect()
        registry = self.display.get_registry()

        self._manager = None
        self._manager_version = 0
        self._seat = None
        self._outputs: dict[int, object] = {}
        self._output_names: dict[int, str] = {}
        self._target = output_name

        def on_global(reg, name, interface, version):
            if interface == "zwlr_virtual_pointer_manager_v1":
                self._manager_version = min(version, 2)
                self._manager = reg.bind(
                    name, ZwlrVirtualPointerManagerV1, self._manager_version
                )
            elif interface == "wl_seat":
                self._seat = reg.bind(name, WlSeat, min(version, 5))
            elif interface == "wl_output" and version >= 4:
                output = reg.bind(name, WlOutput, 4)
                self._outputs[name] = output
                output.dispatcher["name"] = lambda o, n, key=name: self._output_names.update(
                    {key: n}
                )

        registry.dispatcher["global"] = on_global
        self.display.roundtrip()
        self.display.roundtrip()

        if self._manager is None:
            raise RuntimeError("compositor does not offer zwlr_virtual_pointer_manager_v1")
        if self._seat is None:
            # Otherwise this fails deep inside pywayland instead of saying what is wrong.
            raise RuntimeError("compositor exposes no wl_seat")
        self._require_manager_version()

        output = next(
            (self._outputs[key] for key, n in self._output_names.items() if n == output_name),
            None,
        )
        if output is None:
            known = ", ".join(self._output_names.values()) or "none"
            raise RuntimeError(f"output {output_name!r} not found (have: {known})")

        self.pointer = self._manager.create_virtual_pointer_with_output(self._seat, output)
        log.info("virtual pointer bound to %s", output_name)

    def _require_manager_version(self) -> None:
        """create_virtual_pointer_with_output is a v2 request.

        Sending it to a v1 manager is a protocol error the compositor reports
        asynchronously, so it surfaces much later as an unrelated "wayland display error;
        stopping" with no hint that the compositor is simply too old.
        """
        if self._manager_version < 2:
            raise RuntimeError(
                "zwlr_virtual_pointer_manager_v1 is version "
                f"{self._manager_version}, but binding a pointer to an output needs "
                "version 2"
            )

    def motion(self, x: int, x_extent: int, y: int, y_extent: int) -> None:
        self.pointer.motion_absolute(self._now(), x, y, x_extent, y_extent)
        self.pointer.frame()
        self.display.flush()

    def button(self, pressed: bool) -> None:
        self.pointer.button(self._now(), BTN_LEFT, 1 if pressed else 0)
        self.pointer.frame()
        self.display.flush()

    def fileno(self) -> int:
        return self.display.get_fd()

    def dispatch(self) -> None:
        """Drain the display fd so a protocol error surfaces instead of spinning silently.

        `dispatch(block=False)` alone is `wl_display_dispatch_pending`: it never touches
        the fd, so once the compositor sent something the fd would stay readable and the
        poll loop would spin at 100% CPU forever. `read()` is what actually consumes it.
        """
        self.display.read()
        self.display.dispatch(block=False)

    def close(self) -> None:
        """Tear the connection down explicitly.

        Letting the interpreter collect the Display instead segfaults on exit: the
        proxies and the connection are freed in an order libwayland does not accept, so
        the daemon dumps core on every clean shutdown. Verified in the container image —
        see docs/spikes/S6-virtual-pointer.md.
        """
        display, self.display = getattr(self, "display", None), None
        if display is None:
            return
        self.pointer = None
        self._manager = None
        self._seat = None
        for output in self._outputs.values():
            try:
                output.destroy()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                log.debug("wl_output destroy failed", exc_info=True)
        self._outputs = {}
        try:
            display.disconnect()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            log.debug("wayland disconnect failed", exc_info=True)

    @staticmethod
    def _now() -> int:
        return int(time.monotonic() * 1000) & 0xFFFFFFFF


class Bridge:
    def __init__(self, output: str, grab: bool = True) -> None:
        self.output = output
        self.grab = grab
        self.pointer = VirtualPointer(output)
        self._devices: dict[str, object] = {}
        self._states: dict[str, dict] = {}
        self._unopenable: set[str] = set()
        self._identities: dict[str, tuple[int, int, int] | None] = {}
        # Nodes we looked at and did not want, keyed by identity so a rescan does not
        # reopen every /dev/input node on every pass through the event loop.
        self._ignored: dict[str, tuple[int, int] | None] = {}
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    @staticmethod
    def _fresh_state() -> dict:
        return {
            "x": None,
            "y": None,
            "x_info": None,
            "y_info": None,
            "pressed": False,
            "pending_press": False,
            # Like pending_press, deferred to SYN_REPORT: a packet that carries the final
            # coordinates together with the lift must deliver the release at *those*
            # coordinates, not at the previous frame's.
            "pending_release": False,
            # A promotion replaces x/y with the new finger's coordinates, so the outgoing
            # contact's release must precede that frame's motion rather than follow it.
            "release_before_motion": False,
            # Set when a slot was promoted to primary before it ever reported a position.
            # The press waits for that slot's first coordinates instead of landing on the
            # departing finger's.
            "awaiting_slot_position": None,
            # SYN_DROPPED means the kernel truncated a packet: everything up to and
            # including the next SYN_REPORT is garbage and must be discarded.
            "resyncing": False,
            "tracking_id": None,
            "current_slot": 0,
            "primary_slot": None,
            "slots": {},
            # Per-slot last position, so a primary handoff can adopt the new finger's own
            # coordinates instead of the departing finger's.
            "slot_pos": {},
        }

    def discover(self) -> None:
        from evdev import InputDevice, list_devices

        for path in list_devices():
            identity = self._identity(path)
            existing = self._devices.get(path)
            if existing is not None:
                # Sunshine destroys and recreates its uinput nodes on every stream, and the
                # kernel readily reuses the same /dev/input/eventN. Skipping on path alone
                # left the bridge wired to the dead node — it never becomes readable, so the
                # ENODEV drop in run() never fires and touch was silently dead until restart.
                if identity == self._identities.get(path):
                    continue
                log.info("%s was replaced by a new device", path)
                self._drop(path)
            if identity is not None and self._ignored.get(path, "absent") == identity:
                # Already inspected this exact node and did not want it. Re-opening every
                # /dev/input node on each pass is wasted syscalls in the hot path of a
                # latency-sensitive bridge, and open/close churns evdev wakeup sources.
                # A None identity means the stat that produced it was transient/unreliable
                # (or the device just isn't stat-able yet), so it is never treated as a
                # cache hit — otherwise a device briefly unstatable while ignored could
                # never be reconsidered even once it becomes a real, different device.
                continue
            try:
                device = InputDevice(path)
            except OSError as exc:
                if path not in self._unopenable:
                    self._unopenable.add(path)
                    log.debug("cannot open %s: %s", path, exc)
                continue
            if not wanted(device.name):
                self._ignored[path] = identity
                device.close()
                continue
            if self.grab:
                try:
                    device.grab()
                except OSError as exc:
                    # Hiding it from the session compositor is the whole point. Continuing
                    # would violate the input-routing invariant because gamescope would
                    # also receive the raw events. Surface a hard failure so the supervisor
                    # can restart/report the daemon instead of silently half-working.
                    device.close()
                    raise RuntimeError(
                        f"cannot grab {device.name} ({path}): {exc}; "
                        "another process holds it, so the session compositor would also "
                        "see this device"
                    ) from exc
            log.info("attached to %s (%s)", device.name, path)
            self._devices[path] = device
            # Deliberately the identity taken *before* InputDevice(path): re-stat'ing here
            # would describe whatever node exists now, which after a teardown/recreate race
            # is a different device than the one this fd is on — the replug check would then
            # compare equal forever and discover() would never re-attach.
            self._identities[path] = identity
            self._unopenable.discard(path)
            # Sunshine creates touch, pen and absolute-mouse nodes at the same time, and
            # their events interleave. Each needs its own axis and contact state, or one
            # device's SYN flushes another's coordinates.
            self._states[path] = self._fresh_state()

    @staticmethod
    def _identity(path: str) -> tuple[int, int, int] | None:
        """Return identity fields that distinguish a recreated device node."""
        try:
            info = os.stat(path)
        except OSError:
            return None
        return info.st_rdev, info.st_ino, info.st_ctime_ns

    def _release(self, path: str) -> None:
        """Let go of a contact held by `path` so sway is never left with a latched button."""
        state = self._states.get(path)
        if state and state.get("pressed"):
            state["pressed"] = False
            log.debug("releasing held button for %s", path)
            try:
                self.pointer.button(False)
            except Exception:  # noqa: BLE001 - teardown must never mask the real error
                log.debug("could not release button for %s", path, exc_info=True)

    def _drop(self, path: str) -> None:
        device = self._devices.pop(path, None)
        # Before dropping the state: a device that disappears mid-drag would otherwise
        # leave sway holding the left button down forever, which the emulator sees as a
        # stylus permanently pressed against the screen.
        self._release(path)
        self._states.pop(path, None)
        self._identities.pop(path, None)
        if device is not None:
            log.info("detached from %s", path)
            try:
                device.close()
            except OSError:
                pass

    def close(self) -> None:
        """Release every contact and device. Safe to call more than once."""
        for path in list(self._devices):
            self._drop(path)
        # Must come last: _drop() releases held buttons through the pointer, so tearing
        # the display down first would lose those events.
        pointer = self.pointer
        if pointer is not None:
            pointer.close()

    def run(self) -> None:
        try:
            self._run()
        finally:
            # The kernel drops EVIOCGRAB when the fd closes, but a held button is ours to
            # undo — nothing else ever will.
            self.close()

    def _poll_display(self, timeout_ms: int) -> None:
        """Wait on the wayland fd alone, dispatching anything that arrives.

        Used when no input devices are attached, so the compositor connection is still
        watched while the bridge is otherwise idle.
        """
        from select import POLLIN, poll

        try:
            display_fd = self.pointer.fileno()
        except Exception:  # noqa: BLE001 - treated the same as a dispatch failure below
            log.exception("wayland display error; stopping")
            self._stop = True
            return
        poller = poll()
        poller.register(display_fd, POLLIN)
        if not poller.poll(timeout_ms):
            return
        try:
            self.pointer.dispatch()
        except Exception:  # noqa: BLE001 - same rationale as the main loop
            log.exception("wayland display error; stopping")
            self._stop = True

    def _run(self) -> None:
        from evdev import ecodes
        from select import POLLERR, POLLHUP, POLLIN, POLLNVAL, poll

        rescan = True
        while not self._stop:
            # Only rescan when there is nothing to read (or nothing attached). Rescanning
            # after every readable batch meant hundreds of full /dev/input walks a second
            # while the user was dragging.
            if rescan or not self._devices:
                self.discover()
                rescan = False
            if not self._devices:
                # Poll the display rather than sleeping: between streams (the daemon's
                # normal steady state, since Sunshine only creates the nodes while
                # streaming) a bare sleep never reads the wayland fd, so a sway restart or
                # protocol error goes unnoticed and its events pile up in the socket until
                # the user next touches the screen — which is the worst moment to die.
                self._poll_display(1000)
                rescan = True
                continue

            poller = poll()
            by_fd = {device.fileno(): device for device in self._devices.values()}
            for fd in by_fd:
                poller.register(fd, POLLIN)
            display_fd = self.pointer.fileno()
            poller.register(display_fd, POLLIN)

            ready = poller.poll(1000)
            if self._stop:
                break
            if not ready:
                rescan = True
                continue
            for fd, flags in ready:
                if fd == display_fd:
                    try:
                        self.pointer.dispatch()
                    except Exception:  # noqa: BLE001 - see below
                        # A compositor protocol error here is unrecoverable for the
                        # pointer, but exiting would leave the user with no touch at all
                        # and no message. Surface it and stop cleanly instead.
                        log.exception("wayland display error; stopping")
                        self._stop = True
                        break
                    continue
                device = by_fd.get(fd)
                if device is None:
                    continue
                if flags & (POLLERR | POLLHUP | POLLNVAL):
                    # A recreated uinput node hangs up here rather than ever becoming
                    # readable, so this is what actually notices a stream restart.
                    self._drop(device.path)
                    rescan = True
                    continue
                try:
                    events = list(device.read())
                except OSError as exc:
                    if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                        # The queue drained between poll() and read() — a normal race when
                        # several devices are readable in one pass, not a failure.
                        continue
                    if exc.errno in (errno.ENODEV, errno.EBADF):
                        self._drop(device.path)
                        rescan = True
                        continue
                    # One misbehaving device must never take the whole bridge down and
                    # leave the user with no touch input at all.
                    log.error("dropping %s after read error: %s", device.path, exc)
                    self._drop(device.path)
                    rescan = True
                    continue
                self._handle(device, events, ecodes)

    def _absolute_axis(self, state: dict, caps, code, value, axis: str, multitouch: bool) -> bool:
        """Record one absolute X/Y sample. Returns False when the sample is not primary.

        Multitouch positions are recorded for *every* slot, not just the primary one: when
        the primary finger lifts, another slot takes over and needs its own coordinates,
        or the handoff presses the new contact where the departing finger was.
        """
        info_key = f"{axis}_info"
        if multitouch:
            slot_state = state["slot_pos"].setdefault(state["current_slot"], {})
            slot_state[axis] = value
            slot_state[info_key] = caps(code)
            if (
                state["primary_slot"] is not None
                and state["current_slot"] != state["primary_slot"]
            ):
                return False
        state[axis] = value
        state[info_key] = caps(code)
        return True

    @staticmethod
    def _slot_has_position(state: dict, slot) -> bool:
        pos = state["slot_pos"].get(slot)
        return bool(
            pos
            and pos.get("x") is not None
            and pos.get("y") is not None
            and pos.get("x_info")
            and pos.get("y_info")
        )

    def _arm_deferred_press(self, state: dict) -> None:
        """Arm a promotion's press once the promoted slot finally reports a position.

        A finger promoted to primary may not have sent ABS_MT_POSITION_* yet — that is
        legal and can arrive packets later. Arming the press before then would fire it at
        the *departing* finger's coordinates as a stray tap somewhere else entirely.
        """
        slot = state["awaiting_slot_position"]
        if slot is None or not self._slot_has_position(state, slot):
            return
        state["awaiting_slot_position"] = None
        if not state["pressed"]:
            state["pending_press"] = True

    def _forget_slot(self, state: dict, slot) -> None:
        """Drop everything remembered about a lifted contact.

        The kernel reuses slot indices freely, so a stale slot_pos entry left behind by a
        non-primary lift is later adopted verbatim by whatever finger reuses that index.
        """
        state["slot_pos"].pop(slot, None)
        if state["awaiting_slot_position"] == slot:
            state["awaiting_slot_position"] = None

    def _handle(self, device, events, ecodes) -> None:
        caps = device.absinfo
        state = self._states.setdefault(device.path, self._fresh_state())
        for event in events:
            if state["resyncing"]:
                # The kernel contract for SYN_DROPPED: discard everything up to *and
                # including* the next SYN_REPORT, then re-read the device's current state.
                if event.type == ecodes.EV_SYN and event.code == ecodes.SYN_REPORT:
                    state["resyncing"] = False
                    self._resync(device, state, ecodes)
                continue
            if event.type == ecodes.EV_ABS:
                if event.code == ecodes.ABS_MT_SLOT:
                    state["current_slot"] = event.value
                elif event.code in (ecodes.ABS_X, ecodes.ABS_MT_POSITION_X):
                    self._absolute_axis(
                        state,
                        caps,
                        event.code,
                        event.value,
                        "x",
                        event.code == ecodes.ABS_MT_POSITION_X,
                    )
                elif event.code in (ecodes.ABS_Y, ecodes.ABS_MT_POSITION_Y):
                    self._absolute_axis(
                        state,
                        caps,
                        event.code,
                        event.value,
                        "y",
                        event.code == ecodes.ABS_MT_POSITION_Y,
                    )
                elif event.code == ecodes.ABS_MT_TRACKING_ID:
                    tracking_id = event.value
                    slot = state["current_slot"]
                    state["slots"][slot] = tracking_id
                    if tracking_id >= 0 and state["primary_slot"] is None:
                        state["primary_slot"] = slot
                    if tracking_id < 0:
                        # Before the primary check below: the kernel reuses slot indices,
                        # so a stale entry left behind by a *non-primary* lift is later
                        # adopted verbatim by whatever finger reuses that index, pressing
                        # it at a dead contact's coordinates.
                        self._forget_slot(state, slot)
                    if (
                        state["primary_slot"] is not None
                        and slot != state["primary_slot"]
                    ):
                        continue
                    # Some Sunshine/Moonlight paths never emit BTN_TOUCH. In those
                    # cases, treat tracking-id transitions as contact down/up.
                    state["tracking_id"] = tracking_id
                    if tracking_id >= 0 and not state["pressed"]:
                        # Defer the down event until after the first synced motion
                        # update for this contact, so taps land on the new position
                        # instead of whatever the cursor last hovered over.
                        state["pending_press"] = True
                    elif tracking_id < 0:
                        state["pending_press"] = False
                        # Deferred to SYN_REPORT like the press: this same packet may
                        # still carry the contact's final coordinates, and the release
                        # belongs at those, not at the previous frame's.
                        state["pending_release"] = state["pressed"]
                        state["primary_slot"] = next(
                            (
                                candidate
                                for candidate, candidate_id in state["slots"].items()
                                if candidate_id >= 0
                            ),
                            None,
                        )
                        if state["primary_slot"] is not None:
                            # A second finger is taking over as primary. Its own
                            # tracking-id transition was ignored earlier (it wasn't
                            # primary yet), so it never armed pending_press — arm it now
                            # or its motion updates would drag with no button down until
                            # the user lifts and re-touches. But only once that slot has
                            # coordinates of its own: x/y still hold the *departing*
                            # finger's, and pressing there lands a stray tap on whatever
                            # the lifted finger was last over.
                            promoted_slot = state["primary_slot"]
                            if self._slot_has_position(state, promoted_slot):
                                promoted = state["slot_pos"][promoted_slot]
                                state["x"] = promoted["x"]
                                state["x_info"] = promoted["x_info"]
                                state["y"] = promoted["y"]
                                state["y_info"] = promoted["y_info"]
                                state["pending_press"] = True
                                # x/y now describe the *new* finger, so the outgoing
                                # release must be emitted before this frame's motion.
                                state["release_before_motion"] = True
                            else:
                                state["awaiting_slot_position"] = promoted_slot
            elif event.type == ecodes.EV_KEY and event.code in (BTN_TOUCH, BTN_LEFT):
                if event.value:
                    if not state["pressed"] or state["pending_release"]:
                        # Deferred to the SYN_REPORT because real devices send ABS_* and
                        # BTN_TOUCH in one packet, so pressing inline lands the tap at the
                        # *previous* frame's position — the first tap of a stream wherever
                        # the cursor started, later taps at the last contact point.
                        # `pending_release` matters when a lift and a re-touch share one
                        # frame: the contact is still nominally pressed at decode time, but
                        # the release fires first, so this press must be armed or the
                        # pointer ends the frame released.
                        state["pending_press"] = True
                else:
                    state["pending_press"] = False
                    # Deferred: the same packet typically carries the final coordinates
                    # (ABS_X, ABS_Y, BTN_LEFT 0, SYN_REPORT is exactly what Sunshine's
                    # absolute-mouse passthrough produces), so releasing inline delivers
                    # the lift at the previous frame's position.
                    if state["pressed"]:
                        state["pending_release"] = True
            elif (
                event.type == ecodes.EV_SYN
                and event.code == ecodes.SYN_DROPPED
            ):
                if state["pressed"]:
                    state["pressed"] = False
                    self.pointer.button(False)
                state["pending_press"] = False
                state["pending_release"] = False
                state["release_before_motion"] = False
                state["awaiting_slot_position"] = None
                state["x"] = state["y"] = None
                state["x_info"] = state["y_info"] = None
                state["tracking_id"] = None
                state["primary_slot"] = None
                # The rest of this packet is a truncated fragment; keeping it would apply
                # coordinates to whatever slot happens to be current. Everything up to and
                # including the next SYN_REPORT is discarded, then the device is re-read.
                state["current_slot"] = 0
                state["slots"].clear()
                state["slot_pos"].clear()
                state["resyncing"] = True
            elif event.type == ecodes.EV_SYN and event.code == ecodes.SYN_REPORT:
                self._arm_deferred_press(state)
                have_position = (
                    state["x"] is not None
                    and state["y"] is not None
                    and state["x_info"]
                    and state["y_info"]
                )
                if state["pending_release"] and state["release_before_motion"]:
                    # x/y were replaced by the promoted finger's coordinates, so this
                    # frame's motion belongs to the *new* contact — the departing one must
                    # be let go before the pointer moves there.
                    self._emit_release(state)
                if have_position:
                    x, x_extent = normalize(
                        state["x"], state["x_info"].min, state["x_info"].max
                    )
                    y, y_extent = normalize(
                        state["y"], state["y_info"].min, state["y_info"].max
                    )
                    log.debug("motion %.3f %.3f", x / x_extent, y / y_extent)
                    self.pointer.motion(x, x_extent, y, y_extent)
                # After the motion, so the lift is delivered at this frame's position.
                # Before any press, so a same-frame lift-and-retouch cannot invert.
                self._emit_release(state)
                if state["pending_press"] and not state["pressed"]:
                    # Emitted after the motion above so the press lands on this frame's
                    # position. A device that reports a contact without ever sending
                    # coordinates still gets its press, just at the current location.
                    state["pending_press"] = False
                    state["pressed"] = True
                    log.debug("button down (tracking id %s)", state["tracking_id"])
                    self.pointer.button(True)

    def _emit_release(self, state: dict) -> None:
        state["release_before_motion"] = False
        if not state["pending_release"]:
            return
        state["pending_release"] = False
        if state["pressed"]:
            state["pressed"] = False
            log.debug("button up")
            self.pointer.button(False)

    def _resync(self, device, state: dict, ecodes) -> None:
        """Re-read contact state after a SYN_DROPPED, so a finger still down stays down.

        Without this a contact held across the drop reads as lifted until the user
        re-touches, which in an emulator is a stylus that stops mid-stroke. Every read is
        optional: uinput nodes and test doubles may not implement these queries, and a
        missing one must not take the bridge down — the worst case is the old behaviour.
        """
        try:
            active = device.active_keys()
        except Exception:  # noqa: BLE001 - resync is best-effort by design
            active = ()
            log.debug("cannot read active keys for %s", device.path, exc_info=True)
        if any(code in (BTN_TOUCH, BTN_LEFT) for code in active or ()):
            state["pending_press"] = True

        for axis, code in (("x", ecodes.ABS_X), ("y", ecodes.ABS_Y)):
            try:
                info = device.absinfo(code)
            except Exception:  # noqa: BLE001 - see above
                continue
            if info is None:
                continue
            value = getattr(info, "value", None)
            if value is None:
                continue
            state[axis] = value
            state[f"{axis}_info"] = info


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SDSS touch bridge")
    parser.add_argument("--output", default="HEADLESS-1", help="output to map touch onto")
    parser.add_argument(
        "--no-grab",
        action="store_true",
        help="do not take the devices exclusively (testing only)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="sdss-inputd: %(message)s",
    )
    try:
        bridge = Bridge(args.output, grab=not args.no_grab)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    # sway execs this from its config, so SIGTERM — not Ctrl-C — is how it normally dies.
    # Without a handler the default disposition skips run()'s cleanup entirely, leaving
    # sway with a latched pointer button that nothing ever releases.
    def _terminate(_signum, _frame):
        log.info("stopping")
        bridge.stop()

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _terminate)

    try:
        bridge.run()
    except KeyboardInterrupt:
        return 0
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
