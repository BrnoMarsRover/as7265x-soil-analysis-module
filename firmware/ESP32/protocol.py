# protocol.py
#
# The command surface: newline-delimited JSON over the USB serial
# console, and every command the PC may send.
#
#     one command  = one JSON object followed by "\n"  on sys.stdin
#     one response = one JSON object followed by "\n"  on sys.stdout
#
# carried over the development board's CP2102 bridge, on the same cable
# that powers the ESP32.
#
# ONE request in, exactly ONE response frame out - always. A command
# that fails, a command that does not exist, a payload that is not JSON
# and an unexpected internal fault all produce a well-formed answer,
# because the PC blocks waiting for a line terminator and a silent
# failure looks identical to a dead board.
#
# stdout IS the protocol stream. Anything printed there lands in the
# middle of the JSON the PC is parsing, which is why the firmware
# prints nothing outside a frame and why diagnostics go through a debug
# helper gated by config.DEBUG.
#
# WHY ONE FILE
#
# An earlier revision split this into a transport, a router and four
# command modules, and every one of them existed to hold part of the
# same sentence: "read a line, work out what was asked, ask the
# hardware, answer". Reading a request meant opening five files. The
# framing, the dispatch and the handlers are one responsibility - the
# command surface - and they live together.
#
# This is emphatically NOT the servo link. The ST3215 talks over UART2,
# which is a separate hardware peripheral owned by servo.py. The two
# channels never share a byte.
#
# Nothing here interprets a spectrum. Every scientific question - what
# a material is, whether a measurement is any good - belongs to Science
# on the PC.

import gc
import json
import sys
import time

import config

import sensor as sensor_module
import servo as servo_module

from carousel import CarouselError
from sensor import SensorError
from servo import CCW, CW, ServoError, ServoNotSupportedError


# ======================================================================
# transport
# ======================================================================

def debug(*parts):
    """Diagnostic output, silent unless config.DEBUG is switched on by hand."""
    if config.DEBUG:
        print(*parts)


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
    Anything genuinely unsupported (an exception, a driver, a UART) is
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


def _minimal_error(payload, code, message):
    """
    A small frame built with as little allocation as possible.

    Used when the real response could not be produced. It is assembled
    by concatenation rather than by json.dumps, because the reason we
    are here may well be that a large allocation just failed - asking
    for another one would fail the same way and leave the PC with
    nothing at all.
    """
    request_id = None
    cmd = None

    if isinstance(payload, dict):
        request_id = payload.get("request_id")
        cmd = payload.get("cmd")

    def quoted(value):
        if value is None:
            return "null"

        return '"' + str(value).replace('\\', '')[:48].replace('"', '') + '"'

    return (
        '{"request_id": ' + quoted(request_id)
        + ', "ok": false, "cmd": ' + quoted(cmd)
        + ', "error": {"code": "' + code
        + '", "message": "' + message + '"}}'
    )


