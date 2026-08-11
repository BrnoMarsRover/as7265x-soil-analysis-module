#!/usr/bin/env python3
"""
Main-PC client for the Freya science module (AS7265x soil analysis).

Speaks newline-delimited JSON to the ESP32 over the USB serial port
created by the development board's CP2102 bridge:

    one command  = one JSON object followed by "\\n"
    one response = one JSON object followed by "\\n"

The client needs to know nothing about the ESP32 side of that link: no
GPIO numbers, no UART peripheral, only the serial port name. The same
cable also powers the board.

Because that port is the MicroPython console, it also carries the boot
banner, REPL text and tracebacks. Any line that is not valid JSON is
skipped, and the connection is only considered established once a ping
has been answered.

The ESP32 holds the authoritative science data. This program is the
controller and user interface: it drives the workflow, supplies the
timestamps (the ESP32 has no reliable wall clock) and collects the
human metadata that the firmware cannot know by itself.

Usage:

    python rover_science_client.py --port COM5
    python rover_science_client.py --port /dev/ttyUSB0

    python rover_science_client.py --port COM5 --command get_status
    python rover_science_client.py --port COM5 --command measure_sample \\
        --payload '{"slot": 1}'

RoverScienceClient is importable, so future rover software can use the
same API without the interactive menu:

    from rover_science_client import RoverScienceClient

    with RoverScienceClient("COM5") as science:
        science.sync_position(1)
        science.prepare_load(1, "S001")
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

try:
    import serial
except ImportError:  # pragma: no cover - environment dependent
    serial = None


DEFAULT_BAUDRATE = 115200

# Ordinary query/response round trips: ping, status, slot queries.
DEFAULT_TIMEOUT = 10.0

# Commands that move the carousel. Up to four 45 deg steps plus settling.
MOVE_TIMEOUT = 30.0

# A full measurement: carousel movement, mechanical settling, AS7265x
# acquisition, 18-channel processing, comparison against every database
# material, and a flash write. This is far slower than a ping and must
# not be cut short - a premature timeout is exactly what makes the
# operator think nothing happened.
MEASUREMENT_TIMEOUT = 120.0

# Budget for the module to come up after the port is opened. Opening a
# serial port resets many ESP32 development boards, and MicroPython then
# needs to boot and run main.py before it can answer.
CONNECT_TIMEOUT = 20.0

# Mission metadata the firmware stores but cannot determine by itself.
#
# Every one of these is optional: pressing Enter leaves the field null.
# Only the Sample ID is required, so the competition workflow stays fast.
#
# Neither sensor_distance_mm nor operator is asked for. The record
# schema still carries both as null for forward compatibility, but there
# is no distance sensor yet and the operator name adds nothing during a
# timed competition run. Never prompt for a value nobody will use.
METADATA_FIELDS = (
    ("task", "Task"),
    ("hypothesis", "Hypothesis"),
    ("location", "Location"),
    ("map_point", "Map point"),
    ("note", "Note"),
    ("photo", "Photo reference (filename or ID)"),
)


WORKFLOW_TEXT = '''NORMAL WORKFLOW

Initial:
0. Calibrate Slot 1 at soil loading hole

For every sample:
1. Choose physical Slot
2. Prepare Sample / enter Sample ID
3. Deposit soil and Confirm Loaded
4. Measure Sample

If alignment is imperfect:
5. Fine Carousel Alignment
'''


SPECTRAL_CHANNELS = (
    "A", "B", "C", "D", "E", "F",
    "G", "H", "I", "J", "K", "L",
    "R", "S", "T", "U", "V", "W",
)


def utc_timestamp():
    """ISO-8601 UTC timestamp attached to every outgoing command."""
    return datetime.now(timezone.utc).isoformat()


def diagnose_noise(skipped):
    """
    Work out what the non-JSON lines actually mean.

    The most common failure is not a bad cable: it is the ESP32 sitting
    at the MicroPython REPL because main.py did not start. The REPL
    echoes the command back and then prints the Python repr of it, so
    single-quoted dicts and ">>>" are the fingerprint.
    """
    joined = " ".join(skipped)

    if ">>>" in joined or "'cmd':" in joined or "'request_id':" in joined:
        return "\n".join([
            "The ESP32 is sitting at the MicroPython REPL - main.py is "
            "NOT running.",
            "The board echoed the command back and printed it as a "
            "Python dict, which is what the REPL does.",
            "",
            "Usual cause: main.py failed to import, most often because "
            "one of its modules is missing on the device.",
            "",
            "Check what is actually on the board:",
            "    py -m mpremote connect PORT fs ls",
            "",
            "and read the import error with:",
            "    py -m mpremote connect PORT repl",
            "    >>> import main",
        ])

    if "Traceback" in joined or "Error" in joined:
        return "\n".join([
            "The ESP32 printed a Python traceback: main.py crashed.",
            "Open the REPL to read it:",
            "    py -m mpremote connect PORT repl",
        ])

    if "ets " in joined or "rst:0x" in joined:
        return (
            "The board is still booting. Try again, or raise "
            "--connect-timeout."
        )

    return None


class RoverScienceError(Exception):
    """The module rejected a command."""

    def __init__(self, code, message, data=None):
        super().__init__("{}: {}".format(code, message))

        self.code = code
        self.message = message
        self.data = data


class RoverScienceClient:
    """JSON-over-UART client for the ESP32 science module."""

    def __init__(
        self,
        port,
        baudrate=DEFAULT_BAUDRATE,
        timeout=DEFAULT_TIMEOUT,
        connect_timeout=CONNECT_TIMEOUT,
        verbose=False,
    ):
        if serial is None:
            raise RuntimeError(
                "pyserial is not installed. Install it with:\n"
                "    python -m pip install pyserial"
            )

        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.verbose = verbose

        self.serial = None
        self.online = False
        self.last_noise = []
        self._request_id = 0

    # ------------------------------------------------------------------
    # connection
    # ------------------------------------------------------------------

    def open(self):
        self.serial = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.5,
        )

        # Drop anything left over from a previous session so the first
        # response cannot be a stale frame.
        self.serial.reset_input_buffer()
        self.serial.reset_output_buffer()

        self.online = False

        return self

    def wait_online(self, timeout=None):
        """
        Block until the science module answers a ping.

        Opening the port may reset the board on development boards whose
        auto-reset circuit is wired to DTR/RTS. Rather than fighting
        that, this simply tolerates it: MicroPython's boot banner, REPL
        text, reboot noise and partial lines are all skipped, and ping is
        retried until a valid JSON response arrives.
        """
        if timeout is None:
            timeout = self.connect_timeout

        deadline = time.monotonic() + timeout
        last_error = None

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()

            try:
                self.request("ping", timeout=min(remaining, 2.0))
                self.online = True

                return True

            except (TimeoutError, RoverScienceError, ValueError) as error:
                last_error = error

            except serial.SerialException as error:
                # The port can genuinely disappear mid-reset on some
                # USB bridges; that is fatal, not something to retry.
                raise RoverScienceError(
                    "PORT_LOST",
                    "Serial port disappeared: {}".format(error),
                )

        hint = diagnose_noise(self.last_noise)

        message = (
            "Science module did not answer a ping within {:.0f} s."
            .format(timeout)
        )

        if hint:
            message = "{}\n\n{}".format(message, hint)

        elif last_error:
            message = "{} ({})".format(message, last_error)

        raise TimeoutError(message)

    def close(self):
        if self.serial is not None:
            self.serial.close()
            self.serial = None

        self.online = False

    def __enter__(self):
        self.open()
        self.wait_online()

        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

        return False

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------

    def _next_request_id(self):
        self._request_id += 1

        return str(self._request_id)

    def request(self, cmd, timeout=None, **payload):
        """
        Send one command and return its ``data`` object.

        Raises RoverScienceError if the module answered ``ok: false``.
        """
        if self.serial is None:
            raise RuntimeError("Client is not connected; call open() first.")

        if timeout is None:
            timeout = self.timeout

        request_id = self._next_request_id()

        message = {"request_id": request_id, "cmd": cmd}
        message.update(payload)
        message.setdefault("timestamp", utc_timestamp())

        line = json.dumps(message) + "\n"

        if self.verbose:
            print(">>", line.strip())

        self.serial.reset_input_buffer()
        self.serial.write(line.encode("utf-8"))
        self.serial.flush()

        response = self._read_response(request_id, timeout)

        if not response.get("ok"):
            error = response.get("error") or {}

            raise RoverScienceError(
                error.get("code", "UNKNOWN_ERROR"),
                error.get("message", "No error message supplied."),
                response.get("data"),
            )

        return response.get("data")

    def _read_response(self, request_id, timeout):
        """
        Read until the answer to request_id arrives.

        The same USB console also carries MicroPython's boot banner,
        REPL prompts and tracebacks, so any line that is not valid JSON
        is skipped rather than treated as a protocol failure. Skipped
        lines are kept and reported if the wait ultimately times out,
        because a traceback is usually the reason no answer came.
        """
        deadline = time.monotonic() + timeout
        skipped = []
        self.last_noise = skipped

        while True:
            remaining = deadline - time.monotonic()

            if remaining <= 0:
                detail = ""

                if skipped:
                    detail = " Device also sent non-JSON output: {}".format(
                        " | ".join(skipped[-3:])
                    )

                raise TimeoutError(
                    "No response to request {} within {:.0f} s.{}".format(
                        request_id, timeout, detail
                    )
                )

            self.serial.timeout = min(remaining, 0.5)
            raw = self.serial.readline()

            if not raw:
                continue

            line = raw.strip()

            if not line:
                continue

            try:
                response = json.loads(line.decode("utf-8", "replace"))
            except (ValueError, UnicodeDecodeError):
                text = line.decode("utf-8", "replace")[:120]

                if self.verbose:
                    print("?? (not JSON)", text)

                skipped.append(text)

                continue

            if not isinstance(response, dict):
                skipped.append("non-object JSON: {}".format(line[:80]))

                continue

            if self.verbose:
                print("<<", json.dumps(response)[:400])

            answered = response.get("request_id")

            # Accept our own answer, plus frame-level errors that carry
            # no request id (invalid JSON, oversized command).
            if answered is not None and answered != request_id:
                continue

            # A JSON object addressed to us but without the protocol's
            # ok flag is a malformed application response, which - once
            # the link is established - is a genuine error rather than
            # console noise to skip.
            if "ok" not in response:
                raise RoverScienceError(
                    "MALFORMED_RESPONSE",
                    "Response to request {} has no 'ok' field: {}".format(
                        request_id, json.dumps(response)[:200]
                    ),
                )

            return response

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------

    def ping(self):
        return self.request("ping")

    def get_status(self):
        return self.request("get_status")

    def sync_position(self, scan_slot=None, load_slot=None):
        """
        Establish the carousel origin without moving anything.

        Normal use is load_slot=1, meaning "physical Slot 1 is now under
        the soil loading hole". scan_slot is accepted for callers that
        prefer to declare the scanner side.
        """
        if load_slot is not None:
            return self.request("sync_position", load_slot=int(load_slot))

        if scan_slot is not None:
            return self.request("sync_position", scan_slot=int(scan_slot))

        raise ValueError("sync_position needs load_slot or scan_slot")

    def set_origin_slot_one(self):
        """Confirm the current position as Slot 1 at the loading hole."""
        return self.sync_position(load_slot=1)

    def select_slot(self, slot):
        """Choose the physical slot to work with, moving it to the loader."""
        return self.request(
            "select_slot", timeout=MOVE_TIMEOUT, slot=int(slot)
        )

    def move_slots(self, direction, slots=1):
        """Whole-slot movement; each slot is exactly 45 degrees."""
        return self.request(
            "move_slots",
            timeout=MOVE_TIMEOUT,
            direction=direction,
            slots=int(slots),
        )

    def fine_adjust(self, degrees):
        """Small correction in degrees; positive clockwise."""
        return self.request(
            "fine_adjust", timeout=MOVE_TIMEOUT, degrees=float(degrees)
        )

    def get_carousel_status(self):
        return self.request("get_carousel_status")

    def prepare_load(self, slot, sample_id, metadata=None,
                     allow_existing_sample_id=False):
        payload = {
            "slot": int(slot),
            "sample_id": sample_id,
        }

        if metadata:
            payload["metadata"] = metadata

        if allow_existing_sample_id:
            payload["allow_existing_sample_id"] = True

        return self.request(
            "prepare_load", timeout=MOVE_TIMEOUT, **payload
        )

    def confirm_loaded(self, slot):
        return self.request("confirm_loaded", slot=int(slot))

    def measure_sample(self, slot, metadata=None):
        payload = {"slot": int(slot)}

        if metadata:
            payload["metadata"] = metadata

        return self.request(
            "measure_sample", timeout=MEASUREMENT_TIMEOUT, **payload
        )

    def clear_slot(self, slot):
        return self.request("clear_slot", slot=int(slot))

    def get_slots(self):
        return self.request("get_slots")

    def list_samples(self):
        return self.request("list_samples")

    def get_sample(self, sample_id):
        return self.request("get_sample", sample_id=sample_id)

    def update_sample_metadata(self, sample_id, metadata):
        return self.request(
            "update_sample_metadata",
            sample_id=sample_id,
            metadata=metadata,
        )

    def servo_stop(self):
        return self.request("servo_stop", timeout=MOVE_TIMEOUT)

    def servo_jog_cw(self, steps=None, duration_ms=None):
        return self._jog("servo_jog_cw", steps, duration_ms)

    def servo_jog_ccw(self, steps=None, duration_ms=None):
        return self._jog("servo_jog_ccw", steps, duration_ms)

    def _jog(self, cmd, steps, duration_ms):
        payload = {}

        if steps is not None:
            payload["steps"] = int(steps)

        if duration_ms is not None:
            payload["duration_ms"] = int(duration_ms)

        return self.request(cmd, timeout=MOVE_TIMEOUT, **payload)

    def help(self):
        return self.request("help")

    def test_measurement(self):
        """One spectrum + analysis, saved nowhere."""
        return self.request(
            "test_measurement", timeout=MEASUREMENT_TIMEOUT
        )

    def sensor_diagnostics(self, acquire=True):
        """Full staged AS7265x diagnostics."""
        return self.request(
            "sensor_diagnostics",
            timeout=MEASUREMENT_TIMEOUT,
            acquire=bool(acquire),
        )

    def i2c_scan(self):
        """Scan the bus even if sensor startup failed."""
        return self.request("i2c_scan", timeout=MOVE_TIMEOUT)

    def raw_measurement(self):
        """18 raw channels only - no references, no database."""
        return self.request(
            "raw_measurement", timeout=MEASUREMENT_TIMEOUT
        )

    def rename_sample(self, sample_id, new_sample_id):
        return self.request(
            "rename_sample",
            sample_id=sample_id,
            new_sample_id=new_sample_id,
        )

    def delete_sample(self, sample_id, clear_slot=False):
        return self.request(
            "delete_sample",
            sample_id=sample_id,
            clear_slot=bool(clear_slot),
        )

    def get_database_health(self):
        return self.request("get_database_health")

    def get_references(self):
        return self.request("get_references")

    def get_material_names(self):
        return self.request("get_material_names")

    def get_database_status(self):
        return self.request("get_database_status")


# ----------------------------------------------------------------------
# interactive console
# ----------------------------------------------------------------------

def format_slot_table(slots):
    lines = []

    for slot in slots:
        sample_id = slot.get("sample_id") or "----"

        lines.append(
            "{:<2} {:<8} {}".format(
                slot.get("slot_id"),
                sample_id,
                slot.get("state"),
            )
        )

    return lines


def read_state(client):
    """
    One round trip that fetches everything the screens need.

    Returns (slots, carousel, selected_entry) or None if unreachable.
    """
    try:
        data = client.get_slots()
    except (RoverScienceError, TimeoutError) as error:
        print("Connection: ERROR")
        print("Could not read carousel: {}".format(error))

        return None

    slots = data.get("slots") or []
    carousel = data.get("carousel") or {}
    selected = carousel.get("selected_slot")

    entry = next(
        (s for s in slots if s.get("slot_id") == selected), {}
    )

    return slots, carousel, entry


def print_banner():
    print()
    print("=" * 60)
    print(" FREYA SCIENCE MODULE")
    print("=" * 60)
    print()


def print_startup_screen(client, carousel):
    """Stage 0: the carousel has no origin yet, so nothing else matters."""
    print_banner()
    print("Connection: {}".format("ONLINE" if client.online else "UNKNOWN"))
    print()
    print("Carousel:")
    print("NOT CALIBRATED")
    print()
    print("Before working with samples, physical Slot 1 must be aligned")
    print("with the soil loading/drop hole.")
    print()
    print("[0] Initial Carousel Calibration")
    print("    Define the current loading-hole position as Slot 1.")
    print()
    print("[h] Help")
    print("[q] Exit")



def action_states(entry, carousel):
    """
    Work out what the operator can actually do right now.

    Returned as labels for the main menu, so the next legal step is
    obvious without reading any documentation.
    """
    state = entry.get("state", "EMPTY")
    phase = carousel.get("carousel_phase")

    labels = {"1": "", "2": "", "3": "", "4": "", "5": ""}

    if state == "EMPTY":
        labels["2"] = "[AVAILABLE]"
        labels["3"] = "[LOCKED - no sample prepared]"
        labels["4"] = "[LOCKED - no sample prepared]"

    elif state == "READY_TO_LOAD":
        labels["2"] = "[DONE]"
        labels["3"] = "[AVAILABLE]"
        labels["4"] = "[LOCKED - sample not confirmed]"

    elif state == "LOADED":
        labels["2"] = "[DONE]"
        labels["3"] = "[DONE]"

        if phase == "LOAD":
            labels["4"] = "[AVAILABLE]"
        else:
            labels["4"] = "[LOCKED - sample not at loading hole]"

    elif state == "MEASURED":
        labels["2"] = "[DONE]"
        labels["3"] = "[DONE]"
        labels["4"] = "[DONE - MEASURED]"

    return labels


def print_main_screen(client, slots, carousel, entry):
    """Stage 1: the five workflow commands, and nothing else."""
    print_banner()

    print("Selected: Slot {} / {}".format(
        carousel.get("selected_slot"),
        entry.get("sample_id") or "----",
    ))
    print("State:    {}".format(entry.get("state", "-")))
    print("Position: {}".format(carousel.get("carousel_phase", "?")))
    print()
    print("Loader: Slot {}    Scanner: Slot {}".format(
        carousel.get("current_load_slot"),
        carousel.get("current_scan_slot"),
    ))
    print()

    for line in format_slot_table(slots):
        print(line)

    labels = action_states(entry, carousel)

    print()
    print("[1] Choose Sample / Slot")
    print("    Move selected slot to loading position.")
    print()
    print("[2] Prepare Sample {}".format(labels["2"]))
    print("    Add Sample ID and metadata.")
    print()
    print("[3] Confirm Sample Loaded {}".format(labels["3"]))
    print("    Confirm soil is loaded.")
    print()
    print("[4] Measure Sample {}".format(labels["4"]))
    print("    Move 180 deg to sensor, measure, analyze and save.")
    print()
    print("[5] Fine Carousel Alignment")
    print("    Fine position correction in degrees.")
    print()
    print("[t] Tools / Records")
    print("    Status, saved data, tests and maintenance.")
    print()
    print("[h] Help")
    print("[q] Exit")



def ask(prompt, default=""):
    try:
        answer = input("{}: ".format(prompt)).strip()
    except EOFError:
        return default

    return answer or default


def ask_int(prompt, minimum=None, maximum=None, default=None):
    """
    Ask for a whole number.

    Returns None only when the operator deliberately cancels with a
    blank entry and there is no default. Callers MUST say something when
    that happens: silently dropping back to the menu is exactly what made
    "Measure sample" look like it did nothing.
    """
    while True:
        if default is not None:
            raw = ask("{} [{}]".format(prompt, default))

            if not raw:
                return default

        else:
            raw = ask("{} (blank = cancel)".format(prompt))

        if not raw:
            return None

        try:
            value = int(raw)
        except ValueError:
            print("Enter a whole number.")

            continue

        if minimum is not None and value < minimum:
            print("Minimum is {}.".format(minimum))

            continue

        if maximum is not None and value > maximum:
            print("Maximum is {}.".format(maximum))

            continue

        return value


def ask_metadata():
    """
    Collect the human mission fields.

    Every field is optional. Pressing Enter skips it and the record keeps
    a null, which is honest: an unrecorded observation must never be
    invented to satisfy a prompt.
    """
    print()
    print("Metadata - press Enter to skip any field.")
    print()

    metadata = {}

    for key, label in METADATA_FIELDS:
        value = ask("  {} [optional]".format(label))

        if not value:
            continue

        metadata[key] = value

    return metadata or None


def format_score(value):
    if isinstance(value, (int, float)):
        return "{:.2f} %".format(value)

    return "-"


def print_measurement(data, elapsed=None, verbose=False):
    sample = data.get("sample") or {}
    analysis = sample.get("analysis") or {}
    matches = sample.get("reference_matches") or []
    slot = data.get("slot") or {}
    carousel = data.get("carousel") or {}
    measurement = sample.get("measurement") or {}
    calibration = data.get("calibration") or {}

    print_stages(data.get("stages"))

    print()
    print("=" * 60)
    print(" MEASUREMENT COMPLETE")
    print("=" * 60)
    print()
    print("Sample ID:       {}".format(sample.get("sample_id")))
    print("Physical Slot:   {}".format(sample.get("slot_id")))
    print("Previous state:  {}".format(data.get("previous_state", "LOADED")))
    print("New state:       {}".format(data.get("new_state", "MEASURED")))

    if elapsed is not None:
        print("Elapsed: {:.1f} s".format(elapsed))

    raw = measurement.get("raw") or {}
    dark_state = (calibration.get("dark") or {}).get("state", "?")
    white_state = (calibration.get("white") or {}).get("state", "?")

    print()
    print("Acquisition:")
    print("  AS7265x:          OK")
    print("  channels:         {} / 18".format(len(raw)))
    print("  dark reference:   FIXED / {}".format(dark_state))
    print("  white reference:  FIXED / {}".format(white_state))

    analysis_ok = data.get("analysis_status", "OK") == "OK"

    print()
    print("Spectral analysis:")
    print("  references compared: {}".format(len(matches)))

    if not analysis_ok:
        print()
        print("  !! CLASSIFICATION FAILED: {}".format(
            data.get("analysis_error") or "database comparison unavailable"
        ))
        print("     The measured spectrum was still saved and can be "
              "re-analysed later.")

    if matches:
        print()
        print("Best matches:")
        print()

        for match in matches[:5]:
            print(
                "  {:>2}. {:<28} {:>9}".format(
                    match.get("rank"),
                    match.get("material"),
                    format_score(match.get("similarity_percent")),
                )
            )

        if len(matches) > 5:
            print("  ... {} more".format(len(matches) - 5))

        print("  ALL MATCHES AVAILABLE in the saved record")

    print()
    print("Automatic analysis:")

    conclusion = analysis.get("automatic_conclusion")

    if conclusion:
        print("  {}".format(conclusion))

    print()
    print("  status: {} / confidence {}".format(
        analysis.get("status"),
        analysis.get("confidence"),
    ))

    print()
    print("Sample storage:")
    print("  samples.json: {}".format(
        "SAVED" if data.get("saved") else "NOT SAVED"
    ))

    print()
    print("Physical state:")
    print("  Slot {} = {} / {}".format(
        slot.get("slot_id"),
        slot.get("state", "?"),
        "OCCUPIED" if slot.get("occupied") else "EMPTY",
    ))

    print()
    print("Carousel:")
    print("  phase: {}".format(carousel.get("carousel_phase", "?")))
    print("  loader = Slot {}, scanner = Slot {}".format(
        carousel.get("current_load_slot"),
        carousel.get("current_scan_slot"),
    ))

    warning = data.get("warning")

    if warning:
        print()
        print("=" * 60)
        print(" WARNING")
        print("=" * 60)
        print()
        print(warning)
        print()
        print("Please run Sync Carousel before continuing.")


def ask_float(prompt, minimum=None, maximum=None):
    """Ask for a number; blank cancels."""
    while True:
        raw = ask("{} (blank = cancel)".format(prompt))

        if not raw:
            return None

        try:
            value = float(raw.replace(",", "."))
        except ValueError:
            print("Enter a number, for example 2.5 or -1.")

            continue

        if minimum is not None and value < minimum:
            print("Minimum is {}.".format(minimum))

            continue

        if maximum is not None and value > maximum:
            print("Maximum is {}.".format(maximum))

            continue

        return value


def ask_direction():
    """Clockwise or counter-clockwise, without four separate commands."""
    while True:
        raw = ask("Direction [C]lockwise / [A]nti-clockwise "
                  "(blank = cancel)").upper()

        if not raw:
            return None

        if raw.startswith("C"):
            return "cw"

        if raw.startswith("A"):
            return "ccw"

        print("Enter C or A.")


def report_fine_adjust(client, data):
    """Plain confirmation, with the conversion detail only in debug."""
    adjustment = data.get("adjustment") or {}
    degrees = adjustment.get("degrees", 0.0)
    direction = adjustment.get("direction")

    if client.verbose:
        print()
        print("[DEBUG] Fine adjustment")
        print("requested: {:+.2f} deg".format(degrees))
        print("direction: {}".format(
            (direction or "-").upper()
        ))
        print("{} ms/degree: {}".format(
            (direction or "-").upper(),
            round(adjustment.get("ms_per_degree") or 0.0, 3),
        ))
        print("calculated duration: {} ms".format(
            round(adjustment.get("calculated_ms") or 0.0, 1)
        ))
        print("actual duration command: {} ms".format(
            adjustment.get("duration_ms")
        ))

    if not adjustment.get("moved"):
        print("Requested angle was too small to produce any servo "
              "runtime; nothing moved.")
        print("Check the 45 deg step calibration in config.py.")

        return

    print("Adjusted {:+.2f} deg {}.".format(
        degrees,
        "clockwise" if direction == "cw" else "counter-clockwise",
    ))

    if not adjustment.get("reliable", True):
        print("Note: that is shorter than one servo PWM frame, so the "
              "carousel may not have responded. Try a larger angle.")


def menu_fine_adjust(client, limit=None, standalone=True):
    """
    Degree-based correction. Milliseconds are never shown here.

    Physical alignment only: the logical slot, sample ID and slot state
    are all left exactly as they were.
    """
    carousel = {}

    if limit is None or standalone:
        try:
            carousel = client.get_carousel_status()
        except (RoverScienceError, TimeoutError) as error:
            print("Could not read carousel: {}".format(error))

            return

        if limit is None:
            limit = carousel.get("max_fine_adjust_deg") or 15.0

    if standalone:
        print()
        print("=" * 60)
        print(" FINE CAROUSEL ALIGNMENT")
        print("=" * 60)
        print()
        print("Selected Slot:")
        print(carousel.get("selected_slot"))
        print()
        print("Current logical position:")
        print(carousel.get("carousel_phase", "?"))
        print()

    print("Enter correction in degrees.")
    print()
    print("Positive = clockwise")
    print("Negative = counter-clockwise")
    print()
    print("Example:")
    print("+1.5")
    print("-2")
    print()

    degrees = ask_float(
        "Degrees [{:+.0f} ... {:+.0f}]".format(-limit, limit)
    )

    if degrees is None:
        print("Cancelled; carousel not moved.")

        return

    if abs(degrees) > limit:
        print()
        print("Fine adjustment is limited to +/-{:.0f} deg.".format(limit))
        print()
        print("Use whole-slot movement for larger movements.")

        return

    try:
        data = client.fine_adjust(degrees)
    except RoverScienceError as error:
        print("Adjustment refused: {}".format(error.message))

        return

    report_fine_adjust(client, data)

    after = data.get("carousel") or {}

    # Only meaningful once an origin exists; during initial calibration
    # there is no logical slot to reassure the operator about.
    if standalone and after.get("position_valid"):
        print("Slot {} unchanged, still at {}.".format(
            after.get("selected_slot"),
            after.get("carousel_phase"),
        ))



def menu_move_whole_slot(client):
    """Rotate by an exact number of 45 degree slots."""
    direction = ask_direction()

    if direction is None:
        print("Cancelled; carousel not moved.")

        return

    slots = ask_int("Number of slots", 1, 8, default=1)

    if slots is None:
        print("Cancelled; carousel not moved.")

        return

    try:
        data = client.move_slots(direction, slots)
    except RoverScienceError as error:
        print("Movement refused: {}".format(error.message))

        return

    print("Moved {} slot transition(s) {}.".format(
        slots,
        "clockwise" if direction == "cw" else "counter-clockwise",
    ))
    print("Geometric travel {:.0f} deg, commanded as the calibrated "
          "{:.0f} deg equivalent.".format(
              data.get("geometric_degrees", slots * 45.0),
              data.get("effective_degrees", slots * 40.0),
          ))


def menu_initial_calibration(client):
    """
    Stage 0: establish the carousel origin.

    The only goal is physical: get Slot 1 under the soil loading hole,
    then declare it. Returns True once the origin is set.
    """
    while True:
        try:
            carousel = client.get_carousel_status()
        except (RoverScienceError, TimeoutError) as error:
            print("Could not read carousel: {}".format(error))

            return False

        print()
        print("=" * 60)
        print(" INITIAL CAROUSEL CALIBRATION")
        print("=" * 60)
        print()
        print("Goal:")
        print("Align physical Slot 1 exactly under the soil loading hole.")
        print()
        print("Use the movement controls until the position is correct.")
        print()
        print("[1] Move one whole slot clockwise")
        print("[2] Move one whole slot counter-clockwise")
        print()
        print("[3] Fine alignment by degrees")
        print("    Positive = clockwise")
        print("    Negative = counter-clockwise")
        print()
        print("[4] STOP servo")
        print()
        print("[5] SET CURRENT POSITION AS SLOT 1")
        print()
        print("[c] Cancel")

        choice = ask("Select").strip().lower()

        try:
            if choice == "1":
                client.move_slots("cw", 1)
                print("Moved one slot clockwise.")

            elif choice == "2":
                client.move_slots("ccw", 1)
                print("Moved one slot counter-clockwise.")

            elif choice == "3":
                menu_fine_adjust(
                    client,
                    limit=carousel.get("max_fine_adjust_deg"),
                    standalone=False,
                )

            elif choice == "4":
                client.servo_stop()
                print("Servo stopped.")

            elif choice == "5":
                data = client.set_origin_slot_one()
                print_calibration_complete(data.get("carousel") or {})

                return True

            elif choice == "c":
                print("Calibration cancelled; carousel position unchanged.")

                return False

            else:
                print("Unknown option.")

        except RoverScienceError as error:
            print()
            print("Module refused the command:")
            print("  code   : {}".format(error.code))
            print("  message: {}".format(error.message))

        except TimeoutError as error:
            print()
            print("Timeout: {}".format(error))


def print_calibration_complete(carousel):
    print()
    print("Calibration complete.")
    print()
    print("Slot {} = LOADING position".format(
        carousel.get("current_load_slot")
    ))
    print("Slot {} = SCANNER position".format(
        carousel.get("current_scan_slot")
    ))
    print()
    print("Physical slot order:")
    print("1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7 -> 8")
    print("clockwise")
    print()
    print("Entering normal competition workflow...")



def menu_choose_slot(client):
    """
    Physical carousel positioning.

    The requested slot is brought to the SOIL LOADING position. If the
    previous sample is still at the scanner, the firmware restores the
    loading orientation first - the operator never runs that 180 deg
    return by hand.
    """
    state = read_state(client)

    if state is None:
        return

    slots, carousel, _entry = state
    current = carousel.get("selected_slot")

    print()
    print("=" * 60)
    print(" CHOOSE SAMPLE / SLOT")
    print("=" * 60)
    print()

    for slot in slots:
        marker = " <- current" if slot.get("slot_id") == current else ""

        print("  {}  {:<8} {}{}".format(
            slot.get("slot_id"),
            slot.get("sample_id") or "----",
            slot.get("state"),
            marker,
        ))

    print()

    suggested = (current % 8) + 1 if current else 1
    target = ask_int("Choose slot [1-8]", 1, 8, default=suggested)

    if target is None:
        print("Cancelled; carousel not moved.")

        return

    if client.verbose:
        movement = carousel.get("movement_calibration") or {}
        geometry = carousel.get("geometry") or {}

        print()
        print("[DEBUG] CAROUSEL SLOT MOVE")
        print()
        print("From:       Slot {}".format(current))
        print("To:         Slot {}".format(target))
        print()
        print("Geometry:            {:.0f} deg".format(
            geometry.get("slot_spacing_deg", 45.0)
        ))
        print("Calibrated CW:       {:.0f} deg / {} ms".format(
            movement.get("next_slot_cw_effective_deg", 40.0),
            movement.get("next_slot_cw_ms"),
        ))
        print("Calibrated CCW:      {:.0f} deg / {} ms".format(
            movement.get("next_slot_ccw_effective_deg", 45.0),
            movement.get("next_slot_ccw_ms"),
        ))

        if carousel.get("carousel_phase") == "SCAN":
            print()
            print("Carousel is at SCAN; the loading orientation will be")
            print("restored first with the calibrated half turn.")

    print()
    print("Moving Slot {} to loading position...".format(target))
    sys.stdout.flush()

    try:
        result = client.select_slot(target)
    except RoverScienceError as error:
        print("Selection refused: {}".format(error.message))

        return

    move = result.get("move") or {}
    after = result.get("carousel") or {}

    print()

    if move.get("restored_load_orientation"):
        print("Restored the loading orientation with the calibrated "
              "180 deg return.")

    steps = move.get("steps") or 0

    if steps:
        print("Moved {} slot transition(s) {}.".format(
            steps,
            "clockwise" if move.get("direction") == "cw"
            else "counter-clockwise",
        ))
    elif not move.get("moved"):
        print(move.get("message", "Already in position."))

    print()
    print("Selected Slot: {}".format(after.get("selected_slot")))
    print("Loader:        Slot {}".format(after.get("current_load_slot")))
    print("Scanner:       Slot {}".format(after.get("current_scan_slot")))
    print("Phase:         {}".format(after.get("carousel_phase")))


def menu_prepare(client):
    """
    Science-metadata operation for the currently selected slot.

    Not a movement command: Choose Slot has already put the slot under
    the loading hole.
    """
    state = read_state(client)

    if state is None:
        return

    _slots, carousel, entry = state
    slot = carousel.get("selected_slot")

    if slot is None:
        print()
        print("No slot is selected. Use [1] Choose Sample / Slot first.")

        return

    # Never silently overwrite a slot that already holds something.
    if entry.get("state") not in (None, "EMPTY"):
        print()
        print("WARNING")
        print()
        print("Slot {} is currently occupied.".format(slot))
        print()
        print("Sample ID:")
        print(entry.get("sample_id") or "----")
        print()
        print("State:")
        print(entry.get("state"))
        print()
        print("Do you want to clear the PHYSICAL slot and prepare a new")
        print("sample?")
        print()
        print("The persistent measurement {} will NOT be deleted.".format(
            entry.get("sample_id") or ""
        ))
        print()
        print("[y] Yes")
        print("[n] No")

        if not ask("Select").strip().lower().startswith("y"):
            print("Cancelled; slot {} unchanged.".format(slot))

            return

        try:
            client.clear_slot(slot)
        except RoverScienceError as error:
            print("Could not clear the slot: {}".format(error.message))

            return

        print("Slot {} cleared; the stored record was kept.".format(slot))

    print()
    print("=" * 60)
    print(" PREPARE SAMPLE")
    print("=" * 60)
    print()
    print("Selected physical slot:")
    print("Slot {}".format(slot))
    print()

    sample_id = ask("Sample ID")

    if not sample_id:
        print("Sample ID is required; nothing was prepared.")

        return

    metadata = ask_metadata()

    try:
        data = client.prepare_load(slot, sample_id, metadata=metadata)
    except RoverScienceError as error:
        print()

        if error.code == "DUPLICATE_SAMPLE_ID":
            print("Sample ID {} already exists.".format(sample_id))
            print()
            print("Choose another Sample ID.")
        else:
            print("Preparation refused: {}".format(error.message))

        return

    slot_data = data.get("slot") or {}

    print()
    print("Slot {}".format(slot_data.get("slot_id")))
    print("Sample {}".format(slot_data.get("sample_id")))
    print("State = {}".format(slot_data.get("state")))


def menu_confirm(client):
    """Confirm that the external arm has actually deposited soil."""
    state = read_state(client)

    if state is None:
        return

    _slots, carousel, entry = state
    slot = carousel.get("selected_slot")

    print()
    print("=" * 60)
    print(" CONFIRM SAMPLE LOADED")
    print("=" * 60)
    print()

    if entry.get("state") != "READY_TO_LOAD":
        print("Selected Slot: {}".format(slot))
        print("Sample:        {}".format(entry.get("sample_id") or "----"))
        print("Current state: {}".format(entry.get("state", "-")))
        print()
        print("Required state:")
        print("READY_TO_LOAD")
        print()

        if entry.get("state") == "EMPTY":
            print("Use:")
            print("[2] Prepare Sample")
        elif entry.get("state") == "LOADED":
            print("This sample is already confirmed loaded.")
            print()
            print("Use:")
            print("[4] Measure Sample")
        else:
            print("This sample has already been measured.")

        return

    print("Selected Sample:")
    print(entry.get("sample_id"))
    print()
    print("Physical Slot:")
    print(slot)
    print()
    print("Position:")
    print("SOIL LOADING HOLE" if carousel.get("carousel_phase") == "LOAD"
          else carousel.get("carousel_phase"))
    print()
    print("Current state:")
    print(entry.get("state"))
    print()
    print("Is soil physically present and is the slot aligned correctly?")
    print()
    print("[y] Confirm Loaded")
    print("[n] Cancel")

    if not ask("Select").strip().lower().startswith("y"):
        print("Cancelled; nothing was confirmed.")
        print("If the position is slightly wrong, use "
              "[5] Fine Carousel Alignment.")

        return

    try:
        data = client.confirm_loaded(slot)
    except RoverScienceError as error:
        print("Confirmation refused: {}".format(error.message))

        return

    slot_data = data.get("slot") or {}

    print()
    print("Slot {}".format(slot_data.get("slot_id")))
    print("{}".format(slot_data.get("sample_id")))
    print("State = {}".format(slot_data.get("state")))
    print("occupied = {}".format(slot_data.get("occupied")))



def print_stages(stages):
    """Render the firmware's stage log exactly as it was reported."""
    if not stages:
        return

    print()

    for entry in stages:
        marker = "OK" if entry.get("ok") else "FAILED"

        print("[{}/{}] {}".format(
            entry.get("index"),
            entry.get("total"),
            entry.get("stage"),
        ))

        detail = entry.get("detail")

        if detail:
            print("      {}".format(detail))

        print("      {}".format(marker))


