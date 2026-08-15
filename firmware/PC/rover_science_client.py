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

import aggregation                                      # noqa: E402
import config as bd_config                              # noqa: E402
import sample_analysis                                  # noqa: E402
from calibration import (                               # noqa: E402
    CalibrationError,
    CalibrationStore,
    ILLUMINATIONS,
    build_calibration,
    validate_calibration,
)
from database import (                                  # noqa: E402
    DatabaseError,
    MaterialDatabase,
    References,
)

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

# Four physical slots, 90 degrees apart; the scanner sits two slots
# (180 degrees) from the loader.
SLOT_COUNT = 4
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


def report_return_move(return_move):
    """
    Report the 180 deg return as its own outcome.

    Acquisition and mechanical recovery are separate results: a servo
    that failed to come home must never be reported as a failed
    measurement, and a successful measurement must never imply the
    carousel is where the software thinks it is.
    """
    if not return_move:
        return

    if return_move.get("returned"):
        print("Returning Slot home............ PASS")

        return

    print()
    print("!! RETURN MOVEMENT FAILED")
    print("   {}".format(return_move.get("message", "")))

    if return_move.get("exception_message"):
        print("   {}".format(return_move["exception_message"]))

    print("   Carousel position is now UNKNOWN - re-sync before moving.")


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
        self.calibrations = CalibrationStore()

        self.references = None
        self.database = None
        self.active_calibration = None
        self.science_error = None
        self.calibration_error = None

        self.load_science()

    # ------------------------------------------------------------------
    # BD
    # ------------------------------------------------------------------

    def load_science(self):
        """
        Load the protected reference data and the active calibration.

        Two calibrations, always kept apart:

          LEGACY  references.json - what database.json was normalized
                  against, and the only thing it may be compared with.
                  Immutable.

          ACTIVE  the full Dark + WHITE/UV/IR calibration the operator
                  made. Used for the scientific record and for quality
                  control. May legitimately be absent on a fresh
                  install; the legacy comparison still works without it.
        """
        self.references = None
        self.database = None
        self.active_calibration = None
        self.science_error = None
        self.calibration_error = None

        try:
            self.references = References()
            self.database = MaterialDatabase()

        except DatabaseError as error:
            self.science_error = "{}: {}".format(error.code, error.message)

        try:
            self.active_calibration = self.calibrations.active()

            if self.active_calibration is None:
                self.calibration_error = (
                    "No full spectral calibration has been made yet."
                )

        except CalibrationError as error:
            self.calibration_error = "{}: {}".format(
                error.code, error.message
            )

        return self.science_error is None

    def calibration_health(self):
        """PASS / MISSING / INVALID for each of the two calibrations."""
        if self.references is None:
            legacy = "MISSING"
        elif self.references.white_missing or self.references.dark_missing:
            legacy = "INVALID"
        else:
            legacy = "PASS"

        if self.active_calibration is None:
            active = "MISSING"
        else:
            result = self.active_calibration.validate()
            active = "PASS" if result["status"] != "FAIL" else "INVALID"

        return {
            "legacy": legacy,
            "active": active,
            "legacy_id": (
                self.references.calibration_id if self.references else None
            ),
            "active_id": (
                self.active_calibration.calibration_id
                if self.active_calibration else None
            ),
        }

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

    def analyse_raw(self, acquisition, sensor_settings=None,
                    distance_mm=None):
        """
        Acquisition in, complete BD result out.

        Accepts the WHITE/UV/IR protocol and, for an archived record
        from before this release, a bare 18-channel white spectrum.

        Raises AnalysisError or DatabaseError; the caller is responsible
        for preserving the acquired spectra either way.
        """
        if self.references is None:
            raise DatabaseError(
                "REFERENCES_NOT_LOADED",
                self.science_error or "White/Dark references are not loaded.",
            )

        return sample_analysis.analyze(
            acquisition,
            self.references,
            self.database,
            sensor_settings,
            self.active_calibration,
            distance_mm,
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
    print("Sample archive:   {}".format(store.archive_path))

    if store.migrated_from:
        print("  migrated from:  {}".format(store.migrated_from))

    if store.error:
        print("Storage error:    {}".format(store.error))

    print()
    print("BD")

    health = mission.calibration_health()

    if mission.references is not None:
        refs = mission.references.status()

        print("Legacy cal:       {} ({}/{} white + {}/{} dark)".format(
            health["legacy"],
            refs["white_channels"], refs["channels_required"],
            refs["dark_channels"], refs["channels_required"],
        ))
        print("  id:             {}".format(refs["calibration_id"]))
        print("  protected:      YES - database.json depends on it")

        if refs["zero_denominator_channels"]:
            print("  warning: White == Dark on {}".format(
                ",".join(refs["zero_denominator_channels"])
            ))

    else:
        print("Legacy cal:       ERROR - {}".format(mission.science_error))

    print("Active cal:       {}".format(health["active"]))

    if mission.active_calibration is not None:
        print("  id:             {}".format(health["active_id"]))
        print("  illuminations:  WHITE + UV + IR")

    else:
        print("  {}".format(
            mission.calibration_error or "not created yet"
        ))
        print("  Run Tools -> Sensor Test -> Full Spectral Calibration.")

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

    # Acquisitions the ESP32 is still holding, so an operator can see at
    # a glance whether a sync would transfer anything.
    retained = [
        slot for slot in (status.get("slots") or [])
        if slot.get("has_measurement")
    ]

    print("Held acquisitions: {}".format(len(retained)))

    print()
    print("CAROUSEL")
    print("Slots:            {} ({:.0f} deg apart)".format(
        SLOT_COUNT, 360.0 / SLOT_COUNT
    ))
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


def print_processing_table(measurement, dark, white):
    """Every step of the calculation, per channel, side by side."""
    raw = measurement.get("raw") or {}
    corrected = measurement.get("dark_corrected") or {}
    normalized = measurement.get("normalized") or {}

    print("CH   {:>10} {:>10} {:>10} {:>12} {:>12}".format(
        "RAW", "DARK", "WHITE", "DARK-CORR", "NORMALIZED"
    ))

    for channel in sample_analysis.CHANNELS:
        print("{:<4} {:>10} {:>10} {:>10} {:>12} {:>12}".format(
            channel,
            number(raw.get(channel)),
            number(dark.get(channel)),
            number(white.get(channel)),
            number(corrected.get(channel)),
            number(normalized.get(channel), 6),
        ))


def print_triad_table(measurement):
    """All three illuminations side by side - the 54 features."""
    raw = measurement.get("raw") or {}
    active = measurement.get("active_normalized") or {}
    wavelengths = measurement.get("wavelengths") or {}

    if not isinstance(raw, dict) or "white" not in raw:
        print_spectrum_table(measurement)

        return

    have_active = bool(active)

    header = "CH   nm    {:>11} {:>11} {:>11}".format(
        "WHITE raw", "UV raw", "IR raw"
    )

    if have_active:
        header += "   {:>9} {:>9} {:>9}".format("R white", "R uv", "R ir")

    print(header)

    for channel in sample_analysis.CHANNELS:
        row = "{:<4} {:<5} {:>11} {:>11} {:>11}".format(
            channel,
            wavelengths.get(channel, "-"),
            number((raw.get("white") or {}).get(channel)),
            number((raw.get("uv") or {}).get(channel)),
            number((raw.get("ir") or {}).get(channel)),
        )

        if have_active:
            row += "   {:>9} {:>9} {:>9}".format(
                number((active.get("white") or {}).get(channel), 4),
                number((active.get("uv") or {}).get(channel), 4),
                number((active.get("ir") or {}).get(channel), 4),
            )

        print(row)


def print_quality(report):
    """Measurement quality, with the reasons spelled out."""
    if not report:
        return

    print("Overall: {}".format(report.get("status")))

    for check in report.get("checks") or []:
        if check["status"] == "PASS":
            continue

        print("  [{}] {}: {}".format(
            check["status"], check["check"], check["message"]
        ))

    invalid = report.get("invalid_channels") or []

    if invalid:
        print("  Channels excluded from comparison: {}".format(
            ",".join(invalid)
        ))


def print_metric_table(matches, limit=None):
    """
    The ranked comparison, showing every metric.

    All three are printed because they disagree in informative ways -
    collapsing them into one number is exactly what made a 97% cosine
    look like a confident identification.
    """
    if not matches:
        print("No reference materials were compared.")

        return

    shown = matches if limit is None else matches[:limit]

    print("{:<3} {:<26} {:>8} {:>9} {:>8} {:>8}".format(
        "#", "Material", "Combined", "Cosine", "RMSE", "Pearson"
    ))

    for match in shown:
        pearson = match.get("pearson_r")
        rmse_value = match.get("rmse")

        print("{:<3} {:<26} {:>8} {:>9} {:>8} {:>8}".format(
            match.get("combined_rank", match.get("rank")),
            str(match.get("material"))[:26],
            "{}/{}/{}".format(
                match.get("cosine_rank"),
                match.get("rmse_rank"),
                match.get("pearson_rank"),
            ),
            score(match.get("cosine_similarity_percent")),
            "{:.4f}".format(rmse_value) if rmse_value is not None else "-",
            "{:+.3f}".format(pearson) if pearson is not None else "-",
        ))

    if limit is not None and len(matches) > limit:
        print("... {} more (all stored with the sample)".format(
            len(matches) - limit
        ))

    print()
    print("Combined column is the cosine/RMSE/Pearson rank triple.")
    print("Cosine is shape only; RMSE keeps magnitude; Pearson is")
    print("correlation. None of them is a probability.")


def print_agreement(agreement):
    if not agreement or agreement.get("agree") is None:
        return

    if agreement.get("agree"):
        print("Metrics agree: all three rank {} at or near the top.".format(
            agreement.get("combined_best")
        ))

        return

    print("METRICS DISAGREE:")
    print("  best by cosine : {}".format(agreement.get("cosine_best")))
    print("  best by RMSE   : {}".format(agreement.get("rmse_best")))
    print("  best by Pearson: {}".format(agreement.get("pearson_best")))


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

    mode = settings.get("measurement_mode")
    mode_name = settings.get("measurement_mode_name")

    print("Mode:               {}{}".format(
        mode, " {}".format(mode_name) if mode_name else ""
    ))
    print("Integration cycles: {}".format(settings.get("integration_cycles")))
    print("Gain:               {}".format(settings.get("gain_x")))

    currents = settings.get("led_currents_ma")

    if currents:
        print("WHITE current:      {}".format(currents.get("white")))
        print("UV current:         {}".format(currents.get("uv")))
        print("IR current:         {}".format(currents.get("ir")))

    else:
        print("LED current:        {}".format(
            settings.get("led_current_ma", settings.get("led_current"))
        ))


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


def print_calibration_health(mission):
    """
    The two calibrations, at the top of every sensor test.

    The operator has to be able to see at a glance which calibration a
    result was produced under - and be told plainly when the active one
    is missing rather than quietly falling back.
    """
    health = mission.calibration_health()

    print("Calibration:")
    print("  Active full calibration: {}{}".format(
        health["active"],
        "  {}".format(health["active_id"]) if health["active_id"] else "",
    ))
    print("  Legacy DB calibration:   {}{}".format(
        health["legacy"],
        "  {}".format(health["legacy_id"]) if health["legacy_id"] else "",
    ))

    if health["active"] != "PASS":
        print()
        print("  No usable full spectral calibration.")
        print("  UV and IR reflectance will not be computed.")
        print("  Run [5] Sensor Test -> [3] Full Spectral Calibration.")

    return health


def menu_sensor_test(mission):
    """The engineering submenu: test, calibrate, inspect."""
    while True:
        banner("SENSOR TEST / CALIBRATION")

        print_calibration_health(mission)

        print()
        print("[1] Full Sensor + Analysis Test")
        print("    Run the complete production measurement pipeline")
        print("    without saving a Sample.")
        print()
        print("[2] LED / Illumination Test")
        print("    Test WHITE, UV and IR illumination independently.")
        print()
        print("[3] Full Spectral Calibration")
        print("    Create a new complete Dark + WHITE/UV/IR calibration.")
        print()
        print("[4] Show Active Calibration")
        print("[5] Validate Active Calibration")
        print("[6] Calibration History")
        print()
        print("[0] Back")

        selection = choose()

        try:
            if selection == "0":
                return

            if selection == "1":
                menu_full_sensor_test(mission)

            elif selection == "2":
                menu_led_test(mission)

            elif selection == "3":
                menu_full_calibration(mission)

            elif selection == "4":
                show_active_calibration(mission)

            elif selection == "5":
                validate_active_calibration(mission)

            elif selection == "6":
                show_calibration_history(mission)

            elif selection:
                print("Unknown option.")

        except (LinkError, TimeoutError) as error:
            report_failure(error)
            pause()

        except CalibrationError as error:
            print()
            print("Calibration error: {} - {}".format(
                error.code, error.message
            ))
            pause()


# ----------------------------------------------------------------------
# [1] full sensor + analysis test
# ----------------------------------------------------------------------

def menu_full_sensor_test(mission):
    """
    The complete production pipeline, saving nothing.

    Deliberately the SAME code path a real measurement uses: the ESP32
    acquires WHITE/UV/IR through acquire_triad, the PC normalizes it
    both ways, quality control runs, and the legacy comparison ranks
    every material. A pass here means a measurement will work.
    """
    banner("SENSOR + ANALYSIS TEST")

    health = print_calibration_health(mission)

    print()
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

    settings = data.get("sensor_settings")
    blocks = data.get("illuminations")

    if not blocks:
        print()
        print("FAILED STAGE  {}".format(data.get("failed_stage")))

        for entry in data.get("checks") or []:
            if entry.get("ok"):
                continue

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

    print()
    print("ACQUISITION")
    print()

    for name in ILLUMINATIONS:
        block = blocks.get(name) or {}
        print("{:<20}{} repeat(s), {}/18 channels".format(
            "{} illumination".format(name.upper()),
            block.get("repeats"),
            len((block.get("acquisitions") or [{}])[0]),
        ))

    print("{:<20}{}".format(
        "All lamps off", "YES" if data.get("bulbs_off") else "NO"
    ))

    if mission.references is None:
        print()
        print("FAILED STAGE  BD_REFERENCES")
        print("message       {}".format(mission.science_error))
        print()
        print("TEST ONLY - NOTHING SAVED")
        print()
        pause()

        return

    try:
        result = mission.analyse_raw(data, settings)

    except Exception as error:
        print()
        print("FAILED STAGE  BD_ANALYSIS")
        print("error code    {}".format(type(error).__name__))
        print("message       {}".format(error))
        print()
        print("TEST ONLY - NOTHING SAVED")
        print()
        pause()

        return

    print()
    print("MEASUREMENT QUALITY")
    print()
    print_quality(result["quality"])

    print()
    print("SETTINGS")
    print()
    print_settings_block(result["measurement"].get("sensor_settings"))

    print()
    print("FULL SPECTRAL DATA")
    print()
    print_triad_table(result["measurement"])

    if health["active"] != "PASS":
        print()
        print("Active (UV/IR) reflectance columns are absent because no")
        print("full spectral calibration is active.")

    print()
    print("LEGACY DATABASE COMPARISON")
    print("  normalized with {}".format(
        result["calibration"]["legacy_database_calibration_id"]
    ))
    print()
    print_metric_table(result["reference_matches"], limit=8)

    print()
    print_agreement(result.get("metric_agreement"))

    print()
    print("RESULT")
    print()
    print_result_block(result["analysis"])

    print()
    print("TEST ONLY - NOTHING SAVED")
    print()
    pause()


# ----------------------------------------------------------------------
# [2] LED / illumination test
# ----------------------------------------------------------------------

def menu_led_test(mission):
    """Each lamp on its own, with the state read back from the register."""
    banner("LED / ILLUMINATION TEST")

    print("Each lamp is switched on alone, held briefly, then switched")
    print("off. The enable bit is read back at every step.")
    print()

    try:
        data = mission.link.led_test()

    except (LinkError, TimeoutError) as error:
        report_failure(error)
        print()
        pause()

        return

    for entry in data.get("lamps") or []:
        name = str(entry.get("illumination", "?")).upper()

        print("{:<12}{}{}".format(
            "{} LED:".format(name),
            "PASS" if entry.get("ok") else "FAIL",
            "   {}".format(entry.get("current_ma"))
            if entry.get("current_ma") else "",
        ))

        if not entry.get("ok"):
            error = entry.get("error") or {}
            print("             stage   {}".format(error.get("stage")))
            print("             code    {}".format(error.get("code")))
            print("             message {}".format(error.get("message")))

    print()
    print("{:<12}{}".format(
        "ALL LEDs OFF:", "PASS" if data.get("all_off") else "FAIL"
    ))

    states = data.get("final_states") or {}

    if states and not data.get("all_off"):
        print("  still on: {}".format(
            ", ".join(name for name, on in states.items() if on) or "unknown"
        ))

    print()
    print("Overall: {}".format("PASS" if data.get("ok") else "FAIL"))
    print()
    pause()


# ----------------------------------------------------------------------
# [3] full spectral calibration
# ----------------------------------------------------------------------

def _acquire_calibration_block(mission, illumination, repeats, label):
    """One calibration block, with per-acquisition progress."""
    print()
    print("{} acquisition, {} repeats...".format(label, repeats))
    sys.stdout.flush()

    block = mission.link.acquire_block(illumination, repeats)

    taken = len(block.get("acquisitions") or [])

    for index in range(1, taken + 1):
        print("  {} {}/{}".format(label, index, taken))

    if not block.get("bulbs_off", True):
        print("  WARNING: a lamp was still on after the block.")

    return aggregation.aggregate_block(block)


def _print_block_summary(title, aggregated):
    statistics = aggregated["statistics"]
    spectrum = aggregated["spectrum"]

    print()
    print(title)
    print()
    print("CH   {:>10} {:>10} {:>10} {:>9}".format(
        "Median", "Mean", "StdDev", "CV"
    ))

    for channel in sample_analysis.CHANNELS:
        summary = statistics["channels"].get(channel) or {}
        cv = summary.get("cv")

        print("{:<4} {:>10} {:>10} {:>10} {:>9}".format(
            channel,
            number(spectrum.get(channel)),
            number(summary.get("mean")),
            number(summary.get("stdev")),
            "{:.3%}".format(cv) if cv is not None else "-",
        ))

    print()
    print("  repeats {}, rejected {}, unstable channels {}".format(
        statistics.get("repeats"),
        statistics.get("rejected_total"),
        len(statistics.get("unstable_channels") or []),
    ))


def menu_full_calibration(mission):
    """
    Guided Dark + WHITE/UV/IR calibration.

    Creates a NEW calibration file. It never touches references.json or
    database.json, and it does not become active until the operator
    confirms after seeing the validation.
    """
    banner("FULL SPECTRAL CALIBRATION")

    health = mission.calibration_health()

    print("Current active calibration:")
    print("  ID:      {}".format(health["active_id"] or "none"))
    print("  Status:  {}".format(health["active"]))

    if mission.active_calibration is not None:
        print("  Created: {}".format(mission.active_calibration.created_at))

    print()
    print("Legacy DB calibration:")
    print("  ID:        {}".format(health["legacy_id"]))
    print("  Protected: YES")
    print()
    print("A new calibration will NOT modify the legacy database")
    print("calibration, references.json or database.json. The existing")
    print("material library stays valid and does not need remeasuring.")
    print()

    if not confirm("Continue?"):
        print("Cancelled.")

        return

    repeats = ask_int(
        "Repeats per block", 2, 25, default=bd_config_repeats()
    )

    if repeats is None:
        print("Cancelled.")

        return

    # -- step 1: dark --------------------------------------------------
    banner("STEP 1/2 - DARK REFERENCE")

    print("Remove the sample, close the optical path, and make sure no")
    print("light reaches the measurement target.")
    print()
    print("All WHITE, UV and IR LEDs will remain OFF. Dark is the")
    print("detector's own response with no illumination at all.")
    print()

    ask("Press Enter when ready")

    try:
        dark = _acquire_calibration_block(
            mission, "dark", repeats, "Dark"
        )

    except (LinkError, TimeoutError) as error:
        print()
        print("DARK_CALIBRATION_FAILED")
        report_failure(error)
        print()
        pause()

        return

    _print_block_summary("DARK REFERENCE", dark)

    dark_unstable = len(
        dark["statistics"].get("unstable_channels") or []
    )
    dark_missing = dark["statistics"].get("missing_channels") or []

    dark_ok = not dark_missing and dark_unstable <= 8

    print()
    print("Dark quality: {}".format("PASS" if dark_ok else "FAIL"))

    if not dark_ok:
        print()

        if dark_missing:
            print("Missing channels: {}".format(",".join(dark_missing)))

        print("The Dark reference is not usable. Calibration stopped")
        print("before the White step - a bad Dark would corrupt every")
        print("reflectance computed against it.")
        print()
        pause()

        return

    # -- step 2: white target ------------------------------------------
    banner("STEP 2/2 - WHITE REFERENCE")

    print("Install the diffuse White reference target in the exact")
    print("sample measurement position.")
    print()
    print("Do not move the sensor. The three illuminations are measured")
    print("one after another against the same target.")
    print()

    ask("Press Enter when ready")

    white_blocks = {}

    for name in ILLUMINATIONS:
        try:
            white_blocks[name] = _acquire_calibration_block(
                mission, name, repeats, "{} illumination".format(name.upper())
            )

        except (LinkError, TimeoutError) as error:
            print()
            print("{}_CALIBRATION_FAILED".format(name.upper()))
            report_failure(error)
            print()
            pause()

            return

    for name in ILLUMINATIONS:
        _print_block_summary(
            "{} WHITE REFERENCE".format(name.upper()), white_blocks[name]
        )

    # -- build and validate --------------------------------------------
    try:
        status = mission.hardware_status()
        settings = (status.get("sensor") or {}).get("settings") or {}

    except (LinkError, TimeoutError):
        settings = {}

    document = build_calibration(
        dark, white_blocks, settings, repeats
    )

    result = validate_calibration(document, settings)
    document["validation"] = result

    banner("CALIBRATION VALIDATION")

    _print_validation(document, result)

    print()
    print("New calibration ID:")
    print("  {}".format(document["calibration_id"]))
    print()

    if result["status"] == "FAIL":
        print("This calibration did NOT pass validation and cannot be")
        print("activated.")
        print()

        if confirm("Keep it on disk as engineering data?"):
            path = mission.calibrations.save(document)
            print("Saved (inactive, marked invalid): {}".format(path))

        else:
            print("Discarded.")

        print()
        pause()

        return

    path = mission.calibrations.save(document)
    print("Saved: {}".format(path))
    print()

    if not confirm("Activate this calibration?"):
        print("Saved but NOT activated. The previous calibration is still")
        print("in force.")
        print()
        pause()

        return

    mission.calibrations.activate(document["calibration_id"])
    mission.load_science()

    print()
    print("Active calibration is now {}.".format(
        document["calibration_id"]
    ))
    print("The legacy database calibration is unchanged.")
    print()
    pause()


def bd_config_repeats():
    return getattr(bd_config, "CALIBRATION_REPEATS", 10)


def _print_validation(document, result):
    dark_statistics = (document.get("dark") or {}).get("statistics") or {}
    references = document.get("white_reference") or {}

    print("Dark:")
    print("  {}/18 channels valid".format(
        18 - len(dark_statistics.get("missing_channels") or [])
    ))
    print("  repeatability: {}".format(
        "PASS" if len(dark_statistics.get("unstable_channels") or []) <= 3
        else "WARNING"
    ))

    for name in ILLUMINATIONS:
        statistics = (references.get(name) or {}).get("statistics") or {}

        print()
        print("{} illumination:".format(name.upper()))
        print("  {}/18 channels".format(
            18 - len(statistics.get("missing_channels") or [])
        ))
        print("  repeatability: {}".format(
            "PASS" if len(statistics.get("unstable_channels") or []) <= 3
            else "WARNING"
        ))

    settings = document.get("sensor_settings") or {}

    print()
    print("Sensor settings:")
    print("  Mode:        {} [{}]".format(
        settings.get("measurement_mode"),
        "PASS" if settings.get("measurement_mode") == 3 else "FAIL",
    ))
    print("  Gain:        {} [{}]".format(
        settings.get("gain_x"),
        "PASS" if settings.get("gain") == 2 else "FAIL",
    ))
    print("  Integration: {} [{}]".format(
        settings.get("integration_cycles"),
        "PASS" if settings.get("integration_cycles") == 100 else "FAIL",
    ))

    print()
    print("Overall calibration: {}".format(result["status"]))

    for failure in result["failures"]:
        print("  [FAIL]    {}".format(failure["message"]))

    for warning in result["warnings"]:
        print("  [WARNING] {}".format(warning["message"]))


# ----------------------------------------------------------------------
# [4] [5] [6] calibration inspection
# ----------------------------------------------------------------------

def show_active_calibration(mission):
    banner("ACTIVE FULL CALIBRATION")

    calibration = mission.active_calibration

    if calibration is None:
        print("No full spectral calibration is active.")
        print()
        print("Reason: {}".format(
            mission.calibration_error or "none has been created"
        ))
        print()
        print("Run [3] Full Spectral Calibration.")

    else:
        status = calibration.status()

        print("Calibration ID:  {}".format(status["calibration_id"]))
        print("Created:         {}".format(status["created_at"]))
        print("Schema version:  {}".format(status["schema_version"]))
        print("File:            {}".format(status["file"]))
        print("Repeats:         {}".format(status["repeats"]))
        print("Validation:      {}".format(status["validation"]))
        print()
        print("Dark channels:            {}/18".format(
            status["dark_channels"]
        ))

        for name in ILLUMINATIONS:
            print("{:<26}{}/18".format(
                "{}-reference channels:".format(name.upper()),
                status["white_channels"][name],
            ))

        print()
        print_settings_block(status["sensor_settings"])

    print()
    print("=" * 60)
    print()
    print("LEGACY DATABASE CALIBRATION")
    print()

    if mission.references is None:
        print("NOT LOADED: {}".format(mission.science_error))

    else:
        status = mission.references.status()

        print("Calibration ID:  {}".format(status["calibration_id"]))
        print("File:            {}".format(status["file"]))
        print("Protected:       YES - never modified, never regenerated")
        print("Database:        {}".format(status["database"]))
        print("Illumination:    WHITE only (18 reference features)")
        print("Dark channels:   {}/18".format(status["dark_channels"]))
        print("White channels:  {}/18".format(status["white_channels"]))
        print()
        print("This is the ONLY calibration used to compare a measurement")
        print("against database.json. It is why the material library does")
        print("not need remeasuring after a new calibration.")

    print()
    pause()


def validate_active_calibration(mission):
    """Software checks against the active calibration. Writes nothing."""
    banner("ACTIVE CALIBRATION VALIDATION")

    if mission.active_calibration is None:
        print("File integrity:       MISSING")
        print()
        print("No full spectral calibration is active. Run")
        print("[3] Full Spectral Calibration.")
        print()
        pause()

        return

    settings = None

    try:
        status = mission.hardware_status()
        settings = (status.get("sensor") or {}).get("settings")

    except (LinkError, TimeoutError):
        print("(ESP32 not reachable - checking the file only.)")
        print()

    result = mission.active_calibration.validate(settings)
    document = mission.active_calibration.document

    def verdict(condition):
        return "PASS" if condition else "FAIL"

    dark_missing = (
        (document.get("dark") or {}).get("statistics") or {}
    ).get("missing_channels") or []

    references = document.get("white_reference") or {}

    print("{:<22}{}".format(
        "File integrity:",
        verdict(document.get("schema_version")
                == bd_config.CALIBRATION_SCHEMA_VERSION),
    ))
    print("{:<22}{}".format(
        "Dark reference:", verdict(not dark_missing)
    ))

    for name in ILLUMINATIONS:
        missing = (
            (references.get(name) or {}).get("statistics") or {}
        ).get("missing_channels") or []

        print("{:<22}{}".format(
            "{} reference:".format(name.upper()), verdict(not missing)
        ))

    settings_ok = not any(
        failure["code"] == "CALIBRATION_INCOMPATIBLE"
        for failure in result["failures"]
    )

    print("{:<22}{}".format("Sensor configuration:", verdict(settings_ok)))
    print("{:<22}{}".format("Overall:", result["status"]))

    for failure in result["failures"]:
        print()
        print("  [FAIL]    {}".format(failure["message"]))

    for warning in result["warnings"]:
        print()
        print("  [WARNING] {}".format(warning["message"]))

    print()
    pause()


def show_calibration_history(mission):
    banner("CALIBRATION HISTORY")

    print("{:<4} {:<38} {:<22} {:<8} {}".format(
        "#", "Calibration ID", "Date", "Type", "Status"
    ))

    legacy_id = (
        mission.references.calibration_id if mission.references else "-"
    )

    print("{:<4} {:<38} {:<22} {:<8} {}".format(
        1, str(legacy_id)[:38], "-", "LEGACY", "PROTECTED"
    ))

    entries = mission.calibrations.history()

    for index, entry in enumerate(entries, start=2):
        if entry.get("active"):
            state = "ACTIVE"
        elif entry.get("validation") == "FAIL":
            state = "INVALID"
        else:
            state = "INACTIVE"

        print("{:<4} {:<38} {:<22} {:<8} {}".format(
            index,
            str(entry.get("calibration_id"))[:38],
            str(entry.get("created_at") or "-")[:22],
            entry.get("kind", "FULL"),
            state,
        ))

    if not entries:
        print()
        print("No full spectral calibration has been created yet.")

    print()
    print("The legacy calibration is protected and cannot be deleted")
    print("from this interface. Calibration files are immutable; only")
    print("the active pointer changes.")
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
        print("[5] Calibration")
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

            elif selection == "5":
                menu_servo_calibration(mission)

            elif selection == "0":
                return

            elif selection:
                print("Unknown option.")

        except (LinkError, TimeoutError) as error:
            report_failure(error)


# ----------------------------------------------------------------------
# servo calibration
# ----------------------------------------------------------------------

# Editable timings, in the order they should be calibrated.
TIMING_FIELDS = (
    ("slot_cw_ms", "90 deg CW"),
    ("slot_ccw_ms", "90 deg CCW"),
    ("load_to_scan_ms", "180 deg LOAD->SCAN"),
    ("scan_to_load_ms", "180 deg SCAN->LOAD"),
    ("settle_ms", "Settle"),
    ("approach_ms", "Slow approach"),
    ("approach_us_offset", "Approach offset (us)"),
    ("start_kick_ms", "Start kick"),
    ("start_kick_us_offset", "Start kick offset (us)"),
    ("brake_ms", "Reverse brake"),
)


def print_calibration(values, modified, speeds=None):
    speeds = speeds or {}

    def with_speed(key):
        speed = speeds.get(key)

        return "  ({:.2f} deg/s)".format(speed) if speed else ""

    print("Current calibration:")
    print()
    print("  Neutral:            {} us".format(values["neutral_us"]))
    print("  CW pulse:           {} us  (neutral {:+d})".format(
        values["cw_us"], values["cw_us"] - values["neutral_us"]
    ))
    print("  CCW pulse:          {} us  (neutral {:+d})".format(
        values["ccw_us"], values["ccw_us"] - values["neutral_us"]
    ))
    print()
    print("  90 deg CW:          {} ms{}".format(
        values["slot_cw_ms"], with_speed("slot_cw")
    ))
    print("  90 deg CCW:         {} ms{}".format(
        values["slot_ccw_ms"], with_speed("slot_ccw")
    ))
    print()
    print("  180 deg LOAD->SCAN: {} ms{}".format(
        values["load_to_scan_ms"], with_speed("load_to_scan")
    ))
    print("  180 deg SCAN->LOAD: {} ms{}".format(
        values["scan_to_load_ms"], with_speed("scan_to_load")
    ))
    print()
    print("  Settle:             {} ms".format(values["settle_ms"]))
    print("  Slow approach:      {}".format(
        "{} ms at {} us closer to neutral".format(
            values["approach_ms"], values["approach_us_offset"]
        ) if values["approach_ms"] else "disabled"
    ))
    print("  Start kick:         {}".format(
        "{} ms at {} us stronger".format(
            values["start_kick_ms"], values["start_kick_us_offset"]
        ) if values["start_kick_ms"] else "disabled"
    ))
    print("  Reverse brake:      {}".format(
        "{} ms".format(values["brake_ms"])
        if values["brake_ms"] else "disabled"
    ))

    if modified:
        print()
        print("  ** RUNTIME OVERRIDE ACTIVE - lost on reset until you")
        print("     copy these into firmware/ESP32/config.py **")


def print_config_block(values):
    """The exact lines to paste into config.py."""
    print()
    print("Paste into firmware/ESP32/config.py:")
    print()
    print("    SERVO_STOP_US       = {}".format(values["neutral_us"]))
    print("    SERVO_CW_US         = {}".format(values["cw_us"]))
    print("    SERVO_CCW_US        = {}".format(values["ccw_us"]))
    print("    SERVO_SETTLE_MS     = {}".format(values["settle_ms"]))
    print("    SERVO_APPROACH_MS   = {}".format(values["approach_ms"]))
    print("    SERVO_APPROACH_US_OFFSET = {}".format(
        values["approach_us_offset"]
    ))
    print("    SERVO_START_KICK_MS = {}".format(values["start_kick_ms"]))
    print("    SERVO_START_KICK_US_OFFSET = {}".format(
        values["start_kick_us_offset"]
    ))
    print("    SERVO_BRAKE_MS      = {}".format(values["brake_ms"]))
    print("    NEXT_SLOT_CW_MS     = {}".format(values["slot_cw_ms"]))
    print("    NEXT_SLOT_CCW_MS    = {}".format(values["slot_ccw_ms"]))
    print("    LOAD_TO_SCAN_CW_MS  = {}".format(values["load_to_scan_ms"]))
    print("    SCAN_TO_LOAD_CCW_MS = {}".format(values["scan_to_load_ms"]))
    print()


def report_test_move(data):
    print()
    print("  Pulse:     {} us".format(data.get("pulse_us")))
    print("  Neutral:   {} us".format(data.get("neutral_us")))
    print("  Duration:  {} ms x {} = {} ms".format(
        data.get("duration_ms"), data.get("repeat"),
        data.get("total_duration_ms"),
    ))
    print("  Nominal:   {:.0f} deg".format(data.get("nominal_degrees", 0)))

    if data.get("speed_deg_per_s"):
        print("  Speed:     {:.2f} deg/s".format(data["speed_deg_per_s"]))

    print()
    print("Movement complete.")

    if data.get("position_invalidated"):
        print()
        print("Carousel position tracking was invalidated by this test -")
        print("re-sync before normal operation.")


def offer_timing_correction(mission, data, key, label):
    """
    Turn a measured angular error into a corrected duration.

    At an approximately constant speed the angle is proportional to the
    runtime, so a measured error converges the calibration in a couple
    of iterations instead of being nudged by guesswork:

        actual = target + error
        t_new  = t_old * target / actual
    """
    target = data.get("target_degrees")
    duration = data.get("duration_ms")
    repeat = data.get("repeat", 1) or 1

    if not target or not duration:
        return

    # With repeats the operator measures the accumulated error, so the
    # target is the whole travel.
    total_target = target * repeat

    print()
    print("Observed angular error at the end of the movement.")
    print("  + = overshoot (went too far)")
    print("  - = undershoot (fell short)")
    print("  blank = skip")
    print()

    answer = ask("Error [deg]").strip()

    if not answer:
        return

    try:
        error = float(answer.replace(",", "."))

    except ValueError:
        print("Not a number; skipping the correction.")

        return

    actual = total_target + error

    if actual <= 0:
        print("An actual movement of {:.2f} deg cannot be used to "
              "re-time the move.".format(actual))

        return

    corrected = int(round(duration * total_target / actual))

    print()
    print("  Target:        {:.2f} deg".format(total_target))
    print("  Actual:        {:.2f} deg".format(actual))
    print("  Speed:         {:.2f} deg/s".format(
        actual * 1000.0 / (duration * repeat)
    ))
    print()
    print("  old duration:  {} ms".format(duration))
    print("  new duration:  {} ms".format(corrected))

    if corrected == duration:
        print()
        print("Already optimal at this resolution.")

        return

    print()

    if not ask("Apply new duration? [y/N]").strip().lower() in ("y", "yes"):
        print("Not applied.")

        return

    try:
        mission.link.set_servo_calibration({key: corrected})
        print("{} is now {} ms.".format(label, corrected))
        print("Run the test again to confirm the error has shrunk.")

    except LinkError as error:
        print("Refused: {}".format(error.message))


def adjust_pulse(mission, key, label):
    """Nudge one pulse width, one small step at a time."""
    while True:
        values = mission.link.get_servo_calibration()["current"]
        current = values[key]

        print()
        print("{}: {} us".format(label, current))

        if key != "neutral_us":
            print("Neutral is {} us, so the offset is {:+d} us.".format(
                values["neutral_us"], current - values["neutral_us"]
            ))

        print()
        print("Enter a step such as +1, -2, +5, an absolute value such as")
        print("1498, or blank to go back.")

        answer = ask("Adjust").strip()

        if not answer:
            return

        try:
            if answer[0] in "+-":
                target = current + int(answer)
            else:
                target = int(answer)

        except ValueError:
            print("Enter a number, for example +2, -5 or 1498.")

            continue

        try:
            mission.link.set_servo_calibration({key: target})
            print("{} is now {} us.".format(label, target))

        except LinkError as error:
            print("Refused: {}".format(error.message))


def edit_timings(mission):
    """Type new values for the timing constants."""
    values = mission.link.get_servo_calibration()["current"]

    print()
    print("Blank keeps the current value.")
    print()

    updates = {}

    for key, label in TIMING_FIELDS:
        answer = ask("  {} [{}]".format(label, values[key])).strip()

        if not answer:
            continue

        try:
            updates[key] = int(answer)

        except ValueError:
            print("    not a number, keeping {}".format(values[key]))

    if not updates:
        print()
        print("Nothing changed.")

        return

    try:
        result = mission.link.set_servo_calibration(updates)

        print()
        print("Updated: {}".format(", ".join(result.get("changed") or [])))

    except LinkError as error:
        print()
        print("Refused: {}".format(error.message))


def menu_servo_calibration(mission):
    """
    Tune the movement calibration against the real mechanism.

    Values are changed in RAM on the ESP32 so the operator can converge
    without an upload-and-reset cycle per attempt. They are NOT
    persistent: option [c] prints the block to paste into config.py.

    Calibrate in order - neutral first, then the pulses, then the
    timings. Nothing downstream means anything until neutral is right.
    """
    while True:
        banner("SERVO / CAROUSEL CALIBRATION")

        try:
            calibration = mission.link.get_servo_calibration()

        except (LinkError, TimeoutError) as error:
            report_failure(error)
            print()
            pause()

            return

        values = calibration["current"]

        print_calibration(
            values,
            calibration.get("modified"),
            calibration.get("speed_deg_per_s"),
        )

        print()
        print("[1] Test STOP / neutral (hold and watch for creep)")
        print("[2] Test 90 deg CW")
        print("[3] Test 90 deg CCW")
        print("[4] Test 180 deg LOAD->SCAN")
        print("[5] Test 180 deg SCAN->LOAD")
        print("[o] Test 180 deg OUT AND BACK (the measurement path)")
        print()
        print("[6] Fine-adjust neutral pulse")
        print("[7] Fine-adjust CW pulse")
        print("[8] Fine-adjust CCW pulse")
        print("[9] Edit timing values")
        print()
        print("[c] Show config.py block")
        print("[r] Reset to config.py defaults")
        print("[0] Back")

        selection = choose()

        try:
            if selection == "0":
                return

            if selection == "1":
                seconds = ask_int("Hold neutral for how many seconds", 1, 30,
                                  default=5)

                if seconds is None:
                    continue

                print()
                print("Holding neutral ({} us) for {} s. Watch the "
                      "carousel:".format(values["neutral_us"], seconds))
                print("any creep at all means the neutral pulse is wrong.")
                sys.stdout.flush()

                mission.link.servo_test_move(
                    "neutral", hold_ms=seconds * 1000
                )

                print()
                print("Done. If it crept clockwise, lower the neutral "
                      "pulse; if counter-clockwise, raise it.")

            elif selection in ("2", "3", "4", "5"):
                kinds = {
                    "2": ("slot_cw", "90 deg CW", "slot_cw_ms"),
                    "3": ("slot_ccw", "90 deg CCW", "slot_ccw_ms"),
                    "4": ("load_to_scan", "180 deg LOAD->SCAN",
                          "load_to_scan_ms"),
                    "5": ("scan_to_load", "180 deg SCAN->LOAD",
                          "scan_to_load_ms"),
                }

                kind, label, key = kinds[selection]

                repeat = ask_int(
                    "Repeat how many times (4 x 90 deg = one full turn)",
                    1, 8, default=1,
                )

                if repeat is None:
                    continue

                print()
                print("{} TEST".format(label))
                sys.stdout.flush()

                result = mission.link.servo_test_move(kind, repeat=repeat)

                report_test_move(result)
                offer_timing_correction(mission, result, key, label)

            elif selection == "o":
                repeat = ask_int(
                    "How many out-and-back cycles", 1, 8, default=3
                )

                if repeat is None:
                    continue

                print()
                print("180 deg OUT AND BACK x {}".format(repeat))
                print("Each cycle should end exactly where it started.")
                sys.stdout.flush()

                result = mission.link.servo_test_move(
                    "out_and_back", repeat=repeat
                )

                print()
                print("  Out:       {} ms".format(result.get("out_ms")))
                print("  Back:      {} ms".format(result.get("back_ms")))
                print("  Cycles:    {}".format(result.get("repeat")))
                print()
                print("Measure the error at HOME. If the carousel has drifted")
                print("clockwise, shorten 180 deg LOAD->SCAN or lengthen")
                print("180 deg SCAN->LOAD; if counter-clockwise, the reverse.")
                print()
                print("This is the movement Measure Sample depends on, so")
                print("optimise for the smallest error after the PAIR.")

            elif selection == "6":
                adjust_pulse(mission, "neutral_us", "Neutral")

            elif selection == "7":
                adjust_pulse(mission, "cw_us", "CW pulse")

            elif selection == "8":
                adjust_pulse(mission, "ccw_us", "CCW pulse")

            elif selection == "9":
                edit_timings(mission)

            elif selection == "c":
                print_config_block(values)
                pause()

            elif selection == "r":
                mission.link.set_servo_calibration(reset=True)
                print("Reset to the values in config.py.")

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
    target = ask_int(
        "Choose slot [1-{}]".format(SLOT_COUNT), 1, SLOT_COUNT,
        default=suggested,
    )

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


def apply_measurement(record, result):
    """
    Write a complete BD result into a Sample record.

    Everything the pipeline produced is kept: all three 18-channel
    spectra, the White and Dark actually used, the comparison against
    every material, and the whole analysis block. The handful of flat
    fields at the end are mirrors for the compact table and for reading
    the archive by eye - the authoritative values stay in `analysis`.
    """
    analysis = result.get("analysis") or {}

    record.update({
        "measured": bool(result.get("measurement")),
        "schema_version": bd_config.SAMPLE_SCHEMA_VERSION,
        "measurement": result["measurement"],
        "calibration": result["calibration"],
        "database": result["database"],
        "quality": result.get("quality"),
        "reference_matches": result["reference_matches"],
        "metric_agreement": result.get("metric_agreement"),
        "analysis": result["analysis"],
        "analysis_status": result["analysis_status"],
        "analysis_error": result["analysis_error"],

        "best_match": analysis.get("best_match"),
        "best_similarity": analysis.get("best_similarity"),
        "best_rmse": analysis.get("best_rmse"),
        "best_pearson_r": analysis.get("best_pearson_r"),
        "second_match": analysis.get("second_match"),
        "second_similarity": analysis.get("second_similarity"),
        "score_difference": analysis.get("score_difference"),
        "status": analysis.get("status"),
        "quality_status": (result.get("quality") or {}).get("status"),
        "conclusion": analysis.get("automatic_conclusion"),
    })

    return record


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
    print("Checking Sample................. PASS")
    print("Checking carousel.............. PASS")
    print()
    print("The carousel will swing 180 deg to the scanner, acquire one "
          "18-channel spectrum, then swing 180 deg back so the sample "
          "ends where it started. This takes a few seconds.")
    print()
    sys.stdout.flush()

    # ---- ESP32: out, acquire, back ----------------------------------
    try:
        data = mission.link.measure_raw(slot_id, sample_id)

    except (LinkError, TimeoutError) as error:
        print()
        print("Measurement failed before any spectrum was obtained.")

        if isinstance(error, LinkError):
            report_link_error(error)
            report_return_move((error.data or {}).get("return_move"))
        else:
            print("Timeout: {}".format(error))

        print()
        print("Sample {} remains {}. Nothing was saved.".format(
            sample_id, STATE_LOADED
        ))
        print()
        pause()

        return

    settings = data.get("sensor_settings")
    blocks = data.get("illuminations") or {}
    measured_at = utc_timestamp()

    print("Measuring {}.................. PASS".format(sample_id))

    for name in ILLUMINATIONS:
        block = blocks.get(name) or {}

        print("  {:<6} {} repeat(s), {}/18 channels".format(
            name.upper(),
            block.get("repeats", "-"),
            len((block.get("acquisitions") or [{}])[0]),
        ))

    # The acquisition succeeded. Whether the carousel made it home is a
    # separate outcome and must not affect what gets saved.
    report_return_move(data.get("return_move"))

    # ---- BD: analyse -------------------------------------------------
    analysis_error = None

    try:
        result = mission.analyse_raw(data, settings)

    except Exception as error:
        # Acquired science must survive downstream software failure.
        analysis_error = "{}: {}".format(type(error).__name__, error)
        result = {
            "measurement": {
                "wavelengths": sample_analysis.channel_wavelengths(),
                "raw": {
                    name: (blocks.get(name) or {}).get("acquisitions", [{}])[0]
                    for name in blocks
                },
                "active_normalized": {},
                "legacy_database_normalized": {},
                "dark_corrected": None,
                "normalized": None,
                "sensor_settings": settings,
            },
            "calibration": None,
            "database": None,
            "quality": None,
            "reference_matches": [],
            "metric_agreement": {},
            "analysis": None,
            "analysis_status": "FAILED",
            "analysis_error": analysis_error,
        }

        print()
        print("!! ANALYSIS FAILED: {}".format(analysis_error))
        print("   The acquired spectra are intact and will still be saved.")

    # ---- PC: complete the SAME record --------------------------------
    record = mission.store.get_sample(sample_id) or {"sample_id": sample_id}

    timestamps = record.get("timestamps") or {}
    timestamps["measured_at"] = measured_at

    record.update({
        "sample_id": sample_id,
        "slot_id": slot_id,
        "state": STATE_MEASURED,
        "timestamps": timestamps,
        "hardware": {
            "carousel": data.get("carousel"),
            "data_ready_wait_ms": data.get("data_ready_wait_ms"),
            "zero_channels": data.get("zero_channels"),
            "home_restored": data.get("home_restored"),
        },
    })

    apply_measurement(record, result)

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
    print("Home position:  {}".format(
        "RESTORED" if data.get("home_restored") else "NOT RESTORED"
    ))

    print()
    print("SETTINGS")
    print()
    print_settings_block(result["measurement"].get("sensor_settings"))

    quality_report = result.get("quality")

    print()
    print("MEASUREMENT QUALITY")
    print()

    if quality_report:
        print_quality(quality_report)
    else:
        print("Not assessed: the analysis did not run.")

    if result["analysis_status"] == "OK":
        print()
        print("REFERENCE COMPARISON")
        print("  legacy calibration {}".format(
            result["calibration"]["legacy_database_calibration_id"]
        ))
        print()
        print_metric_table(result["reference_matches"], limit=5)

        print()
        print_agreement(result.get("metric_agreement"))

        print()
        print("RESULT")
        print()
        print_result_block(result["analysis"])

    else:
        print()
        print("Analysis status: FAILED - {}".format(result["analysis_error"]))
        print("The acquired spectra are stored and can be re-analysed "
              "offline.")

    print()
    print("Full 54-channel data is in the saved record; open the Sample")
    print("from Tools -> Sample Database to see it.")

    print()

    if data.get("home_restored"):
        print("Slot {} is back at the loading position, exactly where it "
              "started. The soil is still physically in the slot.".format(
                  slot_id
              ))

    else:
        print("The measurement is saved, but the carousel did NOT return "
              "home. Re-sync the carousel (Tools -> Re-sync Carousel) "
              "before moving anything else.")

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
    print("Enter slot number to clear.")
    print("[a] Clear ALL physical slots")
    print("[0] Back")
    print()

    answer = ask("Select (blank = cancel)").strip().lower()

    if not answer or answer == "0":
        print("Cancelled.")

        return

    if answer == "a":
        clear_all_slots(mission, view)

        return

    try:
        slot_id = int(answer)

    except ValueError:
        print("Enter a slot number, 'a' for all, or 0 to go back.")

        return

    if slot_id < 1 or slot_id > SLOT_COUNT:
        print("Slots are 1 to {}.".format(SLOT_COUNT))

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


def clear_all_slots(mission, view):
    """
    Free every physical slot at once.

    Carousel occupancy only. Saved Sample records - on the PC and on the
    ESP32 - are left completely alone; deleting those is a separate,
    separately confirmed operation.
    """
    occupied = [entry for entry in view if entry["state"] != STATE_EMPTY]

    if not occupied:
        print()
        print("All physical slots are already empty.")

        return

    print()
    print("Clear ALL {} physical slots?".format(SLOT_COUNT))
    print()
    print("This only clears carousel occupancy.")
    print("Saved Sample records will NOT be deleted.")
    print()
    print("Currently occupied:")

    for entry in occupied:
        print("  Slot {}  {}  {}".format(
            entry["slot_id"], entry["sample_id"] or "----", entry["state"]
        ))

    print()

    if ask("Type YES to continue") != "YES":
        print("Cancelled; nothing changed.")

        return

    data = mission.link.clear_all_slots()

    # The PC lifecycle state is authoritative, so free the slots there
    # too - without deleting anything.
    failures = []

    for entry in occupied:
        if not entry["sample_id"]:
            continue

        try:
            mission.store.set_state(entry["sample_id"], STATE_EMPTY)

        except StorageError as error:
            failures.append((entry["sample_id"], error.message))

    print()
    print("Physical slots cleared: {}".format(data.get("cleared_count", 0)))
    print("All {} slots are now EMPTY.".format(SLOT_COUNT))

    if failures:
        print()
        print("Some PC records could not be updated:")

        for sample_id, message in failures:
            print("  {}: {}".format(sample_id, message))

    print()
    print("Saved Sample records were NOT deleted.")


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

    # Newer records carry both; older ones only the single legacy id.
    legacy_id = calibration.get("legacy_database_calibration_id") \
        or calibration.get("calibration_id", "-")

    print("  legacy (database):  {}".format(legacy_id))
    print("  active (science):   {}".format(
        calibration.get("active_calibration_id") or "-"
    ))
    print("  {}".format(calibration.get("equation", "-")))

    print()
    print("SENSOR SETTINGS")
    print()
    print_settings_block(measurement.get("sensor_settings"))

    quality_report = record.get("quality")

    if quality_report:
        print()
        print("MEASUREMENT QUALITY")
        print()
        print_quality(quality_report)

    print()
    print("RAW SPECTRUM")
    print()
    print_triad_table(measurement)

    # The White and Dark this sample was actually processed against,
    # snapshotted into the record so the numbers above can be checked
    # by hand without opening references.json.
    dark = calibration.get("dark_reference")
    white = calibration.get("white_reference")

    if dark and white:
        print()
        print("WHITE / DARK PROCESSING (legacy calibration)")
        print()
        print_processing_table(measurement, dark, white)

    matches = record.get("reference_matches") or []

    print()
    print("DATABASE COMPARISON ({} materials)".format(len(matches)))
    print()

    # A record written before the multi-metric release has only cosine.
    if matches and matches[0].get("rmse") is not None:
        print_metric_table(matches)
        print()
        print_agreement(record.get("metric_agreement"))

    else:
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
        print("[6] Import ALL Samples from ESP32")
        print("[7] Delete ALL Samples from ESP32")
        print()
        print("[0] Back")

        selection = choose()

        try:
            if selection == "0":
                return

            if selection == "5":
                store.load()

                continue

            if selection == "6":
                sync_esp32_samples(mission)
                store.load()

                continue

            if selection == "7":
                delete_esp32_samples(mission)

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
# ESP32 -> PC sample synchronization
# ======================================================================

def sync_esp32_samples(mission):
    """
    Copy every acquisition the ESP32 holds into the PC archive.

    This is a COPY, never a move: the ESP32 keeps its records, and an ID
    that already exists on the PC is never overwritten. Running it twice
    therefore transfers nothing the second time.

        ID not on the PC              -> IMPORT
        ID on the PC, same spectrum   -> SKIP
        ID on the PC, different data  -> CONFLICT, left untouched

    It writes only to the measured-Sample archive. The material database
    and the White/Dark references are never opened for writing anywhere
    in this program.
    """
    banner("IMPORT ESP32 SAMPLES TO PC")

    try:
        index = mission.link.list_saved_samples()

    except (LinkError, TimeoutError) as error:
        print("Reading Sample index............ FAIL")
        report_failure(error)
        print()
        pause()

        return

    print("Connecting to ESP32............. PASS")
    print("Reading Sample index............ PASS")
    print()

    entries = index.get("samples") or []

    print("ESP32 Samples: {}".format(len(entries)))
    print()

    if not entries:
        print("The ESP32 is holding no acquisitions. Its buffer is RAM "
              "only and is empty after a reset.")
        print()
        pause()

        return

    imported = []
    skipped = []
    conflicts = []
    failed = []

    for entry in entries:
        sample_id = entry.get("sample_id")

        if not sample_id:
            continue

        try:
            payload = mission.link.get_saved_sample(sample_id)

        except (LinkError, TimeoutError) as error:
            failed.append(sample_id)
            print("{:<12}FAILED              {}".format(sample_id, error))

            continue

        device_raw = _white_spectrum(payload.get("measurement"))
        existing = mission.store.get_sample(sample_id)

        if existing is not None:
            stored_raw = _white_spectrum(existing.get("measurement"))

            if _same_spectrum(stored_raw, device_raw):
                skipped.append(sample_id)
                print("{:<12}already exists      SKIP".format(sample_id))

            else:
                conflicts.append(sample_id)
                print("{:<12}conflict            CONFLICT".format(sample_id))

            continue

        try:
            mission.store.save(
                _build_record(mission, sample_id, entry, payload)
            )

            imported.append(sample_id)
            print("{:<12}imported            PASS".format(sample_id))

        except StorageError as error:
            failed.append(sample_id)
            print("{:<12}FAILED              {}".format(
                sample_id, error.message
            ))

    print()
    print("Imported:   {}".format(len(imported)))
    print("Skipped:    {}".format(len(skipped)))
    print("Conflicts:  {}".format(len(conflicts)))
    print("Failed:     {}".format(len(failed)))

    if imported:
        print()
        print("Imported:")

        for sample_id in imported:
            print("  {}".format(sample_id))

    if conflicts:
        print()
        print("CONFLICTS - the PC already has these IDs with DIFFERENT")
        print("measurement data. Nothing was overwritten. Rename or delete")
        print("the PC record first if the device copy is the one you want:")

        for sample_id in conflicts:
            print("  {}".format(sample_id))

    print()
    print("ESP32 database was NOT modified.")
    print("PC Sample archive updated: {}".format(mission.store.archive_path))
    print()
    pause()


def _white_spectrum(measurement):
    """
    The WHITE 18-channel spectrum out of any measurement shape.

    Handles the device's live acquisition block, an archived record from
    this release, and a bare 18-channel dict from before it - which is
    what makes an identical/conflicting comparison possible across the
    schema change.
    """
    measurement = measurement or {}

    blocks = measurement.get("illuminations")

    if isinstance(blocks, dict):
        acquisitions = (blocks.get("white") or {}).get("acquisitions") or []

        return acquisitions[0] if acquisitions else {}

    raw = measurement.get("raw") or {}

    if isinstance(raw.get("white"), dict):
        return raw["white"]

    return raw


def _same_spectrum(first, second):
    """
    Whether two raw spectra are the same measurement.

    Compared channel by channel with a tolerance, because a value that
    has been through JSON and a round() is not always bit-identical to
    the one that came off the sensor.
    """
    if not first or not second:
        return False

    for channel in sample_analysis.CHANNELS:
        if channel not in first or channel not in second:
            return False

        try:
            if abs(float(first[channel]) - float(second[channel])) > 1e-4:
                return False

        except (TypeError, ValueError):
            return False

    return True


def _build_record(mission, sample_id, entry, payload):
    """
    Turn one retained acquisition into a PC Sample record.

    Every field the device actually stores is copied faithfully; nothing
    is invented. Analysis is run through the normal BD path so the
    imported Sample is a complete record rather than a stub - and if BD
    is unavailable the raw spectrum is stored anyway with
    analysis_status FAILED.
    """
    measurement = payload.get("measurement") or {}

    # The device buffer holds whatever the acquisition returned - the
    # WHITE/UV/IR protocol on current firmware, a bare white spectrum on
    # older. analyse_raw accepts both.
    settings = measurement.get("sensor_settings")
    raw = measurement.get("raw") or {}

    record = {
        "sample_id": sample_id,
        "slot_id": payload.get("slot_id") or entry.get("slot_id"),
        "state": STATE_MEASURED,
        "timestamps": {
            "created_at": None,
            "loaded_at": None,
            "measured_at": utc_timestamp(),
        },
        "metadata": None,
        "source": {
            "origin": "esp32_sync",
            "esp_uptime_ms": measurement.get("esp_uptime_ms"),
            "note": "Copied from the ESP32 acquisition buffer. Timestamps "
                    "and metadata were not recorded on the device.",
        },
        "missing_information": [
            "created_at", "loaded_at", "metadata",
        ],
    }

    try:
        result = mission.analyse_raw(measurement, settings)

    except Exception as error:
        record.update({
            "measured": bool(raw),
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
            "analysis_error": "{}: {}".format(type(error).__name__, error),
        })

        return record

    return apply_measurement(record, result)


def delete_esp32_samples(mission):
    """
    Delete every Sample record held on the ESP32.

    Destructive and deliberately narrow. It removes the device's own
    records and nothing else: the PC archive, the physical slot states,
    the material database and the White/Dark references are all
    untouched.

    Import first if the data matters - this is not chained to the import
    on purpose, so the operator can verify the copy before destroying
    the original.
    """
    banner("DELETE ALL ESP32 SAMPLES")

    try:
        index = mission.link.list_saved_samples()

    except (LinkError, TimeoutError) as error:
        report_failure(error)
        print()
        pause()

        return

    entries = index.get("samples") or []

    if not entries:
        print("ESP32 Sample storage is already empty.")
        print()
        pause()

        return

    print("ESP32 Samples: {}".format(len(entries)))
    print()

    for entry in entries:
        on_pc = mission.store.has_sample(entry.get("sample_id"))

        print("  {:<12}{}".format(
            entry.get("sample_id"),
            "already imported to PC" if on_pc else "NOT ON THE PC YET",
        ))

    missing = [
        entry.get("sample_id") for entry in entries
        if not mission.store.has_sample(entry.get("sample_id"))
    ]

    if missing:
        print()
        print("!! {} of these are NOT in the PC archive. Import them "
              "first, or they are gone for good.".format(len(missing)))

    print()
    print("PC Samples, the material database, the White/Dark references")
    print("and the physical slot states will NOT be changed.")
    print()

    if ask("Delete ALL saved Samples from ESP32? [y/N]").strip().lower() \
            not in ("y", "yes"):
        print("Cancelled.")
        print()
        pause()

        return

    print()
    print("Deleting...")

    try:
        data = mission.link.delete_saved_samples()

    except (LinkError, TimeoutError) as error:
        report_failure(error)
        print()
        pause()

        return

    print("Deleted: {}".format(data.get("deleted_count", 0)))

    # Do not trust the return code. Ask the device what it still holds.
    try:
        after = mission.link.list_saved_samples()

    except (LinkError, TimeoutError) as error:
        print()
        print("DELETE VERIFICATION FAILED - could not read the device back:")
        report_failure(error)
        print()
        pause()

        return

    remaining = after.get("samples") or []

    if remaining:
        print()
        print("DELETE VERIFICATION FAILED")
        print("The device reported success but still holds {} "
              "record(s):".format(len(remaining)))

        for entry in remaining:
            print("  {}".format(entry.get("sample_id")))

    else:
        print()
        print("ESP32 Sample storage is now empty.")
        print("PC Sample archive was not modified.")
        print("Physical slot occupancy was not changed.")

    print()
    pause()


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
    ("5", "Sensor Test / Calibration",
     "Test the sensor and analysis pipeline; create a full calibration.",
     lambda mission, status, view: menu_sensor_test(mission)),
    ("6", "Clear Physical Slot", "Free a physical carousel slot.",
     menu_clear_slot),
    ("7", "Sync ESP32 Samples to PC",
     "Copy acquisitions held on the ESP32 into the PC archive.",
     lambda mission, status, view: sync_esp32_samples(mission)),
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
  5. Measure Sample          (180 deg out, RAW, 180 deg back, saved)

CAROUSEL

  4 slots, 90 degrees apart. The scanner sits 180 degrees - two slots -
  from the loading hole:

      Loader Slot 1  ->  Scanner Slot 3
      Loader Slot 2  ->  Scanner Slot 4
      Loader Slot 3  ->  Scanner Slot 1
      Loader Slot 4  ->  Scanner Slot 2

  Measure Sample swings the slot out to the scanner and back again, so a
  successful measurement ends with the sample exactly where it started.

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
  scientific record; Delete Sample is what removes it.

SYNC ESP32 SAMPLES TO PC

  The ESP32 holds the last raw acquisition per slot in RAM. If this
  program lost a result - a crash, a restart, a different laptop -
  Tools -> Sync ESP32 Samples to PC copies it into the archive. It never
  overwrites an existing Sample and never deletes the ESP32 copy."""


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

    if mission.active_calibration is None:
        print()
        print("FULL CALIBRATION REQUIRED - UV/IR reflectance is not")
        print("available. Tools -> Sensor Test -> Full Spectral")
        print("Calibration. The material library is unaffected.")

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
