"""
A movement whose acknowledgement was destroyed must not make the
carousel's position ambiguous.

WHY THIS SUITE EXISTS

HW-B1-009 caught one damaged frame in 200 requests on the real bench
(0.125% over 800 requests across four qualification runs). Before
letting B3 command real movements, the question had to be answered with
evidence rather than optimism:

    if the servo physically executes a relative move, and the PC then
    receives a damaged response, what does each side believe?

A relative movement is the dangerous kind. It cannot be repeated safely,
and "did it happen?" cannot be answered by asking again.

THE FOUR PROPERTIES CHECKED HERE

    1. the command is transmitted exactly once, never retried
    2. the firmware - which DID execute the move - remains the single
       authority on where the carousel is
    3. the PC holds no position of its own to become stale, so it
       cannot disagree with the firmware
    4. the operator is told the command failed, and is never shown a
       position the firmware does not stand behind

The firmware here is the REAL ESP32 code running in-process behind a
loopback wire, and `lie` replaces only the ANSWER. The move genuinely
happens and the firmware's state genuinely advances - which is exactly
the case the transport anomaly could produce.
"""

import sys
from pathlib import Path

_TESTS_DIR = next(
    p for p in Path(__file__).resolve().parents if p.name == "Tests"
)

if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

_SOFTWARE_DIR = _TESTS_DIR / "software"

if str(_SOFTWARE_DIR) not in sys.path:
    sys.path.insert(0, str(_SOFTWARE_DIR))

import support

support.add_project_root()
support.add_path("PC")

import contextlib                                            # noqa: E402
import io                                                    # noqa: E402

import serial_link                                           # noqa: E402
from serial_link import LinkError                            # noqa: E402

from fakes import FakeClock                                  # noqa: E402
from fakes.clock import install_clock                        # noqa: E402
from fakes.esp32 import MALFORMED, LoopbackDevice            # noqa: E402
from fakes.serial_port import install_fake_serial, open_link  # noqa: E402

from workflow import status as ui_status                     # noqa: E402

checks = support.Checks("damaged-motion")

restore_serial = install_fake_serial(serial_link)


def failure_of(call):
    try:
        call()

    except LinkError as error:
        return error.code, error

    return None, None


def bench(lie=None):
    """The real firmware, a real client, and a wire that can lie."""
    clock = FakeClock()
    installed = install_clock(serial_link, clock)

    servo = support.FakeST3215()
    sensor = support.FakeAS7265X()

    loopback = LoopbackDevice(lie=lie, device=servo, servo=servo)
    loopback.build()

    link, port = open_link(serial_link, loopback, clock=clock)
    link.online = True

    link.connect_servo()
    link.sync_position(load_slot=1)

    return link, loopback, installed


# ======================================================================
checks.section("the move happens, the answer is destroyed")

link, loopback, installed = bench(lie={"move_slots": MALFORMED})

try:
    before = link.get_status()
    carousel_before = before.get("carousel") or {}

    checks.ok(carousel_before.get("position_valid"),
              "the carousel starts synchronized")

    start_scan = carousel_before.get("current_scan_slot")

    code, error = failure_of(lambda: link.move_slots("cw", 1))

    checks.equal(code, "MALFORMED_RESPONSE",
                 "the damaged answer to move_slots is reported as "
                 "damage, not as a timeout - the answer HAS been and "
                 "gone")

    # PROPERTY 1: exactly once.
    sent = [name for name in loopback.seen if name == "move_slots"]

    checks.equal(len(sent), 1,
                 "THE COMMAND WAS SENT EXACTLY ONCE - a retry would "
                 "turn the carousel a second time for one instruction, "
                 "and nothing on the PC would ever learn about it")

    # PROPERTY 2: the firmware executed it and knows where it is.
    after = link.get_status()
    carousel_after = after.get("carousel") or {}

    checks.ok(carousel_after.get("position_valid"),
              "the FIRMWARE still holds a valid position - it executed "
              "the move and verified it against the encoder; only the "
              "reply was lost")

    checks.ok(carousel_after.get("current_scan_slot") != start_scan,
              "and its slot bookkeeping advanced, because the movement "
              "really happened ({} -> {})".format(
                  start_scan, carousel_after.get("current_scan_slot")))

    # PROPERTY 3: the PC keeps no position of its own.
    #
    # This is what makes the whole thing safe. The PC cannot hold a
    # stale position because it holds none: every screen reads the
    # carousel out of get_status, fresh, every time round.
    again = link.get_status()

    checks.equal((again.get("carousel") or {}).get("current_scan_slot"),
                 carousel_after.get("current_scan_slot"),
                 "a second read agrees with the first - the firmware is "
                 "the single authority and it is stable")

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("the PC caches no carousel position to go stale")

# Asserted against the SOURCE, because it is a property of the design
# rather than of one code path: Mission.hardware_status must ask the
# device every time. A cache added here later would reintroduce exactly
# the ambiguity this suite exists to rule out.
import ast                                                   # noqa: E402

session_source = (
    support.FIRMWARE / "PC" / "workflow" / "session.py"
).read_text(encoding="utf-8-sig")

tree = ast.parse(session_source)

hardware_status = None

for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == "hardware_status":
        hardware_status = node

checks.ok(hardware_status is not None,
          "Mission.hardware_status exists")

