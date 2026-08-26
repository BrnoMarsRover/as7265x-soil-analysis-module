"""
B5 - the physical carousel.

GATED BY B3, AND THAT GATE IS THE POINT

Every test here addresses the carousel by SLOT NUMBER, which is a claim
about where the plate is. That claim is built on the encoder, and H-002
is the open question of whether the encoder describes the plate. So B5
does not run until B3 has passed on hardware; running it earlier would
produce slot-addressed results whose meaning depends on an unresolved
contradiction.

THE GEOMETRY IS GENERATED, NOT WRITTEN DOWN

This bench has FOUR slots, 90 degrees apart, with the loader and the
scanner 180 degrees - two slots - apart. Every transition matrix below
is generated from the production slot count, so the campaign is correct
for this plate and would still be correct for an eight-slot one.
"""

from ..core.model import Automation, Safety
from ..core.analysis import failure_rate, summarize


CAMPAIGN = "B5"


def register(registry):
    registry.campaign(
        CAMPAIGN, layer="B5", title="The physical carousel",
        purpose="Establish that slot addressing, the loader/scanner "
                "geometry, backlash and accumulated drift behave on the "
                "real mechanism.",
        prerequisites=("B4",),
        gate_note="Gated by B4, and therefore by B3. Slot-addressed "
                  "evidence is only meaningful once the encoder is "
                  "known to describe the plate.",
    )

    registry.test(
        test_id="HW-B5-001", campaign=CAMPAIGN, layer="B5",
        title="Every adjacent slot transition, both directions",
        objective="Prove each neighbouring transition lands where the "
                  "firmware says it did.",
        hardware_setup="Carousel attached, empty, free to turn. Servo "
                       "connected and synchronized.",
        preconditions="HW-B3-001 passed. HW-B4-003 measured the closing "
                      "error. The carousel has been synchronized with "
                      "physical slot 1 at the loading hole.",
        procedure=(
            "synchronize the position",
            "for every adjacent pair in both directions: select the "
            "slot, record the movement, check the reported slot",
            "confirm with the operator that the plate is where the slot "
            "number says",
        ),
        expected="Every transition completes, the reported slot is the "
                 "requested slot, and the operator agrees.",
        failure_criteria="A transition that fails, lands on the wrong "
                         "slot, or a plate the operator says is "
                         "somewhere else.",
        captures=("each transition with its position error",
                  "reported slot before and after", "elapsed time",
                  "the operator's spot checks"),
        safety=Safety.MOTION, automation=Automation.OPERATOR_ASSISTED,
        requires=("carousel.select_slot", "carousel.sync",
                  "carousel.status", "servo.connect"),
        run=_adjacent, cleanup=_park,
        defect_prefix="HW-CAR",
    )

    registry.test(
        test_id="HW-B5-002", campaign=CAMPAIGN, layer="B5",
        title="Non-adjacent slot transitions",
        objective="Check the movements that cross more than one slot, "
                  "including the one that must take the shorter way "
                  "round.",
        hardware_setup="As HW-B5-001.",
        preconditions="HW-B5-001 passed.",
        procedure=(
            "for every non-adjacent pair: select the slot and record "
            "the movement",
            "check the direction taken is the shorter one",
            "check the reported slot",
        ),
        expected="Every transition lands correctly and takes the "
                 "shorter path.",
        failure_criteria="A movement that goes the long way round, "
                         "which on a plate with a cable loom is a "
                         "mechanical hazard, or one that lands wrong.",
        captures=("each transition", "the direction taken",
                  "the counts travelled", "the reported slot"),
        safety=Safety.MOTION, automation=Automation.AUTOMATIC,
        requires=("carousel.select_slot", "carousel.sync",
                  "carousel.status"),
        run=_non_adjacent, cleanup=_park,
        defect_prefix="HW-CAR",
    )

    registry.test(
        test_id="HW-B5-003", campaign=CAMPAIGN, layer="B5",
        title="Loader to scanner geometry",
        objective="Verify the physical relationship the whole "
                  "measurement depends on: the slot at the loading hole "
                  "arrives under the sensor after the transfer.",
        hardware_setup="Carousel attached and empty. The operator can "
                       "see both the loading hole and the sensor head.",
        preconditions="HW-B5-001 passed.",
        procedure=(
            "synchronize with a known slot at the loader",
            "read the reported load and scan slots",
            "check the offset is the configured number of slots",
            "ask the operator to confirm which physical slot is at the "
            "loading hole and which is under the sensor",
        ),
        expected="The firmware's load and scan slots match what the "
                 "operator sees, and they differ by the configured "
                 "offset.",
        failure_criteria="Any disagreement between the reported "
                         "geometry and the plate. Every measurement "
                         "would then be taken of the wrong slot.",
        captures=("reported load slot", "reported scan slot",
                  "the configured offset",
                  "the operator's observation of both positions"),
        safety=Safety.MOTION, automation=Automation.OPERATOR_ASSISTED,
        requires=("carousel.sync", "carousel.status",
                  "carousel.select_slot"),
        run=_geometry, cleanup=_park,
        assumption="H-005", defect_prefix="HW-CAR",
        notes="Replaces HW-400.",
    )

    registry.test(
        test_id="HW-B5-004", campaign=CAMPAIGN, layer="B5",
        title="Backlash: approaching one slot from both directions",
        objective="Measure whether the resting position of a slot "
                  "depends on which way it was approached.",
        hardware_setup="As HW-B5-001. A reference mark on the plate.",
        preconditions="HW-B4-003 passed.",
        procedure=(
            "approach a chosen slot from the clockwise side and record "
            "the position",
            "approach the same slot from the other side and record it",
            "repeat several times",
            "compute the difference between the two resting positions",
        ),
        expected="The two approaches settle at the same encoder "
                 "position, within the measured repeatability.",
        failure_criteria="A consistent offset between the two "
                         "approaches. That is backlash, and it must be "
                         "measured before a tolerance is set from "
                         "one-directional data.",
        captures=("resting position per approach per repetition",
                  "the difference distribution"),
        safety=Safety.MOTION, automation=Automation.AUTOMATIC,
        requires=("carousel.select_slot", "carousel.status",
                  "servo.read_position"),
        run=_backlash, cleanup=_park,
        default_iterations=5, max_iterations=100,
        assumption="H-006", defect_prefix="HW-CAR",
    )

    registry.test(
        test_id="HW-B5-005", campaign=CAMPAIGN, layer="B5",
        title="Accumulated drift over full rotations",
        objective="Establish whether position drifts after many "
                  "consecutive slot movements in one direction.",
        hardware_setup="As HW-B5-001.",
        preconditions="HW-B5-001 passed.",
        procedure=(
            "record the encoder position at slot 1",
            "walk the whole sequence 1 -> 2 -> ... -> N -> 1",
            "record the position at slot 1 again",
            "repeat for several full rotations",
            "compute the drift per rotation",
        ),
        expected="The encoder returns to its starting position after "
                 "each full rotation, within the measured "
                 "repeatability.",
        failure_criteria="A drift that grows with each rotation. Then a "
                         "re-sync is needed every N rotations and the "
                         "operator has to be told how many.",
        captures=("position at slot 1 each rotation",
                  "the drift per rotation", "the cumulative drift"),
        safety=Safety.MOTION, automation=Automation.AUTOMATIC,
        requires=("carousel.select_slot", "carousel.status",
                  "servo.read_position"),
        run=_drift, cleanup=_park,
        default_iterations=3, max_iterations=100,
        assumption="H-006", defect_prefix="HW-CAR",
    )

    registry.test(
        test_id="HW-B5-006", campaign=CAMPAIGN, layer="B5",
        title="Re-sync after a deliberate hand turn",
        objective="Prove the operator can recover a known position "
                  "after the plate has been moved by hand.",
        hardware_setup="Carousel attached, empty.",
        preconditions="HW-B5-001 passed.",
        procedure=(
            "record the reported slot",
            "ask the operator to turn the plate by hand to a different "
            "slot",
            "read the status and record what the firmware now believes",
            "re-synchronize to the slot the operator actually placed",
            "check the firmware agrees",
            "move one slot and confirm it lands correctly",
        ),
        expected="After the re-sync the firmware and the plate agree, "
                 "and the next movement lands correctly.",
        failure_criteria="A re-sync that does not take, or a firmware "
                         "that keeps its old belief. The operator would "
                         "then have no way back from a disturbed "
                         "carousel except a reboot.",
        captures=("reported slot before the hand turn",
                  "what the operator moved it to",
                  "the status after the hand turn",
                  "the status after the re-sync",
                  "the result of the confirming movement"),
        safety=Safety.MOTION, automation=Automation.OPERATOR_ASSISTED,
        requires=("carousel.sync", "carousel.status",
                  "carousel.select_slot"),
        run=_resync, cleanup=_park,
        defect_prefix="HW-CAR",
        notes="Replaces HW-402.",
    )


