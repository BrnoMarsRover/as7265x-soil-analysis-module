"""
PC: the serial lifecycle, the error kinds, and the workflow's ordering.

The serial link is driven against a fake port that behaves the way the
real one was MEASURED to behave on this bench - it resets on an open
that asserts DTR/RTS, it echoes at the REPL, and it raises the two
Windows errors that distinguish a missing port from a busy one.

The two properties this suite exists for:

    THE PORT IS RELEASED ON EVERY PATH OUT. A normal quit, a failed
    ping, a timeout, a malformed frame and an exception all end with
    the handle closed - because relying on interpreter exit is what
    leaves COM4 held and turns the next run into a PORT_BUSY that has
    nothing to do with another program.

    A FAILURE IS NAMED, NOT GUESSED. "The module did not answer" has
    seven causes needing seven different actions, and telling an
    operator the port is busy when the port opened perfectly sends them
    to Task Manager for a fault that is in the firmware.

Run:  py test_pc.py
"""

import json
import sys

import support

support.add_project_root()
support.add_path("PC")

import serial_link                                   # noqa: E402
from serial_link import DeviceError, LinkError, SerialLink  # noqa: E402

checks = support.Checks("pc")


# ======================================================================
# a fake port that behaves like the measured one
# ======================================================================

class FakePort:
    """
    Stands in for serial.Serial, recording what was done to it.

    `script` is a list of byte strings to hand back, in order, so a
    test can present a boot banner, a REPL echo or a damaged frame and
    watch what the link makes of it.
    """

    def __init__(self, script=None, answer=True):
        self.port = None
        self.baudrate = None
        self.bytesize = None
        self.parity = None
        self.stopbits = None
        self.timeout = None
        self.exclusive = None

        self._dtr = True
        self._rts = True

        self.is_open = False
        self.closed_count = 0
        self.opened_count = 0

        self.written = []
        self.buffer_resets = 0
        self.script = list(script or [])
        self.answer = answer

        # Every DTR/RTS transition, so a test can prove the lines were
        # low BEFORE the port opened rather than merely afterwards.
        self.line_history = []

    # -- the attributes pySerial exposes ------------------------------

    @property
    def dtr(self):
        return self._dtr

    @dtr.setter
    def dtr(self, value):
        self._dtr = value
        self.line_history.append(("dtr", value, self.is_open))

    @property
    def rts(self):
        return self._rts

    @rts.setter
    def rts(self, value):
        self._rts = value
        self.line_history.append(("rts", value, self.is_open))

    def open(self):
        self.is_open = True
        self.opened_count += 1

    def close(self):
        self.is_open = False
        self.closed_count += 1

    def reset_input_buffer(self):
        self.script = []
        self.buffer_resets += 1

    def write(self, data):
        self.written.append(data)

        if self.answer:
            request = json.loads(data.decode("utf-8"))
            self.script.append(
                (json.dumps({
                    "request_id": request["request_id"],
                    "ok": True,
                    "cmd": request["cmd"],
                    "data": {"pong": True, "echoed": request["cmd"]},
                }) + "\n").encode("utf-8")
            )

        return len(data)

    def flush(self):
        pass

    def read(self, count=1):
        if not self.script:
            return b""

        return self.script.pop(0)


def link_with(port, **kwargs):
    """A SerialLink whose open() produces the given fake port."""
    link = SerialLink("COM_TEST", **kwargs)

    def fake_serial():
        return port

    original = serial_link.serial.Serial
    serial_link.serial.Serial = fake_serial

    try:
        link.open()

    finally:
        serial_link.serial.Serial = original

    return link


class FakeSerialModule:
    """Only the pieces serial_link touches."""

    EIGHTBITS = 8
    PARITY_NONE = "N"
    STOPBITS_ONE = 1

    class SerialException(Exception):
        pass

    Serial = FakePort


_real_serial = serial_link.serial
serial_link.serial = FakeSerialModule


