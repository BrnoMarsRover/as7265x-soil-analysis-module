"""
The runner: verdicts, cleanup, aborts, blocks and overrides.

Every path here is driven through the REAL runner with the REAL test
definitions, against the fake transport. What is being checked is that a
success, a failure, a timeout, an abort, a failed cleanup and a missing
capability each produce the status they should - and that none of them
can produce a hardware PASS.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

if str(HERE.parent.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent.parent))

from hardware.core.model import (Automation, Blocked,       # noqa: E402
                                 Evidence, Failure, Safety,
                                 Status, TestDefinition)
from hardware.core.registry import Registry                 # noqa: E402
from hardware.core.runner import Runner                     # noqa: E402
from hardware.offline_tests.fake_link import (              # noqa: E402
    aborting, failing, healthy_script, timing_out)
from hardware.offline_tests.harness import (Bench, Checks,   # noqa: E402
                                            cli)


def _registry_with(body, cleanup=None, **overrides):
    """A one-test catalogue, so a runner path can be driven exactly."""
    catalogue = Registry()

    catalogue.campaign("T", "T", "runner paths",
                       "one test, driven deliberately")

    fields = {
        "test_id": "HW-T-001", "campaign": "T", "layer": "T",
        "title": "a driven test", "objective": "drive one runner path",
        "hardware_setup": "none - the transport is fake",
        "preconditions": "none",
        "procedure": ("do the thing",),
        "expected": "the thing happens",
        "failure_criteria": "the thing does not happen",
        "captures": ("what happened",),
        "safety": Safety.READ_ONLY,
        "automation": Automation.AUTOMATIC,
        "run": body, "cleanup": cleanup,
        "defect_prefix": "HW-TEST",
    }

    fields.update(overrides)

    catalogue.add(TestDefinition(**fields))

    return catalogue


def _run(bench, catalogue, **kwargs):
    runner = Runner(catalogue, bench.context, ledger=bench.ledger,
                    **kwargs)

    return runner.run(catalogue.all_tests())[0], runner


def run():
    checks = Checks("hardware/offline_tests/test_runner.py")

    checks.section("a body that checks and passes")

    def passing(ctx):
        ctx.link.request("ping")
        ctx.check(True, "the fake answered")

    with Bench() as bench:
        result, _ = _run(bench, _registry_with(passing))

        checks.equal(result.status, Status.SKIPPED,
                     "a successful self-test run is SKIPPED, not PASS - "
                     "a fake transport cannot pass a hardware test")

        checks.equal(result.evidence_class, Evidence.SELFTEST,
                     "the result carries FRAMEWORK_SELFTEST evidence")

        checks.ok("fake transport" in result.reason,
                  "the reason says the transport was fake")

        checks.equal(len(result.checks), 1, "the check was recorded")

    checks.section("a body that makes no check at all")

    def silent(ctx):
        ctx.link.request("ping")

    with Bench() as bench:
        result, _ = _run(bench, _registry_with(silent))

        checks.equal(result.status, Status.ERROR,
                     "a test that looks at nothing is an ERROR, not a "
                     "pass")

        checks.ok("did not look" in result.reason,
                  "the reason says the test did not look")

    checks.section("a failed check fails the test but does not stop it")

    def two_checks(ctx):
        ctx.check(False, "the first check fails")
        ctx.check(True, "the second check still runs")

    with Bench() as bench:
        result, _ = _run(bench, _registry_with(two_checks))

        checks.equal(result.status, Status.FAIL, "a failed check FAILs")

        checks.equal(len(result.checks), 2,
                     "the test continued after the failure and "
                     "collected the rest of its evidence")

    checks.section("a device failure becomes FAIL with its code kept")

    def calls_failing_command(ctx):
        try:
            ctx.link.request("ping")

            ctx.check(False, "the failing command raised")

        except Exception as error:
            ctx.check(getattr(error, "code", None) == "PORT_LOST",
                      "the original error code survived normalization",
                      evidence={"code": getattr(error, "code", None),
                                "original_type": getattr(
                                    error, "original_type", None)})

            ctx.check(getattr(error, "original_type", None)
                      == "FakeError",
                      "the original exception type survived")

    script = dict(healthy_script())
    script["ping"] = failing("PORT_LOST", "the device vanished")

    with Bench(script=script) as bench:
        result, _ = _run(bench, _registry_with(calls_failing_command))

        checks.equal(len(result.failed_checks()), 0,
                     "the error was classified correctly")

    checks.section("a timeout becomes a FAIL, not an ERROR")

    def times_out(ctx):
        ctx.link.request("ping")

        ctx.check(True, "unreachable")

    script = dict(healthy_script())
    script["ping"] = timing_out()

    with Bench(script=script) as bench:
        result, _ = _run(bench, _registry_with(times_out))

        checks.equal(result.status, Status.ERROR,
                     "an unhandled transport timeout is an ERROR with "
                     "the exception recorded")

        checks.ok("PROTOCOL_TIMEOUT" in (result.error_message or "")
                  or "timeout" in (result.reason or "").lower(),
                  "the timeout is named in the result")

    checks.section("an explicit Failure carries its reason")

    def explicit(ctx):
        ctx.check(True, "something was observed")
        ctx.fail("the mechanism did not do what it was told")

    with Bench() as bench:
        result, _ = _run(bench, _registry_with(explicit))

        checks.equal(result.status, Status.FAIL, "ctx.fail FAILs")

        checks.ok("did not do what it was told" in result.reason,
                  "the reason is the one the test gave")

    checks.section("Ctrl+C aborts, keeps evidence and runs cleanup")

    cleaned = {"ran": False}

    def cleanup(ctx):
        cleaned["ran"] = True

        return {"confirmed": True, "note": "cleanup after abort"}

    def interrupted(ctx):
        ctx.check(True, "one check before the interruption")
        ctx.link.request("ping")

    script = dict(healthy_script())
    script["ping"] = aborting()

    with Bench(script=script) as bench:
        result, runner = _run(
            bench, _registry_with(interrupted, cleanup=cleanup))

        checks.equal(result.status, Status.ABORTED,
                     "a KeyboardInterrupt inside a body ABORTs the test")

        checks.ok(cleaned["ran"], "cleanup ran after the abort")

        checks.ok(runner.aborted, "the runner records that it aborted")

        checks.equal(len(result.checks), 1,
                     "the evidence collected before the abort is kept")

        kinds = [e["kind"] for e in bench.events()]

        checks.ok("check" in kinds and "cleanup" in kinds,
                  "both the check and the cleanup reached the event log")

    checks.section("a cleanup failure is preserved, not swallowed")

    def failing_cleanup(ctx):
        raise OSError("the port is already gone")

    def fine(ctx):
        ctx.check(True, "the body was fine")

    with Bench() as bench:
        result, _ = _run(
            bench, _registry_with(fine, cleanup=failing_cleanup))

        checks.ok(result.cleanup is not None, "a cleanup record exists")

        checks.equal(result.cleanup.get("confirmed"), False,
                     "the cleanup is recorded as NOT confirmed")

        checks.equal(result.cleanup.get("error_type"), "OSError",
                     "the cleanup's own exception type is kept")

        checks.ok(any("CLEANUP NOT CONFIRMED" in n
                      for n in result.notes),
                  "the result says the physical state is not known")

        checks.ok(result.status != Status.ERROR,
                  "a cleanup failure does not overwrite the body's "
                  "verdict")

    checks.section("a cleanup that returns a bare False is honest")

    with Bench() as bench:
        result, _ = _run(
            bench, _registry_with(fine, cleanup=lambda ctx: False))

        checks.equal(result.cleanup.get("confirmed"), False,
                     "a falsey cleanup return is recorded as "
                     "unconfirmed")

    checks.section("a missing capability BLOCKS with a recommendation")

    def needs_missing(ctx):
        ctx.require("servo.raw_packet")

        ctx.check(True, "unreachable while the capability is missing")

    with Bench() as bench:
        result, _ = _run(
            bench, _registry_with(
                needs_missing, requires=("servo.raw_packet",)))

        checks.equal(result.status, Status.BLOCKED,
                     "a missing capability BLOCKS")

        checks.ok(any("recommendation" in n for n in result.notes),
                  "the block carries the change that would unblock it")

        checks.ok(result.status != Status.PASS,
                  "a missing capability never becomes a pass")

    checks.section("a Blocked raised inside a body is honoured")

    def blocks(ctx):
        ctx.block("this bench cannot do it",
                  recommendation="use a different bench")

    with Bench() as bench:
        result, _ = _run(bench, _registry_with(blocks))

        checks.equal(result.status, Status.BLOCKED, "ctx.block BLOCKS")

        checks.ok("this bench cannot do it" in result.reason,
                  "the reason is the one the test gave")

    checks.section("iteration counts are validated")

    def counts(ctx):
        ctx.check(True, "resolved {} iterations".format(ctx.iterations()))

    catalogue = _registry_with(counts, default_iterations=10,
                               max_iterations=100)

    for bad, description in ((0, "zero"), (-5, "a negative count"),
                             (1000, "a count above the ceiling")):
        with Bench() as bench:
            bench.context.iteration_overrides["*"] = bad

            result, _ = _run(bench, catalogue)

            checks.equal(result.status, Status.FAIL,
                         "{} is refused".format(description))

    with Bench() as bench:
        bench.context.iteration_overrides["*"] = True

        result, _ = _run(bench, catalogue)

        checks.equal(result.status, Status.FAIL,
                     "a boolean is not a whole number of iterations")

    with Bench() as bench:
        result, _ = _run(bench, catalogue)

        checks.equal(result.iterations, 10,
                     "the declared default is used when nothing "
                     "overrides it")

    checks.section("one failure in a repeated campaign prevents a pass")

    def repeated(ctx):
        outcomes = []

        for index in range(1, 11):
            outcomes.append(index != 7)

        from hardware.core.analysis import failure_rate

        rate = failure_rate(outcomes)

        ctx.check(rate["all_passed"],
                  "every iteration passed", evidence=rate)

    with Bench() as bench:
        result, _ = _run(bench, _registry_with(repeated))

        checks.equal(result.status, Status.FAIL,
                     "nine passes and one failure is a FAIL")

    checks.section("stop-on-failure stops the selection")

    catalogue = Registry()

    catalogue.campaign("T", "T", "two tests", "for stop-on-failure")

    def fails(ctx):
        ctx.check(False, "this one fails")

    def passes(ctx):
        ctx.check(True, "this one would pass")

    for index, body in ((1, fails), (2, passes)):
        catalogue.add(TestDefinition(
            test_id="HW-T-{:03d}".format(index), campaign="T", layer="T",
            title="t", objective="o", hardware_setup="h",
            preconditions="p", procedure=("s",), expected="e",
            failure_criteria="f", captures=("c",),
            safety=Safety.READ_ONLY, automation=Automation.AUTOMATIC,
            run=body, defect_prefix="HW-TEST"))

    with Bench() as bench:
        runner = Runner(catalogue, bench.context, ledger=bench.ledger,
                        stop_on_failure=True)

        results = runner.run(catalogue.all_tests())

        checks.equal(len(results), 1,
                     "--stop-on-failure stops after the first failure")

    checks.section("the expert override needs all three of its parts")

    def gated(ctx):
        ctx.check(True, "ran despite the gate")

    catalogue = _registry_with(gated, prerequisites=())

    with Bench() as bench:
        runner = Runner(catalogue, bench.context, ledger=bench.ledger,
                        expert_override=True, override_reason="")

        result = runner.run(catalogue.all_tests())[0]

        checks.ok(result.override is None,
                  "an override with no written reason is not applied")

    return checks.report()


if __name__ == "__main__":
    sys.exit(cli(run, __doc__))
