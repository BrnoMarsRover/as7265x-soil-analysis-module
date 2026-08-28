"""
The operator's whole journey, through the real client loop.

WHY THIS SUITE EXISTS, AND WHY THE EXISTING ONES DID NOT CATCH IT

Every screen here already had tests. They called a render function with
a status dictionary the test had written itself. Two defects lived
through all of that, because both live BETWEEN the tested functions:

    `status.servo_link` read `servo["selected"]` - a key the firmware
    has never sent. The fixtures contained it, so reader and fixture
    agreed and neither agreed with the board. In production the
    function returned NOT SELECTED over an ST3215 that was answering,
    `print_servo_block` printed the literal word "None" instead of all
    the servo telemetry, and Carousel Setup told every operator that no
    servo was connected.

    A failed measurement printed a correct compact block and RETURNED.
    The main loop's next iteration saw `position_valid == False` and
    drew the context-free startup screen, so the sample, the slot, the
    stage and the missing spectrum were gone one keypress after they
    appeared. Nothing rendered anything wrong.

So these tests drive `screen.interactive()` itself with scripted keys,
against the real firmware behind a fake wire, and assert on what the
OPERATOR ends up looking at.
"""

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

import support                                                # noqa: E402

support.add_project_root()
support.add_path("PC")

from fakes.operator import (OperatorBench,                    # noqa: E402
                            stuck_encoder, stuck_on_return)

import contextlib                                             # noqa: E402
import io as _io                                              # noqa: E402


def captured(call):
    """Whatever a screen printed."""
    with contextlib.redirect_stdout(_io.StringIO()) as out:
        call()

    return out.getvalue()

checks = support.Checks("operator-flow")


# ======================================================================
checks.section("the servo status the operator is shown is the board's")

with OperatorBench() as bench:
    bench.bring_up()
    status = bench.status()

    from workflow import status as ui_status                  # noqa: E402
    from workflow.display import print_servo_block            # noqa: E402

    servo = status.get("servo") or {}

    # THE CONTRACT, ASSERTED AGAINST THE REAL FIRMWARE. This is the
    # check whose absence let `selected` survive: every fixture in the
    # tree contained the key, so nothing ever compared it to a board.
    checks.ok("selected" not in servo,
              "the firmware sends NO 'selected' key - any reader that "
              "tests one is testing a constant")

    checks.ok(servo.get("connected") is True,
              "it sends 'connected', and a connected servo says True")

    checks.equal(ui_status.servo_link(status), ui_status.ONLINE,
                 "so an answering servo reads ONLINE - it read NOT "
                 "SELECTED for the whole of 6.0.0")

    checks.ok(ui_status.servo_online(status),
              "and servo_online agrees")

    checks.equal(ui_status.recovery_action(status), (None, False),
                 "a healthy servo needs no recovery action - this "
                 "permanently answered 'Connect Servo / Carousel Setup'")

    printed = captured(lambda: print_servo_block(servo))

    checks.ok("Mode:" in printed and "Voltage:" in printed,
              "and the servo telemetry block actually renders - it "
              "returned after printing the label and the word 'None'")

    checks.ok("None" not in printed.splitlines()[1],
              "the second line is telemetry, not the literal None")

    bench.link.disconnect_servo()
    off = bench.status()

    checks.equal(ui_status.servo_link(off), ui_status.NOT_SELECTED,
                 "and a servo that really is disconnected still reads "
                 "NOT SELECTED, so the fix did not just invert the bug")


# ======================================================================
checks.section("TEST 1 - a servo mismatch during a measurement")

with OperatorBench(servo=stuck_encoder()) as bench:
    bench.bring_up().loaded_sample()
    servo = bench.loopback._servo

    run = bench.run(["4", "0", "q"])          # measure, abort, quit

    checks.ok(run.error is None,
              "the client did not raise")

    checks.ok(run.shows("MEASUREMENT FAILED - MOVE_TO_SCANNER"),
              "the failure names the stage it reached")

    recovery = run.after("MEASUREMENT RECOVERY")

    checks.ok(bool(recovery),
              "and the operator lands in MEASUREMENT RECOVERY, not on "
              "the generic startup screen")

    checks.ok("s0007" in recovery and "Slot 1" in recovery,
              "which still names the sample and the slot")

    checks.ok("LOADED" in recovery,
              "and that the sample is still loaded")

    checks.ok("MOVE_TO_SCANNER" in recovery,
              "and which stage failed")

    checks.ok("Servo:      ONLINE" in recovery,
              "the servo is reported ONLINE - it never disconnected")

    checks.ok("POSITION UNKNOWN" in recovery,
              "while the carousel position is not trusted")

    checks.ok("NOT ACQUIRED" in recovery,
              "and no spectrum is claimed")

    checks.ok("Re-sync carousel" in recovery,
              "the offered recovery is re-sync")

    checks.ok("Retry" not in recovery and "retry" not in recovery,
              "and there is NO retry-the-movement option: the move is "
              "relative and may already have happened")

    checks.equal(len(servo.goals), 1,
                 "exactly one relative movement was ever transmitted")

    measurements = bench.mission.store.get_sample("s0007")["measurements"]

    checks.equal(len(measurements), 1,
                 "one measurement record was written")

    checks.equal(measurements[0]["acquisition_status"], "FAILED",
                 "and it is recorded as FAILED, never as a success")

    checks.ok(not (measurements[0].get("acquisition") or {}).get(
                  "illuminations"),
              "with no spectra in it")


