"""
Asking a human something, and recording that a human answered.

WHY THIS IS NOT `input()` SCATTERED THROUGH THE TESTS

Half of what a hardware campaign needs to know cannot be read off the
serial port. Whether the carousel physically turned 180 degrees, which
way it went, whether only the WHITE bulb lit, whether the sample is
centred under the head - these are observations, and the framework's
only honest options are to ask, or to say it does not know.

FOUR RULES

    1. Every answer is timestamped and written to the evidence, in the
       operator's words, marked OPERATOR so it can never be confused
       with something a register said.

    2. Every answer is validated. "about 180ish" is not an angle, and a
       campaign that accepts it has a column of strings where it needs a
       column of numbers.

    3. In --non-interactive mode nothing is asked and nothing is
       assumed. The test becomes BLOCKED. A default answer here would be
       the framework inventing an observation, which is the worst thing
       it could possibly do.

    4. The operator can always answer ABORT. A test that has physically
       gone wrong must be stoppable at the prompt, not only with Ctrl+C.
"""

import sys

from .model import Aborted, Blocked


ABORT_WORDS = ("abort", "stop", "quit", "cancel")

DIRECTIONS = ("CW", "CCW", "NO_MOTION", "UNKNOWN")

YES = ("y", "yes", "ok", "true", "1")
NO = ("n", "no", "false", "0")


class OperatorRefused(Exception):
    """A confirmation the operator declined. Not an error - an answer."""


class Operator:
    """
    The console, wrapped so that every question is evidence.

    `interactive=False` is the CI / unattended mode: the object still
    exists and is still asked, and it refuses every question by raising
    Blocked. Nothing downstream needs a second code path.
    """

    def __init__(self, evidence=None, interactive=True,
                 stream_in=None, stream_out=None):
        self.evidence = evidence
        self.interactive = bool(interactive)

        self._in = stream_in if stream_in is not None else sys.stdin
        self._out = stream_out if stream_out is not None else sys.stdout

        self.answers = []

    # ------------------------------------------------------------------

    def _say(self, text=""):
        self._out.write(text + "\n")
        self._out.flush()

    def _ask_raw(self, prompt):
        """
        One line from the operator.

        EOF is an ABORT, never an empty answer. Ctrl+D at a hardware
        prompt means the session is over - taking it as "" would let a
        closed terminal answer a question about a physical mechanism.
        """
        self._out.write(prompt)
        self._out.flush()

        line = self._in.readline()

        if line == "":
            raise Aborted("end of input at an operator prompt")

        return line.strip()

    def _record(self, test_id, question, answer, kind):
        entry = {
            "test_id": test_id,
            "question": question,
            "answer": answer,
            "kind": kind,
            "source": "OPERATOR",
        }

        self.answers.append(entry)

        if self.evidence is not None:
            self.evidence.event("operator_answer", **entry)
            self.evidence.operator_note(test_id, question, answer, kind)

        return entry

    def _refuse(self, question):
        raise Blocked(
            "this test needs a human observation and the run is "
            "--non-interactive: {}".format(question),
            capability="operator",
            recommendation="Run this test with an operator at the bench. "
                           "Nothing may assume the answer.",
        )

    # ------------------------------------------------------------------
    # the question types
    # ------------------------------------------------------------------

    def instruct(self, test_id, instruction):
        """
        Tell the operator to do something physical, and wait.

        Used for "disconnect the USB cable now", "reconnect the sensor",
        "power cycle the module". The wait is the point: the framework
        must not send the next command until the human says the physical
        change has been made.
        """
        if not self.interactive:
            self._refuse(instruction)

        self._say()
        self._say("  ACTION REQUIRED")
        self._say("  " + instruction)

        answer = self._ask_raw(
            "  press Enter when done, or type ABORT: ")

        if answer.lower() in ABORT_WORDS:
            raise Aborted("operator aborted at: {}".format(instruction))

        return self._record(test_id, instruction, "done", "ACTION")

    def confirm(self, test_id, question, default=None):
        """
        A yes/no question. There is no implicit default at the bench.

        `default` exists only for the ordinary flow-control questions
        ("continue?"), never for a safety confirmation - `Runner` passes
        None for those, so a bare Enter re-asks instead of agreeing.
        """
        if not self.interactive:
            self._refuse(question)

        suffix = " [y/n]" if default is None else (
            " [Y/n]" if default else " [y/N]")

        while True:
            answer = self._ask_raw("  {}{}: ".format(question, suffix))
            lowered = answer.lower()

            if lowered in ABORT_WORDS:
                raise Aborted("operator aborted at: {}".format(question))

            if not answer and default is not None:
                self._record(test_id, question,
                             "yes" if default else "no", "CONFIRM")

                return bool(default)

            if lowered in YES:
                self._record(test_id, question, "yes", "CONFIRM")

                return True

            if lowered in NO:
                self._record(test_id, question, "no", "CONFIRM")

                return False

            self._say("  answer y or n, or ABORT.")

    def number(self, test_id, question, minimum=None, maximum=None,
               unit=""):
        """
        A measured quantity, e.g. an angle read off a protractor.

        Refuses anything that is not a number and anything outside the
        stated range. "UNKNOWN" is accepted and returns None - an
        operator who could not measure the angle must be able to say so,
        and None is a fact where a zero would be a fabrication.
        """
        if not self.interactive:
            self._refuse(question)

        while True:
            answer = self._ask_raw("  {}{}: ".format(
                question, " [{}]".format(unit) if unit else ""))

            lowered = answer.lower()

            if lowered in ABORT_WORDS:
                raise Aborted("operator aborted at: {}".format(question))

            if lowered in ("unknown", "?", "n/a", "na"):
                self._record(test_id, question, "UNKNOWN", "MEASUREMENT")

                return None

            try:
                value = float(answer.replace(",", "."))

            except ValueError:
                self._say("  that is not a number. Type a number, "
                          "UNKNOWN, or ABORT.")

                continue

            if minimum is not None and value < minimum:
                self._say("  below the accepted range ({}).".format(minimum))

                continue

            if maximum is not None and value > maximum:
                self._say("  above the accepted range ({}).".format(maximum))

                continue

            self._record(test_id, question, value, "MEASUREMENT")

            return value

    def choice(self, test_id, question, options):
        """One of a fixed set, e.g. the observed direction of travel."""
        if not self.interactive:
            self._refuse(question)

        upper = [str(o).upper() for o in options]

        while True:
            answer = self._ask_raw("  {} ({}): ".format(
                question, " / ".join(upper)))

            if answer.lower() in ABORT_WORDS:
                raise Aborted("operator aborted at: {}".format(question))

            if answer.upper() in upper:
                self._record(test_id, question, answer.upper(), "OBSERVATION")

                return answer.upper()

            self._say("  answer one of: {}, or ABORT.".format(
                ", ".join(upper)))

    def direction(self, test_id, question="Observed direction of travel"):
        """The H-002 question, with UNKNOWN and NO_MOTION as real answers."""
        return self.choice(test_id, question, DIRECTIONS)

    def text(self, test_id, question, allow_empty=True):
        """A free-text note, kept verbatim."""
        if not self.interactive:
            self._refuse(question)

        while True:
            answer = self._ask_raw("  {}: ".format(question))

            if answer.lower() in ABORT_WORDS:
                raise Aborted("operator aborted at: {}".format(question))

            if answer or allow_empty:
                self._record(test_id, question, answer, "NOTE")

                return answer

            self._say("  an answer is required here.")
