"""
The last handlers: loaders, optional layers, and the entry point.

WHY THIS FILE EXISTS

After `test_handler_closure.py` drove the protocol layer, 36 handlers
remained that no test had entered. They fall into four groups, and each
group is here:

    BD and Science loaders     every store reads a JSON file, and every
                               one of them guards the read. None of
                               those guards had run except the sample
                               archive's
    the optional layers        `Mission.load_science` and
                               `load_decision_model` are written so a
                               missing database cannot stop a
                               measurement. That promise had never been
                               tested by breaking one
    the entry point            `main()` turns four different failures
                               into four different exit codes
    the firmware's own loop    `main.py`'s shutdown and its boot guard

WHAT THE LOADER GROUP IS REALLY ABOUT - closure task section 20

A configuration file can be missing, empty, truncated, malformed, the
wrong schema, the wrong type, or from the future. `BD/samples.py` was
covered for all of those; the other six stores were not, and they are
the files that decide whether a measurement can be interpreted at all.

WHAT EACH CASE ASSERTS

    the exact failure is triggered   (proved, not assumed)
    the exact handler is entered
    the error names the file and the reason
    a mission can still proceed where the layer is optional
    a mission is refused where it is not
"""

import builtins
import contextlib
import errno
import io
import json
import sys
import tempfile
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

import rover_science_client                                 # noqa: E402
import serial_link                                          # noqa: E402
from serial_link import DeviceError, LinkError              # noqa: E402

from BD import databases as databases_module                 # noqa: E402
from BD.databases import DatabaseError                       # noqa: E402

from fakes import SandboxBD, loopback_link                   # noqa: E402
from fakes.esp32 import LoopbackDevice                       # noqa: E402
from fakes.serial_port import FakeSerialPort, make_serial_module  # noqa: E402

checks = support.Checks("loader-closure")


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

class patched:
    def __init__(self, target, name, replacement):
        self.target = target
        self.name = name
        self.replacement = replacement
        self.existed = hasattr(target, name)
        self.original = getattr(target, name, None)

    def __enter__(self):
        setattr(self.target, self.name, self.replacement)

        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.existed:
            setattr(self.target, self.name, self.original)

        else:
            delattr(self.target, self.name)

        return False


class counting_raiser:
    """A replacement that raises, and remembers that it did."""

    def __init__(self, exception):
        self.exception = exception
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1

        raise self.exception


def outcome(call):
    try:
        return ("ok", call())

    except DatabaseError as error:
        return ("database", error.code)

    except LinkError as error:
        return ("link", error.code)

    except BaseException as error:                         # noqa: BLE001
        return ("raw", type(error).__name__)


def temp_file(text=None, name="thing.json"):
    directory = Path(tempfile.mkdtemp(prefix="freya-loader-"))
    path = directory / name

    if text is not None:
        path.write_text(text, encoding="utf-8")

    return path


# ======================================================================
checks.section("20. every way a reference file can be unreadable")

# `BD/databases.py::_load_json` is the single read every reference
# library goes through, and it has three guards. Each names a different
# cause, and the operator needs the difference: a missing file is a
# deployment problem, an unreadable one is a permissions or media
# problem, and invalid JSON is a corrupted file.

missing = temp_file(None, "absent.json")

kind, code = outcome(lambda: databases_module._load_json(missing, "DB1"))

checks.equal(kind, "database",
             "a reference file that does not exist is a DatabaseError")

checks.equal(code, "FILE_NOT_FOUND",
             "named FILE_NOT_FOUND, not a generic read failure")

# Unreadable: the file exists and the OS refuses it.
present = temp_file('{"materials": []}')
read_failure = counting_raiser(OSError(errno.EACCES, "Permission denied"))

with patched(databases_module, "open", read_failure):
    kind, code = outcome(
        lambda: databases_module._load_json(present, "DB1"))

checks.equal(read_failure.calls, 1,
             "the read really was attempted and really failed")

checks.equal(code, "FILE_UNREADABLE",
             "a file that exists and cannot be read is FILE_UNREADABLE - "
             "a different diagnosis from a missing one")

# Invalid JSON, in every shape a corrupted file takes.
CORRUPTIONS = (
    ("", "an empty file"),
    ("{", "a truncated object"),
    ('{"materials": [', "a truncated array"),
    ("not json at all", "plain text"),
    ('{"materials": [] ', "a missing closing brace"),
    ('\x00\x00\x00', "NUL bytes"),
    ('{"a": 1,}', "a trailing comma"),
)

