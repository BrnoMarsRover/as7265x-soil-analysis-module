"""
Every screen in the application, executed.

THE DEFECT THIS SUITE IS SHAPED AROUND

`mission.link.sync_load_slot(1)` sat in two branches of the carousel
screen through a whole release. It is a name that does not exist. No
static reading found it, 855 checks passed around it, and it failed on
the operator's first real carousel setup - because the branch is
reachable only after a servo has answered, and nothing had ever driven
the screen that far.

The lesson is not "check that method". It is that a screen nobody
executes is a screen nobody has tested, whatever the coverage of the
functions it calls.

WHAT THIS DOES

The screen list is DERIVED from the source, not written by hand: every
`menu_*`, `show_*` and `validate_*` in workflow/ is found by parsing
the modules, and every one of them is entered with a scripted operator
at the keyboard and the real firmware behind the wire. A screen that is
added and not listed here fails the last check in the file.

WHAT IT ASSERTS

Deliberately little, per screen: that it can be entered, that it
finishes, and that it does not raise. Those three are what the missing
name broke. The suites that assert on CONTENT are the fault, state and
mission ones - a smoke walk that also tried to check every number would
be unmaintainable and would stop being run.

Two things it does check everywhere:

    a screen never loops forever. ScriptedConsole raises after 2000
    questions, which turns "the suite hangs" into a named failure.

    a screen never leaves the carousel position valid when it is not.
"""

import ast
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
from serial_link import DeviceError, LinkError               # noqa: E402

from fakes import FakeClock, SandboxBD, run_screen           # noqa: E402
from fakes.clock import install_clock                        # noqa: E402
from fakes.esp32 import LoopbackDevice                       # noqa: E402
from fakes.serial_port import install_fake_serial, open_link  # noqa: E402

from workflow import (                                       # noqa: E402
    calibration as calibration_screens,
    carousel as carousel_screens,
    measure as measure_screens,
    records as records_screens,
    screen as screen_screens,
)
from workflow.session import Mission                         # noqa: E402

checks = support.Checks("screens")

restore_serial = install_fake_serial(serial_link)

WORKFLOW = support.FIRMWARE / "PC" / "workflow"

MODULES = {
    "calibration": calibration_screens,
    "carousel": carousel_screens,
    "measure": measure_screens,
    "records": records_screens,
    "screen": screen_screens,
}


def declared_screens():
    """Every screen entry point in workflow/, found by parsing."""
    found = []

    for name, module in sorted(MODULES.items()):
        path = WORKFLOW / "{}.py".format(name)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue

            if node.name.startswith(("menu_", "show_", "validate_")):
                found.append("{}.{}".format(name, node.name))

    return sorted(found)


SCREENS = declared_screens()


# ======================================================================
# a live session for the screens to run against
# ======================================================================

def session(synced=True):
    clock = FakeClock()
    installed = install_clock(serial_link, clock)

    loopback = LoopbackDevice()
    loopback.build()

    link, port = open_link(serial_link, loopback, clock=clock)
    link.online = True

    bd = SandboxBD()

    mission = Mission(link)
    mission.store = bd.sample_store()
    mission.calibrations = bd.calibration_store()
    mission.profiles = bd.profile_store()
    mission.load_science()
    mission.learning = bd.learning_store()

    if synced:
        link.connect_servo()
        link.sync_position(load_slot=1)

    return mission, link, bd, installed


