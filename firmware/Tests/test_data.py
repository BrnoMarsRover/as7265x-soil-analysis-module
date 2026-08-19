"""
Data: the record model, RAW immutability, and the databases' provenance.

BD is the authoritative store, and these are the properties that make
what it holds worth keeping:

    Sample -> N Measurements -> N AnalysisRuns, always, even when there
        happens to be one of each
    RAW is written once and never again
    a failed acquisition is a record of a failure, not a spectrum of
        zeros
    re-analysis accumulates instead of replacing
    every stored conclusion says which pipeline, model and library
        version produced it
    MEASURED and DERIVED_REFERENCE stay distinguishable

The archive tests run against a temporary file, never the real one -
the run's own data is irreplaceable and is not something a test suite
should be able to touch.

Run:  py test_data.py
"""

import json
import sys
import tempfile
from pathlib import Path

import support

support.add_project_root()

from BD import config as bd_config          # noqa: E402
from BD import samples as samples_module    # noqa: E402
from BD.channels import CHANNELS            # noqa: E402
from BD.registry import DatabaseRegistry    # noqa: E402
from BD.samples import (                    # noqa: E402
    ACQUISITION_FAILED,
    ACQUISITION_SUCCESS,
    STATE_LOADED,
    STATE_MEASURED,
    SampleStore,
    StorageError,
    analysis_runs_of,
    latest_measurement,
    measurements_of,
)

checks = support.Checks("data")


def spectrum(value=100.0):
    return {channel: float(value) for channel in CHANNELS}


def fresh_store():
    """A store on a throwaway file, so the real archive is never touched."""
    handle = tempfile.NamedTemporaryFile(
        suffix=".json", delete=False, mode="w", encoding="utf-8")
    handle.write(json.dumps({"schema_version":
                             bd_config.SAMPLE_SCHEMA_VERSION,
                             "samples": []}))
    handle.close()

    return SampleStore(Path(handle.name)).load()


# ======================================================================
checks.section("the layout says what each directory is for")

for name, path in (
    ("calibration", bd_config.CALIBRATION_DIR),
    ("DB1", bd_config.DB1_DIR),
    ("DB2", bd_config.DB2_DIR),
    ("DB3", bd_config.DB3_DIR),
    ("training", bd_config.TRAINING_DIR),
    ("models", bd_config.MODELS_DIR),
    ("samples", bd_config.SAMPLES_DIR),
):
    checks.ok(path.is_dir(), "BD/{}/ exists".format(name))

for name, path in (
    ("DB1", bd_config.DB1_FILE),
    ("DB2", bd_config.DB2_FILE),
    ("DB3", bd_config.DB3_FILE),
    ("the legacy calibration", bd_config.REFERENCES_FILE),
    ("the calibration library", bd_config.CALIBRATION_LIBRARY_FILE),
    ("the learning history", bd_config.DECISION_LEARNING_DB),
    ("the learning seed", bd_config.DECISION_LEARNING_SEED),
    ("the model registry", bd_config.MODEL_REGISTRY_FILE),
):
    checks.ok(path.exists(), "{} is where config says it is".format(name))


# ======================================================================
checks.section("one Sample, many Measurements")

store = fresh_store()

store.create("S001", 1)
checks.equal(store.count(), 1, "a Sample is created")

record = store.get_sample("S001")
checks.equal(record["measurements"], [],
             "and starts with a LIST of measurements, empty - not a "
             "singular field that would have to be rewritten later")

first = store.add_measurement(
    "S001", raw={"white": spectrum(100.0)},
    acquisition={"firmware_version": "6.0.0"},
    calibration_id="CAL_A")
checks.equal(first["measurement_id"], "M001", "the first is M001")

second = store.add_measurement("S001", raw={"white": spectrum(101.0)})
third = store.add_measurement("S001", raw={"white": spectrum(99.0)})

checks.equal(second["measurement_id"], "M002", "the second is M002")
checks.equal(third["measurement_id"], "M003", "the third is M003")

record = store.get_sample("S001")
checks.equal(len(measurements_of(record)), 3,
             "all three are kept - repeated acquisition of one sample is "
             "repeatability evidence, not an overwrite")

checks.close(record["measurements"][0]["raw"]["white"]["A"], 100.0,
             "M001 still holds its own RAW")
checks.close(record["measurements"][1]["raw"]["white"]["A"], 101.0,
             "M002 holds different RAW")
checks.close(record["measurements"][2]["raw"]["white"]["A"], 99.0,
             "and M003 its own again")

checks.equal(record["state"], STATE_MEASURED,
             "the Sample is MEASURED once a successful acquisition exists")