# ======================================================================
checks.section("opening the port does not reset the board")

port = FakePort()
link = link_with(port)

checks.equal(port.port, "COM_TEST", "the port name is passed through")
checks.equal(port.baudrate, 115200, "at 115200 baud")
checks.ok(port.exclusive is True,
          "exclusive access is requested, so a second client gets a clean "
          "refusal from the OS instead of two programs interleaving bytes")

before_open = [entry for entry in port.line_history if entry[2] is False]

checks.ok(("dtr", False, False) in before_open,
          "DTR is driven low BEFORE the port is opened")
checks.ok(("rts", False, False) in before_open,
          "and so is RTS - after the open would be too late, the reset "
          "circuit has already fired")
checks.ok(port.dtr is False and port.rts is False,
          "and both stay low while the link is in use")

link.close()
checks.equal(port.closed_count, 1, "close() closes the port")

link.close()
checks.equal(port.closed_count, 1,
             "and closing twice is harmless, which is what makes it safe "
             "in a finally block")


# ======================================================================
checks.section("a hardware reset is deliberate and separate")

port = FakePort()
link = link_with(port)

link.hard_reset()

transitions = [entry for entry in port.line_history if entry[2] is True]
rts_pulse = [entry for entry in transitions if entry[0] == "rts"]

checks.ok(("rts", True, True) in transitions,
          "hard_reset asserts RTS")
checks.ok(rts_pulse and rts_pulse[-1] == ("rts", False, True),
          "and releases it again - a pulse, not a hold")
checks.ok(all(entry[1] is False for entry in transitions
              if entry[0] == "dtr"),
          "with DTR held low throughout, which is what boots the "
          "APPLICATION rather than the serial bootloader")

checks.equal(port.buffer_resets, 1,
             "and the receive buffer is cleared AFTER the pulse - "
             "everything already in it came from a board that has just "
             "been reset")

link.close()

# The regression this guards. Observed on hardware: `device.py reset`
# runs an mpremote command first, mpremote leaves a `>>>` prompt in the
# OS receive buffer, and the ping that follows the reset read that
# stale prompt and reported DEVICE_AT_REPL about a board that was at
# that moment booting perfectly.
stale = FakePort(script=[b">>> \r\n"])
link = link_with(stale)

link.hard_reset()

online = link.wait_online(timeout=2.0)

checks.ok(online,
          "a `>>>` left in the buffer by a previous session does NOT "
          "become a DEVICE_AT_REPL verdict about the board that was "
          "just reset")

link.close()


# ======================================================================
checks.section("failures are named")

def failing_open(exception_text):
    link = SerialLink("COM_TEST")

    def raiser():
        raise FakeSerialModule.SerialException(exception_text)

    original = FakeSerialModule.Serial
    FakeSerialModule.Serial = raiser

    try:
        link.open()

        return None

    except LinkError as error:
        return error

    finally:
        FakeSerialModule.Serial = original


error = failing_open(
    "could not open port 'COM4': FileNotFoundError(2, 'The system cannot "
    "find the file specified.', None, 2)")
checks.equal(error.code, "PORT_NOT_FOUND",
             "a missing port is PORT_NOT_FOUND")
checks.ok("not plugged in" in error.message or "does not exist"
          in error.message,
          "and the message says to check the cable")

error = failing_open(
    "could not open port 'COM4': PermissionError(13, 'Access is denied.', "
    "None, 5)")
checks.equal(error.code, "PORT_BUSY", "a held port is PORT_BUSY")
checks.ok("another program" in error.message,
          "and the message says which kind of program to close")

error = failing_open("something else entirely went wrong")
checks.equal(error.code, "PORT_OPEN_FAILED",
             "anything else is PORT_OPEN_FAILED, not guessed at")

checks.ok(error.code != "PORT_BUSY",
          "PORT_BUSY is never the fallback diagnosis")


# ======================================================================
checks.section("a port that opened is never blamed for the firmware")

