"""
The ESP32's own store for acquisitions nobody has imported yet.

WHY THIS SUITE EXISTS

The PC keeps the run in progress in memory, so between a measurement
and an explicit import the DEVICE holds the only copy of the spectrum.
That is only a defensible arrangement if the device's copy is actually
durable, so everything that makes it durable is asserted here rather
than assumed:

    it survives a reboot          the record is on the filesystem
    it survives a bad record      one corrupt slot is discarded, the
                                  other three come back, and the board
                                  still boots
    it survives a power cut       written to a temporary file and
                                  renamed, so a reader sees the whole
                                  previous record or the whole new one
    it never costs a measurement  a store that cannot write says so and
                                  returns; it does not raise into the
                                  acquisition that has already happened

THE LAST ONE IS THE IMPORTANT ONE, and it is the one that was wrong.
`save` caught OSError and ValueError, so a MemoryError while
serializing escaped into `mark_occupied`, out of `handle_measure_raw`,
and turned a completed acquisition into a failed command - losing the
spectrum in order to protect the backup of it.

Everything here drives the REAL firmware modules, imported the way the
device imports them, against a temporary directory.

Run:  py test_device_retention.py
"""

import json
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

from fakes.esp32 import LoopbackDevice                      # noqa: E402

checks = support.Checks("device-retention")


# A record shaped like the one `handle_measure_raw` retains.
def acquisition(sample_id, slot_id):
    return {
        "sample_id": sample_id,
        "slot_id": slot_id,
        "esp_uptime_ms": 1234,
        "repeats": 1,
        "illuminations": {
            "white": {"acquisitions": [{"A": 1.0, "B": 2.0}]},
            "uv": {"acquisitions": [{"A": 3.0}]},
            "ir": {"acquisitions": [{"A": 4.0}]},
        },
    }


def fresh_store():
    """The retention module, pointed at an empty directory."""
    support.load_esp32(support.FakeAS7265X())
    support.purge_esp32_modules()

    import config
    import retention

    config.RETAINED_DIR = tempfile.mkdtemp(prefix="freya-retain-test-")

    return retention, config


# ======================================================================
checks.section("a record survives being written and read back")

retention, config = fresh_store()

checks.equal(retention.save(1, acquisition("R-1", 1)), None,
             "saving a slot's acquisition reports no error")

restored = retention.load_all()

checks.equal(sorted(restored), [1],
             "and loading finds exactly that slot")

checks.equal(restored[1]["sample_id"], "R-1",
             "with the Sample ID it was taken under")

checks.equal(sorted(restored[1]["illuminations"]), ["ir", "uv", "white"],
             "and all three illuminations, which is the part that "
             "cannot be reacquired")

checks.equal(retention.status()["persisted_slots"], [1],
             "and status reports the slot without opening the record")

checks.ok(retention.status()["available"],
          "and says the store is available")


# ======================================================================
checks.section("four slots, independent of each other")

retention, config = fresh_store()

for slot_id in (1, 2, 3, 4):
    retention.save(slot_id, acquisition("R-{}".format(slot_id), slot_id))

checks.equal(sorted(retention.load_all()), [1, 2, 3, 4],
             "all four slots hold their own acquisition")

retention.drop(2)

checks.equal(sorted(retention.load_all()), [1, 3, 4],
             "dropping one removes exactly one")

checks.equal(retention.load_all()[3]["sample_id"], "R-3",
             "and does not disturb its neighbours")

retention.drop(2)

checks.equal(sorted(retention.load_all()), [1, 3, 4],
             "dropping it again is success, not an error - the caller "
             "has already removed the RAM copy")

retention.clear()

checks.equal(retention.load_all(), {},
             "and clear removes every one of them")


# ======================================================================
checks.section("a damaged record is discarded, not guessed at")

# ONE BAD SLOT MUST NOT COST THE OTHER THREE, and must not come back on
# the next boot to be misread again.

CORRUPTIONS = (
    ("truncated JSON", "{\"store_version\": 1, \"slot_id\": 2, \"measu"),
    ("valid JSON, wrong shape", "[1, 2, 3]"),
    ("empty file", ""),
    ("the wrong slot", json.dumps({
        "store_version": 1, "slot_id": 9,
        "measurement": {"illuminations": {"white": {}}},
    })),
    ("a future store version", json.dumps({
        "store_version": 99, "slot_id": 2,
        "measurement": {"illuminations": {"white": {}}},
    })),
    ("no illuminations", json.dumps({
        "store_version": 1, "slot_id": 2,
        "measurement": {"sample_id": "R-2"},
    })),
)