# ======================================================================
checks.section("one Measurement, many AnalysisRuns")

run_one = store.add_analysis_run("S001", "M001", {
    "analysis_status": "OK",
    "versions": {"science": "1.0", "decision_model": "V001"},
    "decision": {"level": "KNOWN_MATERIAL", "material": "basalt"},
})
checks.equal(run_one["analysis_run_id"], "A001", "the first run is A001")

run_two = store.add_analysis_run("S001", "M001", {
    "analysis_status": "OK",
    "versions": {"science": "2.0", "decision_model": "V002"},
    "decision": {"level": "MATERIAL_FAMILY", "family": "silicate"},
})
checks.equal(run_two["analysis_run_id"], "A002", "the second is A002")

record = store.get_sample("S001")
measurement = record["measurements"][0]
runs = analysis_runs_of(measurement)

checks.equal(len(runs), 2, "both runs are kept")
checks.equal(runs[0]["versions"]["science"], "1.0",
             "A001 still records the Science version that produced it")
checks.equal(runs[0]["decision"]["material"], "basalt",
             "and the conclusion it reached at the time")
checks.equal(runs[1]["versions"]["science"], "2.0",
             "A002 records a different one")
checks.ok(runs[0]["decision"] != runs[1]["decision"],
          "and they are allowed to disagree - that is what re-analysis "
          "is for, and why A001 was worth keeping")

# The second Measurement gets its own numbering, not a global counter.
other = store.add_analysis_run("S001", "M002", {"analysis_status": "OK"})
checks.equal(other["analysis_run_id"], "A001",
             "run ids are per-Measurement, so M002's first run is A001 too")


# ======================================================================
checks.section("RAW is immutable")

before = json.dumps(store.get_sample("S001")["measurements"][0]["raw"],
                    sort_keys=True)

store.add_analysis_run("S001", "M001", {
    "analysis_status": "OK",
    "representations": {"normalized": spectrum(0.5)},
})
store.set_conclusion("S001", {"interpretation": "basalt"})
store.update_metadata("S001", {"note": "changed my mind"})

after = json.dumps(store.get_sample("S001")["measurements"][0]["raw"],
                   sort_keys=True)

checks.equal(after, before,
             "RAW is untouched by a new AnalysisRun, a new conclusion and "
             "a metadata edit")

reloaded = SampleStore(store.path).load()
checks.equal(
    json.dumps(reloaded.get_sample("S001")["measurements"][0]["raw"],
               sort_keys=True),
    before,
    "and it survives a reload unchanged")

checks.ok("add_measurement" in dir(store) and "update_measurement"
          not in dir(store),
          "there is no API for modifying a Measurement at all - the only "
          "way to add data is to add a new record")


# ======================================================================
checks.section("a failed acquisition is a failure, not zeros")

failed = store.add_measurement(
    "S001",
    acquisition_status=ACQUISITION_FAILED,
    error={"code": "SENSOR_UNAVAILABLE", "message": "no answer at 0x49"},
)

checks.equal(failed["acquisition_status"], ACQUISITION_FAILED,
             "the acquisition status says FAILED")
checks.ok("raw" not in failed,
          "and there is NO raw key at all - not a null, and certainly not "
          "a spectrum of zeros, which cannot be told from a genuinely "
          "dark reading")
checks.equal(failed["error"]["code"], "SENSOR_UNAVAILABLE",
             "the reason is recorded")

record = store.get_sample("S001")
checks.equal(len(measurements_of(record)), 4,
             "it is stored - a failed acquisition is an operational fact")
checks.equal(len(samples_module.successful_measurements(record)), 3,
             "but it does not count as a successful measurement")
checks.equal(latest_measurement(record)["measurement_id"], "M003",
             "and the latest SUCCESSFUL measurement is still M003")

def store_success_without_raw():
    store.add_measurement("S001", acquisition_status=ACQUISITION_SUCCESS)

checks.raises(StorageError, store_success_without_raw,
              "a SUCCESSFUL measurement with no RAW is refused outright")


# ======================================================================
checks.section("identity and metadata")

checks.raises(StorageError, lambda: store.create("S001", 2),
              "a duplicate Sample ID is refused - an ID is a permanent "
              "handle for physical material")

for bad in ("", "   ", None, "has space", "slash/es", "x" * 40):
    checks.raises(StorageError, lambda bad=bad: store.create(bad, 1),
                  "Sample ID {!r} is refused".format(bad))

store.create("S002", 2, metadata={"task": "traverse", "note": "wet"})
record = store.get_sample("S002")

