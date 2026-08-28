"""
B8 - the carousel and the sensor, in one transaction.

    LOAD -> transfer to the scanner -> WHITE -> UV -> IR -> answer ->
    return to LOAD

This is `measure_raw`, the production command, unmodified. Nothing here
is test-specific, which is the point: B8 measures what the competition
will run.

RF-001 LIVES HERE

The bench failure that opened H-002 happened inside this transaction:
slot 1 loaded, Measure Sample, a half turn, an encoder that reported two
counts, and a firmware that correctly refused to continue. HW-B8-002 is
that exact sequence, run deliberately, with the result pointed back at
H-002 if it reappears - and with the carousel campaigns above it staying
blocked when it does.
"""

from ..core.model import Automation, Requirement, Safety
from ..core.analysis import failure_rate, summarize


CAMPAIGN = "B8"


def register(registry):
    registry.campaign(
        CAMPAIGN, layer="B8",
        title="Carousel and measurement integration",
        purpose="Run the complete production measurement transaction "
                "and check every stage of it, including the return.",
        prerequisites=("B5", "B7"),
        gate_note="Gated by BOTH the carousel and the sensor layers. An "
                  "integration failure with either one unqualified is "
                  "not attributable.",
    )

    registry.test(
        test_id="HW-B8-001", campaign=CAMPAIGN, layer="B8",
        requirements=("HW-REQ-INT-001",),
        title="The complete measurement transaction",
        objective="Run measure_raw end to end and check every stage.",
        hardware_setup="Carousel attached and synchronized, a sample or "
                       "a stable target in the slot being measured, "
                       "sensor connected.",
        preconditions="B5 and B7 passed.",
        procedure=(
            "synchronize the carousel with slot 1 at the loader",
            "record the position before",
            "send measure_raw for the slot",
            "record the position reported at the scanner",
            "validate the spectrum shape of all three illuminations",
            "record the position after the return",
            "check the carousel came back to the loading position",
            "check the position is still valid afterwards",
        ),
        expected="A complete 54-feature measurement, and a carousel "
                 "back at the loading position with its position still "
                 "valid.",
        failure_criteria="Any stage failing. A measurement that "
                         "succeeds but does not return leaves a "
                         "carousel the operator cannot load.",
        captures=("position before", "position at the scanner",
                  "each illumination block", "the spectrum shape",
                  "position after the return", "total elapsed time",
                  "position validity throughout"),
        safety=Safety.FULL_SYSTEM, automation=Automation.AUTOMATIC,
        requires=("carousel.measure", "carousel.sync",
                  "carousel.status", "servo.connect"),
        run=_full_transaction, cleanup=_park,
        defect_prefix="HW-INT",
    )

    registry.test(
        test_id="HW-B8-002", campaign=CAMPAIGN, layer="B8",
        requirements=("HW-REQ-INT-002",),
        title="RF-001 regression: slot 1 loaded, measure, half turn, "
              "return",
        objective="Reproduce the exact bench failure that opened H-002, "
                  "deliberately, and record what happens now.",
        hardware_setup="Carousel attached, slot 1 at the loading hole "
                       "with something in it, sensor connected.",
        preconditions="HW-B8-001 passed.",
        procedure=(
            "synchronize with slot 1 at the loader",
            "mark slot 1 as loaded",
            "send measure_raw for slot 1",
            "record the encoder validation of the half turn in full",
            "record whether the acquisition began",
            "record whether the return succeeded",
            "if the movement/encoder contradiction reappears, raise it "
            "against H-002 and stop treating higher layers as valid",
        ),
        expected="The half turn is verified against the encoder, the "
                 "acquisition runs, and the return succeeds.",
        failure_criteria="The half turn reporting a travel that does "
                         "not match the command - which is RF-001 "
                         "recurring, and which invalidates every "
                         "carousel result above this layer until H-002 "
                         "is explained.",
        captures=("the whole measure_raw answer",
                  "the movement record of the transfer",
                  "travelled counts versus commanded counts",
                  "whether the acquisition began",
                  "the return result",
                  "the carousel position validity at each stage"),
        safety=Safety.FULL_SYSTEM, automation=Automation.AUTOMATIC,
        requires=("carousel.measure", "carousel.sync",
                  "carousel.status"),
        run=_rf001, cleanup=_park,
        assumption="H-002", defect_prefix="HW-INT",
        prerequisites=("HW-B3-001",),
        notes="The mandatory regression named in the campaign plan.",
    )

    registry.test(
        test_id="HW-B8-003", campaign=CAMPAIGN, layer="B8",
        requirements=("HW-REQ-INT-003",),
        title="Repeated measurements across every slot",
        objective="Check that measuring each slot in turn leaves no "
                  "state behind and no accumulated position error.",
        hardware_setup="As HW-B8-001, with every slot loaded or with a "
                       "stable target that can be measured from any "
                       "slot.",
        preconditions="HW-B8-001 passed.",
        procedure=(
            "measure every slot in turn",
            "record the position before and after each",
            "check each answer names the slot that was requested",
            "check no measurement's data appears in another's answer",
            "check the accumulated position error across the whole "
            "round",
        ),
        expected="Every slot measured, every answer attributed to the "
                 "right slot, and no accumulated error.",
        failure_criteria="A measurement attributed to the wrong slot, "
                         "or spectral data repeated between slots - "
                         "which is state leaking between measurements.",
        captures=("per-slot measurement result and timing",
                  "the slot each answer names",
                  "the position error across the round",
                  "a comparison of the spectra between slots"),
        safety=Safety.FULL_SYSTEM, automation=Automation.AUTOMATIC,
        requires=("carousel.measure", "carousel.sync",
                  "carousel.status"),
        run=_every_slot, cleanup=_park,
        defect_prefix="HW-INT",
    )

    registry.test(
        test_id="HW-B8-004", campaign=CAMPAIGN, layer="B8",
        requirements=("HW-REQ-INT-004",),
        title="The measurement's data survives the transport intact",
        objective="Check the 54 features that come back through the "
                  "integrated path are the same shape as the ones the "
                  "sensor produces on its own.",
        hardware_setup="As HW-B8-001.",
        preconditions="HW-B7-001 and HW-B8-001 passed.",
        procedure=(
            "acquire a triad WITHOUT moving anything",
            "run a full measure_raw",
            "validate both against the same 54-feature contract",
            "compare the channel sets",
            "compare the response sizes and the transport counters",
        ),
        expected="Both satisfy the contract and carry the same channels.",
        failure_criteria="A difference in shape between the standalone "
                         "acquisition and the integrated one, which "
                         "would mean the measurement path damages data "
                         "the sensor produced correctly.",
        captures=("both spectra", "both shape validations",
                  "the transport counters for each",
                  "the response sizes"),
        safety=Safety.FULL_SYSTEM, automation=Automation.AUTOMATIC,
        requires=("carousel.measure", "sensor.acquire_triad"),
        run=_data_survives, cleanup=_park,
        defect_prefix="HW-INT",
    )

    registry.test(
        test_id="HW-B8-005", campaign=CAMPAIGN, layer="B8",
        requirements=("HW-REQ-INT-005",),
        title="Every slot is physically centred at the moment of "
              "acquisition",
        objective="Confirm, per slot, that the sample really is under "
                  "the head while the spectrum is being taken - not "
                  "merely that the encoder agrees.",
        hardware_setup="Carousel attached, every slot loaded or with a "
                       "stable target. The operator can see the head "
                       "and the slot beneath it during the "
                       "measurement.",
        preconditions="HW-B5-009 measured the head geometry. HW-B8-001 "
                      "passed.",
        procedure=(
            "for each slot, start a measurement",
            "while the carousel is at the scanner, ask the operator "
            "whether the slot is centred under the head",
            "record the encoder position at the scanner alongside the "
            "observation",
            "let the measurement complete and validate the spectrum",
            "compare the operator's observation with what the firmware "
            "believed",
        ),
        expected="Every slot is observed centred while its spectrum is "
                 "acquired, and the firmware's belief matches.",
        failure_criteria="A slot the operator sees off-centre while the "
                         "firmware reports it in position. Mechanical "
                         "slip after the encoder is invisible to the "
                         "encoder, and that is the whole reason H-002 "
                         "is open.",
        captures=("per-slot centering observation at acquisition time",
                  "the encoder position at the scanner",
                  "what the firmware believed",
                  "the spectrum shape per slot"),
        safety=Safety.FULL_SYSTEM,
        automation=Automation.OPERATOR_ASSISTED,
        requires=("carousel.measure", "carousel.sync",
                  "carousel.status", "servo.read_position"),
        run=_acquisition_centering, cleanup=_park,
        defect_prefix="HW-INT",
    )


