# as7265x.py
from machine import Pin, I2C
import time
import struct
import config

class AS7265X:
    ADDRESS = 0x49
    STATUS_REG = 0x00
    WRITE_REG = 0x01
    READ_REG = 0x02
    TX_VALID = 0x02
    RX_VALID = 0x01

    CONFIG = 0x04
    INTEGRATION_TIME = 0x05
    LED_CONFIG = 0x07  # Required for LED control
    DEV_SELECT_CONTROL = 0x4F

    POLLING_DELAY_MS = 5

    DEV_NIR = 0x00
    DEV_VISIBLE = 0x01
    DEV_UV = 0x02

    LED_WHITE = 0x00
    LED_IR = 0x01
    LED_UV = 0x02

    GAIN_1X = 0b00
    GAIN_3_7X = 0b01
    GAIN_16X = 0b10
    GAIN_64X = 0b11

    MEASUREMENT_MODE_6CHAN_CONTINUOUS = 0b10
    
    R_G_A_CAL = 0x14
    S_H_B_CAL = 0x18
    T_I_C_CAL = 0x1C
    U_J_D_CAL = 0x20
    V_K_E_CAL = 0x24
    W_L_F_CAL = 0x28

    def __init__(self, i2c, address=ADDRESS, debug=False):
        self.i2c = i2c
        self.address = address
        self.debug = debug
        self.max_wait_time_ms = 1071

    def _sleep_ms(self, ms): time.sleep_ms(ms)
    def _ticks_ms(self): return time.ticks_ms()
    def _ticks_diff(self, a, b): return time.ticks_diff(a, b)

    def read_register(self, addr):
        try:
            self.i2c.writeto(self.address, bytes([addr]))
            data = self.i2c.readfrom(self.address, 1)
            return data[0] if len(data) == 1 else 0
        except OSError:
            return 0

    def write_register(self, addr, val):
        try:
            self.i2c.writeto(self.address, bytes([addr, val & 0xFF]))
            return True
        except OSError:
            return False

    def virtual_read_register(self, virtual_addr):
        status = self.read_register(self.STATUS_REG)
        if (status & self.RX_VALID) != 0:
            _ = self.read_register(self.READ_REG)

        start_time = self._ticks_ms()
        while True:
            if self._ticks_diff(self._ticks_ms(), start_time) > self.max_wait_time_ms: return 0
            if (self.read_register(self.STATUS_REG) & self.TX_VALID) == 0: break
            self._sleep_ms(self.POLLING_DELAY_MS)

        self.write_register(self.WRITE_REG, virtual_addr & 0x7F)

        start_time = self._ticks_ms()
        while True:
            if self._ticks_diff(self._ticks_ms(), start_time) > self.max_wait_time_ms: return 0
            if (self.read_register(self.STATUS_REG) & self.RX_VALID) != 0: break
            self._sleep_ms(self.POLLING_DELAY_MS)

        return self.read_register(self.READ_REG)

    def virtual_write_register(self, virtual_addr, data_to_write):
        start_time = self._ticks_ms()
        while True:
            if self._ticks_diff(self._ticks_ms(), start_time) > self.max_wait_time_ms: return
            if (self.read_register(self.STATUS_REG) & self.TX_VALID) == 0: break
            self._sleep_ms(self.POLLING_DELAY_MS)

        self.write_register(self.WRITE_REG, (virtual_addr | 0x80) & 0xFF)

        start_time = self._ticks_ms()
        while True:
            if self._ticks_diff(self._ticks_ms(), start_time) > self.max_wait_time_ms: return
            if (self.read_register(self.STATUS_REG) & self.TX_VALID) == 0: break
            self._sleep_ms(self.POLLING_DELAY_MS)

        self.write_register(self.WRITE_REG, data_to_write & 0xFF)

    def begin(self):
        time.sleep_ms(200)
        value = self.virtual_read_register(self.DEV_SELECT_CONTROL)
        if (value & 0b00110000) == 0:
            raise OSError("AS7265X slave devices not detected")

        # Set up LED brightness from config
        self.set_bulb_current(config.ONBOARD_LED_CURRENT, self.LED_WHITE)
        self.disable_bulb(self.LED_WHITE)

        self.set_integration_cycles(100) 
        self.set_gain(self.GAIN_16X) # Better gain for soil
        self.set_measurement_mode(self.MEASUREMENT_MODE_6CHAN_CONTINUOUS)

        time.sleep_ms(300)
        return True

    # --- LED CONTROL METHODS ---
    def enable_bulb(self, device):
        self.select_device(device)
        value = self.virtual_read_register(self.LED_CONFIG)
        value |= (1 << 3)
        self.virtual_write_register(self.LED_CONFIG, value)

    def disable_bulb(self, device):
        self.select_device(device)
        value = self.virtual_read_register(self.LED_CONFIG)
        value &= ~(1 << 3)
        self.virtual_write_register(self.LED_CONFIG, value)

    def set_bulb_current(self, current, device):
        self.select_device(device)
        if current > 0b11: current = 0b11
        value = self.virtual_read_register(self.LED_CONFIG)
        value &= 0b11001111
        value |= (current << 4)
        self.virtual_write_register(self.LED_CONFIG, value)
    # ---------------------------

    def set_measurement_mode(self, mode):
        value = self.virtual_read_register(self.CONFIG)
        value &= 0b11110011
        value |= (mode << 2)
        self.virtual_write_register(self.CONFIG, value)

    def set_gain(self, gain):
        value = self.virtual_read_register(self.CONFIG)
        value &= 0b11001111
        value |= (gain << 4)
        self.virtual_write_register(self.CONFIG, value)

    def set_integration_cycles(self, cycle_value):
        cycle_value &= 0xFF
        self.max_wait_time_ms = int(cycle_value * 2.8 * 1.5) + 1
        self.virtual_write_register(self.INTEGRATION_TIME, cycle_value)

    def data_available(self):
        return (self.virtual_read_register(self.CONFIG) & (1 << 1)) != 0

    def select_device(self, device):
        self.virtual_write_register(self.DEV_SELECT_CONTROL, device & 0x03)

    def get_calibrated_value(self, cal_address, device):
        self.select_device(device)
        b0 = self.virtual_read_register(cal_address + 0)
        b1 = self.virtual_read_register(cal_address + 1)
        b2 = self.virtual_read_register(cal_address + 2)
        b3 = self.virtual_read_register(cal_address + 3)
        return struct.unpack(">f", bytes([b0, b1, b2, b3]))[0]

    def take_measurements(self):
        self.set_measurement_mode(self.MEASUREMENT_MODE_6CHAN_CONTINUOUS)
        time.sleep_ms(250)
        start_time = self._ticks_ms()
        timeout_ms = max(self.max_wait_time_ms, 1500)
        while self.data_available() is False:
            if self._ticks_diff(self._ticks_ms(), start_time) > timeout_ms:
                raise OSError("AS7265X timeout")
            self._sleep_ms(self.POLLING_DELAY_MS)

    def read_calibrated_channels(self):
        return {
            "A": self.get_calibrated_value(self.R_G_A_CAL, self.DEV_UV),
            "B": self.get_calibrated_value(self.S_H_B_CAL, self.DEV_UV),
            "C": self.get_calibrated_value(self.T_I_C_CAL, self.DEV_UV),
            "D": self.get_calibrated_value(self.U_J_D_CAL, self.DEV_UV),
            "E": self.get_calibrated_value(self.V_K_E_CAL, self.DEV_UV),
            "F": self.get_calibrated_value(self.W_L_F_CAL, self.DEV_UV),
            "G": self.get_calibrated_value(self.R_G_A_CAL, self.DEV_VISIBLE),
            "H": self.get_calibrated_value(self.S_H_B_CAL, self.DEV_VISIBLE),
            "I": self.get_calibrated_value(self.T_I_C_CAL, self.DEV_VISIBLE),
            "J": self.get_calibrated_value(self.U_J_D_CAL, self.DEV_VISIBLE),
            "K": self.get_calibrated_value(self.V_K_E_CAL, self.DEV_VISIBLE),
            "L": self.get_calibrated_value(self.W_L_F_CAL, self.DEV_VISIBLE),
            "R": self.get_calibrated_value(self.R_G_A_CAL, self.DEV_NIR),
            "S": self.get_calibrated_value(self.S_H_B_CAL, self.DEV_NIR),
            "T": self.get_calibrated_value(self.T_I_C_CAL, self.DEV_NIR),
            "U": self.get_calibrated_value(self.U_J_D_CAL, self.DEV_NIR),
            "V": self.get_calibrated_value(self.V_K_E_CAL, self.DEV_NIR),
            "W": self.get_calibrated_value(self.W_L_F_CAL, self.DEV_NIR),
        }

