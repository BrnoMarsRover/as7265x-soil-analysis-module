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

import ast
import json
import sys
import tempfile
import time
from pathlib import Path

# The shared scaffolding lives in firmware/Tests/. Walking up to it by
# name means this suite runs from any working directory and does not
# care how deep under Tests/software/ it sits.
_TESTS_DIR = next(
    p for p in Path(__file__).resolve().parents if p.name == "Tests"
)

if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))


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

    @property
    def in_waiting(self):
        """
        Bytes ready to be read, as pySerial reports them.

        Modelled because the link asks for in_waiting before every
        read: a fake port without it would let a read(512) that blocks
        for the whole port timeout on real hardware pass the suite.
        """
        return sum(len(chunk) for chunk in self.script)

    def read(self, count=1):
        """
        One scripted chunk, or nothing after waiting like a real port.

        pySerial's read() BLOCKS for up to `timeout` when no bytes
        arrive. Returning b"" instantly instead turned every test that
        waits out a timeout into a busy spin: the link's read loop is
        `while monotonic() < deadline`, so a 10-second negative test
        burned a core for ten seconds and its result depended on how
        loaded the machine was. It made this suite fail in run_all and
        pass on its own, which is the least useful kind of failure
        there is.

        Sleeping for the port timeout models the real thing and costs
        no CPU.
        """
        if not self.script:
            time.sleep(self.timeout if self.timeout else 0.01)

            return b""

        return self.script.pop(0)


def link_with(port, **kwargs):
    """
    A SerialLink whose open() produces the given fake port.

    The session nonce and the counter are both PINNED, so the ids are
    "T-1", "T-2", ... and a fixture can script an answer to a known id.

    Production draws the nonce from os.urandom and starts the counter
    at zero - see `_next_request_id` in serial_link.py, and
    contracts/test_request_identity.py, which is the suite that made
    the nonce necessary. Pinning it here is what lets these fixtures
    stay readable; the randomness itself is tested there.
    """
    link = SerialLink("COM_TEST", **kwargs)
    link.session = "T"
    link._request_id = 0

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
checks.section("the same failures, named on Linux")

# The operator's main computer runs Linux, and pySerial spells every one
# of these differently there: it builds the message from the underlying
# OSError instead of from a Windows exception repr. Until these strings
# were recognized, a missing /dev/ttyUSB0 and an account outside the
# dialout group both arrived as PORT_OPEN_FAILED - a code that names no
# action at all.

error = failing_open(
    "[Errno 2] could not open port /dev/ttyUSB0: [Errno 2] No such file "
    "or directory: '/dev/ttyUSB0'")
checks.equal(error.code, "PORT_NOT_FOUND",
             "a missing /dev/ttyUSB0 is PORT_NOT_FOUND")

error = failing_open(
    "Could not exclusively lock port /dev/ttyUSB0: [Errno 11] Resource "
    "temporarily unavailable")
checks.equal(error.code, "PORT_BUSY",
             "a port another process has locked is PORT_BUSY")

error = failing_open(
    "[Errno 16] could not open port /dev/ttyUSB0: [Errno 16] Device or "
    "resource busy: '/dev/ttyUSB0'")
checks.equal(error.code, "PORT_BUSY", "and so is EBUSY")

error = failing_open(
    "[Errno 13] could not open port /dev/ttyUSB0: [Errno 13] Permission "
    "denied: '/dev/ttyUSB0'")
checks.equal(error.code, "PORT_DENIED",
             "a POSIX permission denial is PORT_DENIED, not PORT_BUSY - "
             "no other program is holding anything")
checks.ok("dialout" in error.message,
          "and the message names the group the account has to join")

error = failing_open(
    "could not open port 'COM4': PermissionError(13, 'Access is denied.', "
    "None, 5)")
checks.equal(error.code, "PORT_BUSY",
             "a WINDOWS denial still means another program holds the port")


# ======================================================================
checks.section("the workflow only calls commands the link has")

