"""
The adapters: capability detection, error normalization, and the
production interfaces they claim to wrap.

Capability detection is what decides whether a test is BLOCKED, and it
runs entirely offline - so it is entirely testable offline, which is
what this suite does.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

if str(HERE.parent.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent.parent))

from hardware.adapters.base import (AdapterError,            # noqa: E402
                                    firmware_commands,
                                    firmware_has,
                                    pc_command_surface)
from hardware.core.model import Status                       # noqa: E402
from hardware.offline_tests.fake_link import failing         # noqa: E402
from hardware.offline_tests.harness import (Bench, Checks,   # noqa: E402
                                            cli, registry)


def run():
    checks = Checks("hardware/offline_tests/test_adapters.py")

    checks.section("the firmware command table is read, not guessed")

    commands = firmware_commands()

    checks.ok(len(commands) > 20,
              "the firmware command table was parsed out of protocol.py")

    for command in ("ping", "get_status", "connect_servo",
                    "servo_diagnostics", "servo_bus_scan",
                    "servo_test_move", "get_servo_calibration",
                    "select_slot", "sync_position", "measure_raw",
                    "acquire_block", "acquire_triad", "sensor_test_raw",
                    "led_test", "list_saved_samples"):
        checks.ok(firmware_has(command),
                  "the firmware implements {}".format(command))

    checks.ok(not firmware_has("servo_raw_read"),
              "the firmware has NO raw servo register read - this is "
              "the gap that blocks HW-B2-006 and HW-B3-004")

    checks.ok(not firmware_has("i2c_scan"),
              "the firmware has NO on-demand I2C scan - this is the gap "
              "that blocks HW-B6-001")

    checks.section("the PC command surface is introspected")

    surface = pc_command_surface()

    for method in ("open", "close", "request", "ping", "get_status",
                   "hard_reset", "available_ports", "servo_diagnostics",
                   "servo_test_move", "measure_raw", "acquire_triad"):
        checks.ok(method in surface,
                  "SerialLink exposes {}".format(method))

    checks.section("capabilities are detected without hardware")

    with Bench() as bench:
        capabilities = bench.context.capabilities()

        checks.ok(len(capabilities) > 20,
                  "every adapter contributed capabilities")

        available = {name for name, c in capabilities.items()
                     if c.available}

        for name in ("link.ping", "link.status", "link.hard_reset",
                     "servo.diagnostics", "servo.test_move",
                     "servo.bus_scan", "sensor.acquire_triad",
                     "carousel.measure", "workflow.client",
                     "workflow.screens"):
            checks.ok(name in available,
                      "{} is available".format(name))

        missing = {name: c for name, c in capabilities.items()
                   if not c.available}

        for name in ("servo.raw_packet", "servo.read_register",
                     "sensor.i2c_scan_on_demand", "link.raw_stream",
                     "carousel.interrupt_move", "workflow.drive_menus"):
            checks.ok(name in missing,
                      "{} is correctly reported as missing".format(name))

        for name, capability in sorted(missing.items()):
            checks.ok(bool(capability.reason),
                      "{} says why it is missing".format(name))

            checks.ok(bool(capability.recommendation),
                      "{} names the change that would unblock "
                      "it".format(name))

    checks.section("every BLOCKED test names a capability that exists "
                   "in the framework")

    catalogue = registry()

    with Bench() as bench:
        known = set(bench.context.capabilities())

        for definition in catalogue.all_tests():
            for name in definition.requires:
                checks.ok(name in known,
                          "{} requires {}, which the framework "
                          "knows".format(definition.test_id, name))

    checks.section("the tests that must be BLOCKED today are BLOCKED")

    # The three original blockers now name the DIAGNOSTIC AGENT, not a
    # gap in the competition firmware. That is the point of the agent:
    # they moved from "blocked by something nobody may add" to "blocked
    # until somebody deploys the thing that exists".
    expected_blocked = {
        "HW-B2-006": "diagnostic.servo_raw",
        "HW-B3-004": "diagnostic.servo_raw",
        "HW-B6-001": "diagnostic.i2c_scan",
        "HW-B2-011": "diagnostic.agent",
        "HW-B0-008": "bench.multimeter",
        "HW-B11-007": "bench.thermal_probe",
    }

    for test_id, capability in sorted(expected_blocked.items()):
        with Bench() as bench:
            result = bench.run(test_id)

            checks.equal(result.status, Status.BLOCKED,
                         "{} is BLOCKED".format(test_id))

            checks.ok(capability in result.reason,
                      "{} names {} as the missing "
                      "capability".format(test_id, capability))

            checks.ok(any("recommendation" in n for n in result.notes),
                      "{} carries the production change that would "
                      "unblock it".format(test_id))

    checks.section("errors are normalized without being destroyed")

    original = ValueError("the underlying thing")

    error = AdapterError("wrapped", code="PORT_LOST", original=original,
                         data={"kind": "LINK"})

    checks.equal(error.code, "PORT_LOST", "the code survives")
    checks.equal(error.original_type, "ValueError",
                 "the original exception type survives")
    checks.equal(error.original_message, "the underlying thing",
                 "the original message survives")
    checks.ok(error.original is original,
              "the original exception object itself is kept")

    payload = error.as_dict()

    checks.ok("original_type" in payload and "code" in payload,
              "the serialized form carries both")

    checks.section("a device failure through the fake keeps its code")

    from hardware.offline_tests.fake_link import healthy_script

    script = dict(healthy_script())
    script["servo_diagnostics"] = failing(
        "SERVO_NO_RESPONSE", "servo 1 did not answer")

    with Bench(script=script) as bench:
        try:
            bench.context.servo.diagnostics()

            checks.ok(False, "the scripted failure raised")

        except AdapterError as error:
            checks.equal(error.code, "SERVO_NO_RESPONSE",
                         "the device error code reaches the test side")

            checks.ok("transaction" in error.data,
                      "the failing transaction travels with the error")

    checks.section("the servo adapter refuses an unverifiable movement")

    with Bench() as bench:
        checks.raises(
            ValueError,
            lambda: bench.context.servo.move_degrees(360.0),
            "360 degrees in one leg is refused - the driver cannot "
            "verify more than half a revolution from one reading")

        plan = bench.context.servo.plan_degrees(360.0)

        checks.equal(plan["repeat"], 2,
                     "360 degrees is planned as two verified legs")

        checks.equal(plan["degrees"], 180.0,
                     "each leg is a half turn")

        checks.equal(plan["split"], True,
                     "and the plan says it was split, so nobody reads "
                     "'360' as one commanded movement")

        small = bench.context.servo.plan_degrees(45.0)

        checks.equal(small["split"], False,
                     "a small angle needs no splitting")

        checks.equal(small["repeat"], 1, "and runs as one leg")

        checks.equal(bench.context.servo.plan_degrees(0), None,
                     "a zero-degree movement is no movement")

        negative = bench.context.servo.plan_degrees(-180.0)

        checks.equal(negative["degrees"], -180.0,
                     "the sign is preserved")

    checks.section("the carousel geometry is generated from production")

    with Bench() as bench:
        carousel = bench.context.carousel

        checks.equal(carousel.slot_count(), 4,
                     "the slot count comes from the firmware")

        adjacent = carousel.adjacent_transitions()

        checks.equal(len(adjacent), 8,
                     "a four-slot plate has four adjacent transitions "
                     "in each direction")

        checks.ok((4, 1) in adjacent,
                  "the wrap-around transition is included")

        non_adjacent = carousel.non_adjacent_transitions()

        checks.equal(sorted(non_adjacent), [(1, 3), (2, 4), (3, 1),
                                            (4, 2)],
                     "the non-adjacent pairs are the opposite slots")

        checks.equal(carousel.full_rotation_sequence(),
                     [1, 2, 3, 4, 1],
                     "a full rotation returns to where it started")

    checks.section("the workflow adapter checks the real client")

    with Bench() as bench:
        capabilities = bench.context.workflow.capabilities()

        checks.ok(capabilities["workflow.client"].available,
                  "the production operator client is present")

        checks.equal(
            capabilities["workflow.screens"].detail["missing"], [],
            "every screen the B10 procedure names is still defined in "
            "workflow/screen.py")

        checks.ok(not capabilities["workflow.drive_menus"].available,
                  "the framework deliberately does not drive the "
                  "production menus")

        checks.ok("BD/samples" in
                  capabilities["workflow.drive_menus"].reason
                  or "irreplaceable" in
                  capabilities["workflow.drive_menus"].reason,
                  "and it says the archive is why")

    return checks.report()


if __name__ == "__main__":
    sys.exit(cli(run, __doc__))
