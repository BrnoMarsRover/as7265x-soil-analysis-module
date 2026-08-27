"""
The result model: three axes, and every way a PASS must be unreachable.

This is the suite that would catch the worst kind of regression in this
framework - not a crash, but a quiet re-widening of what counts as a
pass. Everything here is a prohibition, and each one corresponds to a
pattern that was actually present in the campaign bodies before the
audit.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

if str(HERE.parent.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent.parent))

from hardware.core import requirements as requirements_module  # noqa: E402
from hardware.core.model import (Automation, Evidence,        # noqa: E402
                                 Execution, IterationKind,
                                 Readiness, Requirement, Safety,
                                 Status, TestDefinition,
                                 TestResult, Verdict,
                                 project_status)
from hardware.core.registry import Registry                   # noqa: E402
from hardware.core.runner import Runner                       # noqa: E402
from hardware.offline_tests.harness import (Bench, Checks,    # noqa: E402
                                            cli, registry)


def _definition(**overrides):
    fields = {
        "test_id": "HW-T-001", "campaign": "T", "layer": "T",
        "title": "t", "objective": "o", "hardware_setup": "h",
        "preconditions": "p", "procedure": ("s",), "expected": "e",
        "failure_criteria": "f", "captures": ("c",),
        "requirements": ("HW-REQ-FW-001",),
        "safety": Safety.READ_ONLY,
        "automation": Automation.AUTOMATIC,
        "run": lambda ctx: None,
        "defect_prefix": "HW-TEST",
    }

    fields.update(overrides)

    return TestDefinition(**fields)


def _catalogue(body, **overrides):
    catalogue = Registry()

    catalogue.campaign("T", "T", "verdicts", "one test, driven")

    catalogue.add(_definition(run=body, **overrides))

    return catalogue


def _run(bench, catalogue, **kwargs):
    runner = Runner(catalogue, bench.context, ledger=bench.ledger,
                    **kwargs)

    return runner.run(catalogue.all_tests())[0]


def run():
    checks = Checks("hardware/offline_tests/test_verdicts.py")

    definition = _definition()

    # ------------------------------------------------------------------
    checks.section("the three axes are independent")

    checks.equal(sorted(Readiness.ALL), ["BLOCKED", "READY"],
                 "readiness has exactly two values")

    checks.equal(sorted(Execution.ALL),
                 ["ABORTED", "COMPLETED", "ERROR", "NOT_RUN", "RUNNING"],
                 "execution has the five the task specifies")

    checks.equal(sorted(Verdict.ALL),
                 ["CHARACTERIZATION", "FAIL", "INCONCLUSIVE",
                  "NOT_EVALUATED", "PASS"],
                 "verdict has the five the task specifies")

    checks.section("the projection onto the legacy status never "
                   "overclaims")

    cases = (
        ((Readiness.BLOCKED, Execution.NOT_RUN, Verdict.NOT_EVALUATED),
         Status.BLOCKED, "blocked projects to BLOCKED"),
        ((Readiness.READY, Execution.NOT_RUN, Verdict.NOT_EVALUATED),
         Status.NOT_RUN, "ready but unrun projects to NOT_RUN"),
        ((Readiness.READY, Execution.ABORTED, Verdict.NOT_EVALUATED),
         Status.ABORTED, "aborted projects to ABORTED"),
        ((Readiness.READY, Execution.ERROR, Verdict.NOT_EVALUATED),
         Status.ERROR, "errored projects to ERROR"),
        ((Readiness.READY, Execution.COMPLETED, Verdict.PASS),
         Status.PASS, "a completed pass projects to PASS"),
        ((Readiness.READY, Execution.COMPLETED, Verdict.FAIL),
         Status.FAIL, "a completed failure projects to FAIL"),
        ((Readiness.READY, Execution.COMPLETED, Verdict.INCONCLUSIVE),
         Status.SKIPPED,
         "INCONCLUSIVE projects to SKIPPED - the legacy vocabulary has "
         "no word for it and SKIPPED is the only one that does not "
         "overclaim"),
        ((Readiness.READY, Execution.COMPLETED,
          Verdict.CHARACTERIZATION),
         Status.SKIPPED, "CHARACTERIZATION likewise"),
    )

    for axes, expected, description in cases:
        checks.equal(project_status(*axes), expected, description)

    checks.equal(
        project_status(Readiness.READY, Execution.NOT_RUN,
                       Verdict.NOT_EVALUATED, offline_verified=True),
        Status.READY_FOR_HARDWARE,
        "an offline-verified unrun test projects to "
        "READY_FOR_HARDWARE, which is the one distinction the three "
        "axes cannot carry alone")

    checks.section("impossible combinations are refused")

    checks.raises(
        ValueError,
        lambda: TestResult(definition, evidence_class=Evidence.SELFTEST,
                           readiness=Readiness.READY,
                           execution=Execution.COMPLETED,
                           verdict=Verdict.PASS),
        "a PASS with self-test evidence is refused")

    checks.raises(
        ValueError,
        lambda: TestResult(definition, evidence_class=Evidence.DRY_RUN,
                           readiness=Readiness.READY,
                           execution=Execution.COMPLETED,
                           verdict=Verdict.PASS),
        "a PASS with dry-run evidence is refused")

    checks.raises(
        ValueError,
        lambda: TestResult(definition, evidence_class=Evidence.HARDWARE,
                           readiness=Readiness.BLOCKED,
                           execution=Execution.NOT_RUN,
                           verdict=Verdict.FAIL),
        "a BLOCKED test carrying a verdict is refused - a procedure "
        "that never started said nothing about the hardware")

    for execution in (Execution.ABORTED, Execution.ERROR,
                      Execution.NOT_RUN):
        checks.raises(
            ValueError,
            lambda e=execution: TestResult(
                definition, evidence_class=Evidence.HARDWARE,
                readiness=Readiness.READY, execution=e,
                verdict=Verdict.PASS),
            "a PASS with execution {} is refused".format(execution))

        checks.raises(
            ValueError,
            lambda e=execution: TestResult(
                definition, evidence_class=Evidence.HARDWARE,
                readiness=Readiness.READY, execution=e,
                verdict=Verdict.FAIL),
            "a FAIL with execution {} is refused".format(execution))

    checks.section("a missing REQUIRED observation blocks PASS")

    result = TestResult(definition, evidence_class=Evidence.HARDWARE,
                        readiness=Readiness.READY,
                        execution=Execution.COMPLETED)

    result.record_missing_required("the firmware identity")

    result.settle(verdict=Verdict.PASS)

    checks.equal(result.verdict, Verdict.INCONCLUSIVE,
                 "settling to PASS with a missing REQUIRED observation "
                 "yields INCONCLUSIVE instead")

    checks.ok("firmware identity" in result.reason,
              "and the reason names the observation that was missing")

    # ------------------------------------------------------------------
    checks.section("the runner honours every outcome")

    def passing(ctx):
        ctx.check(True, "an observation")

    with Bench() as bench:
        result = _run(bench, _catalogue(passing))

        checks.equal(result.execution, Execution.COMPLETED,
                     "a body that returns COMPLETED the execution axis")

        checks.equal(result.verdict, Verdict.NOT_EVALUATED,
                     "and a fake transport leaves the verdict "
                     "unevaluated rather than passing")

    def inconclusive(ctx):
        ctx.check(True, "something was seen")
        ctx.inconclusive("the operator answered UNKNOWN",
                         missing=("the observed shaft angle",))

    with Bench() as bench:
        result = _run(bench, _catalogue(inconclusive))

        checks.equal(result.verdict, Verdict.INCONCLUSIVE,
                     "ctx.inconclusive produces an INCONCLUSIVE verdict")

        checks.equal(result.execution, Execution.COMPLETED,
                     "with the execution still COMPLETED - the "
                     "procedure ran")

        checks.equal(len(result.missing_required), 1,
                     "and the missing observation is recorded")

        checks.equal(result.reported_status(), Status.SKIPPED,
                     "a non-hardware INCONCLUSIVE reports as SKIPPED")

    def characterizing(ctx):
        ctx.check(True, "measurements were taken")
        ctx.characterize("no requirement judges these numbers")

    with Bench() as bench:
        result = _run(bench, _catalogue(characterizing))

        checks.equal(result.verdict, Verdict.CHARACTERIZATION,
                     "ctx.characterize produces a CHARACTERIZATION "
                     "verdict")

        checks.ok("no requirement" in result.reason,
                  "and keeps the reason the test gave")

    checks.section("a body that misses a REQUIRED field cannot pass")

    def misses_required(ctx):
        ctx.check(True, "an unrelated check that passes")
        ctx.observed("the firmware identity", None,
                     expected="freya-science-module",
                     requirement=Requirement.REQUIRED)

    with Bench() as bench:
        result = _run(bench, _catalogue(misses_required))

        checks.equal(result.verdict, Verdict.FAIL,
                     "a missing REQUIRED observation fails the check it "
                     "belongs to")

        checks.ok(bool(result.missing_required),
                  "and is recorded as missing rather than skipped over")

    def optional_absent(ctx):
        ctx.check(True, "an observation")
        ctx.observed("a diagnostic extra", None,
                     requirement=Requirement.OPTIONAL_DIAGNOSTIC)

    with Bench() as bench:
        result = _run(bench, _catalogue(optional_absent))

        checks.equal(result.error_type, None,
                     "recording an absent optional observation does not "
                     "raise - it once did, because the event carried "
                     "`requirement` both explicitly and inside its "
                     "detail dict")

        checks.equal(len(result.missing_required), 0,
                     "an absent OPTIONAL_DIAGNOSTIC is not recorded as "
                     "missing")

        checks.equal(len(result.failed_checks()), 0,
                     "and does not fail anything")

        kinds = [e["kind"] for e in bench.events()]

        checks.ok("observation_absent" in kinds,
                  "and it still reaches the event log")

    checks.section("every observation path survives being exercised")

    def every_path(ctx):
        ctx.observed("required and correct", 2, expected=2,
                     requirement=Requirement.REQUIRED)
        ctx.observed("optional and present", 1,
                     requirement=Requirement.OPTIONAL_DIAGNOSTIC)
        ctx.observed("optional and absent", None,
                     requirement=Requirement.OPTIONAL_DIAGNOSTIC)
        ctx.observed("not available but reported", 5,
                     requirement=Requirement.NOT_AVAILABLE)
        ctx.observed("required with a matcher", 10,
                     requirement=Requirement.REQUIRED,
                     matches=lambda v: v > 5)
        ctx.require_observation("a present field", "value")

    with Bench() as bench:
        result = _run(bench, _catalogue(every_path))

        checks.equal(result.error_type, None,
                     "no observation path raises")

        checks.equal(len(result.failed_checks()), 0,
                     "and none of the satisfied paths fails")

        checks.equal(len(result.missing_required), 0,
                     "nothing REQUIRED was missing")

    def wrong_value(ctx):
        ctx.observed("the protocol version", 1, expected=2,
                     requirement=Requirement.REQUIRED)

    with Bench() as bench:
        result = _run(bench, _catalogue(wrong_value))

        checks.equal(result.verdict, Verdict.FAIL,
                     "a REQUIRED observation with the WRONG value "
                     "FAILs - the contract was violated, not merely "
                     "unobserved")

        checks.equal(len(result.missing_required), 0,
                     "and is not recorded as missing, because it was "
                     "observed")

    checks.section("require_observation demands presence, not equality")

    def empty_id(ctx):
        ctx.require_observation("the measurement id", "")

    with Bench() as bench:
        result = _run(bench, _catalogue(empty_id))

        checks.equal(result.verdict, Verdict.FAIL,
                     "an empty string does not satisfy a required "
                     "presence - this is the B10 defect where two empty "
                     "ids counted as two distinct ids")

    for absent in (None, [], ""):
        with Bench() as bench:
            result = _run(
                bench,
                _catalogue(lambda ctx, v=absent: ctx.require_observation(
                    "a required field", v)))

            checks.equal(result.verdict, Verdict.FAIL,
                         "{!r} does not satisfy a required "
                         "presence".format(absent))

    # ------------------------------------------------------------------
    checks.section("iteration kinds resolve the right profile limit")

    checks.equal(sorted(IterationKind.ALL),
                 ["MEASUREMENT", "MISSION", "MOVEMENT", "OPEN_CYCLE",
                  "REQUEST", "ROTATION"],
                 "the six kinds the task specifies")

    for kind in IterationKind.ALL:
        checks.ok(kind in IterationKind.PROFILE_LIMIT,
                  "{} resolves to a profile limit".format(kind))

    checks.equal(IterationKind.PROFILE_LIMIT[IterationKind.MOVEMENT],
                 "max_movements",
                 "a MOVEMENT test is bounded by max_movements, NOT by "
                 "the campaign it lives in - B11 contains servo, "
                 "carousel and serial endurance and the old "
                 "campaign-based lookup gave all of them max_requests")

    checks.equal(IterationKind.PROFILE_LIMIT[IterationKind.ROTATION],
                 "max_rotations",
                 "and a ROTATION test by max_rotations")

    catalogue = registry()

    b11 = catalogue.tests_in("B11")

    kinds = {t.test_id: t.iteration_kind for t in b11
             if t.default_iterations is not None}

    checks.ok(len(set(kinds.values())) > 1,
              "B11's repeated tests do NOT all share one iteration "
              "kind, which is exactly why the campaign-based lookup was "
              "wrong: {}".format(kinds))

    checks.section("every repeated test declares what it repeats")

    for test in catalogue.all_tests():
        if test.default_iterations is None:
            continue

        checks.ok(test.iteration_kind in IterationKind.ALL,
                  "{} declares iteration_kind {}".format(
                      test.test_id, test.iteration_kind))

    checks.section("a run below the qualification minimum cannot "
                   "qualify")

    def counted(ctx):
        ctx.iterations()
        ctx.check(True, "ran")

    catalogue = _catalogue(
        counted, default_iterations=100, max_iterations=1000,
        iteration_kind=IterationKind.OPEN_CYCLE,
        qualification_min_iterations=100,
        characterization_min_iterations=10)

    with Bench() as bench:
        bench.context.iteration_overrides["*"] = 20

        result = _run(bench, catalogue)

        checks.equal(result.verdict, Verdict.CHARACTERIZATION,
                     "20 iterations against a qualification minimum of "
                     "100 produces CHARACTERIZATION, never a "
                     "qualification pass")

        checks.equal(result.qualified, False,
                     "and the result says it is not qualified")

        checks.ok("100" in result.reason,
                  "the reason names the minimum it fell short of")

    with Bench() as bench:
        result = _run(bench, catalogue)

        checks.equal(result.qualified, True,
                     "the declared default meets its own minimum")

    checks.section("unsafe iteration counts are still refused")

    for bad, description in ((0, "zero"), (-1, "a negative count"),
                             (True, "a boolean"),
                             (10 ** 6, "a count above the ceiling")):
        with Bench() as bench:
            bench.context.iteration_overrides["*"] = bad

            result = _run(bench, catalogue)

            checks.equal(result.verdict, Verdict.FAIL,
                         "{} is refused".format(description))

    # ------------------------------------------------------------------
    checks.section("requirements are complete in both directions")

    catalogue = registry()

    checks.equal(catalogue.traceability_problems(), [],
                 "no orphan requirement and no unknown requirement id")

    forward = catalogue.requirement_to_tests()
    backward = catalogue.test_to_requirements()

    checks.equal(len(backward), len(catalogue),
                 "every test appears in the test -> requirement map")

    for test_id, names in sorted(backward.items()):
        checks.ok(bool(names),
                  "{} names at least one requirement".format(test_id))

    hardware_requirements = [
        r for r in requirements_module.all_requirements()
        if r.verified_by == requirements_module.VerifiedBy.HARDWARE_TEST
    ]

    for requirement in hardware_requirements:
        checks.ok(bool(forward.get(requirement.requirement_id)),
                  "{} is claimed by at least one test".format(
                      requirement.requirement_id))

    checks.section("a requirement with no authority may only "
                   "characterize")

    unauthoritative = [r for r in requirements_module.all_requirements()
                       if not r.authoritative]

    checks.ok(bool(unauthoritative),
              "some requirements have no authoritative source, and the "
              "catalogue says so rather than inventing one")

    for requirement in requirements_module.all_requirements():
        checks.ok(bool(requirement.rationale),
                  "{} explains why it exists".format(
                      requirement.requirement_id))

        checks.ok(requirement.source in requirements_module.Source.ALL,
                  "{} names a recognised source".format(
                      requirement.requirement_id))

    return checks.report()


if __name__ == "__main__":
    sys.exit(cli(run, __doc__))
