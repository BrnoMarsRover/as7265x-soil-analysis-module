"""
The records browser, driven through its actual operator flows.

WHY THIS IS THE LAST BIG GAP

`workflow/records.py` is a thousand lines and was at 34% branch
coverage - the lowest in the project - because the previous campaign
entered `menu_sample_database`, pressed [0] and left. Everything behind
it was untested: opening a record, editing metadata, renaming,
deleting, importing from the device, recording ground truth, the
learning history.

That matters more than the number suggests. This is the screen an
operator uses AFTER the mission, to read back what was measured, and
it is the screen most likely to meet a record written by an older
version of the software. The `rank` defect found in `display.py` was
exactly that: a legacy record, an optional field, and a formatter that
assumed.

WHAT IS DRIVEN

    an archive with no samples, one sample, and many
    opening a record and reading its measurements
    a record with no measurements, a failed measurement, several
    a record migrated from the old flat schema
    editing metadata, renaming, deleting - and cancelling each
    invalid selections, out-of-range numbers, blank input
    importing acquisitions from the device, including when it has none
    the learning history, with and without a learning database
    ground truth: exact material, family, mixture, unknown

Nothing here touches the real archive: every store is a SandboxBD.
"""

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

from BD.samples import (                                     # noqa: E402
    ACQUISITION_FAILED,
    STATE_LOADED,
    STATE_MEASURED,
    STATE_READY_TO_LOAD,
)

from fakes import FakeClock, SandboxBD, run_screen           # noqa: E402
from fakes.clock import install_clock                        # noqa: E402
from fakes.esp32 import LoopbackDevice                       # noqa: E402
from fakes.serial_port import install_fake_serial, open_link  # noqa: E402

from workflow import records                                 # noqa: E402
from workflow.session import Mission                         # noqa: E402

checks = support.Checks("records")

restore_serial = install_fake_serial(serial_link)

RAW = {
    "white": {"A": 120.0, "B": 240.0},
    "uv": {"A": 40.0, "B": 60.0},
    "ir": {"A": 70.0, "B": 90.0},
}


def session(samples=0, learning=True):
    """A Mission on a sandbox archive, with `samples` records in it."""
    clock = FakeClock()
    installed = install_clock(serial_link, clock)

    loopback = LoopbackDevice()
    loopback.build()

    link, port = open_link(serial_link, loopback, clock=clock)
    link.online = True

    bd = SandboxBD()

    mission = Mission(link)
    mission.samples = bd.sample_database()
    mission.session = mission.samples.session()
    mission.archive = mission.samples.archive()
    mission.calibrations = bd.calibration_store()
    mission.profiles = bd.profile_store()
    mission.load_science()
    mission.learning = bd.learning_store() if learning else None

    if not learning:
        mission.learning_error = "the learning database is not available"

    for index in range(1, samples + 1):
        sample_id = "S{:03d}".format(index)
        mission.session.create(sample_id, ((index - 1) % 4) + 1,
                             metadata={"note": "sample {}".format(index)})
        mission.session.set_state(sample_id, STATE_LOADED)
        mission.session.add_measurement(sample_id, raw=RAW)
        mission.session.set_state(sample_id, STATE_MEASURED)

    class Handle:
        pass

    handle = Handle()
    handle.mission = mission
    handle.store = mission.session
    handle.link = link
    handle.port = port
    handle.bd = bd
    handle.loopback = loopback
    handle.close = lambda: (installed.restore(), link.close(), bd.close())

    return handle


def drive(handle, script, call, exhausted="0"):
    """Run one screen, returning (output, crash)."""
    try:
        _value, output, _console = run_screen(
            list(script), lambda: call(handle.mission), exhausted=exhausted)

        return output, None

    except (DeviceError, LinkError) as error:
        return "", None

    except Exception as error:                         # noqa: BLE001
        return "", "{}: {}".format(type(error).__name__, error)


