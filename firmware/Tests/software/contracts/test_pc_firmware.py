"""
The seam between the two computers, checked from both sides.

WHAT A "CONTRACT" IS HERE

    PC method
      -> payload keys it can send
        -> command name on the wire
          -> firmware command table
            -> handler
              -> keys the handler reads
                -> response envelope
                  -> keys the client reads back

Every arrow is a place where a rename on one side and not the other
produces a defect that no unit test sees and that only appears on the
bench, usually while somebody is holding a soil sample.

THREE KINDS OF CHECK

    mechanical   parse both sides and compare the literal strings. A
                 client that sends `slots` to a handler that reads
                 `count` is found without running anything.
    live         run every command through the REAL firmware behind the
                 REAL client and assert on the real answer.
    hostile      send each command wrong - missing fields, wrong types,
                 unknown names - and require a named refusal rather
                 than a traceback.

WHAT IS DELIBERATELY NOT ASSERTED

The full response SHAPE is not frozen here. Pinning every key of
`get_status` would make the suite fail on every honest addition, and it
would be checking the firmware against itself. What is asserted is the
envelope, plus the specific fields the PC is known to read.
"""

import ast
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

import serial_link                                          # noqa: E402
from serial_link import DeviceError, LinkError, SerialLink   # noqa: E402

from fakes import FakeClock, loopback_link                   # noqa: E402
from fakes.esp32 import LoopbackDevice                       # noqa: E402

checks = support.Checks("contracts")

FIRMWARE = support.FIRMWARE

LINK_SOURCE = FIRMWARE / "PC" / "serial_link.py"
PROTOCOL_SOURCE = FIRMWARE / "ESP32" / "protocol.py"

link_tree = ast.parse(LINK_SOURCE.read_text(encoding="utf-8"))
protocol_tree = ast.parse(PROTOCOL_SOURCE.read_text(encoding="utf-8"))


# ======================================================================
# read both sides of the seam
# ======================================================================

def client_commands():
    """
    method name -> (command string, payload keys it can send).

    Payload keys come from two places, because serial_link builds them
    two ways: keyword arguments straight into `request(...)`, and a
    `payload` dict assembled first for the optional ones.
    """
    found = {}

    for node in ast.walk(link_tree):
        if not isinstance(node, ast.FunctionDef):
            continue

        command = None
        keys = set()

        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue

            if getattr(inner.func, "attr", None) != "request":
                continue

            if not inner.args or not isinstance(inner.args[0], ast.Constant):
                continue

            command = inner.args[0].value

            for keyword in inner.keywords:
                if keyword.arg and keyword.arg not in ("timeout", "retries"):
                    keys.add(keyword.arg)

        if command is None:
            continue

        # Only the `payload` dict counts: `payload["name"] = ...` and
        # the literal it is initialised from.
        #
        # Collecting EVERY dict literal in the function instead reads
        # the `data={"port": ..., "console": ...}` that wait_online
        # attaches to a LinkError as if it were a request field, and
        # then reports the diagnostic payload as a contract mismatch.
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Subscript)
                    and getattr(inner.value, "id", None) == "payload"
                    and isinstance(inner.slice, ast.Constant)
                    and isinstance(inner.slice.value, str)):
                keys.add(inner.slice.value)

            elif (isinstance(inner, ast.Assign)
                  and any(getattr(t, "id", None) == "payload"
                          for t in inner.targets)
                  and isinstance(inner.value, ast.Dict)):
                for key in inner.value.keys:
                    if isinstance(key, ast.Constant) and isinstance(
                            key.value, str):
                        keys.add(key.value)

        found[node.name] = (command, keys)

    return found


def firmware_commands():
    """command string -> handler method name, from the COMMANDS table."""
    for node in ast.walk(protocol_tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "COMMANDS"
                for t in node.targets):
            return {
                key.value: value.value
                for key, value in zip(node.value.keys, node.value.values)
                if isinstance(key, ast.Constant)
                and isinstance(value, ast.Constant)
            }

    return {}


def handler_keys():
    """
    handler name -> request keys it reads.

    Includes the keys read by helpers the handler calls on `self`, one
    level deep: `_slot_from(request)` is where `slot` is read for four
    different commands, and a check that did not follow it would report
    four false mismatches.
    """
    direct = {}
    helper_calls = {}

    for node in ast.walk(protocol_tree):
        if not isinstance(node, ast.FunctionDef):
            continue

        keys = set()
        calls = set()

        for inner in ast.walk(node):
            if (isinstance(inner, ast.Call)
                    and getattr(inner.func, "attr", None) == "get"
                    and getattr(inner.func.value, "id", None) == "request"
                    and inner.args
                    and isinstance(inner.args[0], ast.Constant)):
                keys.add(inner.args[0].value)

            elif (isinstance(inner, ast.Compare)
                  and isinstance(inner.left, ast.Constant)
                  and isinstance(inner.left.value, str)
                  and any(isinstance(op, (ast.In, ast.NotIn))
                          for op in inner.ops)
                  and any(getattr(c, "id", None) == "request"
                          for c in inner.comparators)):
                keys.add(inner.left.value)

            elif (isinstance(inner, ast.Call)
                  and getattr(inner.func, "attr", None)
                  and getattr(inner.func, "value", None) is not None
                  and getattr(inner.func.value, "id", None) == "self"
                  and any(getattr(a, "id", None) == "request"
                          for a in inner.args)):
                calls.add(inner.func.attr)

        direct[node.name] = keys
        helper_calls[node.name] = calls

    resolved = {}

    for name, keys in direct.items():
        combined = set(keys)

        for helper in helper_calls.get(name, ()):
            combined |= direct.get(helper, set())

        resolved[name] = combined

    return resolved


