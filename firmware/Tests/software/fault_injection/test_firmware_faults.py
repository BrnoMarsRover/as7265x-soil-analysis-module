"""
The firmware's own error paths, driven rather than assumed.

WHY THIS FILE EXISTS

`audit/handler_coverage.py` runs every suite under coverage and asks
which `except` bodies actually executed. It found 35 in the ESP32 tree
that no test had ever reached - argument validation, sensor and servo
failures, the response writer's fallbacks, and both MemoryError
handlers. A handler that has never run is a handler whose behaviour is
an assumption.

    11   MemoryError at the firmware's real allocation boundaries
    9    a broad handler fed a programming-type exception
    20   payloads whose fields are the wrong type entirely
    ..   sensor and servo failures, through the real dispatcher

WHAT IS AND IS NOT CLAIMED

CPython cannot reproduce ESP32 heap fragmentation, and nothing here
pretends to. What it does is inject `MemoryError` at the points the
firmware ALREADY guards, and check that the guard does what it was
written for: produce a controlled answer, and leave the command
processor able to serve the next request. Whether the real device has
enough heap during a real acquisition is H-005, and stays hardware.

THE PROPERTY EVERY CASE SHARES

    a bad command -> an error response -> the NEXT command still works

Silence is the one outcome the PC cannot tell apart from a dead board,
so a firmware that answers nothing is worse than one that answers
badly.

WHAT IS FAKED

`machine.I2C` and `machine.UART`, through the fakes that speak the real
register and packet protocols. Every line of `Protocol`, `Carousel`,
`Servo` and `Sensor` runs for real.
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

checks = support.Checks("firmware-faults")


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def fresh():
    """
    A firmware instance with the fake sensor and servo behind it.

    `build_firmware` PURGES and re-imports the ESP32 tree every time, so
    that one test's config override cannot leak into the next. That has
    a consequence worth stating: a `protocol` module imported once at
    the top of this file would, after the first `fresh()`, be a
    different object from the one the live service is running. Patching
    it would inject nothing.

    So the live module is handed back with the instance, and every
    patch below is applied to THAT one.
    """
    main, service, config, servo = support.build_firmware()

    return main, service, config, servo, sys.modules["protocol"]


class patched:
    """Replace one attribute for the duration of a block."""

    def __init__(self, target, name, replacement):
        self.target = target
        self.name = name
        self.replacement = replacement
        self.original = getattr(target, name)

    def __enter__(self):
        setattr(self.target, self.name, self.replacement)

        return self

    def __exit__(self, exc_type, exc_value, traceback):
        setattr(self.target, self.name, self.original)

        return False


def raiser(exception):
    def raise_it(*args, **kwargs):
        raise exception

    return raise_it


def answer(service, cmd, **payload):
    """Dispatch one command and hand back the frame, never an exception."""
    request = {"request_id": "test-1", "cmd": cmd}
    request.update(payload)

    return service.dispatch(request)


def still_alive(service, note):
    """The command processor answers a following ping."""
    frame = answer(service, "ping")

    checks.ok(isinstance(frame, dict) and frame.get("ok") is True,
              "  and the next command is still processed {}".format(note))


# ======================================================================
checks.section("11. MemoryError at the response writer's boundaries")

# `_ChunkSink.flush` joins the pieces of a response and falls back to
# writing them one at a time when the join cannot be allocated. That is
# the firmware's answer to a fragmented heap, and it had never run.

class JoinRefusingOnce(list):
    """
    A parts list whose FIRST iteration raises MemoryError.

    This is how a fragmented heap presents itself to `_ChunkSink.flush`:
    `"".join(parts)` needs one contiguous allocation the size of the
    whole response and cannot get it, while writing the parts one at a
    time needs only the largest single piece and succeeds.

    Raising on the first pass and behaving on the second reproduces
    exactly that, and nothing else - a list that refused BOTH passes
    would be testing a condition the firmware never claimed to survive.
    """

    def __init__(self, items):
        super().__init__(items)
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1

        if self.iterations == 1:
            raise MemoryError("no contiguous block for the joined response")

        return super().__iter__()


_m, _s, _c, _v, protocol_module = fresh()

sink = protocol_module._ChunkSink()
written = []
sink._write = written.append
sink._parts = JoinRefusingOnce(["chunk-one", "chunk-two", "chunk-three"])
sink._size = 27

sink.flush()

checks.equal(written, ["chunk-one", "chunk-two", "chunk-three"],
             "when the joined response cannot be allocated, the pieces "
             "are written one at a time instead - the whole response "
             "still reaches the PC")

checks.equal(sink._size, 0,
             "and the sink is reset, so the fallback does not leave the "
             "next response carrying this one's parts")

# The ordinary path still works, which is what makes the fallback a
# fallback rather than the only behaviour.
sink = protocol_module._ChunkSink()
written = []
sink._write = written.append
sink._parts = ["one", "two"]
sink._size = 6

sink.flush()

checks.equal(written, ["onetwo"],
             "with memory available the pieces are joined into one write")

# Drive the REAL path: a response too large to join in one piece.
main, service, config, servo, protocol_module = fresh()

frame = answer(service, "get_status")

checks.ok(frame.get("ok") is True,
          "an ordinary response is produced normally")

# Now make json.dumps refuse, which is the first thing send_json tries.
with patched(protocol_module.json, "dumps",
             raiser(MemoryError("cannot serialize"))):
    frame = answer(service, "ping")

checks.ok(isinstance(frame, dict),
          "MemoryError while serializing does not stop dispatch from "
          "returning a frame")

still_alive(service, "after a serialization failure")


# ======================================================================
checks.section("11. MemoryError inside a command handler")

# The dispatcher's last-resort handler turns any unexpected exception
# into a well-formed error response, because silence is the one thing
# the PC cannot diagnose.

MEMORY_TARGETS = (
    ("ping", "handle_ping"),
    ("get_status", "handle_get_status"),
    ("sync_position", "handle_sync_position"),
    ("measure_raw", "handle_measure_raw"),
    ("acquire_triad", "handle_acquire_triad"),
    ("servo_diagnostics", "handle_servo_diagnostics"),
)

for cmd, handler_name in MEMORY_TARGETS:
    main, service, config, servo, protocol_module = fresh()

    with patched(type(service), handler_name,
                 raiser(MemoryError("heap exhausted"))):
        frame = answer(service, cmd)

    checks.ok(isinstance(frame, dict),
              "MemoryError inside {} still produces a frame".format(cmd))

    checks.equal(frame.get("ok"), False,
                 "  and the frame says ok:false")

    checks.equal((frame.get("error") or {}).get("exception_type"),
                 "MemoryError",
                 "  and NAMES MemoryError, so a heap failure is not "
                 "filed as a sensor fault")

    still_alive(service, "after MemoryError in {}".format(cmd))


# ======================================================================
checks.section("9. a broad handler fed a programming-type exception")

# The section 9 question: does a broad `except Exception` quietly turn a
# programming defect into a plausible mission state? Each of these is a
# bug in the firmware, not a condition of the hardware, and each must
# arrive on the PC as an error naming its own type.

PROGRAMMING_ERRORS = (
    (AttributeError("'NoneType' object has no attribute 'read'"),
     "AttributeError"),
    (TypeError("unsupported operand type(s)"), "TypeError"),
    (AssertionError("an invariant the firmware believed"),
     "AssertionError"),
    (KeyError("a field the handler assumed"), "KeyError"),
    (IndexError("list index out of range"), "IndexError"),
    (ZeroDivisionError("division by zero"), "ZeroDivisionError"),
)

for exception, name in PROGRAMMING_ERRORS:
    main, service, config, servo, protocol_module = fresh()

    with patched(type(service), "handle_get_status", raiser(exception)):
        frame = answer(service, "get_status")

    checks.equal(frame.get("ok"), False,
                 "{} inside a handler is an error, never a success"
                 .format(name))

    checks.equal((frame.get("error") or {}).get("code"), "INTERNAL_ERROR",
                 "  reported as INTERNAL_ERROR")

    checks.equal((frame.get("error") or {}).get("exception_type"), name,
                 "  with the real exception type preserved for whoever "
                 "has to fix it")

    # THE NEGATIVE THAT MATTERS. A programming defect must not come back
    # looking like an ordinary operational refusal that an operator
    # would retry.
    checks.ok((frame.get("error") or {}).get("code")
              not in ("SENSOR_UNAVAILABLE", "SERVO_ERROR",
                      "POSITION_NOT_SYNCHRONIZED"),
              "  and NOT as a hardware condition the operator would "
              "chase")

    still_alive(service, "after {}".format(name))


# ======================================================================
checks.section("20/21. payload fields of entirely the wrong type")

# The `except (TypeError, ValueError)` handlers in the dispatcher are
# argument validation, and they are reached by sending a field that is
# not the kind of thing it should be.

WRONG_TYPES = (
    ("select_slot", {"slot": "not-a-number"}, "a slot that is a word"),
    ("select_slot", {"slot": None}, "a slot that is null"),
    ("select_slot", {"slot": [1, 2]}, "a slot that is a list"),
    ("select_slot", {"slot": {"a": 1}}, "a slot that is an object"),
    ("select_slot", {"slot": True}, "a slot that is a boolean"),
    ("move_slots", {"direction": "cw", "slots": "many"},
     "a slot count that is a word"),
    ("move_slots", {"direction": 7, "slots": 1},
     "a direction that is a number"),
    ("fine_adjust", {"degrees": "a lot"}, "an angle that is a word"),
    ("fine_adjust", {"degrees": None}, "an angle that is null"),
    ("acquire_block", {"illumination": 5, "repeats": 1},
     "an illumination that is a number"),
    ("acquire_block", {"illumination": "white", "repeats": "three"},
     "a repeat count that is a word"),
    ("measure_raw", {"slot": 1, "repeats": []},
     "a repeat count that is a list"),
    ("clear_slot", {"slot": "all"}, "a slot named 'all'"),
    ("get_saved_sample", {"sample_id": 12}, "a sample id that is a number"),
    ("servo_test_move", {"kind": None}, "a movement kind that is null"),
    ("servo_configure", {"mode": "fast"}, "a mode that is a word"),
)

for cmd, payload, label in WRONG_TYPES:
    main, service, config, servo, protocol_module = fresh()

    try:
        frame = answer(service, cmd, **payload)
        crashed = None

    except BaseException as error:                         # noqa: BLE001
        frame, crashed = None, type(error).__name__

    checks.ok(crashed is None,
              "{} does not escape the dispatcher ({})".format(
                  label, crashed or "handled"))

    if frame is not None:
        checks.equal(frame.get("ok"), False,
                     "  and is refused rather than acted on")

        checks.ok(bool((frame.get("error") or {}).get("code")),
                  "  with an error code the PC can read")

    still_alive(service, "after {}".format(label))


# ======================================================================
checks.section("the response the firmware cannot serialize")

# `send_json` falls back to `make_json_safe` when json.dumps refuses,
# and to a minimal error frame when even that fails. Both are guards
# that had never run.

class Unserializable:
    """A value json refuses: a driver handle, a UART, an exception."""


safe = protocol_module.make_json_safe({
    "driver": Unserializable(),
    "nested": {"uart": Unserializable()},
    "list": [Unserializable(), 1, "text"],
    "number": 12.5,
    "bool": True,
    "none": None,
})

checks.equal(safe["driver"], "<Unserializable>",
             "an unserializable value becomes its type name")

checks.equal(safe["nested"]["uart"], "<Unserializable>",
             "including inside a nested object")

checks.equal(safe["list"][1], 1,
             "and NUMBERS STAY NUMBERS - a stringified spectrum would "
             "be a scientific defect, not a formatting one")

checks.equal(safe["number"], 12.5, "floats are untouched")
checks.equal(safe["bool"], True, "and so are booleans")

# The depth guard, which stops a cyclic or pathological structure from
# recursing forever on a device with a small stack.
deep = current = {}

for _ in range(40):
    current["next"] = {}
    current = current["next"]

result = protocol_module.make_json_safe(deep)
depth = 0
walk = result

while isinstance(walk, dict) and "next" in walk:
    walk = walk["next"]
    depth += 1

checks.ok(depth <= 13,
          "nesting is truncated at the documented depth ({} levels "
          "kept), so a pathological structure cannot exhaust the "
          "device's stack".format(depth))

checks.equal(walk, "<nested too deeply>",
             "and the truncation says so rather than dropping the value")

# A whole dispatch whose result contains something unserializable.
main, service, config, servo, protocol_module = fresh()


def unserializable_status(request):
    return {"handle": Unserializable(), "ok_field": 1}


with patched(type(service), "handle_get_status", unserializable_status):
    frame = answer(service, "get_status")

checks.ok(isinstance(frame, dict),
          "a handler that returns an unserializable value still produces "
          "a frame")

still_alive(service, "after an unserializable response")


# ======================================================================
checks.section("sensor and servo failures, through the real dispatcher")

# The `except SensorError` and `except ServoError` handlers in
# protocol.py, driven by making the fakes fail rather than by patching
# the handlers - so the real driver code runs and produces the real
# error.

# THE EXCEPTION CLASS MUST COME FROM THE LIVE INSTANCE.
#
# `build_firmware` purges and re-imports the ESP32 tree for every
# instance, so `from sensor import SensorError` at the top of this file
# binds a class that the running firmware has never heard of. Raising it
# does not match the live `except SensorError`; the request falls
# through to the last-resort handler and comes back INTERNAL_ERROR.
#
# That is exactly what happened when this section was first written: it
# reported a defect in the firmware's diagnosis when the real fault was
# a test raising the wrong class. It is the §93 failure mode - a test
# that never triggers the fault it believes it is injecting - and the
# fix is to take the class from the instance under test.

# THREE COMMANDS, THREE DIFFERENT CONTRACTS, and they are different on
# purpose. Asserting one shape for all three was the first version of
# this section and it reported two defects that were not there.
#
#   sensor_test_raw   a DIAGNOSTIC. The command succeeds - the test ran -
#                     and the finding is inside the data, with the stage
#                     that stopped it named. A frame that said ok:false
#                     would mean the diagnostic itself could not run.
#   acquire_triad     names WHICH illumination failed, which is more
#                     specific than the sensor error underneath it.
#   led_test          a plain refusal.

main, service, config, servo, protocol_module = fresh()
SensorError = sys.modules["sensor"].SensorError

with patched(type(service.sensor), "ensure_ready",
             raiser(SensorError("SENSOR_UNAVAILABLE",
                                "the sensor did not answer"))):
    frame = answer(service, "sensor_test_raw")

data = frame.get("data") or {}

checks.equal(frame.get("ok"), True,
             "sensor_test_raw with a dead sensor still COMPLETES - the "
             "diagnostic ran, and finding a fault is its purpose")

checks.equal(data.get("ok"), False,
             "  and the result inside says the sensor failed")

checks.ok(not data.get("raw"),
          "  with no spectrum - a test that could not read the sensor "
          "must not return numbers")

failed_checks = [entry for entry in data.get("checks") or []
                 if not entry.get("ok")]

checks.equal([entry["stage"] for entry in failed_checks],
             ["SENSOR_RECOVERY"],
             "  and the stage list names exactly where it stopped")

# `failed_stage` is copied from the ERROR's own stage, not from the
# point in the sequence. A synthetic SensorError raised by this test
# carries no stage, so it reads UNKNOWN - which is the honest answer
# for an error that never said. The stage list above is the field that
# locates the failure, and it is the one worth asserting.
checks.ok(data.get("failed_stage") is not None,
          "  and failed_stage is always present, even when the error "
          "itself did not name one (here: {})".format(
              data.get("failed_stage")))

still_alive(service, "after a sensor failure in sensor_test_raw")

# acquire_triad: a refusal that names the illumination.
main, service, config, servo, protocol_module = fresh()
SensorError = sys.modules["sensor"].SensorError

with patched(type(service.sensor), "ensure_ready",
             raiser(SensorError("SENSOR_UNAVAILABLE",
                                "the sensor did not answer"))):
    frame = answer(service, "acquire_triad")

checks.equal(frame.get("ok"), False,
             "acquire_triad with an unavailable sensor is refused")

code = (frame.get("error") or {}).get("code")

checks.ok(code and "ACQUISITION_FAILED" in code,
          "  with a code naming the acquisition that failed ({}) - more "
          "specific than the sensor error underneath it".format(code))

still_alive(service, "after a sensor failure in acquire_triad")

# led_test: a plain refusal.
main, service, config, servo, protocol_module = fresh()
SensorError = sys.modules["sensor"].SensorError

with patched(type(service.sensor), "ensure_ready",
             raiser(SensorError("SENSOR_UNAVAILABLE",
                                "the sensor did not answer"))):
    frame = answer(service, "led_test")

checks.equal(frame.get("ok"), False,
             "led_test with an unavailable sensor is refused")

checks.equal((frame.get("error") or {}).get("code"), "SENSOR_UNAVAILABLE",
             "  and says the SENSOR is the problem")

still_alive(service, "after a sensor failure in led_test")

SERVO_COMMANDS = ("connect_servo", "servo_diagnostics", "servo_torque",
                  "get_servo_calibration")

for cmd in SERVO_COMMANDS:
    main, service, config, servo, protocol_module = fresh()
    ServoError = sys.modules["servo"].ServoError

    with patched(type(service.servo), "connect",
                 raiser(ServoError("SERVO_NO_RESPONSE",
                                   "the servo did not answer"))):
        frame = answer(service, cmd)

    checks.ok(isinstance(frame, dict),
              "{} with a silent servo produces a frame".format(cmd))

    still_alive(service, "after a servo failure in {}".format(cmd))

# A servo that fails to connect must not leave the carousel pointing at
# it - the handler says so, and this is the check.
main, service, config, servo, protocol_module = fresh()
ServoError = sys.modules["servo"].ServoError

with patched(type(service.servo), "connect",
             raiser(ServoError("SERVO_NO_RESPONSE", "silent"))):
    frame = answer(service, "connect_servo")

status = answer(service, "get_status")
carousel = (status.get("data") or {}).get("carousel") or {}

checks.ok(not carousel.get("position_valid"),
          "a servo that failed to connect leaves the carousel position "
          "INVALID - it is not attached to a driver that is not there")


# ======================================================================
checks.section("every command survives a payload of pure nonsense")

# The blunt version of the argument tests: every command in the table,
# handed fields it never expects, must answer rather than crash.

NONSENSE = {
    "slot": object,
    "degrees": float("nan"),
    "repeats": -1,
    "direction": "sideways",
    "illumination": "gamma",
    "sample_id": ["not", "a", "string"],
    "enable": "maybe",
    "mode": 999999,
    "kind": {"nested": True},
    "hold_ms": "forever",
    "confirm": "yes please",
    "ids": "not a list",
    "bauds": {"a": 1},
    "load_slot": 1e309,
    "scan_slot": -0.0,
}

crashes = []

for cmd in sorted(protocol_module.COMMANDS):
    main, service, config, servo, protocol_module = fresh()

    payload = {key: value for key, value in NONSENSE.items()
               if key != "slot"}
    payload["slot"] = "not-a-slot"

    try:
        frame = service.dispatch({
            "request_id": "nonsense-1", "cmd": cmd, **payload})

        if not isinstance(frame, dict):
            crashes.append("{}: returned {}".format(cmd, type(frame)))

        elif "ok" not in frame:
            crashes.append("{}: frame has no ok field".format(cmd))

    except BaseException as error:                         # noqa: BLE001
        crashes.append("{}: raised {}".format(cmd, type(error).__name__))

checks.equal(crashes, [],
             "all {} commands answered a payload of pure nonsense with a "
             "well-formed frame".format(len(protocol_module.COMMANDS)))

# And an unknown command, which is the one case the table cannot cover.
main, service, config, servo, protocol_module = fresh()

frame = answer(service, "definitely_not_a_command")

checks.equal(frame.get("ok"), False,
             "an unknown command is refused")

checks.ok(bool((frame.get("error") or {}).get("code")),
          "with a code rather than a traceback")

still_alive(service, "after an unknown command")


# ======================================================================
checks.section("22. an over-long command line is refused before parsing")

# The firmware's half of the frame-size policy. MAX_COMMAND_BYTES is
# checked before json.loads is asked to do anything with the line.

main, service, config, servo, protocol_module = fresh()

sent = []

with patched(protocol_module, "send_json", sent.append):
    service.process_line("x" * (config.MAX_COMMAND_BYTES + 1))

checks.equal(len(sent), 1,
             "an over-long line produces exactly one answer")

checks.equal((sent[0].get("error") or {}).get("code"), "COMMAND_TOO_LONG",
             "and the answer is COMMAND_TOO_LONG")

sent = []

with patched(protocol_module, "send_json", sent.append):
    service.process_line("not json at all")

checks.equal((sent[0].get("error") or {}).get("code"), "INVALID_JSON",
             "a line that is not JSON is INVALID_JSON, a different "
             "diagnosis")

# A line exactly at the limit is NOT refused - a cap that rejects legal
# input is worse than no cap.
sent = []
padding = config.MAX_COMMAND_BYTES - len(
    json.dumps({"request_id": "x", "cmd": "ping", "pad": ""}))

with patched(protocol_module, "send_json", sent.append):
    service.process_line(json.dumps(
        {"request_id": "x", "cmd": "ping", "pad": "p" * padding}))

checks.equal(len(sent), 1, "a line exactly at the limit is answered")

checks.equal(sent[0].get("ok"), True,
             "and answered SUCCESSFULLY - the limit is a ceiling, not a "
             "trap")


sys.exit(checks.report())