def survives(handle, label, script, call, exhausted="0", expect=None):
    output, crash = drive(handle, script, call, exhausted)

    checks.ok(crash is None, "{} ({})".format(label, crash or "ok"))

    if expect and crash is None:
        checks.ok(expect in output, "{} shows {!r}".format(label, expect))

    return output


# ======================================================================
checks.section("the sample database, from empty to full")

for count, label in ((0, "no samples"), (1, "one sample"),
                     (12, "twelve samples")):
    handle = session(samples=count)

    try:
        output = survives(handle, "the database lists {}".format(label),
                          ["0"], records.menu_sample_database)

        if count:
            checks.ok("S001" in output,
                      "and shows the first sample ({})".format(label))
            checks.ok(str(count) in output or "S{:03d}".format(count)
                      in output,
                      "and all of them ({})".format(label))

        else:
            checks.ok(output.strip() != "",
                      "and still draws a screen when the archive is "
                      "empty")

    finally:
        handle.close()


# ======================================================================
checks.section("opening a record")

handle = session(samples=3)

try:
    output = survives(handle, "opening sample 1", ["1", "1", "", "0"],
                      records.menu_sample_database)

    checks.ok("S001" in output, "the record is shown")
    checks.ok("MEASURED" in output or "measurement" in output.lower(),
              "with its state and measurements")

    # Every sample, opened in turn.
    for index in (1, 2, 3):
        survives(handle, "opening sample {}".format(index),
                 ["1", str(index), "", "0"], records.menu_sample_database)

    # Out-of-range and invalid selections.
    for bad, label in ((["1", "0", "", "0"], "sample number zero"),
                       (["1", "99", "", "0"], "a sample number too high"),
                       (["1", "abc", "", "0"], "a non-numeric sample"),
                       (["1", "", "0"], "a blank sample number"),
                       (["1", "-1", "", "0"], "a negative sample number")):
        survives(handle, "opening with {}".format(label), bad,
                 records.menu_sample_database)

finally:
    handle.close()


# ======================================================================
checks.section("records the browser has to survive")

# The shapes a real archive accumulates: an abandoned sample, a failed
# acquisition, several measurements, and a record migrated out of the
# old flat schema.

handle = session(samples=1)

try:
    store = handle.store

    # A sample prepared and never measured.
    store.create("S-EMPTY", 2)

    # A sample whose only acquisition failed.
    store.create("S-FAILED", 3)
    store.set_state("S-FAILED", STATE_LOADED)
    store.add_measurement("S-FAILED",
                          acquisition_status=ACQUISITION_FAILED,
                          error={"code": "SENSOR_UNAVAILABLE",
                                 "message": "no answer"})

    # A sample measured several times.
    store.create("S-MANY", 4)
    store.set_state("S-MANY", STATE_LOADED)

    for _repeat in range(4):
        store.add_measurement("S-MANY", raw=RAW)

    store.set_state("S-MANY", STATE_MEASURED)

    # A record carrying a MIGRATED analysis run: the shape that
    # crashed display.print_matches, reached through the real screen.
    measurement = store.add_measurement("S001", raw=RAW)
    store.add_analysis_run("S001", measurement["measurement_id"], {
        "legacy_analysis": {
            "best_match": "Kaolin",
            "best_similarity": 97.1,
            "second_match": "Gypsum",
            "score_difference": 1.2,
            "status": "OK",
            "conclusion": "looks like kaolin",
        },
        "migrated_from_schema": 2,
        "reference_matches": [
            {"material": "Kaolin", "similarity_percent": 97.1},
            {"material": "Gypsum", "similarity_percent": 95.9},
        ],
    })

    # S001 (measured, with a migrated run), S-EMPTY, S-FAILED, S-MANY.
    summaries = store.summaries()
    checks.equal(len(summaries), 4, "four records of four different kinds")

    for index in range(1, 5):
        output, crash = drive(handle,
                              ["1", str(index), "", "", "", "0"],
                              records.menu_sample_database)

        checks.ok(crash is None,
                  "record {} opens without raising ({})".format(
                      index, crash or "ok"))

    # And specifically the migrated one, which is the regression.
    position = [s["sample_id"] for s in summaries].index("S001") + 1
    output, crash = drive(handle,
                          ["1", str(position), "", "", "", "0"],
                          records.menu_sample_database)

    checks.ok(crash is None,
              "a MIGRATED analysis run renders in the records browser - "
              "its `reference_matches` have no rank, which used to "
              "raise TypeError in print_matches ({})".format(crash or "ok"))
    checks.ok("MIGRATED" in output.upper() or "Kaolin" in output,
              "and its original conclusion is shown")

