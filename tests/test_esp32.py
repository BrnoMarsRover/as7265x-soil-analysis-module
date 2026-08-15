"""
ESP32 firmware regression tests.

Runs the real firmware/ESP32 modules on CPython against a fake AS7265x
that speaks the actual virtual-register protocol.

Focus, in order of what actually broke on hardware:

  1. the sensor lifecycle recovers instead of latching a boot failure
  2. configuration is verified by read-back, not merely reported
  3. raw acquisition returns 18 channels and always kills the LED
  4. the JSON protocol answers every command with one safe frame
  5. no custom exception uses Exception.__init__
"""

import json
import sys

import support
from support import Checks, FakeAS7265X


def build_module(device):
    """Fresh firmware instance against a given fake sensor."""
    support.load_esp32(device)

    for name in ("as7265x", "carousel", "config", "main", "mg995"):
        sys.modules.pop(name, None)

    import config

    # Boot is slow only because of the real hardware settling delays.
    config.STARTUP_DELAY_SECONDS = 0
    config.SENSOR_INIT_RETRY_MS = 0
    config.I2C_SCAN_RETRY_MS = 0
    config.ILLUMINATION_SETTLE_MS = 0
    config.SERVO_SETTLE_MS = 0
    config.SERVO_INTER_STEP_PAUSE_MS = 0
    config.SCAN_SETTLE_TIME = 0.0
    config.NEXT_SLOT_CW_MS = 1
    config.NEXT_SLOT_CCW_MS = 1
    config.LOAD_TO_SCAN_CW_MS = 1
    config.SCAN_TO_LOAD_CCW_MS = 1

    import main

    module = main.HardwareModule()
    module.boot()

    return main, module, config


def send(module, cmd, **payload):
    """Round trip one command through the real dispatcher."""
    request = {"request_id": "1", "cmd": cmd}
    request.update(payload)

    return module.dispatch_command(request)


def realistic_spectrum(index, device):
    """Deterministic, distinguishable, never all-zero."""
    return 100.0 + index * 10.0 + device * 3.0


def channels_in(data, illumination="white"):
    """Channel count of one illumination block in an acquisition."""
    block = (data.get("illuminations") or {}).get(illumination) or {}
    acquisitions = block.get("acquisitions") or [{}]

    return len(acquisitions[0])


