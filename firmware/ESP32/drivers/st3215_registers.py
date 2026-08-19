# drivers/st3215_registers.py
# Wire protocol and memory table for Feetech STS / Waveshare ST3215
# serial bus servos.
#
# Split out from the driver because it is pure data and pure functions:
# no UART, no state, no timing. That makes every register address, every
# checksum and every sign convention directly testable without a servo,
# and it keeps the driver itself about behaviour rather than about
# constants.
#
# SOURCES, all official, none guessed:
#
#   Waveshare "ST3215 memory register map-EN.xls" (memory table V3.7)
#       every address, width, unit, permission and value encoding below.
#
#   Waveshare "scservo" library - SCS.cpp, SMS_STS.cpp
#       the reference frame layout, checksum, and the sign-magnitude goal
#       encoding reproduced by build_packet() and encode_signed().
#
#   Waveshare wiki, "ST3215 Servo"
#       1 Mbps factory baud, factory ID 1, 360/4096 resolution, position
#       range 0-4095 with 2047 as the middle, speed in steps per second
#       (50 steps/s = 0.732 RPM), acceleration limit, half duplex.
#
#   Supplied reference driver (st3215.c / st3215_regs.h)
#       cross-checked address by address; the author reports verifying
#       them against a real servo. Where it disagrees with the official
#       memory table the official table wins - see the notes marked
#       "REFERENCE DRIVER DIFFERS" below.
#
# ENDIANNESS: the STS/SMS series is LITTLE-endian for 16-bit registers.
# The older SCS series is big-endian. Do not mix the two.

HEADER_BYTE = 0xFF
BROADCAST_ID = 0xFE

# ======================================================================
# instructions (SCServo INST.h)
# ======================================================================

INST_PING = 0x01
INST_READ = 0x02
INST_WRITE = 0x03
INST_REG_WRITE = 0x04
INST_REG_ACTION = 0x05
INST_RESET = 0x06
INST_SYNC_READ = 0x82
INST_SYNC_WRITE = 0x83

# ======================================================================
# memory table
# ======================================================================

# --- EPROM, read only -------------------------------------------------
REG_FIRMWARE_MAJOR = 0
REG_FIRMWARE_MINOR = 1
REG_SERVO_MAJOR = 3
REG_SERVO_MINOR = 4

# --- EPROM, read/write (needs REG_LOCK opened first) ------------------
REG_ID = 5
REG_BAUD_RATE = 6
REG_RETURN_DELAY = 7             # x2 us
REG_RESPONSE_LEVEL = 8           # 0 = answer READ and PING only
REG_MIN_ANGLE_LIMIT = 9          # 2 bytes
REG_MAX_ANGLE_LIMIT = 11         # 2 bytes
REG_MAX_TEMPERATURE = 13
REG_MAX_VOLTAGE = 14             # x0.1 V
REG_MIN_VOLTAGE = 15             # x0.1 V
REG_MAX_TORQUE = 16              # 2 bytes, 0..1000
REG_PHASE = 18
REG_UNLOADING_CONDITION = 19     # bitmask, same bit order as REG_SERVO_STATUS
REG_LED_ALARM_CONDITION = 20     # bitmask, same bit order
REG_POSITION_P = 21
REG_POSITION_D = 22
REG_POSITION_I = 23
REG_MIN_STARTING_FORCE = 24      # 2 bytes
REG_CW_DEAD_ZONE = 26
REG_CCW_DEAD_ZONE = 27
REG_PROTECTION_CURRENT = 28      # 2 bytes, x6.5 mA
REG_ANGLE_RESOLUTION = 30
REG_POSITION_CORRECTION = 31     # 2 bytes, sign in BIT 11, +/-2047 steps
REG_MODE = 33
REG_PROTECTION_TORQUE = 34
REG_PROTECTION_TIME = 35         # x10 ms
REG_OVERLOAD_TORQUE = 36
REG_SPEED_P = 37
REG_OVERCURRENT_TIME = 38        # x10 ms
REG_SPEED_I = 39

# --- SRAM, read/write -------------------------------------------------
REG_TORQUE_SWITCH = 40
REG_ACCELERATION = 41            # x100 steps/s^2
REG_GOAL_POSITION = 42           # 2 bytes, SIGN-MAGNITUDE, bit 15
REG_GOAL_TIME = 44               # 2 bytes, PWM mode only
REG_GOAL_SPEED = 46              # 2 bytes, steps/s
REG_TORQUE_LIMIT = 48            # 2 bytes, 0..1000
REG_LOCK = 55

