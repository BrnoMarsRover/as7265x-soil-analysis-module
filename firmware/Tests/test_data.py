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

checks.ok(db1.ready and db2.ready and db3.ready,
          "all three databases load independently")
checks.equal(db2.evidence, "MEASURED",
             "DB2 was measured on this instrument, like DB1")
checks.equal(db2.feature_space, "AS7265X_54_MULTIILLUM",
             "but in the 54-feature space, which DB1 is not")
checks.ok(db1.feature_space != db2.feature_space,
          "so a DB1 spectrum can never be scored against a DB2 record "
          "by lining up the first 18 numbers of each")
checks.equal(len(db2.materials), 22,
             "DB2 holds the 22 materials measured under all three lamps")

# DB2 IS A SUBSET OF DB1 AND MUST STAY ONE.
#
# Every material in DB2 was measured from the same container as the DB1
# material of the same name. A key in DB2 that DB1 does not have would
# mean the two libraries had drifted apart on names, and every
# cross-database statement about "the same material" would be wrong.
checks.equal(sorted(set(db2.materials) - set(db1.materials)), [],
             "and every one of them is a DB1 material under the same key")
checks.equal(sorted(set(db1.materials) - set(db2.materials)),
             ["Sodium Bicarbonate"],
             "the one DB1 material never measured under UV and IR is "
             "ABSENT from DB2 rather than present and zero")

checks.equal(db2.database.incomplete_materials(), {},
             "no DB2 record has a hole in its 54 features")

for name, spectrum in sorted(db2.materials.items())[:3]:
    checks.equal(len(spectrum), 54,
                 "{}: 54 features, not 54 wavelengths".format(name))

checks.equal(db2.database.calibration_id,
             "FREYA_FULL_SPECTRAL_CAL_20260817_173914",
             "DB2 names the full calibration it was measured under, not "
             "the legacy one that never touched it")
checks.equal(db1.database.calibration_id, "FREYA_COMPETITION_2026_CAL_V1",
             "and DB1 still names the legacy calibration")

# Both languages, on both databases. The bench labels are Czech and the
# library is English; a material that can only be named in one of them
# cannot be labelled by the person holding the container.
for key, handle in (("DB1", db1), ("DB2", db2)):
    named = [
        name for name in handle.materials
        if (handle.metadata.get(name) or {}).get("name_cs")
    ]

    checks.equal(len(named), len(handle.materials),
                 "{}: every material carries a Czech name beside its "
                 "English one".format(key))


# ======================================================================
checks.section("an acquisition profile records the lamp current")
#
# REGRESSION. from_measurement asked for settings["white_current_ma"]
# and the firmware has never sent a key of that name - it sends
# led_currents_ma as a mapping of labels. All three currents were
# therefore null in every profile ever built from a real acquisition,
# and all three are FINGERPRINT fields: a measurement at 25 mA and one
# at 100 mA produced the same fingerprint and compared COMPATIBLE.

from BD.acquisition_profiles import (          # noqa: E402
    _milliamps,
    compare,
    fingerprint,
    from_measurement,
    unknown_fingerprint_fields,
)

# Exactly what the ESP32 reports, copied from a real sensor_test_raw.
FIRMWARE_SETTINGS = {
    "measurement_mode_name": "one-shot",
    "measurement_mode": 3,
    "config_register": "0x2C",
    "led_currents": {"uv": 1, "white": 1, "ir": 1},
    "led_currents_ma": {"ir": "25mA", "uv": "25mA", "white": "25mA"},
    "led_current_ma": "25mA",
    "led_current": 1,
    "gain_x": "16x",
    "gain": 2,
    "data_ready": False,
    "integration_cycles": 100,
}

profile = from_measurement(FIRMWARE_SETTINGS, firmware_version="6.0.0")

checks.equal(profile["sensor"]["measurement_mode"], 3, "the mode is recorded")
checks.equal(profile["sensor"]["gain"], 2, "the gain is recorded")
checks.equal(profile["sensor"]["integration_cycles"], 100,
             "the integration time is recorded")

checks.equal(profile["illumination"]["white_current_ma"], 25.0,
             "the WHITE lamp current is recorded, as a number")
checks.equal(profile["illumination"]["uv_current_ma"], 25.0,
             "and the UV lamp current")
checks.equal(profile["illumination"]["ir_current_ma"], 25.0,
             "and the IR lamp current")

unknown = unknown_fingerprint_fields(profile)

for field in ("illumination.white_current_ma", "illumination.uv_current_ma",
              "illumination.ir_current_ma"):
    checks.ok(field not in unknown,
              "{} is no longer an unknown fingerprint field".format(field))

# THE FINGERPRINT MUST SEPARATE TWO DIFFERENT LAMP CURRENTS. That is the
# whole point of recording them.
brighter = dict(FIRMWARE_SETTINGS)
brighter["led_currents_ma"] = {"ir": "100mA", "uv": "100mA",
                               "white": "100mA"}