# ======================================================================
checks.section("TEST 2 - the answer to a movement is destroyed")

with OperatorBench(servo=support.FakeST3215(drop_goal_ack=True)) as bench:
    bench.bring_up().loaded_sample()
    servo = bench.loopback._servo
    before = len(servo.goals)

    run = bench.run(["4", "0", "q"])

    checks.ok(run.error is None, "the client survives a lost answer")

    checks.equal(len(servo.goals) - before, 1,
                 "the movement was transmitted EXACTLY ONCE - the servo "
                 "acted and its acknowledgement was lost, and a resend "
                 "would have turned one half turn into two")

    recovery = run.after("MEASUREMENT RECOVERY")

    checks.ok(bool(recovery),
              "the operator is held in recovery")

    checks.ok("POSITION UNKNOWN" in recovery,
              "and the position is invalidated, because a lost answer "
              "is not evidence that nothing moved")


# ======================================================================
checks.section("TEST 3 - re-sync from recovery returns to the sample")

with OperatorBench(servo=stuck_encoder()) as bench:
    bench.bring_up().loaded_sample()

    #      measure  re-sync  aligned?  back-to-sample  pause  quit
    run = bench.run(["4", "2", "y", "5", "", "q"])

    checks.ok(run.error is None, "the journey completes")

    checks.ok(run.shows("Carousel synchronized"),
              "re-sync succeeds from inside recovery")

    after = run.after("Carousel synchronized")

    checks.ok("Selected:   Slot 1 / s0007 / LOADED" in after,
              "and the operator comes back to the SAME sample, still "
              "loaded - not to a menu that forgot what they were doing")

    checks.ok("[4] Measure Sample [AVAILABLE]" in after,
              "and measuring is offered again, because the position is "
              "trustworthy once more")


# ======================================================================
checks.section("TEST 4 - leaving recovery is the operator's decision")

with OperatorBench(servo=stuck_encoder()) as bench:
    bench.bring_up().loaded_sample()

    # Refresh, servo diagnostics, refresh again, and only THEN abort.
    run = bench.run(["4", "1", "1", "0", "q"])

    checks.ok(run.screens("MEASUREMENT RECOVERY") >= 3,
              "the recovery screen is redrawn for as long as the "
              "operator stays - {} times here".format(
                  run.screens("MEASUREMENT RECOVERY")))

    checks.ok(run.error is None, "and nothing forces them out")

    # The startup screen must appear only AFTER the abort, never before.
    first_root = run.text.find("Before working with samples")
    last_recovery = run.text.rfind("MEASUREMENT RECOVERY")

    checks.ok(first_root == -1 or first_root > last_recovery,
              "the generic root screen never appears while recovery is "
              "still running - which is exactly what used to happen")


# ======================================================================
checks.section("TEST 5 - the sensor is proved before anything moves")

with OperatorBench(sensor=support.FakeAS7265X(slaves_present=False)) as bench:
    bench.bring_up().loaded_sample()
    servo = bench.loopback._servo
    before = len(servo.goals)

    run = bench.run(["4", "0", "q"])

    checks.equal(len(servo.goals) - before, 0,
                 "a dead sensor means the carousel is NEVER commanded - "
                 "the sample is not stranded at the scanner")

    checks.ok(run.shows("PRECHECK"),
              "the failure names the PRECHECK stage")

    recovery = run.after("MEASUREMENT RECOVERY")

    checks.ok("s0007" in recovery,
              "the sample context survives a precheck failure too")

    checks.ok("Sensor diagnostics" in recovery,
              "and the sensor recovery action is offered")

    status = bench.status()

    checks.ok((status.get("carousel") or {}).get("position_valid"),
              "the carousel position is still TRUSTED - nothing moved, "
              "so nothing was invalidated")


# ======================================================================
checks.section("TEST 6 - the acquisition fails at the scanner")

