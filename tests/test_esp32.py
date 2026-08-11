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
    config.SERVO_SETTLE_TIME = 0.0
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
        len(response["data"]["raw"]), 18, "recovered sensor returns 18 channels"
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
    checks.section("5. raw acquisition")

    device = FakeAS7265X()
    device.fill_spectrum(realistic_spectrum)
    _main, module, config = build_module(device)

    acquisition = module.sensor.acquire_raw_spectrum()
    spectrum = acquisition["spectrum"]

    checks.equal(len(spectrum), 18, "18 channels acquired")
    checks.ok(
        all(isinstance(value, float) for value in spectrum.values()),
        "every channel is numeric",
    )
    checks.ok(
        len(set(spectrum.values())) > 1,
        "channels are distinguishable, not one repeated value",
    )
    checks.equal(acquisition["zero_channels"], [], "no zero channels")
    checks.ok(not device.led_on, "illumination is off after acquisition")
    checks.ok(device.led_on_count >= 1, "illumination was actually used")

    # A failure mid-acquisition must still leave the LED off.
    device.data_ready_supported = False
    module.sensor.ready = True

    import as7265x

    checks.raises(
        as7265x.SensorError,
        module.sensor.acquire_raw_spectrum,
        "a DATA_READY timeout raises SensorError",
    )
    checks.ok(not device.led_on, "illumination is off after a failure too")

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
        "clear_slot", "measure_raw", "sensor_test_raw", "servo_stop",
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
    checks.section("10. successful measure_raw")

    response = send(module, "measure_raw", slot=1, sample_id="S001")
    checks.ok(response["ok"], "measure_raw succeeds")

    data = response["data"]

    checks.equal(len(data["raw"]), 18, "18 raw channels returned")
    checks.equal(data["slot_id"], 1, "slot echoed")
    checks.equal(data["sample_id"], "S001", "sample id echoed")
    checks.ok(
        data["sensor_settings"]["integration_cycles"] == 100,
        "settings travel with the spectrum",
    )
    checks.equal(
        data["carousel"]["carousel_phase"], "SCAN", "the sample is at the scanner"
    )
    checks.ok(data["slot"]["occupied"], "the slot is marked occupied")

    checks.ok(
        "normalized" not in json.dumps(data),
        "no normalization was performed on the ESP32",
    )
    checks.ok(
        "similarity" not in json.dumps(data),
        "no database comparison was performed on the ESP32",
    )

    response = send(module, "clear_slot", slot=1)
    checks.ok(response["ok"], "clear_slot works")
    checks.ok(
        not response["data"]["slot"]["occupied"], "the slot is free again"
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
    checks.section("13. carousel geometry")

    device = FakeAS7265X()
    device.fill_spectrum(realistic_spectrum)
    _main, module, config = build_module(device)

    carousel = module.carousel

    checks.ok(not carousel.position_valid, "position is unknown after boot")

    carousel.sync_to_load_slot(1)

    checks.equal(carousel.get_load_slot(), 1, "Slot 1 is at the loader")
    checks.equal(carousel.current_scan_slot, 5, "Slot 5 is at the scanner")
    checks.equal(carousel.selected_slot, 1, "Slot 1 is selected")
    checks.equal(carousel.phase(), "LOAD", "phase is LOAD")

    carousel.move_selected_to_scanner()

    checks.equal(carousel.current_scan_slot, 1, "half turn brings Slot 1 round")
    checks.equal(carousel.phase(), "SCAN", "phase is SCAN")

    carousel.invalidate_position("test")

    checks.ok(not carousel.position_valid, "position can be invalidated")
    checks.equal(carousel.phase(), "UNKNOWN", "phase follows the position")

    return checks.report()


if __name__ == "__main__":
    sys.exit(main_tests())
