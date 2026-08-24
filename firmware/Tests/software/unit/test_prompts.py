"""
The operator's keyboard, abused.

WHY THIS IS A WHOLE SUITE

`workflow/prompts.py` is the only module in the project that calls
`input()`. Every number the operator ever types - a slot, an angle, a
repeat count, a confirmation - passes through five small functions
here. If any of them can be made to return something the caller does
not expect, the damage lands somewhere far away: a slot index used to
address a carousel, an angle handed to a servo, a "yes" inferred from
something that was not one.

So they are tested with everything a human at a terminal can produce:
nothing, whitespace, the wrong type of number, a number far outside
the mechanism, a comma instead of a point, unicode, four thousand
characters, and the terminal closing mid-question.

THE RULES BEING ENFORCED

    blank means "no answer", never a guessed default
    a range is enforced where the value is read, not where it is used
    cancelling is distinguishable from answering zero
    only an explicit yes is a yes
    EOF is an answer, not a crash
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

from workflow import prompts                                # noqa: E402

from fakes import run_screen                                # noqa: E402

checks = support.Checks("prompts")


def answered(script, call, exhausted="EOF"):
    value, _output, console = run_screen(script, call, exhausted=exhausted)

    return value, console


# ======================================================================
checks.section("ask: blank is not a guess")

value, _c = answered([""], lambda: prompts.ask("Name"))
checks.equal(value, "", "a bare Enter returns empty, not a default")

value, _c = answered([""], lambda: prompts.ask("Name", default="X"))
checks.equal(value, "X", "unless a default was named explicitly")

value, _c = answered(["   spaced   "], lambda: prompts.ask("Name"))
checks.equal(value, "spaced", "surrounding whitespace is stripped")

value, _c = answered(["\t\t"], lambda: prompts.ask("Name", default="X"))
checks.equal(value, "X",
             "whitespace-only is blank, not a name made of tabs")

value, _c = answered([], lambda: prompts.ask("Name", default="D"))
checks.equal(value, "D",
             "EOF - the terminal closing mid-question - returns the "
             "default instead of crashing the application")


# ======================================================================
checks.section("choose: a menu selection is normalized")

for typed, expected in (("1", "1"), (" 1 ", "1"), ("Q", "q"),
                        ("C", "c"), ("", ""), ("  ", "")):
    value, _c = answered([typed], prompts.choose)
    checks.equal(value, expected,
                 "choose({}) -> {}".format(ascii(typed), ascii(expected)))

value, _c = answered(["рус"], prompts.choose)
checks.ok(isinstance(value, str),
          "a non-ASCII selection is returned as a string, not an error")


# ======================================================================
checks.section("ask_int: the range is enforced at the keyboard")

CANCEL = object()


def as_int(script, **kwargs):
    value, console = answered(script, lambda: prompts.ask_int(
        "Slot", **kwargs))

    return value, console


value, _c = as_int(["3"], minimum=1, maximum=4)
checks.equal(value, 3, "a valid number comes back")

value, _c = as_int([""], minimum=1, maximum=4)
checks.equal(value, None,
             "blank CANCELS and returns None - distinguishable from the "
             "operator typing zero, which matters when the value is a "
             "slot index")

value, _c = as_int([""], minimum=1, maximum=4, default=2)
checks.equal(value, 2, "unless a default was offered")

value, console = as_int(["0", "9", "2"], minimum=1, maximum=4)
checks.equal(value, 2, "out-of-range values are re-asked, not clamped")
checks.equal(console.calls, 3,
             "and the operator was asked once per bad value")

value, console = as_int(["-1", "1"], minimum=1, maximum=4)
checks.equal(value, 1, "a negative slot is refused")

value, console = as_int(["999999999999999999999", "1"], minimum=1,
                        maximum=4)
checks.equal(value, 1,
             "a number far larger than any machine word is refused by "
             "range, not by overflow")

value, console = as_int(["2.5", "2"], minimum=1, maximum=4)
checks.equal(value, 2,
             "a float where a whole number is required is refused - "
             "int('2.5') raises, and silently truncating would move the "
             "carousel to a slot the operator did not choose")

for junk in ("abc", "1a", "--", "0x2", "1e3", "①"):
    value, _c = as_int([junk, "1"], minimum=1, maximum=4)
    checks.equal(value, 1, "{} is refused and re-asked".format(ascii(junk)))

# A space is not junk, it is a blank answer: ask() strips before
# returning, so " " and "" are the same thing to every caller. Worth
# stating, because "the operator pressed space then Enter" cancelling
# rather than erroring is a deliberate choice.
value, _c = as_int([" "], minimum=1, maximum=4)
checks.equal(value, None,
             "a whitespace-only answer is a BLANK answer, so it cancels "
             "rather than being re-asked")

value, _c = as_int(["a" * 4000, "1"], minimum=1, maximum=4)
checks.equal(value, 1, "a 4000-character answer is refused, not stored")

value, _c = as_int([], minimum=1, maximum=4)
checks.equal(value, None, "EOF cancels")

value, _c = as_int([], minimum=1, maximum=4, default=3)
checks.equal(value, 3, "EOF with a default takes the default")

value, _c = as_int(["4"], minimum=1, maximum=4)
checks.equal(value, 4, "the maximum itself is inside the range")

value, _c = as_int(["1"], minimum=1, maximum=4)
checks.equal(value, 1, "and so is the minimum")


# ======================================================================
checks.section("ask_float: angles, where the sign matters")


def as_float(script, **kwargs):
    value, console = answered(script, lambda: prompts.ask_float(
        "Degrees", **kwargs))

    return value, console


value, _c = as_float(["2.5"], minimum=-15.0, maximum=15.0)
checks.close(value, 2.5, "a decimal point works")

value, _c = as_float(["2,5"], minimum=-15.0, maximum=15.0)
checks.close(value, 2.5,
             "and so does a comma - a Czech or German keyboard produces "
             "one, and refusing it would look like the program is broken")

value, _c = as_float(["-3.5"], minimum=-15.0, maximum=15.0)
checks.close(value, -3.5,
             "a negative angle is a direction, not an error")

value, _c = as_float([""], minimum=-15.0, maximum=15.0)
checks.equal(value, None, "blank cancels")

value, _c = as_float(["0"], minimum=-15.0, maximum=15.0)
checks.equal(value, 0.0,
             "and zero is a real answer, NOT a cancellation - `if not "
             "value` on this return would silently swallow it")

value, console = as_float(["99", "-99", "1"], minimum=-15.0, maximum=15.0)
checks.close(value, 1.0,
             "an angle outside the mechanism's range is re-asked")
checks.equal(console.calls, 3, "once per attempt")

for junk in ("abc", "1.2.3", "--5", "nan", "inf", "-inf", "1e999"):
    value, _c = as_float([junk, "1"], minimum=-15.0, maximum=15.0)
    checks.close(value, 1.0,
                 "{} does not become an angle".format(ascii(junk)))

# nan and inf deserve their own statement: float() accepts both, and a
# NaN passes EVERY comparison, so a range check written as
# `value < minimum` lets it straight through to the servo.
value, _c = as_float(["nan", "2"], minimum=-15.0, maximum=15.0)
checks.close(value, 2.0,
             "NaN in particular is refused - it compares False against "
             "every bound, so a naive range check would pass it to the "
             "servo as a goal position")

value, _c = as_float(["inf", "2"], minimum=-15.0, maximum=15.0)
checks.close(value, 2.0, "and so is infinity")

value, _c = as_float([], minimum=-15.0, maximum=15.0)
checks.equal(value, None, "EOF cancels")


# ======================================================================
checks.section("confirm: only yes is yes")

for typed, expected in (
    ("y", True), ("Y", True), ("yes", True), ("YES", True),
    ("", False), (" ", False), ("n", False), ("no", False),
    ("maybe", False), ("1", False), ("ok", False), ("sure", False),
    ("да", False),
):
    value, _c = answered([typed], lambda: prompts.confirm("Move?"))
    checks.equal(value, expected,
                 "confirm({}) is {}".format(ascii(typed), expected))

value, _c = answered([], lambda: prompts.confirm("Move?"))
checks.equal(value, False,
             "EOF is NO - a terminal that closed has not agreed to turn "
             "a carousel")


# ======================================================================
checks.section("formatting helpers never raise on bad input")

for value in (None, "", "abc", [], {}, object(), float("nan"),
              float("inf"), -0.0, 10 ** 30):
    try:
        text = prompts.number(value)
        crashed = None

    except Exception as error:                         # noqa: BLE001
        text = None
        crashed = type(error).__name__

    checks.ok(crashed is None,
              "number({:.30}) formats instead of raising".format(ascii(value)))
    checks.ok(isinstance(text, str), "and returns a string")

for value in (None, "", "abc", [], float("nan")):
    try:
        text = prompts.score(value)
        crashed = None

    except Exception as error:                         # noqa: BLE001
        text = None
        crashed = type(error).__name__

    checks.ok(crashed is None,
              "score({:.20}) formats instead of raising".format(ascii(value)))

checks.equal(prompts.number(None), "-",
             "a missing number prints as a dash, not as 'None' - the "
             "difference between 'not measured' and 'measured as None' "
             "is the whole point")
checks.equal(prompts.score(None), "-", "and so does a missing score")


# ======================================================================
checks.section("prompts is still the only module that reads the keyboard")

# A second input() anywhere would be a screen this suite cannot drive,
# and therefore a screen nothing tests.

# Parsed, not grepped. A substring search reports the workflow
# package's own docstring - "the only input() in the project" - as a
# caller, which is the kind of false positive that gets a useful check
# deleted. Only an actual Call to the bare name counts.
import ast                                                   # noqa: E402

callers = []

for path in sorted((support.FIRMWARE).rglob("*.py")):
    if "__pycache__" in path.parts:
        continue

    if path.relative_to(support.FIRMWARE).parts[0] == "Tests":
        continue

    if path.name == "prompts.py":
        continue

    tree = ast.parse(path.read_text(encoding="utf-8-sig"),
                     filename=str(path))

    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in ("input", "raw_input")):
            callers.append("{}:{}".format(
                path.relative_to(support.FIRMWARE).as_posix(), node.lineno))

checks.equal(callers, [],
             "no module outside prompts.py calls input() - one keyboard "
             "means every screen can be driven by a test")


# ======================================================================
checks.section("and the link refuses to put NaN on the wire either")

# Defence in depth. The keyboard is not the only way a non-finite float
# can reach a payload - an arithmetic result can be one too - and the
# one serial owner is the right place to guarantee that every frame it
# sends is legal JSON.

import serial_link                                           # noqa: E402

from fakes.serial_port import (                              # noqa: E402
    install_fake_serial,
    open_link,
)

restore = install_fake_serial(serial_link)
link, port = open_link(serial_link, None)

try:
    for bad in (float("nan"), float("inf"), float("-inf")):
        try:
            link.request("fine_adjust", degrees=bad)
            code = None

        except serial_link.LinkError as error:
            code = error.code

        except Exception as error:                     # noqa: BLE001
            code = "CRASH:" + type(error).__name__

        checks.equal(code, "INVALID_REQUEST",
                     "fine_adjust({}) is refused before it is "
                     "serialized".format(bad))

    checks.equal(len(port.written), 0,
                 "and NOT ONE BYTE went out - json.dumps would have "
                 "written a bare NaN, which no JSON parser accepts and "
                 "which MicroPython refuses on arrival")

    data = link.request("ping")
    checks.ok(data is not None,
              "and the link still works afterwards - a refused request "
              "is not a broken link")

finally:
    link.close()
    restore()


sys.exit(checks.report())