with OperatorBench(sensor=support.FakeAS7265X(data_ready=False)) as bench:
    bench.bring_up().loaded_sample()

    run = bench.run(["4", "0", "q"])

    sample = bench.mission.store.get_sample("s0007")
    measurements = sample["measurements"]

    checks.ok(all(m["acquisition_status"] == "FAILED"
                  for m in measurements),
              "a failed acquisition is never stored as a success")

    for measurement in measurements:
        checks.ok(not (measurement.get("acquisition") or {}).get(
                      "illuminations"),
                  "and carries no spectra")

    status = bench.status()
    sensor = status.get("sensor") or {}

    checks.ok(not sensor.get("illumination"),
              "every lamp is off after the failure")


# ======================================================================
checks.section("TEST 7 - the spectrum survives a failed return")

with OperatorBench(servo=stuck_on_return()) as bench:
    bench.bring_up().loaded_sample()

    run = bench.run(["4"] + [""] * 3 + ["n", "0", "q"])

    checks.ok(run.shows("RETURN MOVEMENT FAILED"),
              "the failed return is reported")

    recovery = run.after("MEASUREMENT RECOVERY")

    checks.ok(bool(recovery),
              "and it too holds the operator in context - real science "
              "exists and the mechanism is lost, which is not the same "
              "situation as a failed measurement")

    checks.ok("ACQUIRED" in recovery,
              "the spectrum is reported as ACQUIRED, not as missing")

    checks.ok("POSITION UNKNOWN" in recovery,
              "while the carousel position is not trusted")

    sample = bench.mission.store.get_sample("s0007")
    measurement = sample["measurements"][-1]

    checks.equal(measurement["acquisition_status"], "SUCCESS",
                 "the measurement IS a success - the acquisition "
                 "happened and the data is real")

    checks.ok(bool((measurement.get("acquisition") or {}).get(
                   "illuminations")),
              "and the spectra are stored")


# ======================================================================
checks.section("TEST 8 - Activated Carbon into Soil, through the real UI")

# THE EXACT SCENARIO THE OPERATOR ASKED FOR, driven end to end: measure
# a sample, save known truth, choose Prepared mixture, FIND the material
# by typing part of its name, spike it into ordinary soil, and check
# what actually landed in the database.
MIXTURE_KEYS = ["4", "", "y", "3",       # measure, why?, save?, mixture
                "carbon", "1", "30",     # search, pick, 30 %
                "c", "Soil", "y",        # end components, matrix, confirm
                "1", "n", "", "q"]       # verified, no context, pause, quit

with OperatorBench() as bench:
    bench.bring_up().loaded_sample()

    run = bench.run(MIXTURE_KEYS)

    checks.ok(run.error is None, "the journey completes without raising")

    # -- discoverability -------------------------------------------
    checks.ok(run.shows("matches for 'carbon'"),
              "typing part of a name SEARCHES - it used to answer "
              "\"'carbon' is not a known material name, id or alias\"")

    search = run.after("matches for 'carbon'")
    carbon_at = search.find("Activated Carbon")
    black_at = search.find("Carbon Black")

    checks.ok(carbon_at != -1 and (black_at == -1 or carbon_at < black_at),
              "and the material ON THIS BENCH is ranked above the "
              "reference-catalogue one that merely starts with the word")

    checks.ok(run.shows("Activated Carbon                 30.00%"),
              "the review shows the component and its prepared percent")

    checks.ok(run.shows("[reference component]"),
              "labelled as a reference component")

    checks.ok(run.shows("Soil                             70.00%"),
              "the matrix and its remainder, which was inferred rather "
              "than asked for twice")

    checks.ok(run.shows("[matrix / no reference spectrum]"),
              "labelled as the matrix")

    checks.ok(run.shows("total                           100.00%"),
              "and the total is shown before anything is written")

    checks.ok(run.shows("Saved to the learning history as s0007/M001"),
              "it saves, under an id that names the sample")

    checks.ok("{}" not in run.after("Saved to the learning history"),
              "and the confirmation has no unformatted placeholder in it")

    # -- what was actually stored ----------------------------------
    store = bench.mission.learning
    truth = store.get_ground_truth("s0007/M001")

    checks.equal(truth["label_type"], "PREPARED_MIXTURE",
                 "stored as a PREPARED_MIXTURE")

    parts = {p["role"]: p for p in truth["mixture"]}

    checks.equal(sorted(parts), ["COMPONENT", "MATRIX"],
                 "with exactly one component and one matrix")

    component = parts["COMPONENT"]

    checks.equal(component["material_key"], "Activated Carbon",
                 "the component carries the canonical material key")

    checks.equal(component["material_id"], "activated_carbon",
                 "and its canonical material id")

    checks.equal(component["prepared_mass_fraction"], 0.3,
                 "at the prepared fraction")

    matrix = parts["MATRIX"]

    # THE BOUNDARY. §3.
    checks.equal(matrix["matrix_label"], "Soil",
                 "the matrix keeps the operator's own words")

    checks.equal(matrix["material_key"], None,
                 "and NO material key - Soil did not become a material")

    checks.equal(matrix["material_id"], None,
                 "no material id either")

    checks.equal(matrix["family_id"], None,
                 "and no family: a matrix is not a spectral class")

    checks.equal(matrix["prepared_mass_fraction"], 0.7,
                 "at the remaining fraction")

    # -- and the taxonomy is untouched -----------------------------
    taxonomy = bench.mission.taxonomy

    checks.ok(taxonomy.get("Soil") is None,
              "'Soil' still resolves to nothing in the taxonomy - "
              "saving ground truth cannot invent a material")

    checks.ok(all(i.display_name != "Soil"
                  for i in taxonomy.materials.values()),
              "and it is in no material list")


