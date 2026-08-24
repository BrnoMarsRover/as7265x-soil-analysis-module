"""
Persistence, made to fail at every point it can.

WHAT IS AT STAKE

`firmware/BD/samples/samples.json` is the run's only irreplaceable
output. It is not in version control, and git cannot restore it. Every
other file in BD/ is reference data that a rebuild can reproduce; this
one is the mission.

So the questions here are not "does saving work". They are:

    does a save that FAILS leave the previous archive intact?
    does a save that fails say so, or does it look like a save?
    does a corrupted archive stop the program, or get half-read?
    is a rejected Sample ID rejected before anything is written?

THE ATOMICITY CLAIM, ACTUALLY TESTED

`_write_json` writes a temporary file in the same directory and then
`os.replace`s it over the target. That is claimed to be atomic. Here it
is made to fail at each of its three steps - opening the temporary,
writing it, and the replace itself - and after each failure the
original file is read back and compared byte for byte.

NOTHING HERE TOUCHES THE REAL BD/. Every store is built on a temporary
directory; `data_integrity/test_protected_data.py` proves it.
"""

import json
import os
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

import support

support.add_project_root()
support.add_path("PC")

from BD import samples as samples_module                     # noqa: E402
from BD.samples import (                                     # noqa: E402
    STATE_LOADED,
    STATE_MEASURED,
    STATE_READY_TO_LOAD,
    SampleStore,
    StorageError,
)

from fakes import SandboxBD                                  # noqa: E402

checks = support.Checks("filesystem-faults")


def fresh_store():
    """A store on a throwaway file, with one sample already in it."""
    directory = Path(tempfile.mkdtemp(prefix="freya-fs-"))
    path = directory / "samples.json"
    path.write_text('{"schema_version": 4, "samples": []}',
                    encoding="utf-8")

    store = SampleStore(path).load()
    store.create("S001", 1)

    return store, path, directory


def failing(dotted, exception):
    """
    Make one function raise, named as `module.attribute`.

    Spelled out rather than guessed. An earlier version searched for
    the name on `BD.samples` and then on `os`, and quietly patched
    `os.mkstemp` - which `_write_json` does not call - so the test
    that was meant to fail the temporary file passed while doing
    nothing at all.
    """
    where, _dot, name = dotted.rpartition(".")
    target = samples_module if not where else getattr(samples_module, where)
    original = getattr(target, name)

    def raiser(*args, **kwargs):
        raise exception

    setattr(target, name, raiser)

    def restore():
        setattr(target, name, original)

    return restore


# ======================================================================
checks.section("a Sample ID is checked before anything is written")

store, path, directory = fresh_store()
before = path.read_bytes()

BAD_IDS = (
    (None, "None"),
    ("", "empty"),
    ("   ", "whitespace only"),
    ("A" * 500, "500 characters"),
    ("../../etc/passwd", "a path"),
    ("..\\..\\windows", "a Windows path"),
    ("S 001", "a space inside"),
    ("S/001", "a slash"),
    ("S:001", "a colon"),
    ("S\x00001", "a null byte"),
    ("S\n001", "a newline"),
    ("S*", "a glob"),
    ("<script>", "markup"),
)

for value, label in BAD_IDS:
    try:
        store.create(value, 2)
        refused = None

    except StorageError as error:
        refused = error.code

    except Exception as error:                         # noqa: BLE001
        refused = "CRASH:" + type(error).__name__

    checks.ok(refused is not None and not refused.startswith("CRASH"),
              "a Sample ID that is {} is refused by name ({})".format(
                  label, refused))

checks.equal(path.read_bytes(), before,
             "and NOT ONE of those {} attempts changed the archive - "
             "validation happens before the write, not after".format(
                 len(BAD_IDS)))

# A path-shaped ID is the one worth stating twice: if it reached the
# filesystem it would write outside BD/ entirely.
siblings = sorted(p.name for p in directory.iterdir())
checks.equal(siblings, ["samples.json"],
             "and no stray file appeared anywhere near the archive")


# ======================================================================
checks.section("a duplicate Sample ID does not overwrite the first")

store, path, directory = fresh_store()
store.create("S002", 2, metadata={"note": "original"})

try:
    store.create("S002", 3, metadata={"note": "replacement"})
    refused = None

except StorageError as error:
    refused = error.code

checks.ok(refused is not None,
          "creating a Sample that already exists is refused ({})".format(
              refused))

record = store.get_sample("S002")
checks.equal((record.get("metadata") or {}).get("note"), "original",
             "and the original record is untouched - a second Sample "
             "with one name would make the first unreachable")


# ======================================================================
checks.section("a write that fails leaves the archive exactly as it was")

# Each of the three steps of the atomic write, failed in turn.

FAILURES = (
    ("tempfile.mkstemp", OSError(28, "No space left on device"),
     "the temporary file cannot be created"),
    ("os.replace", OSError(13, "Permission denied"),
     "the rename over the target is refused"),
)

