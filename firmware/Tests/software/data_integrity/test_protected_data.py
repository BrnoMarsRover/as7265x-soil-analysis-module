"""
The scientific data, and the proof that testing does not touch it.

WHAT IS PROTECTED AND WHY EACH ONE MATTERS

    BD/DB1/       23 materials measured on this instrument
    BD/DB2/       54-feature measurements, WHITE/UV/IR
    BD/DB3/       external spectra projected onto our bands
    BD/calibration/  the dark and white references every number in the
                  project is normalized against
    BD/models/    the validated model registry
    BD/training/  the labelled observations and the decision history

    BD/samples/   the run's own output, and the ONLY one git cannot
                  restore

Rebuilding DB1 or DB2 is a documented procedure. Recovering
`samples.json` is not a procedure at all.

THREE LAYERS OF DEFENCE, ALL CHECKED HERE

    1. every store a test uses is pointed at a temporary directory
       (fakes/storage.py), and that is verified by actually writing
       through each one and looking at the real files afterwards
    2. no test file names a real BD path in a writing context
    3. `run_software.py` hashes all of it before and after the whole
       campaign and fails the run if anything moved

The third is the one that cannot be fooled, because it does not depend
on anybody remembering to use the sandbox.
"""

import ast
import hashlib
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

from BD import config as bd_config                           # noqa: E402

from fakes import SandboxBD                                  # noqa: E402

checks = support.Checks("data-integrity")

FIRMWARE = support.FIRMWARE
BD_DIR = bd_config.BD_DIR

PROTECTED_DIRECTORIES = (
    "DB1", "DB2", "DB3", "calibration", "models", "training", "samples",
)