# ======================================================================
checks.section("TEST 8b - every prepared ratio, and no id collision")

with OperatorBench() as bench:
    bench.bring_up()

    # `measurement_id` is allocated per SAMPLE, so every sample's first
    # measurement is M001. The learning store keys observations
    # GLOBALLY, so the second sample was refused with "M001 is already
    # recorded ... immutable" and the mixture the operator had just
    # typed in was silently not saved.
    for sample, slot, percent in (("s0007", 1, "30"), ("s0008", 2, "40"),
                                  ("s0009", 3, "20"), ("s0010", 4, "60")):
        bench.loaded_sample(sample, slot)
        bench.link.select_slot(slot, sample_id=sample)

        run = bench.run(["4", "", "y", "3", "carbon", "1", percent, "c",
                         "Soil", "y", "1", "n", "", "q"])

        checks.ok(run.shows(
            "Saved to the learning history as {}/M001".format(sample)),
            "{} at {}/{} saved - and the FIRST measurement of every "
            "sample is M001".format(sample, percent, 100 - int(percent)))

    store = bench.mission.learning
    summary = store.mixture_summary()

    checks.ok(summary["mixtures"] >= 4,
              "all four are on record - {} mixtures".format(
                  summary["mixtures"]))

    fractions = sorted(
        summary["by_material"]["Activated Carbon"]["fractions"])

    checks.equal(fractions, [0.2, 0.3, 0.4, 0.6],
                 "at the four prepared fractions, as weighed")

    checks.equal(sorted(summary["matrices"]), ["Soil"],
                 "all against the same named matrix")

    # -- the downstream consumer accepts what the UI produced -------
    training = store.mixture_training_set()

    checks.ok(len(training) >= 4,
              "and `mixture_training_set` - what "
              "research/training/evaluate_mixtures.py loads - returns "
              "them: {} rows".format(len(training)))

    ids = {row.get("measurement_id") for row in training}

    checks.ok({"s0007/M001", "s0010/M001"} <= ids,
              "including the records the production UI wrote, by their "
              "own ids")


# ======================================================================
checks.section("no screen reads a status key the board does not send")

# THE GENERAL FORM OF THE `selected` DEFECT.
#
# One dead key cost the operator every servo reading on every screen and
# made the whole re-sync-versus-reconnect distinction unreachable. It
# was invisible because it is not a crash: `.get()` on a missing key
# returns None, and None is a perfectly good falsy value.
#
# So every key the PC reads off a `servo`, `sensor` or `carousel` status
# block is collected by parsing the source, and checked against what a
# real firmware actually puts in that block.
import ast                                                    # noqa: E402

STATUS_BLOCKS = ("servo", "sensor", "carousel", "backend")

