# hs5645mg.py
import time
import config
from machine import Pin, PWM

class HS5645MG:
    def __init__(self, pin_num):
        # Initialize PWM at 50Hz (Standard for RC servos)
        self.pwm = PWM(Pin(pin_num), freq=50)
        
        # DEFINITIONS FOR UNDERSTANDING:
        # Most servos use a pulse range of 600us (0°) to 2400us (180°)
        self.min_us = 600 
        self.max_us = 2400
        self.range_us = self.max_us - self.min_us # Total span: 1800us

    def set_angle(self, angle):
        """
        Calculates the exact pulse needed for an angle.
        Formula: (Target Angle / Max Angle) * Pulse Range + Minimum Pulse
        """
        # 1. Keep the angle within 0 to 180 degrees
        angle = max(0, min(angle, 180)) 
        
        # 2. Calculate microsecond (us) pulse
        # Example: 90 degrees -> (90/180) * 1800 + 600 = 1500us (Perfect Center)
        target_us = int((angle / 180.0) * self.range_us + self.min_us)
        
        # 3. Convert to nanoseconds (ns) for the ESP32 hardware timer
        self.pwm.duty_ns(target_us * 1000)
        print(
    "   [Servo] Angle: {} deg, Pulse: {} us".format(
        angle,
        target_us   
        )
)

    def release(self):
        """Cuts signal so the motor stops drawing power and fighting resistance."""
        self.pwm.deinit()

class MeasurementMovement:
    def __init__(self, pin_num=config.SERVO_PIN):
        self.motor = HS5645MG(pin_num=pin_num)

    def move_to_soil(self):
        """Moves 60 degrees left from the center value set in config."""
        target = config.SERVO_CENTER_ANGLE - config.SERVO_SWING_DEGREES
        self.motor.set_angle(target)
        time.sleep(config.MECHANICAL_SETTLE_TIME)

    def move_to_white(self):
        """Moves 60 degrees right from the center value set in config."""
        target = config.SERVO_CENTER_ANGLE + config.SERVO_SWING_DEGREES
        self.motor.set_angle(target)
        time.sleep(config.MECHANICAL_SETTLE_TIME)
        
    def return_to_center(self):
        """Moves to exactly the value defined as center in config."""
        self.motor.set_angle(config.SERVO_CENTER_ANGLE)
        time.sleep(1)

    def shutdown(self):
        self.motor.release()