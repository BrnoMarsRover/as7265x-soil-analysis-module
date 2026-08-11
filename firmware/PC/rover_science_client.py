#!/usr/bin/env python3
"""
Freya science module - main computer application.

Mission controller and operator interface. This program owns the
workflow, the Sample lifecycle and the persistent Sample archive. It
talks to two things:

    PC  ->  serial  ->  ESP32      hardware: carousel + RAW spectrum
    PC  ->  import  ->  BD         science:  White/Dark, normalization,
                                             database comparison

The ESP32 never learns what a sample resembles, and BD never learns
that a serial port exists.

Usage:

    py rover_science_client.py --port COM4
    py rover_science_client.py --port COM4 --command get_status
"""

import argparse
import json
import sys
from pathlib import Path

# BD lives beside this directory. Importing it by path means the
# application runs from anywhere without PYTHONPATH setup.
PC_DIR = Path(__file__).resolve().parent
BD_DIR = PC_DIR.parent / "BD"

if str(BD_DIR) not in sys.path:
    sys.path.insert(0, str(BD_DIR))

import sample_analysis                                  # noqa: E402
from database import DatabaseError, MaterialDatabase, References  # noqa: E402

from esp32_link import (                                # noqa: E402
    CONNECT_TIMEOUT,
    DEFAULT_BAUDRATE,
    DEFAULT_TIMEOUT,
    ESP32Link,
    LinkError,
    MEASUREMENT_TIMEOUT,
    utc_timestamp,
)
from sample_store import (                              # noqa: E402
    METADATA_FIELDS,
    STATE_EMPTY,
    STATE_LOADED,
    STATE_MEASURED,
    STATE_READY_TO_LOAD,
    SampleStore,
    StorageError,
    validate_sample_id,
)

SLOT_COUNT = 8
RULE = "=" * 60


# ======================================================================
# small console helpers
# ======================================================================

def ask(prompt, default=""):
    try:
        answer = input("{}: ".format(prompt)).strip()

    except EOFError:
        return default

    return answer or default


def choose(prompt="Select"):
    """Menu input, normalized. A bare Enter is not an unknown option."""
    return ask(prompt).strip().lower()


