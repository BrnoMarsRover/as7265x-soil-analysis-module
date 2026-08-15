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

# Commands that move the carousel: whole 90 deg slot steps plus settling.
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

# Budget for the module to come up after the port is opened. Opening a
# serial port resets many ESP32 development boards, and MicroPython then
# needs to boot and run main.py before it can answer.
CONNECT_TIMEOUT = 25.0


def utc_timestamp():
    """ISO-8601 UTC timestamp attached to every outgoing command.

    The ESP32 has no reliable wall clock, so it never invents one.
    """
    return datetime.now(timezone.utc).isoformat()


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

    def request(self, cmd, timeout=None, **payload):
        """
        Send one command and return its ``data`` object.

        Raises LinkError if the module answered ``ok: false``; the
        error's ``data`` carries whatever partial result the firmware
        managed to produce.
        """
        if self.serial is None:
            raise RuntimeError("Link is not open; call open() first.")

        if timeout is None:
            timeout = self.timeout

        request_id = self._next_request_id()

        message = {"request_id": request_id, "cmd": cmd}
        message.update(payload)
        message.setdefault("timestamp", utc_timestamp())

        line = json.dumps(message) + "\n"

        if self.verbose:
            print(">>", line.strip())

        self.serial.reset_input_buffer()
        self.serial.write(line.encode("utf-8"))
        self.serial.flush()

        response = self._read_response(request_id, timeout)

        if not response.get("ok"):
            error = response.get("error") or {}

            raise LinkError(
                error.get("code", "UNKNOWN_ERROR"),
                error.get("message", "No error message supplied."),
                response.get("data") or error.get("details"),
            )

        return response.get("data")

    def _read_response(self, request_id, timeout):
        """
        Read until the answer to request_id arrives.

        Lines that are not valid JSON are skipped rather than treated as
        protocol failures, but they are kept and reported if the wait
        times out - a traceback is usually the reason no answer came.
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
                text = line.decode("utf-8", "replace")[:120]

                if self.verbose:
                    print("?? (not JSON)", text)

                skipped.append(text)

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
        return self.request("get_status")

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
            "sensor_test_raw", timeout=MEASUREMENT_TIMEOUT, **payload
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
            illumination=illumination,
            repeats=int(repeats),
        )

    def acquire_triad(self, repeats=None):
        """WHITE, UV and IR, without moving the carousel."""
        payload = {}

        if repeats:
            payload["repeats"] = int(repeats)

        return self.request(
            "acquire_triad", timeout=ACQUISITION_TIMEOUT, **payload
        )

    def led_test(self, hold_ms=400):
        """Exercise each lamp on its own and read its state back."""
        return self.request(
            "led_test", timeout=MOVE_TIMEOUT, hold_ms=int(hold_ms)
        )

    def list_saved_samples(self):
        """Index of the raw acquisitions the ESP32 is still holding."""
        return self.request("list_saved_samples")

    def get_saved_sample(self, sample_id):
        """One retained acquisition, fetched individually to stay small."""
        return self.request("get_saved_sample", sample_id=sample_id)

    def delete_saved_samples(self):
        """
        Delete every acquisition held on the ESP32.

        Touches nothing else: not the PC archive, not physical slot
        state, and certainly not the BD reference data.
        """
        return self.request("delete_saved_samples")

    # ------------------------------------------------------------------
    # servo calibration
    # ------------------------------------------------------------------

    def get_servo_calibration(self):
        return self.request("get_servo_calibration")

    def set_servo_calibration(self, values=None, reset=False):
        if reset:
            return self.request("set_servo_calibration", reset=True)

        return self.request("set_servo_calibration", values=values or {})

    def servo_test_move(self, kind, repeat=1, hold_ms=None):
        """
        Run one calibration movement.

        Generously timed: a deliberately slow move repeated a few times
        can take most of a minute, and cutting it short mid-sweep would
        leave the carousel somewhere unknown.
        """
        payload = {"kind": kind, "repeat": int(repeat)}

        if hold_ms is not None:
            payload["hold_ms"] = int(hold_ms)

        return self.request(
            "servo_test_move", timeout=MEASUREMENT_TIMEOUT, **payload
        )

    def servo_stop(self):
        return self.request("servo_stop", timeout=MOVE_TIMEOUT)
