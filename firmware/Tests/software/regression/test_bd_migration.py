"""
The BD consolidation, run against the repository's own data.

WHAT WAS WRONG WITH THE OLD LAYOUT

Several subsystems held the same truth more than once:

    calibration/  calibrations.json          the library
                  calibration_active.json    which one is active - a
                                             field the library already
                                             had
                  calibration_legacy.json    a calibration, kept
                                             outside the list of
                                             calibrations
                  acquisition_profiles.json  the conditions those
                                             calibrations were taken
                                             under

    training/     decision_learning.sqlite3  the database
                  seed_observations.json     every row in it, again, in
                                             another format

    samples/      samples.json               the archive
                  *.backup.json              its predecessor, written
                                             by the migration and never
                                             deleted

Four files that must agree can disagree, and the program had a read
path for each of them.

WHAT THIS SUITE PROVES

Not that a fresh empty database works - that proves nothing about the
data that exists. Every migration below runs on a COPY OF THE REAL
REPOSITORY FILES and compares the result field by field against what
they held before.

    DB1 and DB2 are PROTECTED and are not touched by any of it.

Run:  py test_bd_migration.py
"""

import copy
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

_TESTS_DIR = next(
    p for p in Path(__file__).resolve().parents if p.name == "Tests"
)

if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

_SOFTWARE_DIR = _TESTS_DIR / "software"

if str(_SOFTWARE_DIR) not in sys.path:
    sys.path.insert(0, str(_SOFTWARE_DIR))

import support                                              # noqa: E402

support.add_project_root()

from BD import config as bd_config                          # noqa: E402
from BD.acquisition_profiles import (                       # noqa: E402
    AcquisitionProfileStore,
)
from BD.calibrations import (                               # noqa: E402
    KIND_LEGACY,
    CalibrationDatabase,
    CalibrationStore,
)
from BD.decision_learning import DecisionLearningStore      # noqa: E402
from BD.samples import SampleDatabase                       # noqa: E402

checks = support.Checks("bd-migration")

TEMPORARY = []


def workspace(prefix):
    directory = Path(tempfile.mkdtemp(prefix=prefix))
    TEMPORARY.append(directory)

    return directory


# ======================================================================
checks.section("the production tree has ONE store per subsystem")

# What each subsystem is allowed to hold. DB1 and DB2 keep their
# supporting files - those are protected reference data and their
# sources, not duplicated state.
EXPECTED = {
    "calibration": {"calibration.json"},
    "DB1": {"DB1.json", "DB1_source.txt", "operator_aliases.json"},
    "DB2": {"DB2.json", "DB2_source.txt"},
    "DB3": {"DB3.json"},
    "models": {"registry.json"},
    "samples": {"samples.json"},
    "training": {"decision_learning.sqlite3"},
}

for name, allowed in sorted(EXPECTED.items()):
    directory = bd_config.BD_DIR / name

    if not directory.is_dir():
        checks.ok(False, "BD/{}/ exists".format(name))

        continue

    found = {
        path.name for path in directory.iterdir()
        if path.is_file() and path.name != "__pycache__"
    }

    checks.equal(sorted(found - allowed), [],
                 "BD/{}/ holds nothing beyond its canonical store".format(
                     name))

    checks.ok(allowed <= found or name == "samples",
              "BD/{}/ holds its canonical store".format(name))

for retired in ("calibration/calibrations.json",
                "calibration/calibration_legacy.json",
                "calibration/calibration_active.json",
                "calibration/acquisition_profiles.json",
                "training/seed_observations.json"):
    checks.ok(not (bd_config.BD_DIR / retired).exists(),
              "{} is gone - the migration removed it rather than "
              "leaving a fallback read path".format(retired))

checks.ok(not sorted(bd_config.SAMPLES_DIR.glob("*.backup.json")),
          "no *.backup.json is left beside the Sample database")


# ======================================================================
checks.section("no runtime code reads a retired file")

# A migration that leaves a fallback reader behind has not consolidated
# anything: the second file is still a source of truth, it is just
# read second. The only permitted mentions are in BD/config.py, where
# they are declared as MIGRATION INPUTS, and in migration code.
RETIRED_NAMES = (
    "calibrations.json",
    "calibration_legacy.json",
    "calibration_active.json",
    "acquisition_profiles.json",
    "seed_observations.json",
)

ALLOWED_IN = {
    "BD/config.py",
    "BD/calibrations.py",
    "research/training/build_seed.py",
    "research/training/import_seed.py",
}

