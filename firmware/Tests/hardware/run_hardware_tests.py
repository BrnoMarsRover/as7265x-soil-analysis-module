"""
The one entry point for the hardware campaign.

    python3 run_hardware_tests.py --list
    python3 run_hardware_tests.py --describe HW-B3-001
    python3 run_hardware_tests.py --dry-run --all
    python3 run_hardware_tests.py --run HW-B0-001 --confirm-hardware \\
            --profile configuration/my-bench.json

THE DEFAULT INVOCATION DOES NOTHING PHYSICAL. With no arguments this
prints the campaign list and exits. There is no default port, no default
selection and no way to reach a device without saying so three times:
`--run` or `--run-campaign`, `--confirm-hardware`, and then the safety
question for whatever class of test was selected.

WHY NOT PYTEST. `Tests/software` is 40 suites of plain Python driven by
`run_software.py`, and this campaign has requirements pytest does not
serve: an operator at a keyboard, ordered layer gates between campaigns,
evidence directories, resumption, and above all the guarantee that
ordinary test discovery CANNOT start it. A `test_*.py` file that turns a
carousel is a file somebody's editor will run on save.

THE FILES HERE ARE NAMED SO THAT DISCOVERY CANNOT FIND THEM. Nothing
under Tests/hardware/ except offline_tests/ is named test_*, no test
body is a module-level function called test_anything, and the offline
tests that ARE discoverable run entirely on a fake transport.
"""

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# RESOLVED BY NAME, NOT BY COUNTING `.parent` HOPS.
#
# `hardware_validation.py` beside this file carries the scar: it moved
# one directory deeper once, and a hop count silently pointed
# FIRMWARE_DIR at Tests/. `Tests/software/regression` now enforces the
# rule repository-wide - three chained `.parent`s anywhere under Tests/
# is a test failure - and it caught this file.
FIRMWARE_DIR = next(p for p in HERE.parents if p.name == "firmware")

REPO_ROOT = FIRMWARE_DIR.parent

TESTS_DIR = HERE.parent

# `hardware` is imported as a package - the campaign modules use
# relative imports between themselves - so Tests/ goes on the path and
# everything below is reached through it. This works identically whether
# the file is run as a script, from another directory, or as
# `python3 -m hardware.run_hardware_tests`.
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from hardware.configuration.profile import (              # noqa: E402
    EXAMPLE_PROFILE, Profile, ProfileError)
from hardware.core import registry as registry_module     # noqa: E402
from hardware.core.context import RunContext              # noqa: E402
from hardware.core.evidence import EvidenceWriter         # noqa: E402
from hardware.core.model import Mode, Status              # noqa: E402
from hardware.core.operator import Operator               # noqa: E402
from hardware.core.runner import Ledger, Runner           # noqa: E402


VERSION = "1.0.0"


# THE CHICKEN AND EGG THIS ANSWERS. A profile identifies the board by
# its USB serial number or its by-id path - and the way to LEARN those
# is HW-B0-004, which needs a profile to run. So the first run is
# bootstrapped with an explicit --port, and its evidence contains the
# identity to write into the profile for every run afterwards.
BOOTSTRAP = """
FIRST RUN ON THIS BENCH? The profile identifies the board by its stable
USB identity, and HW-B0-004 is what tells you what that identity is. So
bootstrap it with an explicit device for the inventory only:

    --run HW-B0-004 --confirm-hardware --port <device>

Then read `usb_serial_number` out of that run's measurements.csv, copy
{example} to a profile of your own, put the serial number in it, and use
--profile from then on. After that the campaign never has to be told a
device name again - and cannot open the wrong board.
"""


# ======================================================================
# the parser
# ======================================================================

