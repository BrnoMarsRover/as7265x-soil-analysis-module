"""
B11 - the long runs, each separately configurable and none of them on
by default.

WHAT MAKES AN ENDURANCE TEST DIFFERENT FROM A LONG TEST

Five rules, enforced by the framework rather than by discipline:

    disabled by default     nothing here is in a default selection; each
                            must be named explicitly
    explicit confirmation   the ENDURANCE safety class asks its own
                            question, naming the duration
    bounded iterations      a ceiling in the definition and another in
                            the bench profile; zero, negative and
                            unbounded are refused by `ctx.iterations`
    periodic progress       so an operator can tell a slow run from a
                            hung one
    partial evidence        every iteration is written as it happens, so
                            an abort at iteration 4,000 leaves 4,000
                            iterations of evidence

AND THE SIXTH, WHICH IS THE POINT

An intermittent failure is never converted into a pass. `failure_rate`
reports `all_passed` only when every single iteration passed, and every
campaign here reports the first failing iteration - because "failed at
3" and "failed at 4,987" are different faults with the same failure
count.
"""

from ..core.model import (Automation, IterationKind, Requirement,
                          Safety)
from ..core.analysis import failure_rate, summarize


CAMPAIGN = "B11"


def register(registry):
    registry.campaign(
        CAMPAIGN, layer="B11", title="Endurance",
        purpose="Run each subsystem, and then the whole system, long "
                "enough for drift, heating and intermittent faults to "
                "appear.",
        prerequisites=("B8",),
        gate_note="Gated by B8. An endurance run of a system whose "
                  "single transaction has not been verified measures "
                  "how long it takes to fail.",
    )

    registry.test(
        test_id="HW-B11-001", campaign=CAMPAIGN, layer="B11",
        requirements=("HW-REQ-END-001", "HW-REQ-END-007"),
        iteration_kind=IterationKind.REQUEST,
        qualification_min_iterations=5000,
        characterization_min_iterations=500,
        title="Serial endurance",
        objective="Very many requests on one open port, looking for "
                  "accumulated corruption.",
        hardware_setup="ESP32 connected. The bench free for hours.",
        preconditions="HW-B1-006 passed.",
        procedure=(
            "open once",
            "alternate ping and get_status for N iterations",
            "record latency throughout and the counters at intervals",
            "report progress every 500 requests",
            "report the failure rate and the first failure",
        ),
        expected="Every request answered and no counter rising.",
        failure_criteria="Any unanswered request, or any corruption "
                         "counter above zero.",
        captures=("latency distribution over the run",
                  "counters at intervals",
                  "failure rate and first failing iteration"),
        safety=Safety.ENDURANCE, automation=Automation.AUTOMATIC,
        requires=("link.ping", "link.status", "link.counters"),
        run=_serial, cleanup=_close,
        default_iterations=5000, max_iterations=100000,
        prerequisites=("HW-B1-006",),
        defect_prefix="HW-SER",
    )

    registry.test(
        test_id="HW-B11-002", campaign=CAMPAIGN, layer="B11",
        requirements=("HW-REQ-END-002",),
        iteration_kind=IterationKind.MEASUREMENT,
        qualification_min_iterations=1000,
        characterization_min_iterations=100,
        title="Sensor endurance",
        objective="Very many acquisitions, looking for heating, drift "
                  "and the intermittent initialization fault.",
        hardware_setup="Sensor connected, target unmoved. Hours.",
        preconditions="HW-B7-003 passed.",
        procedure=(
            "acquire the full triad N times",
            "validate every one",
            "record timing and temperature throughout",
            "compare the first and last tenth",
        ),
        expected="Every acquisition well formed.",
        failure_criteria="Any failure or malformed acquisition.",
        captures=("per-iteration result and timing",
                  "the first and last tenth compared",
                  "failure rate and first failing iteration"),
        safety=Safety.ENDURANCE, automation=Automation.AUTOMATIC,
        requires=("sensor.acquire_triad",),
        run=_sensor, cleanup=_close,
        default_iterations=1000, max_iterations=20000,
        prerequisites=("HW-B7-003",),
        assumption="H-007", defect_prefix="HW-SENSOR",
    )

    registry.test(
        test_id="HW-B11-003", campaign=CAMPAIGN, layer="B11",
        requirements=("HW-REQ-END-003",),
        iteration_kind=IterationKind.MOVEMENT,
        qualification_min_iterations=1000,
        characterization_min_iterations=100,
        title="Servo endurance",
        objective="Very many movements, looking for wear, a loosening "
                  "coupling and thermal drift.",
        hardware_setup="Carousel attached and EMPTY, mechanism free. "
                       "Hours of movement.",
        preconditions="HW-B4-006 passed.",
        procedure=(
            "command the symmetrical out-and-back movement N times",
            "record the closing error of each",
            "compare the first and last tenth",
        ),
        expected="Every movement completes and the closing error does "
                 "not drift.",
        failure_criteria="Any failed movement, or a closing error trend "
                         "across the run.",
        captures=("every closing error with its iteration",
                  "the first and last tenth",
                  "failure rate and first failing iteration"),
        safety=Safety.ENDURANCE, automation=Automation.AUTOMATIC,
        requires=("servo.test_move", "servo.connect"),
        run=_servo, cleanup=_stop,
        default_iterations=1000, max_iterations=20000,
        prerequisites=("HW-B4-006",),
        assumption="H-006", defect_prefix="HW-SERVO",
    )

    registry.test(
        test_id="HW-B11-004", campaign=CAMPAIGN, layer="B11",
        requirements=("HW-REQ-END-004",),
        iteration_kind=IterationKind.ROTATION,
        qualification_min_iterations=50,
        characterization_min_iterations=10,
        title="Carousel endurance",
        objective="Many full rotations, looking for accumulated "
                  "position drift.",
        hardware_setup="Carousel attached and EMPTY. Hours.",
        preconditions="HW-B5-005 passed.",
        procedure=(
            "walk the full slot sequence N times",
            "record the encoder position at slot 1 each rotation",
            "record the cumulative drift",
            "report how many rotations pass before a re-sync would be "
            "needed",
        ),
        expected="No cumulative drift beyond the measured "
                 "repeatability.",
        failure_criteria="Drift that grows with rotations. The result "
                         "then tells the operator how often to re-sync, "
                         "which is a real operational answer.",
        captures=("position at slot 1 per rotation",
                  "cumulative drift", "the rotation at which drift "
                  "exceeds tolerance"),
        safety=Safety.ENDURANCE, automation=Automation.AUTOMATIC,
        requires=("carousel.select_slot", "carousel.status",
                  "servo.read_position"),
        run=_carousel, cleanup=_stop,
        default_iterations=50, max_iterations=2000,
        prerequisites=("HW-B5-005",),
        assumption="H-006", defect_prefix="HW-CAR",
    )

    registry.test(
        test_id="HW-B11-005", campaign=CAMPAIGN, layer="B11",
        requirements=("HW-REQ-END-005",),
        iteration_kind=IterationKind.MEASUREMENT,
        qualification_min_iterations=50,
        characterization_min_iterations=10,
        title="Full-system endurance",
        objective="The complete measurement transaction, repeated, "
                  "which is the only test that stresses everything at "
                  "once.",
        hardware_setup="The complete module, carousel loaded with "
                       "whatever will be measured, sensor connected. "
                       "Hours.",
        preconditions="HW-B8-001 and every other B11 test passed.",
        procedure=(
            "run measure_raw against each slot in turn, N times round",
            "validate every measurement",
            "record timing, position error and position validity "
            "throughout",
            "report progress every 10 measurements",
            "stop and report if the position ever becomes invalid",
        ),
        expected="Every measurement completes with a valid position "
                 "throughout.",
        failure_criteria="Any failure, or a position that becomes "
                         "invalid during an unattended run - which "
                         "would strand the mechanism.",
        captures=("per-measurement result, timing and position error",
                  "the iteration at which anything first went wrong",
                  "the position validity over the whole run"),
        safety=Safety.ENDURANCE, automation=Automation.AUTOMATIC,
        requires=("carousel.measure", "carousel.status",
                  "carousel.sync"),
        run=_full_system, cleanup=_stop,
        default_iterations=50, max_iterations=1000,
        prerequisites=("HW-B8-001",),
        defect_prefix="HW-INT",
    )

    registry.test(
        test_id="HW-B11-006", campaign=CAMPAIGN, layer="B11",
        requirements=("HW-REQ-END-006",),
        title="Partial evidence survives an interrupted endurance run",
        objective="Prove that a long run stopped part way leaves every "
                  "iteration it completed on disk, because partial "
                  "evidence is the only evidence an interrupted run can "
                  "produce.",
        hardware_setup="ESP32 connected. The operator will interrupt "
                       "the run with Ctrl+C part way through.",
        preconditions="HW-B11-001 passed.",
        procedure=(
            "start a bounded request series",
            "record every iteration as it happens, flushing each",
            "ask the operator to interrupt with Ctrl+C part way",
            "on abort, count what reached the evidence file",
            "check the count matches the iterations that completed",
        ),
        expected="Every completed iteration is on disk when the run is "
                 "interrupted, and the result is ABORTED rather than a "
                 "verdict.",
        failure_criteria="Iterations that completed but were not "
                         "written, or a run that reports a verdict "
                         "after being interrupted. An aborted run has "
                         "not established anything.",
        captures=("iterations completed before the interruption",
                  "iterations present in the evidence file",
                  "the abort record and the cleanup result"),
        safety=Safety.ENDURANCE,
        automation=Automation.OPERATOR_ASSISTED,
        requires=("link.ping",),
        run=_partial_evidence, cleanup=_close,
        iteration_kind=IterationKind.REQUEST,
        default_iterations=500, max_iterations=20000,
        characterization_min_iterations=20,
        defect_prefix="HW-SER",
    )

    registry.test(
        test_id="HW-B11-007", campaign=CAMPAIGN, layer="B11",
        requirements=("HW-REQ-THERM-001",),
        title="Component temperatures after bounded endurance",
        objective="Measure how warm the ESP32, the regulator, the servo "
                  "driver, the servo and the sensor get after a bounded "
                  "run, so a thermal fault later has a baseline.",
        hardware_setup="A thermal probe with safe access to each "
                       "component. The module in its normal operating "
                       "position - measuring a board lying in free air "
                       "measures the bench, not the module.",
        preconditions="A B11 endurance run has just finished, or one is "
                      "run as part of this test.",
        procedure=(
            "record the ambient temperature first",
            "measure each component cold",
            "run a bounded movement and acquisition series",
            "measure each component again immediately afterwards",
            "record the rise over ambient for each",
        ),
        expected="Every component is measurable cold and warm, and the "
                 "rise over ambient is recorded.",
        failure_criteria="A component that cannot be measured safely - "
                         "which is BLOCKED, not a failure. The "
                         "TEMPERATURES are characterization: the "
                         "datasheets have the limits and this "
                         "repository does not, so nothing here judges "
                         "them.",
        captures=("ambient temperature",
                  "each component cold and warm",
                  "the rise over ambient",
                  "what the run consisted of",
                  "the measuring instrument"),
        safety=Safety.ENDURANCE,
        automation=Automation.OPERATOR_ASSISTED,
        requires=("bench.thermal_probe", "servo.test_move",
                  "sensor.acquire_triad"),
        run=_thermal_after_endurance, cleanup=_stop,
        iteration_kind=IterationKind.MOVEMENT,
        default_iterations=50, max_iterations=2000,
        characterization_min_iterations=20,
        defect_prefix="HW-PWR",
    )


