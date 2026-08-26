"""
The catalogue itself: ids, completeness, gates and cycles.

The most valuable suite in this folder, because everything else in the
framework assumes the catalogue is sound. A duplicate id makes every
result that quotes it ambiguous; a test with no expected result cannot
fail; a prerequisite cycle is a gate that can never open.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

if str(HERE.parent.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent.parent))

from hardware.core.model import (Automation, Safety,        # noqa: E402
                                 TestDefinition)
from hardware.core.registry import Registry, RegistryError  # noqa: E402
from hardware.offline_tests.harness import (Checks, cli,     # noqa: E402
                                            registry)


def run():
    checks = Checks("hardware/offline_tests/test_registry.py")

    catalogue = registry()

    checks.section("the catalogue is structurally sound")

    problems = catalogue.check()

    checks.ok(not problems,
              "the catalogue reports no structural problems")

    for problem in problems:
        print("       {}".format(problem))

    checks.ok(len(catalogue) > 0, "at least one test is registered")

    checks.section("every test id is unique")

    ids = [t.test_id for t in catalogue.all_tests()]

    checks.equal(len(ids), len(set(ids)), "no duplicate test ids")

    checks.ok(all(i == i.upper() for i in ids),
              "every test id is upper case, so --run is predictable")

    checks.ok(all(i.startswith("HW-") for i in ids),
              "every test id starts with HW-")

    checks.section("every test is documented well enough to be run")

    for definition in catalogue.all_tests():
        faults = definition.problems()

        checks.ok(not faults, "{} has objective, setup, preconditions, "
                              "procedure, expected result, failure "
                              "criteria and captures".format(
                                  definition.test_id))

        if faults:
            for fault in faults:
                print("       {}".format(fault))

    checks.section("prerequisites name something, and form no cycle")

    checks.equal(catalogue.unknown_prerequisites(), [],
                 "every prerequisite names a test or a campaign")

    cycles = catalogue.cycles()

    checks.equal(cycles, [], "the prerequisite graph has no cycles")

    checks.section("a cycle WOULD be detected")

    cyclic = Registry()

    cyclic.campaign("X", "X", "cyclic", "for the test")

    def nothing(_context):                             # pragma: no cover
        pass

    def definition(test_id, prerequisites):
        return TestDefinition(
            test_id=test_id, campaign="X", layer="X",
            title="t", objective="o", hardware_setup="h",
            preconditions="p", procedure=("s",), expected="e",
            failure_criteria="f", captures=("c",),
            safety=Safety.READ_ONLY, automation=Automation.AUTOMATIC,
            run=nothing, prerequisites=prerequisites)

    cyclic.add(definition("HW-X-001", ("HW-X-002",)))
    cyclic.add(definition("HW-X-002", ("HW-X-001",)))

    found = cyclic.cycles()

    checks.ok(bool(found),
              "a two-test cycle is detected rather than recursed into")

    checks.ok(any("prerequisite cycle" in p for p in cyclic.check()),
              "the cycle is reported by check() as a sentence")

    checks.section("a malformed definition is refused at registration")

    checks.raises(
        ValueError,
        lambda: TestDefinition(
            test_id="HW-X-003", campaign="X", layer="X", title="t",
            objective="o", hardware_setup="h", preconditions="p",
            procedure=(), expected="e", failure_criteria="f",
            captures=("c",), safety=Safety.READ_ONLY,
            automation=Automation.AUTOMATIC, run=nothing),
        "a test with an empty procedure is refused")

    checks.raises(
        ValueError,
        lambda: TestDefinition(
            test_id="HW-X-004", campaign="X", layer="X", title="t",
            objective="o", hardware_setup="h", preconditions="p",
            procedure=("s",), expected="e", failure_criteria="f",
            captures=(), safety=Safety.READ_ONLY,
            automation=Automation.AUTOMATIC, run=nothing),
        "a test that says nothing about what to capture is refused")

    checks.raises(
        ValueError,
        lambda: TestDefinition(
            test_id="HW-X-005", campaign="X", layer="X", title="t",
            objective="o", hardware_setup="h", preconditions="p",
            procedure=("s",), expected="e", failure_criteria="f",
            captures=("c",), safety="NOT_A_SAFETY_CLASS",
            automation=Automation.AUTOMATIC, run=nothing),
        "an unknown safety class is refused")

    checks.raises(
        ValueError,
        lambda: TestDefinition(
            test_id="HW-X-006", campaign="X", layer="X", title="t",
            objective="o", hardware_setup="h", preconditions="p",
            procedure=("s",), expected="e", failure_criteria="f",
            captures=("c",), safety=Safety.ENDURANCE,
            automation=Automation.AUTOMATIC, run=nothing),
        "an ENDURANCE test with no iteration ceiling is refused")

    checks.raises(
        RegistryError,
        lambda: cyclic.add(definition("HW-X-001", ())),
        "a duplicate test id is refused")

    checks.section("the layer pyramid is gated in the right direction")

    campaigns = {c.campaign_id: c for c in catalogue.all_campaigns()}

    for expected in ("B0", "B1", "B2", "B3", "B4", "B5", "B6", "B7",
                     "B8", "B9", "B10", "B11", "B12"):
        checks.ok(expected in campaigns,
                  "campaign {} is registered".format(expected))

    checks.ok(not campaigns["B0"].prerequisites,
              "B0 has no prerequisites - it is the floor")

    def gated_by(campaign_id, ancestor):
        """Whether campaign_id depends on ancestor, transitively."""
        seen = set()
        pending = list(campaigns[campaign_id].prerequisites)

        while pending:
            name = pending.pop()

            if name in seen:
                continue

            seen.add(name)

            if name == ancestor:
                return True

            pending.extend(campaigns[name].prerequisites)

        return False

    checks.ok(gated_by("B5", "B3"),
              "B5 is gated by B3 - no carousel evidence while H-002 is "
              "open")

    checks.ok(gated_by("B8", "B5") and gated_by("B8", "B7"),
              "B8 is gated by both the carousel and the sensor layers")

    checks.ok(gated_by("B12", "B10") and gated_by("B12", "B0"),
              "B12 is gated all the way down to B0 - a rehearsal is "
              "never the first hardware test")

    checks.section("selection resolves ids and campaigns")

    selected = catalogue.select(["B0"])

    checks.equal([t.test_id for t in selected],
                 [t.test_id for t in catalogue.tests_in("B0")],
                 "a campaign id selects its whole campaign")

    checks.equal([t.test_id for t in catalogue.select(["hw-b0-001"])],
                 ["HW-B0-001"],
                 "a lower-case test id resolves")

    checks.raises(RegistryError,
                  lambda: catalogue.select(["HW-NOT-A-TEST"]),
                  "an unknown name is refused rather than ignored")

    duplicated = catalogue.select(["B0", "HW-B0-001"])

    checks.equal(len({t.test_id for t in duplicated}), len(duplicated),
                 "a test named twice is selected once")

    checks.section("the H-002 investigation is registered and gating")

    h002 = [t for t in catalogue.all_tests() if t.assumption == "H-002"]

    checks.ok(len(h002) >= 4,
              "at least four tests are traced to the H-002 assumption")

    core = catalogue.get("HW-B3-001")

    checks.equal(core.safety, Safety.MOTION,
                 "the H-002 investigation is classified MOTION")

    checks.equal(core.automation, Automation.OPERATOR_ASSISTED,
                 "the H-002 investigation needs an operator - the "
                 "encoder cannot tell you what the shaft did")

    checks.ok("operator notes" in [c.lower() for c in core.captures]
              or any("operator" in c.lower() for c in core.captures),
              "the H-002 investigation captures the operator's "
              "observation")

    checks.section("every test that can fail can also be traced")

    for definition in catalogue.all_tests():
        if definition.safety == Safety.READ_ONLY:
            continue

        checks.ok(bool(definition.defect_prefix),
                  "{} names a defect family, so a failure gets a "
                  "persistent id".format(definition.test_id))

    return checks.report()


if __name__ == "__main__":
    sys.exit(cli(run, __doc__))
