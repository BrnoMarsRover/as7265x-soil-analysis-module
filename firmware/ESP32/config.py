# config.py
#
# Hardware configuration for the ESP32 side of the Freya science module.
#
# ONE source of truth. No module may hardcode a value that appears here:
# a duplicated gain or integration setting is how the sensor ended up
# running at defaults while the firmware reported the intended values.
#
# Nothing scientific lives here. Analysis thresholds, the calibration ID
# and the database paths belong to firmware/BD/config.py, which never
# reaches the device.

FIRMWARE_NAME = "freya-science-module"
FIRMWARE_VERSION = "4.0.0"

# Bumped when the shape of an acquisition response changes. 2 is the
# WHITE/UV/IR one-shot protocol with repeats; 1 was the single
# continuous-mode white spectrum.
ACQUISITION_PROTOCOL_VERSION = 2

# ==========================================
# I2C & AS7265x
# ==========================================
I2C_BUS = 0
I2C_SDA_PIN = 21
I2C_SCL_PIN = 22
I2C_FREQ = 100000

# Verified on the real board: the AS7265x master answers here.
AS7265X_ADDRESS = 0x49

# Acquisition settings. These are WRITTEN to the sensor and then READ
# BACK and verified during initialization, so what the PC is told is
# what the silicon is actually doing.
SENSOR_INTEGRATION_CYCLES = 100

# Gain: 0b00 = 1x, 0b01 = 3.7x, 0b10 = 16x, 0b11 = 64x
SENSOR_GAIN = 0b10

# Measurement mode:
#   0b10 = 6 channels per device, CONTINUOUS
#   0b11 = all channels, ONE-SHOT
#
# One-shot. Continuous mode free-runs, so a read can return a
# conversion that started before the illumination was switched - the
# spectrum and the lamp that produced it are not guaranteed to belong
# together. One-shot makes every acquisition deterministic: arm it
# after the LED is on and settled, wait for DATA_READY, read once.
SENSOR_MEASUREMENT_MODE = 0b11

# Illumination bulb current, per lamp.
#   0b00 = 12.5mA, 0b01 = 25mA, 0b10 = 50mA, 0b11 = 100mA
#
# Conservative 25 mA on all three, matching what the module has always
# run the white lamp at. Never raise these just to get bigger numbers -
# that changes the optical conditions the calibration was taken under.
WHITE_LED_CURRENT = 0b01
UV_LED_CURRENT = 0b01
IR_LED_CURRENT = 0b01

# Kept as the historical name for the white lamp current.
ONBOARD_LED_CURRENT = WHITE_LED_CURRENT

# Human readable names, used only when reporting hardware state.
SENSOR_GAIN_NAMES = {0: "1x", 1: "3.7x", 2: "16x", 3: "64x"}
SENSOR_LED_CURRENT_NAMES = {0: "12.5mA", 1: "25mA", 2: "50mA", 3: "100mA"}
SENSOR_MEASUREMENT_MODE_NAMES = {
    0: "4-channel", 1: "4-channel alt",
    2: "6-channel continuous", 3: "one-shot",
}

# ==========================================
# REPEATED ACQUISITION
# ==========================================
# A single reading is not a scientific measurement. Both counts are
# configurable and deliberately independent: calibration is done once
# and can afford to be slow, a rover sample cannot.
CALIBRATION_REPEATS = 10
SAMPLE_REPEATS = 3

# Pause between repeats within one block, so consecutive conversions
# are not correlated by the same electrical transient.
REPEAT_DELAY_MS = 60

# Upper bound accepted from the PC, as a runaway guard.
MAX_REPEATS = 25

# Seconds to wait after boot before touching the I2C bus, so the 3.3 V
# rail and the AS7265x internal startup are finished.
STARTUP_DELAY_SECONDS = 3

# Bounded sensor bring-up. A single failed attempt at boot must never
# become permanent runtime state, so initialization retries a few times
# and every later sensor operation may retry again on its own.
SENSOR_INIT_ATTEMPTS = 4
SENSOR_INIT_RETRY_MS = 400

# Bus-level scan retries inside one initialization attempt.
I2C_SCAN_ATTEMPTS = 3
I2C_SCAN_RETRY_MS = 150

# Longest wait for one virtual-register transaction.
VIRTUAL_REGISTER_TIMEOUT_MS = 1071

# Milliseconds between polls of STATUS / DATA_READY.
POLLING_DELAY_MS = 5

# Milliseconds the white LED is on before a measurement starts, so the
# illumination is stable across the whole integration window.
ILLUMINATION_SETTLE_MS = 300