# ======================================================================
# shared
# ======================================================================

def _park(ctx):
    from .b5_carousel import _park as shared

    return shared(ctx)


def _prepare(ctx):
    ctx.link.request("connect_servo", timeout=ctx.servo._move_timeout())

    ctx.carousel.sync(load_slot=1)

    return ctx.carousel.status()


def _transfer_record(answer):
    """
    The outbound movement the transfer performed.

    `move` FIRST, BECAUSE `move` IS WHAT THE FIRMWARE SENDS.

    This looked for `movement`, `motion`, `transfer` and `to_scanner`,
    and the measure answer has never carried any of them: it sends
    `move` for the outbound half turn and `return_move` for the way
    back. So on a real board the reader found nothing, `travelled_counts`
    was None, and the RF-001 regression - the test that exists to say
    whether the bench failure has recurred - reported "the transfer
    did not report how far it travelled" over a transaction that had
    just completed correctly.

    That is the same defect family as `servo["selected"]`: a reader and
    a fixture agreeing with each other and neither compared to a board.
    The alternatives are kept behind `move` rather than removed, since
    they cost nothing and this ran against firmware that predates the
    current answer shape.
    """
    for key in ("move", "movement", "motion", "transfer", "to_scanner"):
        value = (answer or {}).get(key)

        if isinstance(value, dict) and value:
            return value

    return {}