def print_measurement_failure(error, slot):
    """Say exactly which stage stopped the pipeline."""
    data = error.data or {}

    print()
    print("=" * 60)
    print(" MEASUREMENT FAILED")
    print("=" * 60)

    print_stages(data.get("stages"))

    print()
    print("Stage:     {}".format(data.get("failed_stage", "unknown")))
    print("Sample:    {}".format(data.get("sample_id", "-")))
    print("Slot:      {}".format(data.get("slot_id", slot)))
    print("Code:      {}".format(error.code))
    print("Exception: {}".format(error.message))

    missing = data.get("missing_channels")

    if missing:
        print("Missing channels: {}".format(", ".join(missing)))

    print()
    print("Slot remains: {}".format(data.get("slot_state", "LOADED")))
    print("No false MEASURED state was written.")

    if data.get("sample") is not None:
        print()
        print("The spectrum WAS acquired and is included in the response, "
              "but it could not be stored on the ESP32.")


def menu_measure(client):
    """
    Measure the currently selected sample.

    Eligible only when the selected slot holds a sample, is LOADED, and
    is physically at the loading hole. Anything else is explained rather
    than silently refused.
    """
    state = read_state(client)

    if state is None:
        return

    _slots, carousel, entry = state
    slot = carousel.get("selected_slot")
    phase = carousel.get("carousel_phase")
    sample_id = entry.get("sample_id")
    slot_state = entry.get("state", "EMPTY")

    if slot_state != "LOADED" or phase != "LOAD":
        print()
        print("=" * 60)
        print(" MEASUREMENT NOT AVAILABLE")
        print("=" * 60)
        print()
        print("Selected Slot: {}".format(slot))
        print("Sample:        {}".format(sample_id or "----"))
        print("State:         {}".format(slot_state))
        print()
        print("Required state:")
        print("LOADED")
        print()

        if slot_state == "EMPTY":
            print("Use:")
            print("[2] Prepare Sample")

        elif slot_state == "READY_TO_LOAD":
            print("Use:")
            print("[3] Confirm Sample Loaded")

        elif slot_state == "MEASURED":
            print("This sample has already been measured.")
            print()
            print("Use:")
            print("[1] Choose Sample / Slot")

        if phase != "LOAD" and slot_state == "LOADED":
            print("Carousel phase is {}, not LOAD.".format(phase))
            print()
            print("Use:")
            print("[1] Choose Sample / Slot")

        return

    print()
    print("=" * 60)
    print(" MEASURE SAMPLE")
    print("=" * 60)
    print()
    print("Selected Sample:")
    print(sample_id)
    print()
    print("Slot:")
    print(slot)
    print()
    print("State:")
    print(slot_state)
    print()
    print("Current position:")
    print("SOIL LOADING HOLE")
    print()
    print("Measurement will:")
    print()
    print("1. Rotate this sample 180 deg to the AS7265x scanner.")
    print("2. Acquire a new 18-channel spectrum.")
    print("3. Apply the fixed Dark/White references.")
    print("4. Compare against all reference materials.")
    print("5. Generate an automatic interpretation.")
    print("6. Save all measurement information into {}.".format(sample_id))
    print()
    print("[m] Measure {}".format(sample_id))
    print("[c] Cancel")

    if not ask("Select").strip().lower().startswith("m"):
        print("Cancelled; nothing was measured.")

        return

    if client.verbose:
        movement = carousel.get("movement_calibration") or {}

        print()
        print("[DEBUG] LOAD -> SCAN")
        print()
        print("Sample:      {}".format(sample_id))
        print("Slot:        {}".format(slot))
        print()
        print("Physical movement:")
        print("180 deg half-turn")
        print()
        print("Duration:")
        print("{} ms".format(movement.get("load_to_scan_cw_ms")))
        print()
        print("This is independent from the adjacent-slot calibration.")

    print()
    print("Moving {} to scanner...".format(sample_id))
    print("Measurement in progress. Please wait, this takes several "
          "seconds.")
    sys.stdout.flush()

    started = time.monotonic()

    try:
        data = client.measure_sample(slot)

    except RoverScienceError as error:
        print_measurement_failure(error, slot)

        return

    except TimeoutError as error:
        print()
        print("=" * 60)
        print(" NO RESPONSE")
        print("=" * 60)
        print()
        print(error)
        print()
        print("The module may still be measuring. Check System Status in "
              "Tools before retrying, so the same sample is not measured "
              "twice.")

        return

    print_measurement(
        data,
        elapsed=time.monotonic() - started,
        verbose=client.verbose,
    )



