"""
What a hardware test IS, and what a result MEANS.

Nothing in this file touches hardware. It defines the vocabulary the
rest of the framework speaks, and it is deliberately the first thing to
read: a campaign that cannot say precisely what "PASS" means produces
opinions, and this project already has a folder full of software
evidence that only stays trustworthy while the hardware evidence is
kept honestly separate from it.

THE ONE RULE THIS FILE ENFORCES

    PASS is reachable only from a real run, against real hardware,
    that the operator explicitly confirmed.

Every other path - listing, describing, dry-running, the framework's
own self-test on a fake transport - can produce READY_FOR_HARDWARE,
BLOCKED, SKIPPED, FAIL, ERROR or ABORTED, and carries an evidence class
that says in words that no hardware was involved. `TestResult` refuses
to be constructed with PASS and a non-hardware evidence class, so the
rule is a type error rather than a convention somebody has to remember.
"""

import time


# ======================================================================
# result states
# ======================================================================

class Status:
    """
    The eight states a hardware test can end in.

    NOT_RUN and READY_FOR_HARDWARE are both "no result yet", and the
    difference between them is the whole point of this framework:
    NOT_RUN is a test nobody has looked at, READY_FOR_HARDWARE is a test
    whose definition, prerequisites, capabilities and configuration have
    all been checked offline and which will execute the moment a board
    is plugged in.
    """

    NOT_RUN = "NOT_RUN"
    READY_FOR_HARDWARE = "READY_FOR_HARDWARE"
    SKIPPED = "SKIPPED"
    BLOCKED = "BLOCKED"
    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"
    ABORTED = "ABORTED"

    ALL = (NOT_RUN, READY_FOR_HARDWARE, SKIPPED, BLOCKED,
           PASS, FAIL, ERROR, ABORTED)

    # A campaign containing any of these is not a campaign that passed.
    BAD = (FAIL, ERROR, ABORTED)

    # States that mean "this did not run and we know why".
    UNRUN = (NOT_RUN, READY_FOR_HARDWARE, SKIPPED, BLOCKED)


class Evidence:
    """
    What kind of evidence a result is, which is not the same as its status.

    A PASS from the framework's own self-test is a statement about the
    framework. Stamping that difference onto every result, every event
    and every summary line is what stops a self-test transcript being
    quoted six months later as proof that the carousel works.
    """

    HARDWARE = "HARDWARE"
    DRY_RUN = "DRY_RUN / NO_HARDWARE_EVIDENCE"
    SELFTEST = "FRAMEWORK_SELFTEST / NO_HARDWARE_EVIDENCE"

    ALL = (HARDWARE, DRY_RUN, SELFTEST)


class Mode:
    """How the framework was invoked."""

    LIST = "LIST"
    DESCRIBE = "DESCRIBE"
    DRY_RUN = "DRY_RUN"
    EXECUTE = "EXECUTE"
    SELFTEST = "SELFTEST"

    ALL = (LIST, DESCRIBE, DRY_RUN, EXECUTE, SELFTEST)

    # The only mode in which a transport may be opened.
    TOUCHES_HARDWARE = (EXECUTE,)


EVIDENCE_FOR_MODE = {
    Mode.EXECUTE: Evidence.HARDWARE,
    Mode.DRY_RUN: Evidence.DRY_RUN,
    Mode.SELFTEST: Evidence.SELFTEST,
    Mode.LIST: Evidence.DRY_RUN,
    Mode.DESCRIBE: Evidence.DRY_RUN,
}


# ======================================================================
# safety
# ======================================================================

