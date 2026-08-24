"""
Every software suite, in one run, on no hardware at all.

WHAT "SOFTWARE" MEANS HERE

Nothing under Tests/software touches a serial port, an actuator or a
sensor. The hardware boundary is faked at the LOWEST practical level -
`machine.UART`, `machine.I2C`, `serial.Serial` - and everything above
that line is the real production code:

    real workflow screens
    real SerialLink framing, ids, timeouts and error classification
    real ESP32 command parser and handlers
    real carousel geometry and servo abstraction
    real Science
    real persistence

A test that reimplements production logic proves only that the
reimplementation works, so none of them do.

THE GROUPS, AND WHAT EACH ONE IS FOR

    static        boundaries and repository-wide invariants, by parsing
    unit          formulas and record shapes, in isolation
    integration   layers driving each other on fakes
    contracts     PC and firmware agreeing on names, arguments, shapes
    fault         every external operation, made to fail on purpose
    state         carousel and sample state machines, abused
    linux         the main computer is a Linux machine
    entrypoints   importing or --help must not DO anything
    integrity     the protected databases, hashed before and after
    stress        thousands of cycles, looking for drift and leaks
    randomized    seeded chaos, and the seed printed when it bites
    regression    one test per bug that ever reached the bench

Run:  py run_software.py            every group
      py run_software.py linux      only suites whose path matches
"""

import hashlib
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TESTS_DIR = HERE.parent
FIRMWARE = TESTS_DIR.parent

# ----------------------------------------------------------------------
# the scientific data, watched for the whole run
# ----------------------------------------------------------------------
# Individual suites use a sandboxed BD/ and data_integrity checks that
# they do. This is the check that does not depend on anybody
# remembering: everything under BD/ is hashed before the campaign and
# again after it, and a single changed byte fails the run.
#
# It exists because a test that damages the archive is the one kind of
# test failure that costs more than the bug it was looking for. BD/
# holds the reference libraries and `samples/samples.json`, which is
# the run's only irreplaceable output and is not in version control.

WATCHED = FIRMWARE / "BD"


