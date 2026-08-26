"""
The carousel and the complete measurement transaction.

THE GEOMETRY THIS BENCH ACTUALLY HAS, WHICH IS NOT THE ONE IN THE
PROMPT THAT ASKED FOR THIS FRAMEWORK

    CAROUSEL_SLOT_COUNT           4        (not 8)
    CAROUSEL_SLOT_GEOMETRY_DEG    90.0     (not 45)
    CAROUSEL_SCAN_LOAD_OFFSET     2 slots  = 180 deg, loader to scanner
    ST3215_COUNTS_PER_REV         4096
    ST3215_COUNTS_PER_SLOT        1024
    ST3215_HALF_TURN_COUNTS       2048

Those come from `firmware/ESP32/config.py` and they are read, never
copied - see `configuration/profile.py`. Every slot-pair matrix this
adapter builds is generated from the production slot count, so the same
campaign works unchanged if the mechanism is ever rebuilt with a
different plate.

POSITION IS A CLAIM, AND IT EXPIRES

`carousel.status()` reports `position_valid`. After an uncertain
movement, a communication loss, a reset or a power cycle it is false,
and this adapter never repairs that by assuming: `position_state()`
returns POSITION_UNKNOWN / RESYNC_REQUIRED, and a test that needs a
known position must either re-sync explicitly or record that it could
not. That rule is what makes B9's results meaningful.
"""

from .base import Adapter, Capability, firmware_commands, pc_command_surface


