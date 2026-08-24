"""
Reading things from the operator, and the small formatting helpers.

Every question the application asks goes through here, so a prompt
behaves the same everywhere: blank means "no answer" rather than a
guessed default, a range is enforced at the point of entry, and no
caller has to write its own int() parsing.

This is the ONLY module in the project that calls input(). The ESP32
never does - a firmware that stops to ask a question is a firmware the
main computer cannot drive.
"""

RULE = "=" * 60

import json
import sys
import textwrap


def ask(prompt, default=""):
    try:
        answer = input("{}: ".format(prompt)).strip()

    except EOFError:
        return default

    return answer or default


def choose(prompt="Select"):
    """Menu input, normalized. A bare Enter is not an unknown option."""
    return ask(prompt).strip().lower()


def ask_int(prompt, minimum=None, maximum=None, default=None):
    """
    Ask for a whole number; blank cancels unless there is a default.

    Returns None only when the operator deliberately cancels, and every
    caller must say something when that happens - silently dropping back
    to the menu is what made "Measure Sample" look like it did nothing.
    """
    while True:
        if default is not None:
            raw = ask("{} [{}]".format(prompt, default))

            if not raw:
                return default

        else:
            raw = ask("{} (blank = cancel)".format(prompt))

            if not raw:
                return None

        try:
            value = int(raw)

        except ValueError:
            print("Enter a whole number.")

            continue

        if minimum is not None and value < minimum:
            print("Minimum is {}.".format(minimum))

            continue

        if maximum is not None and value > maximum:
            print("Maximum is {}.".format(maximum))

            continue

        return value


def ask_float(prompt, minimum=None, maximum=None):
    while True:
        raw = ask("{} (blank = cancel)".format(prompt))

        if not raw:
            return None

        try:
            value = float(raw.replace(",", "."))

        except ValueError:
            print("Enter a number, for example 2.5 or -1.")

            continue

        # NaN AND INFINITY, BEFORE THE RANGE CHECK.
        #
        # `float("nan")` succeeds, and NaN then compares False against
        # EVERY bound - `nan < -15.0` and `nan > 15.0` are both False -
        # so it walked through both guards below and was returned as a
        # valid angle. From here it reaches json.dumps, which writes a
        # bare `NaN` that is not legal JSON, and then a servo goal.
        #
        # Infinity happens to be caught by the range check; it is
        # refused here anyway, because relying on a bound to reject a
        # value that is not a number is relying on an accident.
        if value != value or value in (float("inf"), float("-inf")):
            print("Enter a real number. NaN and infinity are not "
                  "positions a mechanism can be moved to.")

            continue

        if minimum is not None and value < minimum:
            print("Minimum is {}.".format(minimum))

            continue

        if maximum is not None and value > maximum:
            print("Maximum is {}.".format(maximum))

            continue

        return value


def confirm(prompt):
    """Explicit yes only. Anything else, including a bare Enter, is no."""
    return ask("{} [y/N]".format(prompt)).strip().lower().startswith("y")


def pause():
    ask("Press Enter to continue")


def banner(title):
    print()
    print(RULE)
    print(" {}".format(title))
    print(RULE)
    print()


def score(value):
    return "{:.2f} %".format(value) if isinstance(value, (int, float)) else "-"


def number(value, digits=4):
    return "{:.{}f}".format(value, digits) if isinstance(
        value, (int, float)
    ) else "-"
