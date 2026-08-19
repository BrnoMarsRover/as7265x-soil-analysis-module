"""
Shared test scaffolding.

Runs the REAL firmware modules on CPython. Nothing here reimplements
production logic: it supplies the two things MicroPython has and
CPython does not - the `machine` module and time.ticks_* - plus a fake
AS7265x that speaks the actual virtual-register protocol, so the driver
under test is exercised rather than replaced.
"""

import struct
import sys
import time
import types
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent

# The runtime layers sit under firmware/ (see Documentation/ARCHITECTURE.md).
# ESP32/ is a MicroPython package tree - drivers/, control/, protocol/ -
# imported with ESP32/ itself on the path. BD/ and Measurements/ are
# packages imported from the project root; PC/ is a pair of scripts.
ESP32_DIR = REPO / "ESP32"
PC_DIR = REPO / "PC"
BD_DIR = REPO / "BD"
MEASUREMENTS_DIR = REPO / "Measurements"

# Historical name. Used to point at firmware/, which ARCHITECTURE.md dissolved;
# the layer directories are now the repository root.
FIRMWARE = REPO


# ----------------------------------------------------------------------
# check counting
# ----------------------------------------------------------------------

class Checks:
    def __init__(self, title):
        self.title = title
        self.passed = 0
        self.failed = []

    def section(self, name):
        print()
        print("[{}]".format(name))

    def ok(self, condition, description):
        if condition:
            self.passed += 1
            print("  ok   {}".format(description))

        else:
            self.failed.append(description)
            print("  FAIL {}".format(description))

    def equal(self, actual, expected, description):
        if actual == expected:
            self.ok(True, description)

        else:
            self.ok(False, "{} (got {!r}, expected {!r})".format(
                description, actual, expected
            ))

    def close(self, actual, expected, description, tolerance=1e-9):
        near = (
            isinstance(actual, (int, float))
            and abs(actual - expected) <= tolerance
        )

        if near:
            self.ok(True, description)

        else:
            self.ok(False, "{} (got {!r}, expected {!r})".format(
                description, actual, expected
            ))

    def raises(self, exception_type, call, description):
        try:
            call()

        except exception_type:
            self.passed += 1
            print("  ok   {}".format(description))

            return

        except Exception as error:
            self.failed.append(description)
            print("  FAIL {} (raised {})".format(
                description, type(error).__name__
            ))

            return

        self.failed.append(description)
        print("  FAIL {} (nothing raised)".format(description))

    def report(self):
        print()
        print("=" * 60)

        if self.failed:
            print("{}: {} passed, {} FAILED".format(
                self.title, self.passed, len(self.failed)
            ))

            for description in self.failed:
                print("  - {}".format(description))

            return 1

        print("{}: all {} checks passed".format(self.title, self.passed))

        return 0


# ----------------------------------------------------------------------
# MicroPython time extensions
# ----------------------------------------------------------------------

def patch_time():
    """Add the MicroPython-only time functions to the stdlib module."""
    if not hasattr(time, "ticks_ms"):
        time.ticks_ms = lambda: int(time.monotonic() * 1000)

    if not hasattr(time, "ticks_diff"):
        time.ticks_diff = lambda a, b: a - b

    if not hasattr(time, "ticks_add"):
        time.ticks_add = lambda ticks, delta: ticks + delta

    if not hasattr(time, "sleep_ms"):
        time.sleep_ms = lambda ms: time.sleep(ms / 1000.0)


# ----------------------------------------------------------------------
# fake AS7265x
# ----------------------------------------------------------------------

ADDRESS = 0x49

STATUS_REG = 0x00
WRITE_REG = 0x01
READ_REG = 0x02
TX_VALID = 0x02
RX_VALID = 0x01

V_CONFIG = 0x04
V_INTEGRATION_TIME = 0x05
V_DEVICE_TEMP = 0x06
V_LED_CONFIG = 0x07
V_DEV_SELECT = 0x4F

CAL_BASES = (0x14, 0x18, 0x1C, 0x20, 0x24, 0x28)

# Device ids, as the driver uses them.
DEV_NIR, DEV_VISIBLE, DEV_UV = 0, 1, 2

# Channel letter -> (calibrated-value register, internal device). One
# register base serves three channels, one per device, which is why the
# 18 channels come out of only six addresses.
CHANNEL_ADDRESSES = {}

