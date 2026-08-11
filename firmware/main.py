# main.py
# Command-controlled science module for the Brno Mars Rover / Freya.
#
# MicroPython runs this file automatically after every power-on or reset.
# It deliberately does NOT move the servo, measure anything, or cycle
# through anything on its own. The ESP32 is a passive instrument; the
# main computer is the mission controller.
#
#     boot
#      -> initialize hardware
#      -> load fixed white/dark references
#      -> load reference material database
#      -> initialize 8 empty slots, position unknown
#      -> wait for a command on the USB serial console
#      -> execute exactly that command
#      -> answer with one JSON object
#      -> wait again
#
# Transport
# ---------
# The main computer talks to this firmware over the ESP32 development
# board's USB / CP2102 serial console: commands arrive on sys.stdin,
# responses leave on sys.stdout, one newline-delimited JSON object each
# way. The same cable also powers the board.
#
# No machine.UART peripheral is created. The dedicated PCB UART2 /
# JST-XH connector on GPIO16/GPIO17 is unused and reserved.
#
# Because stdout IS the protocol stream, this file must not print
# anything outside the JSON protocol. Diagnostics go through _debug(),
# which stays silent unless config.DEBUG is switched on by hand.
r'''
Firmware upload (PowerShell, adjust COM port):
   py -m mpremote connect COM4 fs cp boot.py :boot.py
   py -m mpremote connect COM4 fs cp main.py :main.py
   py -m mpremote connect COM4 fs cp config.py :config.py
   py -m mpremote connect COM4 fs cp as7265x.py :as7265x.py
   py -m mpremote connect COM4 fs cp mg995.py :mg995.py
   py -m mpremote connect COM4 fs cp carousel.py :carousel.py
   py -m mpremote connect COM4 fs cp database.py :database.py
   py -m mpremote connect COM4 fs cp database.json :database.json
   py -m mpremote connect COM4 fs cp references.json :references.json
   py -m mpremote connect COM4 fs cp sample_store.py :sample_store.py
   py -m mpremote connect COM4 fs cp sample_analysis.py :sample_analysis.py
   py -m mpremote connect COM4 fs cp sensor_diag.py :sensor_diag.py
  

Reloading the firmware (PowerShell, adjust COM port):
    py -m mpremote connect COM4 reset

Run from the REPL (PowerShell, adjust COM port):
    py ..\host\rover_science_client.py --port COM4

Add Samples BD:
py -m mpremote connect COM4 fs ls
py -m mpremote connect COM4 fs cp :samples.json samples.json

'''

#
# Do NOT upload samples.json: it is competition data created on the
# device and would be overwritten.

import json
import sys
import time

import config
import sample_analysis as analysis

from as7265x import AS7265X, AS7265X_Driver, SoilMeasurementSystem
from carousel import (
    Carousel,
    CarouselError,
    STATE_EMPTY,
    STATE_LOADED,
    STATE_MEASURED,
    STATE_READY_TO_LOAD
)
from database import MaterialDatabase
from mg995 import CCW, CW, MG995
import sensor_diag

from sample_store import (
    SampleStore,
    StorageError,
    validate_sample_id
)


# Mission metadata the PC may attach. Anything the firmware can know by
# itself is filled in automatically and never taken from these fields.
METADATA_FIELDS = (
    "task",
    "hypothesis",
    "operator",
    "location",
    "map_point",
    "note",
    "photo",
    "sensor_distance_mm"
)

# The nine stages of measure_sample, reported back to the PC in order so
# a failure names the exact step that stopped the pipeline.
MEASUREMENT_STAGES = (
    "VALIDATE_SLOT",
    "MOVE_TO_SCANNER",
    "MECHANICAL_SETTLE",
    "SENSOR_READ",
    "LOAD_REFERENCES",
    "NORMALIZE",
    "DATABASE_COMPARISON",
    "SAVE_SAMPLE",
    "UPDATE_SLOT_STATE"
)

MEASUREMENT_STAGE_COUNT = len(MEASUREMENT_STAGES)

CALIBRATION_HELP = (
    "The science module uses one fixed Dark Reference and one fixed "
    "White Reference stored in references.json. They were measured "
    "before the competition and are loaded automatically when the ESP32 "
    "starts. You do NOT need to perform dark or white measurements "
    "before measuring a soil sample."
)


class CommandError(Exception):
    """Rejected command carrying a machine-readable code for the PC."""

    def __init__(self, code, message, data=None):
        Exception.__init__(self, message)

        self.code = code
        self.message = message
        self.data = data


def _debug(*parts):
    """
    Diagnostic output, silent by default.

    stdout is the protocol stream, so this must stay off whenever the
    main computer is attached. It exists for manual REPL work only.
    """
    if config.DEBUG:
        print(*parts)


# ======================================================================
# transport: newline-delimited JSON over the USB serial console
# ======================================================================

def read_command():
    """
    Block until the host sends one line, then return it stripped.

    Returns None when stdin reports end of input, which the command loop
    treats as "nothing to do" rather than as a fatal condition.
    """
    line = sys.stdin.readline()

    if not line:
        return None

    return line.strip()


def make_json_safe(value, depth=0):
    """
    Recursively coerce a payload into JSON-encodable primitives.

    Numbers stay numbers - scientific values must never be stringified.
    Only genuinely unsupported objects (which would be diagnostic, not
    data) are replaced by a short description of their type.
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
    Escape every non-ASCII character as \\uXXXX.

    Keeps the payload one byte per character, which is what makes the
    partial-write accounting below exact. \\uXXXX is valid JSON, so
    nothing is lost.
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

    sys.stdout on the USB console is a non-blocking stream: a single
    write() of a multi-kilobyte response does NOT necessarily send
    everything, and the unwritten tail is silently dropped. That leaves
    the PC waiting forever for a line terminator - which is exactly the
    "no response within 120 s" failure seen on hardware. Honour the
    return value and keep going until every byte is out.
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
    The single exit point for every protocol response.

    Serialization is completed IN FULL before a single byte is written,
    so a value that cannot be encoded can never leave half an object on
    the wire.
    """
    try:
        text = json.dumps(payload)

    except (TypeError, ValueError):
        # Try again with the payload sanitized, so a stray object costs
        # one field rather than the whole response.
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
                               "value ({}: {}).".format(
                                   type(error).__name__, error
                               ),
                    "stage": "SEND_RESPONSE"
                }
            })

    _write_all(_ascii_only(text) + "\n")


def send_line(response):
    """Backwards-compatible alias for send_json()."""
    send_json(response)


def send_response(request_id, cmd, data):
    """Send one successful result."""
    send_line({
        "request_id": request_id,
        "ok": True,
        "cmd": cmd,
        "data": data
    })


def send_error(request_id, code, message, cmd=None, data=None):
    """Send one machine-readable failure."""
    response = {
        "request_id": request_id,
        "ok": False,
        "cmd": cmd,
        "error": {
            "code": code,
            "message": message
        }
    }

    if data is not None:
        response["data"] = data

    send_line(response)


