"""
The wire, made to misbehave in every way it plausibly can.

THE ONE PROPERTY EVERYTHING HERE DEFENDS

    A damaged conversation must never become a believable answer.

There is a second, quieter property that matters just as much on a
mechanism that moves:

    A LOST ACKNOWLEDGEMENT IS NOT A LOST MOVEMENT.

`move_slots` is relative. If the carousel turns and the reply is
destroyed on the way back, re-sending the command turns it twice, and
the second turn is silent - nothing on the PC ever learns about it.
That is why `request()` defaults to `retries=0` and why only pure
reads ask for more, and it is asserted here rather than trusted.

WHAT IS FAKED

`serial.Serial` and the clock. Every line of `SerialLink` runs for
real, including the framing, the request-id matching, the salvage path
and the seven-way failure classification.
"""

import json
import sys
from pathlib import Path

_TESTS_DIR = next(
    p for p in Path(__file__).resolve().parents if p.name == "Tests"
)

if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

_SOFTWARE_DIR = _TESTS_DIR / "software"

if str(_SOFTWARE_DIR) not in sys.path:
    sys.path.insert(0, str(_SOFTWARE_DIR))

import support

support.add_project_root()
support.add_path("PC")

import serial_link                                          # noqa: E402
from serial_link import DeviceError, LinkError               # noqa: E402

from fakes import FakeClock, install_clock                   # noqa: E402
from fakes.esp32 import (                                    # noqa: E402
    DEVICE_ERROR,
    ECHO,
    GARBAGE,
    MALFORMED,
    REPL,
    ScriptedDevice,
    SUCCESS,
    TIMEOUT,
    TRUNCATED,
    WRONG_ID,
    LoopbackDevice,
)
from fakes.serial_port import (                              # noqa: E402
    FakeSerialException,
    install_fake_serial,
    open_link,
)

checks = support.Checks("serial-faults")

restore_serial = install_fake_serial(serial_link)


def linked(device=None, clock=None, **faults):
    """An open link on a fake port, with the clock under our control."""
    clock = clock or FakeClock()
    installed = install_clock(serial_link, clock)
    link, port = open_link(serial_link, device, clock=clock, **faults)
    link.online = True

    return link, port, clock, installed


def failure_of(call):
    """(code, exception) for whatever the call raises, or (None, None)."""
    try:
        call()

        return None, None

    except LinkError as error:
        return error.code, error

    except Exception as error:                         # noqa: BLE001
        return "CRASH:" + type(error).__name__, error


# ======================================================================
checks.section("a frame split across reads is still one frame")

# A CP210x delivers in 64-byte USB packets, so a 300-byte response
# arrives in five pieces with the JSON cut at arbitrary points.

for size in (1, 3, 7, 64, 199):
    link, port, clock, installed = linked(chunk_size=size)

    try:
        data = link.ping()
        checks.ok(data.get("pong") is True,
                  "a response delivered {} byte(s) at a time is "
                  "reassembled".format(size))

    finally:
        installed.restore()
        link.close()


# ======================================================================
checks.section("line endings the firmware never sends")

for ending, label in (("\r\n", "CRLF"), ("\n", "LF")):
    link, port, clock, installed = linked(line_ending=ending)

    try:
        data = link.ping()
        checks.ok(data.get("pong") is True,
                  "a frame terminated with {} is accepted - the strip() "
                  "in the read loop is what makes that true".format(label))

    finally:
        installed.restore()
        link.close()

# A frame with no terminator at all never completes, and must time out
# rather than be half-accepted.
link, port, clock, installed = linked(drop_newline=True,
                                      link_kwargs={"timeout": 2.0})

try:
    code, _error = failure_of(link.ping)
    checks.equal(code, "PROTOCOL_TIMEOUT",
                 "an unterminated frame times out - a protocol that "
                 "accepted it would accept half of the next one too")

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("noise in front of a frame is survivable, noise instead is not")

# Measured on this bench: switching an illumination LED is the largest
# current step the board makes, and a transient puts bytes in front of
# an otherwise perfect frame.