port = FakePort(answer=False)
link = link_with(port, connect_timeout=0.3)

try:
    link.wait_online(timeout=0.3)
    failed = None

except LinkError as error:
    failed = error

checks.ok(failed is not None, "a silent board fails")
checks.equal(failed.code, "PROTOCOL_TIMEOUT",
             "as PROTOCOL_TIMEOUT - the port opened, so the port is not "
             "the problem")
checks.ok("opened" in failed.message,
          "and the message says so explicitly")

link.close()


# ======================================================================
checks.section("the REPL cannot fake a healthy link")

# The measured signature: MicroPython echoes the request and evaluates
# it, so the echo carries OUR request_id and no "ok".
echo = FakePort(answer=False, script=[
    b'{"request_id": "1", "cmd": "ping"}\r\n',
    b"{'request_id': '1', 'cmd': 'ping'}\r\n>>> ",
])
link = link_with(echo, connect_timeout=0.5)

try:
    link.wait_online(timeout=0.5)
    result = "ONLINE"

except LinkError as error:
    result = error.code

checks.equal(result, "DEVICE_AT_REPL",
             "an echoed request is not an answer - it carries our own "
             "request_id and would otherwise report a healthy link to a "
             "board that is not running the firmware")

link.close()


# ======================================================================
checks.section("request and response matching")

port = FakePort()
link = link_with(port)

data = link.request("ping")
checks.equal(data["echoed"], "ping", "the answer to our request comes back")

sent = json.loads(port.written[0].decode("utf-8"))
checks.ok("request_id" in sent, "every request carries an id")
checks.ok("timestamp" in sent, "and a timestamp")
checks.ok(port.written[0].endswith(b"\n"), "and ends with a newline")

first = json.loads(port.written[0].decode("utf-8"))["request_id"]
link.request("get_status")
second = json.loads(port.written[1].decode("utf-8"))["request_id"]

checks.ok(first != second, "request ids advance")

# An answer to a DIFFERENT request must not be accepted as ours.
stray = FakePort(answer=False, script=[
    b'{"request_id": "999", "ok": true, "data": {"stray": true}}\n',
])
link_stray = link_with(stray, timeout=0.3)

try:
    link_stray.request("ping", timeout=0.3)
    outcome = "ACCEPTED"

except LinkError as error:
    outcome = error.code

checks.equal(outcome, "PROTOCOL_TIMEOUT",
             "a frame answering someone else's request is ignored")

link_stray.close()

# A device error is the firmware speaking, not a transport failure.
refuse = FakePort(answer=False, script=[
    b'{"request_id": "1", "ok": false, "cmd": "select_slot", '
    b'"error": {"code": "SERVO_NOT_CONNECTED", "message": "no servo"}}\n',
])
link_refuse = link_with(refuse, timeout=0.5)

try:
    link_refuse.request("select_slot", timeout=0.5)
    outcome = None

except DeviceError as error:
    outcome = error

checks.ok(outcome is not None, "an ok:false answer raises")
checks.equal(outcome.code, "SERVO_NOT_CONNECTED",
             "carrying the firmware's own code")
checks.ok(isinstance(outcome, LinkError),
          "and it is a LinkError too, so one except clause can catch "
          "everything the link can go wrong with")

link_refuse.close()
link.close()


# ======================================================================
checks.section("the buffer is not cleared before every request")

source = open(serial_link.__file__, encoding="utf-8").read()

calls = [
    line for line in source.splitlines()
    if "reset_input_buffer" in line and not line.strip().startswith("#")
]

checks.equal(len(calls), 1,
             "the receive buffer is cleared in exactly ONE place - "
             "clearing it as a matter of routine would throw away the "
             "boot traceback that explains the previous failure")

# And that place is hard_reset, where it is not routine at all:
# everything buffered was produced by a board that no longer exists.
reset_body = source[source.index("def hard_reset"):
                    source.index("def wait_online")]

