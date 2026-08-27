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


# ======================================================================
# the three axes
# ======================================================================
#
# `Status` above is ONE value doing THREE jobs, and that conflation hides
# the distinctions a hardware campaign lives or dies on:
#
#   BLOCKED       is about readiness - the procedure never started
#   ABORTED       is about execution - it started and was interrupted
#   FAIL          is about the verdict - it ran and the hardware was wrong
#
# Reading `SKIPPED` tells you none of those cleanly, and the case the
# old model could not express at all is the important one: a procedure
# that RAN, COMPLETED, and produced numbers against which no
# authoritative requirement exists. That is not a pass and it is not a
# failure - it is characterization, and calling it either is how a
# campaign starts lying.
#
# So the three axes are independent, and `Status` becomes a projection
# of them (see `TestResult.status`) so every existing summary, ledger
# and report keeps reading exactly as before.

class Readiness:
    """Could the procedure start at all?"""

    READY = "READY"
    BLOCKED = "BLOCKED"

    ALL = (READY, BLOCKED)


class Execution:
    """What happened to the procedure once it was allowed to start?"""

    NOT_RUN = "NOT_RUN"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"
    ERROR = "ERROR"

    ALL = (NOT_RUN, RUNNING, COMPLETED, ABORTED, ERROR)

    # The only state from which a verdict may be drawn.
    CONCLUSIVE = (COMPLETED,)


class Verdict:
    """
    What the evidence says about the HARDWARE.

    INCONCLUSIVE and CHARACTERIZATION are the two the old model could
    not say, and both are common and honest:

        INCONCLUSIVE     the procedure ran, but a required observation
                         was missing or ambiguous. An operator who
                         answered UNKNOWN, a field the firmware did not
                         return, a fault injected outside the window it
                         was meant for.

        CHARACTERIZATION measurements were collected and there is no
                         authoritative requirement to judge them
                         against. Most of B4 is this until somebody
                         writes down what the closing error is allowed
                         to be.
    """

    NOT_EVALUATED = "NOT_EVALUATED"
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    CHARACTERIZATION = "CHARACTERIZATION"

    ALL = (NOT_EVALUATED, PASS, FAIL, INCONCLUSIVE, CHARACTERIZATION)

    # Verdicts that mean "the hardware did something wrong".
    ADVERSE = (FAIL,)

    # Verdicts that are not a pass and not a failure.
    UNSETTLED = (NOT_EVALUATED, INCONCLUSIVE, CHARACTERIZATION)


class Requirement:
    """
    How much an observation matters to the test's own objective.

    The distinction decides what a MISSING value means, and getting it
    wrong is the single largest source of false PASSes in a hardware
    campaign - the old bodies were full of `reported is None or reported
    == expected`, which reads "the device identifies correctly OR does
    not identify at all" and passes both.

        REQUIRED             the objective depends on it. Missing is
                             INCONCLUSIVE; wrong is FAIL.
        OPTIONAL_DIAGNOSTIC  useful, recorded, never decides anything.
                             It may be absent, and it may NOT stand in
                             for a REQUIRED observation.
        NOT_AVAILABLE        the shipped system cannot produce it. The
                             test says so and does not pretend.
    """

    REQUIRED = "REQUIRED"
    OPTIONAL_DIAGNOSTIC = "OPTIONAL_DIAGNOSTIC"
    NOT_AVAILABLE = "NOT_AVAILABLE"

    ALL = (REQUIRED, OPTIONAL_DIAGNOSTIC, NOT_AVAILABLE)


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


