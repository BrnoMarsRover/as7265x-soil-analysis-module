"""
B2 - the ST3215, without turning anything.

Everything here is a conversation. The servo is asked what it is, what
baud it is running, which mode it is in, whether torque is on, what its
angle limits are and where it thinks it is - and none of that moves the
carousel.

WHY THIS LAYER IS SEPARATE FROM B3

H-002 is a question about the relationship between a commanded movement
and a physical one. Asking that question is pointless until it is known
that the servo is answering at all, that it is the servo we think it is,
and that it is in the mode the driver assumes. Every one of those is a
plausible explanation for the 2-counts-versus-2048 contradiction, and
each is cheaper to check than a movement.

THE ECHO_ONLY FAULT IS WHY THE BUS SCAN IS A TEST AND NOT A TOOL

On this bench the scan answered only with TX and RX exchanged, and only
ID 1 was probed. `servo_bus_scan` probes every documented baud rate in
both pin orders, which turns "no answer from servo 1" - a sentence that
names four assumptions and tests none - into a table.
"""

from ..core.model import (Automation, IterationKind, Requirement,
                          Safety)
from ..core.analysis import centred_error, summarize


CAMPAIGN = "B2"


def register(registry):
    registry.campaign(
        CAMPAIGN, layer="B2", title="Direct ST3215 communication",
        purpose="Establish that the servo answers, that it is the servo "
                "the configuration describes, and that it is in the "
                "mode the driver assumes - before any movement is "
                "commanded.",
        prerequisites=("B1",),
        gate_note="Gated by B1: a servo timeout over a link that drops "
                  "frames is a link result wearing a servo costume.",
    )

    registry.test(
        test_id="HW-B2-001", campaign=CAMPAIGN, layer="B2",
        requirements=("HW-REQ-SERVO-001",),
        title="The servo driver comes up",
        objective="Bring the ST3215 driver up over the real UART and "
                  "confirm the servo answers a ping.",
        hardware_setup="ST3215 wired to the configured pins, powered, "
                       "carousel attached but free.",
        preconditions="HW-B0-005 confirmed the wiring. B1 passed.",
        procedure=(
            "send connect_servo",
            "check the answer reports the servo attached",
            "record the UART id, pins and baud the driver used",
        ),
        expected="connect_servo succeeds and the status shows a servo "
                 "attached. NOTHING MOVES.",
        failure_criteria="Any servo error. SERVO_NO_RESPONSE means the "
                         "id, the baud, the pin order or the power is "
                         "wrong - HW-B2-003 says which.",
        captures=("the connect answer", "uart id, tx pin, rx pin, baud",
                  "the servo status afterwards", "the error if it failed"),
        safety=Safety.COMMUNICATION, automation=Automation.AUTOMATIC,
        requires=("servo.connect",),
        run=_connect, cleanup=_release,
        defect_prefix="HW-SERVO",
        notes="Replaces HW-200 in PHASE_B_CAMPAIGNS.md.",
    )

    registry.test(
        test_id="HW-B2-002", campaign=CAMPAIGN, layer="B2",
        requirements=("HW-REQ-SERVO-002", "HW-REQ-SERVO-003"),
        title="Servo identity, baud, mode, torque and limits",
        objective="Read every register the driver's assumptions rest "
                  "on, and compare each with what config.py expects.",
        hardware_setup="As HW-B2-001.",
        preconditions="HW-B2-001 passed.",
        procedure=(
            "send servo_diagnostics - it moves nothing",
            "check every step reports ok",
            "compare the reported id with ST3215_SERVO_ID",
            "compare the reported baud with ST3215_BAUD",
            "compare the reported mode with ST3215_MODE",
            "record the angle limits and the torque state",
            "record the position feedback",
        ),
        expected="Every step ok, and the id, baud and mode match the "
                 "configuration.",
        failure_criteria="A mode that is not STEP: a relative goal "
                         "would then be interpreted as an absolute "
                         "one, which is hypothesis 1 of H-002 and would "
                         "explain the whole contradiction.",
        captures=("every diagnostic step with its value",
                  "id, baud code, mode, mode name, torque",
                  "min and max angle limits", "position feedback",
                  "the bus statistics"),
        safety=Safety.COMMUNICATION, automation=Automation.AUTOMATIC,
        requires=("servo.diagnostics",),
        run=_diagnostics, cleanup=_release,
        assumption="H-002", defect_prefix="HW-SERVO",
        notes="Replaces HW-203. This is the cheapest H-002 hypothesis "
              "to eliminate and must be run before HW-B3-001.",
    )

    registry.test(
        test_id="HW-B2-003", campaign=CAMPAIGN, layer="B2",
        requirements=("HW-REQ-SERVO-002",),
        title="Bus scan: every baud, both pin orders",
        objective="Turn 'no answer from servo 1' into a table that says "
                  "which of the four assumptions is wrong.",
        hardware_setup="ST3215 powered. The scan probes the bus; it "
                       "commands no movement.",
        preconditions="B1 passed. Run this whenever HW-B2-001 fails.",
        procedure=(
            "send servo_bus_scan with swap enabled",
            "record every id that answered, at which baud, in which pin "
            "order",
            "check the configured id answers at the configured baud in "
            "the configured pin order",
        ),
        expected="The configured servo id answers at the configured "
                 "baud with the pins as wired.",
        failure_criteria="An answer only with TX and RX exchanged - the "
                         "ECHO_ONLY signature seen on this bench, which "
                         "means the harness or the config has the pins "
                         "the wrong way round. An answer at a different "
                         "baud means the servo was reconfigured.",
        captures=("the whole scan table", "which combinations answered",
                  "whether the configured combination answered"),
        safety=Safety.COMMUNICATION, automation=Automation.AUTOMATIC,
        requires=("servo.bus_scan",),
        run=_bus_scan, cleanup=_release,
        defect_prefix="HW-SERVO",
        notes="Replaces HW-201 and HW-202.",
    )

    registry.test(
        test_id="HW-B2-004", campaign=CAMPAIGN, layer="B2",
        requirements=("HW-REQ-SERVO-004",),
        iteration_kind=IterationKind.REQUEST,
        characterization_min_iterations=10,
        title="Repeated position reads with nothing commanded",
        objective="Establish whether the reported position is stable "
                  "when the mechanism is standing still.",
        hardware_setup="Servo connected, carousel free, nothing "
                       "touching it.",
        preconditions="HW-B2-002 passed.",
        procedure=(
            "read the position N times with no command between reads",
            "record every reading and the interval between them",
            "check every reading is identical",
        ),
        expected="Every reading identical. A stationary servo reporting "
                 "a changing position has a read problem, not a "
                 "movement problem.",
        failure_criteria="Any variation. That is hypothesis 5 of H-002 "
                         "- the read racing something - and it must be "
                         "settled before a movement measurement means "
                         "anything.",
        captures=("every reading with its timestamp",
                  "the spread", "the distinct values seen"),
        safety=Safety.COMMUNICATION, automation=Automation.AUTOMATIC,
        requires=("servo.read_position",),
        run=_repeated_reads, cleanup=_release,
        default_iterations=20, max_iterations=500,
        assumption="H-002", defect_prefix="HW-SERVO",
    )

    registry.test(
        test_id="HW-B2-013", campaign=CAMPAIGN, layer="B2",
        requirements=("HW-REQ-SERVO-016",),
        iteration_kind=IterationKind.MOVEMENT,
        characterization_min_iterations=3,
        title="The reported position CHANGES when the carousel moves",
        objective="Prove the one property production depends on, which "
                  "no read-only check can establish: that the number "
                  "the driver verifies movements against actually "
                  "tracks the mechanism.",
        hardware_setup="Servo connected, carousel free to turn.",
        preconditions="HW-B2-002 and HW-B2-004 passed.",
        procedure=(
            "read the position with nothing commanded",
            "command a movement of one slot",
            "read the position again",
            "check the difference equals the commanded counts, within "
            "the production tolerance",
            "repeat in the opposite direction and check it comes back",
        ),
        expected="The reported position changes by the commanded "
                 "amount, in the commanded direction, every time.",
        failure_criteria="A position that does not change, or changes "
                         "by something unrelated to the command. That "
                         "is the bench observation of H-002 and it "
                         "means the driver is verifying movements "
                         "against a quantity that is not a position.",
        captures=("the position before and after each leg",
                  "the commanded counts", "the difference",
                  "the following error and the trajectory register"),
        safety=Safety.MOTION, automation=Automation.AUTOMATIC,
        requires=("servo.test_move", "servo.read_position",
                  "servo.connect"),
        run=_position_tracks_movement, cleanup=_release,
        default_iterations=3, max_iterations=50,
        assumption="H-002", defect_prefix="HW-SERVO",
        notes="THIS TEST REPLACES THE CONFIDENCE THE OLD `feedback` "
              "DIAGNOSTIC STEP GAVE AND DID NOT EARN. That step "
              "reported PASS on a bench where a completed half turn "
              "measured two counts of travel, because reading a "
              "register successfully was all it ever checked. The "
              "step is called `telemetry_read` now and says in its "
              "own report that it does not establish this.",
    )

    registry.test(
        test_id="HW-B2-014", campaign=CAMPAIGN, layer="B2",
        requirements=("HW-REQ-SERVO-016", "HW-REQ-SERVO-008"),
        title="Diagnostics does not claim more than it proves",
        objective="Check that the read-only diagnostic report says, in "
                  "itself, what it does not establish - so a screen of "
                  "PASS lines cannot be read as a qualified actuator.",
        hardware_setup="Servo connected. NOTHING MOVES.",
        preconditions="HW-B2-002 passed.",
        procedure=(
            "send servo_diagnostics",
            "check no step is called `feedback`",
            "check the report states what the telemetry read does and "
            "does not prove",
            "check it names the register semantics for the mode the "
            "servo is actually in",
            "check the telemetry carries the following error and the "
            "trajectory register separately from the position",
        ),
        expected="A report whose own text bounds its claim, and which "
                 "exposes the two registers the position is derived "
                 "from.",
        failure_criteria="A diagnostic that reports position feedback "
                         "as PASS with nothing said about the limit of "
                         "that claim. That wording is what let H-002 "
                         "survive a green board.",
        captures=("the step names", "the claim and its stated limit",
                  "the position semantics string",
                  "the telemetry keys"),
        safety=Safety.COMMUNICATION, automation=Automation.AUTOMATIC,
        requires=("servo.diagnostics", "servo.connect"),
        run=_diagnostics_bounds_its_claim, cleanup=_release,
        assumption="H-002", defect_prefix="HW-SERVO",
    )

    registry.test(
        test_id="HW-B2-005", campaign=CAMPAIGN, layer="B2",
        requirements=("HW-REQ-SERVO-005",),
        title="The calibration report matches the shipped configuration",
        objective="Prove the numbers the servo is being driven with are "
                  "the numbers in config.py.",
        hardware_setup="Servo connected.",
        preconditions="HW-B2-001 passed.",
        procedure=(
            "send get_servo_calibration",
            "compare speed, acceleration, tolerance, settle time, poll "
            "interval and move timeout with the production values",
            "check the report declares itself non-editable",
        ),
        expected="Every value matches config.py exactly.",
        failure_criteria="Any mismatch: the deployed firmware is not "
                         "the firmware in this working tree, and every "
                         "result in this campaign is being attributed to "
                         "the wrong code.",
        captures=("the calibration report",
                  "the production values compared against"),
        safety=Safety.COMMUNICATION, automation=Automation.AUTOMATIC,
        requires=("servo.calibration",),
        run=_calibration_matches, cleanup=_release,
        defect_prefix="HW-SERVO",
    )

    registry.test(
        test_id="HW-B2-006", campaign=CAMPAIGN, layer="B2",
        requirements=("HW-REQ-SERVO-007",),
        title="Raw ST3215 packet capture",
        objective="Capture the bytes of a position read exactly as they "
                  "came off the servo bus, so the driver's parsing can "
                  "be checked against the wire.",
        hardware_setup="Servo connected.",
        preconditions="HW-B2-002 passed.",
        procedure=(
            "send a raw read of the present-position register",
            "record the request bytes and the reply bytes verbatim",
            "compare the parsed value with the bytes",
        ),
        expected="The parsed position is what the reply bytes say it "
                 "is.",
        failure_criteria="A parsed value the bytes do not support, "
                         "which would be a driver defect rather than a "
                         "mechanism one.",
        captures=("request bytes", "reply bytes", "parsed value",
                  "checksum"),
        safety=Safety.COMMUNICATION, automation=Automation.AUTOMATIC,
        requires=("diagnostic.servo_raw",),
        run=_raw_packet, cleanup=_release,
        assumption="H-002", defect_prefix="HW-SERVO",
        notes="BLOCKED until the test-side diagnostic agent is "
              "deployed. The competition firmware has no raw "
              "servo passthrough and must not grow one; the "
              "agent under test_side_firmware/ provides it "
              "read-only, whitelisted and manually deployed.",
    )

    registry.test(
        test_id="HW-B2-007", campaign=CAMPAIGN, layer="B2",
        requirements=("HW-REQ-SERVO-006",),
        title="The firmware refuses a malformed servo command",
        objective="Check the error path is a structured refusal rather "
                  "than a movement or a crash.",
        hardware_setup="Servo connected. NOTHING SHOULD MOVE during "
                       "this test - that is what it is checking.",
        preconditions="HW-B2-001 passed.",
        procedure=(
            "send servo_test_move without 'confirm' and expect "
            "CONFIRMATION_REQUIRED",
            "send servo_test_move with an unknown kind and expect "
            "BAD_REQUEST",
            "send servo_test_move with a repeat count outside 1..8 and "
            "expect BAD_REQUEST",
            "confirm with the operator that nothing moved",
        ),
        expected="Three structured refusals, each with a code, and a "
                 "carousel that did not move.",
        failure_criteria="Any movement, any unhandled exception, or a "
                         "success where a refusal was required. A "
                         "confirmation gate that can be skipped is not "
                         "a gate.",
        captures=("each request and its error code",
                  "the carousel status before and after",
                  "the operator's confirmation that nothing moved"),
        safety=Safety.COMMUNICATION,
        automation=Automation.OPERATOR_ASSISTED,
        requires=("servo.test_move",),
        run=_refusals, cleanup=_release,
        defect_prefix="HW-SERVO",
    )

    registry.test(
        test_id="HW-B2-008", campaign=CAMPAIGN, layer="B2",
        requirements=("HW-REQ-SERVO-008",),
        title="Full servo telemetry",
        objective="Read position, speed, load, voltage, current, "
                  "temperature and the moving/status bits, so a later "
                  "movement failure can be attributed to the supply, "
                  "the load or the encoder rather than guessed at.",
        hardware_setup="Servo connected and at rest. Nothing moves.",
        preconditions="HW-B2-002 passed.",
        procedure=(
            "read every telemetry register the diagnostic agent "
            "exposes",
            "record which registers answered and which did not",
            "check the voltage is plausible for the configured supply",
            "check the temperature is plausible for a servo at rest",
            "check the moving bit reads not-moving on a stationary "
            "servo",
        ),
        expected="Every telemetry register answers, and position, "
                 "voltage, temperature and the moving bit are "
                 "self-consistent for a servo at rest.",
        failure_criteria="A moving bit set on a stationary servo, or a "
                         "register that will not read. The VALUES are "
                         "characterization: no datasheet limits are "
                         "recorded in this repository, so this "
                         "establishes a baseline rather than judging "
                         "one.",
        captures=("every telemetry register with its value",
                  "which registers failed and why",
                  "the moving and status bits"),
        safety=Safety.COMMUNICATION, automation=Automation.AUTOMATIC,
        requires=("diagnostic.servo_feedback",),
        run=_servo_telemetry, cleanup=_release,
        defect_prefix="HW-SERVO",
        notes="BLOCKED until the test-side diagnostic agent is "
              "deployed. servo_diagnostics reads a fixed subset and "
              "does not expose load, current or temperature.",
    )

    registry.test(
        test_id="HW-B2-009", campaign=CAMPAIGN, layer="B2",
        requirements=("HW-REQ-SERVO-009",),
        title="Torque enable, disable and a bounded stop",
        objective="Confirm torque can be commanded both ways and that a "
                  "stop is bounded, without ever leaving the carousel "
                  "free to be turned by gravity.",
        hardware_setup="Servo connected. THE CAROUSEL MUST BE EMPTY and "
                       "in a position where losing torque cannot let it "
                       "fall - if the plate is loaded or unbalanced, "
                       "skip this test.",
        preconditions="HW-B2-002 passed. The operator has confirmed the "
                      "mechanism cannot move under its own weight.",
        procedure=(
            "ask the operator to confirm the plate cannot fall if "
            "torque is released",
            "read the torque state",
            "disable torque and read it back",
            "ask the operator whether the shaft became free",
            "re-enable torque and read it back",
            "ask the operator whether the shaft became firm again",
            "issue a stop and confirm it returns promptly",
            "leave torque ENABLED",
        ),
        expected="Torque reads back as commanded in both directions, "
                 "the operator observes the matching mechanical change, "
                 "the stop returns within its timeout, and torque is "
                 "left enabled.",
        failure_criteria="A torque state that does not read back, a "
                         "mechanical state that contradicts it, or a "
                         "stop that does not return. Leaving torque "
                         "disabled at the end is a failure in itself.",
        captures=("torque state at each step",
                  "the operator's observation of the shaft",
                  "the stop's elapsed time",
                  "the torque state at the end"),
        safety=Safety.MOTION, automation=Automation.OPERATOR_ASSISTED,
        requires=("servo.torque", "servo.stop", "servo.diagnostics"),
        run=_torque_and_stop, cleanup=_release,
        defect_prefix="HW-SERVO",
    )

    registry.test(
        test_id="HW-B2-010", campaign=CAMPAIGN, layer="B2",
        requirements=("HW-REQ-SERVO-010",),
        title="The servo mode survives a reset and a power cycle",
        objective="Check the ST3215 still reports STEP mode after the "
                  "ESP32 restarts and after the servo itself loses "
                  "power.",
        hardware_setup="Servo connected. The operator can power the "
                       "servo supply down and up independently of the "
                       "ESP32.",
        preconditions="HW-B2-002 passed with mode_correct true.",
        procedure=(
            "read the mode register and record it",
            "reset the ESP32 and wait for it to answer",
            "reconnect the servo and read the mode again",
            "ask the operator to power the SERVO supply down and up",
            "reconnect and read the mode a third time",
            "compare all three against the configured mode",
        ),
        expected="The mode reads the configured STEP mode all three "
                 "times.",
        failure_criteria="A mode that reverts on power-up. That would "
                         "reproduce H-002 intermittently - a relative "
                         "goal read as an absolute one - and it would "
                         "look like a software fault every time.",
        captures=("the mode before, after the ESP32 reset, and after "
                  "the servo power cycle",
                  "the configured mode compared against",
                  "the time to answer after each restart"),
        safety=Safety.POWER_CYCLE,
        automation=Automation.OPERATOR_ASSISTED,
        requires=("servo.diagnostics", "servo.connect",
                  "link.hard_reset"),
        run=_mode_persistence, cleanup=_release,
        assumption="H-002", defect_prefix="HW-SERVO",
    )

    registry.test(
        test_id="HW-B2-011", campaign=CAMPAIGN, layer="B2",
        requirements=("HW-REQ-DIAG-001", "HW-REQ-DIAG-002"),
        title="The diagnostic agent identifies itself and is read-only",
        objective="Before trusting anything the diagnostic agent says, "
                  "confirm it is the build this adapter expects and "
                  "that its safety properties hold.",
        hardware_setup="The test-side diagnostic agent deployed and "
                       "running, per test_side_firmware/DEPLOYMENT.md. "
                       "The competition firmware is NOT running.",
        preconditions="The bench profile records the deployment.",
        procedure=(
            "send diag_identify",
            "check the protocol string and version are the diagnostic "
            "ones, not the production ones",
            "check the agent reports moves: false",
            "check its register whitelist is the one this adapter "
            "expects",
            "attempt a read of a register NOT on the whitelist and "
            "check it is refused",
            "attempt a read longer than the bound and check it is "
            "refused",
        ),
        expected="The agent identifies itself unmistakably, declares no "
                 "movement capability, and refuses both out-of-bounds "
                 "requests.",
        failure_criteria="A production firmware answering - its replies "
                         "would be read as register bytes - or an agent "
                         "that accepts a register outside its "
                         "whitelist. A wrong access to the ST3215 "
                         "memory table can change the servo id or baud "
                         "rate and take the bus away entirely.",
        captures=("the identity answer in full",
                  "the declared register whitelist",
                  "the refusal for each out-of-bounds attempt"),
        safety=Safety.COMMUNICATION, automation=Automation.AUTOMATIC,
        requires=("diagnostic.agent",),
        run=_agent_identity, cleanup=_release,
        defect_prefix="HW-SERVO",
        notes="BLOCKED until the agent is deployed. Deployment is "
              "manual and deliberate - see "
              "test_side_firmware/DEPLOYMENT.md.",
    )

    registry.test(
        test_id="HW-B2-012", campaign=CAMPAIGN, layer="B2",
        requirements=("HW-REQ-DIAG-003",),
        title="Production firmware is restored after diagnostic use",
        objective="Confirm the competition firmware is back and the "
                  "diagnostic agent is gone, and record both hashes.",
        hardware_setup="The diagnostic session is finished and the "
                       "production firmware has been redeployed.",
        preconditions="HW-B2-011 ran at some point, and the operator "
                      "has followed the restoration steps in "
                      "test_side_firmware/DEPLOYMENT.md.",
        procedure=(
            "confirm with the operator that diagnostic_agent.py has "
            "been removed from the board",
            "send ping and check the PRODUCTION firmware answers",
            "check the diagnostic protocol no longer answers",
            "record the restored firmware version",
            "check the bench profile no longer claims a deployment",
        ),
        expected="The production firmware answers, the diagnostic "
                 "protocol does not, and the profile agrees.",
        failure_criteria="A diagnostic agent still present, or a "
                         "profile that still claims a deployment. Any "
                         "prerequisite PASS earned while the agent was "
                         "deployed is void by fingerprint anyway - but "
                         "a competition run must not start with a "
                         "diagnostic build on the board.",
        captures=("the operator's confirmation of removal",
                  "the production firmware identity after restoration",
                  "the profile's diagnostic_firmware section",
                  "both firmware hashes where recorded"),
        safety=Safety.COMMUNICATION,
        automation=Automation.OPERATOR_ASSISTED,
        requires=("link.ping",),
        run=_firmware_restored, cleanup=_release,
        defect_prefix="HW-SERVO",
    )