def build_parser():
    parser = argparse.ArgumentParser(
        prog="run_hardware_tests.py",
        description="Freya science module - real-hardware verification. "
                    "Listing, describing and dry-running touch no "
                    "hardware. Running a test does, and says so first.",
        epilog="Nothing here has been executed against hardware. Every "
               "test is NOT_RUN or READY_FOR_HARDWARE until a real run "
               "says otherwise.",
    )

    selection = parser.add_argument_group("what to do")

    selection.add_argument(
        "--list", action="store_true",
        help="list every registered test and its readiness. Touches "
             "nothing.")
    selection.add_argument(
        "--describe", metavar="TEST-ID",
        help="print one test's complete definition. Touches nothing.")
    selection.add_argument(
        "--dry-run", action="store_true",
        help="check every gate that can be checked offline, for the "
             "selection. Opens no transport.")
    selection.add_argument(
        "--run", action="append", metavar="TEST-ID", default=[],
        help="run one test on real hardware. Repeatable.")
    selection.add_argument(
        "--run-campaign", action="append", metavar="CAMPAIGN-ID",
        default=[],
        help="run a whole campaign, e.g. B1. Repeatable.")
    selection.add_argument(
        "--all", action="store_true",
        help="select every registered test. Useful with --dry-run; with "
             "--run it still passes every gate individually.")
    selection.add_argument(
        "--capabilities", action="store_true",
        help="print what the production system can and cannot be asked "
             "to do, and why. Touches nothing.")

    hardware = parser.add_argument_group("hardware")

    hardware.add_argument(
        "--confirm-hardware", action="store_true",
        help="required for any real run. Confirms a board is connected "
             "and you accept that tests will command it.")
    hardware.add_argument(
        "--port", metavar="DEVICE",
        help="the exact serial device, overriding the profile's "
             "selector. There is no default.")
    hardware.add_argument(
        "--profile", metavar="PATH",
        help="a bench profile. See configuration/profile.example.json.")

    control = parser.add_argument_group("control")

    control.add_argument(
        "--iterations", metavar="N", type=int,
        help="override the iteration count for every selected test that "
             "takes one. Bounded by each test's maximum and by the "
             "profile.")
    control.add_argument(
        "--output", metavar="PATH",
        help="where to write the evidence directory. Defaults to "
             "artifacts/ beside this file.")
    control.add_argument(
        "--non-interactive", action="store_true",
        help="never prompt. Tests that need a human observation become "
             "BLOCKED - never assumed.")
    control.add_argument(
        "--resume", metavar="RUN-ID",
        help="skip tests that already PASSED in the named run, and "
             "continue from there.")
    control.add_argument(
        "--stop-on-failure", action="store_true",
        help="stop the selection at the first FAIL, ERROR or ABORT.")
    control.add_argument(
        "--expert-override", action="store_true",
        help="allow a layer gate to be bypassed. Needs "
             "--override-reason and an interactive confirmation, and is "
             "recorded permanently in the evidence.")
    control.add_argument(
        "--override-reason", metavar="TEXT", default="",
        help="why a gate is being bypassed. Required by "
             "--expert-override.")

    output = parser.add_argument_group("output")

    output.add_argument(
        "--json", action="store_true",
        help="machine-readable output for --list, --describe and "
             "--capabilities.")
    output.add_argument(
        "--verbose", action="store_true",
        help="print every check as it happens, not only the failures.")
    output.add_argument(
        "--version", action="version",
        version="freya hardware test framework {}".format(VERSION))

    return parser


# ======================================================================
# modes that touch nothing
# ======================================================================

