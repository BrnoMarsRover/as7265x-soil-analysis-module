"""
Host-side harness that runs the real ESP32 firmware modules on CPython.

`machine` is stubbed, `time` gets the MicroPython helpers, and the sensor
is replaced by a deterministic fake. Everything else - config, carousel,
mg995, sample_store, sample_analysis, database and the whole main.py
dispatch layer - is the real code that gets uploaded to the board.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import types

# Repo root derived from this file, so the harness is portable.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIRMWARE = os.path.join(REPO, "firmware")

FAILURES = []
CHECKS = [0]


def check(label, condition, detail=""):
    CHECKS[0] += 1
    if condition:
        print("  ok   {}".format(label))
    else:
        print("  FAIL {} {}".format(label, detail))
        FAILURES.append(label)


# ----------------------------------------------------------------------
# MicroPython shims
# ----------------------------------------------------------------------

time.sleep_ms = lambda ms: None
time.ticks_ms = lambda: int(time.monotonic() * 1000)
time.ticks_diff = lambda a, b: a - b

SERVO_LOG = []


class FakePin:
    def __init__(self, num, *a, **k):
        self.num = num


class FakePWM:
    def __init__(self, pin, freq=50):
        self.pin = pin
        self.freq = freq
        self.deinited = False

    def duty_ns(self, ns):
        SERVO_LOG.append(("pulse", ns // 1000))

    def deinit(self):
        self.deinited = True
        SERVO_LOG.append(("deinit", None))


class FakeUART:
    def __init__(self, *a, **k):
        self.written = []

    def any(self):
        return 0

    def read(self, n):
        return None

    def write(self, data):
        self.written.append(data)
        return len(data)


class FakeI2C:
    def __init__(self, *a, **k):
        pass

    def scan(self):
        return []


machine = types.ModuleType("machine")
machine.Pin = FakePin
machine.PWM = FakePWM
machine.UART = FakeUART
machine.I2C = FakeI2C
sys.modules["machine"] = machine

sys.path.insert(0, FIRMWARE)


# ----------------------------------------------------------------------
# deterministic fake AS7265x
# ----------------------------------------------------------------------

class FakeDevice:
    LED_WHITE = 0x00

    def enable_bulb(self, d):
        pass

    def disable_bulb(self, d):
        pass

    def set_bulb_current(self, c, d):
        pass

    def set_integration_cycles(self, c):
        pass

    def set_gain(self, g):
        pass


class FakeDriver:
    def __init__(self, values):
        self.sensor = FakeDevice()
        self.values = values
        self.reads = 0
        self.fail = False

    def read_once(self):
        self.reads += 1
        if self.fail:
            raise OSError("AS7265X timeout")
        return dict(self.values)


def load_main_module():
    """Load main.py without running its trailing main() call."""
    with open(os.path.join(FIRMWARE, "main.py"), "r") as handle:
        source = handle.read()

    assert source.rstrip().endswith("main()")
    source = source.rstrip()[: -len("main()")]

    module = types.ModuleType("main")
    module.__dict__["__name__"] = "main"
    exec(compile(source, "main.py", "exec"), module.__dict__)
    return module


def run():
    workdir = tempfile.mkdtemp(prefix="freya_")
    for name in ("database.json", "references.json"):
        shutil.copy(os.path.join(FIRMWARE, name), os.path.join(workdir, name))
    os.chdir(workdir)

    main = load_main_module()
    import config
    import sample_analysis as analysis
    from carousel import Carousel

    # ------------------------------------------------------------------
    print("\n[1] boot state")
    # ------------------------------------------------------------------
    config.STARTUP_DELAY_SECONDS = 0
    config.SERVO_SETTLE_TIME = 0
    config.SCAN_SETTLE_TIME = 0
    config.NEXT_SLOT_CW_MS = 1
    config.NEXT_SLOT_CCW_MS = 2
    config.SERVO_INTER_STEP_PAUSE_MS = 0

    # stdout IS the protocol stream now, so boot must be silent.
    import contextlib
    import io as _io

    captured = _io.StringIO()
    sci = main.ScienceModule()
    with contextlib.redirect_stdout(captured):
        sci.boot()

    check("boot writes nothing to stdout", captured.getvalue() == "",
          repr(captured.getvalue()[:200]))

    check("sensor init failed gracefully", sci.sensor is None)
    check("references loaded", sci.references_ok is True, sci.references_error)
    check("database has 22 materials", sci.database_count() == 22,
          sci.database_count())
    check("all 8 slots EMPTY",
          all(s["state"] == "EMPTY" for s in sci.carousel.slot_list()))
    check("no slot occupied",
          all(not s["occupied"] for s in sci.carousel.slot_list()))
    check("position starts unsynchronized",
          sci.carousel.position_valid is False)
    check("current_scan_slot is None",
          sci.carousel.current_scan_slot is None)
    check("no servo PWM created at boot",
          sci.servo.pwm is None and SERVO_LOG == [], SERVO_LOG)
    check("no machine.UART peripheral was created",
          not hasattr(sci, "link"))

    # No firmware module may construct a UART peripheral at all.
    uart_users = []
    for name in os.listdir(FIRMWARE):
        if not name.endswith(".py"):
            continue
        text = open(os.path.join(FIRMWARE, name)).read()
        if "UART(" in text or "import UART" in text:
            uart_users.append(name)

    check("no firmware module constructs a UART", not uart_users, uart_users)
    check("uart_protocol.py is gone",
          not os.path.exists(os.path.join(FIRMWARE, "uart_protocol.py")))

    # attach the deterministic sensor
    from as7265x import SoilMeasurementSystem
    raw_values = {
        "A": 12.0, "B": 40.0, "C": 130.0, "D": 70.0, "E": 110.0, "F": 150.0,
        "G": 120.0, "H": 190.0, "I": 190.0, "J": 45.0, "K": 5.0, "L": 7.0,
        "R": 600.0, "S": 180.0, "T": 40.0, "U": 22.0, "V": 33.0, "W": 20.0,
    }
    driver = FakeDriver(raw_values)
    sci.driver = driver
    sci.sensor = SoilMeasurementSystem(driver)
    sci.sensor_error = None
    sci.install_references()

    # ------------------------------------------------------------------
    print("\n[2] basic protocol")
    # ------------------------------------------------------------------
    r = sci.dispatch_command({"request_id": "42", "cmd": "ping"})
    check("ping ok", r["ok"] and r["data"]["pong"] is True)
    check("request_id echoed", r["request_id"] == "42")

    r = sci.dispatch_command({"request_id": "43", "cmd": "get_status"})
    d = r["data"]
    check("get_status ok", r["ok"])
    check("status reports 8 slots", len(d["slots"]) == 8)
    check("status references_loaded", d["references_loaded"] is True)
    check("status material count", d["database_material_count"] == 22)
    check("status position_valid False", d["position_valid"] is False)
    check("status can_measure", d["can_measure"] is True)

    r = sci.dispatch_command({"cmd": "nope"})
    check("unknown command rejected",
          not r["ok"] and r["error"]["code"] == "UNKNOWN_COMMAND")

    r = sci.dispatch_command(["not", "an", "object"])
    check("non-object rejected",
          not r["ok"] and r["error"]["code"] == "BAD_REQUEST")

    r = sci.dispatch_command({"cmd": "get_slots", "slot": 1})
    check("get_slots ok", r["ok"] and len(r["data"]["slots"]) == 8)

    # ------------------------------------------------------------------
    print("\n[3] movement refused before sync")
    # ------------------------------------------------------------------
    r = sci.dispatch_command({"cmd": "prepare_load", "slot": 1, "sample_id": "S001"})
    check("prepare_load refused before sync",
          not r["ok"] and r["error"]["code"] == "POSITION_NOT_SYNCHRONIZED",
          r.get("error"))
    check("no servo movement happened", SERVO_LOG == [], SERVO_LOG)
    check("slot 1 still EMPTY",
          sci.carousel.slots[1]["state"] == "EMPTY")

    # ------------------------------------------------------------------
    print("\n[4] geometry")
    # ------------------------------------------------------------------
    r = sci.dispatch_command({"cmd": "sync_position", "scan_slot": 1})
    check("sync_position ok", r["ok"])
    check("scanner = 1", r["data"]["carousel"]["current_scan_slot"] == 1)
    check("loader = 5", r["data"]["carousel"]["current_load_slot"] == 5)
    check("sync moved nothing", SERVO_LOG == [], SERVO_LOG)

    cz = Carousel(None)
    expected = {1: 5, 2: 6, 3: 7, 4: 8, 5: 1, 6: 2, 7: 3, 8: 4}
    ok = all(cz.load_slot_for(s) == expected[s] for s in expected)
    check("load_slot_for matches 4-slot opposite rule", ok,
          {s: cz.load_slot_for(s) for s in expected})

    spec = all(cz.load_slot_for(s) == ((s + 3) % 8) + 1 for s in range(1, 9))
    check("matches spec formula ((scan+3)%8)+1", spec)

    inv = all(cz.scan_slot_for_load(cz.load_slot_for(s)) == s
              for s in range(1, 9))
    check("scan_slot_for_load inverts load_slot_for", inv)
    check("slot 3 at loader means slot 7 at scanner",
          cz.scan_slot_for_load(3) == 7)

    cz.sync_position(1)
    plans = {t: cz.plan_move(t) for t in range(1, 9)}
    check("plan 1->1 is zero steps", plans[1] == ("cw", 0))
    check("plan 1->2 is 1 cw", plans[2] == ("cw", 1))
    check("plan 1->8 is 1 ccw (shortest path)", plans[8] == ("ccw", 1))
    check("plan 1->7 is 2 ccw", plans[7] == ("ccw", 2))
    check("plan 1->5 is 4 cw (half-turn tie)", plans[5] == ("cw", 4))
    check("no plan exceeds 4 steps",
          all(p[1] <= 4 for p in plans.values()), plans)

    # ------------------------------------------------------------------
    print("\n[5] prepare_load")
    # ------------------------------------------------------------------
    SERVO_LOG.clear()
    r = sci.dispatch_command({
        "cmd": "prepare_load", "slot": 1, "sample_id": "S001",
        "timestamp": "2026-08-10T09:00:00+00:00",
        "metadata": {"task": "regolith survey", "operator": "Maksym"},
    })
    check("prepare_load ok", r["ok"], r.get("error"))
    slot1 = sci.carousel.slots[1]
    check("slot 1 READY_TO_LOAD", slot1["state"] == "READY_TO_LOAD")
    check("sample id assigned", slot1["sample_id"] == "S001")
    check("slot not yet occupied", slot1["occupied"] is False)
    check("created_at from PC",
          slot1["created_at"] == "2026-08-10T09:00:00+00:00")
    check("slot 1 is now at the loader",
          sci.carousel.get_load_slot() == 1)
    check("scanner now shows slot 5",
          sci.carousel.current_scan_slot == 5)
    check("servo actually moved", any(e[0] == "pulse" for e in SERVO_LOG))
    check("4 steps executed", r["data"]["move"]["steps"] == 4,
          r["data"]["move"])

    r = sci.dispatch_command({"cmd": "prepare_load", "slot": 1, "sample_id": "S002"})
    check("prepare_load on busy slot refused",
          not r["ok"] and r["error"]["code"] == "SLOT_NOT_EMPTY")

    r = sci.dispatch_command({"cmd": "prepare_load", "slot": 2, "sample_id": "S001"})
    check("duplicate sample id refused",
          not r["ok"] and r["error"]["code"] == "DUPLICATE_SAMPLE_ID")

    r = sci.dispatch_command({"cmd": "prepare_load", "slot": 9, "sample_id": "S9"})
    check("slot 9 refused",
          not r["ok"] and r["error"]["code"] == "INVALID_SLOT")

    r = sci.dispatch_command({"cmd": "prepare_load", "slot": 0, "sample_id": "S9"})
    check("slot 0 refused",
          not r["ok"] and r["error"]["code"] == "INVALID_SLOT")

    r = sci.dispatch_command({"cmd": "prepare_load", "slot": 2,
                      "sample_id": "bad id!"})
    check("illegal sample id refused",
          not r["ok"] and r["error"]["code"] == "INVALID_SAMPLE_ID")

    r = sci.dispatch_command({"cmd": "prepare_load", "slot": 2})
    check("missing sample_id refused",
          not r["ok"] and r["error"]["code"] == "MISSING_FIELD")

    # ------------------------------------------------------------------
    print("\n[6] measure refuses unconfirmed / empty slots")
    # ------------------------------------------------------------------
    r = sci.dispatch_command({"cmd": "measure_sample", "slot": 1})
    check("measure on READY_TO_LOAD refused",
          not r["ok"] and r["error"]["code"] == "SLOT_NOT_LOADED")

    r = sci.dispatch_command({"cmd": "measure_sample", "slot": 4})
    check("measure on EMPTY refused",
          not r["ok"] and r["error"]["code"] == "SLOT_EMPTY")

    check("no sensor read attempted", driver.reads == 0, driver.reads)

    # ------------------------------------------------------------------
    print("\n[7] confirm_loaded")
    # ------------------------------------------------------------------
    r = sci.dispatch_command({"cmd": "confirm_loaded", "slot": 1,
                      "timestamp": "2026-08-10T09:05:00+00:00"})
    check("confirm_loaded ok", r["ok"], r.get("error"))
    check("slot 1 LOADED", slot1["state"] == "LOADED")
    check("slot 1 occupied", slot1["occupied"] is True)
    check("loaded_at recorded",
          slot1["loaded_at"] == "2026-08-10T09:05:00+00:00")

    r = sci.dispatch_command({"cmd": "confirm_loaded", "slot": 3})
    check("confirm on empty slot refused",
          not r["ok"] and r["error"]["code"] == "SLOT_EMPTY")

    # ------------------------------------------------------------------
    print("\n[8] measure_sample")
    # ------------------------------------------------------------------
    white_before = dict(sci.sensor.white_ref)
    dark_before = dict(sci.sensor.dark_ref)
    ref_mtime = os.stat("references.json").st_mtime
    db_mtime = os.stat("database.json").st_mtime

    SERVO_LOG.clear()
    r = sci.dispatch_command({
        "cmd": "measure_sample", "slot": 1,
        "timestamp": "2026-08-10T09:10:00+00:00",
        "metadata": {"location": "site B", "map_point": "MP-04"},
    })
    check("measure_sample ok", r["ok"], r.get("error"))
    data = r["data"]
    rec = data["sample"]

    check("exactly one sensor read", driver.reads == 1, driver.reads)
    check("slot 1 is at the scanner after measurement",
          sci.carousel.current_scan_slot == 1
          and sci.carousel.phase() == "SCAN", sci.carousel.status())
    check("servo moved for the measurement",
          any(e[0] == "pulse" for e in SERVO_LOG))

    check("record sample_id", rec["sample_id"] == "S001")
    check("record slot_id", rec["slot_id"] == 1)
    check("18 raw channels", len(rec["measurement"]["raw"]) == 18)
    check("18 dark-corrected channels",
          len(rec["measurement"]["dark_corrected"]) == 18)
    check("18 normalized channels",
          len(rec["measurement"]["normalized"]) == 18)
    check("18 wavelengths", len(rec["measurement"]["channels_nm"]) == 18)
    check("wavelength A = 410",
          rec["measurement"]["channels_nm"]["A"] == 410)
    check("wavelength W = 940",
          rec["measurement"]["channels_nm"]["W"] == 940)

    # verify the actual arithmetic on one channel
    refs = json.load(open("references.json"))
    ch = "H"
    exp_dc = raw_values[ch] - refs["dark"][ch]
    exp_norm = exp_dc / (refs["white"][ch] - refs["dark"][ch])
    check("dark correction = Sample - Dark",
          abs(rec["measurement"]["dark_corrected"][ch] - exp_dc) < 1e-3,
          (rec["measurement"]["dark_corrected"][ch], exp_dc))
    check("normalization = (S-D)/(W-D)",
          abs(rec["measurement"]["normalized"][ch] - exp_norm) < 1e-5,
          (rec["measurement"]["normalized"][ch], exp_norm))

    ch = "D"  # the one channel with a non-zero dark reference
    exp_dc = raw_values[ch] - refs["dark"][ch]
    check("non-zero dark reference applied (channel D)",
          abs(rec["measurement"]["dark_corrected"][ch] - exp_dc) < 1e-3,
          (rec["measurement"]["dark_corrected"][ch], exp_dc))

    check("white reference untouched", sci.sensor.white_ref == white_before)
    check("dark reference untouched", sci.sensor.dark_ref == dark_before)
    check("references.json not written",
          os.stat("references.json").st_mtime == ref_mtime)
    check("database.json not written",
          os.stat("database.json").st_mtime == db_mtime)

    matches = rec["reference_matches"]
    check("compared against every material", len(matches) == 22, len(matches))
    scores = [m["similarity_percent"] for m in matches]
    check("matches sorted highest first", scores == sorted(scores,
                                                           reverse=True))
    check("ranks are 1..22",
          [m["rank"] for m in matches] == list(range(1, 23)))
    check("every match names a material",
          all(m["material"] for m in matches))

    an = rec["analysis"]
    check("analysis has a conclusion", bool(an["automatic_conclusion"]))
    check("analysis status is a known value",
          an["status"] in ("STRONG_REFERENCE_MATCH", "AMBIGUOUS",
                           "WEAK_REFERENCE_MATCH", "NO_DATABASE"), an["status"])
    check("best match is rank 1", an["best_match"] == matches[0]["material"])
    check("score difference consistent",
          abs(an["score_difference"] -
              (matches[0]["similarity_percent"] -
               matches[1]["similarity_percent"])) < 0.02)
    check("conclusion avoids the word 'probability'",
          "probabilit" not in an["automatic_conclusion"].lower())
    check("thresholds recorded with the result",
          an["thresholds"]["metric"] == "cosine_similarity")

    check("metadata merged from prepare_load and measure",
          rec["metadata"]["task"] == "regolith survey"
          and rec["metadata"]["location"] == "site B")
    check("unset metadata stays null",
          rec["metadata"]["photo"] is None
          and rec["metadata"]["sensor_distance_mm"] is None)
    check("all three timestamps present",
          rec["timestamps"]["created_at"] == "2026-08-10T09:00:00+00:00"
          and rec["timestamps"]["loaded_at"] == "2026-08-10T09:05:00+00:00"
          and rec["timestamps"]["measured_at"] == "2026-08-10T09:10:00+00:00")
    check("sensor settings recorded",
          rec["measurement"]["sensor_settings"]["integration_cycles"] == 100
          and rec["measurement"]["sensor_settings"]["gain_x"] == "16x")
    check("calibration provenance recorded",
          rec["measurement"]["calibration"]["reference_file"]
          == "references.json")

    check("slot MEASURED", slot1["state"] == "MEASURED")
    check("slot still occupied", slot1["occupied"] is True)
    check("slot measured flag", slot1["measured"] is True)
    check("saved flag true", data["saved"] is True)

    r = sci.dispatch_command({"cmd": "measure_sample", "slot": 1})
    check("re-measure refused",
          not r["ok"] and r["error"]["code"] == "SLOT_ALREADY_MEASURED")

    # ------------------------------------------------------------------
    print("\n[9] persistence")
    # ------------------------------------------------------------------
    check("samples.json created", os.path.exists("samples.json"))
    check("record file created", os.path.exists("samples/S001.json"))
    index = json.load(open("samples.json"))
    check("index holds one summary", len(index["samples"]) == 1)
    check("index summary is compact", len(json.dumps(index)) < 600,
          len(json.dumps(index)))
    check("no leftover temp file", not os.path.exists("samples.json.tmp"))
    check("no leftover backup", not os.path.exists("samples.json.bak"))

    r = sci.dispatch_command({"cmd": "list_samples"})
    check("list_samples ok", r["ok"] and r["data"]["count"] == 1)

    r = sci.dispatch_command({"cmd": "get_sample", "sample_id": "S001"})
    check("get_sample returns the full record",
          r["ok"] and len(r["data"]["sample"]["reference_matches"]) == 22)

    r = sci.dispatch_command({"cmd": "get_sample", "sample_id": "NOPE"})
    check("missing sample reported",
          not r["ok"] and r["error"]["code"] == "SAMPLE_NOT_FOUND")

    r = sci.dispatch_command({"cmd": "update_sample_metadata", "sample_id": "S001",
                      "metadata": {"note": "wet sample", "photo": "IMG_88.jpg"}})
    check("update_sample_metadata ok", r["ok"], r.get("error"))
    stored = json.load(open("samples/S001.json"))
    check("metadata updated on flash",
          stored["metadata"]["note"] == "wet sample"
          and stored["metadata"]["photo"] == "IMG_88.jpg")
    check("earlier metadata preserved",
          stored["metadata"]["task"] == "regolith survey")
    check("spectra untouched by metadata update",
          stored["measurement"]["raw"] == rec["measurement"]["raw"])

    # ------------------------------------------------------------------
    print("\n[10] clear_slot keeps science data")
    # ------------------------------------------------------------------
    r = sci.dispatch_command({"cmd": "clear_slot", "slot": 1})
    check("clear_slot ok", r["ok"])
    check("slot back to EMPTY", sci.carousel.slots[1]["state"] == "EMPTY")
    check("slot no longer occupied",
          sci.carousel.slots[1]["occupied"] is False)
    check("sample id detached", sci.carousel.slots[1]["sample_id"] is None)
    check("record file still on flash", os.path.exists("samples/S001.json"))
    check("clear reports the record was kept",
          r["data"]["science_record_kept"] is True)
    check("still listed as a stored sample", sci.store.count() == 1)

    # ------------------------------------------------------------------
    print("\n[11] servo maintenance")
    # ------------------------------------------------------------------
    sci.dispatch_command({"cmd": "sync_position", "scan_slot": 2})
    r = sci.dispatch_command({"cmd": "servo_jog_cw", "steps": 1})
    check("whole-slot jog keeps tracking",
          r["ok"] and r["data"]["carousel"]["position_valid"] is True)
    check("jog advanced the tracked slot",
          sci.carousel.current_scan_slot == 3,
          sci.carousel.current_scan_slot)

    r = sci.dispatch_command({"cmd": "servo_jog_ccw", "steps": 2})
    check("ccw jog moves the other way",
          sci.carousel.current_scan_slot == 1,
          sci.carousel.current_scan_slot)

    r = sci.dispatch_command({"cmd": "servo_jog_cw", "duration_ms": 120})
    check("free jog invalidates position",
          r["ok"] and r["data"]["carousel"]["position_valid"] is False)
    check("scan slot cleared", sci.carousel.current_scan_slot is None)

    r = sci.dispatch_command({"cmd": "measure_sample", "slot": 3})
    check("movement refused again after invalidation",
          not r["ok"] and r["error"]["code"] in
          ("SLOT_EMPTY", "POSITION_NOT_SYNCHRONIZED"))

    r = sci.dispatch_command({"cmd": "servo_jog_cw", "duration_ms": 99999})
    check("oversized jog refused",
          not r["ok"] and r["error"]["code"] == "BAD_REQUEST")

    r = sci.dispatch_command({"cmd": "servo_jog_cw", "steps": 1, "duration_ms": 100})
    check("steps+duration together refused",
          not r["ok"] and r["error"]["code"] == "BAD_REQUEST")

    r = sci.dispatch_command({"cmd": "servo_stop"})
    check("servo_stop ok", r["ok"])
    check("pwm released after stop", sci.servo.pwm is None)

    # ------------------------------------------------------------------
    print("\n[12] sensor failure does not corrupt state")
    # ------------------------------------------------------------------
    sci.dispatch_command({"cmd": "sync_position", "scan_slot": 1})
    sci.dispatch_command({"cmd": "prepare_load", "slot": 4, "sample_id": "S002"})
    sci.dispatch_command({"cmd": "confirm_loaded", "slot": 4})

    driver.fail = True
    r = sci.dispatch_command({"cmd": "measure_sample", "slot": 4})
    driver.fail = False

    check("sensor timeout reported as SENSOR_DATA_READY_TIMEOUT",
          not r["ok"] and r["error"]["code"] == "SENSOR_DATA_READY_TIMEOUT",
          r.get("error"))
    check("slot stays LOADED after sensor failure",
          sci.carousel.slots[4]["state"] == "LOADED")
    check("no measured data saved after sensor failure",
          sci.store.get_state("S002") != "MEASURED",
          sci.store.get_state("S002"))
    check("record exists but holds no measurement yet",
          "measurement" not in (sci.store.get_sample("S002") or {}))

    # A non-timeout fault must map to the generic sensor error instead.
    original_read = driver.read_once

    def bus_fault():
        raise OSError("I2C bus error")

    driver.read_once = bus_fault
    r = sci.dispatch_command({"cmd": "measure_sample", "slot": 4})
    driver.read_once = original_read

    check("non-timeout sensor fault reported as SENSOR_ERROR",
          not r["ok"] and r["error"]["code"] == "SENSOR_ERROR",
          r.get("error"))
    check("slot still LOADED after bus fault",
          sci.carousel.slots[4]["state"] == "LOADED")

    r = sci.dispatch_command({"cmd": "measure_sample", "slot": 4})
    check("retry after failure succeeds", r["ok"], r.get("error"))
    check("second sample stored", sci.store.count() == 2)

    # ------------------------------------------------------------------
    print("\n[13] database unavailable still preserves science")
    # ------------------------------------------------------------------
    sci.dispatch_command({"cmd": "prepare_load", "slot": 6, "sample_id": "S003"})
    sci.dispatch_command({"cmd": "confirm_loaded", "slot": 6})

    saved_db = sci.database
    sci.database = None
    r = sci.dispatch_command({"cmd": "measure_sample", "slot": 6})
    sci.database = saved_db

    check("measurement still succeeds without database", r["ok"],
          r.get("error"))
    rec3 = r["data"]["sample"]
    check("spectra still stored", len(rec3["measurement"]["normalized"]) == 18)
    check("status marked NO_DATABASE",
          rec3["analysis"]["status"] == "NO_DATABASE")
    check("no matches claimed", rec3["reference_matches"] == [])

    # ------------------------------------------------------------------
    print("\n[14] storage failure is not reported as saved")
    # ------------------------------------------------------------------
    sci.dispatch_command({"cmd": "prepare_load", "slot": 7, "sample_id": "S004"})
    sci.dispatch_command({"cmd": "confirm_loaded", "slot": 7})

    import sample_store

    def boom(path, obj):
        raise sample_store.StorageError("simulated flash failure")

    original_write = sample_store._safe_write_json
    sample_store._safe_write_json = boom
    r = sci.dispatch_command({"cmd": "measure_sample", "slot": 7})
    sample_store._safe_write_json = original_write

    check("storage failure reported",
          not r["ok"] and r["error"]["code"] == "SAMPLE_SAVE_ERROR", r.get("error"))
    check("slot stays LOADED so it can be retried",
          sci.carousel.slots[7]["state"] == "LOADED")
    check("record returned with the error so no science is lost",
          r["data"]["saved"] is False
          and len(r["data"]["sample"]["measurement"]["raw"]) == 18)

    # ------------------------------------------------------------------
    print("\n[15] reboot behaviour")
    # ------------------------------------------------------------------
    samples_before = sci.store.count()
    sci2 = main.ScienceModule()
    sci2.boot()

    check("physical slot state forgotten",
          all(s["state"] == "EMPTY" for s in sci2.carousel.slot_list()))
    check("no slot occupied after reboot",
          all(not s["occupied"] for s in sci2.carousel.slot_list()))
    check("position invalid after reboot",
          sci2.carousel.position_valid is False)
    check("sample history survives reboot",
          sci2.store.count() == samples_before, sci2.store.count())
    check("full record still readable after reboot",
          sci2.store.get_sample("S001")["sample_id"] == "S001")
    check("occupancy NOT reconstructed from history",
          sci2.carousel.slots[1]["sample_id"] is None)

    # ------------------------------------------------------------------
    print("\n[16] missing references")
    # ------------------------------------------------------------------
    os.rename("references.json", "references.hidden")
    sci3 = main.ScienceModule()
    sci3.boot()
    sci3.driver = driver
    sci3.sensor = SoilMeasurementSystem(driver)
    sci3.sensor_error = None
    os.rename("references.hidden", "references.json")

    check("references reported invalid", sci3.references_ok is False)
    r = sci3.dispatch_command({"cmd": "get_status"})
    check("UART still functional without references", r["ok"])
    check("status reports the reference error",
          r["data"]["references_loaded"] is False
          and r["data"]["references_error"] is not None)
    check("can_measure is false", r["data"]["can_measure"] is False)

    sci3.dispatch_command({"cmd": "sync_position", "scan_slot": 1})
    sci3.dispatch_command({"cmd": "prepare_load", "slot": 1, "sample_id": "S900"})
    sci3.dispatch_command({"cmd": "confirm_loaded", "slot": 1})
    r = sci3.dispatch_command({"cmd": "measure_sample", "slot": 1})
    check("measurement refused without references",
          not r["ok"] and r["error"]["code"] == "REFERENCES_NOT_LOADED")

    # ------------------------------------------------------------------
    print("\n[17] interpretation branches")
    # ------------------------------------------------------------------
    strong = analysis.interpret([
        {"rank": 1, "material": "Basalt", "similarity_percent": 96.0},
        {"rank": 2, "material": "Andesite", "similarity_percent": 90.0},
    ])
    check("clear winner is STRONG",
          strong["status"] == "STRONG_REFERENCE_MATCH"
          and strong["confidence"] == "HIGH")

    ambiguous = analysis.interpret([
        {"rank": 1, "material": "Basalt", "similarity_percent": 91.4},
        {"rank": 2, "material": "Andesite", "similarity_percent": 91.0},
    ])
    check("close scores are AMBIGUOUS",
          ambiguous["status"] == "AMBIGUOUS"
          and ambiguous["confidence"] == "MODERATE")
    check("ambiguous wording explains why",
          "ambiguous" in ambiguous["automatic_conclusion"])

    weak = analysis.interpret([
        {"rank": 1, "material": "Talc", "similarity_percent": 40.0},
        {"rank": 2, "material": "Kaolin", "similarity_percent": 10.0},
    ])
    check("low score is WEAK",
          weak["status"] == "WEAK_REFERENCE_MATCH"
          and weak["confidence"] == "LOW")
    check("weak wording is conservative",
          "may not be sufficiently represented"
          in weak["automatic_conclusion"])

    none = analysis.interpret([])
    check("empty database is NO_DATABASE",
          none["status"] == "NO_DATABASE" and none["best_match"] is None)

    for label, result in (("strong", strong), ("ambiguous", ambiguous),
                          ("weak", weak), ("none", none)):
        text = result["automatic_conclusion"].lower()
        forbidden = [w for w in ("probability", "chemical certainty",
                                 "confirmed identification") if w in text]
        check("{} conclusion avoids overclaiming".format(label),
              not forbidden, forbidden)

    # ------------------------------------------------------------------
    print("\n[18] stdin/stdout protocol")
    # ------------------------------------------------------------------
    class FakeConsole:
        """Stands in for the MicroPython USB serial console."""

        def __init__(self, lines=()):
            self.lines = list(lines)
            self.out = []

        def readline(self):
            return self.lines.pop(0) if self.lines else ""

        def write(self, text):
            self.out.append(text)

        def flush(self):
            pass

        def frames(self):
            return "".join(self.out).splitlines()

    class FakeSys:
        def __init__(self, console):
            self.stdin = console
            self.stdout = console

    real_sys = main.sys

    console = FakeConsole([
        '{"request_id": "1", "cmd": "ping"}\n',
        '{"request_id": "2", "cmd": "get_slots"}\r\n',
        'this is not json\n',
        '\n',
        '{"request_id": "3", "cmd": "nope"}\n',
    ])
    main.sys = FakeSys(console)

    try:
        check("read_command strips the terminator",
              main.read_command() == '{"request_id": "1", "cmd": "ping"}')

        # feed that first command back through the full path
        console2 = FakeConsole()
        main.sys = FakeSys(console2)
        sci.process_command('{"request_id": "1", "cmd": "ping"}')
        frames = console2.frames()
        check("one command produces exactly one response line",
              len(frames) == 1, frames)
        reply = json.loads(frames[0])
        check("response is valid JSON with ok and request_id",
              reply["ok"] is True and reply["request_id"] == "1")
        check("response carries the pong payload",
              reply["data"]["pong"] is True)

        # CRLF from a terminal must not break parsing
        console3 = FakeConsole(['{"request_id": "2", "cmd": "ping"}\r\n'])
        main.sys = FakeSys(console3)
        line = main.read_command()
        check("CRLF line ending stripped", line.endswith("}"), repr(line))
        sci.process_command(line)
        check("CRLF command still answered",
              json.loads(console3.frames()[0])["ok"] is True)

        # malformed input
        console4 = FakeConsole()
        main.sys = FakeSys(console4)
        sci.process_command("this is not json")
        reply = json.loads(console4.frames()[0])
        check("invalid JSON answered with INVALID_JSON",
              reply["ok"] is False
              and reply["error"]["code"] == "INVALID_JSON", reply)
        check("invalid JSON reply has a null request_id",
              reply["request_id"] is None)

        # oversized command
        console5 = FakeConsole()
        main.sys = FakeSys(console5)
        sci.process_command("x" * (config.MAX_COMMAND_BYTES + 10))
        reply = json.loads(console5.frames()[0])
        check("oversized command rejected",
              reply["error"]["code"] == "COMMAND_TOO_LONG", reply)

        # end of input
        console6 = FakeConsole([])
        main.sys = FakeSys(console6)
        check("empty stdin returns None", main.read_command() is None)

        # every reply must be exactly one newline-terminated JSON object
        console7 = FakeConsole()
        main.sys = FakeSys(console7)
        for cmd in ("ping", "get_status", "get_slots", "list_samples",
                    "get_carousel_status", "get_references",
                    "get_material_names", "get_database_status"):
            sci.process_command(
                json.dumps({"request_id": cmd, "cmd": cmd})
            )

        raw = "".join(console7.out)
        check("every response ends with a newline",
              raw.endswith("\n"))
        check("one line per command",
              len(raw.splitlines()) == 8, len(raw.splitlines()))

        bad = []
        for frame in raw.splitlines():
            try:
                obj = json.loads(frame)
            except ValueError:
                bad.append(frame[:60])
                continue
            if not isinstance(obj, dict) or "ok" not in obj:
                bad.append(frame[:60])

        check("stdout carries nothing but protocol frames", not bad, bad)

        # send_response / send_error helpers
        console8 = FakeConsole()
        main.sys = FakeSys(console8)
        main.send_response("9", "ping", {"x": 1})
        main.send_error("9", "SOME_CODE", "some message", cmd="ping")
        f = console8.frames()
        check("send_response shape",
              json.loads(f[0]) == {"request_id": "9", "ok": True,
                                   "cmd": "ping", "data": {"x": 1}})
        check("send_error shape",
              json.loads(f[1]) == {"request_id": "9", "ok": False,
                                   "cmd": "ping",
                                   "error": {"code": "SOME_CODE",
                                             "message": "some message"}})

        # an unencodable response must still produce a valid frame
        console9 = FakeConsole()
        main.sys = FakeSys(console9)
        main.send_json({"request_id": "10", "ok": True,
                        "data": {"bad": set([1, 2])}})
        reply = json.loads(console9.frames()[0])
        check("unencodable value is sanitized, response still valid",
              reply["request_id"] == "10"
              and isinstance(reply["data"]["bad"], str), reply)
        check("sanitized value names the offending type",
              "set" in reply["data"]["bad"], reply)

        # numbers must never be stringified by the sanitizer
        safe = main.make_json_safe(
            {"n": 1, "f": 2.5, "b": True, "z": None,
             "t": (1, 2), 3: "int key"}
        )
        check("sanitizer keeps numbers as numbers",
              safe["n"] == 1 and safe["f"] == 2.5)
        check("sanitizer keeps bool and null",
              safe["b"] is True and safe["z"] is None)
        check("sanitizer converts tuples to lists",
              safe["t"] == [1, 2])
        check("sanitizer stringifies non-string keys",
              "3" in safe and safe["3"] == "int key", safe)

    finally:
        main.sys = real_sys

    # ------------------------------------------------------------------
    print("\n[18b] debug output stays off")
    # ------------------------------------------------------------------
    check("config.DEBUG defaults to False", config.DEBUG is False)

    captured = _io.StringIO()
    with contextlib.redirect_stdout(captured):
        main._debug("this must not appear")
    check("_debug is silent by default", captured.getvalue() == "",
          repr(captured.getvalue()))

    config.DEBUG = True
    captured = _io.StringIO()
    with contextlib.redirect_stdout(captured):
        main._debug("visible")
    config.DEBUG = False
    check("_debug works when explicitly enabled",
          "visible" in captured.getvalue())

    # a failing command must not print anything to stdout either
    captured = _io.StringIO()
    with contextlib.redirect_stdout(captured):
        r = sci.dispatch_command({"cmd": "measure_sample", "slot": 99})
    check("rejected command prints nothing",
          captured.getvalue() == "", repr(captured.getvalue()))
    check("rejected command still answers",
          not r["ok"] and r["error"]["code"] == "INVALID_SLOT")

    # ------------------------------------------------------------------
    print("\n[20] measurement stage log")
    # ------------------------------------------------------------------
    sci.dispatch_command({"cmd": "sync_position", "scan_slot": 1})
    sci.dispatch_command({"cmd": "prepare_load", "slot": 8,
                          "sample_id": "S010"})
    sci.dispatch_command({"cmd": "confirm_loaded", "slot": 8})

    reads_before = driver.reads
    r = sci.dispatch_command({"cmd": "measure_sample", "slot": 8})
    check("measure with stages succeeds", r["ok"], r.get("error"))

    stages = r["data"]["stages"]
    names = [s["stage"] for s in stages]

    check("nine stages reported", len(stages) == 9, names)
    check("stages are in the documented order",
          names == ["VALIDATE_SLOT", "MOVE_TO_SCANNER", "MECHANICAL_SETTLE",
                    "SENSOR_READ", "LOAD_REFERENCES", "NORMALIZE",
                    "DATABASE_COMPARISON", "SAVE_SAMPLE",
                    "UPDATE_SLOT_STATE"], names)
    check("measurement does not swing back on its own",
          "RETURN_TO_LOAD" not in names, names)
    check("all stages report ok", all(s["ok"] for s in stages))
    check("stages numbered 1..9",
          [s["index"] for s in stages] == list(range(1, 10)))
    check("every stage carries a detail line",
          all(s["detail"] for s in stages))
    check("a NEW acquisition happened", driver.reads == reads_before + 1)
    check("sensor read stage reports 18 channels",
          "18/18" in stages[3]["detail"], stages[3]["detail"])
    check("update stage records the transition",
          "LOADED -> MEASURED" in stages[8]["detail"], stages[8]["detail"])
    check("carousel phase is SCAN after measuring",
          r["data"]["carousel"]["carousel_phase"] == "SCAN")

    # failure path: the stage log must name where it stopped
    r = sci.dispatch_command({"cmd": "measure_sample", "slot": 3})
    check("empty slot names the failing stage",
          not r["ok"]
          and r["data"]["failed_stage"] == "VALIDATE_SLOT"
          and r["error"]["code"] == "SLOT_EMPTY", r)
    check("failure reports slot state unchanged",
          r["data"]["slot_state"] == "EMPTY")
    check("failure never claims saved", r["data"]["saved"] is False)

    r = sci.dispatch_command({"cmd": "measure_sample", "slot": 8})
    check("already-measured slot names the stage",
          not r["ok"]
          and r["data"]["failed_stage"] == "VALIDATE_SLOT"
          and r["error"]["code"] == "SLOT_ALREADY_MEASURED")

    # sensor failure must stop at SENSOR_READ
    sci.dispatch_command({"cmd": "prepare_load", "slot": 5,
                          "sample_id": "S011"})
    sci.dispatch_command({"cmd": "confirm_loaded", "slot": 5})

    driver.fail = True
    r = sci.dispatch_command({"cmd": "measure_sample", "slot": 5})
    driver.fail = False

    check("sensor failure stops at SENSOR_READ",
          not r["ok"] and r["data"]["failed_stage"] == "SENSOR_READ", r)
    names = [s["stage"] for s in r["data"]["stages"]]
    check("stages up to the failure are recorded",
          names[:4] == ["VALIDATE_SLOT", "MOVE_TO_SCANNER",
                        "MECHANICAL_SETTLE", "SENSOR_READ"], names)
    check("failed stage flagged not-ok",
          r["data"]["stages"][3]["ok"] is False)
    check("carousel recovered to the loading position after failure",
          names[-1] == "RECOVER_POSITION"
          and r["data"]["stages"][-1]["ok"] is True, names)
    check("slot is back at the loader so the retry can run",
          sci.carousel.phase() == "LOAD", sci.carousel.status())
    check("slot stays LOADED after stage failure",
          sci.carousel.slots[5]["state"] == "LOADED")

    # ------------------------------------------------------------------
    print("\n[21] incomplete spectrum is refused")
    # ------------------------------------------------------------------
    full_values = dict(driver.values)
    partial = dict(full_values)
    del partial["W"]
    del partial["R"]
    driver.values = partial

    r = sci.dispatch_command({"cmd": "measure_sample", "slot": 5})
    driver.values = full_values

    check("missing channels rejected",
          not r["ok"] and r["error"]["code"] == "INCOMPLETE_SPECTRUM", r)
    check("failure names the missing channels",
          sorted(r["data"]["missing_channels"]) == ["R", "W"],
          r["data"].get("missing_channels"))
    check("incomplete spectrum stops at SENSOR_READ",
          r["data"]["failed_stage"] == "SENSOR_READ")
    check("incomplete spectrum is not saved as measured",
          sci.store.get_state("S011") != "MEASURED"),
    check("slot still LOADED after incomplete spectrum",
          sci.carousel.slots[5]["state"] == "LOADED")

    # ------------------------------------------------------------------
    print("\n[22] analysis failure never destroys the spectrum")
    # ------------------------------------------------------------------
    class ExplodingDatabase:
        data = {"x": {}}

        def find_matches(self, measured, top_n=3):
            raise ValueError("simulated comparison fault")

    saved_db = sci.database
    sci.database = ExplodingDatabase()
    r = sci.dispatch_command({"cmd": "measure_sample", "slot": 5})
    sci.database = saved_db

    check("measurement still succeeds when analysis fails",
          r["ok"], r.get("error"))
    check("analysis_status reported FAILED",
          r["data"]["analysis_status"] == "FAILED")
    check("analysis error surfaced",
          "simulated comparison fault" in (r["data"]["analysis_error"] or ""))
    rec = r["data"]["sample"]
    check("raw spectrum preserved",
          len(rec["measurement"]["raw"]) == 18)
    check("normalized spectrum preserved",
          len(rec["measurement"]["normalized"]) == 18)
    check("record marked analysis_status FAILED",
          rec["analysis_status"] == "FAILED")
    check("record still saved to flash", sci.store.has_sample("S011"))
    check("slot became MEASURED despite analysis failure",
          sci.carousel.slots[5]["state"] == "MEASURED")
    check("database stage flagged not-ok",
          any(s["stage"] == "DATABASE_COMPARISON" and not s["ok"]
              for s in r["data"]["stages"]))

    # ------------------------------------------------------------------
    print("\n[23] fixed calibration")
    # ------------------------------------------------------------------
    required_white = {
        "A": 29.9166, "B": 135.6742, "C": 458.6046, "D": 256.1812,
        "E": 380.3354, "F": 494.2911, "G": 428.3792, "H": 645.9812,
        "I": 514.9370, "J": 121.7631, "K": 14.9317, "L": 21.1497,
        "R": 1873.6044, "S": 558.7975, "T": 121.0452, "U": 70.2892,
        "V": 105.9207, "W": 61.7612,
    }
    required_dark = dict((c, 0.0) for c in analysis.CHANNELS)
    required_dark["D"] = 3.4855

    refs = json.load(open("references.json"))
    check("references.json has exactly white and dark",
          sorted(refs.keys()) == ["dark", "white"])
    check("white matches the fixed competition reference",
          all(abs(refs["white"][c] - required_white[c]) < 1e-9
              for c in analysis.CHANNELS))
    check("dark matches the fixed competition reference",
          all(abs(refs["dark"][c] - required_dark[c]) < 1e-9
              for c in analysis.CHANNELS))
    check("dark includes W = 0.0", refs["dark"]["W"] == 0.0)

    r = sci.dispatch_command({"cmd": "get_status"})
    cal = r["data"]["calibration"]
    check("status exposes a calibration block", cal is not None)
    check("calibration mode is fixed",
          cal["mode"] == "FIXED_STORED_REFERENCES")
    check("runtime recalibration disabled",
          cal["runtime_recalibration"] == "DISABLED")
    check("dark reported READY 18/18",
          cal["dark"]["state"] == "READY"
          and cal["dark"]["channels_present"] == 18)
    check("white reported READY 18/18",
          cal["white"]["state"] == "READY"
          and cal["white"]["channels_present"] == 18)
    check("calibration id present",
          cal["calibration_id"] == config.CALIBRATION_ID)

    rec = sci.store.get_sample("S010")
    prov = rec["measurement"]["calibration"]
    check("record states fixed calibration mode", prov["mode"] == "fixed")
    check("record names the source", prov["source"] == "references.json")
    check("record names the white reference",
          prov["white_reference"] == "competition_fixed_white")
    check("record names the dark reference",
          prov["dark_reference"] == "competition_fixed_dark")
    check("record carries the calibration id",
          rec["calibration_id"] == config.CALIBRATION_ID)

    # no command may re-measure white or dark
    cmds = list(sci.handlers.keys())
    offenders = [c for c in cmds
                 if any(w in c for w in ("white", "dark", "calibrat"))]
    check("no white/dark calibration command exists", not offenders,
          offenders)

    fw_src = open(os.path.join(FIRMWARE, "main.py")).read()
    check("firmware never calls take_white/take_dark",
          "take_white" not in fw_src and "take_dark" not in fw_src)
    check("no white_measured/dark_measured gate",
          "white_measured" not in fw_src and "dark_measured" not in fw_src)

    # partial calibration must be reported per channel
    broken = {"white": dict(required_white), "dark": dict(required_dark)}
    del broken["white"]["W"]
    with open("references.broken.json", "w") as handle:
        json.dump(broken, handle)

    os.rename("references.json", "references.good.json")
    os.rename("references.broken.json", "references.json")
    sci4 = main.ScienceModule()
    sci4.boot()
    os.remove("references.json")
    os.rename("references.good.json", "references.json")

    cal = sci4.calibration_status()
    check("broken calibration reports not loaded", cal["loaded"] is False)
    check("dark still reported 18/18",
          cal["dark"]["state"] == "READY"
          and cal["dark"]["channels_present"] == 18)
    check("white reported 17/18",
          cal["white"]["channels_present"] == 17, cal["white"])
    check("white names the missing channel",
          cal["white"]["missing"] == ["W"], cal["white"])
    check("white marked ERROR", cal["white"]["state"] == "ERROR")

    sci4.driver = driver
    sci4.sensor = SoilMeasurementSystem(driver)
    sci4.sensor_error = None
    sci4.dispatch_command({"cmd": "sync_position", "scan_slot": 1})
    sci4.dispatch_command({"cmd": "prepare_load", "slot": 1,
                           "sample_id": "S099"})
    sci4.dispatch_command({"cmd": "confirm_loaded", "slot": 1})
    r = sci4.dispatch_command({"cmd": "measure_sample", "slot": 1})
    check("measurement refused with incomplete calibration",
          not r["ok"] and r["error"]["code"] == "REFERENCES_NOT_LOADED", r)
    check("refusal stops at VALIDATE_SLOT",
          r["data"]["failed_stage"] == "VALIDATE_SLOT")

    # ------------------------------------------------------------------
    print("\n[24] help command")
    # ------------------------------------------------------------------
    r = sci.dispatch_command({"cmd": "help"})
    check("help ok", r["ok"], r.get("error"))
    help_data = r["data"]
    check("help lists the commands",
          "measure_sample" in help_data["commands"])
    check("help lists the 9 stages",
          len(help_data["measurement_stages"]) == 9)
    text = help_data["calibration"]["text"]
    check("help explains fixed calibration",
          "fixed Dark Reference" in text and "fixed" in text.lower())
    check("help says no calibration is needed",
          "do NOT need to perform dark or white measurements" in text, text)
    check("help states recalibration disabled",
          help_data["calibration"]["runtime_recalibration"] == "DISABLED")

    # ------------------------------------------------------------------
    print("\n[25] new carousel workflow: sync from the loading hole")
    # ------------------------------------------------------------------
    import mg995

    sci5 = main.ScienceModule()
    sci5.boot()
    sci5.driver = driver
    sci5.sensor = SoilMeasurementSystem(driver)
    sci5.sensor_error = None
    sci5.install_references()

    # Test A - movement is allowed while unsynchronized, so the operator
    # can line Slot 1 up with the loading hole.
    check("starts unsynchronized",
          sci5.carousel.position_valid is False
          and sci5.carousel.selected_slot is None)

    r = sci5.dispatch_command({"cmd": "move_slots",
                               "direction": "cw", "slots": 1})
    check("whole-slot move works before sync", r["ok"], r.get("error"))
    check("one slot spans 45 deg of geometry",
          r["data"]["geometric_degrees"] == 45.0)
    check("one slot is commanded as 45 deg",
          r["data"]["degrees"] == 45.0, r["data"])

    r = sci5.dispatch_command({"cmd": "fine_adjust", "degrees": 2})
    check("fine adjust works before sync", r["ok"], r.get("error"))
    r = sci5.dispatch_command({"cmd": "fine_adjust", "degrees": -1})
    check("negative fine adjust works", r["ok"], r.get("error"))
    check("fine adjust reports CCW for negative",
          r["data"]["adjustment"]["direction"] == "ccw")

    r = sci5.dispatch_command({"cmd": "sync_position", "load_slot": 1})
    check("sync via load_slot ok", r["ok"], r.get("error"))
    cz = r["data"]["carousel"]
    check("loader = Slot 1", cz["current_load_slot"] == 1)
    check("scanner = Slot 5", cz["current_scan_slot"] == 5)
    check("position now valid", cz["position_valid"] is True)
    check("selected slot = 1", cz["selected_slot"] == 1)

    # scanner/loader stay 4 slots apart for every origin
    pairs = {}
    for origin in range(1, 9):
        sci5.carousel.sync_to_load_slot(origin)
        pairs[origin] = sci5.carousel.current_scan_slot
    check("loader N always implies scanner N+4",
          pairs == {1: 5, 2: 6, 3: 7, 4: 8, 5: 1, 6: 2, 7: 3, 8: 4}, pairs)

    # ------------------------------------------------------------------
    print("\n[26] Choose Sample advances 45 deg clockwise")
    # ------------------------------------------------------------------
    sci5.carousel.sync_to_load_slot(1)

    # Test B - every sequential step must be exactly one CW slot.
    sequential = {}
    for start in range(1, 9):
        sci5.carousel.sync_to_load_slot(start)
        target = (start % 8) + 1
        move = sci5.carousel.select_slot(target)
        sequential[(start, target)] = (move["direction"], move["steps"])

    check("every sequential selection is 1 step clockwise",
          all(v == ("cw", 1) for v in sequential.values()), sequential)

    sci5.carousel.sync_to_load_slot(1)
    r = sci5.dispatch_command({"cmd": "select_slot", "slot": 2})
    check("select_slot 1->2 ok", r["ok"], r.get("error"))
    check("moved exactly one slot", r["data"]["move"]["steps"] == 1)
    check("moved clockwise", r["data"]["move"]["direction"] == "cw")
    cz = r["data"]["carousel"]
    check("selected slot is now 2", cz["selected_slot"] == 2)
    check("loader follows to Slot 2", cz["current_load_slot"] == 2)
    check("scanner follows to Slot 6", cz["current_scan_slot"] == 6)

    # non-adjacent selection may still take the shortest path
    sci5.carousel.sync_to_load_slot(1)
    move = sci5.carousel.select_slot(7)
    check("distant slot uses shortest path",
          (move["direction"], move["steps"]) == ("ccw", 2), move)

    # ------------------------------------------------------------------
    print("\n[27] fine adjustment keeps the logical position")
    # ------------------------------------------------------------------
    sci5.carousel.sync_to_load_slot(1)
    sci5.carousel.select_slot(3)

    before = (sci5.carousel.current_scan_slot,
              sci5.carousel.get_load_slot(),
              sci5.carousel.selected_slot)
    check("selected Slot 3 sits at the loader", before[1] == 3, before)
    check("scanner is Slot 7 while loader is Slot 3", before[0] == 7)

    r = sci5.dispatch_command({"cmd": "fine_adjust", "degrees": 1.5})
    check("fine adjust ok", r["ok"], r.get("error"))

    after = (sci5.carousel.current_scan_slot,
             sci5.carousel.get_load_slot(),
             sci5.carousel.selected_slot)
    check("logical position unchanged by fine adjust", before == after,
          (before, after))
    check("adjustment reports no logical change",
          r["data"]["adjustment"]["logical_position_changed"] is False)
    check("position still valid after fine adjust",
          sci5.carousel.position_valid is True)

    r = sci5.dispatch_command({"cmd": "fine_adjust", "degrees": 45})
    check("oversized fine adjust refused",
          not r["ok"] and r["error"]["code"] == "FINE_ADJUST_TOO_LARGE", r)
    check("refusal points at whole-slot movement",
          "whole-slot" in r["error"]["message"])
    check("limit comes from config",
          config.MAX_FINE_ADJUST_DEG == 15.0)

    # ------------------------------------------------------------------
    print("\n[28] movement calibration: geometry vs servo command")
    # ------------------------------------------------------------------
    saved = (config.NEXT_SLOT_CW_MS, config.NEXT_SLOT_CCW_MS,
             config.CW_MS_PER_DEGREE, config.CCW_MS_PER_DEGREE,
             config.LOAD_TO_SCAN_CW_MS, config.SCAN_TO_LOAD_CCW_MS)

    config.NEXT_SLOT_CW_MS = 533
    config.NEXT_SLOT_CCW_MS = 540
    config.CW_MS_PER_DEGREE = 13.3
    config.CCW_MS_PER_DEGREE = 13.5
    config.LOAD_TO_SCAN_CW_MS = 2400
    config.SCAN_TO_LOAD_CCW_MS = 2450

    servo = mg995.MG995()

    check("geometry is still 45 deg per slot",
          config.CAROUSEL_SLOT_GEOMETRY_DEG == 45.0)
    check("one slot is 45 deg in both directions",
          config.SLOT_STEP_DEG == 45.0)
    check("slot step matches the physical geometry",
          config.SLOT_STEP_DEG == config.CAROUSEL_SLOT_GEOMETRY_DEG)
    check("driver reports 45 deg for both directions",
          servo.slot_step_deg("cw") == 45.0
          and servo.slot_step_deg("ccw") == 45.0)

    check("next-slot timing is its own constant",
          servo.next_slot_ms("cw") == 533
          and servo.next_slot_ms("ccw") == 540)
    check("next-slot timing is NOT derived from 45 deg",
          servo.next_slot_ms("cw")
          != config.CAROUSEL_SLOT_GEOMETRY_DEG * servo.ms_per_degree("cw"))

    check("half turn has independent timing",
          servo.half_turn_ms("cw") == 2400
          and servo.half_turn_ms("ccw") == 2450)
    check("half turn is NOT four next-slot moves",
          servo.half_turn_ms("cw") != 4 * servo.next_slot_ms("cw"),
          (servo.half_turn_ms("cw"), 4 * servo.next_slot_ms("cw")))
    check("timing still differs per direction",
          servo.next_slot_ms("cw") != servo.next_slot_ms("ccw"))

    check("fine adjust uses its own ms/degree",
          servo.ms_per_degree("cw") == 13.3
          and servo.ms_per_degree("ccw") == 13.5)

    SERVO_LOG.clear()
    result = servo.rotate_degrees(2.0)
    check("2 deg CW -> 27 ms", result["duration_ms"] == 27, result)
    check("2 deg is clockwise", result["direction"] == "cw")

    result = servo.rotate_degrees(-5.0)
    check("-5 deg CCW -> 68 ms", result["duration_ms"] == 68, result)
    check("-5 deg is counter-clockwise", result["direction"] == "ccw")

    result = servo.rotate_degrees(0)
    check("zero degrees moves nothing", result["moved"] is False)

    result = servo.rotate_half_turn("cw")
    check("half turn runs the dedicated duration",
          result["duration_ms"] == 2400, result)
    check("half turn reports 180 degrees", result["degrees"] == 180.0)
    check("half turn flags independent calibration",
          result["independent_calibration"] is True)

    SERVO_LOG.clear()
    servo.rotate_slots("cw", 1)
    check("one slot move used the CW pulse",
          any(e[1] == config.SERVO_CW_US for e in SERVO_LOG), SERVO_LOG[:4])

    (config.NEXT_SLOT_CW_MS, config.NEXT_SLOT_CCW_MS,
     config.CW_MS_PER_DEGREE, config.CCW_MS_PER_DEGREE,
     config.LOAD_TO_SCAN_CW_MS, config.SCAN_TO_LOAD_CCW_MS) = saved

    # ------------------------------------------------------------------
    print("\n[29] full competition sequence (Tests C, D, E)")
    # ------------------------------------------------------------------
    sci6 = main.ScienceModule()
    sci6.boot()
    sci6.driver = driver
    sci6.sensor = SoilMeasurementSystem(driver)
    sci6.sensor_error = None
    sci6.install_references()

    sci6.dispatch_command({"cmd": "sync_position", "load_slot": 1})
    r = sci6.dispatch_command({"cmd": "select_slot", "slot": 2})
    check("selected Slot 2", r["data"]["carousel"]["selected_slot"] == 2)

    # Test C - prepare, no sensor distance asked for
    r = sci6.dispatch_command({
        "cmd": "prepare_load", "slot": 2, "sample_id": "SEQ1",
        "metadata": {"task": "survey"},
    })
    check("prepare SEQ1 ok", r["ok"], r.get("error"))
    check("Slot 2 READY_TO_LOAD",
          sci6.carousel.slots[2]["state"] == "READY_TO_LOAD")

    # Test D
    r = sci6.dispatch_command({"cmd": "confirm_loaded", "slot": 2})
    check("Slot 2 LOADED", sci6.carousel.slots[2]["state"] == "LOADED")
    check("Slot 2 is at the loader while being filled",
          sci6.carousel.get_load_slot() == 2)

    # Test E - measuring must swing the same physical slot 180 deg
    scan_before = sci6.carousel.current_scan_slot
    r = sci6.dispatch_command({"cmd": "measure_sample", "slot": 2})
    check("measure SEQ1 ok", r["ok"], r.get("error"))

    move = r["data"]["move"]
    check("measurement used a 180 degree half turn",
          move.get("degrees") == 180.0, move)
    check("half turn used its dedicated calibration, not 4 slot moves",
          move.get("duration_ms") == config.LOAD_TO_SCAN_CW_MS
          and move.get("duration_ms") != 4 * config.NEXT_SLOT_CW_MS, move)
    check("scanner was Slot 6 before the swing", scan_before == 6)
    check("selected slot is still the measured slot",
          sci6.carousel.selected_slot == 2)
    check("Slot 2 MEASURED and occupied",
          sci6.carousel.slots[2]["state"] == "MEASURED"
          and sci6.carousel.slots[2]["occupied"] is True)

    # Measurement stops at the scanner; it does not hide a return move.
    check("Slot 2 is now under the scanner",
          sci6.carousel.current_scan_slot == 2)
    check("Slot 6 is now under the loader",
          sci6.carousel.get_load_slot() == 6)
    check("phase reported as SCAN", sci6.carousel.phase() == "SCAN")
    check("status reports SCAN phase",
          r["data"]["carousel"]["carousel_phase"] == "SCAN")
    check("state transition reported",
          r["data"]["previous_state"] == "LOADED"
          and r["data"]["new_state"] == "MEASURED")

    stage_names = [s["stage"] for s in r["data"]["stages"]]
    check("no hidden return stage",
          "RETURN_TO_LOAD" not in stage_names, stage_names)
    check("all nine stages succeeded",
          all(s["ok"] for s in r["data"]["stages"]))

    # Measurement completes the SAME record opened at prepare_load.
    check("exactly one record for this sample",
          len([x for x in sci6.store.summaries()
               if x["sample_id"] == "SEQ1"]) == 1)
    check("record state is MEASURED",
          sci6.store.get_state("SEQ1") == "MEASURED")
    check("no derived sample id was invented",
          not any(x["sample_id"].startswith("SEQ1_")
                  or x["sample_id"] == "SEQ1-2"
                  for x in sci6.store.summaries()))

    rec = sci6.store.get_sample("SEQ1")
    check("sensor distance stored as null",
          rec["metadata"]["sensor_distance_mm"] is None)
    check("optional metadata left null when not supplied",
          rec["metadata"]["location"] is None
          and rec["metadata"]["note"] is None)
    check("supplied metadata kept",
          rec["metadata"]["task"] == "survey")

    # Choose Slot restores the loading orientation automatically.
    check("phase is SCAN before choosing the next slot",
          sci6.carousel.phase() == "SCAN")

    r = sci6.dispatch_command({"cmd": "select_slot", "slot": 3})
    check("Slot 3 selectable after measurement", r["ok"], r.get("error"))

    move = r["data"]["move"]
    check("load orientation restored automatically",
          move["restored_load_orientation"] is True, move)
    check("restore used the dedicated half turn",
          (move.get("restore") or {}).get("duration_ms")
          == config.SCAN_TO_LOAD_CCW_MS, move.get("restore"))
    check("then one clockwise slot transition",
          move["steps"] == 1 and move["direction"] == "cw", move)
    check("Slot 3 arrives at the loader",
          sci6.carousel.get_load_slot() == 3)
    check("phase back to LOAD", sci6.carousel.phase() == "LOAD")
    check("operator never ran the 180 deg return by hand",
          config.AUTO_RESTORE_LOAD_ON_SELECT is True)

    # ------------------------------------------------------------------
    print("\n[30] strict Measure eligibility")
    # ------------------------------------------------------------------
    sci7 = main.ScienceModule()
    sci7.boot()
    sci7.driver = driver
    sci7.sensor = SoilMeasurementSystem(driver)
    sci7.sensor_error = None
    sci7.install_references()
    sci7.dispatch_command({"cmd": "sync_position", "load_slot": 1})

    # Slot 1 LOADED, then move the selection to Slot 2.
    sci7.dispatch_command({"cmd": "prepare_load", "slot": 1,
                           "sample_id": "ELG1"})
    sci7.dispatch_command({"cmd": "confirm_loaded", "slot": 1})
    sci7.dispatch_command({"cmd": "select_slot", "slot": 2})

    r = sci7.dispatch_command({"cmd": "measure_sample", "slot": 1})
    check("LOADED but unselected slot is refused",
          not r["ok"] and r["error"]["code"] == "SLOT_NOT_SELECTED", r)
    check("refusal names the selected slot",
          r["data"]["selected_slot"] == 2)
    check("refusal stops at VALIDATE_SLOT",
          r["data"]["failed_stage"] == "VALIDATE_SLOT")
    check("nothing moved for the refused measurement",
          sci7.carousel.get_load_slot() == 2)

    # Selecting it back makes it eligible again.
    sci7.dispatch_command({"cmd": "select_slot", "slot": 1})
    check("Slot 1 back at the loader",
          sci7.carousel.get_load_slot() == 1
          and sci7.carousel.phase() == "LOAD")

    r = sci7.dispatch_command({"cmd": "measure_sample", "slot": 1})
    check("selected LOADED slot at the loader measures fine",
          r["ok"], r.get("error"))

    # After measuring it sits at SCAN, so a second attempt is refused.
    r = sci7.dispatch_command({"cmd": "measure_sample", "slot": 1})
    check("re-measure refused once already MEASURED",
          not r["ok"] and r["error"]["code"] == "SLOT_ALREADY_MEASURED", r)

    # A READY_TO_LOAD slot is refused for the right reason.
    sci7.dispatch_command({"cmd": "select_slot", "slot": 4})
    sci7.dispatch_command({"cmd": "prepare_load", "slot": 4,
                           "sample_id": "ELG2"})
    r = sci7.dispatch_command({"cmd": "measure_sample", "slot": 4})
    check("READY_TO_LOAD refused as not loaded",
          not r["ok"] and r["error"]["code"] == "SLOT_NOT_LOADED", r)

    # ------------------------------------------------------------------
    print("\n[31] asymmetric direction calibration in use")
    # ------------------------------------------------------------------
    sci8 = main.ScienceModule()
    sci8.boot()
    sci8.dispatch_command({"cmd": "sync_position", "load_slot": 1})

    SERVO_LOG.clear()
    r = sci8.dispatch_command({"cmd": "select_slot", "slot": 2})
    check("Slot 1 -> Slot 2 is clockwise",
          r["data"]["move"]["direction"] == "cw")
    cw_deg = r["data"]["carousel"]["movement_calibration"]["slot_step_deg"]
    check("clockwise commands 45 deg", cw_deg == 45.0)

    r = sci8.dispatch_command({"cmd": "select_slot", "slot": 1})
    check("Slot 2 -> Slot 1 is counter-clockwise",
          r["data"]["move"]["direction"] == "ccw", r["data"]["move"])
    ccw_deg = r["data"]["carousel"]["movement_calibration"]["slot_step_deg"]
    check("counter-clockwise also commands 45 deg", ccw_deg == 45.0)
    check("both directions request the same angle", cw_deg == ccw_deg)
    check("but keep independent timing",
          config.NEXT_SLOT_CW_MS != config.NEXT_SLOT_CCW_MS)

    check("selection returned to Slot 1",
          sci8.carousel.selected_slot == 1
          and sci8.carousel.get_load_slot() == 1)

    # choosing the already-selected slot must not move anything
    r = sci8.dispatch_command({"cmd": "select_slot", "slot": 1})
    check("re-choosing the same slot moves nothing",
          r["data"]["move"]["moved"] is False, r["data"]["move"])
    check("and says where it physically is",
          "already selected" in r["data"]["move"]["message"])

    # ------------------------------------------------------------------
    print("\n[32] partial-write transport")
    # ------------------------------------------------------------------
    class StingyConsole:
        """USB console that accepts only a few bytes per write()."""

        def __init__(self, limit=7):
            self.limit = limit
            self.out = []
            self.calls = 0

        def write(self, text):
            self.calls += 1
            taken = text[:self.limit]
            self.out.append(taken)

            return len(taken)

        def flush(self):
            pass

        def value(self):
            return "".join(self.out)

    class SysStub:
        def __init__(self, console):
            self.stdin = console
            self.stdout = console

    real_sys = main.sys
    console = StingyConsole(limit=7)
    main.sys = SysStub(console)

    try:
        big = {
            "request_id": "1",
            "ok": True,
            "data": {
                "spectrum": dict((c, 1.234567) for c in analysis.CHANNELS),
                "text": "x" * 500,
            },
        }
        main.send_json(big)

    finally:
        main.sys = real_sys

    written = console.value()
    check("short writes are retried until everything is out",
          written.endswith("\n"), repr(written[-20:]))
    check("payload survived the partial writes intact",
          json.loads(written.strip()) == big)
    check("it really took many write() calls", console.calls > 20,
          console.calls)

    console = StingyConsole(limit=5)
    main.sys = SysStub(console)

    try:
        main.send_json({"request_id": "2", "ok": True,
                        "data": {"note": "Проба"}})
    finally:
        main.sys = real_sys

    written = console.value()
    check("non-ASCII escaped to pure ASCII",
          all(ord(ch) < 128 for ch in written), repr(written[:60]))
    check("escaped payload still decodes to the original text",
          json.loads(written.strip())["data"]["note"]
          == "Проба")

    # ------------------------------------------------------------------
    print("\n[33] sensor test saves nothing")
    # ------------------------------------------------------------------
    before_count = sci.store.count()
    before_slots = [dict(x) for x in sci.carousel.slot_list()]
    before_scan = sci.carousel.current_scan_slot
    before_selected = sci.carousel.selected_slot
    reads_before = driver.reads

    SERVO_LOG.clear()
    r = sci.dispatch_command({"cmd": "test_measurement"})

    check("test_measurement ok", r["ok"], r.get("error"))
    d = r["data"]
    check("flagged as test only",
          d["test_only"] is True and d["saved"] is False)
    check("took a NEW acquisition", driver.reads == reads_before + 1)
    check("18 raw channels", len(d["raw"]) == 18)
    check("18 dark-corrected channels", len(d["dark_corrected"]) == 18)
    check("18 normalized channels", len(d["normalized"]) == 18)
    check("compared against every material",
          len(d["reference_matches"]) == 22)
    check("analysis produced", bool(d["analysis"]["automatic_conclusion"]))
    check("uses the fixed references",
          d["calibration"]["mode"] == "FIXED_STORED_REFERENCES")

    check("NOTHING was saved", sci.store.count() == before_count)
    check("no slot state changed",
          [dict(x) for x in sci.carousel.slot_list()] == before_slots)
    check("carousel did not move",
          sci.carousel.current_scan_slot == before_scan
          and sci.carousel.selected_slot == before_selected)
    check("servo was never driven", SERVO_LOG == [], SERVO_LOG[:4])

    # ------------------------------------------------------------------
    print("\n[34] three-file separation is enforced")
    # ------------------------------------------------------------------
    #   references.json  fixed white + dark        READ ONLY
    #   database.json    reference material spectra READ ONLY
    #   samples.json     carousel samples we measure  the only writable one
    import hashlib

    def digest(path):
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()

    refs_before = digest("references.json")
    db_before = digest("database.json")

    sci9 = main.ScienceModule()
    sci9.boot()
    sci9.driver = driver
    sci9.sensor = SoilMeasurementSystem(driver)
    sci9.sensor_error = None
    sci9.install_references()

    # A complete run: calibrate, choose, prepare, confirm, measure,
    # plus a sensor test and a slot clear.
    sci9.dispatch_command({"cmd": "sync_position", "load_slot": 1})
    sci9.dispatch_command({"cmd": "select_slot", "slot": 4})
    sci9.dispatch_command({"cmd": "prepare_load", "slot": 4,
                           "sample_id": "SEP1"})
    sci9.dispatch_command({"cmd": "confirm_loaded", "slot": 4})
    r = sci9.dispatch_command({"cmd": "measure_sample", "slot": 4})
    check("full run measured successfully", r["ok"], r.get("error"))

    sci9.dispatch_command({"cmd": "test_measurement"})
    sci9.dispatch_command({"cmd": "clear_slot", "slot": 4})
    sci9.dispatch_command({"cmd": "update_sample_metadata",
                           "sample_id": "SEP1",
                           "metadata": {"note": "after the fact"}})

    check("references.json byte-identical after a full run",
          digest("references.json") == refs_before)
    check("database.json byte-identical after a full run",
          digest("database.json") == db_before)

    check("samples.json IS written", os.path.exists("samples.json"))
    check("the sample landed in samples.json",
          sci9.store.has_sample("SEP1"))

    stored = json.load(open("samples.json"))
    ids = [x["sample_id"] for x in stored["samples"]]
    check("samples.json holds carousel samples only",
          "SEP1" in ids, ids)

    # No measured soil sample may ever appear in the material database.
    materials = json.load(open("database.json"))
    check("no carousel sample leaked into database.json",
          not any(i in materials for i in ids), ids)
    check("database.json still holds exactly the 22 reference materials",
          len(materials) == 22, len(materials))

    # And the reference file still holds only white + dark.
    refs = json.load(open("references.json"))
    check("references.json still holds only white and dark",
          sorted(refs.keys()) == ["dark", "white"], sorted(refs.keys()))

    # Static guard: nothing may open the read-only files for writing.
    fw_sources = {}

    for name in os.listdir(FIRMWARE):
        if name.endswith(".py"):
            fw_sources[name] = open(
                os.path.join(FIRMWARE, name), encoding="utf-8"
            ).read()

    writers = []

    for name, text in fw_sources.items():
        if name == "wipe.py":
            continue

        for line in text.split("\n"):
            if "open(" not in line or '"w"' not in line.replace("'", '"'):
                continue

            if "REFERENCES_FILE" in line or "DATABASE_FILE" in line:
                writers.append("{}: {}".format(name, line.strip()))

    check("no firmware line opens references/database for writing",
          not writers, writers)

    check("MaterialDatabase.add_material is never called",
          not any("add_material(" in t
                  for n, t in fw_sources.items() if n != "database.py"))

    # ------------------------------------------------------------------
    print("\n[35] sensor diagnostics")
    # ------------------------------------------------------------------
    # A module whose sensor never initialized - the real hardware case.
    cold = main.ScienceModule()
    cold.boot()

    check("cold module has no sensor", cold.sensor is None)
    check("boot error was preserved, not thrown away",
          cold.sensor_error is not None, cold.sensor_error)

    r = cold.dispatch_command({"cmd": "sensor_diagnostics"})
    check("diagnostics answer OK at the protocol level", r["ok"],
          r.get("error"))

    d = r["data"]
    stages = {s["stage"]: s for s in d["diagnostics"]}

    check("command routing reported PASS",
          stages["COMMAND_ROUTING"]["status"] == "PASS")
    check("sensor object reported FAIL",
          stages["SENSOR_OBJECT"]["status"] == "FAIL")
    check("boot error surfaced in the report",
          stages["SENSOR_OBJECT"]["error"]["details"]["boot_error"]
          == cold.sensor_error)
    check("I2C scan ran even with no sensor object",
          "I2C_SCAN" in stages)
    check("dependent stages marked SKIPPED, not failed",
          stages["CHANNEL_ACQUISITION"]["status"] == "SKIPPED")
    check("skip explains the prerequisite",
          "I2C" in stages["CHANNEL_ACQUISITION"]["details"]["reason"])
    check("independent stages still ran",
          stages["REFERENCES"]["status"] == "PASS")
    check("overall verdict is FAIL", d["overall"] == "FAIL")
    check("first failure is named",
          d["first_failure"]["stage"] == "SENSOR_OBJECT",
          d["first_failure"])
    check("every stage carries a status",
          all("status" in s for s in d["diagnostics"]))
    check("every stage carries a duration",
          all("duration_ms" in s for s in d["diagnostics"]))
    check("diagnostics never save", d["saved"] is False)

    # Diagnostics must not require carousel synchronization.
    check("diagnostics ran on an unsynchronized carousel",
          cold.carousel.position_valid is False)

    r = cold.dispatch_command({"cmd": "i2c_scan"})
    check("i2c_scan answers even with no sensor", r["ok"], r.get("error"))
    check("i2c_scan reports the configured pins",
          r["data"]["details"]["sda"] == "GPIO21"
          and r["data"]["details"]["scl"] == "GPIO22")
    check("i2c_scan names the expected address",
          r["data"]["details"]["expected_address"] == "0x49")
    check("i2c_scan gives an explicit verdict",
          r["data"]["status"] in ("PASS", "FAIL"))

    r = cold.dispatch_command({"cmd": "raw_measurement"})
    check("raw_measurement refuses clearly without a sensor",
          not r["ok"] and r["error"]["code"] == "SENSOR_NOT_INITIALIZED",
          r.get("error"))

    # ------------------------------------------------------------------
    print("\n[36] one shared acquisition path")
    # ------------------------------------------------------------------
    reads_before = driver.reads
    science = sci.acquire_science_measurement()

    check("shared core takes a NEW acquisition",
          driver.reads == reads_before + 1)
    check("shared core returns 18 raw channels",
          len(analysis.validate_channels(science["raw"])) == 0)
    check("shared core normalized the spectrum",
          len(science["normalized"]) == 18)
    check("shared core compared every material",
          len(science["matches"]) == 22)
    check("shared core produced an interpretation",
          bool(science["interpretation"]["automatic_conclusion"]))

    src = open(os.path.join(FIRMWARE, "main.py")).read()
    check("only ONE take_sample() call site outside the shared core",
          src.count("self.sensor.take_sample()") == 2,
          src.count("self.sensor.take_sample()"))
    check("measure_sample calls the shared core",
          "self.acquire_science_measurement()" in src)

    # raw_measurement is the deliberate second call site: no references,
    # no database, so it can prove the driver alone works.
    check("raw_measurement is documented as the isolation path",
          "no references, no database" in src.lower()
          or "No references, no database" in src)

    # ------------------------------------------------------------------
    print("\n[37] no swallowed sensor exceptions")
    # ------------------------------------------------------------------
    import re as _re

    offenders = []

    # Only the sensor path matters here. sample_store's _remove_quiet()
    # is a deliberate best-effort unlink of a temp file that may not
    # exist - correct, and unrelated to sensing.
    for name in ("as7265x.py", "sensor_diag.py", "main.py"):
        text = open(os.path.join(FIRMWARE, name)).read()

        for match in _re.finditer(
            r"except[^\n]*:\s*\n\s+(pass|return None|return)\s*\n", text
        ):
            snippet = match.group(0).strip().replace("\n", " ")
            before = text[max(0, match.start() - 300):match.start()].lower()

            # LED shutdown and stdout flush in finally blocks are
            # documented best-effort cleanups, not hidden failures.
            if "led" in before or "flush" in before:
                continue

            offenders.append("{}: {}".format(name, snippet))

    check("no swallowed exceptions in the sensor path",
          not offenders, offenders)

    strict_src = open(os.path.join(FIRMWARE, "as7265x.py")).read()
    check("strict wrapper raises instead of returning 0",
          "class StrictAS7265X" in strict_src
          and "VIRTUAL_RX_TIMEOUT" in strict_src)
    check("every strict wait loop is bounded",
          strict_src.count("timeout_ms") >= 4)

    # ------------------------------------------------------------------
    print("\n[19] response sizes")
    # ------------------------------------------------------------------
    full = json.dumps(sci.store.get_sample("S001"))
    print("      full record: {} bytes".format(len(full)))
    check("full record fits comfortably on the link", len(full) < 8000,
          len(full))

    shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    run()

    print("\n" + "=" * 60)
    if FAILURES:
        print("{} of {} checks FAILED:".format(len(FAILURES), CHECKS[0]))
        for name in FAILURES:
            print("  - {}".format(name))
        sys.exit(1)

    print("all {} checks passed".format(CHECKS[0]))
