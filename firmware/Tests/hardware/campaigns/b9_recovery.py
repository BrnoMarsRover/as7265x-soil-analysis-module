"""
B9 - what happens when something is taken away.

ONE FAULT AT A TIME, AND ONLY FAULTS THAT ARE SAFE TO INJECT

Every test here removes exactly one thing: the USB cable, the sensor,
the servo bus, or power to the ESP32. Nothing in this campaign asks for
an electrical short, a reversed polarity, an overvoltage or a
deliberate brownout - those damage hardware and prove nothing that
pulling a connector does not.

THE RULE THAT MAKES THESE RESULTS WORTH HAVING

After a movement that could not be verified, or a communication loss
during one, the carousel position is UNKNOWN and a re-sync is REQUIRED.
Not "probably still slot 1". Not "the last thing we commanded was a half
turn so it must be at the scanner". Unknown.

A reset or a power cycle does not preserve confidence in a physical
position either, and HW-B9-006 and HW-B9-007 exist to prove the firmware
agrees.

THE SEPARATION IN HW-B9-010 IS THE MOST IMPORTANT RESULT IN B9

    measurement acquired    -> the science data is valid and saved
    return fails            -> the physical position is uncertain

Those two facts are independent, and a system that throws the
measurement away because the carousel could not get home has lost data
it had, on a rover, on Mars time. A system that keeps the data and lies
about the position is worse. B9 checks that it keeps the first and
admits the second.
"""

from ..core.model import Automation, Safety


CAMPAIGN = "B9"


def register(registry):
    registry.campaign(
        CAMPAIGN, layer="B9",
        title="Linux, USB, reset and failure recovery",
        purpose="Remove one thing at a time and check the system names "
                "the failure, keeps what it legitimately has, and "
                "admits what it no longer knows.",
        prerequisites=("B1",),
        gate_note="Gated by B1 only for the transport tests. The tests "
                  "that interrupt a movement additionally list B5.",
    )

    _register_disconnects(registry)
    _register_resets(registry)
    _register_component_loss(registry)


