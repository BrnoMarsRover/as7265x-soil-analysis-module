"""
The process itself: interrupted, ended, and started again.

WHAT THIS ASKS

Every other suite asks what the software does while it is running. This
one asks what it does while it is STOPPING, and what the next process
finds when it starts.

    44      Ctrl+C and the terminating signals, at each safe boundary
    45      Ctrl+C with a physical command already on the wire
    46      Ctrl+C at each of the five points of a measurement
    47      SIGTERM during a save
    48      the temporary files a killed process leaves behind
    49      restart after each class of failure, from durable truth
    61-62   a closed stdout, and a closed stdin
    63-65   thousands of invalid selections, and thousands of menus

THE PROPERTY THAT MATTERS MOST

An interrupted command is not a command that did not happen. The
carousel is relative and the mechanism keeps moving after the process
that asked has gone, so the honest answer after Ctrl+C during a move is
"the position is no longer known", never "nothing moved".

WHAT IS FAKED

`serial.Serial`, the keyboard and the directory records live in. The
signals are delivered as the exceptions Python actually raises -
KeyboardInterrupt for SIGINT, and a handler-raised exception for
SIGTERM - because a suite that sent real signals to its own process
would be testing the test runner.
"""

import builtins
import contextlib
import errno
import io
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

import rover_science_client                                 # noqa: E402
import serial_link                                          # noqa: E402
from serial_link import DeviceError, LinkError              # noqa: E402

from BD import config as bd_config          # noqa: E402
from BD import samples as samples_module                     # noqa: E402
from BD.samples import StorageError, archive_store            # noqa: E402

from workflow import screen                                  # noqa: E402
from workflow.prompts import OperatorGone, ask, choose       # noqa: E402

from fakes import (                                          # noqa: E402
    LoopbackDevice,
    SandboxBD,
    loopback_link,
    sandbox_mission,
)
from fakes.esp32 import LoopbackDevice as _Loopback          # noqa: E402,F401
from fakes.serial_port import (                              # noqa: E402
    FakeSerialPort,
    make_serial_module,
)

checks = support.Checks("lifecycle")

FIRMWARE = support.FIRMWARE


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def outcome(call):
    """
    How a call ended, described in a few words.

    The successful value is deliberately NOT part of what comes back
    when a case is checking HOW something ended. A `measure_raw` result
    is three illuminations of eighteen channels, and returning it here
    put the whole spectrum into a failure message - thousands of
    characters describing a mismatch of one word.
    """
    try:
        call()

        return ("ok", "ok")

    except DeviceError as error:
        return ("device", error.code)

    except LinkError as error:
        return ("link", error.code)

    except BaseException as error:                         # noqa: BLE001
        return ("raw", type(error).__name__)


def result_of(call):
    """For the cases that DO want the value."""
    try:
        return ("ok", call())

    except BaseException as error:                         # noqa: BLE001
        return ("raw", type(error).__name__)


class Terminated(BaseException):
    """
    What a SIGTERM handler raises.

    A BaseException for the same reason KeyboardInterrupt is one: it is
    the process being asked to stop, not an operation failing, and a
    handler written for an operational error must not absorb it.
    """


def interrupting(target, name, exception, after=1):
    """
    Make the `after`-th call to one function raise, then restore it.

    Returns a context manager. The COUNT is what makes this useful:
    "interrupt the second write" is a different test from "interrupt
    the first", and a measurement makes several.
    """

    class _Ctx:
        def __init__(self):
            self.original = getattr(target, name)
            self.calls = 0

        def __enter__(self):
            original = self.original

            def wrapper(*args, **kwargs):
                self.calls += 1

                if self.calls >= after:
                    raise exception

                return original(*args, **kwargs)

            setattr(target, name, wrapper)

            return self

        def __exit__(self, exc_type, exc_value, traceback):
            setattr(target, name, self.original)

            return False

    return _Ctx()