for label, content in CORRUPTIONS:
    retention, config = fresh_store()

    retention.save(1, acquisition("R-1", 1))
    retention.save(3, acquisition("R-3", 3))

    damaged = Path(config.RETAINED_DIR) / "slot2.json"
    damaged.write_text(content, encoding="utf-8")

    found = retention.load_all()

    checks.equal(sorted(found), [1, 3],
                 "{}: the damaged slot is left out, and 1 and 3 come "
                 "back".format(label))

    checks.ok(not damaged.exists(),
              "  and the unusable file is removed, so it cannot be read "
              "again on the next boot")


# ======================================================================
checks.section("an interrupted write leaves the previous record whole")

retention, config = fresh_store()

retention.save(1, acquisition("R-FIRST", 1))

# A temporary file is what a write interrupted before its rename leaves
# behind. The real record is therefore still the previous one.
leftover = Path(config.RETAINED_DIR) / "slot1.tmp"
leftover.write_text("half a record", encoding="utf-8")

found = retention.load_all()

checks.equal(found[1]["sample_id"], "R-FIRST",
             "the previous complete record is what is read")

checks.ok(not leftover.exists(),
          "and the leftover temporary file is cleaned up rather than "
          "left to be mistaken for one")

retention.save(1, acquisition("R-SECOND", 1))

checks.equal(retention.load_all()[1]["sample_id"], "R-SECOND",
             "and the next successful write replaces it completely")

checks.equal(
    sorted(p.name for p in Path(config.RETAINED_DIR).iterdir()),
    ["slot1.json"],
    "leaving exactly one file behind - no temporary survives a "
    "successful write")


# ======================================================================
checks.section("a store that cannot write says so, and raises nothing")

# THE INVERSION THIS PREVENTS. By the time `save` is called the
# spectrum exists and is already on its way back to the PC. A
# persistence failure that propagated would destroy the science in
# order to protect the copy of it.

FAILURES = (
    ("a full disk", OSError(28, "No space left on device")),
    ("a read-only filesystem", OSError(30, "Read-only file system")),
    ("memory exhausted while serializing", MemoryError("out of memory")),
    ("something nobody predicted", RuntimeError("surprise")),
)

for label, exception in FAILURES:
    retention, config = fresh_store()

    original = retention.json.dump

    def refuse(*args, **kwargs):
        raise exception

    retention.json.dump = refuse

    try:
        outcome = retention.save(1, acquisition("R-1", 1))
        raised = None

    except BaseException as error:                     # noqa: BLE001
        outcome, raised = None, type(error).__name__

    finally:
        retention.json.dump = original

    checks.equal(raised, None,
                 "{}: save does not raise - the acquisition has already "
                 "happened".format(label))

    checks.ok(bool(outcome),
              "  and it reports what went wrong, so the response can "
              "say the acquisition is not protected")

    checks.equal(retention.load_all(), {},
                 "  while nothing half-written is left to be loaded")

    checks.equal(
        [p.name for p in Path(config.RETAINED_DIR).iterdir()], [],
        "  and no temporary file is left behind")


# ======================================================================
checks.section("the carousel restores what the store holds, and no more")

# A restored acquisition is a fact about a spectrum. It is NOT a claim
# about where the soil is or where the mechanism is pointing, and the
# firmware must not turn one into the other on the way up.

support.load_esp32(support.FakeAS7265X())
support.purge_esp32_modules()

import config as esp_config                                 # noqa: E402
import retention as esp_retention                           # noqa: E402

esp_config.RETAINED_DIR = tempfile.mkdtemp(prefix="freya-retain-test-")
support.shrink_timings(esp_config)

esp_retention.save(2, acquisition("R-RESTORED", 2))

import carousel as esp_carousel                             # noqa: E402

wheel = esp_carousel.Carousel()

checks.equal(wheel.restored_acquisitions, [2],
             "the carousel restores the stored acquisition on the way "
             "up")

checks.equal(esp_carousel.Carousel.retained_sample_id(wheel.slots[2]),
             "R-RESTORED",
             "under the Sample ID it was measured as")

