import errno
import re
import signal
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runtime" / "inputd"))

import sdss_inputd  # noqa: E402


class TestDeviceSelection(unittest.TestCase):
    def test_claims_sunshine_absolute_devices(self):
        for name in ("Touch passthrough", "Pen passthrough", "Mouse passthrough (absolute)"):
            self.assertTrue(sdss_inputd.wanted(name), name)

    def test_leaves_gamepad_and_keyboard_alone(self):
        # These already work natively; grabbing them would break the game.
        for name in (
            "Sunshine X-Box One (virtual) pad",
            "Keyboard passthrough",
            "Mouse passthrough",
        ):
            self.assertFalse(sdss_inputd.wanted(name), name)

    def test_udev_rule_grants_exactly_the_claimed_devices(self):
        """Every name tagged `uaccess` hands that node's raw event stream to the desktop
        user. Granting one the daemon never claims widens the input-snooping surface for
        no benefit, so the rule and TOUCH_DEVICES must not drift apart."""
        rules = (
            Path(__file__).resolve().parents[2] / "packaging" / "60-sdss-input.rules"
        ).read_text()
        tagged = set(re.findall(r'ATTRS\{name\}=="([^"]+)",\s*TAG\+="uaccess"', rules))
        self.assertEqual(tagged, set(sdss_inputd.TOUCH_DEVICES))


class TestNormalize(unittest.TestCase):
    """evdev absinfo ranges are inclusive, so 0..1919 is 1920 distinct positions."""

    def test_maps_axis_onto_extent(self):
        self.assertEqual(sdss_inputd.normalize(0, 0, 1919), (0, 1920))
        self.assertEqual(sdss_inputd.normalize(960, 0, 1919), (960, 1920))

    def test_far_edge_stays_inside_the_surface(self):
        # motion_absolute divides position by extent, and the protocol wants [0,1) — a
        # position equal to the extent lands outside the output and the last pixel column
        # of the DS bottom screen becomes unreachable.
        position, extent = sdss_inputd.normalize(1919, 0, 1919)
        self.assertEqual((position, extent), (1919, 1920))
        self.assertLess(position / extent, 1.0)

    def test_handles_a_non_zero_minimum(self):
        self.assertEqual(sdss_inputd.normalize(100, 100, 1099), (0, 1000))
        self.assertEqual(sdss_inputd.normalize(600, 100, 1099), (500, 1000))

    def test_clamps_out_of_range_values(self):
        self.assertEqual(sdss_inputd.normalize(-50, 0, 99), (0, 100))
        self.assertEqual(sdss_inputd.normalize(500, 0, 99), (99, 100))

    def test_degenerate_axis_does_not_divide_by_zero(self):
        self.assertEqual(sdss_inputd.normalize(5, 10, 10), (0, 1))


class TestPerDeviceState(unittest.TestCase):
    """Sunshine grabs touch, pen and absolute-mouse at once; their state must not mix."""

    def setUp(self):
        self.bridge = make_bridge()

    def test_each_device_gets_its_own_state(self):
        touch = self.bridge._states.setdefault("/dev/input/event1", self.bridge._fresh_state())
        pen = self.bridge._states.setdefault("/dev/input/event2", self.bridge._fresh_state())
        self.assertIsNot(touch, pen)

    def test_one_device_does_not_clobber_another(self):
        touch = self.bridge._states.setdefault("/dev/input/event1", self.bridge._fresh_state())
        pen = self.bridge._states.setdefault("/dev/input/event2", self.bridge._fresh_state())
        touch["x"], touch["pressed"] = 100, True
        pen["x"] = 900
        self.assertEqual(touch["x"], 100)
        self.assertTrue(touch["pressed"])
        self.assertFalse(pen["pressed"])

    def test_dropping_a_device_drops_its_state(self):
        self.bridge._states["/dev/input/event1"] = self.bridge._fresh_state()
        self.bridge._drop("/dev/input/event1")
        self.assertNotIn("/dev/input/event1", self.bridge._states)

    def test_fresh_state_starts_with_no_contact(self):
        state = self.bridge._fresh_state()
        self.assertFalse(state["pressed"])
        self.assertFalse(state["pending_press"])
        self.assertIsNone(state["x"])


class FakeCodes:
    """The subset of evdev.ecodes the bridge actually reads.

    `_handle` takes `ecodes` as a parameter precisely so the event loop can be driven
    without evdev installed — these tests run on any machine, including the Mac used for
    development, where the real module is unavailable.
    """

    EV_ABS = 0x03
    EV_KEY = 0x01
    EV_SYN = 0x00
    ABS_X = 0x00
    ABS_Y = 0x01
    ABS_MT_SLOT = 0x2F
    ABS_MT_POSITION_X = 0x35
    ABS_MT_POSITION_Y = 0x36
    ABS_MT_TRACKING_ID = 0x39
    SYN_REPORT = 0x00
    SYN_DROPPED = 0x03


class FakeEvent:
    def __init__(self, type_, code, value):
        self.type = type_
        self.code = code
        self.value = value


class FakeAbsInfo:
    def __init__(self, minimum=0, maximum=1279, value=None):
        self.min = minimum
        self.max = maximum
        if value is not None:
            self.value = value


# Deliberately different per axis: with one shared range every coordinate assertion in the
# suite passes even when X and Y are swapped somewhere in the bridge.
X_MAX = 1279
Y_MAX = 799
X_AXES = (FakeCodes.ABS_X, FakeCodes.ABS_MT_POSITION_X)


class FakeDevice:
    def __init__(self, path="/dev/input/event20", name="Touch passthrough", fd=7):
        self.path = path
        self.name = name
        self.grabbed = False
        self.closed = False
        self.ungrabbed = False
        self._fd = fd
        # Each entry is either a list of events to return or an OSError to raise.
        self.reads = []
        # Current-state answers used by the SYN_DROPPED resync. None means "device does
        # not support this query", which must not take the bridge down.
        self.active = []
        self.current = {}

    def fileno(self):
        return self._fd

    def read(self):
        if not self.reads:
            return []
        item = self.reads.pop(0)
        if isinstance(item, OSError):
            raise item
        return item

    def absinfo(self, code):
        maximum = X_MAX if code in X_AXES else Y_MAX
        return FakeAbsInfo(0, maximum, value=self.current.get(code))

    def active_keys(self):
        return list(self.active)

    def grab(self):
        self.grabbed = True

    def ungrab(self):
        self.ungrabbed = True

    def close(self):
        self.closed = True