for _index, _base in enumerate(CAL_BASES):
    for _letters, _device in (
        ("ABCDEF", DEV_UV),
        ("GHIJKL", DEV_VISIBLE),
        ("RSTUVW", DEV_NIR),
    ):
        CHANNEL_ADDRESSES[_letters[_index]] = (_base, _device)


class FakeAS7265X:
    """
    Emulates the master's physical registers and the virtual-register
    window, closely enough that the real driver drives it.

    Configurable failure modes exist so the sensor lifecycle can be
    tested: a sensor that is absent for the first N bus scans is exactly
    the boot situation that used to become permanent runtime state.
    """

    def __init__(self, absent_scans=0, slaves_present=True,
                 accept_config=True, data_ready=True, other_devices=()):
        self.absent_scans = absent_scans
        self.slaves_present = slaves_present
        self.accept_config = accept_config
        self.data_ready_supported = data_ready
        self.other_devices = list(other_devices)

        self.scans = 0
        self.led_on = False
        self.led_on_count = 0

        # Reads of a calibrated-value register: the only evidence that
        # an acquisition actually touched the sensor.
        self.channel_reads = 0

        self.selected_device = 0
        self._rx_value = None
        self._pending_write = None

        self.vregs = {
            V_CONFIG: 0b00010000,      # power-on-ish: gain 1, mode 0
            V_INTEGRATION_TIME: 20,    # power-on-ish default
            V_DEV_SELECT: 0x30 if slaves_present else 0x00,
            V_DEVICE_TEMP: 27,
        }

        # LED_CONFIG is per internal device: each one drives its own
        # lamp, and DEV_SELECT_CONTROL chooses whose register is visible.
        self.led_config = {0: 0x00, 1: 0x00, 2: 0x00}

        # Lamp on/off history, so a test can assert that a lamp was
        # actually used and actually switched off again.
        self.lamp_on_counts = {0: 0, 1: 0, 2: 0}

        # How much of the stored spectrum each illumination returns.
        # A real detector sees almost nothing in the dark and different
        # amounts under each lamp; without that, White minus Dark is
        # zero and a calibration can never validate.
        self.illumination_gain = {
            "dark": 0.004,     # detector floor, no lamp
            "white": 1.0,      # device 0
            "ir": 0.62,        # device 1
            "uv": 0.38,        # device 2
        }

        self._device_to_lamp = {0: "white", 1: "ir", 2: "uv"}
        self._active_gain = self.illumination_gain["dark"]

        for base in CAL_BASES:
            for device in range(3):
                self._store_float(base, device, 0.0)

    # -- calibrated-value storage ------------------------------------

    @staticmethod
    def _key(base, device):
        return ("cal", base, device)

    def _store_float(self, base, device, value):
        self.vregs[self._key(base, device)] = struct.pack(">f", value)

    def load_spectrum(self, values):
        """values: {(base, device): float}"""
        for (base, device), value in values.items():
            self._store_float(base, device, value)

    def fill_spectrum(self, function):
        """function(base_index, device) -> float"""
        for index, base in enumerate(CAL_BASES):
            for device in range(3):
                self._store_float(base, device, function(index, device))

    def fill_from_channels(self, values):
        """
        Load a channel-letter spectrum, e.g. {"A": 12.3, ..., "W": 4.5}.

        Lets a test build a physically plausible sample from the real
        white reference instead of inventing counts that would produce
        impossible reflectance and be rejected by quality control.
        """
        for channel, (base, device) in CHANNEL_ADDRESSES.items():
            if channel in values:
                self._store_float(base, device, float(values[channel]))

    # -- bus ----------------------------------------------------------

    def scan(self):
        self.scans += 1

        if self.scans <= self.absent_scans:
            return list(self.other_devices)

        return list(self.other_devices) + [ADDRESS]

    def writeto(self, address, data):
        if address != ADDRESS:
            raise OSError("no device at 0x{:02X}".format(address))

        if len(data) == 1:
            self._selected_register = data[0]

            return

        register, value = data[0], data[1]
        self._selected_register = register

        if register == WRITE_REG:
            self._handle_virtual(value)

    def readfrom(self, address, count):
        if address != ADDRESS:
            raise OSError("no device at 0x{:02X}".format(address))

        register = getattr(self, "_selected_register", STATUS_REG)

        if register == STATUS_REG:
            status = 0

            if self._rx_value is not None:
                status |= RX_VALID

            return bytes([status])

        if register == READ_REG:
            value = self._rx_value or 0
            self._rx_value = None

            return bytes([value])

        return bytes([0])

    # -- virtual register state machine -------------------------------

    def _handle_virtual(self, value):
        if self._pending_write is not None:
            register = self._pending_write
            self._pending_write = None
            self._write_virtual(register, value)

            return

        if value & 0x80:
            self._pending_write = value & 0x7F

            return

        self._rx_value = self._read_virtual(value & 0x7F)

    def _read_virtual(self, register):
        for base in CAL_BASES:
            if base <= register < base + 4:
                self.channel_reads += 1

                value = struct.unpack(
                    ">f", self.vregs[self._key(base, self.selected_device)]
                )[0]

                packed = struct.pack(">f", value * self._active_gain)

                return packed[register - base]

        if register == V_LED_CONFIG:
            return self.led_config[self.selected_device]

        return self.vregs.get(register, 0)

    def _lamp_gain(self):
        """Gain of whichever lamp is on when a conversion is triggered."""
        for device, state in self.led_config.items():
            if state & (1 << 3):
                return self.illumination_gain[self._device_to_lamp[device]]

        return self.illumination_gain["dark"]

    def _write_virtual(self, register, value):
        if register == V_DEV_SELECT:
            self.selected_device = value & 0x03
            self.vregs[V_DEV_SELECT] = (
                (0x30 if self.slaves_present else 0x00) | (value & 0x03)
            )

            return

        if not self.accept_config and register in (
            V_CONFIG, V_INTEGRATION_TIME
        ):
            # Silently ignores writes: exactly the failure the read-back
            # in apply_configuration() exists to catch.
            return

        if register == V_LED_CONFIG:
            device = self.selected_device
            was_on = bool(self.led_config[device] & (1 << 3))

            self.led_config[device] = value & 0xFF

            now_on = bool(value & (1 << 3))

            if now_on and not was_on:
                self.lamp_on_counts[device] += 1

            # Historical single-lamp counters, kept for older checks.
            self.led_on = any(
                bool(state & (1 << 3)) for state in self.led_config.values()
            )
            self.led_on_count = sum(self.lamp_on_counts.values())

            return

        if register == V_CONFIG:
            # Writing CONFIG is what arms a one-shot conversion, so this
            # is the moment the illumination is sampled.
            self._active_gain = self._lamp_gain()

            if self.data_ready_supported:
                value |= 1 << 1
            else:
                value &= ~(1 << 1)

        self.vregs[register] = value & 0xFF

    def any_lamp_on(self):
        return any(
            bool(state & (1 << 3)) for state in self.led_config.values()
        )