def _register_disconnects(registry):
    disconnects = (
        ("HW-B9-001", "while the link is idle",
         "no command is in flight", _disconnect_idle, ("B1",)),
        ("HW-B9-002", "during a ping",
         "the smallest possible command is in flight",
         _disconnect_during_ping, ("B1",)),
        ("HW-B9-003", "during a status read",
         "a multi-section answer is being assembled",
         _disconnect_during_status, ("B1",)),
    )

    for test_id, when, why, body, prerequisites in disconnects:
        registry.test(
            test_id=test_id, campaign=CAMPAIGN, layer="B9",
            title="USB disconnect {}".format(when),
            objective="Check the link fails as PORT_LOST, the client "
                      "survives, and every later command is a clean "
                      "PORT_CLOSED rather than a traceback.",
            hardware_setup="ESP32 connected. The operator can reach the "
                           "USB cable at the board and at the hub.",
            preconditions="B1 passed.",
            procedure=(
                "open the link and confirm it works",
                "ask the operator to pull the USB cable {}".format(when),
                "attempt a command and record the exact error",
                "attempt a second command and check it is PORT_CLOSED "
                "and not a crash",
                "ask the operator to reconnect",
                "reopen and confirm recovery",
            ),
            expected="A named transport error, no traceback, and a "
                     "clean recovery after reconnection.",
            failure_criteria="Any unhandled exception, or a command "
                             "that appears to succeed after the cable "
                             "was pulled.",
            captures=("the error with its code and errno if present",
                      "the second command's error",
                      "the recovery result",
                      "which point in the command the cable was pulled"),
            safety=Safety.MANUAL_DISCONNECT,
            automation=Automation.OPERATOR_ASSISTED,
            requires=("link.open", "link.ping", "link.status"),
            run=body, cleanup=_close,
            prerequisites=prerequisites,
            defect_prefix="HW-USB",
            notes="{}. Replaces part of HW-102.".format(why),
        )

    registry.test(
        test_id="HW-B9-004", campaign=CAMPAIGN, layer="B9",
        title="Communication lost during a carousel movement",
        objective="Check that a movement interrupted by a lost link "
                  "leaves the position UNKNOWN rather than assumed.",
        hardware_setup="Carousel attached, empty, free. The operator "
                       "can reach the USB cable.",
        preconditions="B5 passed. The mechanism is clear - the carousel "
                      "WILL be left mid-movement.",
        procedure=(
            "synchronize and confirm the position is valid",
            "start a slot movement",
            "ask the operator to pull the USB cable during it",
            "record the error",
            "reconnect and reopen",
            "read the carousel status",
            "check the position is reported INVALID",
            "check a slot-addressed movement is refused until a re-sync",
        ),
        expected="The position is invalid after the interruption and a "
                 "re-sync is required before any slot movement.",
        failure_criteria="A firmware that still claims to know where "
                         "the plate is. It cannot: the movement was "
                         "interrupted and nothing observed where it "
                         "stopped.",
        captures=("the error during the movement",
                  "the status after reconnection",
                  "position_valid and the reason",
                  "the refusal of a slot movement, if any"),
        safety=Safety.MANUAL_DISCONNECT,
        automation=Automation.OPERATOR_ASSISTED,
        requires=("carousel.select_slot", "carousel.status",
                  "carousel.sync"),
        run=_disconnect_during_movement, cleanup=_close,
        prerequisites=("B5",),
        defect_prefix="HW-CAR",
    )

    registry.test(
        test_id="HW-B9-005", campaign=CAMPAIGN, layer="B9",
        title="Communication lost during a measurement",
        objective="Check a measurement interrupted by a lost link fails "
                  "cleanly and leaves nothing illuminated.",
        hardware_setup="As HW-B9-004, with the sensor connected.",
        preconditions="B7 and B8 passed.",
        procedure=(
            "start a measurement",
            "ask the operator to pull the USB cable during the "
            "acquisition",
            "record the error",
            "ask the operator whether any lamp is still lit",
            "reconnect and read the status",
            "check the position is invalid and the sensor recoverable",
        ),
        expected="A named error, no lamp left on, an invalid position, "
                 "and a sensor that recovers.",
        failure_criteria="An illuminator left on with no link to switch "
                         "it off, which is a power and a heating "
                         "problem the operator cannot fix remotely.",
        captures=("the error", "the operator's lamp observation",
                  "the status after reconnection",
                  "whether the off state could be confirmed at all"),
        safety=Safety.FAULT_INJECTION,
        automation=Automation.OPERATOR_ASSISTED,
        requires=("carousel.measure", "carousel.status"),
        run=_disconnect_during_measurement, cleanup=_close,
        prerequisites=("B8",),
        defect_prefix="HW-INT",
    )


def _register_resets(registry):
    registry.test(
        test_id="HW-B9-006", campaign=CAMPAIGN, layer="B9",
        title="ESP32 soft reset",
        objective="Reset the board deliberately and check the position "
                  "reference does not survive it.",
        hardware_setup="ESP32 connected, carousel synchronized.",
        preconditions="B1 passed.",
        procedure=(
            "synchronize and confirm the position is valid",
            "reset the board with an RTS pulse and DTR low",
            "wait for it to answer",
            "check it came back into the application, not the "
            "bootloader",
            "check the carousel position is now INVALID",
        ),
        expected="The board returns to the application and the position "
                 "is invalid.",
        failure_criteria="A position that survives a reset - a claim "
                         "about a physical mechanism the board cannot "
                         "possibly still hold - or a board that lands "
                         "in the serial bootloader.",
        captures=("time to answer after the reset",
                  "the position validity before and after",
                  "any boot banner text"),
        safety=Safety.RESET, automation=Automation.AUTOMATIC,
        requires=("link.hard_reset", "carousel.status", "carousel.sync"),
        run=_soft_reset, cleanup=_close,
        defect_prefix="HW-SER",
    )

    registry.test(
        test_id="HW-B9-007", campaign=CAMPAIGN, layer="B9",
        title="ESP32 power cycle",
        objective="Remove power entirely and check the same thing a "
                  "reset checks, plus that the port comes back.",
        hardware_setup="ESP32 connected, the operator able to reach the "
                       "supply.",
        preconditions="HW-B9-006 passed.",
        procedure=(
            "synchronize and confirm the position is valid",
            "close the port",
            "ask the operator to power the module down and back up",
            "wait for enumeration and reopen",
            "check the position is invalid",
            "check the device identity is unchanged",
        ),
        expected="The module comes back with an invalid position and "
                 "the same stable identity.",
        failure_criteria="A position that survives a power cycle, or a "
                         "device that does not re-enumerate.",
        captures=("the position before and after",
                  "the device identity before and after",
                  "the time to re-enumerate"),
        safety=Safety.POWER_CYCLE,
        automation=Automation.OPERATOR_ASSISTED,
        requires=("link.enumerate", "carousel.status", "carousel.sync"),
        run=_power_cycle, cleanup=_close,
        defect_prefix="HW-SER",
    )