# ======================================================================
# shared
# ======================================================================

def _park(ctx):
    """
    Leave the carousel where the operator can find it, or say we cannot.

    No movement is commanded during cleanup: a carousel whose position
    is already uncertain must not be turned by a cleanup handler that
    does not know where it is. The state is recorded instead.
    """
    record = {}

    try:
        record["position_state"] = ctx.carousel.position_state()

    except Exception as error:
        record["position_state"] = {
            "state": "POSITION_UNREADABLE",
            "reason": "{}: {}".format(type(error).__name__, error),
        }

    closed = ctx.link.close(reason="B5 cleanup")

    record["port_released"] = closed.get("closed")
    record["confirmed"] = bool(
        closed.get("closed")
        and (record["position_state"] or {}).get("state")
        == "POSITION_KNOWN")

    if not record["confirmed"]:
        record["note"] = (
            "the carousel position is not confirmed known after this "
            "test; re-synchronize before the next slot-addressed "
            "movement")

    return record


def _prepare(ctx, load_slot=1):
    """Connect the servo and establish a known position."""
    ctx.link.request("connect_servo", timeout=ctx.servo._move_timeout())

    ctx.carousel.sync(load_slot=load_slot)

    return ctx.carousel.status()


def _select(ctx, slot, stage):
    """One slot selection, recorded whether it worked or not."""
    row = {"stage": stage, "slot": slot}

    try:
        transaction = ctx.carousel.select_slot(slot)

        answer = transaction["data"] or {}
        carousel = answer.get("carousel") or {}
        movement = answer.get("movement") or answer.get("motion") or {}

        row.update({
            "ok": True,
            "elapsed_ms": transaction["elapsed_ms"],
            "reported_scan_slot": carousel.get("current_scan_slot"),
            "reported_load_slot": carousel.get("current_load_slot"),
            "position_valid": carousel.get("position_valid"),
            "position_error": movement.get("position_error"),
            "travelled_counts": movement.get("travelled_counts"),
        })

    except Exception as error:
        row.update({
            "ok": False,
            "error_type": type(error).__name__,
            "error": str(error)[:200],
            "code": getattr(error, "code", None),
        })

    ctx.measure(**row)

    return row


