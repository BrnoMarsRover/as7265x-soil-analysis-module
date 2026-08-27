"""
A deterministic fake of the test-side diagnostic agent. FRAMEWORK-ONLY.

WHY THE AGENT NEEDS ITS OWN FAKE

`diagnostic_agent.py` runs on the ESP32 and imports `machine`, `config`,
`sensor` and `servo`. It cannot be imported on CPython at all, so its
behaviour has to be exercised some other way before anybody flashes it -
and "we will find out when we deploy it" is not a plan for a build whose
whole justification is that it is safe.

So this fake speaks the agent's protocol from the PC side. It is not the
agent: it is a second implementation of the same contract, which is
exactly what makes it useful for testing the ADAPTER. The agent's own
safety properties - the register whitelist, the length bound, the
absence of any movement command - are checked separately by parsing the
agent's source, in `test_diagnostic.py`.

NOTHING HERE RUNS ON HARDWARE. Like every other fake in this folder, it
exists so that the day the agent is deployed, nothing on the PC side is
new.
"""

from ..adapters.diagnostic import (AGENT_PROTOCOL,
                                   AGENT_PROTOCOL_VERSION,
                                   MAX_READ_LENGTH,
                                   READABLE_REGISTERS)
from .fake_link import FakeError


AGENT_BUILD = "1.0.0-fake"

# The registers the fake will answer, and with what. Little-endian, as
# the ST3215 memory table is.
DEFAULT_REGISTERS = {
    5: [1],                       # ID
    6: [0],                       # BAUD_RATE code
    9: [0, 0],                    # MIN_ANGLE_LIMIT
    11: [0, 0],                   # MAX_ANGLE_LIMIT
    33: [3],                      # MODE - STEP
    40: [1],                      # TORQUE_ENABLE
    42: [0, 4],                   # GOAL_POSITION  = 1024
    56: [0, 4],                   # PRESENT_POSITION = 1024
    58: [0, 0],                   # PRESENT_SPEED
    60: [10, 0],                  # PRESENT_LOAD
    62: [120],                    # PRESENT_VOLTAGE (12.0 V)
    63: [31],                     # PRESENT_TEMPERATURE
    66: [0],                      # MOVING
    69: [5, 0],                   # PRESENT_CURRENT
}


def diagnostic_script(registers=None, addresses=("0x49",),
                      protocol=AGENT_PROTOCOL,
                      protocol_version=AGENT_PROTOCOL_VERSION,
                      moves=False):
    """
    A fake agent, scriptable in the ways that matter.

    `protocol`, `protocol_version` and `moves` are parameters so the
    tests can build the three things that must be REFUSED: a production
    firmware answering, a version mismatch, and an agent that admits it
    can move.
    """
    state = dict(DEFAULT_REGISTERS)

    if registers:
        state.update(registers)

    def identify(_payload):
        return {
            "protocol": protocol,
            "protocol_version": protocol_version,
            "build": AGENT_BUILD,
            "production_firmware": "freya-science-module",
            "production_version": "6.0.0",
            "uptime_ms": 1234,
            "readable_registers": sorted(READABLE_REGISTERS),
            "max_read_length": MAX_READ_LENGTH,
            "writes": ["diag_lamps_off"],
            "moves": moves,
            "warning": "DIAGNOSTIC BUILD - not competition firmware",
        }

    def servo_raw_read(payload):
        register = int(payload.get("register", -1))
        length = int(payload.get("length", 2))

        # The fake enforces the SAME bounds as the agent. A fake that is
        # more permissive than the thing it stands in for would let a
        # test pass that the real agent would refuse.
        if register not in READABLE_REGISTERS:
            raise FakeError(
                "REGISTER_NOT_READABLE",
                "register {} is not on the read whitelist".format(
                    register),
                {"readable": sorted(READABLE_REGISTERS)})

        if not 1 <= length <= MAX_READ_LENGTH:
            raise FakeError(
                "BAD_LENGTH",
                "length must be 1..{}".format(MAX_READ_LENGTH))

        raw = list(state.get(register, [0] * length))[:length]

        while len(raw) < length:
            raw.append(0)

        parsed = None

        if length == 1:
            parsed = raw[0]

        elif length == 2:
            parsed = raw[0] | (raw[1] << 8)

        return {
            "register": register,
            "register_name": READABLE_REGISTERS[register],
            "servo_id": int(payload.get("servo_id", 1)),
            "length": length,
            "bytes": raw,
            "parsed_little_endian": parsed,
            "elapsed_ms": 3,
            "last_status": 0,
        }

    def servo_feedback(payload):
        readings = {}

        for register, name in sorted(READABLE_REGISTERS.items()):
            raw = state.get(register, [0])

            readings[name] = (raw[0] | (raw[1] << 8)
                              if len(raw) >= 2 else raw[0])

        return {
            "servo_id": int(payload.get("servo_id", 1)),
            "readings": readings,
            "errors": {},
            "complete": True,
        }

    def i2c_scan(_payload):
        found = list(addresses)

        return {
            "addresses": found,
            "count": len(found),
            "expected_address": "0x49",
            "expected_present": "0x49" in found,
            "bus": {"bus": 0, "sda": "GPIO21", "scl": "GPIO22",
                    "frequency_hz": 100000,
                    "expected_address": "0x49"},
            "elapsed_ms": 7,
        }

    def lamps_off(_payload):
        return {"attempted": True, "errors": {}, "confirmed": True}

    return {
        "diag_identify": identify,
        "diag_servo_raw_read": servo_raw_read,
        "diag_servo_feedback": servo_feedback,
        "diag_i2c_scan": i2c_scan,
        "diag_lamps_off": lamps_off,
    }


def deployed_profile_section(version=AGENT_BUILD):
    """The profile section that marks the agent as deployed."""
    return {
        "deployed": True,
        "version": version,
        "sha256": "0" * 64,
        "deployed_utc": "2026-08-27T00:00:00Z",
        "production_firmware_sha256": "1" * 64,
    }
