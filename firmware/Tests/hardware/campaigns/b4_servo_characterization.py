"""
B4 - what the servo actually does, repeatedly.

This is where `ST3215_POSITION_TOLERANCE = 15` stops being a guess.

H-001 SAYS THE TOLERANCE IS A GUESS, AND IT IS

Fifteen counts is about 1.3 degrees. It decides whether a movement is
accepted or reported as SERVO_POSITION_MISMATCH, and it was chosen
before anyone measured the mechanism. Software has verified that the
configured value is applied inclusively, that the error is a circular
distance with no seam, and that a movement outside it invalidates the
position. What software cannot supply is the number.

HW-B4-003 supplies it: fifty symmetrical out-and-back movements, the
closing error of each, and the distribution. The tolerance should then
be set from the p99 and the worst observed - with margin - and that
change belongs in a commit with this evidence attached, not in a test.

NOTHING IN THIS CAMPAIGN WRITES TO config.py, AND NOTHING IN IT MAY BE
USED TO ARGUE FOR WIDENING A TOLERANCE TO MAKE A FAILURE GO AWAY. If
the measured spread exceeds 15 counts, the honest reading is that either
the tolerance is too tight or the mechanism has a problem the tolerance
was hiding - and which of those it is comes from the SHAPE of the
distribution, not from wanting the test to pass.
"""

from ..core.model import Automation, Safety
from ..core.analysis import (counts_to_degrees, failure_rate, outliers,
                               summarize)


CAMPAIGN = "B4"