# The check that was missing, and the reason it was missing.
#
# `mission.link.sync_load_slot(1)` sat in two branches of the carousel
# screen - option [7] of CAROUSEL SETUP and the whole of RE-SYNC. Both
# are reachable only after a real ST3215 has answered, so no suite here
# and no bench session without hardware ever executed the line. It
# failed with an AttributeError on the operator's first real carousel
# setup, after everything it depends on had already worked.
#
# Reading the call sites out of the source costs nothing and does not
# need the hardware the branch needs.

def link_attributes_used():
    """Every `<something>.link.NAME` the PC layer names, with its file."""
    used = set()

    for source_file in sorted(support.PC_DIR.rglob("*.py")):
        if "__pycache__" in source_file.parts:
            continue

        tree = ast.parse(source_file.read_text(encoding="utf-8"),
                         filename=str(source_file))

        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Attribute)
                    and node.value.attr == "link"):
                used.add((
                    "{}:{}".format(
                        source_file.relative_to(support.PC_DIR).as_posix(),
                        node.lineno,
                    ),
                    node.attr,
                ))

    return used


# Class members plus the attributes __init__ binds: `link.online` is a
# real part of the surface and lives only on the instance.
link_surface = set(dir(SerialLink)) | set(vars(SerialLink("COM_TEST")))
used = link_attributes_used()

missing = sorted(
    "{} calls link.{}".format(where, name)
    for where, name in used
    if name not in link_surface
)

checks.equal(missing, [],
             "every link.<name> the PC layer uses exists on SerialLink")

checks.ok(len(used) > 30,
          "and the check actually walked the workflow ({} call sites "
          "seen)".format(len(used)))


# ======================================================================
checks.section("every command the link sends has a handler")

# The other half of the same seam. serial_link.py names the command
# strings; ESP32/protocol.py owns the table of names it will answer to.
# A typo on either side is a runtime UNKNOWN_COMMAND on the bench, which
# is a slow way to find a misspelling.

link_tree = ast.parse(
    (support.PC_DIR / "serial_link.py").read_text(encoding="utf-8"))

sent = set()

for node in ast.walk(link_tree):
    if (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "request"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)):
        sent.add(node.args[0].value)

protocol_tree = ast.parse(
    (support.ESP32_DIR / "protocol.py").read_text(encoding="utf-8"))

handled = set()

for node in ast.walk(protocol_tree):
    if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "COMMANDS"
            for t in node.targets):
        for key in node.value.keys:
            if isinstance(key, ast.Constant):
                handled.add(key.value)

checks.ok(len(handled) > 20,
          "the ESP32 command table was found and read ({} commands)".format(
              len(handled)))

checks.equal(sorted(sent - handled), [],
             "every command serial_link.py sends is in the ESP32 table")

checks.ok(len(sent) > 20,
          "and the link was read the same way ({} commands sent)".format(
              len(sent)))


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
    b'{"request_id": "T-1", "ok": false, "cmd": "select_slot", '
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


# A throwaway path even though these instances are never loaded or
# written. `SampleStore()` with no argument means the operator's live
# archive, and a rule that is absolute is a rule nobody has to re-check
# every time a line is added below it - see
# data_integrity/test_protected_data.py.
_SURFACE_DIR = Path(tempfile.mkdtemp(prefix="freya-surface-"))

