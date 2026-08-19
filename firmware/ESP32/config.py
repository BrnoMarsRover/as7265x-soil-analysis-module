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
FIRMWARE_VERSION = "6.0.0"

# Bumped when the COMMAND SURFACE changes - a command added, removed or
# renamed, or a response field the PC depends on. 2 is the flat
# firmware with connect_servo/disconnect_servo; 1 was the package
# layout with select_servo and the capability table.
PROTOCOL_VERSION = 2

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

# Seconds after reset before the I2C bus may be touched, so the 3.3 V
# rail and the AS7265x internal startup are finished.
#
# This does NOT delay the protocol. The board serves ping and
# get_status immediately; whatever remains of this wait is paid by the
# first command that actually needs the sensor. See
# sensor.py::SensorRuntime._await_power_on.
STARTUP_DELAY_SECONDS = 3

# Bounded sensor bring-up. A single failed attempt at boot must never
# become permanent runtime state, so initialization retries a few times
# and every later sensor operation may retry again on its own.
SENSOR_INIT_ATTEMPTS = 4
SENSOR_INIT_RETRY_MS = 400

# Bus-level scan retries inside one initialization attempt.
I2C_SCAN_ATTEMPTS = 3
I2C_SCAN_RETRY_MS = 150

# Longest wait for ONE virtual-register transaction.
VIRTUAL_REGISTER_TIMEOUT_MS = 1071

# Longest an entire sensor operation may take, however many
# transactions it is made of.
#
# ONE BOUNDED WAIT DOES NOT BOUND THEIR SUM. Bringing the AS7265x up
# runs about twenty virtual-register transactions, each containing two
# bounded waits, and the whole thing is retried SENSOR_INIT_ATTEMPTS
# times. A sensor that answers on the I2C bus but never sets its ready
# bit makes every one of those waits run to full length: measured on
# hardware, that took the firmware past three minutes without
# answering, which the PC could only read as a dead board.
#
# 8 seconds is generous for a healthy sensor - a good bring-up takes
# well under one - and short enough that a sick one is REPORTED rather
# than waited on.
SENSOR_OPERATION_BUDGET_MS = 8000

# Milliseconds between polls of STATUS / DATA_READY.
POLLING_DELAY_MS = 5

# Milliseconds the white LED is on before a measurement starts, so the
# illumination is stable across the whole integration window.
ILLUMINATION_SETTLE_MS = 300

# ==========================================
# CAROUSEL GEOMETRY (physical facts, never calibrated away)
# ==========================================
# What the mechanism IS, independent of what drives it. The servo and
# the carousel both read these; neither may contradict them, and
# nothing here is ever "tuned".
CAROUSEL_SLOT_COUNT = 4

# 360 / 4 = 90 degrees between neighbouring slot centres.
CAROUSEL_SLOT_GEOMETRY_DEG = 360.0 / CAROUSEL_SLOT_COUNT

# Scanner and loading hole sit exactly opposite: 180 deg = 2 slots.
# Slot 1 at the loader therefore means Slot 3 at the scanner.
#
# This MUST stay at half the slot count - that is what makes the
# loader/scanner mapping its own inverse, and the carousel checks it.
CAROUSEL_SCAN_LOAD_OFFSET = 2
CAROUSEL_HALF_TURN_DEG = 180.0

# Which servo direction advances the slot number at a fixed position,
# i.e. which way to turn for Slot 1 -> Slot 2 -> Slot 3. Software cannot
# know how the carousel is mounted: verify it once with a single
# whole-slot move, and flip this if the slot numbers run backwards.
#
# This is a property of the MECHANISM, not of the actuator.
CAROUSEL_FORWARD_DIRECTION = "cw"

# Fine adjustment exists for small mechanical corrections only.
MAX_FINE_ADJUST_DEG = 15.0

# Measurement returns the sample to the loading position by itself, so
# this normally has nothing to do. It stays as a safety net for the case
# where the carousel is left at SCAN by a failed or interrupted return.
AUTO_RESTORE_LOAD_ON_SELECT = True

# Extra settling (seconds) after the carousel reaches the scanner and
# before the AS7265x is read, on top of the backend's own settling.
SCAN_SETTLE_TIME = 0.5

# Extra settling (seconds) after the sample has been swung back to the
# loading position at the end of a measurement.
HOME_SETTLE_TIME = 0.3

# ==========================================
# CAROUSEL ACTUATOR
# ==========================================
# The carousel is driven by ONE actuator: a Waveshare ST3215 serial bus
# servo. Its settings are in the ST3215 section below.
#
# The firmware still starts with no servo connected, and that is
# deliberate rather than leftover: the UART must be opened and the servo
# must answer before anything is allowed to move. A carousel that turns
# because a driver object happened to construct successfully is a
# carousel that turns with no idea where it is. The operator connects it
# through option [0] Carousel Setup, and after every reboot it is
# disconnected again.
#
# Every movement is commanded in encoder counts and verified by reading
# the encoder back, so nothing in the ST3215 section below is a timing
# calibration.