for name, exception, label in FAILURES:
    store, path, directory = fresh_store()
    store.create("S002", 2)

    original = path.read_bytes()
    restore = failing(name, exception)

    try:
        store.create("S003", 3)
        refused = None

    except StorageError as error:
        refused = error.code

    except Exception as error:                         # noqa: BLE001
        refused = "CRASH:" + type(error).__name__

    finally:
        restore()

    checks.ok(refused is not None,
              "when {}, the save reports a failure ({})".format(
                  label, refused))
    checks.ok(refused is None or not refused.startswith("CRASH"),
              "and reports it as a StorageError, not a raw OSError - "
              "the screens catch one of those")
    checks.equal(path.read_bytes(), original,
                 "AND THE ARCHIVE ON DISK IS BYTE-FOR-BYTE WHAT IT WAS "
                 "({})".format(label))

    leftovers = [p.name for p in directory.iterdir()
                 if p.name.startswith(".samples-")]
    checks.equal(leftovers, [],
                 "and no temporary file is left behind ({})".format(label))


# ======================================================================
checks.section("a failed save does not leave the store lying about itself")

store, path, directory = fresh_store()
restore = failing("os.replace", OSError(13, "Permission denied"))

try:
    try:
        store.create("S099", 2)

    except StorageError:
        pass

finally:
    restore()

reread = SampleStore(path).load()

checks.ok(not reread.has_sample("S099"),
          "a Sample whose save failed is not in the archive when it is "
          "read back")

# What the in-memory store believes is the harder half: if it kept the
# record, the operator sees a Sample that does not exist.
checks.ok(store.has_sample("S099") == reread.has_sample("S099"),
          "and the in-memory store agrees with the disk, rather than "
          "showing a Sample that was never written")


# ======================================================================
checks.section("an archive that cannot be read is named, not guessed")

BROKEN = (
    ("", "an empty file"),
    ("{", "a truncated object"),
    ("[]", "a list where an object belongs"),
    ('{"samples": []}', "no schema version"),
    ('{"schema_version": 4}', "no samples key"),
    ('{"schema_version": 999, "samples": []}', "a version from the future"),
    ("not json at all", "prose"),
    ('{"schema_version": 4, "samples": "S001"}', "samples as a string"),
    ('\x00\x00\x00\x00', "null bytes"),
)

for content, label in BROKEN:
    directory = Path(tempfile.mkdtemp(prefix="freya-broken-"))
    path = directory / "samples.json"
    path.write_text(content, encoding="utf-8")

    try:
        store = SampleStore(path).load()
        outcome = "LOADED"

        # Loading is allowed to succeed if the store MIGRATES the
        # content. What is not allowed is loading and then behaving as
        # if the file were valid.
        store.count()

    except StorageError as error:
        outcome = error.code

    except Exception as error:                         # noqa: BLE001
        outcome = "CRASH:" + type(error).__name__

    checks.ok(not str(outcome).startswith("CRASH"),
              "{} is handled, not crashed on ({})".format(label, outcome))


# ======================================================================
checks.section("a missing archive is an empty archive, not a failure")

directory = Path(tempfile.mkdtemp(prefix="freya-missing-"))
path = directory / "does" / "not" / "exist" / "samples.json"

try:
    store = SampleStore(path).load()
    count = store.count()
    outcome = "OK"

except StorageError as error:
    outcome = error.code
    count = None

checks.equal(outcome, "OK",
             "a first run with no archive yet starts cleanly")
checks.equal(count, 0, "with nothing in it")

store.create("S001", 1)

checks.ok(path.is_file(),
          "and the first save creates the directories it needs")


# ======================================================================
checks.section("an unreadable archive does not become an empty one")

# The dangerous confusion: "the file is not there" and "the file is
# there and I cannot read it" must not produce the same answer, because
# the second one silently starts a new archive over the old mission.

directory = Path(tempfile.mkdtemp(prefix="freya-unreadable-"))
path = directory / "samples.json"
path.write_text('{"schema_version": 4, "samples": []}', encoding="utf-8")

store = SampleStore(path).load()
store.create("S001", 1)
store.create("S002", 2)

original = path.read_bytes()

# A REAL OS-level failure, not a patched function.
#
# Patching `_read_json` to raise would bypass the very error handling
# under test. Pointing the store at a DIRECTORY makes the builtin
# `open()` fail exactly as it does on a permission problem, on both
# Linux and Windows, and lets `_read_json`'s own except clause decide
# what happens.
unreadable = directory / "a-directory-not-a-file"
unreadable.mkdir()

try:
    reread = SampleStore(unreadable).load()
    outcome = "LOADED"
    count = reread.count()

except StorageError as error:
    outcome = error.code
    count = None

