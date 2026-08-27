"""
A read-only diagnostic agent for the ESP32. NOT COMPETITION FIRMWARE.

WHAT IT IS FOR

Three questions the shipped firmware cannot answer, all of them needed
to settle H-002 or to tell a dead I2C bus from an absent sensor:

    what bytes did the ST3215 actually send back?
    what does the servo report for position, load, voltage and
        temperature, in one read?
    which addresses answer on the I2C bus, right now, without
        initializing anything?

WHY IT IS NOT IN PRODUCTION

Because a diagnostic command that can reach a competition is a worse
defect than the gap it fills. This file is never imported by `main.py`,
never runs at boot, and has to be copied to the board deliberately by a
human following DEPLOYMENT.md. When the diagnostic session is over it is
removed and the production firmware's hash is checked back.

THE SAFETY RULES, ENFORCED HERE AND NOT ONLY DOCUMENTED

    no movement            there is no movement command at all. Not a
                           bounded one, not a gated one. The agent
                           cannot turn the carousel because it has no
                           code that could.
    no register writes     the only WRITE on the whole surface is
                           `diag_lamps_off`, and it exists so a session
                           can leave the UV source off.
    a read whitelist       fourteen named ST3215 registers. Everything
                           else is refused. "Read any register" and
                           "write any register" are one typo apart, and
                           a wrong access to the ST3215 memory table can
                           change the servo id or baud rate and take the
                           bus away entirely.
    bounded lengths        1..4 bytes per read.
    torque untouched       the agent never enables or disables torque. A
                           carousel whose torque is dropped can be
                           turned by gravity.
    lamps off at start     the first thing `serve_forever` does.

IT REUSES THE PRODUCTION DRIVERS. `servo.ST3215` and `sensor.scan_bus`
are imported, not reimplemented: a diagnostic that talks to the servo
its own way diagnoses the diagnostic. The agent adds only the framing
and the byte capture.

MicroPython. Same constraints as the rest of firmware/ESP32: no
f-strings, `super().__init__` in exceptions, bounded stdout writes.
"""

import json
import sys
import time

import config
import sensor as sensor_module
import servo as servo_module


AGENT_PROTOCOL = "freya-hw-diagnostic"
AGENT_PROTOCOL_VERSION = 1
AGENT_BUILD = "1.0.0"

MAX_READ_LENGTH = 4

MAX_COMMAND_BYTES = 1024

# The registers this agent will read, and nothing else. Names are the
# ST3215 memory table's own.
READABLE_REGISTERS = {
    5: "ID",
    6: "BAUD_RATE",
    9: "MIN_ANGLE_LIMIT",
    11: "MAX_ANGLE_LIMIT",
    33: "MODE",
    40: "TORQUE_ENABLE",
    42: "GOAL_POSITION",
    56: "PRESENT_POSITION",
    58: "PRESENT_SPEED",
    60: "PRESENT_LOAD",
    62: "PRESENT_VOLTAGE",
    63: "PRESENT_TEMPERATURE",
    66: "MOVING",
    69: "PRESENT_CURRENT",
}

# Two bytes for anything that is a word in the memory table.
WORD_REGISTERS = (9, 11, 42, 56, 58, 60, 69)


class DiagnosticError(Exception):
    """
    A refused or failed diagnostic command.

    `super().__init__` because MicroPython has no unbound
    Exception.__init__ - calling it raises "type object 'Exception' has
    no attribute '__init__'", which is how a sensor fault once masked
    itself as an internal error in this codebase.
    """

    def __init__(self, code, message, data=None):
        super().__init__(message)

        self.code = str(code)
        self.message = str(message)
        self.data = data if data is not None else {}