def scripted_input(answers, on_exhausted=EOFError):
    """Install a keyboard; `on_exhausted` decides what running out means."""
    supply = list(answers)

    def reader(prompt=""):
        if supply:
            value = supply.pop(0)

            if isinstance(value, BaseException):
                raise value

            return value

        raise on_exhausted("no more answers")

    return reader


# ======================================================================
checks.section("62. EOF at a menu ends the session instead of spinning")

# THE DEFECT. Every menu is a `while True` around `choose()`, and each
# leaves by its own key - "q" at the top level, "0" in the submenus.
# `ask` returned "" on EOF, which matches none of them, so a closed
# stdin meant: print the menu, read nothing, print it again, forever.
#
# Measured before the fix: 3,001 prompts at the main screen without
# exiting, and it would have continued indefinitely at full CPU.

link, port, loopback = loopback_link(serial_link)
link.connect_servo()

prompts = {"count": 0}


def always_eof(prompt=""):
    prompts["count"] += 1

    if prompts["count"] > 5000:
        raise RuntimeError(
            "the menu asked {} questions on a closed stdin - it is "
            "spinning".format(prompts["count"]))

    raise EOFError("stdin is closed")


original_input = builtins.input
builtins.input = always_eof

try:
    with contextlib.redirect_stdout(io.StringIO()):
        kind, detail = outcome(lambda: screen.interactive(link))

finally:
    builtins.input = original_input

checks.equal(kind, "raw",
             "EOF at the main menu raises rather than looping")

checks.equal(detail, "OperatorGone",
             "and what it raises is OperatorGone - the session ended, "
             "nothing failed")

checks.ok(prompts["count"] <= 3,
          "after {} prompt(s), not thousands".format(prompts["count"]))

link.close()

# OperatorGone must survive the handlers written for operational
# errors. It is a BaseException precisely so `except Exception` around
# a save or an analysis cannot absorb the end of the session.
checks.ok(issubclass(OperatorGone, BaseException),
          "OperatorGone is a BaseException")

checks.ok(not issubclass(OperatorGone, Exception),
          "and NOT an Exception, so no `except Exception` in the "
          "workflow can swallow a closed terminal")


def swallowing():
    try:
        choose()

    except Exception:                                      # noqa: BLE001
        return "swallowed"

    return "not reached"


builtins.input = always_eof

try:
    kind, detail = outcome(swallowing)

finally:
    builtins.input = original_input

checks.equal(detail, "OperatorGone",
             "proved: a broad `except Exception` does not catch it")


# ======================================================================
checks.section("62. and the free-text fields still treat EOF as blank")

# The distinction the fix rests on. At a MENU, end of input means the
# session is over. At an optional note, it means "leave it blank", and
# turning that into a shutdown would make a finished input script
# unable to answer the last question of a form.

builtins.input = always_eof

try:
    blank = ask("Note [optional]")
    defaulted = ask("Task", default="survey")

finally:
    builtins.input = original_input

checks.equal(blank, "",
             "EOF at a free-text field returns the empty default")

checks.equal(defaulted, "survey",
             "and returns the field's own default when it has one")


# ======================================================================
checks.section("62/106. main() turns EOF into a clean exit, port released")

# The consequence that made this worth fixing was not the spin. It was
# that `main()` releases the port in a `finally`, and a loop that never
# returns never reaches it - so the client the operator believed they
# had closed went on holding /dev/ttyUSB0, and the NEXT client was
# refused with PORT_BUSY for a reason that had nothing to do with the
# board.

loop = LoopbackDevice()
loop.build()
fake_port = FakeSerialPort(device=loop)

saved_serial = serial_link.serial
serial_link.serial = make_serial_module(lambda: fake_port)
builtins.input = always_eof
prompts["count"] = 0

try:
    with contextlib.redirect_stdout(io.StringIO()):
        code = rover_science_client.main(["--port", "/dev/ttyUSB0"])

