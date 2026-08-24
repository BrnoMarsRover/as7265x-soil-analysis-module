"""
The carousel as a state machine, driven through every transition.

THE MECHANISM HAS NO INDEX MARK

There is no limit switch, no Hall sensor and no zero mark. The firmware
knows where the carousel is only because an operator once aligned Slot
1 with the loading hole and said so, and because every movement since
has been commanded in encoder counts and verified by reading the
encoder back.

That makes "where is it" a piece of STATE with exactly two honest
values: a slot number that was verified, or nothing. The dangerous
third value - a slot number that was assumed - is what this suite
exists to prove cannot occur.

THE STATES

    UNSYNCED    no servo, or no origin. Nothing may move.
    SYNCED      an origin was declared against a real encoder reading
    MOVED       a movement completed and was verified
    LOST        a movement failed, or the servo went away, or torque
                was released. Indistinguishable from UNSYNCED by
                design: both mean "ask the operator".

THE INVARIANTS, CHECKED AFTER EVERY SINGLE TRANSITION

    1. position_valid implies both slot numbers are real slots
    2. not position_valid implies no slot number is offered at all
    3. the loader and the scanner are always exactly half a turn apart
    4. nothing moves while the position is invalid
    5. a movement that failed never leaves the position valid
"""

import itertools
import sys
from pathlib import Path

_TESTS_DIR = next(
    p for p in Path(__file__).resolve().parents if p.name == "Tests"
)

if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

_SOFTWARE_DIR = _TESTS_DIR / "software"

if str(_SOFTWARE_DIR) not in sys.path:
    sys.path.insert(0, str(_SOFTWARE_DIR))

import support

support.add_project_root()
support.add_path("PC")

import serial_link                                          # noqa: E402
from serial_link import DeviceError, LinkError               # noqa: E402

from fakes import FakeClock                                  # noqa: E402
from fakes.clock import install_clock                        # noqa: E402
from fakes.esp32 import LoopbackDevice                       # noqa: E402
from fakes.serial_port import install_fake_serial, open_link  # noqa: E402

checks = support.Checks("carousel-states")

restore_serial = install_fake_serial(serial_link)

SLOT_COUNT = 4
SCAN_OFFSET = SLOT_COUNT // 2          # the scanner is half a turn away


def stack(servo=None):
    clock = FakeClock()
    installed = install_clock(serial_link, clock)
    loopback = LoopbackDevice(servo=servo)
    loopback.build()
    link, port = open_link(serial_link, loopback, clock=clock)
    link.online = True

    return link, loopback, installed


def carousel_of(link):
    return link.get_status().get("carousel") or {}


def violations(carousel, where):
    """Every invariant this state breaks. Empty is the only good answer."""
    broken = []

    valid = carousel.get("position_valid")
    load = carousel.get("current_load_slot")
    scan = carousel.get("current_scan_slot")
    selected = carousel.get("selected_slot")

    if valid:
        if not isinstance(load, int) or not 1 <= load <= SLOT_COUNT:
            broken.append("{}: position is valid but the loading slot is "
                          "{!r}".format(where, load))

        if not isinstance(scan, int) or not 1 <= scan <= SLOT_COUNT:
            broken.append("{}: position is valid but the scanner slot is "
                          "{!r}".format(where, scan))

        if isinstance(load, int) and isinstance(scan, int):
            apart = (scan - load) % SLOT_COUNT

            if apart != SCAN_OFFSET:
                broken.append(
                    "{}: loader {} and scanner {} are {} slot(s) apart, "
                    "not {}".format(where, load, scan, apart, SCAN_OFFSET))

    else:
        if scan is not None:
            broken.append("{}: position is INVALID but the scanner slot "
                          "is still reported as {!r}".format(where, scan))

        if load is not None:
            broken.append("{}: position is INVALID but the loading slot "
                          "is still reported as {!r}".format(where, load))

    if selected is not None and not (
            isinstance(selected, int) and 1 <= selected <= SLOT_COUNT):
        broken.append("{}: selected slot is {!r}".format(where, selected))

    return broken


def attempt(call):
    try:
        return "OK", call()

    except DeviceError as error:
        return error.code, None

    except LinkError as error:
        return "LINK:" + error.code, None

    except Exception as error:                         # noqa: BLE001
        return "CRASH:" + type(error).__name__, None


# ======================================================================
checks.section("nothing moves before an origin is declared")

link, loop, installed = stack()

