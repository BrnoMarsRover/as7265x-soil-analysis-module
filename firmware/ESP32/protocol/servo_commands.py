# protocol/servo_commands.py
# Servo selection, diagnostics, calibration and service commands.
#
# The carousel supports two physically different actuators, so the first
# thing the operator does after a boot is state which one is installed:
#
#     select_servo        connect the fitted ST3215
#
# Until then every movement command fails with SERVO_NOT_CONNECTED. There
# is no auto-detection - see control/servo_manager.py for why.
#
# Everything else in this group is deliberately backend-aware rather than
# backend-generic, because the two actuators genuinely differ:
#
#     servo_diagnostics   the ST3215
#                         reports a full communication and telemetry check
#     servo_test_move     the
#                         ST3215 offers encoder-verified movement tests
#     get/set_servo_calibration
#                         the ST3215
#                         has none and says so
#     servo_configure     ST3215 EPROM service operation
#     servo_torque        ST3215 holding torque
#
# The command NAMES are unchanged from when this firmware supported a
# second actuator, so the PC workflow and any stored record still
# resolve.

import config

from control.carousel import CarouselError
from control.servo_manager import (
    SERVO_TYPE,
    ServoSelectionError,
    servo_info,
)
from drivers import st3215 as st3215_driver
from drivers.servo_base import ServoError, ServoNotSupportedError
from protocol.router import CommandError