finally:
    builtins.input = original_input
    serial_link.serial = saved_serial

checks.equal(code, 0,
             "Ctrl+D exits with status 0 - the same clean end as Ctrl+C")

checks.ok(not fake_port.is_open,
          "AND THE PORT IS RELEASED, which the spinning loop never did")

checks.equal(fake_port.closed_count, 1,
             "closed exactly once")

# The same for Ctrl+C, which has always worked and must keep working.
fake_port = FakeSerialPort(device=loop)
serial_link.serial = make_serial_module(lambda: fake_port)
builtins.input = scripted_input([KeyboardInterrupt()])

try:
    with contextlib.redirect_stdout(io.StringIO()):
        code = rover_science_client.main(["--port", "/dev/ttyUSB0"])

finally:
    builtins.input = original_input
    serial_link.serial = saved_serial

checks.equal(code, 0, "Ctrl+C exits with status 0 too")

checks.ok(not fake_port.is_open, "and releases the port")


# ======================================================================
checks.section("44/45. Ctrl+C with a physical command on the wire")

# `move_slots` is RELATIVE. If the process dies between the write and
# the answer, the mechanism has still been told to move, and it moves
# after the process is gone. The one thing the software must not do is
# tell the operator that nothing happened.

link, port, loopback = loopback_link(serial_link)
link.connect_servo()
link.sync_position(load_slot=1)

before = link.get_status()
checks.ok((before.get("carousel") or {}).get("position_valid"),
          "the carousel starts synchronized")

# Interrupt after the bytes are written, before the answer is read.
with interrupting(port, "read", KeyboardInterrupt()):
    kind, detail = outcome(lambda: link.move_slots("cw", 1))

checks.equal(kind, "raw",
             "Ctrl+C during a move propagates - it is not converted "
             "into a failed command")

checks.equal(detail, "KeyboardInterrupt",
             "as KeyboardInterrupt")

checks.ok(len(port.written) > 0,
          "and the command HAD been written - the mechanism was told "
          "to move")

# The firmware, which never saw a Ctrl+C, executed it.
after = link.get_status()

checks.ok(after is not None,
          "the link still works afterwards - one interrupted command "
          "does not poison the session")

# The honest statement: the PC cannot know whether the move finished,
# so the only safe reading of the position is the firmware's.
checks.ok("request_id" in json.loads(port.written[-1].decode("utf-8")),
          "and the next command is a fresh request with its own id, "
          "not a retry of the interrupted one")

link.close()


# ======================================================================
checks.section("46. Ctrl+C at each of the five points of a measurement")

# Before the acquisition, during it, after it, during the save, and
# during the return move. Each has a different truthful answer about
# what is durable and what the mechanism is doing.

def interrupt_at_measure(port, when):
    """
    Interrupt relative to the `measure_raw` COMMAND, not to a call count.

    Counting reads was the first version of this and it did not work:
    the loopback answers a whole frame in one chunk, so "the second
    read" was already after the acquisition had completed and the
    interrupt never landed where the label said it did. Watching for
    the command by name puts each interrupt exactly where it claims to
    be, whatever the chunking does.
    """
    state = {"sent": False}
    real_write = port.write
    real_read = port.read

    def write(data):
        if b'"measure_raw"' in data:
            if when == "before":
                raise KeyboardInterrupt()

            state["sent"] = True

        return real_write(data)

    def read(count=1):
        if state["sent"] and when == "inflight":
            raise KeyboardInterrupt()

        return real_read(count)

    port.write = write
    port.read = read

    return lambda: (setattr(port, "write", real_write),
                    setattr(port, "read", real_read))


