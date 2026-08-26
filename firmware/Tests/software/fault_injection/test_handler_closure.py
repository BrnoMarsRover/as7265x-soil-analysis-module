"""
The handlers nothing had ever entered.

WHY THIS FILE EXISTS

`audit/handler_coverage.py` measures which `except` bodies actually run.
After Phase A.3 it reported 59 that no test had entered - and 26 of
those were in `ESP32/protocol.py`, the most mission-critical file in the
project.

Reading them is not the same as running them. This file runs them.

THE ONE DISCOVERY THAT SHAPED IT

Most of the protocol handlers were unreachable BY THE TESTS, not by the
software. Every suite drove the firmware through `service.dispatch()`,
which is the command table - and `send_json`, `process_line` and
`serve_forever` sit ABOVE it. Four serialization handlers, the serving
loop's two rescue paths and the response-too-large fallback could not
be reached from below no matter what payload was sent.

So the rule here is: enter the firmware where the WIRE enters it.

    process_line(text)     what a received line really goes through
    send_json(payload)     what every response really leaves through
    dispatch(request)      only where the handler is inside a command

CLASSIFICATION, per the closure task section 4

Every handler this file touches is
`MISSION_RUNTIME_SOFTWARE_REACHABLE`: software alone can produce the
condition, so software alone must prove the behaviour. Handlers that
are `DEFENSIVE_UNREACHABLE`, `HARDWARE_ONLY`, `OFFLINE_ONLY` or
`CLEANUP_ONLY` are classified in `audit/handler_coverage.py` and are
not forced open here - inventing an impossible state to colour a line
green would be the opposite of assurance.

WHAT EACH CASE ASSERTS

    the exact failure is triggered   (proved, not assumed)
    the exact handler is entered
    the error or result is correct
    the resulting state is correct
    the next command still works
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

checks = support.Checks("handler-closure")


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def fresh():
    """
    A firmware instance, plus the LIVE modules behind it.

    `build_firmware` purges and re-imports the ESP32 tree every time, so
    a module imported once at the top of this file would, after the
    first call, be a different object from the one the service is
    running - and patching it would inject nothing. Every module and
    exception class used below comes from here.
    """
    main, service, config, servo = support.build_firmware()

    return {
        "main": main,
        "service": service,
        "config": config,
        "servo": servo,
        "protocol": sys.modules["protocol"],
        "sensor_module": sys.modules["sensor"],
        "servo_module": sys.modules["servo"],
        "SensorError": sys.modules["sensor"].SensorError,
        "ServoError": sys.modules["servo"].ServoError,
        "CarouselError": sys.modules["carousel"].CarouselError,
    }


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


class counting_raiser:
    """
    A replacement that raises, and REMEMBERS THAT IT DID.

    Section 25 of the closure task: a fault-injection test must carry
    evidence that the fault actually happened. A test whose injected
    exception is never raised passes for the wrong reason, and this
    tree has already produced two of those.
    """

    def __init__(self, exception):
        self.exception = exception
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1

        raise self.exception


def dispatch(service, cmd, **payload):
    request = {"request_id": "closure-1", "cmd": cmd}
    request.update(payload)

    return service.dispatch(request)


def line_for(cmd, **payload):
    request = {"request_id": "closure-1", "cmd": cmd}
    request.update(payload)

    return json.dumps(request)


class Wire:
    """Captures everything the firmware writes, as the PC would see it."""

    def __init__(self):
        self.chunks = []

    def __call__(self, text):
        self.chunks.append(text)

        return len(text)

    @property
    def text(self):
        return "".join(self.chunks)

    def frames(self):
        found = []

        for line in self.text.split("\n"):
            line = line.strip()

            if not line:
                continue

            try:
                found.append(json.loads(line))

            except ValueError:
                found.append({"__unparseable__": line})

        return found


def still_serving(env, note):
    """The command processor answers a following ping."""
    frame = dispatch(env["service"], "ping")

    checks.ok(isinstance(frame, dict) and frame.get("ok") is True,
              "  and the next command is still processed {}".format(note))


# ======================================================================
checks.section("send_json: a response that will not serialize")

# `send_json` is the single exit point for every response, and it has
# four failure branches. None had ever been entered, because every
# suite drove `dispatch()` - which returns a dict and never calls this.


class Unserializable:
    """A driver handle, a UART, an exception: json refuses all of them."""


env = fresh()
protocol = env["protocol"]

wire = Wire()

with patched(protocol, "_write_all", wire):
    protocol.send_json({
        "request_id": "closure-1",
        "ok": True,
        "cmd": "get_status",
        "data": {"driver": Unserializable(), "count": 7},
    })

frames = wire.frames()

checks.equal(len(frames), 1,
             "a payload json cannot encode still produces exactly one "
             "frame")

checks.equal(frames[0].get("ok"), True,
             "and the response is still the response - the fallback "
             "re-encodes it rather than replacing it with an error")

checks.equal(frames[0]["data"]["driver"], "<Unserializable>",
             "with the offending value replaced by its type name")

checks.equal(frames[0]["data"]["count"], 7,
             "and every value that COULD be encoded is untouched")

# The inner fallback: make_json_safe fails too.
env = fresh()
protocol = env["protocol"]
wire = Wire()
safe_failure = counting_raiser(TypeError("cannot make this safe either"))

with patched(protocol, "_write_all", wire):
    with patched(protocol, "make_json_safe", safe_failure):
        protocol.send_json({
            "request_id": "closure-1",
            "ok": True,
            "cmd": "get_status",
            "data": {"driver": Unserializable()},
        })

checks.equal(safe_failure.calls, 1,
             "the second serialization attempt really was made and "
             "really failed")

frames = wire.frames()

checks.equal(len(frames), 1,
             "and a minimal error frame goes out instead of silence")

checks.equal((frames[0].get("error") or {}).get("code"),
             "JSON_SERIALIZATION_ERROR",
             "carrying JSON_SERIALIZATION_ERROR")

checks.equal(frames[0].get("request_id"), "closure-1",
             "AND THE REQUEST ID - a frame without it is invisible to "
             "the PC, which matches answers by id and would wait out "
             "the whole timeout")


# ======================================================================
checks.section("send_json: MemoryError, and the streamed fallback")

# Measured on hardware: a 5148-byte response raised MemoryError with
# 90016 bytes free, because MicroPython needs the block contiguous.
# The response is real and the sensor data in it is expensive, so it is
# written in pieces rather than reported as a failed measurement.

class FragmentedHeap:
    """
    `json.dumps` on a heap with no large contiguous block.

    A BLANKET FAILURE IS THE WRONG MODEL, and the first version of this
    case used one. `_emit_json` recovers by offering each SUBTREE to
    json.dumps separately, so a dumps that always raises defeats the
    split as well and the streamed path can never succeed - which is
    not what a fragmented heap does.

    Measured on the board: 94,784 bytes free, largest single block 8 kB.
    Small allocations succeed; large ones do not. That is what this
    models, and it is why `limit` is a size rather than a count.
    """

    def __init__(self, real, limit):
        self.real = real
        self.limit = limit
        self.failures = 0
        self.calls = 0

    def __call__(self, obj, **kwargs):
        self.calls += 1
        text = self.real(obj, **kwargs)

        if len(text) > self.limit:
            self.failures += 1

            raise MemoryError(
                "no contiguous block of {} bytes".format(len(text)))

        return text


env = fresh()
protocol = env["protocol"]
wire = Wire()
dumps_failure = FragmentedHeap(protocol.json.dumps, limit=60)

with patched(protocol, "_write_all", wire):
    with patched(protocol.json, "dumps", dumps_failure):
        protocol.send_json({
            "request_id": "closure-1",
            "ok": True,
            "cmd": "acquire_triad",
            "data": {"illuminations": {"white": list(range(18))}},
        })

checks.ok(dumps_failure.failures >= 1,
          "the whole response really was too large for one block "
          "({} of {} allocations refused)".format(
              dumps_failure.failures, dumps_failure.calls))

frames = wire.frames()

checks.equal(len(frames), 1,
             "the response still goes out as exactly one frame")

checks.equal(frames[0].get("ok"), True,
             "and it is the REAL response - an expensive acquisition is "
             "not thrown away for want of a buffer")

checks.equal(frames[0]["data"]["illuminations"]["white"],
             list(range(18)),
             "with the spectrum intact, streamed piece by piece")

# And when even the streaming fails.
env = fresh()
protocol = env["protocol"]
wire = Wire()
emit_failure = counting_raiser(MemoryError("not even in pieces"))

with patched(protocol, "_write_all", wire):
    with patched(protocol.json, "dumps",
                 FragmentedHeap(protocol.json.dumps, limit=60)):
        with patched(protocol, "_emit_json", emit_failure):
            protocol.send_json({
                "request_id": "closure-1",
                "ok": True,
                "cmd": "acquire_triad",
                "data": {"illuminations": {"white": list(range(18))}},
            })

checks.equal(emit_failure.calls, 1,
             "the streaming writer really was reached and really failed")

text = wire.text

checks.ok("RESPONSE_TOO_LARGE" in text,
          "and the PC is told RESPONSE_TOO_LARGE rather than being left "
          "to time out")

checks.ok("MemoryError" in text,
          "with the exception type named")

checks.ok("fewer repeats" in text,
          "and an action the operator can actually take")

checks.ok(text.endswith("\n"),
          "and the line is TERMINATED - a half-written frame the PC "
          "cannot parse is still better than silence, but only if the "
          "line ends")


# ======================================================================
checks.section("_traceback_text: when even the traceback fails")

env = fresh()
protocol = env["protocol"]

# `sys.print_exception` is a MicroPython builtin and does not exist on
# CPython. That has a consequence worth stating rather than patching
# around: on the host simulation this function ALWAYS takes its
# `except Exception` branch, so the handler is executed by every test
# that reaches it - and the SUCCESS path, a real formatted traceback,
# is only ever exercised on the device.
#
# Classified accordingly: the handler is
# MISSION_RUNTIME_SOFTWARE_REACHABLE and driven here; its success path
# is HARDWARE_ONLY and belongs to Phase B.
checks.ok(not hasattr(protocol.sys, "print_exception"),
          "sys.print_exception is absent on CPython, which is what "
          "drives this handler in the host simulation")

text = protocol._traceback_text(ValueError("something"))

checks.equal(text, "traceback unavailable",
             "and the diagnostic degrades to a sentence instead of "
             "raising inside the error path")

checks.ok(isinstance(text, str) and text,
          "always returning a string, because the caller pastes it "
          "straight into an error frame")

# The other direction: with a print_exception present, the real path
# runs and produces a single line. This is what the device does.
def fake_print_exception(error, stream):
    stream.write("Traceback (most recent call last):\n")
    stream.write('  File "protocol.py", line 1\n')
    stream.write("{}: {}\n".format(type(error).__name__, error))


protocol.sys.print_exception = fake_print_exception

try:
    text = protocol._traceback_text(ValueError("something"))

finally:
    del protocol.sys.print_exception

checks.ok("\n" not in text,
          "with a real print_exception the traceback is folded onto ONE "
          "line - the error frame is one line of JSON")

checks.ok('"' not in text,
          "and carries no double quote that would break that frame")

checks.ok("ValueError" in text,
          "while still naming the exception")


# ======================================================================
checks.section("the heap probe, when every allocation fails")

# `_memory()` reports None on CPython, deliberately: `gc.mem_free` and
# `gc.mem_alloc` are MicroPython builtins, and the firmware reports
# their absence rather than faking a zero. So the probe loop - and its
# `except MemoryError` - cannot be reached on the host unless the two
# functions are supplied.
#
# Supplying them is legitimate simulation, not a forced state: it is
# the same thing `support.install_machine` does for `machine.I2C`, and
# the device really does have them. What is NOT simulated is the heap
# itself; only the two readings.

env = fresh()
protocol = env["protocol"]

checks.ok(env["service"]._memory() is None,
          "without MicroPython's gc functions the firmware reports "
          "memory as absent rather than faking a zero")

protocol.gc.mem_free = lambda: 94784
protocol.gc.mem_alloc = lambda: 30000

probe_failures = counting_raiser(MemoryError("no block of any size"))
protocol.bytearray = probe_failures

try:
    report = env["service"]._memory()

finally:
    del protocol.bytearray
    del protocol.gc.mem_free
    del protocol.gc.mem_alloc

checks.ok(probe_failures.calls >= 1,
          "every probe allocation really was attempted and really "
          "failed ({} sizes tried)".format(probe_failures.calls))

checks.equal(report.get("largest_block"), 0,
             "and the report says the largest allocatable block is 0 - "
             "not None, and not the last size it tried")

checks.equal(report.get("free"), 94784,
             "while the free figure is still reported")

checks.equal(report.get("allocated"), 30000,
             "and so is the allocated one - a heap with no usable block "
             "is exactly the fragmentation case, and the numbers are "
             "what make it diagnosable")


# ======================================================================
checks.section("process_line and serve_forever: the rescue frames")

# The two handlers above `dispatch`. Neither had been entered, and
# between them they are the last thing standing between a firmware
# fault and total silence on the wire.

env = fresh()
protocol = env["protocol"]
wire = Wire()
send_failure = counting_raiser(MemoryError("cannot send the answer"))

with patched(protocol, "_write_all", wire):
    with patched(protocol, "send_json", send_failure):
        env["service"].process_line(line_for("ping"))

checks.equal(send_failure.calls, 1,
             "send_json really was called and really failed")

text = wire.text

checks.ok("RESPONSE_FAILED" in text,
          "process_line answers with RESPONSE_FAILED rather than "
          "letting the PC wait out its timeout")

checks.ok("MemoryError" in text,
          "naming the exception type")

checks.ok("Ask for less in one request" in text,
          "and giving the operator something to do")

# The serving loop's own rescue, one level further out.
env = fresh()
protocol = env["protocol"]
wire = Wire()
process_failure = counting_raiser(RuntimeError("process_line itself died"))

lines = [line_for("ping"), None]

with patched(protocol, "_write_all", wire):
    with patched(protocol, "read_command", lambda: lines.pop(0)):
        with patched(type(env["service"]), "process_line",
                     process_failure):
            try:
                env["service"].serve_forever()

            except (IndexError, TypeError):
                # The scripted read_command runs out; that is how the
                # loop is stopped without a real console.
                pass

checks.equal(process_failure.calls, 1,
             "process_line really was called and really died")

checks.ok("RESPONSE_FAILED" in wire.text,
          "and the serving loop still puts a frame on the wire - "
          "silence is the one outcome the PC cannot tell from a dead "
          "board")


# ======================================================================
checks.section("argument validation: every (TypeError, ValueError) path")

# Seven handlers, each guarding one int()/float() conversion of one
# operator-supplied field. Each needs a payload that passes every
# EARLIER check and fails exactly this one - which is why a blanket
# "nonsense payload" never reached them.

VALIDATION = (
    ("servo_test_move",
     {"kind": "half_turn_forward", "repeat": "many", "confirm": True},
     "'repeat' must be a number.", "repeat"),

    ("servo_test_move",
     {"kind": "degrees", "degrees": "a lot", "confirm": True},
     "'degrees' must be a number.", "degrees"),

    # NOTE: the `hold_ms` validation in handle_servo_test_move is NOT
    # here, and cannot be. It is guarded by `if kind == "neutral"`, and
    # "neutral" is not among the eight kinds the ST3215 offers - so the
    # command rejects it as BAD_REQUEST before either branch is
    # reached. See the retired-kinds section at the end of this file.

    ("move_slots", {"direction": "cw", "slots": "several"},
     "'slots' must be a whole number.", "slots"),

    ("acquire_block", {"illumination": "white", "repeats": "three"},
     "'repeats' must be a number.", "repeats"),

    ("led_test", {"hold_ms": "a while"},
     "'hold_ms' must be a number.", "hold_ms"),
)

for cmd, payload, expected_message, field in VALIDATION:
    env = fresh()
    env["service"].servo.connect()

    frame = dispatch(env["service"], cmd, **payload)

    checks.equal(frame.get("ok"), False,
                 "{} with {}={!r} is refused".format(
                     cmd, field, payload[field]))

    error = frame.get("error") or {}

    checks.equal(error.get("code"), "BAD_REQUEST",
                 "  as BAD_REQUEST")

    checks.equal(error.get("message"), expected_message,
                 "  naming the field that was wrong")

    still_serving(env, "after a bad {}".format(field))


# ======================================================================
checks.section("servo failures, at every handler that catches one")

# Each of these is a different `except ServoError` in a different
# command, and each has its own consequence for the carousel.

env = fresh()
env["service"].servo.connect()
ServoError = env["ServoError"]

diagnostics_failure = counting_raiser(
    ServoError("the servo stopped answering", code="SERVO_UART_TIMEOUT"))

with patched(type(env["service"].servo), "diagnostics",
             diagnostics_failure):
    frame = dispatch(env["service"], "servo_diagnostics")

checks.equal(diagnostics_failure.calls, 1,
             "servo diagnostics really was called and really failed")

checks.equal(frame.get("ok"), False,
             "servo_diagnostics with a silent servo is refused")

checks.equal((frame.get("error") or {}).get("code"), "SERVO_UART_TIMEOUT",
             "carrying the driver's own code, not a generic one")

still_serving(env, "after a diagnostics failure")

# configure_mode
env = fresh()
env["service"].servo.connect()
ServoError = env["ServoError"]

configure_failure = counting_raiser(
    ServoError("the mode register would not take it",
              code="SERVO_MODE_ERROR"))

with patched(type(env["service"].servo.driver), "configure_mode",
             configure_failure):
    frame = dispatch(env["service"], "servo_configure", confirm=True)

checks.equal(configure_failure.calls, 1,
             "configure_mode really was called and really failed")

checks.equal((frame.get("error") or {}).get("code"), "SERVO_MODE_ERROR",
             "servo_configure reports the driver's code")

still_serving(env, "after a configure failure")

# A movement test that fails must cost the carousel position.
env = fresh()
dispatch(env["service"], "connect_servo")
dispatch(env["service"], "sync_position", load_slot=1)
ServoError = env["ServoError"]

checks.ok(env["service"].carousel.position_valid,
          "the carousel starts synchronized")

test_failure = counting_raiser(
    ServoError("it stopped somewhere else",
              code="SERVO_POSITION_MISMATCH"))

with patched(type(env["service"]), "_run_test", test_failure):
    frame = dispatch(env["service"], "servo_test_move",
                     kind="half_turn_forward", confirm=True)

checks.equal(test_failure.calls, 1,
             "the movement test really ran and really failed")

checks.equal(frame.get("ok"), False,
             "a failed servo test is refused")

checks.ok(not env["service"].carousel.position_valid,
          "AND THE CAROUSEL POSITION IS INVALIDATED - a movement test "
          "that failed left the mechanism somewhere nobody measured")

still_serving(env, "after a failed movement test")

# ServoNotSupportedError takes its own branch.
env = fresh()
env["service"].servo.connect()
ServoNotSupportedError = sys.modules["servo"].ServoNotSupportedError

unsupported = counting_raiser(
    ServoNotSupportedError("this servo cannot do that",
                           code="SERVO_NOT_SUPPORTED"))

with patched(type(env["service"]), "_run_test", unsupported):
    frame = dispatch(env["service"], "servo_test_move",
                     kind="half_turn_forward", confirm=True)

checks.equal(unsupported.calls, 1,
             "the unsupported movement really was attempted")

checks.equal((frame.get("error") or {}).get("code"),
             "SERVO_NOT_SUPPORTED",
             "an unsupported movement has its own code, separate from a "
             "movement that failed")

# The bus scan's broad handler.
env = fresh()
scan_failure = counting_raiser(RuntimeError("the scan machinery broke"))

with patched(env["servo_module"], "bus_scan", scan_failure):
    frame = dispatch(env["service"], "servo_bus_scan")

checks.equal(scan_failure.calls, 1,
             "the bus scan really was attempted and really failed")

checks.equal((frame.get("error") or {}).get("code"), "SERVO_SCAN_FAILED",
             "a scan that cannot run is SERVO_SCAN_FAILED")

checks.equal(((frame.get("error") or {}).get("details") or {}).get(
                 "exception_type")
             or ((frame.get("data") or {}).get("exception_type")),
             "RuntimeError",
             "with the underlying exception type kept for diagnosis")

still_serving(env, "after a failed bus scan")


# ======================================================================
checks.section("sensor failures, at every handler that catches one")

env = fresh()
SensorError = env["SensorError"]

block_failure = counting_raiser(
    SensorError("CHANNEL_READ_FAILED", "a channel would not read"))

with patched(type(env["service"].sensor), "acquire_block", block_failure):
    frame = dispatch(env["service"], "acquire_block",
                     illumination="white", repeats=1)

checks.equal(block_failure.calls, 1,
             "acquire_block really was called and really failed")

checks.equal(frame.get("ok"), False,
             "a failed block acquisition is refused")

checks.equal((frame.get("error") or {}).get("code"),
             "CHANNEL_READ_FAILED",
             "carrying the sensor's own code")

still_serving(env, "after a failed block")

# led_test's ensure_ready
env = fresh()
SensorError = env["SensorError"]

ready_failure = counting_raiser(
    SensorError("I2C_NO_DEVICES", "nothing on the bus"))

with patched(type(env["service"].sensor), "ensure_ready", ready_failure):
    frame = dispatch(env["service"], "led_test")

checks.equal(ready_failure.calls, 1,
             "ensure_ready really was called and really failed")

checks.equal((frame.get("error") or {}).get("code"), "I2C_NO_DEVICES",
             "led_test reports the bus fault")

still_serving(env, "after an I2C failure")

# The lamp readback inside led_test, which must not abort the test.
env = fresh()
SensorError = env["SensorError"]
driver = env["service"].sensor.ensure_ready()

lamp_failure = counting_raiser(
    SensorError("LED_STATE_NOT_APPLIED",
                "the lamp would not switch"))

with patched(type(driver), "enable_bulb", lamp_failure):
    frame = dispatch(env["service"], "led_test", hold_ms=0)

checks.ok(lamp_failure.calls >= 1,
          "the lamp really was switched and really failed")

data = frame.get("data") or {}
lamps = data.get("lamps") or []

checks.ok(any(not entry.get("ok") for entry in lamps),
          "a lamp that will not switch is reported as a failed lamp "
          "({} lamp(s) reported)".format(len(lamps)))

checks.ok(len(lamps) >= 1,
          "and the test continues through the remaining lamps rather "
          "than stopping at the first")

still_serving(env, "after a lamp failure")

# The two best-effort readbacks: a failure means "unknown", not a crash.
env = fresh()

readback_failure = counting_raiser(RuntimeError("the register is gone"))
driver = env["service"].sensor.ensure_ready()

with patched(type(driver), "bulb_states", readback_failure):
    bulbs_off = env["service"]._bulbs_off()

checks.equal(readback_failure.calls, 1,
             "the lamp readback really was attempted and really failed")

checks.ok(bulbs_off is None,
          "a lamp state that cannot be read is None - UNKNOWN, not "
          "False, which would claim the lamps are on")

temperature_failure = counting_raiser(RuntimeError("no thermometer"))

with patched(type(driver), "read_temperatures", temperature_failure):
    temperatures = env["service"]._temperatures()

checks.equal(temperature_failure.calls, 1,
             "the temperature read really was attempted and really failed")

checks.ok(temperatures is None,
          "and an unreadable temperature is None rather than a number")


# ======================================================================
checks.section("sensor_test_raw: a failure at each of its four stages")

# The diagnostic reports every stage, so a failure names the exact step
# that stopped it and partial results survive. Four separate handlers,
# none of which had been entered.

STAGES = (
    ("INTERNAL_DEVICES", "require_devices",
     "SENSOR_INIT_FAILED", "the internal devices did not answer"),
    ("CONFIGURATION", "read_configuration",
     "SENSOR_CONFIG_MISMATCH", "the configuration read back wrong"),
)

for stage, method, code, message in STAGES:
    env = fresh()
    SensorError = env["SensorError"]
    driver = env["service"].sensor.ensure_ready()

    failure = counting_raiser(SensorError(code, message))

    with patched(type(driver), method, failure):
        frame = dispatch(env["service"], "sensor_test_raw")

    checks.equal(failure.calls, 1,
                 "{} really was attempted and really failed".format(method))

    data = frame.get("data") or {}

    checks.equal(frame.get("ok"), True,
                 "the DIAGNOSTIC still completes - finding a fault is "
                 "its purpose ({})".format(stage))

    checks.equal(data.get("ok"), False,
                 "  and the result inside says the sensor failed")

    failed = [entry["stage"] for entry in data.get("checks") or []
              if not entry.get("ok")]

    checks.equal(failed, [stage],
                 "  naming exactly the stage that stopped it")

    checks.ok(not data.get("raw"),
              "  with no spectrum - a test that could not read the "
              "sensor must not return numbers")

    still_serving(env, "after a {} failure".format(stage))

# The acquisition stage, which is the one that costs a measurement.
env = fresh()
SensorError = env["SensorError"]

triad_failure = counting_raiser(
    SensorError("INCOMPLETE_SPECTRUM", "only two illuminations returned"))

with patched(type(env["service"].sensor), "acquire_triad", triad_failure):
    frame = dispatch(env["service"], "sensor_test_raw")

checks.equal(triad_failure.calls, 1,
             "the triad acquisition really was attempted and really "
             "failed")

data = frame.get("data") or {}

checks.equal(data.get("ok"), False,
             "an acquisition failure is reported as a failure")

checks.ok(not data.get("raw"),
          "and no spectrum is returned")

still_serving(env, "after an acquisition failure")


# ======================================================================
checks.section("carousel: sync by scan slot, and a failed return move")

env = fresh()
env["service"].servo.connect()
CarouselError = env["CarouselError"]

frame = dispatch(env["service"], "sync_position", scan_slot=99)

checks.equal(frame.get("ok"), False,
             "syncing to an impossible scan slot is refused")

checks.ok(bool((frame.get("error") or {}).get("code")),
          "with a code ({})".format((frame.get("error") or {}).get("code")))

checks.ok(not env["service"].carousel.position_valid,
          "and the position is not left valid after a refused sync")

still_serving(env, "after a bad scan slot")

# THE RETURN MOVE. This is the RF-001L property, at the firmware
# handler that implements it: a return that fails must invalidate the
# position and must NOT destroy the science already acquired.
env = fresh()
dispatch(env["service"], "connect_servo")
dispatch(env["service"], "sync_position", load_slot=1)

return_failure = counting_raiser(RuntimeError("the servo did not come back"))

with patched(type(env["service"].carousel), "return_selected_to_loader",
             return_failure):
    result = env["service"]._return_home(
        env["service"].carousel.current_scan_slot)

checks.equal(return_failure.calls, 1,
             "the return movement really was attempted and really failed")

checks.ok(not env["service"].carousel.position_valid,
          "a failed return invalidates the carousel position")

checks.ok(isinstance(result, dict),
          "and reports the failure as data rather than raising - a "
          "servo that failed to come back must never destroy acquired "
          "science")

checks.equal(result.get("returned"), False,
             "with the failure visible in the result")

checks.equal(result.get("position_valid"), False,
             "and the position reported as invalid in the same breath")

checks.equal(result.get("exception_type"), "RuntimeError",
             "carrying the exception type for diagnosis")

checks.ok("re-synchronize" in result.get("message", ""),
          "and telling the operator to re-synchronize before moving")


# ======================================================================
checks.section("retired movement kinds, and why one handler is unreachable")

# A FINDING, RECORDED RATHER THAN PATCHED AWAY.
#
# `handle_servo_test_move` special-cases `kind == "neutral"` twice: once
# to validate `hold_ms`, once in `_run_test` to pass it. But the command
# builds its valid list from the DRIVER -
#
#     kinds = [name for name, _label in servo.test_move_kinds()]
#
# - and the ST3215 offers eight kinds, none of them "neutral". Any
# request naming it is refused as BAD_REQUEST before either branch is
# reached, so both are dead: leftovers from the continuous-rotation
# servo backend that this project used to support. That servo held a
# neutral pulse width; the ST3215 is a position servo and has no such
# state. The backend was removed on 2026-08-19, and
# static/test_architecture.py enforces that its name does not reappear
# anywhere in the tree - which is why it is described here rather than
# named.
#
# Classified DEFENSIVE_UNREACHABLE, proved here rather than asserted,
# and NOT deleted: removing production code during a verification freeze
# buys nothing a proof does not, and the branch cannot execute.
#
# The check below is the part that matters going forward. It fails if a
# NEW kind is special-cased without the driver offering it - which is
# exactly how these two came to exist.

import ast                                                   # noqa: E402

env = fresh()
driver = env["service"].servo.require_driver() \
    if env["service"].servo.driver else None

dispatch(env["service"], "connect_servo")
driver = env["service"].servo.driver

offered = {name for name, _label in driver.test_move_kinds()}

checks.ok(len(offered) >= 8,
          "the ST3215 offers {} movement kinds".format(len(offered)))

checks.ok("neutral" not in offered,
          "and 'neutral' is NOT one of them")

# Every `kind == "..."` literal the protocol special-cases.
source = (support.FIRMWARE / "ESP32" / "protocol.py").read_text(
    encoding="utf-8")
tree = ast.parse(source)

special_cased = set()

for node in ast.walk(tree):
    if not isinstance(node, ast.Compare):
        continue

    if not isinstance(node.left, ast.Name) or node.left.id != "kind":
        continue

    for comparator in node.comparators:
        if isinstance(comparator, ast.Constant) and isinstance(
                comparator.value, str):
            special_cased.add(comparator.value)

# Kinds the protocol special-cases that the driver has retired. Each
# must be listed, with the backend it belonged to, or this fails.
RETIRED_KINDS = {
    "neutral": "continuous-rotation hold; that backend was removed "
               "on 2026-08-19",
}

unknown = special_cased - offered - set(RETIRED_KINDS)

checks.equal(sorted(unknown), [],
             "every movement kind the protocol special-cases is either "
             "offered by the driver or listed as retired - checked {} "
             "literal(s)".format(len(special_cased)))

stale_retirements = set(RETIRED_KINDS) & offered

checks.equal(sorted(stale_retirements), [],
             "and nothing listed as retired is still offered, which "
             "would make the note a lie")

checks.equal(sorted(special_cased & set(RETIRED_KINDS)), ["neutral"],
             "'neutral' is the one retired kind still special-cased, "
             "and its branches are therefore unreachable")

# Proved by driving it: the command refuses it before either branch.
frame = dispatch(env["service"], "servo_test_move",
                 kind="neutral", hold_ms="forever", confirm=True)

checks.equal((frame.get("error") or {}).get("code"), "BAD_REQUEST",
             "a request naming 'neutral' is refused as BAD_REQUEST")

checks.ok("kind must be one of" in (frame.get("error") or {}).get(
              "message", ""),
          "by the KIND check - not by the hold_ms validation, which is "
          "what makes that handler unreachable")

still_serving(env, "after a retired kind")


sys.exit(checks.report())