# ======================================================================
# shared
# ======================================================================

def _close(ctx):
    record = ctx.link.close(reason="B11 cleanup")

    return {"confirmed": bool(record.get("closed")),
            "port_released": record.get("closed")}


def _stop(ctx):
    from .b3_h002 import _stop_and_release as shared

    return shared(ctx)


def _progress(ctx, index, total, failures, every=100):
    if index % every == 0:
        ctx.say("{} of {}, {} failures".format(index, total, failures))


def _raise_if_intermittent(ctx, rate, what, defect_title, layer,
                           evidence):
    """
    One rule, applied identically to every endurance campaign.

    An intermittent failure is a failure. `all_passed` is the only thing
    that produces a pass, and a defect is raised the moment it is false
    - with the first failing iteration, because that is what separates
    "broken from the start" from "broke after four thousand".
    """
    ctx.check(rate["all_passed"],
              "every {} in the endurance run succeeded".format(what),
              evidence=rate)

    if not rate["all_passed"]:
        ctx.defect(
            title=defect_title,
            observed="{} of {} iterations failed, first at iteration "
                     "{} ({}% failure rate)".format(
                         rate["failed"], rate["iterations"],
                         rate["first_failure_iteration"],
                         rate["failure_rate_pct"]),
            expected="every iteration succeeds",
            reproduction=("re-run the same test with the same iteration "
                          "count",),
            suspected_layer=layer,
            evidence=evidence,
        )


