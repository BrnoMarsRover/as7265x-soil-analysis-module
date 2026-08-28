"""
The ST3215, through the production command surface.

WHAT THE SHIPPED SYSTEM ALREADY GIVES A DIAGNOSTIC CAMPAIGN

    servo_diagnostics      uart, ping, id, baud code, mode, torque,
                           angle limits, feedback - each step reported
                           separately, and it MOVES NOTHING
    servo_bus_scan         every documented baud rate, both pin orders,
                           a list of ids - the ECHO_ONLY investigation
    get_servo_calibration  speed, acceleration, tolerance, settle,
                           poll interval, move timeout
    servo_test_move        a verified relative movement, in slots, half
                           turns, an arbitrary angle, or across the
                           encoder seam - with start_position,
                           expected_position, actual_position,
                           position_error, elapsed_ms and the status
                           flags for every leg
    servo_torque           torque on/off
    servo_stop             stop

That is very nearly everything H-002 needs, which is why this adapter is
thin and why no test-side firmware is required for B2, B3 or B4.

WHAT IT DOES NOT GIVE, AND WHAT THAT COSTS

    a raw ST3215 packet     no command sends arbitrary bytes to the
                            servo bus and returns the reply verbatim.
                            The PC therefore sees the driver's
                            INTERPRETATION of the position register and
                            never the two bytes it was built from.
    an arbitrary register   no command reads register N. `diagnostics`
                            reads a fixed set.

Those two gaps block exactly two planned tests (raw-frame capture and
the byte-order interpretation of a raw position read). They are
registered as BLOCKED with the interface that would unblock them. They
are NOT worked around by adding a passthrough to production firmware.

THE SINGLE-LEG LIMIT IS A PRODUCTION RULE, NOT A TEST CHOICE.
`move_relative` refuses more than half a revolution per leg, because
past that a single encoder reading cannot tell a movement from its
complement. A 360 degree investigation step is therefore two 180 degree
legs (`repeat=2`), which the driver verifies individually - and the
adapter says so rather than quietly halving the request.
"""

from .base import Adapter, Capability, firmware_commands, pc_command_surface