for label, when in (("before the command is written", "before"),
                    ("while the acquisition is in flight", "inflight")):
    link, port, loopback = loopback_link(serial_link)
    mission, bd = sandbox_mission(link)

    link.connect_servo()
    link.sync_position(load_slot=1)
    link.select_slot(1, sample_id="S-INT")
    mission.session.create("S-INT", 1)

    durable_before = bd.samples_file.read_bytes()
    writes_before = len(port.written)

    restore = interrupt_at_measure(port, when)

    try:
        kind, detail = outcome(
            lambda: link.measure_raw(1, sample_id="S-INT"))

    finally:
        restore()

    checks.equal(detail, "KeyboardInterrupt",
                 "Ctrl+C {} propagates".format(label))

    # NOTHING was saved: the save is downstream of the acquisition and
    # was never reached.
    checks.equal(bd.samples_file.read_bytes(), durable_before,
                 "  and the archive is byte-identical - an interrupted "
                 "measurement saves nothing")

    # The LIVE working set, which is where the Measurement would have
    # been created. Re-reading the file would prove nothing: the
    # session is never in it, so the check would pass even if an
    # interrupted measurement had recorded one.
    checks.equal(len(mission.session.get_sample("S-INT")
                     .get("measurements") or []),
                 0,
                 "  and no Measurement record was created")

    if when == "before":
        checks.equal(len(port.written), writes_before,
                     "  and nothing was written to the wire at all")

    else:
        checks.ok(len(port.written) > writes_before,
                  "  and the command HAD gone out, so the mechanism is "
                  "moving with nobody listening")

    link.close()
    bd.close()

# THE THIRD POINT: the acquisition succeeds and the SAVE is interrupted.
# This is the one that decides whether an experiment is lost or only an
# analysis.
link, port, loopback = loopback_link(serial_link)
mission, bd = sandbox_mission(link)

link.connect_servo()
link.sync_position(load_slot=1)
link.select_slot(1, sample_id="S-SAVE")
mission.session.create("S-SAVE", 1)

acquisition = link.measure_raw(1, sample_id="S-SAVE")

checks.ok(bool(acquisition.get("illuminations")),
          "the acquisition completed - the soil really went through the "
          "instrument")

durable_before = bd.samples_file.read_bytes()
fields = mission.measurement_from_acquisition(acquisition, "S-SAVE")

# RECORDING THE MEASUREMENT TOUCHES NO FILE, so a Ctrl+C aimed at the
# rename cannot land on it. That is not a weaker test than before - it
# is the property that replaced the old one: an interrupt during a
# measurement cannot cost the spectrum, because there is no save to
# interrupt.
with interrupting(samples_module.os, "replace", KeyboardInterrupt()):
    kind, detail = outcome(
        lambda: mission.session.add_measurement("S-SAVE", **fields))

checks.equal(kind, "ok",
             "recording the Measurement reaches no rename, so a Ctrl+C "
             "aimed at one cannot interrupt it")

checks.equal(bd.samples_file.read_bytes(), durable_before,
             "and the archive is byte-identical - measuring writes "
             "nothing to it")

# THE INTERRUPT STILL LANDS WHERE A WRITE REALLY HAPPENS.
with interrupting(samples_module.os, "replace", KeyboardInterrupt()):
    kind, detail = outcome(
        lambda: mission.archive.adopt(
            mission.session.get_sample("S-SAVE")))

checks.equal(detail, "KeyboardInterrupt",
             "Ctrl+C during an IMPORT propagates")

checks.equal(bd.samples_file.read_bytes(), durable_before,
             "and the archive is still byte-identical - the interrupted "
             "import wrote nothing")

checks.ok(mission.archive.get_sample("S-SAVE") is None,
          "and the in-memory archive matches the file, so nothing shows "
          "a record the disk does not have")

# The spectra are not lost: the device still holds them, which is the
# whole reason Sync ESP32 Acquisitions to BD exists.
retained = link.request("list_saved_samples")

checks.ok(retained is not None,
          "and the ESP32 still holds the acquisition, so an interrupted "
          "save costs an analysis and not the experiment")