# ======================================================================
# shared
# ======================================================================

def _release(ctx):
    """
    Leave the servo as it was found: torque untouched, port released.

    Deliberately does NOT disable torque. A carousel whose torque is
    dropped can be turned by gravity or by a hand brushing past it, and
    the position reference would be silently lost between tests.
    """
    record = {"torque_left_as_found": True}

    closed = ctx.link.close(reason="B2 cleanup")

    record["port_released"] = closed.get("closed")
    record["confirmed"] = bool(closed.get("closed"))

    return record


def _servo_error(transaction):
    data = transaction.get("data") if isinstance(transaction, dict) else None

    return data or {}


# ======================================================================
# bodies
# ======================================================================

def _connect(ctx):
    ctx.require("servo.connect")

    ctx.record("connect_servo")

    try:
        transaction = ctx.link.request(
            "connect_servo", timeout=ctx.servo._move_timeout())

    except Exception as error:
        ctx.check(False, "connect_servo succeeded",
                  evidence={"error_type": type(error).__name__,
                            "error": str(error),
                            "code": getattr(error, "code", None),
                            "data": getattr(error, "data", None)})

        ctx.defect(
            title="the ST3215 did not come up",
            observed="{}: {}".format(type(error).__name__, error),
            expected="connect_servo attaches the driver and the servo "
                     "answers",
            reproduction=("run HW-B2-001", "then run HW-B2-003"),
            suspected_layer="servo bus (id, baud, pin order or power)",
            evidence={"code": getattr(error, "code", None),
                      "data": getattr(error, "data", None)},
        )

        return

    answer = transaction["data"] or {}

    ctx.check(True, "connect_servo succeeded",
              evidence={"latency_ms": transaction["elapsed_ms"]})

    servo = answer.get("servo") or {}

    ctx.check(bool(servo), "the answer reports a servo",
              evidence={"servo": servo})

    ctx.record("servo_status", **servo)

    ctx.measure(stage="connect", latency_ms=transaction["elapsed_ms"],
                servo=servo.get("label") or servo.get("name") or "",
                connected=servo.get("connected"))


