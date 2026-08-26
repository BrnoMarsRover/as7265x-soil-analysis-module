"""
Real statement and BRANCH coverage for the production tree.

WHY THIS IS NOT A TEST SUITE

It measures the other suites; it asserts nothing by itself and is not
part of the acceptance run. `run_software.py` must keep working on a
machine where coverage.py is not installed, so this is a separate
entry point and coverage.py is a development dependency, never a
competition one.

    py coverage_report.py            every suite that executes code
    py coverage_report.py --fast     the mission-critical subset

WHAT BRANCH COVERAGE ADDS

Statement coverage says a line ran. Branch coverage says both ways out
of a decision were taken - which is the difference between "the error
handler exists" and "the error handler works". The previous campaign
had only a line tracer and said so; this replaces it.

Uncovered branches are printed with their line numbers so they can be
classified by hand in SOFTWARE_ACCEPTANCE.md, because a percentage
alone cannot tell a hardware-only branch from a forgotten one.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TESTS = HERE.parent
FIRMWARE = TESTS.parent
REPO = FIRMWARE.parent

# The suites that execute production code. The static, entrypoint and
# integrity suites mostly PARSE it, so including them would credit
# coverage to files nothing ran.
# EVERY suite that executes production code. Kept in step with
# run_software.py by hand, and checked: a suite missing from here
# reports as uncovered code and sends somebody hunting for a gap that
# is already closed. That happened once - `test_records.py` and
# `test_display_shapes.py` were written, registered in the runner and
# forgotten here, and the report still showed records.py at 34%.
SUITES = (
    "unit/test_science.py",
    "unit/test_data.py",
    "unit/test_numeric_edges.py",
    "unit/test_prompts.py",
    "unit/test_display_shapes.py",
    "unit/test_fakes.py",
    "contracts/test_pc_firmware.py",
    "contracts/test_request_identity.py",
    "integration/test_esp32.py",
    "integration/test_pc.py",
    "integration/test_screens.py",
    "integration/test_records.py",
    "integration/test_mission.py",
    "fault_injection/test_serial_faults.py",
    "fault_injection/test_protocol_limits.py",
    "fault_injection/test_resource_faults.py",
    "fault_injection/test_firmware_faults.py",
    "fault_injection/test_handler_closure.py",
    "fault_injection/test_loader_closure.py",
    "fault_injection/test_residual_handlers.py",
    "fault_injection/test_device_faults.py",
    "fault_injection/test_filesystem_faults.py",
    "state_machine/test_carousel_states.py",
    "state_machine/test_sample_lifecycle.py",
    "state_machine/test_reset_recovery.py",
    "linux/test_linux.py",
    "regression/test_regressions.py",
    "regression/test_linux_bench.py",
    "randomized/test_chaos.py",
)

FAST = (
    "unit/test_science.py",
    "contracts/test_pc_firmware.py",
    "integration/test_screens.py",
    "regression/test_linux_bench.py",
)

# What the mission actually runs. research/ and tools/ are offline and
# are reported separately rather than diluting the number that matters.
MISSION = (
    "firmware/PC/*",
    "firmware/Science/*",
    "firmware/BD/*",
    "firmware/ESP32/*",
)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    suites = FAST if "--fast" in argv else SUITES

    # OUTSIDE the repository, deliberately.
    #
    # coverage.py writes its data file continuously, and this script
    # can run while an acceptance campaign is running. A data file
    # under firmware/ would change the tree hash that
    # entrypoints/test_entrypoints.py takes before and after, turning
    # "somebody measured coverage" into a spurious test failure - and
    # it would be one more artifact to keep out of git.
    data_file = Path(tempfile.gettempdir()) / "freya-coverage.data"

    for stale in Path(tempfile.gettempdir()).glob("freya-coverage.data*"):
        stale.unlink()

    for stale in HERE.glob(".coverage*"):
        stale.unlink()

    include = ",".join(str(REPO / pattern) for pattern in MISSION)

    for index, relative in enumerate(suites):
        path = HERE / relative

        if not path.is_file():
            print("  (absent) {}".format(relative))

            continue

        print("  measuring {:<44}".format(relative), end="", flush=True)

        result = subprocess.run(
            [sys.executable, "-m", "coverage", "run",
             "--branch",
             "--append" if index else "--parallel-mode=0",
             "--data-file={}".format(data_file),
             "--include={}".format(include),
             str(path)],
            capture_output=True, text=True, cwd=str(path.parent),
        )

        print("rc={}".format(result.returncode))

        if result.returncode not in (0, 1):
            print(result.stderr[-1500:])

    print()

    subprocess.run(
        [sys.executable, "-m", "coverage", "report",
         "--data-file={}".format(data_file),
         "--show-missing", "--skip-empty", "--sort=cover"],
        cwd=str(HERE),
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
