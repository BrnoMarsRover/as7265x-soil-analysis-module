# servo.py
#
# The carousel actuator: a Waveshare ST3215 serial bus servo on UART2,
# its wire protocol, and its connection lifecycle.
#
# ONE actuator, ONE file. Earlier firmware spread this across a
# register table, an abstract backend contract, the driver and a
# manager that chose between backends. There is one actuator, and an
# abstraction with a single implementation is a second file to read
# before understanding the first.
#
# What survives is everything that was never about choosing a backend:
#
#   rotation vocabulary   an angle on a rotary axis still needs
#                         reducing to (-180, +180]
#   the wire protocol     checksums, packet framing and sign
#                         conventions, still pure functions that can be
#                         tested without a servo
#   the movement gate     nothing moves the carousel before the UART is
#                         open and the servo has answered. A carousel
#                         that turns because a driver object happened
#                         to construct is a carousel turning with no
#                         idea where it is.
#
# The connection is RUNTIME state. After a reboot nothing is connected,
# because a reboot is exactly when someone may have unplugged
# something.
#
# This module knows about a servo. It knows nothing about a carousel,
# slots, samples or geometry beyond the two angles the mechanism
# defines in config.

import time

import config


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


class ServoNotConnectedError(ServoError):
    """
    Nothing has opened the UART and confirmed the servo answers.

    Its own class because it is the one servo fault that is not a
    hardware problem: the operator has simply not run Carousel Setup
    yet, and the PC turns this code into that instruction.
    """

    code = "SERVO_NOT_CONNECTED"


# ======================================================================
# rotation vocabulary
# ======================================================================

# Direction labels, shared between the carousel and the driver.
#
# What "clockwise" MEANS physically is this driver's business: the
# ST3215 gets a positive or negative step count. Which way the CAROUSEL
# must turn to advance a slot number is a third, mechanical question
# and lives in config.CAROUSEL_FORWARD_DIRECTION.
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


# There is deliberately no capability table here.
#
# One existed while a second actuator was supported, so callers
# could ask "can this one measure its own position?" instead of
# testing its name. With one actuator every answer is a constant:
# the ST3215 has an encoder, verifies every movement against it,
# reports telemetry, and takes torque and mode commands. A
# dictionary of seven values that are all True is not information,
# it is seven branches that can never be taken.


# ======================================================================
# wire protocol: memory table
# ======================================================================
# Feetech STS / Waveshare ST3215 serial bus servo.
#
# SOURCES, all official, none guessed:
#
#   Waveshare "ST3215 memory register map-EN.xls" (memory table V3.7)
#       every address, width, unit, permission and value encoding below.
#
#   Waveshare "scservo" library - SCS.cpp, SMS_STS.cpp
#       the frame layout, checksum, and the sign-magnitude goal
#       encoding reproduced by build_packet() and encode_signed().
#
#   Waveshare wiki, "ST3215 Servo"
#       1 Mbps factory baud, factory ID 1, 360/4096 resolution, speed
#       in steps per second, acceleration limit, half duplex.
#
# ONLY the registers this project uses are named. The full memory table
# runs to some eighty addresses; carrying the seventy this firmware
# never touches would mean seventy constants nobody can tell are dead.
# Anything needed later is one line away in the source above.
#
# ENDIANNESS: the STS/SMS series is LITTLE-endian for 16-bit registers.
# The older SCS series is big-endian. Do not mix the two.

HEADER_BYTE = 0xFF

# --- instructions (SCServo INST.h) ------------------------------------
INST_PING = 0x01
INST_READ = 0x02
INST_WRITE = 0x03

# --- EPROM, read/write (needs REG_LOCK opened first) ------------------
REG_ID = 5
REG_BAUD_RATE = 6
REG_MIN_ANGLE_LIMIT = 9          # 2 bytes
REG_MAX_ANGLE_LIMIT = 11         # 2 bytes
REG_MODE = 33

# --- SRAM, read/write -------------------------------------------------
REG_TORQUE_SWITCH = 40

# The goal block: four contiguous fields written as ONE transaction.
# The servo starts moving the moment the goal position lands, so a
# speed written afterwards would apply to the NEXT movement. The
# addresses are named because write_goal's payload layout IS this
# table, and GOAL_BLOCK_LENGTH is what its length is checked against.
REG_ACCELERATION = 41            # 1 byte,  x100 steps/s^2
REG_GOAL_POSITION = 42           # 2 bytes, SIGN-MAGNITUDE, bit 15
REG_GOAL_TIME = 44               # 2 bytes, PWM mode only - always 0
REG_GOAL_SPEED = 46              # 2 bytes, steps/s

GOAL_BLOCK_FIRST = REG_ACCELERATION
GOAL_BLOCK_LENGTH = (REG_GOAL_SPEED + 2) - REG_ACCELERATION   # 7

REG_LOCK = 55

# --- SRAM, read only --------------------------------------------------
REG_PRESENT_POSITION = 56        # 2 bytes, sign-magnitude bit 15
REG_PRESENT_SPEED = 58           # 2 bytes, sign-magnitude bit 15
REG_PRESENT_LOAD = 60            # 2 bytes, sign in BIT 10, per mille
REG_PRESENT_VOLTAGE = 62         # 1 byte, x0.1 V
REG_PRESENT_TEMPERATURE = 63     # 1 byte, degrees C
REG_SERVO_STATUS = 65            # alarm bitmask
REG_MOVING = 66                  # 1 while in motion
REG_PRESENT_CURRENT = 69         # 2 bytes, sign-magnitude bit 15, x6.5 mA

# Registers 56..70 are contiguous, so the whole feedback block is one
# transaction - which is what the reference library's FeedBack() does.
# Every value then belongs to the same instant.
FEEDBACK_FIRST = REG_PRESENT_POSITION
FEEDBACK_LENGTH = (REG_PRESENT_CURRENT + 2) - REG_PRESENT_POSITION   # 15


# ======================================================================
# wire protocol: encodings
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
# 0 and 1 are the only values this firmware ever writes, and that is
# deliberate. The value 128 is documented by one third-party driver as
# a "damped release"; the official memory table and the official
# SCServo library agree it is something else entirely - it rewrites the
# servo's position offset so the shaft's current place reads 2048.
# Writing it believing the other description would silently recalibrate
# the zero point, and the carousel origin would be wrong from then on.
TORQUE_OFF = 0
TORQUE_ON = 1

# EPROM write lock, memory table address 0x37.
LOCK_OPEN = 0
LOCK_CLOSED = 1

# Alarm bits for REG_SERVO_STATUS and for the status packet's error
# byte. The official memory table gives the bit order for address 0x41
# - and identically for the unloading (0x13) and LED alarm (0x14)
# condition registers - as:
#
#     Bit0 Voltage  Bit1 Sensor  Bit2 Temperature
#     Bit3 Current  Bit4 Angle   Bit5 Overload
#
# Three registers agreeing on one bit order is strong evidence. One
# third-party header maps bit 1 to "angle" and omits bit 4; acting on
# that would report an angle error as a sensor fault and a sensor fault
# as nothing at all.
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
# releasing UART pins
# ======================================================================

def release_uart_pins(*pins):
    """
    Hand pins back to the GPIO matrix after uart.deinit().

    MEASURED ON THIS HARDWARE, and the single most misleading fault
    found in the whole servo path.

    `uart.deinit()` stops the peripheral but does NOT detach its pins:
    a pin that has been UART2's TX stays wired to the TX signal
    afterwards. Open UART2 again with that same pin as RX, and every
    byte transmitted arrives straight back - at ANY baud rate, with a
    perfect checksum, looking exactly like a transparent half-duplex
    adapter echoing the bus.

    That is not inference. With GPIO17 previously used as TX, a probe
    transmitting on GPIO18 - a pin wired to nothing at all - got its
    own frame back on GPIO17:

        tx=16 rx=17  (clean boot)      0 bytes
        tx=17 rx=16  (swapped probe)   0 bytes
        tx=16 rx=17  (again)           6 bytes, "echo"
        tx=18 rx=17  (18 unconnected)  6 bytes, "echo"
        tx=16 rx=18  (18 unconnected)  0 bytes

    No external wiring can carry GPIO18 to GPIO17. The loop is inside
    the chip, and it belongs to GPIO17 because GPIO17 was a TX.

    Why it matters: bus_scan deliberately tries the SWAPPED pin order.
    Without this call the first swapped probe poisons every probe for
    the rest of the boot, the scan reports ECHO_ONLY, and its diagnosis
    tells the operator that the ESP32 side is proven good and the servo
    must be unpowered - on evidence the firmware manufactured itself.
    A silent bus must be allowed to stay silent.

    Re-initializing each pin as a plain input detaches it; verified on
    hardware to clear the loopback completely.
    """
    try:
        from machine import Pin

    except ImportError:                                # pragma: no cover
        # Not on the device - the test suites import this module on
        # CPython, where there is no GPIO matrix to hand anything back
        # to.
        return

    for pin in pins:
        if pin is None:
            continue

        try:
            Pin(int(pin), Pin.IN)

        except Exception:
            # A pin that cannot be reset is not a reason to fail the
            # operation that was releasing it.
            pass


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