def do_list(registry, as_json):
    tests = registry.all_tests()

    if as_json:
        print(json.dumps({
            "campaigns": [c.as_dict() for c in registry.all_campaigns()],
            "tests": [t.as_dict() for t in tests],
            "count": len(tests),
        }, indent=2, sort_keys=True))

        return 0

    print("Freya science module - hardware verification catalogue")
    print("=" * 78)
    print()
    print("NOTHING BELOW HAS BEEN RUN AGAINST HARDWARE. Every test is "
          "NOT_RUN until a")
    print("real run says otherwise; listing touches no device.")
    print()

    for campaign in registry.all_campaigns():
        entries = registry.tests_in(campaign.campaign_id)

        print("{} {} - {} test{}".format(
            campaign.campaign_id.ljust(4), campaign.title,
            len(entries), "" if len(entries) == 1 else "s"))

        if campaign.prerequisites:
            print("     gated by: {}".format(
                ", ".join(campaign.prerequisites)))

        for test in entries:
            print("     {:<12} {:<19} {:<20} {}".format(
                test.test_id, test.safety, test.automation, test.title))

        print()

    print("-" * 78)
    print("{} tests in {} campaigns.".format(
        len(tests), len(registry.all_campaigns())))
    print()
    print("  --describe TEST-ID   the complete definition")
    print("  --dry-run --all      check every offline gate")
    print("  --capabilities       what the production system can and "
          "cannot be asked")

    return 0


def do_describe(registry, test_id, as_json):
    try:
        definition = registry.get(test_id.upper())

    except registry_module.RegistryError as error:
        print(error, file=sys.stderr)

        return 2

    if as_json:
        payload = definition.as_dict()
        payload["resolved_prerequisites"] = (
            registry.resolve_prerequisites(definition))

        print(json.dumps(payload, indent=2, sort_keys=True))

        return 0

    print("=" * 78)
    print("{}  {}".format(definition.test_id, definition.title))
    print("=" * 78)
    print()
    print("Campaign          {} (layer {})".format(
        definition.campaign, definition.layer))
    print("Safety class      {}".format(definition.safety))
    print("Automation        {}".format(definition.automation))
    print("Result            {}".format(Status.NOT_RUN))

    if definition.assumption:
        print("Assumption        {}".format(definition.assumption))

    if definition.defect_prefix:
        print("Defects filed as  {}-nnn".format(definition.defect_prefix))

    if definition.default_iterations is not None:
        print("Iterations        {} by default, {} maximum".format(
            definition.default_iterations, definition.max_iterations))

    print()
    _paragraph("Objective", definition.objective)
    _paragraph("Hardware setup", definition.hardware_setup)
    _paragraph("Preconditions", definition.preconditions)

    print("Procedure")

    for index, step in enumerate(definition.procedure, 1):
        print("   {}. {}".format(index, step))

    print()
    _paragraph("Expected result", definition.expected)
    _paragraph("Failure criteria", definition.failure_criteria)

    print("Data captured")

    for item in definition.captures:
        print("   - {}".format(item))

    print()

    if definition.requires:
        print("Required capabilities")

        for name in definition.requires:
            print("   - {}".format(name))

        print()

    prerequisites = registry.resolve_prerequisites(definition)

    if prerequisites:
        print("Prerequisites (must have PASSED on hardware)")

        for name in prerequisites:
            print("   - {}".format(name))

        print()

    if definition.notes:
        _paragraph("Notes", definition.notes)

    return 0


def _paragraph(title, text):
    print(title)

    words = str(text).split()
    line = "   "

    for word in words:
        if len(line) + len(word) + 1 > 76:
            print(line)

            line = "   "

        line += word + " "

    if line.strip():
        print(line.rstrip())

    print()


def do_capabilities(context, as_json):
    capabilities = context.capabilities()

    if as_json:
        print(json.dumps(
            {name: capability.as_dict()
             for name, capability in sorted(capabilities.items())},
            indent=2, sort_keys=True))

        return 0

    print("What the production system can be asked to do")
    print("=" * 78)
    print()
    print("Detected WITHOUT hardware: the PC command surface by "
          "introspection, the")
    print("firmware command table by parsing protocol.py. No device is "
          "touched.")
    print()

    available = [c for c in capabilities.values() if c.available]
    missing = [c for c in capabilities.values() if not c.available]

    for capability in sorted(available, key=lambda c: c.name):
        print("  available  {:<32} {}".format(
            capability.name, capability.reason[:38]))

    print()

    if missing:
        print("MISSING - these BLOCK tests, and each names the change "
              "that would unblock it")
        print("-" * 78)

        for capability in sorted(missing, key=lambda c: c.name):
            print()
            print("  {}".format(capability.name))
            _paragraph("     why", capability.reason)

            if capability.recommendation:
                _paragraph("     recommendation", capability.recommendation)

    print("-" * 78)
    print("{} available, {} missing.".format(
        len(available), len(missing)))

    return 0


