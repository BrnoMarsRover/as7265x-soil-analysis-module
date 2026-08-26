"""
B3 - H-002: does the encoder track the mechanism?

THE CONTRADICTION, AS OBSERVED ON THE BENCH

    physical carousel movement    about 180 degrees
    reported encoder movement     about 2 counts
    expected movement             about 2048 counts

The firmware refused the measurement, which was correct. What is not
known is WHY the encoder and the operator disagreed.

WHAT SOFTWARE HAS ALREADY SETTLED, SO B3 DOES NOT RE-ASK IT

`centred_error` is correct at all 4096 positions. `expected =
wrap_counts(start + requested)` is right. The tolerance comparison is
right, inclusive, and has no seam at 4095/0. Given the readings the
driver received, the refusal was the only correct outcome. The
arithmetic is not a hypothesis.

THE HYPOTHESES THIS CAMPAIGN SEPARATES, NONE OF THEM CHOSEN IN ADVANCE

    1  the servo is in position mode, not STEP  -> HW-B2-002
    2  counts_per_rev is not the real resolution -> HW-B3-002
    3  present-position does not update in STEP  -> HW-B3-001 + B3-003
    4  the servo moves and the encoder does not  -> HW-B3-001
    5  the read races the movement               -> HW-B2-004, HW-B3-003
    6  the response is stale or belongs to
       another command                           -> HW-B3-003
    7  a reduction between servo and carousel    -> HW-B3-005
    8  byte order in the position register       -> HW-B3-004, BLOCKED
    9  the servo id or bus is not what we think  -> HW-B2-003
    10 mechanical slip                           -> HW-B3-001 repeats

THE MEASUREMENT IS A PAIRING, NOT A NUMBER

Every leg records what was commanded, what the encoder said, and what a
human saw with a protractor. It is the DISAGREEMENT between the second
and the third that is the finding, so a leg with no operator observation
is not evidence and the framework will not pretend otherwise.

ST3215_POSITION_TOLERANCE IS NOT TOUCHED BY THIS CAMPAIGN. Nothing here
writes to config.py, and nothing here may be used to argue for widening
it. That number comes from HW-B4-003's measured distribution.
"""

from ..core.model import Automation, Safety
from ..core.analysis import (byte_order_interpretations, centred_error,
                               counts_to_degrees, degrees_to_counts,
                               summarize)


CAMPAIGN = "B3"

# The angle series the investigation walks, forward then reverse. Small
# angles first: if the encoder is going to disagree, finding out at 10
# degrees costs less than finding out at 360.
ANGLE_SERIES = (10.0, 45.0, 90.0, 180.0, 360.0)