# ======================================================================
# errors
# ======================================================================

class ST3215Error(ServoError):
    """Any ST3215 fault. Subclass of the shared actuator error."""

    code = "SERVO_ERROR"


class ST3215TimeoutError(ST3215Error):
    """The servo did not answer within the configured window."""

    code = "SERVO_UART_TIMEOUT"


class ST3215ProtocolError(ST3215Error):
    """A frame arrived, but it was not a well-formed answer to us."""

    code = "SERVO_PROTOCOL_ERROR"


class ST3215ChecksumError(ST3215Error):
    """A complete frame arrived with a checksum that does not match."""

    code = "SERVO_CHECKSUM_ERROR"


class ST3215DeviceError(ST3215Error):
    """The servo answered, reporting one of its own alarm conditions."""

    code = "SERVO_DEVICE_ERROR"


class ST3215ModeError(ST3215Error):
    """The servo is not in the operating mode this movement needs."""

    code = "SERVO_MODE_ERROR"


class ST3215PositionError(ST3215Error):
    """The movement finished somewhere other than where it was sent."""

    code = "SERVO_POSITION_MISMATCH"


class ST3215MoveTimeoutError(ST3215Error):
    """The movement did not complete inside its time budget."""

    code = "SERVO_POSITION_TIMEOUT"


class ST3215NotFoundError(ST3215Error):
    """Nothing on the bus answered a ping for this servo ID."""

    code = "SERVO_NOT_FOUND"


# ======================================================================
# backend
# ======================================================================