# ======================================================================
# selection
# ======================================================================

def resolve_selection(registry, args):
    names = list(args.run) + list(args.run_campaign)

    if args.all:
        return registry.all_tests()

    if not names:
        return []

    return registry.select(names)


def load_profile(args):
    if args.profile:
        profile = Profile.load(args.profile)

    else:
        profile = Profile.default()

    if args.port:
        # An explicit device on the command line wins over the profile's
        # selector, and is recorded in the manifest as having done so.
        profile.data["port"] = args.port
        profile._validate()

    return profile


def resumed_passes(args, root):
    """Test ids that already PASSED in the named run."""
    if not args.resume:
        return set()

    summary = Path(root) / args.resume / "summary.json"

    if not summary.is_file():
        raise SystemExit(
            "--resume {}: no summary.json under {}".format(
                args.resume, summary.parent))

    payload = json.loads(summary.read_text(encoding="utf-8"))

    return {
        entry["test_id"] for entry in payload.get("results", [])
        if entry.get("status") == Status.PASS
        and entry.get("hardware_evidence")
    }


# ======================================================================
# main
# ======================================================================

def main(argv=None):
    args = build_parser().parse_args(argv)

    try:
        registry = registry_module.load()

    except registry_module.RegistryError as error:
        print(error, file=sys.stderr)

        return 2

    try:
        profile = load_profile(args)

    except ProfileError as error:
        print(error, file=sys.stderr)

        return 2

    # --- modes that touch nothing and need no context ---------------

    if args.describe:
        return do_describe(registry, args.describe, args.json)

    wants_run = bool(args.run or args.run_campaign)

    if args.list or not (wants_run or args.dry_run or args.capabilities):
        return do_list(registry, args.json)

    # --- everything below needs a context, but not a device ---------

    root = Path(args.output) if args.output else profile.artifacts_dir

    if args.capabilities and not (wants_run or args.dry_run):
        context = RunContext(
            Mode.LIST, profile, None,
            Operator(None, interactive=False), verbose=args.verbose)

        return do_capabilities(context, args.json)

    selection = resolve_selection(registry, args)

    if not selection:
        print("Nothing selected. Use --run TEST-ID, --run-campaign "
              "CAMPAIGN-ID, or --all.", file=sys.stderr)

        return 2

    mode = Mode.EXECUTE if wants_run and not args.dry_run else Mode.DRY_RUN

    if mode == Mode.EXECUTE and not args.confirm_hardware:
        print(_refusal(selection), file=sys.stderr)

        return 3

    if mode == Mode.EXECUTE:
        try:
            profile.require_valid()

        except ProfileError as error:
            print(error, file=sys.stderr)
            print(BOOTSTRAP.format(example=EXAMPLE_PROFILE),
                  file=sys.stderr)

            return 2

    try:
        skip = resumed_passes(args, root)

    except (OSError, ValueError) as error:
        print("--resume: {}".format(error), file=sys.stderr)

        return 2

    if skip:
        before = len(selection)

        selection = [t for t in selection if t.test_id not in skip]

        print("--resume {}: skipping {} test{} that already passed on "
              "hardware.".format(
                  args.resume, before - len(selection),
                  "" if before - len(selection) == 1 else "s"))

    evidence = EvidenceWriter(
        mode, root=root, profile=profile,
        repo_root=REPO_ROOT, argv=sys.argv)

    operator = Operator(evidence, interactive=not args.non_interactive)

    overrides = {}

    if args.iterations is not None:
        overrides["*"] = args.iterations

    context = RunContext(
        mode, profile, evidence, operator,
        hardware_confirmed=(mode == Mode.EXECUTE and args.confirm_hardware),
        device=args.port, iteration_overrides=overrides,
        verbose=args.verbose)

    ledger = Ledger(root / "ledger.json")

    runner = Runner(
        registry, context, ledger=ledger,
        expert_override=args.expert_override,
        override_reason=args.override_reason,
        stop_on_failure=args.stop_on_failure)

    _banner(mode, evidence, profile, selection)

    try:
        results = runner.run(selection)

    except KeyboardInterrupt:                          # pragma: no cover
        runner.aborted = True
        results = runner.results

    summary = evidence.write_summary(results, aborted=runner.aborted)

    ledger.save()

    evidence.close()

    _report(summary, evidence)

    return _exit_code(results, runner.aborted)