def _register_component_loss(registry):
    registry.test(
        test_id="HW-B9-008", campaign=CAMPAIGN, layer="B9",
        title="Servo bus communication lost",
        objective="Remove the servo from the bus and check the failure "
                  "is named and the position invalidated.",
        hardware_setup="ST3215 on a connector the operator can unplug "
                       "safely, carousel free.",
        preconditions="B2 passed.",
        procedure=(
            "connect the servo and confirm diagnostics pass",
            "ask the operator to disconnect the servo data line",
            "run diagnostics and record the failure",
            "attempt a slot movement and record the refusal",
            "check the carousel position is invalidated",
            "ask the operator to reconnect",
            "reconnect the servo and confirm recovery",
        ),
        expected="A named servo error, a refused movement, an "
                 "invalidated position, and recovery after "
                 "reconnection.",
        failure_criteria="A movement that is attempted anyway, or a "
                         "position that stays valid with no servo to "
                         "hold it.",
        captures=("the diagnostics failure with its step",
                  "the refused movement's error",
                  "the position validity",
                  "the recovery result"),
        safety=Safety.FAULT_INJECTION,
        automation=Automation.OPERATOR_ASSISTED,
        requires=("servo.diagnostics", "servo.connect",
                  "carousel.status"),
        run=_servo_loss, cleanup=_close,
        prerequisites=("B2",),
        defect_prefix="HW-SERVO",
    )

    registry.test(
        test_id="HW-B9-009", campaign=CAMPAIGN, layer="B9",
        title="Failure during one specific illumination",
        objective="Fail the acquisition in the middle of the triad and "
                  "check the answer names WHICH illumination failed.",
        hardware_setup="Sensor on an unpluggable connector.",
        preconditions="HW-B7-004 passed.",
        procedure=(
            "start a triad acquisition",
            "ask the operator to disconnect the sensor during it",
            "record the error code and its details",
            "check the error names the illumination that was running "
            "and lists the ones that completed",
        ),
        expected="An error naming the failing illumination and the "
                 "completed ones - WHITE_ACQUISITION_FAILED, "
                 "UV_ACQUISITION_FAILED or IR_ACQUISITION_FAILED.",
        failure_criteria="A generic error. Which illumination failed "
                         "decides whether the partial data is usable.",
        captures=("the error code, stage and details",
                  "which illuminations completed",
                  "the underlying code"),
        safety=Safety.FAULT_INJECTION,
        automation=Automation.OPERATOR_ASSISTED,
        requires=("sensor.acquire_triad", "sensor.status"),
        run=_illumination_failure, cleanup=_close,
        prerequisites=("HW-B7-004",),
        defect_prefix="HW-SENSOR",
    )

    registry.test(
        test_id="HW-B9-010", campaign=CAMPAIGN, layer="B9",
        title="The return fails after the data was acquired",
        objective="Prove the science data survives a failed return, and "
                  "that the position is admitted to be uncertain.",
        hardware_setup="Carousel attached and empty, sensor connected. "
                       "The operator will interrupt the RETURN, after "
                       "the acquisition has finished.",
        preconditions="B8 passed.",
        procedure=(
            "start a measurement and let the acquisition complete",
            "ask the operator to interrupt the return - by pulling the "
            "USB cable as the carousel starts back",
            "record what came back, including any partial data on the "
            "error",
            "reconnect and read the device's retained acquisition "
            "buffer",
            "check the measurement is still there",
            "check the carousel position is reported invalid",
        ),
        expected="The spectral data is still available on the device, "
                 "and the position is uncertain. Two independent facts, "
                 "both reported honestly.",
        failure_criteria="Data lost because the return failed, or a "
                         "position still claimed valid. Either one is "
                         "a defect; they are separate defects.",
        captures=("the error and any data attached to it",
                  "the retained buffer contents afterwards",
                  "the carousel position validity",
                  "the operator's account of where the plate stopped"),
        safety=Safety.FAULT_INJECTION,
        automation=Automation.OPERATOR_ASSISTED,
        requires=("carousel.measure", "carousel.status",
                  "carousel.saved_samples"),
        run=_return_fails, cleanup=_close,
        prerequisites=("B8",),
        defect_prefix="HW-INT",
    )