def menu_show_slots(client):
    data = client.get_slots()
    carousel = data.get("carousel") or {}

    print()

    for line in format_slot_table(data.get("slots") or []):
        print(line)

    print()
    print("Loader = Slot {}, Scanner = Slot {}, Selected = Slot {}.".format(
        carousel.get("current_load_slot"),
        carousel.get("current_scan_slot"),
        carousel.get("selected_slot"),
    ))


def menu_clear(client):
    slot = ask_int("Slot to clear (1-8)", 1, 8)

    if slot is None:
        print("Cancelled; no slot was cleared.")

        return

    data = client.clear_slot(slot)

    print("Slot {} cleared (was {}).".format(
        slot,
        data.get("previous_state"),
    ))

    if data.get("science_record_kept"):
        print("Sample {} remains stored on the ESP32.".format(
            data.get("cleared_sample_id")
        ))


def print_status(client):
    """Full module status, reached from Tools / Records."""
    status = client.get_status()

    print()
    print("=" * 60)
    print(" SYSTEM STATUS")
    print("=" * 60)
    print()
    print("Firmware  : {} {}".format(
        status.get("firmware"), status.get("version")
    ))
    print("Sensor    : {}".format(
        "ready" if status.get("sensor_initialized")
        else "FAULT - {}".format(status.get("sensor_error"))
    ))
    print("References: {}".format(
        "loaded" if status.get("references_loaded")
        else "INVALID - {}".format(status.get("references_error"))
    ))
    print("Database  : {} materials".format(
        status.get("database_material_count")
    ))
    print("Carousel  : {}".format(
        "Slot {} selected, phase {}".format(
            status.get("selected_slot"),
            status.get("carousel_phase"),
        )
        if status.get("position_valid") else "NOT CALIBRATED"
    ))
    print("Loader    : Slot {}".format(status.get("current_load_slot")))
    print("Scanner   : Slot {}".format(status.get("current_scan_slot")))
    print("Samples   : {} stored".format(status.get("saved_sample_count")))
    print("Sample store: {}".format(
        "ready" if status.get("sample_store_ready")
        else "FAULT - {}".format(status.get("storage_error"))
    ))
    print("Can measure: {}".format(status.get("can_measure")))

    print_calibration(status.get("calibration") or {})


