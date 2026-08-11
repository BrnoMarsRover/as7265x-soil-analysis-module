# sensor_diag.py
# Staged AS7265x diagnostics.
#
# Answers one question: when the sensor path does not work, WHERE does
# it stop? Every stage is timed, reports PASS / FAIL / WARN / SKIPPED,
# and carries a machine-readable code, so the PC can print a precise
# report instead of "sensor error".
#
# Everything returned here is JSON-safe primitives only. stdout on the
# ESP32 is the protocol stream, so this module never prints.
#
# Read-only with respect to science data: no sample is created, no slot
# state changes, nothing is written to samples.json, and the carousel is
# never moved.

import time

import config
import sample_analysis as analysis

from as7265x import (
    AS7265X,
    CHANNEL_LAYOUT,
    SensorError,
    StrictAS7265X,
    scan_i2c_bus,
)


PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
SKIPPED = "SKIPPED"


class Report:
    """Ordered list of stage results plus the first failure seen."""

    def __init__(self):
        self.stages = []
        self.first_failure = None

    def add(self, stage, status, details=None, error=None, duration_ms=0):
        entry = {
            "stage": stage,
            "status": status,
            "duration_ms": duration_ms
        }

        if details:
            entry["details"] = details

        if error:
            entry["error"] = error

        self.stages.append(entry)

        if status == FAIL and self.first_failure is None:
            self.first_failure = {
                "stage": stage,
                "code": (error or {}).get("code", "UNKNOWN"),
                "message": (error or {}).get("message", "")
            }

        return entry

    def skip(self, stage, reason):
        return self.add(
            stage, SKIPPED, details={"reason": reason}
        )

    def overall(self):
        return FAIL if self.first_failure else PASS


def error_dict(code, message, stage=None, details=None, exception=None):
    """Uniform, JSON-safe error shape."""
    out = {"code": code, "message": message}

    if stage:
        out["stage"] = stage

    if exception is not None:
        out["exception_type"] = type(exception).__name__
        out["exception_message"] = str(exception)

    if details:
        out["details"] = details

    return out


def from_sensor_error(error):
    return error_dict(
        error.code,
        error.message,
        stage=error.stage,
        details=error.details,
        exception=error,
    )


def _elapsed(started):
    return time.ticks_diff(time.ticks_ms(), started)


def run_i2c_scan():
    """
    Stage: build the bus and scan it.

    Works even when driver initialization failed at boot, which is
    exactly when the operator needs the answer.
    """
    report = Report()

    started = time.ticks_ms()

    details = {
        "bus": 0,
        "sda": "GPIO{}".format(config.I2C_SDA_PIN),
        "scl": "GPIO{}".format(config.I2C_SCL_PIN),
        "frequency_hz": config.I2C_FREQ,
        "expected_address": "0x{:02X}".format(AS7265X.ADDRESS)
    }

    try:
        i2c, found = scan_i2c_bus()

    except SensorError as error:
        report.add(
            "I2C_SCAN", FAIL,
            details=details,
            error=from_sensor_error(error),
            duration_ms=_elapsed(started),
        )

        return None, report

    details["addresses"] = ["0x{:02X}".format(a) for a in found]
    details["device_count"] = len(found)
    details["as7265x_detected"] = AS7265X.ADDRESS in found

    if not found:
        report.add(
            "I2C_SCAN", FAIL,
            details=details,
            error=error_dict(
                "I2C_NO_DEVICES",
                "The I2C bus responded but no devices answered. Check "
                "wiring, pull-ups and sensor power.",
                stage="I2C_SCAN",
            ),
            duration_ms=_elapsed(started),
        )

        return None, report

    if AS7265X.ADDRESS not in found:
        report.add(
            "I2C_SCAN", FAIL,
            details=details,
            error=error_dict(
                "AS7265X_ADDRESS_NOT_FOUND",
                "Devices answered on the bus, but not the AS7265x at "
                "0x{:02X}.".format(AS7265X.ADDRESS),
                stage="I2C_SCAN",
            ),
            duration_ms=_elapsed(started),
        )

        return None, report

    report.add(
        "I2C_SCAN", PASS, details=details, duration_ms=_elapsed(started)
    )

    return i2c, report