class RecordingPointer:
    def __init__(self, output=None):
        self.output = output
        self.events = []
        self.dispatches = 0
        self.dispatch_error = None
        self.frames = 0
        self.closed = 0

    def motion(self, x, x_extent, y, y_extent):
        # Recorded raw, not as x/x_extent: a ratio hides any bug that gets the extent
        # wrong while preserving the quotient, and the protocol cares about both numbers.
        self.events.append(("motion", (x, x_extent, y, y_extent)))
        self.frames += 1

    def button(self, pressed):
        self.events.append(("button", pressed))
        self.frames += 1

    def fileno(self):
        return 9999

    def dispatch(self):
        self.dispatches += 1
        if self.dispatch_error is not None:
            raise self.dispatch_error

    def close(self):
        self.closed += 1
        self.events.append(("close", None))

    @property
    def buttons(self):
        return [value for kind, value in self.events if kind == "button"]

    @property
    def motions(self):
        return [value for kind, value in self.events if kind == "motion"]


def at(x, y):
    """The four-tuple the bridge must hand motion_absolute for raw sample (x, y)."""
    return (x, X_MAX + 1, y, Y_MAX + 1)


def make_bridge():
    # Built through the real __init__ (with only the pointer swapped) so a new piece of
    # bridge state can never be missing here and pass the suite by accident.
    with mock.patch.object(sdss_inputd, "VirtualPointer", RecordingPointer):
        bridge = sdss_inputd.Bridge("HEADLESS-1", grab=True)
    return bridge


class TestEventLoop(unittest.TestCase):
    """Drives `_handle` with scripted evdev sequences — the path with the real bugs."""

    def setUp(self):
        self.bridge = make_bridge()
        self.device = FakeDevice()
        self.bridge._devices[self.device.path] = self.device
        self.bridge._states[self.device.path] = self.bridge._fresh_state()
        self.codes = FakeCodes

    def feed(self, *events):
        self.bridge._handle(self.device, list(events), self.codes)

    def test_single_tap_presses_after_the_first_motion(self):
        c = self.codes
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_MT_TRACKING_ID, 1),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_X, 640),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_Y, 400),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        kinds = [event[0] for event in self.bridge.pointer.events]
        # The press must follow the motion, or the tap lands where the cursor last was.
        self.assertEqual(kinds, ["motion", "button"])
        self.assertTrue(self.bridge.pointer.events[1][1])

    def test_lifting_the_contact_releases_the_button(self):
        c = self.codes
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_MT_TRACKING_ID, 1),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_X, 640),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_Y, 400),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
            FakeEvent(c.EV_ABS, c.ABS_MT_TRACKING_ID, -1),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        self.assertEqual(self.bridge.pointer.buttons, [True, False])
        self.assertFalse(self.bridge._states[self.device.path]["pressed"])

    def test_a_second_finger_does_not_move_the_pointer(self):
        c = self.codes
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_MT_SLOT, 0),
            FakeEvent(c.EV_ABS, c.ABS_MT_TRACKING_ID, 1),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_X, 640),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_Y, 400),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        before = list(self.bridge.pointer.events)
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_MT_SLOT, 1),
            FakeEvent(c.EV_ABS, c.ABS_MT_TRACKING_ID, 2),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_X, 20),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_Y, 20),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        moved_to = [e for e in self.bridge.pointer.events[len(before) :] if e[0] == "motion"]
        # The pointer may re-report the primary finger, but must never jump to slot 1.
        for _, sample in moved_to:
            self.assertEqual(sample, at(640, 400))

    def test_handoff_presses_the_new_finger_at_its_own_position(self):
        c = self.codes
        # First finger down and moving (primary).
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_MT_SLOT, 0),
            FakeEvent(c.EV_ABS, c.ABS_MT_TRACKING_ID, 1),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_X, 640),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_Y, 400),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        # Second finger touches down while the first is still primary; it must not
        # move the pointer or generate a press of its own yet.
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_MT_SLOT, 1),
            FakeEvent(c.EV_ABS, c.ABS_MT_TRACKING_ID, 2),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_X, 100),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_Y, 100),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        before = list(self.bridge.pointer.events)
        # First finger lifts; slot 1 becomes primary by handoff.
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_MT_SLOT, 0),
            FakeEvent(c.EV_ABS, c.ABS_MT_TRACKING_ID, -1),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        emitted = self.bridge.pointer.events[len(before) :]

        # The press must land on the *promoted* finger's own position (100,100). Using the
        # departing finger's coordinates (640,400) fires a stray tap on unrelated UI.
        presses = [i for i, e in enumerate(emitted) if e[0] == "button" and e[1]]
        self.assertTrue(presses, emitted)
        motions_before = [e for e in emitted[: presses[0]] if e[0] == "motion"]
        self.assertTrue(motions_before, emitted)
        self.assertEqual(motions_before[-1][1], at(100, 100))

    def test_handoff_press_precedes_the_new_fingers_motion(self):
        c = self.codes
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_MT_SLOT, 0),
            FakeEvent(c.EV_ABS, c.ABS_MT_TRACKING_ID, 1),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_X, 640),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_Y, 400),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
            FakeEvent(c.EV_ABS, c.ABS_MT_SLOT, 1),
            FakeEvent(c.EV_ABS, c.ABS_MT_TRACKING_ID, 2),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_X, 100),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_Y, 100),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        before = list(self.bridge.pointer.events)
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_MT_SLOT, 0),
            FakeEvent(c.EV_ABS, c.ABS_MT_TRACKING_ID, -1),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        # The now-primary second finger drags.
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_MT_SLOT, 1),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_X, 200),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_Y, 200),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        emitted = self.bridge.pointer.events[len(before) :]
        kinds = [event[0] for event in emitted]
        last_motion = len(kinds) - 1 - kinds[::-1].index("motion")
        press_before_motion = any(
            kind == "button" and emitted[i][1] for i, kind in enumerate(kinds[:last_motion])
        )
        # Without this the drag reads as pure hover until the user lifts and re-touches.
        self.assertTrue(press_before_motion, emitted)

    def test_syn_dropped_releases_the_button(self):
        c = self.codes
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_MT_TRACKING_ID, 1),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_X, 640),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_Y, 400),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
            FakeEvent(c.EV_SYN, c.SYN_DROPPED, 0),
        )
        self.assertEqual(self.bridge.pointer.buttons[-1], False)

    def test_btn_touch_devices_press_and_release(self):
        c = self.codes
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_X, 100),
            FakeEvent(c.EV_ABS, c.ABS_Y, 100),
            FakeEvent(c.EV_KEY, sdss_inputd.BTN_TOUCH, 1),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
            FakeEvent(c.EV_KEY, sdss_inputd.BTN_TOUCH, 0),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        self.assertEqual(self.bridge.pointer.buttons, [True, False])

    def test_btn_touch_press_lands_on_the_new_position(self):
        """Real touch/pen devices send ABS_* and BTN_TOUCH in the same packet before
        SYN_REPORT. Emitting the press inline puts it at the previous frame's position:
        the first tap of a stream wherever the cursor started, later taps on the last
        contact point."""
        c = self.codes
        # An earlier contact somewhere else, so a stale position exists to land on.
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_X, 1000),
            FakeEvent(c.EV_ABS, c.ABS_Y, 700),
            FakeEvent(c.EV_KEY, sdss_inputd.BTN_TOUCH, 1),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
            FakeEvent(c.EV_KEY, sdss_inputd.BTN_TOUCH, 0),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        before = list(self.bridge.pointer.events)
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_X, 100),
            FakeEvent(c.EV_ABS, c.ABS_Y, 100),
            FakeEvent(c.EV_KEY, sdss_inputd.BTN_TOUCH, 1),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        emitted = self.bridge.pointer.events[len(before) :]
        self.assertEqual([e[0] for e in emitted], ["motion", "button"])
        self.assertEqual(emitted[0][1], at(100, 100))

    def test_btn_touch_without_any_coordinates_still_presses(self):
        c = self.codes
        self.feed(
            FakeEvent(c.EV_KEY, sdss_inputd.BTN_TOUCH, 1),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        self.assertEqual(self.bridge.pointer.buttons, [True])

    def test_x_and_y_are_not_interchangeable(self):
        """The axes have different extents, so a swap cannot hide behind a shared range."""
        c = self.codes
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_X, 300),
            FakeEvent(c.EV_ABS, c.ABS_Y, 300),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        self.assertEqual(self.bridge.pointer.motions, [(300, X_MAX + 1, 300, Y_MAX + 1)])

    def test_every_emission_delivers_a_frame(self):
        """wlr virtual pointers only deliver anything on frame(); without it the pointer
        is completely dead while every other assertion in this suite still passes."""
        c = self.codes
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_X, 100),
            FakeEvent(c.EV_ABS, c.ABS_Y, 100),
            FakeEvent(c.EV_KEY, sdss_inputd.BTN_TOUCH, 1),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        self.assertEqual(self.bridge.pointer.frames, len(self.bridge.pointer.events))

    def test_virtual_pointer_frames_each_event(self):
        pointer = sdss_inputd.VirtualPointer.__new__(sdss_inputd.VirtualPointer)
        calls = []
        pointer.pointer = types.SimpleNamespace(
            motion_absolute=lambda *a: calls.append("motion_absolute"),
            button=lambda *a: calls.append("button"),
            frame=lambda: calls.append("frame"),
        )
        pointer.display = types.SimpleNamespace(flush=lambda: None)
        pointer.motion(1, 2, 3, 4)
        pointer.button(True)
        self.assertEqual(calls, ["motion_absolute", "frame", "button", "frame"])