class IterationKind:
    """
    WHAT is being repeated, which is not the same as which campaign the
    test lives in.

    THE DEFECT THIS EXISTS FOR. The ceiling for a repeated test used to
    be looked up from the CAMPAIGN ID:

        "B11": "max_requests"

    B11 contains serial, sensor, servo, carousel and full-system
    endurance. So a servo endurance run - thousands of real movements of
    a real mechanism - was bounded by `max_requests`, a limit written
    for cheap serial round trips and set to 20,000. The carousel would
    have been allowed twenty thousand movements by a limit that was
    never about movement.

    The kind of thing being repeated is a property of the TEST. It is
    declared, and the profile limit is resolved from it.
    """

    OPEN_CYCLE = "OPEN_CYCLE"      # open the port, talk, close it
    REQUEST = "REQUEST"            # one command on an open port
    MEASUREMENT = "MEASUREMENT"    # one acquisition
    MOVEMENT = "MOVEMENT"          # one commanded servo movement
    ROTATION = "ROTATION"          # one full carousel revolution
    MISSION = "MISSION"            # one complete operator mission

    ALL = (OPEN_CYCLE, REQUEST, MEASUREMENT, MOVEMENT, ROTATION, MISSION)

    # Which profile limit bounds each kind. One mapping, in one place,
    # keyed by what is actually being repeated.
    PROFILE_LIMIT = {
        OPEN_CYCLE: "max_open_cycles",
        REQUEST: "max_requests",
        MEASUREMENT: "max_measurements",
        MOVEMENT: "max_movements",
        ROTATION: "max_rotations",
        MISSION: "max_missions",
    }


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


class Inconclusive(TestControl):
    """
    The procedure ran and the evidence will not support a verdict.

    NOT a failure: nothing was observed to be wrong. NOT a pass: the
    thing the test exists to establish was not established. The
    commonest causes are an operator answering UNKNOWN to an observation
    the objective depends on, a field the firmware did not return, and a
    fault injected outside the window it was aimed at.
    """

    def __init__(self, reason, missing=(), evidence=None):
        super().__init__(reason)

        self.reason = str(reason)
        self.missing = tuple(missing)
        self.evidence = evidence if evidence is not None else {}


class Characterization(TestControl):
    """
    Measurements were collected and there is no requirement to judge them.

    Raised deliberately by a test whose whole job is to produce numbers -
    the closing-error distribution that H-001 needs, the latency
    distribution a timeout should be argued from. Reporting those as
    PASS would invent an acceptance criterion nobody wrote down.
    """

    def __init__(self, reason, measurements=None):
        super().__init__(reason)

        self.reason = str(reason)
        self.measurements = (measurements if measurements is not None
                             else {})


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
                 iteration_kind=None, qualification_min_iterations=None,
                 characterization_min_iterations=None,
                 requirements=(), acceptance_source=None,
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

        # WHAT is repeated - not which campaign this lives in. The
        # profile ceiling is resolved from this. See `IterationKind`.
        self.iteration_kind = iteration_kind

        # How many iterations this test needs before its result is worth
        # anything.
        #
        #   below characterization_min   the run is too small to say
        #                                anything at all
        #   below qualification_min      the run produces
        #                                CHARACTERIZATION, never a
        #                                qualification PASS
        #
        # This is what stops `--iterations 10` on a campaign whose own
        # acceptance criterion says "at least 100 open cycles" from
        # quietly reporting a pass over ten.
        self.qualification_min_iterations = qualification_min_iterations
        self.characterization_min_iterations = (
            characterization_min_iterations)

        # The HW-REQ-nnn requirements this test is evidence for. Both
        # directions are generated from these; a test with none, or a
        # requirement with no test, fails the offline audit.
        self.requirements = tuple(requirements)

        # WHERE the acceptance criterion comes from. A test that can
        # reach PASS must be able to say what says so - a datasheet, a
        # design requirement, a schematic, a measured baseline. None
        # means the test may only characterize.
        self.acceptance_source = acceptance_source

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

            if self.iteration_kind is None:
                found.append(
                    "a repeated test must declare an iteration_kind - "
                    "the profile ceiling is resolved from WHAT is "
                    "repeated, not from which campaign it lives in")

            elif self.iteration_kind not in IterationKind.ALL:
                found.append("unknown iteration_kind {!r}".format(
                    self.iteration_kind))

            minimums = (
                ("qualification_min_iterations",
                 self.qualification_min_iterations),
                ("characterization_min_iterations",
                 self.characterization_min_iterations),
            )

            for name, value in minimums:
                if value is None:
                    continue

                if isinstance(value, bool) or not isinstance(value, int):
                    found.append("{} must be a whole number".format(name))

                elif value < 1:
                    found.append("{} must be at least 1".format(name))

                elif self.max_iterations and value > self.max_iterations:
                    found.append(
                        "{} exceeds max_iterations".format(name))

            if (self.qualification_min_iterations
                    and self.characterization_min_iterations
                    and self.characterization_min_iterations
                    > self.qualification_min_iterations):
                found.append(
                    "characterization_min_iterations is above "
                    "qualification_min_iterations, which would make "
                    "characterization the harder bar")

        elif self.iteration_kind is not None:
            found.append(
                "iteration_kind on a test that declares no iterations")

        if self.safety == Safety.ENDURANCE and self.max_iterations is None:
            found.append("an ENDURANCE test must declare max_iterations")

        if not self.requirements:
            found.append(
                "no requirements - every test must be evidence for at "
                "least one HW-REQ-nnn, or nothing traces back to it")

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


    # ------------------------------------------------------------------
    # the projection back onto the old single-value Status
    # ------------------------------------------------------------------
    #
    # Every existing summary, ledger entry, exit code and report reads
    # `Status`. Rather than rewrite all of them - and rather than leave
    # a year of evidence directories unreadable - the three axes project
    # back onto it. The projection is deliberately lossy in exactly one
    # direction: CHARACTERIZATION and INCONCLUSIVE both become SKIPPED
    # in the legacy field, because the legacy vocabulary has no word for
    # either and SKIPPED is the only one that does not overclaim.
    #
    # Anything reading the new fields sees the truth; anything reading
    # the old field sees something that was never wrong, only coarser.