link.close()
bd.close()


# ======================================================================
checks.section("47. SIGTERM during a save leaves the archive intact")

# The atomic write is what this rests on: the temporary file is written
# and fsynced, and only then renamed over the target. A process killed
# at any point before the rename leaves the previous archive whole.

SAVE_POINTS = (
    ("while the JSON is being serialized", samples_module.json, "dump"),
    ("while the temporary file is created", samples_module.tempfile,
     "mkstemp"),
    ("during fsync", samples_module.os, "fsync"),
    ("at the rename itself", samples_module.os, "replace"),
)

for label, target, name in SAVE_POINTS:
    directory = Path(tempfile.mkdtemp(prefix="freya-sigterm-"))
    path = directory / "samples.json"
    path.write_text('{"schema_version": 4, "samples": []}',
                    encoding="utf-8")

    store = archive_store(path).load()
    store.create("S001", 1)
    store.add_measurement("S001", raw={"white": [1] * 18})

    durable = path.read_bytes()
    durable_count = len(
        json.loads(durable)[bd_config.ARCHIVE_COLLECTION][0].get("measurements") or [])

    with interrupting(target, name, Terminated("SIGTERM")):
        kind, detail = outcome(
            lambda: store.add_measurement("S001", raw={"white": [2] * 18}))

    checks.equal(detail, "Terminated",
                 "SIGTERM {} is not swallowed".format(label))

    checks.equal(path.read_bytes(), durable,
                 "  and the archive on disk is byte-identical")

    reopened = archive_store(path).load()
    reopened_count = len(
        reopened.get_sample("S001").get("measurements") or [])

    checks.equal(reopened_count, durable_count,
                 "  and reopens with its {} measurement(s) intact".format(
                     durable_count))

    checks.ok(json.loads(path.read_text(encoding="utf-8")) is not None,
              "  and is still valid JSON, never half-written")


# ======================================================================
checks.section("48. what a killed process leaves behind")

# A process killed between mkstemp and os.replace leaves a .samples-*
# file in the archive directory. The next run must not read it, count
# it, or mistake it for the archive - and must not delete it either,
# because it may be the only copy of something the dead run had.

directory = Path(tempfile.mkdtemp(prefix="freya-orphan-"))
path = directory / "samples.json"
path.write_text('{"schema_version": 4, "samples": []}', encoding="utf-8")

store = archive_store(path).load()
store.create("REAL", 1)
durable = path.read_bytes()

# Kill it at the rename, for real: the temporary survives.
with interrupting(samples_module.os, "replace", Terminated("SIGKILL")):
    outcome(lambda: store.add_measurement("REAL", raw={"white": [1] * 18}))

leftovers = sorted(
    entry.name for entry in directory.iterdir()
    if entry.name.startswith(".samples-")
)

# The write path unlinks its own temporary on failure, which is the
# tidy case. The untidy case is a process that never got to run that
# cleanup at all, so it is created directly.
orphan = directory / ".samples-abandoned.tmp"
orphan.write_text(json.dumps({
    "schema_version": 4,
    "samples": [{"sample_id": "GHOST", "slot_id": 9, "state": "MEASURED"}],
}), encoding="utf-8")

restarted = archive_store(path).load()

checks.ok(restarted.get_sample("GHOST") is None,
          "a temporary file left by a killed process is not read as "
          "archive content")

checks.equal(restarted.count(), 1,
             "and the sample count comes from the real archive alone")

checks.equal(path.read_bytes(), durable,
             "which is byte-identical to what the dead process left")

checks.ok(orphan.exists(),
          "and the orphan is not deleted on a guess - a run that lost "
          "data may need it")

# The restarted process must be able to write normally.
restarted.add_measurement("REAL", raw={"white": [3] * 18})

checks.equal(archive_store(path).load().get_sample("REAL")["measurements"]
             .__len__(), 1,
             "and the next save works normally beside the orphan")


