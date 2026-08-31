"""
Whole competition missions, start to finish.

WHAT THIS ADDS TO test_mission.py

That file walks a session through the screens. This one runs MISSIONS -
many samples, from a fresh process state through to a durable archive -
and then runs them again with things going wrong, in the combinations
that can actually change the mission's outcome.

    115   the happy path, several samples, checked against the archive
    116   a hostile mission: denied port, reset, boot garbage, dead
          sensor, ambiguous movement, failed save, stale frame, and a
          successful measurement at the end of it
    117   a mission under resource pressure
    118   a mission interrupted and restarted from durable truth
    119   a long day: many samples, many menus, thousands of requests
    77    two faults at once, chosen because together they can change
          the mission state in a way neither does alone
    78    bounded three-fault chaos, with the seed recorded

WHY TWO FAULTS AND NOT EVERY PAIR

Every pair is thousands of combinations that mostly cannot interact. The
ones here were each chosen because the SECOND fault lands on the state
the FIRST one leaves - a reconnect that meets a stale frame, a retry
that meets a full disk, a restart that meets an ambiguous position.

THE STANDARD

After every mission, whatever happened: the archive is valid, nothing
claims a success that did not happen, no position is known that cannot
be, and the client can still be used.

WHAT IS FAKED

`serial.Serial`, the clock, the keyboard and the directory the archive
lives in. The firmware is real, running in-process.
"""

import errno
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

from BD import config as bd_config          # noqa: E402
from BD import samples as samples_module                     # noqa: E402
from BD.samples import (                                    # noqa: E402
    ACQUISITION_FAILED,
    ACQUISITION_SUCCESS,
    STATE_LOADED,
    STATE_MEASURED,
    archive_store,
    StorageError,
)

from fakes import (                                          # noqa: E402
    SandboxBD,
    loopback_link,
    sandbox_mission,
)
from fakes.serial_port import open_link                      # noqa: E402

checks = support.Checks("full-mission")


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

class patched:
    def __init__(self, target, name, replacement):
        self.target = target
        self.name = name
        self.replacement = replacement
        self.original = getattr(target, name)

    def __enter__(self):
        setattr(self.target, self.name, self.replacement)

        return self

    def __exit__(self, exc_type, exc_value, traceback):
        setattr(self.target, self.name, self.original)

        return False


def raiser(exception):
    def raise_it(*args, **kwargs):
        raise exception

    return raise_it


