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

from ..core.model import Automation, Safety
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