def send_json(payload):
    """
    The single exit point for every response.

    Serialization completes IN FULL before a byte is written, so a value
    that cannot be encoded can never leave half an object on the wire.

    AND IT ALWAYS SENDS SOMETHING.

    This used to catch only TypeError and ValueError. A response of a
    few kilobytes is the largest single allocation this firmware makes,
    and after a long acquisition the heap is fragmented enough that it
    can fail with tens of kilobytes still free - MicroPython needs the
    block contiguous. Measured on hardware: a 5148-byte response raised
    MemoryError with 90016 bytes free.

    MemoryError then escaped this function, the serving loop swallowed
    it, and the PC waited out its entire 180-second timeout for a frame
    that was never coming - which reads exactly like a dead board and
    is not one. One request gets one frame, whatever happens.
    """
    # Reclaim before the largest allocation of the cycle, not after it
    # has already failed.
    gc.collect()

    try:
        text = json.dumps(payload)

    except (TypeError, ValueError):
        try:
            text = json.dumps(make_json_safe(payload))

        except Exception as error:
            text = _minimal_error(
                payload, "JSON_SERIALIZATION_ERROR",
                "Response contains a value that cannot be encoded ("
                + type(error).__name__ + ").")

    except MemoryError:
        # One honest retry: collecting again sometimes coalesces enough
        # to fit. If it does not, say so in a frame small enough to
        # build with no room to spare.
        gc.collect()

        try:
            text = json.dumps(payload)

        except Exception:
            text = _minimal_error(
                payload, "RESPONSE_TOO_LARGE",
                "The response did not fit in available memory. Ask for "
                "fewer repeats, or read the parts separately.")

    # The leading newline is a guard, not decoration: it closes anything
    # already sitting on the console so this frame gets a line of its
    # own. See config.RESPONSE_GUARD_NEWLINE.
    #
    # WRITTEN AS THREE PIECES, never concatenated.
    #
    # `prefix + text + "\n"` builds a SECOND complete copy of the
    # response - the largest allocation this firmware makes - and it
    # did so OUTSIDE the MemoryError guard above. Measured on hardware:
    # a six-repeat acquisition block survived json.dumps and then died
    # on that concatenation, so the sensor had been read, the data
    # existed, and the PC was told only "the answer could not be sent
    # (MemoryError)". Two consecutive blocks failed and passed
    # alternately, because the error path's gc.collect() freed for the
    # next request exactly what the concatenation had needed.
    #
    # Three writes allocate nothing beyond the 256-byte chunks
    # _write_all already slices, and _ascii_only returns the SAME
    # object when the text is pure ASCII, which every response of this
    # protocol is.
    if config.RESPONSE_GUARD_NEWLINE:
        _write_all("\n")

    _write_all(_ascii_only(text))
    _write_all("\n")


# ======================================================================
# errors
# ======================================================================

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


def _error_envelope(error):
    """
    Turn a domain exception into the error half of a response.

    Each domain keeps its own code on the way out, so the PC can tell a
    servo fault from a sensor fault from a rejected request. A failed
    movement also carries whatever the driver could still report, which
    is what separates "the servo is unreachable" from "the servo is
    fine but stopped in the wrong place".
    """
    envelope = {"error": {"code": error.code, "message": error.message}}

    data = getattr(error, "data", None)

    if data is not None:
        envelope["data"] = data

    return envelope


# Domain exceptions the dispatcher answers with a code of their own.
# Order matters only in that subclasses must precede their bases.
DOMAIN_ERRORS = (CarouselError, ServoError, SensorError)


# ======================================================================
# the command surface
# ======================================================================

