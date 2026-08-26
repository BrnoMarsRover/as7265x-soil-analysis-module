#!/usr/bin/env python3
"""
Freya science module - main computer application.

The operator interface and the mission controller. This program owns
the workflow, the Sample lifecycle and the orchestration of everything
else. It talks to three things and confuses none of them:

    PC  ->  serial  ->  ESP32      hardware: carousel and RAW spectra
    PC  ->  import  ->  Science    the mathematics and the decision
    PC  ->  import  ->  BD         calibration, DB1/DB2/DB3, records

The ESP32 never learns what a sample resembles. Science never learns
that a serial port exists. BD never learns either.

WHAT THIS FILE IS

The entry point, and nothing else: parse the arguments, open exactly
one serial port, hand it to the workflow, and close it on every path
out. The screens live in workflow/, the transport in serial_link.py,
and no scientific arithmetic lives anywhere on this side at all - if a
number appears on screen, Science computed it.

    python3 firmware/PC/rover_science_client.py --port /dev/ttyUSB0
    py      firmware/PC/rover_science_client.py --port COM4

    python3 firmware/PC/rover_science_client.py --port /dev/ttyUSB0 \
            --command ping --verbose

The first line of each pair is the Linux main computer, the second the
Windows bench. Forward slashes work on both. If Linux answers
PORT_DENIED, the account is not in the serial group - the error says
which command fixes it.

RUN IT FROM THE REPOSITORY ROOT. Every path this program needs is
resolved from __file__, so the working directory does not change what
it reads or writes - but the SCRIPT path on the command line is
resolved by the shell, and `py rover_science_client.py` from somewhere
else is a missing file, not a missing COM port.
"""

import argparse
import json
import sys
from pathlib import Path

# BD and Science are siblings of this directory. Putting the project
# root on the path means the application runs from anywhere without any
# PYTHONPATH setup, and every import below says which layer it came
# from.
PC_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PC_DIR.parent

for _path in (str(PROJECT_ROOT), str(PC_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from serial_link import (                                   # noqa: E402
    CONNECT_TIMEOUT,
    DEFAULT_BAUDRATE,
    DEFAULT_TIMEOUT,
    DeviceError,
    LinkError,
    SerialLink,
)
from workflow.prompts import OperatorGone                    # noqa: E402
from workflow.screen import interactive                     # noqa: E402


def one_shot(link, command, payload_text):
    """
    Send one command and print the answer as JSON.

    For scripting and for diagnosis. It brings up no workflow, loads no
    database and asks no questions - which is what makes it useful when
    the question is "does the board answer at all?".
    """
    payload = {}

    if payload_text:
        try:
            payload = json.loads(payload_text)

        except ValueError as error:
            print("--payload is not valid JSON: {}".format(error),
                  file=sys.stderr)

            return 2

        if not isinstance(payload, dict):
            print("--payload must be a JSON object.", file=sys.stderr)

            return 2

    data = link.request(command, **payload)

    print(json.dumps(data, indent=2, sort_keys=True))

    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="rover_science_client.py",
        description="Freya soil analysis module - operator interface.",
    )
    parser.add_argument("--port", required=True,
                        help="serial port of the ESP32, e.g. COM4")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUDRATE)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="seconds to wait for one command")
    parser.add_argument("--connect-timeout", type=float,
                        default=CONNECT_TIMEOUT,
                        help="seconds to wait for the first ping")
    parser.add_argument("--command",
                        help="send one command and exit")
    parser.add_argument("--payload",
                        help="JSON object of extra fields for --command")
    parser.add_argument(
        "--verbose", action="store_true",
        help="trace the transport: PORT OPEN, TX JSON, RX JSON, "
             "RX NON-JSON, TIMEOUT, PORT CLOSED",
    )

    return parser


def make_console_unbreakable():
    """
    Stop a character the terminal cannot encode from killing a screen.

    THE DEFECT THIS EXISTS FOR.

    Sample notes, locations, tasks and hypotheses are free text typed by
    the operator, and this is a Brno team: "vzorek z pouste, zluty
    pisek" arrives with hacek and carka in it, and an en dash comes free
    with most keyboards. Python encodes stdout using the LOCALE's
    encoding, so on a machine running under LANG=C - a systemd unit, a
    minimal container, a cron job, or a redirected pipe - that encoding
    is ASCII, and printing the note raises UnicodeEncodeError from
    inside print(). The records screen dies with a traceback over a
    character in a comment field.

    Reproduced: `print_full_sample` on a record whose location contains
    an en dash, with stdout at ASCII.

    WHY THE ENCODING IS LEFT ALONE AND ONLY THE ERROR HANDLER CHANGES.

    Forcing UTF-8 onto a terminal that is genuinely cp1252 would replace
    a crash with mojibake, which is a quieter way to be wrong. Changing
    only the handler keeps a UTF-8 terminal perfect - the normal Linux
    case - and turns the unrepresentable character into a visible
    `\\u010d` escape everywhere else. Nothing is silently lost: the
    ARCHIVE is written by json.dump with ensure_ascii=False through an
    explicit encoding="utf-8", so the stored text is unaffected by
    whatever the terminal can show.

    stdin is given the same treatment for the same reason: typing an
    accented character into a prompt should not raise UnicodeDecodeError
    out of input().
    """
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        # A stream replaced by a test, a pipe wrapper or an IDE may not
        # be a TextIOWrapper at all, and reconfigure() is only on those.
        reconfigure = getattr(stream, "reconfigure", None)

        if reconfigure is None:
            continue

        try:
            reconfigure(errors="backslashreplace"
                        if stream is not sys.stdin else "replace")

        except (OSError, ValueError):
            # Detached, already closed, or a stream that will not be
            # reconfigured. The program runs; it just keeps whatever
            # handler it had.
            pass


def main(argv=None):
    make_console_unbreakable()

    args = build_parser().parse_args(argv)

    try:
        link = SerialLink(
            port=args.port,
            baudrate=args.baud,
            timeout=args.timeout,
            connect_timeout=args.connect_timeout,
            verbose=args.verbose,
        )

    except RuntimeError as error:
        print(error, file=sys.stderr)

        return 2

    # OPEN AND CLOSE IN ONE PLACE.
    #
    # Every exit path below - a normal quit, a failed ping, a timeout, a
    # malformed frame, a Science failure, a database failure, Ctrl+C, an
    # unhandled exception - passes through the same finally. Relying on
    # interpreter exit to release the port is what leaves COM4 held
    # after a crash and turns the next run into a PORT_BUSY that has
    # nothing to do with another program.
    try:
        link.open()

    except LinkError as error:
        print("{}: {}".format(error.code, error.message), file=sys.stderr)

        return 2

    try:
        if args.command:
            return one_shot(link, args.command, args.payload)

        return interactive(link)

    except DeviceError as error:
        print("The module refused the command: {} {}".format(
            error.code, error.message), file=sys.stderr)

        return 1

    except LinkError as error:
        print("{}: {}".format(error.code, error.message), file=sys.stderr)

        return 1

    except KeyboardInterrupt:
        print()

        return 0

    # Ctrl+D, a finished input script, or a dropped SSH session. The
    # same clean exit as Ctrl+C: there is nothing wrong, there is just
    # nobody left to ask. Reaching here is what lets the `finally`
    # below release the port - the loop that used to spin on EOF never
    # did, and left /dev/ttyUSB0 held by a client the operator believed
    # they had closed.
    except OperatorGone:
        print()

        return 0

    finally:
        link.close()


if __name__ == "__main__":
    sys.exit(main())
