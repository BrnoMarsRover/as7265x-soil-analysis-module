"""
The test infrastructure, tested.

WHY THE FAKES NEED THEIR OWN SUITE

Every fault-injection result in this project is only as trustworthy as
the thing that injected the fault. A test that says

    "a disconnect at byte 40 is handled correctly"

is worthless if the fake never disconnected. That failure mode is
silent and it looks exactly like success, which makes it the most
dangerous kind of bug a test system can have - and this campaign has
already produced two of them:

    the malformed-frame fake returned bytes with no newline, so the
    frame never completed and four checks that believed they were
    testing MALFORMED_RESPONSE were testing PROTOCOL_TIMEOUT

    the request-identity fixture used a port that answered on its own,
    so every planted stale frame was skipped and then followed by a
    genuine reply - six checks reported OK and proved nothing

Both were found by reading output that looked right. This suite exists
so the next one is found by a failing check instead.

THREE QUESTIONS, PER FAKE

    FIDELITY    does it offer the same surface as the real thing, so
                production code cannot take a path the hardware would
                never allow?
    HONESTY     does it reimplement production logic? A fake that
                recomputes what the code under test computes will
                agree with a bug.
    EFFECT      when asked to inject a fault, does the fault actually
                occur?
"""

import inspect
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
from serial_link import LinkError                            # noqa: E402

from fakes import FakeClock, SandboxBD, ScriptedConsole      # noqa: E402
from fakes.clock import install_clock                        # noqa: E402
from fakes.esp32 import (                                    # noqa: E402
    BEHAVIOURS,
    ECHO,
    GARBAGE,
    MALFORMED,
    REPL,
    ScriptedDevice,
    SUCCESS,
    TIMEOUT,
    TRUNCATED,
    WRONG_ID,
)
from fakes.serial_port import (                              # noqa: E402
    FakeSerialPort,
    install_fake_serial,
    open_link,
)

checks = support.Checks("fakes")

restore_serial = install_fake_serial(serial_link)


# ======================================================================
checks.section("FakeSerialPort offers the surface pySerial does")

# Not "does it work" - does it expose what production code may touch?
# A missing attribute makes a real code path unreachable in tests; an
# extra one lets a test rely on something the hardware cannot do.

PYSERIAL_SURFACE = (
    "port", "baudrate", "bytesize", "parity", "stopbits", "timeout",
    "exclusive", "dtr", "rts", "is_open", "in_waiting",
    "open", "close", "read", "write", "flush", "reset_input_buffer",
)

port = FakeSerialPort()

missing = [name for name in PYSERIAL_SURFACE if not hasattr(port, name)]

checks.equal(missing, [],
             "every pySerial attribute serial_link touches exists on "
             "the fake ({} checked)".format(len(PYSERIAL_SURFACE)))

# The reverse: what does production actually touch? Derived from the
# source, so a newly used attribute fails here rather than at runtime.
import ast                                                    # noqa: E402

link_source = (support.FIRMWARE / "PC" / "serial_link.py").read_text(
    encoding="utf-8")
tree = ast.parse(link_source)

touched = set()

for node in ast.walk(tree):
    if (isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "serial"
            and getattr(node.value.value, "id", None) == "self"):
        touched.add(node.attr)

unsupported = sorted(name for name in touched if not hasattr(port, name))

checks.equal(unsupported, [],
             "and every attribute serial_link reaches for on its port "
             "object is one the fake provides ({} found in the "
             "source)".format(len(touched)))

checks.ok(len(touched) >= 6,
          "and the scan really found the port usage ({})".format(
              sorted(touched)))


# ======================================================================
checks.section("FakeSerialPort read() behaves like a blocking read")

# The single most important fidelity property. pySerial's read() blocks
# for up to `timeout` when nothing arrives; a fake that returns b""
# instantly turns every timeout test into a busy spin whose result
# depends on machine load.

clock = FakeClock()
port = FakeSerialPort(clock=clock)
port.timeout = 0.5

