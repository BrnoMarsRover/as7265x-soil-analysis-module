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

import serial_link                                          # noqa: E402
from serial_link import (                                   # noqa: E402
    CONNECT_TIMEOUT,
    DEFAULT_BAUDRATE,
    DEFAULT_TIMEOUT,
    DeviceError,
    LinkError,
    SerialLink,
    utc_timestamp,
)
from workflow import status as ui_status                     # noqa: E402
from workflow.prompts import OperatorGone                    # noqa: E402
from workflow.screen import interactive                     # noqa: E402


def preflight(link, save_to=None):
    """
    Read-only bench check: what is connected, and is it healthy?

    THE FIRST THING TO RUN AFTER PLUGGING THE MODULE IN, and the thing
    to run again when something goes strange. It answers, in one pass:

        which device this is, and what firmware it runs
        whether this client and that firmware agree on the protocol
        the sensor state, and why if it is not READY
        the servo link, id, mode, encoder, voltage, temperature
        whether the carousel position is trusted
        what the transport has already seen

    IT MOVES NOTHING. Three commands, all reads: `ping`, `get_status`
    and - only when a servo is already connected - `servo_diagnostics`,
    which reads registers from the driver's own bounded whitelist. No
    EPROM write, no torque change, no illumination, no goal position.

    `--save-evidence` writes the whole thing as JSON so a failure can be
    diagnosed later, or compared against the next run, without anybody
    having to remember what the screen said.
    """
    report = {
        "captured_utc": utc_timestamp(),
        "port": link.port,
        "baud": link.baudrate,
        "client_expects_protocol": serial_link.EXPECTED_PROTOCOL_VERSION,
    }

    print()
    print("FREYA PREFLIGHT - read-only, nothing moves")
    print("=" * 60)

    # ---- identity -----------------------------------------------
    try:
        report["ping"] = link.ping()

    except (DeviceError, LinkError) as error:
        report["ping_error"] = {"code": error.code, "message": error.message}

        print("Board:      NO ANSWER - {}".format(error.code))
        print()
        print(error.message)

        _save_preflight(report, save_to)

        return 1

    link._note_identity(report["ping"])

    print("Board:      {} {}  (protocol {})".format(
        link.firmware_name or "?", link.firmware_version or "?",
        link.device_protocol_version))

    if link.protocol_mismatch:
        print("            !! MISMATCH - this client expects protocol {}"
              .format(link.protocol_mismatch["expected"]))
        print("            The device is not running this source.")

    report["protocol_mismatch"] = link.protocol_mismatch

    # ---- state ---------------------------------------------------
    try:
        status = link.get_status()
        report["status"] = status

    except (DeviceError, LinkError) as error:
        report["status_error"] = {"code": error.code,
                                  "message": error.message}
        print("Status:     UNAVAILABLE - {}".format(error.code))
        _save_preflight(report, save_to)

        return 1

    print("Sensor:     {}".format(ui_status.sensor_label(status)))
    print("Servo:      {}".format(ui_status.servo_link(status)))
    print("Carousel:   {}".format(ui_status.carousel_label(status)))

    sensor = status.get("sensor") or {}
    bus = sensor.get("bus") or {}

    if bus.get("addresses") is not None:
        print("I2C:        expected {}, found {}".format(
            sensor.get("address"), bus.get("addresses")))

    first_error = sensor.get("first_init_error")

    if first_error:
        print("Boot error: {}".format(first_error.get("code")))

    # ---- servo registers, only if a servo is already connected ----
    if ui_status.servo_online(status):
        try:
            diagnostics = link.servo_diagnostics()
            report["servo_diagnostics"] = diagnostics

            feedback = diagnostics.get("feedback") or {}
            counters = diagnostics.get("bus") or {}

            print("Servo id:   {}  mode {} ({})".format(
                diagnostics.get("id"), diagnostics.get("mode"),
                diagnostics.get("mode_name")))
            print("Encoder:    {} cnt / {} deg".format(
                feedback.get("position_counts"),
                feedback.get("position_deg")))
            print("Supply:     {} V, {} C".format(
                feedback.get("voltage_v"), feedback.get("temperature_c")))
            print("Servo bus:  {} tx / {} rx, {} retries, {} timeouts, "
                  "{} checksum".format(
                      counters.get("tx"), counters.get("rx"),
                      counters.get("retry"), counters.get("timeout"),
                      counters.get("checksum")))

        except (DeviceError, LinkError) as error:
            report["servo_diagnostics_error"] = {
                "code": error.code, "message": error.message}
            print("Servo diag: FAILED - {}".format(error.code))

    # ---- what this link has already seen --------------------------
    transport = {
        "corrupt_frames": link.corrupt_frames,
        "salvaged_frames": getattr(link, "salvaged_frames", None),
        "stale_frames": getattr(link, "stale_frames", None),
        "oversized_lines": getattr(link, "oversized_lines", None),
        "bytes_read": getattr(link, "bytes_read", None),
        "damage_reports": list(getattr(link, "damage_reports", []) or []),
    }
    report["transport"] = transport

    if link.corrupt_frames:
        print("Transport:  {} damaged frame(s) already this session"
              .format(link.corrupt_frames))

    print("=" * 60)

    _save_preflight(report, save_to)

    return 0


def _save_preflight(report, save_to):
    """Write the capture, and say where it went. Never raises."""
    if not save_to:
        return

    try:
        path = Path(save_to)

        if path.is_dir():
            path = path / "preflight-{}.json".format(
                report["captured_utc"].replace(":", "").replace("-", ""))

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report, indent=1, sort_keys=True, default=str),
            encoding="utf-8")

        print("Evidence:   {}".format(path))

    except OSError as error:
        print("Evidence could NOT be written to {}: {}".format(
            save_to, error), file=sys.stderr)


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
    parser.add_argument(
        "--preflight", action="store_true",
        help="read-only bench check: identity, firmware, sensor, servo, "
             "carousel, transport counters. Moves nothing and exits.")
    parser.add_argument(
        "--save-evidence", metavar="PATH",
        help="with --preflight, write the whole capture as JSON to a "
             "file or directory")
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
        if args.preflight:
            return preflight(link, args.save_evidence)

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
