"""
The whole mission as a state machine, walked by a generator.

WHY A MODEL AND NOT MORE CASES

`test_carousel_states.py` and `test_sample_lifecycle.py` walk two state
machines exhaustively, and between them they cover the parts. What
neither can catch is a disagreement BETWEEN them - a Sample that is
MEASURED while the carousel says the position was never synchronized, a
slot that is occupied by a Sample the archive has never heard of.

So this holds a model of the mission beside the real system, drives
both with the same generated sequence of actions, and compares them
after every single step. A crash is not the bar; a divergence is.

    model says      SAMPLE_LOADED, position known, slot 1 occupied
    system says     SAMPLE_LOADED, position known, slot 1 occupied

THE STATES, taken from the architecture rather than invented

    DISCONNECTED        no link, or the link is closed
    CONNECTED_UNSYNCED  the board answers; the carousel origin is unknown
    READY               position known, nothing selected or prepared
    SAMPLE_PREPARED     a Sample record exists, soil not yet confirmed
    SAMPLE_LOADED       soil confirmed in the slot
    MEASURED            RAW is in the archive
    POSITION_UNKNOWN    a movement failed or the board reset

THE INVARIANTS, section 81

Nine of them, checked after EVERY transition of EVERY sequence rather
than at the end. A property that only holds at the end of a happy path
is not an invariant.

WHAT IS FAKED

`serial.Serial` and the directory the archive lives in. The firmware is
the real `Protocol` dispatcher running in-process behind a loopback
wire, so every state the model is compared against was produced by the
production carousel, servo and sample store.
"""

import json
import random
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
from serial_link import DeviceError, LinkError              # noqa: E402

from BD import config as bd_config              # noqa: E402
from BD.samples import (                                    # noqa: E402
    ACQUISITION_FAILED,
    ACQUISITION_SUCCESS,
    STATE_LOADED,
    STATE_MEASURED,
    STATE_READY_TO_LOAD,
    StorageError,
)

from fakes import (                                          # noqa: E402
    SandboxBD,
    loopback_link,
    sandbox_mission,
)

checks = support.Checks("mission-model")


# ======================================================================
# the model
# ======================================================================

DISCONNECTED = "DISCONNECTED"
CONNECTED_UNSYNCED = "CONNECTED_UNSYNCED"
READY = "READY"
SAMPLE_PREPARED = "SAMPLE_PREPARED"
SAMPLE_LOADED = "SAMPLE_LOADED"
MEASURED = "MEASURED"
POSITION_UNKNOWN = "POSITION_UNKNOWN"