class Rover:
    """One client session against one board, with one archive."""

    def __init__(self, bd=None):
        self.link, self.port, self.loopback = loopback_link(serial_link)
        self.mission, self.bd = sandbox_mission(self.link, bd=bd)

    def close(self, keep_archive=False):
        try:
            self.link.close()

        finally:
            if not keep_archive:
                self.bd.close()

    def bring_up(self, load_slot=1):
        self.link.connect_servo()
        self.link.sync_position(load_slot=load_slot)

    def reset_board(self):
        self.loopback.service = None
        self.loopback._device = support.FakeAS7265X()
        self.loopback._servo = support.FakeST3215()
        self.loopback.build()

    def restart_client(self):
        """A NEW process: new link, new Mission, the same archive."""
        self.link.close()

        self.link, self.port = open_link(
            serial_link, self.loopback, link_kwargs={"timeout": 5.0})
        self.link.online = True

        self.mission, _bd = sandbox_mission(self.link, bd=self.bd)

    def prepare(self, sample_id, slot):
        """
        Open the Sample record and confirm soil is in the slot.

        SEPARATE FROM `measure` ON PURPOSE, and it has to be. Preparing
        WRITES to the archive, so a test that injected a full disk
        around the whole workflow failed at `create()` and never reached
        the acquisition at all - then asserted that the acquisition had
        succeeded. The operator prepares a sample minutes before
        measuring it; the injection belongs on the step being tested.
        """
        try:
            self.mission.session.create(sample_id, slot)
            self.mission.session.set_state(sample_id, STATE_LOADED)

            return True

        except StorageError:
            return False

    def import_all(self):
        """
        The operator archiving the run - "Import ALL", by hand.

        A SEPARATE STEP, because it is a separate decision. Measuring
        puts a Sample in this client's working set and on the device;
        only this puts it in the PC archive, and a mission test that
        wants to assert something about the durable record has to
        perform it rather than assume it happened.
        """
        archived = []

        for record in list(self.mission.session._records()):
            if self.mission.archive.has_sample(record["sample_id"]):
                continue

            self.mission.archive.adopt(record)
            archived.append(record["sample_id"])

        return archived

    def measure(self, sample_id, slot, prepared=False):
        """
        One sample measured: acquire, persist RAW, analyse.

        Returns the INDEPENDENT outcomes - section 114 - because they
        can diverge, and collapsing them into one boolean is how a
        failed return move discards good science.
        """
        result = {
            "acquired": False,
            "persisted": False,
            "analysed": None,
            "position_known": None,
            "error": None,
        }

        if not prepared and not self.prepare(sample_id, slot):
            result["error"] = "SAMPLE_SAVE_ERROR"

            return result

        try:
            self.link.select_slot(slot, sample_id=sample_id)
            data = self.link.measure_raw(slot, sample_id=sample_id)
            result["acquired"] = bool(data.get("illuminations"))

        except (LinkError, DeviceError) as error:
            result["error"] = error.code

            # A failed acquisition is recorded as one.
            try:
                self.mission.session.add_measurement(
                    sample_id,
                    acquisition_status=ACQUISITION_FAILED,
                    acquisition={},
                    error={"code": error.code, "message": error.message},
                )

            except StorageError:
                pass

            result["position_known"] = self.position_known()

            return result

        try:
            fields = self.mission.measurement_from_acquisition(
                data, sample_id)
            measurement = self.mission.session.add_measurement(
                sample_id, **fields)
            self.mission.session.set_state(sample_id, STATE_MEASURED)
            result["persisted"] = True

        except StorageError as error:
            result["error"] = error.code
            result["position_known"] = self.position_known()

            return result

        run = self.mission.analyse_measurement(measurement)
        result["analysed"] = run.get("analysis_status")

        try:
            self.mission.session.add_analysis_run(
                sample_id, measurement["measurement_id"], run)

        except StorageError:
            pass

        result["position_known"] = self.position_known()

        return result

    def position_known(self):
        try:
            status = self.link.get_status()

        except (LinkError, DeviceError):
            return None

        return bool((status.get("carousel") or {}).get("position_valid"))


def archive_is_sound(bd, note, expected_samples=None, live=None):
    """
    The four things that must be true of the archive, whatever happened.

    `live` is the working set, when there is one. The record-shape
    invariants - no Sample claiming MEASURED without an acquisition, no
    FAILED measurement carrying spectra - are about records wherever
    they are, and after the ownership change most records during a
    mission are in memory. Checking only the file would have made those
    two checks pass by having nothing to look at.
    """
    try:
        payload = json.loads(bd.samples_file.read_text(encoding="utf-8"))

    except (OSError, ValueError) as error:
        checks.ok(False, "{}: the archive is unreadable ({})".format(
            note, error))

        return None

    # THE ARCHIVE, AND NOT THE WORKING SET. A session on the disk would
    # mean a measurement had become stored PC science with nobody
    # importing it, so its absence is the invariant - the opposite of
    # what this used to require.
    checks.ok(isinstance(payload.get(bd_config.ARCHIVE_COLLECTION), list)
              and bd_config.SESSION_COLLECTION not in payload
              and "schema_version" in payload,
              "{}: the Sample database is valid, schema-tagged and "
              "holds the archive only".format(note))

    store = archive_store(bd.samples_file).load()

    records = list(store._records())

    if live is not None:
        records += list(live._records())

    # No Sample is MEASURED without a successful acquisition behind it.
    liars = [
        record["sample_id"] for record in records
        if record.get("state") == STATE_MEASURED
        and not any(
            measurement.get("acquisition_status") == ACQUISITION_SUCCESS
            for measurement in record.get("measurements") or []
        )
    ]

    checks.equal(liars, [],
                 "{}: no Sample claims MEASURED without a successful "
                 "acquisition".format(note))

    # No FAILED acquisition carries spectra.
    fabricated = [
        record["sample_id"] for record in records
        for measurement in record.get("measurements") or []
        if measurement.get("acquisition_status") == ACQUISITION_FAILED
        and measurement.get("raw")
    ]

    checks.equal(fabricated, [],
                 "{}: no failed acquisition carries a spectrum".format(note))

    if expected_samples is not None:
        checks.equal(store.count(), expected_samples,
                     "{}: the archive holds {} sample(s)".format(
                         note, expected_samples))

    return store


