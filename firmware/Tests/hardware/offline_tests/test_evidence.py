"""
Evidence: the schemas, the stamps, and what a dry run may not produce.

The framework's promises about honesty are all promises about what gets
written down. This suite checks the writing.
"""

import csv
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

if str(HERE.parent.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent.parent))

from hardware.core.evidence import (campaign_verdicts,       # noqa: E402
                                    count_statuses,
                                    render_defect,
                                    render_markdown)
from hardware.core.model import (Evidence, Mode, Status,     # noqa: E402
                                 TestResult)
from hardware.offline_tests.harness import (Bench, Checks,   # noqa: E402
                                            cli, registry)


def run():
    checks = Checks("hardware/offline_tests/test_evidence.py")

    catalogue = registry()

    checks.section("a run directory is stamped with its mode")

    with Bench() as bench:
        checks.ok("SELFTEST" in bench.evidence.run_id,
                  "the run id carries the mode, so a directory cannot "
                  "be mistaken at a glance")

        checks.equal(bench.evidence.evidence_class, Evidence.SELFTEST,
                     "a self-test run writes FRAMEWORK_SELFTEST "
                     "evidence")

        manifest = json.loads(
            (bench.evidence.directory / "run_manifest.json")
            .read_text(encoding="utf-8"))

        for field in ("run_id", "mode", "evidence_class", "started_utc",
                      "framework_version", "git", "host", "profile"):
            checks.ok(field in manifest,
                      "the manifest carries '{}'".format(field))

        checks.ok("warning" in manifest,
                  "a non-hardware manifest carries a warning in words")

        checks.ok("NO_HARDWARE_EVIDENCE" in manifest["warning"],
                  "the warning says NO_HARDWARE_EVIDENCE")

        checks.ok(manifest["profile"]["production_configuration"][
                      "servo"]["position_tolerance"] == 15,
                  "the manifest snapshots the production tolerance, so "
                  "a result stays interpretable if config.py changes")

    checks.section("events are one JSON object per line, flushed")

    with Bench() as bench:
        bench.evidence.event("example", value=1, nested={"a": [1, 2]})
        bench.evidence.event("example", value=2)

        lines = (bench.evidence.directory / "events.jsonl").read_text(
            encoding="utf-8").splitlines()

        checks.ok(len(lines) >= 2,
                  "events reach the file before the run ends")

        for line in lines:
            parsed = json.loads(line)

            checks.ok(
                {"at", "run_id", "mode", "evidence_class", "kind"}
                <= set(parsed),
                "every event carries at, run_id, mode, evidence_class "
                "and kind")

    checks.section("a caller field cannot overwrite the envelope")

    with Bench() as bench:
        bench.evidence.event(
            "check", kind="OPERATOR", mode="EXECUTE",
            evidence_class="HARDWARE", run_id="somebody-elses-run",
            ordinary="kept as it is")

        written = bench.events()[-1]

        checks.equal(written["kind"], "check",
                     "the event's own kind survives a caller field of "
                     "the same name")

        checks.equal(written["mode"], "SELFTEST",
                     "a test body cannot relabel a self-test run as "
                     "EXECUTE")

        checks.equal(written["evidence_class"], Evidence.SELFTEST,
                     "and cannot relabel its evidence as HARDWARE - the "
                     "one field in the log that must not be forgeable")

        checks.equal(written["field_kind"], "OPERATOR",
                     "the colliding field is kept under a prefix rather "
                     "than dropped")

        checks.equal(written["ordinary"], "kept as it is",
                     "an ordinary field is untouched")

    checks.section("a check reaches the log as a check")

    with Bench() as bench:
        from hardware.core.registry import Registry
        from hardware.core.model import (Automation, Safety,
                                         TestDefinition)
        from hardware.core.runner import Runner

        def observes(ctx):
            ctx.check(True, "an automatic observation")
            ctx.check(True, "a human observation", kind="OPERATOR")

        # A local catalogue, deliberately NOT called `catalogue`: the
        # real one is in scope for the rest of this suite and shadowing
        # it here made the sections below look up HW-B0-001 in a
        # two-test registry.
        scratch = Registry()
        scratch.campaign("T", "T", "checks", "for the test")
        scratch.add(TestDefinition(
            test_id="HW-T-002", campaign="T", layer="T", title="t",
            objective="o", hardware_setup="h", preconditions="p",
            procedure=("s",), expected="e", failure_criteria="f",
            captures=("c",), safety=Safety.READ_ONLY,
            automation=Automation.AUTOMATIC, run=observes,
            defect_prefix="HW-TEST"))

        Runner(scratch, bench.context, ledger=bench.ledger).run(
            scratch.all_tests())

        events = [e for e in bench.events() if e["kind"] == "check"]

        checks.equal(len(events), 2,
                     "both checks reached the event log as checks")

        kinds = sorted(e["check_kind"] for e in events)

        checks.equal(kinds, ["AUTOMATIC", "OPERATOR"],
                     "and each says whether a machine or a human made "
                     "the observation")

    checks.section("an unserializable value does not lose the event")

    with Bench() as bench:
        class Awkward:
            pass

        bench.evidence.event("awkward", value=Awkward())

        line = (bench.evidence.directory / "events.jsonl").read_text(
            encoding="utf-8").splitlines()[-1]

        parsed = json.loads(line)

        checks.ok("Awkward" in str(parsed.get("value")),
                  "an unserializable value becomes its repr rather than "
                  "an exception")

    checks.section("measurements.csv keeps one header")

    with Bench() as bench:
        bench.evidence.measurement({"a": 1, "b": 2})
        bench.evidence.measurement({"a": 3, "b": 4})
        bench.evidence.measurement({"a": 5})
        bench.evidence.measurement({"a": 7, "c": 9})

        path = bench.evidence.directory / "measurements.csv"

        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))

        checks.equal(len(rows), 4, "every row was written")

        checks.equal(sorted(rows[0].keys()), ["a", "b"],
                     "the header is fixed by the first row")

        checks.equal(rows[2]["b"], "",
                     "a missing field becomes blank, not a shifted row")

        extra = [e for e in bench.events()
                 if e["kind"] == "measurement_extra_fields"]

        checks.ok(bool(extra),
                  "a field the header cannot hold is kept in the event "
                  "log rather than dropped")

    checks.section("the summary is complete and honest")

    definition = catalogue.get("HW-B0-001")

    results = [
        TestResult(definition, Status.PASS, Evidence.HARDWARE, "ok"),
        TestResult(catalogue.get("HW-B0-002"), Status.FAIL,
                   Evidence.HARDWARE, "no"),
        TestResult(catalogue.get("HW-B0-003"), Status.BLOCKED,
                   Evidence.HARDWARE, "missing"),
    ]

    counts = count_statuses(results)

    checks.equal(counts[Status.PASS], 1, "passes are counted")
    checks.equal(counts[Status.FAIL], 1, "failures are counted")
    checks.equal(counts[Status.BLOCKED], 1, "blocks are counted")

    verdicts = campaign_verdicts(results)

    checks.equal(verdicts["B0"]["verdict"], "FAIL",
                 "a campaign containing a FAIL is FAIL")

    incomplete = campaign_verdicts([
        TestResult(definition, Status.PASS, Evidence.HARDWARE, "ok"),
        TestResult(catalogue.get("HW-B0-002"), Status.NOT_RUN,
                   Evidence.HARDWARE, "not run"),
    ])

    checks.equal(incomplete["B0"]["verdict"], "INCOMPLETE",
                 "a campaign with an unrun test is INCOMPLETE, not a "
                 "pass")

    all_passed = campaign_verdicts([
        TestResult(definition, Status.PASS, Evidence.HARDWARE, "ok"),
    ])

    checks.equal(all_passed["B0"]["verdict"], "PASS",
                 "a campaign whose every test passed on hardware is "
                 "PASS")

    selftest_passes = campaign_verdicts([
        TestResult(definition, Status.SKIPPED, Evidence.SELFTEST, "fake"),
    ])

    checks.equal(selftest_passes["B0"]["verdict"], "INCOMPLETE",
                 "a campaign made of self-test results is never PASS")

    checks.section("the rendered summary carries the warning")

    markdown = render_markdown({
        "run_id": "HW-X-DRY_RUN", "mode": "DRY_RUN",
        "evidence_class": Evidence.DRY_RUN,
        "finished_utc": "now", "aborted": False,
        "warning": "{} - nothing here is evidence".format(
            Evidence.DRY_RUN),
        "counts": counts,
        "campaign_verdicts": verdicts,
        "results": [r.as_dict() for r in results],
    })

    checks.ok("NO_HARDWARE_EVIDENCE" in markdown,
              "the human-readable summary carries the warning too")

    checks.ok("HW-B0-001" in markdown,
              "the summary lists every test")

    checks.section("a defect record has every field it needs to be "
                   "closable")

    rendered = render_defect({
        "defect_id": "HW-SERVO-001",
        "title": "the encoder disagrees with the mechanism",
        "observed": "2 counts", "expected": "2048 counts",
        "reproduction": ["run HW-B3-001"],
        "suspected_layer": "position register",
        "evidence": {"legs": []},
        "status": "OPEN",
        "test_id": "HW-B3-001", "campaign": "B3",
        "run_id": "HW-X", "assumption": "H-002",
        "raised_utc": "now",
    })

    for heading in ("## Observed", "## Expected", "## Reproduction",
                    "## Evidence", "## Root cause", "## Fix",
                    "## Verification", "## Regression test"):
        checks.ok(heading in rendered,
                  "the defect record has a {} section".format(
                      heading.strip("# ")))

    checks.ok("NOT YET ESTABLISHED" in rendered,
              "an unknown root cause says so rather than being blank")

    checks.section("a self-test run writes a defect to its own directory")

    with Bench() as bench:
        from hardware.core.registry import Registry
        from hardware.core.model import (Automation, Safety,
                                         TestDefinition)

        def raises_defect(ctx):
            ctx.defect(
                title="a scripted defect", observed="x", expected="y",
                reproduction=("run this",), suspected_layer="test")

            ctx.check(True, "the defect was raised")

        scratch = Registry()
        scratch.campaign("T", "T", "defects", "for the test")
        scratch.add(TestDefinition(
            test_id="HW-T-001", campaign="T", layer="T", title="t",
            objective="o", hardware_setup="h", preconditions="p",
            procedure=("s",), expected="e", failure_criteria="f",
            captures=("c",), safety=Safety.READ_ONLY,
            automation=Automation.AUTOMATIC, run=raises_defect,
            defect_prefix="HW-TEST"))

        from hardware.core.runner import Runner

        result = Runner(scratch, bench.context,
                        ledger=bench.ledger).run(
                            scratch.all_tests())[0]

        checks.equal(len(result.defects), 1, "the defect was recorded")

        checks.equal(result.defects[0]["defect_id"], "HW-TEST-001",
                     "the defect id comes from the test's own family")

        files = list((bench.evidence.directory / "defects").glob("*.md"))

        checks.equal(len(files), 1,
                     "the defect was written as its own file")

    checks.section("the ledger only records hardware results")

    with Bench() as bench:
        result = TestResult(definition, Status.SKIPPED,
                            Evidence.SELFTEST, "fake")

        bench.ledger.record(result, "HW-X")

        checks.equal(bench.ledger.status_of("HW-B0-001"), Status.NOT_RUN,
                     "a self-test result does not enter the ledger, so "
                     "it can never open a layer gate")

        hardware = TestResult(definition, Status.PASS,
                              Evidence.HARDWARE, "ok")

        bench.ledger.record(hardware, "HW-Y")

        checks.equal(bench.ledger.status_of("HW-B0-001"), Status.PASS,
                     "a hardware PASS does enter the ledger")

    checks.section("a dry run cannot write hardware evidence")

    with Bench(mode=Mode.DRY_RUN) as bench:
        checks.equal(bench.evidence.evidence_class, Evidence.DRY_RUN,
                     "a DRY_RUN writer produces DRY_RUN evidence")

        result = bench.run("HW-B0-001")

        checks.equal(result.status, Status.READY_FOR_HARDWARE,
                     "a dry run produces READY_FOR_HARDWARE")

        checks.equal(result.evidence_class, Evidence.DRY_RUN,
                     "and it is stamped as a dry run")

        checks.ok(not result.hardware_evidence,
                  "a dry run's result is not hardware evidence")

    return checks.report()


if __name__ == "__main__":
    sys.exit(cli(run, __doc__))