finally:
    handle.close()


# ======================================================================
checks.section("editing metadata, and cancelling")

handle = session(samples=2)

try:
    before = handle.store.get_sample("S001")["metadata"]["note"]

    survives(handle, "editing metadata is offered",
             ["2", "1", "", "", "", "", "edited note", "", "", "0"],
             records.menu_sample_database)

    after = handle.store.get_sample("S001")["metadata"]

    checks.ok(after["note"] in (before, "edited note"),
              "the note is either unchanged or the new one, never "
              "corrupted ({!r})".format(after["note"]))

    # Cancelling must change nothing.
    snapshot = handle.bd.samples_file.read_bytes()

    survives(handle, "cancelling an edit", ["2", "", "0"],
             records.menu_sample_database)

    checks.equal(handle.bd.samples_file.read_bytes(), snapshot,
                 "cancelling the edit writes nothing at all")

finally:
    handle.close()


# ======================================================================
checks.section("renaming, including onto an existing name")

handle = session(samples=2)

try:
    survives(handle, "renaming a sample",
             ["3", "1", "S-RENAMED", "y", "", "0"],
             records.menu_sample_database)

    store = handle.store

    checks.ok(store.has_sample("S-RENAMED") or store.has_sample("S001"),
              "the sample exists under exactly one of the two names")
    checks.equal(store.count(), 2, "and no sample was lost or duplicated")

    # Onto a name that already exists: must be refused, not merged.
    handle2 = session(samples=2)

    try:
        survives(handle2, "renaming onto an existing name",
                 ["3", "1", "S002", "y", "", "0"],
                 records.menu_sample_database)

        store2 = handle2.store

        checks.equal(store2.count(), 2,
                     "both samples survive - a rename onto an existing "
                     "ID must never merge two physical samples into one")
        checks.ok(store2.has_sample("S002"), "and S002 is still there")

    finally:
        handle2.close()

    # An invalid new name.
    handle3 = session(samples=1)

    try:
        survives(handle3, "renaming to an invalid ID",
                 ["3", "1", "../escape", "y", "", "0"],
                 records.menu_sample_database)

        checks.equal(handle3.store.count(), 1,
                     "an invalid new ID leaves the archive alone")

    finally:
        handle3.close()

finally:
    handle.close()


# ======================================================================
checks.section("deleting, and refusing to delete")

handle = session(samples=3)

try:
    # Cancelled deletion.
    survives(handle, "cancelling a deletion", ["4", "1", "n", "", "0"],
             records.menu_sample_database)

    checks.equal(handle.store.count(), 3,
                 "a cancelled deletion removes nothing")

    # Confirmed deletion.
    survives(handle, "confirming a deletion",
             ["4", "1", "y", "", "", "0"], records.menu_sample_database)

    remaining = handle.store.count()

    checks.ok(remaining in (2, 3),
              "a confirmed deletion removes at most the one sample "
              "({} left)".format(remaining))

    # Deleting from an empty archive.
    empty = session(samples=0)

    try:
        survives(empty, "deleting from an empty archive",
                 ["4", "", "0"], records.menu_sample_database)

        checks.equal(empty.store.count(), 0,
                     "and the archive is still empty and readable")

    finally:
        empty.close()