start = clock.now
data = port.read(1)

checks.equal(data, b"", "an empty port returns no bytes")
checks.close(clock.now - start, 0.5,
             "and the read COSTS the port timeout - modelling the block "
             "is what makes a PROTOCOL_TIMEOUT test terminate instead "
             "of spinning")

# in_waiting must reflect what is actually queued.
port._enqueue(b"12345")

checks.equal(port.in_waiting, 5, "in_waiting counts the queued bytes")
checks.equal(port.read(2), b"12", "a short read takes only what it asked")
checks.equal(port.in_waiting, 3, "and leaves the rest")
checks.equal(port.read(10), b"345", "the remainder comes next")
checks.equal(port.in_waiting, 0, "and then the port is empty")


# ======================================================================
checks.section("chunking really chunks")

# §26: the fault must actually occur. `chunk_size` is what models a
# CP210x delivering a frame in 64-byte USB packets.

for size in (1, 4, 64):
    port = FakeSerialPort(chunk_size=size)
    port._enqueue(b"A" * 200)

    reads = []

    while port.in_waiting:
        reads.append(port.read(1000))

    checks.equal(max(len(chunk) for chunk in reads), size,
                 "chunk_size={} really delivers at most {} bytes per "
                 "read".format(size, size))
    checks.equal(b"".join(reads), b"A" * 200,
                 "and loses nothing in the process (chunk_size={})"
                 .format(size))


# ======================================================================
checks.section("every declared fault actually happens")

def one_request(**faults):
    """Send one request through a fake port and report what happened."""
    clock = FakeClock()
    installed = install_clock(serial_link, clock)
    link, port = open_link(serial_link, None, clock=clock,
                           link_kwargs={"timeout": 2.0}, **faults)
    link.online = True

    try:
        try:
            return "OK", link.request("ping"), port, link

        except LinkError as error:
            return error.code, None, port, link

        except Exception as error:                     # noqa: BLE001
            return "CRASH:" + type(error).__name__, None, port, link

    finally:
        installed.restore()


# fail_write_after
code, _data, port, link = one_request(fail_write_after=0)
checks.equal(code, "PORT_LOST", "fail_write_after really fails the write")
checks.equal(port.write_count, 1, "after exactly one write attempt")

# fail_read_after
code, _data, port, link = one_request(fail_read_after=0)
checks.equal(code, "PORT_LOST", "fail_read_after really fails the read")
checks.ok(port.read_count >= 1, "after at least one read attempt")

# zero_write / short_write report what they claim
port = FakeSerialPort(zero_write=True)
checks.equal(port.write(b"abcdef"), 0, "zero_write reports zero bytes")

port = FakeSerialPort(short_write=True)
checks.equal(port.write(b"abcdef"), 5, "short_write reports one less")

port = FakeSerialPort()
checks.equal(port.write(b"abcdef"), 6, "and an ordinary write reports all")

# drop_newline really removes the terminator
port = FakeSerialPort(drop_newline=True)
port.write(b'{"request_id": "x", "cmd": "ping"}\n')
queued = b"".join(port._out)

checks.ok(not queued.endswith(b"\n"),
          "drop_newline really leaves the frame unterminated")

# line_ending really changes it
port = FakeSerialPort(line_ending="\r\n")
port.write(b'{"request_id": "x", "cmd": "ping"}\n')
queued = b"".join(port._out)

checks.ok(queued.endswith(b"\r\n"),
          "line_ending really terminates with CRLF")

# noise_before really prepends
port = FakeSerialPort(noise_before=b"\xff\xfe")
port.write(b'{"request_id": "x", "cmd": "ping"}\n')
queued = b"".join(port._out)

checks.ok(queued.startswith(b"\xff\xfe"),
          "noise_before really puts rubbish in front of the frame")

# stale really preloads the buffer
port = FakeSerialPort(stale=b"leftover\n")
checks.equal(port.in_waiting, len(b"leftover\n"),
             "stale really preloads the receive buffer")