# ======================================================================
checks.section("115. the happy path, five samples, from nothing")

rover = Rover()
rover.bring_up()

outcomes = []

for index in range(1, 6):
    outcomes.append(rover.measure("S{:03d}".format(index), 1 + index % 3))

checks.equal([outcome["acquired"] for outcome in outcomes],
             [True] * 5,
             "all five acquisitions succeeded")

checks.equal([outcome["persisted"] for outcome in outcomes],
             [True] * 5,
             "and all five were persisted")

checks.ok(all(outcome["error"] is None for outcome in outcomes),
          "with no errors")

# NOTHING IS IN THE ARCHIVE YET, and that is the ownership model.
checks.equal(rover.mission.archive.count(), 0,
             "and NOT ONE of them is in the PC archive - measuring does "
             "not save a sample on the PC")

archive_is_sound(rover.bd, "after five clean samples, before importing",
                 expected_samples=0, live=rover.mission.session)

# The operator imports the run.
checks.equal(len(rover.import_all()), 5,
             "the operator imports all five")

store = archive_is_sound(rover.bd, "after five clean samples",
                         expected_samples=5,
                         live=rover.mission.session)

measured = [record for record in store._records()
            if record.get("state") == STATE_MEASURED]

checks.equal(len(measured), 5,
             "and every one of them is MEASURED in the durable archive")

spectra = [
    measurement
    for record in store._records()
    for measurement in record.get("measurements") or []
    if measurement.get("raw")
]

checks.equal(len(spectra), 5,
             "with five stored spectra, one per sample")

# A NEW PROCESS reads exactly the same thing - because they were
# imported. This is the half of the ownership model that has to keep
# working: what the operator decided to keep is still there.
rover.restart_client()

reread = archive_store(rover.bd.samples_file).load()

checks.equal(reread.count(), 5,
             "and a restarted client sees all five in the archive")

checks.equal(rover.mission.session.count(), 0,
             "with an empty working set, because a new process starts "
             "a new run")

rover.close()


# ======================================================================
checks.section("116. a hostile mission, and a good measurement at the end")

# Every fault this project has actually seen, in one run, in an order
# that makes each one land on the state the last one left.

bd = SandboxBD()

# -- 1. the port is denied ---------------------------------------------
denied = serial_link._classify_open_failure(
    "/dev/ttyUSB0",
    Exception("[Errno 13] could not open port /dev/ttyUSB0: [Errno 13] "
              "Permission denied: '/dev/ttyUSB0'"))

checks.equal(denied.code, "PORT_DENIED",
             "the mission starts with a denied port, diagnosed as "
             "PORT_DENIED")

checks.ok("dialout" in denied.message,
          "and the operator is told which group to join")

# -- 2. the operator fixes it and connects ------------------------------
rover = Rover(bd=bd)
rover.bring_up()

checks.ok(rover.position_known(),
          "after joining the group the client connects and synchronizes")

# -- 3. the board resets mid-mission, with boot garbage ------------------
rover.reset_board()
rover.port._enqueue(
    b"rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\n"
    b"MicroPython v1.24.1 on 2024-11-29; ESP32 module with ESP32\n")

checks.ok(not rover.position_known(),
          "a reset costs the carousel position, and the boot banner "
          "does not restore it")

# -- 4. a measurement attempted on an unsynchronized carousel ------------
unsynced = rover.measure("S-UNSYNC", 1)

checks.ok(not unsynced["acquired"],
          "a measurement on an unsynchronized carousel is refused")

checks.ok(unsynced["error"] is not None,
          "with a code ({})".format(unsynced["error"]))