def run_full_diagnostics(module, acquire=True):
    """
    Walk the whole sensor path, stage by stage.

    `module` is the ScienceModule, used only to read its boot-time
    health and its loaded references/database. Nothing is written.

    Independent stages continue after a failure; dependent ones are
    marked SKIPPED, so a working sensor with a broken database still
    reports the raw spectrum.
    """
    report = Report()
    result = {
        "raw": None,
        "dark_corrected": None,
        "normalized": None,
        "reference_matches": None,
        "analysis": None
    }

    # -- 1. command routing -------------------------------------------
    report.add(
        "COMMAND_ROUTING", PASS,
        details={"note": "Handler reached; the command crossed the link."},
    )

    # -- 2. sensor object ---------------------------------------------
    started = time.ticks_ms()

    sensor_ready = module.sensor is not None

    sensor_details = {
        "measurement_system": "READY" if sensor_ready else "MISSING",
        "driver": "READY" if module.driver is not None else "MISSING",
    }

    if sensor_ready:
        report.add(
            "SENSOR_OBJECT", PASS, details=sensor_details,
            duration_ms=_elapsed(started),
        )

    else:
        sensor_details["boot_error"] = module.sensor_error

        report.add(
            "SENSOR_OBJECT", FAIL,
            details=sensor_details,
            error=error_dict(
                "SENSOR_NOT_INITIALIZED",
                "The sensor object is None: AS7265x initialization "
                "failed during boot.",
                stage="SENSOR_OBJECT",
                details={"boot_error": module.sensor_error},
            ),
            duration_ms=_elapsed(started),
        )

    # -- 3. I2C bus and scan ------------------------------------------
    i2c, scan_report = run_i2c_scan()

    for entry in scan_report.stages:
        report.stages.append(entry)

        if entry["status"] == FAIL and report.first_failure is None:
            report.first_failure = {
                "stage": entry["stage"],
                "code": entry.get("error", {}).get("code", "UNKNOWN"),
                "message": entry.get("error", {}).get("message", "")
            }

    if i2c is None:
        for stage in ("PHYSICAL_REGISTER", "VIRTUAL_REGISTER",
                      "SLAVE_DEVICES", "SENSOR_CONFIG", "LED_CONTROL",
                      "WAIT_DATA_READY", "CHANNEL_ACQUISITION"):
            report.skip(stage, "I2C scan failed")

        _run_analysis_stages(module, report, result, None)

        return _finish(report, result)

    strict = StrictAS7265X(i2c)

    # -- 4. physical register -----------------------------------------
    started = time.ticks_ms()

    try:
        status_byte = strict.read_register(AS7265X.STATUS_REG)

        report.add(
            "PHYSICAL_REGISTER", PASS,
            details={
                "register": "STATUS_REG (0x00)",
                "value": "0x{:02X}".format(status_byte)
            },
            duration_ms=_elapsed(started),
        )

    except SensorError as error:
        report.add(
            "PHYSICAL_REGISTER", FAIL,
            error=from_sensor_error(error),
            duration_ms=_elapsed(started),
        )

        for stage in ("VIRTUAL_REGISTER", "SLAVE_DEVICES", "SENSOR_CONFIG",
                      "LED_CONTROL", "WAIT_DATA_READY",
                      "CHANNEL_ACQUISITION"):
            report.skip(stage, "physical register access failed")

        _run_analysis_stages(module, report, result, None)

        return _finish(report, result)

    # -- 5. virtual register + 6. slave devices ------------------------
    started = time.ticks_ms()

    try:
        slave_byte, slaves_ok = strict.read_slave_status()

        report.add(
            "VIRTUAL_REGISTER", PASS,
            details={
                "virtual_register": "DEV_SELECT_CONTROL (0x4F)",
                "value": "0x{:02X}".format(slave_byte)
            },
            duration_ms=_elapsed(started),
        )

        slave_details = {
            "dev_select_control": "0x{:02X}".format(slave_byte),
            "visible_device": "READY" if slave_byte & 0b00010000
            else "NOT DETECTED",
            "nir_device": "READY" if slave_byte & 0b00100000
            else "NOT DETECTED",
        }

        if slaves_ok:
            report.add(
                "SLAVE_DEVICES", PASS, details=slave_details,
            )

        else:
            report.add(
                "SLAVE_DEVICES", FAIL,
                details=slave_details,
                error=error_dict(
                    "AS7265X_SLAVES_NOT_DETECTED",
                    "The master responds on 0x{:02X}, but the AS7265x "
                    "slave devices were not detected. This points at the "
                    "sensor board itself rather than the I2C "
                    "wiring.".format(AS7265X.ADDRESS),
                    stage="SLAVE_DEVICES",
                ),
            )

    except SensorError as error:
        report.add(
            "VIRTUAL_REGISTER", FAIL,
            error=from_sensor_error(error),
            duration_ms=_elapsed(started),
        )
        report.skip("SLAVE_DEVICES", "virtual register access failed")

        for stage in ("SENSOR_CONFIG", "LED_CONTROL", "WAIT_DATA_READY",
                      "CHANNEL_ACQUISITION"):
            report.skip(stage, "virtual register access failed")

        _run_analysis_stages(module, report, result, None)

        return _finish(report, result)

    # -- 7. configuration ---------------------------------------------
    started = time.ticks_ms()

    try:
        configuration = strict.read_configuration()

        configuration["gain_x"] = config.SENSOR_GAIN_NAMES.get(
            configuration["gain"], "unknown"
        )
        configuration["expected_gain"] = config.SENSOR_GAIN
        configuration["expected_integration_cycles"] = (
            config.SENSOR_INTEGRATION_CYCLES
        )

        matches = (
            configuration["gain"] == config.SENSOR_GAIN
            and configuration["integration_cycles"]
            == config.SENSOR_INTEGRATION_CYCLES
        )

        report.add(
            "SENSOR_CONFIG", PASS if matches else WARN,
            details=configuration,
            error=None if matches else error_dict(
                "SENSOR_CONFIG_MISMATCH",
                "The sensor is not configured with the values from "
                "config.py. Acquisition can still work.",
                stage="SENSOR_CONFIG",
            ),
            duration_ms=_elapsed(started),
        )

    except SensorError as error:
        report.add(
            "SENSOR_CONFIG", FAIL,
            error=from_sensor_error(error),
            duration_ms=_elapsed(started),
        )

    # -- 8. LED control ------------------------------------------------
    started = time.ticks_ms()
    led_steps = []

    try:
        strict.set_led(False)
        led_steps.append("OFF -> OK")

        strict.set_led(True)
        led_steps.append("ON -> OK")

        time.sleep_ms(150)

        report.add(
            "LED_CONTROL", PASS,
            details={"sequence": led_steps},
            duration_ms=_elapsed(started),
        )

    except SensorError as error:
        report.add(
            "LED_CONTROL", FAIL,
            details={"sequence": led_steps},
            error=from_sensor_error(error),
            duration_ms=_elapsed(started),
        )

    finally:
        # Never leave the illumination on after a failure.
        try:
            strict.set_led(False)
            led_steps.append("OFF -> OK")

        except Exception:
            led_steps.append("OFF -> FAILED")

    if not acquire:
        for stage in ("WAIT_DATA_READY", "CHANNEL_ACQUISITION"):
            report.skip(stage, "acquisition not requested")

        return _finish(report, result)

    # -- 9. data ready -------------------------------------------------
    started = time.ticks_ms()

    timeout_ms = max(
        int(config.SENSOR_INTEGRATION_CYCLES * 2.8 * 1.5) + 1, 1500
    )

    try:
        strict.set_led(True)
        time.sleep_ms(300)

        waited = strict.wait_data_ready(timeout_ms)

        report.add(
            "WAIT_DATA_READY", PASS,
            details={
                "waited_ms": waited,
                "timeout_ms": timeout_ms,
                "integration_cycles": config.SENSOR_INTEGRATION_CYCLES
            },
            duration_ms=_elapsed(started),
        )

    except SensorError as error:
        report.add(
            "WAIT_DATA_READY", FAIL,
            details={
                "timeout_ms": timeout_ms,
                "integration_cycles": config.SENSOR_INTEGRATION_CYCLES
            },
            error=from_sensor_error(error),
            duration_ms=_elapsed(started),
        )

        try:
            strict.set_led(False)
        except Exception:
            pass

        report.skip("CHANNEL_ACQUISITION", "data ready never asserted")

        _run_analysis_stages(module, report, result, None)

        return _finish(report, result)

    # -- 10. every channel, individually -------------------------------
    started = time.ticks_ms()
    raw = {}
    channel_log = []

    try:
        for channel, nanometres, device, cal, dev_id in CHANNEL_LAYOUT:
            value = strict.read_channel(channel, cal, dev_id)
            raw[channel] = value

            channel_log.append({
                "channel": channel,
                "nm": nanometres,
                "device": device,
                "value": value
            })

        report.add(
            "CHANNEL_ACQUISITION", PASS,
            details={
                "channels_read": len(raw),
                "channels": channel_log
            },
            duration_ms=_elapsed(started),
        )

    except SensorError as error:
        report.add(
            "CHANNEL_ACQUISITION", FAIL,
            details={
                "channels_read": len(raw),
                "channels": channel_log
            },
            error=from_sensor_error(error),
            duration_ms=_elapsed(started),
        )

        raw = None

    finally:
        try:
            strict.set_led(False)
        except Exception:
            pass

    if raw is not None:
        _validate_raw(raw, report)
        result["raw"] = analysis.round_channels(
            raw, analysis.RAW_DECIMALS
        )

    _run_analysis_stages(module, report, result, raw)

    return _finish(report, result)