def register(registry):
    registry.campaign(
        CAMPAIGN, layer="B3",
        title="H-002: encoder versus physical movement",
        purpose="Establish the relationship between a commanded goal, "
                "the reported position, the encoder count and the "
                "angle a human measures on the output shaft. Until "
                "this is settled no carousel result means anything.",
        prerequisites=("B2",),
        gate_note="Gated by B2: the mode, the id, the baud and the "
                  "stability of a position read are all cheaper "
                  "explanations and must be eliminated first.",
    )

    registry.test(
        test_id="HW-B3-001", campaign=CAMPAIGN, layer="B3",
        title="Commanded angle versus encoder versus protractor",
        objective="Measure, for each of +10, +45, +90, +180 and +360 "
                  "degrees and each reverse, what was commanded, what "
                  "the encoder reported, and what a human measured on "
                  "the output shaft.",
        hardware_setup="Servo connected and in STEP mode. THE CAROUSEL "
                       "SHOULD BE DETACHED IF IT CAN BE - this test "
                       "asks about the servo output shaft, and a "
                       "coupling that slips is one of the hypotheses. "
                       "If it cannot be detached, mark the plate and "
                       "measure the plate, and say so in the notes. A "
                       "protractor or an angle gauge is required.",
        preconditions="HW-B2-002 passed with mode_correct true. "
                      "HW-B2-004 passed - a position read is stable at "
                      "rest.",
        procedure=(
            "read the position before the leg",
            "command the angle through servo_test_move kind 'degrees', "
            "splitting anything over half a revolution into legs the "
            "driver can verify",
            "record the raw command payload and the raw answer",
            "read the position after the leg",
            "compute the reported delta as a circular difference",
            "ask the operator for the observed direction",
            "ask the operator for the observed angle in degrees",
            "record the elapsed time and any protocol or status flags",
            "repeat for every angle, forward then reverse",
        ),
        expected="For every leg: reported delta equals the commanded "
                 "count to within the position tolerance, AND the "
                 "operator's measured angle equals the commanded angle "
                 "to within the accuracy of the measurement.",
        failure_criteria="Any leg where the encoder and the protractor "
                         "disagree. The SHAPE of the disagreement is "
                         "the diagnosis: a constant ratio points at a "
                         "reduction or at counts_per_rev; a near-zero "
                         "encoder delta with real movement points at "
                         "the position register or at STEP mode; "
                         "movement in the wrong direction points at the "
                         "sign convention; no movement at all with a "
                         "correct encoder delta points at the coupling.",
        captures=("position before", "commanded goal", "commanded delta "
                  "in counts and degrees", "expected direction",
                  "the raw command", "the raw answer", "position after",
                  "reported delta", "observed physical direction",
                  "observed physical angle", "elapsed time",
                  "protocol, checksum and status flags",
                  "operator notes"),
        safety=Safety.MOTION, automation=Automation.OPERATOR_ASSISTED,
        requires=("servo.test_move", "servo.read_position",
                  "servo.connect"),
        run=_encoder_versus_physical, cleanup=_stop_and_release,
        assumption="H-002", defect_prefix="HW-SERVO",
        notes="Replaces HW-204. This is the single most important test "
              "in the whole hardware campaign.",
    )

    registry.test(
        test_id="HW-B3-002", campaign=CAMPAIGN, layer="B3",
        title="Encoder resolution from a measured full revolution",
        objective="Establish how many counts one physical revolution of "
                  "the output shaft actually is, rather than assuming "
                  "4096.",
        hardware_setup="As HW-B3-001. The operator must be able to see "
                       "a reference mark return to its starting point.",
        preconditions="HW-B3-001 completed - whatever its verdict.",
        procedure=(
            "read the position",
            "command 180 degrees twice, so a full revolution is walked "
            "in two verified legs",
            "read the position after each leg",
            "ask the operator whether the reference mark returned "
            "exactly to its start",
            "compute the counts the encoder reports for one observed "
            "revolution",
            "compare with ST3215_COUNTS_PER_REV",
        ),
        expected="One observed revolution equals the configured counts "
                 "per revolution, to within the position tolerance.",
        failure_criteria="A different number. Then every angle in the "
                         "firmware is wrong by that ratio, which is "
                         "hypothesis 2 of H-002 and would also explain "
                         "H-005.",
        captures=("each leg's positions", "the total reported counts",
                  "whether the mark returned to its start",
                  "the derived counts per revolution",
                  "the configured value compared against"),
        safety=Safety.MOTION, automation=Automation.OPERATOR_ASSISTED,
        requires=("servo.test_move", "servo.read_position",
                  "servo.connect"),
        run=_encoder_resolution, cleanup=_stop_and_release,
        assumption="H-005", defect_prefix="HW-SERVO",
    )

    registry.test(
        test_id="HW-B3-003", campaign=CAMPAIGN, layer="B3",
        title="Is the position answer fresh, or left over?",
        objective="Separate 'the encoder did not move' from 'we were "
                  "given an old answer'.",
        hardware_setup="Servo connected, carousel free.",
        preconditions="HW-B2-004 passed.",
        procedure=(
            "read the position three times and confirm it is stable",
            "command one small movement",
            "read the position immediately, then again after the "
            "configured settle time, then again after twice that",
            "repeat the whole sequence several times",
            "check whether a reading taken later differs from one taken "
            "immediately",
        ),
        expected="The immediate reading and the settled reading agree. "
                 "The servo has finished moving before the driver "
                 "returns.",
        failure_criteria="A reading that keeps changing after the "
                         "driver declared the movement complete. Then "
                         "the settle time is too short and every "
                         "position error in every campaign is measured "
                         "mid-motion - hypothesis 5, and it would make "
                         "the whole tolerance question meaningless.",
        captures=("each triple of readings",
                  "the difference between immediate and settled",
                  "the configured settle time"),
        safety=Safety.MOTION, automation=Automation.AUTOMATIC,
        requires=("servo.test_move", "servo.read_position",
                  "servo.connect"),
        run=_reading_freshness, cleanup=_stop_and_release,
        default_iterations=5, max_iterations=100,
        assumption="H-002", defect_prefix="HW-SERVO",
    )

    registry.test(
        test_id="HW-B3-004", campaign=CAMPAIGN, layer="B3",
        title="Byte-order interpretation of the raw position register",
        objective="Read the position register as bytes and check "
                  "whether an alternative byte order would explain the "
                  "observed contradiction.",
        hardware_setup="Servo connected.",
        preconditions="HW-B3-001 has produced a disagreement worth "
                      "explaining.",
        procedure=(
            "read the present-position register as raw bytes",
            "record the value as read, byte swapped, low byte only and "
            "high byte only",
            "compare each interpretation with the commanded count",
        ),
        expected="The value as the driver reads it is the one that "
                 "matches the commanded movement.",
        failure_criteria="An alternative interpretation matching better "
                         "than the driver's, which would be a driver "
                         "defect - hypothesis 8.",
        captures=("raw bytes", "each interpretation",
                  "the commanded count compared against"),
        safety=Safety.COMMUNICATION, automation=Automation.AUTOMATIC,
        requires=("servo.raw_packet",),
        run=_byte_order, cleanup=_stop_and_release,
        assumption="H-002", defect_prefix="HW-SERVO",
        notes="BLOCKED: no raw servo register access in the shipped "
              "firmware. HW-B3-001 records the same interpretations "
              "against the PARSED value as a diagnostic hint, which is "
              "weaker but needs no firmware change.",
    )

    registry.test(
        test_id="HW-B3-005", campaign=CAMPAIGN, layer="B3",
        title="Servo-to-carousel ratio from measured angles",
        objective="Derive the ratio between the servo's reported "
                  "movement and the carousel's physical movement, so "
                  "hypothesis 7 is a number rather than a suspicion.",
        hardware_setup="Carousel ATTACHED, with a reference mark. A "
                       "protractor or angle gauge.",
        preconditions="HW-B3-001 completed.",
        procedure=(
            "command 90 degrees of servo movement",
            "ask the operator for the angle the CAROUSEL PLATE moved",
            "repeat three times and average",
            "compute plate degrees divided by commanded servo degrees",
            "compare with the profile's assumed gear ratio",
        ),
        expected="A ratio of 1.0 within measurement accuracy, matching "
                 "the profile's assumption.",
        failure_criteria="Any other ratio. The mechanism has a "
                         "reduction, every angle in the firmware is "
                         "wrong by it, and the profile's ASSUMED ratio "
                         "must be replaced by this MEASURED one - in "
                         "the profile, with the run id, never silently.",
        captures=("commanded servo degrees per trial",
                  "encoder-reported degrees per trial",
                  "operator-measured plate degrees per trial",
                  "the derived ratio", "the assumed ratio"),
        safety=Safety.MOTION, automation=Automation.OPERATOR_ASSISTED,
        requires=("servo.test_move", "servo.read_position",
                  "servo.connect"),
        run=_gear_ratio, cleanup=_stop_and_release,
        assumption="H-005", defect_prefix="HW-CAR",
    )


