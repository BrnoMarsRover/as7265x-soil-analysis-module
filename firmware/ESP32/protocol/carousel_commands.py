# protocol/carousel_commands.py
# Carousel position and movement commands.
#
# Every one of these is generic: the request says "move two slots" or
# "adjust by -2.5 degrees", and whichever backend the operator selected
# decides what that means physically. There are deliberately no
# per-actuator move commands - the PC does not need to
# know, and encoding the actuator into the command surface would make
# every future actuator a protocol change.
#
# Movement is blocked entirely while no servo is selected. That check
# lives in the carousel, which raises SERVO_NOT_CONNECTED, so it cannot be
# forgotten by one handler out of six.

import config

from control.carousel import CarouselError
from drivers.servo_base import CCW, CW
from protocol.router import CommandError


class CarouselCommands:
    """Position, movement and physical slot state."""

    def __init__(self, module):
        self.module = module

    @property
    def carousel(self):
        return self.module.carousel

    @property
    def servos(self):
        return self.module.servos

    def _require_slot(self, request):
        return self.module.require_slot(request)

    def handlers(self):
        return {
            "sync_position": self.handle_sync_position,
            "select_slot": self.handle_select_slot,
            "move_slots": self.handle_move_slots,
            "fine_adjust": self.handle_fine_adjust,
            "clear_slot": self.handle_clear_slot,
            "clear_all_slots": self.handle_clear_all_slots,
        }

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------

    def handle_sync_position(self, request):
        """
        Establish the carousel origin. This never moves anything.

        Normal use is 'load_slot': the operator physically aligns Slot 1
        with the soil loading hole and confirms it, making that position
        the origin. 'scan_slot' is accepted for callers that prefer to
        declare the scanner side.

        The one thing this DOES need the servo for is reading the encoder:
        the origin is a real encoder count, taken at the moment the
        operator confirms the alignment. If the servo cannot be read, no
        origin is recorded - a made-up origin would poison every movement
        that followed it.
        """
        if "load_slot" in request:
            try:
                status = self.carousel.sync_to_load_slot(
                    request.get("load_slot")
                )

            except CarouselError as error:
                raise CommandError(error.code, error.message, data=error.data)

        elif "scan_slot" in request:
            try:
                status = self.carousel.sync_position(request.get("scan_slot"))

            except CarouselError as error:
                raise CommandError(error.code, error.message, data=error.data)

        else:
            raise CommandError(
                "MISSING_FIELD",
                "sync_position requires 'load_slot' (the slot now under "
                "the loading hole) or 'scan_slot' (1 to {}).".format(
                    config.CAROUSEL_SLOT_COUNT
                ),
            )

        return {"synchronized": True, "carousel": status}

    def handle_select_slot(self, request):
        """
        Choose the physical slot to work with and bring it to the loader.

        If the previous sample is still at the scanner the firmware
        restores the loading orientation first, so the operator never
        runs the 180 degree return by hand.
        """
        slot_id = self._require_slot(request)

        move = self.carousel.select_slot(slot_id)

        sample_id = request.get("sample_id")

        if sample_id is not None:
            # Correlation only: the PC owns the scientific identity.
            self.carousel.slots[slot_id]["sample_id"] = str(sample_id)

        return {
            "selected_slot": slot_id,
            "move": move,
            "slot": self.carousel.slots[slot_id],
            "carousel": self.carousel.status(),
        }

    def handle_move_slots(self, request):
        """
        Whole-slot movement: one slot spacing per slot, in encoder counts.

        Works before synchronization, so the operator can line Slot 1 up
        with the loading hole during the sync procedure. Each slot is a
        separately verified movement, so a fault partway through a
        multi-slot request is reported instead of accumulating.
        """
        direction = request.get("direction", CW)

        if direction not in (CW, CCW):
            raise CommandError(
                "BAD_REQUEST",
                "direction must be '{}' or '{}'.".format(CW, CCW),
            )

        try:
            slots = int(request.get("slots", 1))

        except (TypeError, ValueError):
            raise CommandError("BAD_REQUEST", "'slots' must be a whole number.")

        if slots < 0 or slots > config.CAROUSEL_SLOT_COUNT:
            raise CommandError(
                "BAD_REQUEST",
                "'slots' must be between 0 and {}.".format(
                    config.CAROUSEL_SLOT_COUNT
                ),
            )

        try:
            move = self.carousel.move_slots(direction, slots)

        except CarouselError as error:
            raise CommandError(error.code, error.message, data=error.data)

        return {
            "move": move,
            "degrees": slots * config.SLOT_STEP_DEG,
            "degrees_per_slot": config.CAROUSEL_SLOT_GEOMETRY_DEG,
            "carousel": self.carousel.status(),
        }

    def handle_fine_adjust(self, request):
        """
        Small mechanical correction in degrees; positive is clockwise.

        Alignment only - the logical slot numbering is left alone. The
        correction is converted to encoder counts, commanded, and then
        verified like any other movement; it is also REMEMBERED, so the
        next slot movement keeps it instead of returning the carousel to
        the old theoretical centre.
        """
        if "degrees" not in request:
            raise CommandError(
                "MISSING_FIELD",
                "fine_adjust requires 'degrees' (positive = clockwise).",
            )

        try:
            degrees = float(request.get("degrees"))

        except (TypeError, ValueError):
            raise CommandError("BAD_REQUEST", "'degrees' must be a number.")

        limit = config.MAX_FINE_ADJUST_DEG

        if abs(degrees) > limit:
            raise CommandError(
                "FINE_ADJUST_TOO_LARGE",
                "Fine adjustment is limited to +/-{:.0f} degrees. Use "
                "whole-slot movement for larger movements.".format(limit),
            )

        try:
            adjustment = self.carousel.fine_adjust(degrees)

        except CarouselError as error:
            raise CommandError(error.code, error.message, data=error.data)

        return {
            "adjustment": adjustment,
            "carousel": self.carousel.status(),
        }

    def handle_clear_slot(self, request):
        """
        Free a physical slot for reuse.

        Physical state only. The PC's persistent scientific record for
        whatever was in the slot is untouched and still exists.
        """
        slot_id = self._require_slot(request)
        previous = self.carousel.slots[slot_id]

        cleared_sample = previous["sample_id"]
        was_occupied = previous["occupied"]

        slot = self.carousel.reset_slot(slot_id)

        return {
            "slot": slot,
            "was_occupied": was_occupied,
            "cleared_sample_id": cleared_sample,
            "carousel": self.carousel.status(),
        }

    def handle_clear_all_slots(self, request):
        """
        Free every physical slot in one operation.

        Physical state only. No saved Sample record is deleted, here or
        on the PC, and the carousel is not moved.
        """
        cleared = self.carousel.reset_all_slots()

        return {
            "cleared_count": len(cleared),
            "cleared": cleared,
            "slots": self.carousel.slot_summary(),
            "carousel": self.carousel.status(),
            "note": "Physical slot state only. Saved Sample records were "
                    "not touched.",
        }