CLIENT = client_commands()
TABLE = firmware_commands()
HANDLER_KEYS = handler_keys()

# Envelope fields, not payload. Every request carries them and no
# handler reads them.
ENVELOPE = {"request_id", "cmd", "timestamp"}


# ======================================================================
checks.section("the two sides were read")

checks.ok(len(CLIENT) >= 25,
          "the client's command surface was parsed ({} methods)".format(
              len(CLIENT)))
checks.ok(len(TABLE) >= 25,
          "the firmware command table was parsed ({} commands)".format(
              len(TABLE)))
checks.ok(len(HANDLER_KEYS) >= 40,
          "and every firmware handler ({} functions)".format(
              len(HANDLER_KEYS)))


# ======================================================================
checks.section("every command the client sends, the firmware answers to")

sent = {command for command, _keys in CLIENT.values()}
unknown = sorted(sent - set(TABLE))

checks.equal(unknown, [],
             "no client method sends a command the firmware has never "
             "heard of")


# ======================================================================
checks.section("every command the firmware answers to, the client can send")

# The reverse direction. A handler nothing can reach is either dead
# code on a memory-constrained device, or a command the client forgot.
orphans = sorted(set(TABLE) - sent)

checks.equal(orphans, [],
             "no firmware command is unreachable from the client")


# ======================================================================
checks.section("argument names match on both sides")

# The mechanical version of the sync_load_slot defect, one level down:
# right command, wrong argument name. The client would send it, the
# firmware would ignore it, and the movement would silently use a
# default.

mismatched = []

for method, (command, keys) in sorted(CLIENT.items()):
    handler = TABLE.get(command)

    if handler is None:
        continue

    read = HANDLER_KEYS.get(handler, set())
    sends = keys - ENVELOPE

    for key in sorted(sends - read):
        mismatched.append("{}() sends {!r} to {} which never reads it"
                          .format(method, key, handler))

checks.equal(mismatched, [],
             "every payload key the client sends is a key the handler "
             "actually reads")


# ======================================================================
checks.section("every command runs, against the real firmware")

clock = FakeClock()
loopback = LoopbackDevice()
link, port, loopback = loopback_link(serial_link, device=loopback,
                                     clock=clock)

# In dependency order: the servo has to be connected before the
# carousel will move, and the origin declared before a slot is chosen.
#
# `servo_bus_scan` sits at the END of the servo block on purpose. It
# reopens UART2 at eight baud rates, so it RELEASES a connected servo
# and invalidates the carousel position - putting it in the middle of
# the sequence made every later servo command fail with
# SERVO_NOT_CONNECTED, which is the firmware being correct and the
# test being wrong. The reconnect after it is part of the contract.
SEQUENCE = (
    ("ping", {}),
    ("get_status", {}),
    ("connect_servo", {}),
    ("get_servo_calibration", {}),
    ("servo_diagnostics", {}),
    ("servo_torque", {"enable": True}),
    ("sync_position", {"load_slot": 1}),
    ("move_slots", {"direction": "cw", "slots": 1}),
    ("fine_adjust", {"degrees": 1.0}),
    ("select_slot", {"slot": 2, "sample_id": "S-CONTRACT"}),
    ("sensor_test_raw", {}),
    ("acquire_block", {"illumination": "white", "repeats": 1}),
    ("acquire_triad", {"repeats": 1}),
    ("led_test", {"hold_ms": 1}),
    ("measure_raw", {"slot": 2, "sample_id": "S-CONTRACT"}),
    ("list_saved_samples", {}),
    ("get_saved_sample", {"sample_id": "S-CONTRACT"}),
    ("clear_slot", {"slot": 2}),
    ("clear_all_slots", {}),
    ("servo_test_move", {"kind": "degrees", "degrees": 2.0,
                         "confirm": True}),
    ("servo_configure", {"confirm": True}),
    ("servo_stop", {}),
    ("delete_saved_samples", {}),
    ("servo_bus_scan", {}),
    ("disconnect_servo", {}),
)

executed = set()

for command, payload in SEQUENCE:
    try:
        data = link.request(command, **payload)
        executed.add(command)

        checks.ok(data is None or isinstance(data, dict),
                  "{} answers with a data object".format(command))

    except (LinkError, DeviceError) as error:
        checks.ok(False, "{} answered: {} {}".format(
            command, error.code, error.message))

