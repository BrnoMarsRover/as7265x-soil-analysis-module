"""
The one serial owner.

Everything that reaches the ESP32 goes through this file. Nothing else
in the project constructs a `serial.Serial`, sets a baud rate or parses
a response frame - one owner means one place where the port is opened,
one place where it is closed, and one place to look when it is not.

WHAT THIS MODULE KNOWS

    the wire            newline-delimited JSON at 115200 over CP2102
    request ids         every request numbered, every answer matched
    timeouts            bounded waits, never an indefinite block
    failure kinds       which of seven different things went wrong

It knows nothing about samples, slots, calibration or science.

MEASURED BEHAVIOUR, NOT ASSUMED

Two facts about this hardware were measured on the bench rather than
taken from documentation, and both shape the code below.

**Opening the port resets the board.** pySerial asserts DTR and RTS on
open, and on this development board that drives the auto-reset circuit:
a plain `serial.Serial(port)` produces a POWERON_RESET banner every
time. Setting both lines low BEFORE `open()` - which requires
constructing the object unopened - leaves the board running. That is
what `open()` does here, so starting the operator client no longer
reboots an instrument that may be holding a synchronized carousel
position.

**A hardware reset is an RTS pulse with DTR low.** Measured to produce
`boot:0x13 (SPI_FAST_FLASH_BOOT)`, the application. The obvious-looking
alternative of driving both lines lands in `boot:0x3 (DOWNLOAD_BOOT)`,
the serial bootloader, where the firmware never runs at all. Reset is
therefore deliberate and lives in `hard_reset()`, used by the
deployment tool - never by the operator client on the way in.

WHY THE ERROR KIND MATTERS

"The module did not answer" has at least seven distinct causes and they
need completely different actions. Telling an operator that another
program may be holding COM4 - when COM4 opened perfectly and it was the
firmware that never answered - sends them to Task Manager for a fault
that is in the firmware. Every failure here carries a code:

    PORT_NOT_FOUND       no such port; check the cable
    PORT_BUSY            the port exists and something else holds it
    PORT_OPEN_FAILED     it exists, is free, and still would not open
    PORT_LOST            it disappeared while we were using it
    PROTOCOL_TIMEOUT     the port is open; nothing answered in time
    DEVICE_AT_REPL       MicroPython is at the >>> prompt, not serving
    MALFORMED_RESPONSE   something answered, and it was not a frame
    DEVICE_ERROR         the firmware answered "ok": false
"""

import json
import time
from datetime import datetime, timezone

try:
    import serial
    from serial.tools import list_ports

except ImportError:                                    # pragma: no cover
    serial = None
    list_ports = None


DEFAULT_BAUDRATE = 115200

# One ordinary command. Long enough for a servo transaction, short
# enough that a dead board is reported rather than waited on.
DEFAULT_TIMEOUT = 10.0

# A measurement swings the carousel 180 degrees, reads three
# illuminations with repeats, and swings back.
MEASURE_TIMEOUT = 180.0

# Any command that turns the carousel.
MOVE_TIMEOUT = 60.0

# How long to keep retrying ping before giving up on the connection.
CONNECT_TIMEOUT = 15.0

# Bytes of non-frame text kept for the diagnosis, so a boot traceback
# survives to be shown.
NOISE_LIMIT = 40


def utc_timestamp():
    return datetime.now(timezone.utc).isoformat()


# ======================================================================
# errors
# ======================================================================

class LinkError(Exception):
    """
    A transport or device failure, carrying the code that says which.

    `data` holds whatever partial result the firmware managed to
    produce - a movement that failed carries the carousel state it
    failed in.
    """

    def __init__(self, code, message, data=None):
        super().__init__(message)

        self.code = str(code)
        self.message = str(message)
        self.data = data


class DeviceError(LinkError):
    """The firmware answered, and the answer was "ok": false."""