# ----------------------------------------------------------------------
# fake ST3215 serial bus servo
# ----------------------------------------------------------------------

# Register addresses, taken from the same official memory table the
# driver uses. Duplicated here on purpose: if the driver's constants ever
# drift, these tests must fail rather than follow along.
SERVO_REG_ID = 5
SERVO_REG_BAUD = 6
SERVO_REG_MIN_ANGLE = 9
SERVO_REG_MAX_ANGLE = 11
SERVO_REG_MODE = 33
SERVO_REG_TORQUE = 40
SERVO_REG_ACC = 41
SERVO_REG_GOAL = 42
SERVO_REG_SPEED = 46
SERVO_REG_LOCK = 55
SERVO_REG_POSITION = 56
SERVO_REG_PRESENT_SPEED = 58
SERVO_REG_LOAD = 60
SERVO_REG_VOLTAGE = 62
SERVO_REG_TEMPERATURE = 63
SERVO_REG_STATUS = 65
SERVO_REG_MOVING = 66
SERVO_REG_CURRENT = 69

SERVO_INST_PING = 0x01
SERVO_INST_READ = 0x02
SERVO_INST_WRITE = 0x03

SERVO_COUNTS_PER_REV = 4096

SERVO_MODE_POSITION = 0
SERVO_MODE_STEP = 3


def servo_checksum(values):
    """The protocol checksum, written out independently of the driver."""
    total = 0

    for value in values:
        total += value

    return (~total) & 0xFF


def servo_encode_signed(value):
    """Sign-magnitude 16 bit, as the memory table specifies."""
    value = int(value)

    if value < 0:
        return (-value) | 0x8000

    return value & 0xFFFF


def servo_decode_signed(value, sign_bit=15):
    mask = 1 << sign_bit

    if value & mask:
        return -(value & ~mask)

    return value


