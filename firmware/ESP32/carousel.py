# carousel.py
# Software model of the 8-slot sample carousel.
#
# There is no encoder, no Hall sensor and no position feedback of any
# kind. The carousel position is therefore purely virtual: it is
# established once by the operator through sync_position and then
# maintained by counting calibrated 45 degree steps.
#
# Two fixed mechanical positions exist, exactly 180 degrees apart:
#
#     SCAN  - the slot under the AS7265x
#     LOAD  - the slot under the loading hole
#
# 180 degrees is 4 of the 8 slots, so:
#
#     load_slot = ((scan_slot - 1 + 4) % 8) + 1
#
# which for scan_slot = 1 gives load_slot = 5, and is its own inverse:
# putting Slot 3 under the loader necessarily puts Slot 7 under the
# scanner.
#
# Physical slot occupancy and the tracked position live in RAM only and
# are intentionally forgotten on reset.
#
# This is deliberately NOT a sample lifecycle. The ESP32 knows only
# whether a slot physically holds soil and which Sample ID the PC
# attached to it, so that clear_slot has something to free and the
# operator can see the real carousel contents. EMPTY / READY_TO_LOAD /
# LOADED / MEASURED are scientific states and belong to the PC, which
# is the authoritative sample archive.

import config
import mg995


class CarouselError(Exception):
    """Movement or state error carrying a machine-readable code."""

    def __init__(self, code, message):
        # super() - MicroPython cannot call Exception.__init__ unbound.
        super().__init__(message)

        self.code = str(code)
        self.message = str(message)


def new_slot(slot_id):
    """Runtime record for one empty physical slot."""
    return {
        "slot_id": slot_id,
        "occupied": False,
        "sample_id": None
    }