# ======================================================================
# shared
# ======================================================================

def _stop_and_release(ctx):
    """
    Stop the servo if it can still be reached, then release the port.

    A stop that cannot be delivered is recorded as unconfirmed. After an
    aborted movement the position is unknown by definition, and this
    cleanup says so rather than quietly leaving a stale slot number in
    place.
    """
    record = {"confirmed": False}

    try:
        ctx.servo.stop()

        record["servo_stopped"] = True

    except Exception as error:
        record["servo_stopped"] = False
        record["stop_error"] = "{}: {}".format(type(error).__name__, error)

    try:
        state = ctx.carousel.position_state()

        record["position_state"] = state

    except Exception as error:                         # pragma: no cover
        record["position_state"] = {
            "state": "POSITION_UNREADABLE",
            "reason": str(error),
        }

    closed = ctx.link.close(reason="B3 cleanup")

    record["port_released"] = closed.get("closed")
    record["confirmed"] = bool(
        record.get("servo_stopped") and closed.get("closed"))

    if not record["confirmed"]:
        record["note"] = (
            "the servo could not be confirmed stopped, so the physical "
            "carousel position after this test is UNKNOWN and a re-sync "
            "is required before any slot-addressed movement")

    return record


def _connect_servo(ctx):
    ctx.link.request("connect_servo", timeout=ctx.servo._move_timeout())


