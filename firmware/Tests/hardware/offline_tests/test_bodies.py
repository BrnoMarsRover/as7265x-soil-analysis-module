"""
The real test bodies, driven against the fake transport.

WHAT THIS PROVES: that the procedures in campaigns/ execute - that they
call the adapters correctly, read the answer shapes the firmware really
produces, make checks, record measurements and raise defects when the
answer is wrong.

WHAT IT DOES NOT PROVE: anything about the hardware. Every result here
is FRAMEWORK_SELFTEST, and the runner converts a would-be pass into
SKIPPED with that reason attached. A green run of this file means the
harness is ready; it says nothing about a carousel.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

if str(HERE.parent.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent.parent))

from hardware.core.model import Evidence, Status             # noqa: E402
from hardware.offline_tests.fake_link import (               # noqa: E402
    failing, healthy_script)
from hardware.offline_tests.harness import (Bench, Checks,   # noqa: E402
                                            cli)


# Answers for the operator-assisted bodies. Generous, because a body
# that asks one more question than expected should re-ask rather than
# read an empty line as an answer - and an exhausted script is an
# ABORTED result, which is itself a correct behaviour to observe.
def _answers(count=80, value="y"):
    return [value] * count


def run():
    checks = Checks("hardware/offline_tests/test_bodies.py")

    checks.section("B0 - environment inventory")

    with Bench() as bench:
        result = bench.run("HW-B0-001")

        checks.ok(result.status in (Status.SKIPPED, Status.FAIL),
                  "the host environment body executes")

        checks.equal(result.evidence_class, Evidence.SELFTEST,
                     "and produces only self-test evidence")

        checks.ok(len(result.checks) >= 2,
                  "it makes real checks")

    with Bench() as bench:
        result = bench.run("HW-B0-002")

        checks.equal(result.status, Status.SKIPPED,
                     "the repository/geometry body passes its checks")

        descriptions = " ".join(c.description for c in result.checks)

        checks.ok("half the slot count" in descriptions,
                  "it checks the loader/scanner offset invariant")

        checks.ok(all(c.ok for c in result.checks),
                  "the shipped geometry is self-consistent")

    with Bench() as bench:
        result = bench.run("HW-B0-003")

        checks.equal(result.status, Status.SKIPPED,
                     "the profile validation body passes on a valid "
                     "profile")

    with Bench() as bench:
        result = bench.run("HW-B0-004")

        checks.equal(result.status, Status.SKIPPED,
                     "the device inventory body resolves the fake port")

        rows = [m for m in result.measurements
                if m.get("stage") == "inventory"]

        checks.ok(bool(rows),
                  "it records every port it saw as a measurement")

    with Bench(answers=_answers()) as bench:
        result = bench.run("HW-B0-005")

        checks.equal(result.status, Status.SKIPPED,
                     "the wiring checklist runs with an operator")

        checks.ok(all(c.kind == "OPERATOR" for c in result.checks),
                  "every one of its checks is marked OPERATOR")

    checks.section("B1 - the link")

    with Bench() as bench:
        result = bench.run("HW-B1-001")

        checks.equal(result.status, Status.SKIPPED,
                     "open and ping executes")

        checks.ok(bench.link.opened >= 1, "it opened the transport")

        checks.ok(result.cleanup.get("confirmed") is True,
                  "and its cleanup released the port")

    with Bench() as bench:
        result = bench.run("HW-B1-002")

        checks.equal(result.status, Status.SKIPPED,
                     "the uptime/reset body executes")

        descriptions = " ".join(c.description for c in result.checks)

        checks.ok("backwards" in descriptions,
                  "it checks that uptime never goes backwards")

    with Bench() as bench:
        result = bench.run("HW-B1-005", )

        checks.ok(result.status in (Status.SKIPPED, Status.FAIL),
                  "the open-cycle campaign executes")

        checks.equal(result.iterations, 100,
                     "it defaults to 100 cycles, as the plan requires")

    with Bench() as bench:
        bench.context.iteration_overrides["*"] = 10

        result = bench.run("HW-B1-005")

        checks.equal(result.status, Status.FAIL,
                     "fewer than 100 cycles FAILS its own acceptance "
                     "criterion rather than quietly passing")

    with Bench() as bench:
        bench.context.iteration_overrides["*"] = 1000

        result = bench.run("HW-B1-006")

        checks.equal(result.status, Status.SKIPPED,
                     "the 1000-request endurance body executes")

    checks.section("a failing device produces a FAIL and a defect")

    script = dict(healthy_script())
    script["ping"] = failing("PORT_LOST", "the device vanished")

    with Bench(script=script) as bench:
        bench.context.iteration_overrides["*"] = 100

        result = bench.run("HW-B1-005")

        checks.equal(result.status, Status.FAIL,
                     "a device that never answers FAILS the open-cycle "
                     "campaign")

        checks.equal(len(result.defects), 1,
                     "and raises exactly one persistent defect")

        checks.ok(result.defects
                  and result.defects[0]["defect_id"].startswith(
                      "HW-USB-"),
                  "the defect gets the campaign's own family id")

        checks.equal(result.first_failure_iteration, 1,
                     "the first failing iteration is recorded")

    checks.section("B2 - the servo, without moving")

    with Bench() as bench:
        result = bench.run("HW-B2-001")

        checks.equal(result.status, Status.SKIPPED,
                     "connect_servo executes")

    with Bench() as bench:
        result = bench.run("HW-B2-002")

        checks.equal(result.status, Status.SKIPPED,
                     "the diagnostics body executes and passes on a "
                     "healthy fake")

        descriptions = " ".join(c.description for c in result.checks)

        checks.ok("mode" in descriptions,
                  "it checks the operating mode, which is H-002 "
                  "hypothesis 1")

    checks.section("a servo in the wrong mode raises the H-002 defect")

    script = dict(healthy_script())

    def wrong_mode(_payload):
        answer = healthy_script()["servo_diagnostics"](_payload)

        answer["mode"] = 0
        answer["mode_name"] = "position"
        answer["mode_correct"] = False

        return answer

    script["servo_diagnostics"] = wrong_mode

    with Bench(script=script) as bench:
        result = bench.run("HW-B2-002")

        checks.equal(result.status, Status.FAIL,
                     "a servo in position mode FAILS the diagnostic")

        checks.equal(len(result.defects), 1, "and raises a defect")

        checks.equal(result.defects[0]["assumption"], "H-002",
                     "the defect is traced to H-002")

        checks.ok(any("hypothesis 1" in n for n in result.notes),
                  "the note names which H-002 hypothesis this is")

    checks.section("an unstable position read is caught")

    script = dict(healthy_script())

    state = {"n": 0}

    def drifting(payload):
        state["n"] += 1

        answer = healthy_script()["servo_diagnostics"](payload)

        answer["feedback"] = {"position": 1000 + state["n"]}
        answer["steps"] = [
            {"step": "feedback", "ok": True,
             "value": {"position": 1000 + state["n"]}}]

        return answer

    script["servo_diagnostics"] = drifting

    with Bench(script=script) as bench:
        bench.context.iteration_overrides["*"] = 5

        result = bench.run("HW-B2-004")

        checks.equal(result.status, Status.FAIL,
                     "a position that changes with nothing commanded "
                     "FAILS")

        checks.ok(bool(result.defects),
                  "and raises a defect naming the read path")

    checks.section("B3 - the H-002 investigation")

    # A scripted protractor: for every leg, the direction the command
    # asked for and the angle it asked for. A blanket "y" would not do -
    # the direction prompt refuses anything that is not one of its four
    # answers, and the angle prompt refuses anything that is not a
    # number, which is the point of those prompts.
    angles = []

    for magnitude in (10.0, 45.0, 90.0, 180.0, 360.0):
        for signed in (magnitude, -magnitude):
            angles.append("CW" if signed > 0 else "CCW")
            angles.append(str(signed))

    answers = ["y", "y"] + angles

    with Bench(answers=answers) as bench:
        result = bench.run("HW-B3-001")

        checks.ok(result.status in (Status.SKIPPED, Status.FAIL,
                                    Status.ABORTED),
                  "the H-002 investigation body executes")

        rows = [m for m in result.measurements
                if m.get("stage") == "h002"]

        checks.ok(len(rows) >= 8,
                  "it records a measurement row per commanded leg")

        if rows:
            row = rows[0]

            for field in ("commanded_deg", "commanded_counts",
                          "position_before", "position_after",
                          "reported_delta_counts", "observed_direction",
                          "observed_deg", "elapsed_ms"):
                checks.ok(field in row,
                          "each H-002 row carries '{}'".format(field))

    checks.section("the H-002 contradiction is recognised")

    script = dict(healthy_script())

    def encoder_stuck(payload):
        """The bench failure: the shaft moves, the encoder does not."""
        return {
            "kind": payload.get("kind"), "moved": True, "verified": True,
            "repeat": int(payload.get("repeat") or 1),
            "legs": [2], "net_counts": 2,
            "start_position": 1024, "end_position": 1026,
            "closed_loop_error_counts": 0, "worst_position_error": 2,
            "tolerance_counts": 15, "position_invalidated": True,
        }

    script["servo_test_move"] = encoder_stuck

    with Bench(script=script, answers=answers) as bench:
        result = bench.run("HW-B3-001")

        checks.equal(result.status, Status.FAIL,
                     "an encoder that reports 2 counts for a 180 degree "
                     "movement FAILS")

        checks.ok(bool(result.defects),
                  "and raises the H-002 defect")

        if result.defects:
            defect = result.defects[0]

            checks.equal(defect["assumption"], "H-002",
                         "the defect is traced to H-002")

            checks.ok("hypotheses_still_open" in defect["evidence"],
                      "the defect lists which hypotheses remain open "
                      "rather than choosing one")

            checks.ok(bool(defect["evidence"]["hypotheses_still_open"]),
                      "and there is at least one of them")

        checks.ok(any("do not widen" in n.lower() for n in result.notes)
                  or any("tolerance" in n.lower() for n in result.notes),
                  "the result says not to widen the tolerance to make "
                  "it pass")

    checks.section("B4 - characterization measures rather than asserts")

    with Bench() as bench:
        bench.context.iteration_overrides["*"] = 10

        result = bench.run("HW-B4-003")

        checks.equal(result.status, Status.SKIPPED,
                     "the closing-error body executes")

        summary = [m for m in result.measurements
                   if m.get("stage") == "h001_summary"]

        checks.ok(bool(summary),
                  "it records the H-001 distribution as a measurement")

        if summary:
            for field in ("mean", "median", "sd", "p95", "p99", "worst",
                          "tolerance"):
                checks.ok(field in summary[0],
                          "the H-001 summary carries '{}'".format(field))

        checks.ok(any("ST3215_POSITION_TOLERANCE" in n
                      for n in result.notes),
                  "and says the tolerance may only be changed from "
                  "these numbers")

    checks.section("B6 and B7 - the sensor")

    with Bench() as bench:
        result = bench.run("HW-B6-002")

        checks.equal(result.status, Status.SKIPPED,
                     "the cold initialization body executes")

        descriptions = " ".join(c.description for c in result.checks)

        checks.ok("0x49" in descriptions,
                  "it checks the AS7265x answered at its address")

    with Bench() as bench:
        result = bench.run("HW-B7-001")

        checks.equal(result.status, Status.SKIPPED,
                     "the 54-feature contract body executes")

        descriptions = " ".join(c.description for c in result.checks)

        checks.ok("54 features" in descriptions,
                  "it checks the feature count explicitly")

    checks.section("a malformed spectrum FAILS and is named")

    script = dict(healthy_script())

    def short_spectrum(payload):
        answer = healthy_script()["acquire_triad"](payload)

        for block in answer["illuminations"].values():
            for acquisition in block["acquisitions"]:
                acquisition.pop("W", None)

        return answer

    script["acquire_triad"] = short_spectrum

    with Bench(script=script) as bench:
        result = bench.run("HW-B7-001")

        checks.equal(result.status, Status.FAIL,
                     "a spectrum missing a channel FAILS")

        checks.ok(bool(result.defects),
                  "and raises a defect against the 54-feature contract")

    checks.section("identical spectra are caught")

    script = dict(healthy_script())

    def lamp_never_switched(payload):
        answer = healthy_script()["acquire_triad"](payload)

        white = answer["illuminations"]["white"]["acquisitions"]

        for name in ("uv", "ir"):
            answer["illuminations"][name]["acquisitions"] = [
                dict(a) for a in white]

        return answer

    script["acquire_triad"] = lamp_never_switched

    with Bench(script=script) as bench:
        result = bench.run("HW-B7-001")

        checks.equal(result.status, Status.FAIL,
                     "three identical spectra FAIL - that is a lamp "
                     "that never switched")

    checks.section("B8 - the RF-001 regression")

    with Bench() as bench:
        result = bench.run("HW-B8-002")

        checks.equal(result.status, Status.SKIPPED,
                     "the RF-001 body executes against a healthy fake")

    script = dict(healthy_script())

    def stuck_transfer(payload):
        answer = healthy_script()["measure_raw"](payload)

        answer["movement"] = {"travelled_counts": 2,
                              "position_error": 2046}

        return answer

    script["measure_raw"] = stuck_transfer

    with Bench(script=script) as bench:
        result = bench.run("HW-B8-002")

        checks.equal(result.status, Status.FAIL,
                     "a transfer reporting 2 counts FAILS the RF-001 "
                     "regression")

        checks.ok(bool(result.defects),
                  "and raises a defect")

        if result.defects:
            checks.equal(result.defects[0]["assumption"], "H-002",
                         "which points back at H-002")

        checks.ok(any("RF-001 HAS RECURRED" in n for n in result.notes),
                  "and says every carousel result above B3 is invalid")

    checks.section("B10 - the workflow readiness check")

    with Bench() as bench:
        result = bench.run("HW-B10-001")

        checks.equal(result.status, Status.SKIPPED,
                     "the client readiness body executes")

        checks.ok(all(c.ok for c in result.checks),
                  "the production client and its screens are present")

    checks.section("B11 - endurance refuses an unbounded run")

    with Bench() as bench:
        bench.context.iteration_overrides["*"] = 0

        result = bench.run("HW-B11-001")

        checks.equal(result.status, Status.FAIL,
                     "a zero-iteration endurance run is refused")

    with Bench() as bench:
        bench.context.iteration_overrides["*"] = 200000

        result = bench.run("HW-B11-001")

        checks.equal(result.status, Status.FAIL,
                     "an iteration count above the ceiling is refused")

    checks.section("no body produced hardware evidence")

    with Bench() as bench:
        results = [bench.run(test_id) for test_id in
                   ("HW-B0-001", "HW-B1-001", "HW-B2-001")]

        checks.ok(all(not r.hardware_evidence for r in results),
                  "not one result from this file is hardware evidence")

        checks.ok(all(r.status != Status.PASS for r in results),
                  "and not one of them is a PASS")

    return checks.report()


if __name__ == "__main__":
    sys.exit(cli(run, __doc__))