def print_calibration(calibration):
    """
    Show that calibration is fixed and needs no operator action.

    The operator should never be tempted to look for a "measure white"
    or "measure dark" command: there isn't one, by design.
    """
    if not calibration:
        return

    print()
    print("Calibration")
    print("  mode:             {}".format(
        calibration.get("mode", "?").replace("_", " ")
    ))
    print("  references file:  {}".format(
        calibration.get("references_file")
    ))
    print("  calibration id:   {}".format(
        calibration.get("calibration_id")
    ))

    for name in ("dark", "white"):
        entry = calibration.get(name) or {}
        missing = entry.get("missing") or []

        print()
        print("  {} reference:   {}".format(
            name,
            entry.get("state", "UNKNOWN"),
        ))
        print("  channels:         {}/{}".format(
            entry.get("channels_present", 0),
            entry.get("channels_required", 18),
        ))

        if missing:
            print("  MISSING:          {}".format(", ".join(missing)))

    print()
    print("  runtime recalibration:")
    print("                    {}".format(
        calibration.get("runtime_recalibration", "DISABLED")
    ))

    if not calibration.get("loaded"):
        print()
        print("  ERROR: {}".format(calibration.get("error")))
        print("  Measurement will be refused until this is corrected.")


def menu_help(client):
    print()
    print("=" * 60)
    print(" HELP")
    print("=" * 60)
    print()

    for line in WORKFLOW_TEXT.strip().split("\n"):
        print(line)

    print()
    print("-" * 60)
    print()
    print("[1] Choose Sample / Slot")
    print("    Physically moves the requested Slot to the loading hole.")
    print("    If the previous sample is still at the scanner, the")
    print("    loading orientation is restored automatically first.")
    print()
    print("[2] Prepare Sample")
    print("    Assigns Sample ID and optional science metadata.")
    print("    No movement and no measurement happen here.")
    print()
    print("[3] Confirm Sample Loaded")
    print("    Confirms that soil has been deposited.")
    print("    Measurement is impossible before this step.")
    print()
    print("[4] Measure Sample")
    print("    Rotates the currently selected LOADED sample 180 deg to")
    print("    the scanner, takes a new spectrum, processes it, compares")
    print("    it against all reference materials and saves the result.")
    print()
    print("[5] Fine Carousel Alignment")
    print("    Makes a small physical correction in degrees without")
    print("    changing the logical slot.")
    print()
    print("[t] Tools / Records")
    print("    Secondary and testing functions: sample database,")
    print("    system status, re-sync, servo and sensor diagnostics.")
    print()
    print("-" * 60)
    print()
    print("SAMPLE DATABASE")
    print()
    print("samples.json stores all real competition Samples.")
    print()
    print("  Prepare Sample       creates the Sample record")
    print("  Confirm Loaded       updates its loading state")
    print("  Measure Sample       adds the spectra and analysis")
    print("  Sample Database      view, edit, rename, delete")
    print()
    print("Deleting a Sample is NOT the same as clearing a Slot:")
    print()
    print("  Clear Physical Slot  frees the carousel slot,")
    print("                       the Sample stays in samples.json")
    print()
    print("  Delete Sample        permanently removes the scientific")
    print("                       record from samples.json")
    print()
    print("Neither one removes soil from the carousel.")
    print()
    print("-" * 60)

    try:
        data = client.help()
    except (RoverScienceError, TimeoutError) as error:
        print("Could not read module help: {}".format(error))

        return

    calibration = data.get("calibration") or {}

    print()
    print("CALIBRATION")
    print()

    line = ""

    for word in calibration.get("text", "").split():
        if len(line) + len(word) + 1 > 60:
            print(line)
            line = word
        else:
            line = "{} {}".format(line, word).strip()

    if line:
        print(line)