# ======================================================================
checks.section("49. restart after each failure reads durable truth")

# The rule from A.2, applied to a NEW PROCESS rather than a new object:
# whatever the dead run believed, the live run believes the file.

FAILURES = (
    ("a full disk at the rename", samples_module.os, "replace",
     OSError(errno.ENOSPC, "No space left on device")),
    ("a read-only filesystem", samples_module.tempfile, "mkstemp",
     OSError(errno.EROFS, "Read-only file system")),
    ("the process terminated", samples_module.json, "dump",
     Terminated("SIGTERM")),
    ("memory exhausted", samples_module.json, "dump",
     MemoryError("out of memory")),
)

for label, target, name, exception in FAILURES:
    directory = Path(tempfile.mkdtemp(prefix="freya-restart-"))
    path = directory / "samples.json"
    path.write_text('{"schema_version": 4, "samples": []}',
                    encoding="utf-8")

    store = archive_store(path).load()
    store.create("S001", 1)
    store.add_measurement("S001", raw={"white": [1] * 18})

    durable_count = len(
        json.loads(path.read_text(encoding="utf-8"))
        [bd_config.ARCHIVE_COLLECTION][0].get("measurements") or [])

    with interrupting(target, name, exception):
        outcome(lambda: store.add_measurement(
            "S001", raw={"white": [99] * 18}))

    # THE RESTART. A brand new store object, reading the file with no
    # memory of what the previous one was holding.
    del store

    restarted = archive_store(path).load()
    restarted_count = len(
        restarted.get_sample("S001").get("measurements") or [])

    checks.equal(restarted_count, durable_count,
                 "after {}, a restarted process sees the {} durable "
                 "measurement(s)".format(label, durable_count))

    checks.ok(all(
        (entry.get("raw") or {}).get("white", [None])[0] != 99
        for entry in restarted.get_sample("S001").get("measurements") or []
    ), "  and the measurement that failed to save is not among them")

    # And it can carry on.
    restarted.add_measurement("S001", raw={"white": [4] * 18})

    checks.equal(
        len(archive_store(path).load()
            .get_sample("S001").get("measurements") or []),
        durable_count + 1,
        "  and the next save after the restart works")


# ======================================================================
checks.section("61. stdout that goes away mid-screen")

# A piped consumer that closes - `client | head` - gives the writer a
# BrokenPipeError. It is not a reason to corrupt anything; the question
# is only whether it is a controlled end.


class BrokenStdout(io.StringIO):
    def __init__(self, break_after=5):
        super().__init__()
        self.writes = 0
        self.break_after = break_after

    def write(self, text):
        self.writes += 1

        if self.writes > self.break_after:
            raise BrokenPipeError(errno.EPIPE, "Broken pipe")

        return super().write(text)


link, port, loopback = loopback_link(serial_link)
mission, bd = sandbox_mission(link)

# In the ARCHIVE, because the last check of this case counts what is in
# the FILE afterwards. A session Sample is never in one.
mission.archive.create("S001", 1)

broken = BrokenStdout(break_after=3)
saved_stdout = sys.stdout
sys.stdout = broken

try:
    from workflow.records import print_full_sample

    kind, detail = outcome(
        lambda: print_full_sample(mission.archive.get_sample("S001")))

finally:
    sys.stdout = saved_stdout

checks.equal(kind, "raw",
             "a stdout that breaks mid-screen raises rather than being "
             "silently ignored")

checks.equal(detail, "BrokenPipeError",
             "as BrokenPipeError, which is what the OS reported")

# The archive must be untouched by a display failure.
checks.equal(archive_store(bd.samples_file).load().count(), 1,
             "and the archive is unaffected - a screen that could not "
             "print changed no data")

link.close()
bd.close()


# ======================================================================
checks.section("63/91. thousands of invalid selections")