def _validate_raw(raw, report):
    """Every channel must be present and numeric; flag odd spectra."""
    missing = analysis.validate_channels(raw)

    if missing:
        report.add(
            "CHANNEL_VALIDATION", FAIL,
            error=error_dict(
                "INCOMPLETE_SPECTRUM",
                "Missing or non-numeric channels: {}".format(
                    ",".join(missing)
                ),
                stage="CHANNEL_VALIDATION",
                details={"missing_channels": missing},
            ),
        )

        return

    zeros = [c for c in analysis.CHANNELS if float(raw.get(c, 0.0)) == 0.0]

    if len(zeros) == len(analysis.CHANNELS):
        report.add(
            "CHANNEL_VALIDATION", WARN,
            details={"zero_channels": len(zeros)},
            error=error_dict(
                "ALL_CHANNELS_ZERO",
                "Every channel read as exactly zero. The transaction "
                "succeeded, so this usually means no illumination or a "
                "sensor that has not finished a conversion.",
                stage="CHANNEL_VALIDATION",
            ),
        )

    elif len(zeros) > len(analysis.CHANNELS) // 2:
        report.add(
            "CHANNEL_VALIDATION", WARN,
            details={"zero_channels": len(zeros),
                     "channels": zeros},
            error=error_dict(
                "MANY_ZERO_CHANNELS",
                "More than half the channels read zero.",
                stage="CHANNEL_VALIDATION",
            ),
        )

    else:
        report.add(
            "CHANNEL_VALIDATION", PASS,
            details={"zero_channels": len(zeros)},
        )


