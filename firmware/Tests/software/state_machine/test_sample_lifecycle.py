"""
A Sample from prepared to saved, broken at every step in turn.

THE LIFECYCLE

    EMPTY -> READY_TO_LOAD -> LOADED -> MEASURED

    EMPTY          nothing is assigned to this slot
    READY_TO_LOAD  a Sample ID exists; the soil is not in yet
    LOADED         the arm has physically deposited soil
    MEASURED       at least one acquisition succeeded

MEASURED IS NOT EMPTY. The soil physically stays in the slot until an
operator empties it and says so, which is why the two are separate
states and why a measured slot is not silently reusable.

WHAT IS BEING PROVEN

For every step, when the step FAILS:

    the previous data is still valid and still readable
    nothing half-finished is committed
    the operator can retry from where they are
    the state on screen is the state on disk

And two claims about the record itself, which are the reason the whole
archive exists:

    a Sample may hold several Measurements, and producing the second
    never touches the first
    a Measurement may hold several AnalysisRuns, and producing the
    second never touches the first
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

from BD import samples as samples_module                     # noqa: E402
from BD.samples import (                                     # noqa: E402
    ACQUISITION_FAILED,
    ACQUISITION_SUCCESS,
    STATE_EMPTY,
    STATE_LOADED,
    STATE_MEASURED,
    STATE_READY_TO_LOAD,
    StorageError,
)

from fakes import SandboxBD                                  # noqa: E402

checks = support.Checks("sample-lifecycle")

RAW = {
    "white": {"A": 100.0, "B": 200.0},
    "uv": {"A": 40.0, "B": 60.0},
    "ir": {"A": 70.0, "B": 90.0},
}


def attempt(call):
    try:
        return "OK", call()

    except StorageError as error:
        return error.code, None

    except Exception as error:                         # noqa: BLE001
        return "CRASH:" + type(error).__name__, None


def refusing_writes(sandbox):
    """Make every save fail, and hand back the restore."""
    original = samples_module.os.replace

    def refuse(*args, **kwargs):
        raise OSError(28, "No space left on device")

    samples_module.os.replace = refuse

    def restore():
        samples_module.os.replace = original

    return restore


# ======================================================================
checks.section("the happy path, one state at a time")

with SandboxBD() as bd:
    store = bd.sample_database().archive()

    record = store.create("S001", 1)
    checks.equal(record["state"], STATE_READY_TO_LOAD,
                 "a created Sample starts READY_TO_LOAD, not EMPTY - "
                 "EMPTY is a property of a SLOT, not of a Sample")

    store.set_state("S001", STATE_LOADED, timestamp_key="loaded_at")
    checks.equal(store.get_state("S001"), STATE_LOADED,
                 "confirming the soil moves it to LOADED")

    measurement = store.add_measurement(
        "S001", raw=RAW, acquisition={"firmware_version": "6.0.0"})

    checks.equal(measurement["acquisition_status"], ACQUISITION_SUCCESS,
                 "an acquisition with RAW is a SUCCESS")

    store.add_analysis_run("S001", measurement["measurement_id"],
                           {"verdict": "KNOWN_MATERIAL"})
    store.set_state("S001", STATE_MEASURED, timestamp_key="measured_at")

    final = store.get_sample("S001")

    checks.equal(final["state"], STATE_MEASURED, "and the Sample is MEASURED")
    checks.ok(final["timestamps"]["loaded_at"],
              "with the moment the soil went in recorded")
    checks.ok(final["timestamps"]["measured_at"],
              "and the moment it was measured")
    checks.equal(len(final["measurements"]), 1, "and one Measurement")
    checks.equal(len(final["measurements"][0]["analysis_runs"]), 1,
                 "carrying one AnalysisRun")


# ======================================================================
checks.section("an unknown state is refused")

with SandboxBD() as bd:
    store = bd.sample_database().archive()
    store.create("S001", 1)

    for value in ("Measured", "MEASURING", "", None, 4, "DONE"):
        code, _ = attempt(lambda v=value: store.set_state("S001", v))

        checks.ok(code != "OK",
                  "set_state({!r}) is refused ({})".format(value, code))
        checks.ok(not code.startswith("CRASH"),
                  "and refused by name")

    checks.equal(store.get_state("S001"), STATE_READY_TO_LOAD,
                 "and none of them changed the state")


# ======================================================================
checks.section("a failed acquisition is a record, not a gap")

with SandboxBD() as bd:
    store = bd.sample_database().archive()
    store.create("S001", 1)
    store.set_state("S001", STATE_LOADED)

    failed = store.add_measurement(
        "S001",
        acquisition_status=ACQUISITION_FAILED,
        error={"code": "SENSOR_UNAVAILABLE", "message": "no answer"},
    )

    checks.equal(failed["acquisition_status"], ACQUISITION_FAILED,
                 "a failed acquisition is stored")
    checks.ok("raw" not in failed,
              "with no raw key at all - not null, not zeros")
    checks.equal(store.get_state("S001"), STATE_LOADED,
                 "and the Sample stays LOADED, because the soil is "
                 "still in the slot and can be measured again")

    # A success cannot be stored without RAW, either.
    code, _ = attempt(lambda: store.add_measurement(
        "S001", raw=None, acquisition_status=ACQUISITION_SUCCESS))

    checks.equal(code, "MISSING_RAW",
                 "and a SUCCESS with no RAW is refused - a successful "
                 "measurement of nothing is the one record that must "
                 "not exist")

    # Then a real one, beside it.
    good = store.add_measurement("S001", raw=RAW)
    record = store.get_sample("S001")

    checks.equal(len(record["measurements"]), 2,
                 "the successful retry sits BESIDE the failure")
    checks.equal(record["measurements"][0]["acquisition_status"],
                 ACQUISITION_FAILED,
                 "and the failure is still there - it is an operational "
                 "fact about the mission")


# ======================================================================
checks.section("failing at each step leaves everything before it intact")

STEPS = (
    "create", "confirm loaded", "add measurement", "add analysis run",
    "set conclusion", "mark measured",
)

for failing_step in STEPS:
    with SandboxBD() as bd:
        store = bd.sample_database().archive()
        restore = None
        reached = []
        measurement_id = None

        try:
            for step in STEPS:
                if step == failing_step:
                    restore = refusing_writes(bd)

                if step == "create":
                    code, _ = attempt(lambda: store.create("S001", 1))

                elif step == "confirm loaded":
                    code, _ = attempt(
                        lambda: store.set_state("S001", STATE_LOADED))

                elif step == "add measurement":
                    code, result = attempt(
                        lambda: store.add_measurement("S001", raw=RAW))

                    if result:
                        measurement_id = result["measurement_id"]

                elif step == "add analysis run":
                    code, _ = attempt(
                        lambda: store.add_analysis_run(
                            "S001", measurement_id, {"verdict": "X"}))

                elif step == "set conclusion":
                    code, _ = attempt(
                        lambda: store.set_conclusion("S001", {"note": "n"}))

                else:
                    code, _ = attempt(
                        lambda: store.set_state("S001", STATE_MEASURED))

                reached.append((step, code))

                if step == failing_step:
                    break

        finally:
            if restore:
                restore()

        outcome = dict(reached)

        checks.ok(outcome[failing_step] != "OK",
                  "with the disk full, '{}' fails".format(failing_step))
        checks.ok(not str(outcome[failing_step]).startswith("CRASH"),
                  "and fails as a StorageError, which the screens catch")

        # What survived is what had already been written.
        on_disk = bd.sample_database().archive()
        record = on_disk.get_sample("S001")

        if failing_step == "create":
            checks.ok(record is None,
                      "and nothing at all was created")

        else:
            checks.ok(record is not None,
                      "everything written before '{}' is still on "
                      "disk".format(failing_step))

            if record:
                measurements = record.get("measurements") or []

                if failing_step in ("confirm loaded", "add measurement"):
                    checks.equal(measurements, [],
                                 "and no Measurement was committed")

                else:
                    checks.equal(len(measurements), 1,
                                 "with the Measurement that had already "
                                 "succeeded")
                    checks.equal(sorted(measurements[0]["raw"]),
                                 ["ir", "uv", "white"],
                                 "and its RAW intact")

        # The store in memory must not be showing more than that.
        live = store.get_sample("S001")
        checks.equal(
            len((live or {}).get("measurements") or []),
            len((record or {}).get("measurements") or []),
            "and the store in memory shows exactly what the disk has "
            "({})".format(failing_step))


# ======================================================================
checks.section("recovery: the operator retries and it works")

with SandboxBD() as bd:
    store = bd.sample_database().archive()
    store.create("S001", 1)
    store.set_state("S001", STATE_LOADED)

    restore = refusing_writes(bd)

    try:
        code, _ = attempt(lambda: store.add_measurement("S001", raw=RAW))

    finally:
        restore()

    checks.ok(code != "OK", "the first save fails")

    code, measurement = attempt(
        lambda: store.add_measurement("S001", raw=RAW))

    checks.equal(code, "OK", "and the retry succeeds")

    record = bd.sample_database().archive().get_sample("S001")

    checks.equal(len(record["measurements"]), 1,
                 "with EXACTLY ONE Measurement on disk - the failed "
                 "attempt did not leave a ghost for the retry to be "
                 "counted beside")


# ======================================================================
checks.section("several Measurements, and none of them touch each other")

with SandboxBD() as bd:
    store = bd.sample_database().archive()
    store.create("S001", 1)
    store.set_state("S001", STATE_LOADED)

    ids = []

    for index in range(1, 6):
        raw = {name: {channel: value + index
                      for channel, value in block.items()}
               for name, block in RAW.items()}

        measurement = store.add_measurement("S001", raw=raw)
        ids.append(measurement["measurement_id"])

    record = bd.sample_database().archive().get_sample("S001")
    measurements = record["measurements"]

    checks.equal(len(measurements), 5,
                 "five acquisitions of one physical sample are five "
                 "Measurements")
    checks.equal(len(set(ids)), 5, "each with its own identifier")
    checks.equal(sorted(ids), ids,
                 "issued in order, so the sequence is readable")

    first = measurements[0]["raw"]["white"]["A"]
    checks.close(first, RAW["white"]["A"] + 1,
                 "and the FIRST one still holds what it held before the "
                 "other four existed")

    # Several AnalysisRuns on one Measurement.
    for version in range(1, 4):
        store.add_analysis_run("S001", ids[0],
                               {"verdict": "V{}".format(version)})

    runs = bd.sample_database().archive().get_sample("S001")["measurements"][0][
        "analysis_runs"]

    checks.equal(len(runs), 3,
                 "three analyses of one Measurement are three "
                 "AnalysisRuns")
    # The result is stored FLAT, exactly as Science produced it, with
    # only the two identifiers and a timestamp added. BD does not wrap
    # it in a "result" key and does not summarise it - a summary
    # written by the storage layer is a scientific judgement made in
    # the wrong place.
    checks.equal(sorted(set(runs[0]) - {"verdict"}),
                 ["analysis_run_id", "created_at", "measurement_id"],
                 "an AnalysisRun is the result itself plus its two "
                 "identifiers and when it was made")
    checks.equal(runs[0]["verdict"], "V1",
                 "and producing the third never touched the first - "
                 "the whole point of storing it was to see what was "
                 "concluded at the time")

    checks.equal(bd.sample_database().archive().get_sample("S001")["measurements"][0]
                 ["raw"]["white"]["A"], first,
                 "and none of the analyses reached the RAW")


# ======================================================================
checks.section("renaming and deleting")

with SandboxBD() as bd:
    store = bd.sample_database().archive()
    store.create("S001", 1)
    store.create("S002", 2)

    code, _ = attempt(lambda: store.rename("S001", "S002"))
    checks.ok(code != "OK",
              "renaming onto an existing ID is refused ({})".format(code))
    checks.ok(store.has_sample("S001") and store.has_sample("S002"),
              "and both Samples are still there")

    code, _ = attempt(lambda: store.rename("S001", "../escape"))
    checks.ok(code != "OK",
              "renaming to an invalid ID is refused ({})".format(code))

    code, _ = attempt(lambda: store.rename("S001", "S010"))
    checks.equal(code, "OK", "a legitimate rename works")
    checks.ok(bd.sample_database().archive().has_sample("S010"),
              "and survives a reload")

    code, _ = attempt(lambda: store.delete("NOT-THERE"))
    checks.ok(code != "OK",
              "deleting a Sample that does not exist is refused "
              "({})".format(code))

    checks.equal(bd.sample_database().archive().count(), 2,
                 "and the archive still holds both real Samples")


sys.exit(checks.report())
