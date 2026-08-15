# main.py
#
# Command-controlled hardware module for the Brno Mars Rover / Freya.
#
# ESP32 responsibilities:
#   - carousel / MG995 control
#   - AS7265x initialization and recovery
#   - raw 18-channel spectral acquisition
#   - hardware/status responses
#
# Scientific processing, White/Dark normalization, database comparison
# and persistent Sample storage run on the PC. This firmware never
# learns what material a sample resembles: it moves hardware, reads
# hardware and reports hardware.
#
# Transport:
#   newline-delimited JSON over USB / CP2102 using sys.stdin/sys.stdout.
#   stdout IS the protocol stream, so nothing here may print outside a
#   JSON frame.
#
# Deployment and terminal instructions:
#   see ../OPERATIONS.md

import json
import sys
import time

import as7265x
import config
import mg995

from as7265x import SensorError, SensorRuntime
from carousel import Carousel, CarouselError
from mg995 import CCW, CW, MG995


class CommandError(Exception):
    """
    Rejected command carrying a machine-readable code for the PC.

    super().__init__ - MicroPython has no unbound Exception.__init__.
    """

    def __init__(self, code, message, data=None):
        super().__init__(message)

        self.code = str(code)
        self.message = str(message)
        self.data = data


def _debug(*parts):
    """Diagnostic output, silent unless config.DEBUG is switched on by hand."""
    if config.DEBUG:
        print(*parts)


# ======================================================================
# transport: newline-delimited JSON over the USB serial console
# ======================================================================

def read_command():
    """Block for one line; None means stdin reported end of input."""
    line = sys.stdin.readline()

    if not line:
        return None

    return line.strip()


def make_json_safe(value, depth=0):
    """
    Coerce a payload into JSON-encodable primitives.

    Numbers stay numbers - spectral values must never be stringified.
    Anything genuinely unsupported (an exception, a driver, a PWM) is
    replaced by its type name rather than breaking the response.
    """
    if depth > 12:
        return "<nested too deeply>"

    if value is None or isinstance(value, bool):
        return value

    if isinstance(value, (int, float, str)):
        return value

    if isinstance(value, dict):
        safe = {}

        for key in value:
            # MicroPython's json refuses non-string keys outright.
            safe_key = key if isinstance(key, str) else str(key)
            safe[safe_key] = make_json_safe(value[key], depth + 1)

        return safe

    if isinstance(value, (list, tuple)):
        return [make_json_safe(item, depth + 1) for item in value]

    return "<{}>".format(type(value).__name__)


def _ascii_only(text):
    """
    Escape non-ASCII as \\uXXXX.

    Keeps one byte per character, which is what makes the partial-write
    accounting in _write_all exact. \\uXXXX is valid JSON.
    """
    for character in text:
        if ord(character) > 127:
            break
    else:
        return text

    out = []

    for character in text:
        if ord(character) > 127:
            out.append("\\u{:04x}".format(ord(character)))
        else:
            out.append(character)

    return "".join(out)


def _write_all(text):
    """
    Write the whole string, however many calls that takes.

    sys.stdout on the USB console is non-blocking: one write() of a
    multi-kilobyte response does NOT necessarily send everything, and
    the unwritten tail is silently dropped - leaving the PC waiting
    forever for a line terminator. Honour the return value.
    """
    total = len(text)
    sent = 0

    while sent < total:
        chunk = text[sent:sent + config.STDOUT_CHUNK_BYTES]

        written = sys.stdout.write(chunk)

        if written is None:
            # Some ports return None and always write everything.
            written = len(chunk)

        if written <= 0:
            # Buffer full; give the USB stack a moment to drain.
            time.sleep_ms(2)

            continue

        sent += written

    try:
        sys.stdout.flush()

    except AttributeError:
        # Not every MicroPython build exposes flush() on the console.
        pass