try:
    carousel = carousel_of(link)

    checks.equal(carousel.get("position_valid"), False,
                 "a freshly booted board does not know where the "
                 "carousel is")
    checks.equal(violations(carousel, "boot"), [],
                 "and offers no slot numbers to go with that")

    link.connect_servo()

    carousel = carousel_of(link)
    checks.equal(carousel.get("position_valid"), False,
                 "connecting the servo does NOT establish a position - "
                 "the servo may have been moved by hand while it was "
                 "disconnected")

    # ABSOLUTE commands need an origin; RELATIVE ones do not, and must
    # not.
    #
    # `move_slots` is how the operator gets Slot 1 under the loading
    # hole in the first place - CAROUSEL SETUP options [1] and [2] are
    # exactly this command - so refusing it before an origin exists
    # would make the origin impossible to establish. What it must NOT
    # do is invent a position out of a blind movement.
    for label, call in (
        ("select_slot", lambda: link.select_slot(2)),
        ("measure_raw", lambda: link.measure_raw(2)),
    ):
        code, _ = attempt(call)

        checks.ok(code != "OK",
                  "{} needs an origin and is refused without one "
                  "({})".format(label, code))
        checks.ok(not code.startswith("CRASH"),
                  "and refused by name, not by exception")

    code, _ = attempt(lambda: link.move_slots("cw", 1))

    checks.equal(code, "OK",
                 "a RELATIVE movement is allowed without an origin - it "
                 "is how the operator aligns Slot 1 in the first place")

    blind = carousel_of(link)

    checks.equal(blind.get("position_valid"), False,
                 "and turning the carousel blind does NOT create a "
                 "position: one slot clockwise from an unknown place is "
                 "still an unknown place")
    checks.equal(violations(blind, "after a blind move"), [],
                 "and still offers no slot numbers")

    code, _ = attempt(lambda: link.select_slot(2))
    checks.ok(code != "OK",
              "so an absolute command is still refused afterwards "
              "({})".format(code))

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("every slot, every direction, every step count")

link, loop, installed = stack()

try:
    link.connect_servo()
    link.sync_position(load_slot=1)

    broken = []
    transitions = 0

    for origin in range(1, SLOT_COUNT + 1):
        link.sync_position(load_slot=origin)

        for direction in ("cw", "ccw"):
            for steps in range(1, SLOT_COUNT + 1):
                before = carousel_of(link)
                link.move_slots(direction, steps)
                after = carousel_of(link)
                transitions += 1

                broken.extend(violations(
                    after, "{} {} x{}".format(origin, direction, steps)))

                # The geometry has to move by exactly the number of
                # slots asked for, in the direction asked for.
                start = before.get("current_load_slot")
                end = after.get("current_load_slot")

                if isinstance(start, int) and isinstance(end, int):
                    moved = (end - start) % SLOT_COUNT
                    expected = (steps if direction == "cw"
                                else -steps) % SLOT_COUNT

                    if moved != expected:
                        broken.append(
                            "{} {} x{}: loader went {} -> {} ({} slots), "
                            "expected {}".format(origin, direction, steps,
                                                 start, end, moved,
                                                 expected))

    checks.equal(broken, [],
                 "{} whole-slot transitions, every one landing where it "
                 "said it would".format(transitions))

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("selecting every slot from every position")

link, loop, installed = stack()

try:
    link.connect_servo()

    broken = []
    selections = 0

    for origin in range(1, SLOT_COUNT + 1):
        link.sync_position(load_slot=origin)

        for target in range(1, SLOT_COUNT + 1):
            link.select_slot(target, sample_id="S{}".format(target))
            after = carousel_of(link)
            selections += 1

            broken.extend(violations(
                after, "sync {} select {}".format(origin, target)))

            if after.get("current_load_slot") != target:
                broken.append(
                    "sync {} select {}: the loader ended at {}".format(
                        origin, target, after.get("current_load_slot")))

    checks.equal(broken, [],
                 "{} selections, every one bringing the chosen slot to "
                 "the loading hole".format(selections))

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("fine alignment moves the mechanism, not the numbering")

link, loop, installed = stack()

try:
    link.connect_servo()
    link.sync_position(load_slot=2)

    before = carousel_of(link)

    for degrees in (0.5, -0.5, 3.0, -3.0, 0.0):
        link.fine_adjust(degrees)

    after = carousel_of(link)

    checks.equal(after.get("current_load_slot"),
                 before.get("current_load_slot"),
                 "five fine adjustments do not renumber the loading slot")
    checks.equal(after.get("current_scan_slot"),
                 before.get("current_scan_slot"),
                 "nor the scanner slot")
    checks.equal(after.get("position_valid"), True,
                 "and the position stays valid - a small correction is "
                 "not a lost origin")
    checks.equal(violations(after, "fine adjust"), [], "and holds")

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("every way of losing the position, and what follows")

LOSSES = (
    ("the servo is disconnected", lambda link, servo:
        link.disconnect_servo()),
    ("the servo stops answering", lambda link, servo:
        setattr(servo, "silent", True)),
    ("the servo is stopped", lambda link, servo: link.servo_stop()),
    ("a bus scan is run", lambda link, servo: link.servo_bus_scan()),
)