calls = [
    node.func.attr for node in ast.walk(hardware_status)
    if isinstance(node, ast.Call) and hasattr(node.func, "attr")
]

checks.ok("get_status" in calls,
          "and it asks the device, every call - the carousel position "
          "on screen is what the firmware says now, not what it said "
          "when the program started")

assigns = [
    target.attr
    for node in ast.walk(hardware_status)
    if isinstance(node, ast.Assign)
    for target in node.targets
    if hasattr(target, "attr")
]

checks.ok("carousel" not in assigns and "position" not in assigns,
          "and it stores no carousel position on the Mission, so there "
          "is nothing that CAN disagree with the firmware")


# ======================================================================
checks.section("no motion command is ever retried")

# The audit, rather than one example. `retries` defaults to 0 and only a
# caller that knows its command is a pure read may raise it; this checks
# that no command which moves anything ever does.
link_source = (
    support.FIRMWARE / "PC" / "serial_link.py"
).read_text(encoding="utf-8-sig")

link_tree = ast.parse(link_source)

MOVES_SOMETHING = {
    "move_slots", "fine_adjust", "servo_test_move", "measure_raw",
    "sync_position", "select_slot", "servo_stop", "servo_torque",
    "servo_configure", "connect_servo", "disconnect_servo",
    "clear_slot", "clear_all_slots",
    "delete_saved_sample", "delete_saved_samples",
    "led_test", "servo_bus_scan",
}

retried = []

for node in ast.walk(link_tree):
    if not isinstance(node, ast.Call):
        continue

    if getattr(node.func, "attr", None) != "request":
        continue

    if not node.args or not isinstance(node.args[0], ast.Constant):
        continue

    command = node.args[0].value

    for keyword in node.keywords:
        if keyword.arg != "retries":
            continue

        value = getattr(keyword.value, "value", None)

        if command in MOVES_SOMETHING and value:
            retried.append((command, value))

checks.equal(retried, [],
             "no command that moves, configures or destroys anything "
             "asks for retries - {} such commands audited".format(
                 len(MOVES_SOMETHING)))

# AND THE CLASSIFICATION COVERS THE WHOLE COMMAND SURFACE.
#
# MOVES_SOMETHING is a hand-written set, and a hand-written set that
# nothing checks is how a command gets added and silently treated as a
# pure read. The audit above would pass for a new motion command that
# nobody remembered to list - it would simply not be looked at.
#
# So the firmware's own dispatch table is the authority: every command
# it serves must be classified as one or the other, deliberately.
# Adding a command to the firmware now fails this suite until somebody
# says which kind it is.
firmware_source = (
    support.FIRMWARE / "ESP32" / "protocol.py"
).read_text(encoding="utf-8-sig")

firmware_tree = ast.parse(firmware_source)

firmware_commands = set()

for node in ast.walk(firmware_tree):
    if not isinstance(node, ast.Assign):
        continue

    for target in node.targets:
        if getattr(target, "id", None) != "COMMANDS":
            continue

        if isinstance(node.value, ast.Dict):
            for key in node.value.keys:
                if isinstance(key, ast.Constant):
                    firmware_commands.add(key.value)

# Pure reads, named explicitly rather than "whatever is left over".
READ_ONLY = {
    "ping", "get_status", "servo_diagnostics", "get_servo_calibration",
    "list_saved_samples", "get_saved_sample",
    # These three drive illumination and acquire, but move NOTHING and
    # write no configuration, so a repeat is a fresh acquisition rather
    # than a second physical action.
    "acquire_block", "acquire_triad", "sensor_test_raw",
}

checks.ok(bool(firmware_commands),
          "the firmware's COMMANDS table was parsed - an empty one "
          "would make every check below vacuous")

checks.equal(sorted(firmware_commands - MOVES_SOMETHING - READ_ONLY), [],
             "every command the firmware serves is classified as either "
             "mutating or read-only - a new one cannot default to being "
             "treated as safe to retry")

checks.equal(sorted((MOVES_SOMETHING | READ_ONLY) - firmware_commands), [],
             "and the classification names no command the firmware does "
             "not serve, so it cannot rot into a list of dead names")

checks.equal(sorted(MOVES_SOMETHING & READ_ONLY), [],
             "and nothing is in both halves")


# ======================================================================
checks.section("the operator is told the truth about a damaged move")

link, loopback, installed = bench(lie={"move_slots": MALFORMED})

try:
    code, error = failure_of(lambda: link.move_slots("cw", 1))

    with contextlib.redirect_stdout(io.StringIO()) as out:
        ui_status.print_failure(error.code, error.data or {},
                                message=error.message)

    printed = out.getvalue()

    checks.ok("MALFORMED_RESPONSE" in printed,
              "the failure names the transport fault")

    # THE CLAIM THAT MUST NOT BE MADE. A damaged reply says nothing
    # about the mechanism, so the screen must not say the carousel is
    # where it was.
    checks.ok("unchanged" not in printed.lower(),
              "and never claims the carousel is unchanged - a destroyed "
              "reply is not evidence that nothing moved, and here the "
              "movement DID happen")

    status = link.get_status()

    checks.equal(ui_status.carousel_label(status),
                 (status.get("carousel") or {}).get("carousel_phase"),
                 "and the phase shown afterwards is the firmware's own, "
                 "because the client re-reads it rather than "
                 "remembering")

finally:
    installed.restore()
    link.close()


restore_serial()

sys.exit(checks.report())