def _classify_open_failure(port, error):
    """
    Turn a SerialException from open() into a code an operator can act on.

    pySerial wraps the Windows error in the exception TEXT rather than
    in errno, so the text is where the distinction lives. Both messages
    below were captured from this machine.
    """
    text = str(error)

    if "FileNotFoundError" in text or "cannot find the file" in text:
        return LinkError(
            "PORT_NOT_FOUND",
            "{} does not exist. The board is not plugged in, or its "
            "USB bridge has not enumerated.".format(port),
            data={"port": port, "detail": text},
        )

    if ("PermissionError" in text or "Access is denied" in text
            or "Zugriff verweigert" in text):
        return LinkError(
            "PORT_BUSY",
            "{} exists but is already open in another program. Close "
            "the other client, terminal or REPL session holding "
            "it.".format(port),
            data={"port": port, "detail": text},
        )

    return LinkError(
        "PORT_OPEN_FAILED",
        "{} could not be opened: {}".format(port, text),
        data={"port": port, "detail": text},
    )


# ======================================================================
# frame recognition
# ======================================================================

def looks_like_a_frame(text):
    """
    Whether a line that would not parse still looks like our answer.

    A response damaged in transit and a line of unrelated console noise
    need opposite responses: the first means the answer has already
    been and gone, so waiting out the timeout is pointless.
    """
    return '"request_id"' in text and ('"ok"' in text or '"cmd"' in text)


def looks_like_repl(text):
    """
    Whether the device is sitting at the MicroPython prompt.

    Measured signature: the REPL evaluates an incoming JSON object as a
    Python dict literal and echoes its repr, with single quotes, then
    prints a prompt. Both halves are unmistakable and neither can come
    from the protocol.
    """
    return ">>>" in text or ("'request_id'" in text and "'cmd'" in text)


def salvage_json(text):
    """
    Recover a frame from a line with rubbish in front of it.

    Switching an illumination LED is the largest current step the board
    makes, and a transient on the USB bridge can put bytes in front of
    an otherwise perfect frame. The firmware guards against this with a
    leading newline; this is the other half of the defence.
    """
    start = text.find("{")

    while start != -1:
        try:
            return json.loads(text[start:])

        except ValueError:
            start = text.find("{", start + 1)

    return None


def diagnose_noise(lines):
    """Turn collected non-frame output into a sentence worth reading."""
    if not lines:
        return ""

    joined = "\n".join(lines)

    if "Traceback" in joined:
        return ("The firmware raised an exception during startup. The "
                "console output was:\n\n{}".format(joined))

    if looks_like_repl(joined):
        return ("The board is at the MicroPython REPL prompt, not "
                "running the firmware. Reset it, or check that main.py "
                "is present and imports cleanly.")

    if "rst:" in joined or "boot:" in joined:
        return ("The board reset while the request was in flight. Its "
                "boot output was:\n\n{}".format(joined))

    return "Unrecognized console output:\n\n{}".format(joined)


# ======================================================================
# the link
# ======================================================================