# ======================================================================
# bodies
# ======================================================================

def _adjacent(ctx):
    ctx.require("carousel.select_slot", "carousel.sync",
                "carousel.status", "servo.connect")

    _prepare(ctx)

    transitions = ctx.carousel.adjacent_transitions()

    ctx.say("{} adjacent transitions on a {}-slot plate".format(
        len(transitions), ctx.carousel.slot_count()))

    rows = []
    outcomes = []

    for source, target in transitions:
        ctx.carousel.select_slot(source)

        row = _select(ctx, target, "adjacent")

        row["from_slot"] = source

        rows.append(row)
        outcomes.append(bool(row.get("ok")))

        if row.get("ok"):
            landed = row.get("reported_load_slot")

            ctx.check(landed in (None, target),
                      "{} -> {}: the firmware reports slot {}".format(
                          source, target, landed),
                      evidence=row)

    rate = failure_rate(outcomes)

    ctx.record("adjacent", transitions=rows, failure_rate=rate)

    ctx.check(rate["all_passed"],
              "every adjacent transition completed",
              evidence=rate)

    ctx.confirm_observation(
        "Is the physical slot the firmware now reports at the loading "
        "hole actually the slot at the loading hole")


def _non_adjacent(ctx):
    ctx.require("carousel.select_slot", "carousel.sync",
                "carousel.status")

    _prepare(ctx)

    pairs = ctx.carousel.non_adjacent_transitions()

    if not pairs:
        ctx.skip(
            "a {}-slot plate has no non-adjacent transitions".format(
                ctx.carousel.slot_count()))

    rows = []
    outcomes = []

    slot_counts = ctx.profile.production["servo"]["counts_per_slot"]
    half = ctx.profile.counts_per_rev // 2

    for source, target in pairs:
        ctx.carousel.select_slot(source)

        row = _select(ctx, target, "non_adjacent")
        row["from_slot"] = source

        rows.append(row)
        outcomes.append(bool(row.get("ok")))

        travelled = row.get("travelled_counts")

        if travelled is not None:
            ctx.check(abs(travelled) <= half + slot_counts,
                      "{} -> {}: the movement took the shorter way "
                      "round ({} counts)".format(
                          source, target, travelled),
                      evidence=row)

    rate = failure_rate(outcomes)

    ctx.record("non_adjacent", transitions=rows, failure_rate=rate)

    ctx.check(rate["all_passed"],
              "every non-adjacent transition completed",
              evidence=rate)


