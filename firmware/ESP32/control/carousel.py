# control/carousel.py
# Software model of the 4-slot sample carousel.
#
# This module owns GEOMETRY and LOGICAL POSITION. It does not own the
# actuator: it asks whichever backend is fitted to "move two slots
# clockwise" or "swing half a turn", and the backend decides whether that
# means a calibrated pulse train or a verified encoder step.
#
# There is deliberately no PWM here, no encoder counts, no packet, no
# register and no timing constant. If any of those appear in this file
# again, the abstraction has leaked.
#
#     Carousel  ->  ST3215  (UART, encoder, closed loop)
#
# The carousel asks the driver for MEASURED travel and never assumes it
# got it. A driver that cannot measure answers None, and the carousel
# reports drift as unmeasurable rather than as zero - which is why the
# question is asked at all rather than the answer being taken on
# faith.
#
# THE ORIGIN. The carousel has no limit switch, no Hall sensor and no
# physical index, so nothing can tell the firmware which physical slot the
# operator calls Slot 1. That is declared once, by hand, through
# sync_position - which never moves anything. The declaration also
# captures the ST3215's encoder reading, tying the logical model to a
# real measurement rather than to a count that drifts.
#
# Two fixed mechanical positions exist, exactly 180 degrees apart:
#
#     SCAN  - the slot under the AS7265x
#     LOAD  - the slot under the loading hole
#
# 180 degrees is 2 of the 4 slots, so:
#
#     load_slot = ((scan_slot - 1 + 2) % 4) + 1
#
# which for scan_slot = 3 gives load_slot = 1, and is its own inverse:
# putting Slot 1 under the loader necessarily puts Slot 3 under the
# scanner. Both the slot count and the offset come from config.py.
#
# Physical slot occupancy and the tracked position live in RAM only and
# are intentionally forgotten on reset.
#
# This is deliberately NOT a sample lifecycle. The ESP32 knows only
# whether a slot physically holds soil and which Sample ID the PC attached
# to it, so that clear_slot has something to free and the operator can see
# the real carousel contents. EMPTY / READY_TO_LOAD / LOADED / MEASURED
# are scientific states and belong to the PC.

import config

from drivers import servo_base
from drivers.servo_base import CCW, CW, ServoError


class CarouselError(Exception):
    """Movement or state error carrying a machine-readable code."""

    def __init__(self, code, message, data=None):
        # super() - MicroPython cannot call Exception.__init__ unbound.
        super().__init__(message)

        self.code = str(code)
        self.message = str(message)
        self.data = data


def new_slot(slot_id):
    """Runtime record for one empty physical slot."""
    return {
        "slot_id": slot_id,
        "occupied": False,
        "sample_id": None,

        # Last raw acquisition taken from this slot, so the PC can pull
        # back a measurement it lost. RAM only, cleared with the slot.
        "measurement": None
    }