class FakeST3215:
    """
    A simulated ST3215 that speaks the real frame protocol.

    It is deliberately a protocol-level fake rather than a stubbed
    driver: the firmware builds real packets, this parses them, and the
    replies go back through the real response parser. Checksums, frame
    lengths, servo IDs, sign-magnitude encoding and the register map are
    all exercised on the way.

    The failure modes are the ones that actually happen on a bus:

        silent            nothing answers - external supply off, wrong
                          pins, no common ground
        corrupt_checksum  a frame arrives but the checksum is wrong
        answer_as         another servo ID answers, i.e. an ID clash
        short_by          the mechanism stops short of the target
        polls_to_finish   how many polls the movement takes; None means
                          it never arrives and the driver must time out
    """

    def __init__(self, position=2048, mode=SERVO_MODE_STEP, torque=1,
                 servo_id=1, silent=False, corrupt_checksum=False,
                 answer_as=None, short_by=0, polls_to_finish=1,
                 status=0, min_angle=0, max_angle=0,
                 drop_goal_ack=False, drop_replies=0):
        self.servo_id = servo_id
        self.silent = silent
        self.corrupt_checksum = corrupt_checksum
        self.answer_as = answer_as
        self.short_by = short_by
        self.polls_to_finish = polls_to_finish

        # The servo acts on a command but its acknowledgement is lost. For
        # a RELATIVE goal position this is the dangerous case: a driver
        # that retried would move the carousel twice.
        self.drop_goal_ack = drop_goal_ack

        # Swallow the first N replies, so an ordinary transport glitch can
        # be told apart from a dead bus.
        self.drop_replies = drop_replies

        self.registers = bytearray(128)
        self.tx = bytearray()

        # Everything the firmware sent, so a test can assert on the
        # actual bytes on the wire.
        self.packets = []
        self.writes = []
        self.goals = []

        self.registers[SERVO_REG_ID] = servo_id
        self.registers[SERVO_REG_BAUD] = 0          # 1 Mbps
        self.registers[SERVO_REG_MODE] = mode
        self.registers[SERVO_REG_TORQUE] = torque
        self.registers[SERVO_REG_LOCK] = 1
        self.registers[SERVO_REG_VOLTAGE] = 74      # 7.4 V
        self.registers[SERVO_REG_TEMPERATURE] = 32
        self.registers[SERVO_REG_STATUS] = status
        self.registers[SERVO_REG_MOVING] = 0

        self._set_word(SERVO_REG_MIN_ANGLE, min_angle)
        self._set_word(SERVO_REG_MAX_ANGLE, max_angle)
        self._set_word(SERVO_REG_LOAD, 0)
        self._set_word(SERVO_REG_CURRENT, 0)
        self._set_word(SERVO_REG_PRESENT_SPEED, 0)

        self.position = position

        self._pending = None

    # -- register helpers ---------------------------------------------

    def _set_word(self, address, value):
        encoded = servo_encode_signed(value)

        self.registers[address] = encoded & 0xFF
        self.registers[address + 1] = (encoded >> 8) & 0xFF

    def _get_word(self, address):
        return self.registers[address] | (self.registers[address + 1] << 8)

    @property
    def position(self):
        return servo_decode_signed(self._get_word(SERVO_REG_POSITION))

    @position.setter
    def position(self, value):
        self._set_word(SERVO_REG_POSITION, value)

    @property
    def mode(self):
        return self.registers[SERVO_REG_MODE]

    @property
    def torque(self):
        return self.registers[SERVO_REG_TORQUE]

    # -- movement -----------------------------------------------------

    def _start_move(self, goal):
        """A goal position was written; work out where it ends up."""
        self.goals.append(goal)

        if self.mode == SERVO_MODE_STEP:
            target = self.position + goal

        else:
            target = goal

        target -= self.short_by

        if self.polls_to_finish is None:
            # Never arrives. The driver has to time out and say so.
            self.registers[SERVO_REG_MOVING] = 1
            self._pending = None

            return

        self.registers[SERVO_REG_MOVING] = 1
        self._pending = [target, max(1, int(self.polls_to_finish))]

    def _advance(self):
        if self._pending is None:
            return

        self._pending[1] -= 1

        if self._pending[1] <= 0:
            self.position = self._pending[0] % SERVO_COUNTS_PER_REV
            self.registers[SERVO_REG_MOVING] = 0
            self._pending = None

    # -- transport ----------------------------------------------------

    def any(self):
        return len(self.tx)

    def read(self, count=None):
        if not self.tx:
            return None

        if count is None:
            count = len(self.tx)

        chunk = bytes(self.tx[:count])
        del self.tx[:count]

        return chunk

    def write(self, data):
        data = bytes(data)

        self.packets.append(data)
        self._handle(data)

        return len(data)

    def _reply(self, payload=b""):
        if self.silent:
            return

        if self.drop_replies > 0:
            self.drop_replies -= 1

            return

        servo_id = (
            self.servo_id if self.answer_as is None else self.answer_as
        )
        length = len(payload) + 2
        error = self.registers[SERVO_REG_STATUS]

        body = bytes([servo_id, length, error]) + payload
        checksum = servo_checksum(body)

        if self.corrupt_checksum:
            checksum = (checksum + 1) & 0xFF

        self.tx.extend(b"\xff\xff" + body + bytes([checksum]))

    def _handle(self, packet):
        if len(packet) < 6:
            return

        if packet[0] != 0xFF or packet[1] != 0xFF:
            return

        servo_id, length, instruction = packet[2], packet[3], packet[4]
        params = packet[5:-1]

        expected = servo_checksum(packet[2:-1])

        if packet[-1] != expected:
            # A real servo would ignore this. So does the fake, which is
            # what makes a firmware-side checksum bug show up as silence.
            return

        if servo_id != self.servo_id:
            return

        if instruction == SERVO_INST_PING:
            self._reply()

            return

        if instruction == SERVO_INST_READ:
            address, count = params[0], params[1]

            covers_moving = address <= SERVO_REG_MOVING < address + count

            # A real servo reports the state at the instant of the read,
            # and it reports itself as MOVING for at least one poll after
            # the goal is written. So the reply is built first and the
            # movement advances afterwards: the first poll of a movement
            # sees moving=1 and the old position, exactly as it would on
            # hardware. A driver that trusted a single reading, or that
            # believed a stopped flag it had never seen set, gets this
            # wrong.
            payload = bytes(self.registers[address:address + count])

            if covers_moving:
                self._advance()

            self._reply(payload)

            return

        if instruction == SERVO_INST_WRITE:
            address = params[0]
            data = params[1:]

            self.writes.append((address, bytes(data)))

            for offset, value in enumerate(data):
                self.registers[address + offset] = value

            # Writing the goal block is what starts a movement. The
            # firmware writes acceleration, goal position, goal time and
            # goal speed in one transaction, exactly as the reference
            # library does.
            goal_write = False

            if address == SERVO_REG_ACC and len(data) >= 3:
                goal = servo_decode_signed(data[1] | (data[2] << 8))
                self._start_move(goal)
                goal_write = True

            elif address == SERVO_REG_GOAL and len(data) >= 2:
                goal = servo_decode_signed(data[0] | (data[1] << 8))
                self._start_move(goal)
                goal_write = True

            if goal_write and self.drop_goal_ack:
                # Acted on, but the acknowledgement never arrives.
                return

            self._reply()

            return