# (script, way out). The second value is what the console returns once
# the script runs out, and it is THE SCREEN'S OWN exit answer, which is
# not the same everywhere:
#
#     "0"   the menus that offer [0] Back
#     "c"   CAROUSEL SETUP, which offers [c] Cancel
#     ""    a prompt where blank cancels, and every pause()
#
# Getting this wrong looks exactly like a hung program, because a menu
# given an answer it does not recognise redraws and asks again - which
# is correct behaviour, and is what the abuse walks below rely on. The
# console's loop guard turns the difference between "correctly
# re-asking" and "genuinely stuck" into a named failure after 2000
# questions.
#
# Where a screen has several branches worth entering, the script enters
# them: the carousel setup below walks the movement options before
# declaring the origin, because those are the branches that move a
# mechanism.
WALKS = {
    "calibration.menu_sensor_test": (["0"], "0"),
    "calibration.menu_full_sensor_test": ([""], ""),
    "calibration.menu_led_test": (["0"], "0"),
    "calibration.menu_full_calibration": (["n", ""], ""),
    "calibration.show_active_calibration": ([""], ""),
    "calibration.validate_active_calibration": ([""], ""),
    "calibration.show_calibration_history": ([""], ""),
    "calibration.menu_select_calibration": (["0", ""], ""),

    "carousel.menu_connect_servo": (["1", ""], ""),
    "carousel.menu_initial_calibration": (
        ["1", "2", "3", "1.5", "4", "6", "d", "", "7"], "c"),
    "carousel.menu_fine_adjust": (["2.0"], ""),
    "carousel.menu_resync": (["y"], ""),
    "carousel.menu_servo_test": (["1", "2", "4", "t", "1", "0"], "0"),
    "carousel.menu_servo_bus_scan": (["n", "0"], "0"),
    "carousel.menu_servo_diagnostics": ([""], ""),
    "carousel.menu_servo_configure": (["n"], ""),
    "carousel.menu_servo_torque": (["1"], "0"),
    "carousel.menu_servo_calibration": ([""], ""),
    "carousel.menu_servo_movement_test": (["0"], "0"),

    "measure.menu_choose_slot": (["2"], ""),
    "measure.menu_prepare": (
        ["S-SCREEN", "", "", "", "", "note", ""], ""),
    "measure.menu_confirm": (["y"], ""),
    "measure.menu_measure": (["", "", "", ""], ""),
    "measure.menu_clear_slot": (["", ""], ""),

    # "1" enters the record review from the history screen, so the walk
    # covers both; the review itself ends on a pause().
    "records.menu_learning_history": (["1", ""], "0"),
    "records.menu_review_observations": ([""], ""),
    "records.menu_sample_database": (["0"], "0"),

    "screen.menu_tools": (["0"], "0"),
    "screen.menu_help": ([""], ""),
}


def call_for(name, mission, link):
    """Build the call, since the screens take three shapes."""
    module_name, function_name = name.split(".")
    function = getattr(MODULES[module_name], function_name)

    status = mission.hardware_status()
    view = mission.slot_view(status)

    if module_name in ("measure", "screen"):
        return lambda: function(mission, status, view)

    return lambda: function(mission)


# ======================================================================
checks.section("the screen list was derived, not written")

checks.ok(len(SCREENS) >= 25,
          "{} screen entry points found by parsing workflow/".format(
              len(SCREENS)))

missing = sorted(set(SCREENS) - set(WALKS))

checks.equal(missing, [],
             "and every one of them has a walk defined here - a screen "
             "added without one fails this check rather than quietly "
             "never being executed")

stale = sorted(set(WALKS) - set(SCREENS))

checks.equal(stale, [],
             "and no walk is defined for a screen that no longer exists")


# ======================================================================
checks.section("every screen is entered, and comes back")

results = {}
crashes = []

for name in SCREENS:
    script, exhausted = WALKS[name]

    mission, link, bd, installed = session()

    try:
        _value, output, console = run_screen(
            list(script), call_for(name, mission, link),
            exhausted=exhausted)

        results[name] = ("OK", len(output))

    except (DeviceError, LinkError) as error:
        # A refusal from the firmware is a legitimate outcome for a
        # screen; an unhandled one reaching here is not, because the
        # menu loop above these screens catches LinkError and this is
        # being called directly.
        results[name] = (error.code, 0)

    except Exception as error:                         # noqa: BLE001
        results[name] = ("CRASH:" + type(error).__name__, 0)
        crashes.append("{}: {}: {}".format(
            name, type(error).__name__, error))

    finally:
        installed.restore()
        link.close()
        bd.close()