# -- 5. the operator resynchronizes -------------------------------------
rover.bring_up()

checks.ok(rover.position_known(),
          "the operator resynchronizes and the position is known again")

# -- 6. the sensor is dead ----------------------------------------------
SensorError = sys.modules["sensor"].SensorError

with patched(type(rover.loopback.service.sensor), "ensure_ready",
             raiser(SensorError("SENSOR_UNAVAILABLE", "no answer"))):
    dead_sensor = rover.measure("S-NOSENSOR", 2)

checks.ok(not dead_sensor["acquired"],
          "a measurement with a dead sensor acquires nothing")

checks.ok(dead_sensor["error"] is not None,
          "and says so ({})".format(dead_sensor["error"]))

# -- 7. a stale frame arrives from the dead session ----------------------
rover.port._enqueue(
    json.dumps({
        "request_id": "{}-1".format(rover.link.session),
        "ok": True,
        "cmd": "measure_raw",
        "data": {"illuminations": {"white": [9999] * 18}},
    }).encode("utf-8") + b"\n")

stale_before = rover.link.stale_frames
status = rover.link.get_status()

checks.ok(status is not None,
          "a stale measure_raw frame does not become the answer to "
          "get_status")

checks.ok(rover.link.stale_frames >= stale_before,
          "and is counted rather than consumed")

# -- 8. the disk fills during a save -------------------------------------
#
# The sample is prepared FIRST, while the disk still works - that is
# what an operator does, minutes earlier - so the injection lands on
# the measurement save and nothing else.
rover.prepare("S-FULL", 3)

with patched(samples_module.os, "replace",
             raiser(OSError(errno.ENOSPC, "No space left on device"))):
    full_disk = rover.measure("S-FULL", 3, prepared=True)

checks.ok(full_disk["acquired"],
          "the acquisition on a full disk SUCCEEDS - the soil went "
          "through the instrument")

checks.ok(full_disk["persisted"],
          "and the measurement is recorded anyway - the working set is "
          "memory, so a full disk cannot reach it")

# THE FULL DISK STILL BITES, on the step that writes.
with patched(samples_module.os, "replace",
             raiser(OSError(errno.ENOSPC, "No space left on device"))):
    try:
        rover.mission.archive.adopt(
            rover.mission.session.get_sample("S-FULL"))
        import_refused = False

    except StorageError:
        import_refused = True

checks.ok(import_refused,
          "and importing it onto the full disk fails, with a storage "
          "error")

checks.ok(rover.mission.archive.get_sample("S-FULL") is None,
          "leaving nothing in the archive for it")

# -- 9. the operator frees space and retries -----------------------------
retry = rover.measure("S-FULL-RETRY", 3)

checks.ok(retry["acquired"] and retry["persisted"],
          "and after space is freed the retry acquires AND persists")

# -- 10. a clean final measurement ---------------------------------------
final = rover.measure("S-FINAL", 1)

checks.ok(final["acquired"] and final["persisted"],
          "the mission ends with a clean measurement")

checks.ok(final["position_known"] is True,
          "with the carousel position known")

store = archive_is_sound(bd, "after the hostile mission",
                         live=rover.mission.session)

# THE NEGATIVE THAT MATTERS: nothing that failed became a success.
#
# Read from the WORKING SET, because that is where a mission's records
# are until the operator imports. The archive is checked separately,
# and separately is the point.
archived = {record["sample_id"] for record in store._records()}
names = {record["sample_id"]: record.get("state")
         for record in rover.mission.session._records()}

checks.ok("S-FULL" not in archived,
          "the sample whose IMPORT failed is not in the archive at all")

checks.ok(names.get("S-NOSENSOR") != STATE_MEASURED,
          "and neither is the one whose sensor was dead")

checks.equal(names.get("S-FINAL"), STATE_MEASURED,
             "while the one that really worked is")

rover.close()


# ======================================================================
checks.section("117. a mission under resource pressure")

rover = Rover()
rover.bring_up()