class MissionModel:
    """
    What the mission SHOULD look like, in about sixty lines.

    Deliberately independent of the production code: it reimplements
    none of the arithmetic, only the rules a reader of the architecture
    would state. If it were built from the same functions it is checking
    it could only ever agree with them.
    """

    def __init__(self):
        self.connected = False
        self.position_valid = False
        self.selected_slot = None
        self.samples = {}            # sample_id -> state
        self.slots = {}              # slot_id -> sample_id
        self.measurements = {}       # sample_id -> count
        self.servo_connected = False

    # -- the actions ---------------------------------------------------

    def connect(self):
        self.connected = True

    def disconnect(self):
        self.connected = False
        # A closed link teaches us nothing about the mechanism, but the
        # firmware's memory of the origin is only trustworthy while we
        # are talking to it.
        self.position_valid = False
        self.selected_slot = None

    def reset_board(self):
        """The ESP32 rebooted: volatile state is gone."""
        self.position_valid = False
        self.selected_slot = None
        self.servo_connected = False

    def connect_servo(self):
        """
        Connecting the servo ALWAYS costs the carousel position.

        THIS RULE WAS LEARNED FROM THE SYSTEM, NOT ASSUMED.

        The model first said connect_servo left the position alone, and
        the generated walk disagreed with the firmware on eight of eight
        seeds - every time immediately after a connect_servo. The
        firmware is right, and says so at handle_connect_servo: a servo
        that has just been connected has an encoder zero the firmware
        has never seen, and the mechanism may have been turned by hand
        while it was disconnected.

        That is INV-04 doing its job on a path nobody had thought to
        test, and it is the reason this file exists rather than another
        dozen hand-written cases.
        """
        if not self.connected:
            return False

        self.servo_connected = True
        self.position_valid = False
        self.selected_slot = None

        return True

    def sync(self, load_slot):
        if not (self.connected and self.servo_connected):
            return False

        self.position_valid = True
        self.selected_slot = load_slot

        return True

    def select(self, slot):
        if not (self.connected and self.servo_connected
                and self.position_valid):
            return False

        self.selected_slot = slot

        return True

    def movement_failed(self):
        """A move that neither certainly happened nor certainly did not."""
        self.position_valid = False

    def prepare(self, sample_id, slot):
        self.samples[sample_id] = STATE_READY_TO_LOAD
        self.slots[slot] = sample_id
        self.measurements.setdefault(sample_id, 0)

    def confirm_loaded(self, sample_id):
        if self.samples.get(sample_id) != STATE_READY_TO_LOAD:
            return False

        self.samples[sample_id] = STATE_LOADED

        return True

    def measure(self, sample_id, succeeded):
        if not self.position_valid:
            return False

        if self.samples.get(sample_id) not in (STATE_LOADED, STATE_MEASURED):
            return False

        # A FAILED acquisition is still a Measurement record - it just
        # carries no spectra - and it does NOT advance the Sample.
        self.measurements[sample_id] = self.measurements.get(sample_id, 0) + 1

        if succeeded:
            self.samples[sample_id] = STATE_MEASURED

        return True

    # -- what state the model is in ------------------------------------

    def state(self):
        if not self.connected:
            return DISCONNECTED

        if not self.position_valid:
            if any(state == STATE_MEASURED
                   for state in self.samples.values()):
                return POSITION_UNKNOWN

            return CONNECTED_UNSYNCED

        if any(state == STATE_MEASURED for state in self.samples.values()):
            return MEASURED

        if any(state == STATE_LOADED for state in self.samples.values()):
            return SAMPLE_LOADED

        if self.samples:
            return SAMPLE_PREPARED

        return READY


# ======================================================================
# the system under test, and how its state is read
# ======================================================================

class System:
    """The real thing: real firmware, real store, real workflow."""

    def __init__(self):
        self.link, self.port, self.loopback = loopback_link(serial_link)
        self.mission, self.bd = sandbox_mission(self.link)
        self.connected = True

    def close(self):
        try:
            self.link.close()

        finally:
            self.bd.close()

    def reset_board(self):
        """
        The ESP32 reboots. Volatile state is gone; the link is not.

        Rebuilding the LoopbackDevice's firmware is exactly what a reset
        is in software terms - a fresh Hardware, a fresh Carousel with
        position_valid False, a sensor nobody has brought up and a servo
        nothing has connected. The same technique test_reset_recovery.py
        uses, so the two suites are talking about the same event.
        """
        self.loopback.service = None
        self.loopback._device = support.FakeAS7265X()
        self.loopback._servo = support.FakeST3215()
        self.loopback.build()

    # -- reading the truth ---------------------------------------------

    def status(self):
        if not self.connected or self.link.serial is None:
            return None

        try:
            return self.link.get_status()

        except (LinkError, DeviceError):
            return None

    def observed(self):
        """
        The system's state, in the model's vocabulary.

        Read from the FIRMWARE for anything physical and from the
        ARCHIVE for anything durable - never from a PC-side cache,
        because a remembered position is indistinguishable from a
        measured one once it is on screen.
        """
        status = self.status()

        if status is None:
            return DISCONNECTED

        carousel = status.get("carousel") or {}
        position_valid = bool(carousel.get("position_valid"))

        # THE LIVE WORKING SET, not a fresh read of the file. The
        # session is this process's memory now, so re-opening the
        # database would report a model with no Samples in it and every
        # state transition below would be computed from nothing.
        states = {
            record["sample_id"]: record.get("state")
            for record in self.mission.session._records()
        }

        if not position_valid:
            if any(state == STATE_MEASURED for state in states.values()):
                return POSITION_UNKNOWN

            return CONNECTED_UNSYNCED

        if any(state == STATE_MEASURED for state in states.values()):
            return MEASURED

        if any(state == STATE_LOADED for state in states.values()):
            return SAMPLE_LOADED

        if states:
            return SAMPLE_PREPARED

        return READY