# ======================================================================
# bodies
# ======================================================================

def _serial(ctx):
    ctx.require("link.ping", "link.status", "link.counters")

    total = ctx.iterations()

    ctx.link.require_link("open once for the serial endurance run")

    before = ctx.link.counters()

    outcomes = []
    latencies = []

    for index in range(1, total + 1):
        command = "ping" if index % 2 else "get_status"

        try:
            transaction = ctx.link.request(command, retries=0)

            outcomes.append(True)
            latencies.append(transaction["elapsed_ms"])

        except Exception as error:
            outcomes.append(False)

            ctx.measure(stage="serial_endurance", iteration=index,
                        command=command, ok=False,
                        error=str(error)[:200])

        if index % 500 == 0:
            counters = ctx.link.counters()

            ctx.measure(stage="serial_counters", iteration=index,
                        **counters)

        _progress(ctx, index, total, outcomes.count(False), every=500)

    after = ctx.link.counters()

    delta = {key: after.get(key, 0) - before.get(key, 0) for key in after}

    rate = failure_rate(outcomes)

    ctx.result.first_failure_iteration = rate["first_failure_iteration"]

    ctx.record("serial_endurance", failure_rate=rate,
               latency=summarize(latencies), counters_delta=delta)

    _raise_if_intermittent(
        ctx, rate, "request",
        "the serial link failed during an endurance run",
        "transport", {"failure_rate": rate, "counters_delta": delta})

    for counter in ("corrupt_frames", "stale_frames", "oversized_lines"):
        ctx.check(delta.get(counter, 0) == 0,
                  "the {} counter did not rise over {} "
                  "requests".format(counter, total),
                  evidence={"delta": delta.get(counter)})