class AS7265X_Driver:
    def __init__(self, i2c_bus=0, scl_pin=config.I2C_SCL_PIN, sda_pin=config.I2C_SDA_PIN, freq=config.I2C_FREQ, debug=False):
        self.i2c = I2C(i2c_bus, scl=Pin(scl_pin), sda=Pin(sda_pin), freq=freq)
        self.debug = debug
        if AS7265X.ADDRESS not in self.i2c.scan():
            raise OSError("AS7265X not found on I2C")
        self.sensor = AS7265X(self.i2c, debug=self.debug)
        self.sensor.begin()

    def read_once(self):
        self.sensor.take_measurements()
        return self.sensor.read_calibrated_channels()

def create_default_driver(debug=False, i2c_bus=0, scl_pin=config.I2C_SCL_PIN, sda_pin=config.I2C_SDA_PIN, freq=config.I2C_FREQ):
    return AS7265X_Driver(i2c_bus=i2c_bus, scl_pin=scl_pin, sda_pin=sda_pin, freq=freq, debug=debug)

# ==========================================
# Merged from measuring_types.py
# ==========================================
class SoilMeasurementSystem:
    def __init__(self, driver):
        self.driver = driver
        self.sensor = driver.sensor
        
        # Memory to store state
        self.dark_ref = None
        self.white_ref = None
        self.sample_data = None
        self.normalized_data = None
        
        self.channels = ['A','B','C','D','E','F','G','H','I','J','K','L','R','S','T','U','V','W']
        self.wavelengths = [410,435,460,485,510,535,560,585,610,645,680,705,730,760,810,860,900,940]

    def take_dark(self):
        """Measurement 1: Dark Reference (Sensor LEDs OFF)"""
        self.sensor.disable_bulb(self.sensor.LED_WHITE)
        time.sleep_ms(500) # Settle time
        self.dark_ref = self.driver.read_once()
        return self.dark_ref

    def take_white(self):
        """Measurement 2: White Reference (Sensor LED ON)"""
        self.sensor.enable_bulb(self.sensor.LED_WHITE)
        time.sleep_ms(500) 
        self.white_ref = self.driver.read_once()
        self.sensor.disable_bulb(self.sensor.LED_WHITE) # Turn off immediately
        return self.white_ref

    def take_sample(self):
        """Measurement 3: Soil Sample (Sensor LED ON)"""
        self.sensor.enable_bulb(self.sensor.LED_WHITE)
        time.sleep_ms(500)
        self.sample_data = self.driver.read_once()
        self.sensor.disable_bulb(self.sensor.LED_WHITE)
        return self.sample_data

    def normalize(self):
        """Calculate Reflectance: R = (S - D) / (W - D)"""
        if not (self.dark_ref and self.white_ref and self.sample_data):
            return False
            
        self.normalized_data = {}
        for ch in self.channels:
            s = self.sample_data[ch]
            d = self.dark_ref[ch]
            w = self.white_ref[ch]
            
            denom = w - d
            if denom == 0:
                self.normalized_data[ch] = 0.0
            else:
                self.normalized_data[ch] = (s - d) / denom
                
        return True