class TestReleaseOrdering(unittest.TestCase):
    """A lift usually arrives in the same packet as the contact's final coordinates."""

    def setUp(self):
        self.bridge = make_bridge()
        self.device = FakeDevice()
        self.bridge._devices[self.device.path] = self.device
        self.bridge._states[self.device.path] = self.bridge._fresh_state()
        self.codes = FakeCodes

    def feed(self, *events):
        self.bridge._handle(self.device, list(events), self.codes)

    def test_release_carrying_coordinates_lands_on_the_new_position(self):
        """Sunshine's absolute-mouse passthrough emits ABS_X, ABS_Y, BTN_LEFT 0,
        SYN_REPORT. Releasing inline at decode time delivers the lift at the *previous*
        frame's position, which in a DS menu is a tap on the wrong entry."""
        c = self.codes
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_X, 1000),
            FakeEvent(c.EV_ABS, c.ABS_Y, 700),
            FakeEvent(c.EV_KEY, sdss_inputd.BTN_LEFT, 1),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        before = list(self.bridge.pointer.events)
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_X, 200),
            FakeEvent(c.EV_ABS, c.ABS_Y, 150),
            FakeEvent(c.EV_KEY, sdss_inputd.BTN_LEFT, 0),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        emitted = self.bridge.pointer.events[len(before) :]
        self.assertEqual(
            emitted, [("motion", at(200, 150)), ("button", False)]
        )

    def test_tracking_id_release_also_waits_for_the_frame(self):
        c = self.codes
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_MT_TRACKING_ID, 1),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_X, 900),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_Y, 600),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        before = list(self.bridge.pointer.events)
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_X, 300),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_Y, 200),
            FakeEvent(c.EV_ABS, c.ABS_MT_TRACKING_ID, -1),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        emitted = self.bridge.pointer.events[len(before) :]
        self.assertEqual(emitted, [("motion", at(300, 200)), ("button", False)])

    def test_a_lift_and_retouch_in_one_frame_does_not_invert(self):
        """Both a release and a press are pending for the same SYN_REPORT; emitting the
        press first would leave the pointer released after a re-touch."""
        c = self.codes
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_X, 400),
            FakeEvent(c.EV_ABS, c.ABS_Y, 300),
            FakeEvent(c.EV_KEY, sdss_inputd.BTN_TOUCH, 1),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        before = list(self.bridge.pointer.events)
        self.feed(
            FakeEvent(c.EV_KEY, sdss_inputd.BTN_TOUCH, 0),
            FakeEvent(c.EV_ABS, c.ABS_X, 500),
            FakeEvent(c.EV_ABS, c.ABS_Y, 350),
            FakeEvent(c.EV_KEY, sdss_inputd.BTN_TOUCH, 1),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        emitted = self.bridge.pointer.events[len(before) :]
        self.assertEqual(
            emitted,
            [("motion", at(500, 350)), ("button", False), ("button", True)],
        )
        self.assertTrue(self.bridge._states[self.device.path]["pressed"])

    def test_release_precedes_motion_when_a_handoff_moves_the_pointer(self):
        """On a promotion the frame's coordinates already belong to the *new* finger, so
        the departing contact must be let go before the pointer travels there — otherwise
        the emulator reads one continuous stroke between two unrelated fingers."""
        c = self.codes
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_MT_SLOT, 0),
            FakeEvent(c.EV_ABS, c.ABS_MT_TRACKING_ID, 1),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_X, 640),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_Y, 400),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
            FakeEvent(c.EV_ABS, c.ABS_MT_SLOT, 1),
            FakeEvent(c.EV_ABS, c.ABS_MT_TRACKING_ID, 2),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_X, 100),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_Y, 90),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        before = list(self.bridge.pointer.events)
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_MT_SLOT, 0),
            FakeEvent(c.EV_ABS, c.ABS_MT_TRACKING_ID, -1),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        emitted = self.bridge.pointer.events[len(before) :]
        self.assertEqual(
            emitted,
            [("button", False), ("motion", at(100, 90)), ("button", True)],
        )


