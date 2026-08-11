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

            "measure_raw": self.handle_measure_raw,
            "sensor_test_raw": self.handle_sensor_test_raw,

            "servo_stop": self.handle_servo_stop,
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
            "slots": self.carousel.slot_list(),
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
        Whole-slot movement: exactly 45 degrees per slot.

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

    def handle_servo_stop(self, request):
        result = self.carousel.stop()
        result["carousel"] = self.carousel.status()

        return result

    # ------------------------------------------------------------------
    # spectral acquisition
    # ------------------------------------------------------------------

    def _raw_payload(self, acquisition):
        """The RAW block every acquisition command returns."""
        return {
            "raw": acquisition["spectrum"],
            "data_ready_wait_ms": acquisition["data_ready_wait_ms"],
            "zero_channels": acquisition["zero_channels"],
            "sensor_settings": self.sensor.settings(),
        }

    def handle_measure_raw(self, request):
        """
        Move the selected slot to the scanner and acquire one RAW
        spectrum.

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

        try:
            acquisition = self.sensor.acquire_raw_spectrum()

        except SensorError as error:
            # The sample is at the scanner and the acquisition failed.
            # Put the mechanism back, or say honestly that it could not
            # be put back - never leave a false position on record.
            recovery = self._recover_to_loader()

            raise self._sensor_error(
                error,
                {
                    "moved": True,
                    "recovery": recovery,
                    "carousel": self.carousel.status(),
                },
            )

        slot = self.carousel.mark_occupied(slot_id, sample_id)

        data = {
            "slot_id": slot_id,
            "sample_id": sample_id,
            "slot": slot,
            "move": move,
            "carousel": self.carousel.status(),
        }
        data.update(self._raw_payload(acquisition))

        return data

    def _recover_to_loader(self):
        """Swing back after a failed measurement, truthfully."""
        try:
            self.carousel.return_selected_to_loader()

            return {
                "returned": True,
                "message": "Carousel returned to the loading position.",
            }

        except Exception as error:
            self.carousel.invalidate_position(
                "recovery after a failed measurement failed"
            )

            return {
                "returned": False,
                "message": "Carousel could NOT be returned; position "
                           "tracking has been invalidated.",
                "exception_type": type(error).__name__,
                "exception_message": str(error),
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
            settings["led_current"] = driver.read_led_current()
            settings["led_current_ma"] = (
                config.SENSOR_LED_CURRENT_NAMES.get(
                    settings["led_current"], "unknown"
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

        # -- illumination ----------------------------------------------
        try:
            driver.set_led(True)
            time.sleep_ms(100)
            driver.set_led(False)

            record("ILLUMINATION", True, detail="white LED on -> off")

        except SensorError as error:
            record("ILLUMINATION", False, error=error.as_dict())

            result["ok"] = False
            result["failed_stage"] = error.stage
            result["sensor"] = self.sensor.status()

            return result

        # -- one new 18-channel acquisition ----------------------------
        try:
            acquisition = self.sensor.acquire_raw_spectrum()

        except SensorError as error:
            record("ACQUISITION", False, error=error.as_dict())

            result["ok"] = False
            result["failed_stage"] = error.stage
            result["sensor"] = self.sensor.status()

            return result

        record(
            "ACQUISITION", True,
            detail="18/18 channels, DATA_READY after {} ms".format(
                acquisition["data_ready_wait_ms"]
            ),
        )

        result.update(self._raw_payload(acquisition))
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