class CarouselAdapter(Adapter):
    """Slot movement, the measurement transaction, and position truth."""

    name = "carousel"

    def __init__(self, context, link):
        super().__init__(context)

        self.link = link

    # ------------------------------------------------------------------

    def _detect(self):
        surface = pc_command_surface()
        commands = firmware_commands()

        found = {}

        found["carousel.status"] = self.from_commands(
            "carousel.status", ["get_status"],
            "add get_status to firmware/ESP32/protocol.py",
            surface, ["get_status"],
        )

        found["carousel.sync"] = self.from_commands(
            "carousel.sync", ["sync_position"],
            "add sync_position to firmware/ESP32/protocol.py",
            surface, ["sync_position"],
        )

        found["carousel.select_slot"] = self.from_commands(
            "carousel.select_slot", ["select_slot"],
            "add select_slot to firmware/ESP32/protocol.py",
            surface, ["select_slot"],
        )

        found["carousel.move_slots"] = self.from_commands(
            "carousel.move_slots", ["move_slots"],
            "add move_slots to firmware/ESP32/protocol.py",
            surface, ["move_slots"],
        )

        found["carousel.fine_adjust"] = self.from_commands(
            "carousel.fine_adjust", ["fine_adjust"],
            "add fine_adjust to firmware/ESP32/protocol.py",
            surface, ["fine_adjust"],
        )

        found["carousel.measure"] = self.from_commands(
            "carousel.measure", ["measure_raw"],
            "add measure_raw to firmware/ESP32/protocol.py",
            surface, ["measure_raw"],
        )

        found["carousel.saved_samples"] = self.from_commands(
            "carousel.saved_samples",
            ["list_saved_samples", "get_saved_sample"],
            "add the retained-acquisition buffer commands to "
            "firmware/ESP32/protocol.py",
            surface, ["list_saved_samples", "get_saved_sample"],
        )

        # Interrupting a movement half way is not something the shipped
        # command surface can do from the PC: `servo_stop` exists, but
        # the protocol is one request at a time, so nothing can be sent
        # while a move_slots is in flight on the same link.
        found["carousel.interrupt_move"] = Capability(
            "carousel.interrupt_move", False,
            reason="the wire is strictly one request at a time, so no "
                   "second command can be sent while a movement is in "
                   "flight; servo_stop cannot be delivered mid-move over "
                   "the same link",
            recommendation=(
                "Interrupt the movement PHYSICALLY instead - pull the "
                "USB cable, or cut servo power - which is what HW-B9-004 "
                "and HW-B9-006 do, and which is a more faithful test of "
                "the failure the rover will actually see. A software "
                "mid-move abort would need either a second transport or "
                "an out-of-band break character in the firmware's read "
                "loop; neither is worth adding for a test."),
        )

        return found

    # ------------------------------------------------------------------
    # state
    # ------------------------------------------------------------------

    def status(self):
        data = self.link.request("get_status", retries=2)["data"]

        return (data or {}).get("carousel") or {}

    def position_state(self):
        """
        POSITION_KNOWN, POSITION_UNKNOWN or POSITION_UNREADABLE.

        Never optimistic. If the status cannot be read at all, the
        answer is UNREADABLE - which is different from UNKNOWN, because
        one means the firmware says it does not know and the other means
        we could not ask.
        """
        try:
            status = self.status()

        except Exception as error:
            return {
                "state": "POSITION_UNREADABLE",
                "resync_required": True,
                "reason": "the module could not be asked: {}: {}".format(
                    type(error).__name__, error),
            }

        valid = status.get("position_valid")

        if valid:
            return {
                "state": "POSITION_KNOWN",
                "resync_required": False,
                "scan_slot": status.get("current_scan_slot"),
                "load_slot": status.get("current_load_slot"),
                "phase": status.get("carousel_phase"),
            }

        return {
            "state": "POSITION_UNKNOWN",
            "resync_required": True,
            "reason": status.get("invalid_reason")
                      or "the firmware reports position_valid false",
            "phase": status.get("carousel_phase"),
        }

    def sync(self, load_slot=None, scan_slot=None):
        payload = {}

        if load_slot is not None:
            payload["load_slot"] = int(load_slot)

        if scan_slot is not None:
            payload["scan_slot"] = int(scan_slot)

        return self.link.request("sync_position", **payload)

    # ------------------------------------------------------------------
    # movement
    # ------------------------------------------------------------------

    def select_slot(self, slot, sample_id=None):
        self.context.require_hardware_mode("turn the carousel")

        payload = {"slot": int(slot)}

        if sample_id is not None:
            payload["sample_id"] = str(sample_id)

        return self.link.request("select_slot",
                                 timeout=self._move_timeout(), **payload)

    def move_slots(self, direction, slots=1):
        self.context.require_hardware_mode("turn the carousel")

        return self.link.request("move_slots",
                                 timeout=self._move_timeout(),
                                 direction=direction, slots=int(slots))

    def fine_adjust(self, degrees):
        self.context.require_hardware_mode("turn the carousel")

        return self.link.request("fine_adjust",
                                 timeout=self._move_timeout(),
                                 degrees=float(degrees))

    def measure(self, slot, sample_id=None, repeats=None):
        """
        The complete transaction: transfer, WHITE, UV, IR, return.

        This is the production `measure_raw`. Nothing about it is
        test-specific, which is the point - B8 measures what the
        competition will run.
        """
        self.context.require_hardware_mode(
            "run a full measurement (movement and illumination)")

        payload = {"slot": int(slot)}

        if sample_id is not None:
            payload["sample_id"] = str(sample_id)

        if repeats is not None:
            payload["repeats"] = int(repeats)

        return self.link.request(
            "measure_raw", timeout=self._measure_timeout(), **payload)

    # ------------------------------------------------------------------
    # geometry, generated from the production slot count
    # ------------------------------------------------------------------

    def slot_count(self):
        return self.context.profile.slot_count

    def adjacent_transitions(self):
        """1->2, 2->3, ... N->1, and the reverse of each."""
        count = self.slot_count()

        forward = [(s, s % count + 1) for s in range(1, count + 1)]
        reverse = [(b, a) for a, b in forward]

        return forward + reverse

    def non_adjacent_transitions(self):
        """
        Every slot pair that is not a neighbour and not the same slot.

        On a 4-slot plate that is 1->3, 2->4 and their reverses; on an
        8-slot plate it would be the 1->3 / 1->5 / 1->8 family the
        campaign plan describes. Generated, so the test does not have to
        be rewritten when the mechanism changes.
        """
        count = self.slot_count()
        neighbours = set(self.adjacent_transitions())

        pairs = []

        for first in range(1, count + 1):
            for second in range(1, count + 1):
                if first == second:
                    continue

                if (first, second) in neighbours:
                    continue

                pairs.append((first, second))

        return pairs

    def full_rotation_sequence(self):
        """1 -> 2 -> ... -> N -> 1, for the drift measurement."""
        count = self.slot_count()

        return list(range(1, count + 1)) + [1]

    # ------------------------------------------------------------------

    def _move_timeout(self):
        return getattr(self.link.module, "MOVE_TIMEOUT", 60.0)

    def _measure_timeout(self):
        configured = self.context.profile.get("measure_timeout_s")

        if configured:
            return float(configured)

        return getattr(self.link.module, "MEASURE_TIMEOUT", 180.0)