def menu_servo_diagnostics(client):
    """Manual movement tools, kept out of the competition workflow."""
    while True:
        print()
        print("=" * 60)
        print(" SERVO / CAROUSEL DIAGNOSTICS")
        print("=" * 60)
        print()
        print("[1] Move whole slots")
        print("[2] Fine alignment by degrees")
        print("[3] STOP servo")
        print("[4] Show carousel status")
        print("[0] Back")

        choice = ask("Select").strip()

        try:
            if choice == "1":
                menu_move_whole_slot(client)

            elif choice == "2":
                menu_fine_adjust(client)

            elif choice == "3":
                client.servo_stop()
                print("Servo stopped.")

            elif choice == "4":
                print()
                print(json.dumps(client.get_carousel_status(), indent=2))

            elif choice == "0":
                return

            else:
                print("Unknown option.")

        except RoverScienceError as error:
            print("Refused: {}".format(error.message))

        except TimeoutError as error:
            print("Timeout: {}".format(error))


def menu_resync(client):
    """Re-establish the carousel origin mid-run."""
    print()
    print("Re-sync will re-establish the physical carousel position.")
    print("Use this only if tracking has become incorrect.")
    print()

    if not ask("Continue? [y/N]").strip().lower().startswith("y"):
        print("Cancelled.")

        return

    menu_initial_calibration(client)


def print_spectrum(title, channels_nm, values):
    """One channel-per-row table: CH, wavelength, value."""
    print()
    print(title)
    print()
    print("CH   nm    value")

    for channel in SPECTRAL_CHANNELS:
        value = values.get(channel)

        print("{:<4} {:<5} {}".format(
            channel,
            channels_nm.get(channel, "?"),
            "{:.6f}".format(value) if isinstance(value, (int, float))
            else "-",
        ))