class ServoCommands:
    """Which actuator is fitted, how it is doing, and how to service it."""

    def __init__(self, module):
        self.module = module

    @property
    def servos(self):
        return self.module.servos

    @property
    def carousel(self):
        return self.module.carousel

    def handlers(self):
        return {
            "get_servo_options": self.handle_get_servo_options,
            "select_servo": self.handle_select_servo,
            "servo_stop": self.handle_servo_stop,
            "servo_diagnostics": self.handle_servo_diagnostics,
            "servo_bus_scan": self.handle_servo_bus_scan,
            "get_servo_calibration": self.handle_get_servo_calibration,
            "set_servo_calibration": self.handle_set_servo_calibration,
            "servo_test_move": self.handle_servo_test_move,
            "servo_configure": self.handle_servo_configure,
            "servo_torque": self.handle_servo_torque,
        }

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _require_servo(self):
        """The one gate. Turns a missing selection into a clear instruction."""
        try:
            return self.servos.require_servo()

        except ServoSelectionError as error:
            raise CommandError(error.code, error.message, data=error.data)

    def _capability(self, name):
        capabilities = self.servos.capabilities() or {}

        return bool(capabilities.get(name))

    def _reject_unsupported(self, what, capability):
        raise CommandError(
            "SERVO_NOT_SUPPORTED",
            "{} is not available on the {}: it has no {}.".format(
                what, self.servos.label(), capability
            ),
            data={
                "servo": self.servos.servo_type,
                "capabilities": self.servos.capabilities(),
            },
        )

    # ------------------------------------------------------------------
    # selection
    # ------------------------------------------------------------------

    def handle_get_servo_options(self, request):
        """
        What the operator may choose from. Asks nothing of the hardware.

        The PC uses this to build the servo selection screen, so a third
        actuator added later needs no PC change.
        """
        return {
            "servo_info": servo_info(),
            # Historical key. One entry now, but the PC screen is still
            # built from the firmware's answer rather than from a list
            # compiled into the client.
            "supported": [servo_info()],
            "connected": self.servos.is_connected(),
            "selected": self.servos.servo_type,
            "servo": self.servos.status(),
            "moved": False,
        }

    def handle_select_servo(self, request):
        """
        State which servo is physically installed.

        This is option [0] on the PC. It brings the chosen backend up, and
        it ALWAYS invalidates the carousel position - physical position
        state cannot survive a change of actuator, because a different
        servo has a different encoder zero and a different mounting.

        Sending "none" releases the current backend and blocks movement
        again, which is the right thing before physically swapping servos.
        """
        # The field is optional now that there is one actuator: omitting
        # it means "connect the servo that is fitted". Sending an
        # explicit name is still honoured, and an unknown name is still
        # refused rather than quietly treated as the ST3215.
        requested = request.get("servo", SERVO_TYPE)

        try:
            selection = self.servos.select(requested)

        except ServoSelectionError as error:
            # A backend that failed to come up leaves nothing connected,
            # so the carousel must not keep pointing at it either.
            self.carousel.attach_servo(None, "servo connection failed")

            raise CommandError(error.code, error.message, data=error.data)

        attachment = self.carousel.attach_servo(
            self.servos.servo,
            "servo connected: {}".format(selection["label"]),
        )

        return {
            "selection": selection,
            "carousel_attachment": attachment,
            "servo": self.servos.status(),
            "carousel": self.carousel.status(),
            "moved": False,
            "message": (
                "{} connected. The carousel position was invalidated - "
                "align Slot 1 with the loading hole and confirm "
                "it.".format(selection["label"])
                if selection["servo"] is not None
                else "The servo is disconnected. Carousel movement is "
                     "blocked."
            ),
        }

    # ------------------------------------------------------------------
    # movement control
    # ------------------------------------------------------------------

    def handle_servo_stop(self, request):
        """
        Stop the actuator where it is.

        The tracked position is dropped, because an aborted movement ends
        between slot centres and the firmware does not guess.
        """
        self._require_servo()

        try:
            result = self.carousel.stop()

        except CarouselError as error:
            raise CommandError(error.code, error.message, data=error.data)

        result["servo"] = self.servos.status()
        result["carousel"] = self.carousel.status()

        return result

    # ------------------------------------------------------------------
    # diagnostics
    # ------------------------------------------------------------------

    def handle_servo_diagnostics(self, request):
        """
        Check the servo. MOVES NOTHING.

        A full communication check: UART, ping, ID, baud, mode, torque,
        angle limits and telemetry. Everything the ST3215 can be asked
        without turning it.
        """
        self._require_servo()

        try:
            report = self.servos.diagnostics()

        except ServoSelectionError as error:
            raise CommandError(error.code, error.message, data=error.data)

        except ServoError as error:
            raise CommandError(error.code, error.message)

        report["moved"] = False
        report["carousel"] = self.carousel.status()

        return report

    def handle_servo_bus_scan(self, request):
        """
        Search the servo bus for anything that answers. MOVES NOTHING.

        Runs WITHOUT a servo being selected, which is the whole point:
        the operator reaches for this precisely when select_servo has
        just failed with SERVO_NOT_FOUND, and requiring a working
        selection before diagnosing why selection does not work would be
        circular.

        Only INST_PING is sent, at every baud rate the ST3215 supports
        and in both pin orders. A ping is a read - it asks a servo to
        identify itself and moves nothing, whatever state that servo is
        in.

        If an ST3215 is currently selected it is RELEASED first: the scan
        reopens UART2 at each rate it tries, and two owners of one
        peripheral is not a thing that can be made to work. The release
        is reported rather than done quietly.
        """
        released = None

        if self.servos.is_connected():
            released = self.servos.release(
                "released so the bus scan can reopen UART2"
            )
            self.carousel.attach_servo(None, "servo released for bus scan")

        ids = request.get("ids")

        if ids in ("all", "ALL"):
            # 0 to 253: 254 is the broadcast address and 255 is not an
            # address at all.
            ids = list(range(0, 254))

        elif isinstance(ids, list):
            ids = [int(value) for value in ids]

        elif ids is not None:
            ids = [int(ids)]

        bauds = request.get("bauds")

        if isinstance(bauds, list):
            bauds = [int(value) for value in bauds]

        elif bauds is not None:
            bauds = [int(bauds)]

        try:
            report = st3215_driver.bus_scan(
                bauds=bauds,
                servo_ids=ids,
                try_swapped_pins=bool(request.get("swap", True)),
                timeout_ms=request.get("timeout_ms"),
            )

        except Exception as error:
            raise CommandError(
                "SERVO_SCAN_FAILED",
                "The bus scan could not run: {}".format(error),
                data={"exception_type": type(error).__name__},
            )

        report["released_servo"] = released
        report["servo"] = self.servos.status()

        return report

    # ------------------------------------------------------------------
    # calibration
    # ------------------------------------------------------------------

    def handle_get_servo_calibration(self, request):
        """
        The servo's tunables.

        The `editable` flag is False for the ST3215: its limits are
        engineering values that belong under version control, not
        numbers to be trimmed at runtime. Nothing here is a timing
        calibration - every movement is verified against the encoder.
        """
        servo = self._require_servo()

        calibration = servo.calibration()

        calibration["servo_type"] = self.servos.servo_type
        calibration["capabilities"] = servo.capabilities()
        calibration["slot_step_deg"] = config.CAROUSEL_SLOT_GEOMETRY_DEG
        calibration["half_turn_deg"] = config.CAROUSEL_HALF_TURN_DEG

        return calibration

    def handle_set_servo_calibration(self, request):
        """
        Override the active backend's calibration in RAM.

        Runtime only, on purpose: nothing here rewrites config.py. The
        operator converges on real numbers with the carousel in front of
        them, then copies the printed block into config.py so it survives
        a reset.

        Guarded by the editable-calibration capability. The ST3215 answers
        SERVO_NOT_SUPPORTED rather than pretending to accept a value it
        would ignore.
        """
        servo = self._require_servo()

        if not self._capability("timed_positioning"):
            self._reject_unsupported(
                "Runtime calibration", "editable timing calibration"
            )

        try:
            if request.get("reset"):
                servo.reset_calibration()

                result = servo.calibration()
                result["changed"] = ["reset to config.py defaults"]

                return result

            values = request.get("values")

            if not isinstance(values, dict):
                raise CommandError(
                    "MISSING_FIELD",
                    "set_servo_calibration requires a 'values' object, or "
                    "'reset': true.",
                )

            changed = servo.set_calibration(values)

        except ServoNotSupportedError as error:
            raise CommandError(error.code, error.message)

        except ServoError as error:
            raise CommandError("BAD_REQUEST", error.message)

        result = servo.calibration()
        result["changed"] = changed

        return result

    # ------------------------------------------------------------------
    # movement tests
    # ------------------------------------------------------------------

    def handle_servo_test_move(self, request):
        """
        Run one movement test on the active backend.

        Requires 'confirm': true, because these movements turn a carousel
        that may have soil in it. The available kinds come from the
        driver, so the PC lists what the servo can actually do rather
        than a list compiled into the client:

            ST3215   one slot each way, the two 180 deg sweeps, the
                     out-and-back pair, and the encoder boundary test,
                     each leg verified against the encoder

        A test with net travel invalidates the carousel position. A
        symmetrical test on a backend that can PROVE it came back leaves
        the position intact; on an open-loop backend it never can, so the
        position goes regardless.
        """
        servo = self._require_servo()

        kinds = [name for name, _label in servo.test_move_kinds()]
        kind = request.get("kind")

        if not request.get("confirm"):
            raise CommandError(
                "CONFIRMATION_REQUIRED",
                "servo_test_move turns the carousel. Check that the "
                "mechanism is clear, then send 'confirm': true.",
                data={
                    "kind": kind,
                    "kinds": kinds,
                    "servo": self.servos.servo_type,
                },
            )

        if kind not in kinds:
            raise CommandError(
                "BAD_REQUEST",
                "kind must be one of: {}.".format(", ".join(kinds)),
                data={"kinds": kinds, "servo": self.servos.servo_type},
            )

        try:
            repeat = int(request.get("repeat", 1))

        except (TypeError, ValueError):
            raise CommandError("BAD_REQUEST", "'repeat' must be a number.")

        if repeat < 1 or repeat > 8:
            raise CommandError(
                "BAD_REQUEST", "'repeat' must be between 1 and 8."
            )

        degrees = None

        if kind == "degrees":
            if "degrees" not in request:
                raise CommandError(
                    "MISSING_FIELD",
                    "kind 'degrees' requires a 'degrees' field.",
                )

            try:
                degrees = float(request.get("degrees"))

            except (TypeError, ValueError):
                raise CommandError(
                    "BAD_REQUEST", "'degrees' must be a number."
                )

        hold_ms = None

        if kind == "neutral":
            try:
                hold_ms = int(request.get("hold_ms", 5000))

            except (TypeError, ValueError):
                raise CommandError(
                    "BAD_REQUEST", "'hold_ms' must be a number."
                )

        # An open-loop backend cannot prove anything about where a test
        # left the mechanism, so tracking goes before it moves. A
        # closed-loop backend keeps it for a symmetrical test and drops it
        # for one with net travel - which the backend reports afterwards.
        verified = self._capability("verified_movement")

        if not verified and kind != "neutral":
            self.carousel.invalidate_position(
                "open-loop servo movement test of unknown angle"
            )

        try:
            result = self._run_test(servo, kind, repeat, degrees, hold_ms)

        except ServoNotSupportedError as error:
            raise CommandError(error.code, error.message)

        except ServoError as error:
            self.carousel.invalidate_position("servo movement test failed")

            raise CommandError(
                error.code,
                error.message,
                data={
                    "kind": kind,
                    "servo": self.servos.servo_type,
                    "position_invalidated": True,
                },
            )

        if result.get("position_invalidated"):
            self.carousel.invalidate_position(
                "servo movement test with net travel"
            )

        result["servo_type"] = self.servos.servo_type
        result["verified_movement"] = verified
        result["servo"] = self.servos.status()
        result["carousel"] = self.carousel.status()

        return result

    def _run_test(self, servo, kind, repeat, degrees, hold_ms):
        """Call the backend's test with only the arguments it accepts."""
        if kind == "neutral":
            return servo.test_move(kind, repeat=repeat, hold_ms=hold_ms)

        return servo.test_move(kind, repeat=repeat, degrees=degrees)

    # ------------------------------------------------------------------
    # service operations
    # ------------------------------------------------------------------

    def handle_servo_configure(self, request):
        """
        SERVICE OPERATION: write the operating mode into servo EPROM.

        Kept firmly apart from carousel calibration. Ordinary carousel
        setup establishes a RUNTIME origin and touches no persistent servo
        state; this command is the only thing in the firmware that writes
        the servo's non-volatile memory, it is confirmed explicitly, and it
        is needed once per servo rather than once per session.

        Guarded by the persistent_config capability rather than by the
        servo's name, so a servo that cannot store settings answers
        SERVO_NOT_SUPPORTED instead of failing mid-write.
        """
        servo = self._require_servo()

        if not self._capability("persistent_config"):
            self._reject_unsupported(
                "Servo configuration", "persistent configuration memory"
            )

        if not request.get("confirm"):
            raise CommandError(
                "CONFIRMATION_REQUIRED",
                "servo_configure writes the servo EPROM. Send "
                "'confirm': true to proceed.",
                data={
                    "servo": self.servos.servo_type,
                    "mode": config.ST3215_MODE,
                    "writes_eprom": True,
                },
            )

        mode = request.get("mode", config.ST3215_MODE)

        try:
            result = servo.configure_mode(mode)

        except ServoError as error:
            raise CommandError(error.code, error.message)

        result["moved"] = False
        result["servo"] = self.servos.status()

        return result

    def handle_servo_torque(self, request):
        """
        Turn the servo's holding torque on or off, explicitly.

        Releasing torque is what lets the operator turn the carousel by
        hand during setup. It is never done automatically: between
        movements the carousel has to stay exactly where it was put, or a
        sample gets measured off centre.

        Releasing it also invalidates the tracked position, because a
        mechanism that can be turned by hand no longer has a position the
        firmware can vouch for.

        Guarded by the torque_control capability, so a servo with no
        torque switch answers SERVO_NOT_SUPPORTED rather than appearing
        to succeed.
        """
        servo = self._require_servo()

        if not self._capability("torque_control"):
            self._reject_unsupported(
                "Torque control", "switchable holding torque"
            )

        enable = request.get("enable", True)

        try:
            if enable:
                servo.enable_torque()

            else:
                servo.disable_torque()
                self.carousel.invalidate_position(
                    "servo torque was released, so the carousel can be "
                    "turned by hand"
                )

        except ServoError as error:
            raise CommandError(error.code, error.message)

        return {
            "torque_enabled": bool(enable),
            "moved": False,
            "servo": self.servos.status(),
            "carousel": self.carousel.status(),
        }
