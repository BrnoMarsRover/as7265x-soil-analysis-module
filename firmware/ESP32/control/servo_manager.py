# control/servo_manager.py
# The carousel actuator's lifecycle, and the gate every movement passes.
#
# There is one actuator: a Waveshare ST3215 serial bus servo. This module
# owns bringing it up, taking it down, and answering "is it usable?".
#
# WHY THIS STILL EXISTS WITH ONE SERVO
#
# It no longer chooses between backends - an earlier revision also
# supported an MG995 and this module picked between them. What it does
# is enforce, in exactly one place, that nothing moves the carousel
# before the UART is open and the servo has answered. That gate is worth
# a module on its own: a carousel that turns because a driver object
# happened to construct is a carousel turning with no idea where it is.
#
# The connection is RUNTIME state. After a reboot nothing is connected,
# because a reboot is exactly when someone may have unplugged something.
#
# This module owns actuator lifecycle. It owns no carousel geometry: it
# does not know what a slot is, and the carousel is attached to the
# backend by the caller.

from drivers import st3215 as st3215_driver
from drivers.servo_base import ServoError

SERVO_TYPE = "st3215"
SERVO_LABEL = "Waveshare ST3215"
SERVO_DESCRIPTION = "Serial bus servo, encoder-based positioning"


class ServoSelectionError(Exception):
    """
    Lifecycle fault carrying a machine-readable code.

    super().__init__ - MicroPython cannot call Exception.__init__ unbound.
    """

    def __init__(self, code, message, data=None):
        super().__init__(message)

        self.code = str(code)
        self.message = str(message)
        self.data = data


def servo_info():
    """What is fitted, with enough text for an operator screen."""
    return {
        "type": SERVO_TYPE,
        "label": SERVO_LABEL,
        "description": SERVO_DESCRIPTION,
    }


class ServoManager:
    """
    Holds at most one live ST3215 backend.

    Everything that moves the carousel goes through the backend this
    object hands out, so "not connected" is enforced in one place rather
    than remembered in twenty.
    """

    def __init__(self):
        self.servo = None

        # Why the connection last changed. Reported in status so an
        # operator can see that a reconnect happened.
        self.last_change = None

        self.connection_count = 0

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------

    @property
    def servo_type(self):
        """The fitted actuator, or None when it is not connected."""
        return SERVO_TYPE if self.servo is not None else None

    def is_connected(self):
        return self.servo is not None

    # Historical name. The carousel and the command layer ask "is a servo
    # available?", which used to mean "has the operator picked one".
    is_selected = is_connected

    def require_servo(self):
        """
        The single gate every movement passes through.

        Raises with a code the PC turns into "run option 0 first" rather
        than into a mysterious hardware error.
        """
        if not self.is_connected():
            raise ServoSelectionError(
                "SERVO_NOT_CONNECTED",
                "The ST3215 is not connected. Run option [0] Carousel "
                "Setup first.",
                data={"servo": servo_info()},
            )

        return self.servo

    def capabilities(self):
        if not self.is_connected():
            return None

        return self.servo.capabilities()

    def label(self):
        if not self.is_connected():
            return "NOT CONNECTED"

        return SERVO_LABEL

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def connect(self):
        """
        Open the link to the ST3215 and confirm it answers.

        Any previous backend is taken down first, and it is taken down
        even if bringing the new one up then fails: a still-open UART
        would be worse than no servo at all.

        Returns a record describing the change. The CALLER is responsible
        for invalidating the carousel position - this object deliberately
        knows nothing about slots.
        """
        previous = None
        was_connected = self.is_connected()

        if self.servo is not None:
            previous = self._release_backend()

        servo = st3215_driver.ST3215()

        try:
            initialization = servo.initialize()

        except ServoError as error:
            # Leave the manager empty rather than holding a backend that
            # never came up. The operator sees the real reason.
            self.servo = None

            raise ServoSelectionError(
                error.code,
                "The ST3215 could not be initialized: {}".format(
                    error.message
                ),
                data={
                    "servo": servo_info(),
                    "released_previous": previous,
                },
            )

        self.servo = servo
        self.connection_count += 1

        self.last_change = {
            "to": SERVO_TYPE,
            "reconnected": was_connected,
            "released_previous": previous,
        }

        return {
            "servo": SERVO_TYPE,
            "label": SERVO_LABEL,
            "description": SERVO_DESCRIPTION,
            "reconnected": was_connected,
            "released_previous": previous,
            "changed": True,
            "capabilities": servo.capabilities(),
            "initialization": initialization,
            "moved": False,
        }

    # Historical name, kept so existing callers and stored records still
    # resolve. The argument is accepted and checked rather than ignored,
    # so a caller asking for a servo this firmware no longer supports is
    # told so instead of silently getting the ST3215.
    def select(self, servo_type=None):
        requested = str(servo_type or SERVO_TYPE).strip().lower()

        if requested in ("", "none"):
            return self.release("operator disconnected the servo")

        if requested != SERVO_TYPE:
            raise ServoSelectionError(
                "INVALID_SERVO",
                "This firmware drives only the {}. '{}' is not "
                "available.".format(SERVO_LABEL, servo_type),
                data={"servo": servo_info()},
            )

        return self.connect()

    def _release_backend(self):
        """Take the current backend down, never raising on the way out."""
        if self.servo is None:
            return None

        try:
            result = self.servo.deinitialize()

        except Exception as error:
            result = {
                "servo": SERVO_TYPE,
                "released": False,
                "error": str(error),
            }

        self.servo = None

        return result

    def release(self, reason="released"):
        """Disconnect the actuator entirely, leaving the manager empty."""
        was_connected = self.is_connected()
        previous = self._release_backend()

        self.last_change = {
            "to": None,
            "reason": reason,
            "released_previous": previous,
        }

        return {
            "servo": None,
            "label": "NOT CONNECTED",
            "released_previous": previous,
            "changed": was_connected,
            "capabilities": None,
            "reason": reason,
            "moved": False,
        }

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------

    def status(self):
        """
        Whether the actuator is in charge, and what it can actually do.

        Always answers, even with nothing connected: "NOT CONNECTED" is
        the single most important thing an operator can be told about the
        carousel, and hiding it behind an error would be the wrong call.
        """
        report = {
            "connected": self.is_connected(),
            # Historical key, still read by the PC client and by stored
            # sample records.
            "selected": self.is_connected(),
            "type": self.servo_type,
            "label": self.label(),
            "servo": servo_info(),
            "connection_count": self.connection_count,
            "last_change": self.last_change,
        }

        if not self.is_connected():
            report["capabilities"] = None
            report["message"] = (
                "The ST3215 is not connected. Run option [0] Carousel "
                "Setup."
            )

            return report

        report["capabilities"] = self.servo.capabilities()
        report["description"] = SERVO_DESCRIPTION

        # The movement tests the backend offers, so the PC builds its
        # menu from the firmware rather than from a hardcoded list that
        # would drift the moment the driver gained a test.
        report["test_move_kinds"] = [
            {"kind": kind, "label": label}
            for kind, label in self.servo.test_move_kinds()
        ]

        try:
            report["backend"] = self.servo.status()

        except Exception as error:
            report["backend"] = {
                "servo": SERVO_TYPE,
                "error": {"code": "SERVO_ERROR", "message": str(error)},
            }

        return report

    def diagnostics(self):
        """Backend diagnostics, or a clear reason there are none."""
        servo = self.require_servo()

        report = servo.diagnostics()

        report["type"] = SERVO_TYPE
        report["label"] = SERVO_LABEL

        return report