def _axes_for_status(status):
    """
    The three axes a legacy `Status` meant.

    Used only when an older caller - or an older evidence file - hands
    the framework a single value. The mapping is the honest reading of
    what each old status actually asserted.
    """
    return {
        Status.NOT_RUN: (Readiness.READY, Execution.NOT_RUN,
                         Verdict.NOT_EVALUATED),
        Status.READY_FOR_HARDWARE: (Readiness.READY, Execution.NOT_RUN,
                                    Verdict.NOT_EVALUATED),
        Status.BLOCKED: (Readiness.BLOCKED, Execution.NOT_RUN,
                         Verdict.NOT_EVALUATED),
        Status.SKIPPED: (Readiness.READY, Execution.COMPLETED,
                         Verdict.NOT_EVALUATED),
        Status.PASS: (Readiness.READY, Execution.COMPLETED,
                      Verdict.PASS),
        Status.FAIL: (Readiness.READY, Execution.COMPLETED,
                      Verdict.FAIL),
        Status.ERROR: (Readiness.READY, Execution.ERROR,
                       Verdict.NOT_EVALUATED),
        Status.ABORTED: (Readiness.READY, Execution.ABORTED,
                         Verdict.NOT_EVALUATED),
    }[status]


# The combinations that are not merely unusual but incoherent. Each one
# is a claim the framework must never be able to make.
def _refuse_impossible(definition, readiness, execution, verdict,
                       evidence_class):
    test_id = getattr(definition, "test_id", "?")

    if verdict == Verdict.PASS and evidence_class != Evidence.HARDWARE:
        raise ValueError(
            "PASS requires hardware evidence; {} was produced with "
            "evidence class {}. A dry run and a framework self-test "
            "cannot pass a hardware test.".format(test_id,
                                                  evidence_class))

    if verdict != Verdict.NOT_EVALUATED and readiness == Readiness.BLOCKED:
        raise ValueError(
            "{} is BLOCKED and carries verdict {}. A procedure that "
            "never started cannot have said anything about the "
            "hardware.".format(test_id, verdict))

    if (verdict in (Verdict.PASS, Verdict.FAIL)
            and execution != Execution.COMPLETED):
        raise ValueError(
            "{} claims verdict {} with execution {}. A verdict of PASS "
            "or FAIL requires a procedure that ran to completion; an "
            "aborted or errored run has not established "
            "either.".format(test_id, verdict, execution))