def print_analysis_block(matches, analysis, limit=None):
    """Ranked matches plus the automatic interpretation."""
    print()
    print("REFERENCE MATCHES")
    print()

    shown = matches if limit is None else matches[:limit]

    for match in shown:
        print("{:>2}. {:<28} {:>9}".format(
            match.get("rank"),
            match.get("material"),
            format_score(match.get("similarity_percent")),
        ))

    if limit is not None and len(matches) > limit:
        print("... {} more (all stored in the record)".format(
            len(matches) - limit
        ))

    print()
    print("ANALYSIS")
    print()
    print("Best match:")
    print(analysis.get("best_match"))
    print()
    print("Similarity:")
    print(format_score(analysis.get("best_match_score")))

    if analysis.get("second_match"):
        print()
        print("Second:")
        print("{} - {}".format(
            analysis.get("second_match"),
            format_score(analysis.get("second_match_score")),
        ))
        print()
        print("Difference:")
        print(format_score(analysis.get("score_difference")))

    print()
    print("Status:")
    print("{} / confidence {}".format(
        analysis.get("status"),
        analysis.get("confidence"),
    ))

    conclusion = analysis.get("automatic_conclusion")

    if conclusion:
        print()
        print("Conclusion:")
        print(conclusion)


STAGE_LABELS = {
    "COMMAND_ROUTING": "Command routing",
    "SENSOR_OBJECT": "Sensor initialization",
    "I2C_SCAN": "I2C scan",
    "PHYSICAL_REGISTER": "Physical registers",
    "VIRTUAL_REGISTER": "Virtual registers",
    "SLAVE_DEVICES": "Slave devices",
    "SENSOR_CONFIG": "Sensor configuration",
    "LED_CONTROL": "LED control",
    "WAIT_DATA_READY": "Data ready",
    "CHANNEL_ACQUISITION": "18-channel acquisition",
    "CHANNEL_VALIDATION": "Channel validation",
    "REFERENCES": "Fixed references",
    "DARK_CORRECTION": "Dark correction",
    "NORMALIZATION": "Normalization",
    "MATERIAL_DATABASE": "Material database",
    "SPECTRAL_COMPARISON": "Spectral comparison",
    "JSON_RESPONSE": "JSON response",
}


def print_stage_detail(details, indent="       "):
    """Render a stage's detail dictionary readably."""
    if not isinstance(details, dict):
        return

    for key in sorted(details.keys()):
        value = details[key]

        if key == "channels" and isinstance(value, list):
            continue

        if isinstance(value, list):
            value = ", ".join(str(v) for v in value) or "none"

        elif isinstance(value, dict):
            value = json.dumps(value)

        print("{}{}: {}".format(indent, key, value))


def print_diagnostics(data):
    """Turn the firmware's structured stages into a readable report."""
    stages = data.get("diagnostics") or []

    print()
    print("=" * 60)
    print(" AS7265x SENSOR DIAGNOSTICS")
    print("=" * 60)
    print()

    for entry in stages:
        stage = entry.get("stage", "?")
        status = entry.get("status", "?")
        label = STAGE_LABELS.get(stage, stage)

        print("[{}] {}  ({} ms)".format(
            status, label, entry.get("duration_ms", 0)
        ))

        print_stage_detail(entry.get("details"))

        error = entry.get("error")

        if error:
            print("       {}".format(error.get("code")))
            print("       {}".format(error.get("message")))

            for key in ("exception_type", "exception_message"):
                if error.get(key):
                    print("       {}: {}".format(key, error[key]))

            print_stage_detail(error.get("details"))

        print()

    print("=" * 60)
    print(" SENSOR DIAGNOSTIC SUMMARY")
    print("=" * 60)
    print()

    for entry in stages:
        label = STAGE_LABELS.get(entry.get("stage"), entry.get("stage"))
        print("{:<26} {}".format(label, entry.get("status")))

    print()
    print("OVERALL:")
    print(data.get("overall", "?"))

    failure = data.get("first_failure")

    if failure:
        print()
        print("FIRST FAILURE:")
        print(failure.get("code"))
        print()
        print("Stage:")
        print(STAGE_LABELS.get(failure.get("stage"), failure.get("stage")))
        print()
        print("Message:")
        print(failure.get("message"))


def print_test_result(data, title=" SENSOR TEST RESULT"):
    """Raw, dark-corrected, normalized and matches, when present."""
    channels_nm = data.get("channels_nm") or {}

    print()
    print("=" * 60)
    print(title)
    print("=" * 60)

    if data.get("raw"):
        print_spectrum("RAW DATA", channels_nm, data["raw"])

    if data.get("dark_corrected"):
        print_spectrum("DARK-CORRECTED", channels_nm,
                       data["dark_corrected"])

    if data.get("normalized"):
        print_spectrum("NORMALIZED", channels_nm, data["normalized"])

    if data.get("reference_matches"):
        print_analysis_block(
            data["reference_matches"], data.get("analysis") or {}
        )

    print()
    print("TEST ONLY - NOTHING SAVED")


def report_sensor_failure(error, stage_hint=None):
    """Always explain a sensor failure; never fall through in silence."""
    data = error.data or {}

    print()
    print("=" * 60)
    print(" SENSOR TEST FAILED")
    print("=" * 60)
    print()
    print("Stage:")
    print(data.get("stage") or stage_hint or "unknown")
    print()
    print("Error:")
    print(error.code)
    print()
    print(error.message)

    if data.get("missing_channels"):
        print()
        print("Missing channels:")
        print(", ".join(data["missing_channels"]))

    if data.get("exception_type"):
        print()
        print("Exception type:")
        print(data["exception_type"])

    print()
    print("Sensor measurement was NOT started or did not complete.")


def pause():
    ask("Press Enter to continue")


def menu_sensor_test(client):
    """
    AS7265x diagnostics.

    Every option always prints either a result or an exact reason. None
    of them touches sample data, slot state or the carousel, and none of
    them requires carousel synchronization.
    """
    while True:
        print()
        print("=" * 60)
        print(" SENSOR TESTING")
        print("=" * 60)
        print()
        print("[1] Full Diagnostics")
        print("    Check complete AS7265x sensor path.")
        print()
        print("[2] I2C Scan")
        print("    Check sensor address and I2C communication.")
        print()
        print("[3] Raw Measurement")
        print("    Read and display 18 raw channels.")
        print()
        print("[4] Full Test Measurement")
        print("    Raw data, normalization and database comparison.")
        print()
        print("[0] Back")

        choice = ask("Select").strip().lower()

        if choice == "0":
            return

        if choice not in ("1", "2", "3", "4"):
            print("Unknown option.")

            continue

        started = time.monotonic()

        try:
            if choice == "1":
                print()
                print("Running diagnostics, please wait...")
                sys.stdout.flush()

                data = client.sensor_diagnostics()
                print_diagnostics(data)

                if data.get("raw"):
                    print_test_result(data)

            elif choice == "2":
                print()
                print("Scanning I2C bus...")
                sys.stdout.flush()

                data = client.i2c_scan()
                print_i2c_scan(data)

            elif choice == "3":
                print()
                print("Reading 18 raw channels...")
                sys.stdout.flush()

                data = client.raw_measurement()
                print_spectrum(
                    "RAW DATA",
                    data.get("channels_nm") or {},
                    data.get("raw") or {},
                )

                print()
                print("Channels received: {}/18".format(
                    data.get("channels_received")
                ))

                if data.get("warning"):
                    print("WARNING: {}".format(data["warning"]))

                print()
                print("TEST ONLY - NOTHING SAVED")

            else:
                print()
                print("Taking test measurement...")
                sys.stdout.flush()

                data = client.test_measurement()
                print_test_result(data)

        except RoverScienceError as error:
            report_sensor_failure(error)

        except TimeoutError as error:
            print()
            print("=" * 60)
            print(" NO RESPONSE")
            print("=" * 60)
            print()
            print(error)

        except Exception as error:                       # noqa: BLE001
            # A client-side bug must be visible, not swallowed.
            print()
            print("CLIENT ERROR")
            print()
            print("Exception type:")
            print(type(error).__name__)
            print()
            print("Message:")
            print(error)

            if client.verbose:
                import traceback

                traceback.print_exc()

        if client.verbose:
            print()
            print("[DEBUG] duration: {:.3f} s".format(
                time.monotonic() - started
            ))

        pause()


def print_i2c_scan(data):
    """Explicit result for the bus scan, pass or fail."""
    details = data.get("details") or {}
    error = data.get("error")

    print()
    print("=" * 60)
    print(" I2C SCAN")
    print("=" * 60)
    print()
    print("Bus:       {}".format(details.get("bus")))
    print("SDA:       {}".format(details.get("sda")))
    print("SCL:       {}".format(details.get("scl")))
    print("Frequency: {} Hz".format(details.get("frequency_hz")))
    print()
    print("Expected AS7265x address:")
    print(details.get("expected_address"))
    print()
    print("Detected addresses:")

    addresses = details.get("addresses")

    if addresses:
        for address in addresses:
            print("  {}".format(address))
    else:
        print("  none")

    print()

    if data.get("status") == "PASS":
        print("[PASS] AS7265x detected at {}.".format(
            details.get("expected_address")
        ))

    else:
        print("[FAIL] {}".format((error or {}).get("code", "UNKNOWN")))
        print()
        print((error or {}).get("message", ""))


CSV_SUMMARY_COLUMNS = (
    "sample_id",
    "slot_id",
    "state",
    "created_at",
    "loaded_at",
    "measured_at",
    "task",
    "hypothesis",
    "location",
    "map_point",
    "note",
    "photo",
    "best_match",
    "best_match_score",
    "second_match",
    "second_match_score",
    "score_difference",
    "status",
    "confidence",
    "calibration_id",
)


def _csv_row(record):
    """Flatten one stored record into summary columns + 18 channels."""
    metadata = record.get("metadata") or {}
    analysis = record.get("analysis") or {}
    timestamps = record.get("timestamps") or {}
    measurement = record.get("measurement") or {}
    normalized = measurement.get("normalized") or {}

    row = {
        "sample_id": record.get("sample_id"),
        "slot_id": record.get("slot_id"),
        "state": record.get("state"),
        "created_at": timestamps.get("created_at"),
        "loaded_at": timestamps.get("loaded_at"),
        "measured_at": timestamps.get("measured_at"),
        "best_match": analysis.get("best_match"),
        "best_match_score": analysis.get("best_match_score"),
        "second_match": analysis.get("second_match"),
        "second_match_score": analysis.get("second_match_score"),
        "score_difference": analysis.get("score_difference"),
        "status": analysis.get("status"),
        "confidence": analysis.get("confidence"),
        "calibration_id": record.get("calibration_id"),
    }

    for key in ("task", "hypothesis", "location", "map_point",
                "note", "photo"):
        row[key] = metadata.get(key)

    for channel in SPECTRAL_CHANNELS:
        row["norm_" + channel] = normalized.get(channel)

    return row