def _refusal(selection):
    classes = sorted({t.safety for t in selection})

    return (
        "REFUSED: --run needs --confirm-hardware.\n"
        "\n"
        "You selected {} test{}, with safety class{} {}.\n"
        "\n"
        "Running them commands a real instrument: some of them turn the "
        "carousel,\n"
        "switch on illumination, reset the board or ask you to pull a "
        "cable.\n"
        "\n"
        "Check the mechanism is clear, then add --confirm-hardware. To "
        "see what\n"
        "would run without touching anything, use --dry-run.".format(
            len(selection), "" if len(selection) == 1 else "s",
            "" if len(classes) == 1 else "es", ", ".join(classes))
    )


def _banner(mode, evidence, profile, selection):
    print()
    print("=" * 78)
    print("Freya science module - hardware verification")
    print("=" * 78)
    print()
    print("Mode      {}".format(mode))
    print("Evidence  {}".format(evidence.evidence_class))
    print("Run       {}".format(evidence.run_id))
    print("Directory {}".format(evidence.directory))
    print("Profile   {}".format(profile.path or "(built-in default)"))
    print("Selected  {} test{}".format(
        len(selection), "" if len(selection) == 1 else "s"))

    if mode == Mode.DRY_RUN:
        print()
        print("DRY RUN. No transport is opened, no test body executes, "
              "and nothing")
        print("in the evidence directory is a statement about the "
              "hardware.")

    else:
        print()
        print("REAL RUN. Tests will command the instrument. Ctrl+C "
              "aborts and keeps")
        print("the evidence collected so far.")

    print()


def _report(summary, evidence):
    counts = summary["counts"]

    print()
    print("=" * 78)
    print("{}  -  {}".format(summary["run_id"], summary["mode"]))
    print("=" * 78)
    print()

    for status in Status.ALL:
        if counts.get(status):
            print("  {:<20} {}".format(status, counts[status]))

    print()

    for campaign in sorted(summary["campaign_verdicts"]):
        entry = summary["campaign_verdicts"][campaign]

        print("  {:<5} {:<12} {} test{}".format(
            campaign, entry["verdict"], entry["tests"],
            "" if entry["tests"] == 1 else "s"))

    print()
    print("Evidence: {}".format(evidence.directory))

    if summary["evidence_class"] != "HARDWARE":
        print()
        print("*** {} ***".format(summary["evidence_class"]))
        print("Nothing in this run is evidence about the hardware.")

    if summary.get("aborted"):
        print()
        print("*** ABORTED *** - the selection did not finish. Partial "
              "evidence kept.")

    print()


def _exit_code(results, aborted):
    if aborted:
        return 4

    if any(r.status in Status.BAD for r in results):
        return 1

    if any(r.status == Status.BLOCKED for r in results):
        return 5

    return 0


if __name__ == "__main__":
    sys.exit(main())