class ST3215:
    """
    One ST3215 on one UART.

    Construction opens nothing and moves nothing. The UART appears on
    first use, and the servo is never commanded until a caller asks for a
    movement, so powering or resetting the ESP32 cannot disturb the
    carousel.
    """

    name = "ST3215"
    description = "Waveshare ST3215 serial bus servo (encoder feedback)"

    def __init__(self, uart=None, servo_id=None, uart_id=None,
                 tx_pin=None, rx_pin=None, baudrate=None,
                 counts_per_rev=None):
        # Formerly inherited from a ServoBackend base that existed only
        # to keep two actuators honest. With one actuator left, the
        # state lives where it is used.
        self.ready = False
        self.last_move = None

        self.servo_id = (
            config.ST3215_SERVO_ID if servo_id is None else int(servo_id)
        )
        self.uart_id = (
            config.ST3215_UART_ID if uart_id is None else int(uart_id)
        )
        self.tx_pin = (
            config.ST3215_TX_PIN if tx_pin is None else int(tx_pin)
        )
        self.rx_pin = (
            config.ST3215_RX_PIN if rx_pin is None else int(rx_pin)
        )
        self.baudrate = (
            config.ST3215_BAUD if baudrate is None else int(baudrate)
        )
        self.counts_per_rev = (
            config.ST3215_COUNTS_PER_REV
            if counts_per_rev is None else int(counts_per_rev)
        )

        # Injected by the tests; otherwise created in _ensure_uart.
        self.uart = uart

        # Last decoded status byte and its alarm flags, so a reported
        # fault is never lost between the transaction and the report.
        self.last_status = 0
        self.last_status_flags = []

        # Cached operating mode. Read from the servo, never assumed.
        self.mode = None

        # Encoder reading captured when the operator confirmed the
        # physical alignment. None until then.
        self.origin_counts = None

        self.moving = False

        # Bus counters, in the same breakdown the supplied reference
        # driver keeps: an intermittent bus shows up as a number rather
        # than as an operator's impression that it is "sometimes flaky".
        self.stats = {
            "tx": 0, "rx": 0, "timeout": 0, "checksum": 0, "retry": 0,
        }

    # ------------------------------------------------------------------
    # geometry and readiness
    #
    # Inherited from ServoBackend before that base class was removed.
    # slot_step_deg and half_turn_deg read carousel GEOMETRY, not
    # calibration: how far one slot is, is a fact about the mechanism,
    # and the ST3215 needs it to turn an angle into encoder counts.
    # ------------------------------------------------------------------

    def slot_step_deg(self):
        """Angle of one logical slot transition. Geometry, not calibration."""
        return config.CAROUSEL_SLOT_GEOMETRY_DEG

    def half_turn_deg(self):
        return config.CAROUSEL_HALF_TURN_DEG

    def require_ready(self):
        if not self.ready:
            raise ServoError(
                "{} backend is not initialized.".format(self.name),
                code="SERVO_NOT_INITIALIZED",
            )

        return True

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def initialize(self):
        """
        Open UART2 and look at the servo. MOVES NOTHING.

        Ping, read the operating mode, read the position, stop. No goal is
        written, no EPROM is touched and no torque state is changed.
        """
        # A slot that is not a whole number of encoder counts would put
        # every movement fractionally off centre, and the error would
        # accumulate. Caught here rather than silently truncated.
        if self.counts_per_rev % config.CAROUSEL_SLOT_COUNT:
            raise ST3215Error(
                "ST3215_COUNTS_PER_REV ({}) does not divide evenly by "
                "CAROUSEL_SLOT_COUNT ({}), so one slot is not a whole "
                "number of encoder counts.".format(
                    self.counts_per_rev, config.CAROUSEL_SLOT_COUNT
                ),
                code="SERVO_CONFIG_ERROR",
            )

        self._ensure_uart()

        self.ping()
        self.read_mode()
        position = self.read_position()

        self.ready = True

        return {
            "servo": self.name,
            "connected": True,
            "moved": False,
            "mode": self.mode,
            "mode_name": MODE_NAMES.get(self.mode, str(self.mode)),
            "mode_correct": self.mode == config.ST3215_MODE,
            "position_counts": position,
            "position_deg": round(self.counts_to_degrees(position), 2),
        }

    def deinitialize(self):
        """
        Release UART2. Torque is deliberately left as it is.

        Handing the bus back must not drop the carousel, and it must not
        assume the same servo will be attached next time: the origin is
        forgotten here, because a different servo has a different encoder
        zero.
        """
        self.origin_counts = None
        self.ready = False
        self.mode = None

        if self.uart is None:
            return {"servo": self.name, "released": False}

        try:
            self.uart.deinit()

        except AttributeError:
            pass

        finally:
            self.uart = None

            # The bus scan reopens UART2 in BOTH pin orders, and it is
            # normally reached straight after a release. A TX pin left
            # attached here would make that scan hear its own frames.
            release_uart_pins(self.tx_pin, self.rx_pin)

        return {"servo": self.name, "released": True, "torque": "unchanged"}

    # ------------------------------------------------------------------
    # unit conversion
    # ------------------------------------------------------------------

    def counts_to_degrees(self, counts):
        return counts_to_degrees(counts, self.counts_per_rev)

    def degrees_to_counts(self, degrees):
        return degrees_to_counts(degrees, self.counts_per_rev)

    def counts_per_slot(self):
        return config.ST3215_COUNTS_PER_SLOT

    def half_turn_counts(self):
        return config.ST3215_HALF_TURN_COUNTS

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------

    def _ensure_uart(self):
        """Create UART2 on first use. Never touched at import or at boot."""
        if self.uart is None:
            from machine import UART, Pin

            self.uart = UART(
                self.uart_id,
                baudrate=self.baudrate,
                tx=Pin(self.tx_pin),
                rx=Pin(self.rx_pin),
                bits=8,
                parity=None,
                stop=1,
                timeout=config.ST3215_TIMEOUT_MS,
                timeout_char=config.ST3215_TIMEOUT_MS,
            )

        return self.uart

    def _flush_input(self):
        """
        Drop anything already sitting in the receive buffer.

        Done before every transmission: a late answer to an abandoned
        transaction must never be read as the answer to this one.
        """
        uart = self._ensure_uart()

        for _ in range(8):
            waiting = uart.any()

            if not waiting:
                return

            uart.read(waiting)

    def _scan_frame(self, buffer, sent, expected_length, seen):
        """
        Find one status packet addressed to us inside `buffer`.

        Returns (error_byte, payload) once a complete, checksum-valid
        frame for this servo has been found, or None if more bytes are
        needed. `seen` collects what was skipped, so a timeout can say
        WHY nothing usable arrived instead of just "no reply".

        The echo skip is taken from the supplied reference driver and is
        the better idea: a half-duplex adapter may loop our own
        transmission back, and rather than needing a configuration flag
        for it, any candidate byte-identical to what was just sent is
        discarded. The same code then works on an echoing single-wire
        adapter and on the Waveshare board, which does not echo.
        """
        index = 0
        length_of_buffer = len(buffer)

        while index + 4 <= length_of_buffer:
            if (
                buffer[index] != HEADER_BYTE
                or buffer[index + 1] != HEADER_BYTE
            ):
                index += 1

                continue

            servo_id = buffer[index + 2]
            length = buffer[index + 3]

            if length < 2 or length > MAX_PACKET_BODY:
                seen["garbage"] += 1
                index += 1

                continue

            end = index + 4 + length

            if end > length_of_buffer:
                # The frame is still arriving.
                return None

            frame = bytes(buffer[index:end])
            body = frame[2:-1]

            if checksum(body) != frame[-1]:
                seen["checksum"] += 1
                index += 1

                continue

            if frame == sent:
                # Our own transmission, looped back by the bus adapter.
                seen["echo"] += 1
                index = end

                continue

            if servo_id != self.servo_id:
                seen["foreign_id"] = servo_id
                index = end

                continue

            payload = frame[5:-1]

            if len(payload) != expected_length:
                seen["bad_length"] = len(payload)
                index = end

                continue

            self.last_status = frame[4]
            self.last_status_flags = decode_status_flags(frame[4])

            return (frame[4], payload)

        return None

    def _receive(self, sent, expected_length, deadline):
        """Read until a usable status packet arrives, or time runs out."""
        uart = self._ensure_uart()

        buffer = bytearray()
        seen = {
            "checksum": 0, "echo": 0, "garbage": 0,
            "foreign_id": None, "bad_length": None,
        }

        while True:
            waiting = uart.any()

            chunk = uart.read(waiting if waiting else 32)

            if chunk:
                buffer.extend(chunk)

                found = self._scan_frame(buffer, sent, expected_length, seen)

                if found is not None:
                    return found

            if len(buffer) > MAX_PACKET_BODY * 4:
                # Desynchronised beyond any useful recovery.
                self.stats["checksum"] += 1

                raise ST3215ProtocolError(
                    "Servo {} bus is desynchronised: {} bytes of unusable "
                    "traffic.".format(self.servo_id, len(buffer))
                )

            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                self._raise_receive_failure(seen, len(buffer))

            time.sleep_ms(1)

    def _raise_receive_failure(self, seen, buffered):
        """Turn "nothing usable arrived" into the most specific fault."""
        if seen["foreign_id"] is not None:
            raise ST3215ProtocolError(
                "A servo with ID {} answered instead of ID {}. Check that "
                "only one servo on the bus uses this ID.".format(
                    seen["foreign_id"], self.servo_id
                )
            )

        if seen["bad_length"] is not None:
            raise ST3215ProtocolError(
                "Servo {} returned {} data bytes, which is not what this "
                "request asked for.".format(
                    self.servo_id, seen["bad_length"]
                )
            )

        if seen["checksum"]:
            self.stats["checksum"] += 1

            raise ST3215ChecksumError(
                "{} frame(s) from servo {} failed the checksum. This is a "
                "wiring, grounding or baud rate problem, not a servo "
                "fault.".format(seen["checksum"], self.servo_id)
            )

        self.stats["timeout"] += 1

        raise ST3215TimeoutError(
            "Servo {} did not answer within {} ms ({} byte(s) "
            "received).".format(
                self.servo_id, config.ST3215_TIMEOUT_MS, buffered
            )
        )

    def _transaction(self, instruction, params, expected_length,
                     retries=None):
        """
        One request and its answer, retried a bounded number of times.

        Only transport faults are retried. A servo that answered with an
        alarm has been heard correctly, and retrying would hide it.

        `retries=0` is for requests that must never be repeated. A goal
        position in step servo mode is RELATIVE: if the servo executed
        the write but its acknowledgement was lost, resending it would
        step a second time and the carousel would travel twice as far.
        Failing the transaction is the safe outcome - the caller
        invalidates its position and the operator re-synchronizes.
        """
        packet = build_packet(self.servo_id, instruction, params)

        if retries is None:
            retries = config.ST3215_RETRIES

        attempts = int(retries) + 1
        last_error = None

        for attempt in range(attempts):
            if attempt:
                self.stats["retry"] += 1
                time.sleep_ms(config.ST3215_RETRY_DELAY_MS)

            try:
                self._flush_input()

                uart = self._ensure_uart()

                self.stats["tx"] += 1
                uart.write(packet)

                deadline = time.ticks_add(
                    time.ticks_ms(), config.ST3215_TIMEOUT_MS
                )

                _error_byte, payload = self._receive(
                    packet, expected_length, deadline
                )

                self.stats["rx"] += 1

                return payload

            except (ST3215TimeoutError, ST3215ProtocolError,
                    ST3215ChecksumError) as error:
                last_error = error

        raise last_error

    # ------------------------------------------------------------------
    # register access
    # ------------------------------------------------------------------

    def ping(self):
        """
        Ask the servo to identify itself. Moves nothing.

        Raises ST3215NotFoundError when nothing answers, which is a
        different fault from a servo that answers with an alarm.
        """
        try:
            self._transaction(INST_PING, b"", 0)

        except ST3215TimeoutError:
            raise ST3215NotFoundError(
                "No answer from servo ID {} on UART{} (TX GPIO{}, RX "
                "GPIO{}, {} baud). Check the external servo supply at the "
                "driver board, the three-wire link and the common "
                "ground.".format(
                    self.servo_id, self.uart_id, self.tx_pin,
                    self.rx_pin, self.baudrate
                )
            )

        return {
            "id": self.servo_id,
            "status": self.last_status,
            "status_flags": list(self.last_status_flags),
        }

    def read_register(self, address, length):
        """Raw register read; returns `length` bytes."""
        return self._transaction(
            INST_READ, bytes([address & 0xFF, length & 0xFF]), length
        )

    def write_register(self, address, data, retries=None):
        """Raw register write. The servo acknowledges with a status packet."""
        return self._transaction(
            INST_WRITE, bytes([address & 0xFF]) + bytes(data), 0,
            retries=retries,
        )

    def read_byte(self, address):
        return self.read_register(address, 1)[0]

    def read_word(self, address):
        data = self.read_register(address, 2)

        return bytes_word(data[0], data[1])

    def write_byte(self, address, value):
        return self.write_register(address, bytes([int(value) & 0xFF]))

    def write_word(self, address, value):
        return self.write_register(address, word_bytes(value))

    # ------------------------------------------------------------------
    # feedback
    # ------------------------------------------------------------------

    def read_position(self):
        """
        Present encoder position, in counts.

        REFERENCE DRIVER DIFFERS: the supplied st3215_position() returns a
        plain uint16_t. That is harmless while the servo stays inside a
        single 0..4095 turn, but the official ReadPos() applies the
        bit-15 sign rule and the memory table allows multi-turn values,
        so the sign is decoded here.
        """
        return decode_signed(self.read_word(REG_PRESENT_POSITION))

    def read_mode(self, refresh=True):
        """Operating mode, read from the servo rather than assumed."""
        if refresh or self.mode is None:
            self.mode = self.read_byte(REG_MODE)

        return self.mode

    def read_moving(self):
        return bool(self.read_byte(REG_MOVING))

    def read_torque_enabled(self):
        return bool(self.read_byte(REG_TORQUE_SWITCH))

    def read_feedback(self):
        """
        The whole telemetry block in one transaction.

        One read of registers 56..70 rather than seven separate ones: it
        is faster, and every value belongs to the same instant.
        """
        data = self.read_register(FEEDBACK_FIRST, FEEDBACK_LENGTH)

        def word(register):
            index = register - FEEDBACK_FIRST

            return bytes_word(data[index], data[index + 1])

        def byte(register):
            return data[register - FEEDBACK_FIRST]

        position = decode_signed(word(REG_PRESENT_POSITION))
        status_byte = byte(REG_SERVO_STATUS)

        self.last_status = status_byte
        self.last_status_flags = decode_status_flags(status_byte)
        self.moving = bool(byte(REG_MOVING))

        return {
            "position_counts": position,
            "position_deg": round(self.counts_to_degrees(position), 2),

            # Sign in bit 15 for speed and current, bit 10 for load. The
            # load rule is unusual and both the official library and the
            # supplied reference driver confirm it.
            "speed_steps_per_s": decode_signed(
                word(REG_PRESENT_SPEED)
            ),
            "load_permille": decode_signed(
                word(REG_PRESENT_LOAD), 10
            ),
            "voltage_v": round(
                byte(REG_PRESENT_VOLTAGE) * VOLTAGE_V_PER_LSB, 1
            ),
            "temperature_c": byte(REG_PRESENT_TEMPERATURE),
            "current_ma": round(
                decode_signed(word(REG_PRESENT_CURRENT))
                * CURRENT_MA_PER_LSB, 1
            ),
            "moving": self.moving,
            "status": status_byte,
            "status_flags": list(self.last_status_flags),
        }

    # ------------------------------------------------------------------
    # torque
    # ------------------------------------------------------------------

    def enable_torque(self):
        """Hold position under load. Commands no movement by itself."""
        self.write_byte(REG_TORQUE_SWITCH, TORQUE_ON)

        return True

    def disable_torque(self):
        """
        Let the output shaft be turned by hand.

        Never called automatically: a carousel that goes limp between
        movements can be nudged by the rover itself, and the sample would
        then be measured off centre.

        Note this writes 0, not 128. Writing 128 to the torque register
        does NOT release the servo - it rewrites the position offset so
        the current position reads 2048. See st3215_registers.py.
        """
        self.write_byte(REG_TORQUE_SWITCH, TORQUE_OFF)

        return True

    def stop(self):
        """
        Abort whatever the servo is doing and hold where it is.

        The official driver board demo stops a servo by dropping torque
        and immediately restoring it, which cancels the outstanding goal
        without leaving the mechanism free to turn.
        """
        self.write_byte(REG_TORQUE_SWITCH, TORQUE_OFF)
        time.sleep_ms(config.ST3215_STOP_PAUSE_MS)
        self.write_byte(REG_TORQUE_SWITCH, TORQUE_ON)

        self.moving = False

        return {
            "servo": self.name,
            "stopped": True,
            "torque": True,
            "position_counts": self.read_position_or_none(),
        }

    # ------------------------------------------------------------------
    # service configuration (EPROM)
    # ------------------------------------------------------------------

    def configure_mode(self, mode=None):
        """
        Put the servo into the operating mode the carousel needs.

        SERVICE OPERATION. This writes EPROM, so it is never part of
        routine carousel calibration: EPROM has a finite write life, and
        silently reconfiguring a servo somebody set up by hand is not
        something a science instrument should do.

        The sequence is the official one from ServoDriverST/STSCTRL.h, and
        the supplied reference driver's eeprom_write() does the same:
        unlock, write, relock - including relocking after a failure.
        """
        if mode is None:
            mode = config.ST3215_MODE

        mode = int(mode)

        if mode not in (MODE_POSITION, MODE_STEP):
            raise ST3215ModeError(
                "Only position servo mode (0) and step servo mode (3) are "
                "supported for the carousel; {} was requested.".format(mode)
            )

        self.write_byte(REG_LOCK, LOCK_OPEN)

        try:
            self.write_byte(REG_MODE, mode)
            self.write_word(REG_MIN_ANGLE_LIMIT, 0)
            self.write_word(REG_MAX_ANGLE_LIMIT, 0)

        finally:
            self.write_byte(REG_LOCK, LOCK_CLOSED)

        self.mode = mode

        return {
            "servo": self.name,
            "mode": mode,
            "mode_name": MODE_NAMES.get(mode, str(mode)),
            "min_angle_limit": self.read_word(REG_MIN_ANGLE_LIMIT),
            "max_angle_limit": self.read_word(REG_MAX_ANGLE_LIMIT),
            "note": "Written to EPROM; it survives a power cycle.",
        }

    def require_mode(self, mode=None):
        """
        Refuse to move unless the servo is in the expected mode.

        A safety check, not a formality: the same goal-position register
        means "step this far" in mode 3 and "go to this absolute count" in
        mode 0, so a servo in the wrong mode would take a request for one
        slot and travel somewhere else entirely.
        """
        if mode is None:
            mode = config.ST3215_MODE

        actual = self.read_mode()

        if actual != mode:
            raise ST3215ModeError(
                "Servo {} is in {} mode ({}), but the carousel needs {} "
                "mode ({}). Run the servo configuration command "
                "first.".format(
                    self.servo_id,
                    MODE_NAMES.get(actual, "unknown"), actual,
                    MODE_NAMES.get(mode, "unknown"), mode,
                )
            )

        return actual

    # ------------------------------------------------------------------
    # movement
    # ------------------------------------------------------------------

    def write_goal(self, value, speed=None, acceleration=None):
        """
        Write acceleration, goal position, goal time and goal speed in one
        seven-byte transaction starting at register 41.

        This is the frame the official WritePosEx() builds and the one the
        supplied st3215_move() builds. Doing it as a single write matters:
        the servo starts moving the moment the goal position lands, so a
        speed written afterwards would apply to the NEXT movement.

        `value` is a relative step count in step servo mode and an
        absolute encoder target in position servo mode. Only the caller
        knows the mode, so only the caller decides.
        """
        if speed is None:
            speed = config.ST3215_SPEED

        if acceleration is None:
            acceleration = config.ST3215_ACCELERATION

        speed = max(0, min(int(speed), 65535))
        acceleration = max(0, min(int(acceleration), 254))

        payload = (
            bytes([acceleration])
            + word_bytes(encode_signed(value))
            + word_bytes(0)                # goal time, PWM mode only
            + word_bytes(speed)
        )

        # Never retried: in step servo mode this is a RELATIVE command, so
        # a resend after a lost acknowledgement would move the carousel
        # twice.
        self.write_register(REG_ACCELERATION, payload, retries=0)

        return {
            "goal": int(value),
            "speed": speed,
            "acceleration": acceleration,
        }

    def move_to_position(self, counts, speed=None, acceleration=None):
        """
        Absolute movement, position servo mode only.

        Deliberately NOT how the carousel moves. With the factory
        0..4095 angle limits an absolute target cannot cross the encoder
        seam, so one slot from count 4000 would become a journey the long
        way round. It exists because a generic servo driver should be able
        to do it.
        """
        self.require_mode(MODE_POSITION)

        return self._execute_move(
            int(counts), None, speed, acceleration, absolute=True
        )

    def move_relative(self, counts, speed=None, acceleration=None):
        """
        Move by a signed number of encoder counts and prove it happened.

        Positive is clockwise. In step servo mode this is exactly what the
        servo's goal register means, so the encoder seam does not exist:
        +512 counts is +512 counts whether the servo sits at 100 or 4000.
        """
        counts = int(counts)

        limit = self.counts_per_rev // 2

        if abs(counts) > limit:
            # Past half a turn a single encoder reading can no longer tell
            # a movement from its complement, so the driver refuses to
            # claim a verification it cannot make. Callers split long
            # movements; the carousel never needs to.
            raise ST3215Error(
                "A single verified movement is limited to {} counts, half "
                "a revolution; {} was requested. Split it into smaller "
                "movements.".format(limit, counts),
                code="SERVO_STEP_TOO_LARGE",
            )

        mode = self.require_mode()

        if mode == MODE_STEP:
            return self._execute_move(
                counts, None, speed, acceleration, absolute=False
            )

        # Position servo mode is only safe for a relative movement when
        # the servo is set up for multi-turn absolute control, which the
        # memory table defines as both angle limits being zero.
        minimum = self.read_word(REG_MIN_ANGLE_LIMIT)
        maximum = self.read_word(REG_MAX_ANGLE_LIMIT)

        if minimum != 0 or maximum != 0:
            raise ST3215ModeError(
                "Servo {} is in position servo mode with angle limits "
                "{}..{}, so it cannot cross the 4095/0 boundary and a "
                "relative movement could travel the long way round. Use "
                "step servo mode, or clear both angle limits for "
                "multi-turn control.".format(self.servo_id, minimum, maximum)
            )

        start = self.read_position()

        return self._execute_move(
            start + counts, start, speed, acceleration,
            absolute=True, requested=counts,
        )

    def _execute_move(self, goal, start, speed, acceleration,
                      absolute, requested=None):
        """
        Command one movement and verify it from encoder feedback:

            read start -> command -> poll to completion -> settle ->
            read again -> compare -> report

        There is no sleep-and-hope path anywhere in here. A movement that
        cannot be proved raises, and the caller invalidates its position
        rather than assuming success.
        """
        started_at = time.ticks_ms()

        if start is None:
            start = self.read_position()

        if requested is None:
            if absolute:
                requested = centred_error(
                    goal - start, self.counts_per_rev
                )
            else:
                requested = int(goal)

        torque_enabled_here = False

        if not self.read_torque_enabled():
            if not config.ST3215_AUTO_ENABLE_TORQUE:
                raise ST3215Error(
                    "Servo {} has its torque switch off, so it would not "
                    "hold the carousel. Enable torque before "
                    "moving.".format(self.servo_id),
                    code="SERVO_TORQUE_DISABLED",
                )

            self.enable_torque()
            time.sleep_ms(config.ST3215_SETTLE_MS)

            # Enabling torque can itself take up a little slack, so the
            # movement is measured from AFTER that, never before.
            start = self.read_position()
            torque_enabled_here = True

        expected = wrap_counts(start + requested, self.counts_per_rev)

        command = self.write_goal(goal, speed, acceleration)

        self.moving = True

        timeout_ms = self._move_timeout_ms(requested, command["speed"])
        deadline = time.ticks_add(time.ticks_ms(), timeout_ms)

        polls = 0
        position = start
        settled = False
        idle_polls = 0
        seen_moving = False

        # Wait for the servo to say it has STOPPED, not merely that it is
        # near the target: declaring victory while the mechanism is still
        # running would measure the overshoot instead of the result.
        #
        # Two guards make "it has stopped" trustworthy:
        #
        #   - the moving flag must read zero on ST3215_STOP_CONFIRM_POLLS
        #     consecutive polls, so a single zero cannot be believed;
        #
        #   - and either the servo must have been seen moving at least
        #     once, or it must already be at the target. Without that, a
        #     servo which has not started yet would be reported as having
        #     stopped in the wrong place, when the honest answer is that
        #     it never began - a timeout, not a mismatch.
        while True:
            time.sleep_ms(config.ST3215_POLL_INTERVAL_MS)

            polls += 1

            position = self.read_position()

            if self.read_moving():
                seen_moving = True
                idle_polls = 0

            else:
                idle_polls += 1

            at_target = (
                abs(centred_error(
                    expected - position, self.counts_per_rev
                ))
                <= config.ST3215_POSITION_TOLERANCE
            )

            if idle_polls >= config.ST3215_STOP_CONFIRM_POLLS:
                if seen_moving or at_target:
                    settled = True

                    break

            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                break

        self.moving = False

        # Let the mechanism stop ringing before the reading that counts.
        if config.ST3215_SETTLE_MS > 0:
            time.sleep_ms(config.ST3215_SETTLE_MS)

        position = self.read_position()
        error = centred_error(expected - position, self.counts_per_rev)
        within = abs(error) <= config.ST3215_POSITION_TOLERANCE

        record = {
            "servo": self.name,
            "moved": bool(requested),
            "verified": True,
            "mode": self.mode,
            "requested_counts": requested,
            "requested_degrees": round(
                self.counts_to_degrees(requested), 3
            ),
            "goal_value": int(goal),
            "absolute": bool(absolute),
            "start_position": start,
            "expected_position": expected,
            "actual_position": position,
            "position_error": error,
            "position_error_deg": round(self.counts_to_degrees(error), 3),
            "tolerance_counts": config.ST3215_POSITION_TOLERANCE,
            "within_tolerance": within,
            "speed": command["speed"],
            "acceleration": command["acceleration"],
            "elapsed_ms": time.ticks_diff(time.ticks_ms(), started_at),
            "timeout_ms": timeout_ms,
            "polls": polls,
            "settled": settled,
            "torque_enabled_by_driver": torque_enabled_here,
            "status": self.last_status,
            "status_flags": list(self.last_status_flags),
        }

        self.last_move = record

        if not within:
            if not settled:
                raise ST3215MoveTimeoutError(
                    "Servo {} did not reach its target within {} ms: {} "
                    "counts were requested and the encoder is still {} "
                    "counts away from position {}.".format(
                        self.servo_id, timeout_ms, requested, error, expected
                    )
                )

            raise ST3215PositionError(
                "Servo {} stopped {} counts ({:.2f} deg) away from "
                "position {}, outside the {} count tolerance.".format(
                    self.servo_id, error, self.counts_to_degrees(error),
                    expected, config.ST3215_POSITION_TOLERANCE
                )
            )

        return record

    def _move_timeout_ms(self, counts, speed):
        """
        Time budget for one movement, from its distance and its speed.

        A single fixed timeout is either too short for the 180 degree
        sweep or so long that a stalled servo takes a minute to report.
        The speed register is in steps per second, so the nominal travel
        time is known exactly; the margin covers the acceleration ramp and
        a loaded mechanism.
        """
        counts = abs(int(counts))

        if speed <= 0:
            return int(config.ST3215_MOVE_TIMEOUT_MS)

        budget = (
            config.ST3215_MOVE_TIMEOUT_BASE_MS
            + (counts * 1000.0 / float(speed))
            * config.ST3215_MOVE_TIMEOUT_MARGIN
        )

        return int(min(budget, config.ST3215_MOVE_TIMEOUT_MS))

    # ------------------------------------------------------------------
    # the carousel-facing interface
    # ------------------------------------------------------------------

    def move_slots(self, direction, slots):
        """
        Move by whole carousel slots, each one separately verified.

        Stepping rather than one long movement is not caution for its own
        sake: a single encoder reading cannot verify a movement of more
        than half a turn, and a fault halfway through must not leave the
        mechanism at an unknown angle with the software none the wiser.
        """
        self.require_ready()
        validate_direction(direction)

        slots = int(slots)

        if slots < 0:
            raise ST3215Error("slot count must not be negative")

        counts = self.counts_per_slot() * direction_sign(direction)
        legs = []

        for _ in range(slots):
            legs.append(self.move_relative(counts))

        record = {
            "servo": self.name,
            "verified": True,
            "direction": direction,
            "slots": slots,
            "degrees": slots * self.slot_step_deg(),
            "counts": counts * slots,
            "moved": slots > 0,
            "legs": legs,
            "position_error": legs[-1]["position_error"] if legs else 0,
            "actual_position": legs[-1]["actual_position"] if legs else None,
            "elapsed_ms": sum(leg["elapsed_ms"] for leg in legs),
        }

        self.last_move = record

        return record

    def move_degrees(self, degrees):
        """Signed relative movement in degrees; positive is clockwise."""
        self.require_ready()

        degrees = float(degrees)
        counts = self.degrees_to_counts(degrees)

        if counts == 0:
            resolution = 360.0 / self.counts_per_rev

            record = {
                "servo": self.name,
                "verified": True,
                "moved": False,
                "requested_degrees": degrees,
                "requested_counts": 0,
                "resolution_deg": round(resolution, 4),
                "message": "Below one encoder count ({:.4f} deg); nothing "
                           "was moved.".format(resolution),
            }

            self.last_move = record

            return record

        record = self.move_relative(counts)

        record["requested_degrees"] = degrees

        # What the servo was actually told to travel, after rounding to
        # whole encoder counts. 2.0 deg becomes 23 counts, which is
        # 2.0215 deg - and the caller deserves to know that rather than
        # discovering it later as apparent drift.
        record["commanded_degrees"] = round(self.counts_to_degrees(counts), 4)

        return record

    def half_turn(self, direction):
        """
        Half a revolution, in the given direction.

        Exactly ST3215_HALF_TURN_COUNTS of encoder travel, verified like
        any other movement. There is no separate calibration for it: with
        a closed-loop actuator, half a turn is half a turn. What the
        carousel USES it for is the carousel's business.
        """
        self.require_ready()
        validate_direction(direction)

        counts = self.half_turn_counts() * direction_sign(direction)

        record = self.move_relative(counts)

        record["direction"] = direction
        record["degrees"] = self.half_turn_deg()

        self.last_move = record

        return record

    def capture_origin(self):
        """
        Store the encoder reading the operator has just confirmed.

        Nothing moves. This is what ties the logical slot numbering to a
        real measurement rather than to an assumption.
        """
        self.require_ready()

        self.origin_counts = self.read_position()

        return {
            "servo": self.name,
            "feedback": True,
            "origin_counts": self.origin_counts,
            "origin_deg": round(
                self.counts_to_degrees(self.origin_counts), 2
            ),
        }

    def travel_since_origin_deg(self):
        """
        Measured angle travelled since capture_origin(), or None.

        Reduced to (-180, +180]: the carousel compares it against an
        equally reduced expectation, which is the only comparison that
        means anything on a rotary axis.
        """
        if self.origin_counts is None:
            return None

        try:
            position = self.read_position()

        except ServoError:
            return None

        return self.counts_to_degrees(
            centred_error(
                position - self.origin_counts, self.counts_per_rev
            )
        )

    def read_position_or_none(self):
        """
        Best effort position, for a failure report.

        Used only after something has already gone wrong. None means the
        servo could not be reached, which is itself the answer: the
        position is unknown and must be re-established by hand.
        """
        try:
            return self.read_position()

        except Exception:
            return None

    # ------------------------------------------------------------------
    # calibration surface
    # ------------------------------------------------------------------

    def calibration(self):
        """
        The ST3215's tunables, for display.

        Read only, and deliberately so: these are not empirical timings
        to be converged on with the carousel in front of you, they are
        engineering limits that belong in config.py under version
        control. `set_calibration` raises SERVO_NOT_SUPPORTED.
        """
        return {
            "servo": self.name,
            "editable": False,
            "current": {
                "speed_steps_per_s": config.ST3215_SPEED,
                "acceleration": config.ST3215_ACCELERATION,
                "position_tolerance_counts": config.ST3215_POSITION_TOLERANCE,
                "settle_ms": config.ST3215_SETTLE_MS,
                "poll_interval_ms": config.ST3215_POLL_INTERVAL_MS,
                "move_timeout_ms": config.ST3215_MOVE_TIMEOUT_MS,
            },
            "units": {
                "speed_steps_per_s": "encoder steps per second, max 3400",
                "acceleration": "x100 steps/s^2, max 254",
                "position_tolerance_counts": "1 count = {:.4f} deg".format(
                    360.0 / self.counts_per_rev
                ),
            },
            "note": "Closed-loop actuator: there is no timing calibration. "
                    "Set the tolerance from the measured closing error of "
                    "an out-and-back test, in firmware/ESP32/config.py.",
        }

    # ------------------------------------------------------------------
    # movement tests
    # ------------------------------------------------------------------

    def test_move_kinds(self):
        return (
            ("slot_out_and_back", "One slot forward and back"),
            ("slot_forward", "One slot forward"),
            ("slot_reverse", "One slot back"),
            ("out_and_back", "Half turn out and back (the measurement path)"),
            ("half_turn_forward", "Half turn forward"),
            ("half_turn_reverse", "Half turn back"),
            ("wrap", "Encoder boundary crossing (4095 -> 0)"),
            ("degrees", "An arbitrary angle"),
        )

    def _test_legs(self, kind, degrees=None):
        """The relative movements one test performs, in encoder counts."""
        slot = self.counts_per_slot()
        half = self.half_turn_counts()
        forward = direction_sign(config.CAROUSEL_FORWARD_DIRECTION)

        if kind == "slot_forward":
            return [slot * forward]

        if kind == "slot_reverse":
            return [-slot * forward]

        if kind == "slot_out_and_back":
            return [slot * forward, -slot * forward]

        if kind == "half_turn_forward":
            return [half * forward]

        if kind == "half_turn_reverse":
            return [-half * forward]

        if kind == "out_and_back":
            return [half * forward, -half * forward]

        if kind == "degrees":
            counts = self.degrees_to_counts(degrees)

            if counts == 0:
                raise ST3215Error(
                    "{} deg is below one encoder count ({:.4f} deg).".format(
                        degrees, 360.0 / self.counts_per_rev
                    ),
                    code="SERVO_BAD_REQUEST",
                )

            return [counts]

        if kind == "wrap":
            # The 4095 -> 0 boundary test. Park a few counts short of the
            # seam, step forward across it, then step back. A driver that
            # thought in absolute single-turn targets would take the long
            # way round here; a relative step cannot.
            margin = min(slot // 4, 64)
            target = self.counts_per_rev - margin
            approach = centred_error(
                target - self.read_position(), self.counts_per_rev
            )

            legs = []

            if approach:
                legs.append(approach)

            legs.append(slot)
            legs.append(-slot)

            return legs

        raise ST3215Error(
            "unknown test kind: {}".format(kind), code="SERVO_BAD_REQUEST"
        )

    def test_move(self, kind, repeat=1, degrees=None):
        """
        Run one movement test, verified against the encoder at every leg.

        Returns the net travel and the closing error, which for a
        symmetrical test is the repeatability figure that actually
        matters.
        """
        self.require_ready()

        legs = self._test_legs(kind, degrees)
        net = sum(legs) * int(repeat)

        start_position = self.read_position()
        results = []

        for _ in range(int(repeat)):
            for counts in legs:
                results.append(self.move_relative(counts))

        end_position = results[-1]["actual_position"] if results else None

        closing = None

        if start_position is not None and end_position is not None:
            closing = centred_error(
                end_position - start_position - net, self.counts_per_rev
            )

        return {
            "servo": self.name,
            "kind": kind,
            "moved": True,
            "verified": True,
            "repeat": int(repeat),
            "legs": legs,
            "leg_count": len(results),
            "net_counts": net,
            "net_degrees": round(self.counts_to_degrees(net), 3),
            "start_position": start_position,
            "end_position": end_position,
            "closed_loop_error_counts": closing,
            "closed_loop_error_deg": (
                None if closing is None
                else round(self.counts_to_degrees(closing), 3)
            ),
            "worst_position_error": max(
                [abs(entry["position_error"]) for entry in results] or [0]
            ),
            "tolerance_counts": config.ST3215_POSITION_TOLERANCE,
            "movements": results,
            "position_invalidated": bool(net),
        }

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------

    def link_description(self):
        """The wiring this backend expects, for status and diagnostics."""
        return {
            "servo": self.name,
            "driver": "ST3215",
            "bus": "Waveshare serial bus servo driver board",
            "id": self.servo_id,
            "uart_id": self.uart_id,
            "tx_pin": self.tx_pin,
            "rx_pin": self.rx_pin,
            "baud": self.baudrate,
            "counts_per_rev": self.counts_per_rev,
            "power": "external supply at the driver board; the ESP32 PCB "
                     "provides TX, RX and ground only",
        }

    def status(self):
        """
        Live servo state. Never raises.

        A servo that has gone quiet must still produce a status the PC can
        display, or a communication fault looks like a dead module.
        """
        report = self.link_description()

        report["ready"] = self.ready
        report["connected"] = False
        report["moving"] = self.moving
        report["origin_counts"] = self.origin_counts

        try:
            report.update(self.read_feedback())

            report["connected"] = True
            report["mode"] = self.read_mode()
            report["mode_name"] = MODE_NAMES.get(
                report["mode"], str(report["mode"])
            )
            report["torque_enabled"] = self.read_torque_enabled()

        except ServoError as error:
            report["error"] = error.as_dict()

        except Exception as error:
            report["error"] = {"code": "SERVO_ERROR", "message": str(error)}

        report["expected_mode"] = config.ST3215_MODE
        report["mode_correct"] = report.get("mode") == config.ST3215_MODE
        report["bus"] = dict(self.stats)
        report["last_move"] = self.last_move

        return report

    def diagnostics(self):
        """
        Full communication check. MOVES NOTHING.

        Every stage is reported separately, so a failure names the exact
        step that stopped it instead of one flat "servo error".
        """
        report = self.link_description()
        report["steps"] = []
        report["ok"] = False
        report["moved"] = False

        def step(name, call):
            try:
                value = call()

                report["steps"].append({
                    "step": name, "ok": True, "value": value
                })

                return value

            except ServoError as error:
                report["steps"].append({
                    "step": name, "ok": False, "error": error.as_dict()
                })

                raise

        try:
            step("uart", lambda: {
                "uart_id": self.uart_id,
                "tx_pin": self.tx_pin,
                "rx_pin": self.rx_pin,
                "baud": self.baudrate,
                "initialized": self._ensure_uart() is not None,
            })

            step("ping", self.ping)
            step("id", lambda: self.read_byte(REG_ID))

            baud_code = step(
                "baud_code", lambda: self.read_byte(REG_BAUD_RATE)
            )
            mode = step("mode", lambda: self.read_mode())
            torque = step("torque", self.read_torque_enabled)
            step("angle_limits", lambda: {
                "min": self.read_word(REG_MIN_ANGLE_LIMIT),
                "max": self.read_word(REG_MAX_ANGLE_LIMIT),
            })
            feedback = step("feedback", self.read_feedback)

            report["ok"] = True
            report["connected"] = True
            report["mode"] = mode
            report["mode_name"] = MODE_NAMES.get(mode, str(mode))
            report["mode_correct"] = mode == config.ST3215_MODE
            report["expected_mode"] = config.ST3215_MODE
            report["torque_enabled"] = torque
            report["baud_reported"] = BAUD_CODES.get(baud_code)
            report["baud_matches"] = (
                BAUD_CODES.get(baud_code) == self.baudrate
            )
            report["feedback"] = feedback
            report["status_flags"] = list(self.last_status_flags)

            if not report["mode_correct"]:
                report["ok"] = False
                report["error"] = {
                    "code": ST3215ModeError.code,
                    "message": "Servo is in {} mode; the carousel needs {} "
                               "mode. Run the servo configuration "
                               "command.".format(
                                   MODE_NAMES.get(mode, mode),
                                   MODE_NAMES.get(
                                       config.ST3215_MODE, config.ST3215_MODE
                                   ),
                               ),
                }

        except ServoError as error:
            report["error"] = error.as_dict()

        report["bus"] = dict(self.stats)

        return report


# ======================================================================
# bus scan
# ======================================================================
#
# WHY THIS EXISTS
#
# "No answer from servo ID 1 on UART2 (TX GPIO17, RX GPIO16, 1000000
# baud)" is true and nearly useless: it names every assumption at once
# and tests none of them. Four independent things must all be right
# before a servo answers - the ID, the baud rate, which physical wire
# carries TX and which carries RX, and whether the servo itself has power
# - and an operator who has checked the wiring and measured the supply
# has no way to tell which one is wrong.
#
# So this asks the bus instead. It probes every documented baud rate, in
# both pin orders, and reports what came back at each combination:
#
#     a valid frame   -> found it, and at which settings
#     our own echo    -> the ESP32 transmits and hears itself, so the TX
#                        path and the adapter work; the servo is not
#                        answering
#     bytes, no frame -> something IS transmitting but nothing decodes:
#                        baud mismatch, or a missing common ground
#     nothing at all  -> not one byte on RX at any rate in either pin
#                        order: no power at the servo, no RX wire, or the
#                        adapter is not passing the bus through
#
# MOVES NOTHING. Only INST_PING is ever sent, which is a read: it asks a
# servo to identify itself, and no servo moves in response.

# The eight rates the ST3215 memory table defines, most likely first.
SCAN_BAUDS = (1000000, 500000, 115200, 250000, 128000, 76800, 57600, 38400)

# Per-probe listening window. Longer than a healthy servo needs - it
# answers within a millisecond - because a probe that is too short would
# report "nothing there" for a slow or marginal bus.
SCAN_TIMEOUT_MS = 30

# Hard ceiling on one scan, so a full sweep cannot outlive the PC's
# patience or look like a hung module.
SCAN_MAX_PROBES = 2200


def _hexlify(data):
    """Hex without binascii, which is not guaranteed on every port."""
    return "".join("{:02x}".format(byte) for byte in data)


def _describe_probe(buffer, sent, servo_id, expect_echo=True):
    """Turn the bytes one probe collected into a verdict about the bus."""
    report = {
        "id": servo_id,
        "bytes": len(buffer),
        "echo": False,
        "answered": False,
        "checksum_errors": 0,
        "status_byte": None,
        "status_flags": [],
        "other_ids": [],
        "sample": _hexlify(buffer[:24]),
    }

    if not buffer:
        return report

    if expect_echo and sent in buffer:
        # A half-duplex adapter loops our own transmission back. That is
        # not an answer, but it does prove the TX path works.
        report["echo"] = True

    index = 0

    while index + 4 <= len(buffer):
        if (
            buffer[index] != HEADER_BYTE
            or buffer[index + 1] != HEADER_BYTE
        ):
            index += 1

            continue

        length = buffer[index + 3]

        if length < 2 or length > MAX_PACKET_BODY:
            index += 1

            continue

        end = index + 4 + length

        if end > len(buffer):
            break

        frame = buffer[index:end]

        if checksum(frame[2:-1]) != frame[-1]:
            report["checksum_errors"] += 1
            index += 1

            continue

        if frame == sent:
            index = end

            continue

        found_id = frame[2]

        if found_id == servo_id:
            report["answered"] = True
            report["status_byte"] = frame[4]
            report["status_flags"] = decode_status_flags(frame[4])

        elif found_id not in report["other_ids"]:
            report["other_ids"].append(found_id)

        index = end

    return report


def _probe_id(uart, servo_id, timeout_ms, expect_echo=True):
    """
    Ping one ID on an already-open UART and describe what came back.

    Raises nothing: a probe that finds nothing is a result, not a fault.
    """
    packet = build_packet(servo_id, INST_PING, b"")

    # Anything already in the buffer belongs to the previous probe.
    for _ in range(8):
        waiting = uart.any()

        if not waiting:
            break

        uart.read(waiting)

    uart.write(packet)

    buffer = bytearray()
    deadline = time.ticks_add(time.ticks_ms(), timeout_ms)

    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        waiting = uart.any()

        if waiting:
            chunk = uart.read(waiting)

            if chunk:
                buffer.extend(chunk)

        else:
            time.sleep_ms(1)

    return _describe_probe(bytes(buffer), packet, servo_id, expect_echo)


def _scan_report(probes, found, uart_id, tx_pin, rx_pin, servo_ids, bauds):
    """Assemble the scan result and say what it means, in one place."""
    any_bytes = any(probe["bytes"] for probe in probes)
    any_echo = any(probe["echo"] for probe in probes)
    any_checksum = any(probe["checksum_errors"] for probe in probes)

    other_ids = []

    for probe in probes:
        for other in probe["other_ids"]:
            if other not in other_ids:
                other_ids.append(other)

    other_ids.sort()

    report = {
        "moved": False,
        "scanned": {
            "uart_id": uart_id,
            "configured_tx_pin": tx_pin,
            "configured_rx_pin": rx_pin,
            "configured_baud": config.ST3215_BAUD,
            "configured_id": config.ST3215_SERVO_ID,
            "ids": servo_ids,
            "id_count": len(servo_ids),
            "bauds": bauds,
            "probes": len(probes),
        },
        "found": found,
        "probes": probes,
        "any_traffic": any_bytes,
        "echo_seen": any_echo,
        "checksum_errors": any_checksum,
        "foreign_ids": other_ids,
    }

    if found:
        first = found[0]

        report["result"] = "SERVO_FOUND"
        report["ok"] = True
        report["matches_config"] = (
            first["id"] == config.ST3215_SERVO_ID
            and first["baud"] == config.ST3215_BAUD
            and first["tx_pin"] == tx_pin
            and first["rx_pin"] == rx_pin
        )

        differences = []

        if first["id"] != config.ST3215_SERVO_ID:
            differences.append(
                "it answers to ID {}, not the configured ID {} "
                "(ST3215_SERVO_ID)".format(
                    first["id"], config.ST3215_SERVO_ID
                )
            )

        if first["baud"] != config.ST3215_BAUD:
            differences.append(
                "it answers at {} baud, not the configured {} "
                "(ST3215_BAUD)".format(first["baud"], config.ST3215_BAUD)
            )

        if first["tx_pin"] != tx_pin:
            differences.append(
                "it answers only with TX and RX exchanged: the servo's "
                "data line reaches GPIO{} and the ESP32 must transmit on "
                "GPIO{}. Swap the two wires, or swap ST3215_TX_PIN and "
                "ST3215_RX_PIN".format(first["rx_pin"], first["tx_pin"])
            )

        report["differences"] = differences
        report["diagnosis"] = (
            "Servo ID {} answered at {} baud (TX GPIO{}, RX GPIO{}).".format(
                first["id"], first["baud"], first["tx_pin"], first["rx_pin"]
            )
            + ("" if not differences else " " + "; ".join(differences) + ".")
        )

        return report

    report["ok"] = False
    report["matches_config"] = False
    report["differences"] = []

    if other_ids:
        report["result"] = "WRONG_ID"
        report["diagnosis"] = (
            "A servo answered, but as ID {} rather than the configured ID "
            "{}. Set ST3215_SERVO_ID to the ID that answered, or "
            "reprogram the servo.".format(
                ", ".join(str(value) for value in other_ids),
                config.ST3215_SERVO_ID,
            )
        )

        return report

    if any_checksum:
        report["result"] = "CORRUPT_TRAFFIC"
        report["diagnosis"] = (
            "Frames arrived but failed their checksum at every baud rate "
            "tried. That is signal integrity, not a servo fault: check "
            "the common ground between the ESP32 and the driver board "
            "first, then the routing and length of the data wire."
        )

        return report

    if any_echo:
        report["result"] = "ECHO_ONLY"
        report["diagnosis"] = (
            "The ESP32 hears its own transmission come back, so the TX "
            "path, the RX path and the adapter all work - the ESP32 side "
            "of the wiring is not the fault. Nothing on the bus replied, "
            "which leaves the servo: no power AT THE SERVO (a powered "
            "driver board is not the same thing), the servo lead not "
            "seated in the bus port, or an ID outside the range scanned. "
            "Sweep the full ID range next."
        )

        return report

    if any_bytes:
        report["result"] = "NOISE_ONLY"
        report["diagnosis"] = (
            "Bytes arrived but never formed a valid frame at any baud "
            "rate. Either something is transmitting at a rate that is "
            "not one of the eight the ST3215 supports, or the ground "
            "reference between the boards is missing."
        )

        return report

    report["result"] = "SILENT_BUS"
    report["diagnosis"] = (
        "Not one byte reached the ESP32, on either pin order, at any of "
        "the {} baud rates. Nothing on the bus is transmitting. In the "
        "order these are usually the cause: the servo has no power "
        "(measure at the servo connector, not at the supply), the "
        "three-wire servo lead is not seated in the driver board, the "
        "driver board's own logic supply is missing, or the RX wire from "
        "the driver board does not reach GPIO{}.".format(
            len(bauds), rx_pin
        )
    )

    return report


def bus_scan(uart_id=None, tx_pin=None, rx_pin=None, bauds=None,
             servo_ids=None, try_swapped_pins=True, timeout_ms=None):
    """
    Look for ANY servo on the bus, across baud rates and pin orders.

    MOVES NOTHING - only INST_PING is sent.

    Every combination is reported, including the ones that found nothing:
    "we heard nothing at 500000 baud either" is part of the answer. The
    UART is opened and closed around each combination, so the scan leaves
    nothing behind for the real backend to trip over.
    """
    from machine import UART, Pin

    uart_id = config.ST3215_UART_ID if uart_id is None else int(uart_id)
    tx_pin = config.ST3215_TX_PIN if tx_pin is None else int(tx_pin)
    rx_pin = config.ST3215_RX_PIN if rx_pin is None else int(rx_pin)
    timeout_ms = SCAN_TIMEOUT_MS if timeout_ms is None else int(timeout_ms)

    bauds = [int(value) for value in (bauds or SCAN_BAUDS)]

    if servo_ids is None:
        servo_ids = [config.ST3215_SERVO_ID]

    servo_ids = [int(value) for value in servo_ids]

    orders = [(tx_pin, rx_pin, "as configured")]

    if try_swapped_pins and tx_pin != rx_pin:
        orders.append((rx_pin, tx_pin, "TX and RX exchanged"))

    probes = []
    found = []
    budget = SCAN_MAX_PROBES

    for tx, rx, order_label in orders:
        for baud in bauds:
            uart = UART(
                uart_id, baudrate=baud, tx=Pin(tx), rx=Pin(rx),
                bits=8, parity=None, stop=1,
                timeout=timeout_ms, timeout_char=timeout_ms,
            )

            combination = {
                "tx_pin": tx, "rx_pin": rx, "pin_order": order_label,
                "baud": baud, "ids_answered": [], "other_ids": [],
                "bytes": 0, "echo": False, "checksum_errors": 0,
                "sample": "",
            }

            try:
                for servo_id in servo_ids:
                    if budget <= 0:
                        combination["truncated"] = True

                        break

                    budget -= 1

                    probe = _probe_id(uart, servo_id, timeout_ms)

                    combination["bytes"] += probe["bytes"]
                    combination["checksum_errors"] += probe[
                        "checksum_errors"
                    ]

                    if probe["echo"]:
                        combination["echo"] = True

                    if not combination["sample"] and probe["sample"]:
                        combination["sample"] = probe["sample"]

                    for other in probe["other_ids"]:
                        if other not in combination["other_ids"]:
                            combination["other_ids"].append(other)

                    if probe["answered"]:
                        combination["ids_answered"].append(servo_id)

                        found.append({
                            "id": servo_id,
                            "baud": baud,
                            "tx_pin": tx,
                            "rx_pin": rx,
                            "pin_order": order_label,
                            "status_byte": probe.get("status_byte"),
                            "status_flags": probe.get("status_flags") or [],
                        })

            finally:
                try:
                    uart.deinit()

                except AttributeError:
                    pass

                # Before the NEXT combination opens UART2. Without this
                # the swapped probe below leaves its TX pin attached,
                # and every later probe hears its own transmission come
                # back as a false echo. See release_uart_pins().
                release_uart_pins(tx, rx)

            probes.append(combination)

    return _scan_report(
        probes, found, uart_id, tx_pin, rx_pin, servo_ids, bauds
    )


# ======================================================================
# connection lifecycle
# ======================================================================

SERVO_TYPE = "st3215"
SERVO_LABEL = "Waveshare ST3215"
SERVO_DESCRIPTION = "Serial bus servo, encoder-based positioning"


def servo_info():
    """What is fitted, with enough text for an operator screen."""
    return {
        "type": SERVO_TYPE,
        "label": SERVO_LABEL,
        "description": SERVO_DESCRIPTION,
    }


class ServoLink:
    """
    Holds at most one live ST3215, and is the gate every movement passes.

    Everything that moves the carousel goes through the driver this
    object hands out, so "not connected" is enforced in one place
    rather than remembered in twenty. That gate is the whole reason
    this class exists with a single actuator: constructing an ST3215
    opens nothing and proves nothing, and a carousel that turns on an
    unproven link is a carousel turning with no idea where it is.

    It owns actuator lifecycle and no carousel geometry: it does not
    know what a slot is, and the carousel is attached to the driver by
    the caller.
    """

    def __init__(self):
        self.driver = None

        # Why the connection last changed. Reported in status so an
        # operator can see that a reconnect happened.
        self.last_change = None

        self.connection_count = 0

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------

    def is_connected(self):
        return self.driver is not None

    def require_driver(self):
        """
        The single gate every movement passes through.

        Raises with a code the PC turns into "run option 0 first"
        rather than into a mysterious hardware error.
        """
        if not self.is_connected():
            raise ServoNotConnectedError(
                "The ST3215 is not connected. Run option [0] Carousel "
                "Setup first."
            )

        return self.driver

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

        Any previous driver is taken down first, and it is taken down
        even if bringing the new one up then fails: a still-open UART
        would be worse than no servo at all.

        Returns a record describing the change. The CALLER is
        responsible for invalidating the carousel position - this
        object deliberately knows nothing about slots.
        """
        previous = None
        was_connected = self.is_connected()

        if self.driver is not None:
            previous = self._release_driver()

        servo = ST3215()

        try:
            initialization = servo.initialize()

        except ServoError as error:
            # Leave the link empty rather than holding a driver that
            # never came up. The operator sees the real reason.
            self.driver = None

            raise ServoError(
                "The ST3215 could not be initialized: {}".format(
                    error.message
                ),
                code=error.code,
            )

        self.driver = servo
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
            "initialization": initialization,
            "moved": False,
        }

    def _release_driver(self):
        """Take the current driver down, never raising on the way out."""
        if self.driver is None:
            return None

        try:
            result = self.driver.deinitialize()

        except Exception as error:
            result = {
                "servo": SERVO_TYPE,
                "released": False,
                "error": str(error),
            }

        self.driver = None

        return result

    def release(self, reason="released"):
        """Disconnect the actuator entirely, leaving the link empty."""
        was_connected = self.is_connected()
        previous = self._release_driver()

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
        the single most important thing an operator can be told about
        the carousel, and hiding it behind an error would be the wrong
        call.
        """
        report = {
            "connected": self.is_connected(),
            "type": SERVO_TYPE if self.is_connected() else None,
            "label": self.label(),
            "servo": servo_info(),
            "connection_count": self.connection_count,
            "last_change": self.last_change,
        }

        if not self.is_connected():
            report["message"] = (
                "The ST3215 is not connected. Run option [0] Carousel "
                "Setup."
            )

            return report

        report["description"] = SERVO_DESCRIPTION

        # The movement tests the driver offers, so the PC builds its
        # menu from the firmware rather than from a hardcoded list that
        # would drift the moment the driver gained a test.
        report["test_move_kinds"] = [
            {"kind": kind, "label": label}
            for kind, label in self.driver.test_move_kinds()
        ]

        try:
            report["backend"] = self.driver.status()

        except Exception as error:
            report["backend"] = {
                "servo": SERVO_TYPE,
                "error": {"code": "SERVO_ERROR", "message": str(error)},
            }

        return report

    def diagnostics(self):
        """Driver diagnostics, or a clear reason there are none."""
        servo = self.require_driver()

        report = servo.diagnostics()

        report["type"] = SERVO_TYPE
        report["label"] = SERVO_LABEL

        return report
