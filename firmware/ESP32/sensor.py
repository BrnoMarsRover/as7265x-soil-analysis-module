# sensor.py
#
# AS7265x triad spectrometer: the driver and the one authoritative
# sensor runtime, in one file because they are one responsibility -
# "what the instrument reads".
#
# There is exactly ONE sensor world. Earlier firmware had two - a
# runtime driver created at boot and a separate "strict" diagnostic
# driver built on its own I2C object. The diagnostic one could read all
# 18 channels while the runtime one stayed None from a single failed
# boot attempt, so the module reported SENSOR_NOT_INITIALIZED while the
# hardware was demonstrably working. SensorRuntime below replaces both.
#
# Every I2C failure raises SensorError. Nothing returns 0 to paper over
# a timeout: a genuine dark reading and a dead bus must never look the
# same.
#
# Protocol note: the AS7265x master at 0x49 exposes its own registers
# plus a virtual-register window (WRITE_REG/READ_REG guarded by
# STATUS_REG) through which the VIS and NIR slaves are reached.
#
# Nothing here interprets a spectrum. Dark correction, normalization,
# quality and material comparison are Science's, on the PC.

import struct
import time

from machine import I2C, Pin

import config


ADDRESS = config.AS7265X_ADDRESS

# Physical registers on the master.
STATUS_REG = 0x00
WRITE_REG = 0x01
READ_REG = 0x02

TX_VALID = 0x02
RX_VALID = 0x01

# Virtual registers.
V_DEVICE_TYPE = 0x00
V_HW_VERSION = 0x01
V_CONFIG = 0x04
V_INTEGRATION_TIME = 0x05
V_DEVICE_TEMP = 0x06
V_LED_CONFIG = 0x07
V_DEV_SELECT_CONTROL = 0x4F

# Internal devices selected through V_DEV_SELECT_CONTROL.
DEV_NIR = 0x00
DEV_VISIBLE = 0x01
DEV_UV = 0x02

# Bits 4 and 5 of DEV_SELECT_CONTROL report the two slaves.
SLAVE_PRESENT_MASK = 0b00110000

# Calibrated-value register bases. Each holds one big-endian float per
# device, so one base address yields three different channels.
R_G_A_CAL = 0x14
S_H_B_CAL = 0x18
T_I_C_CAL = 0x1C
U_J_D_CAL = 0x20
V_K_E_CAL = 0x24
W_L_F_CAL = 0x28

GAIN_1X = 0b00
GAIN_3_7X = 0b01
GAIN_16X = 0b10
GAIN_64X = 0b11

# Illumination lamps. Each of the three internal devices drives one
# bulb, and DEV_SELECT_CONTROL chooses whose LED_CONFIG register is
# visible - so the lamp is addressed by the device that owns it, not by
# a separate LED index.
#
#   WHITE lamp -> device 0x00
#   IR lamp    -> device 0x01
#   UV lamp    -> device 0x02
LED_WHITE = 0x00
LED_IR = 0x01
LED_UV = 0x02

LAMPS = (LED_WHITE, LED_IR, LED_UV)

LAMP_NAMES = {LED_WHITE: "white", LED_IR: "ir", LED_UV: "uv"}
LAMP_BY_NAME = {"white": LED_WHITE, "ir": LED_IR, "uv": LED_UV}

# Per-lamp current, read from config at call time so a runtime change
# is picked up without rebuilding the driver.
LAMP_CURRENTS = {
    LED_WHITE: lambda: config.WHITE_LED_CURRENT,
    LED_IR: lambda: config.IR_LED_CURRENT,
    LED_UV: lambda: config.UV_LED_CURRENT,
}

# LED_CONFIG bit layout. Only these two fields belong to the bulb; the
# indicator LED shares the register and must survive every write.
LED_DRV_ENABLE_BIT = 1 << 3
LED_DRV_CURRENT_MASK = 0b00110000
LED_DRV_CURRENT_SHIFT = 4