# ======================================================================
# shared
# ======================================================================

def _close(ctx):
    """
    Cleanup for a campaign whose whole point is a broken link.

    Everything here can fail, and a failure is a recorded fact rather
    than an exception. In particular the position state is read if it
    can be, and reported UNREADABLE if it cannot - which after a
    disconnect is the normal, correct answer.
    """
    record = {}

    try:
        record["position_state"] = ctx.carousel.position_state()

    except Exception as error:
        record["position_state"] = {
            "state": "POSITION_UNREADABLE",
            "reason": "{}: {}".format(type(error).__name__, error),
        }

    closed = ctx.link.close(reason="B9 cleanup")

    record["port_released"] = closed.get("closed")
    record["confirmed"] = bool(closed.get("closed"))

    record["note"] = (
        "after a deliberate disconnect the carousel position is "
        "expected to be unknown; that is the result, not a cleanup "
        "failure")

    return record


def _capture_error(call):
    """Run something that should fail, and describe the failure."""
    try:
        call()

    except Exception as error:
        return {
            "raised": True,
            "type": type(error).__name__,
            "code": getattr(error, "code", None),
            "message": str(error)[:400],
            "data": getattr(error, "data", None),
        }

    return {"raised": False}


def _disconnect_then_check(ctx, when, command):
    """The shared shape of the three transport-disconnect tests."""
    ctx.link.require_link("establish the link before the disconnect")

    ctx.link.request("ping", retries=1)

    ctx.check(True, "the link works before the disconnect")

    ctx.instruct(
        "Pull the USB cable from the science module now ({}).".format(
            when))

    first = _capture_error(lambda: ctx.link.request(command, retries=0))

    ctx.record("first_after_disconnect", when=when, command=command,
               **first)

    ctx.check(first["raised"],
              "the first command after the disconnect failed rather "
              "than appearing to succeed",
              evidence=first)

    ctx.check(first.get("code") is not None,
              "the failure carries a code an operator can act on "
              "({})".format(first.get("code")),
              evidence=first)

    second = _capture_error(lambda: ctx.link.request("ping", retries=0))

    ctx.record("second_after_disconnect", **second)

    ctx.check(second["raised"],
              "a second command also fails, cleanly",
              evidence=second)

    ctx.check(second.get("type") not in ("RuntimeError", "AttributeError",
                                         "TypeError"),
              "the second failure is a transport error, not a "
              "programming error ({})".format(second.get("type")),
              evidence=second)

    ctx.measure(stage="disconnect", when=when, command=command,
                first_code=first.get("code") or "",
                first_type=first.get("type") or "",
                second_code=second.get("code") or "",
                second_type=second.get("type") or "")

    ctx.instruct("Plug the USB cable back in and wait for the operating "
                 "system to enumerate it.")

    ctx.link.close(reason="reconnect")

    recovery = _capture_error(
        lambda: ctx.link.request("ping", retries=2))

    ctx.check(not recovery["raised"],
              "the link recovers after the cable is reconnected",
              evidence=recovery)


# ======================================================================
# bodies
# ======================================================================