def _leg_evidence(ctx, commanded_degrees, before, transaction):
    """
    Everything one leg produced, from both sides of the disagreement.

    Built in one place so that HW-B3-001, HW-B3-002 and HW-B3-005 all
    record the same fields and their rows can be compared.
    """
    counts_per_rev = ctx.profile.counts_per_rev

    answer = (transaction or {}).get("data") or {}

    after = None

    for key in ("end_position", "actual_position"):
        if answer.get(key) is not None:
            after = answer[key]

            break

    reported_delta = None

    if before is not None and after is not None:
        reported_delta = centred_error(after - before, counts_per_rev)

    commanded_counts = degrees_to_counts(commanded_degrees, counts_per_rev)

    return {
        "commanded_degrees": commanded_degrees,
        "commanded_counts": commanded_counts,
        "expected_direction": (
            "CW" if commanded_degrees > 0 else
            "CCW" if commanded_degrees < 0 else "NONE"),
        "position_before": before,
        "position_after": after,
        "reported_delta_counts": reported_delta,
        "reported_delta_degrees": counts_to_degrees(
            reported_delta, counts_per_rev),
        "net_counts_reported": answer.get("net_counts"),
        "closing_error_counts": answer.get("closed_loop_error_counts"),
        "worst_position_error": answer.get("worst_position_error"),
        "elapsed_ms": (transaction or {}).get("elapsed_ms"),
        "legs": answer.get("legs"),
        "movements": answer.get("movements"),
        "raw_request": {
            "command": (transaction or {}).get("command"),
            "payload": (transaction or {}).get("payload"),
        },
        "byte_order_hint": byte_order_interpretations(after),
    }


# ======================================================================
# bodies
# ======================================================================

def _encoder_versus_physical(ctx):
    ctx.require("servo.connect", "servo.test_move", "servo.read_position")

    _connect_servo(ctx)

    counts_per_rev = ctx.profile.counts_per_rev
    tolerance = ctx.profile.production["servo"]["position_tolerance"]

    ctx.note(
        "ST3215_POSITION_TOLERANCE is {} counts and is NOT changed by "
        "this test. It is quoted here only to say which legs the "
        "firmware itself would have accepted.".format(tolerance))

    ctx.note(
        "Two legs in this series are circularly ambiguous to the "
        "encoder and are judged accordingly: a 180 degree movement "
        "gives the same reading in both directions, and a 360 degree "
        "movement returns a delta of zero. The operator's direction and "
        "angle observations are what carry the sign for those, which is "
        "the whole reason this test is operator-assisted.")

    ctx.confirm_observation(
        "Do you have a protractor or angle gauge, and a reference mark "
        "you can measure against")

    detached = ctx.ask(
        "Is the carousel plate DETACHED from the servo output shaft? "
        "(answer no if you are measuring the plate itself)")

    ctx.record("setup", carousel_detached=bool(detached),
               counts_per_rev=counts_per_rev, tolerance=tolerance)

    legs = []

    for magnitude in ANGLE_SERIES:
        for signed in (magnitude, -magnitude):
            leg = _one_investigation_leg(ctx, signed)

            if leg is None:
                continue

            legs.append(leg)

    ctx.record("h002_legs", legs=legs, carousel_detached=bool(detached))

    _judge_h002(ctx, legs, tolerance, counts_per_rev)