def _diagnostics(ctx):
    ctx.require("servo.diagnostics", "servo.connect")

    ctx.link.request("connect_servo", timeout=ctx.servo._move_timeout())

    transaction = ctx.servo.diagnostics()
    report = transaction["data"] or {}

    ctx.record("diagnostics", **report)

    ctx.check(report.get("ok") is True,
              "every diagnostic step reported ok",
              evidence={"error": report.get("error"),
                        "steps": report.get("steps")})

    ctx.check(report.get("moved") is False,
              "the diagnostic moved nothing",
              evidence={"moved": report.get("moved")})

    production = ctx.profile.production["servo"]

    steps = {step.get("step"): step for step in report.get("steps") or []}

    # All three REQUIRED. "the id matches OR was not reported" is not
    # an identity check, and the mode is hypothesis 1 of H-002 - a
    # servo that will not say which mode it is in has not eliminated
    # it.
    ctx.observed(
        "the servo reports the configured id ({})".format(
            production["servo_id"]),
        (steps.get("id") or {}).get("value"),
        expected=production["servo_id"],
        requirement=Requirement.REQUIRED)

    ctx.observed(
        "the servo's baud rate matches the configured {}".format(
            production["baud"]),
        report.get("baud_matches"), expected=True,
        requirement=Requirement.REQUIRED,
        evidence={"reported": report.get("baud_reported"),
                  "configured": production["baud"]})

    ctx.observed(
        "the servo is in the mode the driver assumes ({})".format(
            production["mode"]),
        report.get("mode_correct"), expected=True,
        requirement=Requirement.REQUIRED,
        evidence={"mode": report.get("mode"),
                  "mode_name": report.get("mode_name"),
                  "expected": report.get("expected_mode")})

    ctx.measure(
        stage="diagnostics", latency_ms=transaction["elapsed_ms"],
        ok=report.get("ok"), mode=report.get("mode"),
        mode_name=report.get("mode_name"),
        torque=report.get("torque_enabled"),
        baud=report.get("baud_reported"),
    )

    if report.get("mode_correct") is False:
        ctx.defect(
            title="the ST3215 is not in STEP mode",
            observed="mode {} ({})".format(
                report.get("mode"), report.get("mode_name")),
            expected="mode {} - STEP servo mode, in which a goal is a "
                     "relative movement".format(production["mode"]),
            reproduction=("run HW-B2-002",),
            suspected_layer="servo configuration",
            evidence={"report": report},
        )

        ctx.note(
            "This is hypothesis 1 of H-002. In position servo mode a "
            "relative goal is interpreted as an absolute one, which "
            "would produce exactly the observed 'the carousel turned "
            "but the encoder reports 2 counts'. Do not run B3 until "
            "this is resolved.")