class Carousel:
    """Runtime state of the physical carousel."""

    def __init__(self, servo=None):
        self.servo = servo

        self.slot_count = config.CAROUSEL_SLOT_COUNT
        self.offset = config.CAROUSEL_SCAN_LOAD_OFFSET

        # Physical occupancy: RAM only, reset on every reboot.
        self.slots = {}

        for slot_id in range(1, self.slot_count + 1):
            self.slots[slot_id] = new_slot(slot_id)

        # Position tracking: unknown until the operator synchronizes.
        self.position_valid = False
        self.current_scan_slot = None

        # The physical slot the operator is currently working with.
        #
        # Deliberately separate from "slot under the loader" and "slot
        # under the scanner": while soil is being deposited the selected
        # slot sits at the loader, but after measure_sample the carousel
        # has turned 180 deg and the same selected slot sits at the
        # scanner instead.
        self.selected_slot = None

        self.last_move = None

    # ------------------------------------------------------------------
    # slot helpers
    # ------------------------------------------------------------------

    def validate_slot(self, slot_id):
        """Coerce and range-check a slot number coming from the PC."""
        try:
            slot_id = int(slot_id)

        except (TypeError, ValueError):
            raise CarouselError(
                "INVALID_SLOT",
                "Slot must be an integer between 1 and {}.".format(
                    self.slot_count
                )
            )

        if slot_id < 1 or slot_id > self.slot_count:
            raise CarouselError(
                "INVALID_SLOT",
                "Slot {} is out of range; valid slots are 1 to {}.".format(
                    slot_id,
                    self.slot_count
                )
            )

        return slot_id

    def slot(self, slot_id):
        return self.slots[self.validate_slot(slot_id)]

    def slot_list(self):
        """All 8 runtime slots, ordered by slot number."""
        return [
            self.slots[slot_id]
            for slot_id in range(1, self.slot_count + 1)
        ]

    def reset_slot(self, slot_id):
        """
        Free a physical slot.

        Runtime state only: the PC's persistent scientific record for
        whatever was in the slot is a completely separate thing and is
        never touched from here.
        """
        slot_id = self.validate_slot(slot_id)
        self.slots[slot_id] = new_slot(slot_id)

        return self.slots[slot_id]

    def mark_occupied(self, slot_id, sample_id=None):
        """
        Record that a slot physically holds soil.

        Set after a successful acquisition: the soil really is in the
        slot, and stays there until clear_slot. The Sample ID is carried
        only so a response can be correlated with the PC's record.
        """
        slot_id = self.validate_slot(slot_id)
        slot = self.slots[slot_id]

        slot["occupied"] = True

        if sample_id is not None:
            slot["sample_id"] = sample_id

        return slot

    # ------------------------------------------------------------------
    # geometry
    # ------------------------------------------------------------------

    def load_slot_for(self, scan_slot):
        """Slot sitting at the loader while scan_slot is at the scanner."""
        return (
            (scan_slot - 1 + self.offset) % self.slot_count
        ) + 1

    def scan_slot_for_load(self, load_slot):
        """Slot that must be at the scanner to put load_slot at the loader."""
        return (
            (load_slot - 1 + self.slot_count - self.offset) % self.slot_count
        ) + 1

    def get_load_slot(self):
        """Current loader slot, or None while the position is unknown."""
        if not self.position_valid or self.current_scan_slot is None:
            return None

        return self.load_slot_for(self.current_scan_slot)

    def phase(self):
        """
        Where the selected slot currently sits.

        Derived rather than stored, so it can never drift out of step
        with the tracked position:

            LOAD     selected slot is under the loading hole
            SCAN     selected slot is under the scanner
            UNKNOWN  position not synchronized, or nothing selected
        """
        if not self.position_valid or self.selected_slot is None:
            return "UNKNOWN"

        if self.get_load_slot() == self.selected_slot:
            return "LOAD"

        if self.current_scan_slot == self.selected_slot:
            return "SCAN"

        return "OTHER"

    # ------------------------------------------------------------------
    # position tracking
    # ------------------------------------------------------------------

    def require_position(self):
        if not self.position_valid or self.current_scan_slot is None:
            raise CarouselError(
                "POSITION_NOT_SYNCHRONIZED",
                "Carousel position is unknown. Send sync_position with the "
                "slot number currently aligned with the scanner before "
                "requesting any movement."
            )

    def sync_position(self, scan_slot):
        """
        Declare which physical slot is currently under the scanner.

        This never moves the carousel. It is the operator asserting a
        fact about the mechanism that the firmware cannot measure.
        """
        scan_slot = self.validate_slot(scan_slot)

        self.current_scan_slot = scan_slot
        self.position_valid = True
        self.selected_slot = self.load_slot_for(scan_slot)
        self.last_move = None

        return self.status()

    def sync_to_load_slot(self, load_slot):
        """
        Establish the origin from the LOADING hole.

        This is the normal synchronization path: the operator physically
        aligns Slot 1 with the soil loading hole and confirms it, which
        makes that position the carousel origin. Everything else follows
        from the fixed 180 deg scanner/loader relationship, so declaring
        Slot 1 at the loader also declares Slot 5 at the scanner.

        Nothing moves here; the carousel is already where the operator
        put it.
        """
        load_slot = self.validate_slot(load_slot)

        self.current_scan_slot = self.scan_slot_for_load(load_slot)
        self.position_valid = True
        self.selected_slot = load_slot
        self.last_move = None

        return self.status()

    def invalidate_position(self, reason):
        """Forget the tracked position; movement can no longer be trusted."""
        self.position_valid = False
        self.current_scan_slot = None
        self.selected_slot = None
        self.last_move = {"invalidated": reason}

    # ------------------------------------------------------------------
    # movement planning
    # ------------------------------------------------------------------

    def _forward_direction(self):
        direction = config.CAROUSEL_FORWARD_DIRECTION

        if direction not in mg995.DIRECTIONS:
            raise CarouselError(
                "CONFIG_ERROR",
                "CAROUSEL_FORWARD_DIRECTION must be 'cw' or 'ccw', "
                "not {}.".format(direction)
            )

        return direction

    def plan_move(self, target_scan_slot):
        """
        Work out the shortest path to bring target_scan_slot to the scanner.

        Returns (direction, steps). A tie at exactly half a turn resolves
        to the forward direction.
        """
        self.require_position()

        target_scan_slot = self.validate_slot(target_scan_slot)

        forward = self._forward_direction()
        reverse = mg995.opposite(forward)

        delta = (
            target_scan_slot - self.current_scan_slot
        ) % self.slot_count

        if delta == 0:
            return (forward, 0)

        if delta * 2 <= self.slot_count:
            return (forward, delta)

        return (reverse, self.slot_count - delta)

    def _apply_move(self, target_scan_slot):
        """Execute a planned move and update the tracked position."""
        direction, steps = self.plan_move(target_scan_slot)

        if steps == 0:
            self.last_move = {
                "target_scan_slot": target_scan_slot,
                "direction": None,
                "steps": 0,
                "moved": False
            }

            return self.last_move

        if self.servo is None:
            raise CarouselError(
                "SERVO_ERROR",
                "Servo driver is not initialized; cannot move the carousel."
            )

        try:
            self.servo.rotate_steps(direction, steps)

        except Exception as error:
            # Without an encoder a failed move means the position can no
            # longer be guaranteed. Refuse to guess.
            self.invalidate_position("movement failed")

            raise CarouselError(
                "MOVEMENT_FAILED",
                "Carousel movement failed ({}). Position tracking has been "
                "invalidated; re-synchronize before moving again.".format(
                    error
                )
            )

        self.current_scan_slot = target_scan_slot

        self.last_move = {
            "target_scan_slot": target_scan_slot,
            "direction": direction,
            "steps": steps,
            "moved": True
        }

        return self.last_move

    # ------------------------------------------------------------------
    # movement commands
    # ------------------------------------------------------------------

    def move_slot_to_scan(self, slot_id):
        """Bring the given slot under the AS7265x."""
        slot_id = self.validate_slot(slot_id)

        return self._apply_move(slot_id)

    def move_slot_to_load(self, slot_id):
        """
        Bring the given slot under the loading hole.

        Slot N at the loader means slot scan_slot_for_load(N) is at the
        scanner at the same time.
        """
        slot_id = self.validate_slot(slot_id)

        return self._apply_move(
            self.scan_slot_for_load(slot_id)
        )

    def half_turn(self, direction):
        """
        Swing the carousel 180 degrees between loader and scanner.

        Uses the servo's dedicated half-turn calibration, NOT four
        adjacent-slot moves. Four adjacent moves command roughly
        4 x 40 = 160 effective degrees, which does not reach the far
        position.

        Four slots is half of eight, so the tracked scanner slot moves by
        the same amount whichever way the carousel swings.
        """
        if self.servo is None:
            raise CarouselError(
                "SERVO_ERROR",
                "Servo driver is not initialized; cannot move."
            )

        self.require_position()

        try:
            result = self.servo.rotate_half_turn(direction)

        except Exception as error:
            self.invalidate_position("half turn failed")

            raise CarouselError(
                "MOVEMENT_FAILED",
                "Half-turn movement failed ({}). Position tracking has "
                "been invalidated; re-synchronize before "
                "continuing.".format(error)
            )

        self.current_scan_slot = (
            (self.current_scan_slot - 1 + self.offset) % self.slot_count
        ) + 1

        result["current_scan_slot"] = self.current_scan_slot
        result["current_load_slot"] = self.get_load_slot()
        result["selected_slot"] = self.selected_slot
        result["phase"] = self.phase()

        self.last_move = result

        return result

    def move_selected_to_scanner(self):
        """Bring the selected slot from the loading hole to the scanner."""
        forward = self._forward_direction()

        return self.half_turn(forward)

    def return_selected_to_loader(self):
        """
        Swing back so the selected slot faces the loading hole again.

        Reversed direction, with its own calibration, so backlash from
        the outbound sweep is taken up rather than accumulated.
        """
        reverse = mg995.opposite(self._forward_direction())

        return self.half_turn(reverse)

    def select_slot(self, slot_id):
        """
        Choose the physical slot to work with and bring it to the loader.

        If the previous sample is still sitting at the SCANNER - which is
        where measure_sample leaves it - the loading orientation is
        restored first with the calibrated half turn, and only then does
        the carousel step to the requested slot. The operator never has
        to run the 180 degree return by hand.

        Sequential progression 1 -> 2 -> 3 ... is then one clockwise slot
        transition, because plan_move resolves any delta of four slots or
        fewer in the forward direction. Selecting a distant slot still
        takes the shortest path.
        """
        slot_id = self.validate_slot(slot_id)

        self.require_position()

        restored = None

        # Choosing the slot we are already working with must not produce
        # a pointless movement; just report where it physically is.
        if slot_id == self.selected_slot:
            return {
                "selected_slot": slot_id,
                "direction": None,
                "steps": 0,
                "moved": False,
                "restored_load_orientation": False,
                "phase": self.phase(),
                "message": (
                    "Slot {} is already selected and currently at the "
                    "{} position.".format(slot_id, self.phase())
                )
            }

        if (
            config.AUTO_RESTORE_LOAD_ON_SELECT
            and self.phase() == "SCAN"
        ):
            restored = self.return_selected_to_loader()

        move = self._apply_move(self.scan_slot_for_load(slot_id))

        self.selected_slot = slot_id

        move["selected_slot"] = slot_id
        move["restored_load_orientation"] = restored is not None
        move["restore"] = restored
        move["phase"] = self.phase()

        return move

    def fine_adjust(self, degrees):
        """
        Small mechanical correction, in degrees.

        This is alignment only: it deliberately does NOT change the
        logical slot numbering. A carousel nudged by +1.5 deg is still
        sitting on the same logical slot, so current_scan_slot,
        selected_slot and position_valid are all left untouched.

        Larger movements must go through whole-slot commands, which is
        enforced by the caller against config.MAX_FINE_ADJUST_DEG.
        """
        if self.servo is None:
            raise CarouselError(
                "SERVO_ERROR",
                "Servo driver is not initialized; cannot adjust."
            )

        try:
            degrees = float(degrees)

        except (TypeError, ValueError):
            raise CarouselError(
                "BAD_REQUEST",
                "Fine adjustment must be a number of degrees."
            )

        try:
            result = self.servo.rotate_degrees(degrees)

        except Exception as error:
            # An interrupted fine adjustment leaves the mechanism at an
            # unknown angle, so tracking can no longer be trusted.
            self.invalidate_position("fine adjustment failed")

            raise CarouselError(
                "MOVEMENT_FAILED",
                "Fine adjustment failed ({}). Position tracking has been "
                "invalidated.".format(error)
            )

        self.last_move = {
            "fine_adjust_deg": result.get("degrees"),
            "direction": result.get("direction"),
            "duration_ms": result.get("duration_ms"),
            "moved": result.get("moved"),
            "logical_position_changed": False
        }

        result["current_scan_slot"] = self.current_scan_slot
        result["current_load_slot"] = self.get_load_slot()
        result["selected_slot"] = self.selected_slot
        result["logical_position_changed"] = False

        return result

    def move_slots(self, direction, slots):
        """
        Whole-slot movement, used during synchronization and maintenance.

        Each slot is exactly 45 deg. Before synchronization this simply
        turns the carousel so the operator can line Slot 1 up with the
        loading hole; afterwards it keeps the tracked position and the
        selected slot in step.
        """
        move = self.jog_steps(direction, slots)

        if self.position_valid and self.current_scan_slot is not None:
            self.selected_slot = self.get_load_slot()

        move["selected_slot"] = self.selected_slot

        return move

    def jog_steps(self, direction, steps):
        """
        Manual jog by whole 45 degree slots.

        This is a known movement, so the tracked position survives it.
        If the position was already unknown it simply stays unknown.
        """
        if self.servo is None:
            raise CarouselError(
                "SERVO_ERROR",
                "Servo driver is not initialized; cannot move the carousel."
            )

        try:
            steps = int(steps)

        except (TypeError, ValueError):
            raise CarouselError(
                "BAD_REQUEST",
                "Jog step count must be an integer."
            )

        if steps < 0:
            raise CarouselError(
                "BAD_REQUEST",
                "Jog step count must not be negative."
            )

        forward = self._forward_direction()

        try:
            self.servo.rotate_steps(direction, steps)

        except Exception as error:
            self.invalidate_position("jog failed")

            raise CarouselError(
                "MOVEMENT_FAILED",
                "Carousel jog failed ({}). Position tracking has been "
                "invalidated.".format(error)
            )

        if self.position_valid and self.current_scan_slot is not None:
            if direction == forward:
                shift = steps
            else:
                shift = -steps

            self.current_scan_slot = (
                (self.current_scan_slot - 1 + shift) % self.slot_count
            ) + 1

        self.last_move = {
            "direction": direction,
            "steps": steps,
            "moved": steps > 0,
            "tracked": self.position_valid
        }

        return self.last_move

    def jog_timed(self, direction, duration_ms):
        """
        Free manual jog for an arbitrary duration.

        The resulting angle is unknown, so the tracked position is
        always invalidated afterwards.
        """
        if self.servo is None:
            raise CarouselError(
                "SERVO_ERROR",
                "Servo driver is not initialized; cannot move the carousel."
            )

        # The angle of a free jog is unknown the moment it starts, so the
        # position is dropped before the servo is touched rather than
        # after it succeeds.
        self.invalidate_position("free jog of unknown angle")

        try:
            self.servo.rotate_for_ms(direction, duration_ms)

        except Exception as error:
            raise CarouselError(
                "MOVEMENT_FAILED",
                "Carousel jog failed ({}). Position tracking has been "
                "invalidated.".format(error)
            )

        self.last_move = {
            "direction": direction,
            "duration_ms": int(duration_ms),
            "moved": True,
            "tracked": False
        }

        return self.last_move

    def stop(self):
        """Brake the servo and release the pin."""
        if self.servo is None:
            raise CarouselError(
                "SERVO_ERROR",
                "Servo driver is not initialized."
            )

        self.servo.stop()
        self.servo.release()

        return {"stopped": True}

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------

    def status(self):
        return {
            "position_valid": self.position_valid,
            "current_scan_slot": self.current_scan_slot,
            "current_load_slot": self.get_load_slot(),
            "selected_slot": self.selected_slot,
            "carousel_phase": self.phase(),
            "slot_count": self.slot_count,
            "scan_load_offset_slots": self.offset,
            "forward_direction": config.CAROUSEL_FORWARD_DIRECTION,
            "max_fine_adjust_deg": config.MAX_FINE_ADJUST_DEG,

            # Physical geometry, kept explicitly apart from the servo
            # command calibration below it.
            "geometry": {
                "slot_spacing_deg": config.CAROUSEL_SLOT_GEOMETRY_DEG,
                "half_turn_deg": config.CAROUSEL_HALF_TURN_DEG
            },
            "movement_calibration": {
                "slot_step_deg": config.SLOT_STEP_DEG,
                "next_slot_cw_ms": config.NEXT_SLOT_CW_MS,
                "next_slot_ccw_ms": config.NEXT_SLOT_CCW_MS,
                "load_to_scan_cw_ms": config.LOAD_TO_SCAN_CW_MS,
                "scan_to_load_ccw_ms": config.SCAN_TO_LOAD_CCW_MS
            },

            # Historical field name for the physical spacing.
            "degrees_per_slot": config.CAROUSEL_SLOT_GEOMETRY_DEG,
            "last_move": self.last_move
        }
