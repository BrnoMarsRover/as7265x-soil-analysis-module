"""
The gates: nothing physical happens unless it was asked for three times.

This is the suite that would catch the worst possible defect in this
framework - a code path that reaches a serial port from a listing, a
dry run or an ordinary test discovery.
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

if str(HERE.parent.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent.parent))

from hardware.core.context import (HardwareNotPermitted,   # noqa: E402
                                   RunContext)
from hardware.core.model import (Evidence, Mode, Safety,  # noqa: E402
                                 Status, TestControl,
                                 TestDefinition, TestResult)
from hardware.core.operator import Operator                # noqa: E402
from hardware.configuration.profile import Profile         # noqa: E402
from hardware.offline_tests.harness import (Bench, Checks,  # noqa: E402
                                            cli, registry)


ENTRY = HERE.parent / "run_hardware_tests.py"

TESTS_DIR = HERE.parent.parent


def _cli(*arguments, output=None):
    """
    Run the real entry point in a subprocess, as an operator would.

    `output` sends the run's evidence to a temporary directory. THIS
    SUITE MUST NOT WRITE INTO THE REPOSITORY. Without it, each dry run
    below leaves an artifacts/ directory under Tests/hardware/, and
    `Tests/software/entrypoints` - which hashes the whole firmware tree
    before and after - reports every one of them as a file a test
    created. It was right to.
    """
    arguments = list(arguments)

    if output is not None:
        arguments += ["--output", str(output)]

    finished = subprocess.run(
        [sys.executable, str(ENTRY)] + arguments,
        capture_output=True, text=True, timeout=180)

    return finished


def _tree_digest():
    """
    Every file under Tests/hardware, hashed.

    The same discipline `Tests/software/entrypoints` applies to the
    whole firmware tree, applied here to the part this suite touches -
    so a stray artifacts/ directory is caught by the suite that created
    it rather than by an unrelated one an hour later.
    """
    digest = {}

    for path in sorted((HERE.parent).rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue

        digest[path.as_posix()] = (
            path.stat().st_size, path.stat().st_mtime_ns)

    return digest


def _changed(before, after):
    return sorted(
        name for name in set(before) | set(after)
        if before.get(name) != after.get(name))


def run():
    checks = Checks("hardware/offline_tests/test_safety.py")

    catalogue = registry()

    checks.section("a context that is not EXECUTE refuses every "
                   "hardware operation")

    for mode in (Mode.LIST, Mode.DESCRIBE, Mode.DRY_RUN):
        context = RunContext(
            mode, Profile.default(), None,
            Operator(None, interactive=False))

        checks.raises(
            HardwareNotPermitted,
            lambda c=context: c.require_hardware_mode("open a port"),
            "{} mode refuses to open a port".format(mode))

        checks.raises(
            HardwareNotPermitted,
            lambda c=context: c.link.enumerate_ports(),
            "{} mode refuses even to enumerate ports".format(mode))

        checks.raises(
            HardwareNotPermitted,
            lambda c=context: c.link.require_link(),
            "{} mode refuses to construct a transport".format(mode))

    checks.section("EXECUTE without a confirmation is still refused")

    unconfirmed = RunContext(
        Mode.EXECUTE, Profile.default(), None,
        Operator(None, interactive=False), hardware_confirmed=False)

    checks.raises(
        HardwareNotPermitted,
        lambda: unconfirmed.require_hardware_mode("move the servo"),
        "EXECUTE without hardware_confirmed refuses to move anything")

    checks.section("a fake transport cannot be installed in a real run")

    for mode in (Mode.EXECUTE, Mode.DRY_RUN, Mode.LIST):
        checks.raises(
            ValueError,
            lambda m=mode: RunContext(
                m, Profile.default(), None,
                Operator(None, interactive=False), fake_transport=True),
            "{} mode refuses a fake transport".format(mode))

    checks.section("PASS is unreachable without hardware evidence")

    definition = catalogue.get("HW-B0-001")

    for evidence_class in (Evidence.DRY_RUN, Evidence.SELFTEST):
        checks.raises(
            ValueError,
            lambda e=evidence_class: TestResult(
                definition, Status.PASS, e),
            "a {} result cannot be constructed as PASS".format(
                evidence_class))

    passing = TestResult(definition, Status.PASS, Evidence.HARDWARE)

    checks.equal(passing.reported_status(), Status.PASS,
                 "a hardware PASS reports as PASS")

    checks.section("--list and --describe touch nothing and exit 0")

    listing = _cli("--list")

    checks.equal(listing.returncode, 0, "--list exits 0")

    checks.ok("NOTHING BELOW HAS BEEN RUN" in listing.stdout,
              "--list says plainly that nothing has been run")

    checks.ok("HW-B3-001" in listing.stdout,
              "--list includes the H-002 investigation")

    describe = _cli("--describe", "HW-B3-001")

    checks.equal(describe.returncode, 0, "--describe exits 0")

    checks.ok("NOT_RUN" in describe.stdout,
              "--describe reports the result as NOT_RUN")

    checks.ok("Procedure" in describe.stdout
              and "Failure criteria" in describe.stdout,
              "--describe prints the procedure and the failure criteria")

    missing = _cli("--describe", "HW-NOT-A-TEST")

    checks.equal(missing.returncode, 2,
                 "--describe on an unknown id exits 2")

    checks.section("--run without --confirm-hardware is refused")

    refused = _cli("--run", "HW-B1-001")

    checks.equal(refused.returncode, 3,
                 "--run without --confirm-hardware exits 3")

    checks.ok("REFUSED" in refused.stderr,
              "the refusal says so in as many words")

    checks.ok("--dry-run" in refused.stderr,
              "the refusal names the safe alternative")

    checks.section("a bare invocation lists rather than runs")

    bare = _cli()

    checks.equal(bare.returncode, 0, "no arguments exits 0")

    checks.ok("catalogue" in bare.stdout.lower(),
              "no arguments prints the catalogue")

    checks.section("--dry-run opens nothing, for every campaign")

    scratch = Path(tempfile.mkdtemp(prefix="freya-hw-dryrun-"))

    tree_before = _tree_digest()

    for campaign in catalogue.all_campaigns():
        finished = _cli("--dry-run", "--run-campaign",
                        campaign.campaign_id, "--non-interactive",
                        output=scratch)

        checks.ok(finished.returncode in (0, 5),
                  "--dry-run {} completes without an error".format(
                      campaign.campaign_id))

        checks.ok("DRY RUN" in finished.stdout,
                  "--dry-run {} says it is a dry run".format(
                      campaign.campaign_id))

        checks.ok("NO_HARDWARE_EVIDENCE" in finished.stdout,
                  "--dry-run {} stamps its output "
                  "NO_HARDWARE_EVIDENCE".format(campaign.campaign_id))

        checks.ok("PASS" not in finished.stdout.replace(
                      "PASSED", "").replace("passed", ""),
                  "--dry-run {} claims no PASS".format(
                      campaign.campaign_id))

    checks.ok(any(scratch.iterdir()),
              "the dry runs wrote their evidence to the directory they "
              "were given")

    shutil.rmtree(scratch, ignore_errors=True)

    checks.section("running this suite changes nothing in the repository")

    checks.equal(_changed(tree_before, _tree_digest()), [],
                 "thirteen dry runs left no file behind under "
                 "Tests/hardware - a test that pollutes the tree it is "
                 "testing is a test that will be blamed for something "
                 "else later")

    checks.section("ordinary test discovery cannot start a campaign")

    discoverable = sorted(
        p.relative_to(TESTS_DIR).as_posix()
        for p in (TESTS_DIR / "hardware").rglob("test_*.py"))

    checks.ok(all(p.startswith("hardware/offline_tests/")
                  for p in discoverable),
              "every discoverable test_*.py under hardware/ is an "
              "offline framework test")

    for path in discoverable:
        source = (TESTS_DIR / path).read_text(encoding="utf-8")

        checks.ok("Mode.EXECUTE" not in source
                  or "refuses" in source or "unconfirmed" in source,
                  "{} does not construct an EXECUTE run".format(path))

    checks.section("a generic pytest run collects nothing here")

    # pytest collects Test* classes out of the NAMESPACE of a test_*.py
    # module, including ones it merely imported - and the offline suites
    # import TestDefinition and TestResult. `__test__ = False` is the
    # documented opt-out, and this is the check that keeps it there.
    for klass in (TestResult, TestDefinition, TestControl):
        checks.equal(getattr(klass, "__test__", None), False,
                     "{} opts out of pytest collection".format(
                         klass.__name__))

    collectable = []

    for path in (TESTS_DIR / "hardware").rglob("test_*.py"):
        if "__pycache__" in path.parts:
            continue

        source = path.read_text(encoding="utf-8")

        for line in source.splitlines():
            if line.startswith("def test_") or line.startswith(
                    "class Test"):
                collectable.append("{}: {}".format(path.name, line))

    checks.equal(collectable, [],
                 "no file under Tests/hardware defines a pytest-style "
                 "test function or class, so an editor or a hook that "
                 "runs pytest finds nothing to execute")

    runner_source = (TESTS_DIR / "run_all.py").read_text(encoding="utf-8")

    checks.ok("hardware" in runner_source.lower(),
              "run_all.py mentions the hardware campaign")

    checks.ok("run_hardware_tests" not in runner_source
              or "cannot" in runner_source.lower(),
              "run_all.py does not invoke the hardware entry point")

    checks.section("safety classes and their questions")

    for definition in catalogue.all_tests():
        if definition.safety in Safety.NEEDS_EXTRA_CONFIRMATION:
            checks.ok(
                definition.safety in Safety.CONFIRMATION_QUESTION,
                "{} is {} and there is a specific confirmation "
                "question for it".format(
                    definition.test_id, definition.safety))

    motion = [t for t in catalogue.all_tests()
              if t.safety == Safety.MOTION]

    checks.ok(bool(motion), "some tests are classified MOTION")

    illumination = [t for t in catalogue.all_tests()
                    if t.safety == Safety.ILLUMINATION]

    checks.ok(bool(illumination),
              "some tests are classified ILLUMINATION")

    checks.section("no test asks for an electrically dangerous fault")

    forbidden = ("short circuit", "reverse polarity", "overvolt",
                 "over-volt", "brownout", "brown-out", "short the")

    for definition in catalogue.all_tests():
        text = " ".join([
            definition.hardware_setup, definition.objective,
            " ".join(definition.procedure), definition.preconditions,
        ]).lower()

        checks.ok(not any(word in text for word in forbidden),
                  "{} asks for no electrically dangerous "
                  "fault".format(definition.test_id))

    checks.section("a motion test declines cleanly when the operator "
                   "says no")

    with Bench(answers=["n"]) as bench:
        result = bench.run("HW-B4-001")

        checks.ok(result.status in (Status.SKIPPED, Status.BLOCKED),
                  "a declined safety confirmation does not run the test")

    checks.section("non-interactive blocks the tests that need a human")

    with Bench(interactive=False) as bench:
        result = bench.run("HW-B0-005")

        checks.equal(result.status, Status.BLOCKED,
                     "an operator-assisted test is BLOCKED in "
                     "--non-interactive mode")

        checks.ok("non-interactive" in result.reason.lower()
                  or "human" in result.reason.lower(),
                  "the block says a human is needed")

    return checks.report()


if __name__ == "__main__":
    sys.exit(cli(run, __doc__))