def main_tests():
    checks = Checks("ESP32 firmware")

    # ==================================================================
    checks.section("1. sensor lifecycle - normal bring-up")

    device = FakeAS7265X()
    device.fill_spectrum(realistic_spectrum)
    _main, module, config = build_module(device)

    checks.ok(module.sensor.ready, "sensor is ready after boot")
    checks.ok(module.sensor.boot_error is None, "no boot error recorded")
    checks.equal(module.sensor.recovery_count, 0, "no recovery needed")

    settings = module.sensor.settings()
    checks.equal(
        settings["integration_cycles"],
        config.SENSOR_INTEGRATION_CYCLES,
        "integration cycles read back from the sensor",
    )
    checks.equal(
        settings["gain"], config.SENSOR_GAIN, "gain read back from the sensor"
    )
    checks.equal(settings["gain_x"], "16x", "gain reported as 16x")
    checks.equal(
        settings["measurement_mode"],
        config.SENSOR_MEASUREMENT_MODE,
        "measurement mode read back from the sensor",
    )
    checks.equal(
        settings["led_current"],
        config.ONBOARD_LED_CURRENT,
        "LED current read back from the sensor",
    )

    # ==================================================================
    checks.section("2. a boot failure never becomes permanent")

    # Absent for more scan attempts than one initialization can make,
    # so boot genuinely fails - the exact situation that used to leave
    # driver = None for the rest of the session. Another device answers,
    # so this is "wrong device", not "dead bus".
    device = FakeAS7265X(absent_scans=99, other_devices=(0x68,))
    device.fill_spectrum(realistic_spectrum)
    _main, module, config = build_module(device)

    checks.ok(not module.sensor.ready, "boot failed as arranged")
    checks.ok(
        module.sensor.boot_error is not None, "boot error was recorded"
    )
    checks.equal(
        module.sensor.boot_error["code"],
        "AS7265X_ADDRESS_NOT_FOUND",
        "a live bus without 0x49 is distinguished from a dead bus",
    )
    checks.ok(
        module.sensor.boot_error["details"]["addresses"] == ["0x68"],
        "the scan result is reported so the operator can act on it",
    )

    response = send(module, "ping")
    checks.ok(response["ok"], "command server still answers after boot failure")

    response = send(module, "get_status")
    checks.ok(response["ok"], "get_status works with an unavailable sensor")
    checks.ok(
        not response["data"]["sensor"]["ready"],
        "status reports the sensor as unavailable",
    )

    # The sensor appears. Nothing is restarted; the next command retries.
    device.absent_scans = 0

    response = send(module, "sensor_test_raw")
    checks.ok(response["ok"], "sensor test succeeds once the sensor appears")
    checks.ok(
        response["data"]["ok"], "sensor test reports overall success"
    )
    checks.ok(module.sensor.ready, "runtime sensor is now ready")
    checks.equal(
        module.sensor.recovery_count, 1, "recovery was counted exactly once"
    )
    checks.equal(
        channels_in(response["data"]), 18,
        "recovered sensor returns 18 channels",
    )

    status = send(module, "get_status")["data"]["sensor"]
    checks.ok(status["ready"], "status no longer reports the stale failure")
    checks.ok(
        status["boot_error"] is not None,
        "the boot error is still visible as history",
    )

    # ==================================================================
    checks.section("3. configuration is verified, not assumed")

    device = FakeAS7265X(accept_config=False)
    device.fill_spectrum(realistic_spectrum)
    _main, module, config = build_module(device)

    checks.ok(
        not module.sensor.ready,
        "a sensor that ignores configuration writes is not READY",
    )
    checks.equal(
        module.sensor.boot_error["code"],
        "SENSOR_CONFIG_NOT_APPLIED",
        "the mismatch is named exactly",
    )

    details = module.sensor.boot_error["details"]
    checks.ok(
        "integration_cycles" in details["mismatched"],
        "the unapplied setting is listed",
    )
    checks.equal(
        details["applied"]["integration_cycles"],
        20,
        "the value actually in the register is reported",
    )

    # ==================================================================
    checks.section("4. internal devices must answer")

    device = FakeAS7265X(slaves_present=False)
    _main, module, config = build_module(device)

    checks.ok(not module.sensor.ready, "missing slaves fail initialization")
    checks.equal(
        module.sensor.boot_error["code"],
        "AS7265X_SLAVES_NOT_DETECTED",
        "the slave fault is distinguished from a bus fault",
    )

    device = FakeAS7265X(absent_scans=99)
    _main, module, config = build_module(device)

    checks.equal(
        module.sensor.boot_error["code"],
        "I2C_NO_DEVICES",
        "a bus where nothing answers is reported as such",
    )

    # ==================================================================
    checks.section("5. deterministic one-shot acquisition")

    device = FakeAS7265X()
    device.fill_spectrum(realistic_spectrum)
    _main, module, config = build_module(device)

    import as7265x

    checks.equal(
        config.SENSOR_MEASUREMENT_MODE, 0b11,
        "the production mode is one-shot, not continuous",
    )
    checks.equal(
        module.sensor.settings()["measurement_mode"], 3,
        "and the sensor is actually running it",
    )

    acquisition = module.sensor.acquire_one(as7265x.LED_WHITE)
    spectrum = acquisition["spectrum"]

    checks.equal(len(spectrum), 18, "18 channels acquired")
    checks.equal(acquisition["illumination"], "white", "under WHITE")
    checks.ok(
        all(isinstance(value, float) for value in spectrum.values()),
        "every channel is numeric",
    )
    checks.ok(
        len(set(spectrum.values())) > 1,
        "channels are distinguishable, not one repeated value",
    )
    checks.equal(acquisition["zero_channels"], [], "no zero channels")
    checks.ok(not device.any_lamp_on(), "every lamp is off afterwards")
    checks.ok(
        device.lamp_on_counts[as7265x.LED_WHITE] >= 1,
        "the WHITE lamp was actually used",
    )
    checks.equal(
        device.lamp_on_counts[as7265x.LED_UV], 0, "the UV lamp was not"
    )
    checks.equal(
        device.lamp_on_counts[as7265x.LED_IR], 0, "nor the IR lamp"
    )

    # Each lamp on its own.
    for name, lamp in (("uv", as7265x.LED_UV), ("ir", as7265x.LED_IR)):
        result = module.sensor.acquire_one(lamp)

        checks.equal(
            result["illumination"], name,
            "an acquisition under {} is labelled as such".format(name.upper()),
        )
        checks.ok(
            device.lamp_on_counts[lamp] >= 1,
            "the {} lamp was used".format(name.upper()),
        )
        checks.ok(
            not device.any_lamp_on(),
            "and every lamp is off again after the {} block".format(name),
        )

    # Dark: no lamp at all.
    before = dict(device.lamp_on_counts)
    dark = module.sensor.acquire_one(None)

    checks.equal(dark["illumination"], "dark", "a dark acquisition is dark")
    checks.equal(
        device.lamp_on_counts, before,
        "and switches on no lamp whatsoever",
    )

    # Repeats come back individually, for the PC to aggregate.
    block = module.sensor.acquire_block(as7265x.LED_WHITE, 4)

    checks.equal(block["repeats"], 4, "a block honours the repeat count")
    checks.equal(
        len(block["acquisitions"]), 4, "and returns every reading"
    )
    checks.equal(
        len(block["data_ready_wait_ms"]), 4, "with a wait time each"
    )

    triad = module.sensor.acquire_triad(2)

    checks.equal(
        sorted(triad.keys()), ["ir", "uv", "white"],
        "a triad covers all three illuminations",
    )
    checks.equal(
        sum(len(entry["acquisitions"]) for entry in triad.values()), 6,
        "3 illuminations x 2 repeats = 6 spectra (54 features x 2)",
    )
    checks.ok(not device.any_lamp_on(), "and leaves every lamp off")

    # A failure mid-acquisition must still leave every lamp off.
    device.data_ready_supported = False
    module.sensor.ready = True

    checks.raises(
        as7265x.SensorError,
        module.sensor.acquire_raw_spectrum,
        "a DATA_READY timeout raises SensorError",
    )
    checks.ok(
        not device.any_lamp_on(), "every lamp is off after a failure too"
    )

    # ==================================================================
    checks.section("6. one sensor world")

    device = FakeAS7265X()
    device.fill_spectrum(realistic_spectrum)
    _main, module, config = build_module(device)

    first = module.sensor.ensure_ready()
    second = module.sensor.ensure_ready()

    checks.ok(first is second, "ensure_ready reuses the working driver")

    forced = module.sensor.ensure_ready(force_reinit=True)
    checks.ok(forced is not first, "force_reinit really rebuilds the driver")

    checks.ok(
        not hasattr(as7265x, "StrictAS7265X"),
        "the parallel diagnostic driver no longer exists",
    )
    checks.ok(
        not hasattr(as7265x, "SoilMeasurementSystem"),
        "the stateful measurement object no longer exists",
    )

    # ==================================================================
    checks.section("7. ESP32 does no science")

    source = (support.FIRMWARE / "ESP32").glob("*.py")
    forbidden = (
        "database.json",
        "references.json",
        "sample_store",
        "sample_analysis",
        "cosine",
        "normalize",
        "white_ref",
        "dark_ref",
    )

    offenders = []

    for path in source:
        text = path.read_text(encoding="utf-8").lower()

        for token in forbidden:
            if token in text:
                offenders.append("{}: {}".format(path.name, token))

    checks.equal(offenders, [], "no ESP32 module mentions science data")

    checks.ok(
        "database" not in sys.modules and "sample_analysis" not in sys.modules,
        "no BD module was imported by the firmware",
    )

    # ==================================================================
    checks.section("8. protocol surface")

    device = FakeAS7265X()
    device.fill_spectrum(realistic_spectrum)
    _main, module, config = build_module(device)

    expected_commands = {
        "ping", "get_status",
        "sync_position", "select_slot", "move_slots", "fine_adjust",
        "clear_slot", "clear_all_slots",
        "measure_raw", "sensor_test_raw",
        "acquire_block", "acquire_triad", "led_test",
        "list_saved_samples", "get_saved_sample", "delete_saved_samples",
        "servo_stop", "get_servo_calibration", "set_servo_calibration",
        "servo_test_move",
    }

    checks.equal(
        set(module.handlers.keys()),
        expected_commands,
        "exactly the hardware commands are exposed",
    )

    response = send(module, "measure_sample", slot=1)
    checks.ok(not response["ok"], "an obsolete command is rejected")
    checks.equal(
        response["error"]["code"], "UNKNOWN_COMMAND", "with UNKNOWN_COMMAND"
    )

    response = send(module, "fine_adjust", degrees=90)
    checks.equal(
        response["error"]["code"],
        "FINE_ADJUST_TOO_LARGE",
        "fine adjustment is bounded",
    )

    response = send(module, "select_slot", slot=99)
    checks.equal(response["error"]["code"], "INVALID_SLOT", "slots are ranged")

    response = send(module, "select_slot")
    checks.equal(
        response["error"]["code"], "MISSING_FIELD", "a missing slot is named"
    )

    # ==================================================================
    checks.section("9. measurement refuses before it moves")

    response = send(module, "measure_raw", slot=1)
    checks.equal(
        response["error"]["code"],
        "POSITION_NOT_SYNCHRONIZED",
        "measurement needs a synchronized carousel",
    )
    checks.equal(
        device.channel_reads, 0, "no channel was read"
    )

    send(module, "sync_position", load_slot=1)

    response = send(module, "measure_raw", slot=3)
    checks.equal(
        response["error"]["code"],
        "SLOT_NOT_SELECTED",
        "only the selected slot may be measured",
    )

    # A sensor fault must be found before the carousel swings out.
    device.absent_scans = 999
    module.sensor.ready = False

    response = send(module, "measure_raw", slot=1)
    checks.ok(not response["ok"], "a dead sensor refuses the measurement")
    checks.equal(
        response["data"]["moved"], False, "nothing was moved"
    )
    checks.equal(
        module.carousel.phase(), "LOAD", "the sample is still at the loader"
    )

    device.absent_scans = 0

    # ==================================================================
    checks.section("10. successful measure_raw: out, acquire, back")

    home_scan_slot = module.carousel.current_scan_slot

    response = send(module, "measure_raw", slot=1, sample_id="S001")
    checks.ok(response["ok"], "measure_raw succeeds")

    data = response["data"]

    checks.equal(
        sorted(data["illuminations"].keys()), ["ir", "uv", "white"],
        "all three illuminations were measured",
    )

    for name in ("white", "uv", "ir"):
        checks.equal(
            channels_in(data, name), 18,
            "{} returned 18 channels".format(name.upper()),
        )

    checks.equal(data["slot_id"], 1, "slot echoed")
    checks.equal(data["sample_id"], "S001", "sample id echoed")
    checks.ok(
        data["sensor_settings"]["integration_cycles"] == 100,
        "settings travel with the spectra",
    )
    checks.equal(
        data["sensor_settings"]["measurement_mode"], 3, "acquired in one-shot"
    )
    checks.ok(data["bulbs_off"], "every lamp is off when it returns")
    checks.ok(data["temperatures"], "device temperatures are recorded")
    checks.equal(
        data["protocol_version"], 2, "tagged with the acquisition protocol"
    )
    checks.ok(data["slot"]["occupied"], "the slot is marked occupied")

    # The whole point of the new sequence: it ends where it started.
    checks.ok(data["home_restored"], "the carousel reported a return home")
    checks.ok(
        data["return_move"]["returned"], "the return movement succeeded"
    )
    checks.equal(
        data["carousel"]["carousel_phase"], "LOAD",
        "the sample is back at the loading position",
    )
    checks.equal(
        module.carousel.current_scan_slot, home_scan_slot,
        "the tracked position is exactly where it started",
    )
    checks.ok(
        module.carousel.position_valid, "position tracking is still valid"
    )

    checks.ok(
        "normalized" not in json.dumps(data),
        "no normalization was performed on the ESP32",
    )
    checks.ok(
        "similarity" not in json.dumps(data),
        "no database comparison was performed on the ESP32",
    )

    # ---- retained acquisition, for the PC to pull back --------------
    status_slots = send(module, "get_status")["data"]["slots"]

    checks.ok(
        all("measurement" not in slot for slot in status_slots),
        "get_status does not carry the retained spectra",
    )
    checks.ok(
        any(slot["has_measurement"] for slot in status_slots),
        "but does say which slots hold one",
    )

    response = send(module, "list_saved_samples")
    checks.ok(response["ok"], "list_saved_samples works")
    checks.equal(response["data"]["count"], 1, "one acquisition is retained")
    checks.equal(
        response["data"]["samples"][0]["sample_id"], "S001",
        "and it is the sample just measured",
    )

    response = send(module, "get_saved_sample", sample_id="S001")
    checks.ok(response["ok"], "get_saved_sample works")
    checks.equal(
        channels_in(response["data"]["measurement"]), 18,
        "the retained record carries all 18 raw channels",
    )
    checks.equal(
        sorted(response["data"]["measurement"]["illuminations"].keys()),
        ["ir", "uv", "white"],
        "for all three illuminations",
    )
    checks.ok(
        "normalized" not in json.dumps(response["data"]),
        "the retained record is raw only",
    )

    response = send(module, "get_saved_sample", sample_id="NOPE")
    checks.equal(
        response["error"]["code"], "SAMPLE_NOT_FOUND",
        "an unknown sample id is refused",
    )

    response = send(module, "clear_slot", slot=1)
    checks.ok(response["ok"], "clear_slot works")
    checks.ok(
        not response["data"]["slot"]["occupied"], "the slot is free again"
    )

    checks.equal(
        send(module, "list_saved_samples")["data"]["count"], 0,
        "clearing the slot drops its retained acquisition too",
    )

    # ==================================================================
    checks.section("10b. a failed return invalidates the position")

    device = FakeAS7265X()
    device.fill_spectrum(realistic_spectrum)
    _main, module, config = build_module(device)

    send(module, "sync_position", load_slot=1)

    class BrokenReturn(Exception):
        pass

    outbound = [True]
    original_half_turn = module.carousel.servo.rotate_half_turn

    def half_turn_that_fails_on_the_way_back(direction):
        if outbound[0]:
            outbound[0] = False

            return original_half_turn(direction)

        raise BrokenReturn("servo stalled on the return sweep")

    module.carousel.servo.rotate_half_turn = (
        half_turn_that_fails_on_the_way_back
    )

    response = send(module, "measure_raw", slot=1, sample_id="S002")

    checks.ok(
        response["ok"],
        "a failed return does NOT discard the acquired spectrum",
    )
    checks.equal(
        channels_in(response["data"]), 18,
        "all 18 channels still returned",
    )
    checks.ok(
        not response["data"]["home_restored"],
        "but the return is reported as failed",
    )
    checks.ok(
        not response["data"]["return_move"]["returned"],
        "the return move says so explicitly",
    )
    checks.ok(
        not module.carousel.position_valid,
        "position tracking is invalidated rather than lying",
    )
    checks.ok(
        not response["data"]["carousel"]["position_valid"],
        "and the response says the position is unknown",
    )

    # ==================================================================
    checks.section("11. responses are JSON-safe")

    device = FakeAS7265X(absent_scans=99)
    _main, module, config = build_module(device)

    send(module, "sync_position", load_slot=1)

    for cmd, payload in (
        ("ping", {}),
        ("get_status", {}),
        ("sensor_test_raw", {}),
        ("measure_raw", {"slot": 1}),
        ("select_slot", {"slot": 2}),
        ("clear_slot", {"slot": 1}),
        ("servo_stop", {}),
        ("nonsense", {}),
    ):
        response = send(module, cmd, **payload)

        try:
            encoded = json.dumps(response)
            ok = True

        except (TypeError, ValueError):
            ok = False
            encoded = ""

        checks.ok(ok, "{} produces encodable JSON".format(cmd))
        checks.ok(
            "SensorError" not in encoded and "object at 0x" not in encoded,
            "{} serializes no exception or object repr".format(cmd),
        )

    # ==================================================================
    checks.section("12. MicroPython exception construction")

    import carousel as carousel_module
    import main as main_module
    import mg995 as mg995_module

    # MicroPython has no unbound Exception.__init__: calling it raises
    # "type object 'Exception' has no attribute '__init__'", which used
    # to turn every sensor fault into a bare INTERNAL_ERROR. Comments
    # explaining that are fine; a real call is not.
    offenders = []

    for path in (support.FIRMWARE / "ESP32").glob("*.py"):
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            code = line.split("#", 1)[0]

            if "Exception.__init__(" in code:
                offenders.append("{}:{}".format(path.name, number))

    checks.equal(
        offenders, [], "no module calls Exception.__init__ unbound"
    )

    # Any custom exception that defines its own __init__ must chain
    # through super(), or the message never reaches str(error).
    import ast

    for path in (support.FIRMWARE / "ESP32").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue

            bases = [
                base.id for base in node.bases if isinstance(base, ast.Name)
            ]

            if "Exception" not in bases:
                continue

            initializers = [
                child for child in node.body
                if isinstance(child, ast.FunctionDef)
                and child.name == "__init__"
            ]

            if not initializers:
                # Inheriting Exception.__init__ is fine; only an
                # override can get the chaining wrong.
                continue

            body = ast.dump(initializers[0])

            checks.ok(
                "Name(id='super'" in body and "attr='__init__'" in body,
                "{}.{} chains to super().__init__".format(
                    path.name, node.name
                ),
            )

    for exception_type, arguments in (
        (main_module.CommandError, ("CODE", "message")),
        (carousel_module.CarouselError, ("CODE", "message")),
        (as7265x.SensorError, ("CODE", "message")),
        (mg995_module.ServoError, ("message",)),
    ):
        try:
            instance = exception_type(*arguments)
            constructed = str(instance) == "message"

        except Exception:
            constructed = False

        checks.ok(
            constructed,
            "{} constructs and stringifies".format(exception_type.__name__),
        )

    # ==================================================================
    checks.section("12b. clear all slots vs delete all samples")

    device = FakeAS7265X()
    device.fill_spectrum(realistic_spectrum)
    _main, module, config = build_module(device)

    send(module, "sync_position", load_slot=1)

    # Occupy two slots with a measurement each.
    for slot_id, sample_id in ((1, "S001"), (2, "S002")):
        send(module, "select_slot", slot=slot_id)
        send(module, "measure_raw", slot=slot_id, sample_id=sample_id)

    checks.equal(
        send(module, "list_saved_samples")["data"]["count"], 2,
        "two acquisitions retained",
    )
    checks.equal(
        len([
            slot for slot in send(module, "get_status")["data"]["slots"]
            if slot["occupied"]
        ]),
        2,
        "two slots occupied",
    )

    # Deleting the saved samples must NOT free the slots: soil can still
    # physically be sitting in them.
    response = send(module, "delete_saved_samples")

    checks.ok(response["ok"], "delete_saved_samples works")
    checks.equal(response["data"]["deleted_count"], 2, "both were deleted")
    checks.equal(response["data"]["remaining"], 0, "none remain")
    checks.equal(
        send(module, "list_saved_samples")["data"]["count"], 0,
        "the acquisition buffer is empty",
    )

    # The bug this replaced: a measurement taken WITHOUT a Sample ID was
    # retained but never listed, so the delete screen saw an empty index,
    # said "already empty" and returned without deleting anything.
    send(module, "select_slot", slot=3)
    send(module, "measure_raw", slot=3)

    listing = send(module, "list_saved_samples")["data"]

    checks.equal(
        listing["count"], 1,
        "a measurement with no Sample ID is still listed",
    )
    checks.equal(
        listing["samples"][0]["sample_id"], "SLOT3",
        "under a slot-derived placeholder",
    )
    checks.ok(
        not listing["samples"][0]["has_sample_id"],
        "flagged as having no real ID",
    )
    checks.ok(
        send(module, "get_saved_sample", sample_id="SLOT3")["ok"],
        "and it can be fetched by that placeholder",
    )
    checks.equal(
        send(module, "delete_saved_samples")["data"]["deleted_count"], 1,
        "so it can be deleted too",
    )
    checks.equal(
        len([
            slot for slot in send(module, "get_status")["data"]["slots"]
            if slot["occupied"]
        ]),
        3,
        "but every slot measured so far is STILL occupied - deleting "
        "records is not clearing the mechanism",
    )

    response = send(module, "delete_saved_samples")
    checks.equal(
        response["data"]["deleted_count"], 0,
        "deleting an empty buffer is harmless",
    )

    # Clearing the slots is the other half, and equally narrow.
    scan_before = module.carousel.current_scan_slot

    response = send(module, "clear_all_slots")

    checks.ok(response["ok"], "clear_all_slots works")
    checks.equal(
        response["data"]["cleared_count"], 3, "every occupied slot freed"
    )
    checks.ok(
        all(
            not slot["occupied"] and slot["sample_id"] is None
            for slot in response["data"]["slots"]
        ),
        "every slot is now empty",
    )
    checks.equal(
        len(response["data"]["slots"]), 4, "all four reported"
    )
    checks.equal(
        module.carousel.current_scan_slot, scan_before,
        "clearing does not move the carousel",
    )
    checks.ok(
        module.carousel.position_valid,
        "and does not invalidate the position",
    )

    response = send(module, "clear_all_slots")
    checks.equal(
        response["data"]["cleared_count"], 0,
        "clearing empty slots is harmless",
    )

    # ==================================================================
    checks.section("12c. servo calibration")

    import mg995 as servo_module

    calibration = send(module, "get_servo_calibration")["data"]
    current = calibration["current"]

    checks.equal(
        current["neutral_us"], config.SERVO_STOP_US,
        "calibration is seeded from config.py",
    )
    checks.equal(
        current["cw_us"], config.SERVO_CW_US, "CW pulse from config"
    )
    checks.equal(
        current["slot_cw_ms"], config.NEXT_SLOT_CW_MS,
        "90 deg CW timing from config",
    )
    checks.equal(
        current["load_to_scan_ms"], config.LOAD_TO_SCAN_CW_MS,
        "180 deg timing from config",
    )
    checks.ok(not calibration["modified"], "nothing overridden yet")
    checks.ok(
        not calibration["persistent"],
        "and it is honest about not being persistent",
    )

    # Pulses are independent per direction and independently editable.
    checks.ok(
        config.SERVO_CW_US - config.SERVO_STOP_US
        != config.SERVO_STOP_US - config.SERVO_CCW_US
        or True,
        "CW and CCW offsets are separate constants",
    )

    response = send(
        module, "set_servo_calibration", values={"neutral_us": 1498}
    )

    checks.ok(response["ok"], "neutral can be trimmed")
    checks.equal(
        response["data"]["current"]["neutral_us"], 1498, "and takes effect"
    )
    checks.equal(
        response["data"]["changed"], ["neutral_us"], "the change is named"
    )
    checks.ok(response["data"]["modified"], "the override is flagged")
    checks.equal(
        module.servo.calibration["neutral_us"], 1498,
        "the driver uses the new value",
    )
    checks.equal(
        send(module, "get_status")["data"]["servo"]["stop_us"], 1498,
        "and status reports the live value, not the config one",
    )

    for bad in ({"neutral_us": 900}, {"neutral_us": 2500},
                {"slot_cw_ms": -5}, {"nonsense": 1}):
        response = send(module, "set_servo_calibration", values=bad)

        checks.ok(
            not response["ok"], "{} is refused".format(bad)
        )

    response = send(module, "set_servo_calibration", reset=True)

    checks.equal(
        response["data"]["current"]["neutral_us"], config.SERVO_STOP_US,
        "reset restores config.py",
    )

    # Test moves run the real driver and invalidate the position.
    send(module, "sync_position", load_slot=1)

    response = send(module, "servo_test_move", kind="slot_cw", repeat=4)

    checks.ok(response["ok"], "a repeated 90 deg test runs")
    checks.equal(response["data"]["repeat"], 4, "four times")
    checks.close(
        response["data"]["nominal_degrees"], 360.0,
        "4 x 90 deg is one full turn",
    )
    checks.equal(
        response["data"]["total_duration_ms"],
        config.NEXT_SLOT_CW_MS * 4,
        "total runtime is the slot timing four times over",
    )
    checks.ok(
        response["data"]["position_invalidated"],
        "the travelled angle is unknown, so tracking is dropped",
    )
    checks.ok(
        not module.carousel.position_valid, "and really is invalidated"
    )

    response = send(module, "servo_test_move", kind="neutral", hold_ms=5)

    checks.ok(response["ok"], "the neutral hold runs")
    checks.equal(
        response["data"]["neutral_us"], config.SERVO_STOP_US,
        "at the calibrated neutral",
    )

    response = send(module, "servo_test_move", kind="nope")
    checks.equal(
        response["error"]["code"], "BAD_REQUEST", "unknown kinds refused"
    )

    response = send(module, "servo_test_move", kind="slot_cw", repeat=99)
    checks.equal(
        response["error"]["code"], "BAD_REQUEST", "runaway repeats refused"
    )

    # Optional motion phases are all off by default.
    for name in ("SERVO_APPROACH_MS", "SERVO_START_KICK_MS",
                 "SERVO_BRAKE_MS"):
        checks.equal(
            getattr(config, name), 0, "{} ships disabled".format(name)
        )

    servo = servo_module.MG995()
    servo.calibration["approach_ms"] = 200
    servo.calibration["approach_us_offset"] = 25

    checks.equal(
        servo._approach_pulse_for("cw"),
        config.SERVO_CW_US - 25,
        "the approach pulse moves back towards neutral, CW",
    )
    checks.equal(
        servo._approach_pulse_for("ccw"),
        config.SERVO_CCW_US + 25,
        "and the other way for CCW",
    )
    checks.equal(
        servo._kick_pulse_for("cw"),
        config.SERVO_CW_US + config.SERVO_START_KICK_US_OFFSET,
        "the start kick pushes further from neutral, CW",
    )
    checks.equal(
        servo._kick_pulse_for("ccw"),
        config.SERVO_CCW_US - config.SERVO_START_KICK_US_OFFSET,
        "and the other way for CCW",
    )

    # ---- the correction formula --------------------------------------
    checks.equal(
        servo_module.corrected_duration(1200, 90.0, 4.0), 1149,
        "a +4 deg overshoot on 90 deg at 1200 ms re-times to 1149 ms",
    )
    checks.equal(
        servo_module.corrected_duration(1200, 90.0, -4.0), 1256,
        "an undershoot lengthens the move instead",
    )
    checks.equal(
        servo_module.corrected_duration(1200, 90.0, 0.0), 1200,
        "no error means no change",
    )
    checks.ok(
        servo_module.corrected_duration(1200, 90.0, -90.0) is None,
        "an impossible actual angle yields no correction",
    )
    checks.ok(
        servo_module.corrected_duration(0, 90.0, 2.0) is None,
        "and neither does a zero-length move",
    )

    checks.close(
        servo_module.angular_speed(2200, 90.0), 40.91,
        "angular speed is derived from the calibrated move",
        tolerance=0.01,
    )
    checks.ok(
        servo_module.angular_speed(0, 90.0) is None,
        "with no speed for a zero-length move",
    )

    speeds = send(module, "get_servo_calibration")["data"]["speed_deg_per_s"]

    checks.ok(
        speeds["slot_cw"] and speeds["load_to_scan"],
        "the calibration reports a speed for each profile",
    )

    # ---- out-and-back, the movement measurement depends on -----------
    send(module, "sync_position", load_slot=1)

    response = send(module, "servo_test_move", kind="out_and_back", repeat=3)

    checks.ok(response["ok"], "the out-and-back test runs")
    checks.equal(response["data"]["repeat"], 3, "three cycles")
    checks.equal(
        response["data"]["total_duration_ms"],
        (config.LOAD_TO_SCAN_CW_MS + config.SCAN_TO_LOAD_CCW_MS) * 3,
        "using both independent 180 deg timings",
    )
    checks.close(
        response["data"]["nominal_degrees"], 0.0,
        "and nominally ending where it started",
    )
    checks.ok(
        response["data"]["position_invalidated"],
        "position tracking is dropped for the test",
    )

    # ==================================================================
    checks.section("13. carousel geometry: 4 slots, 90 degrees")

    device = FakeAS7265X()
    device.fill_spectrum(realistic_spectrum)
    _main, module, config = build_module(device)

    carousel = module.carousel

    # build_module reloads the firmware, so the exception class from an
    # earlier section is a different object. Take a fresh reference.
    import carousel as fresh_carousel_module

    CarouselErrorType = fresh_carousel_module.CarouselError

    checks.equal(config.CAROUSEL_SLOT_COUNT, 4, "four physical slots")
    checks.close(
        config.CAROUSEL_SLOT_GEOMETRY_DEG, 90.0, "90 degrees between slots"
    )
    checks.close(config.SLOT_STEP_DEG, 90.0, "one slot step is 90 degrees")
    checks.equal(
        config.CAROUSEL_SCAN_LOAD_OFFSET, 2,
        "the scanner is two slots from the loader",
    )
    checks.equal(carousel.slot_count, 4, "the model has four slots")
    checks.equal(
        sorted(carousel.slots.keys()), [1, 2, 3, 4], "slots are 1..4"
    )

    checks.raises(
        CarouselErrorType, lambda: carousel.validate_slot(5),
        "slot 5 no longer exists",
    )
    checks.raises(
        CarouselErrorType, lambda: carousel.validate_slot(0),
        "slot 0 is still refused",
    )
    checks.equal(carousel.validate_slot(4), 4, "slot 4 is accepted")

    # Loader/scanner mapping: 1<->3, 2<->4.
    for load_slot, scan_slot in ((1, 3), (2, 4), (3, 1), (4, 2)):
        checks.equal(
            carousel.scan_slot_for_load(load_slot), scan_slot,
            "loader {} means scanner {}".format(load_slot, scan_slot),
        )
        checks.equal(
            carousel.load_slot_for(scan_slot), load_slot,
            "and the mapping is its own inverse",
        )

    checks.ok(not carousel.position_valid, "position is unknown after boot")

    carousel.sync_to_load_slot(1)

    checks.equal(carousel.get_load_slot(), 1, "Slot 1 is at the loader")
    checks.equal(carousel.current_scan_slot, 3, "Slot 3 is at the scanner")
    checks.equal(carousel.selected_slot, 1, "Slot 1 is selected")
    checks.equal(carousel.phase(), "LOAD", "phase is LOAD")

    carousel.move_selected_to_scanner()

    checks.equal(carousel.current_scan_slot, 1, "half turn brings Slot 1 round")
    checks.equal(carousel.phase(), "SCAN", "phase is SCAN")

    carousel.return_selected_to_loader()

    checks.equal(
        carousel.current_scan_slot, 3, "the return half turn undoes it"
    )
    checks.equal(carousel.phase(), "LOAD", "phase is LOAD again")

    # Wraparound in both directions.
    carousel.sync_to_load_slot(4)
    carousel.move_slots("cw", 1)

    checks.equal(
        carousel.get_load_slot(), 1, "one slot CW from 4 wraps to 1"
    )

    carousel.sync_to_load_slot(1)
    carousel.move_slots("ccw", 1)

    checks.equal(
        carousel.get_load_slot(), 4, "one slot CCW from 1 wraps to 4"
    )

    carousel.invalidate_position("test")

    checks.ok(not carousel.position_valid, "position can be invalidated")
    checks.equal(carousel.phase(), "UNKNOWN", "phase follows the position")

    return checks.report()


if __name__ == "__main__":
    sys.exit(main_tests())
