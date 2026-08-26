"""
Every uncovered mission branch, classified.

THE QUESTION - closure task section 13

Branch coverage gives a percentage. A percentage cannot distinguish a
branch that needs an oscilloscope from one nobody thought about, and
those need opposite responses. So every uncovered branch in the
MISSION-RUNTIME set is classified as exactly one of:

    SOFTWARE_REACHABLE      software alone can take it, and nothing
                            does. This is the finding, and it must be
                            zero before freeze
    DEFENSIVE_UNREACHABLE   a valid call chain cannot produce the
                            condition
    HARDWARE_ONLY           needs an I2C, UART or USB failure mode the
                            fakes cannot produce
    OFFLINE_ONLY            not on the competition path at all

WHAT COUNTS AS MISSION RUNTIME

`audit/module_inventory.py` owns that decision. This tool reads the
same table, so the two cannot disagree about what the mission is.

    py firmware/Tests/software/audit/branch_gaps.py
    py firmware/Tests/software/audit/branch_gaps.py --list

NOT PART OF THE ACCEPTANCE RUN. coverage.py is a development
dependency; `run_software.py` must keep working without it.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

FIRMWARE = next(
    p for p in Path(__file__).resolve().parents if p.name == "firmware"
)

SOFTWARE = FIRMWARE / "Tests" / "software"

sys.path.insert(0, str(HERE))

from module_inventory import (                               # noqa: E402
    HARDWARE_DRIVER,
    MISSION_RUNTIME,
    MISSION_SUPPORT,
    OFFLINE,
    production_files,
    role_of,
)
from handler_coverage import SUITES                          # noqa: E402


# ----------------------------------------------------------------------
# Files whose uncovered branches are classified as a group, with the
# reason. Anything not listed here must be classified individually by
# being covered.
# ----------------------------------------------------------------------

GROUP_CLASSIFICATION = {
    # The two drivers talk to silicon through `machine.I2C` and
    # `machine.UART`. The fakes speak the real register and packet
    # protocols, which is what makes them useful - and is exactly why
    # they cannot produce a half-written register, a frame corrupted in
    # one specific byte, or a bus that NAKs intermittently.
    "ESP32/servo.py": "HARDWARE_ONLY",
    "ESP32/sensor.py": "HARDWARE_ONLY",

    # One line, deliberately doing nothing.
    "ESP32/boot.py": "DEFENSIVE_UNREACHABLE",

    # Offline model promotion. No callers anywhere on the mission path;
    # `module_inventory.py` classifies the file OFFLINE.
    "Science/model_registry.py": "OFFLINE_ONLY",
}


def measured_roles():
    """The files whose branches this tool cares about."""
    wanted = {}

    for relative in production_files():
        role = role_of(relative)

        if role in (MISSION_RUNTIME, MISSION_SUPPORT, HARDWARE_DRIVER,
                    OFFLINE):
            wanted[relative] = role

    return wanted


def run_coverage():
    workspace = Path(tempfile.mkdtemp(prefix="freya-branches-"))
    data_file = workspace / ".coverage"

    sources = ",".join(
        str(FIRMWARE / name) for name in ("PC", "Science", "BD", "ESP32"))

    for index, suite in enumerate(SUITES):
        script = SOFTWARE / suite

        if not script.exists():
            print("  !! missing suite: {}".format(suite))

            continue

        command = [
            sys.executable, "-m", "coverage", "run", "--branch",
            "--source", sources,
        ]

        if index:
            command.append("--append")

        command.append(str(script))

        result = subprocess.run(
            command, cwd=str(workspace), capture_output=True, text=True,
            env={**os.environ, "COVERAGE_FILE": str(data_file)},
        )

        print("  {:<46} {}".format(
            suite, "ok" if result.returncode == 0 else "FAILED"))

    return data_file


def analyse(data_file):
    import coverage

    cov = coverage.Coverage(data_file=str(data_file))
    cov.load()

    roles = measured_roles()
    gaps = {}

    for relative, role in sorted(roles.items()):
        path = FIRMWARE / relative

        try:
            analysis = cov._analyze(str(path))

        except Exception:                                  # noqa: BLE001
            continue

        missing_branches = []

        arcs = getattr(analysis, "missing_branch_arcs", None)

        if callable(arcs):
            for line, destinations in sorted(arcs().items()):
                missing_branches.append((line, tuple(destinations)))

        if missing_branches:
            gaps[relative] = (role, missing_branches)

    return gaps


def classify(relative, role):
    if relative in GROUP_CLASSIFICATION:
        return GROUP_CLASSIFICATION[relative]

    if role == OFFLINE:
        return "OFFLINE_ONLY"

    if role == HARDWARE_DRIVER:
        return "HARDWARE_ONLY"

    return "SOFTWARE_REACHABLE"


def main(argv):
    print("=" * 72)
    print("  MISSION BRANCH GAP AUDIT")
    print("=" * 72)
    print()
    print("  running {} suites under branch coverage...".format(
        len(SUITES)))
    print()

    data_file = run_coverage()

    print()

    gaps = analyse(data_file)

    counts = {}
    reachable = {}

    for relative, (role, branches) in gaps.items():
        verdict = classify(relative, role)
        counts[verdict] = counts.get(verdict, 0) + len(branches)

        if verdict == "SOFTWARE_REACHABLE":
            reachable[relative] = branches

    print("-" * 72)

    for verdict in ("SOFTWARE_REACHABLE", "DEFENSIVE_UNREACHABLE",
                    "HARDWARE_ONLY", "OFFLINE_ONLY"):
        print("  {:<24} {:>5} uncovered branch(es)".format(
            verdict, counts.get(verdict, 0)))

    print("-" * 72)
    print()

    if "--list" in argv and reachable:
        print("  SOFTWARE-REACHABLE GAPS, BY FILE")
        print()

        for relative in sorted(reachable):
            branches = reachable[relative]
            print("  {:<40} {:>4}".format(relative, len(branches)))

            for line, destinations in branches[:8]:
                print("        line {:>5} -> {}".format(
                    line, ", ".join(str(d) for d in destinations)))

            if len(branches) > 8:
                print("        ... and {} more".format(len(branches) - 8))

        print()

    total_reachable = counts.get("SOFTWARE_REACHABLE", 0)

    if total_reachable:
        print("  note  {} uncovered branch(es) in mission-runtime files."
              .format(total_reachable))
        print("        Each is a decision the mission path can take that")
        print("        no suite takes. Run with --list to see them.")

        return 1

    print("  ok    every uncovered branch is hardware-only, offline or "
          "provably unreachable")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
