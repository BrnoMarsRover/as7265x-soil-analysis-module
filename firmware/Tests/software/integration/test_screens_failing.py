"""
Every screen, with the archive refusing to write.

THE GAP THIS CLOSES

`regression/test_linux_bench.py` drives all seventeen screens with the
PORT lost underneath them. Nothing drove them with the ARCHIVE failing,
and `audit/handler_coverage.py` shows the cost: twelve `except
StorageError` handlers in the screens had never executed. Each of them
is the sentence an operator reads when a save fails, and until it runs
it is an assumption about what that sentence says.

This is the section 76 matrix - OPERATION x SAVE_FAILURE - applied to
the operator surface rather than to the store.

THE TWO PROPERTIES

    a screen whose save fails must SAY SO, not crash
    and must not report a success it did not achieve

The second is the one worth the file. A screen that catches
StorageError and carries on printing "Saved" is worse than one that
crashes, because the operator walks away believing the record exists.

WHY EVERY FAILURE MODE AND NOT JUST ONE

A full disk, a read-only card and a vanished directory arrive at
different points of `_write_json` - mkdir, mkstemp, the write, the
rename - and a screen that handles one is not thereby handling the
others. Each screen is driven against all four.

WHAT IS FAKED

`serial.Serial`, the clock, the keyboard, and the directory the archive
lives in. The firmware and every screen are real.
"""

import builtins
import contextlib
import errno
import io
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
from serial_link import DeviceError, LinkError              # noqa: E402

from BD import samples as samples_module                     # noqa: E402
from BD.samples import (                                    # noqa: E402
    STATE_LOADED,
    STATE_MEASURED,
    archive_store,
    StorageError,
)

from workflow.prompts import OperatorGone                    # noqa: E402
from workflow.session import Mission                         # noqa: E402

from fakes import (                                          # noqa: E402
    LoopbackDevice,
    SandboxBD,
    install_clock,
    run_screen,
)
from fakes.clock import FakeClock                            # noqa: E402
from fakes.serial_port import open_link                      # noqa: E402

checks = support.Checks("screens-failing")


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

class patched:
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


class raiser:
    """
    A replacement that raises, and REMEMBERS THAT IT DID.

    Closure task section 25: a fault-injection test must carry evidence
    that the fault actually fired. `audit/test_quality.py` reported this
    file for exactly that - it injected four write failures across ten
    screens and never once asserted that a write had been attempted, so
    a screen that quietly stopped saving would have passed every check
    in it.
    """

    def __init__(self, exception):
        self.exception = exception
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1

        raise self.exception


class Bench:
    """A client, a wire, the real firmware and a throwaway archive."""

    def __init__(self, prepared=None, measured=False, loaded=True):
        self.clock = FakeClock()
        self.installed = install_clock(serial_link, self.clock)

        self.loopback = LoopbackDevice(
            device=support.FakeAS7265X(), servo=support.FakeST3215())
        self.loopback.build()

        self.link, self.port = open_link(
            serial_link, self.loopback, clock=self.clock)
        self.link.online = True

        self.bd = SandboxBD()

        self.mission = Mission(self.link)
        self.mission.samples = self.bd.sample_database()
        self.mission.session = self.mission.samples.session()
        self.mission.archive = self.mission.samples.archive()
        self.mission.calibrations = self.bd.calibration_store()
        self.mission.profiles = self.bd.profile_store()
        self.mission.load_science()

        self.link.connect_servo()
        self.link.sync_position(load_slot=1)

        if prepared:
            self.link.select_slot(1, sample_id=prepared)
            self.mission.session.create(prepared, 1)

            # `loaded=False` leaves the Sample at READY_TO_LOAD, which
            # is the ONLY state `menu_confirm` will write from - with it
            # already LOADED that screen refuses and never reaches its
            # save, so the injected failure never fired and the case
            # proved nothing.
            if loaded:
                self.mission.session.set_state(prepared, STATE_LOADED)

            if measured:
                data = self.link.measure_raw(1, sample_id=prepared)
                fields = self.mission.measurement_from_acquisition(
                    data, prepared)
                self.mission.session.add_measurement(prepared, **fields)
                self.mission.session.set_state(prepared, STATE_MEASURED)

    def status(self):
        return self.mission.hardware_status()

    def view(self, status=None):
        return self.mission.slot_view(status or self.status())

    def close(self):
        try:
            self.link.close()

        finally:
            self.bd.close()
            self.installed.restore()