def _sensor(ctx):
    ctx.require("sensor.acquire_triad")

    total = ctx.iterations()

    outcomes = []
    elapsed = []

    for index in range(1, total + 1):
        try:
            transaction = ctx.sensor.triad()

            problems = ctx.sensor.validate_triad(transaction["data"] or {})

            outcomes.append(not problems)
            elapsed.append(transaction["elapsed_ms"])

            if problems:
                ctx.measure(stage="sensor_endurance", iteration=index,
                            ok=False, problems=len(problems))

        except Exception as error:
            outcomes.append(False)

            ctx.measure(stage="sensor_endurance", iteration=index,
                        ok=False, error=str(error)[:200])

        _progress(ctx, index, total, outcomes.count(False), every=50)

    rate = failure_rate(outcomes)

    ctx.result.first_failure_iteration = rate["first_failure_iteration"]

    tenth = max(1, len(elapsed) // 10)

    ctx.record("sensor_endurance", failure_rate=rate,
               elapsed=summarize(elapsed),
               first_tenth=summarize(elapsed[:tenth]),
               last_tenth=summarize(elapsed[-tenth:]))

    _raise_if_intermittent(
        ctx, rate, "acquisition",
        "the sensor failed during an endurance run",
        "sensor or I2C bus", {"failure_rate": rate})


def _servo(ctx):
    ctx.require("servo.test_move", "servo.connect")

    ctx.link.request("connect_servo", timeout=ctx.servo._move_timeout())

    total = ctx.iterations()

    outcomes = []
    closing = []

    for index in range(1, total + 1):
        try:
            transaction = ctx.servo.move("out_and_back", repeat=1)

            answer = transaction["data"] or {}

            error = answer.get("closed_loop_error_counts")

            outcomes.append(True)

            if error is not None:
                closing.append(abs(error))

            if index % 25 == 0:
                ctx.measure(stage="servo_endurance", iteration=index,
                            ok=True, closing_error=error,
                            elapsed_ms=transaction["elapsed_ms"])

        except Exception as failure:
            outcomes.append(False)

            ctx.measure(stage="servo_endurance", iteration=index,
                        ok=False, error=str(failure)[:200])

        _progress(ctx, index, total, outcomes.count(False), every=50)

    rate = failure_rate(outcomes)

    ctx.result.first_failure_iteration = rate["first_failure_iteration"]

    tenth = max(1, len(closing) // 10)

    first_tenth = summarize(closing[:tenth])
    last_tenth = summarize(closing[-tenth:])

    ctx.record("servo_endurance", failure_rate=rate,
               closing=summarize(closing), first_tenth=first_tenth,
               last_tenth=last_tenth)

    _raise_if_intermittent(
        ctx, rate, "movement",
        "the servo failed during an endurance run",
        "servo or mechanism", {"failure_rate": rate})

    if first_tenth and last_tenth:
        drift = last_tenth["mean"] - first_tenth["mean"]

        ctx.check(abs(drift) <= 5.0,
                  "the closing error did not drift across {} "
                  "movements".format(total),
                  evidence={"first_tenth": first_tenth,
                            "last_tenth": last_tenth,
                            "drift": round(drift, 3)})


def _carousel(ctx):
    ctx.require("carousel.select_slot", "carousel.status",
                "servo.read_position")

    ctx.link.request("connect_servo", timeout=ctx.servo._move_timeout())
    ctx.carousel.sync(load_slot=1)

    total = ctx.iterations()
    sequence = ctx.carousel.full_rotation_sequence()
    tolerance = ctx.profile.production["servo"]["position_tolerance"]

    ctx.carousel.select_slot(1)

    origin = ctx.servo.position()

    outcomes = []
    drifts = []
    first_exceeding = None

    for rotation in range(1, total + 1):
        ok = True

        for slot in sequence[1:]:
            try:
                ctx.carousel.select_slot(slot)

            except Exception as error:
                ok = False

                ctx.measure(stage="carousel_endurance", rotation=rotation,
                            slot=slot, ok=False, error=str(error)[:200])

                break

        outcomes.append(ok)

        here = ctx.servo.position()

        drift = (here - origin) if None not in (here, origin) else None

        drifts.append(drift)

        if (drift is not None and abs(drift) > tolerance
                and first_exceeding is None):
            first_exceeding = rotation

        ctx.measure(stage="carousel_endurance", rotation=rotation,
                    ok=ok, position=here, cumulative_drift=drift)

        _progress(ctx, rotation, total, outcomes.count(False), every=10)

    rate = failure_rate(outcomes)

    ctx.result.first_failure_iteration = rate["first_failure_iteration"]

    known = [d for d in drifts if d is not None]

    ctx.record("carousel_endurance", failure_rate=rate,
               drifts=known, distribution=summarize(known),
               first_rotation_exceeding_tolerance=first_exceeding,
               tolerance=tolerance)

    _raise_if_intermittent(
        ctx, rate, "rotation",
        "the carousel failed during an endurance run",
        "carousel or servo", {"failure_rate": rate, "drifts": known[:50]})

    ctx.check(first_exceeding is None,
              "the cumulative drift stayed within the position "
              "tolerance for all {} rotations".format(total),
              evidence={"first_exceeding": first_exceeding,
                        "tolerance": tolerance,
                        "final_drift": known[-1] if known else None})

    if first_exceeding is not None:
        ctx.note(
            "The drift exceeded the position tolerance at rotation {}. "
            "That is the operational answer to 'how often must the "
            "carousel be re-synchronized' - record it in "
            "Documentation/OPERATIONS.md.".format(first_exceeding))


def _full_system(ctx):
    ctx.require("carousel.measure", "carousel.status", "carousel.sync")

    ctx.link.request("connect_servo", timeout=ctx.servo._move_timeout())
    ctx.carousel.sync(load_slot=1)

    rounds = ctx.iterations()
    count = ctx.carousel.slot_count()

    outcomes = []
    elapsed = []
    position_lost_at = None

    for index in range(1, rounds + 1):
        slot = (index - 1) % count + 1

        try:
            transaction = ctx.carousel.measure(
                slot, sample_id="HW-B11-005-{}".format(index))

            answer = transaction["data"] or {}

            carousel = answer.get("carousel") or {}

            valid = carousel.get("position_valid")

            if valid is False and position_lost_at is None:
                position_lost_at = index

            outcomes.append(True)
            elapsed.append(transaction["elapsed_ms"])

            ctx.measure(stage="full_system", iteration=index, slot=slot,
                        ok=True, elapsed_ms=transaction["elapsed_ms"],
                        position_valid=valid)

        except Exception as error:
            outcomes.append(False)

            ctx.measure(stage="full_system", iteration=index, slot=slot,
                        ok=False, error=str(error)[:200])

        _progress(ctx, index, rounds, outcomes.count(False), every=10)

    rate = failure_rate(outcomes)

    ctx.result.first_failure_iteration = rate["first_failure_iteration"]

    ctx.record("full_system", failure_rate=rate,
               elapsed=summarize(elapsed),
               position_lost_at=position_lost_at)

    _raise_if_intermittent(
        ctx, rate, "measurement",
        "the full system failed during an endurance run",
        "integration", {"failure_rate": rate})

    ctx.check(position_lost_at is None,
              "the carousel position stayed valid for the whole run",
              evidence={"lost_at": position_lost_at})


def _partial_evidence(ctx):
    ctx.require("link.ping")

    total = ctx.iterations()

    ctx.link.require_link("run a series that will be interrupted")

    ctx.instruct(
        "A series of {} requests is about to start. Let it run for a "
        "while, then press Ctrl+C to interrupt it. This test is about "
        "what survives the interruption.".format(total))

    completed = 0

    for index in range(1, total + 1):
        ctx.link.request("ping", retries=0)

        completed = index

        # Every iteration is written as it happens. A run killed at
        # iteration 4,000 must leave 4,000 iterations behind, and that
        # is only true if nothing is buffered until the end.
        ctx.measure(stage="partial_evidence", iteration=index, ok=True)

        if index % 50 == 0:
            ctx.say("{} of {} - interrupt whenever you like".format(
                index, total))

    # Reaching here means the operator let it finish, which is a
    # legitimate outcome but does not exercise what the test is for.
    ctx.check(True,
              "the series completed without interruption ({} "
              "iterations)".format(completed))

    ctx.record("partial_evidence", completed=completed,
               interrupted=False)

    ctx.inconclusive(
        "the run was not interrupted, so whether partial evidence "
        "survives an interruption was never exercised. Re-run and press "
        "Ctrl+C part way.",
        missing=("an interruption",),
        evidence={"completed": completed})


def _thermal_after_endurance(ctx):
    ctx.require("bench.thermal_probe", "servo.test_move",
                "sensor.acquire_triad")

    probe = ctx.bench.require_instrument("thermal_probe")

    ctx.record("instrument", **probe)

    components = ("ESP32", "regulator", "servo driver", "servo",
                  "AS7265x")

    ctx.confirm_observation(
        "Is the module in its normal operating position - not lying in "
        "free air, which would measure the bench rather than the "
        "module")

    ambient = ctx.ask_number(
        "Measure the ambient temperature", minimum=-40, maximum=120,
        unit="C")

    if ambient is None:
        ctx.result.record_missing_required(
            "the ambient temperature was not measured")

    cold = {}

    for component in components:
        cold[component] = ctx.ask_number(
            "Measure the {} temperature, cold".format(component),
            minimum=-40, maximum=200, unit="C")

    ctx.record("thermal_cold", ambient=ambient, **cold)

    movements = ctx.iterations()

    ctx.say("running {} movements and acquisitions to warm the "
            "module".format(movements))

    ctx.link.request("connect_servo", timeout=ctx.servo._move_timeout())

    for index in range(1, movements + 1):
        try:
            ctx.servo.move("slot_out_and_back", repeat=1)

            if index % 5 == 0:
                ctx.sensor.triad()

        except Exception as error:
            ctx.measure(stage="thermal_run", iteration=index, ok=False,
                        error=str(error)[:200])

            continue

        if index % 10 == 0:
            ctx.say("{} of {}".format(index, movements))

    warm = {}

    ctx.instruct(
        "The run has finished. Measure each component NOW, before it "
        "cools.")

    for component in components:
        warm[component] = ctx.ask_number(
            "Measure the {} temperature, warm".format(component),
            minimum=-40, maximum=200, unit="C")

    rises = {}

    for component in components:
        if warm[component] is not None and ambient is not None:
            rises[component] = round(warm[component] - ambient, 2)

        ctx.measure(stage="thermal", component=component,
                    ambient=ambient, cold=cold[component],
                    warm=warm[component],
                    rise_over_ambient=rises.get(component),
                    instrument=probe.get("model") or "")

    measured = [c for c in components if warm[c] is not None]

    ctx.check(len(measured) == len(components),
              "every component was measured warm",
              evidence={"measured": measured})

    ctx.record("thermal", ambient=ambient, cold=cold, warm=warm,
               rise_over_ambient=rises, movements=movements)

    ctx.characterize(
        "component temperatures after {} movements: {}. The datasheets "
        "have the limits and this repository does not, so this is the "
        "baseline a later thermal fault is compared against rather than "
        "a judgement.".format(
            movements,
            ", ".join("{} +{}C".format(k, v)
                      for k, v in sorted(rises.items()))))
