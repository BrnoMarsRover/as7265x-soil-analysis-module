"""
Calibration screens: making one, checking one, choosing which is active.

A calibration is the Dark and White the instrument saw under a stated
set of conditions. Everything downstream divides by it, so making one
is the most consequential thing an operator does at the bench and it
gets its own screens rather than a corner of a menu.

The arithmetic is Science/calibration.py's. This module acquires the
blocks, shows what came back, and asks BD to store the result.
"""

from BD import config as bd_config
from BD.calibrations import CalibrationError
from BD.channels import ILLUMINATIONS

from Science import config as science_config, preprocessing
from Science.calibration import build_calibration, validate_calibration

from serial_link import DeviceError, LinkError

from workflow.display import (
    print_science_result,
    ESP32_STAGE_LABELS,
    offer_decision_detail,
    print_check,
    print_database_results,
    print_decision,
    print_evidence_summary,
    print_quality,
    print_settings_block,
    print_spectrum_table,
    print_triad_table,
    report_failure,
    report_link_error,
)
from workflow.prompts import (
    RULE,
    ask,
    ask_int,
    banner,
    choose,
    confirm,
    number,
    pause,
)

import json
import sys
import textwrap

from serial_link import utc_timestamp

from workflow.records import offer_measurement_disposition

from Science import pipeline


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

        if health["stored"]:
            print("  {} calibration(s) are stored: [7] Select Which".format(
                health["stored"]
            ))
            print("  Calibration To Use, or [3] to make a new one.")
        else:
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
        print("[7] Select Which Calibration To Use")
        print("    Choose from every calibration ever made. Nothing new")
        print("    is measured.")
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

            elif selection == "7":
                menu_select_calibration(mission)

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


def print_failed_acquisition(data, settings):
    """
    An acquisition that produced no spectra, and what the sensor was set to.

    ITS OWN FUNCTION so `menu_full_sensor_test` renders no science block
    itself. There is no AnalysisRun here - there is no measurement - so
    `print_science_result` has nothing to render and this is not a
    duplicate of it. Keeping the settings visible matters most in
    exactly this case: a failed acquisition is usually explained by
    what the sensor was set to.
    """
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
        print_failed_acquisition(data, settings)
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
        result = mission.analyse_acquisition(data)

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

    # ---- THE SCIENCE, THROUGH THE ONE RENDERER -----------------------
    #
    # Identical to what a production measurement shows, because it is
    # the same function. Both screens used to render the same
    # AnalysisRun with their own code, and the two had drifted: this
    # one printed the database comparison and the production screen did
    # not.
    #
    # `settings` is what the ESP32 reported for THIS acquisition, read
    # back from the silicon, and it is already in hand above. The
    # renderer falls back to the evidence package's copy when a caller
    # does not have it first-hand.
    print_science_result(result, settings=settings)

    if health["active"] != "PASS":
        print()
        print("Active (UV/IR) reflectance columns are absent because no")
        print("full spectral calibration is active.")

    offer_decision_detail(result)

    # NOTHING HAS BEEN WRITTEN YET, and the operator decides what
    # happens next: the learning history, the Sample archive, both, or
    # neither. See records.offer_measurement_disposition.
    offer_measurement_disposition(mission, data, result)

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

    return preprocessing.aggregate_block(block)


def _acquire_with_retry(mission, illumination, repeats, label):
    """
    One calibration block, retried as often as the operator wants.

    A calibration is four acquisitions taken against two physical
    setups, and it used to be abandoned in full the moment any one of
    them failed - after which the operator had to reinstall the White
    target and start again from the Dark. A failed block is almost
    always a transport fault, so the block itself is what gets repeated.

    Returns the aggregated block, or None if the operator gives up.
    """
    while True:
        try:
            return _acquire_calibration_block(
                mission, illumination, repeats, label
            )

        except (LinkError, TimeoutError) as error:
            print()
            print("{}_CALIBRATION_FAILED".format(illumination.upper()))
            report_failure(error)
            print()
            print("Nothing else is lost: this block alone is repeated, and")
            print("the reference target must NOT be moved.")
            print()

            if not confirm("Try this block again?"):
                return None