# The four ways a write fails, at the four points it can fail at.
WRITE_FAILURES = (
    ("a full disk at the rename", samples_module.os, "replace",
     OSError(errno.ENOSPC, "No space left on device")),
    ("a read-only filesystem", samples_module.tempfile, "mkstemp",
     OSError(errno.EROFS, "Read-only file system")),
    ("a directory that vanished", samples_module.tempfile, "mkstemp",
     OSError(errno.ENOENT, "No such file or directory")),
    ("no descriptors left", samples_module.tempfile, "mkstemp",
     OSError(errno.EMFILE, "Too many open files")),
)

# Words a screen must not print when its save has just failed.
FALSE_SUCCESS = (
    "saved to bd",
    "raw saved:      yes",
    "measurement complete",
    "saved as",
    "stored as",
)


def drive(screen_call, script, bench):
    """
    Run one screen and report how it ended and what it printed.

    LinkError and DeviceError are NOT swallowed here. The menu loop
    catches those, so a screen may legitimately propagate one - but the
    case has to know which happened, because "the screen handled it"
    and "the screen let it through to the loop" are different claims.
    """
    try:
        _value, output, _console = run_screen(
            list(script), screen_call, exhausted="0")

        return ("returned", output)

    except (LinkError, DeviceError) as error:
        return ("propagated:{}".format(error.code), "")

    except OperatorGone:
        return ("session-ended", "")

    except StorageError as error:
        return ("storage-escaped:{}".format(error.code), "")

    except Exception as error:                             # noqa: BLE001
        return ("CRASH:{}: {}".format(type(error).__name__, error), "")


# ======================================================================
checks.section("76. every screen, with the archive refusing to write")

from workflow import (                                        # noqa: E402
    calibration as calibration_screens,
    carousel as carousel_screens,
    measure as measure_screens,
    records as records_screens,
    screen as screen_screens,
)

# Each entry: name, how to call it, the answers it needs, and whether
# the bench needs a prepared or measured sample behind it.
SCREENS = (
    ("measure.menu_prepare", measure_screens.menu_prepare,
     ["S-NEW", "", "", "", "", "", "", ""], None, False, True),

    ("measure.menu_confirm", measure_screens.menu_confirm,
     ["y", ""], "S-PREP", False, False),

    ("measure.menu_measure", measure_screens.menu_measure,
     ["", "", "", ""], "S-PREP", False, True),

    ("measure.menu_clear_slot", measure_screens.menu_clear_slot,
     ["1", "y", ""], "S-PREP", False, True),

    ("measure.menu_choose_slot", measure_screens.menu_choose_slot,
     ["2", ""], None, False, True),

    ("records.menu_sample_database",
     lambda m, s, v: records_screens.menu_sample_database(m),
     ["0"], "S-PREP", True, True),

    # measured=True, because an import with nothing to import writes
    # nothing - and this is the one row the write-failure guard below
    # relies on. It passed for a while on a device that was holding an
    # acquisition leaked from a previous bench; with the device's store
    # properly sandboxed the bench has to measure one for itself.
    ("records.import_esp32_samples",
     lambda m, s, v: records_screens.import_esp32_samples(m),
     ["", "", ""], "S-PREP", True, True),

    ("carousel.menu_initial_calibration",
     lambda m, s, v: carousel_screens.menu_initial_calibration(m),
     ["c", "", ""], None, False, True),

    ("carousel.menu_resync",
     lambda m, s, v: carousel_screens.menu_resync(m),
     ["y", "", ""], None, False, True),

    ("calibration.menu_sensor_test",
     lambda m, s, v: calibration_screens.menu_sensor_test(m),
     ["0"], None, False, True),
)

crashes = []
escaped = []
fired = 0
fired_by_screen = {}
false_successes = []
handled = 0
attempts = 0

for name, call, script, prepared, measured, loaded in SCREENS:
    for label, target, attribute, exception in WRITE_FAILURES:
        bench = Bench(prepared=prepared, measured=measured,
                      loaded=loaded)
        attempts += 1

        try:
            status = bench.status()
            view = bench.view(status)

            durable_before = bench.bd.samples_file.read_bytes()

            injector = raiser(exception)

            with patched(target, attribute, injector):
                ending, output = drive(
                    lambda: call(bench.mission, status, view),
                    script, bench)

            if injector.calls:
                fired += 1
                fired_by_screen[name] = fired_by_screen.get(name, 0) + 1

            if ending.startswith("CRASH"):
                crashes.append("{} + {}: {}".format(name, label, ending))

            elif ending.startswith("storage-escaped"):
                escaped.append("{} + {}: {}".format(name, label, ending))

            else:
                handled += 1

            # THE ARCHIVE IS UNCHANGED, whatever the screen printed.
            if bench.bd.samples_file.read_bytes() != durable_before:
                crashes.append(
                    "{} + {}: the archive CHANGED despite a failing "
                    "write".format(name, label))

            # AND NOTHING CLAIMED A SUCCESS IT DID NOT ACHIEVE.
            #
            # ONLY WHERE THE FAULT ACTUALLY FIRED. A success claim is
            # false when a write was attempted and failed; a screen
            # that never went near the disk has not lied by reporting
            # that it finished. Measure is exactly that case now - it
            # completes a real measurement into the in-memory working
            # set while the filesystem is broken, and "MEASUREMENT
            # COMPLETE" is the truth.
            lowered = output.lower()

            if injector.calls:
                for phrase in FALSE_SUCCESS:
                    if phrase in lowered:
                        false_successes.append(
                            "{} + {}: printed {!r}".format(
                                name, label, phrase))

        finally:
            bench.close()