class Protocol:
    """
    Reads requests, dispatches them to the hardware, answers exactly once.

    Construction touches no hardware at all. That is what lets the
    board be reachable when the sensor is unplugged, the servo has no
    power, or the carousel has never been synchronized: those are
    states the protocol REPORTS, not conditions for it to run.
    """

    def __init__(self, hardware):
        self.hw = hardware

    # ------------------------------------------------------------------
    # shared accessors
    # ------------------------------------------------------------------

    @property
    def sensor(self):
        return self.hw.sensor

    @property
    def servo(self):
        return self.hw.servo

    @property
    def carousel(self):
        return self.hw.carousel

    def command_names(self):
        return sorted(COMMANDS.keys())

    # ------------------------------------------------------------------
    # shared helpers
    # ------------------------------------------------------------------

    def _uptime_ms(self):
        return self.hw.uptime_ms()

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
    # identity and state
    # ------------------------------------------------------------------

    def handle_ping(self, request):
        """
        Proof of life, and the module's identity.

        Answers with NO hardware access whatsoever. A missing sensor, a
        servo with no power and an unsynchronized carousel must all
        leave ping working, because ping is how an operator finds out
        which of those is the problem.
        """
        return {
            "pong": True,
            "firmware": config.FIRMWARE_NAME,
            "version": config.FIRMWARE_VERSION,
            "protocol_version": config.PROTOCOL_VERSION,
            "acquisition_protocol_version":
                config.ACQUISITION_PROTOCOL_VERSION,
            "uptime_ms": self._uptime_ms(),

            # The mechanical facts, so the PC reads its geometry from
            # the firmware instead of keeping a second copy that can
            # silently disagree.
            "slot_count": config.CAROUSEL_SLOT_COUNT,
            "slot_angle_deg": config.CAROUSEL_SLOT_GEOMETRY_DEG,
            "scanner_offset_deg": config.CAROUSEL_HALF_TURN_DEG,
            "scanner_offset_slots": config.CAROUSEL_SCAN_LOAD_OFFSET,
            "servo": servo_module.SERVO_LABEL,
            "sensor": "AS7265x",
        }

    def handle_get_status(self, request):
        """
        Hardware state only. Nothing scientific is known here.

        Every block always answers, even when its hardware is missing:
        "NOT CONNECTED" and "sensor unavailable" are the two most
        important things an operator can be told, so neither is ever
        hidden behind an error.
        """
        return {
            "firmware": config.FIRMWARE_NAME,
            "version": config.FIRMWARE_VERSION,
            "protocol_version": config.PROTOCOL_VERSION,
            "uptime_ms": self._uptime_ms(),
            "transport": "usb-serial-console",
            "commands": self.command_names(),

            "sensor": self.sensor.status(),
            "servo": self.servo.status(),
            "carousel": self.carousel.status(),
            "slots": self.carousel.slot_summary(),
        }

    # ------------------------------------------------------------------
    # servo lifecycle, service and diagnostics
    # ------------------------------------------------------------------

    def handle_connect_servo(self, request):
        """
        Open the link to the ST3215 and prove it answers.

        This is option [0] on the PC, and it ALWAYS invalidates the
        carousel position: a servo that has just been connected has an
        encoder zero the firmware has never seen, and the mechanism may
        have been turned by hand while it was disconnected.
        """
        try:
            connection = self.servo.connect()

        except ServoError as error:
            # A driver that failed to come up leaves nothing connected,
            # so the carousel must not keep pointing at it either.
            self.carousel.attach_servo(None, "servo connection failed")

            raise CommandError(error.code, error.message)

        attachment = self.carousel.attach_servo(
            self.servo.driver,
            "servo connected: {}".format(connection["label"]),
        )

        return {
            "connection": connection,
            "carousel_attachment": attachment,
            "servo": self.servo.status(),
            "carousel": self.carousel.status(),
            "moved": False,
            "message": (
                "{} connected. The carousel position was invalidated - "
                "align Slot 1 with the loading hole and confirm "
                "it.".format(connection["label"])
            ),
        }

    def handle_disconnect_servo(self, request):
        """
        Release the ST3215 and block carousel movement again.

        The right thing before unplugging the servo or its external
        supply. Torque is deliberately left as it was: dropping it here
        would let the carousel turn freely at the moment the firmware
        stops watching it.
        """
        release = self.servo.release("operator disconnected the servo")

        attachment = self.carousel.attach_servo(
            None, "servo disconnected"
        )

        return {
            "connection": release,
            "carousel_attachment": attachment,
            "servo": self.servo.status(),
            "carousel": self.carousel.status(),
            "moved": False,
            "message": "The servo is disconnected. Carousel movement is "
                       "blocked.",
        }

    # ------------------------------------------------------------------
    # movement control
    # ------------------------------------------------------------------

    def handle_servo_stop(self, request):
        """
        Stop the actuator where it is.

        The tracked position is dropped, because an aborted movement ends
        between slot centres and the firmware does not guess.
        """
        self.servo.require_driver()

        try:
            result = self.carousel.stop()

        except CarouselError as error:
            raise CommandError(error.code, error.message, data=error.data)

        result["servo"] = self.servo.status()
        result["carousel"] = self.carousel.status()

        return result

    # ------------------------------------------------------------------
    # diagnostics
    # ------------------------------------------------------------------

    def handle_servo_diagnostics(self, request):
        """
        Check the servo. MOVES NOTHING.

        A full communication check: UART, ping, ID, baud, mode, torque,
        angle limits and telemetry. Everything the ST3215 can be asked
        without turning it.
        """
        self.servo.require_driver()

        try:
            report = self.servo.diagnostics()

        except ServoError as error:
            raise CommandError(error.code, error.message)

        report["moved"] = False
        report["carousel"] = self.carousel.status()

        return report

    def handle_servo_bus_scan(self, request):
        """
        Search the servo bus for anything that answers. MOVES NOTHING.

        Runs WITHOUT a servo being selected, which is the whole point:
        the operator reaches for this precisely when select_servo has
        just failed with SERVO_NOT_FOUND, and requiring a working
        selection before diagnosing why selection does not work would be
        circular.

        Only INST_PING is sent, at every baud rate the ST3215 supports
        and in both pin orders. A ping is a read - it asks a servo to
        identify itself and moves nothing, whatever state that servo is
        in.

        If an ST3215 is currently selected it is RELEASED first: the scan
        reopens UART2 at each rate it tries, and two owners of one
        peripheral is not a thing that can be made to work. The release
        is reported rather than done quietly.
        """
        released = None

        if self.servo.is_connected():
            released = self.servo.release(
                "released so the bus scan can reopen UART2"
            )
            self.carousel.attach_servo(None, "servo released for bus scan")

        ids = request.get("ids")

        if ids in ("all", "ALL"):
            # 0 to 253: 254 is the broadcast address and 255 is not an
            # address at all.
            ids = list(range(0, 254))

        elif isinstance(ids, list):
            ids = [int(value) for value in ids]

        elif ids is not None:
            ids = [int(ids)]

        bauds = request.get("bauds")

        if isinstance(bauds, list):
            bauds = [int(value) for value in bauds]

        elif bauds is not None:
            bauds = [int(bauds)]

        try:
            report = servo_module.bus_scan(
                bauds=bauds,
                servo_ids=ids,
                try_swapped_pins=bool(request.get("swap", True)),
                timeout_ms=request.get("timeout_ms"),
            )

        except Exception as error:
            raise CommandError(
                "SERVO_SCAN_FAILED",
                "The bus scan could not run: {}".format(error),
                data={"exception_type": type(error).__name__},
            )

        report["released_servo"] = released
        report["servo"] = self.servo.status()

        return report

    # ------------------------------------------------------------------
    # calibration
    # ------------------------------------------------------------------

    def handle_get_servo_calibration(self, request):
        """
        The servo's tunables.

        The `editable` flag is False for the ST3215: its limits are
        engineering values that belong under version control, not
        numbers to be trimmed at runtime. Nothing here is a timing
        calibration - every movement is verified against the encoder.
        """
        servo = self.servo.require_driver()

        calibration = servo.calibration()

        # NO capability table. It was seven booleans that were all True
        # with one actuator, and it was deleted with the rest of the
        # multi-servo machinery - but this call to it survived, so
        # get_servo_calibration raised AttributeError against every real
        # ST3215 while the fake-hardware suite never noticed, because
        # nothing exercised this command. Found on the bench, 2026-08-20.
        calibration["servo_type"] = servo_module.SERVO_TYPE
        calibration["slot_step_deg"] = config.CAROUSEL_SLOT_GEOMETRY_DEG
        calibration["half_turn_deg"] = config.CAROUSEL_HALF_TURN_DEG

        return calibration

    def handle_servo_test_move(self, request):
        """
        Run one movement test on the active backend.

        Requires 'confirm': true, because these movements turn a carousel
        that may have soil in it. The available kinds come from the
        driver, so the PC lists what the servo can actually do rather
        than a list compiled into the client:

            ST3215   one slot each way, the two 180 deg sweeps, the
                     out-and-back pair, and the encoder boundary test,
                     each leg verified against the encoder

        A test with net travel invalidates the carousel position. A
        symmetrical test on a backend that can PROVE it came back leaves
        the position intact; on an open-loop backend it never can, so the
        position goes regardless.
        """
        servo = self.servo.require_driver()

        kinds = [name for name, _label in servo.test_move_kinds()]
        kind = request.get("kind")

        if not request.get("confirm"):
            raise CommandError(
                "CONFIRMATION_REQUIRED",
                "servo_test_move turns the carousel. Check that the "
                "mechanism is clear, then send 'confirm': true.",
                data={
                    "kind": kind,
                    "kinds": kinds,
                    "servo": servo_module.SERVO_TYPE,
                },
            )

        if kind not in kinds:
            raise CommandError(
                "BAD_REQUEST",
                "kind must be one of: {}.".format(", ".join(kinds)),
                data={"kinds": kinds, "servo": servo_module.SERVO_TYPE},
            )

        try:
            repeat = int(request.get("repeat", 1))

        except (TypeError, ValueError):
            raise CommandError("BAD_REQUEST", "'repeat' must be a number.")

        if repeat < 1 or repeat > 8:
            raise CommandError(
                "BAD_REQUEST", "'repeat' must be between 1 and 8."
            )

        degrees = None

        if kind == "degrees":
            if "degrees" not in request:
                raise CommandError(
                    "MISSING_FIELD",
                    "kind 'degrees' requires a 'degrees' field.",
                )

            try:
                degrees = float(request.get("degrees"))

            except (TypeError, ValueError):
                raise CommandError(
                    "BAD_REQUEST", "'degrees' must be a number."
                )

        hold_ms = None

        if kind == "neutral":
            try:
                hold_ms = int(request.get("hold_ms", 5000))

            except (TypeError, ValueError):
                raise CommandError(
                    "BAD_REQUEST", "'hold_ms' must be a number."
                )

        # The ST3215 verifies every movement against its encoder, so
        # position tracking survives a symmetrical test and is dropped
        # only for a test with net travel - which the driver reports
        # afterwards.
        try:
            result = self._run_test(servo, kind, repeat, degrees, hold_ms)

        except ServoNotSupportedError as error:
            raise CommandError(error.code, error.message)

        except ServoError as error:
            self.carousel.invalidate_position("servo movement test failed")

            raise CommandError(
                error.code,
                error.message,
                data={
                    "kind": kind,
                    "servo": servo_module.SERVO_TYPE,
                    "position_invalidated": True,
                },
            )

        if result.get("position_invalidated"):
            self.carousel.invalidate_position(
                "servo movement test with net travel"
            )

        result["servo_type"] = servo_module.SERVO_TYPE
        result["servo"] = self.servo.status()
        result["carousel"] = self.carousel.status()

        return result

    def _run_test(self, servo, kind, repeat, degrees, hold_ms):
        """Call the backend's test with only the arguments it accepts."""
        if kind == "neutral":
            return servo.test_move(kind, repeat=repeat, hold_ms=hold_ms)

        return servo.test_move(kind, repeat=repeat, degrees=degrees)

    # ------------------------------------------------------------------
    # service operations
    # ------------------------------------------------------------------

    def handle_servo_configure(self, request):
        """
        SERVICE OPERATION: write the operating mode into servo EPROM.

        Kept firmly apart from carousel calibration. Ordinary carousel
        setup establishes a RUNTIME origin and touches no persistent servo
        state; this command is the only thing in the firmware that writes
        the servo's non-volatile memory, it is confirmed explicitly, and it
        is needed once per servo rather than once per session.

        """
        servo = self.servo.require_driver()

        if not request.get("confirm"):
            raise CommandError(
                "CONFIRMATION_REQUIRED",
                "servo_configure writes the servo EPROM. Send "
                "'confirm': true to proceed.",
                data={
                    "servo": servo_module.SERVO_TYPE,
                    "mode": config.ST3215_MODE,
                    "writes_eprom": True,
                },
            )

        mode = request.get("mode", config.ST3215_MODE)

        try:
            result = servo.configure_mode(mode)

        except ServoError as error:
            raise CommandError(error.code, error.message)

        result["moved"] = False
        result["servo"] = self.servo.status()

        return result

    def handle_servo_torque(self, request):
        """
        Turn the servo's holding torque on or off, explicitly.

        Releasing torque is what lets the operator turn the carousel by
        hand during setup. It is never done automatically: between
        movements the carousel has to stay exactly where it was put, or a
        sample gets measured off centre.

        Releasing it also invalidates the tracked position, because a
        mechanism that can be turned by hand no longer has a position the
        firmware can vouch for.

        """
        servo = self.servo.require_driver()

        enable = request.get("enable", True)

        try:
            if enable:
                servo.enable_torque()

            else:
                servo.disable_torque()
                self.carousel.invalidate_position(
                    "servo torque was released, so the carousel can be "
                    "turned by hand"
                )

        except ServoError as error:
            raise CommandError(error.code, error.message)

        return {
            "torque_enabled": bool(enable),
            "moved": False,
            "servo": self.servo.status(),
            "carousel": self.carousel.status(),
        }

    # ------------------------------------------------------------------
    # carousel
    # ------------------------------------------------------------------

    def handle_sync_position(self, request):
        """
        Establish the carousel origin. This never moves anything.

        Normal use is 'load_slot': the operator physically aligns Slot 1
        with the soil loading hole and confirms it, making that position
        the origin. 'scan_slot' is accepted for callers that prefer to
        declare the scanner side.

        The one thing this DOES need the servo for is reading the encoder:
        the origin is a real encoder count, taken at the moment the
        operator confirms the alignment. If the servo cannot be read, no
        origin is recorded - a made-up origin would poison every movement
        that followed it.
        """
        if "load_slot" in request:
            try:
                status = self.carousel.sync_to_load_slot(
                    request.get("load_slot")
                )

            except CarouselError as error:
                raise CommandError(error.code, error.message, data=error.data)

        elif "scan_slot" in request:
            try:
                status = self.carousel.sync_position(request.get("scan_slot"))

            except CarouselError as error:
                raise CommandError(error.code, error.message, data=error.data)

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
        Whole-slot movement: one slot spacing per slot, in encoder counts.

        Works before synchronization, so the operator can line Slot 1 up
        with the loading hole during the sync procedure. Each slot is a
        separately verified movement, so a fault partway through a
        multi-slot request is reported instead of accumulating.
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

        try:
            move = self.carousel.move_slots(direction, slots)

        except CarouselError as error:
            raise CommandError(error.code, error.message, data=error.data)

        return {
            "move": move,
            "degrees": slots * config.CAROUSEL_SLOT_GEOMETRY_DEG,
            "degrees_per_slot": config.CAROUSEL_SLOT_GEOMETRY_DEG,
            "carousel": self.carousel.status(),
        }

    def handle_fine_adjust(self, request):
        """
        Small mechanical correction in degrees; positive is clockwise.

        Alignment only - the logical slot numbering is left alone. The
        correction is converted to encoder counts, commanded, and then
        verified like any other movement; it is also REMEMBERED, so the
        next slot movement keeps it instead of returning the carousel to
        the old theoretical centre.
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

        try:
            adjustment = self.carousel.fine_adjust(degrees)

        except CarouselError as error:
            raise CommandError(error.code, error.message, data=error.data)

        return {
            "adjustment": adjustment,
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

    # ------------------------------------------------------------------
    # acquisition
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

        lamp = sensor_module.LAMP_BY_NAME.get(name)

        if lamp is None:
            raise CommandError(
                "BAD_REQUEST",
                "illumination must be one of: dark, {}.".format(
                    ", ".join(sorted(sensor_module.LAMP_BY_NAME.keys()))
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
                lamp = sensor_module.LAMP_BY_NAME[name]
                entry = {"illumination": name}

                try:
                    driver.set_bulb_current(
                        lamp, sensor_module.LAMP_CURRENTS[lamp]()
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
        this uses ensure_ready() and acquire_triad(), the same two
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
            "bus": sensor_module.bus_description(),
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
                sensor_module.ADDRESS, config.I2C_BUS
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
                lamp = sensor_module.LAMP_BY_NAME[name]

                driver.set_bulb_current(lamp, sensor_module.LAMP_CURRENTS[lamp]())
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
    # the retained-acquisition buffer
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
    # dispatch
    # ------------------------------------------------------------------

    def dispatch(self, request):
        """Run one parsed request and return the response object."""
        request_id = None
        cmd = None

        try:
            if not isinstance(request, dict):
                raise CommandError(
                    "INVALID_REQUEST", "Command must be a JSON object."
                )

            request_id = request.get("request_id")
            cmd = request.get("cmd")

            method = COMMANDS.get(cmd)

            if method is None:
                raise CommandError(
                    "UNKNOWN_COMMAND",
                    "Unknown command '{}'. Known commands: {}.".format(
                        cmd, ", ".join(self.command_names())
                    ),
                )

            return {
                "request_id": request_id,
                "ok": True,
                "cmd": cmd,
                "data": getattr(self, method)(request),
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

        except DOMAIN_ERRORS as error:
            response = {
                "request_id": request_id,
                "ok": False,
                "cmd": cmd,
            }
            response.update(_error_envelope(error))

            return response

        except Exception as error:
            # An unexpected fault must still produce a well-formed
            # answer, otherwise the PC blocks waiting for a reply.
            # type(error).__name__ - never type(error) itself, which is
            # a class object and cannot be serialized.
            debug("internal error in '{}': {}".format(cmd, error))

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

    def process_line(self, line):
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

        # The fallback lives HERE, not in the serving loop, because this
        # is the last place the request_id is known. A rescue frame
        # without it is invisible to the PC - the reader matches
        # answers by id and ignores anything else, so an unidentified
        # frame is indistinguishable from silence and the caller still
        # waits out its whole timeout.
        try:
            send_json(self.dispatch(request))

        except Exception as error:
            debug("could not send the answer:", error)

            gc.collect()

            _write_all(_minimal_error(
                request if isinstance(request, dict) else None,
                "RESPONSE_FAILED",
                "The command ran but its answer could not be sent ("
                + type(error).__name__ + "). Ask for less in one "
                "request.") + "\n")

    def serve_forever(self):
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
                self.process_line(line)

            except Exception as error:
                # process_line answers its own errors, so reaching here
                # means the answer itself could not be sent. Try once
                # more with a frame small enough to survive whatever
                # went wrong - silence is the one outcome the PC cannot
                # tell apart from a dead board.
                debug("command loop error:", error)

                try:
                    gc.collect()
                    _write_all(_minimal_error(
                        {"request_id": None},
                        "RESPONSE_FAILED",
                        "The command ran but its answer could not be "
                        "sent (" + type(error).__name__ + ").") + "\n")

                except Exception:
                    pass


# ======================================================================
# the command table
# ======================================================================
# Every command the PC may send, in one place. A name maps to a method
# name rather than to a bound method so the table is a plain constant
# that can be read, counted and tested without constructing hardware.

COMMANDS = {
    # identity and state
    "ping": "handle_ping",
    "get_status": "handle_get_status",

    # servo lifecycle and service
    "connect_servo": "handle_connect_servo",
    "disconnect_servo": "handle_disconnect_servo",
    "servo_stop": "handle_servo_stop",
    "servo_diagnostics": "handle_servo_diagnostics",
    "servo_bus_scan": "handle_servo_bus_scan",
    "get_servo_calibration": "handle_get_servo_calibration",
    "servo_test_move": "handle_servo_test_move",
    "servo_configure": "handle_servo_configure",
    "servo_torque": "handle_servo_torque",

    # carousel
    "sync_position": "handle_sync_position",
    "select_slot": "handle_select_slot",
    "move_slots": "handle_move_slots",
    "fine_adjust": "handle_fine_adjust",
    "clear_slot": "handle_clear_slot",
    "clear_all_slots": "handle_clear_all_slots",

    # acquisition
    "measure_raw": "handle_measure_raw",
    "sensor_test_raw": "handle_sensor_test_raw",
    "acquire_block": "handle_acquire_block",
    "acquire_triad": "handle_acquire_triad",
    "led_test": "handle_led_test",

    # the retained-acquisition buffer
    "list_saved_samples": "handle_list_saved_samples",
    "get_saved_sample": "handle_get_saved_sample",
    "delete_saved_samples": "handle_delete_saved_samples",
}
