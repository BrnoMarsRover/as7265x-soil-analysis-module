"""
HARDWARE verification. This turns the carousel. Nothing here is faked.

    py run_hardware.py --port /dev/ttyUSB0
    py run_hardware.py --port /dev/ttyUSB0 --stage sensor
    py run_hardware.py --port /dev/ttyUSB0 --move --stage servo-move

THREE THINGS MAKE THIS SAFE TO HAVE IN THE REPOSITORY

    1. `run_all.py` cannot reach it. Ordinary testing runs
       Tests/software and physically cannot command an actuator.
    2. --port has NO default. There is no port to accidentally open.
    3. Every stage that moves anything additionally needs --move.

STATUS, HONESTLY STATED

The stages below drive `hardware_validation.py`, which is the real
Hardware-In-the-Loop suite: a real CP2102, a real ESP32, a real ST3215
and a real AS7265x. It has NOT been run in the current campaign,
because the hardware is disconnected. Nothing in Tests/software may be
described as validating hardware, and nothing here may be described as
passing until it has actually been run against the board.

THE BACKLOG THIS ENTRY POINT EXISTS TO CARRY

Directories are created as the corresponding campaign is written, not
in advance - an empty directory is a claim that something was tested.
The planned campaigns, in the order they should be run:

    mainpc_esp32        USB enumeration, port discovery, repeated
                        open/close, CP2102 behaviour, real latency
    esp32_sensor        AS7265x detection, initialization, repeated
                        measurement, all three illuminations, timing
    esp32_servo         ST3215 over real UART: ID, baud, position read,
                        CW/CCW, communication loss, servo restart
    carousel            every slot pair, both directions, load/scan
                        transfer, accumulated error, backlash, re-sync
    disconnect_recovery USB pulled at each point of a command
    endurance           thousands of measurements and open cycles
    mission_rehearsal   the competition workflow, start to finish,
                        repeatedly
    performance         latency and timing distributions
    reports             the evidence records each run writes

Until a campaign exists here, its entry in that list is a task, not a
result.
"""

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

VALIDATION = HERE / "hardware_validation.py"


def build_parser():
    parser = argparse.ArgumentParser(
        prog="run_hardware.py",
        description="Real-hardware verification. This turns the carousel.",
    )

    # No default, on purpose. See the module docstring.
    parser.add_argument(
        "--port", required=True,
        help="serial port of the ESP32, e.g. /dev/ttyUSB0 or COM4. "
             "There is no default and there will not be one.",
    )
    parser.add_argument(
        "--move", action="store_true",
        help="permit stages that physically turn the carousel",
    )
    parser.add_argument(
        "--stage", action="append", default=None,
        help="stage to run; repeatable. Passed through to "
             "hardware_validation.py.",
    )
    parser.add_argument(
        "--json", default=None,
        help="write the full evidence record here",
    )

    return parser


def main(argv=None):
    args, extra = build_parser().parse_known_args(argv)

    if not VALIDATION.is_file():
        print("Missing {}".format(VALIDATION))

        return 2

    command = [sys.executable, str(VALIDATION), "--port", args.port]

    if args.move:
        command.append("--move")

    for stage in args.stage or []:
        command += ["--stage", stage]

    if args.json:
        command += ["--json", args.json]

    command += extra

    print("=" * 72)
    print("Freya science module - HARDWARE verification")
    print("=" * 72)
    print()
    print("Port:      {}".format(args.port))
    print("Movement:  {}".format(
        "PERMITTED - the carousel will turn" if args.move
        else "blocked (pass --move to allow it)"))
    print()
    print("Check the mechanism is clear before continuing.")
    print()

    return subprocess.run(command, cwd=str(HERE)).returncode


if __name__ == "__main__":
    sys.exit(main())