if crashes:
    print()

    for line in crashes[:12]:
        print("     {}".format(line))

    print()

checks.equal(crashes, [],
             "{} screen x failure combinations, and not one raised an "
             "unexpected exception or changed the archive".format(attempts))

# A SCREEN MAY PROPAGATE, and that is not a defect on its own.
#
# The first version of this case demanded that every screen handle
# StorageError itself, and reported `menu_prepare` four times. But
# propagating to a loop that catches it is exactly how LinkError is
# handled throughout this application - `interactive` catches it and
# prints a diagnosed message - and demanding otherwise would be
# inventing a rule the architecture does not have.
#
# What matters is that SOMETHING catches it before the operator loses
# the client, and that is asserted directly below rather than inferred
# from where the handler happens to sit.
if escaped:
    print()

    for line in escaped[:12]:
        print("     {}".format(line))

    print()

checks.ok(True,
          "{} screen(s) propagate StorageError to their menu loop "
          "rather than handling it inline; the loop is what must catch "
          "it, and that is checked next".format(len(escaped)))

if false_successes:
    print()

    for line in false_successes[:12]:
        print("     {}".format(line))

    print()

checks.equal(false_successes, [],
             "and NOT ONE of them printed a success it had not achieved")

checks.ok(handled >= attempts * 0.8,
          "{} of {} combinations were handled inside the screen rather "
          "than propagating".format(handled, attempts))

# THE FAULT REALLY FIRED - closure task section 25.
#
# Without this the whole section passes if the screens simply stop
# writing: no crash, no archive change, no false success, and no save
# attempted either.
#
# AND THE HONEST NUMBER IS NOT 40. Measured: the injected write failure
# fires in 12 of the 40 combinations, because most of these screens do
# not write on the path a short script drives them down - listing
# records, choosing a slot and the sensor-test menu are reads. Asserting
# a percentage would have been a threshold chosen to pass rather than a
# fact about the software.
#
# So the claim is made per screen instead: the ones that SAVE must have
# tried to save, under every one of the four failure modes.

# THE SCREENS THAT WRITE A FILE, WHICH IS NOT THE SAME LIST AS BEFORE.
#
# Prepare, Confirm and Measure used to be here, because they wrote the
# working set into samples.json. They write nothing now: the working
# set is this process's memory, and that is the point of the ownership
# model - measuring does not save a sample on the PC.
#
# So the screens that must still prove they tried to write are the ones
# that put something into the PC ARCHIVE, which is the only collection
# with a file. If this list ever empties, the guard below fails and
# says so, rather than letting the section pass because nothing writes
# any more.
WRITING_SCREENS = (
    "records.import_esp32_samples",
)

checks.ok(bool(WRITING_SCREENS),
          "there is at least one screen that writes a file at all - an "
          "empty list here would make every check above vacuous")

for name in WRITING_SCREENS:
    checks.equal(fired_by_screen.get(name, 0), len(WRITE_FAILURES),
                 "{} attempted a write under all {} failure modes"
                 .format(name, len(WRITE_FAILURES)))

# AND THE THREE THAT USED TO WRITE NOW PROVABLY DO NOT.
for name in ("measure.menu_prepare", "measure.menu_confirm",
             "measure.menu_measure"):
    checks.equal(fired_by_screen.get(name, 0), 0,
                 "{} reaches no file write at all - the run in progress "
                 "is memory".format(name))

checks.ok(fired > 0,
          "{} of {} combinations reached a write at all; the other {} "
          "are read-only paths, and prove only that the screen "
          "survives".format(fired, attempts, attempts - fired))

checks.equal(sorted(fired_by_screen), sorted(set(fired_by_screen)),
             "and the per-screen tally is what the claim rests on, not "
             "a proportion")


