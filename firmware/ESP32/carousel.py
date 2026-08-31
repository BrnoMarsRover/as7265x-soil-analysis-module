# carousel.py
# Software model of the 4-slot sample carousel.
#
# This module owns GEOMETRY and LOGICAL POSITION. It does not own the
# actuator: it asks the ST3215 to "move two slots clockwise" or
# "swing half a turn", and servo.py decides what that means in
# encoder counts.
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
import retention

from servo import (
    DIRECTIONS,
    ServoError,
    centred_degrees,
    direction_sign,
    opposite,
)


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
        # back a measurement it lost. Mirrored to the device filesystem
        # by `retention`, so it also survives a reset of this board -
        # see mark_occupied. Kept when the slot is cleared, because
        # emptying a cup and deleting a spectrum are different things.
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

        # Physical occupancy: RAM only, reset on every reboot. The
        # firmware cannot know whether soil is still sitting in a cup
        # after a power cut, and inventing an answer would be worse
        # than admitting it does not know.
        self.slots = {}

        for slot_id in range(1, self.slot_count + 1):
            self.slots[slot_id] = new_slot(slot_id)

        # THE ACQUISITIONS ARE A DIFFERENT MATTER. A spectrum that was
        # measured really was measured, and a reboot does not make that
        # untrue, so the retained acquisitions come back from the
        # device filesystem. The slot they came from is NOT marked
        # occupied by this: the record is restored, the claim about the
        # physical world is not.
        self.retention_error = None

        try:
            restored = retention.load_all()

        except Exception:                              # pragma: no cover
            # `load_all` is written not to raise. If it ever does, the
            # module still has to boot - a carousel that will not
            # construct takes every diagnostic down with it.
            restored = {}
            self.retention_error = "retained acquisitions could not be read"

        for slot_id, measurement in restored.items():
            if slot_id in self.slots:
                self.slots[slot_id]["measurement"] = measurement

        self.restored_acquisitions = sorted(restored.keys())

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

        # Why the tracked position is believed but unproven, or None
        # when it was confirmed by measurement. A movement whose
        # confirming read was lost sets this; it does NOT invalidate the
        # position, because a lost status packet is not a mechanical
        # failure. Cleared by the next successfully verified movement or
        # by a re-sync.
        self.position_unverified_reason = None

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

    def has_feedback(self):
        """
        Whether drift can be MEASURED rather than assumed.

        True exactly when a servo is attached: the ST3215 has an
        absolute encoder and there is no other actuator. It stays a
        question rather than an assumption because the carousel is
        built with no servo at all, and reporting drift of 0.0 for a
        carousel nothing is driving would be a measurement that never
        happened.
        """
        return self.servo is not None

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
        Free a physical slot, KEEPING the acquisition taken from it.

        THE DEFECT THIS FIXES.

        This used to replace the slot with a blank one, which dropped
        `measurement` along with the occupancy - so clearing a slot to
        load the next sample silently destroyed the device's copy of
        the spectrum that had just been taken from it. Both screens
        that call it say the opposite in as many words: "This frees the
        mechanism only" and "Clearing a physical slot is a different
        thing and keeps the record".

        The visible consequence was on the PC. An operator measured,
        cleared the slot, then opened Delete ALL ESP32 Samples - and
        got "ESP32 Sample storage is already empty" over a device that
        had held the acquisition thirty seconds earlier, while the
        Sample Database in front of them still listed the sample. The
        delete looked broken. Nothing was broken: the data had been
        thrown away by a command that promised not to.

        Emptying a cup and deleting a measurement are different
        operations with different consequences, and the firmware
        already has a separate command for the second one
        (`clear_retained_samples`, reached by delete_saved_samples).
        This one no longer does both.
        """
        slot_id = self.validate_slot(slot_id)

        retained = self.slots[slot_id].get("measurement")

        self.slots[slot_id] = new_slot(slot_id)
        self.slots[slot_id]["measurement"] = retained

        return self.slots[slot_id]

    def reset_all_slots(self):
        """
        Free every physical slot at once.

        Runtime occupancy only, exactly like reset_slot repeated: the
        carousel is not moved, the tracked position is untouched, the
        retained acquisitions are kept, and no saved Sample record
        anywhere is affected.
        """
        cleared = []

        for slot_id in range(1, self.slot_count + 1):
            slot = self.slots[slot_id]

            if slot["occupied"] or slot["sample_id"]:
                cleared.append({
                    "slot_id": slot_id,
                    "sample_id": slot["sample_id"],
                    "acquisition_kept": slot["measurement"] is not None,
                })

            self.reset_slot(slot_id)

        return cleared

    def drop_retained_sample(self, sample_id):
        """
        Delete ONE retained acquisition, keeping the physical slot state.

        Per-sample rather than all-or-nothing, because the PC's Sample
        Database offers a per-sample delete that names the ESP32 as its
        target, and an operator who wants one device copy gone should
        not have to destroy the other three to get it.

        Returns the slot it was dropped from, or None if no retained
        acquisition answers to that id. `occupied` and `sample_id` are
        deliberately untouched: soil can still be sitting in the slot.
        """
        slot = self.retained_sample(sample_id)

        if slot is None:
            return None

        slot["measurement"] = None

        # The persisted copy goes with it. A delete that emptied RAM
        # and left the file behind would put the acquisition back on
        # the next reboot, which is the one thing a delete must never
        # do.
        retention.drop(slot["slot_id"])

        return slot

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
                cleared.append(self.retained_sample_id(slot))
                slot["measurement"] = None

        # Unconditionally, over every slot rather than only the ones
        # that had a RAM copy: a file whose record failed to load is
        # not in RAM and would otherwise survive "delete everything".
        retention.clear()

        return cleared

    def mark_occupied(self, slot_id, sample_id=None, measurement=None):
        """
        Record that a slot physically holds soil.

        Set after a successful acquisition: the soil really is in the
        slot, and stays there until clear_slot. The Sample ID is carried
        only so a response can be correlated with the PC's record.

        `measurement` is the raw acquisition kept so the PC can pull it
        back later if it lost the response. It is stored, never
        interpreted - the ESP32 still performs no science.

        THE ACQUISITION IS WRITTEN TO THE DEVICE FILESYSTEM HERE,
        because this is the moment it becomes the only copy that exists
        anywhere: the PC holds its working set in memory until the
        operator imports, so until then this board owns the science.

        A FAILED WRITE DOES NOT FAIL THE MEASUREMENT. The spectrum is
        already in RAM and already going back in the response;
        discarding it because the disk is full would destroy the data
        to protect the backup of it. `retention_error` records what
        went wrong and the measure response reports it, so an
        unprotected acquisition is never silently mistaken for a
        protected one.
        """
        slot_id = self.validate_slot(slot_id)
        slot = self.slots[slot_id]

        slot["occupied"] = True

        if sample_id is not None:
            slot["sample_id"] = sample_id

        if measurement is not None and config.RETAIN_LAST_SPECTRUM:
            slot["measurement"] = measurement
            self.retention_error = retention.save(slot_id, measurement)

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

    @staticmethod
    def retained_sample_id(slot):
        """
        Which Sample a retained acquisition is OF.

        FROM THE MEASUREMENT, NOT FROM THE SLOT. `slot["sample_id"]`
        answers a different question - which Sample is physically in
        this cup right now - and clearing the slot correctly empties it
        while the acquisition taken from it is still held. Reading the
        id from the slot therefore renamed a retained acquisition to
        the SLOTn placeholder the moment the cup was emptied, so the
        PC could no longer match the device's copy to its own record or
        delete it by name.

        The measurement has carried its own `sample_id` since it was
        taken, and that one cannot go stale.
        """
        if slot is None or slot.get("measurement") is None:
            return None

        return (
            (slot["measurement"] or {}).get("sample_id")
            or slot.get("sample_id")
        )

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

            if self.retained_sample_id(slot) == sample_id:
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
        return centred_degrees(
            self.commanded_travel_deg + self.alignment_offset_deg
        )

    def drift_deg(self, measured=None):
        """
        Measured minus expected angle since the origin, or None.

        None on an open-loop actuator - which is the honest answer, not
        zero. This is the number that shows accumulated mechanical error
        over a long session on a servo that can actually measure it.

        `measured` is the already-known travel, when the caller has one.
        """
        if self.servo is None or not self.position_valid:
            return None

        if measured is None:
            measured = self.servo.travel_since_origin_deg()

        if measured is None:
            return None

        return round(
            centred_degrees(measured - self.expected_travel_deg()),
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

        # A re-sync is the operator asserting a physical fact and the
        # servo measuring where that is, so the result is confirmed by
        # construction: the logical angle is 0.0 because the origin was
        # taken from the very reading it is subtracted from.
        self.position_unverified_reason = None

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
        """
        Forget the tracked position; movement can no longer be trusted.

        RESERVED FOR REAL UNCERTAINTY. This is the state that sends an
        operator to look at the plate and re-synchronize by hand, so it
        must not be the generic answer to any warning. A movement that
        completed without a confirming read is UNVERIFIED and keeps its
        position; only a movement whose measurement says the mechanism
        is not where it was sent, a servo that cannot be read at all, or
        a change of actuator gets here.
        """
        self.position_valid = False
        self.current_scan_slot = None
        self.selected_slot = None
        self.origin = None
        self.origin_scan_slot = None
        self.position_unverified_reason = None
        self.last_move = {"invalidated": reason}

        return {
            "position_valid": False,
            "position_state": self.POSITION_UNKNOWN,
            "angle_deg": None,
            "reason": reason,
            "message": "Re-synchronize before moving: align the "
                       "reference slot with the loading hole and "
                       "confirm it.",
        }

    # ------------------------------------------------------------------
    # movement planning
    # ------------------------------------------------------------------

    def _forward_direction(self):
        direction = config.CAROUSEL_FORWARD_DIRECTION

        if direction not in DIRECTIONS:
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
        reverse = opposite(forward)

        delta = (target_scan_slot - self.current_scan_slot) % self.slot_count

        if delta == 0:
            return (forward, 0)

        if delta * 2 <= self.slot_count:
            return (forward, delta)

        return (reverse, self.slot_count - delta)

    # ------------------------------------------------------------------
    # movement execution
    # ------------------------------------------------------------------

    # What a failed movement did to the mechanism. Three cases, because
    # they call for three different physical actions from an operator
    # standing at the rover.
    #
    #   NOT_STARTED   the goal was never written. The carousel is
    #                 exactly where it was; nothing needs checking.
    #   MOVED         the encoder measured travel, and the movement
    #                 could not be verified. The sample is somewhere
    #                 between where it was and where it was going.
    #   UNKNOWN       a goal was written and we cannot say whether the
    #                 mechanism responded. Treated as MOVED for safety.
    #
    # These used to collapse into a single "moved: False", which told
    # an operator on the Linux bench that nothing had moved while the
    # carousel had visibly turned 180 degrees.
    MOTION_NOT_STARTED = "NOT_STARTED"
    MOTION_MOVED = "MOVED"
    MOTION_UNKNOWN = "UNKNOWN"

    @staticmethod
    def motion_verdict(error):
        """
        What the mechanism did, from the evidence the driver attached.

        Conservative by construction: a failure that carries no
        evidence is UNKNOWN, never NOT_STARTED. Claiming the carousel
        did not move is a claim about the physical world, and it may
        only be made when the driver can support it.
        """
        motion = dict(getattr(error, "motion", None) or {})

        if not motion:
            return Carousel.MOTION_UNKNOWN, motion

        if not motion.get("commanded"):
            return Carousel.MOTION_NOT_STARTED, motion

        if motion.get("encoder_moved"):
            return Carousel.MOTION_MOVED, motion

        return Carousel.MOTION_UNKNOWN, motion

    def _movement_failed(self, error, reason, requested):
        """
        Turn a backend failure into an invalidated position and a report.

        The firmware never falls back to assuming the movement worked, and
        it never falls back from one backend's failure to the other
        backend's method. Losing position is a fault to be reported.

        The report now also says WHAT THE MECHANISM DID, which is a
        different question from whether the command succeeded. See
        motion_verdict above.
        """
        measured = self._safe_position_report()
        verdict, motion = self.motion_verdict(error)

        self.invalidate_position(reason)

        # THE CONSEQUENCE IS DATA, NOT PROSE, AND IT IS SAID ONCE.
        #
        # This used to append one of three paragraphs to the message,
        # and the PC then printed the same fact again from `motion` as
        # its `carousel:` line and a third time as an instruction. One
        # real refusal ran to eleven lines saying three things.
        #
        # `motion` and `motion_detail` below already carry the verdict
        # and the evidence, and every reader derives its own wording
        # from them - workflow/status.carousel_outcome is the one place
        # that does it for the operator. So the message states the
        # failure and stops.
        return CarouselError(
            error.code,
            "{} failed: {}".format(reason, error.message),
            data={
                "requested": requested,
                "measured_travel_deg": measured,
                "position_valid": False,
                "position_measurable": measured is not None,

                # The three fields every layer above reads instead of
                # inventing its own answer.
                "motion": verdict,
                "moved": verdict != self.MOTION_NOT_STARTED,
                "motion_detail": motion,
            },
        )

    def _account_verification(self, movement, reason):
        """
        Carry the driver's verification verdict into the tracked state.

        A movement the driver could not confirm leaves the position
        BELIEVED BUT UNPROVEN. It is deliberately not invalidated: the
        goal was written, the profile ran and the servo reported itself
        stopped, so throwing the position away would be as much of an
        invention as claiming it was verified. A later verified movement
        or a re-sync clears it.
        """
        if not isinstance(movement, dict):
            return movement

        if movement.get("verification") == "UNVERIFIED":
            self.position_unverified_reason = (
                movement.get("unverified_reason")
                or "{} completed but its position was not "
                   "confirmed".format(reason)
            )

        elif movement.get("verification") == "VERIFIED":
            self.position_unverified_reason = None

        return movement

    def _run_slots(self, direction, slots, reason):
        servo = self._require_servo()

        try:
            return self._account_verification(
                servo.move_slots(direction, slots), reason
            )

        except ServoError as error:
            raise self._movement_failed(
                error, reason, {"direction": direction, "slots": slots}
            )

    def _run_half_turn(self, direction, reason):
        servo = self._require_servo()

        try:
            return self._account_verification(
                servo.half_turn(direction), reason
            )

        except ServoError as error:
            raise self._movement_failed(
                error, reason, {"direction": direction, "half_turn": True}
            )

    def _run_degrees(self, degrees, reason):
        servo = self._require_servo()

        try:
            return self._account_verification(
                servo.move_degrees(degrees), reason
            )

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
            direction_sign(direction) * float(degrees)
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

        if direction not in DIRECTIONS:
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
        return self.half_turn(opposite(self._forward_direction()))

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

            self.alignment_offset_deg = centred_degrees(
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

        if direction not in DIRECTIONS:
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

    # ------------------------------------------------------------------
    # position confidence
    # ------------------------------------------------------------------
    #
    # UNVERIFIED IS NOT FAILED, AND NEITHER OF THEM IS UNKNOWN.
    #
    # Before H-002 was understood there was one bit - position_valid -
    # and every doubt collapsed into it, so a servo that had performed a
    # movement perfectly and a servo nobody had ever synchronized were
    # reported with the same three words. Three states are needed
    # because three different things go wrong and each has a different
    # next action:
    #
    #   SYNCHRONIZED  an origin exists and the last movement was
    #                 confirmed by measurement. Normal operation.
    #
    #   UNVERIFIED    an origin exists and a movement completed, but the
    #                 confirming read did not arrive. The carousel is
    #                 almost certainly where it was sent; nothing has
    #                 proved it. Safe next action: read the status
    #                 again - the position is measurable, it just was
    #                 not measured at the one moment it mattered.
    #
    #   UNKNOWN       there is no origin, or the mechanism is somewhere
    #                 no logical position corresponds to. Only a
    #                 physical re-sync fixes this.
    POSITION_SYNCHRONIZED = "SYNCHRONIZED"
    POSITION_UNVERIFIED = "UNVERIFIED"
    POSITION_UNKNOWN = "UNKNOWN"

    def position_state(self):
        if not self.position_valid or self.current_scan_slot is None:
            return self.POSITION_UNKNOWN

        if self.position_unverified_reason:
            return self.POSITION_UNVERIFIED

        return self.POSITION_SYNCHRONIZED

    def _raw_encoder_counts(self):
        """The servo-frame measured count, or None. Telemetry only."""
        if self.servo is None:
            return None

        try:
            return self.servo.read_position_or_none()

        except Exception:
            return None

    def angle_deg(self, position=None):
        """
        The LOGICAL carousel angle, signed degrees from the origin.

        Zero at the position the operator re-synchronized, whatever the
        raw encoder count there happened to be. None when there is no
        origin to measure from - which is not the same as zero.

        `position` lets a caller that has ALREADY read the encoder pass
        it in. status() does: it used to reach the servo three separate
        times for one report - once here, once for the raw count and
        once through drift_deg - and get three readings of a moving
        mechanism taken at three different moments.
        """
        if self.servo is None or self.origin is None:
            return None

        try:
            return self.servo.angle_deg(position)

        except Exception:
            return None

    def status(self):
        # ONE ENCODER READING FOR THE WHOLE REPORT. The angle, the raw
        # count and the drift are three views of the same instant, and
        # taking them from three transactions during a movement would
        # let a report contradict itself.
        encoder = self._raw_encoder_counts()

        angle = self.angle_deg(encoder)
        drift = self.drift_deg(angle)

        return {
            "position_valid": self.position_valid,

            # The logical carousel coordinate. THE number an operator
            # reads, and deliberately not the raw encoder count: the
            # raw count is a servo-frame value with an arbitrary offset,
            # and printing it in degrees is what made a carousel that
            # had just been re-synchronized report 0.18 deg.
            "angle_deg": angle,
            "position_state": self.position_state(),
            "position_unverified_reason": self.position_unverified_reason,

            "current_scan_slot": self.current_scan_slot,
            "current_load_slot": self.get_load_slot(),
            "selected_slot": self.selected_slot,
            "carousel_phase": self.phase(),
            "slot_count": self.slot_count,
            "scan_load_offset_slots": self.offset,
            "forward_direction": config.CAROUSEL_FORWARD_DIRECTION,
            "max_fine_adjust_deg": config.MAX_FINE_ADJUST_DEG,
            "servo_attached": self.servo.name if self.servo else None,

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

                # RAW SERVO TELEMETRY, KEPT AND KEPT SEPARATE.
                #
                # None of this is the carousel's coordinate. It is the
                # servo-frame evidence the coordinate was derived from,
                # reported so a screen can show the derivation instead
                # of asking the operator to trust it, and so a bad
                # origin can be recognised for what it is.
                "encoder_counts": encoder,
                "origin_counts": (
                    (self.origin or {}).get("origin_counts")
                ),
            },

            # Historical field name for the physical spacing.
            "degrees_per_slot": self.slot_step_deg,
            "last_move": self.last_move,
        }