# The 18 calibrated channels, in wavelength order:
#   channel, nanometres, internal device name, register base, device id
CHANNEL_LAYOUT = (
    ("A", 410, "UV", R_G_A_CAL, DEV_UV),
    ("B", 435, "UV", S_H_B_CAL, DEV_UV),
    ("C", 460, "UV", T_I_C_CAL, DEV_UV),
    ("D", 485, "UV", U_J_D_CAL, DEV_UV),
    ("E", 510, "UV", V_K_E_CAL, DEV_UV),
    ("F", 535, "UV", W_L_F_CAL, DEV_UV),
    ("G", 560, "VISIBLE", R_G_A_CAL, DEV_VISIBLE),
    ("H", 585, "VISIBLE", S_H_B_CAL, DEV_VISIBLE),
    ("I", 610, "VISIBLE", T_I_C_CAL, DEV_VISIBLE),
    ("J", 645, "VISIBLE", U_J_D_CAL, DEV_VISIBLE),
    ("K", 680, "VISIBLE", V_K_E_CAL, DEV_VISIBLE),
    ("L", 705, "VISIBLE", W_L_F_CAL, DEV_VISIBLE),
    ("R", 730, "NIR", R_G_A_CAL, DEV_NIR),
    ("S", 760, "NIR", S_H_B_CAL, DEV_NIR),
    ("T", 810, "NIR", T_I_C_CAL, DEV_NIR),
    ("U", 860, "NIR", U_J_D_CAL, DEV_NIR),
    ("V", 900, "NIR", V_K_E_CAL, DEV_NIR),
    ("W", 940, "NIR", W_L_F_CAL, DEV_NIR),
)

CHANNELS = tuple(entry[0] for entry in CHANNEL_LAYOUT)


class SensorError(Exception):
    """
    Sensor failure with a machine-readable code and the stage it hit.

    super().__init__ - MicroPython has no unbound Exception.__init__,
    and calling it raises "type object 'Exception' has no attribute
    '__init__'", which used to mask every sensor fault as an internal
    error.
    """

    def __init__(self, code, message, stage=None, details=None):
        super().__init__(message)

        self.code = str(code)
        self.message = str(message)
        self.stage = str(stage) if stage is not None else "UNKNOWN"
        self.details = details if details is not None else {}

    def as_dict(self):
        """JSON-safe error payload. Never serializes the exception."""
        return {
            "code": self.code,
            "message": self.message,
            "stage": self.stage,
            "details": self.details,
        }


def error_payload(error):
    """JSON-safe description of any exception, SensorError or not."""
    if isinstance(error, SensorError):
        return error.as_dict()

    return {
        "code": "INTERNAL_ERROR",
        "message": str(error),
        "stage": "UNKNOWN",
        "details": {
            "exception_type": type(error).__name__,
            "exception_message": str(error),
        },
    }


