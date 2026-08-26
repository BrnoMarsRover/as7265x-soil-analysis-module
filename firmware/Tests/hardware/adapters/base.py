"""
What the production system can be ASKED to do, and what it cannot.

CAPABILITY DETECTION IS THE HONEST HALF OF THIS FRAMEWORK

A hardware test needs an operation - "read the servo's position
register", "scan the I2C bus on demand", "send a raw ST3215 packet and
give me the bytes back". Some of those exist in the shipped system and
some do not. The wrong response to one that does not is to add it to the
production firmware so the test goes green; the right response is to
register the test, mark it BLOCKED, and name the exact interface that is
missing.

So capabilities are detected, not assumed, and detection works WITHOUT
HARDWARE:

    the PC command surface   introspected from firmware/PC/serial_link.py
    the firmware commands    parsed out of firmware/ESP32/protocol.py
                             with `ast`, because protocol.py imports
                             sensor.py which imports `machine` and
                             cannot be imported on CPython at all

That is what lets `--list` print "BLOCKED: no raw servo register access"
on a machine with nothing plugged in.

LAZY EVERYTHING. Constructing an adapter opens no port. The transport is
created on the first call that needs it, and only in EXECUTE mode.
"""

import ast
import sys
from pathlib import Path


HARDWARE_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = HARDWARE_DIR.parent
FIRMWARE_DIR = TESTS_DIR.parent

PC_DIR = FIRMWARE_DIR / "PC"
ESP32_DIR = FIRMWARE_DIR / "ESP32"

PROTOCOL_SOURCE = ESP32_DIR / "protocol.py"


class AdapterError(Exception):
    """
    A production failure, normalized but never disguised.

    `original_type` and `original_message` are kept verbatim. A
    framework that turns `LinkError(PORT_LOST)` into "adapter error"
    has destroyed the diagnosis that the PC layer went to considerable
    trouble to produce.
    """

    def __init__(self, message, code=None, original=None, data=None):
        super().__init__(message)

        self.message = str(message)
        self.code = code
        self.data = data if data is not None else {}

        self.original = original
        self.original_type = (
            type(original).__name__ if original is not None else None)
        self.original_message = (
            str(original) if original is not None else None)

    def as_dict(self):
        return {
            "message": self.message,
            "code": self.code,
            "original_type": self.original_type,
            "original_message": self.original_message,
            "data": self.data,
        }


class Capability:
    """One thing the system can or cannot be asked to do."""

    def __init__(self, name, available, reason="", recommendation="",
                 detail=None):
        self.name = str(name)
        self.available = bool(available)
        self.reason = str(reason)
        self.recommendation = str(recommendation)
        self.detail = detail if detail is not None else {}

    def as_dict(self):
        return {
            "capability": self.name,
            "available": self.available,
            "reason": self.reason,
            "recommendation": self.recommendation,
            "detail": self.detail,
        }

    def __repr__(self):                                # pragma: no cover
        return "<Capability {} {}>".format(
            self.name, "available" if self.available else "MISSING")


# ======================================================================
# reading the production surfaces, without importing what cannot import
# ======================================================================

def load_serial_link_module():
    """
    `firmware/PC/serial_link.py`, imported the way the client imports it.

    Importing it is safe with no hardware: pyserial's import is already
    guarded in that module, and nothing at module level opens anything.
    """
    if str(PC_DIR) not in sys.path:
        sys.path.insert(0, str(PC_DIR))

    import serial_link                                  # noqa: E402

    return serial_link


def pc_command_surface():
    """Every public method `SerialLink` exposes, as a set of names."""
    serial_link = load_serial_link_module()

    return {
        name for name in dir(serial_link.SerialLink)
        if not name.startswith("_")
        and callable(getattr(serial_link.SerialLink, name, None))
    }


_FIRMWARE_COMMANDS = None


def firmware_commands():
    """
    The firmware's COMMANDS table, read with `ast`.

    NOT imported: `protocol.py` imports `sensor.py`, which does
    `from machine import I2C, Pin` at module level, and `machine` exists
    only on the ESP32. Parsing the source is the only way to know the
    real command list on a development machine, and it is exact - the
    table is a literal dict.
    """
    global _FIRMWARE_COMMANDS

    if _FIRMWARE_COMMANDS is not None:
        return _FIRMWARE_COMMANDS

    if not PROTOCOL_SOURCE.is_file():
        _FIRMWARE_COMMANDS = {}

        return _FIRMWARE_COMMANDS

    tree = ast.parse(PROTOCOL_SOURCE.read_text(encoding="utf-8"))

    found = {}

    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue

        names = [t.id for t in node.targets if isinstance(t, ast.Name)]

        if "COMMANDS" not in names:
            continue

        if not isinstance(node.value, ast.Dict):
            continue

        for key, value in zip(node.value.keys, node.value.values):
            if isinstance(key, ast.Constant) and isinstance(
                    value, ast.Constant):
                found[key.value] = value.value

    _FIRMWARE_COMMANDS = found

    return found


def firmware_has(command):
    return command in firmware_commands()


# ======================================================================
# the adapter base
# ======================================================================

class Adapter:
    """
    A production interface, wrapped so tests do not speak protocol.

    Subclasses declare `CAPABILITIES` as a mapping of capability name to
    a description of how it is provided, and implement `_detect` for
    anything that needs more than "does this command exist".
    """

    name = "adapter"

    def __init__(self, context):
        self.context = context
        self._capabilities = None

    # ------------------------------------------------------------------

    def capabilities(self):
        """Detected once, cached, and safe to call with no hardware."""
        if self._capabilities is None:
            self._capabilities = self._detect()

        return self._capabilities

    def _detect(self):                                 # pragma: no cover
        return {}

    def capability(self, name):
        return self.capabilities().get(name)

    def has(self, name):
        found = self.capability(name)

        return bool(found and found.available)

    # ------------------------------------------------------------------

    @staticmethod
    def from_commands(name, commands, recommendation, pc_surface=None,
                      pc_methods=()):
        """
        Build a Capability from "do these firmware commands exist".

        Both halves are checked: the firmware must implement the command
        AND the PC layer must expose a way to send it. A command that
        exists in the firmware but has no client method is still
        reachable through `SerialLink.request`, so that case is
        available with the fact recorded rather than blocked.
        """
        missing_firmware = [c for c in commands if not firmware_has(c)]

        missing_pc = []

        if pc_surface is not None:
            missing_pc = [m for m in pc_methods if m not in pc_surface]

        if missing_firmware:
            return Capability(
                name, False,
                reason="the firmware has no {} command{} ({})".format(
                    "" if len(missing_firmware) == 1 else "s",
                    "" if len(missing_firmware) == 1 else "s",
                    ", ".join(missing_firmware)),
                recommendation=recommendation,
                detail={"missing_firmware_commands": missing_firmware},
            )

        return Capability(
            name, True,
            reason="firmware commands present: {}".format(
                ", ".join(commands)),
            detail={
                "firmware_commands": list(commands),
                "pc_methods": list(pc_methods),
                "pc_methods_missing": missing_pc,
                "sent_via": ("SerialLink.request" if missing_pc
                             else "SerialLink method"),
            },
        )