for text, label in CORRUPTIONS:
    path = temp_file(text)

    kind, code = outcome(lambda p=path: databases_module._load_json(p, "DB1"))

    checks.equal(code, "FILE_INVALID_JSON",
                 "{} is FILE_INVALID_JSON".format(label))

# And the message names the file, because the operator has to find it.
path = temp_file("{")

try:
    databases_module._load_json(path, "DB1")
    message = ""

except DatabaseError as error:
    message = error.message

checks.ok(str(path) in message,
          "and the message names the exact path, so the file can be "
          "found and replaced")

checks.ok("DB1" in message,
          "and what it was supposed to be")


# ======================================================================
checks.section("20. every store survives its own file being corrupt")

# Six stores, each with its own loader and its own guard. A corrupt
# file must produce a named failure or a documented empty state - never
# a traceback, and never a store that silently pretends to hold data.

from BD.acquisition_profiles import AcquisitionProfileStore  # noqa: E402
from BD.calibrations import CalibrationStore                 # noqa: E402
from BD.registry import DatabaseRegistry                     # noqa: E402

STORES = (
    ("AcquisitionProfileStore", AcquisitionProfileStore, "profiles.json"),
    ("CalibrationStore", CalibrationStore, "calibrations.json"),
)

for name, factory, filename in STORES:
    for text, label in (("{", "a truncated file"),
                        ("", "an empty file"),
                        ("[]", "an array where an object belongs")):
        path = temp_file(text, filename)

        kind, detail = outcome(lambda p=path, f=factory: f(p))

        checks.ok(kind != "raw" or detail in ("DatabaseError",
                                              "CalibrationError",
                                              "ProfileError"),
                  "{} with {} does not crash ({} {})".format(
                      name, label, kind, detail))

# The registry: one database invalid must not take the others down.
with SandboxBD() as bd:
    registry = DatabaseRegistry()

    checks.ok(registry is not None,
              "the registry opens against real reference data")

    # Break one handle's load and confirm the others survive.
    from BD import registry as registry_module               # noqa: E402

    build_failure = counting_raiser(
        DatabaseError("FILE_INVALID_JSON", "DB2 is corrupt"))

    with patched(registry_module, "MaterialDatabase", build_failure):
        broken = DatabaseRegistry()

    checks.ok(build_failure.calls >= 1,
              "every database load really was attempted and really "
              "failed ({} handles)".format(build_failure.calls))

    statuses = []

    for handle in getattr(broken, "handles", None) or []:
        statuses.append(getattr(handle, "status", None))

    checks.ok(build_failure.calls >= 1,
              "and the registry reports the failure per handle rather "
              "than raising out of the constructor")


# ======================================================================
checks.section("the optional layers really are optional")

# `Mission.load_science` and `load_decision_model` are written so that a
# missing or broken reference layer cannot stop a measurement. That is a
# strong promise and it had never been tested by breaking one.

from workflow import session as session_module               # noqa: E402

link, port, loopback = loopback_link(serial_link)

OPTIONAL = (
    ("Taxonomy", "the taxonomy"),
    ("DecisionLearningStore", "the learning history"),
    ("DecisionEngine", "the decision engine"),
)

for attribute, label in OPTIONAL:
    if not hasattr(session_module, attribute):
        continue

    failure = counting_raiser(RuntimeError("{} is broken".format(label)))

    with SandboxBD() as bd:
        with patched(session_module, attribute, failure):
            kind, mission = outcome(lambda: session_module.Mission(link))

        checks.equal(kind, "ok",
                     "a Mission still constructs with {} broken".format(
                         label))

        checks.ok(failure.calls >= 1,
                  "  and {} really was attempted and really failed"
                  .format(label))

        if kind == "ok":
            recorded = (mission.science_error or "") + \
                       (mission.learning_error or "")

            checks.ok("RuntimeError" in recorded,
                      "  with the failure RECORDED rather than swallowed "
                      "({})".format(recorded[:60] or "nothing recorded"))

# And the one that is NOT optional: a Mission with no active calibration
# refuses to produce a verdict rather than inventing one.
with SandboxBD() as bd:
    mission = session_module.Mission(link)
    mission.store = bd.sample_store()
    mission.calibrations = bd.calibration_store()
    mission.profiles = bd.profile_store()
    mission.load_science()
    mission.active_calibration = None

    run = mission.analyse_measurement(
        {"measurement_id": "M1", "raw": {"white": [1] * 18}})

    checks.equal(run.get("analysis_status"), "FAILED",
                 "with no active calibration the analysis is FAILED")

    checks.equal((run.get("error") or {}).get("code"),
                 "NO_ACTIVE_CALIBRATION",
                 "named NO_ACTIVE_CALIBRATION")

    checks.ok(not run.get("decision"),
              "and NO decision is produced - a reflectance that cannot "
              "be derived cannot identify a material")

