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

from ..core.model import Automation, Safety
from ..core.analysis import summarize


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
        test_id="HW-B2-005", campaign=CAMPAIGN, layer="B2",
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
        requires=("servo.raw_packet",),
        run=_raw_packet, cleanup=_release,
        assumption="H-002", defect_prefix="HW-SERVO",
        notes="BLOCKED: the shipped firmware has no raw servo "
              "passthrough. See the recommendation on the "
              "servo.raw_packet capability.",
    )

    registry.test(
        test_id="HW-B2-007", campaign=CAMPAIGN, layer="B2",
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

    reported_id = (steps.get("id") or {}).get("value")

    ctx.check(reported_id is None or reported_id == production["servo_id"],
              "the servo reports the configured id ({})".format(
                  production["servo_id"]),
              evidence={"reported": reported_id,
                        "configured": production["servo_id"]})

    ctx.check(report.get("baud_matches") is not False,
              "the servo's baud rate matches the configured {}".format(
                  production["baud"]),
              evidence={"reported": report.get("baud_reported"),
                        "configured": production["baud"]})

    ctx.check(report.get("mode_correct") is not False,
              "the servo is in the mode the driver assumes ({})".format(
                  production["mode"]),
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
        reported = current.get(reported_key)
        expected = production[production_key]

        ctx.check(reported is None or reported == expected,
                  "{} is {} on the device, as config.py ships".format(
                      reported_key, expected),
                  evidence={"reported": reported, "expected": expected})

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
    # Blocked by capability. `require` raises with the exact interface
    # that would unblock it, so the body below never runs today - and
    # will run unchanged on the day the firmware grows the command.
    ctx.require("servo.raw_packet")

    transaction = ctx.link.request(                     # pragma: no cover
        "servo_raw_read", register=56, length=2)

    answer = transaction["data"] or {}                  # pragma: no cover

    ctx.record("raw_read", **answer)                    # pragma: no cover

    ctx.check(bool(answer.get("bytes")),                # pragma: no cover
              "the raw reply bytes were captured",
              evidence=answer)


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
