"""
Servo lifecycle: the state machine and its safety gates.

There is one actuator, an ST3215, and connecting to it is still an
explicit act rather than something that happens on boot. That makes the
connection itself a safety mechanism, so it is tested as one:

    NOT CONNECTED -> CONNECTED -> RECONNECTED -> NOT CONNECTED

For every transition: the backend is live, any previous one was taken
down rather than dropped, and the carousel position was invalidated.
Position state cannot survive a reconnect - the servo may have been
unplugged, turned by hand, or swapped for another.

And before any connection at all, every movement command must fail
safely with SERVO_NOT_CONNECTED rather than driving a UART at hardware
that may not be there.
"""

import sys

import support
from support import Checks, FakeAS7265X, FakeST3215


def main_tests():
    checks = Checks("Servo manager")

    # ==================================================================
    checks.section("1. after boot, nothing is connected")

    _main, module, config, fake = support.build_firmware()

    checks.equal(
        module.servos.servo_type, None, "no servo type is active"
    )
    checks.ok(module.servos.servo is None, "and no backend object exists")
    checks.ok(
        not module.servos.is_connected(),
        "is_connected() says so plainly",
    )
    checks.ok(
        not module.servos.is_selected(),
        "and the historical is_selected() agrees",
    )
    checks.equal(
        module.servos.label(), "NOT CONNECTED",
        "the label an operator sees is NOT CONNECTED",
    )
    checks.ok(
        module.carousel.servo is None,
        "the carousel has no actuator attached",
    )
    checks.ok(
        module.carousel.capabilities() is None,
        "and reports no capabilities",
    )

    # Nothing was touched on the way up.
    checks.equal(fake.packets, [], "not one byte was sent to the servo bus")

    # ==================================================================
    checks.section("2. movement is blocked until the servo is connected")

    blocked = (
        ("move_slots", {"direction": "cw", "slots": 1}),
        ("select_slot", {"slot": 2}),
        ("fine_adjust", {"degrees": 1.0}),
        ("servo_stop", {}),
        ("servo_diagnostics", {}),
        ("get_servo_calibration", {}),
        ("set_servo_calibration", {"values": {"neutral_us": 1500}}),
        ("servo_test_move", {"kind": "slot_cw", "confirm": True}),
        ("servo_configure", {"confirm": True}),
        ("servo_torque", {"enable": True}),
    )

    for command, payload in blocked:
        response = support.command(module, command, **payload)

        checks.ok(
            not response["ok"], "{} is refused".format(command)
        )
        checks.equal(
            response["error"]["code"], "SERVO_NOT_CONNECTED",
            "with SERVO_NOT_CONNECTED, not a hardware error",
        )
        checks.ok(
            "[0]" in response["error"]["message"],
            "and it names the option that fixes it",
        )

    # A measurement needs the carousel, so it is blocked too.
    measurement = support.command(module, "measure_raw", slot=1)

    checks.ok(
        not measurement["ok"],
        "and no measurement runs without an actuator",
    )

    checks.equal(fake.packets, [], "still nothing on the servo bus")

    # Commands that touch no hardware still work, which is what makes the
    # module diagnosable in this state.
    for command in ("ping", "get_status", "get_servo_options",
                    "list_saved_samples"):
        checks.ok(
            support.command(module, command)["ok"],
            "{} still works with no servo connected".format(command),
        )

    options = support.command(module, "get_servo_options")["data"]

    checks.equal(
        options["servo_info"]["type"], "st3215",
        "the firmware reports which actuator is fitted",
    )
    checks.ok(
        bool(options["servo_info"]["description"]),
        "with a description, so an operator screen can be built from it",
    )
    checks.ok(
        not options["connected"], "and reports that it is not connected",
    )

    # ==================================================================
    checks.section("3. an actuator this firmware cannot drive is refused")

    # The MG995 backend was removed. Asking for it must fail loudly
    # rather than quietly handing back the ST3215 - a caller that wanted
    # a timed open-loop servo would otherwise get a closed-loop one and
    # never be told.
    response = support.command(module, "select_servo", servo="mg995")

    checks.ok(not response["ok"], "select_servo mg995 is refused")
    checks.equal(
        response["error"]["code"], "INVALID_SERVO", "with INVALID_SERVO"
    )
    checks.ok(
        "ST3215" in response["error"]["message"],
        "and the message names what this firmware does drive",
    )
    checks.ok(
        module.servos.servo is None,
        "nothing is connected as a side effect",
    )

    response = support.command(module, "select_servo", servo="mg996")

    checks.ok(not response["ok"], "an unknown servo type is refused too")
    checks.equal(
        response["error"]["code"], "INVALID_SERVO", "with INVALID_SERVO"
    )

    # ==================================================================
    checks.section("4. connecting brings the ST3215 up")

    response = support.command(module, "select_servo")

    checks.ok(
        response["ok"],
        "select_servo with no argument connects the fitted servo",
    )
    checks.equal(
        module.servos.servo_type, "st3215", "the ST3215 is now active"
    )
    checks.ok(module.servos.servo is not None, "a backend object exists")
    checks.ok(module.servos.servo.ready, "and it came up ready")
    checks.equal(
        module.servos.label(), "Waveshare ST3215",
        "the operator sees the actuator's name",
    )
    checks.ok(
        module.carousel.servo is module.servos.servo,
        "the carousel is attached to that same backend",
    )
    checks.ok(
        bool(fake.packets),
        "the servo was actually spoken to, not assumed present",
    )
    checks.ok(
        not module.carousel.position_valid,
        "and the carousel position starts invalid",
    )

    capabilities = module.servos.capabilities()

    checks.ok(
        capabilities["encoder"] and capabilities["verified_movement"],
        "the reported capabilities are the closed-loop ones",
    )

    # Movement is allowed now, once the operator has declared Slot 1.
    support.command(module, "sync_position", load_slot=1)

    checks.ok(
        module.carousel.position_valid,
        "declaring Slot 1 makes the position valid",
    )
    checks.ok(
        support.command(
            module, "move_slots", direction="cw", slots=1
        )["ok"],
        "and movement is no longer blocked",
    )

    # ==================================================================
    checks.section("5. reconnecting takes the old backend down first")

    support.command(module, "sync_position", load_slot=1)

    checks.ok(module.carousel.position_valid, "with a valid position first")

    previous_backend = module.servos.servo

    response = support.command(module, "select_servo", servo="st3215")

    checks.ok(response["ok"], "reconnecting works")
    checks.ok(
        module.servos.servo is not previous_backend,
        "a different backend object is live",
    )
    checks.ok(
        not previous_backend.ready,
        "and the previous backend was deinitialized, not just dropped",
    )
    checks.ok(
        response["data"]["selection"]["reconnected"],
        "the response says it was a reconnect",
    )
    checks.ok(
        response["data"]["selection"]["released_previous"] is not None,
        "and reports that the old backend was released",
    )
    checks.ok(
        not module.carousel.position_valid,
        "the position is invalidated - the servo may have been "
        "unplugged or turned by hand in between",
    )

    # ==================================================================
    checks.section("6. disconnecting blocks movement again")

    support.command(module, "sync_position", load_slot=1)

    response = support.command(module, "select_servo", servo="none")

    checks.ok(response["ok"], "the servo can be disconnected")
    checks.equal(module.servos.servo_type, None, "nothing is active")
    checks.ok(module.servos.servo is None, "and no backend object survives")
    checks.ok(
        not module.carousel.position_valid,
        "the carousel position is invalidated",
    )

    blocked = support.command(module, "move_slots", direction="cw", slots=1)

    checks.ok(not blocked["ok"], "and movement is blocked once more")
    checks.equal(
        blocked["error"]["code"], "SERVO_NOT_CONNECTED",
        "with SERVO_NOT_CONNECTED",
    )

    # ==================================================================
    checks.section("7. a backend that cannot come up connects nothing")

    _main, module, config, fake = support.build_firmware(
        servo=FakeST3215(silent=True)
    )

    response = support.command(module, "select_servo")

    checks.ok(not response["ok"], "connecting to an absent ST3215 fails")
    checks.equal(
        response["error"]["code"], "SERVO_NOT_FOUND",
        "with the real reason, not a generic connection error",
    )
    checks.ok(
        module.servos.servo is None,
        "and the manager is left empty rather than holding a dead backend",
    )
    checks.ok(
        module.carousel.servo is None,
        "the carousel is not left pointing at it either",
    )

    blocked = support.command(module, "move_slots", direction="cw", slots=1)

    checks.equal(
        blocked["error"]["code"], "SERVO_NOT_CONNECTED",
        "so movement stays blocked",
    )

    # A failed connection must stay diagnosable: the scan that tells the
    # operator WHY is the one thing that has to keep working here.
    checks.ok(
        support.command(module, "get_status")["ok"],
        "status still answers, so the fault can be investigated",
    )

    # ==================================================================
    checks.section("8. the manager owns no geometry")

    source = (support.ESP32_DIR / "control" / "servo_manager.py").read_text(
        encoding="utf-8"
    )

    for token in ("CAROUSEL_SLOT_COUNT", "slot_count", "scan_slot",
                  "load_slot", "half_turn", "plan_move"):
        checks.ok(
            token not in source,
            "the servo manager never mentions '{}'".format(token),
        )

    checks.ok(
        "import carousel" not in source
        and "from control.carousel" not in source,
        "and it does not import the carousel",
    )

    # The removed backend must be gone from the lifecycle layer entirely,
    # not merely unreachable.
    checks.ok(
        "mg995" not in source.lower().replace(
            "supported an mg995 and this module picked between them", ""
        ),
        "and it drives no removed backend",
    )

    return checks.report()


if __name__ == "__main__":
    sys.exit(main_tests())
