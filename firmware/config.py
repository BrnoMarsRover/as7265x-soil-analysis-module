# config.py
# Master hardware, communication and analysis configuration for the
# AS7265x soil-analysis module (Brno Mars Rover / Freya science payload).
#
# Every value that depends on the real mechanism is collected here so that
# the module can be calibrated without touching driver or protocol code.

FIRMWARE_NAME = "freya-science-module"
FIRMWARE_VERSION = "2.0.0"

# ==========================================
# I2C & SENSOR SETTINGS (AS7265x)
# ==========================================
I2C_SCL_PIN = 22
I2C_SDA_PIN = 21
I2C_FREQ = 100000

# Onboard LED brightness (current limit).
# Options: 0b00 = 12.5mA, 0b01 = 25mA, 0b10 = 50mA, 0b11 = 100mA
ONBOARD_LED_CURRENT = 0b01

# Acquisition settings.
#
# as7265x.begin() already applies these same values internally. They are
# repeated here and re-applied explicitly after initialization so that they
# are both tunable and recorded together with every saved sample.
SENSOR_INTEGRATION_CYCLES = 100

# Gain: 0b00 = 1x, 0b01 = 3.7x, 0b10 = 16x, 0b11 = 64x
SENSOR_GAIN = 0b10

# Human readable names used only inside the saved sample record.
SENSOR_GAIN_NAMES = {0: "1x", 1: "3.7x", 2: "16x", 3: "64x"}
SENSOR_LED_CURRENT_NAMES = {0: "12.5mA", 1: "25mA", 2: "50mA", 3: "100mA"}

# Seconds to wait after boot before touching the I2C bus, so the 3.3 V
# sensor rail and the AS7265x internal startup are finished.
STARTUP_DELAY_SECONDS = 3

# ==========================================
# SERVO MOTOR SETTINGS (MG995 360 deg / continuous rotation)
# ==========================================
# GPIO pin connected to the servo signal wire.
SERVO_PIN = 25
SERVO_PWM_FREQ = 50

# ------------------------------------------------------------------
# !! ALL FOUR GROUPS BELOW MUST BE CALIBRATED ON THE REAL MECHANISM !!
#
# A continuous-rotation servo has no angle command. It only has:
#   - a neutral pulse where it stands still,
#   - pulses above/below neutral that make it turn, with a speed that
#     depends on how far the pulse is from neutral,
#   - and rotation that is controlled purely by TIME.
#
# The values below are safe starting points, NOT known-good values.
# Carousel load, gearing, battery voltage and the individual servo all
# change them. See README section "Servo calibration".
# ------------------------------------------------------------------

# 1. Neutral / stop pulse.
#    Calibrate first: apply this pulse and trim it until the carousel
#    does not creep in either direction.
SERVO_STOP_US = 1500

# 2. Direction pulses.
#    "CW" and "CCW" are just labels. Whichever pulse makes the carousel
#    turn in the direction you decide to call clockwise goes in
#    SERVO_CW_US. If the observed direction is inverted, swap the two
#    values (or flip CAROUSEL_FORWARD_DIRECTION below).
#
#    These are deliberately close to neutral: a slow servo is far easier
#    to time accurately than one running at full speed.
SERVO_CW_US = 1600
SERVO_CCW_US = 1400

# 3. Movement timing lives in the MOVEMENT CALIBRATION section below,
#    because each kind of movement is calibrated independently.

# 4. Timing behaviour around a move.
#    Multi-slot moves are executed as N discrete single steps rather than
#    one long run, so that the start-up acceleration of the servo is paid
#    once per step and the calibration stays linear in the step count.
SERVO_INTER_STEP_PAUSE_MS = 250

# Seconds the stop pulse is held after a move so the mechanism can settle.
SERVO_SETTLE_TIME = 1.0

# Release (deinit) the PWM after a move.
#   True  -> pin stops driving; no risk of creep if SERVO_STOP_US is
#            slightly off, servo draws no holding current. Recommended.
#   False -> stop pulse is held continuously, which brakes the servo
#            electrically but creeps forever if neutral is miscalibrated.
SERVO_RELEASE_AFTER_MOVE = True

# Default duration of a free manual jog when the PC does not specify one.
SERVO_JOG_DEFAULT_MS = 150

# Upper bound for a single free jog, as a crude runaway guard.
SERVO_JOG_MAX_MS = 5000