checks.ok("reset_input_buffer" in reset_body,
          "and that place is hard_reset - after a reset the buffer holds "
          "the previous session's output by definition")

request_body = source[source.index("    def request(self"):
                      source.index("    def _read_response")]

request_calls = [
    line for line in request_body.splitlines()
    if "reset_input_buffer" in line and not line.strip().startswith("#")
]

checks.equal(request_calls, [],
             "NOT in request(), where request ids already separate the "
             "answers")
checks.ok("NO reset_input_buffer here" in request_body,
          "and a comment there says why, at the one place it is tempting")


# ======================================================================
checks.section("the client releases the port on every path out")

import rover_science_client as client   # noqa: E402

ports = []


def tracked_serial():
    port = FakePort()
    ports.append(port)

    return port


FakeSerialModule.Serial = tracked_serial

code = client.main(["--port", "COM_TEST", "--command", "ping"])
checks.equal(code, 0, "a one-shot command succeeds")
checks.equal(ports[-1].closed_count, 1, "and closes the port")

FakeSerialModule.Serial = lambda: FakePort(answer=False)
code = client.main(["--port", "COM_TEST", "--command", "ping"])
checks.ok(code != 0, "a silent board makes the command fail")

ports.clear()
FakeSerialModule.Serial = tracked_serial


def exploding_interactive(link):
    raise RuntimeError("something in the workflow blew up")


original_interactive = client.interactive
client.interactive = exploding_interactive

try:
    client.main(["--port", "COM_TEST"])
    raised = False

except RuntimeError:
    raised = True

finally:
    client.interactive = original_interactive

checks.ok(raised, "an unhandled workflow exception still propagates")
checks.equal(ports[-1].closed_count, 1,
             "and the port is STILL closed - the finally runs whatever "
             "happened above it")

ports.clear()


def interrupting_interactive(link):
    raise KeyboardInterrupt


client.interactive = interrupting_interactive

try:
    code = client.main(["--port", "COM_TEST"])

finally:
    client.interactive = original_interactive

checks.equal(code, 0, "Ctrl+C is a normal exit")
checks.equal(ports[-1].closed_count, 1, "and it closes the port too")


# ======================================================================
checks.section("the workflow is four slots and one science layer")

from workflow import measure, screen, session   # noqa: E402

checks.equal(measure.SLOT_COUNT, 4, "the measurement screens know 4 slots")
checks.equal(screen.SLOT_COUNT, 4, "and so does the main screen")
checks.equal(session.SLOT_COUNT, 4, "and the session")

measure_source = open(measure.__file__, encoding="utf-8").read()

save_at = measure_source.index("add_measurement")
analyse_at = measure_source.index("analyse_measurement")

checks.ok(save_at < analyse_at,
          "RAW IS PERSISTED BEFORE SCIENCE IS CALLED - the whole point of "
          "the ordering, checked in the source because it cannot be "
          "checked any other way without hardware")

checks.ok("ACQUISITION_FAILED" in measure_source,
          "a failed acquisition is recorded as a failure")
checks.ok("full of zeros" in measure_source,
          "and the code says why it is not recorded as a spectrum of zeros")


# ======================================================================
checks.section("the screens actually speak the store's API")

# THE CHECK THAT WAS MISSING.
#
# BD/samples.py was rewritten for the three-layer record model and the
# screens were carried over from the monolith, still calling the flat
# one. `store.save`, `store.ready`, `store.archive_path` and
# `store.migrated_from` had all been deleted, and `active_samples()`
# had changed what it RETURNS - none of which shows up until a screen
# runs. The first thing the operator client did on connecting was
# crash with AttributeError.
#
# Grepping the source for the names is not enough on its own, but it
# is cheap and it catches the whole class.

import ast   # noqa: E402

