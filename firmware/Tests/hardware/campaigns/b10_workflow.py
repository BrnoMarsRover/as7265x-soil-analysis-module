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

from ..core.model import Automation, Requirement, Safety


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
        requirements=("HW-REQ-FLOW-001",),
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
        requirements=("HW-REQ-FLOW-002",),
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
        requirements=("HW-REQ-FLOW-003", "HW-REQ-FLOW-006"),
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
        requirements=("HW-REQ-FLOW-005",),
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

    registry.test(
        test_id="HW-B10-005", campaign=CAMPAIGN, layer="B10",
        requirements=("HW-REQ-FLOW-004", "HW-REQ-FLOW-006"),
        title="Records persist across a client restart, and protected "
              "data is untouched",
        objective="Machine-verify that a saved measurement survives the "
                  "client closing and reopening, and that the reference "
                  "libraries were not modified.",
        hardware_setup="The complete module. One measurement will be "
                       "taken and saved through the production client.",
        preconditions="HW-B10-003 passed.",
        procedure=(
            "hash every protected reference file BEFORE the session",
            "the operator runs the client, measures one sample and "
            "saves it, noting its measurement id",
            "the operator QUITS the client completely",
            "the operator starts the client again and opens Records",
            "the operator confirms the measurement is still listed",
            "the harness reads the device's retained buffer read-only",
            "hash every protected reference file again and compare",
        ),
        expected="The record is present after the restart, its id is "
                 "non-empty, and every protected file hashes "
                 "identically.",
        failure_criteria="A record that did not survive the restart, an "
                         "empty measurement id, or ANY protected "
                         "reference file whose hash changed. The "
                         "reference libraries are read-only by design "
                         "and a test that modifies them costs more than "
                         "the bug it was looking for.",
        captures=("protected file hashes before and after",
                  "the measurement id, and that it is non-empty",
                  "the operator's confirmation after the restart",
                  "the device's retained buffer"),
        safety=Safety.FULL_SYSTEM,
        automation=Automation.OPERATOR_ASSISTED,
        requires=("workflow.client", "workflow.records"),
        run=_persistence_across_restart, cleanup=_close,
        defect_prefix="HW-MISSION",
    )

    registry.test(
        test_id="HW-B10-006", campaign=CAMPAIGN, layer="B10",
        requirements=("HW-REQ-FLOW-007",),
        title="Raw acquisition survives an analysis failure",
        objective="Confirm that when analysis cannot complete, the raw "
                  "54-feature acquisition is still stored and "
                  "retrievable.",
        hardware_setup="The complete module. The operator will induce "
                       "an analysis failure by the least destructive "
                       "means available - typically running without an "
                       "active calibration.",
        preconditions="HW-B10-003 passed. The operator knows how to "
                      "reach a state where analysis fails but "
                      "acquisition does not.",
        procedure=(
            "ask the operator how they will make analysis fail, and "
            "record it",
            "the operator measures one sample in that state",
            "the operator reports whether the client showed an analysis "
            "error",
            "the operator reports whether the raw measurement was still "
            "saved",
            "the harness reads the device's retained buffer and checks "
            "the acquisition is complete and correctly shaped",
        ),
        expected="Analysis fails, and the raw acquisition is still "
                 "stored with all 54 features intact.",
        failure_criteria="A raw acquisition discarded because the "
                         "analysis that came after it failed. The "
                         "acquisition is the expensive part - it needs "
                         "the carousel, the illumination and the time - "
                         "and the analysis can always be redone later.",
        captures=("how the analysis failure was induced",
                  "what the client reported",
                  "whether the raw record was saved",
                  "the retained acquisition and its shape"),
        safety=Safety.FULL_SYSTEM,
        automation=Automation.OPERATOR_ASSISTED,
        requires=("workflow.client", "workflow.records"),
        run=_raw_survives_analysis_failure, cleanup=_close,
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

    # THE DEFECT THIS REPLACES: `first_id != second_id or not (first_id
    # and second_id)` passed when BOTH ids were empty, because two
    # empty strings are "not (first and second)". Two missing ids
    # counted as two distinct ids.
    ctx.require_observation("the first measurement id", first_id)
    ctx.require_observation("the second measurement id", second_id)

    if first_id and second_id:
        ctx.check(first_id != second_id,
                  "the two measurements have different ids",
                  evidence={"first": first_id, "second": second_id},
                  kind="OPERATOR")

    ctx.require_observation("the first sample's slot", first_slot)
    ctx.require_observation("the second sample's slot", second_slot)

    if first_slot is not None and second_slot is not None:
        ctx.check(first_slot != second_slot,
                  "the two samples were measured in different slots",
                  evidence={"first": first_slot,
                            "second": second_slot},
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


def _protected_hashes():
    """
    Every protected reference file, hashed.

    BD/ holds the reference libraries and is read-only by design. The
    one writable thing under it is samples/, which is the run's own
    output and is excluded here - hashing it would report every
    measurement as a violation.
    """
    import hashlib

    from ..configuration.profile import FIRMWARE_DIR

    protected = FIRMWARE_DIR / "BD"

    digest = {}

    if not protected.is_dir():
        return digest

    for path in sorted(protected.rglob("*")):
        if not path.is_file():
            continue

        if "__pycache__" in path.parts or "samples" in path.parts:
            continue

        digest[path.relative_to(FIRMWARE_DIR).as_posix()] = (
            hashlib.sha256(path.read_bytes()).hexdigest())

    return digest


def _persistence_across_restart(ctx):
    ctx.require("workflow.client", "workflow.records")

    before = _protected_hashes()

    ctx.record("protected_before", files=len(before))

    ctx.check(bool(before),
              "the protected reference files were found and hashed",
              evidence={"files": len(before)})

    _hand_over(ctx)

    ctx.instruct(
        "Measure ONE sample through the client and save it. Write down "
        "its measurement id exactly as the client shows it.")

    measurement_id = ctx.operator_note(
        "The measurement id, exactly as shown")

    ctx.require_observation("the measurement id", measurement_id)

    slot = ctx.ask_number(
        "Which slot did you measure it in",
        minimum=1, maximum=ctx.carousel.slot_count())

    ctx.require_observation("the slot the sample was measured in", slot)

    ctx.instruct(
        "Now QUIT the client completely with [q]. Then start it again "
        "and open Records.")

    still_there = ctx.ask(
        "Is measurement {} still listed after the "
        "restart".format(measurement_id))

    ctx.check(bool(still_there),
              "the saved measurement survived a full client restart",
              evidence={"measurement_id": measurement_id},
              kind="OPERATOR")

    right_slot = ctx.ask(
        "Does the record still show it against slot {}".format(slot))

    ctx.check(bool(right_slot),
              "the record still carries the slot it was measured in",
              evidence={"slot": slot}, kind="OPERATOR")

    _take_back(ctx)

    retained = None

    try:
        retained = ctx.workflow.saved_samples()["data"]

    except Exception as error:
        ctx.note("the device's retained buffer could not be read: "
                 "{}: {}".format(type(error).__name__, error))

    after = _protected_hashes()

    changed = sorted(
        name for name in set(before) | set(after)
        if before.get(name) != after.get(name))

    ctx.check(not changed,
              "every protected reference file hashes identically before "
              "and after the workflow session",
              evidence={"changed": changed, "files": len(after)})

    ctx.record("persistence", measurement_id=measurement_id, slot=slot,
               retained=retained, protected_changed=changed)

    ctx.measure(stage="persistence", measurement_id=measurement_id or "",
                slot=slot, survived_restart=bool(still_there),
                protected_files=len(after),
                protected_changed=len(changed))

    if changed:
        ctx.defect(
            title="a protected reference file was modified by a "
                  "workflow session",
            observed="changed: {}".format(", ".join(changed[:10])),
            expected="the reference libraries are read-only and hash "
                     "identically",
            reproduction=("run HW-B10-005",),
            suspected_layer="the PC persistence layer",
            evidence={"changed": changed},
        )


def _raw_survives_analysis_failure(ctx):
    ctx.require("workflow.client", "workflow.records")

    method = ctx.operator_note(
        "How will you make ANALYSIS fail without preventing the "
        "acquisition (for example: no active calibration selected)")

    ctx.require_observation(
        "a described way to induce an analysis failure", method)

    _hand_over(ctx)

    ctx.instruct(
        "Put the client into that state, then measure one sample.")

    analysis_failed = ctx.ask(
        "Did the client report that ANALYSIS failed or could not be "
        "produced")

    ctx.check(bool(analysis_failed),
              "the analysis failure was actually induced - otherwise "
              "this test proves nothing",
              evidence={"method": method}, kind="OPERATOR")

    if not analysis_failed:
        _take_back(ctx)

        ctx.inconclusive(
            "analysis did not fail, so whether a raw acquisition "
            "survives an analysis failure was never exercised",
            missing=("an induced analysis failure",),
            evidence={"method": method})

    saved = ctx.ask(
        "Was the RAW measurement still saved despite the analysis "
        "failure")

    measurement_id = ctx.operator_note(
        "The measurement id of that record, if it has one")

    _take_back(ctx)

    ctx.check(bool(saved),
              "the raw acquisition was still saved after the analysis "
              "failed",
              evidence={"measurement_id": measurement_id},
              kind="OPERATOR")

    retained = None
    shape_problems = None

    try:
        retained = ctx.workflow.saved_samples()["data"]

        samples = (retained or {}).get("samples") or []

        ctx.check(bool(samples),
                  "the device's retained buffer holds at least one "
                  "acquisition after the failure",
                  evidence={"count": len(samples)})

        if samples and measurement_id:
            detail = ctx.workflow.saved_sample(measurement_id)["data"]

            blocks = ((detail or {}).get("sample")
                      or {}).get("illuminations")

            if blocks:
                shape_problems = ctx.sensor.validate_triad(
                    {"illuminations": blocks})

                ctx.check(not shape_problems,
                          "the retained raw acquisition is complete and "
                          "correctly shaped",
                          evidence={"problems": shape_problems})

    except Exception as error:
        ctx.note("the retained buffer could not be inspected: {}: "
                 "{}".format(type(error).__name__, error))

    ctx.record("analysis_failure", method=method,
               measurement_id=measurement_id, saved=bool(saved),
               retained=retained, shape_problems=shape_problems)

    ctx.measure(stage="analysis_failure",
                method=(method or "")[:120],
                measurement_id=measurement_id or "",
                raw_saved=bool(saved),
                shape_problems=len(shape_problems or []))

    if not saved:
        ctx.defect(
            title="the raw acquisition is discarded when analysis fails",
            observed="the operator reports the measurement was not "
                     "saved after analysis failed ({})".format(method),
            expected="the raw 54-feature acquisition is stored "
                     "regardless of what happens downstream of it",
            reproduction=("run HW-B10-006",),
            suspected_layer="the PC measurement/persistence path",
            evidence={"method": method},
        )