def _one_investigation_leg(ctx, degrees):
    """One angle, commanded, measured by the encoder and by a human."""
    plan = ctx.servo.plan_degrees(degrees)

    if plan is None:                                   # pragma: no cover
        return None

    if plan.get("exceeds_repeat_limit"):
        ctx.note(
            "{} degrees needs {} legs and servo_test_move accepts at "
            "most 8 repeats, so this angle is skipped.".format(
                degrees, plan["repeat"]))

        return None

    if plan["split"]:
        ctx.say(
            "{:+.0f} deg is commanded as {} legs of {:+.0f} deg - the "
            "driver refuses a single verified movement larger than half "
            "a revolution".format(
                degrees, plan["repeat"], plan["degrees"]))

    before = ctx.servo.position()

    ctx.say("commanding {:+.0f} deg (encoder reads {} before)".format(
        degrees, before))

    error = None

    try:
        transaction = ctx.servo.move(
            "degrees", repeat=plan["repeat"], degrees=plan["degrees"])

    except Exception as failure:
        transaction = None
        error = {
            "type": type(failure).__name__,
            "message": str(failure),
            "code": getattr(failure, "code", None),
            "data": getattr(failure, "data", None),
        }

    evidence = _leg_evidence(ctx, degrees, before, transaction)

    evidence["plan"] = plan
    evidence["error"] = error

    if evidence["position_after"] is None:
        # The move failed or reported nothing; read the encoder
        # ourselves so the leg still carries an after-position.
        try:
            evidence["position_after"] = ctx.servo.position()

            if before is not None and evidence["position_after"] is not None:
                evidence["reported_delta_counts"] = centred_error(
                    evidence["position_after"] - before,
                    ctx.profile.counts_per_rev)

                evidence["reported_delta_degrees"] = counts_to_degrees(
                    evidence["reported_delta_counts"],
                    ctx.profile.counts_per_rev)

        except Exception as read_error:
            # The leg still counts, and the reason the position could
            # not be read afterwards is part of what it found.
            evidence["position_after_error"] = "{}: {}".format(
                type(read_error).__name__, read_error)

    evidence["observed_direction"] = ctx.observe_direction(
        "Which way did the shaft actually turn for the {:+.0f} deg "
        "command".format(degrees))

    evidence["observed_degrees"] = ctx.ask_number(
        "How many degrees did it actually move (UNKNOWN if you could "
        "not measure)", minimum=-3600, maximum=3600, unit="deg")

    ctx.measure(
        stage="h002",
        commanded_deg=degrees,
        commanded_counts=evidence["commanded_counts"],
        expected_direction=evidence["expected_direction"],
        position_before=evidence["position_before"],
        position_after=evidence["position_after"],
        reported_delta_counts=evidence["reported_delta_counts"],
        reported_delta_deg=evidence["reported_delta_degrees"],
        observed_direction=evidence["observed_direction"],
        observed_deg=evidence["observed_degrees"],
        elapsed_ms=evidence["elapsed_ms"],
        error=(error or {}).get("code") or "",
        legs=plan["legs"],
    )

    ctx.event("h002_leg", **evidence)

    return evidence