class AS7265X:
    """Low-level driver. Every transaction either works or raises."""

    def __init__(self, i2c, address=ADDRESS):
        self.i2c = i2c
        self.address = address
        self.timeout_ms = config.VIRTUAL_REGISTER_TIMEOUT_MS

        # Set by the caller around a multi-transaction operation. None
        # means "no overall budget", which is right for a single read
        # and wrong for a bring-up. See _wait_status.
        self.deadline = None

    def budget(self, milliseconds=None):
        """
        Start an overall time budget for the operation about to run.

        Returns the deadline so a caller can hand the same one to a
        later stage, or None to clear it.
        """
        if milliseconds is None:
            self.deadline = None

            return None

        self.deadline = time.ticks_add(time.ticks_ms(), int(milliseconds))

        return self.deadline

    # ------------------------------------------------------------------
    # physical registers
    # ------------------------------------------------------------------

    def read_register(self, register):
        try:
            self.i2c.writeto(self.address, bytes([register]))
            data = self.i2c.readfrom(self.address, 1)

        except OSError as error:
            raise SensorError(
                "I2C_READ_FAILED",
                "I2C read of register 0x{:02X} failed: {}".format(
                    register, error
                ),
                stage="PHYSICAL_REGISTER",
                details={
                    "register": "0x{:02X}".format(register),
                    "exception_type": type(error).__name__,
                },
            )

        if not data:
            raise SensorError(
                "PHYSICAL_REGISTER_READ_FAILED",
                "Register 0x{:02X} returned no data.".format(register),
                stage="PHYSICAL_REGISTER",
                details={"register": "0x{:02X}".format(register)},
            )

        return data[0]

    def write_register(self, register, value):
        try:
            self.i2c.writeto(self.address, bytes([register, value & 0xFF]))

        except OSError as error:
            raise SensorError(
                "I2C_WRITE_FAILED",
                "I2C write to register 0x{:02X} failed: {}".format(
                    register, error
                ),
                stage="PHYSICAL_REGISTER",
                details={
                    "register": "0x{:02X}".format(register),
                    "exception_type": type(error).__name__,
                },
            )

    # ------------------------------------------------------------------
    # virtual registers
    # ------------------------------------------------------------------

    def _wait_status(self, mask, want_set, virtual_register, code):
        """
        Poll STATUS_REG until the bit matches, or give up in bounded time.

        TWO bounds, not one. `timeout_ms` bounds THIS wait; `deadline`
        bounds the whole operation the caller is running.

        One wait being bounded does not bound their sum, and that is
        not a theoretical distinction: bringing the sensor up runs on
        the order of twenty virtual-register transactions, each with
        two of these waits, and a sensor that answers on the I2C bus
        but never sets the ready bit makes every one of them run to
        full length. Four initialization attempts of that took the
        firmware past three minutes with no reply - long enough for the
        PC to time out and call the whole board dead.
        """
        started = time.ticks_ms()

        while True:
            status = self.read_register(STATUS_REG)
            is_set = (status & mask) != 0

            if is_set == want_set:
                return status

            elapsed = time.ticks_diff(time.ticks_ms(), started)

            if self.deadline is not None and time.ticks_diff(
                self.deadline, time.ticks_ms()
            ) <= 0:
                raise SensorError(
                    "SENSOR_INIT_BUDGET_EXPIRED",
                    "The sensor operation ran out of its overall time "
                    "budget while waiting on virtual register "
                    "0x{:02X}.".format(virtual_register),
                    stage="VIRTUAL_REGISTER",
                    details={
                        "virtual_register": "0x{:02X}".format(
                            virtual_register
                        ),
                        "elapsed_ms": elapsed,
                        "last_status": "0x{:02X}".format(status),
                        "budget_ms": config.SENSOR_OPERATION_BUDGET_MS,
                    },
                )

            if elapsed > self.timeout_ms:
                raise SensorError(
                    code,
                    "Timed out after {} ms waiting on STATUS_REG for "
                    "virtual register 0x{:02X}.".format(
                        elapsed, virtual_register
                    ),
                    stage="VIRTUAL_REGISTER",
                    details={
                        "virtual_register": "0x{:02X}".format(
                            virtual_register
                        ),
                        "elapsed_ms": elapsed,
                        "last_status": "0x{:02X}".format(status),
                    },
                )

            time.sleep_ms(config.POLLING_DELAY_MS)

    def virtual_read(self, virtual_register):
        status = self.read_register(STATUS_REG)

        # Drain a stale byte, otherwise the next read returns the
        # previous transaction's result.
        if (status & RX_VALID) != 0:
            self.read_register(READ_REG)

        self._wait_status(
            TX_VALID, False, virtual_register, "VIRTUAL_TX_TIMEOUT"
        )
        self.write_register(WRITE_REG, virtual_register & 0x7F)

        self._wait_status(
            RX_VALID, True, virtual_register, "VIRTUAL_RX_TIMEOUT"
        )

        return self.read_register(READ_REG)

    def virtual_write(self, virtual_register, value):
        self._wait_status(
            TX_VALID, False, virtual_register, "VIRTUAL_TX_TIMEOUT"
        )
        self.write_register(WRITE_REG, (virtual_register | 0x80) & 0xFF)

        self._wait_status(
            TX_VALID, False, virtual_register, "VIRTUAL_TX_TIMEOUT"
        )
        self.write_register(WRITE_REG, value & 0xFF)

    # ------------------------------------------------------------------
    # device selection and identity
    # ------------------------------------------------------------------

    def select_device(self, device):
        self.virtual_write(V_DEV_SELECT_CONTROL, device & 0x03)

    def read_devices(self):
        """DEV_SELECT_CONTROL byte plus decoded slave presence."""
        value = self.virtual_read(V_DEV_SELECT_CONTROL)

        return {
            "dev_select_control": "0x{:02X}".format(value),
            "visible_device": bool(value & 0b00010000),
            "nir_device": bool(value & 0b00100000),
            "slaves_detected": (value & SLAVE_PRESENT_MASK) != 0,
        }

    def require_devices(self):
        """Both internal slaves must answer, or there is no spectrometer."""
        devices = self.read_devices()

        if not devices["slaves_detected"]:
            raise SensorError(
                "AS7265X_SLAVES_NOT_DETECTED",
                "The master answers on 0x{:02X}, but the VIS/NIR slave "
                "devices were not detected. This points at the sensor "
                "board itself rather than the I2C wiring.".format(
                    self.address
                ),
                stage="INTERNAL_DEVICES",
                details=devices,
            )

        return devices

    # ------------------------------------------------------------------
    # configuration
    # ------------------------------------------------------------------

    def read_configuration(self):
        """What the silicon is ACTUALLY set to, read back from registers."""
        config_byte = self.virtual_read(V_CONFIG)
        integration = self.virtual_read(V_INTEGRATION_TIME)

        gain = (config_byte >> 4) & 0b11

        return {
            "config_register": "0x{:02X}".format(config_byte),
            "gain": gain,
            "gain_x": config.SENSOR_GAIN_NAMES.get(gain, "unknown"),
            "measurement_mode": (config_byte >> 2) & 0b11,
            "data_ready": (config_byte & (1 << 1)) != 0,
            "integration_cycles": integration,
        }

    def set_gain(self, gain):
        value = self.virtual_read(V_CONFIG)
        value &= 0b11001111
        value |= (gain & 0b11) << 4
        self.virtual_write(V_CONFIG, value)

    def set_measurement_mode(self, mode):
        value = self.virtual_read(V_CONFIG)
        value &= 0b11110011
        value |= (mode & 0b11) << 2
        self.virtual_write(V_CONFIG, value)

    def set_integration_cycles(self, cycles):
        self.virtual_write(V_INTEGRATION_TIME, cycles & 0xFF)

    # ------------------------------------------------------------------
    # illumination
    #
    # Three separate lamps, each addressed through the device that owns
    # it. Every write is read-modify-write so the indicator LED bits,
    # which live in the same register, are preserved.
    # ------------------------------------------------------------------

    def set_bulb_current(self, device, current):
        self.select_device(device)

        value = self.virtual_read(V_LED_CONFIG)
        value &= ~LED_DRV_CURRENT_MASK & 0xFF
        value |= (min(current, 0b11) & 0b11) << LED_DRV_CURRENT_SHIFT

        self.virtual_write(V_LED_CONFIG, value)

    def read_bulb_current(self, device):
        self.select_device(device)

        value = self.virtual_read(V_LED_CONFIG)

        return (value & LED_DRV_CURRENT_MASK) >> LED_DRV_CURRENT_SHIFT

    def enable_bulb(self, device):
        self.select_device(device)

        value = self.virtual_read(V_LED_CONFIG)
        value |= LED_DRV_ENABLE_BIT

        self.virtual_write(V_LED_CONFIG, value & 0xFF)

    def disable_bulb(self, device):
        self.select_device(device)

        value = self.virtual_read(V_LED_CONFIG)
        value &= ~LED_DRV_ENABLE_BIT & 0xFF

        self.virtual_write(V_LED_CONFIG, value)

    def bulb_enabled(self, device):
        self.select_device(device)

        return (self.virtual_read(V_LED_CONFIG) & LED_DRV_ENABLE_BIT) != 0

    def disable_all_bulbs(self):
        """
        Every lamp off, unconditionally.

        Called before selecting an illumination and again from the
        finally block of every acquisition. Two lamps on at once would
        silently produce a spectrum nothing can interpret.
        """
        for device in LAMPS:
            self.disable_bulb(device)

    def bulb_states(self):
        """Which lamps are currently on - read back, not assumed."""
        return {
            LAMP_NAMES[device]: self.bulb_enabled(device)
            for device in LAMPS
        }

    def apply_bulb_currents(self):
        """Push the configured current into all three lamps."""
        self.set_bulb_current(LED_WHITE, config.WHITE_LED_CURRENT)
        self.set_bulb_current(LED_IR, config.IR_LED_CURRENT)
        self.set_bulb_current(LED_UV, config.UV_LED_CURRENT)

    def read_bulb_currents(self):
        return {
            LAMP_NAMES[device]: self.read_bulb_current(device)
            for device in LAMPS
        }

    def apply_configuration(self):
        """
        Write the config.py acquisition settings, then READ THEM BACK.

        The read-back is the point. Firmware that only reported its
        intended settings claimed 100 cycles / 16x while the sensor was
        still running its power-on defaults, because nothing ever
        verified that the writes landed.
        """
        self.disable_all_bulbs()
        self.apply_bulb_currents()

        self.set_integration_cycles(config.SENSOR_INTEGRATION_CYCLES)
        self.set_gain(config.SENSOR_GAIN)
        self.set_measurement_mode(config.SENSOR_MEASUREMENT_MODE)

        currents = self.read_bulb_currents()

        applied = self.read_configuration()
        applied["led_current"] = currents["white"]
        applied["led_current_ma"] = config.SENSOR_LED_CURRENT_NAMES.get(
            currents["white"], "unknown"
        )
        applied["led_currents"] = currents
        applied["led_currents_ma"] = {
            name: config.SENSOR_LED_CURRENT_NAMES.get(value, "unknown")
            for name, value in currents.items()
        }
        applied["measurement_mode_name"] = (
            config.SENSOR_MEASUREMENT_MODE_NAMES.get(
                applied["measurement_mode"], "unknown"
            )
        )
        applied["bulbs_off"] = not any(self.bulb_states().values())

        expected = {
            "integration_cycles": config.SENSOR_INTEGRATION_CYCLES,
            "gain": config.SENSOR_GAIN,
            "measurement_mode": config.SENSOR_MEASUREMENT_MODE,
            "led_current": config.WHITE_LED_CURRENT,
        }

        mismatched = [
            name for name in expected
            if applied.get(name) != expected[name]
        ]

        if mismatched:
            raise SensorError(
                "SENSOR_CONFIG_NOT_APPLIED",
                "The AS7265x did not accept {}. Requested {}, read back "
                "{}.".format(
                    ", ".join(mismatched),
                    expected,
                    {name: applied.get(name) for name in expected},
                ),
                stage="CONFIGURATION",
                details={
                    "expected": expected,
                    "applied": applied,
                    "mismatched": mismatched,
                },
            )

        return applied

    # ------------------------------------------------------------------
    # acquisition
    # ------------------------------------------------------------------

    def integration_timeout_ms(self):
        """
        Worst-case conversion time for the configured integration.

        One cycle is 2.8 ms; 1.5x headroom, floor of 1500 ms so a short
        integration still tolerates bus latency.
        """
        estimate = int(config.SENSOR_INTEGRATION_CYCLES * 2.8 * 1.5) + 1

        return max(estimate, 1500)

    def start_measurement(self):
        """
        Arm ONE conversion.

        In one-shot mode writing the measurement mode is the trigger, so
        every spectrum is a new conversion that started after this call
        - and therefore after the illumination was switched and settled.
        """
        self.set_measurement_mode(config.SENSOR_MEASUREMENT_MODE)

    def read_temperatures(self):
        """
        Device temperature in degrees C, per internal device.

        Metadata only. Nothing here compensates for temperature; the
        value is recorded so a drift can be recognised later.
        """
        temperatures = {}

        for name, device in (
            ("uv", DEV_UV), ("visible", DEV_VISIBLE), ("nir", DEV_NIR)
        ):
            self.select_device(device)
            temperatures[name] = self.virtual_read(V_DEVICE_TEMP)

        return temperatures

    def wait_data_ready(self, timeout_ms=None):
        if timeout_ms is None:
            timeout_ms = self.integration_timeout_ms()

        started = time.ticks_ms()

        while True:
            config_byte = self.virtual_read(V_CONFIG)

            if (config_byte & (1 << 1)) != 0:
                return time.ticks_diff(time.ticks_ms(), started)

            elapsed = time.ticks_diff(time.ticks_ms(), started)

            if elapsed > timeout_ms:
                raise SensorError(
                    "SENSOR_DATA_READY_TIMEOUT",
                    "DATA_READY did not become active within {} ms."
                    .format(timeout_ms),
                    stage="WAIT_DATA_READY",
                    details={
                        "waited_ms": elapsed,
                        "timeout_ms": timeout_ms,
                        "config_register": "0x{:02X}".format(config_byte),
                    },
                )

            time.sleep_ms(config.POLLING_DELAY_MS)

    def read_channel(self, channel, cal_register, device):
        """One calibrated channel, naming the channel if it fails."""
        try:
            self.select_device(device)

            raw = bytes([
                self.virtual_read(cal_register + offset)
                for offset in range(4)
            ])

            return struct.unpack(">f", raw)[0]

        except SensorError as error:
            raise SensorError(
                "CHANNEL_READ_FAILED",
                "Channel {} could not be read: {}".format(
                    channel, error.message
                ),
                stage="CHANNEL_ACQUISITION",
                details={
                    "channel": channel,
                    "virtual_register": "0x{:02X}".format(cal_register),
                    "underlying_code": error.code,
                    "underlying_details": error.details,
                },
            )

        except Exception as error:
            raise SensorError(
                "CHANNEL_DECODE_FAILED",
                "Channel {} could not be decoded: {}".format(channel, error),
                stage="CHANNEL_ACQUISITION",
                details={
                    "channel": channel,
                    "exception_type": type(error).__name__,
                },
            )

    def read_all_channels(self):
        """All 18 calibrated channels, in wavelength order."""
        spectrum = {}

        for channel, _nm, _name, cal_register, device in CHANNEL_LAYOUT:
            spectrum[channel] = self.read_channel(
                channel, cal_register, device
            )

        return spectrum