def _geometry(ctx):
    ctx.require("carousel.sync", "carousel.status",
                "carousel.select_slot")

    status = _prepare(ctx, load_slot=1)

    ctx.record("geometry_status", **status)

    configured = ctx.profile.production["carousel"]

    load_slot = status.get("current_load_slot")
    scan_slot = status.get("current_scan_slot")
    offset = status.get("scan_load_offset_slots")

    ctx.check(offset == configured["scan_load_offset_slots"],
              "the reported loader/scanner offset is {} slots".format(
                  configured["scan_load_offset_slots"]),
              evidence={"reported": offset,
                        "configured": configured[
                            "scan_load_offset_slots"]})

    ctx.check(load_slot == 1,
              "the firmware reports slot 1 at the loading hole after "
              "the sync",
              evidence={"load_slot": load_slot})

    ctx.measure(stage="geometry", load_slot=load_slot,
                scan_slot=scan_slot, offset=offset,
                slot_spacing_deg=configured["slot_spacing_deg"],
                half_turn_deg=configured["half_turn_deg"])

    ctx.confirm_observation(
        "Is physical slot 1 at the loading hole right now")

    ctx.confirm_observation(
        "Is physical slot {} under the sensor head right now".format(
            scan_slot))

    observed = ctx.ask_number(
        "How many degrees apart are the loading hole and the sensor "
        "head, measured on the plate (UNKNOWN if you cannot measure it)",
        minimum=0, maximum=360, unit="deg")

    if observed is not None:
        ctx.check(abs(observed - configured["half_turn_deg"]) <= 10.0,
                  "the loader and the scanner are {} degrees apart, as "
                  "configured".format(configured["half_turn_deg"]),
                  evidence={"observed": observed,
                            "configured": configured["half_turn_deg"]},
                  kind="OPERATOR")


def _backlash(ctx):
    ctx.require("carousel.select_slot", "carousel.status",
                "servo.read_position")

    _prepare(ctx)

    repeats = ctx.iterations()
    count = ctx.carousel.slot_count()

    target = 2 if count >= 2 else 1
    before_target = (target - 2) % count + 1
    after_target = target % count + 1

    from_low = []
    from_high = []

    for index in range(1, repeats + 1):
        ctx.carousel.select_slot(before_target)
        ctx.carousel.select_slot(target)

        low = ctx.servo.position()

        ctx.carousel.select_slot(after_target)
        ctx.carousel.select_slot(target)

        high = ctx.servo.position()

        from_low.append(low)
        from_high.append(high)

        difference = (high - low) if None not in (high, low) else None

        ctx.measure(stage="backlash", repetition=index,
                    approach_low=low, approach_high=high,
                    difference=difference)

    differences = [
        h - l for h, l in zip(from_high, from_low)
        if None not in (h, l)
    ]

    distribution = summarize(differences)

    ctx.record("backlash", from_low=from_low, from_high=from_high,
               difference=distribution)

    ctx.check(bool(distribution),
              "both approaches produced a readable position",
              evidence={"n": len(differences)})

    if not distribution:
        return

    tolerance = ctx.profile.production["servo"]["position_tolerance"]

    ctx.check(abs(distribution["mean"]) <= tolerance,
              "the two approaches settle within the position tolerance "
              "of each other ({} counts mean difference)".format(
                  distribution["mean"]),
              evidence={"distribution": distribution,
                        "tolerance": tolerance})

    if abs(distribution["mean"]) > tolerance:
        ctx.defect(
            title="the carousel has measurable backlash",
            observed="approaching slot {} from the two directions "
                     "settles {} counts apart on average (sd {}, n "
                     "{})".format(target, distribution["mean"],
                                  distribution["sd"], distribution["n"]),
            expected="the same resting position from either direction, "
                     "within {} counts".format(tolerance),
            reproduction=("run HW-B5-004",),
            suspected_layer="mechanism (coupling, gear or plate "
                            "mounting)",
            evidence={"distribution": distribution},
        )


