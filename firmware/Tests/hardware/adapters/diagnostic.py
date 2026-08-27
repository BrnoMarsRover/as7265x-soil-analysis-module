"""
The test-side diagnostic agent, from the PC side.

WHAT IT UNBLOCKS

Three tests are blocked by the competition firmware having no way to
show raw bytes or scan a bus on demand:

    HW-B2-006   raw ST3215 packet capture
    HW-B3-004   byte-order interpretation of the position register
    HW-B6-001   on-demand I2C scan

None of those could be unblocked by changing production firmware - an
unverified diagnostic command in a competition build is a worse defect
than the gap it fills. So the agent lives entirely under
`test_side_firmware/`, is deployed by hand, and is deployed only when a
diagnostic session is actually happening.

WHY THE ADAPTER EXISTS NOW, BEFORE ANY DEPLOYMENT

So that the deployment is the ONLY remaining step. The command surface,
the argument bounds, the response shapes and the safety refusals are all
implemented and exercised against a deterministic fake here. On the day
the agent is flashed, nothing on the PC side is new or untested.

THE ADAPTER REFUSES TO ASSUME THE AGENT IS THERE. `diagnostic.agent` is
available only when the bench profile records a deployment, and every
call verifies the identity handshake before doing anything else. A
production firmware that happens to answer `diag_identify` with the
wrong protocol id is treated as not the agent.
"""

from .base import Adapter, Capability


# The agent's own protocol identity. Deliberately not the production
# protocol version: a diagnostic build must be unmistakable.
AGENT_PROTOCOL = "freya-hw-diagnostic"
AGENT_PROTOCOL_VERSION = 1

# Every command the agent will answer. A whitelist, not a convention -
# see test_side_firmware/diagnostic_agent.py, which refuses anything not
# on this list.
COMMANDS = (
    "diag_identify",
    "diag_servo_raw_read",
    "diag_servo_feedback",
    "diag_i2c_scan",
    "diag_lamps_off",
)

# The ST3215 memory-table registers the agent will read. A whitelist,
# because "read any register" and "write any register" are one typo
# apart and a wrong write can change the servo id or baud rate and take
# the bus away entirely.
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

MAX_READ_LENGTH = 4