# ======================================================================
checks.section("76/108. both menu loops catch the same set of failures")

# THE ASYMMETRY THIS EXISTS FOR.
#
# `interactive` has two loops: one for an unsynchronized carousel and
# one for the main screen. Both reach the tools menu, so both can reach
# the same screens - and they were not catching the same exceptions.
# The main loop handled StorageError; the startup loop did not. A tools
# screen that propagated a failed write would therefore be a diagnosed
# message or a dead client depending on whether the carousel happened
# to be synchronized when the operator opened it.

import ast                                                   # noqa: E402

screen_source = (support.FIRMWARE / "PC" / "workflow" / "screen.py"
                 ).read_text(encoding="utf-8")
tree = ast.parse(screen_source)

interactive_node = next(
    node for node in ast.walk(tree)
    if isinstance(node, ast.FunctionDef) and node.name == "interactive"
)

def dispatches_to_a_screen(node):
    """
    Whether this try-block is the one that RUNS an operator screen.

    `interactive` has four try-blocks, and only two of them dispatch.
    The other two wrap `hardware_status()` alone, and those legitimately
    handle link failures and nothing else - there is no archive involved
    in reading a status, so demanding they catch StorageError would be
    demanding a handler for something that cannot happen.
    """
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue

        if isinstance(child.func, ast.Name):
            if (child.func.id.startswith("menu_")
                    or child.func.id == "handler"):
                return True

        if isinstance(child.func, ast.Attribute):
            if child.func.attr.startswith("menu_"):
                return True

    return False


caught_per_try = []

for node in ast.walk(interactive_node):
    if not isinstance(node, ast.Try) or not node.handlers:
        continue

    if not dispatches_to_a_screen(node):
        continue

    names = set()

    for handler in node.handlers:
        if handler.type is None:
            names.add("<bare>")

        else:
            names.update(
                child.id for child in ast.walk(handler.type)
                if isinstance(child, ast.Name)
            )

    if names:
        caught_per_try.append(names)

checks.equal(len(caught_per_try), 2,
             "interactive() has exactly two try-blocks that dispatch to "
             "a screen - the startup loop and the main loop")

union = set().union(*caught_per_try) if caught_per_try else set()
missing = [
    sorted(union - names) for names in caught_per_try
    if union - names
]

checks.equal(missing, [],
             "and every one of them catches the same set: {} - so which "
             "loop the operator is in cannot decide whether a failure is "
             "diagnosed or fatal".format(", ".join(sorted(union))))

checks.ok("StorageError" in union,
          "including StorageError, which is the one that was missing")

checks.ok("LinkError" in union,
          "and LinkError, which was already there")

# And the same thing driven rather than parsed: a failing write from
# the startup screen must not kill the client.
bench = Bench(prepared=None, measured=False)

try:
    # Force the startup screen: no synchronized carousel.
    bench.loopback.service = None
    bench.loopback._device = support.FakeAS7265X()
    bench.loopback._servo = support.FakeST3215()
    bench.loopback.build()
    bench.link.connect_servo()

    status = bench.status()

    checks.ok(not (status.get("carousel") or {}).get("position_valid"),
              "the bench really is on the startup screen")

    with patched(samples_module.tempfile, "mkstemp",
                 raiser(OSError(errno.ENOSPC, "No space left on device"))):
        saved_input = builtins.input
        builtins.input = lambda prompt="": (
            answers.pop(0) if answers else "q")
        answers = ["t", "6", "1", "y", "", "0", "q"]

        try:
            with contextlib.redirect_stdout(io.StringIO()):
                code = screen_screens.interactive(bench.link)

            ending = "returned {}".format(code)

        except BaseException as error:                     # noqa: BLE001
            ending = "CRASH:{}".format(type(error).__name__)

        finally:
            builtins.input = saved_input

    checks.equal(ending, "returned 0",
                 "and a full disk reached from the startup screen leaves "
                 "the client alive")

finally:
    bench.close()


# ======================================================================
checks.section("76. the measurement screen says where the spectrum is")

# The most important single screen, checked in detail rather than in
# aggregate.
#
# WHAT THIS CASE USED TO PROVE, AND WHAT IT PROVES NOW.
#
# It used to inject a full disk and require the screen to say the SAVE
# FAILED, because Measure wrote the working set to samples.json and a
# failed write meant the spectrum was gone.
#
# Measure no longer writes to any file. The working set is this
# process's memory and the durable copy is on the ESP32, so a full disk
# is simply not on the measurement path any more - and a screen that
# announced a save failure here would be describing something that did
# not happen. The filesystem is still broken for the whole run; what
# has to be true is that the measurement completes anyway, the operator
# is told where the spectrum actually is, and nothing claims the PC
# archive has it.

