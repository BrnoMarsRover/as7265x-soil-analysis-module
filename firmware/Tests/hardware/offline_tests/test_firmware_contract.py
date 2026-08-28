"""
The fakes must speak the firmware's dialect, not one of their own.

THE DEFECT THIS SUITE EXISTS FOR

`adapters/servo.py::position()` read `feedback["position"]`. The
firmware has always sent `feedback["position_counts"]`. The fake in
`offline_tests/fake_link.py` sent `"position"` too, so:

    the reader agreed with the fake
    the fake agreed with the reader
    neither agreed with the board

All 1,751 offline checks passed. On the real bench, `position()`
returned None for every read: HW-B2-004 reported `reads: 20,
answered: 0` moments after HW-B2-002 had read `position_counts=1` out
of the same block.

WHY IT MATTERS MORE THAN ONE FAILED TEST

Every encoder reading in B3 - H-002, the campaign the entire servo side
waits on - comes through `ctx.servo.position()`. A None there reads
exactly like "the servo reported no position", which is one of the
conclusions H-002 is supposed to be TESTING. The framework would have
produced confident evidence for a hypothesis it had manufactured
itself.

THE RULE BEING ENFORCED

A fake may replace a wire. It may not invent the format carried on it.
So these checks read the key names out of the REAL firmware source and
require the fakes and the adapters to use them.
"""

import ast
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

if str(HERE.parent.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent.parent))

from hardware.adapters.servo import ServoAdapter              # noqa: E402
from hardware.offline_tests import fake_link                  # noqa: E402
from hardware.offline_tests.harness import Checks, cli        # noqa: E402

# firmware/ BY NAME, never by counting parent hops.
#
# This used to walk up three parents in a row, which is the exact
# pattern that pointed FIRMWARE_DIR at Tests/ when
# hardware_validation.py moved one directory deeper. A file that is
# moved should fail loudly or keep working; it should not silently
# resolve to a different tree - and a suite whose whole purpose is to
# compare against ESP32/servo.py would then be comparing against a file
# that does not exist.
#
# test_regressions.py greps this tree for that hop chain, so the
# spelling is deliberately absent here as well as in the code.
ESP32_SERVO = next(
    p for p in HERE.parents if p.name == "firmware"
) / "ESP32" / "servo.py"


def read_feedback_keys():
    """
    The keys ST3215.read_feedback actually returns, parsed from source.

    Parsed rather than imported: ESP32/servo.py is MicroPython and
    imports `machine`, which does not exist here. The dict literal it
    returns is a fact about the file, and ast can read it without
    running anything.
    """
    tree = ast.parse(ESP32_SERVO.read_text(encoding="utf-8-sig"))

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue

        if node.name != "read_feedback":
            continue

        for statement in ast.walk(node):
            if not isinstance(statement, ast.Return):
                continue

            if not isinstance(statement.value, ast.Dict):
                continue

            return [
                key.value for key in statement.value.keys
                if isinstance(key, ast.Constant)
                and isinstance(key.value, str)
            ]

    return []


class _Link:
    """Just enough to drive ServoAdapter.diagnostics()."""

    # `module` is the production serial_link module the adapter reads
    # its timeouts from. None is enough: every lookup goes through
    # getattr(..., default).
    module = None

    def __init__(self, script):
        self.script = script

    def request(self, command, payload=None, timeout=None, retries=None):
        return {"data": self.script[command](payload or {})}


class _Context:
    """The two hooks the adapter calls while reading."""

    def __init__(self, link):
        self.link = link

    def measure(self, **_kwargs):
        pass

    def record(self, *_args, **_kwargs):
        pass


def run():
    checks = Checks("hardware/offline_tests/test_firmware_contract.py")

    # ------------------------------------------------------------------
    checks.section("the firmware's feedback contract is readable")

    firmware_keys = read_feedback_keys()

    checks.ok(bool(firmware_keys),
              "ST3215.read_feedback's returned keys were parsed from "
              "ESP32/servo.py ({} keys)".format(len(firmware_keys)))

    checks.ok("position_counts" in firmware_keys,
              "and the firmware names the encoder reading "
              "'position_counts' - the name that must appear everywhere "
              "else")

    # ------------------------------------------------------------------
    checks.section("the position reader accepts the firmware's own name")

    checks.ok("position_counts" in ServoAdapter.POSITION_KEYS,
              "ServoAdapter reads 'position_counts' - the defect was "
              "that it read only 'position', 'present_position' and "
              "'current_position', none of which the firmware sends")

    checks.equal(ServoAdapter.POSITION_KEYS[0], "position_counts",
                 "and prefers it over the legacy spellings, so a report "
                 "carrying both cannot resolve to the wrong one")

    # ------------------------------------------------------------------
    checks.section("the fake sends what the firmware sends")

    report = fake_link.healthy_script()["servo_diagnostics"]({})
    feedback = report["feedback"]

    checks.ok("position_counts" in feedback,
              "the healthy fake's feedback block carries position_counts")

    checks.ok("position" not in feedback,
              "and does NOT carry a bare 'position' - keeping it would "
              "let the reader go on passing against a key the board "
              "never sends")

    for step in report["steps"]:
        if step.get("step") == "feedback":
            checks.ok("position_counts" in step["value"],
                      "and the feedback STEP carries it too - the "
                      "adapter falls back to the step list, so a fake "
                      "that fixed only one of the two would still hide "
                      "the defect")

    # Every key the fake claims must be one the firmware really sends.
    # A fake carrying extra fields teaches tests to depend on data that
    # will not be there on the day.
    unknown = sorted(set(feedback) - set(firmware_keys))

    checks.equal(unknown, [],
                 "the fake invents no feedback field the firmware does "
                 "not send")

    # ------------------------------------------------------------------
    checks.section("and the adapter can therefore read a position")

    script = fake_link.healthy_script()
    link = _Link(script)
    adapter = ServoAdapter(_Context(link), link)

    position = adapter.position()

    checks.ok(position is not None,
              "ServoAdapter.position() returns a value against the "
              "corrected fake - it returned None against the old one, "
              "and against every real board")

    checks.equal(position, 1024,
                 "and it is the position the fake is holding, not a "
                 "coincidence")

    # THE REGRESSION, STATED AS THE BENCH SAW IT. HW-B2-004 asks for 20
    # reads and counts how many answered; with the old reader that
    # count was zero, and the check reported it as a hardware fault.
    answered = [adapter.position() for _ in range(20)]

    checks.equal([value for value in answered if value is None], [],
                 "twenty consecutive reads all answer - this is "
                 "HW-B2-004's own check, which failed 20 of 20 on the "
                 "bench for a reason that was never about the servo")

    return checks.report()


if __name__ == "__main__":
    sys.exit(cli(run, __doc__))