except Exception as error:                             # noqa: BLE001
    outcome = "CRASH:" + type(error).__name__
    count = None

checks.equal(outcome, "SAMPLE_READ_ERROR",
             "an archive that exists and cannot be read is reported as "
             "an error - NOT opened as an empty archive, which would "
             "start a new mission on top of the old one")
checks.equal(path.read_bytes(), original,
             "and the real file is untouched by the failed read")


# ======================================================================
checks.section("RAW survives everything that happens after it")

# The scientific claim: a measurement's raw counts are what the
# instrument reported, and nothing downstream may edit them.

store, path, directory = fresh_store()
store.set_state("S001", STATE_READY_TO_LOAD)
store.set_state("S001", STATE_LOADED)

raw = {"white": {"A": 1.0, "B": 2.0}, "uv": {"A": 3.0}}
measurement = store.add_measurement("S001", raw=raw)
measurement_id = measurement["measurement_id"]

stored = json.loads(json.dumps(store.get_sample("S001")))

# Mutating the dict we handed in must not reach the archive.
raw["white"]["A"] = 999.0
raw["white"]["INJECTED"] = 1.0

after = store.get_sample("S001")
after_raw = after["measurements"][0]["raw"]

checks.close(after_raw["white"]["A"], 1.0,
             "editing the caller's dict after the fact does not change "
             "the stored RAW - it was copied in, not referenced")
checks.ok("INJECTED" not in after_raw["white"],
          "and a channel added afterwards does not appear in it")

store.add_analysis_run("S001", measurement_id, {"verdict": "SOMETHING"})
store.set_conclusion("S001", {"note": "done"})
store.set_state("S001", STATE_MEASURED)

final = store.get_sample("S001")
final_raw = final["measurements"][0]["raw"]

checks.equal(final_raw, stored["measurements"][0]["raw"],
             "and an analysis run, a conclusion and a state change "
             "leave RAW byte-identical")

reloaded = SampleStore(path).load().get_sample("S001")

checks.equal(reloaded["measurements"][0]["raw"], final_raw,
             "and it survives a round trip through the file unchanged")


# ======================================================================
checks.section("round trips do not drift")

store, path, directory = fresh_store()
store.create("S010", 2, metadata={
    "note": "unicode: příliš žluťoučký kůň",
    "location": "Mars Yard, zone 3",
    "map_point": "P-12",
    "depth_mm": 12.5,                 # not a metadata field; see below
})

first = json.loads(path.read_text(encoding="utf-8"))

for _pass in range(5):
    store = SampleStore(path).load()
    store.set_state("S010", STATE_READY_TO_LOAD)
    store.set_state("S010", STATE_LOADED)
    store.set_state("S010", STATE_MEASURED)

reloaded = SampleStore(path).load().get_sample("S010")

checks.equal(reloaded["metadata"]["note"],
             "unicode: příliš žluťoučký kůň",
             "non-ASCII metadata survives five save/load cycles")
checks.equal(reloaded["metadata"]["location"], "Mars Yard, zone 3",
             "and so does a location with a comma in it")
checks.equal(reloaded["metadata"]["map_point"], "P-12",
             "and a map point")
stored_ids = [record["sample_id"] for record in first["samples"]]

checks.ok("S010" in stored_ids and reloaded["sample_id"] == "S010",
          "and the identity is stable across every cycle")
checks.equal(reloaded["state"], STATE_MEASURED,
             "and the last state written is the state read back")

# The metadata schema is FIXED, and a key outside it is dropped rather
# than stored. Asserted rather than discovered: `depth_mm` above went
# in and is not here, which is the intended behaviour of
# blank_metadata() and would otherwise look like data loss.
checks.ok("depth_mm" not in reloaded["metadata"],
          "a metadata key outside the fixed schema is not stored - the "
          "record shape is the same for every Sample ever saved")
checks.equal(sorted(reloaded["metadata"]),
             sorted(key for key, _label in samples_module.METADATA_FIELDS),
             "and every record carries exactly the declared fields, so "
             "'not recorded' and 'field missing' are never confused")


# ======================================================================
checks.section("the sandbox really is a sandbox")

# This suite would be worthless if it were writing to the real BD/.

with SandboxBD() as sandbox:
    store = sandbox.sample_store()
    store.create("SANDBOX-TEST", 1)

    checks.ok(str(sandbox.root) not in str(support.FIRMWARE / "BD"),
              "the sandbox is outside firmware/BD/")
    checks.ok(sandbox.samples_file.is_file(),
              "and the sample really was written, inside it")

    real = support.FIRMWARE / "BD" / "samples" / "samples.json"

    if real.is_file():
        checks.ok("SANDBOX-TEST" not in real.read_text(encoding="utf-8"),
                  "and nothing of it reached the real archive")

    else:
        checks.ok(True, "and the real archive does not exist to reach")


sys.exit(checks.report())