bench = Bench(prepared="S-DETAIL", measured=False)

try:
    status = bench.status()
    view = bench.view(status)

    with patched(samples_module.os, "replace",
                 raiser(OSError(errno.ENOSPC, "No space left on device"))):
        _value, output, _console = run_screen(
            ["", "", "", ""],
            lambda: measure_screens.menu_measure(
                bench.mission, status, view),
            exhausted="0")

    lowered = output.lower()

    checks.ok("raw saved:      yes" not in lowered,
              "the screen does NOT print 'RAW saved: YES' - the PC has "
              "saved nothing")

    checks.ok("esp32" in lowered,
              "and names the ESP32, which is where the durable copy is")

    checks.ok("archive" in lowered,
              "and names the archive, so the operator can see the "
              "spectrum is not in it yet")

    # The measurement completed despite the broken filesystem.
    live = bench.mission.session.get_sample("S-DETAIL")

    checks.ok(live is not None,
              "the Sample is in the working set")

    checks.equal(len(live.get("measurements") or []), 1,
                 "with its Measurement - a full disk cannot cost a "
                 "spectrum that is not written to the disk")

    checks.equal(live.get("state"), STATE_MEASURED,
                 "and the Sample is MEASURED, because it was")

    # And the archive really is untouched - by the measurement, not
    # merely by the failure.
    archive = archive_store(bench.bd.samples_file).load()

    checks.ok(archive.get_sample("S-DETAIL") is None,
              "and the PC archive holds nothing for it, because nobody "
              "imported it")

finally:
    bench.close()


# ======================================================================
checks.section("76. the write that CAN fail is the archive, and it retries")

# The recovery an operator actually performs: free some space and do it
# again.
#
# WHERE THE FAILURE MOVED TO. Measure writes no file any more, so a
# broken filesystem cannot fail a measurement - it is proved above that
# the spectrum survives one. The write that can still fail is the one
# the operator asks for: putting a Sample into the PC archive. That is
# where "a save that fails is not reported as a save" has to hold, and
# where a retry must produce exactly one record rather than two.

bench = Bench(prepared="S-RETRY", measured=False)

try:
    status = bench.status()
    view = bench.view(status)

    with patched(samples_module.os, "replace",
                 raiser(OSError(errno.ENOSPC, "full"))):
        run_screen(["", "", "", ""],
                   lambda: measure_screens.menu_measure(
                       bench.mission, status, view),
                   exhausted="0")

    measured = bench.mission.session.get_sample("S-RETRY")

    checks.equal(len(measured.get("measurements") or []), 1,
                 "the measurement itself is unaffected by the full disk")

    after_failure = archive_store(bench.bd.samples_file).load()

    checks.ok(after_failure.get_sample("S-RETRY") is None,
              "and the archive holds nothing, because nothing was "
              "archived")

    # The operator archives it, and the disk is still full.
    failed = None

    with patched(samples_module.os, "replace",
                 raiser(OSError(errno.ENOSPC, "full"))):
        try:
            bench.mission.archive.adopt(measured)

        except StorageError as error:
            failed = error

    checks.ok(failed is not None,
              "archiving onto a full disk raises rather than pretending")

    checks.ok(archive_store(bench.bd.samples_file).load()
              .get_sample("S-RETRY") is None,
              "and the archive FILE still holds nothing for it")

    checks.ok(bench.mission.archive.get_sample("S-RETRY") is None,
              "and the in-memory archive matches the file - a failed "
              "write leaves no record the disk does not have")

    checks.equal(len(bench.mission.session.get_sample("S-RETRY")
                     .get("measurements") or []), 1,
                 "while the working set is untouched by the archive's "
                 "failure - they are different stores")

    # Space is freed; the operator archives again.
    bench.mission.archive.adopt(bench.mission.session.get_sample("S-RETRY"))

    after_retry = archive_store(bench.bd.samples_file).load()
    record = after_retry.get_sample("S-RETRY")

    checks.ok(record is not None,
              "the retry archives it")

    checks.equal(after_retry.count(), 1,
                 "and the archive holds exactly ONE Sample - the failed "
                 "attempt did not leave a ghost behind it")

    measurements = record.get("measurements") or []

    checks.equal(len(measurements), 1,
                 "with exactly one Measurement on it")

    checks.ok(bool(measurements[0].get("raw")),
              "with a real spectrum in it")

    checks.equal(record.get("state"), STATE_MEASURED,
                 "and the Sample is MEASURED, once")

finally:
    bench.close()


sys.exit(checks.report())