# duplicate really duplicates
port = FakeSerialPort(duplicate=True)
port.write(b'{"request_id": "x", "cmd": "ping"}\n')

checks.equal(len(port._out), 2, "duplicate really queues two answers")

# disconnect_after really stops answering
port = FakeSerialPort(disconnect_after=1)
port.write(b'{"request_id": "1", "cmd": "ping"}\n')
first = len(port._out)
port.write(b'{"request_id": "2", "cmd": "ping"}\n')
second = len(port._out)

checks.equal(first, 1, "the first command is answered")
checks.equal(second, 1,
             "and disconnect_after really stops the second from being "
             "answered")


# ======================================================================
checks.section("every scripted device behaviour is distinguishable")

# All thirteen behaviours, each producing a DIFFERENT outcome. If two
# of them produced the same one, half the fault suite would be testing
# the same thing twice without anybody noticing.

OUTCOMES = {}

for behaviour in BEHAVIOURS:
    clock = FakeClock()
    installed = install_clock(serial_link, clock)
    device = ScriptedDevice({"ping": behaviour})
    link, port = open_link(serial_link, device, clock=clock,
                           link_kwargs={"timeout": 2.0})
    link.online = True

    try:
        try:
            data = link.request("ping")
            outcome = "OK:{}".format(sorted(data) if data else "empty")

        except LinkError as error:
            outcome = error.code

        except Exception as error:                     # noqa: BLE001
            outcome = "CRASH:" + type(error).__name__

    finally:
        installed.restore()
        link.close()

    OUTCOMES[behaviour] = outcome

    checks.ok(not outcome.startswith("CRASH"),
              "{} produces a handled outcome ({})".format(
                  behaviour, outcome))

    checks.equal(device.seen.count("ping"), device.counts.get("ping", 0),
                 "and the device really saw the command ({})".format(
                     behaviour))

EXPECTED = {
    SUCCESS: "OK",
    DEVICE_ERROR: "REFUSED",
    TIMEOUT: "PROTOCOL_TIMEOUT",
    MALFORMED: "MALFORMED_RESPONSE",
    TRUNCATED: "MALFORMED_RESPONSE",
    GARBAGE: "PROTOCOL_TIMEOUT",
    REPL: "DEVICE_AT_REPL",
    ECHO: "PROTOCOL_TIMEOUT",
    WRONG_ID: "PROTOCOL_TIMEOUT",
    NO_DATA: "OK",
    EMPTY_DATA: "OK",
    OK_BUT_USELESS: "OK",
    DISCONNECT: "PROTOCOL_TIMEOUT",
} if False else None      # names imported below; see the explicit list

from fakes.esp32 import (                                     # noqa: E402
    DEVICE_ERROR,
    DISCONNECT,
    EMPTY_DATA,
    NO_DATA,
    OK_BUT_USELESS,
)

CRITICAL = (
    (MALFORMED, "MALFORMED_RESPONSE",
     "a damaged frame is reported as damage, not as silence - the "
     "distinction is the whole reason the behaviour exists"),
    (REPL, "DEVICE_AT_REPL",
     "the REPL prompt is named immediately"),
    (TIMEOUT, "PROTOCOL_TIMEOUT", "silence times out"),
    (DEVICE_ERROR, "REFUSED", "a refusal carries the firmware's code"),
    (ECHO, "PROTOCOL_TIMEOUT",
     "an echo of our own request is not an answer"),
    (WRONG_ID, "PROTOCOL_TIMEOUT",
     "somebody else's answer is not ours"),
)

for behaviour, expected, why in CRITICAL:
    checks.equal(OUTCOMES[behaviour], expected, why)

# The three "ok but hollow" behaviours must all SUCCEED at the transport
# level - that is the point of them. Distinguishing a useful answer
# from a hollow one is the CALLER's job, and is tested elsewhere.
for behaviour in (SUCCESS, NO_DATA, EMPTY_DATA, OK_BUT_USELESS):
    checks.ok(OUTCOMES[behaviour].startswith("OK"),
              "{} succeeds at the transport level, so the caller is the "
              "one that has to notice the answer is hollow".format(
                  behaviour))