def _judge_h002(ctx, legs, tolerance, counts_per_rev):
    """
    Turn the legs into checks - and into the shape of the disagreement.

    Deliberately does NOT pick a hypothesis. It reports which of three
    patterns the data has, because choosing between "the encoder does "
    "not follow" and "the mechanism is geared" is exactly the judgement
    a measured campaign is supposed to earn rather than assume.
    """
    measured = [leg for leg in legs
                if leg.get("observed_degrees") is not None]

    ctx.check(bool(legs), "at least one leg was commanded",
              evidence={"legs": len(legs)})

    ctx.check(len(measured) == len(legs),
              "every commanded leg has an operator measurement beside "
              "it - a leg with no human observation is not evidence "
              "about the mechanism",
              evidence={"legs": len(legs), "measured": len(measured)})

    encoder_agrees = []
    physical_agrees = []
    ratios = []

    for leg in legs:
        commanded = leg["commanded_counts"]
        reported = leg["reported_delta_counts"]

        if commanded is not None and reported is not None:
            # CIRCULARLY, because a position delta is circular and a
            # plain subtraction is wrong at exactly the two angles this
            # investigation cares most about:
            #
            #   180 deg   +2048 and -2048 are the SAME encoder reading.
            #             One reading cannot tell a half turn from its
            #             complement - which is why the production
            #             driver refuses to verify more than half a
            #             revolution in one leg, and why the operator's
            #             direction observation is the only thing that
            #             settles the sign here.
            #
            #   360 deg   a full revolution returns to its start, so the
            #             correct reported delta is 0, not 4096.
            #
            # Comparing arithmetically would fail both of those legs on
            # perfectly healthy hardware and raise a spurious H-002
            # defect at the most important angle in the campaign.
            difference = centred_error(reported - commanded,
                                       counts_per_rev)

            agrees = abs(difference) <= tolerance

            encoder_agrees.append(agrees)

            ctx.check(
                agrees,
                "{:+.0f} deg: the encoder moved {} counts, {} were "
                "commanded (circular difference {})".format(
                    leg["commanded_degrees"], reported, commanded,
                    difference),
                evidence={"commanded": commanded, "reported": reported,
                          "circular_difference": difference,
                          "tolerance": tolerance,
                          "direction_from_encoder_alone":
                              "AMBIGUOUS at half a revolution - see the "
                              "operator's observation"
                              if abs(commanded) % counts_per_rev
                              == counts_per_rev // 2 else "unambiguous",
                          "byte_order_hint": leg.get("byte_order_hint")},
            )

        observed = leg.get("observed_degrees")

        if observed is not None:
            close = abs(observed - leg["commanded_degrees"]) <= max(
                5.0, abs(leg["commanded_degrees"]) * 0.1)

            physical_agrees.append(close)

            ctx.check(
                close,
                "{:+.0f} deg: the operator measured {:+.1f} deg".format(
                    leg["commanded_degrees"], observed),
                evidence={"commanded_deg": leg["commanded_degrees"],
                          "observed_deg": observed},
                kind="OPERATOR",
            )

            if leg["commanded_degrees"]:
                ratios.append(observed / leg["commanded_degrees"])

        direction = leg.get("observed_direction")

        if direction in ("CW", "CCW"):
            ctx.check(
                direction == leg["expected_direction"],
                "{:+.0f} deg: the shaft turned {} as expected".format(
                    leg["commanded_degrees"], leg["expected_direction"]),
                evidence={"observed": direction,
                          "expected": leg["expected_direction"]},
                kind="OPERATOR",
            )

    encoder_ok = bool(encoder_agrees) and all(encoder_agrees)
    physical_ok = bool(physical_agrees) and all(physical_agrees)

    ratio_summary = summarize(ratios)

    ctx.record("h002_verdict",
               encoder_agrees=encoder_ok, physical_agrees=physical_ok,
               ratio=ratio_summary, legs=len(legs))

    if encoder_ok and physical_ok:
        ctx.note(
            "H-002 is not reproduced in this run: the encoder and the "
            "protractor agree on every leg. The bench failure was then "
            "a one-off event, and the travelled-counts diagnostic added "
            "in Phase A.2 will say so if it recurs. B5 may proceed.")

        return

    pattern = _describe_pattern(legs, ratio_summary, encoder_ok,
                                physical_ok)

    ctx.defect(
        title="H-002: the encoder and the mechanism disagree",
        observed=pattern["observed"],
        expected="the encoder delta equals the commanded count to "
                 "within {} counts, and the measured angle equals the "
                 "commanded angle".format(tolerance),
        reproduction=(
            "run HW-B2-002 and record the mode",
            "run HW-B2-004 and record the read stability",
            "run HW-B3-001 with the same angle series",
        ),
        suspected_layer=pattern["suspected_layer"],
        evidence={"legs": legs, "ratio": ratio_summary,
                  "counts_per_rev": counts_per_rev,
                  "hypotheses_still_open": pattern["open"]},
    )

    ctx.note(
        "B5 and every carousel campaign above it stay gated until this "
        "is explained. Do not widen ST3215_POSITION_TOLERANCE to make "
        "it pass.")


def _describe_pattern(legs, ratio, encoder_ok, physical_ok):
    """
    Which shape the disagreement has, without choosing a cause.

    Three shapes are distinguishable from the data alone, and each
    points at a different subset of the hypothesis list.
    """
    reported = [leg.get("reported_delta_counts") for leg in legs]
    commanded = [leg.get("commanded_counts") for leg in legs]
    observed = [leg.get("observed_degrees") for leg in legs]

    moved_physically = any(
        value not in (None, 0) for value in observed)

    encoder_flat = all(
        value is None or abs(value) < 16 for value in reported)

    if moved_physically and encoder_flat:
        return {
            "observed": "the shaft moved but the encoder reported "
                        "almost nothing: reported deltas {} for "
                        "commanded {}".format(reported, commanded),
            "suspected_layer": "position register / STEP mode / "
                               "encoder coupling",
            "open": [
                "3 - present-position does not update in STEP mode",
                "4 - the servo moves and the encoder does not follow",
                "6 - the reply is stale or belongs to another command",
                "8 - byte order in the position register (HW-B3-004)",
            ],
        }

    if ratio and ratio["n"] >= 2 and ratio["sd"] < 0.15 and abs(
            ratio["mean"] - 1.0) > 0.1:
        return {
            "observed": "the physical movement is a consistent {:.3f} "
                        "times the commanded angle across {} legs "
                        "(sd {:.3f})".format(
                            ratio["mean"], ratio["n"], ratio["sd"]),
            "suspected_layer": "mechanism ratio / encoder resolution",
            "open": [
                "2 - counts_per_rev is not the real resolution",
                "7 - a reduction between servo and carousel (H-005)",
            ],
        }

    return {
        "observed": "encoder agreement: {}; physical agreement: {}; "
                    "reported deltas {} for commanded {}; measured "
                    "angles {}".format(
                        encoder_ok, physical_ok, reported, commanded,
                        observed),
        "suspected_layer": "UNKNOWN - the disagreement has no single "
                           "shape across the series",
        "open": ["every hypothesis in the B3 module docstring remains "
                 "open"],
    }