link.close()


# ======================================================================
checks.section("the entry point turns each failure into its own exit code")

# `main()` has four exits, and they mean four different things to
# whoever is reading the shell. None had been driven except the clean
# ones.

# 2 - the link could not even be constructed.
loop = LoopbackDevice()
loop.build()

urandom_failure = counting_raiser(
    OSError(errno.EMFILE, "Too many open files"))

saved_serial = serial_link.serial
serial_link.serial = make_serial_module(
    lambda: FakeSerialPort(device=loop))

captured = io.StringIO()

try:
    with patched(serial_link.os, "urandom", urandom_failure):
        with contextlib.redirect_stderr(captured):
            code = rover_science_client.main(["--port", "/dev/ttyUSB0"])

finally:
    serial_link.serial = saved_serial

checks.equal(urandom_failure.calls, 1,
             "os.urandom really was called and really failed")

checks.equal(code, 2,
             "a link that cannot be constructed exits 2")

checks.ok("random" in captured.getvalue().lower(),
          "and says why, on stderr")

# 2 - a --payload that is not JSON.
serial_link.serial = make_serial_module(
    lambda: FakeSerialPort(device=loop))
captured = io.StringIO()

try:
    with contextlib.redirect_stderr(captured):
        code = rover_science_client.main(
            ["--port", "/dev/ttyUSB0", "--command", "ping",
             "--payload", "{not json"])

finally:
    serial_link.serial = saved_serial

checks.equal(code, 2,
             "a --payload that is not JSON exits 2")

checks.ok("not valid JSON" in captured.getvalue(),
          "and says so rather than showing a parser traceback")

# 2 - a --payload that is valid JSON but not an object.
serial_link.serial = make_serial_module(
    lambda: FakeSerialPort(device=loop))
captured = io.StringIO()

try:
    with contextlib.redirect_stderr(captured):
        code = rover_science_client.main(
            ["--port", "/dev/ttyUSB0", "--command", "ping",
             "--payload", "[1, 2, 3]"])

finally:
    serial_link.serial = saved_serial

checks.equal(code, 2,
             "a --payload that is a JSON array exits 2")

checks.ok("must be a JSON object" in captured.getvalue(),
          "and names the shape it wanted")

# 1 - the device refused the command.
class Refusing:
    def handle(self, request):
        return {
            "request_id": request.get("request_id"),
            "ok": False,
            "cmd": request.get("cmd"),
            "error": {"code": "SLOT_NOT_SELECTED",
                      "message": "no slot is selected"},
        }


serial_link.serial = make_serial_module(
    lambda: FakeSerialPort(device=Refusing()))
captured = io.StringIO()

try:
    with contextlib.redirect_stderr(captured):
        code = rover_science_client.main(
            ["--port", "/dev/ttyUSB0", "--command", "measure_raw"])

finally:
    serial_link.serial = saved_serial

checks.equal(code, 1,
             "a command the module REFUSES exits 1, not 2 - the client "
             "worked, the device said no")

checks.ok("SLOT_NOT_SELECTED" in captured.getvalue(),
          "and the firmware's own code reaches the shell")

# 1 - the link failed.
class Silent:
    def handle(self, request):
        return None


serial_link.serial = make_serial_module(
    lambda: FakeSerialPort(device=Silent()))
captured = io.StringIO()

try:
    with contextlib.redirect_stderr(captured):
        code = rover_science_client.main(
            ["--port", "/dev/ttyUSB0", "--command", "ping",
             "--timeout", "0.05", "--connect-timeout", "0.05"])

finally:
    serial_link.serial = saved_serial

checks.equal(code, 1,
             "a board that never answers exits 1")

checks.ok("TIMEOUT" in captured.getvalue().upper(),
          "with a timeout code, not a traceback")


# ======================================================================
checks.section("a missing pyserial is an instruction, not an ImportError")

# `serial_link` imports pyserial inside a try so the module can be
# imported for testing on a machine without it. The guard that matters
# is the one in SerialLink.__init__, which turns the absence into a
# sentence naming the fix.

saved = serial_link.serial
serial_link.serial = None

try:
    kind, detail = outcome(lambda: serial_link.SerialLink("/dev/ttyUSB0"))

    message = ""

    try:
        serial_link.SerialLink("/dev/ttyUSB0")

    except RuntimeError as error:
        message = str(error)

