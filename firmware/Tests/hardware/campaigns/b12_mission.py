"""
B12 - the competition rehearsal.

THE LAST TEST, NEVER THE FIRST

A mission rehearsal that is run early tells you almost nothing: when it
fails - and it will - the failure could be the link, the servo, the
encoder, the sensor, the geometry, the workflow or the operator, and
separating them afterwards costs more than running B0 to B11 in order
would have.

So B12 is gated by B10, which is gated by B8, which is gated by B5 and
B7, which are gated by B3 and B6, which are gated by B2 and B1, which
are gated by B0. That chain is the whole argument of this framework: a
rehearsal PASS means something only because everything under it already
passed on real hardware.

WHAT A REHEARSAL IS

The operator does what they will do on the field, at the pace they will
do it, using only the production client - and the harness records what
happened, times it, and asks the questions afterwards that a debrief
would ask. It is not a test of the software; by this point the software
has been tested. It is a test of the whole instrument in the hands of
the person who will use it.
"""

from ..core.model import Automation, Safety
from ..core.analysis import summarize


CAMPAIGN = "B12"


def register(registry):
    registry.campaign(
        CAMPAIGN, layer="B12", title="Competition mission rehearsal",
        purpose="Run the mission the way it will be run, with the "
                "operator who will run it, and record what actually "
                "happened.",
        prerequisites=("B10",),
        gate_note="Gated by B10 and therefore by the whole chain. This "
                  "must never be the first hardware test executed.",
    )

    registry.test(
        test_id="HW-B12-001", campaign=CAMPAIGN, layer="B12",
        title="One complete mission rehearsal",
        objective="Take the instrument through a full competition "
                  "sequence - setup, every slot, analysis, records - "
                  "and time it.",
        hardware_setup="The competition configuration exactly: the "
                       "module as it will be mounted, the carousel "
                       "loaded as it will be loaded, the client on the "
                       "machine that will run it.",
        preconditions="Every campaign B0 to B11 has passed on hardware. "
                      "This is checked by the layer gate, and an "
                      "override here would defeat the point of the "
                      "test.",
        procedure=(
            "record the start time",
            "the operator runs the production client and completes the "
            "whole mission: setup, then each sample - choose, prepare, "
            "confirm, measure, read the analysis, save",
            "the operator confirms the records are all present",
            "record the finish time",
            "ask how long the operator expected it to take",
            "ask what, if anything, they had to work around",
            "read the module status afterwards",
        ),
        expected="A complete mission, every sample measured and saved, "
                 "the carousel position still valid at the end, and "
                 "nothing the operator had to work around.",
        failure_criteria="Any sample that could not be measured, any "
                         "record that did not save, a carousel left in "
                         "an unknown position, or any workaround the "
                         "operator needed. A workaround is a defect "
                         "that has not been written down yet.",
        captures=("start and finish time", "the elapsed mission time",
                  "the number of samples measured",
                  "every workaround the operator reported",
                  "the module status afterwards",
                  "the operator's debrief, verbatim"),
        safety=Safety.FULL_SYSTEM,
        automation=Automation.OPERATOR_ASSISTED,
        requires=("workflow.client", "workflow.records",
                  "carousel.status"),
        run=_rehearsal, cleanup=_close,
        defect_prefix="HW-MISSION",
    )

    registry.test(
        test_id="HW-B12-002", campaign=CAMPAIGN, layer="B12",
        title="Repeated mission rehearsals",
        objective="Establish that the mission is repeatable and that "
                  "its duration is predictable enough to plan around.",
        hardware_setup="As HW-B12-001.",
        preconditions="HW-B12-001 passed.",
        procedure=(
            "run the rehearsal N times, resetting the samples between "
            "runs",
            "record the elapsed time of each",
            "record every workaround from every run",
            "compute the distribution of mission times",
        ),
        expected="Every rehearsal completes, and the durations are "
                 "consistent.",
        failure_criteria="Any rehearsal that cannot complete, or a "
                         "duration that varies so much the mission "
                         "cannot be planned.",
        captures=("elapsed time per rehearsal",
                  "the distribution", "workarounds per rehearsal",
                  "the operator's notes per rehearsal"),
        safety=Safety.FULL_SYSTEM,
        automation=Automation.OPERATOR_ASSISTED,
        requires=("workflow.client", "carousel.status"),
        run=_repeated_rehearsals, cleanup=_close,
        default_iterations=3, max_iterations=20,
        prerequisites=("HW-B12-001",),
        defect_prefix="HW-MISSION",
    )


# ======================================================================
# shared
# ======================================================================

def _close(ctx):
    from .b10_workflow import _close as shared

    return shared(ctx)