def project_status(readiness, execution, verdict,
                   offline_verified=False):
    """
    The legacy `Status` for a three-axis result. Never overclaims.

    `offline_verified` carries the one distinction the three axes cannot
    express on their own: NOT_RUN and READY_FOR_HARDWARE are the same
    point on all three - ready, not run, not evaluated - and the
    difference between them is whether anything CHECKED. A dry run
    checks the definition, the prerequisites, the capabilities and the
    profile; a test nobody has looked at has had none of that done.
    """
    if readiness == Readiness.BLOCKED:
        return Status.BLOCKED

    if execution == Execution.NOT_RUN:
        if offline_verified:
            return Status.READY_FOR_HARDWARE

        return Status.NOT_RUN

    if execution == Execution.ABORTED:
        return Status.ABORTED

    if execution == Execution.ERROR:
        return Status.ERROR

    if execution == Execution.RUNNING:
        return Status.NOT_RUN

    if verdict == Verdict.PASS:
        return Status.PASS

    if verdict == Verdict.FAIL:
        return Status.FAIL

    # COMPLETED, but INCONCLUSIVE / CHARACTERIZATION / NOT_EVALUATED.
    return Status.SKIPPED


class TestResult:
    """
    What one test did, on three independent axes.

        readiness    could it start
        execution    what happened once it did
        verdict      what the evidence says about the hardware

    `status` is a read-only projection of the three onto the older
    single-value vocabulary, so every existing summary keeps working.

    Constructing this with a PASS verdict and anything other than
    `Evidence.HARDWARE` raises. That is the single mechanical guarantee
    behind every "no hardware PASS was claimed" statement in this
    repository, and it is now enforced on the verdict axis where PASS
    actually lives.
    """

    __test__ = False            # see the note above TestControl

    def __init__(self, definition, status=None, evidence_class=None,
                 reason="", started_at=None, finished_at=None,
                 readiness=None, execution=None, verdict=None):
        if evidence_class not in Evidence.ALL:
            raise ValueError(
                "unknown evidence class {!r}".format(evidence_class))

        # A legacy caller passes `status`; the framework's own code
        # passes the axes. Accepting both is what makes the migration a
        # non-event for the offline suites and for anything reading an
        # older evidence directory.
        # Whether anything checked this test offline. See
        # `project_status`: it is what separates READY_FOR_HARDWARE from
        # NOT_RUN, which are otherwise the same point on all three axes.
        self.offline_verified = False

        if status is not None:
            if status not in Status.ALL:
                raise ValueError("unknown status {!r}".format(status))

            if status == Status.READY_FOR_HARDWARE:
                self.offline_verified = True

            readiness, execution, verdict = _axes_for_status(status)

        readiness = readiness or Readiness.READY
        execution = execution or Execution.NOT_RUN
        verdict = verdict or Verdict.NOT_EVALUATED

        for value, allowed, name in (
                (readiness, Readiness.ALL, "readiness"),
                (execution, Execution.ALL, "execution"),
                (verdict, Verdict.ALL, "verdict")):
            if value not in allowed:
                raise ValueError("unknown {} {!r}".format(name, value))

        _refuse_impossible(definition, readiness, execution, verdict,
                           evidence_class)

        self.definition = definition
        self.test_id = definition.test_id
        self.campaign = definition.campaign
        self.layer = definition.layer

        self.readiness = readiness
        self.execution = execution
        self.verdict = verdict

        self.evidence_class = evidence_class
        self.reason = str(reason)

        self.started_at = started_at
        self.finished_at = finished_at

        self.checks = []
        self.measurements = []
        self.observations = []
        self.defects = []

        # Observations classified REQUIRED that were not obtained. Any
        # entry here makes a PASS impossible - see `settle`.
        self.missing_required = []

        self.iterations = None
        self.first_failure_iteration = None
        self.iteration_kind = None
        self.qualified = None

        self.cleanup = None
        self.override = None

        self.error_type = None
        self.error_message = None

        self.notes = []

    # ------------------------------------------------------------------
    # the axes
    # ------------------------------------------------------------------

    @property
    def status(self):
        """The legacy single-value status. Derived, never stored."""
        return project_status(self.readiness, self.execution,
                              self.verdict, self.offline_verified)

    @status.setter
    def status(self, value):
        """
        Set all three axes from one legacy value.

        A compatibility shim, and a deliberately limited one. It handles
        the unambiguous cases - BLOCKED, ABORTED, ERROR, PASS, FAIL -
        where the old vocabulary meant exactly one thing. It CANNOT
        express INCONCLUSIVE or CHARACTERIZATION, because the old
        vocabulary had no word for either; code that needs those must
        call `settle` with the axes it means.

        It exists so that migrating the framework did not require
        rewriting every gate in one commit, and so that an evidence file
        written before the migration still loads.
        """
        if value not in Status.ALL:
            raise ValueError("unknown status {!r}".format(value))

        if value == Status.READY_FOR_HARDWARE:
            self.offline_verified = True

        readiness, execution, verdict = _axes_for_status(value)

        self.settle(readiness=readiness, execution=execution,
                    verdict=verdict)

    def settle(self, readiness=None, execution=None, verdict=None,
               reason=None):
        """
        Move the result along its axes, refusing impossible combinations.

        The one rule applied here that nothing else can bypass: a PASS
        verdict is refused while any REQUIRED observation is missing. A
        test cannot pass on evidence it did not obtain, however many
        other checks succeeded.
        """
        readiness = readiness or self.readiness
        execution = execution or self.execution
        verdict = verdict or self.verdict

        if verdict == Verdict.PASS and self.missing_required:
            verdict = Verdict.INCONCLUSIVE

            reason = (
                "{} required observation{} were not obtained: {}".format(
                    len(self.missing_required),
                    "" if len(self.missing_required) == 1 else "s",
                    "; ".join(m.get("description", "?")
                              for m in self.missing_required[:3])))

        _refuse_impossible(self.definition, readiness, execution,
                           verdict, self.evidence_class)

        self.readiness = readiness
        self.execution = execution
        self.verdict = verdict

        if reason is not None:
            self.reason = str(reason)

        return self

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
            if self.verdict == Verdict.CHARACTERIZATION:
                return "CHARACTERIZATION"

            if self.verdict == Verdict.INCONCLUSIVE:
                return "INCONCLUSIVE"

            return self.status

        if self.status == Status.PASS:            # pragma: no cover
            return "SELFTEST_PASS"                # unreachable by design

        return self.status

    def failed_checks(self):
        return [c for c in self.checks if not c.ok]

    def record_missing_required(self, description, evidence=None):
        """
        A REQUIRED observation that was not obtained.

        Recorded rather than raised, so a test collects every missing
        field before concluding - three missing registers is a more
        useful diagnosis than the first one. `settle` refuses PASS while
        any entry is here.
        """
        entry = {
            "description": str(description),
            "requirement": Requirement.REQUIRED,
            "evidence": evidence if evidence is not None else {},
        }

        self.missing_required.append(entry)

        return entry

    def as_dict(self):
        return {
            "test_id": self.test_id,
            "campaign": self.campaign,
            "layer": self.layer,
            "status": self.status,
            "reported_status": self.reported_status(),
            "readiness": self.readiness,
            "execution": self.execution,
            "verdict": self.verdict,
            "evidence_class": self.evidence_class,
            "hardware_evidence": self.hardware_evidence,
            "reason": self.reason,
            "duration_s": self.duration,
            "iterations": self.iterations,
            "iteration_kind": self.iteration_kind,
            "qualified": self.qualified,
            "missing_required": list(self.missing_required),
            "requirements": list(self.definition.requirements),
            "acceptance_source": self.definition.acceptance_source,
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