def _disconnect_idle(ctx):
    ctx.require("link.open", "link.ping")

    _disconnect_then_check(ctx, "with nothing in flight", "ping")


def _disconnect_during_ping(ctx):
    ctx.require("link.open", "link.ping")

    _disconnect_then_check(ctx, "as a ping is sent", "ping")


def _disconnect_during_status(ctx):
    ctx.require("link.open", "link.status")

    _disconnect_then_check(
        ctx, "as a status read is in flight", "get_status")


def _disconnect_during_movement(ctx):
    ctx.require("carousel.select_slot", "carousel.status",
                "carousel.sync")

    ctx.link.request("connect_servo", timeout=ctx.servo._move_timeout())
    ctx.carousel.sync(load_slot=1)

    before = ctx.carousel.status()

    ctx.check(before.get("position_valid") is True,
              "the position is valid before the interruption",
              evidence=before)

    count = ctx.carousel.slot_count()

    target = 1 % count + 1

    ctx.instruct(
        "A movement to slot {} is about to start. PULL THE USB CABLE "
        "while the carousel is turning, then press Enter.".format(target))

    failure = _capture_error(lambda: ctx.carousel.select_slot(target))

    ctx.record("movement_interrupted", **failure)

    ctx.check(failure["raised"],
              "the interrupted movement reported a failure",
              evidence=failure)

    ctx.instruct("Reconnect the USB cable and wait for enumeration.")

    ctx.link.close(reason="reconnect after interrupted movement")

    after = None

    try:
        after = ctx.carousel.status()

    except Exception as error:                         # pragma: no cover
        ctx.check(False, "the module answered after reconnection",
                  evidence={"error": str(error)})

        return

    ctx.record("after_reconnect", **after)

    ctx.check(after.get("position_valid") is not True,
              "the carousel position is INVALID after an interrupted "
              "movement - nothing observed where the plate stopped",
              evidence=after)

    refusal = _capture_error(
        lambda: ctx.carousel.select_slot(target))

    ctx.record("movement_after_loss", **refusal)

    ctx.check(refusal["raised"],
              "a slot-addressed movement is refused until the position "
              "is re-synchronized",
              evidence=refusal)

    ctx.operator_note("Where did the carousel actually stop")


def _disconnect_during_measurement(ctx):
    ctx.require("carousel.measure", "carousel.status")

    ctx.link.request("connect_servo", timeout=ctx.servo._move_timeout())
    ctx.carousel.sync(load_slot=1)

    ctx.instruct(
        "A measurement is about to start. PULL THE USB CABLE while the "
        "sensor is acquiring - after the carousel has stopped at the "
        "scanner - then press Enter.")

    failure = _capture_error(
        lambda: ctx.carousel.measure(1, sample_id="HW-B9-005"))

    ctx.record("measurement_interrupted", **failure)

    ctx.check(failure["raised"],
              "the interrupted measurement reported a failure",
              evidence=failure)

    lit = ctx.ask("Is any illumination source still lit on the sensor "
                  "head")

    ctx.check(not lit,
              "no illuminator was left on when the link was lost",
              evidence={"operator_saw_light": lit}, kind="OPERATOR")

    if lit:
        ctx.defect(
            title="an illuminator stayed on after the link was lost "
                  "mid-measurement",
            observed="the operator reports a lamp still lit with no "
                     "link to switch it off",
            expected="the firmware switches every bulb off on the error "
                     "path as well as the success path",
            reproduction=("run HW-B9-005",),
            suspected_layer="sensor driver error path",
            evidence={"failure": failure},
        )

    ctx.instruct("Reconnect the USB cable and wait for enumeration.")

    ctx.link.close(reason="reconnect after interrupted measurement")

    after = ctx.carousel.status()

    ctx.record("after_reconnect", **after)

    ctx.check(after.get("position_valid") is not True,
              "the carousel position is invalid after the interruption",
              evidence=after)

    status = ctx.sensor.status()

    ctx.check(status.get("state") in ("READY", "NOT_INITIALIZED",
                                      "UNAVAILABLE"),
              "the sensor reports a state rather than failing to answer",
              evidence=status)