from BD.acquisition_profiles import AcquisitionProfileStore  # noqa: E402
from BD.calibrations import CalibrationStore                 # noqa: E402
from BD.decision_learning import DecisionLearningStore       # noqa: E402
from BD.registry import DatabaseRegistry                     # noqa: E402
from BD.samples import SampleStore                           # noqa: E402

WORKFLOW = support.PC_DIR / "workflow"


def attributes_reached_for(name):
    """Every `<...>.name.ATTR` the workflow reaches for."""
    found = {}

    for path in sorted(WORKFLOW.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue

            base = node.value
            hit = (
                (isinstance(base, ast.Attribute) and base.attr == name)
                or (isinstance(base, ast.Name) and base.id == name)
            )

            if hit:
                found.setdefault(node.attr, set()).add(
                    "%s:%d" % (path.name, node.lineno))

    return found


for name, instance in (
    ("store", SampleStore()),
    ("calibrations", CalibrationStore()),
    ("profiles", AcquisitionProfileStore()),
    ("registry", DatabaseRegistry()),
):
    used = attributes_reached_for(name)
    missing = sorted(a for a in used if not hasattr(instance, a))

    checks.equal(
        missing, [],
        "every attribute the screens use on `{}` exists ({} checked)"
        .format(name, len(used)))


# ======================================================================
checks.section("the main screen renders")

import tempfile              # noqa: E402
from pathlib import Path     # noqa: E402

from workflow.session import Mission   # noqa: E402

STATUS = {
    "firmware": "freya-science-module",
    "version": "6.0.0",
    "protocol_version": 2,
    "commands": ["ping", "get_status"],
    "sensor": {"state": "NOT_INITIALIZED", "ready": False},
    "servo": {"connected": False, "label": "NOT CONNECTED"},
    "carousel": {
        "slot_count": 4,
        "position_valid": True,
        "selected_slot": 1,
        "current_load_slot": 1,
        "current_scan_slot": 3,
        "carousel_phase": "LOAD",
    },
    "slots": [
        {"slot_id": n, "occupied": False, "sample_id": None}
        for n in range(1, 5)
    ],
}


class FakeLink:
    online = True
    port = "COM_TEST"

    def get_status(self):
        return dict(STATUS)


mission = Mission(FakeLink())

handle = tempfile.NamedTemporaryFile(suffix=".json", delete=False,
                                     mode="w", encoding="utf-8")
handle.write(json.dumps({"schema_version": 4, "samples": []}))
handle.close()

mission.store = SampleStore(Path(handle.name)).load()

status = mission.hardware_status()
checks.equal(mission.firmware_version, "6.0.0",
             "the session records which firmware it is talking to")

view = mission.slot_view(status)

checks.equal(len(view), 4, "the slot view has one row per slot")
checks.equal([row["slot_id"] for row in view], [1, 2, 3, 4],
             "numbered 1 to 4")
checks.ok(all(row["sample_id"] is None for row in view),
          "and an empty archive gives four empty slots")

mission.store.create("S001", 2)
view = mission.slot_view(status)
row = mission.entry_for(view, 2)

checks.equal(row["sample_id"], "S001", "a created Sample appears in its slot")
checks.equal(row["state"], "READY_TO_LOAD", "with its lifecycle state")
checks.equal(row["measurement_count"], 0, "and no measurements yet")

mission.store.set_state("S001", "LOADED")
mission.store.add_measurement(
    "S001", raw={"white": {"A": 1.0}},
    acquisition={"firmware_version": "6.0.0"})

row = mission.entry_for(mission.slot_view(status), 2)
checks.equal(row["state"], "MEASURED", "measuring moves it to MEASURED")
checks.equal(row["measurement_count"], 1, "and the measurement is counted")

missing = mission.entry_for(mission.slot_view(status), 4)
checks.equal(missing["sample_id"], None, "an empty slot reports no sample")
checks.equal(missing["measurement_count"], 0, "and no measurements")

# The screen itself. It prints; what matters is that it does not raise.
from workflow import display, screen   # noqa: E402

import io                              # noqa: E402
import contextlib                      # noqa: E402

for name, call in (
    ("print_main_screen",
     lambda: screen.print_main_screen(mission, status,
                                      mission.slot_view(status))),
    ("print_startup_screen",
     lambda: screen.print_startup_screen("NOT CONNECTED")),
    ("print_system_status",
     lambda: display.print_system_status(mission)),
):
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            call()

        raised = None

    except Exception as error:
        raised = "{}: {}".format(type(error).__name__, error)

    checks.ok(raised is None,
              "{} renders without raising{}".format(
                  name, "" if raised is None else " (" + raised + ")"))


# ======================================================================
checks.section("the record screens render a three-layer record")

from BD.channels import CHANNELS as ALL_CHANNELS   # noqa: E402
from workflow import records                       # noqa: E402

mission.store.add_analysis_run("S001", "M001", {
    "analysis_status": "OK",
    "decision_status": "OK",
    "versions": {"science": "5.0.0", "decision_model": "V001",
                 "databases": {"DB1": "measured18-v1"}},
    "quality": {"hardware": {"status": "PASS"},
                "normalization": {"status": "OK"}},
    "representations": {
        "normalized": {"white": {c: 0.5 for c in ALL_CHANNELS}},
    },
    "database_results": [
        {"database": "DB1", "status": "READY",
         "families": {"angular": {"winner": "Pink Clay"}}},
    ],
    "decision": {"level": "AMBIGUOUS_SET", "status": "AMBIGUOUS",
                 "material": None, "confidence": "LOW",
                 "candidates": [], "reason": "two candidates"},
})

record = mission.store.get_sample("S001")

for name, call in (
    ("print_sample_table",
     lambda: records.print_sample_table(mission.store)),
    ("print_full_sample", lambda: records.print_full_sample(record)),
):
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            call()

        raised = None

    except Exception as error:
        raised = "{}: {}".format(type(error).__name__, error)

    checks.ok(raised is None,
              "{} renders without raising{}".format(
                  name, "" if raised is None else " (" + raised + ")"))

# The RAW table must actually print the numbers it was given, not a
# column of dashes - which is what a shape mismatch looks like, and is
# indistinguishable on screen from a measurement of nothing.
buffer = io.StringIO()

with contextlib.redirect_stdout(buffer):
    display.print_triad_table({"white": {c: 123.5 for c in ALL_CHANNELS}})

rendered = buffer.getvalue()

checks.ok("123.5" in rendered,
          "print_triad_table prints the RAW values it was handed")
checks.ok("WHITE raw" in rendered, "under a named illumination column")
checks.ok("One illumination" in rendered,
          "and says plainly that an 18-channel record is a complete "
          "legacy measurement, not a 54-feature one with parts missing")

buffer = io.StringIO()

with contextlib.redirect_stdout(buffer):
    display.print_triad_table(
        {"white": {c: 100.0 for c in ALL_CHANNELS},
         "uv": {c: 40.0 for c in ALL_CHANNELS},
         "ir": {c: 60.0 for c in ALL_CHANNELS}},
        {"white": {c: 0.25 for c in ALL_CHANNELS},
         "uv": {c: 0.25 for c in ALL_CHANNELS},
         "ir": {c: 0.25 for c in ALL_CHANNELS}},
    )

rendered = buffer.getvalue()

for column in ("WHITE raw", "UV raw", "IR raw"):
    checks.ok(column in rendered,
              "a three-illumination measurement shows {}".format(column))

checks.ok("0.25" in rendered,
          "with reflectance beside RAW when a run produced it")

buffer = io.StringIO()

with contextlib.redirect_stdout(buffer):
    display.print_triad_table(None)

checks.ok("no spectrum" in buffer.getvalue(),
          "and an absent RAW block says so instead of printing an empty "
          "table")

Path(handle.name).unlink(missing_ok=True)


serial_link.serial = _real_serial

sys.exit(checks.report())