for name, instance in (
    ("store", SampleStore(_SURFACE_DIR / "samples.json")),
    ("calibrations", CalibrationStore(directory=_SURFACE_DIR)),
    ("profiles", AcquisitionProfileStore(_SURFACE_DIR / "profiles.json")),
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

# noqa placeholder - Path is imported at the top of the file

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
    # The whole status dict, which is what the screen takes: it has to
    # tell a servo that is not connected from one that is connected
    # with the carousel position lost, and a bare label cannot say
    # which of those is true.
    ("print_startup_screen",
     lambda: screen.print_startup_screen(status)),
    ("print_startup_screen (nothing connected)",
     lambda: screen.print_startup_screen(None)),
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


# ======================================================================
checks.section("a bench measurement is saved only where the operator says")
#
# The sensor test used to end by offering the learning history and
# nothing else, so a bench measurement worth keeping as a Sample had to
# be taken again through the mission workflow. Three outcomes now:
# learning history, Sample archive, or nothing at all.

import builtins                                   # noqa: E402


class ScriptedInput:
    """Feeds a prompt sequence to workflow.prompts, which owns input()."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = []

    def __call__(self, prompt=""):
        self.asked.append(prompt)

        if not self.answers:
            raise EOFError("the script ran out of answers")

        return self.answers.pop(0)


def with_answers(answers, call):
    original = builtins.input
    script = ScriptedInput(answers)
    builtins.input = script

    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            value = call()

    finally:
        builtins.input = original

    return value, out.getvalue(), script


def bench_acquisition():
    """What the ESP32 returns from sensor_test_raw, in miniature."""
    def spectrum(offset):
        return {channel: 100.0 + offset + index
                for index, channel in enumerate(ALL_CHANNELS)}

    return {
        "illuminations": {
            name: {
                "illumination": name,
                "repeats": 2,
                "acquisitions": [spectrum(n), spectrum(n + 0.5)],
                "data_ready_wait_ms": [595, 596],
            }
            for n, name in enumerate(("white", "uv", "ir"))
        },
        "sensor_settings": {"gain": 2, "integration_cycles": 100},
        "bulbs_off": True,
    }


bench_handle = tempfile.NamedTemporaryFile(suffix=".json", delete=False,
                                           mode="w", encoding="utf-8")
bench_handle.write(json.dumps({"schema_version": 4, "samples": []}))
bench_handle.close()

bench = Mission(FakeLink())
bench.store = SampleStore(Path(bench_handle.name)).load()
bench.learning = None

acquisition = bench_acquisition()

# ---- [3] EXIT: nothing is written anywhere ---------------------------
result, output, script = with_answers(
    ["3"],
    lambda: records.offer_measurement_disposition(bench, acquisition, {}),
)

checks.equal(result, (None, False), "exit reports that nothing was saved")
checks.equal(len(bench.store.active_samples()), 0,
             "and no Sample was created")
checks.ok("NOTHING SAVED" in output, "and it says so plainly")

# ---- [1] with no learning database: refused, not crashed -------------
result, output, script = with_answers(
    ["1", "3"],
    lambda: records.offer_measurement_disposition(bench, acquisition, {}),
)

checks.equal(result, (None, False),
             "asking for the learning history without one saves nothing")
checks.ok("UNAVAILABLE" in output,
          "and the menu says the learning database is unavailable")

# ---- [2] save as a Sample --------------------------------------------
# Answers: menu 2, sample id, every metadata field blank, then exit.
metadata_blanks = [""] * (len(records.METADATA_FIELDS) + 2)

result, output, script = with_answers(
    ["2", "BENCH1"] + metadata_blanks + ["3"],
    lambda: records.offer_measurement_disposition(bench, acquisition, {}),
)

saved_sample, saved_learning = result

checks.equal(saved_sample, "BENCH1", "the Sample is reported as saved")
checks.ok(not saved_learning, "and the learning history was not touched")

stored = bench.store.get_sample("BENCH1")

checks.ok(stored is not None, "the Sample exists in the archive")
checks.equal(stored["slot_id"], None,
             "a bench measurement belongs to no carousel slot")

stored_measurements = records.measurements_of(stored)

checks.equal(len(stored_measurements), 1, "with exactly one measurement")

stored_raw = stored_measurements[0].get("raw") or {}

checks.equal(sorted(stored_raw), ["ir", "uv", "white"],
             "and its RAW is still grouped by illumination")
checks.equal(len(stored_raw["white"]), 18,
             "with all 18 channels under each lamp")

# RAW SURVIVES A FAILED ANALYSIS. No calibration is active in this
# fixture, so Science cannot produce reflectance - and that must cost
# the analysis, never the measurement.
checks.ok("RAW saved:      YES" in output,
          "RAW is reported as saved even though the analysis could not run")

records_source = open(records.__file__, encoding="utf-8").read()
bench_save_at = records_source.index("def save_acquisition_as_sample")
bench_add_at = records_source.index("add_measurement", bench_save_at)
bench_analyse_at = records_source.index("analyse_measurement", bench_save_at)

checks.ok(bench_add_at < bench_analyse_at,
          "and the bench save persists RAW BEFORE it calls Science, the "
          "same ordering the mission workflow uses")

# ---- saving twice does not write twice -------------------------------
#
# THIS CASE DID NOT TEST WHAT IT SAID, and the A.3 EOF fix is what
# exposed it. The script was ["2", "3"]: "2" chose Save, and "3" was
# then consumed as the SAMPLE ID, so the record was named "3", the six
# optional metadata prompts were answered by a script that had run out,
# and the screen left through the `not selection` branch. It saved
# ONCE, never revisited the menu, and asserted nothing at all - three
# ways of not being the test its comment described.
#
# It only passed because `ask` turned end-of-input into a blank answer.
# Once EOF became OperatorGone - because a closed stdin was spinning
# the real menus forever - this stopped working, which is the correct
# outcome: the test was relying on the defect.
#
# Rewritten to do what it always claimed. The sample is named, the
# metadata prompts are answered deliberately, Save is chosen a SECOND
# time, and the screen is left on purpose.
result, output, script = with_answers(
    ["2", "S-DOUBLE",                       # save, and name it
     "", "", "", "", "", "",                # six optional metadata fields
     "2",                                   # save again - the actual test
     "3"],                                  # and leave deliberately
    lambda: records.offer_measurement_disposition(
        bench, acquisition, {}, measurement_id="M_TEST"),
)

checks.equal(result[0], "S-DOUBLE",
             "the acquisition is saved as a Sample under the name given")

checks.ok("ALREADY SAVED" in output,
          "and choosing Save a second time says ALREADY SAVED rather "
          "than saving again")

saved_ids = [entry.get("sample_id") for entry in bench.store.summaries()]

checks.equal(saved_ids.count("S-DOUBLE"), 1,
             "and the archive holds exactly ONE record for it - the "
             "second Save wrote nothing")

checks.equal(script.answers, [],
             "and the script was consumed exactly, so the screen asked "
             "the questions this case thinks it asked")

Path(bench_handle.name).unlink(missing_ok=True)


# ======================================================================
checks.section("the sensor test reads the keys an AnalysisRun really has")
#
# THREE BUGS, ONE CAUSE. The sensor-test screen and the learning store
# were written against a previous analysis shape and asked an
# AnalysisRun for a "measurement" key it does not have. Each one only
# fired when a sensor test got as far as a SUCCESSFUL analysis on real
# hardware, which is why they survived:
#
#   calibration.menu_full_sensor_test  KeyError: 'measurement'
#   display.print_evidence_summary     AttributeError: 'dict' object
#                                      has no attribute 'summarize'
#   session.record_observation         refused every observation with
#                                      "carries no raw spectra"

from BD.decision_learning import DecisionLearningStore   # noqa: E402


def analysis_run_fixture():
    """The shape Science.pipeline.analyze actually returns."""
    raw = {
        name: {channel: 10.0 + index
               for index, channel in enumerate(ALL_CHANNELS)}
        for name in ("white", "uv", "ir")
    }

    return {
        "analysis_run_id": None,
        "measurement_id": "SENSOR_TEST",
        "sample_id": None,
        "analysis_status": "OK",
        "decision_status": "OK",
        "calibration": {"calibration_id": "CAL_A",
                        "legacy_calibration_id": "CAL_LEGACY"},
        "versions": {"decision_model": "FREYA_DECISION_V001"},
        "representations": {"normalized": {}},
        "database_results": [
            {"database": "DB1", "database_id": "DB1",
             "version": "measured18-v1", "status": "READY",
             "candidate_count": 23, "channels_compared": 17,
             "normalization": "legacy:CAL_LEGACY",
             "metrics": {
                 "cosine": {"winner": "Red Clay", "winner_score": 0.72,
                            "absolute_margin": 0.004},
                 "rmse": {"winner": "Bentonite", "winner_score": 0.25,
                          "absolute_margin": 0.009},
                 "pearson_r": {"winner": "Kaolin", "winner_score": 0.33,
                               "absolute_margin": 0.005},
             }},
            {"database": "DB2", "status": "EMPTY",
             "reason": "DB2 cannot be derived from DB1."},
        ],
        "quality": {"hardware": {"status": "WARNING"},
                    "normalization": {"status": "OK"}},
        "evidence": {
            "raw": raw,
            "quality": {"hardware": {"status": "WARNING"},
                        "normalization": {"status": "OK"}},
            "channel_reliability": {
                "features_total": 54,
                "raw_valid_total": 54,
                "normalized_valid_total": 27,
                "by_illumination": {
                    name: {"raw_valid_channels": 18,
                           "normalized_valid_channels": 9}
                    for name in ("white", "uv", "ir")
                },
            },
            "acquisition": {
                "sensor_settings": {"gain_x": "16x",
                                    "integration_cycles": 100},
                "acquisition_profile_id": "PROFILE_TEST",
            },
        },
        "decision": {"level": "UNKNOWN", "material": None,
                     "decision_model_version": "FREYA_DECISION_V001"},
    }


run_fixture = analysis_run_fixture()

checks.ok("measurement" not in run_fixture,
          "an AnalysisRun has no top-level 'measurement' key - the whole "
          "cause of this section")

# HARDWARE QC IS 'WARNING' ON PURPOSE. print_evidence_summary only
# reaches the quality.summarize() call when the QC is imperfect, which
# is why a local named `quality` could shadow the Science.quality module
# for so long without anybody noticing.
for name, call in (
    ("print_evidence_summary",
     lambda: display.print_evidence_summary(run_fixture["evidence"])),
    ("print_database_results",
     lambda: display.print_database_results(
         run_fixture["database_results"])),
    ("print_decision",
     lambda: display.print_decision(run_fixture["decision"])),
):
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            call()

        raised = None

    except Exception as error:
        raised = "{}: {}".format(type(error).__name__, error)

    checks.ok(raised is None,
              "{} renders a real AnalysisRun{}".format(
                  name, "" if raised is None else " (" + raised + ")"))

# The screen must not ask for keys the run does not carry.
calibration_source = open(
    support.PC_DIR / "workflow" / "calibration.py", encoding="utf-8"
).read()
sensor_test_source = calibration_source[
    calibration_source.index("def menu_full_sensor_test"):
    calibration_source.index("def menu_led_test")
]

def code_only(source):
    """
    The source with comments and docstrings removed.

    The comments in that function NAME the dead keys, deliberately, so
    the next reader knows what the screen used to ask for and why it
    broke. A scan that does not strip them finds the explanation and
    reports it as the bug.
    """
    lines = []

    for line in source.splitlines():
        stripped = line.strip()

        if stripped.startswith("#"):
            continue

        lines.append(line.split("  # ")[0])

    text = chr(10).join(lines)

    # Docstrings too - the repaired code documents the old shape.
    while '"""' in text:
        start = text.index('"""')
        end = text.find('"""', start + 3)

        if end == -1:
            break

        text = text[:start] + text[end + 3:]

    return text


sensor_test_code = code_only(sensor_test_source)

for dead_key in ('result["measurement"]', 'result["reference_matches"]',
                 'result["analysis"]', '"cross_database"',
                 '"metric_agreement"',
                 '"legacy_database_calibration_id"'):
    checks.ok(dead_key not in sensor_test_code,
              "the sensor test no longer reads {}".format(dead_key))

# ---- the learning store accepts a real result -------------------------
learning_file = Path(tempfile.mkdtemp()) / "learning.sqlite3"

bench.learning = DecisionLearningStore(learning_file)

record = bench.record_observation(
    "M_FIXTURE", run_fixture, label_type="UNKNOWN_SAMPLE",
    verification_status="UNKNOWN",
)

checks.ok(record is not None,
          "record_observation stores an observation from a real "
          "AnalysisRun - it used to refuse every one of them")

stored_status = bench.learning.status()

checks.equal(stored_status["observations"], 1,
             "and the learning history holds exactly that one")

learning_source = open(
    support.PC_DIR / "workflow" / "session.py", encoding="utf-8"
).read()
observation_source = learning_source[
    learning_source.index("def record_observation"):
    learning_source.index("def calibration_health")
]

checks.ok('measurement.get("raw")' not in code_only(observation_source),
          "and it takes RAW from the evidence package, not from a "
          "'measurement' key that does not exist")

# The disposition menu offers the learning history once a store is open.
result, output, script = with_answers(
    ["3"],
    lambda: records.offer_measurement_disposition(
        bench, acquisition, run_fixture),
)

checks.ok("UNAVAILABLE" not in output,
          "with a learning database open, saving to it is offered rather "
          "than reported unavailable")


# ======================================================================
checks.section("the carousel origin is actually declared")

# Not a static check this time: the two screens are RUN, with the
# answers an operator would type, against a link that serves the real
# response shape. `sync_load_slot` passed every static reading of this
# file and every suite in run_all.py; it did not survive being called.

from workflow import carousel as carousel_screen           # noqa: E402

SYNCED_CAROUSEL = {
    "slot_count": 4,
    "position_valid": True,
    "selected_slot": 1,
    "current_load_slot": 1,
    "current_scan_slot": 3,
    "carousel_phase": "LOAD",
    "reference": {
        "origin": {
            "servo": "ST3215",
            "feedback": True,
            "origin_counts": 2048,
            "origin_deg": 180.0,
        },
        "origin_scan_slot": 3,
        "alignment_offset_deg": 0.0,
        "drift_deg": 0.0,
        "drift_measurable": True,
    },
}


class SyncingLink:
    """A link with a connected servo that answers sync_position."""

    online = True
    port = "COM_TEST"

    def __init__(self):
        self.synced = []

    def get_status(self):
        status = dict(STATUS)
        status["servo"] = {
            # `connected` only. `selected` is not a key any firmware
            # sends, and a fake that invents one lets a reader go on
            # passing against it - which is exactly what happened.
            "connected": True,
            "label": "Waveshare ST3215",
            "backend": {
                "connected": True, "id": 1, "position_counts": 2048,
                "position_deg": 180.0, "mode_name": "STEP",
                "voltage_v": 11.9, "temperature_c": 34,
            },
        }
        status["carousel"] = dict(SYNCED_CAROUSEL)

        return status

    def sync_position(self, load_slot=None, scan_slot=None):
        self.synced.append((load_slot, scan_slot))

        return {"synchronized": True, "carousel": dict(SYNCED_CAROUSEL)}


sync_link = SyncingLink()
sync_mission = Mission(sync_link)

# CAROUSEL SETUP, option [7]: "set current position as Slot 1 / LOAD".
result, output, script = with_answers(
    ["7"],
    lambda: carousel_screen.menu_initial_calibration(sync_mission),
)

checks.equal(result, True,
             "CAROUSEL SETUP [7] completes instead of raising - this is "
             "the branch that failed with AttributeError on the first "
             "real carousel setup")
checks.equal(sync_link.synced, [(1, None)],
             "and it declares Slot 1 as the LOADING slot, by name")
checks.ok("Carousel setup complete" in output,
          "and says the setup is done")
checks.ok("2048" in output,
          "and reports the encoder origin the servo captured")

# RE-SYNC, the same command from the other screen.
sync_link.synced = []

result, output, script = with_answers(
    ["y"],
    lambda: carousel_screen.menu_resync(sync_mission),
)

checks.equal(sync_link.synced, [(1, None)],
             "RE-SYNC declares the same origin the same way")
checks.ok("Synchronized" in output,
          "and confirms it to the operator")


serial_link.serial = _real_serial

sys.exit(checks.report())