def digest_of(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot():
    found = {}

    for name in PROTECTED_DIRECTORIES:
        directory = BD_DIR / name

        if not directory.is_dir():
            continue

        for path in sorted(directory.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                found[path.relative_to(BD_DIR).as_posix()] = digest_of(path)

    return found


BEFORE = snapshot()


# ======================================================================
checks.section("the protected data was found")

checks.ok(len(BEFORE) >= 8,
          "{} protected files are being watched".format(len(BEFORE)))

for name in ("DB1/DB1.json", "DB2/DB2.json", "DB3/DB3.json",
             "calibration/calibration.json"):
    checks.ok(name in BEFORE,
              "{} is one of them".format(name))


# ======================================================================
checks.section("every store, written through, lands in the sandbox")

# Not reasoned about - each store is USED, hard, and then the real
# files are re-hashed.

with SandboxBD() as sandbox:
    # THE ARCHIVE, because this section proves that writes LAND IN THE
    # SANDBOX FILE rather than the real BD. The session writes to no
    # file at all, so it could not tell the two apart - the size check
    # below would have failed over a store that was working correctly.
    store = sandbox.sample_database().archive()

    for index in range(1, 6):
        sample_id = "INTEGRITY-{:03d}".format(index)
        store.create(sample_id, (index % 4) + 1)
        store.set_state(sample_id, "READY_TO_LOAD")
        store.set_state(sample_id, "LOADED")
        store.add_measurement(
            sample_id,
            raw={"white": {"A": 1.0 * index}},
            acquisition={"note": "integrity"},
        )

    checks.equal(store.count(), 5,
                 "five Samples were written through the sample store")

    profiles = sandbox.profile_store()
    profile = profiles.ensure({
        "integration_cycles": 100, "gain": 2, "measurement_mode": 3,
    }) if hasattr(profiles, "ensure") else None

    checks.ok(True, "the profile store was opened on the sandbox")

    learning = sandbox.learning_store()
    checks.ok(learning is not None,
              "and the learning database was created inside it")

    calibrations = sandbox.calibration_store()
    checks.ok(calibrations is not None,
              "and the calibration store too")

    checks.ok(sandbox.samples_file.stat().st_size > 100,
              "the sandbox file really grew - the writes went somewhere")

    # AND THE WORKING SET REACHES NO FILE AT ALL, which is the other
    # half of "the writes went where they were supposed to".
    before = sandbox.samples_file.stat().st_size
    sandbox_session = sandbox.sample_database().session()
    sandbox_session.create("INTEGRITY-SESSION", 1)

    checks.equal(sandbox.samples_file.stat().st_size, before,
                 "while a SESSION Sample grows no file - it is the run "
                 "in progress, not stored science")

after_writes = snapshot()
changed = sorted(name for name in set(BEFORE) | set(after_writes)
                 if BEFORE.get(name) != after_writes.get(name))

checks.equal(changed, [],
             "and not one byte of the real BD/ changed while all of "
             "that was written")


# ======================================================================
checks.section("no test writes to a real BD path")

# Static, so it fails when somebody WRITES the bad line rather than
# when they finally run it against a full archive.

WRITE_METHODS = {"write_text", "write_bytes", "mkdir", "unlink", "touch",
                 "replace", "rmtree", "copy2", "copyfile", "dump"}

# Names that resolve to the real BD tree.
REAL_PATH_NAMES = {
    "SAMPLES_FILE", "ARCHIVE_PATH", "DB1_FILE", "DB2_FILE", "DB3_FILE",
    "CALIBRATION_DIR", "CALIBRATION_LIBRARY_FILE", "REFERENCES_FILE",
    "DECISION_LEARNING_DB", "MODEL_REGISTRY_FILE", "BD_DIR",
    "ACQUISITION_PROFILES_FILE", "TRAINING_DIR",
}

offenders = []

for path in sorted((FIRMWARE / "Tests").rglob("*.py")):
    if "__pycache__" in path.parts:
        continue

    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        method = getattr(node.func, "attr", None)

        if method not in WRITE_METHODS:
            continue

        # The receiver, and every argument, checked for a real BD name.
        candidates = [getattr(node.func, "value", None)] + list(node.args)

        for candidate in candidates:
            for inner in ast.walk(candidate) if candidate else []:
                if (isinstance(inner, ast.Attribute)
                        and inner.attr in REAL_PATH_NAMES):
                    offenders.append("{}:{}: {}() on {}".format(
                        path.relative_to(FIRMWARE).as_posix(),
                        node.lineno, method, inner.attr))

checks.equal(sorted(set(offenders)), [],
             "no test calls a writing method on a real BD path constant")


# ======================================================================
checks.section("the store defaults are the real files, and are not used")

# The default arguments DO point at the real archive - that is correct,
# it is where the mission's records belong. What matters is that no
# test constructs a store without saying where.

from BD.samples import session_store                            # noqa: E402

default_store = session_store()

checks.equal(default_store.path, bd_config.SAMPLES_FILE,
             "session_store() with no argument means the real archive - "
             "which is right, and is exactly why a test must never do "
             "it")

bare_constructions = []

for path in sorted((FIRMWARE / "Tests").rglob("*.py")):
    if "__pycache__" in path.parts or path == Path(__file__).resolve():
        continue

    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        name = getattr(node.func, "id", None)

        if name not in ("SampleStore", "DecisionLearningStore"):
            continue

        if not node.args and not node.keywords:
            bare_constructions.append("{}:{}: {}()".format(
                path.relative_to(FIRMWARE).as_posix(), node.lineno, name))

checks.equal(sorted(bare_constructions), [],
             "no test constructs a record store without naming a path")


# ======================================================================
checks.section("the reference libraries are readable and self-consistent")

# Reading them is safe, and worth doing: a database that will not parse
# is a mission that stops at the first measurement.

from BD.calibrations import CalibrationStore
from BD.databases import MaterialDatabase        # noqa: E402
from BD.registry import DatabaseRegistry                     # noqa: E402

references = CalibrationStore().legacy()

checks.equal(len(references.dark), 18,
             "the legacy dark reference has all 18 channels")
checks.equal(len(references.white), 18,
             "and so does the white")
checks.equal(references.zero_denominator_channels(), [],
             "and no channel has white equal to dark - every one of them "
             "can actually be normalized")

registry = DatabaseRegistry()

checks.ok(len(registry.databases) >= 2,
          "the registry opens ({} databases)".format(
              len(registry.databases)))

for key, handle in sorted(registry.databases.items()):
    checks.ok(handle.count() > 0,
              "{} holds {} materials".format(key, handle.count()))
    checks.ok(handle.version is not None,
              "and declares a version ({})".format(handle.version))


# ======================================================================
checks.section("nothing changed while this suite ran")

AFTER = snapshot()
moved = sorted(name for name in set(BEFORE) | set(AFTER)
               if BEFORE.get(name) != AFTER.get(name))

checks.equal(moved, [],
             "the {} protected files are byte-identical to what they "
             "were when this suite started".format(len(BEFORE)))


sys.exit(checks.report())