# ==========================================
# ST3215 (Waveshare serial bus servo, UART2)
# ==========================================
# The ST3215 is reached over UART2 through a Waveshare Serial Bus Servo
# Driver Board. The link from this PCB is three wires and nothing else:
#
#     ESP32 GPIO16 ---> driver board RX      (this board TRANSMITS here)
#     ESP32 GPIO17 <--- driver board TX      (this board RECEIVES here)
#     ESP32 GND    <--> driver board GND
#
# MEASURED ON THE FITTED HARDWARE 2026-08-19, three independent ways.
#
# 1. Line states, with the ESP32's own pulls and nothing transmitting:
#
#        GPIO16   follows the pull both ways   -> FLOATING
#        GPIO17   high against a pull-down     -> DRIVEN by something
#
#    An adapter INPUT is high impedance and cannot hold a line, so a
#    floating pin is the one wired to the adapter's RX. An adapter
#    OUTPUT idles high, so the driven pin is the one wired to its TX.
#    That alone fixes the direction: transmit on 16, receive on 17.
#
# 2. With the servo bus powered, orientation TX 16 / RX 17 returned a
#    clean 6-byte echo of our own frame at every one of eight baud
#    rates. A transparent half-duplex adapter echoes at any baud
#    because it converts levels rather than reframing; the opposite
#    orientation never produced a clean echo.
#
# 3. A genuine status packet was captured from the servo:
#
#        FF FF 01 02 00 FC     ID 1, error byte 0x00, checksum valid
#
#    so the servo exists, answers to ID 1, and runs at 1 Mbps.
#
# The previous assignment had these the other way round, which is why
# the bus scan reported ECHO_ONLY and sent everyone looking for a
# power fault that was not there.
#
# The servo is powered from an EXTERNAL supply at the driver board. No
# servo current flows through this PCB, and this firmware has no
# authority over servo power: there is deliberately no ST3215 power pin,
# enable or switch anywhere in this file.
#
# The ST3215 has a 4096-count absolute magnetic encoder and runs its own
# closed position loop, so NOTHING in this section is a timing
# calibration. Every movement is commanded in encoder counts and then
# verified by reading the encoder back.

ST3215_UART_ID = 2

# THIS BOARD TRANSMITS ON 16 AND RECEIVES ON 17. See the wiring note
# above for the three measurements that establish it.
ST3215_TX_PIN = 16
ST3215_RX_PIN = 17

# 1 Mbps is the ST3215 factory baud rate (memory table address 0x06, code
# 0). Change it only if the servo itself has been reconfigured.
ST3215_BAUD = 1000000

# Factory default servo ID. There is one servo on this bus, so the
# factory value is kept; diagnostics reads it back and complains if the
# servo disagrees.
ST3215_SERVO_ID = 1

# Longest wait for one status packet. A reply at 1 Mbps takes well under
# a millisecond, so anything approaching this means the link or the
# external servo supply is down, not that the servo is busy.
ST3215_TIMEOUT_MS = 50

# Bounded retries, for transport faults only. A servo that answers with
# an alarm is never retried - that would hide it - and a relative goal
# position is never retried either, because a resend would move the
# carousel twice.
ST3215_RETRIES = 2
ST3215_RETRY_DELAY_MS = 5

# 360 deg / 4096 steps, from the official specification. Everything
# angular in the ST3215 backend is derived from this and never hardcoded.
ST3215_COUNTS_PER_REV = 4096

# One slot transition and the loader <-> scanner sweep, in encoder
# counts: 4096 / 4 = 1024 and 4096 / 2 = 2048. Derived, never typed out.
ST3215_COUNTS_PER_SLOT = ST3215_COUNTS_PER_REV // CAROUSEL_SLOT_COUNT
ST3215_HALF_TURN_COUNTS = ST3215_COUNTS_PER_REV // 2

# Operating mode, memory table address 0x21:
#
#   0  position servo   absolute target; one revolution only, unless
#                       both angle limits are cleared for multi-turn
#   1  constant speed   no position control at all
#   2  PWM open loop    no position control at all
#   3  step servo       the goal register IS a relative step count
#
# STEP SERVO MODE (3) is what the carousel uses. A carousel turns forever
# in one direction: an absolute single-turn target would have to cross
# the 4095/0 seam and would send the mechanism the long way round, and a
# multi-turn absolute count would eventually run out of range. A relative
# step of one slot has neither problem.
#
# Written to the servo's EPROM by the SERVICE configuration command,
# never implicitly at boot and never as part of routine carousel
# calibration. The firmware REFUSES to move a servo that reports a
# different mode.
ST3215_MODE = 3