# ----------------------------------------------------------------------
# machine stub
# ----------------------------------------------------------------------

def install_machine(device, servo=None):
    """
    Install a `machine` module backed by the given fake devices.

    I2C for the AS7265x and UART for the ST3215. Each records what the
    firmware did to it, which is how a test asserts on packets without a
    servo on the bench.

    There is deliberately no PWM peripheral. Nothing in the firmware can
    create one since the MG995 was removed, and a stub for a peripheral
    that cannot be reached only invites assertions that pass for the
    wrong reason.
    """
    machine = types.ModuleType("machine")

    if servo is None:
        servo = FakeST3215()

    class Pin:
        def __init__(self, number, *args, **kwargs):
            self.number = number

    class I2C:
        def __init__(self, bus, scl=None, sda=None, freq=None):
            self.bus = bus
            self.scl = scl
            self.sda = sda
            self.freq = freq
            self.device = device

        def scan(self):
            return self.device.scan()

        def writeto(self, address, data):
            return self.device.writeto(address, data)

        def readfrom(self, address, count):
            return self.device.readfrom(address, count)

    class UART:
        instances = []

        def __init__(self, uart_id, baudrate=None, tx=None, rx=None,
                     bits=8, parity=None, stop=1, timeout=0,
                     timeout_char=0):
            self.uart_id = uart_id
            self.baudrate = baudrate
            self.tx = tx
            self.rx = rx
            self.bits = bits
            self.parity = parity
            self.stop = stop
            self.timeout = timeout
            self.active = True
            self.device = servo

            # A fake that cares which settings it is being talked to at -
            # the bus scan reopens the UART at eight baud rates and in
            # both pin orders, and a servo only answers at one of them.
            configure = getattr(servo, "configure_link", None)

            if configure is not None:
                configure(uart_id, baudrate, tx, rx)

            UART.instances.append(self)

        def write(self, data):
            return self.device.write(data)

        def read(self, count=None):
            return self.device.read(count)

        def any(self):
            return self.device.any()

        def deinit(self):
            self.active = False

    machine.Pin = Pin
    machine.I2C = I2C
    machine.UART = UART

    sys.modules["machine"] = machine

    return machine