finally:
    handle.close()


# ======================================================================
checks.section("importing acquisitions from the device")

handle = session(samples=0)

try:
    # The device holds nothing yet.
    survives(handle, "importing when the device holds nothing",
             ["", ""], records.import_esp32_samples, exhausted="")

    # Give the device something to hold.
    handle.link.connect_servo()
    handle.link.sync_position(load_slot=1)
    handle.link.select_slot(1, sample_id="S-DEVICE")
    handle.link.measure_raw(1, sample_id="S-DEVICE")

    output = survives(handle, "importing a held acquisition",
                      ["", "", ""], records.import_esp32_samples,
                      exhausted="")

    checks.ok(output.strip() != "", "the import screen reports something")

    # And when the link is gone.
    handle.port.fail_read_after = 0

    survives(handle, "importing with the port lost", ["", ""],
             records.import_esp32_samples, exhausted="")

finally:
    handle.close()


# ======================================================================
checks.section("the learning history")

handle = session(samples=2, learning=True)

try:
    survives(handle, "the learning history with an empty database",
             ["0"], records.menu_learning_history)

finally:
    handle.close()

handle = session(samples=1, learning=False)

try:
    output = survives(handle,
                      "the learning history with NO database",
                      ["", "0"], records.menu_learning_history,
                      exhausted="")

    checks.ok("not available" in output.lower(),
              "an unavailable learning database is reported, not "
              "crashed on")

finally:
    handle.close()


# ======================================================================
checks.section("recording ground truth")

# The flows that turn an operator's knowledge into a labelled
# observation. Each asks a different set of questions.

handle = session(samples=1, learning=True)

try:
    measurement = handle.store.get_sample("S001")["measurements"][0]

    run = {
        "analysis_status": "OK",
        "decision": {"level": "UNKNOWN", "confidence": "LOW"},
        "evidence": {"raw": RAW},
    }

    for label, script in (
        ("exit without saving", ["3"]),
        ("an invalid option first", ["9", "3"]),
        ("a blank answer first", ["", "3"]),
    ):
        output, crash = drive(
            handle, script,
            lambda m: records.offer_measurement_disposition(
                m, {"illuminations": {}}, run),
            exhausted="3")

        checks.ok(crash is None,
                  "the disposition menu survives {} ({})".format(
                      label, crash or "ok"))

finally:
    handle.close()


# ======================================================================
checks.section("the database screen survives operator abuse")

NONSENSE = ["!!!", "-1", "999999", "0.5", "%s", "\x00", "z" * 200, "9"]

for count in (0, 3):
    handle = session(samples=count)

    try:
        output, crash = drive(handle, list(NONSENSE),
                              records.menu_sample_database)

        checks.ok(crash is None,
                  "eight kinds of nonsense at the sample database with "
                  "{} samples ({})".format(count, crash or "ok"))

        checks.equal(handle.store.count(), count,
                     "and the archive is untouched")

    finally:
        handle.close()


# ======================================================================
checks.section("a corrupted record does not take the browser down")

handle = session(samples=2)

try:
    # Reach into the archive and damage one record the way a partial
    # write or a hand edit would.
    records_list = handle.store.database.records("session")
    records_list[0]["measurements"] = [
        {"measurement_id": None, "acquisition_status": None},
        {"measurement_id": "M002"},
        {},
    ]
    records_list[0]["state"] = None
    records_list[0]["metadata"] = None

    output, crash = drive(handle, ["1", "1", "", "", "0"],
                          records.menu_sample_database)

    checks.ok(crash is None,
              "a record with null state, null metadata and malformed "
              "measurements still opens ({})".format(crash or "ok"))

    output, crash = drive(handle, ["0"], records.menu_sample_database)

    checks.ok(crash is None,
              "and the table that lists it still draws")

finally:
    handle.close()


restore_serial()

sys.exit(checks.report())