class TestSlotBookkeeping(unittest.TestCase):
    """The kernel reuses slot indices, so a lifted contact must leave nothing behind."""

    def setUp(self):
        self.bridge = make_bridge()
        self.device = FakeDevice()
        self.bridge._devices[self.device.path] = self.device
        self.bridge._states[self.device.path] = self.bridge._fresh_state()
        self.codes = FakeCodes

    def feed(self, *events):
        self.bridge._handle(self.device, list(events), self.codes)

    @property
    def state(self):
        return self.bridge._states[self.device.path]

    def test_a_non_primary_lift_forgets_that_slots_position(self):
        c = self.codes
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_MT_SLOT, 0),
            FakeEvent(c.EV_ABS, c.ABS_MT_TRACKING_ID, 1),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_X, 640),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_Y, 400),
            FakeEvent(c.EV_ABS, c.ABS_MT_SLOT, 1),
            FakeEvent(c.EV_ABS, c.ABS_MT_TRACKING_ID, 2),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_X, 20),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_Y, 20),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
            # Slot 1 (never primary) lifts.
            FakeEvent(c.EV_ABS, c.ABS_MT_SLOT, 1),
            FakeEvent(c.EV_ABS, c.ABS_MT_TRACKING_ID, -1),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        self.assertNotIn(1, self.state["slot_pos"])

    def test_a_reused_slot_index_does_not_inherit_a_dead_contact(self):
        """Slot 1 lifts while non-primary, then slot 0 lifts and slot 1 is reused by a new
        finger that has not reported coordinates yet. A stale slot_pos entry would press
        the promoted contact at the dead finger's position."""
        c = self.codes
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_MT_SLOT, 0),
            FakeEvent(c.EV_ABS, c.ABS_MT_TRACKING_ID, 1),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_X, 640),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_Y, 400),
            FakeEvent(c.EV_ABS, c.ABS_MT_SLOT, 1),
            FakeEvent(c.EV_ABS, c.ABS_MT_TRACKING_ID, 2),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_X, 20),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_Y, 20),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
            FakeEvent(c.EV_ABS, c.ABS_MT_SLOT, 1),
            FakeEvent(c.EV_ABS, c.ABS_MT_TRACKING_ID, -1),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
            # A third finger reuses slot index 1, coordinates not yet reported.
            FakeEvent(c.EV_ABS, c.ABS_MT_SLOT, 1),
            FakeEvent(c.EV_ABS, c.ABS_MT_TRACKING_ID, 3),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        before = list(self.bridge.pointer.events)
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_MT_SLOT, 0),
            FakeEvent(c.EV_ABS, c.ABS_MT_TRACKING_ID, -1),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        emitted = self.bridge.pointer.events[len(before) :]
        # (20, 20) is the dead second finger's position; nothing may land there.
        self.assertNotIn(("motion", at(20, 20)), emitted)
        self.assertNotIn(("button", True), emitted)

    def test_promotion_without_a_position_defers_the_press(self):
        """The promoted finger's first ABS_MT_POSITION_* may legally arrive packets later.
        Arming the press immediately fires it at the departing finger's coordinates."""
        c = self.codes
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_MT_SLOT, 0),
            FakeEvent(c.EV_ABS, c.ABS_MT_TRACKING_ID, 1),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_X, 640),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_Y, 400),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
            # Second finger appears with a tracking id but no coordinates yet.
            FakeEvent(c.EV_ABS, c.ABS_MT_SLOT, 1),
            FakeEvent(c.EV_ABS, c.ABS_MT_TRACKING_ID, 2),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        before = list(self.bridge.pointer.events)
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_MT_SLOT, 0),
            FakeEvent(c.EV_ABS, c.ABS_MT_TRACKING_ID, -1),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        handoff = self.bridge.pointer.events[len(before) :]
        # No press yet, and certainly not at the departing finger's (640,400).
        self.assertNotIn(("button", True), handoff)
        self.assertEqual(self.state["awaiting_slot_position"], 1)

        before = list(self.bridge.pointer.events)
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_MT_SLOT, 1),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_X, 111),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_Y, 222),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        emitted = self.bridge.pointer.events[len(before) :]
        self.assertEqual(
            emitted, [("motion", at(111, 222)), ("button", True)]
        )