# The firmware's own name for measured travel, first.
TRAVEL_KEYS = ("measured_travel", "travelled_counts", "counts")


def _travelled_counts(transfer):
    """
    How far the ENCODER says the transfer actually went, or None.

    The carousel's `move` block is the carousel's own record and the
    driver's record hangs off it under `servo` - so the number that
    matters is one level down, and a multi-leg move keeps its legs
    below that again. Descending is not defensive coding here: this
    value is the whole subject of the RF-001 regression, and reading
    None for it is how that test managed to report a contradiction
    over a transaction that had completed correctly.

    MEASURED, NOT COMMANDED. `trajectory_travelled` sits in the same
    record and is always the commanded amount, because the trajectory
    register is open loop. Taking it would make this test pass over a
    carousel that never turned.
    """
    transfer = transfer or {}

    for source in (transfer, transfer.get("servo") or {}):
        for key in TRAVEL_KEYS:
            value = source.get(key)

            if isinstance(value, int):
                return value

        legs = source.get("legs")

        if isinstance(legs, list) and legs:
            total = 0

            for leg in legs:
                measured = (leg or {}).get("measured_travel")

                if not isinstance(measured, int):
                    total = None

                    break

                total += measured

            if total is not None:
                return total

    return None


def _spectra_from(answer):
    """The illumination blocks out of a measure_raw answer."""
    for key in ("illuminations", "spectra", "raw"):
        value = (answer or {}).get(key)

        if isinstance(value, dict) and {"white", "uv", "ir"} & set(value):
            return value

    return {}


# ======================================================================
# bodies
# ======================================================================

def _full_transaction(ctx):
    ctx.require("carousel.measure", "carousel.sync", "carousel.status",
                "servo.connect")

    before = _prepare(ctx)

    ctx.record("before", **before)

    ctx.check(before.get("position_valid") is True,
              "the carousel position is valid before the measurement",
              evidence={"carousel": before})

    transaction = ctx.carousel.measure(1, sample_id="HW-B8-001")

    answer = transaction["data"] or {}

    ctx.record("measure_raw", elapsed_ms=transaction["elapsed_ms"],
               keys=sorted(answer.keys()))

    ctx.check(True, "measure_raw completed",
              evidence={"elapsed_ms": transaction["elapsed_ms"]})

    blocks = _spectra_from(answer)

    problems = ctx.sensor.validate_triad(
        {"illuminations": blocks} if blocks else answer)

    ctx.check(not problems,
              "the measurement carries a well-formed 54-feature "
              "spectrum",
              evidence={"problems": problems})

    transfer = _transfer_record(answer)

    ctx.record("transfer", **transfer)

    after = answer.get("carousel") or ctx.carousel.status()

    ctx.record("after", **after)

    ctx.check(after.get("position_valid") is True,
              "the carousel position is still valid after the "
              "measurement",
              evidence={"carousel": after})

    ctx.check(after.get("current_load_slot") == 1,
              "the carousel returned to the loading position with slot "
              "1 at the loader",
              evidence={"load_slot": after.get("current_load_slot")})

    ctx.measure(
        stage="measurement", slot=1,
        elapsed_ms=transaction["elapsed_ms"],
        features=ctx.sensor.feature_count(
            {"illuminations": blocks} if blocks else answer),
        position_before=before.get("current_load_slot"),
        position_after=after.get("current_load_slot"),
        position_valid_after=after.get("position_valid"),
        travelled_counts=_travelled_counts(transfer),
        position_error=transfer.get("position_error"),
    )