def send_json(payload):
    """
    The single exit point for every response.

    Serialization completes IN FULL before a byte is written, so a value
    that cannot be encoded can never leave half an object on the wire.
    """
    try:
        text = json.dumps(payload)

    except (TypeError, ValueError):
        try:
            text = json.dumps(make_json_safe(payload))

        except Exception as error:
            text = json.dumps({
                "request_id": payload.get("request_id")
                if isinstance(payload, dict) else None,
                "ok": False,
                "error": {
                    "code": "JSON_SERIALIZATION_ERROR",
                    "message": "Response contains a non-serializable "
                               "value.",
                    "exception_type": type(error).__name__,
                    "exception_message": str(error),
                },
            })

    _write_all(_ascii_only(text) + "\n")


# ======================================================================
# hardware module
# ======================================================================

class HardwareModule:
    """Runtime state and command handlers for the ESP32 hardware."""

    def __init__(self):
        self.started_at = time.ticks_ms()

        self.sensor = SensorRuntime()
        self.servo = MG995()
        self.carousel = Carousel(self.servo)

        self.handlers = {
            "ping": self.handle_ping,
            "get_status": self.handle_get_status,

            "sync_position": self.handle_sync_position,
            "select_slot": self.handle_select_slot,
            "move_slots": self.handle_move_slots,
            "fine_adjust": self.handle_fine_adjust,
            "clear_slot": self.handle_clear_slot,
            "clear_all_slots": self.handle_clear_all_slots,

            "measure_raw": self.handle_measure_raw,
            "sensor_test_raw": self.handle_sensor_test_raw,
            "acquire_block": self.handle_acquire_block,
            "acquire_triad": self.handle_acquire_triad,
            "led_test": self.handle_led_test,

            "list_saved_samples": self.handle_list_saved_samples,
            "get_saved_sample": self.handle_get_saved_sample,
            "delete_saved_samples": self.handle_delete_saved_samples,

            "servo_stop": self.handle_servo_stop,
            "get_servo_calibration": self.handle_get_servo_calibration,
            "set_servo_calibration": self.handle_set_servo_calibration,
            "servo_test_move": self.handle_servo_test_move,
        }

    # ------------------------------------------------------------------
    # boot
    # ------------------------------------------------------------------

    def boot(self):
        """
        Bring the hardware up, quietly.

        stdout belongs to the protocol from the moment the board starts,
        so nothing is printed. A sensor that does not answer here does
        not stop anything: the command server, the carousel and status
        all still work, and the next sensor command retries by itself.
        """
        time.sleep(config.STARTUP_DELAY_SECONDS)

        self.sensor.boot()

        _debug("{} {} ready; sensor={}".format(
            config.FIRMWARE_NAME,
            config.FIRMWARE_VERSION,
            self.sensor.ready,
        ))

        return True

    # ------------------------------------------------------------------
    # shared validation
    # ------------------------------------------------------------------

    def _require_slot(self, request):
        if "slot" not in request:
            raise CommandError(
                "MISSING_FIELD",
                "Command requires a 'slot' field (1 to {}).".format(
                    config.CAROUSEL_SLOT_COUNT
                ),
            )

        return self.carousel.validate_slot(request.get("slot"))

    def _sensor_error(self, error, extra=None):
        """Turn a SensorError into a CommandError with the same detail."""
        data = error.as_dict()

        if extra:
            data.update(extra)

        return CommandError(error.code, error.message, data=data)

    # ------------------------------------------------------------------
    # basic commands
    # ------------------------------------------------------------------

    def _uptime_ms(self):
        return time.ticks_diff(time.ticks_ms(), self.started_at)

    def handle_ping(self, request):
        return {
            "pong": True,
            "firmware": config.FIRMWARE_NAME,
            "version": config.FIRMWARE_VERSION,
            "uptime_ms": self._uptime_ms(),
        }

    def handle_get_status(self, request):
        """Hardware state only. Nothing scientific is known here."""
        return {
            "firmware": config.FIRMWARE_NAME,
            "version": config.FIRMWARE_VERSION,
            "uptime_ms": self._uptime_ms(),
            "transport": "usb-serial-console",

            "sensor": self.sensor.status(),
            "carousel": self.carousel.status(),
            "slots": self.carousel.slot_summary(),
            "servo": self.servo.status(),
        }

    # ------------------------------------------------------------------
    # carousel commands
    # ------------------------------------------------------------------

    def handle_sync_position(self, request):
        """
        Establish the carousel origin. This never moves anything.

        Normal use is 'load_slot': the operator physically aligns Slot 1
        with the soil loading hole and confirms it, making that position
        the origin. 'scan_slot' is accepted for callers that prefer to
        declare the scanner side.
        """
        if "load_slot" in request:
            status = self.carousel.sync_to_load_slot(request.get("load_slot"))

        elif "scan_slot" in request:
            status = self.carousel.sync_position(request.get("scan_slot"))

        else:
            raise CommandError(
                "MISSING_FIELD",
                "sync_position requires 'load_slot' (the slot now under "
                "the loading hole) or 'scan_slot' (1 to {}).".format(
                    config.CAROUSEL_SLOT_COUNT
                ),
            )

        return {"synchronized": True, "carousel": status}

    def handle_select_slot(self, request):
        """
        Choose the physical slot to work with and bring it to the loader.

        If the previous sample is still at the scanner the firmware
        restores the loading orientation first, so the operator never
        runs the 180 degree return by hand.
        """
        slot_id = self._require_slot(request)

        move = self.carousel.select_slot(slot_id)

        sample_id = request.get("sample_id")

        if sample_id is not None:
            # Correlation only: the PC owns the scientific identity.
            self.carousel.slots[slot_id]["sample_id"] = str(sample_id)

        return {
            "selected_slot": slot_id,
            "move": move,
            "slot": self.carousel.slots[slot_id],
            "carousel": self.carousel.status(),
        }

    def handle_move_slots(self, request):
        """
        Whole-slot movement: exactly 90 degrees per slot.

        Works before synchronization, so the operator can line Slot 1 up
        with the loading hole during the sync procedure.
        """
        direction = request.get("direction", CW)

        if direction not in (CW, CCW):
            raise CommandError(
                "BAD_REQUEST",
                "direction must be '{}' or '{}'.".format(CW, CCW),
            )

        try:
            slots = int(request.get("slots", 1))

        except (TypeError, ValueError):
            raise CommandError("BAD_REQUEST", "'slots' must be a whole number.")

        if slots < 0 or slots > config.CAROUSEL_SLOT_COUNT:
            raise CommandError(
                "BAD_REQUEST",
                "'slots' must be between 0 and {}.".format(
                    config.CAROUSEL_SLOT_COUNT
                ),
            )

        move = self.carousel.move_slots(direction, slots)

        return {
            "move": move,
            "degrees": slots * config.SLOT_STEP_DEG,
            "carousel": self.carousel.status(),
        }

    def handle_fine_adjust(self, request):
        """
        Small mechanical correction in degrees; positive is clockwise.

        Alignment only - the logical slot numbering is left alone.
        """
        if "degrees" not in request:
            raise CommandError(
                "MISSING_FIELD",
                "fine_adjust requires 'degrees' (positive = clockwise).",
            )

        try:
            degrees = float(request.get("degrees"))

        except (TypeError, ValueError):
            raise CommandError("BAD_REQUEST", "'degrees' must be a number.")

        limit = config.MAX_FINE_ADJUST_DEG

        if abs(degrees) > limit:
            raise CommandError(
                "FINE_ADJUST_TOO_LARGE",
                "Fine adjustment is limited to +/-{:.0f} degrees. Use "
                "whole-slot movement for larger movements.".format(limit),
            )

        return {
            "adjustment": self.carousel.fine_adjust(degrees),
            "carousel": self.carousel.status(),
        }

    def handle_clear_slot(self, request):
        """
        Free a physical slot for reuse.

        Physical state only. The PC's persistent scientific record for
        whatever was in the slot is untouched and still exists.
        """
        slot_id = self._require_slot(request)
        previous = self.carousel.slots[slot_id]

        cleared_sample = previous["sample_id"]
        was_occupied = previous["occupied"]

        slot = self.carousel.reset_slot(slot_id)

        return {
            "slot": slot,
            "was_occupied": was_occupied,
            "cleared_sample_id": cleared_sample,
            "carousel": self.carousel.status(),
        }

    def handle_clear_all_slots(self, request):
        """
        Free every physical slot in one operation.

        Physical state only. No saved Sample record is deleted, here or
        on the PC, and the carousel is not moved.
        """
        cleared = self.carousel.reset_all_slots()

        return {
            "cleared_count": len(cleared),
            "cleared": cleared,
            "slots": self.carousel.slot_summary(),
            "carousel": self.carousel.status(),
            "note": "Physical slot state only. Saved Sample records were "
                    "not touched.",
        }

    def handle_servo_stop(self, request):
        result = self.carousel.stop()
        result["carousel"] = self.carousel.status()

        return result

    # ------------------------------------------------------------------
    # retained acquisitions, for the PC to pull back
    # ------------------------------------------------------------------

    def handle_list_saved_samples(self, request):
        """
        Sample IDs whose raw acquisition is still held in RAM.

        Deliberately an index only: one record carries 18 floats plus the
        settings block, and sending several at once is exactly the kind
        of oversized MicroPython response that used to truncate. The PC
        fetches each record individually with get_saved_sample.
        """
        retained = self.carousel.retained_samples()

        return {
            "count": len(retained),
            "samples": [
                {
                    # A measurement taken without a Sample ID still has
                    # to be listable, or it can never be exported or
                    # deleted. Fall back to the slot it came from.
                    "sample_id": (
                        slot["sample_id"]
                        or "SLOT{}".format(slot["slot_id"])
                    ),
                    "has_sample_id": slot["sample_id"] is not None,
                    "slot_id": slot["slot_id"],
                    "occupied": slot["occupied"],
                }
                for slot in retained
            ],
            "storage": "ram_only",
            "note": "Raw acquisitions held since the last reset. The PC "
                    "is the persistent archive.",
        }

    def handle_get_saved_sample(self, request):
        """One retained raw acquisition, exactly as it was acquired."""
        sample_id = request.get("sample_id")

        if not sample_id:
            raise CommandError(
                "MISSING_FIELD",
                "get_saved_sample requires a 'sample_id'.",
            )

        slot = self.carousel.retained_sample(sample_id)

        if slot is None:
            raise CommandError(
                "SAMPLE_NOT_FOUND",
                "No retained acquisition for sample {}.".format(sample_id),
            )

        return {
            "sample_id": sample_id,
            "slot_id": slot["slot_id"],
            "occupied": slot["occupied"],
            "measurement": slot["measurement"],
        }

    def handle_delete_saved_samples(self, request):
        """
        Delete every retained acquisition held on this device.

        Deliberately narrow: it removes ONLY the stored measurements.
        Physical slot occupancy is left exactly as it was, because soil
        can still be sitting in a slot whose record has been exported,
        and the PC archive is a completely separate store that this
        command cannot reach.
        """
        cleared = self.carousel.clear_retained_samples()

        return {
            "deleted_count": len(cleared),
            "deleted": cleared,
            "remaining": len(self.carousel.retained_samples()),
            "slots": self.carousel.slot_summary(),
            "note": "ESP32 acquisitions only. Physical slot state and the "
                    "PC archive were not touched.",
        }

    # ------------------------------------------------------------------
    # servo calibration
    # ------------------------------------------------------------------

    def handle_get_servo_calibration(self, request):
        calibration = self.servo.get_calibration()
        calibration["slot_step_deg"] = config.SLOT_STEP_DEG
        calibration["half_turn_deg"] = config.CAROUSEL_HALF_TURN_DEG

        return calibration

    def handle_set_servo_calibration(self, request):
        """
        Override movement calibration in RAM.

        Runtime only, on purpose: nothing here rewrites config.py. The
        operator converges on real numbers with the carousel in front of
        them, then copies the printed block into config.py so it
        survives a reset.
        """
        if request.get("reset"):
            self.servo.reset_calibration()

            result = self.servo.get_calibration()
            result["changed"] = ["reset to config.py defaults"]

            return result

        values = request.get("values")

        if not isinstance(values, dict):
            raise CommandError(
                "MISSING_FIELD",
                "set_servo_calibration requires a 'values' object, or "
                "'reset': true.",
            )

        try:
            changed = self.servo.set_calibration(values)

        except Exception as error:
            raise CommandError("BAD_REQUEST", str(error))

        result = self.servo.get_calibration()
        result["changed"] = changed

        return result

    def handle_servo_test_move(self, request):
        """
        Run one calibration movement, without touching tracked position.

        These are measurement-free mechanical tests: the operator judges
        the result by eye against a physical reference. Because the
        angle actually travelled is unknown, position tracking is
        invalidated rather than guessed at.
        """
        kind = request.get("kind")

        try:
            repeat = int(request.get("repeat", 1))

        except (TypeError, ValueError):
            raise CommandError("BAD_REQUEST", "'repeat' must be a number.")

        if repeat < 1 or repeat > 8:
            raise CommandError(
                "BAD_REQUEST", "'repeat' must be between 1 and 8."
            )

        calibration = self.servo.calibration

        if kind == "neutral":
            try:
                hold_ms = int(request.get("hold_ms", 5000))

            except (TypeError, ValueError):
                raise CommandError(
                    "BAD_REQUEST", "'hold_ms' must be a number."
                )

            detail = self.servo.hold_neutral(hold_ms)
            detail["kind"] = kind
            detail["repeat"] = 1
            detail["position_invalidated"] = False

            return detail

        # The critical one: a measurement is 180 out plus 180 back, so
        # what actually matters is the error after the PAIR, not after
        # either half. Calibrate the pair to land back home.
        if kind == "out_and_back":
            self.carousel.invalidate_position("servo calibration test move")

            for index in range(repeat):
                self.servo.run_calibration_move(
                    CW, calibration["load_to_scan_ms"]
                )
                self.servo.run_calibration_move(
                    CCW, calibration["scan_to_load_ms"]
                )

            return {
                "kind": kind,
                "repeat": repeat,
                "out_ms": calibration["load_to_scan_ms"],
                "back_ms": calibration["scan_to_load_ms"],
                "total_duration_ms": (
                    calibration["load_to_scan_ms"]
                    + calibration["scan_to_load_ms"]
                ) * repeat,
                "nominal_degrees": 0.0,
                "neutral_us": calibration["neutral_us"],
                "settle_ms": calibration["settle_ms"],
                "position_invalidated": True,
                "note": "Each cycle should end where it started. Measure "
                        "the error at HOME, not at the scanner.",
                "carousel": self.carousel.status(),
            }

        moves = {
            "slot_cw": (CW, calibration["slot_cw_ms"], config.SLOT_STEP_DEG),
            "slot_ccw": (
                CCW, calibration["slot_ccw_ms"], config.SLOT_STEP_DEG
            ),
            "load_to_scan": (
                CW, calibration["load_to_scan_ms"],
                config.CAROUSEL_HALF_TURN_DEG,
            ),
            "scan_to_load": (
                CCW, calibration["scan_to_load_ms"],
                config.CAROUSEL_HALF_TURN_DEG,
            ),
        }

        if kind not in moves:
            raise CommandError(
                "BAD_REQUEST",
                "kind must be one of: neutral, out_and_back, {}.".format(
                    ", ".join(sorted(moves.keys()))
                ),
            )

        direction, duration_ms, degrees = moves[kind]

        # The travelled angle is unknown until the operator measures it,
        # so any tracked position becomes a guess.
        self.carousel.invalidate_position("servo calibration test move")

        result = self.servo.run_calibration_move(
            direction, duration_ms, repeat
        )

        result["kind"] = kind
        result["nominal_degrees"] = degrees * repeat
        result["target_degrees"] = degrees
        result["speed_deg_per_s"] = mg995.angular_speed(duration_ms, degrees)
        result["position_invalidated"] = True
        result["carousel"] = self.carousel.status()

        return result

    # ------------------------------------------------------------------
    # spectral acquisition
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

        return {
            "illuminations": blocks,
            "repeats": repeats,
            "sensor_settings": self.sensor.settings(),
            "temperatures": self._temperatures(),
            "bulbs_off": self._bulbs_off(),
            "protocol_version": config.ACQUISITION_PROTOCOL_VERSION,
        }

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

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------

    def dispatch_command(self, request):
        """Run one command and build exactly one response object."""
        request_id = None
        cmd = None

        try:
            if not isinstance(request, dict):
                raise CommandError(
                    "BAD_REQUEST", "A command must be a JSON object."
                )

            request_id = request.get("request_id")
            cmd = request.get("cmd")

            if not cmd:
                raise CommandError(
                    "MISSING_FIELD", "A command must contain a 'cmd' field."
                )

            handler = self.handlers.get(cmd)

            if handler is None:
                raise CommandError(
                    "UNKNOWN_COMMAND",
                    "Unknown command '{}'. Known commands: {}.".format(
                        cmd, ", ".join(sorted(self.handlers.keys()))
                    ),
                )

            return {
                "request_id": request_id,
                "ok": True,
                "cmd": cmd,
                "data": handler(request),
            }

        except CommandError as error:
            response = {
                "request_id": request_id,
                "ok": False,
                "cmd": cmd,
                "error": {"code": error.code, "message": error.message},
            }

            if error.data is not None:
                response["data"] = error.data

            return response

        except SensorError as error:
            return {
                "request_id": request_id,
                "ok": False,
                "cmd": cmd,
                "error": error.as_dict(),
            }

        except CarouselError as error:
            return {
                "request_id": request_id,
                "ok": False,
                "cmd": cmd,
                "error": {"code": error.code, "message": error.message},
            }

        except Exception as error:
            # An unexpected fault must still produce a well-formed
            # answer, otherwise the PC blocks waiting for a reply.
            # type(error).__name__ - never type(error) itself, which is
            # a class object and cannot be serialized.
            _debug("internal error in '{}': {}".format(cmd, error))

            return {
                "request_id": request_id,
                "ok": False,
                "cmd": cmd,
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": str(error),
                    "exception_type": type(error).__name__,
                    "exception_message": str(error),
                },
            }

    def process_command(self, line):
        """Parse one received line and answer it with exactly one frame."""
        if len(line) > config.MAX_COMMAND_BYTES:
            send_json({
                "request_id": None,
                "ok": False,
                "error": {
                    "code": "COMMAND_TOO_LONG",
                    "message": "Command exceeded {} bytes.".format(
                        config.MAX_COMMAND_BYTES
                    ),
                },
            })

            return

        try:
            request = json.loads(line)

        except (ValueError, TypeError):
            send_json({
                "request_id": None,
                "ok": False,
                "error": {
                    "code": "INVALID_JSON",
                    "message": "Command is not valid JSON.",
                },
            })

            return

        send_json(self.dispatch_command(request))

    def command_loop(self):
        """
        Wait for commands and execute them, one at a time, forever.

        There is no automatic activity of any kind: the module does
        nothing at all until the main computer asks for something.
        """
        while True:
            line = read_command()

            if not line:
                # Empty line or end of input. Pause briefly so a closed
                # console cannot turn this into a busy loop.
                time.sleep_ms(config.IDLE_DELAY_MS)

                continue

            try:
                self.process_command(line)

            except Exception as error:
                # process_command answers its own errors; reaching here
                # means the transport itself failed. Keep serving rather
                # than dropping to the REPL.
                _debug("command loop error:", error)

    def run(self):
        self.boot()

        try:
            self.command_loop()

        except KeyboardInterrupt:
            _debug("command loop stopped by user")

        finally:
            try:
                self.servo.release()

            except Exception as error:
                _debug("could not release servo PWM:", error)


# Module-level handle so the running instance can be inspected from the
# REPL after Ctrl+C:  import main; main.module.carousel.status()
module = None


def main():
    global module

    module = HardwareModule()
    module.run()


# MicroPython executes main.py automatically after boot/reset, and runs
# it as __main__, so this starts the command server on the device. The
# guard is what lets the test harness import the handlers without also
# starting a loop that blocks on stdin forever.
if __name__ == "__main__":
    main()