class Safety:
    """
    What a test can physically do, which decides what it must ask first.

    The classes are ordered by consequence, and the ordering is used:
    anything at or above MOTION needs its own confirmation on top of the
    global hardware confirmation, because "yes, the board is connected"
    and "yes, the mechanism is clear and nothing is loaded" are two
    different statements and an operator may honestly mean the first
    without having checked the second.
    """

    READ_ONLY = "READ_ONLY"
    COMMUNICATION = "COMMUNICATION"
    MOTION = "MOTION"
    ILLUMINATION = "ILLUMINATION"
    MANUAL_DISCONNECT = "MANUAL_DISCONNECT"
    RESET = "RESET"
    POWER_CYCLE = "POWER_CYCLE"
    FAULT_INJECTION = "FAULT_INJECTION"
    ENDURANCE = "ENDURANCE"
    FULL_SYSTEM = "FULL_SYSTEM"

    ALL = (READ_ONLY, COMMUNICATION, MOTION, ILLUMINATION,
           MANUAL_DISCONNECT, RESET, POWER_CYCLE, FAULT_INJECTION,
           ENDURANCE, FULL_SYSTEM)

    # Classes that need a second, specific confirmation.
    NEEDS_EXTRA_CONFIRMATION = (
        MOTION, ILLUMINATION, MANUAL_DISCONNECT, RESET, POWER_CYCLE,
        FAULT_INJECTION, ENDURANCE, FULL_SYSTEM,
    )

    # The sentence the operator has to agree with, per class. Vague
    # confirmations get vague answers, so each one names the physical
    # thing that has to be true.
    CONFIRMATION_QUESTION = {
        MOTION:
            "This test TURNS THE CAROUSEL. Confirm the mechanism is "
            "clear, no sample can be thrown out, and nothing is in the "
            "path of the carousel",
        ILLUMINATION:
            "This test SWITCHES ON the WHITE, UV or IR illumination. "
            "Confirm nobody is looking into the sensor head and that UV "
            "exposure is acceptable",
        MANUAL_DISCONNECT:
            "This test asks you to PULL A CABLE while the module is "
            "running. Confirm you are at the bench and may do that",
        RESET:
            "This test RESETS the ESP32. Confirm that losing the "
            "carousel position reference is acceptable right now",
        POWER_CYCLE:
            "This test asks you to POWER CYCLE the module. Confirm you "
            "can reach the supply and that no sample is mid-measurement",
        FAULT_INJECTION:
            "This test deliberately BREAKS one thing at a time. Confirm "
            "you have read the procedure and accept the fault it injects",
        ENDURANCE:
            "This test runs for a long time and may move or illuminate "
            "thousands of times. Confirm the bench is free for the "
            "duration and the iteration count is what you want",
        FULL_SYSTEM:
            "This test drives the COMPLETE mission: movement, "
            "illumination and persistence. Confirm the module is set up "
            "exactly as it would be for a competition run",
    }


class Automation:
    """
    Who performs the procedure.

    OPERATOR_ASSISTED is not a lesser kind of test. Whether the carousel
    physically turned 180 degrees is not knowable from the serial port -
    that is the entire content of H-002 - and a framework that pretended
    otherwise would be manufacturing the exact false evidence this
    project is trying to avoid.
    """

    AUTOMATIC = "AUTOMATIC"
    OPERATOR_ASSISTED = "OPERATOR_ASSISTED"
    MANUAL = "MANUAL"

    ALL = (AUTOMATIC, OPERATOR_ASSISTED, MANUAL)

    # Cannot run without a human at the keyboard.
    NEEDS_OPERATOR = (OPERATOR_ASSISTED, MANUAL)


# ======================================================================
# control-flow exceptions
# ======================================================================

# PYTEST MUST NOT COLLECT ANY OF THIS.
#
# Three classes here are named Test* - TestControl, TestDefinition and
# TestResult - and pytest collects Test* classes out of the namespace of
# any test_*.py module, including ones that merely IMPORTED them. The
# offline suites import all three.
#
# Nothing would actually execute (they have constructors, so pytest
# skips them with a warning), but "it warns instead of running your
# carousel" is a poor guarantee to rest on. `__test__ = False` is
# pytest's documented opt-out and makes it a fact: a generic pytest run
# over this repository collects zero items from the hardware framework.
class TestControl(Exception):
    """Base for the three ways a test body ends other than by returning."""

    __test__ = False


class Blocked(TestControl):
    """
    The test cannot run, and the reason is a missing capability, not a
    fault in the hardware.

    `recommendation` is required, and required for a reason: "BLOCKED"
    with no route out of it is a dead entry in a table. Every block in
    this framework names the production change, wiring change or
    prerequisite result that would unblock it.
    """

    def __init__(self, reason, capability=None, recommendation=None):
        super().__init__(reason)

        self.reason = str(reason)
        self.capability = capability
        self.recommendation = str(recommendation or "")


class Failure(TestControl):
    """The procedure ran and the hardware did not satisfy it."""

    def __init__(self, reason, defect=None, evidence=None):
        super().__init__(reason)

        self.reason = str(reason)
        self.defect = defect
        self.evidence = evidence if evidence is not None else {}


class Skip(TestControl):
    """A precondition that is nobody's fault, e.g. an optional fixture."""

    def __init__(self, reason):
        super().__init__(reason)

        self.reason = str(reason)