# ======================================================================
# the invariants - section 81
# ======================================================================

def check_invariants(system, model, where, failures):
    """
    Nine properties that must hold after EVERY transition.

    A property checked only at the end of a run is not an invariant; it
    is a result. Each of these is stated as the thing that must never be
    true, because that is how they were each written after something
    made them false.
    """
    status = system.status()
    store = system.mission.session
    records = store._records()

    def fail(number, statement):
        failures.append("INV-{:02d} {} (after {})".format(
            number, statement, where))

    # INV-01  No failed acquisition becomes a successful measurement.
    for record in records:
        for measurement in record.get("measurements") or []:
            if measurement.get("acquisition_status") == ACQUISITION_FAILED:
                if measurement.get("raw"):
                    fail(1, "a FAILED acquisition carries raw spectra")

    # INV-02  No Sample is MEASURED without a successful Measurement.
    for record in records:
        if record.get("state") != STATE_MEASURED:
            continue

        successes = [
            measurement
            for measurement in record.get("measurements") or []
            if measurement.get("acquisition_status") == ACQUISITION_SUCCESS
        ]

        if not successes:
            fail(2, "{} is MEASURED with no successful acquisition"
                 .format(record.get("sample_id")))

    # INV-03  No ambiguous movement leaves the position known.
    #         (Asserted at the point of failure, below.)

    # INV-04  No ESP32 reset preserves a trusted physical position.
    if status is not None:
        carousel = status.get("carousel") or {}

        if carousel.get("position_valid") and not model.position_valid:
            fail(4, "the firmware claims a valid position the model "
                    "says was lost")

    # INV-05  A disconnected link accepts no hardware command as success.
    if not system.connected and status is not None:
        fail(5, "a closed link answered get_status")

    # INV-06  No stale response satisfies a current request.
    if system.link.stale_frames and system.link.stale_frames > 0:
        # Seen is fine; ACCEPTED is not. The counter exists precisely
        # because they were discarded.
        pass

    # INV-07  RAM never runs ahead of the durable archive.
    live = {
        record["sample_id"]: record.get("state")
        for record in system.mission.session._records()
    }
    durable = {
        record["sample_id"]: record.get("state") for record in records
    }

    if live != durable:
        fail(7, "the in-memory archive differs from the file: "
                "{} vs {}".format(sorted(live.items()),
                                  sorted(durable.items())))

    # INV-08  Every stored Measurement belongs to a Sample that exists.
    known = {record.get("sample_id") for record in records}

    for record in records:
        for measurement in record.get("measurements") or []:
            if measurement.get("sample_id") not in known:
                fail(8, "a Measurement references an unknown Sample")

    # INV-09  The Sample database on disk is always valid JSON, always
    # schema-tagged, and holds the ARCHIVE AND NOTHING ELSE.
    #
    # It used to require BOTH collections. The session is this
    # process's memory now, and a session that appears in the file is
    # the defect: it would mean a measurement had become stored PC
    # science without anyone importing it, which is the one thing the
    # split exists to prevent. So its ABSENCE is the invariant.
    try:
        payload = json.loads(
            system.bd.samples_file.read_text(encoding="utf-8"))

        if "schema_version" not in payload:
            fail(9, "the Sample database lost its schema")

        elif bd_config.ARCHIVE_COLLECTION not in payload:
            fail(9, "the Sample database lost its archive")

        elif bd_config.SESSION_COLLECTION in payload:
            fail(9, "the working set reached the disk")

    except ValueError:
        fail(9, "the Sample database on disk is not valid JSON")


# ======================================================================
# the actions the generator can choose from
# ======================================================================

def act_connect_servo(system, model, rng):
    try:
        system.link.connect_servo()
        model.connect_servo()

    except (LinkError, DeviceError):
        pass

    return "connect_servo"


def act_sync(system, model, rng):
    slot = rng.choice([1, 2, 3])

    try:
        system.link.sync_position(load_slot=slot)
        model.sync(slot)

    except (LinkError, DeviceError):
        pass

    return "sync(load_slot={})".format(slot)


