"""
Serial link to the ESP32 hardware controller.

    one command  = one JSON object followed by "\\n"
    one response = one JSON object followed by "\\n"

carried over the development board's USB / CP2102 console at 115200
baud. The PC needs to know nothing about the ESP32 side of that link:
no GPIO numbers, no UART peripheral, only the port name. The same cable
also powers the board.

Because that port is the MicroPython console it also carries the boot
banner, REPL text and tracebacks. Any line that is not valid JSON is
skipped, and the connection counts as established only once a ping has
been answered.

This module is transport plus a thin command API. It contains no
science and no user interface, so it is importable by future rover
software that has its own.
"""

import json
import time
from datetime import datetime, timezone

try:
    import serial
except ImportError:  # pragma: no cover - environment dependent
    serial = None


DEFAULT_BAUDRATE = 115200

# Ordinary query/response round trips: ping, status.
DEFAULT_TIMEOUT = 10.0

# Commands that move the carousel: whole slot steps, each one commanded
# in encoder counts, polled to completion and then verified.
MOVE_TIMEOUT = 30.0

# A full measurement: 180 deg out, mechanical settling, illumination
# settling, a full 18-channel integration, then 180 deg back. Far slower
# than a ping, and a premature timeout is exactly what makes an operator
# think nothing happened.
MEASUREMENT_TIMEOUT = 180.0

# A calibration block is ten one-shot conversions at 100 integration
# cycles, plus settling between each. Generous on purpose: cutting one
# short mid-block would waste the whole calibration step.
ACQUISITION_TIMEOUT = 180.0

# How many times a PURE READ is re-sent when its answer arrives damaged.
#
# Only commands that change nothing carry this: an acquisition, a status,
# a diagnostic. Anything that moves the carousel is sent exactly once,
# because a relative movement whose acknowledgement was lost has still
# happened. See ESP32Link.request.
READ_RETRIES = 2

# Budget for the module to come up after the port is opened. Opening a
# serial port resets many ESP32 development boards, and MicroPython then
# needs to boot and run main.py before it can answer.
CONNECT_TIMEOUT = 25.0


def utc_timestamp():
    """ISO-8601 UTC timestamp attached to every outgoing command.

    The ESP32 has no reliable wall clock, so it never invents one.
    """
    return datetime.now(timezone.utc).isoformat()


class CorruptFrameError(Exception):
    """
    A response arrived, and it was unreadable.

    Kept apart from TimeoutError on purpose. "Nothing came back" and "the
    answer came back damaged" have different causes and different cures:
    the first means the module is not answering, the second means the
    bytes were mangled between the two ends and asking again will very
    probably work.
    """

    def __init__(self, message, sample=None):
        super().__init__(message)

        self.message = message
        self.sample = sample or ""


class LinkError(Exception):
    """The module rejected a command, or the link itself failed."""

    def __init__(self, code, message, data=None):
        super().__init__("{}: {}".format(code, message))

        self.code = code
        self.message = message
        self.data = data or {}


def diagnose_noise(skipped):
    """
    Work out what the non-JSON lines actually mean.

    The most common failure is not a bad cable: it is the ESP32 sitting
    at the MicroPython REPL because main.py did not start. The REPL
    echoes the command back and prints the Python repr of it, so
    single-quoted dicts and ">>>" are the fingerprint.
    """
    joined = " ".join(skipped)

    if ">>>" in joined or "'cmd':" in joined or "'request_id':" in joined:
        return "\n".join([
            "The ESP32 is sitting at the MicroPython REPL - main.py is "
            "NOT running.",
            "The board echoed the command back and printed it as a "
            "Python dict, which is what the REPL does.",
            "",
            "Usual cause: main.py failed to import, most often because "
            "one of its modules is missing on the device.",
            "",
            "Check what is actually on the board:",
            "    py -m mpremote connect COM4 fs ls",
            "",
            "and read the import error with:",
            "    py -m mpremote connect COM4 repl",
            "    >>> import main",
        ])

    if "Traceback" in joined or "Error" in joined:
        return "\n".join([
            "The ESP32 printed a Python traceback: main.py crashed.",
            "Open the REPL to read it:",
            "    py -m mpremote connect COM4 repl",
        ])

    if "ets " in joined or "rst:0x" in joined:
        return (
            "The board is still booting. Try again, or raise "
            "--connect-timeout."
        )

    return None


