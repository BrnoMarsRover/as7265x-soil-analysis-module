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
# Rotation is therefore purely timed, and one carousel step (90 degrees)
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


# Runtime calibration keys, in the order the operator tunes them.
#
# Every movement value the servo uses lives in this one dictionary,
# seeded from config.py at construction. The calibration screen edits it
# in RAM so the operator can converge on real numbers without an
# upload-and-reset cycle per attempt; the values are then pasted back
# into config.py, which stays the source of truth across a reset.
CALIBRATION_KEYS = (
    "neutral_us",
    "cw_us",
    "ccw_us",
    "slot_cw_ms",
    "slot_ccw_ms",
    "load_to_scan_ms",
    "scan_to_load_ms",
    "settle_ms",
    "approach_ms",
    "approach_us_offset",
    "start_kick_ms",
    "start_kick_us_offset",
    "brake_ms",
)

# Guard rails. A pulse far from neutral would drive the carousel fast
# enough to lose position, and a servo pulse outside roughly 1000-2000 us
# is meaningless.
PULSE_MIN_US = 1000
PULSE_MAX_US = 2000
MAX_MOVE_MS = 30000


def default_calibration():
    """The shipped calibration, read from config.py."""
    return {
        "neutral_us": config.SERVO_STOP_US,
        "cw_us": config.SERVO_CW_US,
        "ccw_us": config.SERVO_CCW_US,
        "slot_cw_ms": config.NEXT_SLOT_CW_MS,
        "slot_ccw_ms": config.NEXT_SLOT_CCW_MS,
        "load_to_scan_ms": config.LOAD_TO_SCAN_CW_MS,
        "scan_to_load_ms": config.SCAN_TO_LOAD_CCW_MS,
        "settle_ms": config.SERVO_SETTLE_MS,
        "approach_ms": config.SERVO_APPROACH_MS,
        "approach_us_offset": config.SERVO_APPROACH_US_OFFSET,
        "start_kick_ms": config.SERVO_START_KICK_MS,
        "start_kick_us_offset": config.SERVO_START_KICK_US_OFFSET,
        "brake_ms": config.SERVO_BRAKE_MS,
    }


def corrected_duration(duration_ms, target_deg, error_deg):
    """
    Re-time a move from the angular error it actually produced.

    At an approximately constant speed the angle is proportional to the
    runtime, so:

        actual = target + error
        t_new  = t_old * target / actual

    A +4 deg overshoot on a 90 deg move at 1200 ms gives
    1200 * 90 / 94 = 1149 ms. This is what makes calibration converge
    instead of being a guessing game.

    Returns None when the correction is meaningless - a zero or negative
    resulting angle means the move did not do what was being measured.
    """
    actual = float(target_deg) + float(error_deg)

    if actual <= 0 or duration_ms <= 0:
        return None

    return int(round(float(duration_ms) * float(target_deg) / actual))