def watch():
    found = {}

    if not WATCHED.is_dir():
        return found

    for path in sorted(WATCHED.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            found[path.relative_to(FIRMWARE).as_posix()] = hashlib.sha256(
                path.read_bytes()).hexdigest()

    return found

# Order is deliberate and diagnostic, not alphabetical.
#
# Static and contract failures explain later failures, so they run
# first: a suite that fails because a command was renamed should be
# read after the check that says the command was renamed. Stress and
# chaos run last because they are the slowest and the least specific -
# a chaos failure is a lead, the earlier groups are the diagnosis.
SUITES = (
    ("static/test_architecture.py",
     "static", "domain boundaries, obsolete architecture"),
    ("static/test_static_api.py",
     "static", "names, imports and call sites, repository-wide"),

    ("unit/test_science.py",
     "unit", "formulas, comparison, Decision Model"),
    ("unit/test_data.py",
     "unit", "record model, RAW immutability, provenance"),
    ("unit/test_numeric_edges.py",
     "unit", "zero, NaN, infinity, empty, wrong length"),
    ("unit/test_prompts.py",
     "unit", "operator input, abused"),

    ("contracts/test_pc_firmware.py",
     "contracts", "every command, argument and response key"),

    ("integration/test_esp32.py",
     "integration", "protocol, drivers, carousel, on fake hardware"),
    ("integration/test_pc.py",
     "integration", "serial lifecycle, error kinds, measurement order"),
    ("integration/test_screens.py",
     "integration", "every screen entered, and abused"),
    ("integration/test_mission.py",
     "integration", "a whole session, PC screens to fake firmware"),

    ("fault_injection/test_serial_faults.py",
     "fault", "framing, truncation, noise, disconnect, stale data"),
    ("fault_injection/test_device_faults.py",
     "fault", "sensor and servo failures, through the firmware"),
    ("fault_injection/test_filesystem_faults.py",
     "fault", "missing, unreadable, malformed and failing writes"),

    ("state_machine/test_carousel_states.py",
     "state", "position validity across every transition"),
    ("state_machine/test_sample_lifecycle.py",
     "state", "a sample from prepared to saved, failing at each step"),

    ("linux/test_linux.py",
     "linux", "ports, permissions, paths, case, working directory"),

    ("entrypoints/test_entrypoints.py",
     "entrypoints", "importing and --help must not act"),

    ("data_integrity/test_protected_data.py",
     "integrity", "the reference libraries, hashed before and after"),

    ("stress/test_stress.py",
     "stress", "thousands of cycles, looking for drift"),

    ("randomized/test_chaos.py",
     "randomized", "seeded fault storms over whole workflows"),

    ("regression/test_regressions.py",
     "regression", "one test per bug that reached the bench"),
)


def run(relative):
    """Run one suite in its own process, returning (passed, checks, result)."""
    path = HERE / relative

    if not path.is_file():
        return None, 0, None

    result = subprocess.run(
        [sys.executable, str(path)],
        capture_output=True, text=True, cwd=str(path.parent),
    )

    checks = 0

    for line in result.stdout.splitlines():
        if "all " in line and " checks passed" in line:
            try:
                checks = int(line.split("all ")[1].split(" checks")[0])

            except (IndexError, ValueError):
                checks = 0

    return result.returncode == 0, checks, result


USAGE = """usage: run_software.py [-h] [PATTERN]

Run the software verification suites. Nothing here touches hardware.

positional arguments:
  PATTERN     run only suites whose path or group matches. Groups are:
              static, unit, contracts, integration, fault, state,
              linux, entrypoints, integrity, stress, randomized,
              regression

options:
  -h, --help  show this message and exit
"""


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv

    if any(flag in argv for flag in ("-h", "--help", "help")):
        print(USAGE)

        return 0

    pattern = argv[0] if argv else None

    suites = [
        entry for entry in SUITES
        if pattern is None or pattern in entry[0] or pattern == entry[1]
    ]

    if not suites:
        print("No suite matches {!r}.".format(pattern))

        return 2

    print("=" * 72)
    print("Freya science module - SOFTWARE verification (no hardware)")
    print("=" * 72)
    print()

    before = watch()

    total = 0
    failures = []
    missing = []
    by_group = {}
    group = None

    for relative, tag, description in suites:
        if tag != group:
            group = tag
            print("  [{}]".format(tag))

        name = relative.split("/")[-1]
        print("  {:<30} {:<42}".format(name, description),
              end="", flush=True)

        passed, checks, result = run(relative)

        if passed is None:
            print("  (absent)")
            missing.append(relative)

            continue

        total += checks
        by_group[tag] = by_group.get(tag, 0) + checks

        if passed:
            print("{:>5} ok".format(checks))

        else:
            print("   FAILED")
            failures.append((relative, result))

    print()
    print("=" * 72)

    for tag in sorted(by_group):
        print("  {:<14} {:>6} checks".format(tag, by_group[tag]))

    print()

    after = watch()
    damaged = sorted(
        name for name in set(before) | set(after)
        if before.get(name) != after.get(name)
    )

    if damaged:
        print("!! THE TEST RUN CHANGED THE SCIENTIFIC DATA:")
        print()

        for name in damaged:
            state = ("added" if name not in before
                     else "deleted" if name not in after else "modified")
            print("     {:<10} {}".format(state, name))

        print()
        print("   Every suite must write through a sandbox. Restore the")
        print("   files with git before running anything else - except")
        print("   BD/samples/, which git cannot restore.")
        print()

        return 1

    print("  BD/ unchanged  {:>6} files hashed before and after".format(
        len(before)))
    print()

    if missing:
        print("{} suite(s) not present: {}".format(
            len(missing), ", ".join(missing)))
        print()

    if failures:
        print("{} of {} suites FAILED, {} checks passed elsewhere".format(
            len(failures), len(suites) - len(missing), total))

        for relative, result in failures:
            print()
            print("-" * 72)
            print("{}:".format(relative))
            print("-" * 72)

            for line in result.stdout.splitlines():
                if line.strip().startswith("FAIL") or "FAILED" in line:
                    print(line)

            if result.stderr.strip():
                print(result.stderr.strip()[-3000:])

        return 1

    print("all {} software suites passed, {} checks total".format(
        len(suites) - len(missing), total))

    return 0


if __name__ == "__main__":
    sys.exit(main())
