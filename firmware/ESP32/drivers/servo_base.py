# drivers/servo_base.py
# Rotation vocabulary, and the servo error base.
#
# The carousel talks about turning: clockwise, counter-clockwise, by an
# angle, back to the middle of a circle. The driver talks about encoder
# counts and register writes. This module holds the first of those two
# vocabularies, so that control/carousel.py can reason about direction
# and angle without importing a driver's internals.
#
# That is the whole job. There is no abstract backend class here.
#
# An earlier revision defined a ServoBackend contract because there were
# two actuators to keep honest - an MG995 driven open-loop on timed
# pulses, and the ST3215. The MG995 has been removed, and an abstract
# base with exactly one implementation is not an abstraction, it is a
# second file to read before understanding the first. ST3215 now stands
# on its own.
#
# What survives is what was never about having two backends: an angle on
# a rotary axis still needs reducing to (-180, +180], and a movement
# fault still needs a machine-readable code.
#
# Dependency direction: control/ imports drivers/, never the reverse. A
# driver must not know that a carousel, a slot or a sample exists.


# ======================================================================
# rotation vocabulary
# ======================================================================

# Direction labels, shared between the carousel and the driver.
#
# What "clockwise" MEANS physically is the driver's business: the ST3215
# gets a positive or negative step count. Which way the CAROUSEL must
# turn to advance a slot number is a third, mechanical question and
# lives in config.CAROUSEL_FORWARD_DIRECTION.
CW = "cw"
CCW = "ccw"

DIRECTIONS = (CW, CCW)


def opposite(direction):
    """Return the direction opposite to the given one."""
    if direction == CW:
        return CCW

    if direction == CCW:
        return CW

    raise ServoError("unknown direction: {}".format(direction))


def direction_sign(direction):
    """+1 for clockwise, -1 for counter-clockwise."""
    if direction == CW:
        return 1

    if direction == CCW:
        return -1

    raise ServoError("unknown direction: {}".format(direction))


def centred_degrees(degrees):
    """
    Reduce an angle to (-180, +180].

    The carousel compares an expected angle against a measured one on a
    rotary axis, where 350 degrees and -10 degrees are the same place.
    Both sides go through here before they are subtracted.
    """
    return ((float(degrees) + 180.0) % 360.0) - 180.0


def validate_direction(direction):
    if direction not in DIRECTIONS:
        raise ServoError(
            "direction must be '{}' or '{}', not {}".format(
                CW, CCW, direction
            ),
            code="SERVO_BAD_DIRECTION",
        )

    return direction


# ======================================================================
# errors
# ======================================================================

class ServoError(Exception):
    """
    Actuator fault carrying a machine-readable code.

    The driver raises subclasses of this, so the carousel can handle a
    movement failure through one except clause and the PC can turn the
    code into an instruction instead of a stack trace.

    super().__init__ - MicroPython cannot call Exception.__init__ unbound.
    """

    code = "SERVO_ERROR"

    def __init__(self, message, code=None):
        super().__init__(message)

        self.message = str(message)

        if code is not None:
            self.code = str(code)

    def as_dict(self):
        return {"code": self.code, "message": self.message}


class ServoNotSupportedError(ServoError):
    """The servo cannot do this. Not a fault - a capability."""

    code = "SERVO_NOT_SUPPORTED"


# ======================================================================
# capability reporting
# ======================================================================

# Every capability a caller may ask about. Named so a test can assert
# that the driver declares all of them - a missing key would silently
# read as False at exactly the moment it mattered.
CAPABILITY_KEYS = (
    "position_feedback",
    "encoder",
    "timed_positioning",
    "telemetry",
    "torque_control",
    "verified_movement",
    "persistent_config",
)


def capabilities(position_feedback=False, encoder=False,
                 timed_positioning=False, telemetry=False,
                 torque_control=False, verified_movement=False,
                 persistent_config=False):
    """
    Build a complete capability dictionary; no key is ever missing.

    Callers ask what the servo can do rather than testing its name, and
    a missing key would read as False at exactly the moment it mattered.
    """
    return {
        "position_feedback": bool(position_feedback),
        "encoder": bool(encoder),
        "timed_positioning": bool(timed_positioning),
        "telemetry": bool(telemetry),
        "torque_control": bool(torque_control),
        "verified_movement": bool(verified_movement),
        "persistent_config": bool(persistent_config),
    }