def register(registry):
    registry.campaign(
        CAMPAIGN, layer="B4",
        title="Servo repeatability and tolerance characterization",
        purpose="Measure the closing error, the timing and the failure "
                "rate of real movements, so the shipped tolerance can "
                "be set from evidence.",
        prerequisites=("B3",),
        gate_note="Gated by B3: a repeatability figure computed from an "
                  "encoder that does not track the mechanism is a "
                  "repeatability figure for the encoder.",
    )

    registry.test(
        test_id="HW-B4-001", campaign=CAMPAIGN, layer="B4",
        title="One slot forward and one slot back",
        objective="Compare the two directions at the smallest movement "
                  "the carousel ever makes.",
        hardware_setup="Servo connected, carousel attached and free.",
        preconditions="HW-B3-001 resolved.",
        procedure=(
            "command one slot forward and record the movement record",
            "command one slot back and record it",
            "repeat several times",
            "compare the position error of each direction",
        ),
        expected="Both directions complete within tolerance, and their "
                 "error distributions are similar.",
        failure_criteria="A movement outside tolerance, or one "
                         "direction consistently worse than the other - "
                         "which is backlash and belongs to HW-B5-004.",
        captures=("per-movement position error", "elapsed time",
                  "the distribution per direction"),
        safety=Safety.MOTION, automation=Automation.AUTOMATIC,
        requires=("servo.test_move", "servo.connect"),
        run=_directions, cleanup=_stop_and_release,
        default_iterations=5, max_iterations=200,
        assumption="H-001", defect_prefix="HW-SERVO",
    )

    registry.test(
        test_id="HW-B4-002", campaign=CAMPAIGN, layer="B4",
        title="Movements of 45, 90 and 180 degrees",
        objective="Establish whether the error depends on the size of "
                  "the movement.",
        hardware_setup="As HW-B4-001.",
        preconditions="HW-B4-001 passed.",
        procedure=(
            "for each of 45, 90 and 180 degrees: command out and back",
            "record the closing error of each",
            "repeat several times",
            "compare the distributions",
        ),
        expected="The closing error does not grow with the size of the "
                 "movement.",
        failure_criteria="An error that scales with the angle, which "
                         "points at a resolution or ratio problem "
                         "rather than at mechanical repeatability.",
        captures=("per-angle closing error distribution",
                  "elapsed time per angle"),
        safety=Safety.MOTION, automation=Automation.AUTOMATIC,
        requires=("servo.test_move", "servo.connect"),
        run=_angle_sweep, cleanup=_stop_and_release,
        default_iterations=5, max_iterations=100,
        assumption="H-001", defect_prefix="HW-SERVO",
    )

    registry.test(
        test_id="HW-B4-003", campaign=CAMPAIGN, layer="B4",
        title="Closing-error distribution over repeated half turns",
        objective="MEASURE the number ST3215_POSITION_TOLERANCE should "
                  "be. This is H-001.",
        hardware_setup="Servo connected, carousel attached, mechanism "
                       "free, nothing loaded.",
        preconditions="HW-B4-002 passed.",
        procedure=(
            "command the symmetrical out-and-back half turn - the "
            "measurement path - N times",
            "record the closing error of every repetition",
            "record the worst per-leg position error of every "
            "repetition",
            "compute min, max, mean, median, p95, sd and worst",
            "list the outliers",
        ),
        expected="A distribution. This test MEASURES; a pass means "
                 "every movement completed and the numbers were "
                 "recorded.",
        failure_criteria="A movement that could not complete, or an "
                         "error distribution whose p99 exceeds the "
                         "shipped tolerance - which is a finding about "
                         "the tolerance, recorded as a defect for a "
                         "human to decide on, never fixed here.",
        captures=("every closing error", "every worst-leg error",
                  "the full distribution", "the outliers",
                  "the shipped tolerance compared against"),
        safety=Safety.MOTION, automation=Automation.AUTOMATIC,
        requires=("servo.test_move", "servo.connect"),
        run=_closing_error, cleanup=_stop_and_release,
        default_iterations=50, max_iterations=500,
        assumption="H-001", defect_prefix="HW-SERVO",
        notes="Replaces HW-205. The output of this test is the input "
              "to any future change of ST3215_POSITION_TOLERANCE.",
    )

    registry.test(
        test_id="HW-B4-004", campaign=CAMPAIGN, layer="B4",
        title="Movement timing distribution",
        objective="Measure how long a real movement takes, so "
                  "ST3215_MOVE_TIMEOUT_MS can be argued about with "
                  "numbers.",
        hardware_setup="As HW-B4-003.",
        preconditions="HW-B4-003 passed.",
        procedure=(
            "command a slot movement N times and record elapsed_ms",
            "command a half turn N times and record elapsed_ms",
            "compute the distribution of each",
            "compare the worst against the configured move timeout",
        ),
        expected="Every movement completes well inside the configured "
                 "timeout.",
        failure_criteria="A movement whose duration approaches the "
                         "timeout, which means the timeout is doing "
                         "nothing and a genuinely stuck servo would be "
                         "indistinguishable from a slow one.",
        captures=("every elapsed time", "the distribution per movement "
                  "size", "the configured timeout"),
        safety=Safety.MOTION, automation=Automation.AUTOMATIC,
        requires=("servo.test_move", "servo.connect"),
        run=_timing, cleanup=_stop_and_release,
        default_iterations=20, max_iterations=500,
        defect_prefix="HW-SERVO",
    )

    registry.test(
        test_id="HW-B4-005", campaign=CAMPAIGN, layer="B4",
        title="Lost responses, checksums and timeouts during movement",
        objective="Count the transport faults that occur while the "
                  "servo bus is busy, which is when they are most "
                  "likely.",
        hardware_setup="As HW-B4-003.",
        preconditions="HW-B4-003 passed.",
        procedure=(
            "snapshot the transport counters",
            "run a series of movements",
            "snapshot the counters again",
            "read the servo's own bus statistics from diagnostics",
            "compare",
        ),
        expected="No corrupt frames, no stale frames, no oversized "
                 "lines, and a servo bus error count that did not rise.",
        failure_criteria="Any counter rising during movement. A servo "
                         "bus that only misbehaves under load is the "
                         "hardest kind of fault to find later.",
        captures=("every transport counter before and after",
                  "the servo bus statistics before and after",
                  "the number of movements"),
        safety=Safety.MOTION, automation=Automation.AUTOMATIC,
        requires=("servo.test_move", "servo.diagnostics",
                  "link.counters"),
        run=_fault_counters, cleanup=_stop_and_release,
        default_iterations=20, max_iterations=500,
        defect_prefix="HW-SERVO",
    )

    registry.test(
        test_id="HW-B4-006", campaign=CAMPAIGN, layer="B4",
        title="Bounded servo endurance series",
        objective="Look for drift, heating and intermittent failure "
                  "over a longer run than any single-figure test.",
        hardware_setup="As HW-B4-003. The bench free for the duration.",
        preconditions="HW-B4-003 and HW-B4-005 passed.",
        procedure=(
            "command the symmetrical out-and-back movement N times",
            "record the closing error of each",
            "report progress every 25 movements",
            "check whether the error drifts across the run",
            "preserve everything recorded so far if the run is "
            "interrupted",
        ),
        expected="No failure, and no trend in the closing error between "
                 "the first tenth of the run and the last.",
        failure_criteria="Any failed movement, or a closing error that "
                         "grows through the run - which is heating, "
                         "wear or a loosening coupling.",
        captures=("every closing error with its iteration",
                  "the distribution of the first and last tenth",
                  "the first failing iteration"),
        safety=Safety.ENDURANCE, automation=Automation.AUTOMATIC,
        requires=("servo.test_move", "servo.connect"),
        run=_endurance, cleanup=_stop_and_release,
        default_iterations=200, max_iterations=5000,
        prerequisites=("HW-B4-003",),
        defect_prefix="HW-SERVO",
    )