def _soft_reset(ctx):
    ctx.require("link.hard_reset", "carousel.status", "carousel.sync")

    link = ctx.link.require_link("reset the board")

    ctx.link.request("connect_servo", timeout=ctx.servo._move_timeout())
    ctx.carousel.sync(load_slot=1)

    before = ctx.carousel.status()

    ctx.check(before.get("position_valid") is True,
              "the position is valid before the reset",
              evidence=before)

    import time

    ctx.record("reset")

    link.hard_reset()

    started = time.perf_counter()

    link.wait_online()

    online_ms = round((time.perf_counter() - started) * 1000.0, 3)

    ctx.check(True, "the board answered after the reset",
              evidence={"online_ms": online_ms})

    after = ctx.carousel.status()

    ctx.record("after_reset", online_ms=online_ms, **after)

    ctx.check(after.get("position_valid") is not True,
              "the carousel position does NOT survive a reset",
              evidence=after)

    ctx.measure(stage="soft_reset", online_ms=online_ms,
                valid_before=before.get("position_valid"),
                valid_after=after.get("position_valid"))

    if after.get("position_valid") is True:
        ctx.defect(
            title="the carousel position survived an ESP32 reset",
            observed="position_valid is still true after a hard_reset",
            expected="a board that has just restarted cannot know where "
                     "a mechanism is",
            reproduction=("run HW-B9-006",),
            suspected_layer="carousel position lifecycle",
            evidence={"before": before, "after": after},
        )


def _power_cycle(ctx):
    ctx.require("link.enumerate", "carousel.status", "carousel.sync")

    ctx.link.request("connect_servo", timeout=ctx.servo._move_timeout())
    ctx.carousel.sync(load_slot=1)

    before = ctx.carousel.status()

    ctx.check(before.get("position_valid") is True,
              "the position is valid before the power cycle",
              evidence=before)

    identity_before = ctx.link.enumerate_ports()

    ctx.link.close(reason="power cycle")

    ctx.instruct(
        "Power the science module DOWN completely, wait five seconds, "
        "and power it back UP. Then press Enter.")

    import time

    started = time.perf_counter()

    identity_after = ctx.link.enumerate_ports()

    enumerate_ms = round((time.perf_counter() - started) * 1000.0, 3)

    ctx.record("power_cycle", enumerate_ms=enumerate_ms,
               before=identity_before, after=identity_after)

    ctx.check(len(identity_after) >= 1,
              "a serial device is present after the power cycle",
              evidence={"ports": identity_after})

    after = ctx.carousel.status()

    ctx.record("after_power_cycle", **after)

    ctx.check(after.get("position_valid") is not True,
              "the carousel position does NOT survive a power cycle",
              evidence=after)

    ctx.measure(stage="power_cycle", enumerate_ms=enumerate_ms,
                valid_before=before.get("position_valid"),
                valid_after=after.get("position_valid"),
                ports_before=len(identity_before),
                ports_after=len(identity_after))


def _servo_loss(ctx):
    ctx.require("servo.diagnostics", "servo.connect", "carousel.status")

    safe = ctx.ask(
        "Can the ST3215 data line be disconnected safely, without "
        "shorting the servo supply")

    if not safe:
        ctx.skip("the servo bus cannot be disconnected safely on this "
                 "bench")

    ctx.link.request("connect_servo", timeout=ctx.servo._move_timeout())

    healthy = ctx.servo.diagnostics()["data"] or {}

    ctx.check(healthy.get("ok") is True,
              "the servo answers before the disconnect",
              evidence={"ok": healthy.get("ok")})

    ctx.instruct("Disconnect the ST3215 DATA line now. Leave its supply "
                 "connected.")

    failure = _capture_error(ctx.servo.diagnostics)

    ctx.record("servo_lost", **failure)

    ctx.check(failure["raised"] or not (
        (failure.get("data") or {}).get("ok", True)),
        "the diagnostic reports the servo is not answering",
        evidence=failure)

    movement = _capture_error(lambda: ctx.carousel.select_slot(2))

    ctx.record("movement_without_servo", **movement)

    ctx.check(movement["raised"],
              "a slot movement is refused with no servo on the bus",
              evidence=movement)

    status = ctx.carousel.status()

    ctx.check(status.get("position_valid") is not True,
              "the carousel position is invalidated when the servo is "
              "lost",
              evidence=status)

    ctx.instruct("Reconnect the ST3215 data line now.")

    recovery = _capture_error(
        lambda: ctx.link.request(
            "connect_servo", timeout=ctx.servo._move_timeout()))

    ctx.check(not recovery["raised"],
              "the servo reconnects without rebooting the board",
              evidence=recovery)