class Aborted(TestControl):
    """Ctrl+C, or an operator answering ABORT to a prompt."""

    def __init__(self, reason="interrupted by the operator"):
        super().__init__(reason)

        self.reason = str(reason)


# ======================================================================
# the definition of one test
# ======================================================================

REQUIRED_TEXT_FIELDS = (
    "title", "objective", "hardware_setup", "preconditions",
    "expected", "failure_criteria",
)


class TestDefinition:
    """
    One hardware test, complete enough to survive the session it was
    written in.

    The nine documentation fields are not optional and are not checked
    "later": `offline_tests/test_registry.py` fails the build if any
    registered test is missing one. A procedure with no expected result
    cannot fail, and a test that cannot fail is decoration.
    """

    __test__ = False            # see the note above TestControl

    def __init__(self, test_id, campaign, layer, title, objective,
                 hardware_setup, preconditions, procedure, expected,
                 failure_criteria, captures, safety, automation,
                 run, cleanup=None, requires=(), prerequisites=(),
                 default_iterations=None, max_iterations=None,
                 assumption=None, defect_prefix=None, notes=""):
        self.test_id = str(test_id)
        self.campaign = str(campaign)
        self.layer = str(layer)

        self.title = str(title)
        self.objective = str(objective)
        self.hardware_setup = str(hardware_setup)
        self.preconditions = str(preconditions)
        self.procedure = tuple(procedure)
        self.expected = str(expected)
        self.failure_criteria = str(failure_criteria)
        self.captures = tuple(captures)

        self.safety = str(safety)
        self.automation = str(automation)

        self.run = run
        self.cleanup = cleanup

        self.requires = tuple(requires)
        self.prerequisites = tuple(prerequisites)

        self.default_iterations = default_iterations
        self.max_iterations = max_iterations

        # The H-nnn assumption from HARDWARE_VERIFICATION_PLAN.md this
        # test settles, if any. Traceability in the direction that
        # matters: from a result back to the question it answers.
        self.assumption = assumption

        # The HW-xxx-nnn family a failure here belongs to.
        self.defect_prefix = defect_prefix

        self.notes = str(notes)

        self.validate()

    # ------------------------------------------------------------------

    def validate(self):
        """Raise if this definition could not produce a usable result."""
        problems = self.problems()

        if problems:
            raise ValueError("{}: {}".format(
                self.test_id, "; ".join(problems)
            ))

    def problems(self):
        """Every structural fault in this definition, as sentences."""
        found = []

        if not self.test_id:
            found.append("no test id")

        for field in REQUIRED_TEXT_FIELDS:
            if not str(getattr(self, field, "")).strip():
                found.append("empty '{}'".format(field))

        if not self.procedure:
            found.append("empty 'procedure'")

        if not self.captures:
            found.append("empty 'captures' - a test that says nothing "
                         "about what to keep produces an opinion")

        if self.safety not in Safety.ALL:
            found.append("unknown safety class {!r}".format(self.safety))

        if self.automation not in Automation.ALL:
            found.append("unknown automation level {!r}".format(
                self.automation))

        if not callable(self.run):
            found.append("'run' is not callable")

        if self.cleanup is not None and not callable(self.cleanup):
            found.append("'cleanup' is not callable")

        if self.default_iterations is not None:
            if self.max_iterations is None:
                found.append("iterations without a maximum")

            elif self.default_iterations > self.max_iterations:
                found.append("default_iterations exceeds max_iterations")

        if self.safety == Safety.ENDURANCE and self.max_iterations is None:
            found.append("an ENDURANCE test must declare max_iterations")

        return found

    # ------------------------------------------------------------------

    @property
    def needs_operator(self):
        return self.automation in Automation.NEEDS_OPERATOR

    @property
    def needs_extra_confirmation(self):
        return self.safety in Safety.NEEDS_EXTRA_CONFIRMATION

    def as_dict(self):
        return {
            "test_id": self.test_id,
            "campaign": self.campaign,
            "layer": self.layer,
            "title": self.title,
            "objective": self.objective,
            "hardware_setup": self.hardware_setup,
            "preconditions": self.preconditions,
            "procedure": list(self.procedure),
            "expected": self.expected,
            "failure_criteria": self.failure_criteria,
            "captures": list(self.captures),
            "safety": self.safety,
            "automation": self.automation,
            "requires": list(self.requires),
            "prerequisites": list(self.prerequisites),
            "default_iterations": self.default_iterations,
            "max_iterations": self.max_iterations,
            "assumption": self.assumption,
            "defect_prefix": self.defect_prefix,
            "has_cleanup": self.cleanup is not None,
            "notes": self.notes,
        }