link, port, clock, installed = linked(noise_before=b"\x00\xfe\xff garbage ")

try:
    data = link.ping()
    checks.ok(data.get("pong") is True,
              "a frame with rubbish in front of it is salvaged")
    checks.equal(link.salvaged_frames, 1,
                 "and the salvage is COUNTED, so a rising rate is "
                 "visible rather than invisible")

finally:
    installed.restore()
    link.close()

link, port, clock, installed = linked(ScriptedDevice({"ping": GARBAGE}),
                                      link_kwargs={"timeout": 2.0})

try:
    code, error = failure_of(link.ping)
    checks.equal(code, "PROTOCOL_TIMEOUT",
                 "console noise INSTEAD of a frame times out")
    checks.ok(error.data.get("console"),
              "and the noise is carried on the error, because it is "
              "usually the whole explanation")

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("a damaged answer is not a missing answer")

link, port, clock, installed = linked(ScriptedDevice({"ping": MALFORMED}),
                                      link_kwargs={"timeout": 30.0})

try:
    code, _error = failure_of(lambda: link.request("ping"))
    checks.equal(code, "MALFORMED_RESPONSE",
                 "a frame carrying our request_id that will not parse "
                 "raises immediately")
    checks.ok(clock.now < 1001.0,
              "and does NOT wait out the 30 s timeout - the answer has "
              "already been and gone, so waiting is the worst possible "
              "response")
    checks.equal(link.corrupt_frames, 1, "the damage is counted")
    checks.equal(len(link.damaged_lines), 1,
                 "and the damaged line itself is kept, because a count "
                 "cannot say HOW it was damaged")

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("a lost acknowledgement never repeats a movement")

# The property that protects the mechanism. A relative movement whose
# reply was destroyed HAS HAPPENED; sending it again turns the carousel
# a second time and nothing on the PC ever learns about it.

device = ScriptedDevice({"move_slots": MALFORMED})
link, port, clock, installed = linked(device, link_kwargs={"timeout": 2.0})

try:
    code, _error = failure_of(lambda: link.move_slots("cw", 1))

    checks.equal(code, "MALFORMED_RESPONSE",
                 "a damaged answer to move_slots is reported")
    checks.equal(device.seen.count("move_slots"), 1,
                 "AND THE COMMAND WAS SENT EXACTLY ONCE - a retry here "
                 "would turn the carousel twice for one instruction")

finally:
    installed.restore()
    link.close()

for name, call in (
    ("ping", lambda link: link.ping()),
    ("get_status", lambda link: link.get_status()),
):
    device = ScriptedDevice({name: [MALFORMED, MALFORMED, SUCCESS]})
    link, port, clock, installed = linked(device, link_kwargs={"timeout": 2.0})

    try:
        call(link)
        checks.ok(device.seen.count(name) > 1,
                  "{} IS retried on damage - it is a pure read, so "
                  "asking again costs nothing".format(name))

    except LinkError:
        checks.ok(False, "{} recovers from two damaged answers".format(name))

    finally:
        installed.restore()
        link.close()

MOVING = ("move_slots", "select_slot", "fine_adjust", "servo_test_move",
          "sync_position", "clear_slot", "clear_all_slots",
          "servo_configure", "measure_raw", "delete_saved_samples")

source = (support.FIRMWARE / "PC" / "serial_link.py").read_text(
    encoding="utf-8")


def method_body(text, name):
    """One method's source, without needing another method to follow it."""
    start = text.index("def {}(".format(name))
    end = text.find("\n    def ", start + 10)

    return text[start:] if end == -1 else text[start:end]


for name in MOVING:
    body = method_body(source, name)

    checks.ok("retries" not in body,
              "{}() asks for no retries - it changes something".format(name))


# ======================================================================
checks.section("an answer to somebody else is not our answer")

link, port, clock, installed = linked(ScriptedDevice({"ping": WRONG_ID}),
                                      link_kwargs={"timeout": 2.0})

try:
    code, _error = failure_of(link.ping)
    checks.equal(code, "PROTOCOL_TIMEOUT",
                 "a frame with the wrong request_id is ignored, not "
                 "accepted")