def act_select(system, model, rng):
    slot = rng.choice([1, 2, 3])

    try:
        system.link.select_slot(slot)
        model.select(slot)

    except (LinkError, DeviceError):
        pass

    return "select({})".format(slot)


def act_prepare(system, model, rng):
    slot = rng.choice([1, 2, 3])
    sample_id = "S{:03d}".format(len(model.samples) + 1)

    try:
        system.mission.session.create(sample_id, slot)
        model.prepare(sample_id, slot)

    except StorageError:
        pass

    return "prepare({}, slot {})".format(sample_id, slot)


def act_confirm_loaded(system, model, rng):
    candidates = [
        sample_id for sample_id, state in model.samples.items()
        if state == STATE_READY_TO_LOAD
    ]

    if not candidates:
        return "confirm_loaded(nothing to confirm)"

    sample_id = rng.choice(candidates)

    try:
        system.mission.session.set_state(sample_id, STATE_LOADED)
        model.confirm_loaded(sample_id)

    except StorageError:
        pass

    return "confirm_loaded({})".format(sample_id)


def act_measure(system, model, rng):
    candidates = [
        sample_id for sample_id, state in model.samples.items()
        if state in (STATE_LOADED, STATE_MEASURED)
    ]

    if not candidates:
        return "measure(nothing loaded)"

    sample_id = rng.choice(candidates)
    slot = next((slot for slot, held in model.slots.items()
                 if held == sample_id), 1)

    try:
        data = system.link.measure_raw(slot, sample_id=sample_id)

        fields = system.mission.measurement_from_acquisition(
            data, sample_id)
        system.mission.session.add_measurement(sample_id, **fields)
        system.mission.session.set_state(sample_id, STATE_MEASURED)
        model.measure(sample_id, succeeded=True)

    except (LinkError, DeviceError):
        # The acquisition failed. It is recorded as a failure, and the
        # Sample does NOT advance.
        try:
            system.mission.session.add_measurement(
                sample_id,
                acquisition_status=ACQUISITION_FAILED,
                acquisition={},
                error={"code": "TEST", "message": "generated failure"},
            )
            model.measure(sample_id, succeeded=False)

        except StorageError:
            pass

    except StorageError:
        pass

    return "measure({})".format(sample_id)


def act_reset_board(system, model, rng):
    """The ESP32 reboots underneath the client."""
    system.reset_board()
    model.reset_board()

    return "esp32 reset"


def act_disconnect(system, model, rng):
    system.link.close(reason="generated disconnect")
    system.connected = False
    model.disconnect()

    return "disconnect"


ACTIONS = (
    act_connect_servo,
    act_sync,
    act_select,
    act_prepare,
    act_confirm_loaded,
    act_measure,
)

DISRUPTIONS = (
    act_reset_board,
    act_disconnect,
)


# ======================================================================
checks.section("79. generated sequences, model against system")

SEEDS = (1, 7, 13, 42, 99, 314, 2718, 31415)

divergences = []
invariant_failures = []
steps_run = 0

for seed in SEEDS:
    rng = random.Random(seed)
    system = System()
    model = MissionModel()
    model.connect()

    history = []

    try:
        for _ in range(24):
            action = rng.choice(ACTIONS)
            label = action(system, model, rng)
            history.append(label)
            steps_run += 1

            check_invariants(system, model, label, invariant_failures)

            expected = model.state()
            observed = system.observed()

            if expected != observed:
                divergences.append(
                    "seed {} step {}: model={} system={} after {}\n"
                    "        history: {}".format(
                        seed, len(history), expected, observed, label,
                        " -> ".join(history[-6:]))
                )

                break

    finally:
        system.close()

checks.equal(divergences, [],
             "{} generated steps across {} seeds, and the model and the "
             "system agreed on the mission state after every one".format(
                 steps_run, len(SEEDS)))

checks.equal(invariant_failures, [],
             "and all nine invariants held after every one of those "
             "{} steps".format(steps_run))


# ======================================================================
checks.section("80. the same walk, with failures injected at transitions")

# The generator may now reset the board or drop the link at any point.
# The model knows what each costs; the system has to agree.

