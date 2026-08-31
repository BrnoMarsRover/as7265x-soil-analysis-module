"""
The one serial owner.

Everything that reaches the ESP32 goes through this file. Nothing else
in the project constructs a `serial.Serial`, sets a baud rate or parses
a response frame - one owner means one place where the port is opened,
one place where it is closed, and one place to look when it is not.

WHAT THIS MODULE KNOWS

    the wire            newline-delimited JSON at 115200 over CP2102
    request ids         a session nonce and a counter, every answer
                        matched against both
    timeouts            bounded waits, never an indefinite block
    failure kinds       which of ten different things went wrong

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

"The module did not answer" has at least nine distinct causes and they
need completely different actions. Telling an operator that another
program may be holding COM4 - when COM4 opened perfectly and it was the
firmware that never answered - sends them to Task Manager for a fault
that is in the firmware. Every failure here carries a code:

    PORT_NOT_FOUND       no such port; check the cable
    PORT_BUSY            the port exists and something else holds it
    PORT_DENIED          it exists, nothing holds it, and this account
                         may not open it - Linux serial group
    PORT_OPEN_FAILED     it exists, is free, and still would not open
    PORT_LOST            it disappeared while we were using it
    PROTOCOL_TIMEOUT     the port is open; nothing answered in time
    DEVICE_AT_REPL       MicroPython is at the >>> prompt, not serving
    MALFORMED_RESPONSE   something answered, and it was not a frame
    INVALID_REQUEST      we were asked to send something that is not
                         legal JSON, and refused before sending it
    DEVICE_ERROR         the firmware answered "ok": false

MATCHING AN ANSWER TO ITS QUESTION

Three things must agree before a frame is accepted as the answer to a
request: the request id, the presence of `ok`, and the command name.

The id is `<24 random bits>-<counter>`, and the randomness is not
decoration. Two earlier designs collided ACROSS SESSIONS while looking
perfectly reasonable:

    a counter from 1        every session's first request was "1", and
                            open() deliberately does not clear the
                            receive buffer, so a dead client's answer
                            was the next client's first result

    a clock-seeded counter  `time.time()*1000 % 1000000` wraps every
                            1000 seconds, so two clients started
                            sixteen minutes apart produced identical
                            ids - and `wait_online` makes `ping` the
                            first command of every session, so the
                            command check could not tell them apart

The command name is the second defence, for the case where the first
is defeated anyway. See `_matches_request` and
Tests/software/contracts/test_request_identity.py.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

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

# THE LONGEST LINE THIS READER WILL ACCUMULATE BEFORE GIVING UP ON IT.
#
# The framing is newline-delimited, so a device that talks without ever
# sending a newline is a device whose "line" never ends. Measured: a
# stuck stream at 115200 fills 2.08 MB during one MEASURE_TIMEOUT, all
# of it in a single Python string that is copied on every append - so
# the cost is quadratic as well as unbounded, and this is exactly the
# shape a half-crashed board or a noisy bridge produces.
#
# The firmware has had the matching rule from the start
# (config.MAX_COMMAND_BYTES, 4096); the host had none, which left the
# protection one-sided.
#
# 64 KiB is chosen against the real numbers rather than a round guess.
# The largest legitimate frame this firmware can build is
# `sensor_test_raw` at MAX_REPEATS = 25, measured at 16,454 bytes, so
# this is roughly four times the worst legal case - wide enough that no
# real response can ever be truncated by it, small enough that a stuck
# stream costs 64 KiB instead of megabytes.
#
# An over-long line is DISCARDED, not turned into an error: garbage on
# the wire is not an answer, and resynchronizing at the next newline is
# what lets the real frame behind it still be read. If none arrives,
# the ordinary PROTOCOL_TIMEOUT says so.
MAX_FRAME_BYTES = 65536

# The command surface this client was written against.
#
# `ESP32/config.py` bumps PROTOCOL_VERSION whenever a command is added,
# removed or renamed, or a response field the PC depends on changes. So
# a device reporting a different number is running firmware that does
# not match this source, and the failure that produces is a dead key or
# a missing command diagnosed as a hardware fault.
#
# This exists to make "new PC code, old ESP32 firmware" say so on the
# first ping instead of three screens later. It is a WARNING, not a
# refusal: an operator at the bench may deliberately point a newer
# client at an older board to find out what changed, and refusing to
# connect would take that away.
#
# The strong check is `device.py verify`, which rebuilds every module
# and compares SHA256 against the device. This is the cheap one that
# runs every time anybody connects.
EXPECTED_PROTOCOL_VERSION = 2


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


# Substrings that identify an open() failure, per cause. pySerial wraps
# the OS error in the exception TEXT rather than in errno, so the text is
# where the distinction lives - and the text differs by platform, which
# is why each cause lists more than one spelling.
#
# The Windows strings were captured on this machine. The POSIX strings
# are what `serialposix.py` builds from the underlying OSError, and they
# matter because the main computer is a Linux machine: without them a
# missing /dev/ttyUSB0 and a user who is not in `dialout` both fall
# through to PORT_OPEN_FAILED, which names no action at all.

_NOT_FOUND_MARKERS = (
    "FileNotFoundError",                # Windows, pySerial's repr
    "cannot find the file",             # Windows, English
    "No such file or directory",        # POSIX, ENOENT
)

_BUSY_MARKERS = (
    "Access is denied",                 # Windows, English
    "Zugriff verweigert",               # Windows, German
    "PermissionError",                  # Windows, pySerial's repr
    "could not exclusively lock",       # POSIX, our own flock request
    "Resource temporarily unavailable",  # POSIX, EAGAIN from flock
    "Device or resource busy",          # POSIX, EBUSY
)

# POSIX only, and checked AFTER the busy markers: on Windows a denial IS
# another program holding the port, while on Linux it is almost always
# the login account missing from the serial group.
_DENIED_MARKERS = (
    "Permission denied",                # POSIX, EACCES
)


def _matches(text, markers):
    lowered = text.lower()

    return any(marker.lower() in lowered for marker in markers)


def _classify_open_failure(port, error):
    """
    Turn a SerialException from open() into a code an operator can act on.

    Four outcomes, because they need four different actions: plug the
    board in, close the other program, add yourself to the serial group,
    or read the raw text because we genuinely do not know.
    """
    text = str(error)

    if _matches(text, _NOT_FOUND_MARKERS):
        return LinkError(
            "PORT_NOT_FOUND",
            "{} does not exist. The board is not plugged in, or its "
            "USB bridge has not enumerated.".format(port),
            data={"port": port, "detail": text},
        )

    if _matches(text, _BUSY_MARKERS):
        return LinkError(
            "PORT_BUSY",
            "{} exists but is already open in another program. Close "
            "the other client, terminal or REPL session holding "
            "it.".format(port),
            data={"port": port, "detail": text},
        )

    if _matches(text, _DENIED_MARKERS):
        return LinkError(
            "PORT_DENIED",
            "{} exists but this account may not open it. On Linux the "
            "serial devices belong to a group - usually 'dialout', "
            "'uucp' on some distributions - and the account has to be a "
            "member:\n"
            "    sudo usermod -aG dialout $USER\n"
            "then log out and back in, because group membership is read "
            "at login. Nothing is wrong with the board.".format(port),
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

        # RecursionError, not only ValueError. CPython's JSON scanner
        # recurses once per level of nesting, and a line carrying about
        # 17,000 opening brackets exhausts the stack instead of
        # returning a parse error. RecursionError is not a ValueError,
        # so it escaped this handler and every other one in the module
        # and killed the application from a line of console noise.
        #
        # MAX_FRAME_BYTES makes that line unlikely to survive the reader
        # at all, but the threshold is a property of the interpreter's
        # stack rather than of this protocol - it moves with the Python
        # build and the platform - so the parse is guarded here as well
        # rather than relying on a byte count to stay below it.
        except (ValueError, RecursionError):
            start = text.find("{", start + 1)

    return None


# How much of a damaged line is preserved, and how much context is kept
# around the point it stopped parsing.
#
# BOUNDED ON PURPOSE. The whole line can be up to MAX_FRAME_BYTES, and a
# transport diagnostic that attaches 64 KB to an exception - which is
# then appended to `damaged_lines`, up to NOISE_LIMIT times - turns a
# link fault into a memory problem during the one session where the link
# is already misbehaving. A prefix, a suffix and a window around the
# fault answer every question the shape of the damage can answer; the
# middle of an intact 4 KB frame answers none of them.
DAMAGE_PREFIX_CHARS = 240
DAMAGE_SUFFIX_CHARS = 240
DAMAGE_WINDOW_CHARS = 96

# The CP210x moves data in 64-byte USB packets. A frame that loses or
# repeats a whole packet fails at an offset that is a multiple of 64,
# and that is a different fault from a single flipped byte: one is the
# bridge or the host driver, the other is the wire. The alignment is
# REPORTED, never concluded from - a 64-aligned offset can happen by
# chance, and one sample is not a diagnosis.
CP210X_PACKET_BYTES = 64
CP210X_ALIGNMENT_SLACK = 2


def _field_from(text, name):
    """
    Pull one scalar field out of a line too damaged to parse.

    Deliberately crude string work rather than a parser: the input is by
    definition not valid JSON, and the point is to recover the request
    id and command so a damaged frame can be tied to the request that
    provoked it. Returns None when it cannot be read, never a guess.
    """
    marker = '"{}"'.format(name)
    at = text.find(marker)

    if at == -1:
        return None

    at = text.find(":", at + len(marker))

    if at == -1:
        return None

    rest = text[at + 1:at + 80].strip()

    if not rest:
        return None

    if rest[0] == '"':
        end = rest.find('"', 1)

        return rest[1:end] if end != -1 else None

    for terminator in (",", "}"):
        end = rest.find(terminator)

        if end != -1:
            rest = rest[:end]

    rest = rest.strip()

    return rest or None


def describe_damage(line, parse_error=None, counters=None,
                    terminated_by_newline=True, port_present=None):
    """
    Everything a single damaged frame can be asked, within a fixed size.

    WHY THIS EXISTS. The MALFORMED_RESPONSE error used to carry
    `line[:400]` and nothing else. Measured on the bench, 2026-08-27:
    HW-B1-009 caught one damaged frame in 200 requests, and the evidence
    it preserved was the first 400 characters - all of them intact,
    because the damage was further in. The capture proved a frame had
    been damaged and made it impossible to say how.

    This changes NO transport behaviour. Nothing here parses, retries,
    salvages or times anything differently; it only describes a line the
    reader has already given up on.
    """
    line = line or ""
    length = len(line)

    report = {
        # `line` stays, under its old name, so existing readers and the
        # damaged_lines buffer keep working unchanged.
        "line": line[:400],

        "length": length,
        "prefix": line[:DAMAGE_PREFIX_CHARS],
        "suffix": line[-DAMAGE_SUFFIX_CHARS:] if length else "",
        "truncated_middle": length > (
            DAMAGE_PREFIX_CHARS + DAMAGE_SUFFIX_CHARS
        ),
        "terminated_by_newline": bool(terminated_by_newline),

        # What the line claims to be about. Recovered by string search
        # because it will not parse.
        "request_id": _field_from(line, "request_id"),
        "cmd": _field_from(line, "cmd"),
        "ok": _field_from(line, "ok"),

        # Whether the other half of the defence found anything.
        "json_start": line.find("{"),
        "salvageable": salvage_json(line) is not None,
    }

    if port_present is not None:
        # Whether the USB device was still enumerated after the damage.
        # A bridge that vanished mid-frame and a bridge that corrupted
        # one are different faults.
        report["port_present"] = bool(port_present)

    offset = getattr(parse_error, "pos", None)

    if isinstance(offset, int) and 0 <= offset <= length:
        report["parse_error_offset"] = offset
        report["parse_error"] = str(parse_error)[:200]

        start = max(0, offset - DAMAGE_WINDOW_CHARS // 2)
        report["window"] = line[start:start + DAMAGE_WINDOW_CHARS]
        report["window_offset"] = start

        # The bytes either side of the exact failure point, escaped, so
        # a non-printable or a replacement character is visible rather
        # than being swallowed by the terminal.
        report["at_fault"] = repr(line[max(0, offset - 8):offset + 8])

        remainder = offset % CP210X_PACKET_BYTES
        distance = min(remainder, CP210X_PACKET_BYTES - remainder)

        report["cp210x_packet_offset"] = remainder
        report["cp210x_packet_aligned"] = distance <= CP210X_ALIGNMENT_SLACK

    if length:
        remainder = length % CP210X_PACKET_BYTES
        report["length_packet_offset"] = remainder
        report["length_packet_aligned"] = min(
            remainder, CP210X_PACKET_BYTES - remainder
        ) <= CP210X_ALIGNMENT_SLACK

    # The replacement character is what a decode failure leaves behind,
    # so its presence separates "bytes arrived wrong" from "bytes
    # arrived fine and the structure is wrong".
    replacements = line.count("�")

    report["replacement_chars"] = replacements
    report["undecodable_bytes"] = bool(replacements)

    if counters is not None:
        report["counters"] = {
            "corrupt_frames": getattr(counters, "corrupt_frames", None),
            "salvaged_frames": getattr(counters, "salvaged_frames", None),
            "stale_frames": getattr(counters, "stale_frames", None),
            "oversized_lines": getattr(counters, "oversized_lines", None),
            "bytes_read": getattr(counters, "bytes_read", None),
        }

    return report


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
            # sys.executable rather than `py`: the launcher is Windows
            # only, and the file is in firmware/PC/, not the repository
            # root. The old hint named neither correctly, which on the
            # Linux main computer made it advice that could not be
            # followed.
            raise RuntimeError(
                "pyserial is not installed. Install it with:\n"
                "    {} -m pip install -r {}".format(
                    sys.executable,
                    Path(__file__).resolve().parent / "requirements.txt",
                )
            )

        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.verbose = verbose

        self.serial = None
        self.online = False

        # What the board says it is, filled by the first successful
        # ping. None until then - never guessed.
        self.firmware_name = None
        self.firmware_version = None
        self.device_protocol_version = None
        self.protocol_mismatch = None

        # WHY the link is closed, when it was closed by a failure rather
        # than by a normal quit. It turns "the connection is not open"
        # into "the connection was lost because the device disappeared",
        # which is the difference between a puzzled operator and one who
        # knows to check the cable.
        self.closed_reason = None

        # Answers that arrived damaged, and answers recovered from a
        # line with rubbish in front of them. Not fatal on their own -
        # both are handled - but a rising count is the difference
        # between one unlucky frame and an unhealthy link.
        self.corrupt_frames = 0
        self.salvaged_frames = 0

        # Frames with our request id and somebody else's command. See
        # _matches_request: these are a previous session's answers,
        # still in the driver buffer, and one of them being accepted
        # would hand this session the last one's measurement.
        self.stale_frames = 0

        # Lines that ran past MAX_FRAME_BYTES without a terminator and
        # were thrown away. A device that never finishes a line is a
        # different fault from one that answers slowly, and the timeout
        # alone cannot tell them apart.
        self.oversized_lines = 0

        # Bytes received from the port, for measuring what a response
        # really costs on a 115200 wire. A byte is 10 bits with 8N1
        # framing, so 86.8 us: the size of a response is the floor under
        # its latency, and the only way to tell transport cost from
        # firmware cost is to know both.
        self.bytes_read = 0
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

        # THE SAME EVENTS, DESCRIBED RATHER THAN QUOTED.
        #
        # `damaged_lines` holds text, and several readers depend on it
        # being text, so it stays exactly as it was. This holds the
        # `describe_damage` report for the same frames: length, parse
        # offset, the window around the fault, packet alignment, the
        # counters at the time, and which request was in flight.
        #
        # Kept beside rather than instead, because the useful evidence
        # is structured and the old field cannot carry it: a 400-char
        # excerpt of an intact prefix proved a frame had been damaged
        # and could not say how. Capped by the same NOISE_LIMIT.
        self.damage_reports = []

        # A RANDOM SESSION NONCE, PLUS A COUNTER.
        #
        # Request ids exist to match an answer to its question. That
        # only works if no OTHER session can produce the same id, and
        # two earlier designs could:
        #
        #   counter from 1        every session's first request was
        #                         "1", so a dead client's leftover
        #                         answer was the next client's first
        #                         result
        #   clock-seeded counter  better, and still wrong: the seed was
        #                         `time.time()*1000 % 1000000`, which
        #                         WRAPS EVERY 1000 SECONDS. Two clients
        #                         started 16 minutes apart got byte-
        #                         identical ids, which is a routine
        #                         interval for an operator restarting
        #                         the client during a run.
        #
        # A nonce removes the coincidence instead of making it rarer.
        # 24 random bits from the OS, so two sessions collide about
        # once in 16.7 million - and even then the counter and the
        # command name must line up as well.
        #
        # `os.urandom`, not `random`: `random` can be seeded, and a
        # test or a tool that seeds it globally would quietly make
        # every session's ids identical again.
        #
        # THE FORMAT IS A STRING ON PURPOSE. The firmware echoes
        # `request_id` verbatim and never parses it, so this needs no
        # firmware change and no protocol version bump.
        #
        # AND IF THE OS WILL NOT GIVE US ANY. `os.urandom` can fail:
        # on Linux it falls back to opening /dev/urandom, which needs a
        # file descriptor, so a process that has exhausted its
        # descriptors gets an OSError here - and a raw OSError out of a
        # constructor is an unhandled traceback before the operator has
        # seen a single screen.
        #
        # It is refused rather than worked around. A fixed fallback
        # nonce would make every session's ids identical again, which
        # is the precise defect the nonce was introduced to remove, and
        # it would do it silently. RuntimeError, because `main()`
        # already turns that into a diagnosed exit-2 - the same
        # treatment as a missing pyserial.
        try:
            self.session = os.urandom(3).hex()

        except (OSError, NotImplementedError) as error:
            raise RuntimeError(
                "The operating system would not supply random bytes for "
                "the session id ({}). Request ids exist to stop one "
                "session's answer being accepted by another, and a "
                "fixed fallback would defeat that silently, so the "
                "client will not start.".format(error)
            )

        self._request_id = 0

    # ------------------------------------------------------------------
    # diagnostics output
    # ------------------------------------------------------------------

    def _trace(self, marker, detail=""):
        if self.verbose:
            print("   {:<16} {}".format(marker, detail))

    def _port_present(self):
        """
        Is this device still enumerated by the OS? None if unanswerable.

        DIAGNOSTIC ONLY, and it must never influence anything. It is
        called on the damaged-frame path to separate two faults that
        look identical from inside the reader: a bridge that corrupted
        a frame, and a bridge that disappeared in the middle of one.

        Every failure mode returns None rather than raising. This runs
        while an exception is already being built, and a diagnostic that
        throws would replace a MALFORMED_RESPONSE - which names the real
        problem - with whatever went wrong while describing it.

        `is_open` is not consulted: it reports what pySerial believes,
        not what the OS still has. Enumeration is the independent fact.
        """
        if list_ports is None:                         # pragma: no cover
            return None

        try:
            return any(
                entry.device == self.port
                for entry in list_ports.comports()
            )

        # BaseException is deliberate and narrow in effect. Enumeration
        # walks the OS device tree and can fail in ways pySerial does
        # not wrap - see the raw OSError that `in_waiting` raises - and
        # the answer to "is it still there" is then honestly "unknown".
        except Exception:
            return None

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
        self.closed_reason = None       # a successful open clears it

        self._trace("PORT OPEN", "{} at {} baud, dtr={} rts={}".format(
            self.port, self.baudrate, handle.dtr, handle.rts))

        return self

    def close(self, reason=None):
        """
        Release the port. Safe to call twice, and never raises.

        `reason` is remembered so that a later command can say WHY the
        link is closed instead of only that it is. A link closed by a
        normal quit and one closed because the device vanished need
        different sentences on screen.
        """
        if self.serial is not None:
            try:
                self.serial.close()

            except Exception:
                pass

            self.serial = None
            self._trace("PORT CLOSED", self.port)

        if reason is not None:
            self.closed_reason = str(reason)

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
            raise self._closed_error("hard_reset")

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

    def _note_identity(self, pong):
        """
        Remember what the board says it is, and whether it matches.

        Read-only and never raises: identity is diagnostic information,
        and a board that answers a ping with an unexpected shape is
        still a board that answered.
        """
        pong = pong or {}

        self.firmware_name = pong.get("firmware")
        self.firmware_version = pong.get("version")
        self.device_protocol_version = pong.get("protocol_version")

        expected = EXPECTED_PROTOCOL_VERSION
        actual = self.device_protocol_version

        self.protocol_mismatch = (
            None if actual is None or actual == expected
            else {"expected": expected, "device": actual}
        )

        return self.protocol_mismatch

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
                pong = self.request("ping", timeout=min(remaining, 2.5))
                self.online = True
                self._note_identity(pong)

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

    def _closed_error(self, cmd=None):
        """
        Asking a closed link to do something is a LinkError, not a crash.

        THE DEFECT THIS EXISTS FOR, observed on the Linux bench:

            /dev/ttyUSB0 disappeared mid-request
            -> PORT_LOST, and the link correctly closed itself
            -> the operator returned to the main screen
            -> the menu loop called hardware_status()
            -> RuntimeError("Link is not open; call open() first.")
            -> traceback, application gone

        The same traceback was reachable from the sensor test, and from
        every other screen: 39 places in the PC layer catch LinkError
        and exactly one caught RuntimeError. So the fix is not to add
        `except RuntimeError` in 39 places - it is for the one serial
        owner to speak the language its callers already handle.

        A RuntimeError here was never right anyway. "The device is
        gone" is an operational condition an operator can act on, not
        a programming error.
        """
        if self.closed_reason:
            return LinkError(
                "PORT_CLOSED",
                "The connection to the science module is closed ({}). "
                "Reconnect the module before running {}.".format(
                    self.closed_reason, cmd or "hardware commands"
                ),
                data={"port": self.port, "cmd": cmd,
                      "reason": self.closed_reason},
            )

        return LinkError(
            "PORT_CLOSED",
            "The connection to the science module is not open. "
            "Reconnect the module before running {}.".format(
                cmd or "hardware commands"),
            data={"port": self.port, "cmd": cmd},
        )

    def _next_request_id(self):
        """`<session nonce>-<counter>`; unique within and across runs."""
        self._request_id += 1

        return "{}-{}".format(self.session, self._request_id)

    def _matches_request(self, frame, request_id, cmd):
        """
        Whether this frame is the answer to the request we just sent.

        THREE CONDITIONS, AND THE THIRD WAS MISSING.

        The id must match, and the frame must carry "ok" - only a
        response has one, which is what stops the REPL's echo of our
        own request from being accepted.

        The third is that the COMMAND must match, and it exists because
        request ids restart at 1 in every session while the board does
        not restart with them. A client that died with a `measure_raw`
        in flight leaves its answer in the driver buffer; the next
        client opens the port - which deliberately does not clear that
        buffer, because it may hold the traceback explaining the death -
        numbers its first request 1, and finds a frame already there
        with request_id 1 on it. Without this check the previous
        session's measurement was returned as the answer to the new
        session's first command.

        A frame with no `cmd` at all is still accepted: every response
        this firmware builds carries one, but refusing an answer for a
        field we do not strictly need would turn a compatible firmware
        into a dead link.
        """
        if frame.get("request_id") != request_id:
            return False

        if "ok" not in frame:
            return False

        answered = frame.get("cmd")

        return answered is None or answered == cmd

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
            raise self._closed_error(cmd)

        if timeout is None:
            timeout = self.timeout

        attempts = int(retries) + 1
        last_damage = None

        for attempt in range(attempts):
            request_id = self._next_request_id()

            message = {"request_id": request_id, "cmd": cmd}
            message.update(payload)
            message["timestamp"] = utc_timestamp()

            # allow_nan=False, so this owner never puts a frame on the
            # wire that is not legal JSON.
            #
            # Python writes NaN and Infinity as the bare words `NaN`
            # and `Infinity`, which no JSON specification allows and
            # which MicroPython's parser refuses. The failure that
            # produces is a parse error on the far side, reported
            # against a command that looked perfectly ordinary here -
            # so the value is refused at the point it is serialized,
            # where the offending field can still be named.
            try:
                line = json.dumps(message, allow_nan=False) + "\n"

            except ValueError as error:
                raise LinkError(
                    "INVALID_REQUEST",
                    "{} cannot be sent: {}. A payload field is not a "
                    "finite number, and JSON has no way to write "
                    "one.".format(cmd, error),
                    data={"cmd": cmd, "payload": {
                        key: repr(value) for key, value in payload.items()
                    }},
                )

            self._trace("TX JSON", line.strip())

            try:
                # NO reset_input_buffer here. Request ids are what
                # separate this answer from anything else on the wire,
                # and clearing the buffer would throw away a boot
                # traceback or a late frame that is the whole
                # explanation for the previous failure.
                self.serial.write(line.encode("utf-8"))
                self.serial.flush()

                response = self._read_response(request_id, timeout, cmd)

            # OSError AS WELL AS SerialException, and pySerial is why.
            #
            # pySerial does not wrap every OS failure. `read()` and
            # `write()` catch OSError and re-raise it as a
            # SerialException; `in_waiting` and `flush()` do not:
            #
            #     in_waiting   fcntl.ioctl(self.fd, TIOCINQ, ...)
            #     flush        termios.tcdrain(self.fd)
            #
            # Both are raw syscalls, and on the Linux main computer both
            # raise a bare OSError the moment the device node goes away -
            # EIO for a vanished tty, ENODEV for one physically removed.
            # `_read_response` asks for `in_waiting` before EVERY read, so
            # this is not an exotic path: it is the FIRST thing that fails
            # when /dev/ttyUSB0 disappears, which is exactly the event
            # H-004 was opened for.
            #
            # Catching only SerialException therefore let the one failure
            # this module exists to classify escape as an unhandled
            # traceback - the same defect as RF-002, in a different
            # exception type. SerialException is itself an OSError
            # subclass, so the pair covers the real module; the fake's
            # exception is not, which is why both are named.
            except (serial.SerialException, OSError) as error:
                self.close(reason="{} was lost during a {} command"
                                  .format(self.port, cmd))

                detail = str(error)
                errno_name = getattr(error, "errno", None)

                if errno_name is not None:
                    detail = "{} (errno {})".format(detail, errno_name)

                raise LinkError(
                    "PORT_LOST",
                    "{} disappeared while the request was in flight: "
                    "{}\nReconnect the module before continuing; every "
                    "hardware command will be refused until you "
                    "do.".format(self.port, detail),
                    data={"port": self.port, "cmd": cmd,
                          "errno": errno_name,
                          "reconnect_required": True},
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

                if len(self.damage_reports) < NOISE_LIMIT:
                    # The structured description of the same event, with
                    # the command that provoked it - which the frame
                    # itself may be too damaged to name.
                    report = dict(error.data or {})
                    report["request_command"] = cmd

                    self.damage_reports.append(report)

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

    def _read_response(self, request_id, timeout, cmd=None):
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

        # True while the tail of an over-long line is being thrown away.
        # Without it the bytes AFTER the cap would be read as the start
        # of a fresh line and reported as noise, which is a second
        # misleading diagnosis on top of the first.
        discarding = False

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

            self.bytes_read += len(chunk)

            buffer += chunk.decode("utf-8", "replace")

            # BOUND THE UNTERMINATED LINE. See MAX_FRAME_BYTES.
            #
            # The check runs on EVERY pass, not only the first. Skipping
            # it once `discarding` was set left the buffer growing
            # exactly as before between the cap and the newline that
            # ends it - and against a stream that never sends one, that
            # is every byte of it. The counter and the diagnosis are
            # what happen once; the discarding is what has to keep
            # happening.
            if len(buffer) > MAX_FRAME_BYTES:
                if not discarding:
                    self.oversized_lines += 1
                    self._trace(
                        "RX OVERSIZED",
                        "line exceeded {} bytes with no newline; "
                        "discarding until one arrives".format(
                            MAX_FRAME_BYTES),
                    )

                    if len(self.last_noise) < NOISE_LIMIT:
                        self.last_noise.append(
                            "<a line longer than {} bytes arrived with no "
                            "terminator and was discarded>".format(
                                MAX_FRAME_BYTES)
                        )

                    discarding = True

                buffer = ""

            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)

                if discarding:
                    # That newline ends the over-long line. Everything
                    # before it belonged to a line already given up on.
                    discarding = False

                    continue

                line = line.strip()

                if not line:
                    continue

                try:
                    frame = json.loads(line)

                # RecursionError as well: pathological nesting exhausts
                # the stack rather than failing to parse. See
                # salvage_json for the whole argument.
                #
                # `as parse_error` only so the failure OFFSET can be
                # reported. Nothing about the handling changed.
                except (ValueError, RecursionError) as parse_error:
                    salvaged = salvage_json(line)

                    if salvaged is not None and isinstance(salvaged, dict):
                        if salvaged.get("request_id") == request_id:
                            self.salvaged_frames += 1
                            self._trace("RX SALVAGED", line[:120])

                            return salvaged

                        continue

                    if looks_like_a_frame(line):
                        # THE SHAPE OF THE DAMAGE, NOT JUST ITS FIRST
                        # 400 CHARACTERS. See describe_damage: the old
                        # payload could prove a frame had been damaged
                        # and never say how, which is what stalled the
                        # HW-B1-009 investigation.
                        raise LinkError(
                            "MALFORMED_RESPONSE",
                            "The module answered, but the answer was "
                            "damaged in transit and could not be "
                            "parsed.",
                            data=describe_damage(
                                line,
                                parse_error=parse_error,
                                counters=self,
                                terminated_by_newline=True,
                                port_present=self._port_present(),
                            ),
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

                if self._matches_request(frame, request_id, cmd):
                    self._trace("RX JSON", line[:200])

                    return frame

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

                # Right id, right shape, wrong command: a leftover from
                # a previous session whose ids overlap ours.
                self._trace("RX STALE", line[:120])
                self.stale_frames += 1

                if len(self.last_noise) < NOISE_LIMIT:
                    self.last_noise.append(line)

                continue

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

    def delete_saved_sample(self, sample_id):
        """
        Delete ONE acquisition from the device's buffer.

        No retries. A delete is not a pure read: a re-send whose first
        attempt actually landed comes back SAMPLE_NOT_FOUND, which
        reads as a failure over a delete that succeeded. The PC
        verifies by listing the device afterwards instead.
        """
        return self.request("delete_saved_sample",
                            sample_id=str(sample_id))

    def delete_saved_samples(self):
        return self.request("delete_saved_samples")