class TestSynDropped(unittest.TestCase):
    """SYN_DROPPED means the packet was truncated: discard through the next SYN_REPORT."""

    def setUp(self):
        self.bridge = make_bridge()
        self.device = FakeDevice()
        self.bridge._devices[self.device.path] = self.device
        self.bridge._states[self.device.path] = self.bridge._fresh_state()
        self.codes = FakeCodes

    def feed(self, *events):
        self.bridge._handle(self.device, list(events), self.codes)

    @property
    def state(self):
        return self.bridge._states[self.device.path]

    def test_the_truncated_tail_is_discarded(self):
        c = self.codes
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_MT_TRACKING_ID, 1),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_X, 640),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_Y, 400),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        before = list(self.bridge.pointer.events)
        self.feed(
            FakeEvent(c.EV_SYN, c.SYN_DROPPED, 0),
            # Fragment of the packet the kernel truncated — meaningless on its own.
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_X, 5),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_Y, 5),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        emitted = self.bridge.pointer.events[len(before) :]
        # Only the safety release; the garbage coordinates must never reach the pointer.
        self.assertEqual(emitted, [("button", False)])
        self.assertFalse(self.state["resyncing"])

    def test_current_slot_is_reset_so_garbage_cannot_land_on_a_slot(self):
        c = self.codes
        self.feed(
            FakeEvent(c.EV_ABS, c.ABS_MT_SLOT, 3),
            FakeEvent(c.EV_SYN, c.SYN_DROPPED, 0),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        self.assertEqual(self.state["current_slot"], 0)

    def test_events_after_the_resync_are_processed_normally(self):
        c = self.codes
        self.feed(
            FakeEvent(c.EV_SYN, c.SYN_DROPPED, 0),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_X, 5),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
            FakeEvent(c.EV_ABS, c.ABS_MT_TRACKING_ID, 7),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_X, 640),
            FakeEvent(c.EV_ABS, c.ABS_MT_POSITION_Y, 400),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        self.assertEqual(
            self.bridge.pointer.events[-2:],
            [("motion", at(640, 400)), ("button", True)],
        )

    def test_a_contact_still_down_is_recovered_from_the_device(self):
        """Without re-reading current state a finger held across the drop stays "up" until
        the user lifts and re-touches — a stylus that stops mid-stroke."""
        c = self.codes
        self.device.active = [sdss_inputd.BTN_TOUCH]
        self.device.current = {c.ABS_X: 800, c.ABS_Y: 500}
        self.feed(
            FakeEvent(c.EV_SYN, c.SYN_DROPPED, 0),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
            FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
        )
        self.assertEqual(
            self.bridge.pointer.events,
            [("motion", at(800, 500)), ("button", True)],
        )

    def test_a_device_that_cannot_report_state_does_not_kill_the_loop(self):
        c = self.codes

        class Unqueryable(FakeDevice):
            def active_keys(self):
                raise OSError("not supported")

            def absinfo(self, _code):
                raise OSError("not supported")

        device = Unqueryable()
        self.bridge._states[device.path] = self.bridge._fresh_state()
        self.bridge._handle(
            device,
            [
                FakeEvent(c.EV_SYN, c.SYN_DROPPED, 0),
                FakeEvent(c.EV_SYN, c.SYN_REPORT, 0),
            ],
            c,
        )
        self.assertFalse(self.bridge._states[device.path]["resyncing"])


class TestContactIsNeverLeftHeld(unittest.TestCase):
    """A latched button is a stylus permanently pressed on the screen — nothing recovers it."""

    def setUp(self):
        self.bridge = make_bridge()
        self.device = FakeDevice()
        self.bridge._devices[self.device.path] = self.device
        self.bridge._states[self.device.path] = self.bridge._fresh_state()
        self.bridge._identities[self.device.path] = (1, 1)

    def press(self):
        self.bridge._states[self.device.path]["pressed"] = True

    def test_dropping_a_device_mid_drag_releases_the_button(self):
        self.press()
        self.bridge._drop(self.device.path)
        self.assertEqual(self.bridge.pointer.buttons, [False])
        self.assertTrue(self.device.closed)

    def test_close_releases_every_held_contact(self):
        self.press()
        self.bridge.close()
        self.assertEqual(self.bridge.pointer.buttons, [False])
        self.assertEqual(self.bridge._devices, {})

    def test_dropping_an_idle_device_emits_nothing(self):
        self.bridge._drop(self.device.path)
        self.assertEqual(self.bridge.pointer.buttons, [])

    def test_run_releases_contacts_even_when_the_loop_raises(self):
        self.press()

        def boom():
            raise RuntimeError("loop exploded")

        self.bridge._run = boom
        with self.assertRaises(RuntimeError):
            self.bridge.run()
        self.assertEqual(self.bridge.pointer.buttons, [False])

    def test_a_failing_release_does_not_mask_teardown(self):
        self.press()

        def refuse(_pressed):
            raise OSError("display gone")

        self.bridge.pointer.button = refuse
        self.bridge.close()  # must not raise
        self.assertEqual(self.bridge._devices, {})


