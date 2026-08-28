"""
Every offline framework suite, in one run, on no hardware at all.

    python3 run_offline.py            every suite
    python3 run_offline.py registry   only suites whose name matches

WHAT THIS IS AND IS NOT

It is the test suite for the hardware-test FRAMEWORK. It proves the
catalogue is sound, the gates hold, the evidence is honest and the test
bodies execute - against a deterministic fake transport that is clearly
labelled as such.

It is NOT a hardware campaign and cannot become one. Every context it
builds is Mode.SELFTEST with a fake transport, `TestResult` refuses to
hold PASS with self-test evidence, and the ledger that opens layer gates
ignores everything this file produces.

Exit status is non-zero if any suite fails, so this is usable in a hook.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

if str(HERE.parent.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent.parent))


SUITES = (
    ("test_registry", "the catalogue: ids, completeness, layer gates"),
    ("test_support", "statistics, operator, profile, ports, data shape"),
    ("test_verdicts", "the three axes, requirements and iteration kinds"),
    ("test_adapters", "capability detection and error normalization"),
    ("test_diagnostic", "the test-side agent, its safety and its adapter"),
    ("test_evidence", "manifests, events, CSV, summaries, defects"),
    ("test_runner", "verdicts, cleanup, aborts, blocks, iterations"),
    ("test_safety", "the gates, end to end, through the real CLI"),
    ("test_bodies", "every campaign body, against the fake transport"),
    ("test_firmware_contract",
     "the fakes speak the firmware's key names, not their own"),
)


def unregistered():
    """
    Suite files on disk that SUITES does not name.

    A SUITE THAT IS NOT LISTED DOES NOT RUN. The same manifest existed
    in Tests/software/run_software.py, where four regression suites
    written on 2026-08-27 sat unlisted and never executed while the
    campaign reported that everything passed. The manifest shape is
    right - the order is deliberate - but nothing checked it against
    the directory, so it is checked here.
    """
    listed = {name for name, _description in SUITES}

    return sorted(
        path.stem for path in HERE.glob("test_*.py")
        if path.stem not in listed
    )


USAGE = """usage: run_offline.py [-h] [PATTERN]

Run the offline test suites for the hardware-test FRAMEWORK. No serial
port is opened, no device is touched, and nothing produced here is
evidence about the hardware.

positional arguments:
  PATTERN     run only suites whose name matches, e.g. "registry"

options:
  -h, --help  show this message and exit

The hardware campaign itself is a separate entry point and needs an
explicit confirmation before it touches anything:

    ../run_hardware_tests.py --list
"""


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    # Answered here, and answered by DOING NOTHING. An entry point asked
    # for help must not run a campaign - a rule the repository enforces
    # in Tests/software/entrypoints/test_entrypoints.py, and one this
    # file broke by treating "--help" as a suite-name filter.
    if any(argument in ("-h", "--help") for argument in argv):
        print(USAGE)

        return 0

    pattern = argv[0] if argv else None

    selected = [
        (name, description) for name, description in SUITES
        if pattern is None or pattern.lower() in name.lower()
    ]

    if not selected:
        print("No suite matches {!r}. Available:".format(pattern))

        for name, description in SUITES:
            print("   {:<16} {}".format(name, description))

        return 2

    print("=" * 70)
    print("Freya hardware-test framework - OFFLINE self-test")
    print("=" * 70)
    print()
    print("No serial port is opened. No device is touched. Nothing here")
    print("is evidence about the hardware.")

    orphans = unregistered()

    if orphans and pattern is None:
        print()
        print("!! {} SUITE(S) EXIST BUT ARE NOT LISTED IN SUITES:".format(
            len(orphans)))

        for name in orphans:
            print("     {}".format(name))

        print()
        print("   They did not run. Add them to SUITES.")

        return 1

    failures = []

    for name, description in selected:
        print()
        print("=" * 70)
        print("{}  -  {}".format(name, description))
        print("=" * 70)

        module = __import__(
            "hardware.offline_tests.{}".format(name),
            fromlist=["run"])

        try:
            status = module.run()

        except Exception as error:                     # pragma: no cover
            import traceback

            traceback.print_exc()

            status = 1

            print("{} raised {}: {}".format(
                name, type(error).__name__, error))

        if status:
            failures.append(name)

    print()
    print("=" * 70)
    print("{} of {} suites passed".format(
        len(selected) - len(failures), len(selected)))

    for name in failures:
        print("   FAILED  {}".format(name))

    print("=" * 70)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