def _encoder_resolution(ctx):
    ctx.require("servo.connect", "servo.test_move", "servo.read_position")

    _connect_servo(ctx)

    counts_per_rev = ctx.profile.counts_per_rev

    ctx.instruct(
        "Mark the current position of the output shaft (or the plate) "
        "so you can tell when it has returned to exactly this point.")

    start = ctx.servo.position()

    legs = []
    position = start

    for index in (1, 2):
        transaction = ctx.servo.move("degrees", repeat=1, degrees=180.0)

        after = (transaction["data"] or {}).get("end_position")

        if after is None:
            after = ctx.servo.position()

        delta = (centred_error(after - position, counts_per_rev)
                 if position is not None and after is not None else None)

        legs.append({"leg": index, "before": position, "after": after,
                     "delta": delta})

        ctx.measure(stage="revolution", leg=index, before=position,
                    after=after, delta_counts=delta,
                    elapsed_ms=transaction["elapsed_ms"])

        position = after

    returned = ctx.ask(
        "Has the reference mark returned exactly to where it started")

    total = sum(leg["delta"] for leg in legs
                if leg["delta"] is not None)

    ctx.record("resolution", start=start, legs=legs, total_counts=total,
               returned_to_start=bool(returned),
               configured_counts_per_rev=counts_per_rev)

    ctx.check(len([leg for leg in legs if leg["delta"] is not None]) == 2,
              "both half-turn legs reported a position delta",
              evidence={"legs": legs})

    if returned:
        derived = abs(total)

        ctx.check(
            abs(derived - counts_per_rev) <= max(
                32, counts_per_rev * 0.02),
            "one observed revolution is {} counts, and the firmware is "
            "configured for {}".format(derived, counts_per_rev),
            evidence={"derived": derived, "configured": counts_per_rev},
        )

        ctx.measure(stage="resolution_summary", derived_counts=derived,
                    configured_counts=counts_per_rev,
                    ratio=(derived / counts_per_rev
                           if counts_per_rev else None))

    else:
        ctx.check(False,
                  "the mark returned to its starting point after two "
                  "half turns",
                  evidence={"total_counts": total},
                  kind="OPERATOR")

        ctx.note(
            "Two commanded half turns did not bring the mark back. "
            "Either the commanded angle is not the physical angle "
            "(hypothesis 2 or 7) or the mechanism slipped (hypothesis "
            "10). HW-B3-005 measures the ratio.")


def _reading_freshness(ctx):
    ctx.require("servo.connect", "servo.test_move", "servo.read_position")

    _connect_servo(ctx)

    rounds = ctx.iterations()

    settle_ms = ctx.profile.production["servo"]["settle_ms"]
    counts_per_rev = ctx.profile.counts_per_rev

    import time

    differences = []

    for index in range(1, rounds + 1):
        ctx.servo.move("degrees", repeat=1, degrees=10.0)

        immediate = ctx.servo.position()

        time.sleep(settle_ms / 1000.0)

        settled = ctx.servo.position()

        time.sleep(2 * settle_ms / 1000.0)

        late = ctx.servo.position()

        drift_settled = (
            centred_error(settled - immediate, counts_per_rev)
            if None not in (settled, immediate) else None)

        drift_late = (
            centred_error(late - settled, counts_per_rev)
            if None not in (late, settled) else None)

        differences.append({"round": index, "immediate": immediate,
                            "settled": settled, "late": late,
                            "drift_settled": drift_settled,
                            "drift_late": drift_late})

        ctx.measure(stage="freshness", round=index, immediate=immediate,
                    settled=settled, late=late,
                    drift_settled=drift_settled, drift_late=drift_late)

        # Put it back, so the test is symmetrical and leaves no net
        # travel to explain.
        ctx.servo.move("degrees", repeat=1, degrees=-10.0)

    drifts = [d["drift_settled"] for d in differences]
    late_drifts = [d["drift_late"] for d in differences]

    ctx.record("freshness", rounds=differences,
               settled=summarize(drifts), late=summarize(late_drifts),
               settle_ms=settle_ms)

    ctx.check(all(d == 0 for d in drifts if d is not None),
              "the position read immediately after a movement equals "
              "the position read after the configured settle time",
              evidence={"drifts": drifts, "settle_ms": settle_ms})

    ctx.check(all(d == 0 for d in late_drifts if d is not None),
              "the position does not keep changing after settling",
              evidence={"drifts": late_drifts})


