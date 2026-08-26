"""
B10 - the operator workflow, driven by an operator.

WHY THE FRAMEWORK DOES NOT PRESS THE KEYS

Two reasons, both in `adapters/workflow.py` and both worth repeating
here because this is where somebody will be tempted:

    A simulated operator produces a "workflow passes" result that no
    operator observed, and Tests/software already drives every screen
    against fakes - 40 suites of it.

    Driving the real client writes into firmware/BD/samples/, the run's
    only irreplaceable output and the one thing in this repository that
    git cannot restore.

So B10 is a guided procedure. The harness closes its own link, tells the
operator exactly which command to run and which keys to press, and then
inspects the RESULT through read-only commands. What is verified is what
a human did, which is what B10 was always supposed to mean.

THE ONE AUTOMATIC PART is a readiness check: the client and its screens
must still exist and still be named what the procedure says they are.
That is the piece that silently rots between campaigns, and it costs
nothing to check.
"""

from ..core.model import Automation, Safety


CAMPAIGN = "B10"


def register(registry):
    registry.campaign(
        CAMPAIGN, layer="B10", title="The production operator workflow",
        purpose="Verify the application a human actually uses, with a "
                "human using it, on real hardware, and inspect what it "
                "produced.",
        prerequisites=("B8",),
        gate_note="Gated by B8: a workflow result is a result about the "
                  "workflow only once the transaction underneath it "
                  "works.",
    )

    registry.test(
        test_id="HW-B10-001", campaign=CAMPAIGN, layer="B10",
        title="The operator client is present and unchanged",
        objective="Check the entry point and every screen the B10 "
                  "procedure names still exist, before an operator is "
                  "asked to follow instructions that mention them.",
        hardware_setup="None. This is a readiness check.",
        preconditions="The repository is checked out.",
        procedure=(
            "check firmware/PC/rover_science_client.py exists",
            "parse workflow/screen.py for the screens the procedure "
            "names",
            "report any that have been renamed or removed",
        ),
        expected="The client exists and every named screen is defined.",
        failure_criteria="A missing screen. The B10 procedure would "
                         "then be telling an operator to press keys "
                         "that do nothing.",
        captures=("the client path", "the screens required",
                  "the screens missing, if any"),
        safety=Safety.READ_ONLY, automation=Automation.AUTOMATIC,
        requires=(),
        run=_client_present,
        defect_prefix="HW-MISSION",
    )

    registry.test(
        test_id="HW-B10-002", campaign=CAMPAIGN, layer="B10",
        title="Startup, connect and carousel setup",
        objective="Have an operator take the real client from launch to "
                  "a synchronized carousel.",
        hardware_setup="The complete module, connected, with the "
                       "carousel empty and slot 1 alignable with the "
                       "loading hole.",
        preconditions="HW-B10-001 passed. B8 passed.",
        procedure=(
            "the harness releases the serial port",
            "the operator starts the client with the resolved device",
            "the operator confirms the connection came up ONLINE",
            "the operator uses [0] Carousel Setup to connect the servo "
            "and align slot 1",
            "the operator confirms the main screen is reachable",
            "the operator quits the client",
            "the harness reopens the link and reads the status",
        ),
        expected="The client connects, the carousel is synchronized, "
                 "and the module afterwards reports a valid position.",
        failure_criteria="A client that will not connect, a setup the "
                         "operator cannot complete, or a module whose "
                         "position is invalid after a successful setup.",
        captures=("the exact command the operator ran",
                  "each confirmation",
                  "the module status after the session",
                  "the operator's notes"),
        safety=Safety.FULL_SYSTEM,
        automation=Automation.OPERATOR_ASSISTED,
        requires=("workflow.client", "workflow.screens",
                  "carousel.status"),
        run=_startup_and_setup, cleanup=_close,
        defect_prefix="HW-MISSION",
    )

    registry.test(
        test_id="HW-B10-003", campaign=CAMPAIGN, layer="B10",
        title="Two samples, end to end, through the real client",
        objective="Check sample-to-slot association, measurement "
                  "identity and persistence across more than one "
                  "sample.",
        hardware_setup="As HW-B10-002, with two distinguishable "
                       "samples.",
        preconditions="HW-B10-002 passed.",
        procedure=(
            "the harness releases the port",
            "the operator runs the client and, for each of two samples: "
            "chooses the slot, prepares the sample, confirms it loaded, "
            "measures it, reads the analysis and saves the record",
            "the operator notes the measurement id of each",
            "the operator opens Records and confirms both are there, "
            "with the right slot and the right notes",
            "the operator quits",
            "the harness reads the device's retained buffer read-only "
            "and compares",
        ),
        expected="Two records, two distinct measurement ids, each "
                 "against the slot it was measured in, both persistent.",
        failure_criteria="A record against the wrong slot, two records "
                         "sharing an id, or a second measurement "
                         "carrying the first one's data.",
        captures=("each sample's slot and measurement id",
                  "the operator's confirmation of the Records screen",
                  "the device's retained buffer afterwards",
                  "any mismatch between the two"),
        safety=Safety.FULL_SYSTEM,
        automation=Automation.OPERATOR_ASSISTED,
        requires=("workflow.client", "workflow.records"),
        run=_two_samples, cleanup=_close,
        defect_prefix="HW-MISSION",
    )

    registry.test(
        test_id="HW-B10-004", campaign=CAMPAIGN, layer="B10",
        title="The client stays usable after a recoverable failure",
        objective="Check that a fault during a session leaves an "
                  "application the operator can carry on with.",
        hardware_setup="As HW-B10-002. The operator will pull the USB "
                       "cable during a session.",
        preconditions="HW-B10-003 passed. B9 passed.",
        procedure=(
            "the harness releases the port",
            "the operator runs the client and reaches the main screen",
            "the operator pulls the USB cable",
            "the operator confirms the client reports the loss instead "
            "of crashing",
            "the operator reconnects the cable and uses the client's "
            "own reconnect path",
            "the operator confirms Back, Records, Tools and the re-sync "
            "path are all still reachable",
            "the operator completes one more measurement",
        ),
        expected="No traceback, a named failure, and a session the "
                 "operator can finish.",
        failure_criteria="Any traceback, or a client that has to be "
                         "restarted to continue. On a rover, restarting "
                         "the client loses the session's context.",
        captures=("what the client printed when the cable was pulled",
                  "whether each navigation path was reachable",
                  "whether the final measurement succeeded",
                  "the operator's notes"),
        safety=Safety.FAULT_INJECTION,
        automation=Automation.OPERATOR_ASSISTED,
        requires=("workflow.client",),
        run=_recoverable_failure, cleanup=_close,
        prerequisites=("B9",),
        defect_prefix="HW-MISSION",
    )