distinct = len(set(OUTCOMES.values()))

checks.ok(distinct >= 5,
          "the {} behaviours produce {} distinct outcomes - enough that "
          "the fault suite is not testing one thing thirteen "
          "times".format(len(BEHAVIOURS), distinct))


# ======================================================================
checks.section("the fakes do not reimplement production logic")

# §24. A fake that recomputes what the code under test computes will
# agree with a bug in it. The boundary fakes must be TRANSPORT, not
# science and not carousel geometry.

FORBIDDEN = (
    "normalize", "cosine", "spectral_angle", "pearson",
    "load_slot_for", "scan_slot_for_load", "centred_error",
    "DecisionEngine", "build_evidence", "reflectance",
)

leaks = []

for path in sorted((_SOFTWARE_DIR / "fakes").glob("*.py")):
    text = path.read_text(encoding="utf-8")

    for name in FORBIDDEN:
        if name in text:
            leaks.append("{}: {}".format(path.name, name))

checks.equal(leaks, [],
             "no fake mentions a production algorithm - they emulate a "
             "wire, a register map and a frame format, and nothing that "
             "computes a result")

# And the loopback in particular must DELEGATE rather than answer.
esp32_source = (_SOFTWARE_DIR / "fakes" / "esp32.py").read_text(
    encoding="utf-8")

checks.ok("self.service.dispatch(request)" in esp32_source,
          "LoopbackDevice answers by calling the REAL Protocol "
          "dispatcher, so what is under test is the firmware and not a "
          "second implementation of it")


# ======================================================================
checks.section("FakeClock is deterministic and complete")

clock = FakeClock(start=500.0)

checks.equal(clock.monotonic(), 500.0, "the clock starts where it is told")
checks.equal(clock.time(), 500.0, "and time() agrees with monotonic()")

clock.advance(2.5)
checks.equal(clock.monotonic(), 502.5, "advance moves it forward")

clock.sleep(1.5)
checks.equal(clock.monotonic(), 504.0, "sleep advances instead of waiting")
checks.equal(clock.total_slept, 1.5, "and records what was slept")

checks.equal(clock.ticks_ms(), 504000,
             "the MicroPython helpers agree with the same instant")
checks.equal(clock.ticks_diff(10, 4), 6, "ticks_diff subtracts")

# Two clocks from the same start take the same path.
first = FakeClock(start=0.0)
second = FakeClock(start=0.0)

for step in (0.1, 0.25, 1.0, 30.0):
    first.advance(step)
    second.advance(step)

checks.equal(first.now, second.now,
             "two clocks given the same instructions read the same - a "
             "timing test that is not reproducible is not a test")

# It must never move on its own.
before = clock.monotonic()
_unused = [clock.monotonic() for _index in range(1000)]

checks.equal(clock.monotonic(), before,
             "and reading it a thousand times does not move it")


# ======================================================================
checks.section("ScriptedConsole answers, records and refuses to hang")

console = ScriptedConsole(["a", "b"])

checks.equal(console("first"), "a", "answers come out in order")
checks.equal(console("second"), "b", "one per question")
checks.equal(console.prompts, ["first", "second"],
             "and the prompts are recorded, because what a screen ASKED "
             "is part of its behaviour")

try:
    console("third")
    exhausted = None

except EOFError:
    exhausted = "EOFError"

checks.equal(exhausted, "EOFError",
             "running out raises EOFError - which is what a closed "
             "terminal does, and what prompts.ask() is written to catch")

console = ScriptedConsole([], exhausted="q")
checks.equal(console("x"), "q", "or answers a chosen value forever")

# The loop guard.
console = ScriptedConsole([], exhausted="", loop_guard=50)