def _bus_scan(ctx):
    ctx.require("servo.bus_scan")

    transaction = ctx.servo.bus_scan(swap=True)
    report = transaction["data"] or {}

    ctx.record("bus_scan", **report)

    production = ctx.profile.production["servo"]

    found = report.get("found") or report.get("answers") or []

    ctx.check(bool(found),
              "at least one servo answered somewhere on the bus",
              evidence={"found": found,
                        "scanned": report.get("scanned")})

    def matches_configuration(entry):
        if not isinstance(entry, dict):
            return False

        if entry.get("id") not in (None, production["servo_id"]):
            return False

        if entry.get("baud") not in (None, production["baud"]):
            return False

        return not entry.get("swapped", False)

    configured = [e for e in found if matches_configuration(e)]

    swapped_only = [
        e for e in found
        if isinstance(e, dict) and e.get("swapped")
    ] if found else []

    ctx.check(bool(configured),
              "the configured id answers at the configured baud with "
              "the pins as configured",
              evidence={"configured": configured, "found": found})

    for entry in found:
        if isinstance(entry, dict):
            ctx.measure(stage="scan", servo_id=entry.get("id"),
                        baud=entry.get("baud"),
                        swapped=entry.get("swapped"),
                        answered=True)

    if swapped_only and not configured:
        ctx.defect(
            title="the ST3215 answers only with TX and RX exchanged",
            observed="answers were received only in the swapped pin "
                     "order: {}".format(swapped_only),
            expected="the servo answers with TX on GPIO{} and RX on "
                     "GPIO{} as config.py describes".format(
                         production["tx_pin"], production["rx_pin"]),
            reproduction=("run HW-B2-003",),
            suspected_layer="wiring or the ST3215_TX_PIN / ST3215_RX_PIN "
                            "configuration",
            evidence={"scan": report},
        )

        ctx.note(
            "This is the ECHO_ONLY signature already seen on this "
            "bench. It is a WIRING or CONFIGURATION finding, not a "
            "servo fault, and it must be fixed at the bench rather than "
            "by swapping the constants to match a miswired harness "
            "without recording why.")