# ======================================================================
# shared
# ======================================================================

def _stop_and_release(ctx):
    from .b3_h002 import _stop_and_release as shared

    return shared(ctx)


def _connect(ctx):
    ctx.link.request("connect_servo", timeout=ctx.servo._move_timeout())


def _move_record(transaction):
    """The fields a movement answer carries, flattened for a CSV row."""
    answer = (transaction or {}).get("data") or {}

    return {
        "closing_error": answer.get("closed_loop_error_counts"),
        "closing_error_deg": answer.get("closed_loop_error_deg"),
        "worst_leg_error": answer.get("worst_position_error"),
        "net_counts": answer.get("net_counts"),
        "start_position": answer.get("start_position"),
        "end_position": answer.get("end_position"),
        "elapsed_ms": (transaction or {}).get("elapsed_ms"),
        "tolerance": answer.get("tolerance_counts"),
        "position_invalidated": answer.get("position_invalidated"),
    }


def _run_series(ctx, kind, count, stage, degrees=None):
    """
    Command one movement kind `count` times, recording each.

    Returns (records, outcomes). A failure does not stop the series: the
    failure RATE and the iteration it first appeared at are the result,
    and stopping at the first one would throw that away.
    """
    records = []
    outcomes = []

    for index in range(1, count + 1):
        try:
            transaction = ctx.servo.move(
                kind, repeat=1, degrees=degrees)

            record = _move_record(transaction)
            record["ok"] = True

            outcomes.append(True)

        except Exception as error:
            record = {
                "ok": False,
                "error_type": type(error).__name__,
                "error": str(error)[:200],
                "code": getattr(error, "code", None),
            }

            outcomes.append(False)

        record["iteration"] = index
        record["kind"] = kind

        records.append(record)

        ctx.measure(stage=stage, **record)

    return records, outcomes


# ======================================================================
# bodies
# ======================================================================

def _directions(ctx):
    ctx.require("servo.connect", "servo.test_move")

    _connect(ctx)

    repeats = ctx.iterations()

    measured = {}

    for kind, stage in (("slot_forward", "forward"),
                        ("slot_reverse", "reverse")):
        records, outcomes = _run_series(ctx, kind, repeats, stage)

        errors = [r.get("worst_leg_error") for r in records if r.get("ok")]

        measured[kind] = {
            "records": records,
            "failure_rate": failure_rate(outcomes),
            "error": summarize(errors),
        }

        ctx.check(measured[kind]["failure_rate"]["all_passed"],
                  "every {} movement completed".format(kind),
                  evidence=measured[kind]["failure_rate"])

    ctx.record("directions", **measured)

    forward = measured["slot_forward"]["error"]
    reverse = measured["slot_reverse"]["error"]

    if forward and reverse:
        gap = abs(forward["mean"] - reverse["mean"])

        ctx.check(gap <= max(4.0, forward.get("sd", 0)
                             + reverse.get("sd", 0)),
                  "the two directions have comparable error "
                  "distributions",
                  evidence={"forward": forward, "reverse": reverse,
                            "difference": round(gap, 3)})


def _angle_sweep(ctx):
    ctx.require("servo.connect", "servo.test_move")

    _connect(ctx)

    repeats = ctx.iterations()

    measured = {}

    for degrees in (45.0, 90.0, 180.0):
        records = []
        outcomes = []

        for index in range(1, repeats + 1):
            try:
                out = ctx.servo.move("degrees", repeat=1, degrees=degrees)
                back = ctx.servo.move("degrees", repeat=1,
                                      degrees=-degrees)

                record = _move_record(back)
                record["ok"] = True
                record["out_elapsed_ms"] = out["elapsed_ms"]

                outcomes.append(True)

            except Exception as error:
                record = {"ok": False,
                          "error_type": type(error).__name__,
                          "error": str(error)[:200]}

                outcomes.append(False)

            record["iteration"] = index
            record["degrees"] = degrees

            records.append(record)

            ctx.measure(stage="angle_sweep", **record)

        errors = [r.get("worst_leg_error") for r in records if r.get("ok")]

        measured[str(degrees)] = {
            "failure_rate": failure_rate(outcomes),
            "error": summarize(errors),
        }

        ctx.check(measured[str(degrees)]["failure_rate"]["all_passed"],
                  "every {} degree out-and-back completed".format(degrees),
                  evidence=measured[str(degrees)]["failure_rate"])

    ctx.record("angle_sweep", **measured)

    means = [(float(k), v["error"]["mean"])
             for k, v in measured.items() if v["error"]]

    if len(means) >= 2:
        means.sort()

        growth = means[-1][1] - means[0][1]

        ctx.check(abs(growth) <= 8.0,
                  "the error does not grow with the size of the "
                  "movement",
                  evidence={"means": means, "growth": round(growth, 3)})