# Every module name the ESP32 firmware tree can occupy. Reloading the
# firmware means clearing all of them: a stale `control.carousel` holding a
# reference to the previous `config` is how a test ends up asserting
# against the wrong constants.
ESP32_TOP_LEVEL = ("config", "main", "boot")
ESP32_PACKAGES = ("drivers", "control", "protocol")


def purge_esp32_modules():
    """Drop every cached ESP32 firmware module, packages included."""
    for name in list(sys.modules):
        if name in ESP32_TOP_LEVEL:
            del sys.modules[name]

            continue

        for package in ESP32_PACKAGES:
            if name == package or name.startswith(package + "."):
                del sys.modules[name]

                break


def load_esp32(device, servo=None):
    """
    Put ESP32/ on the path with a working machine module.

    Returns the fake servo the firmware will talk to, so a test can watch
    the bytes on the wire and inject faults.
    """
    patch_time()

    if servo is None:
        servo = FakeST3215()

    install_machine(device, servo)

    path = str(ESP32_DIR)

    if path not in sys.path:
        sys.path.insert(0, path)

    return servo


def shrink_timings(config):
    """
    Take the real hardware settling delays out of a test run.

    Only DURATIONS are touched. Every angle, count, pulse width and slot
    figure is left exactly as shipped, because those are what the tests
    are actually checking.
    """
    config.STARTUP_DELAY_SECONDS = 0
    config.SENSOR_INIT_RETRY_MS = 0
    config.I2C_SCAN_RETRY_MS = 0
    config.ILLUMINATION_SETTLE_MS = 0
    config.SCAN_SETTLE_TIME = 0.0
    config.HOME_SETTLE_TIME = 0.0

    # ST3215: the encoder counts and the tolerance stay as shipped.
    config.ST3215_SETTLE_MS = 0
    config.ST3215_POLL_INTERVAL_MS = 0
    config.ST3215_TIMEOUT_MS = 5
    config.ST3215_RETRY_DELAY_MS = 0
    config.ST3215_MOVE_TIMEOUT_BASE_MS = 20
    config.ST3215_MOVE_TIMEOUT_MARGIN = 0.0
    config.ST3215_MOVE_TIMEOUT_MS = 50
    config.ST3215_STOP_PAUSE_MS = 0

    return config


def build_firmware(device=None, servo=None, boot=True):
    """
    A freshly loaded firmware instance, wired to fake hardware.

    Returns (main_module, HardwareModule, config, fake_servo). The
    firmware tree is purged and re-imported every time, so one test's
    config override cannot leak into the next - and so the exception
    classes a test catches belong to the instance under test.

    NO SERVO IS SELECTED on return. That is the real boot state: the
    caller selects one, exactly as the operator does.
    """
    if device is None:
        device = FakeAS7265X()

    fake_servo = load_esp32(device, servo)
    purge_esp32_modules()

    import config

    shrink_timings(config)

    import main

    module = main.HardwareModule()

    if boot:
        module.boot()

    # Handles for the test, not for the firmware.
    module.fake_servo = fake_servo
    module.fake_sensor = device

    return main, module, config, fake_servo


def command(module, cmd, **payload):
    """Round trip one command through the real dispatcher."""
    request = {"request_id": "1", "cmd": cmd}
    request.update(payload)

    return module.dispatch_command(request)


def add_project_root():
    """
    Put the project root on the path.

    This is how BD and Measurements are reached: they are packages, so
    `from BD.samples import SampleStore` resolves from the
    root. It replaces the old add_path("BD"), which put BD's *contents*
    on the path as top-level modules and made `import config` ambiguous
    between two different configuration files.
    """
    path = str(REPO)

    if path not in sys.path:
        sys.path.insert(0, path)

    return path


def add_path(subdirectory):
    """
    Put one layer directory on the path.

    Still correct for PC/, whose modules are flat scripts. For BD and
    Measurements use add_project_root() and import them as packages.
    """
    path = str(REPO / subdirectory)

    if path not in sys.path:
        sys.path.insert(0, path)

    return path