class ServoAdapter(Adapter):
    """ST3215 diagnostics and bounded, verified movement."""

    name = "servo"

    def __init__(self, context, link):
        super().__init__(context)

        self.link = link

    # ------------------------------------------------------------------

    def _detect(self):
        surface = pc_command_surface()
        commands = firmware_commands()

        found = {}

        found["servo.connect"] = self.from_commands(
            "servo.connect", ["connect_servo", "disconnect_servo"],
            "add connect_servo to firmware/ESP32/protocol.py",
            surface, ["connect_servo", "disconnect_servo"],
        )

        found["servo.diagnostics"] = self.from_commands(
            "servo.diagnostics", ["servo_diagnostics"],
            "add servo_diagnostics to firmware/ESP32/protocol.py",
            surface, ["servo_diagnostics"],
        )

        found["servo.bus_scan"] = self.from_commands(
            "servo.bus_scan", ["servo_bus_scan"],
            "add servo_bus_scan to firmware/ESP32/protocol.py",
            surface, ["servo_bus_scan"],
        )

        found["servo.calibration"] = self.from_commands(
            "servo.calibration", ["get_servo_calibration"],
            "add get_servo_calibration to firmware/ESP32/protocol.py",
            surface, ["get_servo_calibration"],
        )

        found["servo.test_move"] = self.from_commands(
            "servo.test_move", ["servo_test_move"],
            "add servo_test_move to firmware/ESP32/protocol.py",
            surface, ["servo_test_move"],
        )

        found["servo.torque"] = self.from_commands(
            "servo.torque", ["servo_torque"],
            "add servo_torque to firmware/ESP32/protocol.py",
            surface, ["servo_torque"],
        )

        found["servo.stop"] = self.from_commands(
            "servo.stop", ["servo_stop"],
            "add servo_stop to firmware/ESP32/protocol.py",
            surface, ["servo_stop"],
        )

        # --- the two real gaps ---------------------------------------

        found["servo.raw_packet"] = Capability(
            "servo.raw_packet", "servo_raw" in commands,
            reason="no firmware command sends a raw ST3215 packet and "
                   "returns the reply bytes; the PC sees the driver's "
                   "parsed values only",
            recommendation=(
                "Add a bounded, read-only diagnostic command to "
                "firmware/ESP32/protocol.py - suggested name "
                "`servo_raw_read` - taking {id, register, length} with "
                "length limited to 1..4, calling the existing "
                "ST3215.read_byte / read_word path, and returning both "
                "the parsed value AND the raw reply bytes as a list of "
                "integers. Read-only: no register WRITE passthrough, "
                "because a wrong write to the ST3215 memory table can "
                "change the servo id or baud rate and take the bus away "
                "entirely. Until it exists, HW-B2-006 and HW-B3-004 "
                "stay BLOCKED."),
        )

        found["servo.read_register"] = Capability(
            "servo.read_register", "servo_read_register" in commands,
            reason="no firmware command reads an arbitrary ST3215 "
                   "register; servo_diagnostics reads a fixed set (id, "
                   "baud code, mode, torque, angle limits, feedback)",
            recommendation=(
                "The same `servo_raw_read` command described for "
                "servo.raw_packet covers this. Alternatively extend the "
                "existing diagnostics report with the registers a "
                "campaign needs, which is smaller but has to be changed "
                "again next time."),
        )

        # A position read on demand: available, but only as part of the
        # diagnostics report, which is a heavier operation than a single
        # register read. Recorded as available WITH that qualification
        # so a latency measured through it is not read as a register
        # round-trip time.
        found["servo.read_position"] = Capability(
            "servo.read_position",
            "servo_diagnostics" in commands,
            reason="position is available through servo_diagnostics -> "
                   "feedback; there is no lighter single-register read, "
                   "so a latency measured this way includes the whole "
                   "diagnostics sequence",
            recommendation="See servo.raw_packet for the lighter path.",
        )

        return found

    # ------------------------------------------------------------------
    # operations
    # ------------------------------------------------------------------

    def connect(self):
        return self.link.request("connect_servo",
                                 timeout=self._move_timeout())["data"]

    def disconnect(self):
        return self.link.request("disconnect_servo",
                                 timeout=self._move_timeout())["data"]

    def diagnostics(self):
        """Full communication check. Moves nothing."""
        return self.link.request("servo_diagnostics",
                                 timeout=self._move_timeout(),
                                 retries=1)

    def bus_scan(self, ids=None, bauds=None, swap=True):
        payload = {"swap": bool(swap)}

        if ids is not None:
            payload["ids"] = list(ids)

        if bauds is not None:
            payload["bauds"] = list(bauds)

        return self.link.request("servo_bus_scan",
                                 timeout=self._move_timeout(), **payload)

    def calibration(self):
        return self.link.request("get_servo_calibration", retries=1)

    def torque(self, enable=True):
        return self.link.request("servo_torque",
                                 timeout=self._move_timeout(),
                                 enable=bool(enable))

    def stop(self):
        return self.link.request("servo_stop", timeout=self._move_timeout())

    # THE NAME THE FIRMWARE ACTUALLY SENDS, FIRST.
    #
    # `ST3215.read_feedback` returns `position_counts`. It always has.
    # This reader looked for "position", "present_position" and
    # "current_position" and none of those has ever been a key of that
    # dict, so `position()` returned None on every real board.
    #
    # It passed offline because the fake in offline_tests/fake_link.py
    # emits "position" - the fake was written to match the reader and
    # the reader to match the fake, so the pair agreed with each other
    # and neither agreed with the firmware. That is the one thing a
    # fake may never do: it replaced a DECISION about the wire format,
    # not the wire.
    #
    # Measured on the bench, 2026-08-27: HW-B2-004 reported
    # "reads: 20, answered: 0" while HW-B2-002, one test earlier, had
    # just read position_counts=1 out of the same feedback block.
    #
    # This matters far beyond B2-004. Every encoder reading in B3 -
    # H-002, the campaign the whole servo side waits on - comes through
    # here, and a None reads exactly like "the encoder told us
    # nothing", which is the very conclusion H-002 exists to test.
    POSITION_KEYS = (
        "position_counts", "position", "present_position",
        "current_position",
    )

    def position(self):
        """
        The servo's present position in encoder counts, or None.

        Returns None rather than raising when the report has no
        position: "the servo did not tell us where it is" is a result,
        and a KeyError here would lose it.
        """
        report = self.diagnostics()["data"]

        feedback = (report or {}).get("feedback") or {}

        for key in self.POSITION_KEYS:
            if key in feedback:
                return feedback[key]

        for step in (report or {}).get("steps", []):
            if step.get("step") == "feedback" and isinstance(
                    step.get("value"), dict):
                value = step["value"]

                for key in self.POSITION_KEYS:
                    if key in value:
                        return value[key]

        return None

    # ------------------------------------------------------------------

    def move(self, kind, repeat=1, degrees=None, hold_ms=None):
        """
        One verified movement test. THE CAROUSEL TURNS.

        The safety confirmation has already happened in `Runner` before
        any test with a MOTION safety class runs; `confirm=True` here is
        the firmware's own second gate, not a bypass of ours.
        """
        self.context.require_hardware_mode("move the servo")

        payload = {"kind": kind, "repeat": int(repeat), "confirm": True}

        if degrees is not None:
            payload["degrees"] = float(degrees)

        if hold_ms is not None:
            payload["hold_ms"] = int(hold_ms)

        return self.link.request(
            "servo_test_move", timeout=self._move_timeout(), **payload)

    def move_degrees(self, degrees, repeat=1):
        """
        Move by an angle, splitting nothing silently.

        The production driver refuses a single verified leg larger than
        half a revolution. Rather than quietly halving the request or
        looping behind the caller's back, this raises with the exact
        arithmetic - and `plan_degrees` below is what a test calls to
        get a legal plan in the first place.
        """
        limit = self.context.profile.data["motion"]["max_degrees_per_leg"]

        if abs(float(degrees)) > limit:
            raise ValueError(
                "{} deg exceeds the {} deg per-leg limit. The ST3215 "
                "driver cannot verify a movement larger than half a "
                "revolution from one encoder reading. Use plan_degrees() "
                "to split it into legs.".format(degrees, limit))

        return self.move("degrees", repeat=repeat, degrees=float(degrees))

    def plan_degrees(self, total_degrees):
        """
        A legal (degrees, repeat) plan for a total angle, or None.

        360 degrees becomes 180 x 2 rather than one impossible leg, and
        the plan is recorded in the evidence so nobody later reads "360"
        and assumes it was a single commanded movement.
        """
        limit = float(
            self.context.profile.data["motion"]["max_degrees_per_leg"])

        total = float(total_degrees)

        if total == 0:
            return None

        sign = 1.0 if total > 0 else -1.0
        magnitude = abs(total)

        if magnitude <= limit:
            return {"degrees": total, "repeat": 1, "legs": 1,
                    "total_degrees": total, "split": False}

        # Only whole multiples of the limit can be expressed as one
        # command with `repeat`; anything else needs separate commands
        # and the caller is told so.
        repeat = int(magnitude // limit)
        remainder = magnitude - repeat * limit

        plan = {
            "degrees": sign * limit,
            "repeat": repeat,
            "legs": repeat + (1 if remainder else 0),
            "total_degrees": total,
            "split": True,
            "remainder_degrees": sign * remainder if remainder else 0.0,
        }

        if repeat > 8:
            # servo_test_move caps repeat at 8.
            plan["exceeds_repeat_limit"] = True

        return plan

    # ------------------------------------------------------------------

    def _move_timeout(self):
        return getattr(self.link.module, "MOVE_TIMEOUT", 60.0)