# ==========================================
# CAROUSEL GEOMETRY  (physical facts, never "calibrated away")
# ==========================================
# Eight physical sample slots on a full 360 degree carousel.
CAROUSEL_SLOT_COUNT = 8

# 360 / 8 = 45 degrees between neighbouring slot CENTRES. This is the
# real mechanical spacing of the carousel and does not change.
CAROUSEL_SLOT_GEOMETRY_DEG = 360.0 / CAROUSEL_SLOT_COUNT

# Kept as the historical name for the same physical quantity.
CAROUSEL_DEGREES_PER_SLOT = CAROUSEL_SLOT_GEOMETRY_DEG

# The scanner and the loading hole sit exactly opposite each other:
# 180 degrees = 4 slots. Slot 1 at the loader means Slot 5 at the
# scanner.
CAROUSEL_SCAN_LOAD_OFFSET = 4
CAROUSEL_HALF_TURN_DEG = 180.0

# ==========================================
# MOVEMENT CALIBRATION  (empirical, per movement type)
# ==========================================
# !! EVERY VALUE IN THIS SECTION MUST BE TUNED ON THE REAL MECHANISM !!
#
# Critical distinction, learned from hardware testing:
#
#   GEOMETRY is what the carousel IS.
#   These timings are what the SERVO must be COMMANDED to do.
#
# They are not the same thing, and one must never be derived from the
# other. A continuous-rotation servo has acceleration, deceleration,
# backlash and load-dependent speed, so the command that actually lands
# a slot lands correctly is not simply "45 degrees worth of runtime",
# and it differs between the two directions.
#
# Each movement type therefore gets its own independent calibration:
#
#   adjacent slot   ->  NEXT_SLOT_*_MS   (45 deg either way)
#   half turn       ->  LOAD_TO_SCAN_CW_MS / SCAN_TO_LOAD_CCW_MS
#   fine alignment  ->  *_MS_PER_DEGREE
#
# ------------------------------------------------------------------
# Adjacent slot: Slot N -> Slot N+1
# ------------------------------------------------------------------
# One logical slot is 45 degrees in BOTH directions - that is the
# physical geometry and what the operator asks for.
#
# The MG995 is a continuous-rotation servo and does not run at the same
# speed each way, so the two directions keep independent TIMING below.
# The requested angle, however, is the same.
SLOT_STEP_DEG = CAROUSEL_SLOT_GEOMETRY_DEG

# Runtime for exactly one 45 degree slot transition, from standstill.
# Tune each direction on its own: step forward one slot, then back one
# slot, and check that the carousel returns to where it started.
NEXT_SLOT_CW_MS = 533
NEXT_SLOT_CCW_MS = 600

# ------------------------------------------------------------------
# Half turn: loading hole <-> scanner (180 degrees)
# ------------------------------------------------------------------
# INDEPENDENTLY CALIBRATED. Do NOT compute this as four adjacent-slot
# moves: a continuous servo pays its acceleration ramp once per move,
# so four short runs and one long sweep do not cover the same angle.
LOAD_TO_SCAN_CW_MS = 2400

# The return sweep gets its own value; a continuous servo rarely runs at
# the same speed in both directions.
SCAN_TO_LOAD_CCW_MS = 2400

# Measurement deliberately does NOT swing back on its own: after a
# measurement the sample really is at the scanner, and the status says
# so. Choose Slot restores the loading orientation when the operator
# moves on to the next sample, which keeps the physical state honest
# instead of hiding a motor movement inside the measurement.
AUTO_RESTORE_LOAD_ON_SELECT = True

# ------------------------------------------------------------------
# Fine alignment: small manual corrections in degrees
# ------------------------------------------------------------------
# Separate from slot movement, and separate per direction for the same
# asymmetry reason as the adjacent-slot timing above.
#
# Starting points come from each direction's own calibration:
#   CW  533 ms / 40 deg = 13.3
#   CCW 600 ms / 45 deg = 13.3
# They are independent constants and should be trimmed on their own.
CW_MS_PER_DEGREE = 13.3
CCW_MS_PER_DEGREE = 13.3