class DiagnosticAdapter(Adapter):
    """The diagnostic agent's command surface, bounded and read-only."""

    name = "diagnostic"

    def __init__(self, context, link):
        super().__init__(context)

        self.link = link

        self._identity = None

    # ------------------------------------------------------------------

    def _detect(self):
        declared = self.context.profile.diagnostic_firmware()

        deployed = bool(declared.get("deployed"))

        recommendation = (
            "Deploy the agent from "
            "firmware/Tests/hardware/test_side_firmware/ following its "
            "DEPLOYMENT.md, then record deployed/version/sha256 and the "
            "production firmware's sha256 in the bench profile under "
            "diagnostic_firmware. The agent is NOT part of the "
            "competition boot and must be removed afterwards.")

        found = {
            "diagnostic.agent": Capability(
                "diagnostic.agent", deployed,
                reason=(
                    "the profile records diagnostic agent {} "
                    "deployed".format(declared.get("version"))
                    if deployed else
                    "the test-side diagnostic agent is not deployed on "
                    "this bench"),
                recommendation="" if deployed else recommendation,
                detail={"declared": declared},
            ),
        }

        for name, purpose in (
                ("diagnostic.servo_raw",
                 "raw ST3215 request and reply bytes"),
                ("diagnostic.servo_feedback",
                 "position, speed, load, voltage, current and "
                 "temperature in one read"),
                ("diagnostic.i2c_scan",
                 "an on-demand I2C bus scan that initializes nothing")):
            found[name] = Capability(
                name, deployed,
                reason=(
                    "provided by the deployed diagnostic agent"
                    if deployed else
                    "{} needs the test-side diagnostic agent, which is "
                    "not deployed".format(purpose)),
                recommendation="" if deployed else recommendation,
            )

        return found

    # ------------------------------------------------------------------
    # the handshake
    # ------------------------------------------------------------------

    def identify(self):
        """
        Confirm the thing answering is the agent, and which build it is.

        Called before every other diagnostic command. A production
        firmware that answers at all is not the agent, and treating it
        as one would mean interpreting whatever it returned as raw
        register bytes.
        """
        transaction = self.link.request("diag_identify", retries=1)

        answer = transaction["data"] or {}

        protocol = answer.get("protocol")
        version = answer.get("protocol_version")

        if protocol != AGENT_PROTOCOL:
            from ..core.model import Blocked

            raise Blocked(
                "the device answered diag_identify with protocol {!r}, "
                "not {!r}. Whatever is running, it is not the "
                "diagnostic agent, and its answers must not be read as "
                "register bytes.".format(protocol, AGENT_PROTOCOL),
                capability="diagnostic.agent",
                recommendation="Deploy the agent, or clear "
                               "diagnostic_firmware.deployed in the "
                               "bench profile.")

        if version != AGENT_PROTOCOL_VERSION:
            from ..core.model import Blocked

            raise Blocked(
                "the deployed agent speaks protocol version {} and this "
                "adapter speaks {}".format(version,
                                           AGENT_PROTOCOL_VERSION),
                capability="diagnostic.agent",
                recommendation="Redeploy the agent from this working "
                               "tree so the two versions match.")

        self._identity = answer

        return answer

    @property
    def identity(self):
        return dict(self._identity or {})

    # ------------------------------------------------------------------
    # the bounded read-only surface
    # ------------------------------------------------------------------

    def servo_raw_read(self, register, length=2, servo_id=None):
        """
        Read one whitelisted ST3215 register, keeping the reply bytes.

        The bounds are checked HERE as well as in the agent. Two checks
        of the same rule is not redundancy when one of them runs on a
        microcontroller that may be a version behind.
        """
        self.context.require_hardware_mode("read a servo register")

        register = int(register)
        length = int(length)

        if register not in READABLE_REGISTERS:
            raise ValueError(
                "register {} is not on the diagnostic read whitelist. "
                "The agent reads {} named registers and refuses "
                "everything else, because a wrong access to the ST3215 "
                "memory table can take the bus away.".format(
                    register, len(READABLE_REGISTERS)))

        if not 1 <= length <= MAX_READ_LENGTH:
            raise ValueError(
                "length must be 1..{}, got {}".format(
                    MAX_READ_LENGTH, length))

        if self._identity is None:
            self.identify()

        payload = {"register": register, "length": length}

        if servo_id is not None:
            payload["servo_id"] = int(servo_id)

        transaction = self.link.request("diag_servo_raw_read",
                                        retries=0, **payload)

        answer = transaction["data"] or {}

        answer["register_name"] = READABLE_REGISTERS[register]

        return transaction

    def servo_feedback(self, servo_id=None):
        """Position, speed, load, voltage, current, temperature, status."""
        self.context.require_hardware_mode("read servo telemetry")

        if self._identity is None:
            self.identify()

        payload = {}

        if servo_id is not None:
            payload["servo_id"] = int(servo_id)

        return self.link.request("diag_servo_feedback", retries=1,
                                 **payload)

    def i2c_scan(self):
        """
        Every address answering on the I2C bus. Initializes nothing.

        This is the on-demand scan the production firmware does not
        have: it distinguishes "the sensor is absent" from "the bus is
        dead" without disturbing a sensor that is working.
        """
        self.context.require_hardware_mode("scan the I2C bus")

        if self._identity is None:
            self.identify()

        return self.link.request("diag_i2c_scan", retries=1)

    def lamps_off(self):
        """
        Switch every illumination source off. The one WRITE the agent has.

        It exists because a diagnostic session that leaves a UV source
        lit is worse than one that cannot switch it off, and because
        cleanup has to be able to try.
        """
        self.context.require_hardware_mode("switch the lamps off")

        return self.link.request("diag_lamps_off", retries=1)

    # ------------------------------------------------------------------

    @staticmethod
    def interpret_bytes(raw):
        """
        The plausible readings of a raw register reply.

        Diagnostic only. The ST3215 memory table is little-endian, so
        `little` is the driver's reading and the others exist to make a
        byte-swap visible to a human rather than to be believed by the
        framework.
        """
        if not raw:
            return None

        data = list(int(b) & 0xFF for b in raw)

        little = 0
        big = 0

        for index, value in enumerate(data):
            little |= value << (8 * index)
            big = (big << 8) | value

        return {
            "bytes": data,
            "hex": " ".join("{:02X}".format(b) for b in data),
            "little_endian": little,
            "big_endian": big,
            "note": "little_endian is the ST3215 memory-table order and "
                    "the one the driver uses; the rest are diagnostics, "
                    "not measurements",
        }