# --- SRAM, read only --------------------------------------------------
REG_PRESENT_POSITION = 56        # 2 bytes, sign-magnitude bit 15
REG_PRESENT_SPEED = 58           # 2 bytes, sign-magnitude bit 15
REG_PRESENT_LOAD = 60            # 2 bytes, sign in BIT 10, per mille
REG_PRESENT_VOLTAGE = 62         # 1 byte, x0.1 V
REG_PRESENT_TEMPERATURE = 63     # 1 byte, degrees C
REG_ASYNC_WRITE_FLAG = 64
REG_SERVO_STATUS = 65            # alarm bitmask
REG_MOVING = 66                  # 1 while in motion
REG_PRESENT_CURRENT = 69         # 2 bytes, sign-magnitude bit 15, x6.5 mA

# Registers 56..70 are contiguous, so the whole feedback block is one
# transaction - which is what the reference library's FeedBack() does and
# what the supplied driver does. Every value then belongs to the same
# instant.
FEEDBACK_FIRST = REG_PRESENT_POSITION
FEEDBACK_LENGTH = (REG_PRESENT_CURRENT + 2) - REG_PRESENT_POSITION   # 15

# ======================================================================
# encodings
# ======================================================================

# Operation modes, memory table address 0x21.
MODE_POSITION = 0     # closed-loop absolute position servo
MODE_WHEEL = 1        # constant speed, direction in bit 15 of 0x2E
MODE_PWM = 2          # PWM open loop
MODE_STEP = 3         # step servo: goal position IS a relative step count

MODE_NAMES = {
    MODE_POSITION: "position servo",
    MODE_WHEEL: "constant speed",
    MODE_PWM: "pwm open loop",
    MODE_STEP: "step servo",
}

# Torque switch values, memory table address 0x28.
#
# REFERENCE DRIVER DIFFERS: the supplied st3215_regs.h names 128
# ST3215_TORQUE_RELEASE_HERE and describes it as a "damped release, holds
# present position". The official memory table says something quite
# different for that value - "Arbitrary current position correction to
# 2048" - and the official SCServo library confirms it: CalibrationOfs()
# is literally writeByte(ID, TORQUE_ENABLE, 128).
#
# So 128 does not release anything. It rewrites the servo's position
# offset so that wherever the shaft happens to be now reads 2048. Acting
# on the reference driver's description would silently recalibrate the
# servo's zero point while the operator believed they were releasing
# torque, and the carousel origin would be wrong from then on.
#
# The official meaning is used here, and the constant is named after what
# it actually does.
TORQUE_OFF = 0
TORQUE_ON = 1
TORQUE_SET_MIDDLE = 128

# EPROM write lock, memory table address 0x37.
LOCK_OPEN = 0
LOCK_CLOSED = 1

# Alarm bits for REG_SERVO_STATUS and for the status packet's error byte.
#
# REFERENCE DRIVER DIFFERS: the supplied st3215_regs.h maps bit 1 to
# "angle" and has no bit 4 at all. The official memory table gives the bit
# order for address 0x41 - and identically for the unloading (0x13) and
# LED alarm (0x14) condition registers - as:
#
#     Bit0 Voltage  Bit1 Sensor  Bit2 Temperature
#     Bit3 Current  Bit4 Angle   Bit5 Overload
#
# Three registers agreeing on one bit order is strong evidence, so the
# official map is used. The practical consequence of the reference map
# would be an angle error reported as a sensor fault and a sensor fault
# reported as nothing at all.
STATUS_BITS = (
    (0x01, "voltage"),
    (0x02, "sensor"),
    (0x04, "temperature"),
    (0x08, "current"),
    (0x10, "angle"),
    (0x20, "overload"),
)

# Baud rate codes, memory table address 0x06.
BAUD_CODES = {
    0: 1000000, 1: 500000, 2: 250000, 3: 128000,
    4: 115200, 5: 76800, 6: 57600, 7: 38400,
}

# Physical constants from the specification.
STEPS_PER_REV = 4096
VOLTAGE_V_PER_LSB = 0.1
CURRENT_MA_PER_LSB = 6.5

# Longest legal status packet body. Rejects a corrupted length byte
# before it can make the driver wait for bytes that will never come.
MAX_PACKET_BODY = 64