def _rf001(ctx):
    ctx.require("carousel.measure", "carousel.sync", "carousel.status")

    before = _prepare(ctx)

    ctx.record("rf001_before", **before)

    counts_per_rev = ctx.profile.counts_per_rev
    half_turn = ctx.profile.production["servo"]["half_turn_counts"]
    tolerance = ctx.profile.production["servo"]["position_tolerance"]

    failure = None

    try:
        transaction = ctx.carousel.measure(1, sample_id="RF-001")

        answer = transaction["data"] or {}

    except Exception as error:
        answer = getattr(error, "data", None) or {}

        failure = {
            "type": type(error).__name__,
            "code": getattr(error, "code", None),
            "message": str(error)[:400],
        }

        transaction = None

    transfer = _transfer_record(answer)

    ctx.record("rf001_transfer", failure=failure, **transfer)

    travelled = _travelled_counts(transfer)

    ctx.check(failure is None,
              "the measurement completed without an error",
              evidence=failure or {"ok": True})

    if travelled is not None:
        agrees = abs(abs(travelled) - half_turn) <= max(tolerance, 64)

        ctx.check(agrees,
                  "the transfer travelled {} counts; a half turn is {} "
                  "counts".format(travelled, half_turn),
                  evidence={"travelled": travelled,
                            "expected": half_turn,
                            "tolerance": tolerance,
                            "counts_per_rev": counts_per_rev})

        if not agrees:
            ctx.defect(
                title="RF-001 reproduced: the transfer moved the plate "
                      "but the encoder disagrees",
                observed="the encoder reported {} counts of travel "
                         "where a half turn is {}".format(
                             travelled, half_turn),
                expected="{} counts, within {} counts".format(
                    half_turn, tolerance),
                reproduction=(
                    "synchronize with slot 1 at the loader",
                    "run HW-B8-002",
                ),
                suspected_layer="H-002 - the relationship between the "
                                "encoder and the mechanism",
                evidence={"transfer": transfer, "failure": failure,
                          "half_turn_counts": half_turn},
            )

            ctx.note(
                "RF-001 HAS RECURRED. Every carousel result above B3 is "
                "invalid until H-002 is explained: re-run HW-B2-002, "
                "HW-B2-004 and HW-B3-001, and do not treat B5, B8, B10 "
                "or B12 results from this run as evidence.")

    else:
        ctx.check(False,
                  "the transfer reported how far it travelled",
                  evidence={"answer_keys": sorted(answer.keys()),
                            "failure": failure})

    blocks = _spectra_from(answer)

    ctx.check(bool(blocks),
              "the acquisition began - illumination blocks are present "
              "in the answer",
              evidence={"blocks": sorted(blocks) if blocks else []})

    after = answer.get("carousel") or {}

    if not after:
        try:
            after = ctx.carousel.status()

        except Exception:                              # pragma: no cover
            after = {}

    ctx.record("rf001_after", **after)

    ctx.check(after.get("current_load_slot") == 1,
              "the return succeeded and slot 1 is back at the loader",
              evidence={"carousel": after})

    ctx.measure(stage="rf001", travelled_counts=travelled,
                expected_counts=half_turn,
                failure_code=(failure or {}).get("code") or "",
                blocks=len(blocks),
                load_slot_after=after.get("current_load_slot"),
                position_valid_after=after.get("position_valid"))