def _closing_error(ctx):
    ctx.require("servo.connect", "servo.test_move")

    _connect(ctx)

    repeats = ctx.iterations()
    tolerance = ctx.profile.production["servo"]["position_tolerance"]
    counts_per_rev = ctx.profile.counts_per_rev

    ctx.say("{} out-and-back half turns - this is the H-001 "
            "measurement".format(repeats))

    records, outcomes = _run_series(
        ctx, "out_and_back", repeats, "closing_error")

    closing = [abs(r["closing_error"]) for r in records
               if r.get("ok") and r.get("closing_error") is not None]

    worst_leg = [r.get("worst_leg_error") for r in records if r.get("ok")]

    distribution = summarize(closing)
    leg_distribution = summarize(worst_leg)

    rate = failure_rate(outcomes)

    ctx.result.first_failure_iteration = rate["first_failure_iteration"]

    ctx.record("closing_error", failure_rate=rate,
               closing=distribution, worst_leg=leg_distribution,
               outliers=outliers(closing), tolerance=tolerance)

    ctx.check(rate["all_passed"], "every movement completed",
              evidence=rate)

    ctx.check(bool(distribution),
              "a closing-error distribution was produced",
              evidence={"n": len(closing)})

    if not distribution:
        return

    ctx.measure(
        stage="h001_summary", n=distribution["n"],
        mean=distribution["mean"], median=distribution["median"],
        sd=distribution["sd"], p95=distribution["p95"],
        p99=distribution["p99"], worst=distribution["worst_abs"],
        worst_deg=counts_to_degrees(distribution["worst_abs"],
                                    counts_per_rev),
        tolerance=tolerance,
    )

    ctx.note(
        "H-001 MEASURED: closing error over {} symmetrical half turns - "
        "mean {}, median {}, sd {}, p95 {}, p99 {}, worst {} counts "
        "({} deg). The shipped tolerance is {} counts. Any change to "
        "ST3215_POSITION_TOLERANCE must be argued from these numbers, "
        "in a commit, with this run id attached.".format(
            distribution["n"], distribution["mean"],
            distribution["median"], distribution["sd"],
            distribution["p95"], distribution["p99"],
            distribution["worst_abs"],
            counts_to_degrees(distribution["worst_abs"], counts_per_rev),
            tolerance))

    ctx.check(distribution["p99"] <= tolerance,
              "the p99 closing error is within the shipped tolerance of "
              "{} counts".format(tolerance),
              evidence={"p99": distribution["p99"],
                        "worst": distribution["worst_abs"],
                        "tolerance": tolerance})

    if distribution["p99"] > tolerance:
        ctx.defect(
            title="the measured closing error exceeds the shipped "
                  "tolerance",
            observed="p99 {} counts, worst {} counts over {} "
                     "movements".format(
                         distribution["p99"], distribution["worst_abs"],
                         distribution["n"]),
            expected="p99 within ST3215_POSITION_TOLERANCE = {} "
                     "counts".format(tolerance),
            reproduction=("run HW-B4-003 with the same iteration count",),
            suspected_layer="mechanism repeatability, or a tolerance "
                            "that was always too tight",
            evidence={"distribution": distribution,
                      "worst_leg": leg_distribution,
                      "outliers": outliers(closing)},
        )

        ctx.note(
            "TWO READINGS ARE POSSIBLE and the distribution says which. "
            "A tight distribution centred away from zero is a "
            "systematic offset - a mechanism problem. A wide "
            "distribution centred on zero is repeatability, and the "
            "tolerance was too tight. Do not widen it without deciding "
            "which, and do not widen it to make this test pass.")


