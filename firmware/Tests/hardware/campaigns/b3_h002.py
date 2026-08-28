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

from ..core.model import (Automation, IterationKind, Requirement,
                          Safety)
from ..core.analysis import (byte_order_interpretations,
                             centred_error, counts_to_degrees,
                             degrees_to_counts, summarize)


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
        requirements=(
            "HW-REQ-H002-001",
            "HW-REQ-H002-004",
            "HW-REQ-H002-006",
        ),
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
        requirements=("HW-REQ-H002-002",),
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
        requirements=("HW-REQ-H002-003",),
        iteration_kind=IterationKind.MOVEMENT,
        characterization_min_iterations=5,
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
        test_id="HW-B3-006", campaign=CAMPAIGN, layer="B3",
        requirements=("HW-REQ-SERVO-017",),
        title="A re-sync makes the carousel read exactly zero degrees",
        objective="Prove the logical carousel coordinate is the "
                  "operator's own reference and not the servo's, at "
                  "whatever raw count the mechanism happens to sit.",
        hardware_setup="Servo connected, carousel free. NOTHING MOVES "
                       "- a re-sync records where the mechanism "
                       "already is.",
        preconditions="HW-B2-002 passed.",
        procedure=(
            "read the raw encoder count and record it",
            "send sync_position for the slot at the loading hole",
            "check the reported carousel angle is exactly 0.0 degrees",
            "check the raw encoder count is UNCHANGED - nothing moved",
            "check the raw count is still separately reported",
            "move one slot and re-sync again at a different raw count, "
            "and check the angle is 0.0 there too",
        ),
        expected="0.0 degrees after every re-sync, at two different raw "
                 "counts, with the raw counts still visible.",
        failure_criteria="A non-zero angle at the origin - which is "
                         "the raw count being reported as the "
                         "carousel's position, the confusion that made "
                         "a freshly aligned carousel read 0.18 deg. Or "
                         "a raw count that CHANGED, which would mean a "
                         "re-sync moves the mechanism.",
        captures=("the raw count before and after each sync",
                  "the reported angle after each sync",
                  "the recorded origin"),
        safety=Safety.MOTION, automation=Automation.AUTOMATIC,
        requires=("servo.read_position", "servo.connect",
                  "carousel.sync"),
        run=_origin_is_zero, cleanup=_stop_and_release,
        assumption="H-002", defect_prefix="HW-CAR",
    )

    registry.test(
        test_id="HW-B3-007", campaign=CAMPAIGN, layer="B3",
        requirements=("HW-REQ-SERVO-018",),
        title="A movement the mechanism does not make is reported failed",
        objective="Prove the driver's verification survives contact "
                  "with a real mechanical failure, and that its report "
                  "separates 'the profile never ran' from 'the profile "
                  "ran and the shaft did not follow'.",
        hardware_setup="Servo connected. THE OPERATOR HOLDS THE "
                       "CAROUSEL. Hold the plate lightly by its rim so "
                       "it cannot turn; do not brace it against "
                       "anything and let go the moment the test says "
                       "so. One slot is commanded, at the production "
                       "speed, and the driver's own timeout bounds how "
                       "long the servo pushes.",
        preconditions="HW-B2-013 passed - a movement that CAN be "
                      "verified must be demonstrated before a movement "
                      "that cannot.",
        procedure=(
            "confirm the operator is holding the carousel",
            "command one slot",
            "check the driver reports a failure, not a success",
            "check the report says the trajectory register DID advance "
            "by the commanded amount",
            "check it says the measured travel did NOT",
            "ask the operator to release, then re-sync and confirm "
            "normal movement is restored",
        ),
        expected="A refused movement whose evidence names the "
                 "mechanism, not the bus: trajectory ran, measurement "
                 "did not follow.",
        failure_criteria="A movement reported as verified while the "
                         "plate was held. The trajectory register is "
                         "open loop and reaches its target under a "
                         "stall, so a driver reading only that would "
                         "pass this - and would pass a carousel that "
                         "never turned.",
        captures=("the error code and its motion evidence",
                  "trajectory_travelled and measured_travel",
                  "the following error at the end",
                  "the operator's confirmation at each step"),
        safety=Safety.MOTION, automation=Automation.OPERATOR_ASSISTED,
        requires=("servo.test_move", "servo.read_position",
                  "servo.connect"),
        run=_held_carousel_is_a_failure, cleanup=_stop_and_release,
        assumption="H-002", defect_prefix="HW-SERVO",
    )

    registry.test(
        test_id="HW-B3-008", campaign=CAMPAIGN, layer="B3",
        requirements=("HW-REQ-SERVO-019",),
        iteration_kind=IterationKind.MOVEMENT,
        title="The trajectory register is folded back before it clamps",
        objective="Prove a long one-directional session never reaches "
                  "the register clamp, and that the fold moves nothing "
                  "and changes no logical angle.",
        hardware_setup="Servo connected, carousel free. LONG: this "
                       "drives the carousel through roughly eight "
                       "revolutions at the production speed, about two "
                       "minutes. Nothing is loaded in the slots.",
        preconditions="HW-B2-013 passed.",
        procedure=(
            "re-sync, then record the trajectory register and the angle",
            "command half turns in one direction, repeatedly",
            "watch the trajectory register accumulate",
            "check a fold happens BEFORE the register reaches 32766",
            "check the carousel angle across the fold is what the "
            "commanded travel says it should be",
            "check movement still works afterwards",
        ),
        expected="The register is folded back inside one revolution "
                 "before the clamp, the fold is reported, and the "
                 "logical angle is continuous across it.",
        failure_criteria="The register reaching 32766. Measured there: "
                         "the servo stops moving in BOTH directions and "
                         "still reports a following error of about 2, "
                         "so every later movement would report VERIFIED "
                         "and would not happen.",
        captures=("the trajectory register at every leg",
                  "the headroom at every leg",
                  "where the fold happened and what it reported",
                  "the angle either side of it"),
        safety=Safety.MOTION, automation=Automation.AUTOMATIC,
        requires=("servo.test_move", "servo.read_position",
                  "servo.connect", "carousel.sync"),
        run=_trajectory_is_folded, cleanup=_stop_and_release,
        default_iterations=20, max_iterations=40,
        defect_prefix="HW-SERVO",
    )

    registry.test(
        test_id="HW-B3-004", campaign=CAMPAIGN, layer="B3",
        requirements=("HW-REQ-SERVO-007",),
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
        requires=("diagnostic.servo_raw",),
        run=_byte_order, cleanup=_stop_and_release,
        assumption="H-002", defect_prefix="HW-SERVO",
        notes="BLOCKED until the test-side diagnostic agent is "
              "deployed. HW-B3-001 meanwhile records the same "
              "interpretations against the PARSED value as a hint - "
              "weaker, because it cannot see the wire, but it needs no "
              "deployment at all.",
    )

    registry.test(
        test_id="HW-B3-005", campaign=CAMPAIGN, layer="B3",
        requirements=("HW-REQ-H002-005",),
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

def _origin_is_zero(ctx):
    """
    Re-sync twice, at two different raw counts, and check both read 0.0.

    The MOVEMENT in this test is only there to reach a second raw
    count. The property under test is that a re-sync moves nothing and
    that the logical zero is the operator's reference rather than the
    servo's.
    """
    ctx.require("servo.read_position", "servo.connect", "carousel.sync")

    ctx.link.request("connect_servo", timeout=ctx.servo._move_timeout())

    observations = []

    for index, move_first in enumerate((False, True), start=1):
        if move_first:
            ctx.carousel.move_slots("cw", 1)

        raw_before = ctx.servo.position()

        ctx.carousel.sync(load_slot=1)

        feedback = ctx.servo.feedback()
        raw_after = feedback.get("position_counts")
        angle = feedback.get("angle_deg")

        entry = {
            "sync": index,
            "raw_before": raw_before,
            "raw_after": raw_after,
            "angle_deg": angle,
            "origin_counts": feedback.get("origin_counts"),
        }

        observations.append(entry)

        ctx.measure(stage="origin", **entry)

        ctx.check(angle == 0.0,
                  "sync {}: the carousel reads exactly 0.0 deg at raw "
                  "count {}".format(index, raw_after),
                  evidence=entry)

        ctx.check(raw_before == raw_after,
                  "sync {}: the raw encoder count did not change - a "
                  "re-sync records, it does not move".format(index),
                  evidence=entry)

        ctx.check(feedback.get("origin_counts") == raw_after,
                  "sync {}: and the origin recorded is that same raw "
                  "count, still separately reported".format(index),
                  evidence=entry)

    raws = [entry["raw_after"] for entry in observations]

    ctx.check(len(set(raws)) == len(raws),
              "the two syncs happened at DIFFERENT raw counts ({}), so "
              "0.0 deg is not an accident of where the servo "
              "was".format(raws),
              evidence={"observations": observations})

    ctx.record("origin_is_zero", observations=observations)

    non_zero = [entry for entry in observations
                if entry["angle_deg"] not in (0.0, 0, -0.0)]

    if non_zero:
        ctx.defect(
            title="a freshly synchronized carousel does not read zero",
            observed="{}".format(non_zero),
            expected="0.0 deg immediately after every sync_position",
            reproduction=("run HW-B3-006",),
            suspected_layer="the carousel coordinate - most likely the "
                            "raw encoder count being reported in "
                            "degrees instead of the angle from the "
                            "origin",
            evidence={"observations": observations},
        )


def _held_carousel_is_a_failure(ctx):
    """
    Ask the operator to hold the plate, then command a movement.

    THE ONLY WAY TO PRODUCE A REAL STALL THROUGH THE PRODUCTION COMMAND
    SURFACE. The competition firmware has no command that writes an
    arbitrary servo register, and it must not grow one, so the torque
    limit cannot be dropped from here. A hand on the rim is the honest
    substitute and it tests the same thing: a profile that runs while
    the shaft does not follow.
    """
    ctx.require("servo.test_move", "servo.read_position", "servo.connect")

    ctx.link.request("connect_servo", timeout=ctx.servo._move_timeout())

    held = ctx.confirm_observation(
        "Hold the carousel plate lightly by its rim so it cannot turn. "
        "Do not brace it against anything, and let go if it starts to "
        "move. Are you holding it now?")

    if not held:
        ctx.inconclusive(
            "the operator did not confirm they were holding the plate, "
            "so nothing was commanded",
            missing=("an operator holding the carousel",))

        return

    before = ctx.servo.feedback()

    outcome = {"raised": False}

    try:
        transaction = ctx.servo.move_degrees(
            ctx.profile.production["carousel"]["slot_spacing_deg"])

        outcome["record"] = transaction["data"]

    except Exception as error:                             # noqa: BLE001
        outcome["raised"] = True
        outcome["code"] = getattr(error, "code", None)
        outcome["message"] = str(error)
        outcome["motion"] = (getattr(error, "data", None) or {}).get(
            "motion_detail") or (getattr(error, "data", None) or {})

    ctx.instruct("Let go of the carousel now.")

    after = ctx.servo.feedback()

    ctx.record("held_carousel", before=before, after=after, **outcome)

    ctx.check(outcome["raised"],
              "the driver refused the movement instead of reporting it "
              "verified",
              evidence=outcome)

    motion = outcome.get("motion") or {}

    ctx.check(motion.get("trajectory_ran") is True,
              "and its evidence says the trajectory register DID "
              "advance by the commanded amount - the servo accepted and "
              "ran the command",
              evidence={"motion": motion})

    ctx.check(motion.get("encoder_moved") is False
              or abs(motion.get("travelled_counts") or 0)
              < abs(motion.get("expected_position") or 1),
              "while the measured travel did not follow it - so the "
              "report names the MECHANISM, not the bus",
              evidence={"motion": motion})

    # And the mechanism is fine afterwards.
    ctx.carousel.sync(load_slot=1)

    recovered = ctx.servo.move_degrees(
        ctx.profile.production["carousel"]["slot_spacing_deg"])

    ctx.check((recovered["data"] or {}).get("verified") is True,
              "and once released, a normal movement verifies again",
              evidence={"record": recovered["data"]})


def _trajectory_is_folded(ctx):
    """
    Drive the trajectory register toward its clamp and watch it fold.

    LONG AND DELIBERATELY SO. The register only accumulates NET
    one-directional travel, so nothing short of about eight revolutions
    reaches the interesting part. The alternative - trusting that the
    fold works because the code says so - is exactly the reasoning that
    produced H-002.
    """
    ctx.require("servo.test_move", "servo.read_position",
                "servo.connect", "carousel.sync")

    ctx.link.request("connect_servo", timeout=ctx.servo._move_timeout())
    ctx.carousel.sync(load_slot=1)

    half_turn = ctx.profile.data["motion"]["max_degrees_per_leg"]
    limit = None
    legs = []
    folds = []

    for index in range(1, ctx.iterations() + 1):
        transaction = ctx.servo.move_degrees(half_turn)
        record = transaction["data"] or {}

        feedback = ctx.servo.feedback()

        limit = feedback.get("trajectory_limit") or limit

        leg = {
            "leg": index,
            "trajectory": feedback.get("trajectory_counts"),
            "headroom": feedback.get("trajectory_headroom"),
            "angle_deg": feedback.get("angle_deg"),
            "verified": record.get("verified"),
            "reseeds": feedback.get("trajectory_reseeds"),
        }

        legs.append(leg)
        ctx.measure(stage="accumulate", **leg)

        ctx.check(record.get("verified") is True,
                  "leg {}: the half turn verified".format(index),
                  evidence=leg)

        if leg["reseeds"] and (not folds or leg["reseeds"] > folds[-1]):
            folds.append(leg["reseeds"])

    ctx.record("trajectory_accumulation", legs=legs, limit=limit,
               folds=folds)

    reached = [leg for leg in legs
               if leg["trajectory"] is not None and limit
               and abs(leg["trajectory"]) >= limit]

    ctx.check(not reached,
              "the trajectory register never reached its {} clamp - "
              "past it the servo stops moving in both directions and "
              "still reports a following error of about 2".format(limit),
              evidence={"reached": reached})

    if not folds:
        ctx.characterize(
            "the register did not accumulate far enough to fold in {} "
            "legs; the headroom left is {}. Raise the iteration count "
            "to reach it.".format(
                len(legs), legs[-1]["headroom"] if legs else None),
            measurements=legs)

        return

    ctx.check(bool(folds),
              "the register was folded back {} time(s) before the "
              "clamp".format(len(folds)),
              evidence={"folds": folds, "legs": legs})

    # The angle must be continuous across the fold: the fold is a
    # change of raw frame and nothing else, so a jump here would mean
    # the carousel's own coordinate moved when nothing did.
    jumps = []

    for previous, current in zip(legs, legs[1:]):
        if previous["angle_deg"] is None or current["angle_deg"] is None:
            continue

        step = centred_degrees(
            current["angle_deg"] - previous["angle_deg"])

        if abs(abs(step) - half_turn) > 2.0:
            jumps.append({"from": previous, "to": current, "step": step})

    ctx.check(not jumps,
              "and the logical carousel angle advanced by one half turn "
              "at every leg, including across the fold - the fold "
              "changes the raw frame and nothing else",
              evidence={"jumps": jumps})


def centred_degrees(degrees):
    """Fold an angle into (-180, 180]. Local, so B3 needs no import."""
    return ((float(degrees) + 180.0) % 360.0) - 180.0


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
    """
    Does an alternative byte order explain the contradiction?

    Reads the position register as bytes before and after a small
    commanded movement, and asks which interpretation moved by the
    commanded amount. Diagnostic, not authoritative: the little-endian
    reading is the ST3215 memory-table order and the one the driver
    uses, and the others are here so a swap is visible to a human.
    """
    ctx.require("diagnostic.servo_raw", "servo.test_move",
                "servo.connect")

    ctx.diagnostic.identify()

    _connect_servo(ctx)

    counts_per_rev = ctx.profile.counts_per_rev

    before = ctx.diagnostic.servo_raw_read(register=56,
                                           length=2)["data"] or {}

    before_bytes = before.get("bytes")

    ctx.require_observation("the raw position bytes before the movement",
                            before_bytes, evidence={"answer": before})

    commanded_degrees = 45.0

    commanded_counts = degrees_to_counts(commanded_degrees,
                                         counts_per_rev)

    ctx.servo.move("degrees", repeat=1, degrees=commanded_degrees)

    after = ctx.diagnostic.servo_raw_read(register=56,
                                          length=2)["data"] or {}

    after_bytes = after.get("bytes")

    ctx.require_observation("the raw position bytes after the movement",
                            after_bytes, evidence={"answer": after})

    if not (before_bytes and after_bytes):
        return

    first = ctx.diagnostic.interpret_bytes(before_bytes)
    second = ctx.diagnostic.interpret_bytes(after_bytes)

    deltas = {}

    for reading in ("little_endian", "big_endian"):
        deltas[reading] = centred_error(
            second[reading] - first[reading], counts_per_rev)

    tolerance = ctx.profile.production["servo"]["position_tolerance"]

    matching = [name for name, delta in sorted(deltas.items())
                if delta is not None
                and abs(delta - commanded_counts) <= tolerance]

    ctx.record("byte_order", before=first, after=second,
               commanded_counts=commanded_counts,
               commanded_degrees=commanded_degrees,
               deltas=deltas, matching=matching, tolerance=tolerance)

    ctx.measure(stage="byte_order",
                commanded_counts=commanded_counts,
                before_hex=first["hex"], after_hex=second["hex"],
                little_delta=deltas.get("little_endian"),
                big_delta=deltas.get("big_endian"),
                matching=";".join(matching))

    ctx.check(
        "little_endian" in matching,
        "the little-endian reading - the ST3215 memory-table order, and "
        "the one the driver uses - moved by the commanded {} "
        "counts".format(commanded_counts),
        evidence={"deltas": deltas, "commanded": commanded_counts,
                  "tolerance": tolerance})

    if "little_endian" not in matching and "big_endian" in matching:
        ctx.defect(
            title="the position register reads correctly only when "
                  "byte-swapped",
            observed="a commanded {} counts moved the big-endian "
                     "reading by {} and the little-endian reading by "
                     "{}".format(commanded_counts,
                                 deltas.get("big_endian"),
                                 deltas.get("little_endian")),
            expected="the little-endian reading moves by the commanded "
                     "count",
            reproduction=("run HW-B3-004 with the diagnostic agent "
                          "deployed",),
            suspected_layer="the position register byte order - "
                            "hypothesis 8 of H-002",
            evidence={"before": first, "after": second,
                      "deltas": deltas},
        )


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