def menu_export(client):
    """
    Copy every stored sample from the ESP32 onto this PC.

    Uses the ordinary list_samples / get_sample commands over the link
    that is already open, so no mpremote and no free serial port are
    needed. The ESP32 copy is left untouched - this is a download, not
    a move.
    """
    print()
    print("=" * 60)
    print(" EXPORT SAMPLES TO PC")
    print("=" * 60)
    print()

    try:
        listing = client.list_samples()

    except (RoverScienceError, TimeoutError) as error:
        print("Could not read the sample list: {}".format(error))

        return

    summaries = listing.get("samples") or []

    print("Samples stored on the ESP32: {}".format(len(summaries)))

    if not summaries:
        print()
        print("Nothing to export.")

        return

    default_dir = "samples_export"
    target = ask("Destination folder [{}]".format(default_dir))
    target = target or default_dir

    try:
        os.makedirs(target, exist_ok=True)

    except OSError as error:
        print("Could not create {}: {}".format(target, error))

        return

    print()

    records = []
    failed = []

    for summary in summaries:
        sample_id = summary.get("sample_id")

        try:
            record = client.get_sample(sample_id).get("sample") or {}

        except (RoverScienceError, TimeoutError) as error:
            print("  {:<10} FAILED  {}".format(sample_id, error))
            failed.append(sample_id)

            continue

        path = os.path.join(target, "{}.json".format(sample_id))

        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(record, handle, indent=2, ensure_ascii=False)

        except OSError as error:
            print("  {:<10} FAILED  {}".format(sample_id, error))
            failed.append(sample_id)

            continue

        records.append(record)
        print("  {:<10} OK      {}".format(sample_id, path))

    if not records:
        print()
        print("No sample could be exported.")

        return

    # Index, mirroring samples.json on the device.
    index_path = os.path.join(target, "samples.json")

    with open(index_path, "w", encoding="utf-8") as handle:
        json.dump(
            {"version": 1, "samples": summaries},
            handle, indent=2, ensure_ascii=False,
        )

    # Flat table for reports and plotting.
    csv_path = os.path.join(target, "samples.csv")
    columns = list(CSV_SUMMARY_COLUMNS) + [
        "norm_" + channel for channel in SPECTRAL_CHANNELS
    ]

    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()

        for record in records:
            writer.writerow(_csv_row(record))

    print()
    print("Exported {} sample(s) to {}".format(
        len(records), os.path.abspath(target)
    ))
    print()
    print("  samples.json    index")
    print("  samples.csv     summary + normalized spectra")
    print("  <SampleID>.json full scientific record")

    if failed:
        print()
        print("Could not export: {}".format(", ".join(failed)))

    print()
    print("The ESP32 copy was NOT modified or deleted.")


EDITABLE_FIELDS = (
    ("task", "Task"),
    ("hypothesis", "Hypothesis"),
    ("location", "Location"),
    ("map_point", "Map point"),
    ("note", "Note"),
    ("photo", "Photo reference"),
)


def print_database_error(operation, sample_id, error):
    """Every database failure is shown; none returns in silence."""
    print()
    print("=" * 60)
    print(" SAMPLE DATABASE ERROR")
    print("=" * 60)
    print()
    print("Operation:")
    print(operation)

    if sample_id:
        print()
        print("Sample:")
        print(sample_id)

    print()
    print("Error:")
    print(error.code)
    print()
    print("Message:")
    print(error.message)
    print()
    print("No data was changed.")


def load_database(client):
    """Health + summaries in one go, or None if unreachable."""
    try:
        health = client.get_database_health()
        listing = client.list_samples()

    except (RoverScienceError, TimeoutError) as error:
        print()
        print("Could not read the sample database: {}".format(error))

        return None

    return health, listing.get("samples") or []


def print_database_table(health, samples):
    print()
    print("=" * 60)
    print(" SAMPLE DATABASE")
    print("=" * 60)
    print()
    print("File:")
    print(health.get("file"))
    print()

    if not health.get("ready"):
        print("Status:")
        print("ERROR - {}".format(health.get("error_code")))
        print()
        print(health.get("error"))
        print()
        print("No write operation will be performed until the database")
        print("problem is resolved.")

        return

    print("Status:")
    print("READY")
    print()
    print("Samples:")
    print(health.get("count", 0))

    states = health.get("states") or {}

    if states:
        print()

        for state in sorted(states.keys()):
            print("  {:<16} {}".format(state, states[state]))

    if not samples:
        print()
        print("Sample database is empty.")

        return

    print()
    print("{:<10} {:<6} {:<14} {:<22} {}".format(
        "ID", "Slot", "State", "Best Match", "Similarity"
    ))
    print("-" * 66)

    for entry in samples:
        score = entry.get("best_match_score")

        print("{:<10} {:<6} {:<14} {:<22} {}".format(
            str(entry.get("sample_id")),
            str(entry.get("slot_id")),
            str(entry.get("state")),
            str(entry.get("best_match") or "----"),
            format_score(score) if isinstance(score, (int, float))
            else "----",
        ))


def print_full_sample(record):
    """The complete stored record, in readable form."""
    metadata = record.get("metadata") or {}
    timestamps = record.get("timestamps") or {}
    measurement = record.get("measurement") or {}
    analysis = record.get("analysis") or {}
    matches = record.get("reference_matches") or []

    print()
    print("=" * 60)
    print(" SAMPLE {}".format(record.get("sample_id")))
    print("=" * 60)
    print()
    print("GENERAL")
    print()
    print("Sample ID:      {}".format(record.get("sample_id")))
    print("Physical Slot:  {}".format(record.get("slot_id")))
    print("State:          {}".format(record.get("state")))
    print()
    print("Created:  {}".format(timestamps.get("created_at")))
    print("Loaded:   {}".format(timestamps.get("loaded_at")))
    print("Measured: {}".format(timestamps.get("measured_at")))

    print()
    print("METADATA")
    print()

    for key, label in EDITABLE_FIELDS:
        print("{:<18} {}".format(label + ":", metadata.get(key)))

    if not measurement:
        print()
        print("MEASUREMENT")
        print()
        print("No measurement has been recorded yet.")
        print("Current state: {}".format(record.get("state")))

        return

    channels_nm = measurement.get("channels_nm") or {}
    calibration = measurement.get("calibration") or {}
    settings = measurement.get("sensor_settings") or {}

    print()
    print("MEASUREMENT")
    print()
    print("Calibration:  {} ({})".format(
        calibration.get("source"), calibration.get("mode")
    ))
    print("Calibration ID: {}".format(record.get("calibration_id")))
    print("Channels:     {} / 18".format(
        len(measurement.get("raw") or {})
    ))
    print("Gain:         {}".format(settings.get("gain_x")))
    print("Integration:  {} cycles".format(
        settings.get("integration_cycles")
    ))

    print_spectrum("RAW SPECTRUM", channels_nm,
                   measurement.get("raw") or {})
    print_spectrum("DARK-CORRECTED SPECTRUM", channels_nm,
                   measurement.get("dark_corrected") or {})
    print_spectrum("NORMALIZED SPECTRUM", channels_nm,
                   measurement.get("normalized") or {})

    if matches:
        print_analysis_block(matches, analysis)


def menu_db_open(client, samples):
    sample_id = ask("Sample ID")

    if not sample_id:
        print("Cancelled.")

        return

    try:
        record = client.get_sample(sample_id).get("sample") or {}

    except RoverScienceError as error:
        print_database_error("Open Sample", sample_id, error)

        return

    print_full_sample(record)


def menu_db_edit(client, samples):
    """
    Edit mission metadata only.

    Spectra, matches, analysis, timestamps, sensor settings and
    calibration are produced by the measurement pipeline and stay
    scientifically traceable - they are never editable here.
    """
    sample_id = ask("Sample ID")

    if not sample_id:
        print("Cancelled.")

        return

    try:
        record = client.get_sample(sample_id).get("sample") or {}

    except RoverScienceError as error:
        print_database_error("Edit Sample", sample_id, error)

        return

    while True:
        metadata = record.get("metadata") or {}

        print()
        print("=" * 60)
        print(" EDIT SAMPLE {}".format(record.get("sample_id")))
        print("=" * 60)
        print()

        for index, (key, label) in enumerate(EDITABLE_FIELDS, start=1):
            print("[{}] {:<18} {}".format(
                index, label, metadata.get(key)
            ))

        print()
        print("[0] Back")
        print()
        print("Measurement data is read-only and cannot be edited here.")

        choice = ask("Select").strip()

        if choice == "0":
            return

        try:
            position = int(choice) - 1
            key, label = EDITABLE_FIELDS[position]

        except (ValueError, IndexError):
            print("Unknown option.")

            continue

        print()
        print("Current {}:".format(label))
        print(metadata.get(key))
        print()
        print('Enter "-" to clear the field, or press Enter to keep it.')

        value = ask("New value")

        if not value:
            print("Unchanged.")

            continue

        if value == "-":
            value = None

        try:
            updated = client.update_sample_metadata(
                record["sample_id"], {key: value}
            )

        except RoverScienceError as error:
            print_database_error(
                "Edit Sample", record.get("sample_id"), error
            )

            continue

        record["metadata"] = updated.get("metadata") or {}
        print("{} updated.".format(label))


def menu_db_rename(client, samples):
    sample_id = ask("Sample ID to rename")

    if not sample_id:
        print("Cancelled.")

        return

    new_id = ask("New Sample ID")

    if not new_id:
        print("Cancelled.")

        return

    print()
    print("Rename:")
    print()
    print(sample_id)
    print("->")
    print(new_id)
    print()
    print("[y] confirm")
    print("[n] cancel")

    if not ask("Select").strip().lower().startswith("y"):
        print("Cancelled. No data was changed.")

        return

    try:
        data = client.rename_sample(sample_id, new_id)

    except RoverScienceError as error:
        print_database_error("Rename Sample", sample_id, error)

        return

    print()
    print("Renamed {} -> {}".format(
        data.get("previous_sample_id"), data.get("sample_id")
    ))

    if data.get("slot_updated"):
        print("Runtime Slot {} now points at the new ID.".format(
            data["slot_updated"]
        ))