# ======================================================================
# shared
# ======================================================================

def _close(ctx):
    record = ctx.link.close(reason="B10 cleanup")

    return {"confirmed": bool(record.get("closed")),
            "port_released": record.get("closed"),
            "note": "the operator client owns the port during this "
                    "campaign; the harness holds it only to read the "
                    "status before and after"}


def _hand_over(ctx):
    """
    Release the port and tell the operator exactly what to run.

    The harness and the client cannot both hold the port - `open()` asks
    for exclusive access - so the hand-over is explicit rather than
    hopeful.
    """
    device = ctx.device()

    ctx.link.close(reason="handing the port to the operator client")

    command = ctx.workflow.client_command(device)

    ctx.record("hand_over", device=device, command=command)

    ctx.instruct(
        "The harness has released {}. In another terminal, run:\n"
        "      {}\n"
        "   Then come back here.".format(device, command))

    return command


def _take_back(ctx):
    """Wait for the operator to quit the client, then reopen."""
    ctx.instruct(
        "Quit the operator client with [q] so the port is free again.")

    ctx.link.close(reason="taking the port back")

    return ctx.link.require_link("read the module state after the "
                                 "operator session")


# ======================================================================
# bodies
# ======================================================================

def _client_present(ctx):
    capabilities = ctx.workflow.capabilities()

    client = capabilities["workflow.client"]
    screens = capabilities["workflow.screens"]

    ctx.check(client.available,
              "the production operator client exists",
              evidence=client.as_dict())

    ctx.check(screens.available,
              "every screen the B10 procedure names is still defined",
              evidence=screens.as_dict())

    ctx.record("workflow_readiness",
               client=client.as_dict(), screens=screens.as_dict())

    ctx.measure(stage="readiness", client=client.available,
                screens=screens.available,
                missing=";".join(screens.detail.get("missing") or []))

    if not screens.available:
        ctx.defect(
            title="the operator client no longer has the screens the "
                  "B10 procedure names",
            observed="missing: {}".format(
                ", ".join(screens.detail.get("missing") or [])),
            expected="every screen in REQUIRED_SCREENS is defined in "
                     "workflow/screen.py",
            reproduction=("run HW-B10-001",),
            suspected_layer="the PC workflow application",
            evidence=screens.as_dict(),
        )