def ask_int(prompt, minimum=None, maximum=None, default=None):
    """
    Ask for a whole number; blank cancels unless there is a default.

    Returns None only when the operator deliberately cancels, and every
    caller must say something when that happens - silently dropping back
    to the menu is what made "Measure Sample" look like it did nothing.
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


def ask_float(prompt, minimum=None, maximum=None):
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


def confirm(prompt):
    """Explicit yes only. Anything else, including a bare Enter, is no."""
    return ask("{} [y/N]".format(prompt)).strip().lower().startswith("y")


def pause():
    ask("Press Enter to continue")


def banner(title):
    print()
    print(RULE)
    print(" {}".format(title))
    print(RULE)
    print()


def score(value):
    return "{:.2f} %".format(value) if isinstance(value, (int, float)) else "-"


def number(value, digits=4):
    return "{:.{}f}".format(value, digits) if isinstance(
        value, (int, float)
    ) else "-"


def report_link_error(error):
    print()
    print("Module refused the command:")
    print("  code   : {}".format(error.code))
    print("  message: {}".format(error.message))

    data = error.data or {}

    if data.get("recovery"):
        print("  carousel: {}".format(data["recovery"].get("message")))

    elif data.get("moved") is False:
        print("  carousel: nothing was moved.")


def report_failure(error):
    """One place that knows how to print either kind of link failure."""
    if isinstance(error, LinkError):
        report_link_error(error)

    else:
        print()
        print("Timeout: {}".format(error))


# ======================================================================
# mission controller
# ======================================================================

class Mission:
    """
    Everything the operator screens need, in one place.

    Holds the link to the ESP32, the persistent Sample archive and the
    loaded BD science layer. Sample state is read from the archive, so
    it survives a restart of this program; carousel position is read
    from the ESP32, which forgets it on reset.
    """

    def __init__(self, link):
        self.link = link
        self.store = SampleStore()

        self.references = None
        self.database = None
        self.science_error = None

        self.load_science()

    # ------------------------------------------------------------------
    # BD
    # ------------------------------------------------------------------

    def load_science(self):
        """Load the protected White/Dark and material database, read-only."""
        self.references = None
        self.database = None
        self.science_error = None

        try:
            self.references = References()
            self.database = MaterialDatabase()

        except DatabaseError as error:
            self.science_error = "{}: {}".format(error.code, error.message)

        return self.science_error is None

    # ------------------------------------------------------------------
    # state
    # ------------------------------------------------------------------

    def hardware_status(self):
        return self.link.get_status()

    def slot_view(self, status):
        """
        Merge the PC's Sample lifecycle with the ESP32's physical state.

        The PC is authoritative for what a slot MEANS; the ESP32 is
        authoritative for what it physically holds.
        """
        by_slot = self.store.active_samples()
        physical = {
            slot.get("slot_id"): slot
            for slot in (status.get("slots") or [])
        }

        view = []

        for slot_id in range(1, SLOT_COUNT + 1):
            entry = by_slot.get(slot_id)

            view.append({
                "slot_id": slot_id,
                "sample_id": entry.get("sample_id") if entry else None,
                "state": entry.get("state") if entry else STATE_EMPTY,
                "occupied": bool(
                    (physical.get(slot_id) or {}).get("occupied")
                ),
            })

        return view

    def entry_for(self, view, slot_id):
        for entry in view:
            if entry["slot_id"] == slot_id:
                return entry

        return {"slot_id": slot_id, "sample_id": None, "state": STATE_EMPTY}

    # ------------------------------------------------------------------
    # the measurement pipeline
    # ------------------------------------------------------------------

    def analyse_raw(self, raw, sensor_settings):
        """
        RAW in, complete BD result out.

        Raises AnalysisError or DatabaseError; the caller is responsible
        for preserving the RAW spectrum either way.
        """
        if self.references is None:
            raise DatabaseError(
                "REFERENCES_NOT_LOADED",
                self.science_error or "White/Dark references are not loaded.",
            )

        return sample_analysis.analyze(
            raw, self.references, self.database, sensor_settings
        )


# ======================================================================
# screens: status and hardware
# ======================================================================

def print_system_status(mission):
    """PC, BD and ESP32 in one place. A failure in one still shows the rest."""
    banner("SYSTEM STATUS")

    store = mission.store

    print("PC")
    print("Connection:       {}".format(
        "ONLINE" if mission.link.online else "UNKNOWN"
    ))
    print("Sample storage:   {}".format("READY" if store.ready else "ERROR"))
    print("Samples saved:    {}".format(store.count()))
    print("Data directory:   {}".format(store.record_dir.parent))

    if store.error:
        print("Storage error:    {}".format(store.error))

    print()
    print("BD")

    if mission.references is not None:
        refs = mission.references.status()

        print("References:       READY ({}/{} white + {}/{} dark)".format(
            refs["white_channels"], refs["channels_required"],
            refs["dark_channels"], refs["channels_required"],
        ))
        print("Calibration:      {}".format(refs["calibration_id"]))

        if refs["zero_denominator_channels"]:
            print("  warning: White == Dark on {}".format(
                ",".join(refs["zero_denominator_channels"])
            ))

    else:
        print("References:       ERROR - {}".format(mission.science_error))

    if mission.database is not None:
        print("Material DB:      READY ({} materials)".format(
            mission.database.count()
        ))

        incomplete = mission.database.incomplete_materials()

        if incomplete:
            print("  warning: {} material(s) have incomplete spectra".format(
                len(incomplete)
            ))

    else:
        print("Material DB:      ERROR - {}".format(mission.science_error))

    print()
    print("ESP32")

    try:
        status = mission.hardware_status()

    except (LinkError, TimeoutError) as error:
        print("Controller:       UNREACHABLE ({})".format(error))
        print()

        return

    sensor = status.get("sensor") or {}
    settings = sensor.get("settings") or {}
    carousel = status.get("carousel") or {}

    print("Controller:       READY ({} {})".format(
        status.get("firmware"), status.get("version")
    ))
    print("Sensor:           {}".format(
        "READY" if sensor.get("ready") else "UNAVAILABLE"
    ))
    print("I2C:              {} on bus {}".format(
        sensor.get("address"), (sensor.get("bus") or {}).get("bus")
    ))

    if settings:
        print("Integration:      {} cycles".format(
            settings.get("integration_cycles")
        ))
        print("Gain:             {}".format(settings.get("gain_x")))
        print("LED current:      {}".format(
            settings.get("led_current_ma", settings.get("led_current"))
        ))
        print("Mode:             {}".format(settings.get("measurement_mode")))

    # A boot failure that was later recovered is a warning, never a
    # reason to report the sensor as unavailable now.
    if sensor.get("boot_error") and sensor.get("ready"):
        print("Boot warning:     {} (recovered {}x)".format(
            sensor["boot_error"].get("code"), sensor.get("recovery_count")
        ))

    elif sensor.get("boot_error"):
        print("Boot error:       {} - {}".format(
            sensor["boot_error"].get("code"),
            sensor["boot_error"].get("message"),
        ))

    if sensor.get("current_error"):
        print("Current error:    {} - {}".format(
            sensor["current_error"].get("code"),
            sensor["current_error"].get("message"),
        ))

    print()
    print("CAROUSEL")
    print("Synchronized:     {}".format(
        "YES" if carousel.get("position_valid") else "NO"
    ))
    print("Selected slot:    {}".format(carousel.get("selected_slot")))
    print("Loader:           {}".format(carousel.get("current_load_slot")))
    print("Scanner:          {}".format(carousel.get("current_scan_slot")))
    print()


# ======================================================================
# screens: spectrum and analysis
# ======================================================================

def print_spectrum_table(measurement):
    wavelengths = measurement.get("wavelengths") or {}
    raw = measurement.get("raw") or {}
    corrected = measurement.get("dark_corrected") or {}
    normalized = measurement.get("normalized") or {}

    print("CH   nm    {:>12} {:>12} {:>12}".format(
        "RAW", "DARK-CORR", "NORMALIZED"
    ))

    for channel in sample_analysis.CHANNELS:
        print("{:<4} {:<5} {:>12} {:>12} {:>12}".format(
            channel,
            wavelengths.get(channel, "-"),
            number(raw.get(channel)),
            number(corrected.get(channel)),
            number(normalized.get(channel), 6),
        ))


def print_matches(matches, limit=None):
    if not matches:
        print("No reference materials were compared.")

        return

    shown = matches if limit is None else matches[:limit]

    print("{:<4} {:<32} {:>12}".format("#", "Material", "Similarity"))

    for match in shown:
        print("{:<4} {:<32} {:>12}".format(
            match.get("rank"),
            str(match.get("material"))[:32],
            score(match.get("similarity_percent")),
        ))

    if limit is not None and len(matches) > limit:
        print("... {} more (all stored with the sample)".format(
            len(matches) - limit
        ))


def print_result_block(analysis):
    print("Best match:   {}".format(analysis.get("best_match")))
    print("Similarity:   {}".format(score(analysis.get("best_similarity"))))
    print("Second match: {}".format(analysis.get("second_match")))
    print("Difference:   {}".format(score(analysis.get("score_difference"))))
    print("Status:       {}".format(analysis.get("status")))
    print()
    print("Conclusion:")
    print("  {}".format(analysis.get("automatic_conclusion")))


def print_settings_block(settings):
    settings = settings or {}

    print("Integration cycles: {}".format(settings.get("integration_cycles")))
    print("Gain:               {}".format(settings.get("gain_x")))
    print("LED current:        {}".format(
        settings.get("led_current_ma", settings.get("led_current"))
    ))
    print("Mode:               {}".format(settings.get("measurement_mode")))


# ======================================================================
# screens: sensor test
# ======================================================================

ESP32_STAGE_LABELS = (
    ("SENSOR_RECOVERY", "Sensor recovery"),
    ("I2C_ADDRESS", "I2C 0x49"),
    ("INTERNAL_DEVICES", "Internal devices"),
    ("CONFIGURATION", "Configuration"),
    ("ILLUMINATION", "Illumination"),
    ("ACQUISITION", "18-channel acquisition"),
)


def print_check(label, ok):
    print("{:<27}{}".format(label, "PASS" if ok else "FAIL"))


def menu_sensor_test(mission):
    """
    One command that tests the whole system through the PRODUCTION path.

    No carousel movement, no synchronization, no Sample ID, nothing
    saved. The operator never has to decide which internal layer to
    test, and a failure always shows how far it got.
    """
    banner("AS7265x SENSOR TEST")

    print("ESP32 HARDWARE")
    print()

    try:
        data = mission.link.sensor_test_raw()

    except (LinkError, TimeoutError) as error:
        print_check("Serial communication", False)
        print()
        print("FAILED STAGE  SERIAL_LINK")

        if isinstance(error, LinkError):
            print("error code    {}".format(error.code))
            print("message       {}".format(error.message))
        else:
            print("message       {}".format(error))

        print()
        pause()

        return

    print_check("Serial communication", True)

    checks = {entry["stage"]: entry for entry in data.get("checks") or []}

    for stage, label in ESP32_STAGE_LABELS:
        entry = checks.get(stage)

        if entry is None:
            print("{:<27}{}".format(label, "SKIPPED"))
        else:
            print_check(label, entry.get("ok"))

    failed = [
        entry for entry in data.get("checks") or []
        if not entry.get("ok")
    ]

    raw = data.get("raw")
    settings = data.get("sensor_settings")

    if failed and raw is None:
        print()
        print("FAILED STAGE  {}".format(data.get("failed_stage")))

        for entry in failed:
            error = entry.get("error") or {}

            print("error code    {}".format(error.get("code")))
            print("message       {}".format(error.get("message")))

            if error.get("details"):
                print("details       {}".format(
                    json.dumps(error["details"])[:300]
                ))

        if settings:
            print()
            print("SETTINGS")
            print()
            print_settings_block(settings)

        print()
        print("TEST ONLY - NOTHING SAVED")
        print()
        pause()

        return

    # ---- PC / BD science pipeline -----------------------------------
    print()
    print("PC SCIENCE PIPELINE")
    print()

    print_check("White/Dark references", mission.references is not None)

    if mission.references is None:
        print()
        print("FAILED STAGE  BD_REFERENCES")
        print("message       {}".format(mission.science_error))
        print()
        print("The RAW spectrum was acquired and is shown below.")
        print()

        for channel in sample_analysis.CHANNELS:
            print("  {}  {}".format(channel, number(raw.get(channel))))

        print()
        print("TEST ONLY - NOTHING SAVED")
        print()
        pause()

        return

    try:
        result = mission.analyse_raw(raw, settings)

    except Exception as error:
        print_check("Dark correction", False)
        print()
        print("FAILED STAGE  BD_ANALYSIS")
        print("error code    {}".format(type(error).__name__))
        print("message       {}".format(error))
        print()
        print("TEST ONLY - NOTHING SAVED")
        print()
        pause()

        return

    measurement = result["measurement"]
    matches = result["reference_matches"]

    print_check("Dark correction", True)
    print_check("Normalization", True)
    print_check("Material database", mission.database is not None)
    print_check("Database comparison", result["analysis_status"] == "OK")

    print()
    print("SETTINGS")
    print()
    print_settings_block(settings)

    print()
    print("SPECTRUM")
    print()
    print_spectrum_table(measurement)

    print()
    print("DATABASE COMPARISON")
    print()
    print_matches(matches)

    print()
    print("RESULT")
    print()

    if result["analysis_status"] != "OK":
        print("Database comparison FAILED: {}".format(
            result["analysis_error"]
        ))
        print()

    print_result_block(result["analysis"])

    print()
    print("TEST ONLY - NOTHING SAVED")
    print()
    pause()


# ======================================================================
# screens: carousel
# ======================================================================

def menu_initial_calibration(mission):
    """
    Establish the carousel origin.

    The goal is purely physical: get Slot 1 under the soil loading hole,
    then declare it. There is no encoder, so this is the operator
    asserting a fact the firmware cannot measure.
    """
    while True:
        banner("INITIAL CAROUSEL CALIBRATION")

        print("Goal:")
        print("Align physical Slot 1 exactly under the soil loading hole.")
        print()
        print("[1] Move one whole slot clockwise")
        print("[2] Move one whole slot counter-clockwise")
        print("[3] Fine alignment by degrees (+ = clockwise)")
        print("[4] STOP servo")
        print()
        print("[5] SET CURRENT POSITION AS SLOT 1")
        print()
        print("[c] Cancel")

        selection = choose()

        try:
            if selection == "1":
                mission.link.move_slots("cw", 1)
                print("Moved one slot clockwise.")

            elif selection == "2":
                mission.link.move_slots("ccw", 1)
                print("Moved one slot counter-clockwise.")

            elif selection == "3":
                menu_fine_adjust(mission)

            elif selection == "4":
                mission.link.servo_stop()
                print("Servo stopped.")

            elif selection == "5":
                data = mission.link.sync_load_slot(1)
                carousel = data.get("carousel") or {}

                print()
                print("Calibration complete.")
                print("Slot {} = LOADING position".format(
                    carousel.get("current_load_slot")
                ))
                print("Slot {} = SCANNER position".format(
                    carousel.get("current_scan_slot")
                ))
                print()

                return True

            elif selection == "c":
                print("Cancelled; carousel position unchanged.")

                return False

            elif selection:
                print("Unknown option.")

        except (LinkError, TimeoutError) as error:
            report_failure(error)


def menu_fine_adjust(mission):
    """Small mechanical correction. Does not change slot numbering."""
    banner("FINE CAROUSEL ALIGNMENT")

    print("Positive = clockwise, negative = counter-clockwise.")
    print()

    degrees = ask_float("Degrees", -15.0, 15.0)

    if degrees is None:
        print("Cancelled; carousel not moved.")

        return

    try:
        data = mission.link.fine_adjust(degrees)

    except (LinkError, TimeoutError) as error:
        report_failure(error)

        return

    adjustment = data.get("adjustment") or {}

    print()
    print("Requested: {:.2f} deg {}".format(
        abs(degrees), adjustment.get("direction")
    ))
    print("Runtime:   {} ms".format(adjustment.get("duration_ms")))

    if adjustment.get("moved") is False:
        print("The request was too small to produce any servo runtime; "
              "nothing moved.")

    elif adjustment.get("reliable") is False:
        print("Warning: the runtime is shorter than one PWM frame, so the "
              "servo may not have responded.")


def menu_resync(mission):
    """Re-declare the origin after a reboot or a lost position."""
    banner("RE-SYNC CAROUSEL")

    print("Align physical Slot 1 with the soil loading hole, then confirm.")
    print()

    if not confirm("Is Slot 1 now under the loading hole?"):
        print("Cancelled; position tracking unchanged.")

        return

    data = mission.link.sync_load_slot(1)
    carousel = data.get("carousel") or {}

    print()
    print("Synchronized. Loader = Slot {}, scanner = Slot {}.".format(
        carousel.get("current_load_slot"),
        carousel.get("current_scan_slot"),
    ))


def menu_servo_test(mission):
    """Movement and calibration checks. Deliberately small."""
    while True:
        banner("SERVO / CAROUSEL TEST")

        print("[1] One slot clockwise")
        print("[2] One slot counter-clockwise")
        print("[3] Fine adjustment by degrees")
        print("[4] STOP servo")
        print()
        print("[0] Back")

        selection = choose()

        try:
            if selection == "1":
                mission.link.move_slots("cw", 1)
                print("Moved one slot clockwise.")

            elif selection == "2":
                mission.link.move_slots("ccw", 1)
                print("Moved one slot counter-clockwise.")

            elif selection == "3":
                menu_fine_adjust(mission)

            elif selection == "4":
                mission.link.servo_stop()
                print("Servo stopped.")

            elif selection == "0":
                return

            elif selection:
                print("Unknown option.")

        except (LinkError, TimeoutError) as error:
            report_failure(error)


# ======================================================================
# screens: the sample workflow
# ======================================================================

def menu_choose_slot(mission, status, view):
    """Bring a physical slot to the soil loading position."""
    carousel = status.get("carousel") or {}
    current = carousel.get("selected_slot")

    banner("CHOOSE SAMPLE / SLOT")

    for entry in view:
        marker = " <- current" if entry["slot_id"] == current else ""

        print("  {}  {:<10} {}{}".format(
            entry["slot_id"],
            entry["sample_id"] or "----",
            entry["state"],
            marker,
        ))

    print()

    suggested = (current % SLOT_COUNT) + 1 if current else 1
    target = ask_int("Choose slot [1-8]", 1, SLOT_COUNT, default=suggested)

    if target is None:
        print("Cancelled; carousel not moved.")

        return

    entry = mission.entry_for(view, target)

    print()
    print("Moving Slot {} to the loading position...".format(target))
    sys.stdout.flush()

    data = mission.link.select_slot(target, entry.get("sample_id"))
    move = data.get("move") or {}

    if move.get("restored_load_orientation"):
        print("Restored the loading orientation with the calibrated "
              "half turn first.")

    print("Slot {} is now at the {} position.".format(
        target, (data.get("carousel") or {}).get("carousel_phase")
    ))


def ask_metadata():
    """Every field optional. A skipped field stays null, never invented."""
    print()
    print("Metadata - press Enter to skip any field.")
    print()

    metadata = {}

    for key, label in METADATA_FIELDS:
        value = ask("  {} [optional]".format(label))

        if value:
            metadata[key] = value

    return metadata or None


def menu_prepare(mission, status, view):
    """Create the persistent Sample record and bring its slot to the loader."""
    carousel = status.get("carousel") or {}
    slot_id = carousel.get("selected_slot")

    if slot_id is None:
        print("No slot is selected. Choose a slot first.")

        return

    entry = mission.entry_for(view, slot_id)

    if entry["state"] != STATE_EMPTY:
        print("Slot {} already holds sample {} ({}). Clear the physical "
              "slot before preparing a new one.".format(
                  slot_id, entry["sample_id"], entry["state"]
              ))

        return

    banner("PREPARE SAMPLE")

    print("Physical slot: {}".format(slot_id))
    print()

    raw_id = ask("Sample ID (blank = cancel)")

    if not raw_id:
        print("Cancelled; no sample was created.")

        return

    try:
        sample_id = validate_sample_id(raw_id)

    except StorageError as error:
        print(error.message)

        return

    if mission.store.get_state(sample_id) == STATE_MEASURED:
        print("Sample ID {} already exists as a MEASURED record. Choose "
              "another ID so the earlier result is not overwritten.".format(
                  sample_id
              ))

        return

    metadata = ask_metadata()

    if carousel.get("carousel_phase") != "LOAD":
        print()
        print("Bringing Slot {} to the loading position...".format(slot_id))

    # Also attaches the Sample ID to the slot so the ESP32 can echo it
    # back for correlation.
    mission.link.select_slot(slot_id, sample_id)

    record = mission.store.create(
        sample_id, slot_id, utc_timestamp(), metadata
    )

    print()
    print("Sample {} created in slot {}.".format(sample_id, slot_id))
    print("State: {}".format(record["state"]))
    print()
    print("The rover arm can now deposit soil. Confirm it afterwards with "
          "[3].")


def menu_confirm(mission, status, view):
    """Record that the arm has physically deposited soil."""
    carousel = status.get("carousel") or {}
    slot_id = carousel.get("selected_slot")
    entry = mission.entry_for(view, slot_id) if slot_id else None

    if not entry or entry["state"] == STATE_EMPTY:
        print("No sample is prepared in the selected slot.")

        return

    if entry["state"] != STATE_READY_TO_LOAD:
        print("Slot {} is {}, not {}.".format(
            slot_id, entry["state"], STATE_READY_TO_LOAD
        ))

        return

    banner("CONFIRM SAMPLE LOADED")

    print("Slot {} / sample {}".format(slot_id, entry["sample_id"]))
    print()

    if not confirm("Has the soil been deposited in this slot?"):
        print("Not confirmed; the sample stays {}.".format(
            STATE_READY_TO_LOAD
        ))

        return

    record = mission.store.set_state(
        entry["sample_id"], STATE_LOADED, "loaded_at", utc_timestamp()
    )

    print()
    print("Sample {} is now {}.".format(record["sample_id"], record["state"]))


def menu_measure(mission, status, view):
    """
    The full measurement: ESP32 acquires RAW, BD analyses it, PC saves it.

    The Sample record opened by Prepare is COMPLETED here. No second
    Sample ID is ever created.
    """
    carousel = status.get("carousel") or {}
    slot_id = carousel.get("selected_slot")
    entry = mission.entry_for(view, slot_id) if slot_id else None

    # ---- everything that can be refused before the mechanism moves --
    if not entry or entry["state"] == STATE_EMPTY:
        print("No sample is prepared in the selected slot.")

        return

    if entry["state"] == STATE_READY_TO_LOAD:
        print("Slot {} does not contain confirmed soil yet. Use [3] "
              "Confirm Sample Loaded first.".format(slot_id))

        return

    if entry["state"] == STATE_MEASURED:
        print("Sample {} has already been measured. Clear the physical "
              "slot to reuse it.".format(entry["sample_id"]))

        return

    if not carousel.get("position_valid"):
        print("The carousel is not synchronized. Use Tools -> Re-sync "
              "Carousel first.")

        return

    if carousel.get("carousel_phase") != "LOAD":
        print("Slot {} is not at the loading position (phase {}). Choose "
              "the slot again to bring it back.".format(
                  slot_id, carousel.get("carousel_phase")
              ))

        return

    if mission.references is None:
        print("BD references are not loaded: {}".format(mission.science_error))
        print("Measuring now would acquire a spectrum that cannot be "
              "normalized. Fix firmware/BD first.")

        return

    sample_id = entry["sample_id"]

    banner("MEASURE SAMPLE")

    print("Slot {} / sample {}".format(slot_id, sample_id))
    print()
    print("The carousel will swing 180 deg to the scanner and acquire one "
          "18-channel spectrum. This takes a few seconds.")
    print()
    sys.stdout.flush()

    # ---- ESP32: move and acquire RAW --------------------------------
    try:
        data = mission.link.measure_raw(slot_id, sample_id)

    except (LinkError, TimeoutError) as error:
        print()
        print("Measurement failed before any spectrum was obtained.")

        if isinstance(error, LinkError):
            report_link_error(error)
        else:
            print("Timeout: {}".format(error))

        print()
        print("Sample {} remains {}. Nothing was saved.".format(
            sample_id, STATE_LOADED
        ))
        print()
        pause()

        return

    raw = data.get("raw") or {}
    settings = data.get("sensor_settings")
    measured_at = utc_timestamp()

    print("RAW spectrum received: {}/18 channels.".format(len(raw)))

    # ---- BD: analyse -------------------------------------------------
    analysis_error = None

    try:
        result = mission.analyse_raw(raw, settings)

    except Exception as error:
        # Acquired science must survive downstream software failure.
        analysis_error = "{}: {}".format(type(error).__name__, error)
        result = {
            "measurement": {
                "wavelengths": sample_analysis.channel_wavelengths(),
                "raw": raw,
                "dark_corrected": None,
                "normalized": None,
                "sensor_settings": settings,
            },
            "calibration": None,
            "database": None,
            "reference_matches": [],
            "analysis": None,
            "analysis_status": "FAILED",
            "analysis_error": analysis_error,
        }

        print()
        print("!! ANALYSIS FAILED: {}".format(analysis_error))
        print("   The RAW spectrum is intact and will still be saved.")

    # ---- PC: complete the SAME record --------------------------------
    record = mission.store.get_sample(sample_id) or {"sample_id": sample_id}

    timestamps = record.get("timestamps") or {}
    timestamps["measured_at"] = measured_at

    record.update({
        "sample_id": sample_id,
        "slot_id": slot_id,
        "state": STATE_MEASURED,
        "timestamps": timestamps,
        "measurement": result["measurement"],
        "calibration": result["calibration"],
        "database": result["database"],
        "reference_matches": result["reference_matches"],
        "analysis": result["analysis"],
        "analysis_status": result["analysis_status"],
        "analysis_error": result["analysis_error"],
        "hardware": {
            "carousel": data.get("carousel"),
            "data_ready_wait_ms": data.get("data_ready_wait_ms"),
            "zero_channels": data.get("zero_channels"),
        },
    })

    try:
        mission.store.save(record)
        saved = True

    except StorageError as error:
        saved = False

        print()
        print("!! COULD NOT SAVE: {}".format(error.message))
        print("   The measurement below was NOT written to disk.")

    # ---- report ------------------------------------------------------
    banner("MEASUREMENT COMPLETE")

    print("Sample ID:      {}".format(sample_id))
    print("Physical slot:  {}".format(slot_id))
    print("State:          {} -> {}".format(STATE_LOADED, STATE_MEASURED))
    print("Saved:          {}".format("YES" if saved else "NO"))

    if data.get("zero_channels"):
        print("Zero channels:  {}".format(
            ",".join(data["zero_channels"])
        ))

    print()
    print("SPECTRUM")
    print()
    print_spectrum_table(result["measurement"])

    if result["analysis_status"] == "OK":
        print()
        print("DATABASE COMPARISON")
        print()
        print_matches(result["reference_matches"], limit=5)

        print()
        print("RESULT")
        print()
        print_result_block(result["analysis"])

    else:
        print()
        print("Analysis status: FAILED - {}".format(result["analysis_error"]))
        print("The RAW spectrum is stored and can be re-analysed offline.")

    print()
    print("The sample is now at the SCANNER. Choosing the next slot "
          "restores the loading orientation automatically.")
    print()
    pause()


def menu_clear_slot(mission, status, view):
    """
    Free a physical slot.

    This frees the mechanism only. The saved scientific record stays in
    the PC archive; Delete Sample is what removes that.
    """
    banner("CLEAR PHYSICAL SLOT")

    for entry in view:
        print("  {}  {:<10} {}".format(
            entry["slot_id"], entry["sample_id"] or "----", entry["state"]
        ))

    print()

    slot_id = ask_int("Clear which slot [1-8]", 1, SLOT_COUNT)

    if slot_id is None:
        print("Cancelled.")

        return

    entry = mission.entry_for(view, slot_id)

    if entry["state"] == STATE_EMPTY:
        print("Slot {} is already empty.".format(slot_id))

        return

    print()
    print("Slot {} holds sample {} ({}).".format(
        slot_id, entry["sample_id"], entry["state"]
    ))
    print("The saved scientific record will be KEPT.")
    print()

    if not confirm("Has the soil been physically removed?"):
        print("Cancelled; nothing changed.")

        return

    mission.link.clear_slot(slot_id)

    try:
        mission.store.set_state(entry["sample_id"], STATE_EMPTY)

    except StorageError as error:
        print("Slot freed, but the record could not be updated: {}".format(
            error.message
        ))

        return

    print()
    print("Slot {} is free. Sample {} remains in the archive.".format(
        slot_id, entry["sample_id"]
    ))


# ======================================================================
# screens: sample database
# ======================================================================

def print_sample_table(store):
    summaries = store.summaries()

    if not summaries:
        print("No samples have been saved yet.")

        return summaries

    print("{:<4} {:<12} {:<6} {:<14} {:<22} {:>10}".format(
        "#", "Sample", "Slot", "State", "Best match", "Similarity"
    ))

    for index, entry in enumerate(summaries, start=1):
        print("{:<4} {:<12} {:<6} {:<14} {:<22} {:>10}".format(
            index,
            str(entry.get("sample_id"))[:12],
            str(entry.get("slot_id") or "-"),
            str(entry.get("state"))[:14],
            str(entry.get("best_match") or "-")[:22],
            score(entry.get("best_similarity")),
        ))

    return summaries


def print_full_sample(record):
    banner("SAMPLE {}".format(record.get("sample_id")))

    timestamps = record.get("timestamps") or {}
    metadata = record.get("metadata") or {}

    print("Slot:        {}".format(record.get("slot_id")))
    print("State:       {}".format(record.get("state")))
    print("Created:     {}".format(timestamps.get("created_at")))
    print("Loaded:      {}".format(timestamps.get("loaded_at")))
    print("Measured:    {}".format(timestamps.get("measured_at")))

    print()
    print("METADATA")

    for key, label in METADATA_FIELDS:
        print("  {:<18}{}".format(label + ":", metadata.get(key) or "-"))

    measurement = record.get("measurement")

    if not measurement:
        print()
        print("No spectrum has been acquired for this sample yet.")
        print()
        pause()

        return

    calibration = record.get("calibration") or {}

    print()
    print("CALIBRATION")
    print("  {}".format(calibration.get("calibration_id", "-")))
    print("  {}".format(calibration.get("equation", "-")))

    print()
    print("SETTINGS")
    print()
    print_settings_block(measurement.get("sensor_settings"))

    print()
    print("SPECTRUM")
    print()
    print_spectrum_table(measurement)

    matches = record.get("reference_matches") or []

    print()
    print("DATABASE COMPARISON ({} materials)".format(len(matches)))
    print()
    print_matches(matches)

    analysis = record.get("analysis")

    print()
    print("RESULT")
    print()

    if analysis:
        print_result_block(analysis)
    else:
        print("Analysis status: {} - {}".format(
            record.get("analysis_status"), record.get("analysis_error")
        ))

    print()
    pause()


def pick_sample(summaries, prompt="Sample number"):
    index = ask_int(prompt, 1, len(summaries))

    if index is None:
        return None

    return summaries[index - 1].get("sample_id")


def menu_sample_database(mission):
    """
    View and manage saved samples.

    Measurement fields are read-only here: raw, dark_corrected,
    normalized, sensor settings, matches, analysis and calibration are
    scientific results, not editable text. Metadata may be corrected.
    """
    store = mission.store

    while True:
        banner("SAMPLE DATABASE")

        summaries = print_sample_table(store)

        print()
        print("[1] Open sample")
        print("[2] Edit metadata")
        print("[3] Rename sample")
        print("[4] Delete sample")
        print("[5] Refresh")
        print()
        print("[0] Back")

        selection = choose()

        try:
            if selection == "0":
                return

            if selection == "5":
                store.load()

                continue

            if not summaries:
                if selection:
                    print("There are no samples yet.")

                continue

            if selection == "1":
                sample_id = pick_sample(summaries)

                if sample_id:
                    record = store.get_sample(sample_id)

                    if record is None:
                        print("Record file for {} is missing.".format(
                            sample_id
                        ))
                    else:
                        print_full_sample(record)

            elif selection == "2":
                sample_id = pick_sample(summaries)

                if sample_id:
                    metadata = ask_metadata()

                    if metadata:
                        store.update_metadata(sample_id, metadata)
                        print("Metadata updated.")
                    else:
                        print("Nothing entered; metadata unchanged.")

            elif selection == "3":
                sample_id = pick_sample(summaries)

                if sample_id:
                    new_id = ask("New Sample ID (blank = cancel)")

                    if new_id:
                        store.rename(sample_id, new_id)
                        print("Renamed {} -> {}.".format(sample_id, new_id))

            elif selection == "4":
                sample_id = pick_sample(summaries)

                if sample_id:
                    print()
                    print("This permanently deletes the scientific record "
                          "for {}.".format(sample_id))
                    print("Clearing a physical slot is a different thing "
                          "and keeps the record.")
                    print()

                    typed = ask(
                        "Type the Sample ID to confirm deletion"
                    )

                    if typed == sample_id:
                        store.delete(sample_id)
                        print("Sample {} deleted.".format(sample_id))
                    else:
                        print("Not deleted.")

            elif selection:
                print("Unknown option.")

        except StorageError as error:
            print()
            print("Storage error: {} ({})".format(error.message, error.code))


# ======================================================================
# tools menu
# ======================================================================

TOOLS_MENU = (
    ("1", "Sample Database", "View and manage saved Samples.",
     lambda mission, status, view: menu_sample_database(mission)),
    ("2", "System Status", "Show PC, database and hardware state.",
     lambda mission, status, view: (print_system_status(mission), pause())),
    ("3", "Re-sync Carousel", "Restore physical position tracking.",
     lambda mission, status, view: menu_resync(mission)),
    ("4", "Servo / Carousel Test", "Test movement and calibration.",
     lambda mission, status, view: menu_servo_test(mission)),
    ("5", "Sensor Test", "Test ESP32 sensor + PC analysis pipeline.",
     lambda mission, status, view: menu_sensor_test(mission)),
    ("6", "Clear Physical Slot", "Free a physical carousel slot.",
     menu_clear_slot),
)


def menu_tools(mission, status, view):
    while True:
        banner("TOOLS / RECORDS")

        for key, label, description, _handler in TOOLS_MENU:
            print("[{}] {}".format(key, label))
            print("    {}".format(description))
            print()

        print("[0] Back")

        selection = choose()

        if selection == "0":
            return

        handler = None

        for key, _label, _description, action in TOOLS_MENU:
            if selection == key:
                handler = action

                break

        if handler is None:
            if selection:
                print("Unknown option.")

            continue

        try:
            handler(mission, status, view)

            # Slot and carousel state may have changed underneath us.
            status = mission.hardware_status()
            view = mission.slot_view(status)

        except LinkError as error:
            report_link_error(error)

        except TimeoutError as error:
            print()
            print("Timeout: {}".format(error))

        except KeyboardInterrupt:
            print()
            print("Cancelled.")


# ======================================================================
# help
# ======================================================================

HELP_TEXT = """NORMAL COMPETITION WORKFLOW

  0. Initial Carousel Calibration - once, Slot 1 under the loading hole
  1. Choose Sample / Slot
  2. Prepare Sample          (creates the persistent record)
  3. Rover arm deposits soil
  4. Confirm Sample Loaded
  5. Measure Sample          (180 deg, RAW, analysis, saved)