def angular_speed(duration_ms, degrees):
    """Effective deg/s implied by a calibrated move."""
    if not duration_ms:
        return None

    return round(float(degrees) * 1000.0 / float(duration_ms), 2)


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

        self.calibration = default_calibration()

    # ------------------------------------------------------------------
    # calibration
    # ------------------------------------------------------------------

    def get_calibration(self):
        """Current values plus what config.py would restore."""
        current = {}

        for key in CALIBRATION_KEYS:
            current[key] = self.calibration[key]

        return {
            "current": current,
            "config_defaults": default_calibration(),
            "modified": current != default_calibration(),
            "persistent": False,

            # Effective speed implied by each calibrated move, so the
            # numbers can be sanity-checked against each other: the two
            # 180 deg figures should land near the matching 90 deg one.
            "speed_deg_per_s": {
                "slot_cw": angular_speed(
                    current["slot_cw_ms"], config.SLOT_STEP_DEG
                ),
                "slot_ccw": angular_speed(
                    current["slot_ccw_ms"], config.SLOT_STEP_DEG
                ),
                "load_to_scan": angular_speed(
                    current["load_to_scan_ms"], config.CAROUSEL_HALF_TURN_DEG
                ),
                "scan_to_load": angular_speed(
                    current["scan_to_load_ms"], config.CAROUSEL_HALF_TURN_DEG
                ),
            },
            "note": "Runtime only. Copy the values into "
                    "firmware/ESP32/config.py to keep them across a reset.",
        }

    def set_calibration(self, values):
        """
        Override calibration in RAM, with range checks.

        Returns the list of keys actually changed so the caller can
        report exactly what happened.
        """
        if not isinstance(values, dict):
            raise ServoError("calibration must be an object")

        changed = []

        for key in values:
            if key not in CALIBRATION_KEYS:
                raise ServoError("unknown calibration key: {}".format(key))

            value = values[key]

            try:
                value = int(value)

            except (TypeError, ValueError):
                raise ServoError(
                    "{} must be a whole number".format(key)
                )

            if key.endswith("_us") and key != "approach_us_offset":
                if not (PULSE_MIN_US <= value <= PULSE_MAX_US):
                    raise ServoError(
                        "{} must be between {} and {} us".format(
                            key, PULSE_MIN_US, PULSE_MAX_US
                        )
                    )

            elif value < 0:
                raise ServoError("{} must not be negative".format(key))

            elif key.endswith("_ms") and value > MAX_MOVE_MS:
                raise ServoError(
                    "{} must be at most {} ms".format(key, MAX_MOVE_MS)
                )

            if self.calibration[key] != value:
                self.calibration[key] = value
                changed.append(key)

        return changed

    def reset_calibration(self):
        """Go back to the values in config.py."""
        self.calibration = default_calibration()

        return self.calibration

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
            return self.calibration["cw_us"]

        if direction == CCW:
            return self.calibration["ccw_us"]

        raise ServoError("unknown direction: {}".format(direction))

    def _approach_pulse_for(self, direction):
        """
        Pulse for the slow final approach: the movement pulse, nudged
        back towards neutral so the carousel coasts into position.
        """
        neutral = self.calibration["neutral_us"]
        offset = self.calibration["approach_us_offset"]
        pulse = self._pulse_for(direction)

        if pulse > neutral:
            return max(neutral + 1, pulse - offset)

        return min(neutral - 1, pulse + offset)

    def next_slot_ms(self, direction):
        """
        Calibrated runtime for ONE adjacent-slot transition.

        Both directions travel the same 90 degrees, but they need
        different runtimes: a continuous-rotation servo does not turn at
        the same speed clockwise and counter-clockwise.
        """
        if direction == CW:
            return self.calibration["slot_cw_ms"]

        if direction == CCW:
            return self.calibration["slot_ccw_ms"]

        raise ServoError("unknown direction: {}".format(direction))

    def slot_step_deg(self, direction=None):
        """
        Angle of one logical slot transition: 90 degrees, either way.

        Only the TIMING differs between directions, because the servo
        does not run at the same speed clockwise and counter-clockwise.
        """
        return config.SLOT_STEP_DEG

    def half_turn_ms(self, direction):
        """
        Calibrated runtime for the 180 degree loader <-> scanner sweep.

        Independent of next_slot_ms on purpose: a continuous servo pays
        its acceleration ramp once per move, so two adjacent-slot moves
        and one long sweep do not cover the same angle.
        """
        if direction == CW:
            return self.calibration["load_to_scan_ms"]

        if direction == CCW:
            return self.calibration["scan_to_load_ms"]

        raise ServoError("unknown direction: {}".format(direction))

    # ------------------------------------------------------------------
    # public control
    # ------------------------------------------------------------------

    def stop(self):
        """Apply the calibrated neutral pulse so the servo brakes."""
        self._pulse(self.calibration["neutral_us"])
        self.moving = False

    def hold_neutral(self, duration_ms):
        """
        Hold the neutral pulse for a while, without releasing the pin.

        Used to check for creep during calibration: if the carousel
        drifts while this runs, the neutral pulse is wrong and nothing
        else is worth calibrating yet.
        """
        duration_ms = int(duration_ms)

        if duration_ms < 0 or duration_ms > MAX_MOVE_MS:
            raise ServoError(
                "hold duration must be between 0 and {} ms".format(
                    MAX_MOVE_MS
                )
            )

        self._pulse(self.calibration["neutral_us"])
        self.moving = False

        time.sleep_ms(duration_ms)

        if config.SERVO_RELEASE_AFTER_MOVE:
            self.release()

        return {
            "neutral_us": self.calibration["neutral_us"],
            "held_ms": duration_ms,
        }

    def rotate_cw(self):
        """Start turning clockwise. Does not return the servo to stop."""
        self._pulse(self.calibration["cw_us"])
        self.moving = True
        self.last_direction = CW

    def rotate_ccw(self):
        """Start turning counter-clockwise. Does not stop the servo."""
        self._pulse(self.calibration["ccw_us"])
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

    def _kick_pulse_for(self, direction):
        """Briefly stronger pulse, to break static friction on starting."""
        neutral = self.calibration["neutral_us"]
        offset = self.calibration["start_kick_us_offset"]
        pulse = self._pulse_for(direction)

        if pulse > neutral:
            return min(PULSE_MAX_US, pulse + offset)

        return max(PULSE_MIN_US, pulse - offset)

    def _run(self, direction, duration_ms):
        """
        One timed move, in up to four phases:

            kick      brief stronger pulse so a near-deadband cruise
                      speed still starts reliably          (default off)
            cruise    the calibrated slow precision pulse
            approach  final stretch closer to neutral, to coast in
                      rather than stop dead                (default off)
            brake     brief reverse burst to kill inertia  (default off)

        Only cruise runs by default. The three optional phases exist so
        they can be measured against plain stopping, not because they
        are assumed to help - active braking in particular is not
        automatically better than simply going to neutral.

        The kick and approach are taken OUT of the requested duration,
        so total commanded time stays exactly `duration_ms` and the
        angular calibration keeps its meaning. Braking happens after.

        No settling and no release, so multi-step moves can chain
        several of these. The neutral pulse is applied from a finally
        block, so an interrupt or an error can never leave the servo
        running.
        """
        duration_ms = int(duration_ms)

        if duration_ms <= 0:
            return

        kick_ms = int(self.calibration["start_kick_ms"])
        approach_ms = int(self.calibration["approach_ms"])
        brake_ms = int(self.calibration["brake_ms"])

        # The optional phases must never eat the move they are shaping.
        if kick_ms + approach_ms >= duration_ms:
            kick_ms = 0
            approach_ms = 0

        cruise_ms = duration_ms - kick_ms - approach_ms

        self.moving = True
        self.last_direction = direction
        self.last_duration_ms = duration_ms

        try:
            if kick_ms > 0:
                self._pulse(self._kick_pulse_for(direction))
                time.sleep_ms(kick_ms)

            self._pulse(self._pulse_for(direction))
            time.sleep_ms(cruise_ms)

            if approach_ms > 0:
                self._pulse(self._approach_pulse_for(direction))
                time.sleep_ms(approach_ms)

            if brake_ms > 0:
                self._pulse(self._pulse_for(opposite(direction)))
                time.sleep_ms(brake_ms)

        finally:
            self._pulse(self.calibration["neutral_us"])
            self.moving = False

    def _settle(self):
        """Hold the neutral pulse, then optionally release the pin."""
        settle_ms = int(self.calibration["settle_ms"])

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
        Rotate by a whole number of adjacent-slot transitions (90 deg each).

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

    def run_calibration_move(self, direction, duration_ms, repeat=1):
        """
        Run a raw timed move N times, for calibration only.

        Repeating the same move is how repeatability is judged: four
        90 degree steps should come back to the starting orientation,
        and an out-and-back pair should land where it began. Deliberately
        does not touch any tracked position - the caller owns that.
        """
        duration_ms = int(duration_ms)
        repeat = int(repeat)

        if duration_ms < 0 or duration_ms > MAX_MOVE_MS:
            raise ServoError(
                "duration must be between 0 and {} ms".format(MAX_MOVE_MS)
            )

        if repeat < 1:
            raise ServoError("repeat must be at least 1")

        for index in range(repeat):
            self._run(direction, duration_ms)

            if index < repeat - 1:
                time.sleep_ms(config.SERVO_INTER_STEP_PAUSE_MS)

        self._settle()

        return {
            "direction": direction,
            "duration_ms": duration_ms,
            "repeat": repeat,
            "total_duration_ms": duration_ms * repeat,
            "pulse_us": self._pulse_for(direction),
            "neutral_us": self.calibration["neutral_us"],
            "settle_ms": self.calibration["settle_ms"],
            "approach_ms": self.calibration["approach_ms"],
        }

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
        """Live values, so a runtime override is never hidden."""
        return {
            "pin": self.pin_num,
            "pwm_active": self.pwm is not None,
            "moving": self.moving,
            "last_direction": self.last_direction,
            "last_duration_ms": self.last_duration_ms,
            "stop_us": self.calibration["neutral_us"],
            "cw_us": self.calibration["cw_us"],
            "ccw_us": self.calibration["ccw_us"],
            "next_slot_cw_ms": self.calibration["slot_cw_ms"],
            "next_slot_ccw_ms": self.calibration["slot_ccw_ms"],
            "load_to_scan_cw_ms": self.calibration["load_to_scan_ms"],
            "scan_to_load_ccw_ms": self.calibration["scan_to_load_ms"],
            "settle_ms": self.calibration["settle_ms"],
            "approach_ms": self.calibration["approach_ms"],
            "calibration_modified": (
                self.calibration != default_calibration()
            ),
            "slot_step_deg": config.SLOT_STEP_DEG,
            "cw_ms_per_degree": config.CW_MS_PER_DEGREE,
            "ccw_ms_per_degree": config.CCW_MS_PER_DEGREE,
            "slot_geometry_deg": config.CAROUSEL_SLOT_GEOMETRY_DEG
        }