# Bytes of noise tolerated while hunting for a frame header, matching the
# reference implementation's checkHead().
MAX_HEADER_SKIP = 16


# ======================================================================
# framing
# ======================================================================

def checksum(values):
    """
    Frame checksum: the one's complement of the byte sum.

    Covers the ID, the length, the instruction (or the error byte) and
    every parameter - everything except the two header bytes and the
    checksum itself. Identical in the official library and in the
    supplied reference driver.
    """
    total = 0

    for value in values:
        total += value

    return (~total) & 0xFF


def build_packet(servo_id, instruction, params=()):
    """
    One instruction frame:

        0xFF 0xFF  ID  LENGTH  INSTRUCTION  PARAM...  CHECKSUM

    LENGTH counts the instruction, the parameters and the checksum, so it
    is len(params) + 2.
    """
    params = bytes(params)
    length = len(params) + 2

    body = bytes([servo_id & 0xFF, length, instruction]) + params

    return bytes([HEADER_BYTE, HEADER_BYTE]) + body + bytes([checksum(body)])


# ======================================================================
# value encodings
# ======================================================================

def encode_signed(value, sign_bit=15):
    """
    Sign-magnitude, which is how the ST3215 encodes goal position.

    A negative value is sent as a magnitude with the sign bit set, NOT as
    two's complement. That is what makes a step of -512 counts mean "512
    counts backwards" instead of an enormous positive number.

    REFERENCE DRIVER DIFFERS: the supplied st3215_move() takes an
    unsigned uint16_t goal position, so it cannot express a negative
    step at all. In step servo mode - the mode this carousel uses - the
    goal register IS a signed step count, so that omission would make
    reverse movement impossible. The official SMS_STS::WritePosEx applies
    exactly the encoding below, and the memory table gives register 0x2A
    a range of -30719..30719.
    """
    value = int(value)
    mask = 1 << sign_bit

    if value < 0:
        return (-value) | mask

    return value & 0xFFFF


def decode_signed(value, sign_bit=15):
    """
    Undo encode_signed.

    Load uses bit 10 rather than bit 15, because its magnitude is per
    mille of maximum torque and so needs only ten bits. The supplied
    reference driver documents having confirmed this on hardware:
    decoding load with the bit-15 rule produces values above the
    register's own 0..1000 range, which is the tell that the rule is
    wrong. Official ReadLoad() agrees.
    """
    mask = 1 << sign_bit

    if value & mask:
        return -(value & ~mask)

    return value


def word_bytes(value):
    """Little-endian pair, the byte order the memory table specifies."""
    value = int(value) & 0xFFFF

    return bytes([value & 0xFF, (value >> 8) & 0xFF])


def bytes_word(low, high):
    return (int(high) << 8) | int(low)


def decode_status_flags(status_byte):
    """Human readable alarm bits from a servo status byte."""
    flags = []

    for mask, name in STATUS_BITS:
        if status_byte & mask:
            flags.append(name)

    return flags


# ======================================================================
# angular arithmetic on a seamless axis
# ======================================================================

def wrap_counts(counts, counts_per_rev=STEPS_PER_REV):
    """Fold an encoder value into the range 0 .. counts_per_rev - 1."""
    return int(counts) % int(counts_per_rev)


def centred_error(delta, counts_per_rev=STEPS_PER_REV):
    """
    Shortest signed representation of an encoder difference.

    A rotary axis has no seam: 4090 -> 10 is +16 counts, never -4080.
    Every comparison of two encoder readings goes through here, which is
    what turns the 4095/0 boundary into a non-event.
    """
    counts_per_rev = int(counts_per_rev)
    half = counts_per_rev // 2

    return ((int(delta) + half) % counts_per_rev) - half


def counts_to_degrees(counts, counts_per_rev=STEPS_PER_REV):
    return float(counts) * 360.0 / float(counts_per_rev)


def degrees_to_counts(degrees, counts_per_rev=STEPS_PER_REV):
    """
    Degrees to whole encoder counts, rounded half away from zero.

    round() is deliberately not used: it rounds halves to even, so the
    smallest possible alignment nudge would land on zero counts and
    silently do nothing.
    """
    exact = float(degrees) * float(counts_per_rev) / 360.0

    if exact >= 0:
        return int(exact + 0.5)

    return -int(-exact + 0.5)