CALIBRATION

  The module uses one fixed Dark and one fixed White reference stored
  in firmware/BD/references.json. They were accepted before the
  competition and are used for every sample. You are never asked to
  measure White or Dark.

      C = Sample - Dark
      R = (Sample - Dark) / (White - Dark)

WHERE THINGS LIVE

  ESP32   carousel + AS7265x, RAW spectra only
  BD      White/Dark, material database, all analysis
  PC      workflow, Sample records, this interface

  Saved samples: firmware/PC/data/

MEASURED IS NOT EMPTY

  The soil physically stays in the slot after a measurement. Use
  Tools -> Clear Physical Slot when it has been removed. That keeps the
  scientific record; Delete Sample is what removes it."""


def menu_help(mission, status, view):
    banner("HELP")

    print(HELP_TEXT)
    print()
    pause()


# ======================================================================
# main screen
# ======================================================================

def action_labels(entry, carousel):
    """What the operator can actually do right now."""
    state = entry.get("state", STATE_EMPTY)
    phase = carousel.get("carousel_phase")

    labels = {"2": "", "3": "", "4": ""}

    if state == STATE_EMPTY:
        labels["2"] = "[AVAILABLE]"
        labels["3"] = "[LOCKED - no sample prepared]"
        labels["4"] = "[LOCKED - no sample prepared]"

    elif state == STATE_READY_TO_LOAD:
        labels["2"] = "[DONE]"
        labels["3"] = "[AVAILABLE]"
        labels["4"] = "[LOCKED - sample not confirmed]"

    elif state == STATE_LOADED:
        labels["2"] = "[DONE]"
        labels["3"] = "[DONE]"
        labels["4"] = (
            "[AVAILABLE]" if phase == "LOAD"
            else "[LOCKED - sample not at loading hole]"
        )

    elif state == STATE_MEASURED:
        labels["2"] = "[DONE]"
        labels["3"] = "[DONE]"
        labels["4"] = "[DONE - MEASURED]"

    return labels


def print_main_screen(mission, status, view):
    carousel = status.get("carousel") or {}
    selected = carousel.get("selected_slot")
    entry = mission.entry_for(view, selected) if selected else {}

    banner("FREYA SCIENCE MODULE")

    print("Selected: Slot {} / {}".format(
        selected, entry.get("sample_id") or "----"
    ))
    print("State:    {}".format(entry.get("state", "-")))
    print("Position: {}".format(carousel.get("carousel_phase", "?")))
    print()
    print("Loader: Slot {}    Scanner: Slot {}".format(
        carousel.get("current_load_slot"),
        carousel.get("current_scan_slot"),
    ))
    print()

    for item in view:
        print("{}  {:<8} {}".format(
            item["slot_id"], item["sample_id"] or "----", item["state"]
        ))

    sensor = status.get("sensor") or {}

    if not sensor.get("ready"):
        print()
        print("Sensor: UNAVAILABLE - it will be retried automatically on "
              "the next measurement or sensor test.")

    if mission.science_error:
        print()
        print("BD: {}".format(mission.science_error))

    labels = action_labels(entry, carousel)

    print()
    print("[1] Choose Sample / Slot")
    print("[2] Prepare Sample {}".format(labels["2"]))
    print("[3] Confirm Sample Loaded {}".format(labels["3"]))
    print("[4] Measure Sample {}".format(labels["4"]))
    print("[5] Fine Carousel Alignment")
    print()
    print("[t] Tools / Records")
    print("[h] Help")
    print("[q] Exit")


def print_startup_screen():
    banner("FREYA SCIENCE MODULE")

    print("Carousel: NOT CALIBRATED")
    print()
    print("Before working with samples, physical Slot 1 must be aligned")
    print("with the soil loading hole.")
    print()
    print("[0] Initial Carousel Calibration")
    print("[t] Tools / Records")
    print("[h] Help")
    print("[q] Exit")


MAIN_ACTIONS = {
    "1": menu_choose_slot,
    "2": menu_prepare,
    "3": menu_confirm,
    "4": menu_measure,
    "5": lambda mission, status, view: menu_fine_adjust(mission),
    "t": menu_tools,
    "h": menu_help,
}


def interactive(link):
    """Calibrate the carousel once, then run the sample loop."""
    print("Connecting to the science module on {}...".format(link.port))

    try:
        link.wait_online()

    except (LinkError, TimeoutError) as error:
        print("No answer from the science module: {}".format(error))
        print()
        print("Check that the USB cable is connected, that the port is "
              "correct, and that no other program (REPL, mpremote, serial "
              "monitor) is holding the port open.")

        return 1

    print("Connection: ONLINE")

    mission = Mission(link)

    if mission.science_error:
        print("BD warning: {}".format(mission.science_error))

    while True:
        try:
            status = mission.hardware_status()

        except (LinkError, TimeoutError) as error:
            print()
            print("Could not read the hardware state: {}".format(error))

            if choose("Retry? [Y/n]").startswith("n"):
                return 1

            continue

        view = mission.slot_view(status)
        carousel = status.get("carousel") or {}

        if not carousel.get("position_valid"):
            print_startup_screen()

            selection = choose()

            if selection == "q":
                return 0

            try:
                if selection == "0":
                    menu_initial_calibration(mission)

                elif selection == "t":
                    menu_tools(mission, status, view)

                elif selection == "h":
                    menu_help(mission, status, view)

                elif selection:
                    print("Unknown option.")

            except LinkError as error:
                report_link_error(error)

            except TimeoutError as error:
                print("Timeout: {}".format(error))

            except KeyboardInterrupt:
                print()
                print("Cancelled.")

            continue

        print_main_screen(mission, status, view)

        selection = choose()

        if selection == "q":
            return 0

        handler = MAIN_ACTIONS.get(selection)

        if handler is None:
            if selection:
                print("Unknown option.")

            continue

        try:
            handler(mission, status, view)

        except LinkError as error:
            report_link_error(error)

        except TimeoutError as error:
            print()
            print("Timeout: {}".format(error))

        except StorageError as error:
            print()
            print("Storage error: {} ({})".format(error.message, error.code))

        except KeyboardInterrupt:
            print()
            print("Cancelled.")


# ======================================================================
# entry point
# ======================================================================

def one_shot(link, command, payload_text):
    payload = json.loads(payload_text) if payload_text else {}

    if not isinstance(payload, dict):
        print("--payload must be a JSON object.", file=sys.stderr)

        return 2

    link.wait_online()

    print(json.dumps(
        link.request(command, timeout=MEASUREMENT_TIMEOUT, **payload),
        indent=2,
    ))

    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="Main-PC application for the Freya AS7265x science "
                    "module."
    )

    parser.add_argument(
        "--port", required=True,
        help="Serial port of the ESP32, e.g. COM4 or /dev/ttyUSB0.",
    )
    parser.add_argument(
        "--baud", type=int, default=DEFAULT_BAUDRATE,
        help="Baud rate (default: {}).".format(DEFAULT_BAUDRATE),
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT,
        help="Response timeout for ordinary commands (default: "
             "{:.0f} s).".format(DEFAULT_TIMEOUT),
    )
    parser.add_argument(
        "--connect-timeout", type=float, default=CONNECT_TIMEOUT,
        help="Seconds to wait for the module to answer a ping after the "
             "port is opened (default: {:.0f}).".format(CONNECT_TIMEOUT),
    )
    parser.add_argument(
        "--command",
        help="Run a single hardware command and print the JSON result "
             "instead of opening the menu.",
    )
    parser.add_argument(
        "--payload",
        help="JSON object with extra fields for --command, e.g. "
             "'{\"slot\": 1}'.",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Echo the raw protocol traffic.",
    )

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    try:
        link = ESP32Link(
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
        link.open()

    except Exception as error:  # serial.SerialException and friends
        print("Could not open {}: {}".format(args.port, error),
              file=sys.stderr)
        print("If the port is busy, close any REPL, mpremote session or "
              "serial monitor holding it.", file=sys.stderr)

        return 2

    try:
        if args.command:
            return one_shot(link, args.command, args.payload)

        return interactive(link)

    except LinkError as error:
        print("Module refused the command: {}".format(error), file=sys.stderr)

        return 1

    except TimeoutError as error:
        print("Timeout: {}".format(error), file=sys.stderr)

        return 1

    except KeyboardInterrupt:
        print()

        return 0

    finally:
        link.close()


if __name__ == "__main__":
    sys.exit(main())