def _timing(ctx):
    ctx.require("servo.connect", "servo.test_move")

    _connect(ctx)

    repeats = ctx.iterations()
    configured = ctx.profile.production["servo"]["move_timeout_ms"]

    measured = {}

    for kind in ("slot_out_and_back", "out_and_back"):
        records, outcomes = _run_series(ctx, kind, repeats, "timing")

        elapsed = [r.get("elapsed_ms") for r in records if r.get("ok")]

        measured[kind] = {
            "failure_rate": failure_rate(outcomes),
            "elapsed": summarize(elapsed),
        }

        ctx.check(measured[kind]["failure_rate"]["all_passed"],
                  "every {} movement completed".format(kind),
                  evidence=measured[kind]["failure_rate"])

        distribution = measured[kind]["elapsed"]

        if distribution:
            ctx.check(distribution["max"] < configured * 0.5,
                      "the slowest {} took {} ms, comfortably inside "
                      "the {} ms move timeout".format(
                          kind, distribution["max"], configured),
                      evidence={"distribution": distribution,
                                "timeout_ms": configured})

    ctx.record("timing", configured_timeout_ms=configured, **measured)


def _fault_counters(ctx):
    ctx.require("servo.connect", "servo.test_move", "servo.diagnostics",
                "link.counters")

    _connect(ctx)

    movements = ctx.iterations()

    before_link = ctx.link.counters()
    before_bus = (ctx.servo.diagnostics()["data"] or {}).get("bus") or {}

    records, outcomes = _run_series(
        ctx, "slot_out_and_back", movements, "fault_counters")

    after_link = ctx.link.counters()
    after_bus = (ctx.servo.diagnostics()["data"] or {}).get("bus") or {}

    link_delta = {key: after_link.get(key, 0) - before_link.get(key, 0)
                  for key in after_link}

    bus_delta = {key: after_bus.get(key, 0) - before_bus.get(key, 0)
                 for key in after_bus
                 if isinstance(after_bus.get(key), (int, float))
                 and isinstance(before_bus.get(key), (int, float))}

    ctx.record("fault_counters", movements=movements,
               link_before=before_link, link_after=after_link,
               link_delta=link_delta, bus_before=before_bus,
               bus_after=after_bus, bus_delta=bus_delta,
               failure_rate=failure_rate(outcomes))

    ctx.check(failure_rate(outcomes)["all_passed"],
              "every movement completed",
              evidence=failure_rate(outcomes))

    for counter in ("corrupt_frames", "stale_frames", "oversized_lines"):
        ctx.check(link_delta.get(counter, 0) == 0,
                  "the transport's {} counter did not rise while the "
                  "servo bus was busy".format(counter),
                  evidence={"delta": link_delta.get(counter)})

    for counter, value in sorted(bus_delta.items()):
        if "error" in counter or "timeout" in counter or "retry" in counter:
            ctx.check(value == 0,
                      "the servo bus counter {} did not rise".format(
                          counter),
                      evidence={"delta": value,
                                "before": before_bus.get(counter),
                                "after": after_bus.get(counter)})

    ctx.measure(stage="fault_counters", movements=movements,
                **{"link_" + k: v for k, v in link_delta.items()})


def _endurance(ctx):
    ctx.require("servo.connect", "servo.test_move")

    _connect(ctx)

    movements = ctx.iterations()

    ctx.say("{} movements. Progress every 25; partial evidence is "
            "preserved if this is interrupted.".format(movements))

    records = []
    outcomes = []

    for index in range(1, movements + 1):
        try:
            transaction = ctx.servo.move("out_and_back", repeat=1)

            record = _move_record(transaction)
            record["ok"] = True

            outcomes.append(True)

        except Exception as error:
            record = {"ok": False, "error_type": type(error).__name__,
                      "error": str(error)[:200]}

            outcomes.append(False)

        record["iteration"] = index

        records.append(record)

        ctx.measure(stage="servo_endurance", **record)

        if index % 25 == 0:
            failures = outcomes.count(False)

            ctx.say("{} of {} movements, {} failures".format(
                index, movements, failures))

    rate = failure_rate(outcomes)

    ctx.result.first_failure_iteration = rate["first_failure_iteration"]

    closing = [abs(r["closing_error"]) for r in records
               if r.get("ok") and r.get("closing_error") is not None]

    tenth = max(1, len(closing) // 10)

    first_tenth = summarize(closing[:tenth])
    last_tenth = summarize(closing[-tenth:])

    ctx.record("servo_endurance", failure_rate=rate,
               closing=summarize(closing), first_tenth=first_tenth,
               last_tenth=last_tenth)

    ctx.check(rate["all_passed"],
              "every movement in the endurance series completed",
              evidence=rate)

    if first_tenth and last_tenth:
        drift = last_tenth["mean"] - first_tenth["mean"]

        ctx.check(abs(drift) <= 5.0,
                  "the closing error did not drift between the start "
                  "and the end of the run",
                  evidence={"first_tenth": first_tenth,
                            "last_tenth": last_tenth,
                            "drift": round(drift, 3)})