bright_profile = from_measurement(brighter, firmware_version="6.0.0")

checks.equal(bright_profile["illumination"]["white_current_ma"], 100.0,
             "a 100 mA acquisition reads as 100 mA")
checks.ok(fingerprint(profile) != fingerprint(bright_profile),
          "and it fingerprints differently from the 25 mA one - which it "
          "did not while both were null")
checks.equal(compare(profile, bright_profile)["status"], "INCOMPATIBLE",
             "so the two are reported as different acquisition conditions")

# A number, a label, and nonsense.
checks.equal(_milliamps(25), 25.0, "a bare number is a current")
checks.equal(_milliamps("25mA"), 25.0, "and so is the label the firmware sends")
checks.equal(_milliamps("12.5mA"), 12.5, "including a fractional one")
checks.equal(_milliamps(None), None, "absent stays absent")
checks.equal(_milliamps("not a current"), None,
             "and anything unparseable stays absent rather than becoming 0")

# THE SAME CONDITION MUST HASH THE SAME.
#
# REGRESSION. The fingerprint is a hash of JSON, and 25 is not the same
# JSON as 25.0. A profile built from a seed file that wrote the lamp
# current as an integer got a different id from one built from the same
# current after _milliamps turned it into a float - which split the
# 2026-08-17 session across two acquisition profiles, the exact mistake
# the fingerprint exists to prevent.
integral = from_measurement(dict(FIRMWARE_SETTINGS))
floated = from_measurement(dict(FIRMWARE_SETTINGS))
floated["illumination"]["white_current_ma"] = 25.0
floated["illumination"]["uv_current_ma"] = 25.0
floated["illumination"]["ir_current_ma"] = 25.0
integral["illumination"]["white_current_ma"] = 25
integral["illumination"]["uv_current_ma"] = 25
integral["illumination"]["ir_current_ma"] = 25

checks.equal(fingerprint(integral), fingerprint(floated),
             "25 mA and 25.0 mA are one acquisition condition, not two")

fractional = from_measurement(dict(FIRMWARE_SETTINGS))
fractional["illumination"]["white_current_ma"] = 25.5

checks.ok(fingerprint(integral) != fingerprint(fractional),
          "while 25.5 mA is genuinely different and still says so")


# ======================================================================
checks.section("a prepared mixture records what was weighed")
#
# The record that lets the system learn to find one material inside
# another. Everything here is about refusing a mixture that would
# become a misleading training example - a fraction that is really a
# percent, a total with a hole in it, a component nobody can score.

from BD.decision_learning import (                     # noqa: E402
    DecisionLearningStore,
    LABEL_EXACT_MATERIAL,
    LABEL_PREPARED_MIXTURE,
    LearningError,
    ROLE_COMPONENT,
    ROLE_MATRIX,
    normalize_mixture,
)

SPIKE = [
    {"role": ROLE_COMPONENT, "material_key": "Iron(III) Oxide Red",
     "prepared_mass_fraction": 0.1},
    {"role": ROLE_MATRIX, "matrix_label": "garden soil",
     "prepared_mass_fraction": 0.9},
]

cleaned = normalize_mixture(SPIKE)
checks.equal(len(cleaned), 2, "a spike into soil is two ingredients")
checks.equal(cleaned[0]["role"], ROLE_COMPONENT,
             "the weighed material is a COMPONENT")
checks.equal(cleaned[1]["role"], ROLE_MATRIX,
             "and what it went into is the MATRIX, not a leftover")
checks.equal(cleaned[1]["matrix_label"], "garden soil",
             "which is named, because it is most of the signal")

checks.raises(
    LearningError,
    lambda: normalize_mixture([
        {"material_key": "Iron(III) Oxide Red",
         "prepared_mass_fraction": 10},
    ]),
    "a fraction of 10 is refused - percentages are the classic way to "
    "record a sample as 1000% hematite",
)

checks.raises(
    LearningError,
    lambda: normalize_mixture([
        {"material_key": "Iron(III) Oxide Red",
         "prepared_mass_fraction": 0.1},
        {"material_key": "Bentonite", "prepared_mass_fraction": 0.2},
    ]),
    "fractions that add to 0.3 are refused: the missing mass was in the "
    "cup and attributing it to what WAS listed makes both wrong",
)

checks.raises(
    LearningError,
    lambda: normalize_mixture([
        {"material_key": "Iron(III) Oxide Red",
         "prepared_mass_fraction": 0.5},
        {"material_key": "Bentonite"},
    ]),
    "half a mixture weighed is not a partially known mixture - a "
    "fraction is a share of a total",
)

checks.raises(
    LearningError,
    lambda: normalize_mixture([
        {"material_key": "Bentonite", "prepared_mass_fraction": 0.5},
        {"material_key": "Bentonite", "prepared_mass_fraction": 0.5},
    ]),
    "one material cannot have two proportions",
)