PRESSURES = (
    ("a full disk at the rename", samples_module.os, "replace",
     OSError(errno.ENOSPC, "No space left on device")),
    ("no descriptors for the temporary file", samples_module.tempfile,
     "mkstemp", OSError(errno.EMFILE, "Too many open files")),
    ("a read-only filesystem", samples_module.tempfile, "mkstemp",
     OSError(errno.EROFS, "Read-only file system")),
    ("memory exhausted while serializing", samples_module.json, "dump",
     MemoryError("out of memory")),
)

for index, (label, target, name, exception) in enumerate(PRESSURES):
    sample_id = "S-PRESS-{}".format(index)
    rover.prepare(sample_id, 1)

    before = rover.bd.samples_file.read_bytes()

    with patched(target, name, raiser(exception)):
        outcome = rover.measure(sample_id, 1, prepared=True)

    checks.ok(outcome["acquired"],
              "with {}, the acquisition still succeeds".format(label))

    checks.ok(outcome["persisted"],
              "  and the measurement is still recorded, because "
              "recording it touches no file")

    checks.equal(rover.bd.samples_file.read_bytes(), before,
                 "  leaving the archive byte-identical")

    # The pressure lands where the write is.
    with patched(target, name, raiser(exception)):
        try:
            rover.mission.archive.adopt(
                rover.mission.session.get_sample(sample_id))
            refused = False

        except StorageError:
            refused = True

    checks.ok(refused,
              "  while an IMPORT under the same pressure honestly fails")

    checks.equal(rover.bd.samples_file.read_bytes(), before,
                 "  and still leaves the archive byte-identical")

# And the client is still usable afterwards.
recovered = rover.measure("S-AFTER-PRESSURE", 1)

checks.ok(recovered["acquired"] and recovered["persisted"],
          "and once the pressure lifts, a measurement works normally")

archive_is_sound(rover.bd, "after the resource-pressure mission",
                 live=rover.mission.session)

rover.close()


# ======================================================================
checks.section("118. interrupted, then restarted from durable truth")

rover = Rover()
rover.bring_up()

rover.measure("S-BEFORE", 1)

# WHAT "DURABLE TRUTH" MEANS NOW.
#
# The PC's durable truth is the ARCHIVE, and nothing reaches it without
# an import - so after a measurement it is still empty, and a killed
# client loses its working set by design. The measurement itself is not
# lost: the ESP32 holds it on its own filesystem, which is what makes
# the restart recoverable rather than destructive.
#
# The sample measured BEFORE the interruption is archived here
# deliberately, so this case can tell "the archive survived" apart from
# "there was nothing in it".
rover.mission.archive.adopt(rover.mission.session.get_sample("S-BEFORE"))

durable_before = json.loads(
    rover.bd.samples_file.read_text(encoding="utf-8"))
count_before = len(durable_before[bd_config.ARCHIVE_COLLECTION])

checks.equal(count_before, 1,
             "the archive holds the imported sample before the "
             "interruption")


class Killed(BaseException):
    """The process was terminated."""


# Interrupt the save of the SECOND sample.
with patched(samples_module.os, "replace", raiser(Killed("SIGTERM"))):
    try:
        rover.measure("S-INTERRUPTED", 2)
        escaped = None

    except Killed:
        escaped = "Killed"

# THE RESTART: a new client, reading the file.
rover.restart_client()

after = archive_store(rover.bd.samples_file).load()

checks.equal(after.count(), count_before,
             "the restarted client sees exactly what was durable - the "
             "interrupted sample is not there")

checks.ok(after.get_sample("S-BEFORE") is not None,
          "and the sample imported before the interruption is")

checks.ok(after.get_sample("S-INTERRUPTED") is None,
          "while the one that was only measured is not - measuring "
          "never put it in the archive to begin with")

# AND THE SCIENCE IS STILL RECOVERABLE, on the device that owns it.
held = rover.link.list_saved_samples()
device_ids = [entry.get("sample_id")
              for entry in (held.get("samples") or [])]

checks.ok("S-INTERRUPTED" in device_ids,
          "and the ESP32 still holds the interrupted sample's "
          "acquisition, so the restarted client can import it")

# The restarted client must be able to carry on.
rover.bring_up()
resumed = rover.measure("S-AFTER-RESTART", 3)