for label, lose in LOSSES:
    servo = support.FakeST3215()
    link, loop, installed = stack(servo=servo)

    try:
        link.connect_servo()
        link.sync_position(load_slot=1)

        checks.equal(carousel_of(link).get("position_valid"), True,
                     "before: an origin is declared ({})".format(label))

        attempt(lambda: lose(link, servo))

        # A movement is attempted, because some losses only become
        # visible when something is asked of the mechanism.
        attempt(lambda: link.move_slots("cw", 1))

        after = carousel_of(link)

        checks.equal(after.get("position_valid"), False,
                     "after {}: the position is not valid".format(label))
        checks.equal(violations(after, label), [],
                     "and no slot number survives it")

        code, _ = attempt(lambda: link.measure_raw(1))
        checks.ok(code != "OK",
                  "and a measurement is refused until an operator "
                  "re-declares the origin ({})".format(code))

    finally:
        installed.restore()
        link.close()


# ======================================================================
checks.section("recovery is always available, from every lost state")

for label, lose in LOSSES:
    servo = support.FakeST3215()
    link, loop, installed = stack(servo=servo)

    try:
        link.connect_servo()
        link.sync_position(load_slot=1)

        attempt(lambda: lose(link, servo))
        attempt(lambda: link.move_slots("cw", 1))

        # Whatever went wrong, this is the operator's way back: connect
        # the servo, align Slot 1 by hand, say so.
        if getattr(servo, "silent", False):
            servo.silent = False

        attempt(link.connect_servo)
        code, _ = attempt(lambda: link.sync_position(load_slot=1))

        after = carousel_of(link)

        checks.equal(code, "OK",
                     "after {}, re-declaring the origin succeeds"
                     .format(label))
        checks.equal(after.get("position_valid"), True,
                     "and the carousel is usable again")
        checks.equal(violations(after, "recovered " + label), [],
                     "with a consistent geometry")

    finally:
        installed.restore()
        link.close()


# ======================================================================
checks.section("operation sequences: every ordering of the real actions")

# Not a fuzz test - an exhaustive walk of every ORDERING of the
# operator's real actions, three deep. The point is that no order of
# legal operations can produce an illegal state, including the orders
# nobody would choose on purpose.

ACTIONS = {
    "connect": lambda link: link.connect_servo(),
    "sync": lambda link: link.sync_position(load_slot=1),
    "select": lambda link: link.select_slot(3, sample_id="SEQ"),
    "move": lambda link: link.move_slots("ccw", 1),
    "fine": lambda link: link.fine_adjust(1.0),
    "stop": lambda link: link.servo_stop(),
    "release": lambda link: link.servo_torque(False),
    "hold": lambda link: link.servo_torque(True),
    "disconnect": lambda link: link.disconnect_servo(),
}

link, loop, installed = stack()

try:
    broken = []
    crashed = []
    sequences = 0
    steps = 0

    for order in itertools.permutations(sorted(ACTIONS), 3):
        # Each sequence starts from a fresh, known state.
        attempt(link.connect_servo)
        attempt(lambda: link.sync_position(load_slot=1))

        sequences += 1

        for name in order:
            code, _ = attempt(lambda: ACTIONS[name](link))
            steps += 1

            if code.startswith("CRASH"):
                crashed.append("{}: {} raised {}".format(
                    " -> ".join(order), name, code))

            state = carousel_of(link)
            broken.extend(violations(
                state, "{} after {}".format(" -> ".join(order), name)))

    checks.equal(crashed[:5], [],
                 "no ordering of operator actions raises an exception")
    checks.equal(broken[:5], [],
                 "and none of them produces an inconsistent carousel "
                 "state")
    checks.ok(sequences >= 500,
              "{} sequences, {} transitions, all invariants held".format(
                  sequences, steps))

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("the position survives nothing it should not")

# Two specific claims that are easy to get wrong in opposite
# directions: a REFUSED command must not disturb a good position, and
# a FAILED movement must always destroy it.

link, loop, installed = stack()

try:
    link.connect_servo()
    link.sync_position(load_slot=1)

    before = carousel_of(link)

    for label, call in (
        ("an invalid slot", lambda: link.select_slot(99)),
        ("an unknown direction", lambda: link.move_slots("sideways", 1)),
        ("an oversized adjustment", lambda: link.fine_adjust(999.0)),
        ("an unknown command", lambda: link.request("nonsense")),
    ):
        code, _ = attempt(call)
        after = carousel_of(link)

        checks.ok(code != "OK", "{} is refused ({})".format(label, code))
        checks.equal(after.get("position_valid"), True,
                     "and {} does NOT destroy a good position - a "
                     "refusal means nothing happened".format(label))
        checks.equal(after.get("current_load_slot"),
                     before.get("current_load_slot"),
                     "and leaves the slot numbering alone")

finally:
    installed.restore()
    link.close()


restore_serial()

sys.exit(checks.report())