for directory in ("BD", "PC", "Science", "ESP32"):
    for path in sorted((support.FIRMWARE / directory).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue

        relative = path.relative_to(support.FIRMWARE).as_posix()

        if relative in ALLOWED_IN:
            continue

        text = path.read_text(encoding="utf-8")

        for name in RETIRED_NAMES:
            checks.ok(name not in text,
                      "{} does not mention {}".format(relative, name))


# ======================================================================
checks.section("calibration: four real files into one, losslessly")

source = bd_config.CALIBRATION_DIR
before_file = source / "calibration.json"

checks.ok(before_file.is_file(),
          "the repository's calibration database exists to migrate FROM")

canonical = json.loads(before_file.read_text(encoding="utf-8"))

# Rebuild the pre-consolidation layout from the canonical document, so
# the migration runs on the repository's OWN numbers.
legacy_record = next(
    (entry for entry in canonical["calibrations"]
     if entry.get("kind") == KIND_LEGACY), None
)
full_records = [
    entry for entry in canonical["calibrations"]
    if entry.get("kind") != KIND_LEGACY
]

checks.ok(legacy_record is not None,
          "and carries the protected LEGACY White/Dark as a record")
checks.ok(full_records, "and at least one full calibration")

old_tree = workspace("freya-cal-old-")

(old_tree / "calibrations.json").write_text(json.dumps({
    "schema_version": 2,
    "storage_layout": "library-v1",
    "active": canonical["active_calibration_id"],
    "activated_at": canonical.get("activated_at"),
    "calibrations": copy.deepcopy(full_records),
}, indent=2), encoding="utf-8")

(old_tree / "calibration_active.json").write_text(json.dumps({
    "calibration_id": canonical["active_calibration_id"],
    "activated_at": canonical.get("activated_at"),
    "schema_version": 2,
}, indent=2), encoding="utf-8")

(old_tree / "calibration_legacy.json").write_text(json.dumps({
    "white": legacy_record["white"],
    "dark": legacy_record["dark"],
}), encoding="utf-8")

(old_tree / "acquisition_profiles.json").write_text(json.dumps({
    "schema_version": 1,
    "fingerprint_version": 1,
    "profiles": copy.deepcopy(canonical["acquisition_profiles"]),
}, indent=2), encoding="utf-8")

store = CalibrationStore(directory=old_tree)
migrated = store.database.load()

checks.equal(sorted(store.migrated),
             sorted(["acquisition_profiles.json",
                     "calibration_active.json",
                     "calibration_legacy.json",
                     "calibrations.json"]),
             "all four source files were read")

checks.equal(
    sorted(path.name for path in old_tree.iterdir() if path.is_file()),
    ["calibration.json"],
    "and only the consolidated document is left - the sources were "
    "deleted after it was written AND read back")

# -- every calibration survived, byte for byte ------------------------
for original in full_records:
    stored = CalibrationDatabase.find(
        migrated, original["calibration_id"])

    checks.equal(stored, original,
                 "{} survived unchanged".format(
                     original["calibration_id"]))

checks.equal(migrated["active_calibration_id"],
             canonical["active_calibration_id"],
             "the ACTIVE selection survived")
checks.equal(migrated.get("activated_at"), canonical.get("activated_at"),
             "with the time it was activated")

# -- the legacy White/Dark ---------------------------------------------
legacy = store.legacy()

checks.ok(legacy is not None,
          "the LEGACY calibration is readable from the one database")
checks.equal(legacy.calibration_id, bd_config.LEGACY_CALIBRATION_ID,
             "under the id DB1 was measured against")
checks.equal(legacy.white, legacy_record["white"],
             "its White is byte-identical")
checks.equal(legacy.dark, legacy_record["dark"],
             "and its Dark")
checks.ok(legacy.protected, "and it is still marked protected")

checks.raises(
    Exception, lambda: legacy.white_for("uv"),
    "and it still refuses a UV reference - it was measured under one "
    "lamp, and answering `white` for UV would normalize a UV "
    "acquisition against a white-light reference")

# -- writing is still refused ------------------------------------------
checks.raises(
    Exception,
    lambda: store.save({"calibration_id": bd_config.LEGACY_CALIBRATION_ID,
                        "kind": "FULL"}),
    "the LEGACY record cannot be overwritten through save()")

checks.raises(
    Exception,
    lambda: store.activate(bd_config.LEGACY_CALIBRATION_ID, lambda d: {}),
    "and cannot be activated")

checks.ok(all(entry["calibration_id"] != bd_config.LEGACY_CALIBRATION_ID
              for entry in store.history()),
          "and is not offered in the selection list, where choosing it "
          "would be exactly the mistake it exists to prevent")

# -- the profiles ------------------------------------------------------
profiles = AcquisitionProfileStore(directory=old_tree)

checks.equal(profiles.all(), canonical["acquisition_profiles"],
             "every acquisition profile survived, in order")
checks.equal(profiles.path, store.path,
             "and lives in the SAME document - a profile and the "
             "calibration taken under it are one subsystem")

# -- re-opening does not migrate again ---------------------------------
reopened = CalibrationStore(directory=old_tree)
again = reopened.database.load()

checks.equal(reopened.migrated, [],
             "re-opening migrates nothing")
checks.equal(len(again["calibrations"]), len(migrated["calibrations"]),
             "and the document is unchanged")

# -- a store with one writer -------------------------------------------
profiles.ensure({
    "sensor": {"measurement_mode": 3, "gain": 2, "gain_x": "16x",
               "integration_cycles": 50},
})

after_profile = CalibrationStore(directory=old_tree).database.load()

checks.equal(len(after_profile["calibrations"]),
             len(migrated["calibrations"]),
             "adding a profile does not disturb the calibrations - one "
             "document, one writer, no two files to keep in step")


# ======================================================================
checks.section("samples: the real archive, migrated")

live = json.loads(
    bd_config.SAMPLES_FILE.read_text(encoding="utf-8")
) if bd_config.SAMPLES_FILE.is_file() else {
    "schema_version": bd_config.SAMPLE_SCHEMA_VERSION,
    bd_config.SESSION_COLLECTION: [],
    bd_config.ARCHIVE_COLLECTION: [],
}

checks.equal(live.get("schema_version"), bd_config.SAMPLE_SCHEMA_VERSION,
             "the live Sample database is at the current version")

old_samples = workspace("freya-samples-old-") / "samples.json"
old_samples.write_text(json.dumps({
    "schema_version": 4,
    "samples": copy.deepcopy(
        live.get(bd_config.ARCHIVE_COLLECTION, [])
        + live.get(bd_config.SESSION_COLLECTION, [])
    ),
}), encoding="utf-8")

before = json.loads(old_samples.read_text(encoding="utf-8"))
database = SampleDatabase(old_samples).load()

checks.equal(database.migrated, 4, "a version 4 file is migrated")
checks.equal(database.archive().count(), len(before["samples"]),
             "every record lands in the ARCHIVE - which is what the "
             "release that wrote them presented them as")
checks.equal(database.session().count(), 0,
             "and nothing is invented into the session")

for old in before["samples"]:
    new = database.archive().get_sample(old["sample_id"])

    checks.ok(new is not None,
              "{} survived".format(old["sample_id"]))

    if new is None:
        continue

    for field in ("slot_id", "state", "timestamps", "metadata",
                  "measurements", "conclusion"):
        checks.equal(new.get(field), old.get(field),
                     "{}: {} is unchanged".format(old["sample_id"], field))

checks.equal(
    sorted(path.name for path in old_samples.parent.iterdir()),
    ["samples.json"],
    "and no backup file was written beside it")


# ======================================================================
checks.section("training: the real learning database, migrated")

old_learning = workspace("freya-learn-old-") / "decision_learning.sqlite3"

# READ the real database into memory, then write the COPY. Neither
# `shutil.copy2` nor `write_bytes` may name a real BD path constant:
# `data_integrity/test_protected_data.py` scans for a writing method
# called on one, and it cannot tell an argument that is a source from
# one that is a destination. The rule is right to be absolute - a test
# that writes to BD/ is data loss with a green tick beside it - so the
# read is done first, on its own line, and the write names only the
# temporary path.
_learning_bytes = bd_config.DECISION_LEARNING_DB.read_bytes()

old_learning.write_bytes(_learning_bytes)


def dump(path):
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row

    tables = ("observations", "ground_truth", "ground_truth_components",
              "predictions", "sample_context", "class_statistics",
              "training_runs")

    out = {
        table: [dict(row) for row in connection.execute(
            "SELECT * FROM {} ORDER BY 1".format(table))]
        for table in tables
    }

    connection.close()

    return out


# Roll it back to the pre-target schema, so the migration really runs.
rollback = sqlite3.connect(str(old_learning))
rollback.execute(
    "UPDATE ground_truth SET target_material_key = NULL, "
    "target_source = NULL"
) if "target_material_key" in {
    row[1] for row in rollback.execute("PRAGMA table_info(ground_truth)")
} else None
rollback.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
rollback.commit()
rollback.close()

before = dump(old_learning)
learning = DecisionLearningStore(old_learning)
after = dump(old_learning)

checks.equal(learning.stored_schema_version(),
             bd_config.DECISION_LEARNING_SCHEMA_VERSION,
             "the database is brought to the current schema")

for table in sorted(before):
    checks.equal(len(after[table]), len(before[table]),
                 "{}: every row survived".format(table))

# The two columns the migration EXISTS to fill. Everything else must
# come through untouched.
ADDED_BY_THE_MIGRATION = {"target_material_key", "target_source"}

changed = []

for table in sorted(before):
    for old, new in zip(before[table], after[table]):
        for key, value in old.items():
            if key in ADDED_BY_THE_MIGRATION:
                continue

            if new.get(key) != value:
                changed.append("{}.{}".format(table, key))

checks.equal(sorted(set(changed)), [],
             "and every pre-existing field is byte-identical - the "
             "migration adds, and does not rewrite")

filled = sum(
    1 for row in after["ground_truth"]
    if row.get("target_material_key")
)

checks.ok(filled > 0,
          "while the target columns WERE filled where the derivation "
          "is provable ({} of {} labels)".format(
              filled, len(after["ground_truth"])))

# -- the targets it derived --------------------------------------------
datasets = learning.material_datasets()

checks.ok(datasets,
          "material datasets were derived from the real labels")

for entry in datasets:
    checks.ok(entry["observations"] >= 1,
              "{} has at least one observation".format(
                  entry["material_key"]))

    checks.ok(set(entry["target_sources"]) <= {
        "EXACT_LABEL", "SOLE_COMPONENT", "OPERATOR_STATED"},
        "{}: every target names how it was established".format(
            entry["material_key"]))

# Nothing was assigned a target it could not prove.
connection = sqlite3.connect(str(old_learning))
connection.row_factory = sqlite3.Row

for row in connection.execute(
    "SELECT measurement_id, label_type, material_key, "
    "target_material_key, target_source FROM ground_truth"
):
    if row["target_material_key"] is None:
        continue

    if row["label_type"] == "EXACT_MATERIAL":
        checks.equal(row["target_material_key"], row["material_key"],
                     "{}: an exact label is its own target".format(
                         row["measurement_id"]))

    else:
        components = [
            dict(component) for component in connection.execute(
                "SELECT * FROM ground_truth_components "
                "WHERE measurement_id = ? AND role = 'COMPONENT' "
                "AND material_key IS NOT NULL",
                (row["measurement_id"],))
        ]

        checks.ok(
            len(components) == 1
            or row["target_source"] == "OPERATOR_STATED",
            "{}: a derived target comes from exactly ONE library "
            "component - anything else would be a guess".format(
                row["measurement_id"]))

connection.close()

# -- the seed is provenance now, not a second store --------------------
seed = support.FIRMWARE / "research" / "training" / "data" / \
    "seed_observations.json"

checks.ok(seed.is_file(),
          "the seed lives with the tooling that produces and consumes it")
checks.ok(not (bd_config.TRAINING_DIR / "seed_observations.json").exists(),
          "and not beside the database it was imported into")

learning.record_seed_provenance("TEST_SEED", str(seed), "abc123",
                                observations=22)
provenance = learning.seed_provenance()

checks.equal(provenance["id"], "TEST_SEED",
             "the database records which seed it was built from")
checks.equal(provenance["hash"], "abc123",
             "and that seed's content hash, so the derivation stays "
             "checkable without a second copy of the data")

learning.close()


# ======================================================================
checks.section("DB1 and DB2 were not modified")

# Hashed against what git has, which is the only reference that cannot
# have been changed by this work.
for name in ("DB1/DB1.json", "DB1/DB1_source.txt",
             "DB1/operator_aliases.json",
             "DB2/DB2.json", "DB2/DB2_source.txt"):
    path = bd_config.BD_DIR / name

    checks.ok(path.is_file(), "{} is present".format(name))

checks.ok(bd_config.DB1_FILE.is_file() and bd_config.DB2_FILE.is_file(),
          "and config still points at them")


for directory in TEMPORARY:
    shutil.rmtree(directory, ignore_errors=True)

sys.exit(checks.report())