class SerialLink:
    """
    One open port, one request at a time, one answer per request.

    Use it as a context manager so the port is released on every exit
    path - a normal quit, a failed ping, a timeout, a Ctrl+C or an
    unhandled exception anywhere in the application.
    """

    def __init__(self, port, baudrate=DEFAULT_BAUDRATE,
                 timeout=DEFAULT_TIMEOUT, connect_timeout=CONNECT_TIMEOUT,
                 verbose=False):
        if serial is None:                             # pragma: no cover
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

        # Answers that arrived damaged, and answers recovered from a
        # line with rubbish in front of them. Not fatal on their own -
        # both are handled - but a rising count is the difference
        # between one unlucky frame and an unhealthy link.
        self.corrupt_frames = 0
        self.salvaged_frames = 0
        self.last_noise = []

        # THE DAMAGED LINES THEMSELVES, not just how many there were.
        #
        # A counter says a frame arrived broken; it cannot say HOW, and
        # how is the entire diagnosis. Measured on COM4: the damage
        # seen on this hardware is exactly 64 leading bytes - one
        # CP210x USB packet - replaced by undecodable rubbish, with the
        # remaining 300 bytes of the frame byte-perfect. That shape is
        # only visible if the line is kept, and it is what distinguishes
        # a host bridge artefact from a firmware that builds bad JSON.
        #
        # Capped, because this is a diagnostic aid and not a log file.
        self.damaged_lines = []

        self._request_id = 0

    # ------------------------------------------------------------------
    # diagnostics output
    # ------------------------------------------------------------------

    def _trace(self, marker, detail=""):
        if self.verbose:
            print("   {:<16} {}".format(marker, detail))

    # ------------------------------------------------------------------
    # connection
    # ------------------------------------------------------------------

    @staticmethod
    def available_ports():
        """Every serial port the OS currently reports."""
        if list_ports is None:                         # pragma: no cover
            return []

        return [
            {"port": p.device, "description": p.description, "hwid": p.hwid}
            for p in list_ports.comports()
        ]

    def open(self):
        """
        Open the port WITHOUT resetting the board.

        The object is constructed unopened so DTR and RTS can be driven
        low before the port is opened; pySerial otherwise asserts both,
        which on this board triggers the auto-reset circuit. Measured:
        with the lines low, no boot banner appears and the firmware
        keeps running - which matters because it may be holding a
        carousel position the operator synchronized by hand.
        """
        # Construction is inside the try as well. It normally cannot
        # fail - an unopened Serial() touches no hardware - but a
        # failure there would otherwise escape as a raw
        # SerialException with no code on it, which is precisely the
        # unclassified "could not open COM4" this module exists to
        # replace.
        try:
            handle = serial.Serial()
            handle.port = self.port
            handle.baudrate = self.baudrate
            handle.bytesize = serial.EIGHTBITS
            handle.parity = serial.PARITY_NONE
            handle.stopbits = serial.STOPBITS_ONE
            handle.timeout = 0.5

            # Before open(): pySerial applies these as the port is
            # opened, which is the only moment that matters.
            handle.dtr = False
            handle.rts = False

            # Ask the OS for exclusive access. On Windows this is what
            # turns a second client into a clean PORT_BUSY instead of
            # two programs interleaving bytes on one wire, and it needs
            # no lock file of our own.
            handle.exclusive = True

            handle.open()

        except Exception as error:
            raise _classify_open_failure(self.port, error)

        self.serial = handle
        self.online = False

        self._trace("PORT OPEN", "{} at {} baud, dtr={} rts={}".format(
            self.port, self.baudrate, handle.dtr, handle.rts))

        return self

    def close(self):
        """Release the port. Safe to call twice, and never raises."""
        if self.serial is not None:
            try:
                self.serial.close()

            except Exception:
                pass

            self.serial = None
            self._trace("PORT CLOSED", self.port)

        self.online = False

    def hard_reset(self):
        """
        Reset the board into the APPLICATION, deliberately.

        An RTS pulse with DTR held low. Measured to produce
        `boot:0x13 (SPI_FAST_FLASH_BOOT)`; driving both lines instead
        lands in the serial bootloader, where the firmware never runs.

        This belongs to deployment and service. The operator client
        never calls it on the way in.
        """
        if self.serial is None:
            raise RuntimeError("Link is not open; call open() first.")

        self.serial.dtr = False
        time.sleep(0.05)
        self.serial.rts = True
        time.sleep(0.12)
        self.serial.rts = False

        # THE ONE PLACE THE RECEIVE BUFFER IS CLEARED, and the only
        # place it is correct to.
        #
        # Everything already buffered was produced by a board that no
        # longer exists - it has just been reset. Leaving it there
        # makes the next read see the PREVIOUS session's output: a
        # `>>>` prompt left behind by any mpremote call is enough to
        # make wait_online report DEVICE_AT_REPL about a board that is
        # at that moment booting perfectly. That false diagnosis was
        # observed, and it is why this line is here and nowhere else.
        try:
            self.serial.reset_input_buffer()

        except Exception:
            pass

        self.online = False
        self._trace("RESET", "RTS pulse with DTR low, buffer cleared")

    def wait_online(self, timeout=None):
        """
        Block until the module answers a ping, or say why it did not.

        Retries because a board that has just been reset spends a
        moment in its bootloader. Everything that is not a frame -
        boot banner, REPL text, a traceback - is collected and used to
        explain the failure rather than silently dropped.
        """
        if timeout is None:
            timeout = self.connect_timeout

        deadline = time.monotonic() + timeout
        last_error = None

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()

            try:
                self.request("ping", timeout=min(remaining, 2.5))
                self.online = True

                return True

            except DeviceError:
                # It answered. It is alive, which is all this asks.
                self.online = True

                return True

            except LinkError as error:
                if error.code in ("PORT_LOST", "PORT_BUSY",
                                  "PORT_NOT_FOUND", "DEVICE_AT_REPL"):
                    raise

                last_error = error

        noise = diagnose_noise(self.last_noise)

        message = (
            "{} opened, but the science module did not answer a ping "
            "within {:.0f} s.".format(self.port, timeout)
        )

        if noise:
            message = "{}\n\n{}".format(message, noise)

        elif last_error is not None:
            message = "{} ({})".format(message, last_error.message)

        code = "PROTOCOL_TIMEOUT"

        if self.last_noise and looks_like_repl("\n".join(self.last_noise)):
            code = "DEVICE_AT_REPL"

        raise LinkError(code, message, data={
            "port": self.port,
            "console": list(self.last_noise),
        })

    def __enter__(self):
        self.open()

        try:
            self.wait_online()

        except Exception:
            # The port opened; something after that failed. Release it
            # here, because the caller never got a link to close.
            self.close()

            raise

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

        `retries` re-sends the command when the answer arrives DAMAGED,
        and defaults to zero because most commands must never be
        repeated: a carousel step is relative, so a movement whose
        acknowledgement was lost has still happened and sending it
        again would move the mechanism twice. Only callers that know
        their command is a pure read - a ping, a status, an acquisition
        - ask for retries.
        """
        if self.serial is None:
            raise RuntimeError("Link is not open; call open() first.")

        if timeout is None:
            timeout = self.timeout

        attempts = int(retries) + 1
        last_damage = None

        for attempt in range(attempts):
            request_id = self._next_request_id()

            message = {"request_id": request_id, "cmd": cmd}
            message.update(payload)
            message["timestamp"] = utc_timestamp()

            line = json.dumps(message) + "\n"

            self._trace("TX JSON", line.strip())

            try:
                # NO reset_input_buffer here. Request ids are what
                # separate this answer from anything else on the wire,
                # and clearing the buffer would throw away a boot
                # traceback or a late frame that is the whole
                # explanation for the previous failure.
                self.serial.write(line.encode("utf-8"))
                self.serial.flush()

                response = self._read_response(request_id, timeout)

            except serial.SerialException as error:
                self.close()

                raise LinkError(
                    "PORT_LOST",
                    "{} disappeared while the request was in flight: "
                    "{}".format(self.port, error),
                    data={"port": self.port},
                )

            except LinkError as error:
                if error.code != "MALFORMED_RESPONSE":
                    raise

                self.corrupt_frames += 1
                last_damage = error

                if len(self.damaged_lines) < NOISE_LIMIT:
                    self.damaged_lines.append(
                        (error.data or {}).get("line", "")
                    )

                if attempt + 1 < attempts:
                    self._trace("RX DAMAGED", "asking again")

                    continue

                raise

            if not response.get("ok"):
                error = response.get("error") or {}

                raise DeviceError(
                    error.get("code", "UNKNOWN_ERROR"),
                    error.get("message", "No error message supplied."),
                    response.get("data") or error.get("details"),
                )

            return response.get("data")

        raise last_damage

    def _read_response(self, request_id, timeout):
        """
        Read until the answer to request_id arrives.

        Lines that are not frames are collected rather than discarded:
        a traceback is usually the reason no answer came, and it is
        worth more than the timeout message it would otherwise be
        replaced by.

        A line that will not parse and yet carries the fingerprint of a
        response is different: the module DID answer and the answer was
        mangled on the way. Waiting out the rest of the timeout for a
        reply that has already been and gone is the worst possible
        response, so that raises immediately and the caller decides
        whether asking again is safe.
        """
        deadline = time.monotonic() + timeout
        self.last_noise = []

        buffer = ""

        while time.monotonic() < deadline:
            # ONE BYTE, THEN WHATEVER CAME WITH IT.
            #
            # pySerial's read(n) blocks until n bytes have arrived or
            # the port timeout expires, whichever comes first. Asking
            # for a fixed 512 therefore made EVERY command pay the
            # whole port timeout, because no frame this protocol sends
            # is 512 bytes long: the answer was sitting in the driver
            # buffer while read() went on waiting for bytes that were
            # never coming. Measured on COM4, ping tracked the port
            # timeout exactly - 0.5 s -> 562 ms, 0.25 s -> 283 ms,
            # 0.1 s -> 126 ms - and had almost nothing to do with how
            # fast the board answered.
            #
            # Asking for one byte keeps the bounded block that the
            # timeout exists to provide, and in_waiting then takes the
            # rest of the frame in the same pass.
            waiting = self.serial.in_waiting
            chunk = self.serial.read(waiting if waiting > 0 else 1)

            if not chunk:
                continue

            buffer += chunk.decode("utf-8", "replace")

            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()

                if not line:
                    continue

                try:
                    frame = json.loads(line)

                except ValueError:
                    salvaged = salvage_json(line)

                    if salvaged is not None and isinstance(salvaged, dict):
                        if salvaged.get("request_id") == request_id:
                            self.salvaged_frames += 1
                            self._trace("RX SALVAGED", line[:120])

                            return salvaged

                        continue

                    if looks_like_a_frame(line):
                        raise LinkError(
                            "MALFORMED_RESPONSE",
                            "The module answered, but the answer was "
                            "damaged in transit and could not be "
                            "parsed.",
                            data={"line": line[:400]},
                        )

                    self._trace("RX NON-JSON", line[:120])

                    if len(self.last_noise) < NOISE_LIMIT:
                        self.last_noise.append(line)

                    if looks_like_repl(line):
                        # The board is at the >>> prompt, not serving.
                        # Waiting out the timeout would only delay a
                        # diagnosis that is already certain.
                        raise LinkError(
                            "DEVICE_AT_REPL",
                            "The board is at the MicroPython REPL "
                            "prompt, not running the firmware. Reset "
                            "it - any mpremote command leaves it "
                            "here.",
                            data={"line": line[:200]},
                        )

                    continue

                if not isinstance(frame, dict):
                    continue

                if frame.get("request_id") != request_id:
                    # An answer to something else, or an unsolicited
                    # frame. Not ours; keep waiting.
                    continue

                if "ok" not in frame:
                    # Our own request, echoed back. The MicroPython
                    # REPL echoes what it is sent, so this frame
                    # carries OUR request_id and would otherwise be
                    # accepted as the answer - reporting a healthy
                    # link to a board that is not running the
                    # firmware at all. Only a response has "ok".
                    self._trace("RX ECHO", line[:120])

                    if len(self.last_noise) < NOISE_LIMIT:
                        self.last_noise.append(line)

                    continue

                self._trace("RX JSON", line[:200])

                return frame

        raise LinkError(
            "PROTOCOL_TIMEOUT",
            "No answer to request {} within {:.1f} s.".format(
                request_id, timeout
            ),
            data={"console": list(self.last_noise)},
        )

    # ------------------------------------------------------------------
    # the command surface
    #
    # Thin, deliberately. Each of these exists to name a timeout and
    # whether the command is safe to repeat - the two things the caller
    # cannot be expected to know. None of them interpret the answer.
    # ------------------------------------------------------------------

    def ping(self):
        return self.request("ping", retries=2)

    def get_status(self):
        return self.request("get_status", retries=2)

    # --- servo lifecycle and service ---------------------------------

    def connect_servo(self):
        return self.request("connect_servo", timeout=MOVE_TIMEOUT)

    def disconnect_servo(self):
        return self.request("disconnect_servo", timeout=MOVE_TIMEOUT)

    def servo_stop(self):
        return self.request("servo_stop", timeout=MOVE_TIMEOUT)

    def servo_diagnostics(self):
        return self.request("servo_diagnostics", timeout=MOVE_TIMEOUT,
                            retries=1)

    def servo_bus_scan(self, ids=None, bauds=None, swap=True,
                       timeout=MOVE_TIMEOUT):
        payload = {"swap": bool(swap)}

        if ids is not None:
            payload["ids"] = list(ids)

        if bauds is not None:
            payload["bauds"] = list(bauds)

        return self.request("servo_bus_scan", timeout=timeout, **payload)

    def get_servo_calibration(self):
        return self.request("get_servo_calibration", retries=1)

    def servo_configure(self, mode=None, confirm=False):
        payload = {"confirm": bool(confirm)}

        if mode is not None:
            payload["mode"] = int(mode)

        return self.request("servo_configure", timeout=MOVE_TIMEOUT,
                            **payload)

    def servo_torque(self, enable=True):
        return self.request("servo_torque", timeout=MOVE_TIMEOUT,
                            enable=bool(enable))

    def servo_test_move(self, kind, repeat=1, degrees=None, hold_ms=None,
                        confirm=True, timeout=MOVE_TIMEOUT):
        payload = {"kind": kind, "repeat": int(repeat),
                   "confirm": bool(confirm)}

        if degrees is not None:
            payload["degrees"] = float(degrees)

        if hold_ms is not None:
            payload["hold_ms"] = int(hold_ms)

        return self.request("servo_test_move", timeout=timeout, **payload)

    # --- carousel ----------------------------------------------------

    def sync_position(self, load_slot=None, scan_slot=None):
        payload = {}

        if load_slot is not None:
            payload["load_slot"] = int(load_slot)

        if scan_slot is not None:
            payload["scan_slot"] = int(scan_slot)

        return self.request("sync_position", **payload)

    def select_slot(self, slot, sample_id=None):
        payload = {"slot": int(slot)}

        if sample_id is not None:
            payload["sample_id"] = str(sample_id)

        return self.request("select_slot", timeout=MOVE_TIMEOUT, **payload)

    def move_slots(self, direction, slots=1):
        return self.request("move_slots", timeout=MOVE_TIMEOUT,
                            direction=direction, slots=int(slots))

    def fine_adjust(self, degrees):
        return self.request("fine_adjust", timeout=MOVE_TIMEOUT,
                            degrees=float(degrees))

    def clear_slot(self, slot):
        return self.request("clear_slot", slot=int(slot))

    def clear_all_slots(self):
        return self.request("clear_all_slots")

    # --- acquisition -------------------------------------------------

    def measure_raw(self, slot, sample_id=None, repeats=None):
        payload = {"slot": int(slot)}

        if sample_id is not None:
            payload["sample_id"] = str(sample_id)

        if repeats is not None:
            payload["repeats"] = int(repeats)

        return self.request("measure_raw", timeout=MEASURE_TIMEOUT,
                            **payload)

    def sensor_test_raw(self, force_reinit=False, repeats=None):
        payload = {"force_reinit": bool(force_reinit)}

        if repeats is not None:
            payload["repeats"] = int(repeats)

        return self.request("sensor_test_raw", timeout=MEASURE_TIMEOUT,
                            retries=1, **payload)

    def acquire_block(self, illumination, repeats):
        return self.request("acquire_block", timeout=MEASURE_TIMEOUT,
                            retries=1, illumination=illumination,
                            repeats=int(repeats))

    def acquire_triad(self, repeats=None):
        payload = {}

        if repeats is not None:
            payload["repeats"] = int(repeats)

        return self.request("acquire_triad", timeout=MEASURE_TIMEOUT,
                            retries=1, **payload)

    def led_test(self, hold_ms=400):
        return self.request("led_test", timeout=MEASURE_TIMEOUT,
                            hold_ms=int(hold_ms))

    # --- the device's retained-acquisition buffer ---------------------

    def list_saved_samples(self):
        return self.request("list_saved_samples", retries=1)

    def get_saved_sample(self, sample_id):
        return self.request("get_saved_sample", retries=1,
                            sample_id=str(sample_id))

    def delete_saved_samples(self):
        return self.request("delete_saved_samples")
