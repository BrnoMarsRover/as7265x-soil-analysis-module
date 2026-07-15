# config.py - Master hardware setup and configuration

# ==========================================
# I2C & SENSOR SETTINGS (AS7265x)
# ==========================================
I2C_SCL_PIN = 22
I2C_SDA_PIN = 21
I2C_FREQ = 100000

# Onboard LED Brightness (Current Limit)
# Options: 0b00 = 12.5mA, 0b01 = 25mA, 0b10 = 50mA, 0b11 = 100mA
ONBOARD_LED_CURRENT = 0b01 

# ==========================================
# SERVO MOTOR SETTINGS (HS-5645MG)
# ==========================================
# GPIO pin connected to the servo's signal wire (Yellow/White)
SERVO_PIN = 25 

# Measurement positions
SERVO_CENTER_ANGLE = 90 # Center position (degrees)
SERVO_SWING_DEGREES = 30 # How many degrees left/right the arm moves

# ==========================================
# SYSTEM TIMING SETTINGS
# ==========================================
# Serial command polling delay in milliseconds
CMD_POLL_DELAY_MS = 100

# Pause time (in seconds) to let the mechanical arm settle before taking a reading
MECHANICAL_SETTLE_TIME = 1.5