def _one_rehearsal(ctx, label):
    """
    One mission, handed to the operator and timed.

    Returns the record rather than making the checks, so that
    HW-B12-002 can run several and compare them.
    """
    import time

    from .b10_workflow import _hand_over, _take_back

    command = _hand_over(ctx)

    started = time.time()

    ctx.instruct(
        "Run the COMPLETE mission now, exactly as you would on the "
        "field. Take as long as it takes. Press Enter here when the "
        "mission is finished and you have quit the client.")

    finished = time.time()

    _take_back(ctx)

    samples = ctx.ask_number(
        "{}: how many samples did you measure".format(label),
        minimum=0, maximum=100)

    complete = ctx.ask(
        "{}: did every sample measure and save successfully".format(
            label))

    workarounds = ctx.operator_note(
        "{}: what did you have to work around, if anything (blank if "
        "nothing)".format(label))

    expected_minutes = ctx.ask_number(
        "{}: how long did you EXPECT this to take".format(label),
        minimum=0, maximum=600, unit="minutes")

    status = None

    try:
        status = ctx.carousel.status()

    except Exception as error:                         # pragma: no cover
        status = {"error": "{}: {}".format(type(error).__name__, error)}

    record = {
        "label": label,
        "command": command,
        "elapsed_s": round(finished - started, 1),
        "elapsed_min": round((finished - started) / 60.0, 2),
        "samples": samples,
        "complete": bool(complete),
        "workarounds": workarounds,
        "expected_minutes": expected_minutes,
        "status_after": status,
    }

    ctx.event("rehearsal", **record)

    ctx.measure(stage="rehearsal", label=label,
                elapsed_min=record["elapsed_min"], samples=samples,
                complete=record["complete"],
                workarounds=workarounds or "",
                expected_minutes=expected_minutes,
                position_valid=(status or {}).get("position_valid"))

    return record


# ======================================================================
# bodies
# ======================================================================

def _rehearsal(ctx):
    ctx.require("workflow.client", "workflow.records", "carousel.status")

    record = _one_rehearsal(ctx, "rehearsal")

    ctx.record("mission", **record)

    ctx.check(record["complete"],
              "every sample measured and saved successfully",
              evidence=record, kind="OPERATOR")

    ctx.check(not (record["workarounds"] or "").strip(),
              "the operator needed no workarounds",
              evidence={"workarounds": record["workarounds"]},
              kind="OPERATOR")

    status = record["status_after"] or {}

    ctx.check(status.get("position_valid") is True,
              "the carousel position is still valid at the end of the "
              "mission",
              evidence=status)

    ctx.confirm_observation(
        "Does the Records screen contain every measurement from this "
        "mission")

    ctx.operator_note("Debrief: anything at all about this rehearsal")

    if (record["workarounds"] or "").strip():
        ctx.defect(
            title="the mission rehearsal needed an operator workaround",
            observed=record["workarounds"],
            expected="a mission that runs without the operator having "
                     "to work around anything",
            reproduction=("run HW-B12-001",),
            suspected_layer="UNKNOWN - the workaround describes it",
            evidence=record,
        )


def _repeated_rehearsals(ctx):
    ctx.require("workflow.client", "carousel.status")

    rounds = ctx.iterations()

    records = []

    for index in range(1, rounds + 1):
        if index > 1:
            ctx.instruct(
                "Reset the carousel and the samples for rehearsal {} of "
                "{}.".format(index, rounds))

        records.append(_one_rehearsal(
            ctx, "rehearsal {}".format(index)))

    durations = [r["elapsed_min"] for r in records]

    distribution = summarize(durations)

    ctx.record("repeated_rehearsals", records=records,
               duration=distribution)

    ctx.check(all(r["complete"] for r in records),
              "every rehearsal completed",
              evidence={"records": [
                  {"label": r["label"], "complete": r["complete"]}
                  for r in records]},
              kind="OPERATOR")

    workarounds = [r for r in records if (r["workarounds"] or "").strip()]

    ctx.check(not workarounds,
              "no rehearsal needed a workaround",
              evidence={"workarounds": [
                  {"label": r["label"], "workaround": r["workarounds"]}
                  for r in workarounds]},
              kind="OPERATOR")

    if distribution and distribution["n"] >= 2:
        ctx.check(distribution["range"] <= max(
                      5.0, distribution["mean"] * 0.5),
                  "the mission duration is consistent: {} to {} minutes "
                  "(mean {})".format(distribution["min"],
                                     distribution["max"],
                                     distribution["mean"]),
                  evidence=distribution)

        ctx.note(
            "Mission time over {} rehearsals: mean {} min, worst {} "
            "min. That worst case is the number to plan the "
            "competition slot against.".format(
                distribution["n"], distribution["mean"],
                distribution["max"]))