def _run_analysis_stages(module, report, result, raw):
    """
    References, correction, normalization and the database comparison.

    Runs even when acquisition failed, so a broken analysis path is
    still reported rather than hidden behind an earlier failure.
    """
    # -- references ----------------------------------------------------
    started = time.ticks_ms()

    if module.references_ok:
        report.add(
            "REFERENCES", PASS,
            details={
                "file": config.REFERENCES_FILE,
                "dark": "{}/18".format(
                    module.reference_report["dark"]["channels_present"]
                ),
                "white": "{}/18".format(
                    module.reference_report["white"]["channels_present"]
                ),
            },
            duration_ms=_elapsed(started),
        )

    else:
        report.add(
            "REFERENCES", FAIL,
            details=module.reference_report,
            error=error_dict(
                "REFERENCES_NOT_LOADED",
                module.references_error or "References are not loaded.",
                stage="REFERENCES",
            ),
            duration_ms=_elapsed(started),
        )

    # -- dark correction and normalization -----------------------------
    if raw is None:
        report.skip("DARK_CORRECTION", "no spectrum acquired")
        report.skip("NORMALIZATION", "no spectrum acquired")

    elif not module.references_ok:
        report.skip("DARK_CORRECTION", "references not loaded")
        report.skip("NORMALIZATION", "references not loaded")

    else:
        started = time.ticks_ms()

        try:
            corrected = analysis.dark_correct(raw, module.dark_reference)
            result["dark_corrected"] = corrected

            report.add(
                "DARK_CORRECTION", PASS,
                duration_ms=_elapsed(started),
            )

        except Exception as error:
            report.add(
                "DARK_CORRECTION", FAIL,
                error=error_dict(
                    "DARK_CORRECTION_FAILED",
                    "Sample - Dark failed.",
                    stage="DARK_CORRECTION",
                    exception=error,
                ),
                duration_ms=_elapsed(started),
            )

        started = time.ticks_ms()

        zero_denominators = [
            channel for channel in analysis.CHANNELS
            if float(module.white_reference.get(channel, 0.0))
            - float(module.dark_reference.get(channel, 0.0)) == 0.0
        ]

        try:
            module.install_references()
            module.sensor.sample_data = raw

            if not module.sensor.normalize():
                raise ValueError("normalize() reported failure")

            result["normalized"] = analysis.round_channels(
                module.sensor.normalized_data,
                analysis.NORMALIZED_DECIMALS,
            )

            report.add(
                "NORMALIZATION",
                WARN if zero_denominators else PASS,
                details={"zero_denominator_channels": zero_denominators},
                error=error_dict(
                    "NORMALIZATION_ZERO_DENOMINATOR",
                    "White minus dark is zero on {} channel(s); those "
                    "are reported as 0.0.".format(len(zero_denominators)),
                    stage="NORMALIZATION",
                ) if zero_denominators else None,
                duration_ms=_elapsed(started),
            )

        except Exception as error:
            report.add(
                "NORMALIZATION", FAIL,
                error=error_dict(
                    "NORMALIZATION_FAILED",
                    "Reflectance calculation failed.",
                    stage="NORMALIZATION",
                    exception=error,
                ),
                duration_ms=_elapsed(started),
            )

    # -- material database ---------------------------------------------
    started = time.ticks_ms()
    count = module.database_count()

    if count > 0:
        report.add(
            "MATERIAL_DATABASE", PASS,
            details={"file": config.DATABASE_FILE,
                     "materials_loaded": count},
            duration_ms=_elapsed(started),
        )

    else:
        report.add(
            "MATERIAL_DATABASE", FAIL,
            details={"file": config.DATABASE_FILE},
            error=error_dict(
                "DATABASE_EMPTY",
                module.database_error
                or "No reference materials are loaded.",
                stage="MATERIAL_DATABASE",
            ),
            duration_ms=_elapsed(started),
        )

    # -- comparison ------------------------------------------------------
    if result.get("normalized") is None:
        report.skip("SPECTRAL_COMPARISON", "no normalized spectrum")

    elif count == 0:
        report.skip("SPECTRAL_COMPARISON", "database empty")

    else:
        started = time.ticks_ms()

        try:
            matches = analysis.build_matches(
                module.database, module.sensor.normalized_data
            )
            result["reference_matches"] = matches
            result["analysis"] = analysis.interpret(matches)

            report.add(
                "SPECTRAL_COMPARISON", PASS,
                details={"materials_compared": len(matches)},
                duration_ms=_elapsed(started),
            )

        except Exception as error:
            report.add(
                "SPECTRAL_COMPARISON", FAIL,
                error=error_dict(
                    "DATABASE_MATCH_FAILED",
                    "Comparison against the reference materials failed.",
                    stage="DATABASE_COMPARE",
                    exception=error,
                ),
                duration_ms=_elapsed(started),
            )


def _finish(report, result):
    """Assemble the JSON-safe diagnostic response."""
    report.add(
        "JSON_RESPONSE", PASS,
        details={"note": "Response assembled from primitives only."},
    )

    out = {
        "diagnostics": report.stages,
        "overall": report.overall(),
        "first_failure": report.first_failure,
        "channels_nm": analysis.channel_wavelengths(),
        "test_only": True,
        "saved": False
    }

    out.update(result)

    return out