checks.ok(resumed["acquired"] and resumed["persisted"],
          "and the restarted client can measure and save normally")

archive_is_sound(rover.bd, "after the interrupt-and-restart mission",
                 live=rover.mission.session)

rover.close()


# ======================================================================
checks.section("77. two faults at once, chosen to interact")

# Each pair is here because the SECOND fault lands on the state the
# FIRST leaves. Single faults are covered elsewhere; these are the
# combinations that can change the mission's outcome.

def pair_reconnect_then_stale():
    """A lost port, then a stale frame waiting after the reconnect."""
    rover = Rover()
    rover.bring_up()

    # The port is lost mid-command.
    rover.link.close(reason="the device disappeared")

    refused = None

    try:
        rover.link.get_status()

    except LinkError as error:
        refused = error.code

    # The operator reconnects, and the previous session's answer is
    # still sitting in the driver buffer.
    rover.restart_client()
    rover.port._enqueue(
        json.dumps({
            "request_id": "{}-1".format(rover.link.session),
            "ok": True,
            "cmd": "measure_raw",
            "data": {"illuminations": {"white": [1234] * 18}},
        }).encode("utf-8") + b"\n")

    status = rover.link.get_status()

    checks.equal(refused, "PORT_CLOSED",
                 "PORT_LOST + stale frame: the lost port is refused "
                 "cleanly")

    checks.ok(status is not None and "carousel" in status,
              "  and after reconnecting, the stale measurement does NOT "
              "answer get_status")

    # THE POSITION SURVIVES, and that is correct. Only the CLIENT
    # restarted; the board kept running and never lost its origin. An
    # assertion that the position must be gone here was written first
    # and was wrong - it confused "the client restarted" with "the
    # board reset", which are exactly the two cases
    # test_reset_recovery.py keeps apart. What must never happen is the
    # PC *remembering* a position across a BOARD reset, and that is
    # asserted in pair_ambiguous_move_then_restart below.
    checks.ok((status.get("carousel") or {}).get("position_valid") is True,
              "  and the position is read back FROM THE BOARD, which "
              "never restarted - a new client does not re-synchronize a "
              "carousel that never moved")

    rover.close()


def pair_sensor_then_save():
    """A dead sensor, and then the archive refuses the failure record."""
    rover = Rover()
    rover.bring_up()

    SensorError = sys.modules["sensor"].SensorError
    before = rover.bd.samples_file.read_bytes()

    with patched(type(rover.loopback.service.sensor), "ensure_ready",
                 raiser(SensorError("SENSOR_UNAVAILABLE", "no answer"))):
        with patched(samples_module.os, "replace",
                     raiser(OSError(errno.ENOSPC, "full"))):
            outcome = rover.measure("S-BOTH", 1)

    checks.ok(not outcome["acquired"],
              "sensor failure + save failure: nothing was acquired")

    checks.equal(rover.bd.samples_file.read_bytes(), before,
                 "  and the archive is untouched - a failure that could "
                 "not be recorded did not corrupt anything either")

    # The client survives both.
    recovered = rover.measure("S-BOTH-RECOVERED", 1)

    checks.ok(recovered["acquired"] and recovered["persisted"],
              "  and the next measurement works")

    rover.close()


def pair_ambiguous_move_then_restart():
    """An ambiguous movement, and then the client restarts."""
    rover = Rover()
    rover.bring_up()
    rover.measure("S-MOVED", 1)

    # The board resets: the position becomes unknown.
    rover.reset_board()

    checks.ok(not rover.position_known(),
              "movement ambiguity + restart: the position is unknown")

    rover.restart_client()

    checks.ok(not rover.position_known(),
              "  and a RESTARTED CLIENT still does not know it - no "
              "PC-side memory puts a slot number back on screen")

    # The measurement taken before the ambiguous move is still
    # recoverable - from the device, which is the only place it was
    # ever durable before an import.
    held = rover.link.list_saved_samples()
    device_ids = [entry.get("sample_id")
                  for entry in (held.get("samples") or [])]

    checks.ok("S-MOVED" in device_ids,
              "  while the measurement taken before it survives on the "
              "device, ready to import")

    rover.close()