# ----------------------------------------------------------------------
# bus helpers
# ----------------------------------------------------------------------

def create_i2c():
    """Build the configured I2C bus."""
    try:
        return I2C(
            config.I2C_BUS,
            scl=Pin(config.I2C_SCL_PIN),
            sda=Pin(config.I2C_SDA_PIN),
            freq=config.I2C_FREQ,
        )

    except Exception as error:
        raise SensorError(
            "I2C_INIT_FAILED",
            "Could not initialize I2C bus {} (SDA=GPIO{}, SCL=GPIO{}): "
            "{}".format(
                config.I2C_BUS,
                config.I2C_SDA_PIN,
                config.I2C_SCL_PIN,
                error,
            ),
            stage="I2C_INIT",
            details={"exception_type": type(error).__name__},
        )


def scan_bus(i2c):
    try:
        return [int(address) for address in i2c.scan()]

    except Exception as error:
        raise SensorError(
            "I2C_SCAN_FAILED",
            "I2C scan failed: {}".format(error),
            stage="I2C_SCAN",
            details={"exception_type": type(error).__name__},
        )


def bus_description():
    return {
        "bus": config.I2C_BUS,
        "sda": "GPIO{}".format(config.I2C_SDA_PIN),
        "scl": "GPIO{}".format(config.I2C_SCL_PIN),
        "frequency_hz": config.I2C_FREQ,
        "expected_address": "0x{:02X}".format(ADDRESS),
    }


