# main.py
#
# Application entry point for the Brno Mars Rover / Freya science module.
#
# This file builds the subsystems, registers the command groups, and
# starts the serving loop. That is all it does. The things it used to do
# as well now live where they belong:
#
#     drivers/    AS7265x registers, ST3215 packets and encoder
#     control/    carousel geometry, servo selection and lifecycle
#     protocol/   JSON framing, command routing, the command handlers
#
# ESP32 responsibilities as a whole:
#   - carousel control through the selected servo backend
#   - AS7265x initialization and recovery
#   - raw 18-channel spectral acquisition
#   - hardware/status responses
#
# Scientific processing, White/Dark normalization, database comparison and
# persistent Sample storage run on the PC. This firmware never learns what
# material a sample resembles: it moves hardware, reads hardware and
# reports hardware.
#
# Transport:
#   newline-delimited JSON over USB / CP2102 using sys.stdin/sys.stdout.
#   stdout IS the protocol stream, so nothing here may print outside a
#   JSON frame.
#
# Deployment and terminal instructions:
#   see ../../Documentation/OPERATIONS.md

import time

import config

from control.carousel import Carousel, CarouselError
from control.servo_manager import ServoManager, ServoSelectionError
from drivers.as7265x import SensorError, SensorRuntime
from drivers.servo_base import ServoError
from protocol import transport
from protocol.carousel_commands import CarouselCommands
from protocol.router import CommandError, Router
from protocol.sample_commands import SampleCommands
from protocol.sensor_commands import SensorCommands
from protocol.servo_commands import ServoCommands
from protocol.transport import debug


class HardwareModule:
    """Runtime state and command surface for the ESP32 hardware."""

    def __init__(self):
        self.started_at = time.ticks_ms()

        self.sensor = SensorRuntime()

        # No servo is selected at startup, and none is guessed at. A
        # reboot is exactly when someone may have changed the actuator, so
        # the operator states which one is fitted before the carousel will
        # move at all.
        self.servos = ServoManager()
        self.carousel = Carousel(None)

        self.commands = (
            ServoCommands(self),
            CarouselCommands(self),
            SensorCommands(self),
            SampleCommands(self),
        )

        self.router = Router(
            handlers={
                "ping": self.handle_ping,
                "get_status": self.handle_get_status,
            },
            error_types=(
                # Each domain error keeps its own code on the way out, so
                # the PC can tell a servo fault from a sensor fault from a
                # rejected request.
                (CarouselError, self._carousel_envelope),
                (ServoSelectionError, self._selection_envelope),
                (ServoError, self._servo_envelope),
                (SensorError, self._sensor_envelope),
            ),
        )

        for group in self.commands:
            self.router.register(group.handlers())

    # ------------------------------------------------------------------
    # error envelopes
    # ------------------------------------------------------------------

    @staticmethod
    def _carousel_envelope(error):
        envelope = {
            "error": {"code": error.code, "message": error.message}
        }

        # A failed movement carries whatever the backend could still
        # report, so the PC can tell "the servo is unreachable" apart from
        # "the servo is fine but stopped in the wrong place".
        if error.data is not None:
            envelope["data"] = error.data

        return envelope

    @staticmethod
    def _selection_envelope(error):
        envelope = {
            "error": {"code": error.code, "message": error.message}
        }

        if error.data is not None:
            envelope["data"] = error.data

        return envelope

    @staticmethod
    def _servo_envelope(error):
        return {"error": error.as_dict()}

    @staticmethod
    def _sensor_envelope(error):
        return {"error": error.as_dict()}

    # ------------------------------------------------------------------
    # boot
    # ------------------------------------------------------------------

    def boot(self):
        """
        Bring the hardware up, quietly.

        stdout belongs to the protocol from the moment the board starts, so
        nothing is printed. A sensor that does not answer here does not
        stop anything: the command server, the carousel and status all
        still work, and the next sensor command retries by itself.

        NO SERVO IS TOUCHED. Not initialized, not pinged, not powered, not
        moved - the firmware does not yet know which actuator is fitted,
        and guessing would mean driving a PWM pin or a UART at hardware
        that may not be there. The operator selects the servo first.
        """
        time.sleep(config.STARTUP_DELAY_SECONDS)

        self.sensor.boot()

        debug("{} {} ready; sensor={} servo=none".format(
            config.FIRMWARE_NAME,
            config.FIRMWARE_VERSION,
            self.sensor.ready,
        ))

        return True

    # ------------------------------------------------------------------
    # shared helpers, used by the command groups
    # ------------------------------------------------------------------

    def uptime_ms(self):
        return time.ticks_diff(time.ticks_ms(), self.started_at)

    def require_slot(self, request):
        if "slot" not in request:
            raise CommandError(
                "MISSING_FIELD",
                "Command requires a 'slot' field (1 to {}).".format(
                    config.CAROUSEL_SLOT_COUNT
                ),
            )

        return self.carousel.validate_slot(request.get("slot"))

    def sensor_error(self, error, extra=None):
        """Turn a SensorError into a CommandError with the same detail."""
        data = error.as_dict()

        if extra:
            data.update(extra)

        return CommandError(error.code, error.message, data=data)

    # ------------------------------------------------------------------
    # basic commands
    # ------------------------------------------------------------------

    def handle_ping(self, request):
        return {
            "pong": True,
            "firmware": config.FIRMWARE_NAME,
            "version": config.FIRMWARE_VERSION,
            "uptime_ms": self.uptime_ms(),
        }

    def handle_get_status(self, request):
        """
        Hardware state only. Nothing scientific is known here.

        The servo block always answers, even with nothing selected: "NOT
        SELECTED" is the single most important thing an operator can be
        told about the carousel, so it is never hidden behind an error.
        """
        return {
            "firmware": config.FIRMWARE_NAME,
            "version": config.FIRMWARE_VERSION,
            "uptime_ms": self.uptime_ms(),
            "transport": "usb-serial-console",
            "commands": self.router.command_names(),

            "sensor": self.sensor.status(),
            "servo": self.servos.status(),
            "carousel": self.carousel.status(),
            "slots": self.carousel.slot_summary(),
        }

    # ------------------------------------------------------------------
    # dispatch, kept for callers and tests that drive one command
    # ------------------------------------------------------------------

    def dispatch_command(self, request):
        return self.router.dispatch(request)

    def process_command(self, line):
        return self.router.process_line(line)

    # ------------------------------------------------------------------
    # run
    # ------------------------------------------------------------------

    def run(self):
        self.boot()

        try:
            self.router.serve_forever()

        except KeyboardInterrupt:
            debug("command loop stopped by user")

        finally:
            try:
                # Releases whichever backend is live. Torque, where the
                # actuator has any, is deliberately left alone: dropping to
                # the REPL must not let the carousel turn and lose the
                # position the operator synchronized.
                self.servos.release("firmware stopped")

            except Exception as error:
                debug("could not release the servo backend:", error)


# Module-level handle so the running instance can be inspected from the
# REPL after Ctrl+C:  import main; main.module.carousel.status()
module = None


def main():
    global module

    module = HardwareModule()
    module.run()


if __name__ == "__main__":
    main()