def menu_db_delete(client, samples):
    """
    Permanent deletion, guarded by a typed confirmation.

    A bare 'y' is not enough: destroying a measured sample is not
    recoverable, so the operator has to type the ID itself.
    """
    sample_id = ask("Sample ID to delete")

    if not sample_id:
        print("Cancelled.")

        return

    entry = next(
        (s for s in samples if s.get("sample_id") == sample_id), None
    )

    if entry is None:
        print("No sample with ID {} in the database.".format(sample_id))

        return

    print()
    print("=" * 60)
    print(" DELETE SAMPLE")
    print("=" * 60)
    print()
    print("Sample:      {}".format(entry.get("sample_id")))
    print("Slot:        {}".format(entry.get("slot_id")))
    print("State:       {}".format(entry.get("state")))
    print("Measurement: {}".format(
        "YES" if entry.get("measured") else "NO"
    ))

    if entry.get("best_match"):
        print("Best match:  {} - {}".format(
            entry.get("best_match"),
            format_score(entry.get("best_match_score")),
        ))

    linked = None

    try:
        slots = client.get_slots().get("slots") or []
        linked = next(
            (s for s in slots if s.get("sample_id") == sample_id), None
        )

    except (RoverScienceError, TimeoutError):
        pass

    clear_slot = False

    if linked is not None:
        print()
        print("WARNING")
        print()
        print("{} is still linked to physical Slot {}.".format(
            sample_id, linked.get("slot_id")
        ))
        print()
        print("Deleting this Sample removes its persistent scientific")
        print("record. The physical soil may still be inside the slot.")
        print()
        print("[1] Delete Sample and clear runtime Slot {}".format(
            linked.get("slot_id")
        ))
        print("[2] Cancel")

        if ask("Select").strip() != "1":
            print("Deletion cancelled. No data was changed.")

            return

        clear_slot = True

    print()
    print("WARNING")
    print()
    print("This permanently removes {} from samples.json.".format(
        sample_id
    ))
    print()
    print("This cannot be undone.")
    print()
    print('Type the Sample ID to confirm, or "c" to cancel.')

    typed = ask("Confirm").strip()

    if typed.lower() in ("c", "cancel", ""):
        print("Deletion cancelled. No data was changed.")

        return

    if typed != sample_id:
        print()
        print("Typed ID does not match. Deletion cancelled.")
        print("No data was changed.")

        return

    try:
        data = client.delete_sample(sample_id, clear_slot=clear_slot)

    except RoverScienceError as error:
        print_database_error("Delete Sample", sample_id, error)
        print()
        print("Original Sample was NOT deleted.")

        return

    print()
    print("{} removed from samples.json.".format(data.get("sample_id")))

    if data.get("slot_cleared"):
        print("Runtime Slot {} reset to EMPTY.".format(
            data["slot_cleared"]
        ))

    print()
    print("This only changes software state.")
    print("It does NOT physically remove soil from the carousel.")


def menu_sample_database(client):
    """View, edit, rename and delete persistent Samples."""
    while True:
        state = load_database(client)

        if state is None:
            pause()

            return

        health, samples = state
        print_database_table(health, samples)

        print()
        print("[1] Open Sample")
        print("[2] Edit Sample")
        print("[3] Rename Sample ID")
        print("[4] Delete Sample")
        print("[5] Refresh Database")
        print("[6] Export Samples to PC")
        print("[0] Back")

        choice = ask("Select").strip().lower()

        if choice == "0":
            return

        actions = {
            "1": menu_db_open,
            "2": menu_db_edit,
            "3": menu_db_rename,
            "4": menu_db_delete,
        }

        if choice == "5":
            print("Database reloaded.")

            continue

        if choice == "6":
            menu_export(client)
            pause()

            continue

        handler = actions.get(choice)

        if handler is None:
            print("Unknown option.")

            continue

        if not health.get("ready"):
            print()
            print("The database is not readable; no operation will run.")
            pause()

            continue

        try:
            handler(client, samples)

        except RoverScienceError as error:
            print_database_error("Sample Database", None, error)

        except TimeoutError as error:
            print()
            print("Timeout: {}".format(error))

        pause()


TOOLS_MENU = (
    ("1", "Show Slots",
     "Show physical carousel states.", menu_show_slots),
    ("2", "Sample Database",
     "View, edit and delete saved Samples.", menu_sample_database),
    ("3", "System Status",
     "Show module status.", None),
    ("4", "Re-sync Carousel",
     "Restore carousel position tracking.", menu_resync),
    ("5", "Servo / Carousel Diagnostics",
     "Test carousel movement.", menu_servo_diagnostics),
    ("6", "Sensor Testing",
     "Test AS7265x and spectral analysis.", menu_sensor_test),
    ("7", "Clear Physical Slot",
     "Free a slot; the saved Sample is kept.", menu_clear),
)


def menu_tools(client):
    """Everything that is not part of the five-step competition loop."""
    while True:
        print()
        print("=" * 60)
        print(" TOOLS / RECORDS")
        print("=" * 60)
        print()

        for key, label, description, _handler in TOOLS_MENU:
            print("[{}] {}".format(key, label))
            print("    {}".format(description))
            print()

        print("[0] Back")

        choice = ask("Select").strip().lower()

        if choice == "0":
            return

        handler = None

        for key, _label, _description, action in TOOLS_MENU:
            if choice == key:
                handler = action or print_status

                break

        if handler is None:
            print("Unknown option.")

            continue

        try:
            handler(client)

        except RoverScienceError as error:
            print()
            print("Module refused the command:")
            print("  code   : {}".format(error.code))
            print("  message: {}".format(error.message))

        except TimeoutError as error:
            print()
            print("Timeout: {}".format(error))

        except KeyboardInterrupt:
            print()
            print("Cancelled.")


MAIN_ACTIONS = {
    "1": menu_choose_slot,
    "2": menu_prepare,
    "3": menu_confirm,
    "4": menu_measure,
    "5": menu_fine_adjust,
    "t": menu_tools,
    "h": menu_help,
}


def interactive(client):
    """
    Two stages: calibrate the carousel once, then run the sample loop.

    Keeping them apart is what makes the competition workflow short -
    the operator never sees sample commands that cannot work yet.
    """
    print("Connecting to the science module on {}...".format(client.port))

    try:
        client.wait_online()
    except (RoverScienceError, TimeoutError) as error:
        print("No answer from the science module: {}".format(error))
        print("Check that the USB cable is connected, that the port is "
              "correct, and that no other program (REPL, mpremote, serial "
              "monitor) is holding the port open.")

        return 1

    print("Connection: ONLINE")

    while True:
        state = read_state(client)

        if state is None:
            if not ask("Retry? [Y/n]").strip().lower().startswith("n"):
                continue

            return 1

        slots, carousel, entry = state

        # ---- STAGE 0: no origin yet -------------------------------
        if not carousel.get("position_valid"):
            print_startup_screen(client, carousel)

            choice = ask("Select").strip().lower()

            if choice == "q":
                return 0

            if choice == "0":
                menu_initial_calibration(client)

            elif choice == "h":
                menu_help(client)

            else:
                print("Unknown option.")

            continue

        # ---- STAGE 1: normal sample workflow -----------------------
        print_main_screen(client, slots, carousel, entry)

        choice = ask("Select").strip().lower()

        if choice == "q":
            return 0

        handler = MAIN_ACTIONS.get(choice)

        if handler is None:
            print("Unknown option.")

            continue

        try:
            handler(client)

        except RoverScienceError as error:
            print()
            print("Module refused the command:")
            print("  code   : {}".format(error.code))
            print("  message: {}".format(error.message))

        except TimeoutError as error:
            print()
            print("Timeout: {}".format(error))

        except KeyboardInterrupt:
            print()
            print("Cancelled.")



def one_shot(client, command, payload_text):
    payload = {}

    if payload_text:
        payload = json.loads(payload_text)

        if not isinstance(payload, dict):
            print("--payload must be a JSON object.", file=sys.stderr)

            return 2

    # Opening the port may have reset the board; let it come up first.
    client.wait_online()

    data = client.request(command, timeout=MEASUREMENT_TIMEOUT, **payload)

    print(json.dumps(data, indent=2))

    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Main-PC client for the Freya AS7265x science module."
        )
    )

    parser.add_argument(
        "--port",
        required=True,
        help="Serial port of the rover UART link, e.g. COM5 "
             "or /dev/ttyUSB0.",
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=DEFAULT_BAUDRATE,
        help="Baud rate (default: {}).".format(DEFAULT_BAUDRATE),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="Response timeout in seconds for ordinary commands "
             "(default: {:.0f}).".format(DEFAULT_TIMEOUT),
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=CONNECT_TIMEOUT,
        help="Seconds to wait for the module to answer a ping after the "
             "port is opened, which may reset the board "
             "(default: {:.0f}).".format(CONNECT_TIMEOUT),
    )
    parser.add_argument(
        "--command",
        help="Run a single command and print the JSON result instead of "
             "opening the interactive menu.",
    )
    parser.add_argument(
        "--payload",
        help="JSON object with extra fields for --command, "
             "e.g. '{\"slot\": 1}'.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Echo the raw protocol traffic.",
    )

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    try:
        client = RoverScienceClient(
            port=args.port,
            baudrate=args.baud,
            timeout=args.timeout,
            connect_timeout=args.connect_timeout,
            verbose=args.verbose,
        )
    except RuntimeError as error:
        print(error, file=sys.stderr)

        return 2

    try:
        client.open()
    except Exception as error:  # serial.SerialException and friends
        print("Could not open {}: {}".format(args.port, error),
              file=sys.stderr)

        return 2

    try:
        if args.command:
            return one_shot(client, args.command, args.payload)

        return interactive(client)

    except RoverScienceError as error:
        print("Module refused the command: {}".format(error),
              file=sys.stderr)

        return 1

    except TimeoutError as error:
        print("Timeout: {}".format(error), file=sys.stderr)

        return 1

    except KeyboardInterrupt:
        print()

        return 0

    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