def make_metadata(*sources):
    """
    Merge metadata dictionaries over a template of known fields.

    Unknown fields are preserved so the PC can attach mission-specific
    context without a firmware change. Anything absent stays null rather
    than being invented.
    """
    result = {}

    for field in METADATA_FIELDS:
        result[field] = None

    for source in sources:
        if not isinstance(source, dict):
            continue

        for key in source:
            result[key] = source[key]

    return result


class ScienceModule:
    """Runtime state and command handlers for the science payload."""

    def __init__(self):
        self.started_at = time.ticks_ms()

        # Sensor
        self.driver = None
        self.sensor = None
        self.sensor_error = None

        # Fixed calibration references
        self.white_reference = None
        self.dark_reference = None
        self.references_ok = False
        self.references_error = None
        self.reference_report = self._blank_reference_report()

        # Reference material database
        self.database = None
        self.database_error = None

        # Mechanism
        self.servo = None
        self.carousel = None

        # Persistent science storage
        self.store = None

        self.handlers = {
            "ping": self.handle_ping,
            "get_status": self.handle_get_status,

            "sync_position": self.handle_sync_position,
            "get_carousel_status": self.handle_get_carousel_status,

            "select_slot": self.handle_select_slot,
            "move_slots": self.handle_move_slots,
            "fine_adjust": self.handle_fine_adjust,

            "prepare_load": self.handle_prepare_load,
            "confirm_loaded": self.handle_confirm_loaded,
            "measure_sample": self.handle_measure_sample,
            "clear_slot": self.handle_clear_slot,

            "get_slots": self.handle_get_slots,

            "list_samples": self.handle_list_samples,
            "get_sample": self.handle_get_sample,
            "update_sample_metadata": self.handle_update_sample_metadata,

            "servo_stop": self.handle_servo_stop,
            "servo_jog_cw": self.handle_servo_jog_cw,
            "servo_jog_ccw": self.handle_servo_jog_ccw,

            "test_measurement": self.handle_test_measurement,
            "sensor_diagnostics": self.handle_sensor_diagnostics,
            "i2c_scan": self.handle_i2c_scan,
            "raw_measurement": self.handle_raw_measurement,

            "get_references": self.handle_get_references,
            "get_material_names": self.handle_get_material_names,
            "get_database_status": self.handle_get_database_status,

            "help": self.handle_help
        }

    # ==================================================================
    # initialization
    # ==================================================================

    def init_sensor(self):
        """
        Bring up the AS7265x.

        A missing or broken sensor must not take the command server down:
        the module still answers on UART and reports the fault through
        get_status, it simply refuses to measure.
        """
        try:
            self.driver = AS7265X_Driver()
            self.sensor = SoilMeasurementSystem(self.driver)

        except Exception as error:
            self.driver = None
            self.sensor = None
            self.sensor_error = "AS7265x initialization failed: {}".format(
                error
            )

            _debug(self.sensor_error)

            return False

        # Re-apply the acquisition settings from config.py. begin()
        # already applies the same values internally; doing it again here
        # makes them tunable and guarantees that what is recorded with
        # each sample is what the sensor actually used.
        try:
            device = self.driver.sensor

            device.set_bulb_current(
                config.ONBOARD_LED_CURRENT,
                AS7265X.LED_WHITE
            )
            device.disable_bulb(AS7265X.LED_WHITE)

            device.set_integration_cycles(
                config.SENSOR_INTEGRATION_CYCLES
            )
            device.set_gain(config.SENSOR_GAIN)

        except Exception as error:
            _debug("could not apply sensor settings:", error)

        self.sensor_error = None

        return True

    def load_references(self):
        """
        Load the single fixed white and dark reference.

        references.json is the canonical calibration for the entire
        competition run and is treated as strictly read-only. The
        references are never re-measured, not at startup and not before
        a sample.
        """
        self.white_reference = None
        self.dark_reference = None
        self.references_ok = False
        self.references_error = None
        self.reference_report = self._blank_reference_report()

        try:
            with open(config.REFERENCES_FILE, "r") as handle:
                data = json.load(handle)

        except OSError:
            self.references_error = "{} not found".format(
                config.REFERENCES_FILE
            )

            _debug(self.references_error)

            return False

        except ValueError:
            self.references_error = "{} is not valid JSON".format(
                config.REFERENCES_FILE
            )

            _debug(self.references_error)

            return False

        if not isinstance(data, dict):
            self.references_error = "{} must contain a JSON object".format(
                config.REFERENCES_FILE
            )

            return False

        white = data.get("white")
        dark = data.get("dark")

        problems = []

        # Report each reference separately and per channel, so a partial
        # calibration file says exactly what is wrong instead of just
        # "invalid".
        for name, values in (("white", white), ("dark", dark)):
            report = self.reference_report[name]

            if values is None:
                report["state"] = "MISSING"
                report["missing"] = list(analysis.CHANNELS)
                report["channels_present"] = 0

                problems.append("missing '{}'".format(name))

                continue

            missing = analysis.validate_channels(values)

            report["missing"] = missing
            report["channels_present"] = len(analysis.CHANNELS) - len(missing)

            if missing:
                report["state"] = "ERROR"

                problems.append(
                    "{} is missing channels {}".format(
                        name,
                        ",".join(missing)
                    )
                )

            else:
                report["state"] = "READY"

        if problems:
            self.references_error = "{}: {}".format(
                config.REFERENCES_FILE,
                "; ".join(problems)
            )

            _debug(self.references_error)

            return False

        self.white_reference = analysis.copy_channels(white)
        self.dark_reference = analysis.copy_channels(dark)
        self.references_ok = True

        return True

    def _blank_reference_report(self):
        """Per-reference channel accounting shown by get_status."""
        return {
            "white": {
                "state": "NOT_LOADED",
                "channels_present": 0,
                "channels_required": len(analysis.CHANNELS),
                "missing": list(analysis.CHANNELS)
            },
            "dark": {
                "state": "NOT_LOADED",
                "channels_present": 0,
                "channels_required": len(analysis.CHANNELS),
                "missing": list(analysis.CHANNELS)
            }
        }

    def calibration_status(self):
        """
        Make it obvious that calibration is fixed and needs no operator
        action. There is exactly one white and one dark reference for the
        whole competition, and nothing can re-measure them at runtime.
        """
        return {
            "mode": "FIXED_STORED_REFERENCES",
            "references_file": config.REFERENCES_FILE,
            "calibration_id": config.CALIBRATION_ID,
            "runtime_recalibration": "DISABLED",
            "loaded": self.references_ok,
            "error": self.references_error,
            "dark": self.reference_report["dark"],
            "white": self.reference_report["white"]
        }

    def install_references(self):
        """Push the fixed references into the measurement system."""
        if self.sensor is None or not self.references_ok:
            return False

        self.sensor.dark_ref = self.dark_reference
        self.sensor.white_ref = self.white_reference

        return True

    def load_database(self):
        """Load the reference material spectra. Never modified here."""
        try:
            self.database = MaterialDatabase(config.DATABASE_FILE)
            self.database_error = None

        except Exception as error:
            self.database = None
            self.database_error = "database load failed: {}".format(error)

            _debug(self.database_error)

            return False

        if not self.database.data:
            self.database_error = "{} is empty or unreadable".format(
                config.DATABASE_FILE
            )

            _debug(self.database_error)

            return False

        return True

    def database_count(self):
        if self.database is None or not self.database.data:
            return 0

        return len(self.database.data)

    def init_mechanism(self):
        """
        Prepare the carousel model.

        The MG995 object is created but its PWM peripheral is not: that
        happens on the first movement command. Nothing drives the servo
        pin during boot, so powering up can never nudge the carousel.
        """
        self.servo = MG995()
        self.carousel = Carousel(self.servo)

        return True

    def init_storage(self):
        self.store = SampleStore()

        return self.store.ready

    def boot(self):
        """
        Bring every subsystem up, quietly.

        Nothing is printed: stdout belongs to the protocol from the
        moment the board starts. A subsystem that fails to initialize
        does not stop the command loop - the fault is recorded and
        reported through get_status, so the main computer can diagnose a
        half-working module instead of facing a silent serial port.
        """
        time.sleep(config.STARTUP_DELAY_SECONDS)

        self.init_mechanism()
        self.init_sensor()

        self.load_references()
        self.install_references()

        self.load_database()
        self.init_storage()

        _debug(
            "{} {} ready; sensor={} refs={} materials={} samples={}".format(
                config.FIRMWARE_NAME,
                config.FIRMWARE_VERSION,
                self.sensor is not None,
                self.references_ok,
                self.database_count(),
                self.store.count()
            )
        )

        return True

    # ==================================================================
    # shared validation
    # ==================================================================

    def _require_slot(self, request):
        if "slot" not in request:
            raise CommandError(
                "MISSING_FIELD",
                "Command requires a 'slot' field (1 to {}).".format(
                    config.CAROUSEL_SLOT_COUNT
                )
            )

        return self.carousel.validate_slot(request.get("slot"))

    def _require_sensor(self):
        if self.sensor is None:
            raise CommandError(
                "SENSOR_NOT_INITIALIZED",
                self.sensor_error or "AS7265x is not available."
            )

    def _require_references(self):
        if not self.references_ok:
            raise CommandError(
                "REFERENCES_NOT_LOADED",
                self.references_error
                or "White/dark references are not loaded."
            )

    def _timestamp(self, request):
        """
        Take the timestamp from the PC.

        The ESP32 has no reliable wall clock, so it never invents one.
        Uptime is recorded separately for correlation.
        """
        return request.get("timestamp")

    # ==================================================================
    # basic commands
    # ==================================================================

    def handle_ping(self, request):
        return {
            "pong": True,
            "firmware": config.FIRMWARE_NAME,
            "version": config.FIRMWARE_VERSION,
            "uptime_ms": time.ticks_diff(
                time.ticks_ms(),
                self.started_at
            )
        }

    def handle_get_status(self, request):
        return {
            "firmware": config.FIRMWARE_NAME,
            "version": config.FIRMWARE_VERSION,
            "uptime_ms": time.ticks_diff(
                time.ticks_ms(),
                self.started_at
            ),

            "sensor_initialized": self.sensor is not None,
            "sensor_error": self.sensor_error,

            "database_loaded": self.database_count() > 0,
            "database_material_count": self.database_count(),
            "database_error": self.database_error,

            "references_loaded": self.references_ok,
            "references_error": self.references_error,

            # Fixed calibration, spelled out so the operator can see at a
            # glance that no white/dark measurement is required.
            "calibration": self.calibration_status(),

            "transport": "usb-serial-console",

            "position_valid": self.carousel.position_valid,
            "current_scan_slot": self.carousel.current_scan_slot,
            "current_load_slot": self.carousel.get_load_slot(),
            "selected_slot": self.carousel.selected_slot,
            "carousel_phase": self.carousel.phase(),

            "saved_sample_count": self.store.count(),
            "sample_store_ready": self.store.ready,
            "storage_error": self.store.error,

            "can_measure": (
                self.sensor is not None and self.references_ok
            ),

            "slots": self.carousel.slot_list()
        }

    # ==================================================================
    # carousel commands
    # ==================================================================

    def handle_sync_position(self, request):
        """
        Establish the carousel origin. This never moves anything.

        Normal use is 'load_slot': the operator physically aligns Slot 1
        with the soil loading hole and confirms it, making that position
        the origin. 'scan_slot' remains accepted for callers that prefer
        to declare the scanner side instead.
        """
        if "load_slot" in request:
            status = self.carousel.sync_to_load_slot(
                request.get("load_slot")
            )

        elif "scan_slot" in request:
            status = self.carousel.sync_position(request.get("scan_slot"))

        else:
            raise CommandError(
                "MISSING_FIELD",
                "sync_position requires 'load_slot' (the slot now under "
                "the loading hole) or 'scan_slot' (1 to {}).".format(
                    config.CAROUSEL_SLOT_COUNT
                )
            )

        return {
            "synchronized": True,
            "carousel": status
        }

    def handle_select_slot(self, request):
        """
        Choose the physical slot to work with and bring it to the loader.

        Physical operation only: this says nothing about the science
        identity of whatever ends up in the slot, which is what
        prepare_load is for.
        """
        slot_id = self._require_slot(request)

        move = self.carousel.select_slot(slot_id)

        return {
            "selected_slot": slot_id,
            "move": move,
            "slot": self.carousel.slots[slot_id],
            "carousel": self.carousel.status()
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
                "direction must be '{}' or '{}'.".format(CW, CCW)
            )

        slots = request.get("slots", 1)

        try:
            slots = int(slots)

        except (TypeError, ValueError):
            raise CommandError(
                "BAD_REQUEST",
                "'slots' must be a whole number."
            )

        if slots < 0 or slots > config.CAROUSEL_SLOT_COUNT:
            raise CommandError(
                "BAD_REQUEST",
                "'slots' must be between 0 and {}.".format(
                    config.CAROUSEL_SLOT_COUNT
                )
            )

        move = self.carousel.move_slots(direction, slots)

        return {
            "move": move,
            "geometric_degrees": slots * config.CAROUSEL_SLOT_GEOMETRY_DEG,
            "degrees": slots * config.SLOT_STEP_DEG,
            "carousel": self.carousel.status()
        }

    def handle_fine_adjust(self, request):
        """
        Small mechanical correction in degrees; positive is clockwise.

        The logical slot numbering is intentionally left alone: this
        aligns the mechanism, it does not advance the carousel.
        """
        if "degrees" not in request:
            raise CommandError(
                "MISSING_FIELD",
                "fine_adjust requires 'degrees' (positive = clockwise)."
            )

        try:
            degrees = float(request.get("degrees"))

        except (TypeError, ValueError):
            raise CommandError(
                "BAD_REQUEST",
                "'degrees' must be a number."
            )

        limit = config.MAX_FINE_ADJUST_DEG

        if abs(degrees) > limit:
            raise CommandError(
                "FINE_ADJUST_TOO_LARGE",
                "Fine adjustment is limited to +/-{:.0f} degrees. Use "
                "whole-slot movement for larger movements.".format(limit)
            )

        result = self.carousel.fine_adjust(degrees)

        return {
            "adjustment": result,
            "carousel": self.carousel.status()
        }

    def handle_get_carousel_status(self, request):
        return self.carousel.status()

    def handle_get_slots(self, request):
        return {
            "slots": self.carousel.slot_list(),
            "carousel": self.carousel.status()
        }

    # ==================================================================
    # sample workflow
    # ==================================================================

    def handle_prepare_load(self, request):
        """
        Assign a Sample ID to a free slot and move it to the loader.

        Sample ID and slot are separate concepts: the scientific identity
        of the soil is not forced to equal the physical slot number.
        """
        slot_id = self._require_slot(request)
        slot = self.carousel.slots[slot_id]

        if "sample_id" not in request:
            raise CommandError(
                "MISSING_FIELD",
                "prepare_load requires a 'sample_id'."
            )

        try:
            sample_id = validate_sample_id(request.get("sample_id"))

        except StorageError as error:
            raise CommandError("INVALID_SAMPLE_ID", str(error))

        if slot["state"] != STATE_EMPTY:
            raise CommandError(
                "SLOT_NOT_EMPTY",
                "Slot {} is {} and holds sample {}. Clear it before "
                "preparing a new sample.".format(
                    slot_id,
                    slot["state"],
                    slot["sample_id"]
                )
            )

        self.carousel.require_position()

        allow_existing = bool(request.get("allow_existing_sample_id"))

        # Re-preparing an ID that was never measured is fine - the
        # operator may simply be redoing a load. Re-using the ID of a
        # sample that already has spectral data is not.
        if (
            not allow_existing
            and self.store.get_state(sample_id) == STATE_MEASURED
        ):
            raise CommandError(
                "DUPLICATE_SAMPLE_ID",
                "Sample ID {} is already stored as a measured sample. "
                "Choose another ID, or resend with "
                "allow_existing_sample_id.".format(sample_id)
            )

        occupied_slot = self.carousel.find_slot_by_sample(sample_id)

        if occupied_slot is not None:
            raise CommandError(
                "DUPLICATE_SAMPLE_ID",
                "Sample ID {} is already assigned to slot {}.".format(
                    sample_id,
                    occupied_slot["slot_id"]
                )
            )

        timestamp = self._timestamp(request)

        # Assign first so the move has a meaning, then roll the
        # assignment back if the mechanism fails.
        slot["sample_id"] = sample_id
        slot["created_at"] = timestamp
        slot["metadata"] = make_metadata(request.get("metadata"))

        try:
            move = self.carousel.move_slot_to_load(slot_id)

        except CarouselError:
            self.carousel.reset_slot(slot_id)

            raise

        # This slot has just been brought to the loading hole, so it is
        # now the one being worked with.
        self.carousel.selected_slot = slot_id

        slot["state"] = STATE_READY_TO_LOAD

        # Open the persistent scientific record now. Measurement later
        # completes THIS record rather than creating a second one, so a
        # sample is one coherent object from preparation to analysis.
        stored = self._persist_sample(slot, STATE_READY_TO_LOAD)

        return {
            "slot": slot,
            "move": move,
            "sample_record_created": stored,
            "carousel": self.carousel.status(),
            "message": (
                "Slot {} (sample {}) is at the loading position.".format(
                    slot_id,
                    sample_id
                )
            )
        }

    def _persist_sample(self, slot, state):
        """
        Create or update the persistent record for a slot's sample.

        Used by prepare_load and confirm_loaded to keep one record per
        Sample ID through its whole lifecycle. Storage problems here are
        reported but never block the physical workflow: the operator can
        still load and measure, and the measurement save will surface any
        real filesystem fault.
        """
        sample_id = slot.get("sample_id")

        if not sample_id:
            return False

        record = self.store.get_sample(sample_id) or {}

        record["sample_id"] = sample_id
        record["slot_id"] = slot["slot_id"]
        record["state"] = state
        record["calibration_id"] = config.CALIBRATION_ID

        timestamps = record.get("timestamps")

        if not isinstance(timestamps, dict):
            timestamps = {}

        if slot.get("created_at"):
            timestamps["created_at"] = slot["created_at"]

        if slot.get("loaded_at"):
            timestamps["loaded_at"] = slot["loaded_at"]

        record["timestamps"] = timestamps

        # Preparation metadata is merged, never replaced: anything added
        # to the stored record in the meantime survives.
        record["metadata"] = make_metadata(
            record.get("metadata"),
            slot.get("metadata")
        )

        try:
            self.store.add_sample(record)

            return True

        except (StorageError, OSError) as error:
            _debug("could not persist sample record:", error)

            return False

    def handle_confirm_loaded(self, request):
        """Record that the external arm has deposited soil in the slot."""
        slot_id = self._require_slot(request)
        slot = self.carousel.slots[slot_id]

        if slot["state"] == STATE_EMPTY:
            raise CommandError(
                "SLOT_EMPTY",
                "Slot {} has no assigned sample. Send prepare_load "
                "first.".format(slot_id)
            )

        if slot["state"] != STATE_READY_TO_LOAD:
            raise CommandError(
                "SLOT_NOT_READY",
                "Slot {} is {}, not {}.".format(
                    slot_id,
                    slot["state"],
                    STATE_READY_TO_LOAD
                )
            )

        slot["state"] = STATE_LOADED
        slot["occupied"] = True
        slot["loaded_at"] = self._timestamp(request)

        # Same record, next lifecycle state.
        stored = self._persist_sample(slot, STATE_LOADED)

        return {
            "slot": slot,
            "sample_record_updated": stored,
            "carousel": self.carousel.status()
        }

    def handle_measure_sample(self, request):
        """
        One command: move, settle, acquire, normalize, compare, save.

        The white and dark references are the fixed ones loaded from
        references.json. Neither is re-measured here.
        """
        slot_id = self._require_slot(request)
        slot = self.carousel.slots[slot_id]

        sample_id = slot["sample_id"]
        timestamp = self._timestamp(request)

        # Every stage records what it did. The list travels back with the
        # response - success or failure - so the operator can see exactly
        # how far the pipeline got and where it stopped.
        stages = []

        def done(name, detail=None):
            stages.append({
                "index": len(stages) + 1,
                "total": MEASUREMENT_STAGE_COUNT,
                "stage": name,
                "ok": True,
                "detail": detail
            })

        # Set once the carousel has swung out to the scanner, so a later
        # failure can put it back.
        swung_to_scanner = [False]

        def fail(name, code, message, extra=None):
            stages.append({
                "index": len(stages) + 1,
                "total": MEASUREMENT_STAGE_COUNT,
                "stage": name,
                "ok": False,
                "detail": message
            })

            # A failed measurement must leave the mechanism where it
            # started. Otherwise the sample is stranded at the scanner
            # while its slot is still LOADED, and the retry would be
            # refused for not being at the loading hole.
            recovered = None

            if swung_to_scanner[0]:
                try:
                    self.carousel.return_selected_to_loader()
                    recovered = "carousel returned to the loading position"

                except Exception as recovery_error:
                    self.carousel.invalidate_position(
                        "recovery after failed measurement failed"
                    )

                    recovered = (
                        "carousel could NOT be returned ({}); position is "
                        "now unsynchronized".format(recovery_error)
                    )

                stages.append({
                    "index": len(stages) + 1,
                    "total": MEASUREMENT_STAGE_COUNT,
                    "stage": "RECOVER_POSITION",
                    "ok": recovered.startswith("carousel returned"),
                    "detail": recovered
                })

            data = {
                "sample_id": sample_id,
                "slot_id": slot_id,
                "failed_stage": name,
                "stages": stages,
                "saved": False,
                "slot_state": slot["state"],
                "message": (
                    "Slot {} remains {}; no MEASURED state was "
                    "written.".format(slot_id, slot["state"])
                ),
                "recovery": recovered
            }

            if extra:
                data.update(extra)

            raise CommandError(code, message, data=data)

        # --- [1/10] VALIDATE_SLOT -------------------------------------
        # Only a confirmed, loaded slot may be measured. Everything else
        # is refused here, before the mechanism is touched.
        if slot["state"] == STATE_EMPTY:
            fail(
                "VALIDATE_SLOT",
                "SLOT_EMPTY",
                "Slot {} is empty; there is nothing to measure.".format(
                    slot_id
                )
            )

        if slot["state"] == STATE_READY_TO_LOAD:
            fail(
                "VALIDATE_SLOT",
                "SLOT_NOT_LOADED",
                "Slot {} does not contain a confirmed sample. Send "
                "confirm_loaded once the arm has deposited soil.".format(
                    slot_id
                )
            )

        if slot["state"] == STATE_MEASURED:
            fail(
                "VALIDATE_SLOT",
                "SLOT_ALREADY_MEASURED",
                "Slot {} (sample {}) has already been measured. Clear "
                "the slot to reuse it.".format(
                    slot_id,
                    sample_id
                )
            )

        if slot["state"] != STATE_LOADED:
            fail(
                "VALIDATE_SLOT",
                "SLOT_NOT_LOADED",
                "Slot {} is {}.".format(slot_id, slot["state"])
            )

        # Measurement operates ONLY on the currently selected slot, and
        # only while that slot is physically under the loading hole.
        # This is what keeps the software's idea of the carousel and the
        # real mechanism from drifting apart.
        if self.carousel.selected_slot != slot_id:
            fail(
                "VALIDATE_SLOT",
                "SLOT_NOT_SELECTED",
                "Slot {} is not the selected slot (Slot {} is). Use "
                "select_slot to bring Slot {} to the loading position "
                "first.".format(
                    slot_id,
                    self.carousel.selected_slot,
                    slot_id
                ),
                {"selected_slot": self.carousel.selected_slot}
            )

        phase = self.carousel.phase()

        if phase != "LOAD":
            fail(
                "VALIDATE_SLOT",
                "SLOT_NOT_AT_LOADER",
                "Slot {} is not at the loading position (carousel phase "
                "is {}). Measurement starts from the loading hole and "
                "swings the sample to the scanner.".format(slot_id, phase),
                {"carousel_phase": phase}
            )

        # Refuse before touching the mechanism if the module cannot
        # actually complete a measurement.
        if self.sensor is None:
            fail(
                "VALIDATE_SLOT",
                "SENSOR_NOT_INITIALIZED",
                self.sensor_error or "AS7265x is not available."
            )

        if not self.references_ok:
            fail(
                "VALIDATE_SLOT",
                "REFERENCES_NOT_LOADED",
                self.references_error
                or "Fixed white/dark references are not loaded.",
                {"calibration": self.calibration_status()}
            )

        done(
            "VALIDATE_SLOT",
            "slot {} state = {}, sample {}".format(
                slot_id,
                slot["state"],
                sample_id
            )
        )

        # --- [2/10] MOVE_TO_SCANNER ----------------------------------
        # The slot being measured is the one being worked with.
        self.carousel.selected_slot = slot_id

        from_slot = self.carousel.current_scan_slot
        at_loader = self.carousel.get_load_slot() == slot_id
        half_turn_used = False

        try:
            if self.carousel.current_scan_slot == slot_id:
                # Already facing the scanner; nothing to do.
                move = {"steps": 0, "direction": None, "moved": False}

            elif at_loader:
                # The normal path: one calibrated 180 degree sweep, using
                # its own timing rather than four adjacent-slot moves.
                move = self.carousel.move_selected_to_scanner()
                half_turn_used = True

            else:
                # Unusual case - measuring a slot that is neither at the
                # loader nor the scanner. Fall back to slot stepping.
                move = self.carousel.move_slot_to_scan(slot_id)

        except CarouselError as error:
            fail(
                "MOVE_TO_SCANNER",
                error.code,
                error.message,
                {"carousel": self.carousel.status()}
            )

        if half_turn_used:
            swung_to_scanner[0] = True

        if half_turn_used:
            detail = (
                "LOAD -> SCAN half turn, {:.0f} deg {}, {} ms "
                "(independent calibration)".format(
                    config.CAROUSEL_HALF_TURN_DEG,
                    move.get("direction"),
                    move.get("duration_ms")
                )
            )

        else:
            detail = "scanner {} -> {}, direction {}, {} slot(s)".format(
                from_slot,
                slot_id,
                move.get("direction"),
                move.get("steps")
            )

        done("MOVE_TO_SCANNER", detail)

        # --- [3/10] MECHANICAL_SETTLE ---------------------------------
        if config.SCAN_SETTLE_TIME > 0:
            time.sleep(config.SCAN_SETTLE_TIME)

        done(
            "MECHANICAL_SETTLE",
            "{} s".format(config.SCAN_SETTLE_TIME)
        )

        # --- [4/10] SENSOR_READ ---------------------------------------
        # --- [5/10] LOAD_REFERENCES -----------------------------------
        # --- [6/10] NORMALIZE -----------------------------------------
        # --- [7/10] DATABASE_COMPARISON -------------------------------
        #
        # All four go through acquire_science_measurement(), the SAME
        # path Sensor Testing uses. There is deliberately only one sensor
        # implementation, so a passing sensor test really does prove this
        # acquisition works and only movement/storage remain as suspects.
        try:
            science = self.acquire_science_measurement()

        except CommandError as error:
            stage_map = {
                "SENSOR_ACQUISITION": "SENSOR_READ",
                "REFERENCE_PROCESSING": "LOAD_REFERENCES",
            }

            failed_stage = stage_map.get(
                (error.data or {}).get("stage"), "SENSOR_READ"
            )

            if error.code == "NORMALIZATION_FAILED":
                failed_stage = "NORMALIZE"

            fail(
                failed_stage,
                error.code,
                error.message,
                error.data
            )

        raw = science["raw"]
        normalized = science["normalized"]
        matches = science["matches"]
        interpretation = science["interpretation"]
        analysis_status = science["analysis_status"]
        analysis_error = science["analysis_error"]

        done(
            "SENSOR_READ",
            "{}/{} channels received".format(
                len(analysis.CHANNELS),
                len(analysis.CHANNELS)
            )
        )

        done(
            "LOAD_REFERENCES",
            "dark = {}, white = {} (fixed, {})".format(
                self.reference_report["dark"]["state"],
                self.reference_report["white"]["state"],
                config.CALIBRATION_ID
            )
        )

        done("NORMALIZE", "18 reflectance values computed")

        if analysis_status == "OK":
            done(
                "DATABASE_COMPARISON",
                "{} reference material(s) compared".format(len(matches))
            )

        else:
            stages.append({
                "index": len(stages) + 1,
                "total": MEASUREMENT_STAGE_COUNT,
                "stage": "DATABASE_COMPARISON",
                "ok": False,
                "detail": "FAILED: {} (spectrum preserved)".format(
                    analysis_error
                )
            })

        # Complete the record opened at prepare_load. Same Sample ID,
        # same object: the measurement is appended to the existing
        # scientific sample rather than filed as a separate one.
        existing = self.store.get_sample(sample_id) or {}

        record = analysis.build_record(
            sample_id=sample_id,
            slot_id=slot_id,
            timestamps={
                "created_at": slot["created_at"],
                "loaded_at": slot["loaded_at"],
                "measured_at": timestamp,
                "esp_uptime_ms": time.ticks_diff(
                    time.ticks_ms(),
                    self.started_at
                )
            },
            metadata=make_metadata(
                existing.get("metadata"),
                slot["metadata"],
                request.get("metadata")
            ),
            raw=raw,
            dark_reference=self.dark_reference,
            normalized=normalized,
            matches=matches,
            analysis=interpretation,
            sensor_settings=analysis.sensor_settings(),
            database_material_count=self.database_count()
        )

        # Preserve any earlier timestamps the stored record already had.
        stored_times = existing.get("timestamps")

        if isinstance(stored_times, dict):
            for key in ("created_at", "loaded_at"):
                if not record["timestamps"].get(key):
                    record["timestamps"][key] = stored_times.get(key)

        record["state"] = STATE_MEASURED
        record["analysis_status"] = analysis_status
        record["calibration_id"] = config.CALIBRATION_ID

        # --- [8/10] SAVE_SAMPLE ---------------------------------------
        # Persist before claiming anything. A measurement that could not
        # be stored is never reported as stored, and the slot stays
        # LOADED so it can be retried. The record travels back with the
        # error so no science is lost either way.
        try:
            self.store.add_sample(record)

        except (StorageError, OSError) as error:
            fail(
                "SAVE_SAMPLE",
                "SAMPLE_SAVE_ERROR",
                "Measurement succeeded but could not be saved: {}".format(
                    error
                ),
                {"sample": record}
            )

        done("SAVE_SAMPLE", config.SAMPLE_INDEX_FILE)

        # --- [9/10] UPDATE_SLOT_STATE ---------------------------------
        # Only now, with the record safely on flash, does the slot become
        # MEASURED. The soil stays physically in the slot.
        previous_state = slot["state"]

        slot["state"] = STATE_MEASURED
        slot["measured"] = True
        slot["occupied"] = True
        slot["measured_at"] = timestamp

        done(
            "UPDATE_SLOT_STATE",
            "{} -> {}, occupied = True".format(
                previous_state,
                STATE_MEASURED
            )
        )

        # The sample really is at the scanner now, and the reported
        # carousel phase says so. Nothing swings back here: Choose Slot
        # restores the loading orientation when the operator moves on,
        # which keeps the physical state explicit rather than hiding an
        # extra motor movement inside the measurement.
        return {
            "saved": True,
            "sample": record,
            "sample_id": sample_id,
            "slot_id": slot_id,
            "slot": slot,
            "previous_state": previous_state,
            "new_state": STATE_MEASURED,
            "move": move,
            "stages": stages,
            "analysis_status": analysis_status,
            "analysis_error": analysis_error,
            "calibration": self.calibration_status(),
            "carousel": self.carousel.status(),
            "saved_sample_count": self.store.count()
        }

    def handle_clear_slot(self, request):
        """
        Free a physical slot for reuse.

        Runtime state only. The measured scientific record stays in
        persistent storage and is never deleted here.
        """
        slot_id = self._require_slot(request)
        previous = self.carousel.slots[slot_id]

        cleared_sample = previous["sample_id"]
        previous_state = previous["state"]

        slot = self.carousel.reset_slot(slot_id)

        return {
            "slot": slot,
            "previous_state": previous_state,
            "cleared_sample_id": cleared_sample,
            "science_record_kept": (
                self.store.has_sample(cleared_sample)
                if cleared_sample else False
            ),
            "carousel": self.carousel.status()
        }

    # ==================================================================
    # persistent sample queries
    # ==================================================================

    def handle_list_samples(self, request):
        return {
            "count": self.store.count(),
            "samples": self.store.summaries()
        }

    def handle_get_sample(self, request):
        if "sample_id" not in request:
            raise CommandError(
                "MISSING_FIELD",
                "get_sample requires a 'sample_id'."
            )

        try:
            sample_id = validate_sample_id(request.get("sample_id"))
            record = self.store.get_sample(sample_id)

        except StorageError as error:
            raise CommandError("INVALID_SAMPLE_ID", str(error))

        if record is None:
            raise CommandError(
                "SAMPLE_NOT_FOUND",
                "No stored sample with ID {}.".format(sample_id)
            )

        return {"sample": record}

    def handle_update_sample_metadata(self, request):
        """Add or change mission metadata without touching spectral data."""
        if "sample_id" not in request:
            raise CommandError(
                "MISSING_FIELD",
                "update_sample_metadata requires a 'sample_id'."
            )

        metadata = request.get("metadata")

        if not isinstance(metadata, dict):
            raise CommandError(
                "MISSING_FIELD",
                "update_sample_metadata requires a 'metadata' object."
            )

        try:
            record = self.store.update_metadata(
                request.get("sample_id"),
                metadata
            )

        except StorageError as error:
            raise CommandError("SAMPLE_SAVE_ERROR", str(error))

        if record is None:
            raise CommandError(
                "SAMPLE_NOT_FOUND",
                "No stored sample with ID {}.".format(
                    request.get("sample_id")
                )
            )

        return {
            "sample_id": record.get("sample_id"),
            "metadata": record.get("metadata")
        }

    # ==================================================================
    # servo maintenance
    # ==================================================================

    def handle_servo_stop(self, request):
        result = self.carousel.stop()
        result["carousel"] = self.carousel.status()

        return result

    def _jog(self, request, direction):
        """
        Manual jog.

        With 'steps' this is a known whole-slot move and the tracked
        position survives it. With 'duration_ms' the resulting angle is
        unknown, so the position is invalidated and the operator must
        re-synchronize.
        """
        steps = request.get("steps")
        duration_ms = request.get("duration_ms")

        if steps is not None and duration_ms is not None:
            raise CommandError(
                "BAD_REQUEST",
                "Send either 'steps' or 'duration_ms', not both."
            )

        if steps is not None:
            move = self.carousel.jog_steps(direction, steps)

        else:
            if duration_ms is None:
                duration_ms = config.SERVO_JOG_DEFAULT_MS

            try:
                duration_ms = int(duration_ms)

            except (TypeError, ValueError):
                raise CommandError(
                    "BAD_REQUEST",
                    "'duration_ms' must be an integer."
                )

            if duration_ms < 0 or duration_ms > config.SERVO_JOG_MAX_MS:
                raise CommandError(
                    "BAD_REQUEST",
                    "'duration_ms' must be between 0 and {}.".format(
                        config.SERVO_JOG_MAX_MS
                    )
                )

            move = self.carousel.jog_timed(direction, duration_ms)

        return {
            "move": move,
            "carousel": self.carousel.status(),
            "servo": self.servo.status()
        }

    def handle_servo_jog_cw(self, request):
        return self._jog(request, CW)

    def handle_servo_jog_ccw(self, request):
        return self._jog(request, CCW)

    # ==================================================================
    # optional information commands
    # ==================================================================

    # ==================================================================
    # shared science acquisition
    # ==================================================================

    def acquire_science_measurement(self):
        """
        The ONE sensor path. Both Measure Sample and Sensor Testing use
        this, so a working sensor test really does prove the measurement
        acquisition works.

        Takes a NEW spectrum, validates all 18 channels, applies the
        fixed references, normalizes and compares against every material.

        Knows nothing about persistence, slots or the carousel: the
        caller decides whether the result is saved.

        Raises CommandError with a specific code and stage on failure.
        """
        self._require_sensor()
        self._require_references()

        # 1. new acquisition - never reuse a previous spectrum
        try:
            raw = self.sensor.take_sample()

        except Exception as error:
            if "timeout" in str(error).lower():
                code = "SENSOR_DATA_READY_TIMEOUT"
            else:
                code = "SENSOR_ERROR"

            raise CommandError(
                code,
                "AS7265x acquisition failed: {}".format(error),
                data={
                    "stage": "SENSOR_ACQUISITION",
                    "exception_type": type(error).__name__
                }
            )

        if raw is None:
            raise CommandError(
                "SENSOR_ERROR",
                "AS7265x returned no data.",
                data={"stage": "SENSOR_ACQUISITION"}
            )

        # 2. all 18 channels must be present and numeric
        missing = analysis.validate_channels(raw)

        if missing:
            raise CommandError(
                "INCOMPLETE_SPECTRUM",
                "AS7265x returned {}/{} channels; missing {}.".format(
                    len(analysis.CHANNELS) - len(missing),
                    len(analysis.CHANNELS),
                    ",".join(missing)
                ),
                data={
                    "stage": "SENSOR_ACQUISITION",
                    "missing_channels": missing
                }
            )

        # 3. fixed dark/white, never re-measured
        if not self.install_references():
            raise CommandError(
                "REFERENCES_NOT_LOADED",
                "Fixed references could not be applied to the sensor.",
                data={"stage": "REFERENCE_PROCESSING"}
            )

        # 4. R = (S - D) / (W - D)
        if not self.sensor.normalize():
            raise CommandError(
                "NORMALIZATION_FAILED",
                "Reflectance calculation failed; the references or the "
                "measurement are incomplete.",
                data={"stage": "REFERENCE_PROCESSING"}
            )

        normalized = self.sensor.normalized_data

        # 5. compare against every material; a database fault must not
        #    cost us the spectrum
        analysis_status = "OK"
        analysis_error = None

        try:
            matches = analysis.build_matches(self.database, normalized)
            interpretation = analysis.interpret(matches)

        except Exception as error:
            matches = []
            analysis_status = "FAILED"
            analysis_error = "{}: {}".format(
                type(error).__name__, error
            )

            interpretation = {
                "best_match": None,
                "best_match_score": None,
                "second_match": None,
                "second_match_score": None,
                "score_difference": None,
                "status": "ANALYSIS_FAILED",
                "confidence": "NONE",
                "automatic_conclusion": (
                    "The spectrum was acquired successfully, but the "
                    "comparison against the reference database failed "
                    "({}). The measured data is intact.".format(
                        analysis_error
                    )
                ),
                "error": analysis_error
            }

        return {
            "raw": raw,
            "normalized": normalized,
            "matches": matches,
            "interpretation": interpretation,
            "analysis_status": analysis_status,
            "analysis_error": analysis_error
        }

    def handle_test_measurement(self, request):
        """
        Take one spectrum and analyse it, saving nothing.

        Uses acquire_science_measurement() - exactly the same path as a
        real measurement - so a passing test proves the acquisition and
        analysis chain works.

        No carousel movement, no Sample ID, no slot state change, no
        write to samples.json. Deliberately does NOT require carousel
        synchronization: diagnostics must work on a cold module.
        """
        science = self.acquire_science_measurement()

        raw = science["raw"]

        return {
            "test_only": True,
            "saved": False,
            "channels_nm": analysis.channel_wavelengths(),
            "raw": analysis.round_channels(raw, analysis.RAW_DECIMALS),
            "dark_corrected": analysis.dark_correct(
                raw, self.dark_reference
            ),
            "normalized": analysis.round_channels(
                science["normalized"], analysis.NORMALIZED_DECIMALS
            ),
            "sensor_settings": analysis.sensor_settings(),
            "calibration": self.calibration_status(),
            "reference_matches": science["matches"],
            "analysis": science["interpretation"],
            "analysis_status": science["analysis_status"],
            "analysis_error": science["analysis_error"],
            "database_material_count": self.database_count(),
            "note": "Test only - nothing was saved and nothing moved."
        }

    def handle_sensor_diagnostics(self, request):
        """
        Walk the whole AS7265x path and report every stage.

        Always succeeds at the protocol level: a hardware fault is
        reported inside the staged result, never as a bare error, so the
        operator always sees how far it got.
        """
        acquire = request.get("acquire", True)

        return sensor_diag.run_full_diagnostics(self, acquire=bool(acquire))

    def handle_i2c_scan(self, request):
        """
        Scan the I2C bus, independently of sensor initialization.

        Works even when the driver failed at boot - which is exactly
        when the operator needs to know what is on the bus.
        """
        i2c, report = sensor_diag.run_i2c_scan()

        entry = report.stages[0] if report.stages else {}

        return {
            "stage": entry.get("stage"),
            "status": entry.get("status"),
            "details": entry.get("details"),
            "error": entry.get("error"),
            "overall": report.overall()
        }

    def handle_raw_measurement(self, request):
        """
        Read and return the 18 raw channels, nothing else.

        No references, no database, no carousel, no sample state. If
        this works, the sensor and driver are fundamentally healthy.
        """
        self._require_sensor()

        try:
            raw = self.sensor.take_sample()

        except Exception as error:
            if "timeout" in str(error).lower():
                code = "SENSOR_DATA_READY_TIMEOUT"
            else:
                code = "SENSOR_ERROR"

            raise CommandError(
                code,
                "AS7265x acquisition failed: {}".format(error),
                data={
                    "stage": "SENSOR_ACQUISITION",
                    "exception_type": type(error).__name__
                }
            )

        if raw is None:
            raise CommandError(
                "SENSOR_ERROR",
                "AS7265x returned no data.",
                data={"stage": "SENSOR_ACQUISITION"}
            )

        missing = analysis.validate_channels(raw)
        zeros = [
            c for c in analysis.CHANNELS
            if float(raw.get(c, 0.0) or 0.0) == 0.0
        ]

        return {
            "test_only": True,
            "saved": False,
            "channels_nm": analysis.channel_wavelengths(),
            "raw": analysis.round_channels(raw, analysis.RAW_DECIMALS),
            "channels_received": len(analysis.CHANNELS) - len(missing),
            "missing_channels": missing,
            "zero_channels": zeros,
            "sensor_settings": analysis.sensor_settings(),
            "warning": "ALL_CHANNELS_ZERO"
            if len(zeros) == len(analysis.CHANNELS) else None
        }

    def handle_get_references(self, request):
        return {
            "loaded": self.references_ok,
            "error": self.references_error,
            "file": config.REFERENCES_FILE,
            "read_only": True,
            "calibration": self.calibration_status(),
            "channels_nm": analysis.channel_wavelengths(),
            "white": self.white_reference,
            "dark": self.dark_reference
        }

    def handle_help(self, request):
        """Machine-readable help, including the calibration explanation."""
        commands = list(self.handlers.keys())
        commands.sort()

        return {
            "firmware": config.FIRMWARE_NAME,
            "version": config.FIRMWARE_VERSION,
            "commands": commands,
            "measurement_stages": list(MEASUREMENT_STAGES),
            "calibration": {
                "title": "CALIBRATION",
                "text": CALIBRATION_HELP,
                "mode": "FIXED_STORED_REFERENCES",
                "calibration_id": config.CALIBRATION_ID,
                "runtime_recalibration": "DISABLED"
            },
            "workflow": [
                "sync_position: align physical Slot 1 with the loading "
                "hole, then confirm it as the origin",
                "select_slot: choose the physical slot to work with "
                "(normally the next one clockwise)",
                "prepare_load: assign a Sample ID and science metadata",
                "confirm_loaded: the external arm deposited soil",
                "measure_sample: move to scanner, measure, analyse, save",
                "clear_slot when the slot is needed again"
            ],
            "carousel": {
                "slot_count": config.CAROUSEL_SLOT_COUNT,
                "slot_geometry_deg": config.CAROUSEL_SLOT_GEOMETRY_DEG,
                "slot_step_deg": config.SLOT_STEP_DEG,
                "half_turn_deg": config.CAROUSEL_HALF_TURN_DEG,
                "numbering": "1..8 clockwise, Slot 1 = loading position",
                "scanner_offset_slots": config.CAROUSEL_SCAN_LOAD_OFFSET,
                "max_fine_adjust_deg": config.MAX_FINE_ADJUST_DEG
            }
        }

    def handle_get_material_names(self, request):
        if self.database is None or not self.database.data:
            return {"count": 0, "materials": []}

        names = list(self.database.data.keys())
        names.sort()

        return {"count": len(names), "materials": names}

    def handle_get_database_status(self, request):
        return {
            "file": config.DATABASE_FILE,
            "loaded": self.database_count() > 0,
            "material_count": self.database_count(),
            "error": self.database_error,
            "metric": "cosine_similarity",
            "read_only": True
        }

    # ==================================================================
    # dispatch
    # ==================================================================

    def dispatch_command(self, request):
        """Run one command and build exactly one response object."""
        request_id = None
        cmd = None

        try:
            if not isinstance(request, dict):
                raise CommandError(
                    "BAD_REQUEST",
                    "A command must be a JSON object."
                )

            request_id = request.get("request_id")
            cmd = request.get("cmd")

            if not cmd:
                raise CommandError(
                    "MISSING_FIELD",
                    "A command must contain a 'cmd' field."
                )

            handler = self.handlers.get(cmd)

            if handler is None:
                raise CommandError(
                    "UNKNOWN_COMMAND",
                    "Unknown command '{}'.".format(cmd)
                )

            data = handler(request)

            return {
                "request_id": request_id,
                "ok": True,
                "cmd": cmd,
                "data": data
            }

        except CommandError as error:
            response = {
                "request_id": request_id,
                "ok": False,
                "cmd": cmd,
                "error": {
                    "code": error.code,
                    "message": error.message
                }
            }

            if error.data is not None:
                response["data"] = error.data

            return response

        except CarouselError as error:
            return {
                "request_id": request_id,
                "ok": False,
                "cmd": cmd,
                "error": {
                    "code": error.code,
                    "message": error.message
                }
            }

        except StorageError as error:
            return {
                "request_id": request_id,
                "ok": False,
                "cmd": cmd,
                "error": {
                    "code": "SAMPLE_SAVE_ERROR",
                    "message": str(error)
                }
            }

        except Exception as error:
            # An unexpected fault must still produce a well-formed
            # answer, otherwise the PC blocks waiting for a reply.
            #
            # type(error).__name__ - never type(error) itself, which is
            # a class object and cannot be serialized.
            _debug("internal error in '{}': {}".format(cmd, error))

            return {
                "request_id": request_id,
                "ok": False,
                "cmd": cmd,
                "error": {
                    "code": "INTERNAL_ERROR",
                    "exception_type": type(error).__name__,
                    "message": "{}".format(error)
                }
            }

    # ==================================================================
    # command server
    # ==================================================================

    def process_command(self, line):
        """Parse one received line and answer it with exactly one frame."""
        if len(line) > config.MAX_COMMAND_BYTES:
            send_error(
                None,
                "COMMAND_TOO_LONG",
                "Command exceeded {} bytes.".format(
                    config.MAX_COMMAND_BYTES
                )
            )

            return

        try:
            request = json.loads(line)

        except (ValueError, TypeError):
            send_error(
                None,
                "INVALID_JSON",
                "Command is not valid JSON."
            )

            return

        response = self.dispatch_command(request)

        # Prove the response encodes BEFORE anything is written, so a
        # stray value can never leave half an object on the wire.
        try:
            json.dumps(response)

        except (TypeError, ValueError) as error:
            response = {
                "request_id": request.get("request_id")
                if isinstance(request, dict) else None,
                "ok": False,
                "cmd": request.get("cmd")
                if isinstance(request, dict) else None,
                "error": {
                    "code": "JSON_SERIALIZATION_ERROR",
                    "exception_type": type(error).__name__,
                    "message": "Measurement result contains a "
                               "non-serializable value.",
                    "stage": "SEND_RESPONSE"
                }
            }

        send_json(response)

    def command_loop(self):
        """
        Wait for commands and execute them, one at a time, forever.

        There is no automatic activity of any kind here: the module does
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
                # means the failure was in the transport itself. Keep
                # serving rather than dropping to the REPL.
                _debug("command loop error:", error)

    def run(self):
        self.boot()

        try:
            self.command_loop()

        except KeyboardInterrupt:
            _debug("command loop stopped by user")

        finally:
            if self.servo is not None:
                try:
                    self.servo.release()
                    _debug("servo PWM released")

                except Exception as error:
                    _debug("could not release servo PWM:", error)


# Module-level handle so the running instance can be inspected from the
# REPL after Ctrl+C:  import main; main.science.carousel.status()
science = None


def main():
    global science

    science = ScienceModule()
    science.run()


# MicroPython executes main.py automatically after boot/reset.
main()