# Fragments that only ever appear inside a response frame. A line that
# carries one of these and still will not parse is a damaged answer, not
# console noise.
FRAME_FINGERPRINTS = ('"request_id"', '"ok":', '"data":', '"cmd":')


def salvage_json(text):
    """
    Recover a JSON object from a line with rubbish in front of it.

    The cheap case, and worth trying first: the frame itself is intact
    but something landed on the console immediately before it. Parsing
    from the first '{' recovers the whole response.

    Returns None when there is nothing recoverable - including the case
    where the damage ate into the object itself, which no amount of
    scanning can undo.
    """
    start = text.find("{")

    while start != -1:
        try:
            value = json.loads(text[start:])

        except ValueError:
            start = text.find("{", start + 1)

            continue

        if isinstance(value, dict):
            return value

        start = text.find("{", start + 1)

    return None


def looks_like_a_frame(text):
    """Was this unparseable line a response of ours, damaged in transit?"""
    return any(mark in text for mark in FRAME_FINGERPRINTS)


class ESP32Link:
    """JSON-over-USB client for the ESP32 hardware controller."""

    def __init__(
        self,
        port,
        baudrate=DEFAULT_BAUDRATE,
        timeout=DEFAULT_TIMEOUT,
        connect_timeout=CONNECT_TIMEOUT,
        verbose=False,
    ):
        if serial is None:
            raise RuntimeError(
                "pyserial is not installed. Install it with:\n"
                "    py -m pip install -r requirements.txt"
            )

        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.verbose = verbose

        self.serial = None
        self.online = False
        self.last_noise = []

        # Answers that arrived damaged, and answers recovered from a line
        # with rubbish in front of them. Not fatal on their own - both are
        # handled - but a rising count is the difference between "one
        # unlucky frame" and "this link is not healthy".
        self.corrupt_frames = 0
        self.salvaged_frames = 0

        self._request_id = 0

    # ------------------------------------------------------------------
    # connection
    # ------------------------------------------------------------------

    def open(self):
        self.serial = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.5,
        )

        # Drop anything left over from a previous session so the first
        # response cannot be a stale frame.
        self.serial.reset_input_buffer()
        self.serial.reset_output_buffer()

        self.online = False

        return self

    def wait_online(self, timeout=None):
        """
        Block until the module answers a ping.

        Opening the port resets development boards whose auto-reset
        circuit is wired to DTR/RTS. Rather than fighting that, this
        tolerates it: boot banner, REPL text and partial lines are all
        skipped and ping is retried until valid JSON arrives.
        """
        if timeout is None:
            timeout = self.connect_timeout

        deadline = time.monotonic() + timeout
        last_error = None

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()

            try:
                self.request("ping", timeout=min(remaining, 2.0))
                self.online = True

                return True

            except (TimeoutError, LinkError, ValueError) as error:
                last_error = error

            except serial.SerialException as error:
                # The port can genuinely disappear mid-reset on some USB
                # bridges; that is fatal, not something to retry.
                raise LinkError(
                    "PORT_LOST",
                    "Serial port disappeared: {}".format(error),
                )

        message = (
            "The science module did not answer a ping within {:.0f} s."
            .format(timeout)
        )

        hint = diagnose_noise(self.last_noise)

        if hint:
            message = "{}\n\n{}".format(message, hint)
        elif last_error:
            message = "{} ({})".format(message, last_error)

        raise TimeoutError(message)

    def close(self):
        if self.serial is not None:
            self.serial.close()
            self.serial = None

        self.online = False

    def __enter__(self):
        self.open()
        self.wait_online()

        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

        return False

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------

    def _next_request_id(self):
        self._request_id += 1

        return str(self._request_id)

    def request(self, cmd, timeout=None, retries=0, **payload):
        """
        Send one command and return its ``data`` object.

        Raises LinkError if the module answered ``ok: false``; the
        error's ``data`` carries whatever partial result the firmware
        managed to produce.

        `retries` re-sends the command when the answer arrives DAMAGED,
        and it defaults to zero because most commands must never be
        repeated: a carousel step is relative, so a movement whose
        acknowledgement was lost has still happened and sending it again
        would move the mechanism twice. Only callers that know their
        command is a pure read - a ping, a status, an acquisition - ask
        for retries.
        """
        if self.serial is None:
            raise RuntimeError("Link is not open; call open() first.")

        if timeout is None:
            timeout = self.timeout

        attempts = int(retries) + 1
        last_error = None

        for attempt in range(attempts):
            request_id = self._next_request_id()

            message = {"request_id": request_id, "cmd": cmd}
            message.update(payload)
            message["timestamp"] = utc_timestamp()

            line = json.dumps(message) + "\n"

            if self.verbose:
                print(">>", line.strip())

            self.serial.reset_input_buffer()
            self.serial.write(line.encode("utf-8"))
            self.serial.flush()

            try:
                response = self._read_response(request_id, timeout)

            except CorruptFrameError as error:
                self.corrupt_frames += 1
                last_error = error

                if attempt + 1 < attempts:
                    if self.verbose:
                        print("?? damaged answer, asking again:",
                              error.message)

                    continue

                # Out of attempts. Reported as a timeout because that is
                # what the caller has to handle either way, but the text
                # says plainly that an answer DID come back - the two
                # faults have completely different causes.
                raise TimeoutError(
                    "Request {} was answered, but the answer was damaged "
                    "in transit and could not be read after {} "
                    "attempt(s).\n{}".format(
                        request_id, attempts, error.message
                    )
                )

            if not response.get("ok"):
                error = response.get("error") or {}

                raise LinkError(
                    error.get("code", "UNKNOWN_ERROR"),
                    error.get("message", "No error message supplied."),
                    response.get("data") or error.get("details"),
                )

            return response.get("data")

        raise TimeoutError(str(last_error))

    def _read_response(self, request_id, timeout):
        """
        Read until the answer to request_id arrives.

        Lines that are not valid JSON are skipped rather than treated as
        protocol failures, but they are kept and reported if the wait
        times out - a traceback is usually the reason no answer came.

        A line that will not parse and yet carries the fingerprint of a
        response frame is a different matter: the module DID answer and
        the answer was mangled on the way. Waiting out the rest of the
        timeout for a reply that has already been and gone is the worst
        possible response to that, so it raises CorruptFrameError
        immediately and the caller decides whether asking again is safe.
        """
        deadline = time.monotonic() + timeout
        skipped = []
        self.last_noise = skipped

        while True:
            remaining = deadline - time.monotonic()

            if remaining <= 0:
                detail = ""

                if skipped:
                    detail = " Device also sent non-JSON output: {}".format(
                        " | ".join(skipped[-3:])
                    )

                raise TimeoutError(
                    "No response to request {} within {:.0f} s.{}".format(
                        request_id, timeout, detail
                    )
                )

            self.serial.timeout = min(remaining, 0.5)
            raw = self.serial.readline()

            if not raw:
                continue

            line = raw.strip()

            if not line:
                continue

            try:
                response = json.loads(line.decode("utf-8", "replace"))

            except (ValueError, UnicodeDecodeError):
                text = line.decode("utf-8", "replace")

                # Cheap case first: the frame is whole, something just
                # landed in front of it. Recovering it costs nothing and
                # saves an acquisition.
                recovered = salvage_json(text)

                if isinstance(recovered, dict) and "ok" in recovered:
                    self.salvaged_frames += 1

                    if self.verbose:
                        print("?? recovered a frame from a damaged line")

                    response = recovered

                elif looks_like_a_frame(text):
                    raise CorruptFrameError(
                        "A response frame arrived damaged: {} byte(s), "
                        "unparseable, but carrying response fields. This "
                        "is line noise on the USB console, not a module "
                        "fault.".format(len(text)),
                        sample=text[:160],
                    )

                else:
                    if self.verbose:
                        print("?? (not JSON)", text[:120])

                    skipped.append(text[:120])

                    continue

            if not isinstance(response, dict):
                skipped.append("non-object JSON: {}".format(line[:80]))

                continue

            if self.verbose:
                print("<<", json.dumps(response)[:400])

            answered = response.get("request_id")

            # Accept our own answer, plus frame-level errors that carry
            # no request id (invalid JSON, oversized command).
            if answered is not None and answered != request_id:
                continue

            if "ok" not in response:
                raise LinkError(
                    "MALFORMED_RESPONSE",
                    "Response to request {} has no 'ok' field.".format(
                        request_id
                    ),
                )

            return response

    # ------------------------------------------------------------------
    # hardware command API
    # ------------------------------------------------------------------

    def ping(self):
        return self.request("ping")

    def get_status(self):
        return self.request("get_status", retries=READ_RETRIES)

    def sync_load_slot(self, load_slot):
        """Declare which slot is now under the loading hole. Moves nothing."""
        return self.request("sync_position", load_slot=int(load_slot))

    def select_slot(self, slot, sample_id=None):
        payload = {"slot": int(slot)}

        if sample_id:
            payload["sample_id"] = sample_id

        return self.request("select_slot", timeout=MOVE_TIMEOUT, **payload)

    def move_slots(self, direction, slots=1):
        return self.request(
            "move_slots",
            timeout=MOVE_TIMEOUT,
            direction=direction,
            slots=int(slots),
        )

    def fine_adjust(self, degrees):
        return self.request(
            "fine_adjust", timeout=MOVE_TIMEOUT, degrees=float(degrees)
        )

    def clear_slot(self, slot):
        return self.request("clear_slot", slot=int(slot))

    def clear_all_slots(self):
        """Free every physical slot. Deletes no Sample record anywhere."""
        return self.request("clear_all_slots")

    def measure_raw(self, slot, sample_id=None, repeats=None):
        payload = {"slot": int(slot)}

        if sample_id:
            payload["sample_id"] = sample_id

        if repeats:
            payload["repeats"] = int(repeats)

        return self.request(
            "measure_raw", timeout=MEASUREMENT_TIMEOUT, **payload
        )

    def sensor_test_raw(self, force_reinit=False, repeats=None):
        payload = {"force_reinit": bool(force_reinit)}

        if repeats:
            payload["repeats"] = int(repeats)

        return self.request(
            "sensor_test_raw", timeout=MEASUREMENT_TIMEOUT,
            retries=READ_RETRIES, **payload
        )

    def acquire_block(self, illumination, repeats):
        """
        One illumination, repeated. 'dark' means every lamp off.

        The building block of a calibration; every individual reading
        comes back so the PC can aggregate and archive them.
        """
        return self.request(
            "acquire_block",
            timeout=ACQUISITION_TIMEOUT,
            retries=READ_RETRIES,
            illumination=illumination,
            repeats=int(repeats),
        )

    def acquire_triad(self, repeats=None):
        """WHITE, UV and IR, without moving the carousel."""
        payload = {}

        if repeats:
            payload["repeats"] = int(repeats)

        return self.request(
            "acquire_triad", timeout=ACQUISITION_TIMEOUT,
            retries=READ_RETRIES, **payload
        )

    def led_test(self, hold_ms=400):
        """Exercise each lamp on its own and read its state back."""
        return self.request(
            "led_test", timeout=MOVE_TIMEOUT, hold_ms=int(hold_ms)
        )

    def list_saved_samples(self):
        """Index of the raw acquisitions the ESP32 is still holding."""
        return self.request("list_saved_samples", retries=READ_RETRIES)

    def get_saved_sample(self, sample_id):
        """One retained acquisition, fetched individually to stay small."""
        return self.request(
            "get_saved_sample", retries=READ_RETRIES, sample_id=sample_id
        )

    def delete_saved_samples(self):
        """
        Delete every acquisition held on the ESP32.

        Touches nothing else: not the PC archive, not physical slot
        state, and certainly not the BD reference data.
        """
        return self.request("delete_saved_samples")

    # ------------------------------------------------------------------
    # ST3215 servo
    # ------------------------------------------------------------------

    def get_servo_options(self):
        """Which actuators the firmware supports, and which is selected."""
        return self.request("get_servo_options")

    def select_servo(self, servo):
        """
        State which servo is physically installed.

        Always invalidates the carousel position on the ESP32: physical
        position state cannot survive a change of actuator. Pass "none" to
        release the current backend and block movement again.
        """
        return self.request(
            "select_servo", timeout=MOVE_TIMEOUT, servo=str(servo)
        )

    def servo_diagnostics(self):
        """Communication and telemetry check. Moves nothing."""
        return self.request("servo_diagnostics", retries=READ_RETRIES)

    def servo_bus_scan(self, ids=None, bauds=None, swap=True,
                       timeout_ms=None):
        """
        Search the servo bus for anything that answers. Moves nothing.

        Works with no servo selected - it is the tool for exactly that
        situation. Generously timed: a full ID sweep is several hundred
        probes and must not be cut off half way, because a scan that
        stops early reports "nothing found" for a servo it never reached.
        """
        payload = {"swap": bool(swap)}

        if ids is not None:
            payload["ids"] = ids

        if bauds is not None:
            payload["bauds"] = bauds

        if timeout_ms is not None:
            payload["timeout_ms"] = int(timeout_ms)

        return self.request(
            "servo_bus_scan", timeout=MEASUREMENT_TIMEOUT,
            retries=READ_RETRIES, **payload
        )

    def get_servo_calibration(self):
        """The active backend's tunables. Shape differs by backend."""
        return self.request("get_servo_calibration")

    def set_servo_calibration(self, values=None, reset=False):
        """
        Override the active backend's calibration in RAM.

        Nothing the ST3215 exposes is runtime-editable; it answers
        SERVO_NOT_SUPPORTED rather than accepting a value it would ignore.
        """
        if reset:
            return self.request("set_servo_calibration", reset=True)

        return self.request("set_servo_calibration", values=values or {})

    def servo_configure(self, mode=None, confirm=False):
        """
        Write the carousel's operating mode into the servo EPROM.

        Requires confirm=True, because it changes non-volatile servo
        state. Called once per servo, at bring-up.
        """
        payload = {"confirm": bool(confirm)}

        if mode is not None:
            payload["mode"] = int(mode)

        return self.request("servo_configure", timeout=MOVE_TIMEOUT, **payload)

    def servo_torque(self, enable=True):
        """
        Enable or release the servo's holding torque.

        Releasing it is what lets the carousel be turned by hand, and it
        invalidates the tracked position on the ESP32.
        """
        return self.request(
            "servo_torque", timeout=MOVE_TIMEOUT, enable=bool(enable)
        )

    def servo_test_move(self, kind, repeat=1, degrees=None, hold_ms=None,
                        confirm=False):
        """
        Run one movement test on the active backend.

        The available kinds come from the firmware rather than from a
        list compiled into this client, so a movement test added to the
        driver appears in the menu without a PC change.

        Generously timed: a deliberately slow movement repeated a few
        times can take most of a minute, and cutting it short mid-sweep
        would leave the carousel somewhere unknown.
        """
        payload = {
            "kind": kind,
            "repeat": int(repeat),
            "confirm": bool(confirm),
        }

        if degrees is not None:
            payload["degrees"] = float(degrees)

        if hold_ms is not None:
            payload["hold_ms"] = int(hold_ms)

        return self.request(
            "servo_test_move", timeout=MEASUREMENT_TIMEOUT, **payload
        )

    def servo_stop(self):
        return self.request("servo_stop", timeout=MOVE_TIMEOUT)