checks.equal(record["metadata"]["task"], "traverse", "metadata is stored")
checks.ok(record["metadata"]["hypothesis"] is None,
          "and an unrecorded field stays null rather than becoming an "
          "empty string that looks like an answer")
checks.equal(sorted(record["metadata"]), sorted(samples_module.METADATA_KEYS),
             "every metadata key is present")

checks.ok("operator" not in record["metadata"],
          "no operator field is required")
checks.ok("distance_mm" not in record["metadata"],
          "and no manual distance - it is not a controlled instrument "
          "variable")


# ======================================================================
checks.section("the summary is derived, never stored")

summary = samples_module.summary_of(store.get_sample("S001"))

checks.equal(summary["measurement_count"], 4, "counts every measurement")
checks.equal(summary["successful_measurement_count"], 3, "and the successes")
checks.equal(summary["failed_measurement_count"], 1, "and the failures")
checks.equal(summary["analysis_run_count"], 4,
             "and every AnalysisRun across all of them")
checks.equal(summary["latest_measurement_id"], "M003",
             "naming the latest successful measurement")

stored = store.get_sample("S001")
checks.ok("measurement_count" not in stored,
          "none of which is written into the record - a stored count is a "
          "second copy of a fact that can go stale")


# ======================================================================
checks.section("the migration was lossless")

archive = json.loads(bd_config.SAMPLES_FILE.read_text(encoding="utf-8"))

checks.equal(archive["schema_version"], bd_config.SAMPLE_SCHEMA_VERSION,
             "the live archive is at the current schema version")

backups = sorted(bd_config.SAMPLES_DIR.glob("*.backup.json"))
checks.ok(backups,
          "and the pre-migration archive was copied aside first - it is "
          "the only copy of the original and is not in version control")

if backups:
    old = json.loads(backups[-1].read_text(encoding="utf-8"))

    for old_record in old.get("samples", []):
        sample_id = old_record["sample_id"]
        new_record = next(
            (r for r in archive["samples"] if r["sample_id"] == sample_id),
            None,
        )

        checks.ok(new_record is not None,
                  "{} survived the migration".format(sample_id))

        if new_record is None:
            continue

        legacy = old_record.get("measurement") or {}

        if legacy.get("raw"):
            migrated = new_record["measurements"][0]["raw"]["white"]

            checks.equal(migrated, legacy["raw"],
                         "{}: RAW is byte-identical after migration"
                         .format(sample_id))
            checks.equal(
                new_record["measurements"][0]["acquisition"]
                ["illuminations"],
                ["white"],
                "{}: an 18-channel legacy record is one illumination, and "
                "stays valid legacy data".format(sample_id))

            run = new_record["measurements"][0]["analysis_runs"][0]

            checks.equal(
                run["representations"]["normalized"],
                legacy.get("normalized"),
                "{}: the CALCULATED representations moved into the "
                "AnalysisRun, where they belong - they were never RAW"
                .format(sample_id))

        checks.equal(new_record["metadata"], old_record["metadata"],
                     "{}: metadata is unchanged".format(sample_id))


# ======================================================================
checks.section("the three databases stay independent")

registry = DatabaseRegistry()

checks.equal(sorted(registry.databases), ["DB1", "DB2", "DB3"],
             "three databases, separately addressable")

for key, handle in sorted(registry.databases.items()):
    checks.ok(handle.version is not None,
              "{} declares a version, so a conclusion can name the "
              "library that produced it".format(key))
    checks.ok(handle.evidence is not None,
              "{} declares what KIND of evidence it holds".format(key))

db1 = registry.get("DB1")
db3 = registry.get("DB3")

checks.equal(db1.evidence, "MEASURED",
             "DB1 was measured on this instrument")
checks.equal(db3.evidence, "DERIVED_REFERENCE",
             "DB3 was projected from external spectra, and says so - "
             "calling it MEASURED would claim this instrument saw "
             "something it never saw")
checks.ok(db1.evidence != db3.evidence,
          "and the two are never conflated")

checks.equal(db1.feature_space, "AS7265X_18",
             "DB1 is the legacy 18-channel space")
checks.equal(len(db1.materials), 23, "with 23 materials")
checks.equal(len(db3.materials), 84, "and DB3 with 84")

db2 = registry.get("DB2")
checks.ok(not db2.ready,
          "DB2 is empty and reports itself unready rather than pretending")
checks.ok(db2.status, "with a status naming why")
checks.ok(db1.ready and db3.ready,
          "and an empty DB2 does not take the other two down with it")


sys.exit(checks.report())