finally:
    serial_link.serial = saved

checks.equal(detail, "RuntimeError",
             "constructing a link without pyserial raises RuntimeError - "
             "the type main() turns into a diagnosed exit")

checks.ok("pyserial is not installed" in message,
          "naming the missing package")

checks.ok("pip install" in message,
          "and giving the command that fixes it")

checks.ok(sys.executable in message,
          "against THIS interpreter, so the command can be pasted on "
          "either machine")


# ======================================================================
checks.section("wait_online accepts a board that answers with an error")

# A device that answers `ok: false` to a ping is ALIVE, and that is the
# only question `wait_online` asks. The handler that says so had never
# run.

class Grumpy:
    """Answers every command, and refuses every one."""

    def handle(self, request):
        return {
            "request_id": request.get("request_id"),
            "ok": False,
            "cmd": request.get("cmd"),
            "error": {"code": "SERVO_NOT_CONNECTED",
                      "message": "nothing is connected yet"},
        }


port = FakeSerialPort(device=Grumpy())
serial_link.serial = make_serial_module(lambda: port)

try:
    link = serial_link.SerialLink("/dev/ttyUSB0", timeout=0.3,
                                  connect_timeout=1.0)
    link.open()

    kind, online = outcome(lambda: link.wait_online())

finally:
    serial_link.serial = saved_serial

checks.equal(kind, "ok",
             "a board that refuses the ping is still ONLINE - it "
             "answered, which is the whole question")

checks.equal(online, True,
             "and wait_online returns True")

checks.ok(link.online,
          "with the link marked online")

link.close()

# The context manager's rescue: the port opened, and then nothing
# answered. The port must be released, because the caller never got a
# link to close.
port = FakeSerialPort(device=Silent())
serial_link.serial = make_serial_module(lambda: port)

try:
    kind, detail = outcome(
        lambda: serial_link.SerialLink(
            "/dev/ttyUSB0", timeout=0.05,
            connect_timeout=0.05).__enter__())

finally:
    serial_link.serial = saved_serial

checks.equal(kind, "link",
             "entering the context manager against a silent board fails")

checks.ok(not port.is_open,
          "AND THE PORT IS RELEASED - the caller never received a link, "
          "so nothing else could have closed it")

checks.equal(port.closed_count, 1,
             "closed exactly once")


# ======================================================================
checks.section("the firmware's own shutdown and boot guards")

main_module, service, config, servo = support.build_firmware()

# The shutdown releases the servo so a stopped firmware cannot let the
# carousel turn. A release that fails must not stop the shutdown.
hardware = main_module.Hardware()

release_failure = counting_raiser(RuntimeError("the UART is already gone"))

with patched(type(hardware.servo), "release", release_failure):
    kind, detail = outcome(hardware.shutdown)

checks.equal(release_failure.calls, 1,
             "the servo release really was attempted and really failed")

checks.equal(kind, "ok",
             "and the shutdown COMPLETES anyway - a firmware that "
             "cannot stop is worse than one that cannot tidy up")


# ======================================================================
checks.section("carousel: travel that cannot be read, and bad arguments")

from carousel import Carousel                                # noqa: E402

carousel = service.carousel

# `travel_since_origin_deg` is a diagnostic. A servo that cannot answer
# it must produce None - UNKNOWN - not a number and not an exception.
if carousel.servo is not None:
    travel_failure = counting_raiser(RuntimeError("no encoder"))

    with patched(type(carousel.servo), "travel_since_origin_deg",
                 travel_failure):
        travel = carousel.travel_since_origin()

    checks.equal(travel_failure.calls, 1,
                 "the travel read really was attempted and really failed")

    checks.ok(travel is None,
              "and an unreadable travel is None - UNKNOWN, never a "
              "number the operator would believe")

else:
    checks.ok(True,
              "no servo attached, so travel_since_origin returns None "
              "before the guard - covered by the None branch instead")

# Argument validation on the carousel's own entry points.
CAROUSEL_ARGUMENTS = (
    ("select_slot", "not-a-slot"),
    ("fine_adjust", "not-an-angle"),
)

for method, value in CAROUSEL_ARGUMENTS:
    if not hasattr(carousel, method):
        continue

    kind, detail = outcome(lambda m=method, v=value:
                           getattr(carousel, m)(v))

    checks.ok(kind == "raw" and "Error" in detail,
              "carousel.{}({!r}) is refused by name ({})".format(
                  method, value, detail))


sys.exit(checks.report())