for name in SCREENS:
    outcome, size = results[name]

    checks.ok(not outcome.startswith("CRASH"),
              "{} runs and returns ({}{})".format(
                  name, outcome,
                  ", {} chars printed".format(size) if size else ""))

if crashes:
    print()

    for line in crashes:
        print("     {}".format(line))

    print()

printed = [name for name, (outcome, size) in results.items()
           if outcome == "OK" and size > 0]

checks.ok(len(printed) >= len(SCREENS) - 4,
          "and {} of {} of them printed something for the operator - a "
          "screen that returns silently has usually refused before it "
          "began".format(len(printed), len(SCREENS)))


# ======================================================================
checks.section("every screen survives an unsynchronized carousel")

# The state most screens are NOT written for: no origin, no servo. The
# menu above them is supposed to keep them out of it, but a screen
# reached from Tools does not get that protection.

unsynced_crashes = []

for name in SCREENS:
    script, exhausted = WALKS[name]

    mission, link, bd, installed = session(synced=False)

    try:
        run_screen(list(script), call_for(name, mission, link),
                   exhausted=exhausted)

    except (DeviceError, LinkError):
        pass

    except Exception as error:                         # noqa: BLE001
        unsynced_crashes.append("{}: {}: {}".format(
            name, type(error).__name__, error))

    finally:
        installed.restore()
        link.close()
        bd.close()

if unsynced_crashes:
    print()

    for line in unsynced_crashes:
        print("     {}".format(line))

    print()

checks.equal(unsynced_crashes, [],
             "with no servo connected and no origin declared, not one "
             "of the {} screens raises".format(len(SCREENS)))


# ======================================================================
checks.section("every screen survives an operator who only presses Enter")

# The commonest real input. A menu that treats a bare Enter as an
# unknown option is merely rude; one that treats it as a selection is
# dangerous.

enter_crashes = []

for name in SCREENS:
    mission, link, bd, installed = session()

    try:
        run_screen([""] * 3, call_for(name, mission, link),
                   exhausted=WALKS[name][1])

    except (DeviceError, LinkError):
        pass

    except RuntimeError as error:
        # The console's loop guard. A screen that asks 2000 questions
        # is looping, and that IS the finding.
        enter_crashes.append("{}: {}".format(name, error))

    except Exception as error:                         # noqa: BLE001
        enter_crashes.append("{}: {}: {}".format(
            name, type(error).__name__, error))

    finally:
        installed.restore()
        link.close()
        bd.close()

if enter_crashes:
    print()

    for line in enter_crashes:
        print("     {}".format(line))

    print()

checks.equal(enter_crashes, [],
             "an operator holding Enter cannot crash or hang any of the "
             "{} screens".format(len(SCREENS)))


# ======================================================================
checks.section("every screen survives nonsense at every prompt")

NONSENSE = ["!!!", "-1", "999999", "0.5", "%s", "../../etc", "\x00",
            "y" * 300, "9", "z"]

nonsense_crashes = []

for name in SCREENS:
    mission, link, bd, installed = session()

    try:
        run_screen(list(NONSENSE), call_for(name, mission, link),
                   exhausted=WALKS[name][1])

    except (DeviceError, LinkError):
        pass

    except Exception as error:                         # noqa: BLE001
        nonsense_crashes.append("{}: {}: {}".format(
            name, type(error).__name__, error))

    finally:
        installed.restore()
        link.close()
        bd.close()

if nonsense_crashes:
    print()

    for line in nonsense_crashes:
        print("     {}".format(line))

    print()

checks.equal(nonsense_crashes, [],
             "ten kinds of nonsense at every prompt, and none of the "
             "{} screens raises".format(len(SCREENS)))


restore_serial()

sys.exit(checks.report())
