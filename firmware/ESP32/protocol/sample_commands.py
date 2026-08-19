# protocol/sample_commands.py
# Retained raw acquisitions, for the PC to pull back.
#
# The ESP32 keeps the last raw acquisition per slot in RAM so the PC can
# recover a measurement it lost - a crash, a restart, a different laptop.
# This is an acquisition buffer, not an archive: RAM only, never written
# to flash, forgotten on reset, and never interpreted here.

from protocol.router import CommandError


class SampleCommands:
    """Commands over the acquisitions held in RAM."""

    def __init__(self, module):
        self.module = module

    @property
    def carousel(self):
        return self.module.carousel

    def handlers(self):
        return {
            "list_saved_samples": self.handle_list_saved_samples,
            "get_saved_sample": self.handle_get_saved_sample,
            "delete_saved_samples": self.handle_delete_saved_samples,
        }

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------

    def handle_list_saved_samples(self, request):
        """
        Sample IDs whose raw acquisition is still held in RAM.

        Deliberately an index only: one record carries 18 floats plus the
        settings block, and sending several at once is exactly the kind
        of oversized MicroPython response that used to truncate. The PC
        fetches each record individually with get_saved_sample.
        """
        retained = self.carousel.retained_samples()

        return {
            "count": len(retained),
            "samples": [
                {
                    # A measurement taken without a Sample ID still has
                    # to be listable, or it can never be exported or
                    # deleted. Fall back to the slot it came from.
                    "sample_id": (
                        slot["sample_id"]
                        or "SLOT{}".format(slot["slot_id"])
                    ),
                    "has_sample_id": slot["sample_id"] is not None,
                    "slot_id": slot["slot_id"],
                    "occupied": slot["occupied"],
                }
                for slot in retained
            ],
            "storage": "ram_only",
            "note": "Raw acquisitions held since the last reset. The PC "
                    "is the persistent archive.",
        }

    def handle_get_saved_sample(self, request):
        """One retained raw acquisition, exactly as it was acquired."""
        sample_id = request.get("sample_id")

        if not sample_id:
            raise CommandError(
                "MISSING_FIELD",
                "get_saved_sample requires a 'sample_id'.",
            )

        slot = self.carousel.retained_sample(sample_id)

        if slot is None:
            raise CommandError(
                "SAMPLE_NOT_FOUND",
                "No retained acquisition for sample {}.".format(sample_id),
            )

        return {
            "sample_id": sample_id,
            "slot_id": slot["slot_id"],
            "occupied": slot["occupied"],
            "measurement": slot["measurement"],
        }

    def handle_delete_saved_samples(self, request):
        """
        Delete every retained acquisition held on this device.

        Deliberately narrow: it removes ONLY the stored measurements.
        Physical slot occupancy is left exactly as it was, because soil
        can still be sitting in a slot whose record has been exported,
        and the PC archive is a completely separate store that this
        command cannot reach.
        """
        cleared = self.carousel.clear_retained_samples()

        return {
            "deleted_count": len(cleared),
            "deleted": cleared,
            "remaining": len(self.carousel.retained_samples()),
            "slots": self.carousel.slot_summary(),
            "note": "ESP32 acquisitions only. Physical slot state and the "
                    "PC archive were not touched.",
        }