class TestReplugDetection(unittest.TestCase):
    """Sunshine recreates its uinput nodes per stream and the kernel reuses eventN."""

    def test_identity_changes_when_the_node_is_recreated(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "event20")
            with open(path, "w") as handle:
                handle.write("")
            first = sdss_inputd.Bridge._identity(path)
            os.unlink(path)
            with open(path, "w") as handle:
                handle.write("")
            second = sdss_inputd.Bridge._identity(path)
        self.assertIsNotNone(first)
        # Same path, different inode — this is what tells discover() to re-attach instead
        # of staying bound to a dead device until the daemon restarts.
        self.assertNotEqual(first, second)

    def test_identity_of_a_missing_path_is_none(self):
        self.assertIsNone(sdss_inputd.Bridge._identity("/dev/input/definitely-not-here"))

    def test_stale_device_is_dropped_and_replaced_on_rediscovery(self):
        bridge = make_bridge()
        old = FakeDevice()
        bridge._devices[old.path] = old
        bridge._states[old.path] = bridge._fresh_state()
        bridge._states[old.path]["pressed"] = True
        bridge._identities[old.path] = (1, 1)

        new = FakeDevice()
        fake_evdev = types.ModuleType("evdev")
        fake_evdev.list_devices = lambda: [old.path]
        fake_evdev.InputDevice = lambda _path: new
        with mock.patch.dict(sys.modules, {"evdev": fake_evdev}):
            with mock.patch.object(sdss_inputd.Bridge, "_identity", staticmethod(lambda _p: (1, 2))):
                bridge.discover()

        self.assertTrue(old.closed)
        self.assertIs(bridge._devices[old.path], new)
        self.assertTrue(new.grabbed)
        # The contact held by the dead node must not survive the swap.
        self.assertEqual(bridge.pointer.buttons, [False])
        self.assertFalse(bridge._states[old.path]["pressed"])

    def test_unchanged_device_is_left_alone(self):
        bridge = make_bridge()
        device = FakeDevice()
        bridge._devices[device.path] = device
        bridge._states[device.path] = bridge._fresh_state()
        bridge._identities[device.path] = (1, 1)

        fake_evdev = types.ModuleType("evdev")
        fake_evdev.list_devices = lambda: [device.path]

        def refuse(_path):
            raise AssertionError("must not reopen an unchanged device")

        fake_evdev.InputDevice = refuse
        with mock.patch.dict(sys.modules, {"evdev": fake_evdev}):
            with mock.patch.object(sdss_inputd.Bridge, "_identity", staticmethod(lambda _p: (1, 1))):
                bridge.discover()
        self.assertIs(bridge._devices[device.path], device)

    def test_unwanted_nodes_are_not_reopened_every_pass(self):
        bridge = make_bridge()
        opened = []

        def opener(path):
            opened.append(path)
            return FakeDevice(path=path, name="Keyboard passthrough")

        fake_evdev = types.ModuleType("evdev")
        fake_evdev.list_devices = lambda: ["/dev/input/event9"]
        fake_evdev.InputDevice = opener
        with mock.patch.dict(sys.modules, {"evdev": fake_evdev}):
            with mock.patch.object(sdss_inputd.Bridge, "_identity", staticmethod(lambda _p: (1, 5))):
                bridge.discover()
                bridge.discover()
                bridge.discover()
        # Reopening every /dev/input node on each pass is wasted syscalls in the hot path.
        self.assertEqual(len(opened), 1)

    def test_ungrabbable_device_is_not_retried_every_pass(self):
        """A grab failure is a hard error because gamescope would otherwise see raw input."""
        bridge = make_bridge()
        opened = []

        class Ungrabbable(FakeDevice):
            def grab(self):
                raise OSError("device busy")

        def opener(path):
            opened.append(path)
            return Ungrabbable(path=path)

        fake_evdev = types.ModuleType("evdev")
        fake_evdev.list_devices = lambda: ["/dev/input/event9"]
        fake_evdev.InputDevice = opener
        with mock.patch.dict(sys.modules, {"evdev": fake_evdev}):
            with self.assertRaises(RuntimeError):
                bridge.discover()
        self.assertEqual(len(opened), 1)
        self.assertNotIn("/dev/input/event9", bridge._devices)

    def test_identity_describes_the_node_that_was_actually_opened(self):
        """Sunshine can tear the node down and recreate it between the stat and the open.
        Re-stat'ing afterwards stores the *new* node's identity against a fd on the dead
        one, so the replug check compares equal forever and touch never comes back."""
        bridge = make_bridge()
        device = FakeDevice()
        seen = iter([(1, 1), (1, 2), (1, 2)])

        fake_evdev = types.ModuleType("evdev")
        fake_evdev.list_devices = lambda: [device.path]
        fake_evdev.InputDevice = lambda _path: device
        with mock.patch.dict(sys.modules, {"evdev": fake_evdev}):
            with mock.patch.object(
                sdss_inputd.Bridge, "_identity", staticmethod(lambda _p: next(seen))
            ):
                bridge.discover()
        # The identity taken before the open, not the one the recreated node would report.
        self.assertEqual(bridge._identities[device.path], (1, 1))

    def test_no_grab_leaves_the_device_shared(self):
        bridge = make_bridge()
        bridge.grab = False
        device = FakeDevice()
        fake_evdev = types.ModuleType("evdev")
        fake_evdev.list_devices = lambda: [device.path]
        fake_evdev.InputDevice = lambda _path: device
        with mock.patch.dict(sys.modules, {"evdev": fake_evdev}):
            with mock.patch.object(sdss_inputd.Bridge, "_identity", staticmethod(lambda _p: (1, 1))):
                bridge.discover()
        self.assertFalse(device.grabbed)
        self.assertIs(bridge._devices[device.path], device)


