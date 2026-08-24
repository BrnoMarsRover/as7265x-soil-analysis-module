"""
The test entry point. Runs SOFTWARE tests, and only software tests.

    py run_all.py            every software suite
    py run_all.py linux      only software suites matching "linux"

TWO CAMPAIGNS, DELIBERATELY SEPARATE

    Tests/software/    no hardware, nothing physical can move
    Tests/hardware/    a real board on a real port, and it turns things

This file reaches ONLY the first. Hardware verification has its own
entry point and needs an explicit --port that has no default:

    py hardware/run_hardware.py --port /dev/ttyUSB0

That separation is not tidiness. `run_all.py` is the command run by
reflex, in a hook, from an editor, by someone who is not standing next
to the mechanism - and the carousel may be holding samples or be
mechanically constrained. A test suite that can turn an actuator must
never be reachable by reflex.

Exit status is non-zero if any suite fails, so this is usable in a hook.
"""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

SOFTWARE_RUNNER = HERE / "software" / "run_software.py"
HARDWARE_RUNNER = HERE / "hardware" / "run_hardware.py"


USAGE = """usage: run_all.py [-h] [PATTERN]

Run the Freya software test suites. No hardware is touched and nothing
physical can move.

positional arguments:
  PATTERN     run only suites whose path or group matches, e.g. "linux"

options:
  -h, --help  show this message and exit

Hardware verification is a separate campaign and is NOT reachable from
here - it turns the carousel:

    hardware/run_hardware.py --port /dev/ttyUSB0
"""


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    # Answered here rather than passed down. `--help` reaching the
    # software runner would be taken for a suite-name filter, match
    # nothing, and exit 2 with "No suite matches '--help'" - which is
    # the small version of the defect this whole campaign started from.
    if any(flag in argv for flag in ("-h", "--help", "help")):
        print(USAGE)

        return 0

    # Refused rather than ignored: someone typing this means to run
    # hardware tests, and silently running the software suite instead
    # would report a pass for a campaign that never happened.
    for flag in ("--hardware", "--with-hardware", "hardware"):
        if flag in argv:
            print("run_all.py runs SOFTWARE tests only.")
            print()
            print("Hardware verification is a separate campaign with its")
            print("own entry point, because it physically turns the")
            print("carousel:")
            print()
            print("    {} {} --port /dev/ttyUSB0".format(
                Path(sys.executable).name,
                HARDWARE_RUNNER.relative_to(HERE.parent.parent).as_posix(),
            ))

            return 2

    if not SOFTWARE_RUNNER.is_file():
        print("Missing {}".format(SOFTWARE_RUNNER))

        return 2

    result = subprocess.run(
        [sys.executable, str(SOFTWARE_RUNNER)] + argv,
        cwd=str(SOFTWARE_RUNNER.parent),
    )

    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