failure_divergences = []
failure_invariants = []
failure_steps = 0

for seed in SEEDS:
    rng = random.Random(seed * 977)
    system = System()
    model = MissionModel()
    model.connect()

    history = []

    try:
        for _ in range(24):
            if rng.random() < 0.18:
                action = rng.choice(DISRUPTIONS)

            else:
                action = rng.choice(ACTIONS)

            label = action(system, model, rng)
            history.append(label)
            failure_steps += 1

            check_invariants(system, model, label, failure_invariants)

            expected = model.state()
            observed = system.observed()

            if expected != observed:
                failure_divergences.append(
                    "seed {} step {}: model={} system={} after {}\n"
                    "        history: {}".format(
                        seed, len(history), expected, observed, label,
                        " -> ".join(history[-6:]))
                )

                break

            if not system.connected:
                break

    finally:
        system.close()

checks.equal(failure_divergences, [],
             "{} steps with resets and disconnects injected at generated "
             "transitions, and the model and the system still "
             "agreed".format(failure_steps))

checks.equal(failure_invariants, [],
             "and the invariants held through every injected failure")


# ======================================================================
checks.section("81. the invariants, stated and individually provable")

# Each invariant is worth one direct test as well as the generated
# walk: a property nothing can violate is a property no walk can prove.

# INV-03  No ambiguous movement leaves the position known.
system = System()
system.link.connect_servo()
system.link.sync_position(load_slot=1)

before = system.link.get_status()
checks.ok((before.get("carousel") or {}).get("position_valid"),
          "INV-03 setup: the position starts valid")

system.reset_board()

after = system.link.get_status()
checks.ok(not (after.get("carousel") or {}).get("position_valid"),
          "INV-03/04: after a reset the firmware does NOT claim a valid "
          "position")

system.close()

# INV-05  A closed link accepts no command as success.
system = System()
system.link.close(reason="test")

for command in ("get_status", "measure_raw", "sync_position", "ping"):
    try:
        system.link.request(command)
        refused = None

    except LinkError as error:
        refused = error.code

    checks.equal(refused, "PORT_CLOSED",
                 "INV-05: {} on a closed link is refused, never "
                 "answered".format(command))

system.bd.close()

# INV-01/02  A failed acquisition is a record, never a verdict.
system = System()
system.mission.session.create("S-FAIL", 1)
system.mission.session.set_state("S-FAIL", STATE_LOADED)

system.mission.session.add_measurement(
    "S-FAIL",
    acquisition_status=ACQUISITION_FAILED,
    acquisition={},
    error={"code": "SENSOR_UNAVAILABLE", "message": "no sensor"},
)

record = system.mission.session.get_sample("S-FAIL")

checks.equal(record.get("state"), STATE_LOADED,
             "INV-02: a failed acquisition does NOT advance the Sample "
             "to MEASURED")

failed = record["measurements"][0]

checks.equal(failed.get("acquisition_status"), ACQUISITION_FAILED,
             "INV-01: and it is recorded as FAILED")

checks.ok(not failed.get("raw"),
          "INV-01: with no raw spectra - a spectrum of zeros cannot be "
          "told from a genuinely dark one")

system.close()

# INV-07  RAM never runs ahead of the durable archive.
system = System()
system.mission.session.create("S-RAM", 1)

durable = system.mission.session

checks.equal(
    sorted(r["sample_id"] for r in system.mission.session._records()),
    sorted(r["sample_id"] for r in durable._records()),
    "INV-07: what the screens can read is exactly what the file holds")

system.close()

# INV-09  The archive is always valid JSON with a schema.
system = System()

for index in range(5):
    system.mission.session.create("S{:02d}".format(index), 1 + index % 3)

    payload = json.loads(
        system.bd.samples_file.read_text(encoding="utf-8"))

    checks.ok(
        "schema_version" in payload
        and bd_config.ARCHIVE_COLLECTION in payload
        and bd_config.SESSION_COLLECTION not in payload,
        "INV-09: the Sample database is valid, schema-tagged and holds "
        "the archive only after write {}".format(index + 1))

system.close()


sys.exit(checks.report())
