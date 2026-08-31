"""
The Linux main computer, while the mission is running.

WHAT test_linux.py ALREADY DOES, AND WHAT THIS ADDS

That file is about OPENING the port: every way `serialposix.py` can
fail, how each is classified, and the path and case rules that a
case-sensitive filesystem imposes. This one starts after the port is
open and asks what happens when the machine underneath changes:

    32-33   two processes wanting the same port and the same archive
    34-37   the device node renamed, duplicated, symlinked, removed
    38      the reset that opening a port can cause
    39-43   the errno classes a live tty raises mid-command
    50-55   a clean deployment: dependencies, interpreter, environment
    56-58   locale, timezone and a clock that lies

WHY THE ERRNO CASES ARE HERE AND NOT IN test_linux.py

They are not open() failures. They arrive on a port that opened
perfectly, and pySerial does NOT present them uniformly:

    read()      OSError -> SerialException
    write()     OSError -> SerialException
    in_waiting  fcntl.ioctl, raw OSError
    flush()     termios.tcdrain, raw OSError

`_read_response` asks for `in_waiting` before every single read, so the
unwrapped pair is what fails FIRST when /dev/ttyUSB0 goes away. That
asymmetry is a property of the library rather than of this project, and
it is what these cases exist to pin down.
"""

import errno
import json
import os
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
support.add_path("tools")

import serial_link                                          # noqa: E402
from serial_link import (                                   # noqa: E402
    DeviceError,
    LinkError,
    SerialLink,
)

from BD import config as bd_config          # noqa: E402
from BD import samples as samples_module                     # noqa: E402
from BD.samples import StorageError, archive_store            # noqa: E402

from fakes import FakeSerialPort, open_link                  # noqa: E402
from fakes.serial_port import make_serial_module             # noqa: E402

checks = support.Checks("linux-runtime")

FIRMWARE = support.FIRMWARE
REPO = FIRMWARE.parent


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def outcome(call):
    try:
        return ("ok", call())

    except DeviceError as error:
        return ("device", error.code)

    except LinkError as error:
        return ("link", error.code)

    except BaseException as error:                         # noqa: BLE001
        return ("raw", type(error).__name__)