checks.equal(wheel.slots[2]["occupied"], False,
             "and the slot is NOT marked occupied - the firmware cannot "
             "know whether the soil is still in the cup")

checks.equal(wheel.slots[2]["sample_id"], None,
             "nor does it claim a Sample is loaded in it")

checks.equal(wheel.position_valid, False,
             "and the position is still unknown, because a stored "
             "spectrum says nothing about the mechanism")

checks.equal(wheel.retention_error, None,
             "with no retention error reported")


# ======================================================================
checks.section("clearing a slot keeps the acquisition it produced")

# The semantic bug this guards: emptying a cup and deleting a
# measurement are different operations, and only the second one may
# remove the record.

wheel.mark_occupied(2, "R-RESTORED", acquisition("R-RESTORED", 2))

checks.equal(len(wheel.retained_samples()), 1,
             "the slot holds an acquisition")

wheel.reset_slot(2)

checks.equal(len(wheel.retained_samples()), 1,
             "clear_slot frees the mechanism and KEEPS the acquisition")

checks.equal(sorted(esp_retention.load_all()), [2],
             "and keeps it on the filesystem too, so it survives a "
             "reboot after the slot was cleared")

checks.equal(esp_carousel.Carousel.retained_sample_id(wheel.slots[2]),
             "R-RESTORED",
             "still under its own Sample ID, not the SLOTn placeholder")

wheel.mark_occupied(1, "R-OTHER", acquisition("R-OTHER", 1))
wheel.reset_all_slots()

checks.equal(len(wheel.retained_samples()), 2,
             "clear_all_slots keeps every acquisition as well")

checks.equal(sorted(esp_retention.load_all()), [1, 2],
             "and every file with them")

# Only an explicit delete removes one.
wheel.drop_retained_sample("R-OTHER")

checks.equal(sorted(esp_retention.load_all()), [2],
             "delete_saved_sample removes the file as well as the RAM "
             "copy - a delete that left the file would resurrect it on "
             "the next boot")

wheel.clear_retained_samples()

checks.equal(esp_retention.load_all(), {},
             "and delete_saved_samples removes them all")


# ======================================================================
checks.section("a whole device, measured and rebooted")

# End to end, through the real protocol: measure, then rebuild the
# firmware on the same filesystem, which is what a reset is.

loopback = LoopbackDevice()
loopback.build()

support.command(loopback.service, "connect_servo")
support.command(loopback.service, "sync_position", load_slot=1)
support.command(loopback.service, "select_slot", slot=1,
                sample_id="S-REBOOT")

measured = support.command(loopback.service, "measure_raw", slot=1,
                           sample_id="S-REBOOT")

checks.ok(measured["ok"], "the measurement succeeded")
checks.ok(measured["data"]["retained"],
          "and the device says it retained the acquisition")
checks.ok(measured["data"]["retention_durable"],
          "durably")

listed = support.command(loopback.service, "list_saved_samples")

checks.equal(listed["data"]["storage"], "device_filesystem",
             "and list_saved_samples says where it is kept")

# THE REBOOT.
loopback.service = None
loopback.build()

listed = support.command(loopback.service, "list_saved_samples")
ids = [entry["sample_id"] for entry in listed["data"]["samples"]]

checks.equal(ids, ["S-REBOOT"],
             "after a reset the device still lists the acquisition")

fetched = support.command(loopback.service, "get_saved_sample",
                          sample_id="S-REBOOT")

checks.ok(fetched["ok"], "and it can still be fetched")
checks.equal(
    sorted(fetched["data"]["measurement"]["illuminations"]),
    ["ir", "uv", "white"],
    "with all three illuminations intact")

status = support.command(loopback.service, "get_status")

checks.equal(status["data"]["retention"]["count"], 1,
             "and get_status reports one persisted acquisition")

checks.ok(status["data"]["retention"]["durable"],
          "and that retention is durable on this board")

deleted = support.command(loopback.service, "delete_saved_sample",
                          sample_id="S-REBOOT")

checks.ok(deleted["ok"], "deleting it succeeds")

loopback.service = None
loopback.build()

listed = support.command(loopback.service, "list_saved_samples")

checks.equal(listed["data"]["samples"], [],
             "and it stays deleted across the next reset - a delete "
             "that only emptied RAM would bring it back")


sys.exit(checks.report())
