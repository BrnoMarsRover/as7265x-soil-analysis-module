"""
The operator's keyboard and screen, under test control.

`workflow/prompts.py` is the only module in the project that calls
`input()`, which is what makes this possible: replacing that one
builtin drives every screen in the application.

TWO THINGS THIS GETS RIGHT THAT A NAIVE STUB DOES NOT

    RUNNING OUT OF ANSWERS IS NOT AN ERROR. `ask()` catches EOFError
    and returns the default, which is exactly what happens when the
    operator's terminal closes. A stub that raises instead would make
    "the menu loops forever" look like a passing test.

    THE PROMPTS ARE RECORDED. What a screen ASKED is as much a part of
    its behaviour as what it printed - a screen that stops asking for
    confirmation before turning the carousel has changed in a way no
    output assertion would catch.
"""

import builtins
import contextlib
import io


class ScriptedConsole:
    """Feeds prepared answers to input(), and remembers the prompts."""

    def __init__(self, script, exhausted="EOF", loop_guard=2000):
        self.script = list(script)
        self.prompts = []
        self.exhausted = exhausted
        self.calls = 0
        self.loop_guard = loop_guard

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        self.calls += 1

        # A menu that ignores its own exit option loops forever, and an
        # infinite loop in a test suite looks like a hang rather than a
        # failure. This turns it into a diagnosable exception.
        if self.calls > self.loop_guard:
            raise RuntimeError(
                "a screen asked {} questions without finishing - it is "
                "almost certainly looping. Last prompt: {!r}".format(
                    self.calls, self.prompts[-1]))

        if self.script:
            return self.script.pop(0)

        if self.exhausted == "EOF":
            raise EOFError("the script ran out of answers")

        if self.exhausted == "QUIT":
            return "q"

        return self.exhausted

    @property
    def unused(self):
        return list(self.script)


@contextlib.contextmanager
def answers(script, exhausted="EOF"):
    """Install a scripted console for the duration of the block."""
    console = ScriptedConsole(script, exhausted=exhausted)
    original = builtins.input
    builtins.input = console

    try:
        yield console

    finally:
        builtins.input = original


def run_screen(script, call, exhausted="EOF"):
    """
    Drive one screen and collect everything about the run.

    Returns (value, output, console). Exceptions are NOT swallowed -
    a screen that raises is the finding, not something to be tidied
    into a return value.
    """
    with answers(script, exhausted=exhausted) as console:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            value = call()

    return value, out.getvalue(), console