# ==========================================
# SERVO (MG995, modified for continuous rotation)
# ==========================================
SERVO_PIN = 25
SERVO_PWM_FREQ = 50

# ------------------------------------------------------------------
# !! EVERY PULSE AND TIMING BELOW MUST BE CALIBRATED ON THE REAL
#    MECHANISM. THE SHIPPED VALUES ARE STARTING POINTS, NOT MEASURED. !!
#
# A continuous-rotation servo has no angle command. It has a neutral
# pulse where it stands still, pulses either side of neutral that make
# it turn at a speed depending on the distance from neutral, and TIME.
# There is no encoder, so no calculation can guarantee an exact angle -
# only careful calibration can make the open loop REPEATABLE.
#
# PRECISION IS PRIORITISED OVER SPEED. The pulses below sit deliberately
# close to neutral: a slow servo is far easier to time accurately, and a
# slow approach overshoots less. Do not "speed the carousel up" - that
# trade was made on purpose.
#
# Use the PC client: Tools -> Servo / Carousel Test -> Calibration. It
# tunes these values at runtime and prints the block to paste back here.
# ------------------------------------------------------------------

# TRUE NEUTRAL. Calibrate this FIRST and on its own: hold it for several
# seconds and trim in 1-2 us steps until the carousel creeps in neither
# direction. Everything else is meaningless until this is right, and it
# is often NOT exactly 1500.
SERVO_STOP_US = 1500

# Direction pulses, as absolute pulse widths. "CW"/"CCW" are labels; if
# the observed direction is inverted, swap these two values or flip
# CAROUSEL_FORWARD_DIRECTION.
#
# These are the smallest offsets from neutral that still turn the servo
# reliably, plus a small margin. Calibrate each direction INDEPENDENTLY:
# a continuous servo is not symmetric, and CW may well need a different
# offset from CCW.
#
#     shipped: neutral +/- 55 us  (previously +/- 100 us)
#
# Smaller offset -> slower -> more repeatable. Reduce further only while
# the servo still starts reliably every time and never stalls.
SERVO_CW_US = 1555
SERVO_CCW_US = 1445

# Optional slow final approach, to take out overshoot: the last
# SERVO_APPROACH_MS of a move run at a pulse this many microseconds
# closer to neutral.
#
# DISABLED BY DEFAULT (0 ms). A well-calibrated single slow pulse is
# preferable to an elaborate untested motion profile. Enable it only if
# hardware testing shows the carousel consistently overshoots.
SERVO_APPROACH_MS = 0
SERVO_APPROACH_US_OFFSET = 25

# Optional start kick. A pulse chosen close to the deadband for
# precision may not always break static friction; a brief stronger pulse
# at the start of every move fixes that without affecting cruise speed.
#
# DISABLED BY DEFAULT (0 ms). Enable only if the carousel sometimes
# fails to start. The kick duration is part of the total move time, so
# re-calibrate the 90/180 timings after switching it on.
SERVO_START_KICK_MS = 0
SERVO_START_KICK_US_OFFSET = 60

# Optional reverse braking pulse: a very short burst in the opposite
# direction at the end of a move, to kill inertia instead of coasting
# into the neutral pulse.
#
# DISABLED BY DEFAULT (0 ms). Active braking is NOT automatically
# better than simply stopping - keep it only if repeated measurements
# show a lower angular error than stopping at neutral.
SERVO_BRAKE_MS = 0

# Multi-slot moves run as N discrete single steps, so the servo's
# start-up ramp is paid once per step and the calibration stays linear
# in the step count. This is the pause between those steps.
SERVO_INTER_STEP_PAUSE_MS = 250

# Milliseconds the neutral pulse is held after a move so the mechanism
# stops ringing before anything else happens. Speed is not important
# here; a generous settle is cheap and improves repeatability.
SERVO_SETTLE_MS = 1200

# Release the PWM after a move: the pin stops driving, so a slightly
# mistrimmed neutral cannot make the carousel creep.
SERVO_RELEASE_AFTER_MOVE = True

# ==========================================
# CAROUSEL GEOMETRY (physical facts, never calibrated away)
# ==========================================
CAROUSEL_SLOT_COUNT = 4

# 360 / 4 = 90 degrees between neighbouring slot centres.
CAROUSEL_SLOT_GEOMETRY_DEG = 360.0 / CAROUSEL_SLOT_COUNT

# Scanner and loading hole sit exactly opposite: 180 deg = 2 slots.
# Slot 1 at the loader therefore means Slot 3 at the scanner.
CAROUSEL_SCAN_LOAD_OFFSET = 2
CAROUSEL_HALF_TURN_DEG = 180.0