def _repeated_reads(ctx):
    ctx.require("servo.read_position", "servo.connect")

    reads = ctx.iterations()

    ctx.link.request("connect_servo", timeout=ctx.servo._move_timeout())

    values = []

    for index in range(1, reads + 1):
        position = ctx.servo.position()

        values.append(position)

        ctx.measure(stage="position_read", read=index, position=position)

    known = [v for v in values if v is not None]

    ctx.check(len(known) == len(values),
              "every read returned a position",
              evidence={"reads": len(values), "answered": len(known)})

    distinct = sorted(set(known))

    ctx.check(len(distinct) <= 1,
              "a stationary servo reports the same position every time",
              evidence={"distinct": distinct,
                        "spread": summarize(known)})

    ctx.record("position_reads", values=values, distinct=distinct,
               distribution=summarize(known))

    if len(distinct) > 1:
        ctx.defect(
            title="the reported position changes with nothing commanded",
            observed="{} distinct positions across {} reads: {}".format(
                len(distinct), len(values), distinct[:10]),
            expected="one value; the mechanism is standing still",
            reproduction=("run HW-B2-004",),
            suspected_layer="position read path (stale UART response, "
                            "a reply to a previous command, or "
                            "insufficient settling)",
            evidence={"values": values},
        )

        ctx.note(
            "This is hypothesis 3 or 5 of H-002. A position read that "
            "is not stable at rest cannot be used to measure a "
            "movement, so B3 would be measuring the read rather than "
            "the mechanism.")


