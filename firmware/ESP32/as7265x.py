# as7265x.py
#
# AS7265x triad spectrometer: driver plus the one authoritative sensor
# runtime lifecycle.
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

LED_WHITE = 0x00

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
        """Poll STATUS_REG until the bit matches, or give up in bounded time."""
        started = time.ticks_ms()

        while True:
            status = self.read_register(STATUS_REG)
            is_set = (status & mask) != 0

            if is_set == want_set:
                return status

            elapsed = time.ticks_diff(time.ticks_ms(), started)

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

    def set_led_current(self, current):
        value = self.virtual_read(V_LED_CONFIG)
        value &= 0b11001111
        value |= (min(current, 0b11) & 0b11) << 4
        self.virtual_write(V_LED_CONFIG, value)

    def read_led_current(self):
        return (self.virtual_read(V_LED_CONFIG) >> 4) & 0b11

    def set_led(self, on):
        """Onboard white illumination LED."""
        self.select_device(LED_WHITE)
        value = self.virtual_read(V_LED_CONFIG)

        if on:
            value |= 1 << 3
        else:
            value &= ~(1 << 3)

        self.virtual_write(V_LED_CONFIG, value & 0xFF)

    def apply_configuration(self):
        """
        Write the config.py acquisition settings, then READ THEM BACK.

        The read-back is the point. Firmware that only reported its
        intended settings claimed 100 cycles / 16x while the sensor was
        still running its power-on defaults, because nothing ever
        verified that the writes landed.
        """
        self.select_device(LED_WHITE)
        self.set_led_current(config.ONBOARD_LED_CURRENT)
        self.set_led(False)

        self.set_integration_cycles(config.SENSOR_INTEGRATION_CYCLES)
        self.set_gain(config.SENSOR_GAIN)
        self.set_measurement_mode(config.SENSOR_MEASUREMENT_MODE)

        applied = self.read_configuration()
        applied["led_current"] = self.read_led_current()
        applied["led_current_ma"] = config.SENSOR_LED_CURRENT_NAMES.get(
            applied["led_current"], "unknown"
        )

        expected = {
            "integration_cycles": config.SENSOR_INTEGRATION_CYCLES,
            "gain": config.SENSOR_GAIN,
            "measurement_mode": config.SENSOR_MEASUREMENT_MODE,
            "led_current": config.ONBOARD_LED_CURRENT,
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
        """Re-arm the measurement mode so the next result is a NEW one."""
        self.set_measurement_mode(config.SENSOR_MEASUREMENT_MODE)

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

        # Health, deliberately four plain fields rather than a framework.
        self.boot_error = None
        self.current_error = None
        self.recovery_count = 0

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

    def _initialize(self):
        """One full bring-up attempt: bus, scan, devices, configuration."""
        self.i2c = create_i2c()

        self._scan_for_sensor()

        driver = AS7265X(self.i2c)

        # A physical register read proves the master is really there,
        # before any virtual-register transaction can time out on it.
        driver.read_register(STATUS_REG)

        driver.require_devices()

        self.configuration = driver.apply_configuration()
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

    def boot(self):
        """
        Try to bring the sensor up at startup.

        A failure here is recorded and nothing else. The command server,
        the carousel and status all keep working, and the next
        sensor-dependent command retries from scratch - a boot failure
        must never become permanent runtime state.
        """
        try:
            self.ensure_ready()

            self.boot_error = None

            # The bring-up at boot is not a recovery from anything.
            self.recovery_count = 0

            return True

        except SensorError as error:
            self.boot_error = error.as_dict()

            return False

    # ------------------------------------------------------------------
    # acquisition
    # ------------------------------------------------------------------

    def acquire_raw_spectrum(self):
        """
        The ONE raw acquisition path.

        Illumination on, settle, start a new integration, wait for
        DATA_READY, read all 18 channels, validate, illumination off.
        The LED is switched off from a finally block, so no failure can
        leave it burning.

        Returns raw counts only. No dark correction, no white
        normalization, no database - that is the PC's and BD's work.
        """
        driver = self.ensure_ready()

        try:
            driver.set_led(True)
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
                driver.set_led(False)

            except Exception:
                pass

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
                stage="CHANNEL_VALIDATION",
                details={"missing_channels": missing},
            )

        self.current_error = None

        return {
            "spectrum": spectrum,
            "data_ready_wait_ms": waited_ms,
            "zero_channels": [
                channel for channel in CHANNELS
                if spectrum[channel] == 0.0
            ],
        }

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------

    def settings(self):
        """Configuration read back from the sensor, or None if unknown."""
        return self.configuration

    def status(self):
        return {
            "ready": self.ready,
            "address": "0x{:02X}".format(ADDRESS),
            "bus": bus_description(),
            "last_scan": self.last_scan,
            "settings": self.configuration,
            "boot_error": self.boot_error,
            "current_error": (
                self.current_error.as_dict()
                if isinstance(self.current_error, SensorError)
                else self.current_error
            ),
            "recovery_count": self.recovery_count,
        }
