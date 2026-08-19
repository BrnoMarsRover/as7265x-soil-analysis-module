"""
Carousel geometry, planning and position tracking.

The carousel is the layer that must behave IDENTICALLY on both actuators.
Everything below it differs - pulse widths on one, encoder counts on the
other - so the same logical movement is run on both backends here and the
logical outcome is required to match.

What is specifically checked:

  * the slot geometry and the loader/scanner mapping;
  * shortest-path planning, including that Slot 4 -> Slot 1 is ONE slot
    backwards and never three forwards;
  * that the plan is made in SLOTS, before any actuator sees it, which is
    what makes the encoder seam irrelevant;
  * fine alignment: remembered, and never a change of logical slot;
  * failures: invalidated rather than assumed, on both backends;
  * an eight-slot configuration, because nothing here may be written for
    four slots specifically.
"""

import sys

import support
from support import Checks, FakeAS7265X, FakeST3215


def build(servo=None):
    """Firmware with the ST3215 connected and ready to move."""
    _main, module, config, fake = support.build_firmware(servo=servo)

    response = support.command(module, "select_servo")

    if not response["ok"]:
        raise AssertionError(
            "could not connect the servo: %r" % (response["error"],)
        )

    return module, config, fake


def main_tests():
    checks = Checks("Carousel")

    # ==================================================================
    checks.section("1. geometry is configured, not hardcoded")

    module, config, fake = build(FakeST3215(position=2048))

    carousel = module.carousel

    checks.equal(config.CAROUSEL_SLOT_COUNT, 4, "four physical slots")
    checks.close(
        config.CAROUSEL_SLOT_GEOMETRY_DEG, 90.0, "90 degrees between slots"
    )
    checks.equal(
        config.CAROUSEL_SCAN_LOAD_OFFSET, 2,
        "the scanner is two slots from the loader",
    )
    checks.close(
        config.CAROUSEL_HALF_TURN_DEG, 180.0, "which is 180 degrees"
    )
    checks.ok(
        carousel.geometry_error is None,
        "the configured geometry is self-consistent",
    )
    checks.equal(carousel.slot_count, 4, "the model has four slots")
    checks.equal(
        sorted(carousel.slots.keys()), [1, 2, 3, 4], "slots are 1..4"
    )

    from control.carousel import CarouselError

    checks.raises(
        CarouselError, lambda: carousel.validate_slot(5),
        "slot 5 does not exist",
    )
    checks.raises(
        CarouselError, lambda: carousel.validate_slot(0), "slot 0 is refused"
    )
    checks.equal(carousel.validate_slot(4), 4, "slot 4 is accepted")

    # Loader/scanner mapping: 1<->3, 2<->4, and its own inverse.
    for load_slot, scan_slot in ((1, 3), (2, 4), (3, 1), (4, 2)):
        checks.equal(
            carousel.scan_slot_for_load(load_slot), scan_slot,
            "loader {} means scanner {}".format(load_slot, scan_slot),
        )
        checks.equal(
            carousel.load_slot_for(scan_slot), load_slot,
            "and the mapping is its own inverse",
        )

    # ==================================================================
    checks.section("2. the carousel contains no actuator detail")

    import ast

    source = (support.ESP32_DIR / "control" / "carousel.py").read_text(
        encoding="utf-8"
    )

    # Every name the code actually USES. Comments and docstrings are
    # excluded on purpose: the module header legitimately names the things
    # the abstraction excludes, and a plain substring search cannot tell
    # that apart from a real leak.
    names = set()

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name):
            names.add(node.id)

        elif isinstance(node, ast.Attribute):
            names.add(node.attr)

        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)

    identifiers = " ".join(sorted(names))

    for token in ("duty_ns", "PWM", "pulse", "_us", "packet", "checksum",
                  "REG_", "counts_per_rev", "encode_signed", "counts"):
        checks.ok(
            token not in identifiers,
            "carousel.py uses no identifier containing '{}' - the "
            "abstraction holds".format(token),
        )

    checks.ok(
        "drivers.mg995" not in source and "import mg995" not in source,
        "and it names no removed backend",
    )
    checks.ok(
        "drivers.st3215" not in source and "import st3215" not in source,
        "nor the ST3215 implementation",
    )
    checks.ok(
        "from drivers import servo_base" in source,
        "only the shared actuator vocabulary",
    )

    # ==================================================================
    checks.section("3. synchronization records a reference")

    checks.ok(not carousel.position_valid, "position is unknown after boot")
    checks.ok(carousel.origin is None, "and there is no origin yet")

    carousel.sync_to_load_slot(1)

    checks.equal(carousel.get_load_slot(), 1, "Slot 1 is at the loader")
    checks.equal(carousel.current_scan_slot, 3, "Slot 3 is at the scanner")
    checks.equal(carousel.selected_slot, 1, "Slot 1 is selected")
    checks.equal(carousel.phase(), "LOAD", "phase is LOAD")
    checks.ok(carousel.position_valid, "and the position is valid")

    checks.ok(
        carousel.origin["feedback"],
        "on the ST3215 the origin is a real measurement",
    )
    checks.equal(
        carousel.origin["origin_counts"], 2048,
        "namely the encoder reading at the moment of confirmation",
    )

    # ==================================================================
    checks.section("4. shortest path is planned in slots")

    for start_load, target_load, direction, steps in (
        (1, 2, "cw", 1),
        (2, 1, "ccw", 1),
        (3, 4, "cw", 1),
        (4, 1, "cw", 1),
        (1, 4, "ccw", 1),
        (1, 3, "cw", 2),
        (3, 1, "cw", 2),
    ):
        carousel.sync_to_load_slot(start_load)

        plan = carousel.plan_move(carousel.scan_slot_for_load(target_load))

        checks.equal(
            plan, (direction, steps),
            "loader {} -> {} is {} slot(s) {}".format(
                start_load, target_load, steps, direction
            ),
        )

    carousel.sync_to_load_slot(1)

    opposite_slot = (
        (carousel.current_scan_slot - 1 + 2) % config.CAROUSEL_SLOT_COUNT
    ) + 1

    checks.equal(
        carousel.plan_move(opposite_slot)[0],
        config.CAROUSEL_FORWARD_DIRECTION,
        "an exact half-turn tie resolves to the forward direction",
    )

    # ==================================================================
    checks.section("5. Slot 4 -> Slot 1 is one slot, on the encoder too")

    # The plan is made in slots, so where the absolute encoder happens to
    # be reading is irrelevant. Started right before the 4095/0 seam on
    # purpose.
    fake.position = 3900
    carousel.sync_to_load_slot(4)

    before = len(fake.goals)
    carousel.select_slot(1)

    checks.equal(
        fake.goals[before:], [1024],
        "a single +1024 count step, not +3072 the long way round",
    )
    checks.equal(
        carousel.get_load_slot(), 1, "and Slot 1 really is at the loader"
    )
    checks.equal(
        fake.position, 828, "having crossed the encoder boundary from 3900"
    )

    fake.position = 100
    carousel.sync_to_load_slot(1)

    before = len(fake.goals)
    carousel.select_slot(4)

    checks.equal(
        fake.goals[before:], [-1024],
        "and Slot 1 -> Slot 4 is a single -1024 count step",
    )
    checks.equal(fake.position, 3172, "wrapping down through zero")

    # A multi-slot jog is separate verified movements, because one encoder
    # reading cannot verify more than half a turn.
    fake.position = 2048
    carousel.sync_to_load_slot(1)

    before = len(fake.goals)
    carousel.move_slots("cw", 3)

    checks.equal(
        fake.goals[before:], [1024, 1024, 1024],
        "a three-slot jog is three verified 1024-count movements",
    )
    checks.equal(carousel.get_load_slot(), 4, "landing three slots on")

    # ==================================================================
    checks.section("6. the measurement path closes on itself")

    fake.position = 2048
    carousel.sync_to_load_slot(1)

    carousel.move_selected_to_scanner()

    checks.equal(
        carousel.current_scan_slot, 1, "the half turn brings Slot 1 round"
    )
    checks.equal(carousel.phase(), "SCAN", "phase is SCAN")
    checks.equal(
        fake.goals[-1], 2048, "commanded as 2048 counts, not a duration"
    )

    carousel.return_selected_to_loader()

    checks.equal(
        carousel.current_scan_slot, 3, "the return half turn undoes it"
    )
    checks.equal(carousel.phase(), "LOAD", "phase is LOAD again")
    checks.equal(fake.goals[-1], -2048, "in the opposite direction")
    checks.equal(
        fake.position, 2048, "and the mechanism is back at the origin"
    )
    checks.close(
        carousel.drift_deg(), 0.0, "with no measured drift",
        tolerance=0.01,
    )

    # ==================================================================
    checks.section("7. fine alignment is remembered, not undone")

    fake.position = 2048
    carousel.sync_to_load_slot(1)

    scan_before = carousel.current_scan_slot
    selected_before = carousel.selected_slot

    adjustment = carousel.fine_adjust(2.0)

    checks.equal(
        carousel.current_scan_slot, scan_before,
        "fine alignment does not change the scanner slot",
    )
    checks.equal(
        carousel.selected_slot, selected_before, "nor the selected slot"
    )
    checks.ok(carousel.position_valid, "and the position stays valid")
    checks.ok(
        not adjustment["logical_position_changed"],
        "which the response states explicitly",
    )
    checks.equal(
        fake.position, 2048 + 23, "the mechanism moved 23 encoder counts"
    )
    checks.close(
        carousel.alignment_offset_deg, 2.0215,
        "and the offset records what was actually commanded, not the "
        "rounded request",
        tolerance=0.001,
    )
    checks.close(
        carousel.drift_deg(), 0.0,
        "so quantization does not masquerade as drift",
        tolerance=0.01,
    )

    # The next slot movement must KEEP the correction.
    carousel.move_slots("cw", 1)

    checks.equal(
        fake.position, 2048 + 23 + 1024,
        "a slot movement carries the alignment offset with it",
    )
    checks.close(
        carousel.alignment_offset_deg, 2.0215,
        "the offset itself is unchanged by a slot movement",
        tolerance=0.001,
    )
    checks.close(
        carousel.drift_deg(), 0.0, "and there is still no drift",
        tolerance=0.01,
    )

    # A second nudge accumulates.
    carousel.fine_adjust(-1.0)

    checks.close(
        carousel.alignment_offset_deg, 2.0215 - 0.9668,
        "a further correction accumulates, again in commanded degrees "
        "(-1.0 deg is 11 counts, so -0.9668 deg)",
        tolerance=0.001,
    )

    # Re-synchronizing folds the offset into the new origin.
    carousel.sync_to_load_slot(1)

    checks.close(
        carousel.alignment_offset_deg, 0.0,
        "and a re-sync makes the confirmed position the new reference",
        tolerance=1e-9,
    )

    tiny = carousel.fine_adjust(0.01)

    checks.ok(not tiny["moved"], "0.01 deg is below one encoder count")
    checks.close(
        carousel.alignment_offset_deg, 0.0, "and changes no offset",
        tolerance=1e-9,
    )

    # ==================================================================
    checks.section("8. real drift is detected")

    fake.position = 2048
    carousel.sync_to_load_slot(1)

    # The mechanism slips: 40 counts of the next movement are lost. Under
    # tolerance, so the movement itself passes - which is exactly the case
    # that accumulates silently without a drift figure.
    fake.short_by = 12
    carousel.move_slots("cw", 1)
    fake.short_by = 0

    drift = carousel.drift_deg()

    checks.ok(drift is not None, "drift is measurable on the ST3215")
    checks.close(
        drift, -12 * 360.0 / 4096.0,
        "and reports the 12 counts the mechanism fell short",
        tolerance=0.01,
    )

    # ==================================================================
    checks.section("9. a failed movement never becomes a position")

    module, config, fake = build(FakeST3215(position=2048))

    from control.carousel import CarouselError

    carousel = module.carousel
    carousel.sync_to_load_slot(1)

    # The mechanism jams: the servo stops well short from now on.
    fake.short_by = 300

    checks.raises(
        CarouselError,
        lambda: carousel.move_slots("cw", 1),
        "a movement that lands off target raises",
    )
    checks.ok(
        not carousel.position_valid,
        "and the tracked position is invalidated, not guessed",
    )
    checks.equal(
        carousel.phase(), "UNKNOWN", "the phase follows the position"
    )

    response = support.command(module, "move_slots", direction="cw", slots=1)

    checks.ok(not response["ok"], "the PC is told the movement failed")
    checks.equal(
        response["error"]["code"], "SERVO_POSITION_MISMATCH",
        "with a machine-readable code from the backend",
    )
    checks.ok(
        response["data"]["measured_travel_deg"] is not None,
        "and the encoder is still read, rather than the position guessed",
    )
    checks.ok(
        not response["data"]["position_valid"],
        "which is reported as an invalid position",
    )

    response = support.command(module, "measure_raw", slot=1)

    checks.ok(
        not response["ok"],
        "and no measurement is taken while the position is unknown",
    )

    # ==================================================================
    checks.section("10. a movement failure invalidates the position")

    module, config, fake = build()

    carousel = module.carousel

    # Re-imported AFTER build(): each build reloads the firmware, so the
    # CarouselError bound earlier in this function belongs to a previous
    # load and would never match the one raised now.
    from control.carousel import CarouselError as LiveCarouselError
    from drivers.servo_base import ServoError

    carousel.sync_to_load_slot(1)

    checks.ok(carousel.position_valid, "the position starts valid")

    original = module.servos.servo.move_slots

    def jam(direction, slots):
        raise ServoError("the mechanism jammed", code="SERVO_JAMMED")

    module.servos.servo.move_slots = jam

    checks.raises(
        LiveCarouselError,
        lambda: carousel.move_slots("cw", 1),
        "a driver movement failure surfaces as a CarouselError",
    )
    checks.ok(
        not carousel.position_valid,
        "and the tracked position is invalidated, because where the "
        "mechanism stopped is now unknown",
    )

    module.servos.servo.move_slots = original

    # ==================================================================
    checks.section("11. eight slots, from the same code")

    # The other configuration this geometry has to support: 45 degrees per
    # slot, four slots between loader and scanner. Nothing in the carousel
    # is written for four slots specifically, and this is what proves it.
    module, config, fake = build(FakeST3215(position=0))

    from control import carousel as carousel_module

    config.CAROUSEL_SLOT_COUNT = 8
    config.CAROUSEL_SCAN_LOAD_OFFSET = 4
    config.CAROUSEL_SLOT_GEOMETRY_DEG = 45.0
    config.ST3215_COUNTS_PER_SLOT = 512

    eight = carousel_module.Carousel(module.servos.servo)

    checks.ok(
        eight.geometry_error is None, "an eight-slot carousel is consistent"
    )
    checks.close(eight.slot_step_deg, 45.0, "45 degrees between slots")

    eight.sync_to_load_slot(1)

    checks.equal(eight.current_scan_slot, 5, "loader 1 means scanner 5")

    for start_load, target_load, direction, steps in (
        (1, 2, "cw", 1),
        (7, 8, "cw", 1),
        (8, 1, "cw", 1),
        (1, 8, "ccw", 1),
        (1, 5, "cw", 4),
        (5, 1, "cw", 4),
    ):
        eight.sync_to_load_slot(start_load)

        plan = eight.plan_move(eight.scan_slot_for_load(target_load))

        checks.equal(
            plan, (direction, steps),
            "eight slots: loader {} -> {} is {} slot(s) {}".format(
                start_load, target_load, steps, direction
            ),
        )

    fake.position = 4090
    eight.sync_to_load_slot(8)

    before = len(fake.goals)
    eight.select_slot(1)

    checks.equal(
        fake.goals[before:], [512],
        "Slot 8 -> Slot 1 is +512 counts, never 3584 the long way round",
    )
    checks.equal(
        fake.position, 506, "crossing the encoder boundary from 4090"
    )

    # A geometry that does not line up is refused rather than producing
    # movements that miss the slot centres.
    config.CAROUSEL_SCAN_LOAD_OFFSET = 3

    broken = carousel_module.Carousel(module.servos.servo)

    checks.ok(
        broken.geometry_error is not None,
        "an offset that is not half the slot count is rejected",
    )
    checks.raises(
        carousel_module.CarouselError,
        lambda: broken.jog_slots("cw", 1),
        "and a broken geometry refuses to move at all",
    )

    return checks.report()


if __name__ == "__main__":
    sys.exit(main_tests())
