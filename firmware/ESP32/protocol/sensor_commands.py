# protocol/sensor_commands.py
# Spectral acquisition and illumination commands.
#
# The measurement cycle is the one place where the carousel and the sensor
# have to co-operate:
#
#     sample at LOAD -> half turn -> verify -> settle -> acquire
#                    -> half turn back -> report
#
# Two rules hold that sequence together, and neither is negotiable:
#
#   - the sensor is proved usable BEFORE anything moves, so a sensor fault
#     never leaves a sample stranded at the scanner;
#
#   - the return movement is reported as its own outcome, never as part of
#     the acquisition, because by the time it runs the spectra already
#     exist and a mechanical failure must not destroy acquired science.
#
# Everything scientific - dark correction, normalization, database
# comparison, interpretation - happens on the PC. This layer returns raw
# counts.

import time

import config

from control.carousel import CarouselError
from drivers import as7265x
from drivers.as7265x import SensorError
from protocol.router import CommandError


class SensorCommands:
    """Acquisition, illumination and the full measurement cycle."""

    def __init__(self, module):
        self.module = module

    @property
    def sensor(self):
        return self.module.sensor

    @property
    def carousel(self):
        return self.module.carousel

    @property
    def servos(self):
        return self.module.servos

    def _require_slot(self, request):
        return self.module.require_slot(request)

    def _sensor_error(self, error, extra=None):
        return self.module.sensor_error(error, extra)

    def _uptime_ms(self):
        return self.module.uptime_ms()

    def handlers(self):
        return {
            "measure_raw": self.handle_measure_raw,
            "sensor_test_raw": self.handle_sensor_test_raw,
            "acquire_block": self.handle_acquire_block,
            "acquire_triad": self.handle_acquire_triad,
            "led_test": self.handle_led_test,
        }

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------

    def _raw_payload(self, acquisition):
        """The RAW block a single-spectrum acquisition returns."""
        return {
            "raw": acquisition["spectrum"],
            "data_ready_wait_ms": acquisition["data_ready_wait_ms"],
            "zero_channels": acquisition["zero_channels"],
            "sensor_settings": self.sensor.settings(),
        }

    def _requested_repeats(self, request, default):
        try:
            repeats = int(request.get("repeats", default))

        except (TypeError, ValueError):
            raise CommandError("BAD_REQUEST", "'repeats' must be a number.")

        if repeats < 1 or repeats > config.MAX_REPEATS:
            raise CommandError(
                "BAD_REQUEST",
                "'repeats' must be between 1 and {}.".format(
                    config.MAX_REPEATS
                ),
            )

        return repeats

    def _lamp_from(self, request):
        """Illumination name -> lamp id. None means a dark acquisition."""
        name = request.get("illumination", "white")

        if name in (None, "dark", "none"):
            return None

        lamp = as7265x.LAMP_BY_NAME.get(name)

        if lamp is None:
            raise CommandError(
                "BAD_REQUEST",
                "illumination must be one of: dark, {}.".format(
                    ", ".join(sorted(as7265x.LAMP_BY_NAME.keys()))
                ),
            )

        return lamp

    def handle_acquire_block(self, request):
        """
        Repeat ONE illumination and return every individual reading.

        The building block the PC uses for calibration: dark, white
        target under WHITE, under UV and under IR are all this same
        command with a different illumination. No statistics here - the
        readings go to the PC intact so they can be aggregated and
        archived where the arithmetic is trustworthy.
        """
        lamp = self._lamp_from(request)
        repeats = self._requested_repeats(request, config.CALIBRATION_REPEATS)

        try:
            block = self.sensor.acquire_block(lamp, repeats)

        except SensorError as error:
            raise self._sensor_error(error, {"repeats": repeats})

        block["sensor_settings"] = self.sensor.settings()
        block["bulbs_off"] = self._bulbs_off()

        self._settle_before_responding()

        return block

    def handle_acquire_triad(self, request):
        """
        WHITE, UV and IR, repeated - one complete spectral measurement,
        without moving anything.

        Used by the Sensor Test and by any caller that wants the full
        54-feature acquisition on its own.
        """
        repeats = self._requested_repeats(request, config.SAMPLE_REPEATS)

        try:
            blocks = self.sensor.acquire_triad(repeats)

        except SensorError as error:
            raise self._sensor_error(error, {"repeats": repeats})

        report = {
            "illuminations": blocks,
            "repeats": repeats,
            "sensor_settings": self.sensor.settings(),
            "temperatures": self._temperatures(),
            "bulbs_off": self._bulbs_off(),
            "protocol_version": config.ACQUISITION_PROTOCOL_VERSION,
        }

        self._settle_before_responding()

        return report

    def _settle_before_responding(self):
        """
        Let the supply recover before the answer goes out on the console.

        Switching an illumination LED off is the largest current step
        this board makes, and without this the multi-kilobyte response is
        written straight into that transient. A corrupted response is
        indistinguishable from no response at the far end, so it costs
        the PC its whole timeout - which is how one bad IR block used to
        take a complete calibration with it. See
        config.ACQUISITION_RESPONSE_SETTLE_MS.
        """
        delay = getattr(config, "ACQUISITION_RESPONSE_SETTLE_MS", 0)

        if delay:
            time.sleep_ms(int(delay))

    def _bulbs_off(self):
        """Read the lamp state back rather than assuming it."""
        try:
            return not any(self.sensor.driver.bulb_states().values())

        except Exception:
            return None

    def _temperatures(self):
        try:
            return self.sensor.driver.read_temperatures()

        except Exception:
            return None

    def handle_led_test(self, request):
        """
        Exercise each lamp on its own and verify it goes off again.

        Reads the enable bit back at every step, so a lamp that silently
        refuses to switch is reported instead of assumed working.
        """
        try:
            driver = self.sensor.ensure_ready()

        except SensorError as error:
            raise self._sensor_error(error)

        try:
            hold_ms = int(request.get("hold_ms", 400))

        except (TypeError, ValueError):
            raise CommandError("BAD_REQUEST", "'hold_ms' must be a number.")

        hold_ms = max(0, min(hold_ms, 3000))

        lamps = []

        try:
            driver.disable_all_bulbs()

            for name in ("white", "uv", "ir"):
                lamp = as7265x.LAMP_BY_NAME[name]
                entry = {"illumination": name}

                try:
                    driver.set_bulb_current(
                        lamp, as7265x.LAMP_CURRENTS[lamp]()
                    )
                    entry["current"] = driver.read_bulb_current(lamp)
                    entry["current_ma"] = (
                        config.SENSOR_LED_CURRENT_NAMES.get(
                            entry["current"], "unknown"
                        )
                    )

                    driver.enable_bulb(lamp)
                    entry["on_readback"] = driver.bulb_enabled(lamp)

                    time.sleep_ms(hold_ms)

                    driver.disable_bulb(lamp)
                    entry["off_readback"] = not driver.bulb_enabled(lamp)

                    entry["ok"] = bool(
                        entry["on_readback"] and entry["off_readback"]
                    )

                    if not entry["ok"]:
                        entry["error"] = {
                            "code": "LED_STATE_NOT_APPLIED",
                            "message": "The {} lamp did not read back the "
                                       "requested state.".format(name),
                            "stage": "LED_TEST",
                        }

                except SensorError as error:
                    entry["ok"] = False
                    entry["error"] = error.as_dict()

                lamps.append(entry)

        finally:
            try:
                driver.disable_all_bulbs()

            except Exception:
                pass

        states = {}

        try:
            states = driver.bulb_states()
            all_off = not any(states.values())

        except Exception:
            all_off = None

        return {
            "test_only": True,
            "lamps": lamps,
            "final_states": states,
            "all_off": all_off,
            "ok": all(entry.get("ok") for entry in lamps) and bool(all_off),
            "sensor_settings": self.sensor.settings(),
        }

    def handle_measure_raw(self, request):
        """
        The full measurement cycle:

            180 deg to the scanner -> acquire RAW -> 180 deg back home

        A successful measurement leaves the sample at exactly the
        position it started from, so the operator never has to think
        about where the carousel ended up.

        The scientific pipeline ends here. Dark correction,
        normalization, database comparison and interpretation all run on
        the PC, against the protected references in firmware/BD.
        """
        slot_id = self._require_slot(request)
        sample_id = request.get("sample_id")

        self.carousel.require_position()

        if self.carousel.selected_slot != slot_id:
            raise CommandError(
                "SLOT_NOT_SELECTED",
                "Slot {} is not the selected slot (Slot {} is). Select it "
                "first so the mechanism and the request agree.".format(
                    slot_id, self.carousel.selected_slot
                ),
                data={"selected_slot": self.carousel.selected_slot},
            )

        phase = self.carousel.phase()

        if phase != "LOAD":
            raise CommandError(
                "SLOT_NOT_AT_LOADER",
                "Slot {} is not at the loading position (carousel phase "
                "is {}). Measurement starts from the loading hole and "
                "swings the sample to the scanner.".format(slot_id, phase),
                data={"carousel_phase": phase},
            )

        # Prove the sensor is usable BEFORE moving anything. A sensor
        # fault should not leave a sample stranded at the scanner.
        try:
            self.sensor.ensure_ready()

        except SensorError as error:
            raise self._sensor_error(
                error,
                {
                    "moved": False,
                    "carousel": self.carousel.status(),
                    "message": "Nothing was moved; the sample is still at "
                               "the loading position.",
                },
            )

        # The position the sample must be back at when this is over.
        home_scan_slot = self.carousel.current_scan_slot

        # --- out: 180 deg LOAD -> SCAN -------------------------------
        try:
            move = self.carousel.move_selected_to_scanner()

        except CarouselError as error:
            raise CommandError(
                error.code,
                error.message,
                data={"moved": False, "carousel": self.carousel.status()},
            )

        if config.SCAN_SETTLE_TIME > 0:
            time.sleep(config.SCAN_SETTLE_TIME)

        # --- acquire: WHITE, UV and IR --------------------------------
        repeats = self._requested_repeats(request, config.SAMPLE_REPEATS)

        try:
            blocks = self.sensor.acquire_triad(repeats)

        except SensorError as error:
            # The sample is at the scanner and the acquisition failed.
            # Put the mechanism back, or say honestly that it could not
            # be put back - never leave a false position on record.
            recovery = self._return_home(home_scan_slot)

            raise self._sensor_error(
                error,
                {
                    "moved": True,
                    "return_move": recovery,
                    "carousel": self.carousel.status(),
                },
            )

        # --- back: 180 deg SCAN -> LOAD -------------------------------
        # The spectra already exist at this point. Whatever the return
        # movement does, it must not cost us that data, so the return is
        # reported as its own outcome rather than raising.
        return_move = self._return_home(home_scan_slot)

        if return_move["returned"] and config.HOME_SETTLE_TIME > 0:
            time.sleep(config.HOME_SETTLE_TIME)

        acquisition = {
            "illuminations": blocks,
            "repeats": repeats,
            "sensor_settings": self.sensor.settings(),
            "temperatures": self._temperatures(),
            "bulbs_off": self._bulbs_off(),
            "protocol_version": config.ACQUISITION_PROTOCOL_VERSION,
        }

        measurement = {
            "sample_id": sample_id,
            "slot_id": slot_id,
            "esp_uptime_ms": self._uptime_ms(),
        }
        measurement.update(acquisition)

        slot = self.carousel.mark_occupied(slot_id, sample_id, measurement)

        data = {
            "slot_id": slot_id,
            "sample_id": sample_id,
            "slot": slot,
            "move": move,
            "return_move": return_move,
            "home_restored": return_move["returned"],
            "carousel": self.carousel.status(),
        }
        data.update(acquisition)

        return data

    def _return_home(self, home_scan_slot):
        """
        Swing the sample back to where the measurement started.

        Reports its own outcome instead of raising: by the time this runs
        the spectrum may already exist, and a servo that failed to come
        back must never destroy acquired science.
        """
        try:
            self.carousel.return_selected_to_loader()

        except Exception as error:
            self.carousel.invalidate_position(
                "the return movement after a measurement failed"
            )

            return {
                "returned": False,
                "position_valid": False,
                "message": "The carousel could NOT be returned to the "
                           "loading position. Position tracking has been "
                           "invalidated - re-synchronize before moving "
                           "again.",
                "exception_type": type(error).__name__,
                "exception_message": str(error),
            }

        # The tracked position must be back where it started, or the
        # software and the mechanism disagree and nothing downstream can
        # be trusted.
        if self.carousel.current_scan_slot != home_scan_slot:
            self.carousel.invalidate_position(
                "the carousel did not return to its starting position"
            )

            return {
                "returned": False,
                "position_valid": False,
                "message": "The return movement completed but the tracked "
                           "position does not match the starting position. "
                           "Position tracking has been invalidated - "
                           "re-synchronize before moving again.",
                "expected_scan_slot": home_scan_slot,
            }

        return {
            "returned": True,
            "position_valid": True,
            "message": "The sample is back at the loading position.",
            "scan_slot": self.carousel.current_scan_slot,
            "load_slot": self.carousel.get_load_slot(),
        }

    def handle_sensor_test_raw(self, request):
        """
        Exercise the whole sensor path through the PRODUCTION code, and
        return one new RAW spectrum.

        No carousel movement, no synchronization, no Sample ID, nothing
        saved. Every stage is reported so a failure names the exact step
        that stopped it, and partial results survive.

        There is deliberately no second diagnostic sensor implementation:
        this uses ensure_ready() and acquire_raw_spectrum(), the same two
        calls measure_raw uses.
        """
        checks = []

        def record(stage, ok, detail=None, error=None):
            entry = {"stage": stage, "ok": ok}

            if detail is not None:
                entry["detail"] = detail

            if error is not None:
                entry["error"] = error

            checks.append(entry)

            return entry

        result = {
            "test_only": True,
            "saved": False,
            "bus": as7265x.bus_description(),
            "checks": checks,
            "raw": None,
            "sensor_settings": None,
        }

        # -- sensor lifecycle (bus, scan, address, devices, config) ----
        try:
            driver = self.sensor.ensure_ready(
                force_reinit=bool(request.get("force_reinit"))
            )

        except SensorError as error:
            record(
                "SENSOR_RECOVERY", False,
                detail="Sensor could not be brought up.",
                error=error.as_dict(),
            )

            result["ok"] = False
            result["failed_stage"] = error.stage
            result["sensor"] = self.sensor.status()

            return result

        record(
            "SENSOR_RECOVERY", True,
            detail="recovery count {}".format(self.sensor.recovery_count),
        )
        record(
            "I2C_ADDRESS", True,
            detail="0x{:02X} present on bus {}".format(
                as7265x.ADDRESS, config.I2C_BUS
            ),
        )

        # -- internal devices ------------------------------------------
        try:
            devices = driver.require_devices()
            record("INTERNAL_DEVICES", True, detail=devices)

        except SensorError as error:
            record(
                "INTERNAL_DEVICES", False, error=error.as_dict()
            )

            result["ok"] = False
            result["failed_stage"] = error.stage
            result["sensor"] = self.sensor.status()

            return result

        # -- configuration, read back from the registers ---------------
        try:
            settings = driver.read_configuration()

            currents = driver.read_bulb_currents()
            settings["led_current"] = currents["white"]
            settings["led_current_ma"] = (
                config.SENSOR_LED_CURRENT_NAMES.get(
                    currents["white"], "unknown"
                )
            )
            settings["led_currents"] = currents
            settings["led_currents_ma"] = {
                name: config.SENSOR_LED_CURRENT_NAMES.get(value, "unknown")
                for name, value in currents.items()
            }
            settings["measurement_mode_name"] = (
                config.SENSOR_MEASUREMENT_MODE_NAMES.get(
                    settings["measurement_mode"], "unknown"
                )
            )

            expected_ok = (
                settings["integration_cycles"]
                == config.SENSOR_INTEGRATION_CYCLES
                and settings["gain"] == config.SENSOR_GAIN
                and settings["measurement_mode"]
                == config.SENSOR_MEASUREMENT_MODE
            )

            result["sensor_settings"] = settings

            record(
                "CONFIGURATION", expected_ok,
                detail=settings,
                error=None if expected_ok else {
                    "code": "SENSOR_CONFIG_MISMATCH",
                    "message": "The sensor is not running the settings "
                               "from config.py.",
                    "stage": "CONFIGURATION",
                    "details": {
                        "expected_integration_cycles":
                            config.SENSOR_INTEGRATION_CYCLES,
                        "expected_gain": config.SENSOR_GAIN,
                        "expected_measurement_mode":
                            config.SENSOR_MEASUREMENT_MODE,
                    },
                },
            )

        except SensorError as error:
            record("CONFIGURATION", False, error=error.as_dict())

            result["ok"] = False
            result["failed_stage"] = error.stage
            result["sensor"] = self.sensor.status()

            return result

        # -- illumination: all three lamps, each read back -------------
        try:
            driver.disable_all_bulbs()
            lamp_report = {}

            for name in ("white", "uv", "ir"):
                lamp = as7265x.LAMP_BY_NAME[name]

                driver.set_bulb_current(lamp, as7265x.LAMP_CURRENTS[lamp]())
                driver.enable_bulb(lamp)
                on = driver.bulb_enabled(lamp)

                time.sleep_ms(80)

                driver.disable_bulb(lamp)
                off = not driver.bulb_enabled(lamp)

                lamp_report[name] = {"on": on, "off": off}

            driver.disable_all_bulbs()

            states = driver.bulb_states()
            all_off = not any(states.values())
            lamps_ok = all(
                entry["on"] and entry["off"]
                for entry in lamp_report.values()
            )

            record(
                "ILLUMINATION", lamps_ok and all_off,
                detail={"lamps": lamp_report, "all_off": all_off},
                error=None if (lamps_ok and all_off) else {
                    "code": "LED_STATE_NOT_APPLIED",
                    "message": "One or more lamps did not read back the "
                               "requested state.",
                    "stage": "ILLUMINATION",
                    "details": {"lamps": lamp_report, "final": states},
                },
            )

        except SensorError as error:
            record("ILLUMINATION", False, error=error.as_dict())

            try:
                driver.disable_all_bulbs()

            except Exception:
                pass

            result["ok"] = False
            result["failed_stage"] = error.stage
            result["sensor"] = self.sensor.status()

            return result

        # -- WHITE, UV and IR acquisition ------------------------------
        repeats = self._requested_repeats(request, config.SAMPLE_REPEATS)

        try:
            blocks = self.sensor.acquire_triad(repeats)

        except SensorError as error:
            record("ACQUISITION", False, error=error.as_dict())

            result["ok"] = False
            result["failed_stage"] = error.stage
            result["sensor"] = self.sensor.status()

            return result

        record(
            "ACQUISITION", True,
            detail="3 illuminations x {} repeats, 18/18 channels "
                   "each".format(repeats),
        )

        result["illuminations"] = blocks
        result["repeats"] = repeats
        result["temperatures"] = self._temperatures()
        result["bulbs_off"] = self._bulbs_off()
        result["protocol_version"] = config.ACQUISITION_PROTOCOL_VERSION
        result["ok"] = True
        result["failed_stage"] = None
        result["sensor"] = self.sensor.status()

        return result