with OperatorBench() as live:
    live.bring_up().loaded_sample()

    # Every key a REAL board puts anywhere in the responses these
    # screens read. Gathered from actual traffic rather than from a
    # hand-written allow-list, because a hand-written list is the same
    # kind of artefact as the fixture that caused the defect.
    def collect(value, into):
        if isinstance(value, dict):
            into |= set(value)

            for item in value.values():
                collect(item, into)

        elif isinstance(value, list):
            for item in value[:4]:
                collect(item, into)

        return into

    available = set()
    collect(live.status(), available)
    collect(live.link.servo_diagnostics(), available)
    collect(live.link.move_slots("cw", 1), available)
    collect(live.link.fine_adjust(1.0), available)

    # `servo["message"]` exists ONLY when nothing is connected, which is
    # exactly the state a connected board cannot show. A key that is
    # conditional is still a key the board sends.
    live.link.disconnect_servo()
    collect(live.status(), available)
    live.link.connect_servo()

    # The refusal payload, which carries the motion evidence the
    # failure screens read.
    from serial_link import DeviceError                        # noqa: E402

    with OperatorBench(servo=stuck_encoder()) as broken:
        broken.bring_up().loaded_sample()

        try:
            broken.link.measure_raw(1, "s0007")

        except DeviceError as error:
            collect(error.data or {}, available)

    suspicious = {}

    for path in sorted((support.FIRMWARE / "PC").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            func = node.func

            if not isinstance(func, ast.Attribute) or func.attr != "get":
                continue

            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue

            key = node.args[0].value

            if not isinstance(key, str):
                continue

            # Only reads off a variable NAMED like a firmware block.
            if getattr(func.value, "id", None) not in STATUS_BLOCKS:
                continue

            if key not in available:
                suspicious.setdefault(key, set()).add(path.name)

    checks.ok(len(available) > 60,
              "a live board's responses were read - {} distinct keys - "
              "so the check below is not vacuous".format(len(available)))

    checks.equal(sorted(suspicious), [],
                 "every key the screens read off a servo/sensor/carousel "
                 "block is one a real board sends somewhere. `selected` "
                 "failed this for the whole of 6.0.0 and cost the "
                 "operator every servo reading on every screen")


# ======================================================================
checks.section("the servo movement test actually reports its legs")

# TOMORROW'S H-002 SCREEN. `report_move_test` gated its ENTIRE output on
# `result["verified_movement"]` - a key the firmware has never sent; the
# record says `verified`. So a real movement test printed
#
#     Movement complete.
#
# and nothing else: no legs, no encoder start/end, no net travel, no
# worst error, no closing error. The only place `verified_movement`
# existed was a fixture in test_display_shapes.py that invented it,
# which is exactly why the rendering test stayed green.
from workflow.carousel import report_move_test                # noqa: E402

with OperatorBench() as bench:
    bench.bring_up()

    result = bench.link.servo_test_move("slot_out_and_back", repeat=2)

    checks.ok("verified" in result,
              "the firmware's move record says 'verified'")

    checks.ok("verified_movement" not in result,
              "and never 'verified_movement'")

    printed = captured(lambda: report_move_test(result))

    checks.ok("Encoder:" in printed,
              "so the report prints the encoder travel - the whole "
              "block was unreachable")

    checks.ok("Net travel:" in printed and "Worst error:" in printed,
              "with net travel and worst error, which is what a servo "
              "characterisation run exists to produce")

    checks.ok("leg 1:" in printed and "leg 4:" in printed,
              "and every individual leg, so a bad leg can be located")

    checks.ok("Closing error:" in printed,
              "and the closing error, which is the H-002 measurement")

    legs_line = printed.split("Legs:")[1].splitlines()[0]

    checks.ok("[" not in legs_line,
              "the leg count is a number, not the raw list - it read "
              "\"2 x [1024, -1024] = 4\"")


# ======================================================================
checks.section("a failed acquisition does not blame the carousel")

# `carousel_outcome` read `data["recovery"]`, a key no firmware sends -
# the block is called `return_move`. The branch was dead, so every
# acquisition failure fell through to the `motion` test, which
# describes the OUTBOUND transfer and is therefore always MOVED there.
#
# The screen then contradicted itself two lines apart:
#
#     Carousel:   POSITION UNKNOWN - the encoder measured travel
#     Action:     inspect the mechanism and re-sync
#     Returning Slot home............ PASS
#
# over a carousel the firmware had brought home and still trusted.
with OperatorBench(sensor=support.FakeAS7265X(data_ready=False)) as bench:
    bench.bring_up().loaded_sample()

    run = bench.run(["4", "0", "q"])
    block = run.text[run.text.find("MEASUREMENT FAILED"):]

    checks.ok("ACQUISITION" in block,
              "the acquisition failure names its own stage")

    checks.ok("POSITION UNKNOWN" not in block.split("MEASUREMENT RECOVERY")[0],
              "and does NOT declare the position unknown - the sample "
              "came home and the firmware still trusts it")

    checks.ok("re-sync" not in block.split("MEASUREMENT RECOVERY")[0].lower(),
              "so the operator is not sent to re-sync working hardware "
              "after a purely sensor fault")

    status = bench.status()

    checks.ok((status.get("carousel") or {}).get("position_valid"),
              "and the firmware agrees the position is still valid")

    checks.ok(ui_status.carousel_label(status) == "LOAD",
              "with the carousel back at LOAD")

# The other half: when the return really fails, it IS unknown.
with OperatorBench(servo=stuck_on_return()) as bench:
    bench.bring_up().loaded_sample()

    run = bench.run(["4"] + [""] * 3 + ["n", "0", "q"])
    recovery = run.after("MEASUREMENT RECOVERY")

    checks.ok("POSITION UNKNOWN" in recovery,
              "a return that failed DOES leave the position unknown - "
              "the fix did not simply stop reporting the state")

    checks.ok("ACQUIRED" in recovery,
              "while the spectra that were taken are still reported")


# ======================================================================
checks.section("measurement quality actually reaches the operator")

# `print_quality` read `report["status"]` and `report["checks"]`.
# `Science.pipeline.split_quality` separates the report into `hardware`
# and `normalization`, each with its own status and checks, and leaves
# NO top-level status. So every screen showing measurement quality
# printed "Overall: None" and then listed nothing: the reasons a
# measurement was degraded were computed on every analysis and shown on
# none of them.
from workflow.display import print_quality                    # noqa: E402
from BD.channels import CHANNELS                              # noqa: E402

with OperatorBench() as bench:
    bench.bring_up().loaded_sample()
    bench.run(["4", "", "n", "", "q"])

    measurement = bench.mission.store.get_sample(
        "s0007")["measurements"][-1]
    quality = bench.mission.analyse_measurement(measurement).get("quality")

    checks.ok(quality.get("status") is None,
              "the real report has NO top-level status - which is why "
              "reading one printed None")

    checks.ok("hardware" in quality and "normalization" in quality,
              "it carries the two split verdicts instead")

    printed = captured(lambda: print_quality(quality))

    checks.ok("None" not in printed,
              "and the screen no longer prints None at the operator")

    checks.ok("Hardware:" in printed and "Normalization:" in printed,
              "both verdicts are shown - a hardware FAIL and a "
              "normalization problem mean different things")

# The reasons must appear, which is the whole value of the block.
degraded = {
    "hardware": {"status": "HARDWARE_WARNING", "checks": [
        {"check": "repeatability", "status": "WARNING",
         "message": "WHITE repeats varied by 8.2%"},
        {"check": "validity", "status": "PASS", "message": "all finite"}]},
    "normalization": {"status": "DEGRADED", "checks": [
        {"check": "reflectance", "status": "FAIL",
         "message": "reflectance above 1.0"}]},
    "usable_channels": [c for c in CHANNELS if c not in ("A", "B")],
}

printed = captured(lambda: print_quality(degraded))

checks.ok("repeatability" in printed and "8.2%" in printed,
          "a WARNING check is named with its reason")

checks.ok("reflectance" in printed,
          "and so is a FAIL check")

checks.ok("all finite" not in printed,
          "while PASS checks stay out of the way - this screen is the "
          "exceptions, not a list of everything that went right")

checks.ok("A,B" in printed,
          "and the channels dropped from comparison are named")

# A migrated flat record must still render.
legacy = captured(lambda: print_quality({
    "status": "WARNING",
    "checks": [{"check": "validity", "status": "WARNING",
                "message": "two channels saturated"}],
    "invalid_channels": ["A"]}))

checks.ok("Overall: WARNING" in legacy and "saturated" in legacy,
          "and a record migrated from the flat schema still reads")


# ======================================================================
checks.section("the client proves it matches the firmware it is talking to")

# `new PC code + old ESP32 firmware` presents as dead keys and missing
# commands, which read like hardware faults and cost bench time. The
# firmware bumps PROTOCOL_VERSION whenever the command surface changes,
# so the number is the cheap proof - checked on the first ping, every
# time anybody connects.
import serial_link                                            # noqa: E402

with OperatorBench() as bench:
    run = bench.run(["q"])

    checks.ok(run.shows("Firmware:"),
              "the build identity is printed where the operator already "
              "reads the connection line")

    checks.equal(bench.link.device_protocol_version,
                 serial_link.EXPECTED_PROTOCOL_VERSION,
                 "and this source matches the firmware in the tree")

    checks.ok(bench.link.protocol_mismatch is None,
              "so no mismatch is reported")

    checks.ok(bench.link.firmware_version,
              "the firmware version is captured from the ping, not "
              "assumed")

    # THE CHECK MUST BE ABLE TO FIRE. A board one protocol behind.
    stale = bench.link._note_identity(
        {"firmware": "freya-science-module", "version": "5.9.0",
         "protocol_version": serial_link.EXPECTED_PROTOCOL_VERSION - 1})

    checks.ok(stale is not None,
              "a device reporting a different protocol version IS "
              "flagged - the check is not decorative")

    checks.equal(stale["device"],
                 serial_link.EXPECTED_PROTOCOL_VERSION - 1,
                 "and the mismatch names what the board reported")

    # A board that does not report one at all must not be called a
    # mismatch: absent is not wrong.
    silent = bench.link._note_identity({"firmware": "x"})

    checks.ok(silent is None,
              "while firmware that reports no protocol version is not "
              "accused of mismatching")


# ======================================================================
checks.section("--preflight captures the bench without moving it")

import json as _json                                          # noqa: E402
import tempfile as _tempfile                                  # noqa: E402
import rover_science_client as client                         # noqa: E402

with OperatorBench() as bench:
    bench.bring_up()
    servo = bench.loopback._servo
    before = len(servo.goals)

    target = Path(_tempfile.mkdtemp()) / "preflight.json"
    printed = captured(lambda: client.preflight(bench.link, str(target)))

    # THE PROPERTY THAT MAKES IT SAFE TO RUN AT ANY MOMENT.
    checks.equal(len(servo.goals) - before, 0,
                 "preflight issues NO movement - it is the command to "
                 "run when something has already gone wrong")

    for line in ("Board:", "Sensor:", "Servo:", "Carousel:",
                 "Encoder:", "Supply:", "Servo bus:"):
        checks.ok(line in printed,
                  "it reports {}".format(line.rstrip(":").lower()))

    checks.ok("None" not in printed,
              "and shows no None to the operator")

    checks.ok(target.is_file(), "the evidence file is written")

    saved = _json.loads(target.read_text())

    for key in ("ping", "status", "servo_diagnostics", "transport",
                "captured_utc", "port", "client_expects_protocol"):
        checks.ok(key in saved,
                  "the capture carries {} - enough to diagnose the run "
                  "later".format(key))

    checks.ok(saved["servo_diagnostics"].get("feedback"),
              "including the servo feedback registers H-002 needs")

# A board that does not answer must still produce a capture.
with OperatorBench(servo=support.FakeST3215(silent=True)) as bench:
    target = Path(_tempfile.mkdtemp()) / "dead.json"
    printed = captured(lambda: client.preflight(bench.link, str(target)))

    checks.ok(target.is_file(),
              "a preflight against an unhealthy bench still writes its "
              "evidence - that is when it matters most")


# ======================================================================
checks.section("the raw encoder word survives, for H-002")

# `decode_signed` maps 0x8002 to -2. On a screen that is
# indistinguishable from a genuine reading of -2, and the raw word was
# discarded the instant it was decoded - so no evidence could separate
# "the encoder really is near zero" from "the sign rule is reading a
# large value as a small one". Those need opposite investigations, and
# H-002 is exactly the case where the difference matters.
from servo import decode_signed                               # noqa: E402

checks.equal(decode_signed(0x8002), -2,
             "0x8002 decodes to -2 - the ambiguity is real, not "
             "hypothetical")

checks.equal(decode_signed(0x0002), 2,
             "and 0x0002 decodes to 2, so the decoded value alone "
             "cannot tell the two apart")

with OperatorBench() as bench:
    bench.bring_up()

    feedback = (bench.link.servo_diagnostics() or {}).get("feedback") or {}

    checks.ok("position_raw" in feedback,
              "the feedback block carries the undecoded word")

    checks.ok(feedback.get("trajectory_raw") is not None,
              "and the trajectory word beside it - the measured "
              "position is derived from BOTH, so one raw value alone "
              "could not be checked against the wire")

    checks.ok(feedback.get("position_counts") is not None,
              "beside the decoded value the system actually uses")

    # THE DERIVATION IS REPORTED, SO IT CAN BE CHECKED RATHER THAN
    # TRUSTED. Register 56 is the following error in step mode and 67
    # is the commanded trajectory; the position is one minus the other.
    # Reading 56 as a position on its own is precisely what H-002 was.
    checks.equal(
        feedback["position_counts"],
        feedback["trajectory_counts"] - feedback["following_error_counts"],
        "the measured position IS the trajectory minus the following "
        "error, and all three are on the screen")

    printed = captured(
        lambda: __import__("workflow.display", fromlist=["x"])
        .print_servo_telemetry(feedback))

    checks.ok("Carousel" in printed,
              "the telemetry screen leads with the LOGICAL carousel "
              "angle, not with an encoder count")

    checks.ok("reg 67" in printed and "reg 56" in printed,
              "and names the two registers the position came from")

    # The ambiguity itself, on the screen that has to resolve it.
    negative = dict(feedback)
    negative["position_raw"] = 0x8002
    negative["following_error_counts"] = -2
    negative["following_error_deg"] = -0.176

    printed = captured(
        lambda: __import__("workflow.display", fromlist=["x"])
        .print_servo_telemetry(negative))

    checks.ok("raw 0x8002" in printed,
              "and a following error of -2 shows the undecoded word in "
              "hex, where the set bit 15 is visible at a glance")

    # It must be diagnostic ONLY - nothing may steer on it.
    control = (support.FIRMWARE / "ESP32" / "servo.py").read_text(
        encoding="utf-8-sig")

    for line in control.splitlines():
        stripped = line.strip()

        if "last_position_raw" not in stripped or stripped.startswith("#"):
            continue

        checks.ok(stripped.startswith("self.last_position_raw =")
                  or "position_raw" in stripped,
                  "last_position_raw is only ever assigned or reported, "
                  "never used in a decision: {!r}".format(stripped[:48]))


# ======================================================================
checks.section("a closed link does not offer a retry that cannot work")

# PORT_LOST closes the link and clears the serial handle. Nothing in the
# menu loop re-opens it, so every "Retry? [Y/n]" after that raised
# PORT_CLOSED again - forever, with a prompt defaulting to Yes. An
# operator whose USB cable fell out pressed Enter, saw the same line,
# and had every reason to think the client had hung.
with OperatorBench(servo=stuck_encoder()) as bench:
    bench.bring_up().loaded_sample()

    original = bench.link.get_status
    calls = {"n": 0}

    def dying(*args, **kwargs):
        calls["n"] += 1

        if calls["n"] > 2:
            bench.link.close(reason="the port disappeared mid-request")

            raise bench.link._closed_error("get_status")

        return original(*args, **kwargs)

    bench.link.get_status = dying

    run = bench.run(["4", "0", "q"])

    checks.ok(run.error is None,
              "the client does not raise when the port vanishes")

    checks.equal(len([p for p in run.prompts if "Retry" in p]), 0,
                 "and it does NOT ask 'Retry?' - that prompt could "
                 "never have succeeded")

    checks.equal(run.exit_code, 1,
                 "it exits with a failure code instead of looping")

    checks.ok(run.shows("cannot re-open it"),
              "saying plainly that this client cannot recover the link")

    checks.ok(run.shows("start the client"),
              "and what the operator must actually do")

    checks.ok(run.shows("resets the board"),
              "including that re-opening resets the ESP32 - so the "
              "carousel will need re-syncing, which is not obvious")

    checks.ok(bench.link.serial is None,
              "the port really is released, not left held")


# ======================================================================
checks.section("a deployment receipt proves the bytes without recompiling")

# mpy-cross embeds the source path, so `verify` rebuilding from a
# different checkout reports a mismatch about the PATH, not about the
# firmware. The receipt records what the deployment already proved onto
# the device, which is path-independent.
import sys as _sys                                            # noqa: E402
import tempfile as _tf                                        # noqa: E402

_sys.path.insert(0, str(support.FIRMWARE / "tools"))

import device as device_tool                                  # noqa: E402

_original_receipt = device_tool.RECEIPT_PATH
_original_sha = device_tool.device_sha256

try:
    device_tool.RECEIPT_PATH = Path(_tf.mkdtemp()) / "receipt.json"

    built = device_tool.build_firmware(device_tool.Report())

    checks.ok(bool(built),
              "the firmware builds - without this the rest is vacuous")

    receipt = device_tool.write_receipt("PORT_TEST", built)

    checks.equal(sorted(receipt["files"]), sorted(device_tool.ESP32_FILES),
                 "the receipt records every file that reaches the device")

    checks.ok(receipt.get("mpy_cross"),
              "and which compiler produced them")

    checks.equal(device_tool.read_receipt()["files"], receipt["files"],
                 "it round-trips through the file")

    truth = dict(receipt["files"])
    device_tool.device_sha256 = lambda port, name: truth.get(name)

    matching = support.Checks("_probe")
    report = device_tool.Report()

    checks.ok(device_tool.check_against_receipt("PORT_TEST", report) is True,
              "a device holding those bytes matches")

    checks.ok(not report.failed, "and the check does not fail")

    # IT MUST BE ABLE TO FAIL.
    truth["servo.mpy"] = "0" * 64
    report = device_tool.Report()

    checks.ok(device_tool.check_against_receipt("PORT_TEST", report) is False,
              "a device whose servo.mpy differs is caught - the check "
              "is not decorative")

    checks.ok(report.failed, "and that is a failure")

    # A board deployed before receipts existed is not a fault.
    device_tool.RECEIPT_PATH = Path(_tf.mkdtemp()) / "absent.json"
    report = device_tool.Report()

    checks.ok(device_tool.check_against_receipt("PORT_TEST", report) is None,
              "no receipt means no opinion, not a failure")

    checks.ok(not report.failed,
              "so an older deployment still verifies the old way")

finally:
    device_tool.RECEIPT_PATH = _original_receipt
    device_tool.device_sha256 = _original_sha


sys.exit(checks.report())