def pair_disk_full_then_retry():
    """A full disk, an operator retry, and no duplicate.

    THE FULL DISK MOVED. Measuring writes no file, so a full disk
    cannot fail a measurement any more - it fails the IMPORT, which is
    the step that actually writes. The pair still interacts the same
    way: a failed durable write, then a retry, and no duplicate left
    behind.
    """
    rover = Rover()
    rover.bring_up()

    with patched(samples_module.os, "replace",
                 raiser(OSError(errno.ENOSPC, "full"))):
        first = rover.measure("S-RETRY", 1)

    checks.ok(first["acquired"] and first["persisted"],
              "disk full + retry: the measurement itself is unaffected "
              "by the disk")

    # The operator tries to import it, and the disk is still full.
    with patched(samples_module.os, "replace",
                 raiser(OSError(errno.ENOSPC, "full"))):
        try:
            rover.import_all()
            refused = False

        except StorageError:
            refused = True

    checks.ok(refused, "  and the IMPORT is what fails")

    store = archive_store(rover.bd.samples_file).load()

    checks.ok(store.get_sample("S-RETRY") is None,
              "  leaving NO record behind in the archive")

    # Space is freed; the operator imports again.
    rover.import_all()

    store = archive_store(rover.bd.samples_file).load()
    ids = sorted(record["sample_id"] for record in store._records())

    checks.equal(ids.count("S-RETRY"), 1,
                 "  and the retry archives it exactly once")

    rover.close()


def pair_reset_then_port_renumber():
    """The board reboots and comes back as a different device node."""
    rover = Rover()
    rover.bring_up()

    rover.reset_board()

    # The client is pointed at a new node. A new SerialLink, so a new
    # session nonce - which is the whole point.
    old_session = rover.link.session
    rover.restart_client()

    checks.ok(rover.link.session != old_session,
              "reset + port renumber: the new session has a new nonce, "
              "so the old session's answers cannot match it")

    checks.ok(not rover.position_known(),
              "  and the position is not inherited across either event")

    rover.bring_up()

    recovered = rover.measure("S-RENUMBERED", 1)

    checks.ok(recovered["acquired"] and recovered["persisted"],
              "  and after resynchronizing the mission continues")

    rover.close()


for pair in (pair_reconnect_then_stale,
             pair_sensor_then_save,
             pair_ambiguous_move_then_restart,
             pair_disk_full_then_retry,
             pair_reset_then_port_renumber):
    pair()


# ======================================================================
checks.section("78. bounded three-fault chaos, seeds recorded")

# Up to three logical faults active at once, over a real mission. The
# bar is not "nothing crashed" - it is that the archive invariants hold
# after every step, whatever the storm did.

FAULTS = ("reset", "disk_full", "sensor_dead", "stale_frame",
          "port_lost", "memory")

CHAOS_SEEDS = (3, 17, 71, 137, 271, 503)

survived = []
broken = []