# Slots are numbered 1..8 in the CLOCKWISE direction, and the origin is
# established by the operator during synchronization: physical Slot 1 is
# aligned with the SOIL LOADING HOLE and declared as Slot 1.
#
#   Slot 1 = loading position (the calibrated origin)
#   Slot 2 = 45 deg clockwise from Slot 1
#   ...
#   Slot 8 = 315 deg clockwise from Slot 1
#
# This setting says which servo direction advances the slot number at a
# fixed position, i.e. which way to turn to go Slot 1 -> Slot 2 -> Slot 3.
#
#   "cw"  -> the CW servo direction advances the slot number
#   "ccw" -> the CCW servo direction advances the slot number
#
# Software cannot know this. Verify it once on the real carousel with a
# single whole-slot move: if choosing the next slot walks the numbering
# backwards, either flip this value or swap SERVO_CW_US / SERVO_CCW_US.
CAROUSEL_FORWARD_DIRECTION = "cw"

# Largest correction allowed by the degree-based fine adjustment. Fine
# adjustment exists for small mechanical corrections only; anything
# larger should use whole-slot movement instead.
MAX_FINE_ADJUST_DEG = 15.0

# Extra settling time (seconds) after the carousel reaches the scanner and
# before the AS7265x is read, on top of SERVO_SETTLE_TIME.
SCAN_SETTLE_TIME = 0.5

# ==========================================
# HOST LINK (USB serial console)
# ==========================================
# Commands arrive on sys.stdin and responses leave on sys.stdout. Both
# are the MicroPython serial console carried over the ESP32 development
# board's CP2102 USB bridge - the same cable that powers the board.
#
# No machine.UART peripheral is initialized. The dedicated PCB UART2 /
# JST-XH connector on GPIO16/GPIO17 is unused and reserved for a future
# revision, so there are deliberately no UART pin settings here.
#
# The baud rate is chosen by the PC when it opens the port (115200); the
# USB console does not need the firmware to agree on a divisor.

# Longest accepted single command line, in bytes.
MAX_COMMAND_BYTES = 4096

# Responses are written to the console in chunks of this size, looping
# until everything is out. sys.stdout on the USB console is a
# non-blocking stream: one write() of a multi-kilobyte response does not
# necessarily send it all, and the remainder would be silently lost.
STDOUT_CHUNK_BYTES = 256

# Pause when stdin reports end of input, so an unusable console cannot
# spin the CPU at full speed.
IDLE_DELAY_MS = 50

# ==========================================
# DEBUG
# ==========================================
# stdout IS the protocol stream. Anything printed there lands in the
# middle of the JSON the main computer is parsing, so routine logging is
# off. Enable only for manual REPL debugging, never for a competition
# run with the PC client attached.
DEBUG = False

# ==========================================
# PERSISTENT SAMPLE STORAGE
# ==========================================
# Index of every measured sample. Small enough to always keep in RAM.
SAMPLE_INDEX_FILE = "samples.json"

# One full scientific record per measured sample lives in this directory.
# Splitting the records keeps peak RAM low: the ESP32 never has to parse
# the whole science history at once.
SAMPLE_RECORD_DIR = "samples"

# Fixed calibration references. Treated as strictly read-only.
#
# This file holds exactly ONE white and ONE dark reference, measured and
# accepted before the competition. They are loaded at boot and reused for
# every sample. The firmware never measures a new white or dark, and the
# operator is never asked to calibrate during normal operation.
REFERENCES_FILE = "references.json"

# Identifies the single immutable calibration set for this competition
# version. Stored with every sample so a record can always be traced back
# to the references it was normalized against.
CALIBRATION_ID = "FREYA_COMPETITION_2026_CAL_V1"

# Reference-material spectra used for comparison. Strictly read-only.
DATABASE_FILE = "database.json"

# ==========================================
# AUTOMATIC INTERPRETATION THRESHOLDS
# ==========================================
# HEURISTIC COMPETITION-SUPPORT THRESHOLDS - NOT SCIENTIFICALLY VALIDATED.
#
# The comparison metric is cosine similarity between 18-channel reflectance
# vectors. Because reflectance is non-negative, every material in the
# database tends to score high; useful information is in the DIFFERENCE
# between the top candidates, not in the absolute number.
#
# Tune both values against real measurements of known materials.

# Below this best-match score the result is reported as a weak match.
MIN_SIMILARITY_PERCENT = 85.0

# If the best and second-best scores are closer than this many percentage
# points, the classification is reported as ambiguous.
AMBIGUITY_MARGIN_PERCENT = 1.5

# ==========================================
# SAMPLE ID RULES
# ==========================================
# Sample IDs are also used as filenames, so the character set is limited.
# Examples of valid IDs: S001, D1, AB1, ROCK-07
SAMPLE_ID_MAX_LENGTH = 24
SAMPLE_ID_ALLOWED_EXTRA = "-_"