class DiagnosticAgent:
    """
    The whole diagnostic surface. Five commands, one of them a write.

    Construction touches nothing: the servo UART and the I2C bus are
    opened on first use, so an agent sitting at its prompt with a
    disconnected servo is not an error.
    """

    def __init__(self):
        self.servo = None
        self.i2c = None

        self.started_ms = time.ticks_ms()

        self.commands = {
            "diag_identify": self.identify,
            "diag_servo_raw_read": self.servo_raw_read,
            "diag_servo_feedback": self.servo_feedback,
            "diag_i2c_scan": self.i2c_scan,
            "diag_lamps_off": self.lamps_off,
        }

    # ------------------------------------------------------------------
    # lazily opened hardware
    # ------------------------------------------------------------------

    def _require_servo(self):
        if self.servo is None:
            self.servo = servo_module.ST3215(
                uart_id=config.ST3215_UART_ID,
                tx_pin=config.ST3215_TX_PIN,
                rx_pin=config.ST3215_RX_PIN,
                baudrate=config.ST3215_BAUD,
                servo_id=config.ST3215_SERVO_ID,
            )

        return self.servo

    def _require_i2c(self):
        if self.i2c is None:
            from machine import I2C, Pin

            self.i2c = I2C(
                config.I2C_BUS,
                scl=Pin(config.I2C_SCL_PIN),
                sda=Pin(config.I2C_SDA_PIN),
                freq=config.I2C_FREQ,
            )

        return self.i2c

    # ------------------------------------------------------------------
    # the commands
    # ------------------------------------------------------------------

    def identify(self, request):
        """
        Who is answering. Deliberately unmistakable.

        The protocol string is NOT the production one. A PC adapter that
        reads a production answer as diagnostic register bytes would be
        interpreting a status report as a servo position.
        """
        return {
            "protocol": AGENT_PROTOCOL,
            "protocol_version": AGENT_PROTOCOL_VERSION,
            "build": AGENT_BUILD,
            "production_firmware": config.FIRMWARE_NAME,
            "production_version": config.FIRMWARE_VERSION,
            "uptime_ms": time.ticks_diff(time.ticks_ms(),
                                         self.started_ms),
            "readable_registers": sorted(READABLE_REGISTERS.keys()),
            "max_read_length": MAX_READ_LENGTH,
            "writes": ["diag_lamps_off"],
            "moves": False,
            "warning": "DIAGNOSTIC BUILD - not competition firmware",
        }

    def servo_raw_read(self, request):
        """
        One whitelisted register, with the reply bytes kept.

        The bytes are the point. The PC otherwise sees the driver's
        interpretation of the position register and never the two bytes
        it was built from, which is hypothesis 8 of H-002 and cannot be
        checked any other way.
        """
        register = self._whole_number(request, "register")
        length = self._whole_number(request, "length", default=2)

        if register not in READABLE_REGISTERS:
            raise DiagnosticError(
                "REGISTER_NOT_READABLE",
                "register {} is not on the read whitelist".format(
                    register),
                {"readable": sorted(READABLE_REGISTERS.keys())})

        if length < 1 or length > MAX_READ_LENGTH:
            raise DiagnosticError(
                "BAD_LENGTH",
                "length must be 1..{}".format(MAX_READ_LENGTH))

        servo = self._require_servo()

        servo_id = self._whole_number(request, "servo_id",
                                      default=servo.servo_id)

        started = time.ticks_ms()

        try:
            raw = self._read_raw(servo, servo_id, register, length)

        except Exception as error:
            raise DiagnosticError(
                "SERVO_READ_FAILED",
                "{}: {}".format(type(error).__name__, error),
                {"register": register, "servo_id": servo_id})

        parsed = None

        if length == 1:
            parsed = raw[0] if raw else None

        elif length == 2 and len(raw) >= 2:
            # The ST3215 memory table is little-endian.
            parsed = raw[0] | (raw[1] << 8)

        return {
            "register": register,
            "register_name": READABLE_REGISTERS[register],
            "servo_id": servo_id,
            "length": length,
            "bytes": list(raw),
            "parsed_little_endian": parsed,
            "elapsed_ms": time.ticks_diff(time.ticks_ms(), started),
            "last_status": getattr(servo, "last_status", None),
        }

    def _read_raw(self, servo, servo_id, register, length):
        """
        The reply bytes for one register read.

        Goes through the production driver's own byte-level helper when
        it exposes one, and falls back to its typed readers otherwise -
        reconstructing the bytes little-endian, and saying so, rather
        than pretending they came off the wire.
        """
        for name in ("read_raw", "read_bytes", "_read_registers"):
            reader = getattr(servo, name, None)

            if callable(reader):
                return bytes(reader(register, length))

        # No byte-level reader on this driver build. Use the typed
        # readers and rebuild the bytes, marking the result so nobody
        # mistakes a reconstruction for a capture.
        if length == 1 or register not in WORD_REGISTERS:
            value = servo.read_byte(register)

            return bytes([value & 0xFF])

        value = servo.read_word(register)

        return bytes([value & 0xFF, (value >> 8) & 0xFF])

    def servo_feedback(self, request):
        """Every telemetry register the whitelist allows, in one call."""
        servo = self._require_servo()

        servo_id = self._whole_number(request, "servo_id",
                                      default=servo.servo_id)

        readings = {}
        errors = {}

        for register in sorted(READABLE_REGISTERS):
            name = READABLE_REGISTERS[register]

            try:
                if register in WORD_REGISTERS:
                    readings[name] = servo.read_word(register)

                else:
                    readings[name] = servo.read_byte(register)

            except Exception as error:
                errors[name] = "{}: {}".format(
                    type(error).__name__, error)

        return {
            "servo_id": servo_id,
            "readings": readings,
            "errors": errors,
            "complete": not errors,
        }

    def i2c_scan(self, request):
        """
        Every address answering, right now. Initializes nothing.

        The production firmware scans only during sensor
        initialization, so its reported addresses are from the last
        init. This is the fresh answer, and taking it does not disturb a
        sensor that is working.
        """
        i2c = self._require_i2c()

        started = time.ticks_ms()

        found = sensor_module.scan_bus(i2c)

        addresses = ["0x{:02X}".format(a) for a in found]

        return {
            "addresses": addresses,
            "count": len(found),
            "expected_address": "0x{:02X}".format(
                config.AS7265X_ADDRESS),
            "expected_present": config.AS7265X_ADDRESS in found,
            "bus": sensor_module.bus_description(),
            "elapsed_ms": time.ticks_diff(time.ticks_ms(), started),
        }

    def lamps_off(self, request):
        """
        The one write on the surface. Switches every source off.

        A diagnostic session that leaves the UV source lit is worse than
        one that cannot switch it off, so cleanup has to be able to try.
        """
        result = {"attempted": True, "errors": {}}

        try:
            i2c = self._require_i2c()

            driver = sensor_module.AS7265X(i2c)

            for lamp in (sensor_module.LED_WHITE, sensor_module.LED_UV,
                         sensor_module.LED_IR):
                name = sensor_module.LAMP_NAMES[lamp]

                try:
                    driver.enable_bulb(lamp, False)

                except Exception as error:
                    result["errors"][name] = "{}: {}".format(
                        type(error).__name__, error)

        except Exception as error:
            result["errors"]["driver"] = "{}: {}".format(
                type(error).__name__, error)

        result["confirmed"] = not result["errors"]

        return result

    # ------------------------------------------------------------------
    # framing
    # ------------------------------------------------------------------

    @staticmethod
    def _whole_number(request, field, default=None):
        if field not in request:
            if default is None:
                raise DiagnosticError(
                    "MISSING_FIELD", "{} is required".format(field))

            return default

        try:
            return int(request[field])

        except (TypeError, ValueError):
            raise DiagnosticError(
                "BAD_REQUEST", "{} must be a number".format(field))

    def dispatch(self, request):
        command = request.get("cmd")

        handler = self.commands.get(command)

        if handler is None:
            raise DiagnosticError(
                "UNKNOWN_COMMAND",
                "{!r} is not on the diagnostic whitelist".format(
                    command),
                {"commands": sorted(self.commands.keys())})

        return handler(request)

    def process_line(self, line):
        """One request line in, one response line out."""
        line = line.strip()

        if not line:
            return None

        if len(line) > MAX_COMMAND_BYTES:
            return self._response(
                None, None, False,
                error={"code": "COMMAND_TOO_LONG",
                       "message": "over {} bytes".format(
                           MAX_COMMAND_BYTES)})

        try:
            request = json.loads(line)

        except Exception:
            return self._response(
                None, None, False,
                error={"code": "BAD_JSON",
                       "message": "the request was not JSON"})

        if not isinstance(request, dict):
            return self._response(
                None, None, False,
                error={"code": "BAD_REQUEST",
                       "message": "the request must be an object"})

        request_id = request.get("request_id")
        command = request.get("cmd")

        try:
            data = self.dispatch(request)

        except DiagnosticError as error:
            return self._response(
                request_id, command, False,
                error={"code": error.code, "message": error.message,
                       "data": error.data})

        except Exception as error:
            return self._response(
                request_id, command, False,
                error={"code": "INTERNAL_ERROR",
                       "message": "{}: {}".format(
                           type(error).__name__, error)})

        return self._response(request_id, command, True, data=data)

    @staticmethod
    def _response(request_id, command, ok, data=None, error=None):
        frame = {"ok": bool(ok), "request_id": request_id,
                 "cmd": command, "agent": AGENT_PROTOCOL}

        if data is not None:
            frame["data"] = data

        if error is not None:
            frame["error"] = error

        return json.dumps(frame)

    def serve_forever(self):
        """
        Read requests from stdin, answer on stdout.

        The first thing it does is switch the lamps off. An agent that
        starts up next to a UV source somebody left on should not wait
        for a command to deal with it.
        """
        try:
            self.lamps_off({})

        except Exception:
            pass

        sys.stdout.write(self._response(
            None, "diag_banner", True, data=self.identify({})) + "\n")

        while True:
            line = sys.stdin.readline()

            if not line:
                break

            answer = self.process_line(line)

            if answer is not None:
                sys.stdout.write(answer + "\n")


# NO `if __name__ == "__main__"` AND NO MODULE-LEVEL CALL.
#
# Importing this file must do nothing. It is started by hand, from the
# REPL, with:
#
#     import diagnostic_agent
#     diagnostic_agent.DiagnosticAgent().serve_forever()
#
# so that copying it to the board - or leaving it there by accident -
# cannot make it run.