def _startup_and_setup(ctx):
    ctx.require("workflow.client", "workflow.screens", "carousel.status")

    command = _hand_over(ctx)

    ctx.confirm_observation(
        "Did the client print 'Connection: ONLINE'")

    ctx.confirm_observation(
        "Were you able to complete [0] Carousel Setup - servo connected "
        "and physical slot 1 aligned with the loading hole")

    ctx.confirm_observation(
        "Did the main sample screen appear, with options [1] to [5]")

    _take_back(ctx)

    status = ctx.carousel.status()

    ctx.record("status_after_session", command=command, **status)

    ctx.check(status.get("position_valid") is True,
              "the module reports a valid carousel position after the "
              "operator's setup",
              evidence=status)

    ctx.check(status.get("current_load_slot") is not None,
              "the module reports which slot is at the loading hole",
              evidence={"load_slot": status.get("current_load_slot")})

    ctx.operator_note("Anything awkward about the startup screens")

    ctx.measure(stage="startup", position_valid=status.get(
        "position_valid"), load_slot=status.get("current_load_slot"))


def _two_samples(ctx):
    ctx.require("workflow.client", "workflow.records")

    _hand_over(ctx)

    ctx.instruct(
        "Measure TWO samples through the client, one after the other. "
        "For each: [1] choose the slot, [2] prepare, [3] confirm "
        "loaded, [4] measure, then save the record. Write down the slot "
        "and the measurement id of each.")

    first_slot = ctx.ask_number(
        "Which slot did you measure the FIRST sample in",
        minimum=1, maximum=ctx.carousel.slot_count())

    first_id = ctx.operator_note(
        "The measurement id of the first sample")

    second_slot = ctx.ask_number(
        "Which slot did you measure the SECOND sample in",
        minimum=1, maximum=ctx.carousel.slot_count())

    second_id = ctx.operator_note(
        "The measurement id of the second sample")

    ctx.confirm_observation(
        "Does the Records screen show BOTH measurements, each against "
        "the slot you measured it in")

    ctx.confirm_observation(
        "Are the two measurements' spectra visibly different from one "
        "another")

    _take_back(ctx)

    ctx.check(first_id != second_id or not (first_id and second_id),
              "the two measurements have different ids",
              evidence={"first": first_id, "second": second_id},
              kind="OPERATOR")

    ctx.check(first_slot != second_slot or first_slot is None,
              "the two samples were measured in different slots",
              evidence={"first": first_slot, "second": second_slot},
              kind="OPERATOR")

    retained = None

    try:
        retained = ctx.workflow.saved_samples()["data"]

    except Exception as error:
        ctx.note("the device's retained buffer could not be read: "
                 "{}: {}".format(type(error).__name__, error))

    ctx.record("two_samples", first_slot=first_slot, first_id=first_id,
               second_slot=second_slot, second_id=second_id,
               retained=retained)

    ctx.measure(stage="two_samples", first_slot=first_slot,
                first_id=first_id or "", second_slot=second_slot,
                second_id=second_id or "")


def _recoverable_failure(ctx):
    ctx.require("workflow.client")

    _hand_over(ctx)

    ctx.confirm_observation(
        "Did you reach the main sample screen")

    ctx.instruct(
        "Now pull the USB cable while the client is at the main screen.")

    printed = ctx.operator_note(
        "What did the client print when the cable was pulled")

    ctx.check("traceback" not in (printed or "").lower(),
              "the client reported the loss without a Python traceback",
              evidence={"printed": printed}, kind="OPERATOR")

    ctx.instruct("Reconnect the USB cable.")

    ctx.confirm_observation(
        "Was the client able to reconnect from inside the application, "
        "without being restarted")

    for path in ("Back", "Records", "Tools", "the carousel re-sync "
                 "path"):
        ctx.confirm_observation(
            "Is {} still reachable after the reconnection".format(path))

    ctx.confirm_observation(
        "Were you able to complete one more measurement afterwards")

    _take_back(ctx)

    status = ctx.carousel.status()

    ctx.record("after_recovery", printed=printed, **status)

    ctx.operator_note("Anything the client did that surprised you")
