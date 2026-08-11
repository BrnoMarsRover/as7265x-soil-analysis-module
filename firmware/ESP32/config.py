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
FIRMWARE_VERSION = "3.0.0"

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

# Measurement mode: 0b10 = 6 channels per device, continuous.
SENSOR_MEASUREMENT_MODE = 0b10

# Onboard white LED current: 0b00 = 12.5mA, 0b01 = 25mA,
#                            0b10 = 50mA,   0b11 = 100mA
ONBOARD_LED_CURRENT = 0b01

# Human readable names, used only when reporting hardware state.
SENSOR_GAIN_NAMES = {0: "1x", 1: "3.7x", 2: "16x", 3: "64x"}
SENSOR_LED_CURRENT_NAMES = {0: "12.5mA", 1: "25mA", 2: "50mA", 3: "100mA"}

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
# !! EVERY TIMING BELOW MUST BE CALIBRATED ON THE REAL MECHANISM !!
#
# A continuous-rotation servo has no angle command. It has a neutral
# pulse where it stands still, pulses either side of neutral that make
# it turn, and TIME. The shipped values are consistent starting points,
# not measured ones.
# ------------------------------------------------------------------

# Neutral pulse. Calibrate first: trim until the carousel does not creep.
SERVO_STOP_US = 1500

# Direction pulses. "CW"/"CCW" are labels; if the observed direction is
# inverted, swap these two values or flip CAROUSEL_FORWARD_DIRECTION.
# Deliberately close to neutral - a slow servo is far easier to time.
SERVO_CW_US = 1600
SERVO_CCW_US = 1400

# Multi-slot moves run as N discrete single steps, so the servo's
# start-up ramp is paid once per step and the calibration stays linear
# in the step count. This is the pause between those steps.
SERVO_INTER_STEP_PAUSE_MS = 250

# Seconds the stop pulse is held after a move so the mechanism settles.
SERVO_SETTLE_TIME = 1.0

# Release the PWM after a move: the pin stops driving, so a slightly
# mistrimmed neutral cannot make the carousel creep.
SERVO_RELEASE_AFTER_MOVE = True

# ==========================================
# CAROUSEL GEOMETRY (physical facts, never calibrated away)
# ==========================================
CAROUSEL_SLOT_COUNT = 8

# 360 / 8 = 45 degrees between neighbouring slot centres.
CAROUSEL_SLOT_GEOMETRY_DEG = 360.0 / CAROUSEL_SLOT_COUNT

# Scanner and loading hole sit exactly opposite: 180 deg = 4 slots.
CAROUSEL_SCAN_LOAD_OFFSET = 4
CAROUSEL_HALF_TURN_DEG = 180.0

# ==========================================
# MOVEMENT CALIBRATION (empirical, per movement type)
# ==========================================
# GEOMETRY is what the carousel IS. These timings are what the SERVO
# must be COMMANDED to do. One must never be derived from the other.

# One logical slot transition is 45 deg in both directions; only the
# runtime differs, because the servo is not symmetric.
SLOT_STEP_DEG = CAROUSEL_SLOT_GEOMETRY_DEG

NEXT_SLOT_CW_MS = 533
NEXT_SLOT_CCW_MS = 600

# The 180 deg loader <-> scanner sweep is INDEPENDENTLY calibrated.
# Never compute it as four adjacent-slot moves: a continuous servo pays
# its acceleration ramp once per move, so four short runs and one long
# sweep do not cover the same angle.
LOAD_TO_SCAN_CW_MS = 2400
SCAN_TO_LOAD_CCW_MS = 2400

# Fine alignment, per direction, for the same asymmetry reason.
CW_MS_PER_DEGREE = 13.3
CCW_MS_PER_DEGREE = 13.3

# Which servo direction advances the slot number at a fixed position,
# i.e. which way to turn for Slot 1 -> Slot 2 -> Slot 3. Software cannot
# know this; verify it once with a single whole-slot move.
CAROUSEL_FORWARD_DIRECTION = "cw"

# Fine adjustment exists for small mechanical corrections only.
MAX_FINE_ADJUST_DEG = 15.0

# Measurement leaves the sample at the scanner on purpose; the loading
# orientation is restored when the operator selects the next slot.
AUTO_RESTORE_LOAD_ON_SELECT = True

# Extra settling (seconds) after the carousel reaches the scanner and
# before the AS7265x is read, on top of SERVO_SETTLE_TIME.
SCAN_SETTLE_TIME = 0.5

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