class Campaign:
    """A layer of the pyramid: B0 through B12."""

    def __init__(self, campaign_id, layer, title, purpose,
                 prerequisites=(), gate_note=""):
        self.campaign_id = str(campaign_id)
        self.layer = str(layer)
        self.title = str(title)
        self.purpose = str(purpose)
        self.prerequisites = tuple(prerequisites)
        self.gate_note = str(gate_note)

    def as_dict(self):
        return {
            "campaign_id": self.campaign_id,
            "layer": self.layer,
            "title": self.title,
            "purpose": self.purpose,
            "prerequisites": list(self.prerequisites),
            "gate_note": self.gate_note,
        }


# ======================================================================
# the result of one test
# ======================================================================

class Check:
    """One yes/no observation inside a test, with the numbers behind it."""

    def __init__(self, description, ok, evidence=None, kind="AUTOMATIC"):
        self.description = str(description)
        self.ok = bool(ok)
        self.evidence = evidence if evidence is not None else {}
        self.kind = kind

    def as_dict(self):
        return {
            "check": self.description,
            "ok": self.ok,
            "kind": self.kind,
            "evidence": self.evidence,
        }


class TestResult:
    """
    What one test did, and what class of evidence that is.

    Constructing this with PASS and anything other than
    `Evidence.HARDWARE` raises. That is the single mechanical guarantee
    behind every "no hardware PASS was claimed" statement in this
    repository.
    """

    __test__ = False            # see the note above TestControl

    def __init__(self, definition, status, evidence_class,
                 reason="", started_at=None, finished_at=None):
        if status not in Status.ALL:
            raise ValueError("unknown status {!r}".format(status))

        if evidence_class not in Evidence.ALL:
            raise ValueError(
                "unknown evidence class {!r}".format(evidence_class))

        if status == Status.PASS and evidence_class != Evidence.HARDWARE:
            raise ValueError(
                "PASS requires hardware evidence; {} was produced with "
                "evidence class {}. A dry run and a framework self-test "
                "cannot pass a hardware test.".format(
                    definition.test_id, evidence_class
                )
            )

        self.definition = definition
        self.test_id = definition.test_id
        self.campaign = definition.campaign
        self.layer = definition.layer

        self.status = status
        self.evidence_class = evidence_class
        self.reason = str(reason)

        self.started_at = started_at
        self.finished_at = finished_at

        self.checks = []
        self.measurements = []
        self.observations = []
        self.defects = []

        self.iterations = None
        self.first_failure_iteration = None

        self.cleanup = None
        self.override = None

        self.error_type = None
        self.error_message = None

        self.notes = []

    # ------------------------------------------------------------------

    @property
    def duration(self):
        if self.started_at is None or self.finished_at is None:
            return None

        return round(self.finished_at - self.started_at, 3)

    @property
    def hardware_evidence(self):
        return self.evidence_class == Evidence.HARDWARE

    def reported_status(self):
        """
        The status as it may be QUOTED.

        A self-test that exercised the success path reports
        SELFTEST_PASS, never PASS: the framework works, the carousel is
        still an open question.
        """
        if self.hardware_evidence:
            return self.status

        if self.status == Status.PASS:            # pragma: no cover
            return "SELFTEST_PASS"                # unreachable by design

        return self.status

    def failed_checks(self):
        return [c for c in self.checks if not c.ok]

    def as_dict(self):
        return {
            "test_id": self.test_id,
            "campaign": self.campaign,
            "layer": self.layer,
            "status": self.status,
            "reported_status": self.reported_status(),
            "evidence_class": self.evidence_class,
            "hardware_evidence": self.hardware_evidence,
            "reason": self.reason,
            "duration_s": self.duration,
            "iterations": self.iterations,
            "first_failure_iteration": self.first_failure_iteration,
            "checks": [c.as_dict() for c in self.checks],
            "checks_passed": len([c for c in self.checks if c.ok]),
            "checks_failed": len(self.failed_checks()),
            "measurements": len(self.measurements),
            "observations": list(self.observations),
            "defects": list(self.defects),
            "cleanup": self.cleanup,
            "override": self.override,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "notes": list(self.notes),
            "assumption": self.definition.assumption,
        }


def now():
    """One clock for the whole framework, so durations are comparable."""
    return time.monotonic()
