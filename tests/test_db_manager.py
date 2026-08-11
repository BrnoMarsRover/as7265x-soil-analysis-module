"""Acceptance test for the Sample Database manager (spec section 28)."""

import os
import shutil
import sys
import tempfile

import test_firmware as fw

sys.path.insert(0, os.path.join(fw.REPO, "host"))


def main():
    wd = tempfile.mkdtemp(prefix="freya_db_")
    for n in ("database.json", "references.json"):
        shutil.copy(os.path.join(fw.FIRMWARE, n), os.path.join(wd, n))
    os.chdir(wd)

    main_mod = fw.load_main_module()
    import config

    config.STARTUP_DELAY_SECONDS = 0
    config.SERVO_SETTLE_TIME = 0
    config.SCAN_SETTLE_TIME = 0
    config.NEXT_SLOT_CW_MS = 1
    config.NEXT_SLOT_CCW_MS = 2
    config.LOAD_TO_SCAN_CW_MS = 1
    config.SCAN_TO_LOAD_CCW_MS = 1
    config.SERVO_INTER_STEP_PAUSE_MS = 0

    sci = main_mod.ScienceModule()
    sci.boot()

    from as7265x import SoilMeasurementSystem

    drv = fw.FakeDriver({c: 100.0 for c in "ABCDEFGHIJKLRSTUVW"})
    sci.driver = drv
    sci.sensor = SoilMeasurementSystem(drv)
    sci.sensor_error = None
    sci.install_references()

    d = sci.dispatch_command
    check = fw.check

    print("\n[db] exception fix")
    # The bug that masked every sensor error: constructing CommandError.
    err = main_mod.CommandError("TEST_CODE", "test message",
                                data={"stage": "X"})
    check("CommandError constructs", err.code == "TEST_CODE")
    check("CommandError keeps its message", str(err) == "test message")

    from carousel import CarouselError
    from as7265x import SensorError

    check("CarouselError constructs",
          CarouselError("C", "m").code == "C")
    check("SensorError constructs and defaults its stage",
          SensorError("S", "m").stage == "unknown")
    check("SensorError keeps details",
          SensorError("S", "m", details={"a": 1}).details == {"a": 1})

    # The real symptom: raw_measurement used to die inside CommandError.
    cold = main_mod.ScienceModule()
    cold.boot()
    r = cold.dispatch_command({"cmd": "raw_measurement"})
    check("raw_measurement now names the real fault",
          not r["ok"]
          and r["error"]["code"] == "SENSOR_NOT_INITIALIZED",
          r.get("error"))
    check("no INTERNAL_ERROR masking it",
          r["error"]["code"] != "INTERNAL_ERROR")

    print("\n[db] acceptance sequence (section 28)")
    d({"cmd": "sync_position", "load_slot": 1})
    d({"cmd": "select_slot", "slot": 1})
    d({"cmd": "prepare_load", "slot": 1, "sample_id": "S001",
       "metadata": {"note": "first"}})

    r = d({"cmd": "list_samples"})
    entry = r["data"]["samples"][0]
    check("S001 exists after prepare", entry["sample_id"] == "S001")
    check("state READY_TO_LOAD", entry["state"] == "READY_TO_LOAD")
    check("measurement flag false", entry["measured"] is False)

    d({"cmd": "confirm_loaded", "slot": 1})
    check("state LOADED after confirm",
          sci.store.get_state("S001") == "LOADED")

    r = d({"cmd": "measure_sample", "slot": 1})
    check("measurement succeeded", r["ok"], r.get("error"))

    r = d({"cmd": "list_samples"})
    entry = r["data"]["samples"][0]
    check("state MEASURED", entry["state"] == "MEASURED")
    check("measurement flag true", entry["measured"] is True)
    check("best match visible in the list",
          entry["best_match"] is not None)

    record = d({"cmd": "get_sample", "sample_id": "S001"})["data"]["sample"]
    check("raw data present", len(record["measurement"]["raw"]) == 18)
    check("normalized data present",
          len(record["measurement"]["normalized"]) == 18)
    check("all matches present",
          len(record["reference_matches"]) == 22)
    check("analysis present",
          bool(record["analysis"]["automatic_conclusion"]))

    # edit metadata, measurement must survive untouched
    raw_before = dict(record["measurement"]["raw"])
    matches_before = list(record["reference_matches"])

    d({"cmd": "update_sample_metadata", "sample_id": "S001",
       "metadata": {"note": "edited note"}})

    record = d({"cmd": "get_sample", "sample_id": "S001"})["data"]["sample"]
    check("new note visible", record["metadata"]["note"] == "edited note")
    check("raw spectrum unchanged by the edit",
          record["measurement"]["raw"] == raw_before)
    check("matches unchanged by the edit",
          record["reference_matches"] == matches_before)
    check("state still MEASURED", record["state"] == "MEASURED")

    print("\n[db] rename")
    r = d({"cmd": "rename_sample", "sample_id": "S001",
           "new_sample_id": "TEST_SOIL_1"})
    check("rename ok", r["ok"], r.get("error"))
    check("runtime slot followed the rename",
          sci.carousel.slots[1]["sample_id"] == "TEST_SOIL_1",
          sci.carousel.slots[1]["sample_id"])
    check("new ID exists", sci.store.has_sample("TEST_SOIL_1"))
    check("old ID is gone", not sci.store.has_sample("S001"))

    renamed = d({"cmd": "get_sample",
                 "sample_id": "TEST_SOIL_1"})["data"]["sample"]
    check("science survived the rename",
          renamed["measurement"]["raw"] == raw_before)
    check("old record file removed",
          not os.path.exists("samples/S001.json"))

    # a second sample, to prove nothing else is touched
    d({"cmd": "select_slot", "slot": 2})
    d({"cmd": "prepare_load", "slot": 2, "sample_id": "S002"})
    d({"cmd": "confirm_loaded", "slot": 2})
    d({"cmd": "measure_sample", "slot": 2})
    other_before = d({"cmd": "get_sample",
                      "sample_id": "S002"})["data"]["sample"]

    r = d({"cmd": "rename_sample", "sample_id": "TEST_SOIL_1",
           "new_sample_id": "S002"})
    check("rename onto an existing ID refused",
          not r["ok"] and r["error"]["code"] == "SAMPLE_ID_ALREADY_EXISTS",
          r.get("error"))

    print("\n[db] delete")
    r = d({"cmd": "delete_sample", "sample_id": "NOPE"})
    check("deleting a missing sample is refused",
          not r["ok"] and r["error"]["code"] == "SAMPLE_NOT_FOUND")

    r = d({"cmd": "delete_sample", "sample_id": "TEST_SOIL_1",
           "clear_slot": True})
    check("delete ok", r["ok"], r.get("error"))
    check("record removed", not sci.store.has_sample("TEST_SOIL_1"))
    check("linked slot reported",
          r["data"]["was_linked_to_slot"] == 1)
    check("runtime slot cleared", r["data"]["slot_cleared"] == 1)
    check("no dangling sample_id",
          sci.carousel.slots[1]["sample_id"] is None
          and sci.carousel.slots[1]["state"] == "EMPTY")
    check("record file removed",
          not os.path.exists("samples/TEST_SOIL_1.json"))

    other_after = d({"cmd": "get_sample",
                     "sample_id": "S002"})["data"]["sample"]
    check("the other sample was NOT modified",
          other_after == other_before)

    print("\n[db] corrupted database refuses writes")
    with open("samples.json", "w") as handle:
        handle.write("{ this is not json")

    broken = main_mod.ScienceModule()
    broken.boot()

    r = broken.dispatch_command({"cmd": "get_database_health"})
    check("health reports the corruption",
          r["data"]["ready"] is False
          and r["data"]["error_code"] == "SAMPLES_JSON_INVALID", r["data"])

    r = broken.dispatch_command({"cmd": "delete_sample",
                                 "sample_id": "S002"})
    check("delete refused on a corrupt database",
          not r["ok"] and r["error"]["code"] == "SAMPLES_JSON_INVALID",
          r.get("error"))

    with open("samples.json") as handle:
        check("corrupt file was NOT overwritten",
              handle.read() == "{ this is not json")

    print("\n[db] other databases untouched")
    import hashlib

    def digest(path):
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()

    check("database.json untouched by the manager",
          digest("database.json")
          == digest(os.path.join(fw.FIRMWARE, "database.json")))
    check("references.json untouched by the manager",
          digest("references.json")
          == digest(os.path.join(fw.FIRMWARE, "references.json")))

    shutil.rmtree(wd, ignore_errors=True)


if __name__ == "__main__":
    main()

    print("\n" + "=" * 60)

    if fw.FAILURES:
        print("{} of {} FAILED:".format(len(fw.FAILURES), fw.CHECKS[0]))
        for name in fw.FAILURES:
            print("  - {}".format(name))
        sys.exit(1)

    print("all {} database checks passed".format(fw.CHECKS[0]))