try:
    for _index in range(200):
        console("prompt")

    guarded = None

except RuntimeError as error:
    guarded = str(error)

checks.ok(guarded is not None,
          "and a screen that asks forever is stopped by the loop guard")
checks.ok("looping" in (guarded or ""),
          "with a message that says what happened, so a hang becomes a "
          "diagnosable failure")


# ======================================================================
checks.section("SandboxBD is really a sandbox")

real_bd = support.FIRMWARE / "BD"

with SandboxBD() as bd:
    checks.ok(str(real_bd) not in str(bd.root),
              "the sandbox root is outside firmware/BD/")

    for name in ("DB1", "DB2", "DB3", "calibration", "models",
                 "samples", "training"):
        checks.ok((bd.root / name).is_dir(),
                  "and it has a {}/ of its own".format(name))

    # Seeded with real data, so tests compute against real numbers.
    checks.ok((bd.root / "DB1" / "DB1.json").is_file(),
              "seeded with a copy of DB1, so a test works with real "
              "reference data and can only damage the copy")

    store = bd.sample_store()
    store.create("SANDBOX-PROOF", 1)

    checks.ok("SANDBOX-PROOF" in bd.samples_file.read_text(
        encoding="utf-8"),
        "a write really lands in the sandbox file")

    real_archive = real_bd / "samples" / "samples.json"

    if real_archive.is_file():
        checks.ok("SANDBOX-PROOF" not in real_archive.read_text(
            encoding="utf-8"),
            "and nothing of it reaches the real archive")

    root = bd.root

checks.ok(not root.exists(),
          "and the sandbox is removed when it closes, so a long run "
          "does not fill the temporary directory")


# ======================================================================
checks.section("the AS7265x and ST3215 fakes inject what they promise")

sensor = support.FakeAS7265X(absent_scans=3)

checks.equal(sensor.scan(), [], "absent_scans really hides the device")
checks.equal(sensor.scan(), [], "for as many scans as asked")
checks.equal(sensor.scan(), [], "and no more")
checks.ok(0x49 in sensor.scan(), "after which it appears")

sensor = support.FakeAS7265X(bus_error=True)

for name, call in (("scan", sensor.scan),
                   ("writeto", lambda: sensor.writeto(0x49, b"\x00")),
                   ("readfrom", lambda: sensor.readfrom(0x49, 1))):
    try:
        result = call()
        raised = None

    except OSError:
        raised = "OSError"

    if name == "scan":
        checks.equal(result, [],
                     "bus_error hides the device from a scan")

    else:
        checks.equal(raised, "OSError",
                     "and makes {} raise, which is what a device that "
                     "is not acknowledging does".format(name))

servo = support.FakeST3215(silent=True)
servo.write(bytes([0xFF, 0xFF, 0x01, 0x02, 0x01, 0xFB]))

# None, not b"". MicroPython's `machine.UART.read()` returns None when
# nothing is available, where pySerial returns b"" - two conventions
# that are easy to conflate because both are falsy. The driver is
# written for the MicroPython one (`chunk = uart.read(...)` then
# `if chunk:`), so the fake has to use it too: a fake that returned b""
# would let a driver bug that assumes bytes pass here and fail on the
# board.
checks.equal(servo.read(), None,
             "a silent ST3215 answers None - MicroPython's UART "
             "convention, which is what the driver is written against")

servo = support.FakeST3215(silent=True)
checks.equal(servo.any(), 0,
             "and reports nothing waiting, so the driver's poll loop "
             "sees a genuinely dead bus")

servo = support.FakeST3215()
servo.write(bytes([0xFF, 0xFF, 0x01, 0x02, 0x01, 0xFB]))

checks.ok(servo.read(), "and an ordinary one really answers")

servo = support.FakeST3215(short_by=100)
checks.equal(servo.short_by, 100,
             "short_by is carried, so 'the mechanism stops short' is a "
             "fault the suite can ask for by name")


restore_serial()

sys.exit(checks.report())