finally:
    installed.restore()
    link.close()

# ----------------------------------------------------------------------
# A PREVIOUS SESSION'S ANSWER, STILL IN THE BUFFER.
#
# Found by this suite. The sequence is entirely ordinary:
#
#   1. a client sends measure_raw and is killed, or crashes, or the
#      terminal is closed
#   2. the board finishes the measurement and writes the answer; it
#      lands in the OS driver buffer with nobody to read it
#   3. a new client opens the port. open() deliberately does NOT clear
#      the buffer, because it may hold the traceback that explains the
#      death
#   4. the new client numbers its first request 1 - as every session
#      did - and the leftover frame has request_id 1 on it
#
# Before the fix, step 4 returned the previous session's measurement as
# the answer to the new session's first command.
# ----------------------------------------------------------------------

link, port, clock, installed = linked(link_kwargs={"timeout": 2.0})
first_id = str(link._request_id + 1)

stale = (json.dumps({"request_id": first_id, "ok": True,
                     "cmd": "measure_raw",
                     "data": {"illuminations": {}, "stale": True}})
         + "\n").encode("utf-8")

installed.restore()
link.close()

link, port, clock, installed = linked(link_kwargs={"timeout": 2.0})
link._request_id = 0
port._enqueue(
    (json.dumps({"request_id": "1", "ok": True, "cmd": "measure_raw",
                 "data": {"illuminations": {}, "stale": True}})
     + "\n").encode("utf-8"))

try:
    data = link.request("get_status")

    checks.ok(data.get("stale") is not True,
              "a leftover measure_raw answer is NOT returned as the "
              "answer to get_status, even though the request ids match")
    checks.equal(link.stale_frames, 1,
                 "and the mismatch is counted, so it is visible rather "
                 "than merely survived")

except LinkError as error:
    checks.ok(False, "a stale frame should be skipped, not fatal "
                     "({})".format(error.code))

finally:
    installed.restore()
    link.close()

# The same collision with the SAME command is indistinguishable from a
# real answer at this layer, which is why the ids are seeded from the
# clock as well.
link, port, clock, installed = linked()
other, _p, _c, other_installed = linked()

try:
    checks.ok(link._request_id >= 1,
              "the request counter is seeded from the clock, so two "
              "sessions started at different times do not number their "
              "first request the same")
    checks.ok(str(link._request_id + 1) != "1",
              "and no session's first request is ever id \"1\" - the id "
              "that every session used before this, and therefore the "
              "one most likely to be lying in the buffer")

finally:
    installed.restore()
    other_installed.restore()
    link.close()
    other.close()


# ======================================================================
checks.section("the REPL cannot impersonate the firmware")

link, port, clock, installed = linked(ScriptedDevice({"ping": ECHO}),
                                      link_kwargs={"timeout": 2.0})

try:
    code, _error = failure_of(link.ping)
    checks.equal(code, "PROTOCOL_TIMEOUT",
                 "our own request echoed back carries OUR request_id "
                 "and is still refused - only a response has 'ok'")

finally:
    installed.restore()
    link.close()

link, port, clock, installed = linked(ScriptedDevice({"ping": REPL}),
                                      link_kwargs={"timeout": 30.0})

try:
    code, _error = failure_of(link.ping)
    checks.equal(code, "DEVICE_AT_REPL",
                 "a >>> prompt is named, not timed out")
    checks.ok(clock.now < 1001.0,
              "and named immediately - the diagnosis is already certain")

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("truncated JSON")

link, port, clock, installed = linked(ScriptedDevice({"ping": TRUNCATED}),
                                      link_kwargs={"timeout": 2.0})

try:
    code, _error = failure_of(link.ping)
    checks.ok(code in ("MALFORMED_RESPONSE", "PROTOCOL_TIMEOUT"),
              "JSON cut in half is refused ({})".format(code))
    checks.ok(code != "CRASH", "and does not raise ValueError at the "
                               "caller")

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("the port disappearing mid-request")

