"""
Can an answer to another question ever be accepted as the answer to ours?

WHY THIS DESERVES ITS OWN SUITE

Request identity is the single mechanism that makes the protocol
trustworthy. Everything else - timeouts, framing, salvage, the failure
codes - assumes that when a frame is accepted it is the answer to the
command that was sent. If that assumption can be broken, a measurement
from a previous session can be filed as this one's, and nothing
downstream can tell.

TWO DESIGNS HAVE ALREADY FAILED HERE

    counter from 1        Every session's first request was "1". A
                          client that died with a command in flight
                          left its answer in the driver buffer, and
                          the next client took it. Found by
                          fault_injection, fixed, regression-tested.

    clock-seeded counter  `time.time()*1000 % 1000000` WRAPS EVERY
                          1000 SECONDS. Two clients started sixteen
                          minutes apart produced byte-identical ids -
                          a routine interval for an operator
                          restarting the client mid-run. The command
                          check masked it for cross-command cases and
                          not for same-command ones, and `wait_online`
                          makes `ping` the first command of EVERY
                          session. Found by review, not by a test.

THE DESIGN UNDER TEST

    request_id = "<24 random bits from os.urandom>-<counter>"

and a frame is accepted only when all three hold:

    the id matches
    the frame carries "ok"          (an echo does not)
    the command name matches        (or the frame carries none)

WHAT THIS SUITE PROVES

Not that a collision is unlikely - that a collision is not sufficient.
Every check below FORGES a matching id and then asks whether the frame
is accepted anyway.
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
from serial_link import DeviceError, LinkError, SerialLink   # noqa: E402

from fakes import FakeClock                                  # noqa: E402
from fakes.clock import install_clock                        # noqa: E402
from fakes.esp32 import ScriptedDevice, TIMEOUT               # noqa: E402
from fakes.serial_port import install_fake_serial, open_link  # noqa: E402

checks = support.Checks("request-identity")

restore_serial = install_fake_serial(serial_link)


def linked(answering=False, **kwargs):
    """
    A link on a port that answers NOTHING unless asked to.

    `device=None` makes FakeSerialPort generate a valid reply to every
    request, which would mean each check below saw the planted frame
    skipped and then the fake's own genuine answer - reporting OK and
    proving nothing. A silent device leaves the planted frame as the
    only thing in the buffer, which is the situation being tested.
    """
    clock = FakeClock()
    installed = install_clock(serial_link, clock)
    device = None if answering else ScriptedDevice(default=TIMEOUT)
    link, port = open_link(serial_link, device, clock=clock, **kwargs)
    link.online = True

    return link, port, clock, installed


def frame(request_id, cmd="get_status", ok=True, data=None):
    payload = {"request_id": request_id, "cmd": cmd}

    if ok is not None:
        payload["ok"] = ok

    payload["data"] = data if data is not None else {"planted": True}

    return (json.dumps(payload) + "\n").encode("utf-8")


def next_id(link):
    """The id the next request will carry, without consuming it."""
    return "{}-{}".format(link.session, link._request_id + 1)


def outcome(call):
    try:
        return "OK", call()

    except LinkError as error:
        return error.code, None

    except Exception as error:                         # noqa: BLE001
        return "CRASH:" + type(error).__name__, None


# ======================================================================
checks.section("the identifier is unique by construction")

sessions = []

for _index in range(200):
    link, port, clock, installed = linked()
    sessions.append(link.session)
    installed.restore()
    link.close()

checks.equal(len(set(sessions)), len(sessions),
             "200 sessions built back to back, and all {} nonces are "
             "different - the previous design's seed wrapped every 1000 "
             "seconds and gave two of them the same ids".format(
                 len(sessions)))

checks.ok(all(len(value) >= 6 for value in sessions),
          "each nonce is at least 24 bits")

# §32: can a clock adjustment move ids backwards? The nonce does not
# come from the clock, so this is answered by construction rather than
# by hoping the clock is monotonic.
source = (support.FIRMWARE / "PC" / "serial_link.py").read_text(
    encoding="utf-8")

checks.ok("os.urandom" in source,
          "the nonce comes from os.urandom, so no clock adjustment - "
          "NTP step, manual change, timezone - can move it backwards "
          "or make two sessions agree")
checks.ok("random.seed" not in source and "import random" not in source,
          "and not from `random`, which a test or a tool could seed "
          "globally and make every session identical again")


# ======================================================================
checks.section("ids within one session never repeat")

link, port, clock, installed = linked()

try:
    seen = [link._next_request_id() for _index in range(5000)]

    checks.equal(len(set(seen)), 5000,
                 "5000 ids in one session, none repeated")

    nonces = {value.rsplit("-", 1)[0] for value in seen}
    counters = [int(value.rsplit("-", 1)[1]) for value in seen]

    checks.equal(len(nonces), 1, "all carrying one session nonce")
    checks.equal(counters, list(range(1, 5001)),
                 "and a counter with no gaps and no repeats")

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("a forged matching id is still not enough")

# Every case below plants a frame whose id is EXACTLY the one the link
# is about to use. The nonce is bypassed on purpose: what is under test
# is what happens when it is defeated.

CASES = (
    ("a different command", "measure_raw", True, "PROTOCOL_TIMEOUT"),
    ("another different command", "connect_servo", True,
     "PROTOCOL_TIMEOUT"),
    ("no ok field at all - an echo", "get_status", None,
     "PROTOCOL_TIMEOUT"),
    ("a stale SUCCESS for another command", "acquire_triad", True,
     "PROTOCOL_TIMEOUT"),
)

for label, cmd, ok, expected in CASES:
    link, port, clock, installed = linked(link_kwargs={"timeout": 2.0})

    try:
        port._enqueue(frame(next_id(link), cmd=cmd, ok=ok))

        code, data = outcome(lambda: link.request("get_status"))

        checks.equal(code, expected,
                     "{}: refused as {}".format(label, expected))
        checks.ok(data is None or not data.get("planted"),
                  "and the planted payload never reaches the caller "
                  "({})".format(label))

    finally:
        installed.restore()
        link.close()

# The one case that IS accepted, stated explicitly so the boundary is
# visible: a frame with the right id, the right command and an ok flag
# is the answer, and must be.
link, port, clock, installed = linked(link_kwargs={"timeout": 2.0})

try:
    port._enqueue(frame(next_id(link), cmd="get_status", ok=True))

    code, data = outcome(lambda: link.request("get_status"))

    checks.equal(code, "OK",
                 "a frame with the right id AND the right command IS "
                 "the answer - this is the boundary, and it has to be "
                 "on the accepting side of it")
    checks.equal((data or {}).get("planted"), True,
                 "and its payload is delivered")

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("a stale ERROR cannot fail a healthy command")

# The mirror of the case above, and the more dangerous direction: a
# leftover ok:false would turn a working command into a device error
# the firmware never reported.

link, port, clock, installed = linked(link_kwargs={"timeout": 2.0})

try:
    planted = json.dumps({
        "request_id": next_id(link), "ok": False, "cmd": "measure_raw",
        "error": {"code": "SENSOR_UNAVAILABLE", "message": "stale"},
    }) + "\n"

    port._enqueue(planted.encode("utf-8"))

    code, _data = outcome(lambda: link.request("get_status"))

    checks.equal(code, "PROTOCOL_TIMEOUT",
                 "a leftover REFUSAL for another command does not fail "
                 "this one - inventing a device error is as bad as "
                 "inventing a success")
    checks.equal(link.stale_frames, 1, "and it is counted as stale")

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("a storm of stale frames, then the real one")

# §36. The buffer holds a whole previous session, and the current
# answer is at the end of it.

link, port, clock, installed = linked(link_kwargs={"timeout": 5.0})

try:
    target = next_id(link)

    for index in range(200):
        port._enqueue(frame("{}-{}".format(link.session, index + 500),
                            cmd="measure_raw"))
        port._enqueue(frame(target, cmd="acquire_triad"))
        port._enqueue(b"rst:0x1 (POWERON_RESET)\n")

    # And finally the frame that really is ours.
    port._enqueue(frame(target, cmd="get_status",
                        data={"real": True}))

    code, data = outcome(lambda: link.request("get_status"))

    checks.equal(code, "OK",
                 "the real answer is found after 600 frames of rubbish")
    checks.equal((data or {}).get("real"), True,
                 "and it is the real one, not any of the 200 forged "
                 "frames that carried the same id")

    checks.ok(link.stale_frames >= 200,
              "every same-id wrong-command frame was counted as stale "
              "({})".format(link.stale_frames))
    checks.ok(len(link.last_noise) <= serial_link.NOISE_LIMIT,
              "and the diagnostic buffer stayed capped at {} through "
              "all of it ({} held)".format(serial_link.NOISE_LIMIT,
                                           len(link.last_noise)))

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("the id survives the round trip unchanged")

# The whole scheme depends on the firmware echoing the id verbatim. If
# it ever normalised, truncated or re-typed it, matching would silently
# stop working - so this is checked against the REAL firmware rather
# than assumed.

from fakes.esp32 import LoopbackDevice                        # noqa: E402

clock = FakeClock()
installed = install_clock(serial_link, clock)
loopback = LoopbackDevice()
loopback.build()
link, port = open_link(serial_link, loopback, clock=clock)
link.online = True

try:
    for _index in range(20):
        link.ping()

    sent = [request["request_id"] for request in port.requests]
    answered = [frame_.get("request_id") for frame_ in loopback.responses]

    checks.equal(answered, sent,
                 "the real firmware echoes all {} ids back byte for "
                 "byte, including the hyphen and the hex nonce".format(
                     len(sent)))

    checks.ok(all(isinstance(value, str) for value in answered),
              "and as strings - nothing parses the id as a number, "
              "which is what lets the format change without a protocol "
              "version bump")

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("two sessions cannot answer for each other")

# The scenario that motivated the whole design: one client dies, another
# starts, and the buffer still holds the first one's answers.

first, first_port, _c1, first_installed = linked()
first_id = next_id(first)
first_installed.restore()
first.close()

second, second_port, _c2, second_installed = linked(
    link_kwargs={"timeout": 2.0})

try:
    # The dead session's answer, verbatim, still in the driver buffer.
    second_port._enqueue(frame(first_id, cmd="get_status",
                               data={"from_previous_session": True}))

    code, data = outcome(lambda: second.request("get_status"))

    checks.equal(code, "PROTOCOL_TIMEOUT",
                 "the previous session's answer is not accepted even "
                 "though the COMMAND matches - the nonce differs, and "
                 "that is the defence the command check cannot provide")
    checks.ok(data is None or not data.get("from_previous_session"),
              "and none of its payload reaches the new session")

finally:
    second_installed.restore()
    second.close()


restore_serial()

sys.exit(checks.report())