def _print_block_summary(title, aggregated):
    statistics = aggregated["statistics"]
    spectrum = aggregated["spectrum"]

    print()
    print(title)
    print()
    print("CH   {:>10} {:>10} {:>10} {:>9}".format(
        "Median", "Mean", "StdDev", "CV"
    ))

    for channel in pipeline.CHANNELS:
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


def report_save(mission, document, note):
    """
    Save a calibration, and say plainly if it could not be saved.

    THE SAVE USED TO BE UNGUARDED.

    `mission.calibrations.save(document)` was called bare, twice. A
    full disk or a read-only directory raised out of BD, past the menu
    loop - which catches LinkError and TimeoutError - and out of the
    application, as a traceback.

    The calibration lost with it is not cheap: it is a multi-minute
    procedure with physical white and dark references placed in the
    carousel by hand, and it cannot be recovered by restarting.
    Telling the operator that the save failed at least lets them free
    some space and press the key again while the references are still
    in the slots.

    Returns True when the calibration is on disk.
    """
    try:
        path = mission.calibrations.save(document)

    except CalibrationError as error:
        print()
        print("!! COULD NOT SAVE THE CALIBRATION: {}".format(error.message))
        print("   The measurement is still in memory and the references")
        print("   are still in the carousel. Free some space and choose")
        print("   this option again - restarting the client loses it.")
        print()
        pause()

        return False

    print("Saved{}: {}".format(
        " ({})".format(note) if note else "", path))

    return True