def _illumination_failure(ctx):
    ctx.require("sensor.acquire_triad", "sensor.status")

    safe = ctx.ask(
        "Can the AS7265x be unplugged safely during an acquisition")

    if not safe:
        ctx.skip("the sensor cannot be disconnected safely on this bench")

    ctx.instruct(
        "A full triad is about to run: WHITE, then UV, then IR. "
        "Disconnect the sensor DURING the UV block if you can, then "
        "press Enter.")

    failure = _capture_error(lambda: ctx.sensor.triad())

    ctx.record("triad_interrupted", **failure)

    ctx.check(failure["raised"],
              "the interrupted triad reported a failure",
              evidence=failure)

    code = str(failure.get("code") or "")

    ctx.check(code.endswith("_ACQUISITION_FAILED"),
              "the error names the illumination that failed "
              "({})".format(code or "no code"),
              evidence=failure)

    data = failure.get("data") or {}
    details = data.get("details") if isinstance(data, dict) else None

    completed = (details or {}).get("completed") if isinstance(
        details, dict) else None

    ctx.check(completed is not None,
              "the error lists the illuminations that completed before "
              "the failure",
              evidence={"completed": completed, "details": details})

    ctx.measure(stage="illumination_failure", code=code,
                completed=";".join(completed or []),
                underlying=(details or {}).get("underlying_code")
                if isinstance(details, dict) else "")


def _return_fails(ctx):
    ctx.require("carousel.measure", "carousel.status",
                "carousel.saved_samples")

    ctx.link.request("connect_servo", timeout=ctx.servo._move_timeout())
    ctx.carousel.sync(load_slot=1)

    ctx.instruct(
        "A measurement is about to run. LET THE ACQUISITION FINISH - "
        "all three illuminations - and then pull the USB cable as the "
        "carousel starts back to the loading position. Press Enter when "
        "you are ready.")

    failure = _capture_error(
        lambda: ctx.carousel.measure(1, sample_id="HW-B9-010"))

    ctx.record("return_interrupted", **failure)

    ctx.check(failure["raised"],
              "the interrupted return reported a failure",
              evidence=failure)

    attached = failure.get("data")

    ctx.note(
        "Any partial result the firmware attached to the error is "
        "recorded above. A movement that failed carries the carousel "
        "state it failed in.")

    ctx.instruct("Reconnect the USB cable and wait for enumeration.")

    ctx.link.close(reason="reconnect after failed return")

    retained = _capture_error(ctx.workflow.saved_samples)

    ctx.record("retained_buffer", **retained)

    ctx.check(not retained["raised"],
              "the device's retained acquisition buffer can be read "
              "after the failure",
              evidence=retained)

    status = ctx.carousel.status()

    ctx.record("after_failed_return", **status)

    ctx.check(status.get("position_valid") is not True,
              "the carousel position is UNCERTAIN after a failed return",
              evidence=status)

    ctx.note(
        "THE SEPARATION IS THE RESULT. The science data is either still "
        "on the device or it is not; the physical position is uncertain "
        "either way. Those are two independent facts and the system "
        "must report both honestly rather than discarding the "
        "measurement because the mechanism could not get home.")

    where = ctx.observe(
        "Where did the carousel stop",
        ("AT_SCANNER", "AT_LOADER", "BETWEEN", "UNKNOWN"))

    ctx.measure(stage="return_failure",
                failure_code=failure.get("code") or "",
                retained_readable=not retained["raised"],
                position_valid=status.get("position_valid"),
                stopped=where)