# Goal speed in encoder steps per second (memory table address 0x2E,
# maximum 3400). 50 steps/s = 0.732 RPM.
#
# PRECISION AND SAMPLE SAFETY BEFORE SPEED. 600 steps/s is about 8.8 RPM,
# so one 90 deg slot transition takes roughly 1.7 s and the 180 deg
# measurement sweep roughly 3.4 s. Loose soil in an open slot does not
# want to be flung, and a slow approach overshoots less.
ST3215_SPEED = 600

# Start/stop acceleration (memory table address 0x29) in units of
# 100 steps/s^2, maximum 254. 20 means 2000 steps/s^2, which reaches
# ST3215_SPEED in about 0.3 s - gentle enough not to shake a sample out
# of its slot.
ST3215_ACCELERATION = 20

# The servo's torque switch is in an unknown state after a power cycle. A
# movement command may enable torque before moving, which is the one
# moment when doing so cannot surprise anyone. Torque is NEVER dropped
# automatically: the carousel has to stay put while the AS7265x reads.
ST3215_AUTO_ENABLE_TORQUE = True

# Pause inside the stop command, between dropping and restoring torque.
ST3215_STOP_PAUSE_MS = 20

# ------------------------------------------------------------------
# !! ST3215_POSITION_TOLERANCE MUST BE CHECKED ON THE REAL MECHANISM.
#    The shipped value is deliberately conservative, NOT measured. !!
# ------------------------------------------------------------------
# 15 counts is about 1.3 deg. The servo's own electronic dead zone is
# 0.176 deg (2 counts) and its default insensitive zones are 1 count
# each, so a healthy mechanism should settle far inside this. Tighten it
# once the real repeatability is known - but a tolerance that is too
# tight turns ordinary backlash into a failed measurement.
ST3215_POSITION_TOLERANCE = 15

# Poll interval while waiting for a movement to finish.
ST3215_POLL_INTERVAL_MS = 20

# Consecutive polls that must report the moving flag clear before the
# movement is treated as finished. One zero could still be the instant
# between the goal being written and the motor starting.
ST3215_STOP_CONFIRM_POLLS = 2

# Milliseconds to wait after the servo reports that it has stopped,
# before the encoder reading that decides success. This lets the
# mechanism stop ringing; being generous is cheap and improves
# repeatability.
ST3215_SETTLE_MS = 250

# Movement time budget, computed per movement from the distance and
# ST3215_SPEED, because one fixed number is either too short for the
# 180 deg sweep or too slow to report a stalled servo:
#
#     budget = BASE_MS + nominal travel time * MARGIN, capped at _MS
ST3215_MOVE_TIMEOUT_BASE_MS = 1200
ST3215_MOVE_TIMEOUT_MARGIN = 2.5
ST3215_MOVE_TIMEOUT_MS = 12000

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
# This is NOT the servo link. UART2 on GPIO16/GPIO17 is a separate
# hardware peripheral that talks to the ST3215 servo driver board and
# has its own settings above. The two channels never share a byte: a
# servo transaction cannot disturb the host console, and vice versa.

MAX_COMMAND_BYTES = 4096

# sys.stdout on the USB console is non-blocking: one write() of a
# multi-kilobyte response does not necessarily send it all, and the
# unwritten tail is silently dropped. Responses go out in chunks of this
# size, looping on the returned count until everything is out.
STDOUT_CHUNK_BYTES = 256

# Pause when stdin reports end of input, so an unusable console cannot
# spin the CPU at full speed.
IDLE_DELAY_MS = 50

# Every response frame is preceded by a newline.
#
# A response is one line of JSON, so the PC parses whatever arrived
# between two newlines. If ANYTHING lands on the console immediately
# before a frame - a partial line from a previous write, a byte mangled
# by a switching transient on the USB bridge - it merges into the frame's
# line and the whole response becomes unparseable, even though every byte
# of the JSON itself arrived intact. That failure was observed on
# hardware: a calibration block came back with about sixty corrupted
# bytes in front of an otherwise perfect frame, and the PC waited out its
# full 180 s timeout for a response it had already received.
#
# One leading newline closes whatever came before, so the damage stays in
# its own line and the JSON gets a clean one. The PC skips blank lines.
RESPONSE_GUARD_NEWLINE = True

# Settle after the last lamp is switched off, before the response is
# written.
#
# Switching an illumination LED is the largest current step this board
# makes, and the response to an acquisition is written microseconds
# afterwards. Waiting a few milliseconds keeps the console transmission
# out of the switching transient. Cheap insurance: it costs 5 ms per
# acquisition block against losing a whole calibration step.
ACQUISITION_RESPONSE_SETTLE_MS = 5

# ==========================================
# DEBUG
# ==========================================
# stdout IS the protocol stream. Anything printed there lands in the
# middle of the JSON the PC is parsing. Enable only for manual REPL
# work, never with the PC client attached.
DEBUG = False