def menu_full_calibration(mission):
    """
    Guided Dark + WHITE/UV/IR calibration.

    Appends a NEW calibration to the database. It never touches the
    protected LEGACY White/Dark or DB1.json, and it does not become
    active until the operator confirms after seeing the validation.
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
    print("calibration, the LEGACY White/Dark or DB1.json. The existing")
    print("material library stays valid and does not need remeasuring.")

    stored = health["stored"]

    if stored:
        print()
        print("{} calibration(s) are already stored. A new one is only".format(
            stored
        ))
        print("needed if the optics, the lamps or the sensor settings have")
        print("changed - otherwise use [7] Select Which Calibration To Use.")

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

    dark = _acquire_with_retry(mission, "dark", repeats, "Dark")

    if dark is None:
        print()
        print("Calibration abandoned at the Dark step. Nothing was saved")
        print("and the active calibration is unchanged.")
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
        block = _acquire_with_retry(
            mission, name, repeats, "{} illumination".format(name.upper())
        )

        if block is None:
            print()
            print("Calibration abandoned at the {} step. The {} block(s)".format(
                name.upper(), len(white_blocks)
            ))
            print("already acquired are discarded: a calibration is only")
            print("meaningful if Dark and all three illuminations were")
            print("measured against the same target in the same session.")
            print()
            pause()

            return

        white_blocks[name] = block

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
            if not report_save(mission, document, "inactive, marked "
                                                  "invalid"):
                return

        else:
            print("Discarded.")

        print()
        pause()

        return

    if not report_save(mission, document, None):
        return

    print()

    if not confirm("Activate this calibration?"):
        print("Saved but NOT activated. The previous calibration is still")
        print("in force.")
        print()
        pause()

        return

    # The validator is injected: BD stores calibrations but must not
    # import the science layer, so PC supplies the scientific judgement
    # that decides whether this one may become active. See ARCHITECTURE.md.
    try:
        mission.calibrations.activate(
            document["calibration_id"], validate_calibration
        )

    except CalibrationError as error:
        print()
        print("!! ACTIVATION REFUSED: {}".format(error.message))
        print("   The calibration is saved but the previous one is still")
        print("   in force.")
        print()
        pause()

        return

    mission.load_science()

    print()
    print("Active calibration is now {}.".format(
        document["calibration_id"]
    ))
    print("The legacy database calibration is unchanged.")
    print()
    pause()


def bd_config_repeats():
    """
    Default repeats per calibration block.

    This used to read CALIBRATION_REPEATS off the BD config with a
    fallback, but that constant only ever existed in ESP32/config.py, so
    the lookup always missed and the fallback was the real value. It is
    now a named host-side default.
    """
    return science_config.DEFAULT_CALIBRATION_REPEATS


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
        print("against DB1.json. It is why the material library does")
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

    document = mission.active_calibration.document
    result = validate_calibration(document, settings)

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


def calibration_state(entry):
    """ACTIVE / INVALID / INACTIVE for one stored calibration."""
    if entry.get("active"):
        return "ACTIVE"

    if entry.get("validation") == "FAIL":
        return "INVALID"

    return "INACTIVE"


def print_calibration_list(entries):
    """
    The stored calibrations, with the settings each was made under.

    An ID and a date are not enough to choose between two calibrations -
    what separates them is gain, integration time and how many repeats
    they averaged, so those are on the same line.
    """
    print("{:<4} {:<40} {:<20} {:<7} {:<7} {:<4} {}".format(
        "#", "Calibration ID", "Created", "Gain", "Cycles", "Rep", "Status"
    ))

    for index, entry in enumerate(entries, start=1):
        created = str(entry.get("created_at") or "-")

        print("{:<4} {:<40} {:<20} {:<7} {:<7} {:<4} {}".format(
            index,
            str(entry.get("calibration_id"))[:40],
            created[:19].replace("T", " "),
            str(entry.get("gain_x") or "-"),
            str(entry.get("integration_cycles") or "-"),
            str(entry.get("repeats") or "-"),
            calibration_state(entry),
        ))


def show_calibration_history(mission):
    banner("CALIBRATION HISTORY")

    entries = mission.calibrations.history()

    print("Library: {}".format(mission.calibrations.path))
    print("Stored:  {} calibration(s)".format(len(entries)))
    print()

    if entries:
        print_calibration_list(entries)
    else:
        print("No full spectral calibration has been created yet.")

    print()
    print("LEGACY DATABASE CALIBRATION (separate, protected)")
    print()
    print("  {}".format(
        mission.references.calibration_id if mission.references else "-"
    ))
    print("  Never listed above and never selectable: it is the White and")
    print("  Dark DB1 was measured against, and the only calibration DB1")
    print("  may be compared with.")
    print()
    print("Stored calibrations are immutable. [7] Select Which Calibration")
    print("To Use changes which one is in force; it rewrites none of them.")
    print()
    pause()


def menu_select_calibration(mission):
    """
    Choose which stored calibration is in force. Measures nothing.

    The reason this screen exists: a calibration survives a restart, so
    an operator coming back to the instrument should be able to pick
    yesterday's rather than being pushed into making a new one every
    session. Making a new calibration is a deliberate act, not a
    consequence of starting the program.
    """
    while True:
        banner("SELECT WHICH CALIBRATION TO USE")

        entries = mission.calibrations.history()

        if not entries:
            print("No full spectral calibration has been made yet.")
            print()
            print("Run [3] Full Spectral Calibration to create the first")
            print("one. Until then UV and IR reflectance are not computed")
            print("and the legacy DB1 comparison runs on its own.")
            print()
            pause()

            return

        print("Library: {}".format(mission.calibrations.path))
        print()
        print_calibration_list(entries)
        print()
        print("A calibration marked INVALID failed its scientific checks")
        print("and cannot be activated. It is kept as engineering data.")
        print()

        choice = ask_int(
            "Calibration to use", 1, len(entries), default=None
        )

        if choice is None:
            print("Cancelled - the active calibration is unchanged.")
            print()
            pause()

            return

        entry = entries[choice - 1]

        if entry.get("active"):
            print()
            print("{} is already active.".format(entry["calibration_id"]))
            print()
            pause()

            return

        # The validator is injected: BD stores calibrations but must not
        # import the science layer, so PC supplies the scientific
        # judgement that decides whether this one may become active.
        try:
            mission.calibrations.activate(
                entry["calibration_id"], validate_calibration
            )

        except CalibrationError as error:
            print()
            print("!! NOT ACTIVATED: {}".format(error.message))
            print("   The previous calibration is still in force.")
            print()
            pause()

            continue

        mission.load_science()

        print()
        print("Active calibration is now {}.".format(
            entry["calibration_id"]
        ))
        print("Created {}.".format(entry.get("created_at")))
        print("The legacy database calibration is unchanged.")
        print()
        pause()

        return


# ======================================================================
# screens: carousel
# ======================================================================
