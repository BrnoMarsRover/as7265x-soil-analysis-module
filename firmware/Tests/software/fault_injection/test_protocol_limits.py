"""
The wire, at its limits and beyond them.

WHAT test_serial_faults.py ALREADY DOES, AND WHAT THIS ADDS

That file asks whether a DAMAGED conversation can become a believable
answer. This one asks a different question: whether an ABUSIVE one can
exhaust the reader, confuse the framer, or slip a frame past the
identity checks by arriving at the right moment rather than with the
right contents.

    22   frame sizes from nothing to a hundred times too large
    23   a stream that never sends a newline at all
    24   a valid frame far larger than the protocol should ever carry
    25   nesting deep enough to exhaust the parser's stack
    26   the same key twice in one object
    27   binary, invalid UTF-8, NULs and control characters
    28   the echo storm this bench has actually produced
    29   valid frames nobody asked for

THE ONE PROPERTY EVERY CASE SHARES

Whatever arrives, two things must remain true: the reader stays
bounded, and nothing is accepted as an answer unless it is the answer
to the question that was asked. Failing is allowed. Hanging, growing
without limit, and believing the wrong frame are not.

WHAT IS FAKED

`serial.Serial` and the clock. Every line of `SerialLink` runs for
real - the framing, the salvage path, the request-id matching and the
whole failure classification.
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
from serial_link import (                                   # noqa: E402
    DeviceError,
    LinkError,
    MAX_FRAME_BYTES,
    SerialLink,
    salvage_json,
)

from fakes import FakeSerialPort, install_clock, open_link   # noqa: E402

checks = support.Checks("protocol-limits")


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def link_with(payloads, timeout=1.0, clock=None):
    """
    An open link whose port will deliver exactly `payloads`, in order,
    in answer to whatever is asked.

    The device answers nothing of its own, so the ONLY bytes the reader
    sees are the ones the case supplies. That is what makes each result
    attributable to one input.
    """
    supply = list(payloads)

    def device(request):
        return supply.pop(0) if supply else None

    link, port = open_link(
        serial_link, device, clock=clock,
        link_kwargs={"timeout": timeout},
    )

    return link, port


def outcome(call):
    try:
        return ("ok", call())

    except DeviceError as error:
        return ("device", error.code)

    except LinkError as error:
        return ("link", error.code)

    except BaseException as error:                         # noqa: BLE001
        return ("raw", type(error).__name__)


def answer_for(request, **extra):
    """A perfectly valid response to a request, as the firmware builds it."""
    frame = {
        "request_id": request.get("request_id"),
        "ok": True,
        "cmd": request.get("cmd"),
        "data": {"pong": True},
    }
    frame.update(extra)

    return frame


# ======================================================================
checks.section("22. frame sizes, from nothing to a hundred times too large")

# The largest legitimate frame this firmware can build was measured at
# 16,454 bytes - sensor_test_raw at MAX_REPEATS. Everything here is
# expressed against that so the numbers mean something.
LEGITIMATE_MAX = 16454

# The genuinely SMALL cases first, and as raw bytes rather than padded
# frames: "zero bytes" has to mean zero bytes on the wire, not a
# minimal frame described as one. Padding these up to a valid frame
# would have made three cases that all silently tested the same thing.
TINY = (
    (b"", "nothing at all"),
    (b"\n", "a bare newline"),
    (b"x", "one byte, no terminator"),
    (b"{}\n", "an empty JSON object"),
    (b"[]\n", "a JSON array where an object belongs"),
    (b"null\n", "a JSON null"),
    (b"0\n", "a bare JSON number"),
)

for payload, label in TINY:
    link, port = open_link(serial_link, lambda request, p=payload: p,
                           link_kwargs={"timeout": 0.3})

    kind, detail = outcome(lambda: link.request("ping"))

    checks.equal(kind, "link",
                 "{} is not an answer ({})".format(label, detail))

    checks.ok(kind != "raw",
              "  and does not escape as an unhandled exception")

    link.close()

SIZES = (
    (200, "an ordinary frame"),
    (LEGITIMATE_MAX, "the largest legitimate frame"),
    (LEGITIMATE_MAX * 2, "twice that"),
    (LEGITIMATE_MAX * 10, "ten times that"),
    (LEGITIMATE_MAX * 100, "a hundred times that"),
)

for size, label in SIZES:
    # A syntactically valid frame padded to the requested size, so the
    # only variable is length.
    def device(request, size=size):
        frame = {
            "request_id": request.get("request_id"),
            "ok": True,
            "cmd": request.get("cmd"),
            "data": {"pad": ""},
        }
        base = len(json.dumps(frame))
        frame["data"]["pad"] = "p" * max(0, size - base)

        return frame

    link, port = open_link(serial_link, device,
                           link_kwargs={"timeout": 1.0})

    kind, detail = outcome(lambda: link.request("ping"))

    if size <= MAX_FRAME_BYTES:
        checks.equal(kind, "ok",
                     "an ordinary read: {} ({:,} bytes)".format(label, size))

    else:
        checks.equal(kind, "link",
                     "a frame of {} ({:,} bytes) is refused rather than "
                     "accumulated - {}".format(label, size, detail))

        checks.ok(link.oversized_lines >= 1,
                  "  and is counted as an oversized line")

    link.close()

# The boundary itself, from both sides. A cap that rejects a legal
# frame is worse than no cap at all.
for size, expected, label in (
    (MAX_FRAME_BYTES - 200, "ok", "just under the cap"),
    (MAX_FRAME_BYTES + 4096, "link", "just over the cap"),
):
    def device(request, size=size):
        frame = {"request_id": request.get("request_id"), "ok": True,
                 "cmd": request.get("cmd"), "data": {"pad": ""}}
        frame["data"]["pad"] = "p" * max(
            0, size - len(json.dumps(frame)))

        return frame

    link, port = open_link(serial_link, device,
                           link_kwargs={"timeout": 1.0})
    kind, _detail = outcome(lambda: link.request("ping"))

    checks.equal(kind, expected,
                 "a frame {} ({:,} bytes) is {}".format(
                     label, size,
                     "accepted" if expected == "ok" else "refused"))

    link.close()


# ======================================================================
checks.section("23. a stream that never sends a newline")

# The framing is newline-delimited, so a device that never sends one is
# a device whose line never ends. Before MAX_FRAME_BYTES this
# accumulated 2.08 MB inside a single MEASURE_TIMEOUT, in one string
# that was copied on every append.


class EndlessPort(FakeSerialPort):
    """A port that talks forever and never terminates a line."""

    def __init__(self, *args, clock=None, **kwargs):
        super().__init__(*args, clock=clock, **kwargs)
        self.delivered = 0

    @property
    def in_waiting(self):
        return 4096

    def read(self, count=1):
        wanted = count if count and count > 0 else 1

        # 115200 8N1 is 11,520 bytes a second. Spending the clock at
        # that rate is what makes the deadline arrive after the same
        # number of bytes a real port would have delivered.
        self._spend(wanted / 11520.0)
        self.delivered += wanted

        return b"x" * wanted


clock_handle = install_clock(serial_link)
clock = clock_handle.clock

port = EndlessPort(device=None, clock=clock)
module = serial_link.serial
serial_link.serial = type("serial", (), {
    "EIGHTBITS": 8, "PARITY_NONE": "N", "STOPBITS_ONE": 1,
    "SerialException": type("SerialException", (Exception,), {}),
    "Serial": staticmethod(lambda: port),
})

link = SerialLink("PORT_TEST")
link.open()

import tracemalloc                                          # noqa: E402

tracemalloc.start()
kind, detail = outcome(
    lambda: link.request("measure_raw", timeout=serial_link.MEASURE_TIMEOUT)
)
_current, peak = tracemalloc.get_traced_memory()
tracemalloc.stop()

serial_link.serial = module
clock_handle.restore()

checks.equal(kind, "link",
             "an endless unterminated stream ends in a controlled "
             "failure ({})".format(detail))

checks.equal(detail, "PROTOCOL_TIMEOUT",
             "and the failure is the honest one - nothing answered in "
             "time, because nothing ever completed a line")

checks.ok(port.delivered > 2_000_000,
          "the device really did deliver {:,} bytes".format(port.delivered))

# The claim that matters: the reader did not keep them.
checks.ok(peak < MAX_FRAME_BYTES * 4,
          "and the reader's peak allocation stayed at {:,} bytes - "
          "bounded by MAX_FRAME_BYTES, not by how long the device "
          "talked".format(peak))

checks.equal(link.oversized_lines, 1,
             "counted once, not once per chunk - the diagnosis is a "
             "fact about the line, not about the traffic")

link.close()


# ======================================================================
checks.section("24. an oversized but perfectly valid frame")

# Not damaged, not malformed: a real JSON object far larger than this
# protocol should ever carry. The distinction matters because a reader
# that only rejects INVALID input would accept this one.

for multiplier in (2, 10):
    size = MAX_FRAME_BYTES * multiplier

    def device(request, size=size):
        return {
            "request_id": request.get("request_id"),
            "ok": True,
            "cmd": request.get("cmd"),
            "data": {"spectrum": list(range(size // 8))},
        }

    link, port = open_link(serial_link, device,
                           link_kwargs={"timeout": 1.0})

    kind, detail = outcome(lambda: link.request("ping"))

    checks.equal(kind, "link",
                 "a VALID frame {}x the cap is still refused ({}) - "
                 "well-formed is not the same as reasonable".format(
                     multiplier, detail))

    link.close()

# And the other direction: an oversized frame must not poison the link
# for the frame that follows it.
sequence = [
    b'{"request_id": "flood", "ok": true, "data": {"pad": "'
    + b"p" * (MAX_FRAME_BYTES * 2) + b'"}}',
    None,
]


def recovering(request):
    if sequence:
        first = sequence.pop(0)

        if first is not None:
            return first

    return answer_for(request)


link, port = open_link(serial_link, recovering,
                       link_kwargs={"timeout": 1.0})

kind, _detail = outcome(lambda: link.request("ping"))
checks.equal(kind, "link", "the oversized frame fails its own request")

kind, data = outcome(lambda: link.request("ping"))
checks.equal(kind, "ok",
             "and THE NEXT REQUEST SUCCEEDS - the reader resynchronized "
             "at the newline instead of staying confused")

link.close()


# ======================================================================
checks.section("25. nesting deep enough to exhaust the parser")

# CPython's JSON scanner recurses once per level. Around 17,000 opening
# brackets it raises RecursionError, which is NOT a ValueError and
# therefore escaped every `except ValueError` in the module.

for depth, label in ((5_000, "5,000 deep"), (50_000, "50,000 deep")):
    line = ('{"request_id": "x", "ok": true, "deep": '
            + "[" * depth + "]" * depth + "}")

    kind, detail = outcome(lambda line=line: salvage_json(line))

    # The RESULT is not printed. Printing it once meant a 5,000-deep
    # nested list arriving in the acceptance log as a single line
    # thousands of characters wide, which is unreadable and, in a suite
    # whose output is compared between runs, actively unhelpful.
    checks.equal(kind, "ok",
                 "salvage_json survives a value {} - it either parses "
                 "or returns None, and never exhausts the stack".format(
                     label))

# Through the reader, as bytes on the wire.
for depth, label in ((5_000, "5,000"), (50_000, "50,000")):
    payload = ('{"request_id": "nested", "ok": true, "cmd": "ping", '
               '"data": ' + "[" * depth + "]" * depth + "}")

    link, port = open_link(
        serial_link, lambda request, p=payload: p.encode("utf-8"),
        link_kwargs={"timeout": 1.0},
    )

    kind, detail = outcome(lambda: link.request("ping"))

    checks.ok(kind in ("link", "ok"),
              "a frame nested {} levels deep produces a controlled "
              "outcome ({} {}), never a RecursionError out of the "
              "reader".format(label, kind, detail))

    checks.ok(kind != "raw",
              "  and specifically not an unhandled exception")

    link.close()


# ======================================================================
checks.section("26. the same key twice in one object")

# Python's json keeps the LAST value for a duplicated key, silently.
# The question is whether that silence can be used to turn one frame
# into another at a boundary that matters.

duplicated = json.loads(
    '{"request_id": "aaa-1", "ok": true, "cmd": "ping", "cmd": "measure_raw"}'
)

checks.equal(duplicated["cmd"], "measure_raw",
             "a duplicated key resolves to the LAST value - documented "
             "here because it is a silent rule, not an error")

# The defence is that the command must match the command that was SENT.
# A frame claiming to be two commands is at most one of them, and it is
# not the one we asked for unless the last value happens to match.
matcher = SerialLink.__dict__["_matches_request"]

checks.ok(not matcher(None, duplicated, "aaa-1", "ping"),
          "so a frame whose cmd is duplicated to something else is NOT "
          "accepted as the answer to ping")

checks.ok(matcher(None, duplicated, "aaa-1", "measure_raw"),
          "and is accepted only for the command it actually resolves to "
          "- there is no state in which it satisfies both")

# The dangerous version: `ok` twice, trying to turn a refusal into a
# success. Same rule - the last one wins - so the test is that this is
# understood rather than accidental.
flipped = json.loads('{"request_id": "b-1", "ok": false, "ok": true}')

checks.equal(flipped["ok"], True,
             "an `ok` duplicated false-then-true resolves to true")

link, port = open_link(
    serial_link,
    lambda request: (
        '{"request_id": "' + request["request_id"] + '", "ok": true, '
        '"ok": false, "cmd": "' + request["cmd"] + '"}'
    ).encode("utf-8"),
    link_kwargs={"timeout": 1.0},
)

kind, detail = outcome(lambda: link.request("ping"))

checks.equal(kind, "device",
             "and end to end, a frame that says ok twice is judged on "
             "the LAST one - true-then-false is a device error ({}), "
             "not a success".format(detail))

link.close()

# THE REASON THIS IS DOCUMENTED RATHER THAN REJECTED. The firmware
# builds its responses from dicts, and a dict cannot hold one key
# twice, so no duplicate can originate from the board. A duplicate on
# this wire is corruption or another program, and both are already
# handled by the id and command checks above.
frame_source = (support.REPO / "ESP32" / "protocol.py").read_text(
    encoding="utf-8")

checks.ok("json.dumps" in frame_source or "send_json" in frame_source,
          "the firmware serializes responses from dicts, so it cannot "
          "produce a duplicate key at all")


# ======================================================================
checks.section("27. binary, invalid UTF-8, NULs and control characters")

HOSTILE = (
    (b"\x00" * 64, "NUL bytes"),
    (b"\xff\xfe\xfd\xfc" * 32, "invalid UTF-8"),
    (bytes(range(32)) * 8, "every C0 control character"),
    (b"\x1b[2J\x1b[H", "an ANSI screen-clear escape"),
    (b"\x07" * 200, "two hundred bells"),
    (b'{"request_id": "x", "ok": tr\x00ue}', "a NUL inside a frame"),
    (b"\xed\xa0\x80", "a lone UTF-16 surrogate"),
)

for payload, label in HOSTILE:
    # The hostile bytes arrive first; a perfectly good answer follows.
    state = {"sent": False}

    def device(request, payload=payload, state=state):
        if not state["sent"]:
            state["sent"] = True

            return payload

        return answer_for(request)

    link, port = open_link(serial_link, device,
                           link_kwargs={"timeout": 1.0})

    kind, detail = outcome(lambda: link.request("ping", retries=1))

    checks.ok(kind != "raw",
              "{} produces a controlled outcome ({} {})".format(
                  label, kind, detail))

    link.close()

# And the recovery property: rubbish followed by a real frame, in the
# same read, must still find the real frame.
def noisy(request):
    return (b"\xff\xfe\x00garbage\n"
            + json.dumps(answer_for(request)).encode("utf-8"))


link, port = open_link(serial_link, noisy, link_kwargs={"timeout": 1.0})
kind, data = outcome(lambda: link.request("ping"))

checks.equal(kind, "ok",
             "and a real frame behind a line of binary rubbish is still "
             "read - the rubbish is noise, not a wall")

link.close()


# ======================================================================
checks.section("28. echo storms")

# This bench has produced echo-like UART behaviour before, and it is
# recorded in memory as a way a REPL can fake a valid response. The
# storm below is the worst version: our own request echoed back several
# times, then boot text, then the real answer.


def echo_storm(request):
    line = json.dumps(request).encode("utf-8")

    return (
        line + b"\n"
        + line + b"\n"
        + line + b"\n"
        + b"rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\n"
        + b"MicroPython v1.24.1 on 2024-11-29; ESP32 module with ESP32\n"
        + json.dumps(answer_for(request)).encode("utf-8")
    )


link, port = open_link(serial_link, echo_storm,
                       link_kwargs={"timeout": 2.0})

kind, data = outcome(lambda: link.request("ping"))

checks.equal(kind, "ok",
             "the real answer is found behind three echoes and a boot "
             "banner")

checks.equal(data, {"pong": True},
             "and it is the REAL answer, not an echo dressed as one")

# The echoes carry our own request_id. The only thing separating them
# from an answer is that a request has no `ok`.
echoes = [
    line for line in link.last_noise
    if '"cmd"' in line and '"ok"' not in line
]

checks.ok(len(echoes) >= 1,
          "the echoes were seen and set aside ({} kept for "
          "diagnosis)".format(len(echoes)))

link.close()

# The storm with NO real answer behind it must not become a success.
link, port = open_link(
    serial_link,
    lambda request: (json.dumps(request).encode("utf-8") + b"\n") * 8,
    link_kwargs={"timeout": 0.3},
)

kind, detail = outcome(lambda: link.request("ping"))

checks.equal(kind, "link",
             "and an echo storm with no answer behind it fails ({})"
             .format(detail))

checks.ok(detail in ("PROTOCOL_TIMEOUT", "DEVICE_AT_REPL"),
          "with a code that names the real condition, not a fabricated "
          "success")

link.close()


# ======================================================================
checks.section("29. valid frames nobody asked for")

# Every one of these is a well-formed response. None of them answers
# the question being asked, and the ways they differ are exactly the
# three conditions _matches_request tests.

UNSOLICITED = (
    ("a different request id",
     lambda request: {"request_id": "someone-else-1", "ok": True,
                      "cmd": request["cmd"], "data": {"stolen": True}}),

    ("our id, a different command",
     lambda request: {"request_id": request["request_id"], "ok": True,
                      "cmd": "measure_raw",
                      "data": {"illuminations": {"white": [1] * 18}}}),

    ("our id and command, but no ok - our own request echoed",
     lambda request: {"request_id": request["request_id"],
                      "cmd": request["cmd"], "timestamp": "now"}),

    ("a previous session's measurement, id and all",
     lambda request: {"request_id": request["request_id"], "ok": True,
                      "cmd": "measure_raw",
                      "data": {"illuminations": {"white": [999] * 18}}}),

    ("an unsolicited status report",
     lambda request: {"request_id": "status-broadcast", "ok": True,
                      "cmd": "get_status",
                      "data": {"carousel": {"position_valid": True}}}),
)

for label, build in UNSOLICITED:
    link, port = open_link(serial_link, build,
                           link_kwargs={"timeout": 0.3})

    kind, detail = outcome(lambda: link.request("ping"))

    checks.equal(kind, "link",
                 "{} is not accepted as the answer to ping ({})".format(
                     label, detail))

    link.close()

# Now the same frames arriving BEFORE the real answer. The real one
# must still be the one returned.
for label, build in UNSOLICITED:
    def device(request, build=build):
        return (json.dumps(build(request)).encode("utf-8") + b"\n"
                + json.dumps(answer_for(request)).encode("utf-8"))

    link, port = open_link(serial_link, device,
                           link_kwargs={"timeout": 1.0})

    kind, data = outcome(lambda: link.request("ping"))

    checks.equal(kind, "ok",
                 "with a real answer behind it, {} does not displace "
                 "it".format(label))

    checks.equal(data, {"pong": True},
                 "  and the data returned is the real answer's")

    link.close()

# The stale-frame counter is what makes this visible rather than merely
# correct: a link quietly discarding somebody else's answers is a link
# worth looking at.
link, port = open_link(
    serial_link,
    lambda request: {"request_id": request["request_id"], "ok": True,
                     "cmd": "measure_raw", "data": {}},
    link_kwargs={"timeout": 0.2},
)

outcome(lambda: link.request("ping"))

checks.ok(link.stale_frames >= 1,
          "a frame with our id and the wrong command is counted as "
          "stale ({} seen)".format(link.stale_frames))

link.close()


sys.exit(checks.report())
