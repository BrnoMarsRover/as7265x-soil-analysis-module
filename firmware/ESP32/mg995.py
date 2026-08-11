# mg995.py
# Driver for an MG995 modified for 360 degree / continuous rotation.
#
# A continuous-rotation servo has NO angle command. set_angle() is
# meaningless for this hardware. The only things it understands are:
#
#   - a neutral pulse width, at which it stands still,
#   - pulse widths above and below neutral, which make it turn, at a
#     speed proportional to the distance from neutral,
#   - and time.
#
# Rotation is therefore purely timed, and one carousel step (45 degrees)
# is a calibrated duration held in config.py.
#
# The PWM peripheral is created lazily, on the first movement command.
# Nothing drives the servo pin at boot, so powering the ESP32 can never
# nudge the carousel out of its known position.

import time

from machine import Pin, PWM

import config


CW = "cw"
CCW = "ccw"

DIRECTIONS = (CW, CCW)


class ServoError(Exception):
    """Raised when the servo cannot execute a requested movement."""


def opposite(direction):
    """Return the direction opposite to the given one."""
    if direction == CW:
        return CCW

    if direction == CCW:
        return CW

    raise ServoError("unknown direction: {}".format(direction))


class MG995:
    """Timed continuous-rotation servo on a single PWM pin."""

    def __init__(self, pin_num=None, freq=None):
        if pin_num is None:
            pin_num = config.SERVO_PIN

        if freq is None:
            freq = config.SERVO_PWM_FREQ

        self.pin_num = pin_num
        self.freq = freq

        # Created on first use, released again after each move.
        self.pwm = None

        self.moving = False
        self.last_direction = None
        self.last_duration_ms = 0

    # ------------------------------------------------------------------
    # low level
    # ------------------------------------------------------------------

    def _ensure_pwm(self):
        if self.pwm is None:
            self.pwm = PWM(
                Pin(self.pin_num),
                freq=self.freq
            )

        return self.pwm

    def _pulse(self, microseconds):
        """Drive the signal pin with the given pulse width."""
        pwm = self._ensure_pwm()
        pwm.duty_ns(int(microseconds) * 1000)

    def _pulse_for(self, direction):
        if direction == CW:
            return config.SERVO_CW_US

        if direction == CCW:
            return config.SERVO_CCW_US

        raise ServoError("unknown direction: {}".format(direction))

    def next_slot_ms(self, direction):
        """
        Calibrated runtime for ONE adjacent-slot transition.

        Both directions travel the same 45 degrees, but they need
        different runtimes: a continuous-rotation servo does not turn at
        the same speed clockwise and counter-clockwise.
        """
        if direction == CW:
            return config.NEXT_SLOT_CW_MS

        if direction == CCW:
            return config.NEXT_SLOT_CCW_MS

        raise ServoError("unknown direction: {}".format(direction))

    def slot_step_deg(self, direction=None):
        """
        Angle of one logical slot transition: 45 degrees, either way.

        Only the TIMING differs between directions, because the servo
        does not run at the same speed clockwise and counter-clockwise.
        """
        return config.SLOT_STEP_DEG

    def half_turn_ms(self, direction):
        """
        Calibrated runtime for the 180 degree loader <-> scanner sweep.

        Independent of next_slot_ms on purpose. Four adjacent-slot moves
        would command roughly 160 degrees of effective rotation, which
        does not reach the scanner.
        """
        if direction == CW:
            return config.LOAD_TO_SCAN_CW_MS

        if direction == CCW:
            return config.SCAN_TO_LOAD_CCW_MS

        raise ServoError("unknown direction: {}".format(direction))

    # ------------------------------------------------------------------
    # public control
    # ------------------------------------------------------------------

    def stop(self):
        """Apply the neutral pulse so the servo brakes and holds."""
        self._pulse(config.SERVO_STOP_US)
        self.moving = False

    def rotate_cw(self):
        """Start turning clockwise. Does not return the servo to stop."""
        self._pulse(config.SERVO_CW_US)
        self.moving = True
        self.last_direction = CW

    def rotate_ccw(self):
        """Start turning counter-clockwise. Does not stop the servo."""
        self._pulse(config.SERVO_CCW_US)
        self.moving = True
        self.last_direction = CCW

    def release(self):
        """Cut the PWM signal entirely and free the pin."""
        if self.pwm is None:
            return

        try:
            self.pwm.deinit()

        finally:
            self.pwm = None
            self.moving = False

    # ------------------------------------------------------------------
    # timed movement
    # ------------------------------------------------------------------

    def _run(self, direction, duration_ms):
        """
        Raw timed rotation: start, wait, brake.

        No settling and no release, so that multi-step moves can chain
        several of these together. The stop pulse is applied from a
        finally block, so an interrupt or an error can never leave the
        servo running.
        """
        duration_ms = int(duration_ms)

        if duration_ms <= 0:
            return

        pulse = self._pulse_for(direction)

        self.moving = True
        self.last_direction = direction
        self.last_duration_ms = duration_ms

        try:
            self._pulse(pulse)
            time.sleep_ms(duration_ms)

        finally:
            self._pulse(config.SERVO_STOP_US)
            self.moving = False

    def _settle(self):
        """Hold the stop pulse, then optionally release the pin."""
        settle_ms = int(config.SERVO_SETTLE_TIME * 1000)

        if settle_ms > 0:
            time.sleep_ms(settle_ms)

        if config.SERVO_RELEASE_AFTER_MOVE:
            self.release()

    def rotate_for_ms(self, direction, duration_ms):
        """
        Rotate for an arbitrary duration.

        Used for manual jogging and for calibration. The resulting angle
        is unknown to the software, so the caller is responsible for
        invalidating any tracked carousel position.
        """
        duration_ms = int(duration_ms)

        if duration_ms < 0:
            raise ServoError("duration must not be negative")

        if duration_ms > config.SERVO_JOG_MAX_MS:
            raise ServoError(
                "duration {} ms exceeds SERVO_JOG_MAX_MS ({} ms)".format(
                    duration_ms,
                    config.SERVO_JOG_MAX_MS
                )
            )

        self._run(direction, duration_ms)
        self._settle()

        return duration_ms

    def ms_per_degree(self, direction):
        """
        Runtime per degree, used only for fine alignment.

        A dedicated calibration rather than something derived from the
        slot timing: small corrections and full slot transitions do not
        share the same acceleration behaviour.
        """
        if direction == CW:
            return float(config.CW_MS_PER_DEGREE)

        if direction == CCW:
            return float(config.CCW_MS_PER_DEGREE)

        raise ServoError("unknown direction: {}".format(direction))

    def rotate_degrees(self, degrees):
        """
        Rotate by a signed angle: positive clockwise, negative
        counter-clockwise.

        Milliseconds are an internal implementation detail. The operator
        works in slots and degrees; the conversion happens here and is
        reported back so it can be inspected in debug mode.
        """
        degrees = float(degrees)

        if degrees > 0:
            direction = CW
        elif degrees < 0:
            direction = CCW
        else:
            return {
                "degrees": 0.0,
                "direction": None,
                "ms_per_degree": None,
                "calculated_ms": 0.0,
                "duration_ms": 0,
                "moved": False
            }

        per_degree = self.ms_per_degree(direction)
        calculated_ms = abs(degrees) * per_degree
        duration_ms = int(round(calculated_ms))

        # One PWM frame. A command shorter than this carries at most a
        # single pulse, so the servo may not respond to it at all. That
        # is reported rather than hidden: it usually means the requested
        # angle is below what this calibration can physically resolve.
        period_ms = 1000.0 / float(self.freq)
        reliable = duration_ms >= period_ms

        # A request so small it rounds to no runtime at all is reported
        # honestly rather than pretending the carousel moved.
        if duration_ms <= 0:
            return {
                "degrees": degrees,
                "direction": direction,
                "ms_per_degree": per_degree,
                "calculated_ms": calculated_ms,
                "duration_ms": 0,
                "pwm_period_ms": period_ms,
                "reliable": False,
                "moved": False
            }

        self._run(direction, duration_ms)
        self._settle()

        return {
            "degrees": degrees,
            "direction": direction,
            "ms_per_degree": per_degree,
            "calculated_ms": calculated_ms,
            "duration_ms": duration_ms,
            "pwm_period_ms": period_ms,
            "reliable": reliable,
            "moved": True
        }

    def rotate_slots(self, direction, slots):
        """
        Rotate by a whole number of adjacent-slot transitions.

        Executed as discrete single-slot moves with a short pause between
        them, each using the calibrated adjacent-slot runtime. A
        continuous servo needs time to accelerate, so one long run would
        overshoot: the acceleration ramp would be paid once but budgeted
        N times. Stepping keeps the calibration linear in the slot count.

        This is NOT how the 180 degree half turn is produced - see
        rotate_half_turn().
        """
        slots = int(slots)

        if slots < 0:
            raise ServoError("slot count must not be negative")

        if slots == 0:
            return 0

        step_ms = self.next_slot_ms(direction)

        for index in range(slots):
            self._run(direction, step_ms)

            if index < slots - 1:
                time.sleep_ms(
                    config.SERVO_INTER_STEP_PAUSE_MS
                )

        self._settle()

        return slots

    # Historical name kept so existing callers and scripts keep working.
    rotate_steps = rotate_slots

    def rotate_half_turn(self, direction):
        """
        One continuous 180 degree sweep between loader and scanner.

        Uses its own calibrated runtime. Deliberately a single sweep
        rather than four slot moves, both because the effective angle of
        four adjacent moves falls short of 180 degrees and because one
        long run pays the acceleration ramp only once.
        """
        duration_ms = int(self.half_turn_ms(direction))

        self._run(direction, duration_ms)
        self._settle()

        return {
            "direction": direction,
            "degrees": config.CAROUSEL_HALF_TURN_DEG,
            "duration_ms": duration_ms,
            "independent_calibration": True
        }

    # ------------------------------------------------------------------
    # diagnostics
    # ------------------------------------------------------------------

    def status(self):
        return {
            "pin": self.pin_num,
            "pwm_active": self.pwm is not None,
            "moving": self.moving,
            "last_direction": self.last_direction,
            "last_duration_ms": self.last_duration_ms,
            "stop_us": config.SERVO_STOP_US,
            "cw_us": config.SERVO_CW_US,
            "ccw_us": config.SERVO_CCW_US,
            "next_slot_cw_ms": config.NEXT_SLOT_CW_MS,
            "next_slot_ccw_ms": config.NEXT_SLOT_CCW_MS,
            "slot_step_deg": config.SLOT_STEP_DEG,
            "load_to_scan_cw_ms": config.LOAD_TO_SCAN_CW_MS,
            "scan_to_load_ccw_ms": config.SCAN_TO_LOAD_CCW_MS,
            "cw_ms_per_degree": config.CW_MS_PER_DEGREE,
            "ccw_ms_per_degree": config.CCW_MS_PER_DEGREE,
            "slot_geometry_deg": config.CAROUSEL_SLOT_GEOMETRY_DEG
        }