# ----------------------------------------------------------------------
# the one sensor runtime
# ----------------------------------------------------------------------

class SensorRuntime:
    """
    The single authoritative sensor lifecycle.

    ensure_ready() is the only way anything gets a working driver. A
    failure is never latched: the next call retries from scratch, so a
    sensor that was not powered at boot recovers by itself as soon as
    the operator asks for anything that needs it.
    """

    def __init__(self):
        self.i2c = None
        self.driver = None

        self.ready = False
        self.configuration = None

        # The 3.3 V rail and the AS7265x internal startup need a moment
        # after reset before the I2C bus means anything. That wait is
        # the SENSOR's, not the protocol's: the board answers ping and
        # get_status from the instant it boots, and the first command
        # that actually needs the sensor pays whatever is left of this.
        self.power_on_ms = time.ticks_ms()
        self.startup_wait_done = False

        # Health, deliberately plain fields rather than a framework.
        self.first_init_error = None
        self.current_error = None
        self.recovery_count = 0
        self.attempted = False

        self.last_scan = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def _scan_for_sensor(self):
        """Bounded scan retries, then confirm the expected address."""
        attempts = max(1, config.I2C_SCAN_ATTEMPTS)
        found = []

        for attempt in range(attempts):
            found = scan_bus(self.i2c)
            self.last_scan = ["0x{:02X}".format(a) for a in found]

            if ADDRESS in found:
                return found

            if attempt < attempts - 1:
                time.sleep_ms(config.I2C_SCAN_RETRY_MS)

        details = dict(bus_description())
        details["addresses"] = self.last_scan
        details["device_count"] = len(found)
        details["scan_attempts"] = attempts

        if not found:
            raise SensorError(
                "I2C_NO_DEVICES",
                "The I2C bus responded but no device answered. Check "
                "wiring, pull-ups and sensor power.",
                stage="I2C_SCAN",
                details=details,
            )

        raise SensorError(
            "AS7265X_ADDRESS_NOT_FOUND",
            "Devices answered on the bus, but not the AS7265x at "
            "0x{:02X}.".format(ADDRESS),
            stage="I2C_SCAN",
            details=details,
        )

    def _await_power_on(self):
        """
        Sleep out whatever remains of the post-reset settling time.

        Called once, immediately before the first I2C transaction of
        the run. Blocking here blocks one command; blocking at boot
        would have blocked the protocol.
        """
        if self.startup_wait_done:
            return 0

        self.startup_wait_done = True

        elapsed = time.ticks_diff(time.ticks_ms(), self.power_on_ms)
        remaining = (config.STARTUP_DELAY_SECONDS * 1000) - elapsed

        if remaining > 0:
            time.sleep_ms(remaining)

            return remaining

        return 0

    def _initialize(self):
        """One full bring-up attempt: bus, scan, devices, configuration."""
        self._await_power_on()

        self.i2c = create_i2c()

        self._scan_for_sensor()

        driver = AS7265X(self.i2c)

        # ONE budget for the whole bring-up. Every virtual-register
        # wait inside it is individually bounded, and their SUM is not
        # - which is how a sensor that answers the bus but never sets
        # its ready bit used to hold the firmware for minutes.
        driver.budget(config.SENSOR_OPERATION_BUDGET_MS)

        try:
            # A physical register read proves the master is really
            # there, before any virtual-register transaction can time
            # out on it.
            driver.read_register(STATUS_REG)

            driver.require_devices()

            self.configuration = driver.apply_configuration()

        finally:
            # Ordinary reads afterwards get their own per-transaction
            # bound and no overall one; a budget left set would expire
            # mid-measurement hours later.
            driver.budget(None)

        self.driver = driver
        self.ready = True
        self.current_error = None

        return driver

    def ensure_ready(self, force_reinit=False):
        """
        Return a working driver, bringing the sensor up if necessary.

        This is the ONLY entry point. Normal measurement, the sensor
        test and status all come through here, so there is no second
        sensor implementation that can succeed while this one reports a
        failure.
        """
        if self.ready and self.driver is not None and not force_reinit:
            return self.driver

        self.ready = False
        self.driver = None
        self.configuration = None
        self.attempted = True

        attempts = max(1, config.SENSOR_INIT_ATTEMPTS)
        last_error = None

        for attempt in range(attempts):
            try:
                driver = self._initialize()

                # Every bring-up counts; boot() subtracts its own, so
                # what remains is genuinely "times the sensor had to be
                # brought back during the run".
                self.recovery_count += 1

                return driver

            except SensorError as error:
                last_error = error

            except Exception as error:
                last_error = SensorError(
                    "SENSOR_INIT_FAILED",
                    "Unexpected failure during sensor initialization: "
                    "{}".format(error),
                    stage="SENSOR_INIT",
                    details={"exception_type": type(error).__name__},
                )

            # Drop the half-built bus so the retry starts clean.
            self.i2c = None

            if attempt < attempts - 1:
                time.sleep_ms(config.SENSOR_INIT_RETRY_MS)

        last_error.details["init_attempts"] = attempts
        self.current_error = last_error

        raise last_error

    def bring_up(self):
        """
        Try to bring the sensor up, recording the outcome either way.

        Called by the first command that needs the sensor, never at
        boot. A failure is recorded and nothing else: the command
        server, the carousel and status all keep working, and the next
        sensor-dependent command retries from scratch - one failure
        must never become permanent runtime state.
        """
        try:
            self.ensure_ready()

            self.first_init_error = None

            # The first bring-up is not a recovery from anything.
            self.recovery_count = 0

            return True

        except SensorError as error:
            self.first_init_error = error.as_dict()

            return False

    # ------------------------------------------------------------------
    # acquisition
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_spectrum(spectrum, stage):
        missing = [
            channel for channel in CHANNELS
            if not isinstance(spectrum.get(channel), (int, float))
            or isinstance(spectrum.get(channel), bool)
        ]

        if missing:
            raise SensorError(
                "INCOMPLETE_SPECTRUM",
                "Acquisition returned {}/{} channels; missing {}.".format(
                    len(CHANNELS) - len(missing),
                    len(CHANNELS),
                    ",".join(missing),
                ),
                stage=stage,
                details={"missing_channels": missing},
            )

        return spectrum

    def acquire_one(self, lamp=None):
        """
        The ONE raw acquisition path. Every spectrum in the system -
        sample, calibration, dark, sensor test - comes through here.

            all bulbs OFF
              -> select illumination (if any)
              -> set configured bulb current
              -> enable bulb
              -> wait for the illumination to settle
              -> trigger the one-shot conversion
              -> wait DATA_READY
              -> read all 18 calibrated channels
              -> all bulbs OFF

        `lamp` is LED_WHITE / LED_IR / LED_UV, or None for a dark
        acquisition with every lamp off.

        The lamps are killed from a finally block, so no failure at any
        point can leave one burning. Returns raw counts only - no dark
        correction, no normalization, no database.
        """
        driver = self.ensure_ready()

        try:
            # Never change illumination from an unknown state.
            driver.disable_all_bulbs()

            if lamp is not None:
                driver.set_bulb_current(lamp, LAMP_CURRENTS[lamp]())
                driver.enable_bulb(lamp)
                time.sleep_ms(config.ILLUMINATION_SETTLE_MS)

            driver.start_measurement()
            waited_ms = driver.wait_data_ready()

            spectrum = driver.read_all_channels()

        except SensorError as error:
            # The driver is suspect now; the next command re-initializes
            # rather than reusing a half-dead one.
            self.ready = False
            self.current_error = error

            raise

        finally:
            try:
                driver.disable_all_bulbs()

            except Exception:
                pass

        self._validate_spectrum(spectrum, "CHANNEL_VALIDATION")
        self.current_error = None

        return {
            "spectrum": spectrum,
            "data_ready_wait_ms": waited_ms,
            "illumination": (
                LAMP_NAMES[lamp] if lamp is not None else "dark"
            ),
            "zero_channels": [
                channel for channel in CHANNELS
                if spectrum[channel] == 0.0
            ],
        }

    def acquire_block(self, lamp=None, repeats=1):
        """
        Repeat one illumination N times and return every reading.

        The ESP32 does no statistics: the individual acquisitions travel
        to the PC intact, so aggregation, outlier handling and
        repeatability all happen where there is real floating point and
        where the raw readings can be archived and audited.
        """
        repeats = max(1, min(int(repeats), config.MAX_REPEATS))

        acquisitions = []
        waits = []

        for index in range(repeats):
            result = self.acquire_one(lamp)

            acquisitions.append(result["spectrum"])
            waits.append(result["data_ready_wait_ms"])

            if index < repeats - 1:
                time.sleep_ms(config.REPEAT_DELAY_MS)

        return {
            "illumination": (
                LAMP_NAMES[lamp] if lamp is not None else "dark"
            ),
            "repeats": repeats,
            "acquisitions": acquisitions,
            "data_ready_wait_ms": waits,
        }

    def acquire_triad(self, repeats=None):
        """
        One complete sample measurement: WHITE, then UV, then IR.

        18 channels under each of the three illuminations - up to 54
        spectral features per sample. Each block is fully independent
        and every lamp is off between them.
        """
        if repeats is None:
            repeats = config.SAMPLE_REPEATS

        blocks = {}
        codes = {
            "white": ("WHITE_ACQUISITION_FAILED", LED_WHITE),
            "uv": ("UV_ACQUISITION_FAILED", LED_UV),
            "ir": ("IR_ACQUISITION_FAILED", LED_IR),
        }

        for name in ("white", "uv", "ir"):
            code, lamp = codes[name]

            try:
                blocks[name] = self.acquire_block(lamp, repeats)

            except SensorError as error:
                raise SensorError(
                    code,
                    "{} illumination acquisition failed: {}".format(
                        name.upper(), error.message
                    ),
                    stage=error.stage,
                    details={
                        "illumination": name,
                        "completed": sorted(blocks.keys()),
                        "underlying_code": error.code,
                        "underlying_details": error.details,
                    },
                )

        return blocks


    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------

    def settings(self):
        """Configuration read back from the sensor, or None if unknown."""
        return self.configuration

    def state(self):
        """
        READY, UNAVAILABLE, or NOT_INITIALIZED - never a bare False.

        NOT_INITIALIZED is a real and common state now that the sensor
        is brought up lazily: the board has booted, the protocol is
        serving, and nothing has yet asked for a spectrum. It must not
        be reported as a fault, because it is not one.
        """
        if self.ready and self.driver is not None:
            return "READY"

        if not self.attempted:
            return "NOT_INITIALIZED"

        return "UNAVAILABLE"

    def status(self):
        """
        What the sensor is, without touching it.

        Deliberately asks NOTHING of the hardware: get_status must
        answer at the same speed whether the AS7265x is present,
        missing or half dead.
        """
        return {
            "state": self.state(),
            "ready": self.ready,
            "address": "0x{:02X}".format(ADDRESS),
            "bus": bus_description(),
            "last_scan": self.last_scan,
            "settings": self.configuration,
            "first_init_error": self.first_init_error,
            "current_error": (
                self.current_error.as_dict()
                if isinstance(self.current_error, SensorError)
                else self.current_error
            ),
            "recovery_count": self.recovery_count,
        }
