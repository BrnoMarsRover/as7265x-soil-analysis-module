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
FIRMWARE = REPO / "firmware"


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
# machine stub
# ----------------------------------------------------------------------

def install_machine(device):
    """Install a `machine` module backed by the given fake device."""
    machine = types.ModuleType("machine")

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

    class PWM:
        instances = []

        def __init__(self, pin, freq=None):
            self.pin = pin
            self.freq = freq
            self.duty = None
            self.active = True
            PWM.instances.append(self)

        def duty_ns(self, value):
            self.duty = value

        def deinit(self):
            self.active = False

    machine.Pin = Pin
    machine.I2C = I2C
    machine.PWM = PWM

    sys.modules["machine"] = machine

    return machine


def load_esp32(device):
    """Put firmware/ESP32 on the path with a working machine module."""
    patch_time()
    install_machine(device)

    path = str(FIRMWARE / "ESP32")

    if path not in sys.path:
        sys.path.insert(0, path)


def add_path(subdirectory):
    path = str(FIRMWARE / subdirectory)

    if path not in sys.path:
        sys.path.insert(0, path)

    return path
