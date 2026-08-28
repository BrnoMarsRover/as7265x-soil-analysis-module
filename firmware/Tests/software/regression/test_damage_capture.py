"""
A damaged frame must say HOW it was damaged, within a fixed size.

WHAT WENT WRONG

`MALFORMED_RESPONSE` carried `line[:400]` and nothing else. On the bench,
2026-08-27, HW-B1-009 caught one damaged frame in 200 requests and the
preserved evidence was the first 400 characters of it - every one of them
intact, because the damage was further in. The capture could prove a
frame had been damaged and could not say how, which is the one question
worth asking.

Worse, it read as a measurement: "length 400" looked like a frame
truncated at 400 bytes, when 400 was the diagnostic's own cap.

WHAT IS CHECKED HERE

That the four shapes a damaged frame can take are distinguishable from
the captured evidence alone, that the evidence stays bounded whatever
arrives, and that collecting it changes no transport behaviour.

    truncation          the frame stops early
    a corrupted byte    structure intact, one value wrong
    undecodable bytes   the decoder left replacement characters
    leading noise       a good frame with rubbish in front

WHAT IS DELIBERATELY NOT CHECKED

Any acceptance threshold. This suite is about EVIDENCE. HW-B1-009 still
qualifies a clean link and still fails on a single damaged frame; making
that test pass is not a goal and a change here must never be able to.
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

from serial_link import (                                    # noqa: E402
    CP210X_PACKET_BYTES,
    DAMAGE_PREFIX_CHARS,
    DAMAGE_SUFFIX_CHARS,
    describe_damage,
)

checks = support.Checks("damage-capture")


COMMANDS = ",".join('"{}"'.format(name) for name in (
    "acquire_block", "acquire_triad", "clear_all_slots", "clear_slot",
    "connect_servo", "delete_saved_samples", "disconnect_servo",
    "fine_adjust", "get_saved_sample", "get_servo_calibration",
    "get_status", "led_test", "list_saved_samples", "measure_raw",
    "move_slots", "ping", "select_slot", "sensor_test_raw"))

FRAME = ('{"request_id": 41, "cmd": "get_status", "ok": true, "data": '
         '{"version": "6.0.0", "protocol_version": 2, "commands": ['
         + COMMANDS + ']}}')


def damage(line):
    """Describe `line` exactly as the reader would, or fail the suite."""
    try:
        json.loads(line)

    except (ValueError, RecursionError) as error:
        return describe_damage(line, parse_error=error, counters=None)

    raise AssertionError(
        "the fixture parsed cleanly, so it is not a damaged frame"
    )


# ======================================================================
checks.section("the request behind a damaged frame is recoverable")

# THE FIELD THAT TIES DAMAGE TO A COMMAND. Without it a damaged frame is
# an anonymous event; with it, "which command was in flight" can be
# answered - and for a MOVEMENT command that is the difference between
# a diagnostic curiosity and an unsafe retry.
report = damage(FRAME[:120])

checks.equal(report["request_id"], "41",
             "the request id is recovered from a line that will not "
             "parse")
checks.equal(report["cmd"], "get_status",
             "and so is the command it was answering")

# It must not invent one when the field never arrived.
headless = damage('{"ok": true, "data": {"x": ')

checks.equal(headless["request_id"], None,
             "a frame with no request_id reports None, never a guess")


# ======================================================================
checks.section("the four damage shapes are distinguishable")

truncated = damage(FRAME[:120])

checks.ok(truncated["length"] == 120,
          "a truncated frame reports its REAL length, not the cap - the "
          "old capture reported 400 for everything and read like a "
          "measurement")
checks.ok(truncated["parse_error_offset"] is not None,
          "and the offset where parsing stopped")
checks.ok(not truncated["salvageable"],
          "and that nothing could be salvaged from it")
checks.equal(truncated["json_start"], 0,
             "and that the frame did start where it should")

corrupted = damage(FRAME[:300] + "\x00" + FRAME[301:])

checks.equal(corrupted["length"], len(FRAME),
             "a single corrupted byte leaves the length intact")
checks.equal(corrupted["parse_error_offset"], 300,
             "and the offset names where the byte went wrong")
checks.ok("\\x00" in corrupted["at_fault"],
          "and the bytes at the fault are escaped, so a non-printable "
          "is visible instead of vanishing into the terminal")
checks.ok("get_" in corrupted["window"],
          "with readable context around it")

undecodable = damage(FRAME[:60] + "��" + FRAME[62:])

checks.equal(undecodable["replacement_chars"], 2,
             "replacement characters are counted")
checks.ok(undecodable["undecodable_bytes"],
          "and flagged - this separates 'bytes arrived wrong' from "
          "'bytes arrived fine and the structure is wrong'")
checks.ok(not corrupted["undecodable_bytes"],
          "while a clean-byte corruption is not flagged as a decode "
          "failure")

noisy = damage("XYZ!!" + FRAME)

checks.equal(noisy["json_start"], 5,
             "leading noise is located by where the JSON starts")
checks.ok(noisy["salvageable"],
          "and such a frame is reported as salvageable - the reader "
          "recovers it, and the evidence should say so rather than "
          "reading like an unexplained loss")


# ======================================================================
checks.section("the CP210x packet alignment is reported, not concluded")

# A frame that loses or repeats a whole 64-byte USB packet fails at a
# multiple of 64. That is a different fault from a flipped byte - the
# bridge or the host driver rather than the wire - so the alignment is
# surfaced. It is EVIDENCE, not a verdict: one sample proves nothing,
# and a 64-aligned offset can happen by chance.
aligned = damage(FRAME[:CP210X_PACKET_BYTES] + "\x00"
                 + FRAME[CP210X_PACKET_BYTES + 1:])

checks.equal(aligned["cp210x_packet_offset"], 0,
             "a fault exactly on a 64-byte boundary reports offset 0")
checks.ok(aligned["cp210x_packet_aligned"],
          "and is flagged as packet-aligned")

checks.ok(not corrupted["cp210x_packet_aligned"],
          "a fault at 300 is not packet-aligned (300 % 64 = 44), so the "
          "flag discriminates instead of always firing")

checks.ok("verdict" not in aligned and "cause" not in aligned,
          "and nothing in the report claims a CAUSE - the alignment is "
          "an observation for a human to weigh")


# ======================================================================
checks.section("the evidence stays bounded")

# A damaged line can be up to MAX_FRAME_BYTES, and every one of them is
# also appended to `damaged_lines` up to NOISE_LIMIT times. An unbounded
# diagnostic turns a link fault into a memory problem during the one
# session where the link is already misbehaving.
huge = '{"request_id": 7, "cmd": "get_status", "ok": true, "d": "' \
       + "x" * 200000

report = damage(huge)

checks.equal(report["length"], len(huge),
             "the true length of a huge damaged line is still reported")
checks.ok(len(report["prefix"]) <= DAMAGE_PREFIX_CHARS,
          "but the prefix is capped")
checks.ok(len(report["suffix"]) <= DAMAGE_SUFFIX_CHARS,
          "and the suffix is capped")
checks.ok(report["truncated_middle"],
          "and the report says its middle was dropped, so nobody reads "
          "the excerpt as the whole frame")

budget = sum(
    len(value) for value in report.values() if isinstance(value, str)
)

checks.ok(budget < 4000,
          "the whole report is under 4 kB of strings for a 200 kB line "
          "({} chars)".format(budget))

# The legacy field survives, because damaged_lines and existing readers
# still use it.
checks.ok(len(report["line"]) <= 400,
          "the original `line` field is still present and still capped, "
          "so existing readers keep working")


# ======================================================================
checks.section("describing damage changes nothing")

# The diagnostic must be inert. It is called while an exception is being
# built, on a link that is already misbehaving.
original = FRAME[:120]
before = str(original)

report = damage(original)

checks.equal(original, before,
             "describing a damaged line does not modify it")

# It must never raise, whatever it is handed - a diagnostic that throws
# replaces MALFORMED_RESPONSE, which names the real problem, with
# whatever went wrong while describing it.
for hostile in ("", "{", "\x00\x00\x00", "}" * 500, '{"request_id":'):
    try:
        describe_damage(hostile, parse_error=None, counters=None)
        raised = None

    except Exception as error:
        raised = "{}: {}".format(type(error).__name__, error)

    checks.ok(raised is None,
              "describe_damage({!r}) does not raise{}".format(
                  hostile[:12], "" if raised is None else
                  " (" + raised + ")"))

# A None parse_error is normal: RecursionError carries no offset.
report = describe_damage(FRAME, parse_error=None, counters=None)

checks.ok("parse_error_offset" not in report,
          "with no parse error there is no offset field, rather than a "
          "zero that would read as 'failed at the first byte'")


# ======================================================================
checks.section("the counters travel with the frame")


class _Counters:
    corrupt_frames = 3
    salvaged_frames = 1
    stale_frames = 0
    oversized_lines = 0
    bytes_read = 239464


report = describe_damage(FRAME[:120], parse_error=None,
                         counters=_Counters())

checks.equal(report["counters"]["corrupt_frames"], 3,
             "the transport counters are attached to the frame that "
             "raised, so a single event carries the run's context")
checks.equal(report["counters"]["bytes_read"], 239464,
             "including how much had been read when it happened")


sys.exit(checks.report())