def _every_slot(ctx):
    ctx.require("carousel.measure", "carousel.sync", "carousel.status")

    _prepare(ctx)

    count = ctx.carousel.slot_count()

    outcomes = []
    firsts = {}
    positions = []

    for slot in range(1, count + 1):
        row = {"stage": "per_slot", "slot": slot}

        try:
            # SELECT, THEN MEASURE - THE PRODUCTION ORDER.
            #
            # `measure_raw` refuses a slot that is not the selected one:
            # "Slot 2 is not the selected slot (Slot 1 is)". That refusal
            # is correct and it is what the operator flow is built on -
            # the mechanism and the request have to agree before a
            # sample is swung to the scanner. This test measured four
            # slots without ever selecting one, so it reported three
            # failures against a firmware that was behaving exactly as
            # designed.
            ctx.carousel.select_slot(
                slot, sample_id="HW-B8-003-slot{}".format(slot))

            transaction = ctx.carousel.measure(
                slot, sample_id="HW-B8-003-slot{}".format(slot))

            answer = transaction["data"] or {}

            blocks = _spectra_from(answer)

            problems = ctx.sensor.validate_triad(
                {"illuminations": blocks} if blocks else answer)

            outcomes.append(not problems)

            row["ok"] = not problems
            row["elapsed_ms"] = transaction["elapsed_ms"]
            # `slot_id`, NOT `slot`. The answer's `slot` is the whole
            # slot RECORD - occupancy, sample id and the measurement
            # itself - so comparing it with a slot NUMBER could never
            # match, and the check that exists to prove no state leaked
            # between measurements failed on every run against a board.
            row["reported_slot"] = answer.get("slot_id")

            if row["reported_slot"] is None:
                row["reported_slot"] = (
                    (answer.get("slot") or {}).get("slot_id")
                    if isinstance(answer.get("slot"), dict)
                    else answer.get("slot")
                )

            carousel = answer.get("carousel") or {}

            row["load_slot_after"] = carousel.get("current_load_slot")
            row["position_valid"] = carousel.get("position_valid")

            positions.append(carousel.get("current_load_slot"))

            white = (blocks.get("white") or {}).get("acquisitions") or []

            if white and isinstance(white[0], dict):
                firsts[slot] = tuple(sorted(white[0].items()))

            # REQUIRED: an answer that does not name its slot cannot
            # show that state did not leak between measurements, which
            # is the whole objective.
            ctx.observed(
                "the answer for slot {} names the slot it was asked "
                "for".format(slot),
                row["reported_slot"], expected=slot,
                requirement=Requirement.REQUIRED, evidence=row)

            if problems:
                ctx.check(False,
                          "slot {}: the spectrum is well formed".format(
                              slot),
                          evidence={"problems": problems})

        except Exception as error:
            outcomes.append(False)

            row["ok"] = False
            row["error_type"] = type(error).__name__
            row["error"] = str(error)[:200]

        ctx.measure(**row)

    rate = failure_rate(outcomes)

    ctx.record("every_slot", failure_rate=rate, positions=positions)

    ctx.check(rate["all_passed"],
              "every slot measured successfully",
              evidence=rate)

    duplicates = [
        (first, second)
        for first in sorted(firsts)
        for second in sorted(firsts)
        if first < second and firsts[first] == firsts[second]
    ]

    ctx.check(not duplicates,
              "no two slots returned identical spectral data",
              evidence={"duplicate_pairs": duplicates})

    if duplicates:
        ctx.defect(
            title="two slots returned identical spectra",
            observed="slots {} produced byte-identical white "
                     "spectra".format(duplicates),
            expected="each measurement returns its own acquisition",
            reproduction=("run HW-B8-003",),
            suspected_layer="measurement state leaking between samples, "
                            "or a carousel that did not actually move",
            evidence={"duplicates": duplicates},
        )