def _byte_order(ctx):
    ctx.require("servo.raw_packet")

    transaction = ctx.link.request(                     # pragma: no cover
        "servo_raw_read", register=56, length=2)

    answer = transaction["data"] or {}                  # pragma: no cover

    ctx.record("raw_position", **answer)                # pragma: no cover

    ctx.check(bool(answer.get("bytes")),                # pragma: no cover
              "the raw position bytes were captured",
              evidence=answer)


def _gear_ratio(ctx):
    ctx.require("servo.connect", "servo.test_move", "servo.read_position")

    _connect_servo(ctx)

    counts_per_rev = ctx.profile.counts_per_rev
    assumed = ctx.profile.gear_ratio

    ctx.confirm_observation(
        "Is the carousel PLATE attached to the servo output shaft for "
        "this test")

    trials = []

    for trial in (1, 2, 3):
        before = ctx.servo.position()

        transaction = ctx.servo.move("degrees", repeat=1, degrees=90.0)

        after = (transaction["data"] or {}).get("end_position")

        if after is None:
            after = ctx.servo.position()

        reported = (centred_error(after - before, counts_per_rev)
                    if None not in (before, after) else None)

        plate = ctx.ask_number(
            "Trial {}: how many degrees did the CAROUSEL PLATE "
            "move".format(trial),
            minimum=-360, maximum=360, unit="deg")

        entry = {
            "trial": trial,
            "commanded_deg": 90.0,
            "reported_counts": reported,
            "reported_deg": counts_to_degrees(reported, counts_per_rev),
            "plate_deg": plate,
            "ratio": (plate / 90.0) if plate is not None else None,
        }

        trials.append(entry)

        ctx.measure(stage="gear_ratio", **entry)

        # Return, so three trials do not accumulate a quarter turn each.
        ctx.servo.move("degrees", repeat=1, degrees=-90.0)

    ratios = [t["ratio"] for t in trials if t["ratio"] is not None]

    distribution = summarize(ratios)

    ctx.record("gear_ratio", trials=trials, ratio=distribution,
               assumed_ratio=assumed)

    ctx.check(bool(ratios),
              "the operator measured the plate movement on at least one "
              "trial",
              evidence={"trials": trials})

    if not distribution:
        return

    ctx.check(abs(distribution["mean"] - assumed) <= 0.1,
              "the measured servo-to-plate ratio is {:.3f}, and the "
              "profile assumes {:.3f}".format(
                  distribution["mean"], assumed),
              evidence={"measured": distribution, "assumed": assumed})

    if abs(distribution["mean"] - assumed) > 0.1:
        ctx.defect(
            title="the carousel is geared differently from the assumption",
            observed="measured ratio {:.3f} (sd {:.3f}, n {})".format(
                distribution["mean"], distribution["sd"],
                distribution["n"]),
            expected="ratio {:.3f}, the profile's ASSUMED value".format(
                assumed),
            reproduction=("run HW-B3-005",),
            suspected_layer="mechanism (H-005)",
            evidence={"trials": trials, "ratio": distribution},
        )

        ctx.note(
            "Update mechanism.gear_ratio_servo_to_carousel in the bench "
            "profile to the measured value and set its provenance to "
            "MEASURED with this run id. Do NOT change config.py from a "
            "test - the firmware's geometry is a separate decision that "
            "belongs to the engineer, in a commit, with this evidence "
            "attached.")