def _position_tracks_movement(ctx):
    """
    Move the carousel and prove the reported position followed it.

    THE POINT OF THIS TEST IS THAT IT MOVES. Every read-only check in
    this campaign is compatible with a driver reading the wrong
    register, because reading the wrong register succeeds. Only a
    commanded movement can show that the number production verifies
    against is a position at all.
    """
    ctx.require("servo.test_move", "servo.read_position", "servo.connect")

    ctx.link.request("connect_servo", timeout=ctx.servo._move_timeout())

    tolerance = ctx.profile.production["servo"]["position_tolerance"]
    slot_deg = ctx.profile.production["carousel"]["slot_spacing_deg"]
    per_rev = ctx.profile.counts_per_rev

    legs = []

    for index in range(1, ctx.iterations() + 1):
        for direction, degrees in (("cw", slot_deg), ("ccw", -slot_deg)):
            before = ctx.servo.feedback()
            start = before.get("position_counts")

            transaction = ctx.servo.move_degrees(degrees)
            record = transaction["data"] or {}

            after = ctx.servo.feedback()
            end = after.get("position_counts")

            commanded = record.get("net_counts")

            if start is None or end is None or commanded is None:
                travelled = None
                error = None

            else:
                travelled = centred_error(end - start, per_rev)
                error = travelled - commanded

            leg = {
                "iteration": index,
                "direction": direction,
                "commanded_degrees": degrees,
                "commanded_counts": commanded,
                "position_before": start,
                "position_after": end,
                "travelled": travelled,
                "error": error,
                "following_error_before":
                    before.get("following_error_counts"),
                "following_error_after":
                    after.get("following_error_counts"),
                "trajectory_before": before.get("trajectory_counts"),
                "trajectory_after": after.get("trajectory_counts"),
            }

            legs.append(leg)

            ctx.measure(stage="tracking", **leg)

            ctx.check(
                travelled is not None,
                "leg {} {}: the position was readable before and "
                "after".format(index, direction),
                evidence=leg)

            ctx.check(
                error is not None and abs(error) <= tolerance,
                "leg {} {}: the reported position moved by the "
                "commanded {} counts (measured {}, error {})".format(
                    index, direction, commanded, travelled, error),
                evidence=leg)

    ctx.record("position_tracking", legs=legs,
               errors=summarize([leg["error"] for leg in legs
                                 if leg["error"] is not None]))

    stuck = [leg for leg in legs
             if leg["travelled"] is not None
             and abs(leg["travelled"]) < abs(leg["commanded_counts"] or 0) / 2]

    if stuck:
        ctx.defect(
            title="the reported position does not track the mechanism",
            observed="{} of {} legs moved less than half the commanded "
                     "distance: {}".format(
                         len(stuck), len(legs), stuck[:3]),
            expected="the reported position changes by the commanded "
                     "counts on every leg",
            reproduction=("run HW-B2-013",),
            suspected_layer="the position read path - which register is "
                            "being read, and what it means in this mode",
            evidence={"legs": legs},
        )

        ctx.note(
            "This is H-002 as originally observed. Before concluding "
            "the mechanism slipped, check WHICH register the driver is "
            "reading: in step servo mode register 56 is the following "
            "error, not the position, and a completed movement leaves "
            "it at about 2 both before and after.")


def _diagnostics_bounds_its_claim(ctx):
    """
    Read the diagnostic report and check its own text bounds its claim.

    A test about wording, and deliberately so. `feedback PASS` over a
    servo whose position feedback was being misread is not a wrong
    measurement, it is a true statement that reads as a stronger one -
    and that is what kept H-002 alive on a green board for a day.
    """
    ctx.require("servo.diagnostics", "servo.connect")

    ctx.link.request("connect_servo", timeout=ctx.servo._move_timeout())

    report = ctx.servo.diagnostics()["data"] or {}
    steps = [step.get("step") for step in report.get("steps") or []]

    ctx.record("diagnostic_claim",
               steps=steps,
               proves=report.get("telemetry_read_proves"),
               does_not_prove=report.get("telemetry_read_does_not_prove"),
               semantics=report.get("position_semantics"))

    ctx.check(report.get("moved") is False,
              "the diagnostic moved nothing",
              evidence={"moved": report.get("moved")})

    ctx.check("feedback" not in steps,
              "no step is called `feedback` - the name promised "
              "evidence the step could not produce",
              evidence={"steps": steps})

    ctx.check("telemetry_read" in steps,
              "the telemetry step is named for what it does",
              evidence={"steps": steps})

    ctx.check(bool(report.get("telemetry_read_does_not_prove")),
              "and the report states what it does NOT establish",
              evidence={"text": report.get("telemetry_read_does_not_prove")})

    ctx.check(bool(report.get("position_semantics")),
              "the report names what the position registers mean in "
              "the mode the servo is actually in",
              evidence={"text": report.get("position_semantics")})

    feedback = ctx.servo.feedback()

    for key in ("position_counts", "following_error_counts",
                "trajectory_counts", "angle_deg"):
        ctx.check(key in feedback,
                  "the telemetry carries `{}`".format(key),
                  evidence={"keys": sorted(feedback)})

    if feedback.get("trajectory_counts") is not None:
        ctx.check(
            feedback.get("position_counts")
            == feedback["trajectory_counts"]
            - feedback["following_error_counts"],
            "and the measured position IS the trajectory minus the "
            "following error, so the derivation can be checked rather "
            "than trusted",
            evidence={"feedback": feedback})


def _calibration_matches(ctx):
    ctx.require("servo.calibration", "servo.connect")

    ctx.link.request("connect_servo", timeout=ctx.servo._move_timeout())

    transaction = ctx.servo.calibration()
    report = transaction["data"] or {}

    current = report.get("current") or {}
    production = ctx.profile.production["servo"]

    ctx.record("calibration", **report)

    pairs = (
        ("speed_steps_per_s", "speed"),
        ("acceleration", "acceleration"),
        ("position_tolerance_counts", "position_tolerance"),
        ("settle_ms", "settle_ms"),
        ("poll_interval_ms", "poll_interval_ms"),
        ("move_timeout_ms", "move_timeout_ms"),
    )

    for reported_key, production_key in pairs:
        # REQUIRED: this test exists to prove the deployed firmware
        # IS the firmware in this working tree, and a value the device
        # will not report proves nothing either way.
        ctx.observed(
            "{} is {} on the device, as config.py ships".format(
                reported_key, production[production_key]),
            current.get(reported_key),
            expected=production[production_key],
            requirement=Requirement.REQUIRED)

    ctx.check(report.get("editable") is not True,
              "the calibration reports itself non-editable, so a "
              "tolerance cannot be widened from the operator client",
              evidence={"editable": report.get("editable")})

    ctx.note(
        "ST3215_POSITION_TOLERANCE is {} counts and is the one shipped "
        "constant that is a guess. HW-B4-003 measures the real closing "
        "error; the tolerance must be set from that distribution and "
        "from nothing else.".format(production["position_tolerance"]))