class Carousel:
    """Runtime state of the physical carousel."""

    def __init__(self, servo=None):
        self.servo = servo

        self.slot_count = config.CAROUSEL_SLOT_COUNT
        self.offset = config.CAROUSEL_SCAN_LOAD_OFFSET
        self.slot_step_deg = config.CAROUSEL_SLOT_GEOMETRY_DEG
        self.half_turn_deg = config.CAROUSEL_HALF_TURN_DEG

        # Geometry is checked once, here, rather than trusted. It is not
        # raised from the constructor on purpose: a firmware that refuses
        # to boot also refuses to run diagnostics, and the operator would
        # be left with a dead module and no explanation. Movement is
        # blocked instead, and status() says why.
        self.geometry_error = self._check_geometry()

        # Physical occupancy: RAM only, reset on every reboot.
        self.slots = {}

        for slot_id in range(1, self.slot_count + 1):
            self.slots[slot_id] = new_slot(slot_id)

        self._reset_position()

    def _reset_position(self):
        # Position tracking: unknown until the operator synchronizes.
        self.position_valid = False
        self.current_scan_slot = None

        # The physical slot the operator is currently working with.
        #
        # Deliberately separate from "slot under the loader" and "slot
        # under the scanner": while soil is being deposited the selected
        # slot sits at the loader, but after a measurement the carousel
        # has turned 180 deg and the same selected slot sits at the
        # scanner instead.
        self.selected_slot = None

        # What the backend recorded when the operator confirmed the
        # alignment. Its shape is backend-specific; the carousel only
        # reports it.
        self.origin = None
        self.origin_scan_slot = None

        # Accumulated fine alignment, in degrees. A correction to the
        # MECHANICAL reference, not to the logical slot numbering: a
        # carousel nudged by +2 deg must stay nudged, so this is carried
        # into the expected position from then on.
        self.alignment_offset_deg = 0.0

        # Signed angle the carousel has been COMMANDED to travel since the
        # origin was captured. Compared against the backend's measured
        # travel to expose drift - on a backend that can measure.
        self.commanded_travel_deg = 0.0

        self.last_move = None

    def _check_geometry(self):
        """
        Confirm the configured geometry is self-consistent.

        The loader/scanner offset has to be exactly half the slot count,
        or the loader/scanner mapping stops being its own inverse and a
        half turn no longer swaps them. Returns a description of the
        problem, or None when everything lines up.
        """
        if self.slot_count < 2:
            return "CAROUSEL_SLOT_COUNT must be at least 2."

        if self.offset * 2 != self.slot_count:
            return (
                "CAROUSEL_SCAN_LOAD_OFFSET ({}) must be exactly half of "
                "CAROUSEL_SLOT_COUNT ({}); the loader and the scanner sit "
                "180 degrees apart.".format(self.offset, self.slot_count)
            )

        expected_spacing = 360.0 / self.slot_count

        if abs(self.slot_step_deg - expected_spacing) > 1e-6:
            return (
                "CAROUSEL_SLOT_GEOMETRY_DEG ({}) must be 360 / "
                "CAROUSEL_SLOT_COUNT ({}).".format(
                    self.slot_step_deg, expected_spacing
                )
            )

        return None

    # ------------------------------------------------------------------
    # actuator attachment
    # ------------------------------------------------------------------

    def attach_servo(self, servo, reason="servo selected"):
        """
        Bind the carousel to an actuator backend, or to None.

        Every tracked position is dropped. That is not caution, it is
        correctness: physical position state cannot be carried across a
        change of actuator. A different servo has a different encoder
        zero, a different mounting and possibly a different mechanical
        reference, so the origin, the alignment offset and the slot
        mapping all become meaningless the moment the hardware changes.
        """
        self.servo = servo

        self._reset_position()
        self.last_move = {"invalidated": reason}

        return {
            "attached": servo.name if servo is not None else None,
            "position_valid": False,
            "reason": reason,
            "message": "Carousel position was invalidated by the servo "
                       "change. Re-synchronize before moving.",
        }

    def capabilities(self):
        if self.servo is None:
            return None

        return self.servo.capabilities()

    def has_feedback(self):
        capabilities = self.capabilities()

        return bool(capabilities and capabilities.get("position_feedback"))

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
                    slot_id, self.slot_count
                )
            )

        return slot_id

    def slot(self, slot_id):
        return self.slots[self.validate_slot(slot_id)]

    def slot_list(self):
        """Every runtime slot, ordered by slot number."""
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

    def reset_all_slots(self):
        """
        Free every physical slot at once.

        Runtime occupancy only, exactly like reset_slot repeated: the
        carousel is not moved, the tracked position is untouched, and no
        saved Sample record anywhere is affected.
        """
        cleared = []

        for slot_id in range(1, self.slot_count + 1):
            slot = self.slots[slot_id]

            if slot["occupied"] or slot["sample_id"] or slot["measurement"]:
                cleared.append({
                    "slot_id": slot_id,
                    "sample_id": slot["sample_id"],
                })

            self.reset_slot(slot_id)

        return cleared

    def clear_retained_samples(self):
        """
        Drop every retained acquisition, keeping physical slot state.

        Deleting saved measurements and freeing the mechanism are
        different things: soil may still physically sit in a slot whose
        record has been exported and deleted, so `occupied` and
        `sample_id` are deliberately left alone here.
        """
        cleared = []

        for slot in self.slot_list():
            if slot.get("measurement") is not None:
                cleared.append(slot.get("sample_id"))
                slot["measurement"] = None

        return cleared

    def mark_occupied(self, slot_id, sample_id=None, measurement=None):
        """
        Record that a slot physically holds soil.

        Set after a successful acquisition: the soil really is in the
        slot, and stays there until clear_slot. The Sample ID is carried
        only so a response can be correlated with the PC's record.

        `measurement` is the raw acquisition kept in RAM so the PC can
        pull it back later if it lost the response. It is stored, never
        interpreted - the ESP32 still performs no science.
        """
        slot_id = self.validate_slot(slot_id)
        slot = self.slots[slot_id]

        slot["occupied"] = True

        if sample_id is not None:
            slot["sample_id"] = sample_id

        if measurement is not None and config.RETAIN_LAST_SPECTRUM:
            slot["measurement"] = measurement

        return slot

    def slot_summary(self):
        """
        Slot state without the retained spectra.

        get_status is polled on every screen refresh, and a full raw
        acquisition per slot would add kilobytes to each poll. The spectra
        are fetched deliberately, with get_saved_sample.
        """
        return [
            {
                "slot_id": slot["slot_id"],
                "occupied": slot["occupied"],
                "sample_id": slot["sample_id"],
                "has_measurement": slot.get("measurement") is not None,
            }
            for slot in self.slot_list()
        ]

    def retained_samples(self):
        """
        Every slot holding a raw acquisition.

        Keyed on the measurement ALONE. It used to also require a Sample
        ID, which meant a measurement taken without one was held in RAM
        but never listed - so the delete screen saw an empty index,
        reported "already empty" and returned without deleting anything,
        while the data was still there. Anything stored must be visible.
        """
        return [
            slot for slot in self.slot_list()
            if slot.get("measurement") is not None
        ]

    def retained_sample(self, sample_id):
        """
        One retained acquisition by Sample ID.

        Also accepts the SLOTn placeholder the index reports for a
        measurement that was taken without a Sample ID, so everything
        listed can actually be fetched.
        """
        for slot in self.slot_list():
            if slot.get("measurement") is None:
                continue

            if slot.get("sample_id") == sample_id:
                return slot

            if sample_id == "SLOT{}".format(slot["slot_id"]):
                return slot

        return None

    # ------------------------------------------------------------------
    # geometry
    # ------------------------------------------------------------------

    def load_slot_for(self, scan_slot):
        """Slot sitting at the loader while scan_slot is at the scanner."""
        return ((scan_slot - 1 + self.offset) % self.slot_count) + 1

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

        Derived rather than stored, so it can never drift out of step with
        the tracked position:

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

    def expected_travel_deg(self):
        """
        Angle the carousel should have travelled since the origin.

        Commanded slot travel plus every fine alignment applied since,
        reduced to (-180, 180] so it can be compared with a measurement
        on a rotary axis.
        """
        return servo_base.centred_degrees(
            self.commanded_travel_deg + self.alignment_offset_deg
        )

    def drift_deg(self):
        """
        Measured minus expected angle since the origin, or None.

        None on an open-loop actuator - which is the honest answer, not
        zero. This is the number that shows accumulated mechanical error
        over a long session on a servo that can actually measure it.
        """
        if self.servo is None or not self.position_valid:
            return None

        measured = self.servo.travel_since_origin_deg()

        if measured is None:
            return None

        return round(
            servo_base.centred_degrees(measured - self.expected_travel_deg()),
            3,
        )

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

    def _require_servo(self):
        """
        The gate every movement passes through.

        SERVO_NOT_CONNECTED is a distinct code from SERVO_ERROR on
        purpose: one means "open the link to the servo", the other means
        "the servo is connected and it failed".
        """
        if self.servo is None:
            raise CarouselError(
                "SERVO_NOT_CONNECTED",
                "The carousel servo is not connected. Run option [0] "
                "Carousel Setup first."
            )

        if self.geometry_error:
            raise CarouselError("CONFIG_ERROR", self.geometry_error)

        return self.servo

    def _safe_position_report(self):
        """
        Whatever the backend can still tell us after a failure.

        Used only when something has already gone wrong. An open-loop
        backend answers None, which is itself the answer: the position is
        unknown and must be re-established by hand.
        """
        if self.servo is None:
            return None

        try:
            return self.servo.travel_since_origin_deg()

        except Exception:
            return None

    def sync_position(self, scan_slot):
        """
        Declare which physical slot is currently under the scanner.

        This never moves the carousel. The operator is asserting the one
        fact the firmware cannot measure - which physical slot is called
        Slot 1 - and the backend records whatever reference it can.

        Any accumulated fine alignment is folded into the new origin and
        reset: the position the operator has just confirmed IS the
        reference from now on.
        """
        servo = self._require_servo()

        scan_slot = self.validate_slot(scan_slot)

        try:
            origin = servo.capture_origin()

        except ServoError as error:
            raise CarouselError(
                error.code,
                "Cannot synchronize: the servo could not establish a "
                "reference ({}). Nothing was recorded, because an origin "
                "nobody checked would poison every later "
                "movement.".format(error.message),
            )

        self.origin = origin
        self.origin_scan_slot = scan_slot
        self.alignment_offset_deg = 0.0
        self.commanded_travel_deg = 0.0

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
        Slot 1 at the loader also declares Slot 3 at the scanner.

        Nothing moves here; the carousel is already where the operator put
        it.
        """
        load_slot = self.validate_slot(load_slot)

        return self.sync_position(self.scan_slot_for_load(load_slot))

    def invalidate_position(self, reason):
        """Forget the tracked position; movement can no longer be trusted."""
        self.position_valid = False
        self.current_scan_slot = None
        self.selected_slot = None
        self.origin = None
        self.origin_scan_slot = None
        self.last_move = {"invalidated": reason}

    # ------------------------------------------------------------------
    # movement planning
    # ------------------------------------------------------------------

    def _forward_direction(self):
        direction = config.CAROUSEL_FORWARD_DIRECTION

        if direction not in servo_base.DIRECTIONS:
            raise CarouselError(
                "CONFIG_ERROR",
                "CAROUSEL_FORWARD_DIRECTION must be 'cw' or 'ccw', not "
                "{}.".format(direction)
            )

        return direction

    def plan_move(self, target_scan_slot):
        """
        Work out the shortest path to bring target_scan_slot to the scanner.

        Returns (direction, slots). A tie at exactly half a turn resolves
        to the forward direction.

        Shortest path is decided in SLOTS, before the request ever reaches
        an actuator. That is what keeps Slot 4 -> Slot 1 one slot backwards
        rather than three slots forwards, whichever backend is fitted and
        wherever an absolute encoder happens to be reading.
        """
        self.require_position()

        target_scan_slot = self.validate_slot(target_scan_slot)

        forward = self._forward_direction()
        reverse = servo_base.opposite(forward)

        delta = (target_scan_slot - self.current_scan_slot) % self.slot_count

        if delta == 0:
            return (forward, 0)

        if delta * 2 <= self.slot_count:
            return (forward, delta)

        return (reverse, self.slot_count - delta)

    # ------------------------------------------------------------------
    # movement execution
    # ------------------------------------------------------------------

    def _movement_failed(self, error, reason, requested):
        """
        Turn a backend failure into an invalidated position and a report.

        The firmware never falls back to assuming the movement worked, and
        it never falls back from one backend's failure to the other
        backend's method. Losing position is a fault to be reported.
        """
        measured = self._safe_position_report()

        self.invalidate_position(reason)

        return CarouselError(
            error.code,
            "{} failed ({}). Position tracking has been invalidated; "
            "re-synchronize before moving again.".format(
                reason, error.message
            ),
            data={
                "requested": requested,
                "measured_travel_deg": measured,
                "position_valid": False,
                "position_measurable": measured is not None,
            },
        )

    def _run_slots(self, direction, slots, reason):
        servo = self._require_servo()

        try:
            return servo.move_slots(direction, slots)

        except ServoError as error:
            raise self._movement_failed(
                error, reason, {"direction": direction, "slots": slots}
            )

    def _run_half_turn(self, direction, reason):
        servo = self._require_servo()

        try:
            return servo.half_turn(direction)

        except ServoError as error:
            raise self._movement_failed(
                error, reason, {"direction": direction, "half_turn": True}
            )

    def _run_degrees(self, degrees, reason):
        servo = self._require_servo()

        try:
            return servo.move_degrees(degrees)

        except ServoError as error:
            raise self._movement_failed(
                error, reason, {"degrees": degrees}
            )

    def _apply_move(self, target_scan_slot):
        """Execute a planned move and update the tracked position."""
        self._require_servo()

        direction, slots = self.plan_move(target_scan_slot)

        if slots == 0:
            self.last_move = {
                "target_scan_slot": target_scan_slot,
                "direction": None,
                "steps": 0,
                "moved": False,
            }

            return self.last_move

        movement = self._run_slots(
            direction, slots,
            "Carousel movement to slot {}".format(target_scan_slot),
        )

        self.current_scan_slot = target_scan_slot
        self._account_travel(direction, slots * self.slot_step_deg)

        self.last_move = {
            "target_scan_slot": target_scan_slot,
            "direction": direction,
            "steps": slots,
            "degrees": slots * self.slot_step_deg,
            "moved": True,
            "servo": movement,
        }

        return self.last_move

    def _account_travel(self, direction, degrees):
        """Add a completed movement to the commanded-travel accumulator."""
        self.commanded_travel_deg += (
            servo_base.direction_sign(direction) * float(degrees)
        )

    # ------------------------------------------------------------------
    # movement commands
    # ------------------------------------------------------------------

    def move_slot_to_scan(self, slot_id):
        """Bring the given slot under the AS7265x."""
        return self._apply_move(self.validate_slot(slot_id))

    def move_slot_to_load(self, slot_id):
        """
        Bring the given slot under the loading hole.

        Slot N at the loader means slot scan_slot_for_load(N) is at the
        scanner at the same time.
        """
        slot_id = self.validate_slot(slot_id)

        return self._apply_move(self.scan_slot_for_load(slot_id))

    def half_turn(self, direction):
        """
        Swing the carousel 180 degrees between loader and scanner.

        A distinct request rather than "move offset slots": half a turn
        is a fixed mechanical relationship between the loader and the
        scanner, and the ST3215 travels it as one verified movement.

        The offset is half the slot count, so the tracked scanner slot
        moves by the same amount whichever way the carousel swings.
        """
        self._require_servo()

        if direction not in servo_base.DIRECTIONS:
            raise CarouselError(
                "BAD_REQUEST",
                "direction must be 'cw' or 'ccw', not {}.".format(direction)
            )

        self.require_position()

        movement = self._run_half_turn(
            direction, "Carousel half turn ({})".format(direction)
        )

        self.current_scan_slot = (
            (self.current_scan_slot - 1 + self.offset) % self.slot_count
        ) + 1

        self._account_travel(direction, self.half_turn_deg)

        result = {
            "direction": direction,
            "degrees": self.half_turn_deg,
            "moved": True,
            "servo": movement,
            "current_scan_slot": self.current_scan_slot,
            "current_load_slot": self.get_load_slot(),
            "selected_slot": self.selected_slot,
            "phase": self.phase(),
        }

        self.last_move = result

        return result

    def move_selected_to_scanner(self):
        """Bring the selected slot from the loading hole to the scanner."""
        return self.half_turn(self._forward_direction())

    def return_selected_to_loader(self):
        """
        Swing back so the selected slot faces the loading hole again.

        Reversed direction, so backlash from the outbound sweep is taken
        up rather than accumulated. Both halves are the same verified
        travel, so the pair closes on itself by construction.
        """
        return self.half_turn(servo_base.opposite(self._forward_direction()))

    def select_slot(self, slot_id):
        """
        Choose the physical slot to work with and bring it to the loader.

        A measurement swings the sample back to the loading position by
        itself, so normally there is nothing to restore. If the carousel is
        nonetheless found at SCAN - an interrupted return, say - the
        loading orientation is restored first, and only then does the
        carousel step to the requested slot.

        Sequential progression 1 -> 2 -> 3 -> 4 is then one forward slot
        transition, because plan_move resolves any delta of half the slot
        count or fewer in the forward direction. Selecting a distant slot
        still takes the shortest path.
        """
        slot_id = self.validate_slot(slot_id)

        self._require_servo()
        self.require_position()

        restored = None

        # Choosing the slot we are already working with must not produce a
        # pointless movement; just report where it physically is.
        if slot_id == self.selected_slot:
            return {
                "selected_slot": slot_id,
                "direction": None,
                "steps": 0,
                "moved": False,
                "restored_load_orientation": False,
                "phase": self.phase(),
                "message": (
                    "Slot {} is already selected and currently at the {} "
                    "position.".format(slot_id, self.phase())
                )
            }

        if config.AUTO_RESTORE_LOAD_ON_SELECT and self.phase() == "SCAN":
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

        This is alignment only: it deliberately does NOT change the logical
        slot numbering. A carousel nudged by +1.5 deg is still sitting on
        the same logical slot, so current_scan_slot, selected_slot and
        position_valid are all left untouched.

        The nudge IS remembered. It accumulates into
        alignment_offset_deg, which is part of the expected position from
        then on, so the next slot movement keeps the correction instead of
        quietly undoing it.

        Larger movements must go through whole-slot commands, which the
        caller enforces against config.MAX_FINE_ADJUST_DEG.
        """
        self._require_servo()

        try:
            degrees = float(degrees)

        except (TypeError, ValueError):
            raise CarouselError(
                "BAD_REQUEST", "Fine adjustment must be a number of degrees."
            )

        movement = self._run_degrees(
            degrees, "Fine alignment of {:+.2f} deg".format(degrees)
        )

        result = dict(movement)

        if movement.get("moved"):
            # A backend that quantizes the request - the ST3215 rounds to
            # whole encoder counts - reports what it actually commanded.
            # Accumulating that instead of the request keeps quantization
            # out of the drift figure, where it would look like mechanical
            # error that no adjustment could remove.
            applied = movement.get("commanded_degrees", degrees)

            self.alignment_offset_deg = servo_base.centred_degrees(
                self.alignment_offset_deg + applied
            )

        result["requested_degrees"] = degrees
        result["logical_position_changed"] = False
        result["alignment_offset_deg"] = round(self.alignment_offset_deg, 3)
        result["current_scan_slot"] = self.current_scan_slot
        result["current_load_slot"] = self.get_load_slot()
        result["selected_slot"] = self.selected_slot
        result["drift_deg"] = self.drift_deg()

        self.last_move = {
            "fine_adjust_deg": degrees,
            "moved": movement.get("moved"),
            "logical_position_changed": False,
        }

        return result

    def move_slots(self, direction, slots):
        """
        Whole-slot movement, used during synchronization and maintenance.

        Before synchronization this simply turns the carousel so the
        operator can line Slot 1 up with the loading hole; afterwards it
        keeps the tracked position and the selected slot in step.
        """
        move = self.jog_slots(direction, slots)

        if self.position_valid and self.current_scan_slot is not None:
            self.selected_slot = self.get_load_slot()

        move["selected_slot"] = self.selected_slot

        return move

    def jog_slots(self, direction, slots):
        """
        Manual jog by whole slots.

        This is a known movement, so the tracked position survives it. If
        the position was already unknown it simply stays unknown - which is
        the case during synchronization, before an origin exists.
        """
        self._require_servo()

        if direction not in servo_base.DIRECTIONS:
            raise CarouselError(
                "BAD_REQUEST",
                "direction must be 'cw' or 'ccw', not {}.".format(direction)
            )

        try:
            slots = int(slots)

        except (TypeError, ValueError):
            raise CarouselError(
                "BAD_REQUEST", "Jog slot count must be an integer."
            )

        if slots < 0:
            raise CarouselError(
                "BAD_REQUEST", "Jog slot count must not be negative."
            )

        movement = self._run_slots(
            direction, slots,
            "Carousel jog of {} slot(s) {}".format(slots, direction),
        )

        forward = self._forward_direction()
        shift = slots if direction == forward else -slots

        if self.position_valid and self.current_scan_slot is not None:
            self.current_scan_slot = (
                (self.current_scan_slot - 1 + shift) % self.slot_count
            ) + 1

        if slots:
            self._account_travel(direction, slots * self.slot_step_deg)

        self.last_move = {
            "direction": direction,
            "steps": slots,
            "degrees": slots * self.slot_step_deg,
            "moved": slots > 0,
            "tracked": self.position_valid,
            "servo": movement,
        }

        return self.last_move

    def stop(self):
        """
        Stop the actuator where it is.

        The tracked position is dropped: an aborted movement ended
        somewhere between two slot centres, and pretending otherwise is
        exactly the kind of guess this firmware is not allowed to make.
        """
        servo = self._require_servo()

        try:
            result = servo.stop()

        except ServoError as error:
            self.invalidate_position("stop command failed")

            raise CarouselError(
                error.code,
                "Could not stop the servo ({}).".format(error.message),
            )

        self.invalidate_position("movement stopped by the operator")

        result["position_valid"] = False
        result["message"] = (
            "Servo stopped. The carousel may be between slots, so "
            "re-synchronize before the next movement."
        )

        return result

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------

    def status(self):
        drift = self.drift_deg()

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
            "servo_attached": self.servo.name if self.servo else None,
            "capabilities": self.capabilities(),

            # Physical geometry. Never calibrated away, never per-backend.
            "geometry": {
                "slot_spacing_deg": self.slot_step_deg,
                "half_turn_deg": self.half_turn_deg,
                "error": self.geometry_error,
            },

            # The position reference, in backend-neutral terms. drift_deg
            # is None on an actuator that cannot measure, which is the
            # honest answer rather than a fabricated zero.
            "reference": {
                "origin": self.origin,
                "origin_scan_slot": self.origin_scan_slot,
                "alignment_offset_deg": round(self.alignment_offset_deg, 3),
                "commanded_travel_deg": round(self.commanded_travel_deg, 3),
                "expected_travel_deg": round(self.expected_travel_deg(), 3),
                "drift_deg": drift,
                "drift_measurable": self.has_feedback(),
            },

            # Historical field name for the physical spacing.
            "degrees_per_slot": self.slot_step_deg,
            "last_move": self.last_move,
        }
