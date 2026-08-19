# main.py
#
# Application entry point for the Brno Mars Rover / Freya science module.
#
# It does three things and nothing else:
#
#     build the runtime state
#     build the protocol
#     serve
#
# PROTOCOL FIRST. Nothing between reset and the serving loop touches a
# peripheral - no I2C transaction, no UART, no settling delay, no retry
# loop. A board that cannot be reached cannot be diagnosed, so a broken
# sensor or an unpowered servo must never be able to make the module
# silent. Both are states the protocol REPORTS:
#
#     sensor      NOT_INITIALIZED until something needs it, then
#                 READY or UNAVAILABLE
#     servo       not connected until the operator connects it
#     carousel    position invalid until it is synchronized
#
# and in every one of those states ping, get_status and the diagnostics
# still answer.
#
# ESP32 responsibilities as a whole:
#   - AS7265x initialization, recovery and raw acquisition
#   - ST3215 communication and verified movement
#   - carousel physical movement and position state
#   - hardware status and the JSON protocol
#
# Scientific processing, White/Dark normalization, database comparison
# and persistent Sample storage run on the PC. This firmware never
# learns what material a sample resembles: it moves hardware, reads
# hardware and reports hardware.
#
# Transport:
#   newline-delimited JSON over USB / CP2102 using sys.stdin/sys.stdout.
#   stdout IS the protocol stream, so nothing here may print outside a
#   JSON frame.
#
# Deployment and terminal instructions: see Documentation/OPERATIONS.md

import time

from carousel import Carousel
from protocol import Protocol, debug
from sensor import SensorRuntime
from servo import ServoLink


class Hardware:
    """
    Everything the module owns, and nothing it does.

    Construction opens no bus and commands no peripheral: it creates
    three objects that each know how to bring their own hardware up
    later, on demand. That is what makes the protocol reachable one
    instant after reset.
    """

    def __init__(self):
        self.started_at = time.ticks_ms()

        # Brings itself up on the first command that needs a spectrum,
        # and retries from scratch after any failure.
        self.sensor = SensorRuntime()

        # No servo is connected at startup, and none is guessed at. A
        # reboot is exactly when someone may have unplugged the servo
        # or its external supply, so the operator connects it through
        # option [0] before the carousel will move at all.
        self.servo = ServoLink()
        self.carousel = Carousel(None)

    def uptime_ms(self):
        return time.ticks_diff(time.ticks_ms(), self.started_at)

    def shutdown(self):
        """
        Release the servo link on the way out, never raising.

        Torque is deliberately left alone: dropping to the REPL must
        not let the carousel turn and lose the position the operator
        synchronized.
        """
        try:
            self.servo.release("firmware stopped")

        except Exception as error:
            debug("could not release the servo link:", error)


# Module-level handles so the running instance can be inspected from
# the REPL after Ctrl+C:  import main; main.hardware.carousel.status()
hardware = None
service = None


def main():
    global hardware, service

    hardware = Hardware()
    service = Protocol(hardware)

    try:
        service.serve_forever()

    except KeyboardInterrupt:
        debug("command loop stopped by user")

    finally:
        hardware.shutdown()


if __name__ == "__main__":
    main()
