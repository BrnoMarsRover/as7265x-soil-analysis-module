"""
The production measurement and the Sensor + Analysis Test must not drift.

THE DEFECT THIS SUITE EXISTS FOR

Both screens render the same object - an AnalysisRun - and each had its
own code for it. They had already diverged:

    the sensor test printed DATABASE COMPARISON; the production
    measurement printed nothing at all about what DB1, DB2 and DB3 had
    each said

    the sensor test printed the FULL SPECTRAL DATA table; the
    production measurement printed a "RAW, BY ILLUMINATION" one

    neither printed which calibration the numbers came from

So an operator running a REAL measurement saw strictly less about it
than one running a bench test of the same hardware, and no test could
have noticed, because each screen was checked against itself.

WHAT IS ASSERTED

    STRUCTURAL   both screens call `display.print_science_result`, and
                 neither reimplements the blocks it owns. Checked by
                 parsing the source, so a future edit that copies the
                 block back into one screen fails here.

    BEHAVIOURAL  the same AnalysisRun through both paths produces the
                 same science sections, section for section.

The production screen still prints its own MISSION block - Sample,
slot, measurement id, movement, what was persisted where. That is the
part a sensor test genuinely does not have, and it is asserted to be
present rather than assumed.

Run:  py test_science_display.py
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

import support                                              # noqa: E402

support.add_project_root()
support.add_path("PC")

import serial_link                                          # noqa: E402

from fakes import run_screen                                # noqa: E402
from fakes.clock import FakeClock, install_clock            # noqa: E402
from fakes.esp32 import loopback_link                       # noqa: E402
from fakes.serial_port import install_fake_serial           # noqa: E402
from fakes.storage import sandbox_mission                   # noqa: E402

from workflow import calibration as calibration_screens     # noqa: E402
from workflow import display                                # noqa: E402
from workflow import measure as measure_screens             # noqa: E402

checks = support.Checks("science-display")

restore_serial = install_fake_serial(serial_link)

WORKFLOW = support.FIRMWARE / "PC" / "workflow"


# ----------------------------------------------------------------------
# the structural half: neither screen may reimplement the science block
# ----------------------------------------------------------------------

def calls_in(path, function_name):
    """Every function name called inside one top-level function."""
    tree = ast.parse(path.read_text(encoding="utf-8"))

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue

        if node.name != function_name:
            continue

        found = set()

        for call in ast.walk(node):
            if isinstance(call, ast.Call):
                target = call.func

                if isinstance(target, ast.Name):
                    found.add(target.id)

                elif isinstance(target, ast.Attribute):
                    found.add(target.attr)

        return found

    return None


# The blocks `print_science_result` owns. A screen that calls one of
# these directly has started rendering the science itself again, which
# is exactly how the two drifted apart the first time.
OWNED_BY_THE_RENDERER = (
    "print_settings_block",
    "print_triad_table",
    "print_quality",
    "print_database_results",
    "print_evidence_summary",
    "print_decision",
)

SCREENS = (
    ("the production measurement",
     WORKFLOW / "measure.py", "menu_measure"),
    ("the Sensor + Analysis Test",
     WORKFLOW / "calibration.py", "menu_full_sensor_test"),
)

# ======================================================================
checks.section("both screens render through the ONE renderer")

for label, path, function_name in SCREENS:
    called = calls_in(path, function_name)

    checks.ok(called is not None,
              "{} was found in {}".format(function_name, path.name))

    if called is None:
        continue

    checks.ok("print_science_result" in called,
              "{} calls print_science_result".format(label))

    reimplemented = sorted(
        name for name in OWNED_BY_THE_RENDERER if name in called
    )

    checks.equal(
        reimplemented, [],
        "{} does not render any science block itself - a copy of one "
        "here is how the two screens diverged before".format(label))


# ======================================================================
checks.section("the renderer owns every science block")

renderer = calls_in(WORKFLOW / "display.py", "print_science_result")

checks.ok(renderer is not None, "print_science_result exists")

for name in OWNED_BY_THE_RENDERER:
    checks.ok(name in (renderer or set()),
              "print_science_result renders {}".format(name))


# ======================================================================
checks.section("the mission block stays with the mission")

measure_calls = calls_in(WORKFLOW / "measure.py", "menu_measure") or set()

checks.ok("report_return_move" in measure_calls,
          "the production screen still reports the carousel movement - "
          "that is mission information and does not belong in a "
          "renderer shared with a bench test")


# ----------------------------------------------------------------------
# the behavioural half: the same run, through both paths
# ----------------------------------------------------------------------

install_clock(serial_link, FakeClock())
link, fake, loop = loopback_link(serial_link)
mission, bd = sandbox_mission(link)

link.request("connect_servo")
link.request("sync_position", load_slot=1)

# ---- the sensor test -------------------------------------------------
_value, sensor_output, _console = run_screen(
    ["", "3", ""],
    lambda: calibration_screens.menu_full_sensor_test(mission),
)

# ---- a production measurement ---------------------------------------
status = link.get_status()
view = mission.slot_view(status)

run_screen(["S-DISPLAY"] + [""] * 8,
           lambda: measure_screens.menu_prepare(mission, status, view))

status = link.get_status()
view = mission.slot_view(status)

run_screen(["y"], lambda: measure_screens.menu_confirm(
    mission, status, view))

status = link.get_status()
view = mission.slot_view(status)

_value, production_output, _console = run_screen(
    ["", "", "3"],
    lambda: measure_screens.menu_measure(mission, status, view),
)


# ======================================================================
checks.section("every science section appears on BOTH screens")

SECTIONS = (
    ("CALIBRATION", "which calibrations the numbers were derived under"),
    ("SETTINGS", "what the sensor was set to"),
    ("MEASUREMENT QUALITY", "whether the measurement is usable"),
    ("FULL SPECTRAL DATA", "the raw and reflectance table"),
    ("DATABASE COMPARISON", "what each library says, separately"),
    ("DECISION MODEL", "what the model concluded"),
)

for section, why in SECTIONS:
    checks.ok(section in sensor_output,
              "sensor test shows {} ({})".format(section, why))
    checks.ok(section in production_output,
              "production measurement shows {} ({})".format(section, why))


# ======================================================================
checks.section("all three databases are reported, separately, on both")

for database in ("DB1", "DB2", "DB3"):
    checks.ok(database in sensor_output,
              "sensor test names {}".format(database))
    checks.ok(database in production_output,
              "production measurement names {}".format(database))

for metric in ("cosine", "rmse", "pearson_r"):
    checks.ok(metric in production_output,
              "the production database block reports {}".format(metric))

checks.ok("margin" in production_output,
          "and the margin beside every winner - a large score with a "
          "tiny margin is not an identification")

checks.ok("HIGHER is better" in production_output
          and "LOWER is better" in production_output,
          "with the direction of each metric spelled out, so 0.0618 RMSE "
          "can be read without the source code")


# ======================================================================
checks.section("the production screen keeps its mission block")

# "RAW saved:" became "RAW:" when it stopped being true. Measure
# writes no PC file; the line now says the acquisition is retained on
# the ESP32 and is not in the archive until it is imported.
for line in ("Sample ID:", "Physical slot:", "Measurement:",
             "AnalysisRun:", "RAW:", "Home position:"):
    checks.ok(line in production_output,
              "production measurement still reports {}".format(line))

    checks.ok(line not in sensor_output,
              "and the sensor test does not - it has no {}".format(
                  line.rstrip(":").lower()))


# ======================================================================
checks.section("one renderer, one output - compared block by block")


# EVERY heading the science half emits, in the order it emits them.
# `SECTIONS` above is what must be PRESENT; this is what closes one
# section and opens the next, and it has to include the two the
# renderer prints from inside its sub-blocks - MEASUREMENT from the
# evidence summary and DECISION MODEL from the decision - or the
# database block would swallow everything after it.
BOUNDARIES = (
    "CALIBRATION",
    "SETTINGS",
    "MEASUREMENT QUALITY",
    "FULL SPECTRAL DATA",
    "DATABASE COMPARISON",
    "MEASUREMENT",
    "DECISION MODEL",
)


def _boundary(line):
    stripped = line.strip()

    for name in BOUNDARIES:
        if stripped == name or stripped.startswith(name + "  "):
            return name

    return None


def science_sections(text):
    """The science half of a screen, keyed by section heading."""
    sections = {}
    current = None

    for line in text.splitlines():
        heading = _boundary(line)

        if heading is not None:
            current = heading
            sections.setdefault(current, [])

        elif current is not None:
            sections[current].append(line.rstrip())

    return sections


def labels(lines):
    return [
        line.split(":")[0].strip()
        for line in lines
        if ":" in line and line.strip()
    ]


sensor_sections = science_sections(sensor_output)
production_sections = science_sections(production_output)

checks.equal(sorted(sensor_sections), sorted(production_sections),
             "both screens produce exactly the same set of science "
             "sections")

# The two acquisitions are different, so the NUMBERS differ. What must
# match is the SHAPE: the same labels, in the same order.
#
# DECISION MODEL is the LAST section, so what follows it on each screen
# is that screen's own epilogue - the disposition menu on one, ground
# truth capture on the other - and comparing those would be comparing
# the mission halves this split exists to keep apart. Its own labels
# are asserted separately below.
for section in BOUNDARIES[:-1]:
    if (section not in sensor_sections
            or section not in production_sections):
        continue

    checks.equal(
        labels(sensor_sections[section]),
        labels(production_sections[section]),
        "{}: the same labels, in the same order, on both screens".format(
            section),
    )

for screen_name, sections in (("sensor test", sensor_sections),
                              ("production", production_sections)):
    decision_labels = labels(sections.get("DECISION MODEL", []))

    for expected in ("Level", "Confidence"):
        checks.ok(expected in decision_labels,
                  "{}: the decision block reports {}".format(
                      screen_name, expected))


# ======================================================================
checks.section("[w] Why? explains the model's real decision path")

result = mission.analyse_acquisition(link.sensor_test_raw())
decision = result.get("decision") or {}

checks.ok(decision.get("gates"),
          "the decision records the gates it walked")
checks.ok(decision.get("confidence_reasons") is not None,
          "and the penalties behind its confidence")
checks.ok((decision.get("evidence") or {}).get("coverage"),
          "and how much measurement it rests on")

for candidate in decision.get("candidates") or []:
    checks.ok(candidate.get("per_database") is not None,
              "candidate {} carries its per-database support, so 'why "
              "this and not that' is answerable".format(
                  candidate.get("material")))

    break

_value, why_output, _console = run_screen(
    ["w", ""], lambda: display.offer_decision_detail(result))

for block in ("EVIDENCE COVERAGE", "DATABASE CONTRIBUTIONS",
              "CANDIDATE EVIDENCE", "DECISION GATES", "CONFIDENCE",
              "READING THE NUMBERS", "PROVENANCE"):
    checks.ok(block in why_output,
              "[w] Why? shows {}".format(block))

gate_names = [gate["gate"] for gate in decision.get("gates") or []]

for gate in ("MEASUREMENT_USABLE", "MATERIAL_LEVEL", "FAMILY_LEVEL",
             "AMBIGUOUS_SET"):
    checks.ok(gate in gate_names,
              "the ladder recorded the {} gate".format(gate))
    checks.ok(gate in why_output,
              "and [w] Why? shows it")

verdicts = {gate["verdict"] for gate in decision.get("gates") or []}

checks.ok(verdicts <= {"PASS", "FAIL", "NOT_REACHED"},
          "every gate is PASS, FAIL or NOT_REACHED - a gate the ladder "
          "never reached must not be reported as one that failed")

for gate in decision.get("gates") or []:
    checks.ok(gate.get("detail"),
              "the {} gate says WHY, not just what".format(gate["gate"]))


bd.close()
restore_serial()

sys.exit(checks.report())