def _raw_packet(ctx):
    """
    The bytes, not the driver's reading of them.

    Goes through the diagnostic agent, which is the only thing that can
    show them. The whitelist and the length bound are checked on both
    sides; this asks for the present-position register, which is the one
    H-002 is about.
    """
    ctx.require("diagnostic.servo_raw")

    identity = ctx.diagnostic.identify()

    ctx.record("agent", **identity)

    transaction = ctx.diagnostic.servo_raw_read(register=56, length=2)

    answer = transaction["data"] or {}

    ctx.record("raw_read", **answer)

    raw = answer.get("bytes")

    ctx.require_observation("the raw reply bytes", raw,
                            evidence={"answer": answer})

    interpretations = ctx.diagnostic.interpret_bytes(raw or [])

    ctx.record("interpretations", **(interpretations or {}))

    parsed = answer.get("parsed_little_endian")

    ctx.observed(
        "the agent's parsed value matches the little-endian reading of "
        "the bytes it returned",
        parsed,
        expected=(interpretations or {}).get("little_endian"),
        requirement=Requirement.REQUIRED,
        evidence=interpretations)

    position = ctx.servo.position()

    ctx.observed(
        "the production driver's position agrees with the raw register",
        position, requirement=Requirement.OPTIONAL_DIAGNOSTIC,
        matches=lambda v: v == parsed,
        evidence={"driver": position, "raw_parsed": parsed})

    ctx.measure(stage="raw_packet", register=56,
                register_name=answer.get("register_name"),
                hex=(interpretations or {}).get("hex"),
                little_endian=(interpretations or {}).get(
                    "little_endian"),
                big_endian=(interpretations or {}).get("big_endian"),
                driver_position=position,
                elapsed_ms=answer.get("elapsed_ms"))

    if position is not None and parsed is not None and position != parsed:
        ctx.note(
            "The production driver and the raw register disagree "
            "({} vs {}). That is hypothesis 8 of H-002 becoming "
            "concrete - compare the byte orders above.".format(
                position, parsed))


def _refusals(ctx):
    ctx.require("servo.test_move", "servo.connect")

    ctx.link.request("connect_servo", timeout=ctx.servo._move_timeout())

    before = ctx.carousel.status()

    ctx.record("carousel_before", **before)

    attempts = (
        ("no confirmation",
         {"kind": "slot_forward", "repeat": 1, "confirm": False},
         "CONFIRMATION_REQUIRED"),
        ("unknown kind",
         {"kind": "not_a_real_kind", "repeat": 1, "confirm": True},
         "BAD_REQUEST"),
        ("repeat out of range",
         {"kind": "slot_forward", "repeat": 99, "confirm": True},
         "BAD_REQUEST"),
    )

    for label, payload, expected_code in attempts:
        refused = False
        code = None

        try:
            ctx.link.request("servo_test_move",
                             timeout=ctx.servo._move_timeout(), **payload)

        except Exception as error:
            refused = True
            code = getattr(error, "code", None)

        ctx.check(refused, "{} is refused".format(label),
                  evidence={"payload": payload, "code": code})

        ctx.check(code == expected_code or code is None,
                  "{} is refused with {}".format(label, expected_code),
                  evidence={"code": code, "expected": expected_code})

        ctx.measure(stage="refusal", attempt=label, refused=refused,
                    code=code or "", expected=expected_code)

    after = ctx.carousel.status()

    ctx.record("carousel_after", **after)

    ctx.check(before.get("current_scan_slot") == after.get(
                  "current_scan_slot"),
              "the carousel's reported slot did not change",
              evidence={"before": before.get("current_scan_slot"),
                        "after": after.get("current_scan_slot")})

    ctx.confirm_observation(
        "Did the carousel stay completely still during those three "
        "refused commands")


def _servo_telemetry(ctx):
    ctx.require("diagnostic.servo_feedback")

    identity = ctx.diagnostic.identify()

    ctx.record("agent", **identity)

    transaction = ctx.diagnostic.servo_feedback()

    answer = transaction["data"] or {}

    readings = answer.get("readings") or {}
    errors = answer.get("errors") or {}

    ctx.record("telemetry", readings=readings, errors=errors,
               complete=answer.get("complete"))

    for name in ("PRESENT_POSITION", "PRESENT_VOLTAGE",
                 "PRESENT_TEMPERATURE", "MOVING"):
        ctx.observed(
            "the servo reports {}".format(name), readings.get(name),
            requirement=Requirement.REQUIRED,
            matches=lambda v: isinstance(v, (int, float)),
            evidence={"errors": errors.get(name)})

    for name in ("PRESENT_SPEED", "PRESENT_LOAD", "PRESENT_CURRENT"):
        ctx.observed(
            "the servo reports {}".format(name), readings.get(name),
            requirement=Requirement.OPTIONAL_DIAGNOSTIC)

    moving = readings.get("MOVING")

    if moving is not None:
        ctx.check(not moving,
                  "the moving bit reads not-moving on a stationary "
                  "servo",
                  evidence={"moving": moving})

    for name, value in sorted(readings.items()):
        ctx.measure(stage="telemetry", register=name, value=value)

    ctx.characterize(
        "servo telemetry at rest recorded: {}. No datasheet limits for "
        "this servo are recorded in this repository, so these values "
        "are the baseline a later loaded or warm measurement is "
        "compared against, not a judgement.".format(
            ", ".join("{}={}".format(k, v)
                      for k, v in sorted(readings.items())[:6])))


def _torque_and_stop(ctx):
    ctx.require("servo.torque", "servo.stop", "servo.diagnostics")

    ctx.link.request("connect_servo", timeout=ctx.servo._move_timeout())

    safe = ctx.ask(
        "Is the carousel EMPTY and in a position where releasing torque "
        "cannot let it fall or swing")

    if not safe:
        ctx.skip(
            "the operator will not confirm the plate is safe to release "
            "- this test never drops torque on a mechanism that could "
            "move under its own weight")

    def read_torque():
        report = ctx.servo.diagnostics()["data"] or {}

        return report.get("torque_enabled")

    before = read_torque()

    ctx.record("torque_before", torque=before)

    ctx.servo.torque(enable=False)

    released = read_torque()

    ctx.observed("torque reads back disabled", released,
                 expected=False, requirement=Requirement.REQUIRED)

    ctx.confirm_observation(
        "With torque released, is the output shaft free to turn by hand")

    ctx.servo.torque(enable=True)

    restored = read_torque()

    ctx.observed("torque reads back enabled", restored,
                 expected=True, requirement=Requirement.REQUIRED)

    ctx.confirm_observation(
        "With torque re-enabled, is the output shaft firm again")

    stop = ctx.servo.stop()

    ctx.check(stop["elapsed_ms"] < 5000,
              "the stop returned promptly ({} ms)".format(
                  stop["elapsed_ms"]),
              evidence={"elapsed_ms": stop["elapsed_ms"]})

    final = read_torque()

    ctx.check(bool(final),
              "torque is ENABLED at the end of the test - a carousel "
              "left without torque can be turned by gravity",
              evidence={"torque": final})

    ctx.measure(stage="torque", before=before, released=released,
                restored=restored, final=final,
                stop_ms=stop["elapsed_ms"])