class TestVirtualPointerBinding(unittest.TestCase):
    """create_virtual_pointer_with_output is a v2 request."""

    def _pointer_with_manager_version(self, version):
        pointer = sdss_inputd.VirtualPointer.__new__(sdss_inputd.VirtualPointer)
        pointer._manager = object()
        pointer._manager_version = version
        pointer._seat = object()
        return pointer

    def test_a_version_1_manager_is_rejected_up_front(self):
        # On a v1 manager the request is a protocol error the compositor reports
        # asynchronously, surfacing later as an unrelated "wayland display error".
        pointer = self._pointer_with_manager_version(1)
        with self.assertRaises(RuntimeError) as caught:
            sdss_inputd.VirtualPointer._require_manager_version(pointer)
        self.assertIn("version 2", str(caught.exception))

    def test_a_version_2_manager_is_accepted(self):
        pointer = self._pointer_with_manager_version(2)
        sdss_inputd.VirtualPointer._require_manager_version(pointer)

    def _connect_against_manager_version(self, version):
        """Drive the real __init__ against a fake pywayland advertising `version`.

        The guard existing is not enough — it has to run *before* the v2-only
        create_virtual_pointer_with_output request is sent, which only driving the
        constructor itself can prove.
        """
        created = []

        class FakeManager:
            def create_virtual_pointer_with_output(self, seat, output):
                created.append((seat, output))
                return object()

        class FakeRegistry:
            def __init__(self):
                self.dispatcher = {}

        class FakeOutput:
            def __init__(self):
                self.dispatcher = {}

        registry = FakeRegistry()

        class FakeDisplay:
            def connect(self):
                pass

            def get_registry(self):
                return registry

            def roundtrip(self):
                for name, interface, ver in (
                    (1, "zwlr_virtual_pointer_manager_v1", version),
                    (2, "wl_seat", 5),
                    (3, "wl_output", 4),
                ):
                    registry.dispatcher["global"](registry, name, interface, ver)

        def bind(name, interface, _version):
            if name == 3:
                output = FakeOutput()
                # wl_output announces its name asynchronously, same as the real thing.
                registry.dispatcher.setdefault("_outputs", []).append(output)
                return output
            return FakeManager() if name == 1 else object()

        registry.bind = bind

        fake_client = types.ModuleType("pywayland.client")
        fake_client.Display = FakeDisplay
        fake_wayland = types.ModuleType("pywayland.protocol.wayland")
        fake_wayland.WlOutput = object
        fake_wayland.WlSeat = object
        fake_wlr = types.ModuleType(
            "pywayland.protocol.wlr_virtual_pointer_unstable_v1"
        )
        fake_wlr.ZwlrVirtualPointerManagerV1 = object
        modules = {
            "pywayland": types.ModuleType("pywayland"),
            "pywayland.client": fake_client,
            "pywayland.protocol": types.ModuleType("pywayland.protocol"),
            "pywayland.protocol.wayland": fake_wayland,
            "pywayland.protocol.wlr_virtual_pointer_unstable_v1": fake_wlr,
        }
        with mock.patch.dict(sys.modules, modules):
            with self.assertRaises(RuntimeError) as caught:
                sdss_inputd.VirtualPointer("HEADLESS-1")
        return caught.exception, created

    def test_construction_refuses_a_v1_manager_before_sending_the_v2_request(self):
        error, created = self._connect_against_manager_version(1)
        self.assertIn("version 2", str(error))
        self.assertEqual(created, [], "v2-only request was sent to a v1 manager")

    def test_construction_gets_past_the_version_guard_on_a_v2_manager(self):
        # Same fake, v2: it must fail later (no matching output), proving the version
        # guard is not what stops a v2 compositor.
        error, _ = self._connect_against_manager_version(2)
        self.assertNotIn("version 2", str(error))


class TestMain(unittest.TestCase):
    """sway execs this from its config, so SIGTERM is how the daemon normally dies."""

    def setUp(self):
        # main() calls logging.basicConfig, which would otherwise reformat the whole
        # suite's log output from the first test that runs it onwards.
        patcher = mock.patch.object(sdss_inputd.logging, "basicConfig")
        patcher.start()
        self.addCleanup(patcher.stop)
        log_patcher = mock.patch.object(sdss_inputd, "log")
        log_patcher.start()
        self.addCleanup(log_patcher.stop)

    def _patched_bridge(self, bridge):
        return mock.patch.object(sdss_inputd, "Bridge", lambda *a, **k: bridge)

    def test_no_grab_flag_reaches_the_bridge(self):
        seen = {}

        class FakeBridge:
            def __init__(self, output, grab=True):
                seen["output"], seen["grab"] = output, grab

            def stop(self):
                pass

            def run(self):
                pass

        with mock.patch.object(sdss_inputd, "Bridge", FakeBridge):
            with mock.patch.object(signal, "signal", lambda *a: None):
                self.assertEqual(sdss_inputd.main(["--no-grab", "--output", "HEADLESS-9"]), 0)
        self.assertEqual(seen, {"output": "HEADLESS-9", "grab": False})

    def test_termination_signals_stop_the_bridge(self):
        handlers = {}
        bridge = make_bridge()
        with self._patched_bridge(bridge):
            with mock.patch.object(
                signal, "signal", lambda sig, handler: handlers.__setitem__(sig, handler)
            ):
                with mock.patch.object(bridge, "run", lambda: None):
                    sdss_inputd.main([])
        # Without a handler the default disposition skips run()'s cleanup entirely and
        # leaves sway with a latched pointer button that nothing ever releases.
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            self.assertIn(sig, handlers)
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        self.assertTrue(bridge._stop)

    def test_sigterm_mid_drag_releases_the_contact_and_the_devices(self):
        bridge = make_bridge()
        device = FakeDevice()
        bridge._devices[device.path] = device
        bridge._states[device.path] = bridge._fresh_state()
        bridge._states[device.path]["pressed"] = True
        handlers = {}

        def run():
            handlers[signal.SIGTERM](signal.SIGTERM, None)

        with self._patched_bridge(bridge):
            with mock.patch.object(
                signal, "signal", lambda sig, handler: handlers.__setitem__(sig, handler)
            ):
                with mock.patch.object(bridge, "_run", run):
                    self.assertEqual(sdss_inputd.main([]), 0)
        self.assertEqual(bridge.pointer.buttons, [False])
        self.assertTrue(device.closed)
        self.assertEqual(bridge._devices, {})
        self.assertEqual(bridge.pointer.closed, 1)

    def test_a_startup_failure_is_reported_not_raised(self):
        def refuse(*_a, **_k):
            raise RuntimeError("no zwlr_virtual_pointer_manager_v1")

        with mock.patch.object(sdss_inputd, "Bridge", refuse):
            self.assertEqual(sdss_inputd.main([]), 1)


class TestIdleDisplayPolling(unittest.TestCase):
    """Between streams no devices are attached, but the compositor link must stay watched."""

    def test_poll_display_dispatches_when_readable(self):
        bridge = make_bridge()
        with mock.patch("select.poll", return_value=_FakePoll([[(9999, 1)]])):
            bridge._poll_display(1000)
        self.assertEqual(bridge.pointer.dispatches, 1)
        self.assertFalse(bridge._stop)

    def test_poll_display_stops_the_bridge_on_a_protocol_error(self):
        bridge = make_bridge()
        bridge.pointer.dispatch_error = RuntimeError("display gone")
        with mock.patch("select.poll", return_value=_FakePoll([[(9999, 1)]])):
            bridge._poll_display(1000)
        # A dead compositor must surface here rather than at the user's next touch.
        self.assertTrue(bridge._stop)

    def test_poll_display_is_quiet_when_nothing_arrives(self):
        bridge = make_bridge()
        with mock.patch("select.poll", return_value=_FakePoll([[]])):
            bridge._poll_display(1000)
        self.assertEqual(bridge.pointer.dispatches, 0)
        self.assertFalse(bridge._stop)