def _drift(ctx):
    ctx.require("carousel.select_slot", "carousel.status",
                "servo.read_position")

    _prepare(ctx)

    rotations = ctx.iterations()
    sequence = ctx.carousel.full_rotation_sequence()

    ctx.carousel.select_slot(1)

    origin = ctx.servo.position()

    positions = [origin]
    drifts = []

    for rotation in range(1, rotations + 1):
        for slot in sequence[1:]:
            _select(ctx, slot, "drift")

        here = ctx.servo.position()

        positions.append(here)

        drift = (here - origin) if None not in (here, origin) else None

        drifts.append(drift)

        ctx.measure(stage="rotation", rotation=rotation, position=here,
                    cumulative_drift=drift)

        ctx.say("rotation {} of {}, cumulative drift {}".format(
            rotation, rotations, drift))

    tolerance = ctx.profile.production["servo"]["position_tolerance"]

    known = [d for d in drifts if d is not None]

    ctx.record("drift", origin=origin, positions=positions,
               drifts=drifts, distribution=summarize(known))

    ctx.check(bool(known),
              "the encoder position was readable at the end of every "
              "rotation",
              evidence={"drifts": drifts})

    if known:
        ctx.check(abs(known[-1]) <= tolerance * len(known),
                  "the cumulative drift after {} rotations is {} "
                  "counts".format(len(known), known[-1]),
                  evidence={"drifts": known, "tolerance": tolerance})

        growing = all(
            abs(known[index]) >= abs(known[index - 1])
            for index in range(1, len(known))
        ) and len(known) >= 3 and abs(known[-1]) > tolerance

        ctx.check(not growing,
                  "the drift does not grow monotonically with each "
                  "rotation",
                  evidence={"drifts": known})


def _resync(ctx):
    ctx.require("carousel.sync", "carousel.status",
                "carousel.select_slot")

    _prepare(ctx)

    before = ctx.carousel.status()

    ctx.record("before_hand_turn", **before)

    ctx.instruct(
        "Turn the carousel plate BY HAND to a different slot. Note "
        "which physical slot you have placed at the loading hole.")

    during = ctx.carousel.status()

    ctx.record("after_hand_turn", **during)

    ctx.note(
        "The firmware still believes slot {} is at the loader; the "
        "plate has been moved. Nothing detects a hand turn, which is "
        "why the re-sync exists.".format(
            during.get("current_load_slot")))

    placed = ctx.ask_number(
        "Which slot number is now at the loading hole",
        minimum=1, maximum=ctx.carousel.slot_count())

    if placed is None:
        ctx.block(
            "the operator could not say which slot is at the loader, so "
            "there is nothing to re-synchronize to",
            recommendation="Mark the slots on the plate so a hand turn "
                           "can always be reported.")

    ctx.carousel.sync(load_slot=int(placed))

    after = ctx.carousel.status()

    ctx.record("after_resync", **after)

    ctx.check(after.get("current_load_slot") == int(placed),
              "after the re-sync the firmware reports slot {} at the "
              "loader".format(int(placed)),
              evidence={"reported": after.get("current_load_slot"),
                        "placed": placed})

    ctx.check(after.get("position_valid") is True,
              "the position is valid again after the re-sync",
              evidence={"position_valid": after.get("position_valid")})

    next_slot = int(placed) % ctx.carousel.slot_count() + 1

    row = _select(ctx, next_slot, "resync_confirm")

    ctx.check(bool(row.get("ok")),
              "a movement after the re-sync completes",
              evidence=row)

    ctx.confirm_observation(
        "Did the plate move exactly one slot, and is slot {} now at the "
        "loading hole".format(next_slot))