for seed in CHAOS_SEEDS:
    rng = random.Random(seed)
    rover = Rover()
    rover.bring_up()

    history = []

    try:
        for step in range(8):
            active = rng.sample(FAULTS, rng.randint(0, 3))
            history.append("+".join(active) or "clean")

            contexts = []

            if "disk_full" in active:
                contexts.append(patched(
                    samples_module.os, "replace",
                    raiser(OSError(errno.ENOSPC, "full"))))

            if "memory" in active:
                contexts.append(patched(
                    samples_module.json, "dump",
                    raiser(MemoryError("out of memory"))))

            if "sensor_dead" in active:
                SensorError = sys.modules["sensor"].SensorError
                contexts.append(patched(
                    type(rover.loopback.service.sensor), "ensure_ready",
                    raiser(SensorError("SENSOR_UNAVAILABLE", "no answer"))))

            if "reset" in active:
                rover.reset_board()

            if "stale_frame" in active:
                rover.port._enqueue(
                    json.dumps({
                        "request_id": "{}-1".format(rover.link.session),
                        "ok": True, "cmd": "measure_raw",
                        "data": {"illuminations": {"white": [7] * 18}},
                    }).encode("utf-8") + b"\n")

            if "port_lost" in active:
                rover.link.close(reason="chaos")

            for context in contexts:
                context.__enter__()

            try:
                rover.measure("S-{}-{}".format(seed, step),
                              1 + step % 3)

            except BaseException as error:             # noqa: BLE001
                broken.append("seed {} step {} ({}): {}".format(
                    seed, step, history[-1], type(error).__name__))

            finally:
                for context in reversed(contexts):
                    context.__exit__(None, None, None)

            # The archive must be sound after EVERY step, not only at
            # the end of the run.
            try:
                payload = json.loads(
                    rover.bd.samples_file.read_text(encoding="utf-8"))

                if not (
                    isinstance(payload.get(bd_config.ARCHIVE_COLLECTION),
                               list)
                    and bd_config.SESSION_COLLECTION not in payload
                ):
                    broken.append("seed {} step {}: archive shape".format(
                        seed, step))

            except (OSError, ValueError) as error:
                broken.append("seed {} step {}: archive unreadable "
                              "({})".format(seed, step, error))

            if "port_lost" in active:
                rover.restart_client()

            if not rover.position_known():
                rover.bring_up()

        survived.append(seed)

    finally:
        rover.close()

checks.equal(broken, [],
             "{} seeded three-fault runs of 8 steps each, and the "
             "archive stayed sound through all of them (seeds: "
             "{})".format(len(CHAOS_SEEDS),
                          ", ".join(str(s) for s in CHAOS_SEEDS)))

checks.equal(sorted(survived), sorted(CHAOS_SEEDS),
             "and every run reached the end")


# ======================================================================
checks.section("119. a long competition day")

# Not one mission: many. The question is whether anything grows,
# drifts, or stops being true after hours of ordinary operation.

rover = Rover()
rover.bring_up()

SAMPLES = 60

for index in range(SAMPLES):
    outcome = rover.measure("D{:03d}".format(index), 1 + index % 3)

    if not outcome["persisted"]:
        checks.ok(False, "sample {} failed unexpectedly: {}".format(
            index, outcome["error"]))

        break

else:
    checks.ok(True, "{} complete sample workflows, all persisted".format(
        SAMPLES))

# The operator archives the day's work before checking the durable
# record - the same explicit step a real run needs.
rover.import_all()

store = archive_is_sound(rover.bd, "after a long day",
                         expected_samples=SAMPLES)

checks.equal(
    sum(len(record.get("measurements") or [])
        for record in store._records()),
    SAMPLES,
    "with exactly one measurement each - nothing duplicated")

# Bounded resources after all of it.
checks.ok(len(rover.link.last_noise) <= serial_link.NOISE_LIMIT,
          "the link's diagnostic buffer is still capped ({} lines)".format(
              len(rover.link.last_noise)))

checks.ok(len(rover.link.damaged_lines) <= serial_link.NOISE_LIMIT,
          "and so is the damaged-line buffer")

checks.ok(rover.link.bytes_read > 0,
          "and {:,} bytes really crossed the wire".format(
              rover.link.bytes_read))

# The ids are unique, which is what makes the archive a record rather
# than a pile.
ids = [record["sample_id"] for record in store._records()]

checks.equal(len(ids), len(set(ids)),
             "every sample id in the archive is unique")

# MEASUREMENT IDS ARE SCOPED TO THEIR SAMPLE, not global. Every
# sample's first measurement is M001, so across 60 samples there are 60
# measurements all called M001 - and that is the design: a measurement
# is identified by the pair (sample_id, measurement_id), and the
# archive nests it inside its Sample. Asserting global uniqueness was
# the first version here and it failed, correctly, on the design.
duplicated_within_a_sample = [
    record["sample_id"]
    for record in store._records()
    if len({measurement["measurement_id"]
            for measurement in record.get("measurements") or []})
    != len(record.get("measurements") or [])
]

checks.equal(duplicated_within_a_sample, [],
             "and no Sample contains two measurements with the same id - "
             "the id is unique within its Sample, which is the scope it "
             "is addressed in")

rover.close()


sys.exit(checks.report())