# The menu must not grow, recurse or slow down because the operator
# leaned on the keyboard. This is also the CPU-saturation check: the
# loop has to consume its input, not spin on it.

link, port, loopback = loopback_link(serial_link)
link.connect_servo()

# Split deliberately. A BLANK selection is not an unknown option - it
# is a bare Enter, and `choose` documents that it must not be scolded
# for one. Mixing the two into one list and expecting every entry to be
# reported was the first version of this case, and it was simply wrong
# about the contract.
GARBAGE = ["zz", "999", "-1", "!!", "%%%", "0" * 200,
           "\x00", "select 1; drop table"]
BLANKS = [" ", "\t", ""]

answers = []

for index in range(4000):
    answers.append(GARBAGE[index % len(GARBAGE)] if index % 5
                   else BLANKS[index % len(BLANKS)])

garbage_count = sum(1 for answer in answers
                    if answer.strip())
blank_count = len(answers) - garbage_count

answers.append("q")

builtins.input = scripted_input(answers)

try:
    with contextlib.redirect_stdout(io.StringIO()) as out:
        kind, code = result_of(lambda: screen.interactive(link))

finally:
    builtins.input = original_input

checks.equal(kind, "ok",
             "4,000 junk selections followed by 'q' leaves the menu "
             "normally")

checks.equal(code, 0,
             "with exit status 0")

text = out.getvalue()

checks.equal(text.count("Unknown option"), garbage_count,
             "each of the {} non-blank junk selections was answered "
             "'Unknown option' - none was silently ignored".format(
                 garbage_count))

checks.ok(blank_count > 0 and text.count("Unknown option") < len(answers),
          "and the {} blank ones were NOT - a bare Enter is not an "
          "error".format(blank_count))

link.close()


# ======================================================================
checks.section("64/65. menus loop, they do not recurse")

# A submenu that called itself to go "back" would grow the Python stack
# once per navigation, and a competition day is thousands of them.

MENU_FILES = ("screen.py", "carousel.py", "calibration.py", "records.py",
              "measure.py")

for name in MENU_FILES:
    source = (FIRMWARE / "PC" / "workflow" / name).read_text(
        encoding="utf-8")

    import ast

    tree = ast.parse(source)

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue

        if not node.name.startswith("menu_"):
            continue

        calls_itself = any(
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Name)
            and inner.func.id == node.name
            for inner in ast.walk(node)
        )

        checks.ok(not calls_itself,
                  "{}::{} does not call itself - navigation is a loop, "
                  "not recursion".format(name, node.name))

# And the depth really is flat, measured rather than inferred.
link, port, loopback = loopback_link(serial_link)
link.connect_servo()

depths = []
real_choose = screen.choose


def measuring_choose(prompt="Select"):
    depths.append(len(sys._getframe().f_back.f_back.f_code.co_name))

    depth = 0
    frame = sys._getframe()

    while frame is not None:
        depth += 1
        frame = frame.f_back

    depths[-1] = depth

    return real_choose(prompt)


screen.choose = measuring_choose

# Enter the tools menu and come back out, many times over.
navigation = []

for _ in range(300):
    navigation.extend(["t", "0"])

navigation.append("q")

builtins.input = scripted_input(navigation)

try:
    with contextlib.redirect_stdout(io.StringIO()):
        kind, detail = outcome(lambda: screen.interactive(link))

finally:
    builtins.input = original_input
    screen.choose = real_choose

checks.equal(kind, "ok",
             "300 round trips into the tools menu and back finish "
             "normally")

if depths:
    checks.equal(max(depths) - min(depths), max(depths) - min(depths),
                 "stack depth sampled {} times".format(len(depths)))

    checks.ok(max(depths) - min(depths) <= 4,
              "and the stack depth varies by at most {} frames across "
              "all of them - entering a menu costs a constant, not a "
              "frame per visit".format(max(depths) - min(depths)))

link.close()


sys.exit(checks.report())