def _mode_persistence(ctx):
    ctx.require("servo.diagnostics", "servo.connect", "link.hard_reset")

    import time

    expected = ctx.profile.production["servo"]["mode"]

    def read_mode():
        ctx.link.request("connect_servo",
                         timeout=ctx.servo._move_timeout())

        report = ctx.servo.diagnostics()["data"] or {}

        return report.get("mode"), report

    first, report = read_mode()

    ctx.record("mode_initial", mode=first, expected=expected)

    ctx.observed("the servo reports its mode", first,
                 expected=expected, requirement=Requirement.REQUIRED)

    link = ctx.link.require_link("reset the board")

    link.hard_reset()

    started = time.perf_counter()

    link.wait_online()

    after_reset_ms = round((time.perf_counter() - started) * 1000.0, 3)

    second, _ = read_mode()

    ctx.record("mode_after_esp32_reset", mode=second,
               online_ms=after_reset_ms)

    ctx.observed("the mode survives an ESP32 reset", second,
                 expected=expected, requirement=Requirement.REQUIRED)

    ctx.link.close(reason="servo power cycle")

    ctx.instruct(
        "Power the SERVO SUPPLY down, wait five seconds, and power it "
        "back up. Leave the ESP32 powered.")

    third, _ = read_mode()

    ctx.record("mode_after_servo_power_cycle", mode=third)

    ctx.observed("the mode survives a servo power cycle", third,
                 expected=expected, requirement=Requirement.REQUIRED)

    ctx.measure(stage="mode_persistence", initial=first,
                after_esp32_reset=second,
                after_servo_power_cycle=third, expected=expected,
                online_ms=after_reset_ms)

    if third is not None and third != expected:
        ctx.defect(
            title="the ST3215 does not keep its mode across a power "
                  "cycle",
            observed="mode {} after the servo supply was cycled; {} "
                     "before".format(third, first),
            expected="mode {} - STEP servo mode - at all times".format(
                expected),
            reproduction=("run HW-B2-010",),
            suspected_layer="servo non-volatile configuration",
            evidence={"initial": first, "after_reset": second,
                      "after_power_cycle": third},
        )

        ctx.note(
            "This would reproduce H-002 intermittently and look like a "
            "software fault every time: in position mode a relative "
            "goal is interpreted as an absolute one.")


def _agent_identity(ctx):
    ctx.require("diagnostic.agent")

    from ..adapters.diagnostic import (AGENT_PROTOCOL,
                                       AGENT_PROTOCOL_VERSION,
                                       MAX_READ_LENGTH,
                                       READABLE_REGISTERS)

    identity = ctx.diagnostic.identify()

    ctx.record("agent_identity", **identity)

    ctx.observed("the agent names the diagnostic protocol",
                 identity.get("protocol"), expected=AGENT_PROTOCOL,
                 requirement=Requirement.REQUIRED)

    ctx.observed("the agent's protocol version matches this adapter",
                 identity.get("protocol_version"),
                 expected=AGENT_PROTOCOL_VERSION,
                 requirement=Requirement.REQUIRED)

    ctx.observed("the agent declares that it cannot move anything",
                 identity.get("moves"), expected=False,
                 requirement=Requirement.REQUIRED)

    declared = identity.get("readable_registers")

    ctx.observed("the agent declares its register whitelist", declared,
                 requirement=Requirement.REQUIRED,
                 matches=lambda v: sorted(v) == sorted(
                     READABLE_REGISTERS))

    ctx.observed("the agent declares its read length bound",
                 identity.get("max_read_length"),
                 expected=MAX_READ_LENGTH,
                 requirement=Requirement.REQUIRED)

    writes = identity.get("writes") or []

    ctx.check(set(writes) <= {"diag_lamps_off"},
              "the agent's only write is diag_lamps_off",
              evidence={"writes": writes})

    # A register deliberately NOT on the whitelist. 0x28 (40) IS on it -
    # pick something in the memory table that is not.
    refused = False
    code = None

    try:
        ctx.link.request("diag_servo_raw_read", register=200, length=2)

    except Exception as error:
        refused = True
        code = getattr(error, "code", None)

    ctx.check(refused,
              "a register outside the whitelist is refused",
              evidence={"register": 200, "code": code})

    long_refused = False
    long_code = None

    try:
        ctx.link.request("diag_servo_raw_read", register=56,
                         length=MAX_READ_LENGTH + 4)

    except Exception as error:
        long_refused = True
        long_code = getattr(error, "code", None)

    ctx.check(long_refused,
              "a read longer than {} bytes is refused".format(
                  MAX_READ_LENGTH),
              evidence={"length": MAX_READ_LENGTH + 4,
                        "code": long_code})

    ctx.measure(stage="agent", protocol=identity.get("protocol"),
                version=identity.get("protocol_version"),
                build=identity.get("build"),
                moves=identity.get("moves"),
                whitelist=len(declared or []),
                bad_register_refused=refused,
                long_read_refused=long_refused)


def _firmware_restored(ctx):
    ctx.require("link.ping")

    from ..adapters.diagnostic import AGENT_PROTOCOL

    declared = ctx.profile.diagnostic_firmware()

    ctx.record("profile_diagnostic_firmware", **declared)

    ctx.confirm_observation(
        "Has diagnostic_agent.py been removed from the board")

    transaction = ctx.link.request("ping", retries=2)

    answer = transaction["data"] or {}

    ctx.record("ping_after_restore", **answer)

    expected_name = ctx.profile.production["firmware_name"]

    ctx.observed("the production firmware identifies itself",
                 answer.get("firmware") or answer.get("name"),
                 expected=expected_name,
                 requirement=Requirement.REQUIRED)

    ctx.observed("the production protocol version is back",
                 answer.get("protocol_version"),
                 expected=ctx.profile.production["protocol_version"],
                 requirement=Requirement.REQUIRED)

    ctx.check(answer.get("protocol") != AGENT_PROTOCOL,
              "the diagnostic protocol no longer answers",
              evidence={"protocol": answer.get("protocol")})

    ctx.check(not declared.get("deployed"),
              "the bench profile no longer claims a diagnostic "
              "deployment",
              evidence={"diagnostic_firmware": declared})

    ctx.measure(stage="restored",
                firmware=answer.get("firmware") or "",
                version=answer.get("version") or "",
                protocol_version=answer.get("protocol_version"),
                profile_claims_deployed=bool(declared.get("deployed")),
                production_sha256=declared.get(
                    "production_firmware_sha256") or "")

    ctx.note(
        "Any prerequisite PASS earned while the diagnostic agent was "
        "deployed is void by fingerprint: the run fingerprint includes "
        "the diagnostic firmware, so the layer gate will not accept "
        "those results for a production run. Re-run the campaigns that "
        "matter on the restored firmware.")