class _FakePoll:
    """Stand-in for select.poll that replays a scripted sequence of poll() results."""

    def __init__(self, script):
        self._script = script
        self.registered = []

    def register(self, fd, _flags):
        self.registered.append(fd)

    def poll(self, _timeout):
        if not self._script:
            return []
        return self._script.pop(0)


def run_loop(bridge, poll_script, devices):
    """Drive Bridge._run() with a scripted poller, without evdev or a compositor.

    _run imports `evdev` and `select` at call time, which is what makes this possible on a
    machine that has neither.
    """
    import errno as _errno

    polls = [_FakePoll(poll_script)]

    def make_poll():
        # _run builds a fresh poller every pass; keep handing back the same script.
        return polls[0]

    select_stub = types.SimpleNamespace(
        poll=make_poll, POLLIN=1, POLLERR=8, POLLHUP=16, POLLNVAL=32
    )
    evdev_stub = types.SimpleNamespace(ecodes=FakeCodes)

    bridge._devices = dict(devices)
    original_discover = bridge.discover
    bridge.discover = lambda: None
    try:
        with mock.patch.dict(
            sys.modules, {"evdev": evdev_stub, "select": select_stub}, clear=False
        ):
            bridge._run()
    finally:
        bridge.discover = original_discover
    return polls[0], _errno


class TestRunLoop(unittest.TestCase):
    """Covers the poll loop itself: the display fd, hangups, and the EAGAIN race."""

    def setUp(self):
        # These cases deliberately trigger error paths; keep their logging out of the
        # test output so a genuine failure still stands out.
        patcher = mock.patch.object(sdss_inputd, "log")
        self.log = patcher.start()
        self.addCleanup(patcher.stop)

    def _stop_after(self, bridge, passes):
        # Ends the loop deterministically instead of relying on wall-clock time.
        state = {"n": 0}

        def dispatch():
            state["n"] += 1
            bridge.pointer.dispatches += 1
            if state["n"] >= passes:
                bridge._stop = True

        bridge.pointer.dispatch = dispatch

    def test_display_fd_is_polled_and_dispatched(self):
        bridge = make_bridge()
        device = FakeDevice(fd=7)
        self._stop_after(bridge, 1)
        poller, _ = run_loop(bridge, [[(9999, 1)]], {device.path: device})
        self.assertIn(9999, poller.registered)
        self.assertIn(7, poller.registered)
        self.assertEqual(bridge.pointer.dispatches, 1)

    def test_a_display_error_stops_the_loop_instead_of_escaping(self):
        bridge = make_bridge()
        device = FakeDevice(fd=7)
        bridge._states[device.path] = bridge._fresh_state()
        bridge._states[device.path]["pressed"] = True
        bridge.pointer.dispatch_error = RuntimeError("protocol error")
        # Must return rather than propagate; run() then releases held contacts.
        run_loop(bridge, [[(9999, 1)], [(9999, 1)]], {device.path: device})
        self.assertTrue(bridge._stop)
        bridge.close()
        # The user-visible consequence: a dead compositor must not leave the emulator with
        # a stylus permanently pressed against the screen.
        self.assertIn(False, bridge.pointer.buttons)

    def test_eagain_does_not_drop_the_device(self):
        bridge = make_bridge()
        device = FakeDevice(fd=7)
        device.reads = [OSError(errno.EAGAIN, "try again")]
        self._stop_after(bridge, 1)
        run_loop(bridge, [[(7, 1)], [(9999, 1)]], {device.path: device})
        self.assertIn(device.path, bridge._devices)
        self.assertFalse(device.closed)

    def test_hangup_drops_the_device(self):
        bridge = make_bridge()
        device = FakeDevice(path="/dev/input/event20", fd=7)
        # A second device keeps the loop running; with none left it would correctly park
        # in the re-plug sleep and never reach the stop dispatch.
        keeper = FakeDevice(path="/dev/input/event21", fd=8)
        self._stop_after(bridge, 1)
        # POLLHUP (16) is how a recreated uinput node announces itself.
        run_loop(
            bridge,
            [[(7, 16)], [(9999, 1)]],
            {device.path: device, keeper.path: keeper},
        )
        self.assertNotIn(device.path, bridge._devices)
        self.assertTrue(device.closed)
        self.assertIn(keeper.path, bridge._devices)

    def test_an_unexpected_read_error_drops_only_that_device(self):
        bridge = make_bridge()
        good = FakeDevice(path="/dev/input/event20", fd=7)
        bad = FakeDevice(path="/dev/input/event21", fd=8)
        bad.reads = [OSError(errno.EIO, "io error")]
        self._stop_after(bridge, 1)
        run_loop(
            bridge,
            [[(8, 1)], [(9999, 1)]],
            {good.path: good, bad.path: bad},
        )
        self.assertNotIn(bad.path, bridge._devices)
        self.assertIn(good.path, bridge._devices)


class TestPointerTeardown(unittest.TestCase):
    """Bridge.close() must tear the display down, and only after releasing contacts.

    Letting the interpreter garbage-collect the pywayland Display segfaults on exit
    (verified on hardware, docs/spikes/S6-virtual-pointer.md).
    """

    def test_close_closes_the_pointer(self):
        bridge = make_bridge()
        bridge.close()
        self.assertEqual(bridge.pointer.closed, 1)

    def test_held_contact_is_released_before_the_display_goes_away(self):
        bridge = make_bridge()
        device = FakeDevice()
        bridge._devices[device.path] = device
        bridge._states[device.path] = {"pressed": True, "x": 0, "y": 0}
        bridge.close()
        kinds = [kind for kind, _ in bridge.pointer.events]
        self.assertEqual(kinds, ["button", "close"])
        self.assertEqual(bridge.pointer.events[0], ("button", False))

    def test_close_is_idempotent(self):
        bridge = make_bridge()
        bridge.close()
        bridge.close()
        self.assertEqual(bridge.pointer.closed, 2)


if __name__ == "__main__":
    unittest.main()