covered = sorted(set(TABLE) - executed)
checks.equal(covered, [],
             "every one of the {} firmware commands was executed for "
             "real, not merely named".format(len(TABLE)))


# ======================================================================
checks.section("the bus scan says what it costs")

# Found by this suite: a scan releases the servo and invalidates the
# carousel position. That is correct - two owners of one UART is not a
# thing that can be made to work - but the operator has to be told,
# and the screen used to say only "MOVES NOTHING".

link.connect_servo()
link.sync_position(load_slot=1)

before = link.get_status()
checks.ok(before["servo"]["connected"] is True
          and before["carousel"]["position_valid"] is True,
          "with a servo connected and an origin declared")

report = link.servo_bus_scan()

checks.ok(report.get("released_servo"),
          "the scan reports that it released the servo, in the response "
          "itself rather than leaving the client to infer it")

after = link.get_status()

checks.equal(after["servo"]["connected"], False,
             "and the servo really is released afterwards")
checks.equal(after["carousel"]["position_valid"], False,
             "and the carousel position is invalidated with it - a "
             "position that survived a released servo would be a "
             "remembered number with nothing behind it")

from workflow.display import print_bus_scan                  # noqa: E402
import contextlib                                            # noqa: E402
import io                                                    # noqa: E402

with contextlib.redirect_stdout(io.StringIO()) as out:
    print_bus_scan(report)

printed = out.getvalue()

checks.ok("RELEASED" in printed.upper(),
          "and the screen the operator reads says the servo was released")
checks.ok("re-declare" in printed or "re-sync" in printed.lower(),
          "and tells them the origin has to be declared again")

link.connect_servo()
link.sync_position(load_slot=1)

recovered = link.get_status()
checks.ok(recovered["carousel"]["position_valid"] is True,
          "and the session recovers by connecting and syncing again")


# ======================================================================
checks.section("the response envelope is the same for every command")

for frame in loopback.responses:
    if not isinstance(frame, dict):
        continue

    if not (set(frame) >= {"request_id", "ok", "cmd"}):
        checks.ok(False, "a response is missing an envelope field: "
                         "{}".format(sorted(frame)))

        break

else:
    checks.ok(True, "every response carries request_id, ok and cmd")

errors = [f for f in loopback.responses if not f.get("ok")]

for frame in errors:
    error = frame.get("error") or {}

    if not (error.get("code") and error.get("message")):
        checks.ok(False, "a refusal has no code or no message: "
                         "{}".format(frame))

        break

else:
    checks.ok(True, "every refusal carries both a code and a message "
                    "({} seen)".format(len(errors)))


# ======================================================================
checks.section("a command sent wrong is refused, not obeyed")

WRONG = (
    ("no_such_command", {}, "UNKNOWN_COMMAND"),
    ("select_slot", {}, None),
    ("select_slot", {"slot": 0}, None),
    ("select_slot", {"slot": 99}, None),
    ("select_slot", {"slot": "two"}, None),
    ("select_slot", {"slot": None}, None),
    ("sync_position", {}, "MISSING_FIELD"),
    ("move_slots", {"direction": "sideways"}, None),
    ("fine_adjust", {}, None),
    ("fine_adjust", {"degrees": "a lot"}, None),
    ("fine_adjust", {"degrees": 9999}, None),
    ("acquire_block", {"illumination": "gamma", "repeats": 1}, None),
    ("acquire_block", {"illumination": "white", "repeats": -5}, None),
    ("servo_test_move", {"kind": "degrees", "degrees": 5.0}, None),
    ("servo_test_move", {"kind": "nonsense", "confirm": True}, None),
    ("servo_configure", {}, None),
    ("clear_slot", {"slot": 0}, None),
    ("measure_raw", {"slot": 77}, None),
    ("get_saved_sample", {}, None),
)

for command, payload, expected in WRONG:
    try:
        link.request(command, **payload)
        refused = None

    except DeviceError as error:
        refused = error.code

    except LinkError as error:
        refused = "LINK:{}".format(error.code)

    except Exception as error:                          # noqa: BLE001
        refused = "CRASH:{}".format(type(error).__name__)

    label = "{} {}".format(command, payload or "{}")

    if refused is None:
        checks.ok(False, "{} was ACCEPTED and should not have been"
                  .format(label))

    elif refused.startswith("CRASH:"):
        checks.ok(False, "{} raised {} instead of a named refusal"
                  .format(label, refused))

    elif expected and refused != expected:
        checks.ok(False, "{} was refused as {} not {}".format(
            label, refused, expected))

    else:
        checks.ok(True, "{} is refused as {}".format(label, refused))


# ======================================================================
checks.section("the firmware survives everything it just refused")

data = link.ping()

checks.ok(data.get("pong") is True,
          "after {} malformed and invalid commands the firmware still "
          "answers a ping - one bad command must not take down the "
          "command loop".format(len(WRONG)))

status = link.get_status()

checks.ok(isinstance(status.get("carousel"), dict),
          "and still reports its state")


link.close()

sys.exit(checks.report())