# ==========================================
# MOVEMENT CALIBRATION (empirical, per movement type)
# ==========================================
# GEOMETRY is what the carousel IS. These timings are what the SERVO
# must be COMMANDED to do. One must never be derived from the other.

# One logical slot transition is 90 deg in both directions; only the
# runtime differs, because the servo is not symmetric.
SLOT_STEP_DEG = CAROUSEL_SLOT_GEOMETRY_DEG

# !! NOT MEASURED - RECALIBRATE BEFORE THE FIRST REAL RUN !!
#
# Runtime for ONE 90 degree slot transition, per direction, calibrated
# INDEPENDENTLY. Never derive one from the other, and never derive
# either from the fine-alignment constant.
#
# These were scaled when the direction pulses were slowed from +/-100 us
# to +/-55 us: roughly half the speed, so roughly twice the runtime
# (1200 -> 2200 ms). Servo speed is NOT linear in pulse offset near the
# deadband, so treat this only as a place to start measuring.
NEXT_SLOT_CW_MS = 2200
NEXT_SLOT_CCW_MS = 2200

# The 180 deg loader <-> scanner sweep is INDEPENDENTLY calibrated.
# Never compute it from adjacent-slot moves - a continuous servo pays its
# acceleration ramp once per move, so two short runs and one long sweep
# do not cover the same angle.
#
# Same scaling as above: 2400 -> 4300 ms at the slower pulse. NOT
# MEASURED. This is the movement Measure Sample depends on, so it is the
# most important pair to get right.
LOAD_TO_SCAN_CW_MS = 4300
SCAN_TO_LOAD_CCW_MS = 4300

# Fine alignment, per direction, for the same asymmetry reason.
#
# Scaled with the slower direction pulses (13.3 -> 24.4 ms/deg), and
# equally NOT MEASURED. Calibrate by requesting +/-5 deg and measuring
# the travel that actually results.
CW_MS_PER_DEGREE = 24.4
CCW_MS_PER_DEGREE = 24.4

# Which servo direction advances the slot number at a fixed position,
# i.e. which way to turn for Slot 1 -> Slot 2 -> Slot 3. Software cannot
# know this; verify it once with a single whole-slot move.
CAROUSEL_FORWARD_DIRECTION = "cw"

# Fine adjustment exists for small mechanical corrections only.
MAX_FINE_ADJUST_DEG = 15.0

# Measurement returns the sample to the loading position by itself, so
# this normally has nothing to do. It stays as a safety net for the case
# where the carousel is left at SCAN by a failed or interrupted return.
AUTO_RESTORE_LOAD_ON_SELECT = True

# Extra settling (seconds) after the carousel reaches the scanner and
# before the AS7265x is read, on top of SERVO_SETTLE_MS.
SCAN_SETTLE_TIME = 0.5

# Extra settling (seconds) after the sample has been swung back to the
# loading position at the end of a measurement.
HOME_SETTLE_TIME = 0.3

# ==========================================
# ACQUIRED-SPECTRUM RETENTION (RAM ONLY)
# ==========================================
# The ESP32 keeps the last raw acquisition per slot so the PC can pull a
# measurement it lost - a crash, a restart, or a different laptop. This
# is a small acquisition buffer, not a science archive: RAM only, never
# written to flash, forgotten on reset, and never processed here.
#
# 4 slots x 18 floats plus the settings read-back is well under 2 kB.
RETAIN_LAST_SPECTRUM = True

# ==========================================
# HOST LINK (USB serial console)
# ==========================================
# Commands arrive on sys.stdin, responses leave on sys.stdout - the
# MicroPython console carried over the board's CP2102 bridge, on the
# same cable that powers the ESP32.
#
# No machine.UART is created. The PCB UART2 / JST-XH connector on
# GPIO16/GPIO17 is unused and reserved, so it has no settings here.

MAX_COMMAND_BYTES = 4096

# sys.stdout on the USB console is non-blocking: one write() of a
# multi-kilobyte response does not necessarily send it all, and the
# unwritten tail is silently dropped. Responses go out in chunks of this
# size, looping on the returned count until everything is out.
STDOUT_CHUNK_BYTES = 256

# Pause when stdin reports end of input, so an unusable console cannot
# spin the CPU at full speed.
IDLE_DELAY_MS = 50

# ==========================================
# DEBUG
# ==========================================
# stdout IS the protocol stream. Anything printed there lands in the
# middle of the JSON the PC is parsing. Enable only for manual REPL
# work, never with the PC client attached.
DEBUG = False