def _data_survives(ctx):
    ctx.require("carousel.measure", "sensor.acquire_triad")

    _prepare(ctx)

    standalone = ctx.sensor.triad()

    standalone_report = standalone["data"] or {}

    standalone_problems = ctx.sensor.validate_triad(standalone_report)

    ctx.check(not standalone_problems,
              "the standalone triad is well formed",
              evidence={"problems": standalone_problems})

    integrated = ctx.carousel.measure(1, sample_id="HW-B8-004")

    answer = integrated["data"] or {}

    blocks = _spectra_from(answer)

    integrated_problems = ctx.sensor.validate_triad(
        {"illuminations": blocks} if blocks else answer)

    ctx.check(not integrated_problems,
              "the integrated measurement is well formed",
              evidence={"problems": integrated_problems})

    def channels_of(report):
        found = {}

        for name, block in (
                (report.get("illuminations") or {}).items()):
            acquisitions = (block or {}).get("acquisitions") or []

            if acquisitions and isinstance(acquisitions[0], dict):
                found[name] = sorted(acquisitions[0])

        return found

    standalone_channels = channels_of(standalone_report)
    integrated_channels = channels_of(
        {"illuminations": blocks} if blocks else answer)

    ctx.check(standalone_channels == integrated_channels,
              "the integrated path returns the same channels as the "
              "standalone acquisition",
              evidence={"standalone": standalone_channels,
                        "integrated": integrated_channels})

    ctx.measure(
        stage="data_survives",
        standalone_ms=standalone["elapsed_ms"],
        integrated_ms=integrated["elapsed_ms"],
        standalone_features=ctx.sensor.feature_count(standalone_report),
        integrated_features=ctx.sensor.feature_count(
            {"illuminations": blocks} if blocks else answer),
    )

    ctx.record("counters", **ctx.link.counters())


def _acquisition_centering(ctx):
    ctx.require("carousel.measure", "carousel.sync", "carousel.status",
                "servo.read_position")

    _prepare(ctx)

    count = ctx.carousel.slot_count()

    observations = []

    ctx.instruct(
        "Watch the sensor head for the whole of this test. After each "
        "measurement you will be asked what you saw while the plate "
        "was at the scanner.")

    for slot in range(1, count + 1):
        row = {"slot": slot}

        try:
            transaction = ctx.carousel.measure(
                slot, sample_id="HW-B8-005-slot{}".format(slot))

            answer = transaction["data"] or {}

            blocks = _spectra_from(answer)

            problems = ctx.sensor.validate_triad(
                {"illuminations": blocks} if blocks else answer)

            carousel = answer.get("carousel") or {}

            row.update({
                "ok": not problems,
                "elapsed_ms": transaction["elapsed_ms"],
                "believed_scan_slot": carousel.get("current_scan_slot"),
                "position_valid": carousel.get("position_valid"),
                "problems": len(problems),
            })

            ctx.check(not problems,
                      "slot {}: the spectrum is well formed".format(slot),
                      evidence={"problems": problems})

        except Exception as error:
            row.update({"ok": False,
                        "error": str(error)[:200],
                        "error_type": type(error).__name__})

            ctx.check(False,
                      "slot {}: the measurement completed".format(slot),
                      evidence=row)

        position = ctx.servo.position()

        row["encoder_after"] = position

        centred = ctx.ask(
            "Slot {}: while the plate was at the scanner, was the slot "
            "centred under the sensor head".format(slot))

        row["observed_centred"] = bool(centred)

        offset = None

        if not centred:
            offset = ctx.ask_number(
                "How far off centre was it",
                minimum=-100, maximum=100, unit="mm")

        row["observed_offset_mm"] = offset

        observations.append(row)

        ctx.measure(stage="acquisition_centering", **row)

    off_centre = [o for o in observations
                  if not o.get("observed_centred")]

    ctx.check(not off_centre,
              "every slot was observed centred under the head at the "
              "moment its spectrum was acquired",
              evidence={"off_centre": off_centre}, kind="OPERATOR")

    ctx.record("acquisition_centering", observations=observations,
               off_centre=len(off_centre))

    disagreements = [
        o for o in observations
        if not o.get("observed_centred") and o.get("position_valid")
    ]

    if disagreements:
        ctx.defect(
            title="the firmware believes the plate is in position while "
                  "the operator sees it off-centre",
            observed="slots {} were observed off-centre with the "
                     "firmware reporting a valid position".format(
                         [o["slot"] for o in disagreements]),
            expected="the operator's observation and the firmware's "
                     "belief agree",
            reproduction=("run HW-B8-005",),
            suspected_layer="mechanical slip after the encoder - H-002",
            evidence={"observations": disagreements},
        )

        ctx.note(
            "This is exactly the class of fault H-002 is about: the "
            "encoder cannot see slip that happens after it. Do not "
            "treat any slot-addressed result above this layer as valid "
            "until it is explained.")