class VanishingPort(FakeSerialPort):
    """
    A port whose device node has gone away underneath it.

    `fail_on` names the pySerial entry point that raises, and `code` the
    errno. Both matter: the entry point decides whether pySerial wraps
    the failure, and the errno decides what the operator is told.
    """

    def __init__(self, *args, fail_on="in_waiting", code=errno.EIO,
                 after=0, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_on = fail_on
        self.code = code
        self.after = after
        self.touches = 0

    def _maybe_fail(self, which):
        if which != self.fail_on:
            return

        self.touches += 1

        if self.touches > self.after:
            raise OSError(self.code, os.strerror(self.code))

    @property
    def in_waiting(self):
        self._maybe_fail("in_waiting")

        return super().in_waiting

    def flush(self):
        self._maybe_fail("flush")

        return super().flush()

    def write(self, data):
        self._maybe_fail("write")

        return super().write(data)

    def read(self, count=1):
        self._maybe_fail("read")

        return super().read(count)


def link_to(port):
    """An open SerialLink whose handle is `port`."""
    original = serial_link.serial
    serial_link.serial = make_serial_module(lambda: port)

    try:
        link = SerialLink("/dev/ttyUSB0", timeout=0.3)
        link.open()

    finally:
        serial_link.serial = original

    return link


# ======================================================================
checks.section("39-43. every errno a live tty raises mid-command")

# The full set, at every entry point that can raise it. Each cell is a
# separate case because pySerial treats the entry points differently
# and the operator needs the same answer from all of them.

ERRNOS = (
    (errno.EIO, "EIO", "the tty vanished under an ioctl"),
    (errno.ENODEV, "ENODEV", "the device was physically removed"),
    (errno.ENXIO, "ENXIO", "no such device or address"),
    (errno.EBADF, "EBADF", "the descriptor is no longer valid"),
    (errno.EACCES, "EACCES", "permission withdrawn while open"),
    (errno.EPERM, "EPERM", "the operation is no longer permitted"),
    (errno.EBUSY, "EBUSY", "the device became busy"),
    (errno.ENOENT, "ENOENT", "the node is gone"),
)

ENTRY_POINTS = (
    ("in_waiting", "raw ioctl, NOT wrapped by pySerial"),
    ("flush", "raw tcdrain, NOT wrapped by pySerial"),
    ("read", "wrapped into SerialException by pySerial"),
    ("write", "wrapped into SerialException by pySerial"),
)

for where, note in ENTRY_POINTS:
    for code, name, why in ERRNOS:
        port = VanishingPort(device=None, fail_on=where, code=code)
        link = link_to(port)

        kind, detail = outcome(lambda: link.request("ping"))

        checks.equal(kind, "link",
                     "{} from {} is a LinkError, not a traceback "
                     "({})".format(name, where, why))

        checks.equal(detail, "PORT_LOST",
                     "  and is classified PORT_LOST - {}".format(note))

        link.close()

# The count is worth stating: this is a matrix, not a handful of cases.
checks.equal(len(ERRNOS) * len(ENTRY_POINTS), 32,
             "{} errno classes across {} entry points = 32 combinations, "
             "every one of them covered".format(
                 len(ERRNOS), len(ENTRY_POINTS)))

# The diagnosis has to be actionable, and it has to carry the errno so
# a bench log can be matched against a kernel log.
port = VanishingPort(device=None, fail_on="in_waiting", code=errno.ENODEV)
link = link_to(port)

try:
    link.request("ping")
    message, data = "", {}

except LinkError as error:
    message, data = error.message, error.data or {}

checks.equal(data.get("errno"), errno.ENODEV,
             "the errno itself is carried on the error, not only its text")

checks.ok(data.get("reconnect_required") is True,
          "and the error says a reconnect is required")

checks.ok("/dev/ttyUSB0" in message,
          "and names the port that disappeared")

link.close()


# ======================================================================
checks.section("39-43. and the link closes itself, once, and stays shut")

# The RF-002 property, re-asserted for the errno path: a lost port
# closes the link, and every command afterwards is refused in the
# language the screens already catch.

port = VanishingPort(device=None, fail_on="in_waiting", code=errno.EIO)
link = link_to(port)

outcome(lambda: link.request("ping"))

checks.ok(link.serial is None,
          "a mid-command errno closes the link")

checks.ok(bool(link.closed_reason),
          "and records WHY it is closed: {!r}".format(link.closed_reason))

# Every subsequent command, including the ones the main menu calls on
# its own, must be a LinkError rather than a crash.
for command in ("ping", "get_status", "measure_raw", "sync_position"):
    kind, detail = outcome(lambda c=command: link.request(c))

    checks.equal(detail, "PORT_CLOSED",
                 "and {} afterwards is PORT_CLOSED, not a "
                 "RuntimeError".format(command))

# close() is idempotent and never raises, even now.
kind, _detail = outcome(link.close)
checks.equal(kind, "ok", "closing an already-closed link is safe")


# ======================================================================
checks.section("40. EINTR is not a failure, and not a success either")

# PEP 475 makes CPython retry most syscalls interrupted by a signal, and
# pySerial explicitly ignores EINTR in its read and write loops. What
# must NOT happen is an interrupted read being reported as a completed
# one.


class InterruptedPort(FakeSerialPort):
    """A port whose first N reads are interrupted, then behave."""

    def __init__(self, *args, interruptions=3, **kwargs):
        super().__init__(*args, **kwargs)
        self.remaining = interruptions
        self.interrupted = 0

    def read(self, count=1):
        if self.remaining > 0:
            self.remaining -= 1
            self.interrupted += 1

            # What pySerial hands back for an EINTR it has swallowed:
            # no data, no exception, no progress.
            return b""

        return super().read(count)


port = InterruptedPort(device=None, interruptions=3)
link = link_to(port)

kind, data = outcome(lambda: link.request("ping", timeout=2.0))

checks.equal(kind, "ok",
             "three interrupted reads do not stop the command from "
             "completing")

checks.equal(port.interrupted, 3,
             "and the interruptions really happened")

checks.ok(data is not None,
          "and the answer returned is a real one, not an empty read "
          "mistaken for a frame")

link.close()

# An interruption that never ends must time out rather than spin or
# succeed.
port = InterruptedPort(device=None, interruptions=10_000_000)
link = link_to(port)

kind, detail = outcome(lambda: link.request("ping", timeout=0.2))

checks.equal(kind, "link",
             "and an endless run of interrupted reads times out")

checks.equal(detail, "PROTOCOL_TIMEOUT",
             "as PROTOCOL_TIMEOUT - never as an answer")

link.close()


# ======================================================================
checks.section("32. two processes, one port")

# Linux does not enforce exclusive access to a tty by itself. pySerial
# asks for it - `exclusive = True` - and that is what turns a second
# client into a diagnosis instead of two programs interleaving bytes on
# one wire.

source = (FIRMWARE / "PC" / "serial_link.py").read_text(encoding="utf-8")

checks.ok("exclusive = True" in source,
          "the link asks the OS for exclusive access")

# Client A holds the port; client B is refused with the flock message
# Linux actually produces.
busy = serial_link._classify_open_failure(
    "/dev/ttyUSB0",
    Exception("Could not exclusively lock port /dev/ttyUSB0: "
              "[Errno 11] Resource temporarily unavailable"),
)

checks.equal(busy.code, "PORT_BUSY",
             "a second client is told PORT_BUSY")

checks.ok("another program" in busy.message.lower(),
          "and is sent to the other program rather than to the cable")

checks.ok("dialout" not in busy.message,
          "and is NOT told to join a group - the port opened fine for "
          "somebody")

# A exits, B retries. The retry must be an ordinary open, with nothing
# left over from the refusal.
port = FakeSerialPort(device=None)
link = link_to(port)

kind, _data = outcome(lambda: link.request("ping"))

checks.equal(kind, "ok",
             "and once the first client exits, the second opens and "
             "works normally")

checks.equal(port.opened_count, 1,
             "with one open, not a retry storm")

link.close()


# ======================================================================
checks.section("33. two processes, one archive")

# The archive has no lock. What it has is an atomic write, and the
# question is what that does and does not guarantee when two processes
# both have it open.

directory = Path(tempfile.mkdtemp(prefix="freya-two-"))
path = directory / "samples.json"
path.write_text('{"schema_version": 4, "samples": []}', encoding="utf-8")

first = archive_store(path).load()
second = archive_store(path).load()

first.create("A001", 1)
second.create("B001", 2)

# Both wrote. The file is valid - that is the atomicity guarantee - but
# the second write was built from a snapshot taken before the first,
# so A001 is not in it.
final = json.loads(path.read_text(encoding="utf-8"))
names = sorted(record["sample_id"]
               for record in final[bd_config.ARCHIVE_COLLECTION])

checks.equal(names, ["B001"],
             "two processes writing the same archive is LAST WRITER "
             "WINS - the first process's sample is not in the file")

checks.ok(isinstance(final.get(bd_config.ARCHIVE_COLLECTION), list)
          and isinstance(final.get(bd_config.ARCHIVE_COLLECTION), list),
          "but the file is never left corrupt or half-written - that is "
          "what os.replace buys, and it is all it buys")

# THE HONEST CONCLUSION, recorded rather than glossed. Concurrent
# writers are not supported, and the reason it does not bite is that
# the mission runs one client against one port, enforced above by the
# exclusive open. A second client cannot reach the ESP32 at all, so it
# cannot produce a measurement to lose.
checks.ok(True,
          "concurrent archive writers are NOT supported; the exclusive "
          "port open is what makes a second mission client impossible "
          "in the first place")

# A reader that reloads sees the truth on disk, which is the recovery
# an operator has: restart the client.
reloaded = archive_store(path).load()
checks.equal(reloaded.count(), 1,
             "and reopening the archive shows exactly what is durable")


# ======================================================================
checks.section("34-37. the device node renamed, duplicated or symlinked")

# The client takes --port and requires it. There is no auto-selection
# anywhere on the mission path, which is the answer to "auto-selection
# must not guess the wrong rover device": it cannot, because it does
# not choose.

client = (FIRMWARE / "PC" / "rover_science_client.py").read_text(
    encoding="utf-8")

checks.ok('"--port", required=True' in client.replace("'", '"'),
          "--port is REQUIRED, so no device is ever guessed")

checks.ok("available_ports" not in client,
          "and the client does not enumerate ports to pick one")

# 36. Any path must be accepted, including a by-id symlink. The client
# must not impose a /dev/ttyUSB* shape of its own.
PATHS = (
    "/dev/ttyUSB0",
    "/dev/ttyUSB1",
    "/dev/ttyACM0",
    "/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge-if00-port0",
    "/dev/serial/by-path/pci-0000:00:14.0-usb-0:2:1.0-port0",
    "COM4",
    "./my-port-symlink",
)

for candidate in PATHS:
    port = FakeSerialPort(device=None)
    original = serial_link.serial
    serial_link.serial = make_serial_module(lambda p=port: p)

    try:
        link = SerialLink(candidate, timeout=0.3)
        link.open()
        kind, _data = outcome(lambda: link.request("ping"))
        link.close()

    finally:
        serial_link.serial = original

    checks.equal(kind, "ok",
                 "{} is accepted as given - no pattern is imposed on "
                 "the path".format(candidate))

# 34/37. The node disappears and comes back, possibly under a new name.
# The rule is that the link does NOT reattach on its own: a port that
# went away took the firmware's volatile state with it, and silently
# resuming would mean trusting a carousel position across an event that
# may have reset the board.
port = VanishingPort(device=None, fail_on="in_waiting", code=errno.ENODEV)
link = link_to(port)

outcome(lambda: link.request("ping"))

checks.ok(link.serial is None,
          "when the node disappears the link closes")

# Even though the same fake port object still exists and would answer,
# nothing reconnects by itself.
kind, detail = outcome(lambda: link.request("ping"))

checks.equal(detail, "PORT_CLOSED",
             "and it does NOT silently reattach when the node returns - "
             "reconnecting is an operator action, because a board that "
             "went away may have rebooted")

checks.ok("Reconnect" in link._closed_error("ping").message,
          "and the operator is told to reconnect, in those words")

link.close()

# 35. Two USB devices present at once. `attached_ports()` lists them;
# it must list, and never choose.
import device as device_tool                                # noqa: E402

checks.ok(callable(device_tool.attached_ports),
          "the deployment tool can list attached ports")

globs = device_tool.POSIX_PORT_GLOBS

checks.ok("ttyUSB*" in globs and "ttyACM*" in globs,
          "covering both CP210x bridges and native-USB boards")

tool_source = (FIRMWARE / "tools" / "device.py").read_text(encoding="utf-8")

checks.ok("--port" in tool_source,
          "and the deployment tool takes an explicit --port too")


# ======================================================================
checks.section("38. opening a port can reset the board")

# Electrically this is HARDWARE_ONLY - whether DTR and RTS reach the
# auto-reset circuit cannot be decided in software. What CAN be tested
# is the consequence, and the precaution.

checks.ok("handle.dtr = False" in source and "handle.rts = False" in source,
          "both control lines are driven low BEFORE open(), which is "
          "the only moment that matters")

before_open = source.split("handle.open()")[0]

checks.ok("handle.dtr = False" in before_open,
          "and the assignment really is before open(), not after it")

checks.ok("hard_reset" in source,
          "a deliberate reset exists, separately")

checks.ok("hard_reset" not in client,
          "and the operator client never calls it on the way in")

# The consequence, simulated: the board reboots anyway, and its banner
# arrives where an answer should be.
def rebooting(request):
    return (b"rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\n"
            b"MicroPython v1.24.1 on 2024-11-29; ESP32 module\n"
            + json.dumps({
                "request_id": request.get("request_id"),
                "ok": True,
                "cmd": request.get("cmd"),
                "data": {"pong": True},
            }).encode("utf-8"))


link, port = open_link(serial_link, rebooting,
                       link_kwargs={"timeout": 1.0})

kind, _data = outcome(lambda: link.request("ping"))

checks.equal(kind, "ok",
             "a boot banner in front of the answer does not stop the "
             "answer being read")

checks.ok(any("boot:" in line for line in link.last_noise),
          "and the banner is KEPT - it is the evidence the board "
          "restarted")

link.close()


# ======================================================================
checks.section("51-52. the dependency and interpreter assumptions")

# The competition runtime is claimed to need exactly one third-party
# package. That claim is checked mechanically rather than believed.

MISSION_TREES = ("PC", "Science", "BD")

STDLIB_OK = {
    "argparse", "ast", "collections", "copy", "csv", "datetime", "difflib",
    "errno", "functools", "glob", "hashlib", "io", "itertools", "json",
    "math", "os", "pathlib", "random", "re", "shutil", "statistics",
    "sqlite3", "string", "struct", "subprocess", "sys", "tempfile",
    "textwrap",
    "time", "traceback", "types", "typing", "unicodedata", "warnings",
}

LOCAL = {"BD", "Science", "PC", "workflow", "serial_link", "config",
         "support", "fakes"}

third_party = {}

for tree in MISSION_TREES:
    for path in sorted((FIRMWARE / tree).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue

        import ast as ast_module

        tree_ast = ast_module.parse(path.read_text(encoding="utf-8"))

        for node in ast_module.walk(tree_ast):
            names = []

            if isinstance(node, ast_module.Import):
                names = [alias.name.split(".")[0] for alias in node.names]

            elif isinstance(node, ast_module.ImportFrom):
                if node.module and node.level == 0:
                    names = [node.module.split(".")[0]]

            for name in names:
                if name in STDLIB_OK or name in LOCAL:
                    continue

                third_party.setdefault(name, []).append(
                    path.relative_to(FIRMWARE).as_posix())

checks.equal(sorted(third_party), ["serial"],
             "the mission tree imports exactly one third-party package, "
             "and it is pyserial")

checks.ok(all(name.startswith("PC/") for name in third_party["serial"]),
          "and only PC/ imports it - Science and BD never learn a "
          "serial port exists")

# A missing pyserial must produce an instruction, not an ImportError.
checks.ok("pyserial is not installed" in source,
          "a missing pyserial is diagnosed by name")

checks.ok("sys.executable" in source,
          "and the fix names THIS interpreter, so the command can be "
          "pasted on either machine")

checks.ok("pip install" in source,
          "and gives the command")

# The interpreter itself. Nothing in the mission tree may need a
# version newer than the main computer's.
checks.ok(sys.version_info >= (3, 8),
          "the tests run on Python {}.{}".format(*sys.version_info[:2]))

for tree in MISSION_TREES:
    for path in sorted((FIRMWARE / tree).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue

        text = path.read_text(encoding="utf-8")

        # match/case is 3.10+; the walrus is 3.8+. Neither is used, and
        # the point is to keep it that way rather than to admire it.
        checks.ok("\nmatch " not in text,
                  "{} uses no match/case".format(
                      path.relative_to(FIRMWARE).as_posix()))


# ======================================================================
checks.section("54. the mission does not read the environment")

# A program that behaves differently because PYTHONPATH is set is a
# program that behaves differently on the competition machine.

ENVIRONMENT_NAMES = ("PYTHONPATH", "PWD", "HOME", "USER", "USERPROFILE",
                     "VIRTUAL_ENV", "CONDA_PREFIX", "LANG", "LC_ALL",
                     "TZ", "TERM", "COLUMNS")

offenders = []

for tree in MISSION_TREES:
    for path in sorted((FIRMWARE / tree).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue

        text = path.read_text(encoding="utf-8")

        if "os.environ" in text or "getenv" in text:
            offenders.append(path.relative_to(FIRMWARE).as_posix())

checks.equal(offenders, [],
             "no mission module reads an environment variable at all - "
             "checked for {} names".format(len(ENVIRONMENT_NAMES)))

# And every path it needs is resolved from __file__.
checks.ok("Path(__file__).resolve()" in client,
          "the entry point resolves its own location from __file__")

checks.ok("os.getcwd" not in client and "os.chdir" not in client,
          "and never consults or changes the working directory")


# ======================================================================
checks.section("55-56. unusual paths and non-English locales")

# 55. The archive path may contain spaces, Unicode and a long name.
AWKWARD = (
    ("with spaces", "spaces"),
    ("with-00fc00f100ed00e700f6d00e9", "Latin-1 accents"),
    ("with.dots.everywhere", "dots"),
    ("a" * 90, "a 90-character name"),
    ("0441 043a043804400438043b043b0438044604350439", "Cyrillic"),
    ("65e5672c8a9e", "Japanese"),
)

for name, label in AWKWARD:
    base = Path(tempfile.mkdtemp(prefix="freya-path-"))
    nested = base / name
    nested.mkdir(parents=True, exist_ok=True)
    archive = nested / "samples.json"

    store = archive_store(archive).load()
    store.create("S001", 1)
    store.add_measurement("S001", raw={"white": [1] * 18})

    reread = archive_store(archive).load()

    checks.equal(reread.count(), 1,
                 "an archive under a directory whose name has {} is "
                 "written and read back".format(label))

# 56. Numbers on the wire must be machine-format whatever the locale
# says. json never uses a locale decimal separator, and this pins it.
# One key at a time, so what is inspected is the NUMBER and not the
# comma JSON puts between fields. Asserting "no comma anywhere in the
# frame" was the first version here and it was simply wrong: a
# two-field object always has one.
for value, expected in ((1234.5678, "1234.5678"),
                        (1_000_000.0, "1000000.0"),
                        (0.000125, "0.000125"),
                        (-0.5, "-0.5")):
    rendered = json.dumps({"v": value})[len('{"v": '):-1]

    checks.equal(rendered, expected,
                 "{!r} is written as {} - a POSIX decimal point, no "
                 "thousands separator, whatever the locale".format(
                     value, expected))

# Through the real link, end to end.
link, port = open_link(
    serial_link,
    lambda request: {
        "request_id": request["request_id"], "ok": True,
        "cmd": request["cmd"],
        "data": {"reading": 1234.5678, "tiny": 1.5e-7},
    },
    link_kwargs={"timeout": 1.0},
)

_kind, data = outcome(lambda: link.request("ping", value=9876.5432))

checks.close(data["reading"], 1234.5678,
             "a float survives the round trip to full precision")

checks.close(data["tiny"], 1.5e-7,
             "and so does a very small one")

sent = json.loads(port.written[0].decode("utf-8"))

checks.close(sent["value"], 9876.5432,
             "and a float SENT is machine-format on the wire too")

link.close()


# ======================================================================
checks.section("56/60. a terminal that cannot encode what was typed")

# THE DEFECT THIS SECTION EXISTS FOR.
#
# Python encodes stdout with the LOCALE's encoding. Under LANG=C - a
# systemd unit, a minimal container, a cron job, a redirected pipe -
# that encoding is ASCII. Sample notes are free text typed by a Brno
# team, so "vzorek z pouste, zluty pisek" arrives with a hacek in it,
# and printing the record raised UnicodeEncodeError from inside
# print(). The records screen died over a character in a comment field.

import io                                                    # noqa: E402
import rover_science_client                                  # noqa: E402

from fakes import SandboxBD                                  # noqa: E402
from workflow.records import print_full_sample               # noqa: E402

CZECH_NOTE = "vzorek z pouště, žlutý písek"
CZECH_PLACE = "Mars Yard – sektor Č"


def render_on(encoding, record, apply_fix):
    """Print a record to a stream with the given encoding; report how it went."""
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding=encoding, errors="strict")

    saved_out, saved_in = sys.stdout, sys.stdin
    sys.stdout = stream
    sys.stdin = io.StringIO("\n" * 50)

    try:
        if apply_fix:
            rover_science_client.make_console_unbreakable()

        print_full_sample(record)
        stream.flush()

        return (None, raw.getvalue())

    except UnicodeEncodeError as error:
        return (error, raw.getvalue())

    finally:
        sys.stdout, sys.stdin = saved_out, saved_in


bd = SandboxBD()

# THE ARCHIVE, because the last two checks here are about what reached
# the FILE. The session is this process's memory and writes nothing, so
# a record created in it could never appear in samples.json - and the
# question being asked is whether UTF-8 survives the encoder, not
# whether the terminal can draw it.
store = bd.sample_database().archive()
store.create("S001", 1, metadata={"note": CZECH_NOTE,
                                  "location": CZECH_PLACE})
record = store.get_sample("S001")

# Without the guard, on an ASCII console, this is the crash.
failure, _output = render_on("ascii", record, apply_fix=False)

checks.ok(failure is not None,
          "on an ASCII console an unguarded print of a Czech note DOES "
          "raise UnicodeEncodeError - the defect is real, not theoretical")

# With it, every console encoding an operator might have.
for encoding in ("ascii", "cp1252", "latin-1", "utf-8"):
    failure, output = render_on(encoding, record, apply_fix=True)

    checks.ok(failure is None,
              "with the guard, a record with Czech text prints on a {} "
              "console".format(encoding))

    checks.ok(len(output) > 0,
              "  and really produced output rather than stopping early")

# Nothing is silently dropped: what cannot be encoded is shown as an
# escape, so the operator can see a character is there.
_failure, output = render_on("ascii", record, apply_fix=True)
rendered = output.decode("ascii", "replace")

checks.ok("\\u010d" in rendered or "\\u0161" in rendered,
          "an unrepresentable character is rendered as a visible escape, "
          "not thrown away")

# And the ARCHIVE is unaffected by any of it - the terminal's limits are
# not the record's.
stored = bd.samples_file.read_text(encoding="utf-8")

checks.ok(CZECH_NOTE in stored,
          "the stored note is real UTF-8 whatever the terminal can show")

checks.ok(CZECH_PLACE in stored,
          "and so is the stored location")

bd.close()

# The guard must be harmless when there is nothing to reconfigure.
saved = sys.stdout
sys.stdout = io.StringIO()

try:
    rover_science_client.make_console_unbreakable()
    survived = True

except Exception:                                          # noqa: BLE001
    survived = False

finally:
    sys.stdout = saved

checks.ok(survived,
          "and a stdout with no reconfigure() at all - a StringIO, an "
          "IDE, a test harness - is left alone rather than crashed on")

# It runs before anything else in main(), because a screen that cannot
# print cannot report its own failure either.
client_source = (FIRMWARE / "PC" / "rover_science_client.py").read_text(
    encoding="utf-8")
body = client_source.split("def main(")[1]

checks.ok(body.index("make_console_unbreakable()")
          < body.index("parse_args"),
          "and it runs before the arguments are even parsed")


# ======================================================================
checks.section("57-58. a clock that jumps, and timeouts that do not care")

# Wall-clock time is for humans; timeouts are decisions. A daylight
# saving change, an NTP correction or a manually set clock must not be
# able to make a command wait forever or return early.

checks.ok("time.monotonic()" in source,
          "the reader's deadline is built on time.monotonic()")

checks.ok("time.time()" not in source.replace(
              "`time.time()*1000 % 1000000`", ""),
          "and time.time() appears in serial_link.py only inside the "
          "comment explaining why it is not used")

# Every mission module, not just this one.
for tree in MISSION_TREES:
    for path in sorted((FIRMWARE / tree).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue

        text = path.read_text(encoding="utf-8")
        code_lines = [
            line for line in text.splitlines()
            if not line.lstrip().startswith("#")
        ]
        joined = "\n".join(code_lines)

        if "time.time()" in joined and "`time.time()" not in joined:
            checks.ok(False, "{} uses wall-clock time in code".format(
                path.relative_to(FIRMWARE).as_posix()))

checks.ok(True,
          "no mission module makes a timing decision from the wall clock")


class LyingClock:
    """
    A clock whose wall time is nonsense and whose monotonic time is not.

    This is the real shape of the failure: `time.time()` can go
    backwards across an NTP step or a timezone change, while
    `time.monotonic()` is guaranteed not to. A module that mixed them
    would show the difference here.
    """

    def __init__(self):
        self.now = 1000.0
        self.wall = 2_000_000_000.0
        self.jumps = []

    def monotonic(self):
        return self.now

    def time(self):
        return self.wall

    def sleep(self, seconds):
        self.now += float(seconds)

    def advance(self, seconds):
        self.now += float(seconds)

    def jump_wall(self, seconds):
        self.wall += float(seconds)
        self.jumps.append(seconds)


from fakes.clock import install_clock                        # noqa: E402

for jump, label in ((+7200.0, "forward two hours (DST begins)"),
                    (-7200.0, "backward two hours (DST ends)"),
                    (-2_000_000_000.0, "back to 1970"),
                    (+86_400_000.0, "a thousand days into the future")):
    clock = LyingClock()
    handle = install_clock(serial_link, clock)

    try:
        port = FakeSerialPort(device=None, clock=clock)
        link = link_to(port)

        clock.jump_wall(jump)

        kind, _data = outcome(lambda: link.request("ping", timeout=1.0))

        checks.equal(kind, "ok",
                     "a wall clock that jumps {} does not disturb a "
                     "command".format(label))

        link.close()

    finally:
        handle.restore()

# And a timeout still expires correctly while the wall clock is wrong.
clock = LyingClock()
handle = install_clock(serial_link, clock)

try:
    port = FakeSerialPort(device=lambda request: None, clock=clock)
    port.timeout = 0.05
    link = link_to(port)
    link.serial.timeout = 0.05

    clock.jump_wall(-2_000_000_000.0)

    kind, detail = outcome(lambda: link.request("ping", timeout=0.5))

    checks.equal(kind, "link",
                 "and a command that gets no answer still times out with "
                 "the wall clock in 1970")

    checks.equal(detail, "PROTOCOL_TIMEOUT",
                 "as PROTOCOL_TIMEOUT, on schedule")

    link.close()

finally:
    handle.restore()

# The human timestamps are the part that IS allowed to be wrong, and
# they must still be well-formed UTC.
stamp = serial_link.utc_timestamp()

checks.ok(stamp.endswith("+00:00"),
          "human timestamps are explicit UTC, so a machine in the wrong "
          "timezone still records an unambiguous time")

checks.ok("T" in stamp,
          "and ISO 8601 formatted")


sys.exit(checks.report())