checks.raises(
    LearningError,
    lambda: normalize_mixture([
        {"role": ROLE_COMPONENT, "prepared_mass_fraction": 1.0},
    ]),
    "a component that names no material cannot be scored against "
    "anything and is refused",
)

checks.raises(
    LearningError,
    lambda: normalize_mixture([]),
    "an empty mixture is an unknown sample, and there is a label for that",
)

# Unweighed mixtures are legitimate: they say WHAT was in the sample
# without saying how much, which still supports detection.
unweighed = normalize_mixture([
    {"material_key": "Iron(III) Oxide Red"},
    {"role": ROLE_MATRIX, "matrix_label": "garden soil"},
])
checks.equal(len(unweighed), 2,
             "a mixture with no proportions at all is accepted - it "
             "scores detection, not quantity")


# -- the store round trip ---------------------------------------------
mixture_db = Path(tempfile.gettempdir()) / "freya_test_mixture.sqlite3"

if mixture_db.exists():
    mixture_db.unlink()

learning = DecisionLearningStore(mixture_db)
counts = {channel: 100.0 for channel in CHANNELS}

learning.add_observation("MIX_01", {"white": counts})
learning.add_ground_truth(
    "MIX_01", LABEL_PREPARED_MIXTURE, mixture=SPIKE,
    verification_status="VERIFIED",
    verification_source="operator_known_reference_material",
)

components = learning.components("MIX_01")
checks.equal(len(components), 2, "both ingredients are queryable as rows")
checks.equal(components[0]["material_key"], "Iron(III) Oxide Red",
             "in the order they were entered")
checks.close(components[0]["prepared_mass_fraction"], 0.1,
             "with the fraction that was weighed")

found = learning.observations_containing("Iron(III) Oxide Red")
checks.equal([entry["measurement_id"] for entry in found], ["MIX_01"],
             "and the mixture is found by asking what contains hematite")

checks.equal(
    learning.observations_containing(
        "Iron(III) Oxide Red", min_fraction=0.5
    ),
    [],
    "a 10% spike does not answer a query for 50% or more",
)

# A pure jar counts as containing itself: it is the easy end of the
# same detection curve and a model needs both ends.
learning.add_observation("PURE_01", {"white": counts})
learning.add_ground_truth(
    "PURE_01", LABEL_EXACT_MATERIAL,
    material_key="Iron(III) Oxide Red",
    verification_status="VERIFIED",
    verification_source="operator_known_reference_material",
)

found = learning.observations_containing("Iron(III) Oxide Red")
checks.equal(sorted(entry["measurement_id"] for entry in found),
             ["MIX_01", "PURE_01"],
             "the pure jar counts as containing itself at fraction 1.0")

checks.raises(
    LearningError,
    lambda: learning.add_ground_truth(
        "PURE_01", LABEL_EXACT_MATERIAL,
        material_key="Iron(III) Oxide Red", mixture=SPIKE, replace=True,
    ),
    "composition on a non-mixture label is refused - it would make a "
    "pure material look like a mixture in every query that counts them",
)

summary = learning.mixture_summary()
checks.equal(summary["mixtures"], 1, "one prepared mixture on file")
checks.equal(summary["matrices"], {"garden soil": 1},
             "against one named matrix")


# ======================================================================
checks.section("how the sample was presented is recorded, or is absent")

learning.add_sample_context(
    "MIX_01", sensor_to_sample_mm=30.0, sample_mass_g=12.5,
    packing="TAMPED", moisture="AIR_DRY", substrate="garden soil",
)

context = learning.get_sample_context("MIX_01")
checks.close(context["sensor_to_sample_mm"], 30.0,
             "the sensor-to-sample distance is stored")
checks.equal(context["packing"], "TAMPED", "and how it was packed")
checks.equal(context["sample_depth_mm"], None,
             "a question that was not answered stays NULL - 'not "
             "recorded' and 'zero deep' are different facts")

checks.raises(
    LearningError,
    lambda: learning.add_sample_context("MIX_01", sensor_to_sample_mm=99.0),
    "a second context is refused without replace: the first was "
    "observed, a later one is remembered",
)

checks.raises(
    LearningError,
    lambda: learning.add_sample_context("PURE_01", packing="SQUASHED"),
    "packing is a controlled vocabulary, not free text",
)

checks.raises(
    LearningError,
    lambda: learning.add_sample_context("PURE_01",
                                        sensor_to_sample_mm=-5.0),
    "and a negative distance is refused rather than stored",
)

checks.raises(
    LearningError,
    lambda: learning.add_sample_context("NO_SUCH_MEASUREMENT",
                                        packing="LOOSE"),
    "context for an observation that does not exist is refused",
)

coverage = learning.context_coverage()
checks.equal(coverage["with_any_context"], 1,
             "coverage says how much of the history carries context")
checks.equal(coverage["by_field"]["sample_depth_mm"], 0,
             "field by field, so a null-heavy column is visible before "
             "a training run discovers it")

learning.close()
mixture_db.unlink()


sys.exit(checks.report())