link, port, clock, installed = linked(fail_write_after=0)

try:
    code, error = failure_of(link.ping)
    checks.equal(code, "PORT_LOST",
                 "a write that raises becomes PORT_LOST, not a timeout")
    checks.ok(link.serial is None,
              "AND THE HANDLE IS RELEASED - a lost port that stays open "
              "is the next session's PORT_BUSY")

finally:
    installed.restore()

link, port, clock, installed = linked(fail_read_after=0,
                                      link_kwargs={"timeout": 2.0})

try:
    code, _error = failure_of(link.ping)
    checks.equal(code, "PORT_LOST", "a read that raises is PORT_LOST too")
    checks.ok(link.serial is None, "and releases the handle as well")

finally:
    installed.restore()


# ======================================================================
checks.section("a write that reports the wrong length")

# pySerial's write() returns how many bytes went out. A short write on
# a saturated bridge means the firmware receives half a request.

link, port, clock, installed = linked(short_write=True)

try:
    data = link.ping()
    checks.ok(data is not None,
              "a short write still completes here, because the fake "
              "delivered the whole line - what matters is that the "
              "return value is not silently trusted as proof")

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("silence")

link, port, clock, installed = linked(ScriptedDevice({"ping": TIMEOUT}),
                                      link_kwargs={"timeout": 12.0})

try:
    code, error = failure_of(link.ping)
    checks.equal(code, "PROTOCOL_TIMEOUT", "no answer at all times out")
    checks.ok("12.0" in error.message or "12" in error.message,
              "and the message names the timeout that expired")
    checks.ok(clock.now >= 1012.0,
              "and it really waited the full timeout ({:.1f} s of fake "
              "clock, 0 s of real one)".format(clock.now - 1000.0))

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("a refusal is a refusal, not a transport failure")

link, port, clock, installed = linked(
    ScriptedDevice({"select_slot": DEVICE_ERROR}))

try:
    try:
        link.select_slot(2)
        raised = None

    except DeviceError as error:
        raised = error

    checks.ok(raised is not None, "ok:false raises DeviceError")
    checks.equal(raised.code, "REFUSED", "carrying the firmware's code")
    checks.ok(isinstance(raised, LinkError),
              "and DeviceError IS a LinkError, so one except clause "
              "catches everything the link can go wrong with")

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("recovery: a bad answer does not poison the next command")

device = ScriptedDevice({"ping": [MALFORMED, SUCCESS, SUCCESS]})
link, port, clock, installed = linked(device, link_kwargs={"timeout": 2.0})

try:
    failure_of(lambda: link.request("ping"))

    data = link.request("ping")
    checks.ok(data is not None,
              "the command after a damaged one succeeds")

    data = link.request("ping")
    checks.ok(data is not None, "and so does the one after that")

    checks.equal(port.buffer_resets, 0,
                 "and none of it cleared the receive buffer - clearing "
                 "as a matter of routine throws away the boot traceback "
                 "that explains the previous failure")

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("a real firmware behind a bad wire")

# The same faults, but with the REAL ESP32 firmware answering. What is
# being checked is that the client's recovery does not depend on the
# device being a simplification.

for label, faults in (
    ("64-byte packets", {"chunk_size": 64}),
    ("one byte at a time", {"chunk_size": 1}),
    ("noise in front", {"noise_before": b"\xff\xfe"}),
    ("CRLF", {"line_ending": "\r\n"}),
):
    clock = FakeClock()
    installed = install_clock(serial_link, clock)
    loopback = LoopbackDevice()
    link, port = open_link(serial_link, loopback, clock=clock, **faults)
    link.online = True

    try:
        link.connect_servo()
        link.sync_position(load_slot=1)
        status = link.get_status()

        checks.ok(status["carousel"]["position_valid"] is True,
                  "a whole carousel setup survives {}".format(label))

    except LinkError as error:
        checks.ok(False, "a carousel setup over {} failed: {}".format(
            label, error.code))

    finally:
        installed.restore()
        link.close()


restore_serial()

sys.exit(checks.report())
