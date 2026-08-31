"""
Turning results into something a person can read.

Formatting only. Nothing here computes a metric, decides anything or
writes a file: every number printed was produced by Science, and every
verdict shown was reached by the Decision Model. If a value has to be
derived to be displayed, it is derived in the layer that owns it and
passed in.

That separation is what keeps the tables honest. A display function
that quietly rounds, clamps or recomputes a similarity is a second
scientific implementation with no tests and no version number.

Nothing here writes a report either. A structured Sample record is this
project's final product; turning one into prose is somebody else's job
and deliberately outside the repository.
"""

from BD.channels import CHANNELS, ILLUMINATIONS, WAVELENGTHS

# AS `ui_status`, NOT `status`. Three functions in this file bind a
# local named `status` to the get_status dict, exactly as six of them
# once bound `carousel` and shadowed the carousel module. A plain
# `from workflow import status` would make `status.print_failure` read
# as a call into this module in some scopes and a dict lookup in
# others, decided by which function you are standing in.
from workflow import status as ui_status

from workflow.prompts import (
    RULE,
    RULE,
    banner,
    choose,
    number,
    pause,
    score,
)

import json
import sys
import textwrap

SLOT_COUNT = 4

from serial_link import DeviceError, LinkError

from Science import class_models, pipeline, quality

from BD.samples import (
    ACQUISITION_SUCCESS,
    STATE_EMPTY,
    STATE_LOADED,
    STATE_MEASURED,
    STATE_READY_TO_LOAD,
)


def report_link_error(error):
    """
    A refusal, and what it means for the mechanism. Said once.

    THE SECOND HALF IS THE MISSION-CRITICAL HALF.

    The operator is standing at the rover. What they do next - open the
    lid, reach for a slot, assume the sample is still under the loader -
    depends entirely on whether the carousel moved. This screen once
    printed "carousel: nothing was moved" whenever the firmware did not
    explicitly say otherwise, which on the Linux bench meant it printed
    it after a half turn that had visibly happened.

    So the claim is only made when the firmware makes it. Silence is
    reported as uncertainty, never as stillness.

    THE FORMATTING NOW LIVES IN workflow.status. This function used to
    hold four branches that each rewrote the same two fields into a
    different paragraph, beneath a message that had already said it -
    the refusal an operator showed us ran to eleven lines and repeated
    "the position is unknown" three times in three different wordings.
    `status.print_failure` renders the evidence as an aligned block and
    the consequence as one line. §3.
    """
    data = error.data or {}

    ui_status.print_failure(
        error.code, data, message=error.message,
        title="{}{}".format(
            error.code,
            "  ({})".format(data["phase"]) if data.get("phase") else "",
        ),
    )


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


def print_system_status(mission):
    """PC, BD and ESP32 in one place. A failure in one still shows the rest."""
    banner("SYSTEM STATUS")

    print("PC")
    print("Connection:       {}".format(
        "ONLINE" if mission.link.online else "UNKNOWN"
    ))

    # THE TWO PC COLLECTIONS, COUNTED SEPARATELY.
    #
    # One "Samples: 12" line cannot say whether those twelve are a run
    # in progress or twelve records the operator has decided to keep,
    # and those call for opposite responses at the end of a mission.
    try:
        session = mission.session.status()
        archive = mission.archive.status()

    except Exception as error:
        session = archive = None

        print("Sample storage:   ERROR")
        print("Storage error:    {}: {}".format(
            type(error).__name__, error))

    if session and archive:
        print("Sample storage:   READY")
        print("Sample database:  {}".format(archive["path"]))
        print("Schema version:   {}".format(archive["schema_version"]))
        print()

        # The three record layers, counted separately. "12 samples"
        # alone cannot distinguish twelve measured samples from twelve
        # prepared ones that were never measured.
        for label, entry in (("Session (this run)", session),
                             ("Archive (permanent)", archive)):
            print("  {:<20}{} sample(s), {} measurement(s), {} "
                  "analysis run(s)".format(
                      label, entry["samples"], entry["measurements"],
                      entry["analysis_runs"]))

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
        print("  protected:      YES - DB1.json depends on it")

        if refs["zero_denominator_channels"]:
            print("  warning: White == Dark on {}".format(
                ",".join(refs["zero_denominator_channels"])
            ))

    else:
        print("Legacy cal:       ERROR - {}".format(mission.science_error))

    print("Active cal:       {}".format(health["active"]))

    if mission.active_calibration is not None:
        print("  id:             {}".format(health["active_id"]))
        print("  created:        {}".format(
            mission.active_calibration.created_at
        ))
        print("  illuminations:  WHITE + UV + IR")

    else:
        print("  {}".format(
            mission.calibration_error or "not created yet"
        ))

    print("  library:        {} calibration(s) in {}".format(
        health["stored"], mission.calibrations.path.name
    ))

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
    print("DECISION MODEL")

    if mission.decision_engine is not None:
        print("Model:            {} ({})".format(
            mission.decision_engine.version, mission.decision_engine.kind
        ))

    else:
        print("Model:            UNAVAILABLE - {}".format(
            mission.learning_error
        ))

    if mission.learning is not None:
        learning = mission.learning.status()

        print("Learning history: {} observation(s), {} verified".format(
            learning["observations"], learning["by_verification"]["VERIFIED"]
        ))
        print("  file:           {}".format(learning["file"]))
        print("  materials with verified measurements: {}".format(
            len(learning["verified_materials"])
        ))

    if mission.class_snapshot is not None:
        coverage = class_models.coverage(mission.class_snapshot)

        print("Class models:     {} material(s), {} with measurable "
              "scatter".format(
                  mission.class_snapshot["materials"],
                  coverage["with_scatter"],
              ))
        print("  snapshot:       {}".format(
            mission.class_snapshot["snapshot_id"]
        ))

    if mission.taxonomy is not None:
        taxonomy = mission.taxonomy.status()

        print("Vocabulary:       {} material(s) in {} families".format(
            taxonomy["materials"], taxonomy["families"]
        ))

    if mission.registry is not None:
        print()
        print("DATABASES  (scored separately, never pooled)")
        print()

        for line in mission.registry.summary().splitlines():
            print("  {}".format(line))

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
    # The firmware's own three-state answer, not the boolean. READY,
    # UNAVAILABLE and NOT INITIALIZED are different situations and only
    # two of them are faults - a lazily-initialised sensor that nothing
    # has asked for yet was being reported as broken. §8.
    print("Sensor:           {}".format(ui_status.sensor_label(status)))
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

    # `first_init_error` IS THE KEY THE FIRMWARE SENDS.
    #
    # Both branches read `sensor["boot_error"]`, and no firmware has
    # ever produced that name - AS7265x.status() has always called it
    # `first_init_error`. So both were dead: a sensor that failed to
    # initialise printed a bare "UNAVAILABLE" and the code saying why
    # was silently dropped, on every screen, for the whole release.
    #
    # A boot failure that was later recovered is a warning, never a
    # reason to report the sensor as unavailable now.
    first_error = sensor.get("first_init_error")

    if first_error and sensor.get("ready"):
        print("Boot warning:     {} (recovered {}x)".format(
            first_error.get("code"), sensor.get("recovery_count")
        ))

    elif first_error:
        print("Boot error:       {} - {}".format(
            first_error.get("code"), first_error.get("message"),
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

    geometry = carousel.get("geometry") or {}

    # `reference`, NOT `encoder`. Carousel.status() has never sent an
    # `encoder` block - the origin and the drift live under `reference`,
    # and the origin counts one level deeper again. So this screen
    # printed "Origin: None counts", "Alignment offset: None counts
    # (None deg)", and never printed drift at all, because the test
    # that gates it read a key that could not exist.
    reference = carousel.get("reference") or {}
    origin = reference.get("origin") or {}

    # `counts_per_slot` is not in the geometry block - it never was.
    # The carousel reports its geometry in DEGREES, which is what the
    # mechanism is defined in; counts are the servo's unit and are
    # printed in the SERVO block below, from the backend that owns
    # them. This line used to end "None counts".
    print("Slots:            {} ({:.0f} deg apart, half turn {:.0f} deg)"
          .format(
              carousel.get("slot_count", SLOT_COUNT),
              geometry.get("slot_spacing_deg", 360.0 / SLOT_COUNT),
              geometry.get("half_turn_deg", 180.0),
          ))
    print("Synchronized:     {}".format(
        carousel.get("position_state")
        or ("YES" if carousel.get("position_valid") else "NO")
    ))

    # THE CAROUSEL COORDINATE, ON ITS OWN LINE, ABOVE THE RAW COUNTS.
    #
    # Zero at the operator's own reference whatever the servo reads
    # there. The two `Encoder`/`Origin` lines below are servo-frame
    # telemetry and say so: the origin used to be printed as
    # "N counts (M deg)", and that M was the raw count in degrees,
    # which is a number about the servo and not about the carousel.
    print("Carousel angle:   {}".format(
        "no origin - re-sync to set 0 deg"
        if carousel.get("angle_deg") is None
        else "{:+.1f} deg".format(carousel["angle_deg"])
    ))
    print("Selected slot:    {}".format(carousel.get("selected_slot")))
    print("Loader:           {}".format(carousel.get("current_load_slot")))
    print("Scanner:          {}".format(carousel.get("current_scan_slot")))

    if origin.get("feedback"):
        print("Encoder raw:      {} counts".format(
            reference.get("encoder_counts")
        ))
        print("Origin raw:       {} counts".format(
            origin.get("origin_counts")
        ))

    else:
        print("Origin:           not captured")

    # Degrees only: the firmware reports the offset and the drift in
    # degrees and does NOT send a counts form of either. Printing a
    # counts column the board never sent is what produced the two
    # "None counts" lines this replaces.
    print("Alignment offset: {} deg".format(
        reference.get("alignment_offset_deg")
    ))

    if reference.get("drift_measurable"):
        print("Drift vs nominal: {} deg (commanded {}, expected {})".format(
            reference.get("drift_deg"),
            reference.get("commanded_travel_deg"),
            reference.get("expected_travel_deg"),
        ))

    if geometry.get("error"):
        print("GEOMETRY ERROR:   {}".format(geometry["error"]))

    print()
    print("SERVO")

    # Which actuator is in charge is the single most important thing an
    # operator can be told about the carousel, so it is never hidden
    # behind an error.
    servo = status.get("servo") or {}
    backend = servo.get("backend") or {}

    print_servo_block(servo)

    # Same dead key: this warning could never fire, so a servo that was
    # attached but silent showed no "NOT ANSWERING" line at all.
    if servo.get("connected") and not backend.get("connected"):
        error = backend.get("error") or {}

        if error:
            print("  ** NOT ANSWERING: {} - {}".format(
                error.get("code"), error.get("message")
            ))
            print("  The ST3215 subsystem is powered externally; check that")
            print("  supply, the common ground and the TX/RX pair first.")

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

    for channel in pipeline.CHANNELS:
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

    for channel in pipeline.CHANNELS:
        print("{:<4} {:>10} {:>10} {:>10} {:>12} {:>12}".format(
            channel,
            number(raw.get(channel)),
            number(dark.get(channel)),
            number(white.get(channel)),
            number(corrected.get(channel)),
            number(normalized.get(channel), 6),
        ))


def print_triad_table(raw, normalized=None, wavelengths=None):
    """
    RAW by illumination, with reflectance beside it where it exists.

    `raw` is the block the record stores: {"white": {...}, "uv": {...},
    "ir": {...}}. A legacy 18-channel measurement has only "white" and
    prints one column - it is not a degraded 54-feature measurement, it
    is a complete measurement of a different kind, and the table says
    so rather than showing two columns of dashes.

    `normalized` is the CALCULATED reflectance from an AnalysisRun, in
    the same shape. It is optional because RAW exists without it: a
    measurement whose analysis has not run, or failed, still has every
    count the detector reported.
    """
    raw = raw or {}

    if not isinstance(raw, dict) or not raw:
        print("  no spectrum")

        return

    wavelengths = wavelengths or WAVELENGTHS

    present = [name for name in ILLUMINATIONS if raw.get(name)]

    if not present:
        # Not grouped by illumination - a bare 18-channel spectrum.
        present = ["white"]
        raw = {"white": raw}

    normalized = normalized or {}
    have_normalized = any(normalized.get(name) for name in present)

    header = "CH   nm   " + "".join(
        "{:>12}".format(name.upper() + " raw") for name in present
    )

    if have_normalized:
        header += "  " + "".join(
            "{:>10}".format("R " + name) for name in present
        )

    print(header)

    for channel in CHANNELS:
        row = "{:<4} {:<5}".format(channel, wavelengths.get(channel, "-"))

        for name in present:
            row += "{:>12}".format(
                number((raw.get(name) or {}).get(channel)))

        if have_normalized:
            row += "  "

            for name in present:
                row += "{:>10}".format(
                    number((normalized.get(name) or {}).get(channel), 4))

        print(row)

    if len(present) == 1 and present[0] == "white":
        print()
        print("  One illumination (WHITE): 18 channels. A legacy "
              "measurement,")
        print("  and complete as one - not a 54-feature measurement with "
              "parts missing.")


def _print_quality_checks(report):
    """The checks that are not PASS, from one quality sub-report."""
    for check in report.get("checks") or []:
        if check.get("status") == "PASS":
            continue

        print("  [{}] {}: {}".format(
            check.get("status"), check.get("check"), check.get("message")
        ))


def print_quality(report):
    """
    Measurement quality: the hardware verdict and the normalization one.

    THE REPORT IS SPLIT, AND THIS PRINTED THE OLD FLAT SHAPE.

    `Science.pipeline.split_quality` deliberately separates the two -
    "the checks were always right, it was the single combined verdict
    that was wrong" - so the current report has no top-level `status`
    and no top-level `checks`. This asked for both, so every screen
    that shows measurement quality printed

        Overall: None

    and then listed nothing, because the loop was iterating an empty
    list. The reasons a measurement was degraded were computed on every
    single analysis and shown on none of them.

    The two verdicts are printed separately because that is the whole
    point of the split: a hardware FAIL means the numbers are suspect,
    while a normalization problem lowers the weight of what is derived
    from them and leaves the raw measurement intact.
    """
    if not report:
        return

    hardware = report.get("hardware")
    normalization = report.get("normalization")

    if hardware or normalization:
        if hardware:
            print("Hardware:      {}".format(hardware.get("status")))
            _print_quality_checks(hardware)

        if normalization:
            print("Normalization: {}".format(normalization.get("status")))
            _print_quality_checks(normalization)

        usable = report.get("usable_channels")

        if usable is not None and len(usable) < len(CHANNELS):
            excluded = [c for c in CHANNELS if c not in usable]

            print("  Channels excluded from comparison: {}".format(
                ",".join(excluded)))

        return

    # A record migrated from the flat schema still carries the old
    # single verdict, and is still readable in the records browser.
    print("Overall: {}".format(report.get("status")))

    _print_quality_checks(report)

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
            rank_of(match),
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


def rank_of(match):
    """
    A match's rank, as something that can always be printed.

    THE DEFECT THIS EXISTS FOR.

    Three tables formatted `match.get("rank")` straight into a `{:<4}`
    field. `"{:<4}".format(None)` raises TypeError, and None is exactly
    what that lookup returns for a record MIGRATED from the old flat
    schema: `reference_matches` there was whatever the previous
    software stored, and it has no rank. So opening a legacy sample in
    the records browser crashed the screen.

    `score()` and `number()` beside this already return "-" for
    anything that is not a number; rank simply never got the same
    treatment. Note that `.get("combined_rank", match.get("rank"))`
    does NOT protect against this on its own - the default only applies
    when the key is absent, not when it is present and null.
    """
    for key in ("combined_rank", "rank"):
        value = match.get(key)

        if isinstance(value, int) and not isinstance(value, bool):
            return value

        if isinstance(value, str) and value.strip():
            return value.strip()[:6]

    return "-"


def print_matches(matches, limit=None):
    if not matches:
        print("No reference materials were compared.")

        return

    shown = matches if limit is None else matches[:limit]

    print("{:<4} {:<32} {:>12}".format("#", "Material", "Similarity"))

    for match in shown:
        print("{:<4} {:<32} {:>12}".format(
            rank_of(match),
            str(match.get("material"))[:32],
            score(match.get("similarity_percent")),
        ))

    if limit is not None and len(matches) > limit:
        print("... {} more (all stored with the sample)".format(
            len(matches) - limit
        ))


DATABASE_LABELS = {
    "DB1": "measured here, 18 bands",
    "DB2": "measured here, 54 features",
    "DB3": "external spectra, projected",
}

# What KIND of evidence each database is. Not decoration: a cosine of
# 0.97 against DB1 means "this looks like something we measured on this
# instrument", and the same number against DB3 means "this looks like a
# laboratory spectrum, after modelling our sensor". Those are different
# claims and the screen has to say which one it is showing.
DATABASE_EVIDENCE = {
    "MEASURED": "measured on THIS instrument",
    "REFERENCE_PROJECTED": "external laboratory spectra, projected",
    "DERIVED_REFERENCE": "external laboratory spectra, projected",
}


def print_database_results(database_results, metrics=("cosine", "rmse",
                                                      "pearson_r"),
                           show_key=True):
    """
    What each database says, per metric, with its margin.

    ONE BLOCK PER DATABASE, never a single ranked list. The databases
    answer different questions - DB1 from spectra measured on this
    instrument, DB3 from laboratory spectra put through a model of it -
    so their scores are shown side by side and never averaged into a
    number that would mean nothing. DISAGREEMENT BETWEEN THEM IS
    EVIDENCE and is left visible.

    THE MARGIN IS THE POINT. A winner is only worth reading next to the
    distance to the runner-up: two materials separated by 0.004 cosine
    is not an identification, and printing only the winner is how a 97%
    similarity came to look like a confident answer.

    Each block says, before any number: which database, which version,
    what kind of evidence it is, how many materials were in the field,
    how many features were actually compared, and which normalization
    the measurement went through to be comparable with it. All six are
    needed to read the numbers underneath - a cosine over 3 shared
    channels is not the same measurement as a cosine over 54.
    """
    if not database_results:
        print("  No database comparison was made.")

        return

    unavailable = []

    for entry in database_results:
        name = entry.get("database")
        status = entry.get("status")

        if status != "READY" or not entry.get("metrics"):
            unavailable.append(entry)

            continue

        print("{}  {}".format(name, entry.get("version")))
        print("     {}".format(
            DATABASE_EVIDENCE.get(entry.get("evidence"))
            or entry.get("evidence")
            or DATABASE_LABELS.get(name, "")
        ))
        print("     {} material(s) compared over {} feature(s) of {}".format(
            entry.get("candidate_count"),
            entry.get("channels_compared"),
            entry.get("feature_space"),
        ))
        print("     normalized with {}".format(
            entry.get("normalization") or "-"
        ))
        print()
        print("     {:<12} {:<26} {:>10} {:>10}".format(
            "metric", "winner", "score", "margin"))

        for metric in metrics:
            summary = (entry.get("metrics") or {}).get(metric)

            if not summary:
                continue

            print("     {:<12} {:<26} {:>10} {:>10}".format(
                metric,
                str(summary.get("winner"))[:26],
                number(summary.get("winner_score")),
                number(summary.get("absolute_margin")),
            ))

        # WHERE THIS DATABASE DISAGREES WITH ITSELF. Three metrics that
        # name three different materials is a finding, and reading three
        # rows and noticing it is work the screen can do.
        winners = {
            (entry.get("metrics") or {}).get(metric, {}).get("winner")
            for metric in metrics
            if (entry.get("metrics") or {}).get(metric)
        }
        winners.discard(None)

        if len(winners) > 1:
            print()
            print("     METRICS DISAGREE inside {}: {}".format(
                name, ", ".join(sorted(str(w) for w in winners))))

        print()

    for entry in unavailable:
        print("{}  {}".format(entry.get("database"), entry.get("status")))

        reason = entry.get("reason") or (
            "not available" if not entry.get("available") else None
        )

        if reason:
            for line in textwrap.wrap(str(reason), 64):
                print("     {}".format(line))

        print()

    if show_key:
        for line in textwrap.wrap(METRIC_NOTE, 66):
            print("  {}".format(line))


DECISION_HEADLINE = {
    "KNOWN_MATERIAL": "KNOWN MATERIAL",
    "MATERIAL_FAMILY": "MATERIAL FAMILY",
    "AMBIGUOUS_SET": "AMBIGUOUS SET",
    "UNKNOWN": "UNKNOWN",
}


def print_evidence_summary(package):
    """The measurement half: what the instrument actually supports."""
    if not package:
        return

    # NOT `quality`. The Science.quality MODULE is imported at the top
    # of this file, and a local of that name shadowed it: the
    # quality.summarize() call at the end of this function became
    # dict.summarize() and raised AttributeError. It only fires when
    # the QC is imperfect - which is every real measurement that is not
    # a clean one - so the screen worked in the one case nobody needed
    # it to.
    quality_report = package.get("quality") or {}
    reliability = package.get("channel_reliability") or {}

    hardware = (quality_report.get("hardware") or {}).get("status")
    normalization = (quality_report.get("normalization") or {}).get("status")

    print("MEASUREMENT")
    print()
    print("  Hardware QC:    {}".format(hardware))
    print("  Normalization:  {}".format(normalization))
    print("  Reliable features: {}/{} raw, {}/{} usable as reflectance".format(
        reliability.get("raw_valid_total"),
        reliability.get("features_total"),
        reliability.get("normalized_valid_total"),
        reliability.get("features_total"),
    ))

    for illumination in ILLUMINATIONS:
        entry = (reliability.get("by_illumination") or {}).get(illumination)

        if not entry:
            continue

        print("    {:<6} raw {}/18, reflectance {}/18".format(
            illumination.upper(),
            entry["raw_valid_channels"],
            entry["normalized_valid_channels"],
        ))

    if hardware != "PASS" or normalization != "OK":
        print()

        for line in textwrap.wrap(
            quality.summarize(reliability), 66
        ):
            print("  {}".format(line))


def print_decision(decision, detail=False):
    """
    The conclusion, in the four-level vocabulary and nothing else.

    Deliberately compact by default: the operator needs the level, the
    answer and the confidence at a glance, and everything behind it on
    request.
    """
    if not decision:
        return

    level = decision.get("level")

    print("DECISION MODEL   {}".format(
        decision.get("decision_model_version")
    ))
    print()
    print("  Level:       {}".format(DECISION_HEADLINE.get(level, level)))

    if decision.get("material"):
        print("  Material:    {}".format(decision["material"]))

    if decision.get("family"):
        print("  Family:      {}".format(decision["family"]))

    print("  Confidence:  {}".format(decision.get("confidence")))

    candidates = decision.get("candidates") or []

    if candidates:
        print()
        print("  Candidates:")

        for index, candidate in enumerate(candidates, start=1):
            # `or "-"`, not `.get(key, "-")`. The default form only
            # applies when the KEY IS ABSENT; a key that is present and
            # null still returns None, and `"{:<7}".format(None)`
            # raises. Decisions are rendered from STORED AnalysisRuns
            # as well as fresh ones, so a field an older Science
            # version left null reaches this line years later - the
            # same blind spot that crashed the match tables on
            # migrated records.
            print("    {}. {:<30} {:<7} {}".format(
                index,
                str(candidate.get("material"))[:30],
                candidate.get("evidence_level") or "-",
                candidate.get("family") or "",
            ))

    secondary = decision.get("secondary_interpretations") or []

    if secondary:
        print()
        print("  Also: {}".format(", ".join(secondary)))

    if decision.get("reason"):
        print()

        for line in textwrap.wrap("Why: {}".format(decision["reason"]), 66):
            print("  {}".format(line))

    if not detail:
        return

    print_decision_detail(decision)


def print_decision_detail(decision):
    """
    The complete evidence trace: how this conclusion was actually reached.

    EVERY NUMBER HERE WAS COMPUTED BY THE DECISION MODEL. Nothing is
    re-derived for the screen and nothing is a plausible-sounding
    reconstruction: the gates are the ladder's own branches with their
    own thresholds, the per-database support is the fusion table, and
    the confidence penalties are the same conditions `_confidence`
    counted. If the model did not record something, this says so rather
    than inventing it.

    Read top to bottom it answers, in order: what was concluded, how
    much measurement it rests on, what each database contributed, which
    candidates were in play and what supported each, which gate stopped
    the answer where it stopped, and why the confidence is what it is.
    """
    evidence = decision.get("evidence") or {}

    # ---- coverage ----------------------------------------------------
    coverage = evidence.get("coverage") or {}

    print()
    print("  EVIDENCE COVERAGE")
    print()
    print("    Hardware QC:        {}".format(coverage.get("hardware_qc")))
    print("    Normalization:      {}".format(
        coverage.get("normalization")))
    print("    Raw features:       {} of {}".format(
        coverage.get("raw_valid_total"), coverage.get("features_total")))
    print("    Usable reflectance: {} of {}".format(
        coverage.get("normalized_valid_total"),
        coverage.get("features_total"),
    ))

    for illumination, entry in sorted(
        (coverage.get("by_illumination") or {}).items()
    ):
        print("      {:<6} raw {}/18, reflectance {}/18".format(
            illumination.upper(),
            entry.get("raw_valid_channels"),
            entry.get("normalized_valid_channels"),
        ))

    # ---- what each database contributed ------------------------------
    print()
    print("  DATABASE CONTRIBUTIONS")

    databases = evidence.get("databases") or {}

    if not databases:
        print()
        print("    No database produced usable support.")

    for key, entry in sorted(databases.items()):
        print()
        print("    {} (database weight {})".format(
            key, entry.get("database_weight")))

        for family, summary in sorted((entry.get("families") or {}).items()):
            print("      {:<14} best {:<26} margin {:<9} z {}".format(
                family,
                str(summary.get("winner"))[:26],
                (
                    "{:.4f}".format(summary["absolute_margin"])
                    if summary.get("absolute_margin") is not None else "-"
                ),
                (
                    "{:.1f}".format(summary["z_separation"])
                    if summary.get("z_separation") is not None else "-"
                ),
            ))

        supported = sorted(
            (entry.get("candidates") or {}).items(),
            key=lambda item: -(item[1].get("support") or 0.0),
        )[:4]

        if supported:
            print("      strongest support from this database:")

            for material, support in supported:
                print("        {:<28} {:<8} {}".format(
                    str(material)[:28],
                    number(support.get("support")),
                    "" if support.get("votes")
                    else "DISCOUNTED - class reliability {}".format(
                        support.get("class_reliability")),
                ))

    discounted = evidence.get("discounted") or []

    if discounted:
        by_reason = {}

        for entry in discounted:
            key = (entry["database"], entry["reason"])
            by_reason.setdefault(key, []).append(entry["material"])

        print()
        print("    DISCOUNTED - named by a database, not counted")

        for (database, reason), materials in sorted(by_reason.items()):
            print()
            print("      {} x{}: {}".format(
                database, len(materials), reason))

            for material in materials[:DISCOUNTED_SHOWN]:
                print("        {}".format(material))

            if len(materials) > DISCOUNTED_SHOWN:
                print("        ... and {} more".format(
                    len(materials) - DISCOUNTED_SHOWN))

    # ---- candidate by candidate --------------------------------------
    print()

    nearest_known = decision.get("candidates_are") == "NEAREST_KNOWN"

    if nearest_known:
        print("  NEAREST KNOWN MATERIALS")
        print()

        for line in textwrap.wrap(
            "Not candidates. The decision was UNKNOWN, so these are the "
            "closest things in the libraries - context, not a claim "
            "about the sample. No per-database support was fused for "
            "them, because none of them was being judged.", 66
        ):
            print("    {}".format(line))

    else:
        print("  CANDIDATE EVIDENCE")

    candidates = decision.get("candidates") or []

    if not candidates:
        print()
        print("    No candidate was carried forward.")

    for index, candidate in enumerate(candidates, start=1):
        print()
        print("    {}. {}   strength {}  ({})".format(
            index,
            candidate.get("material"),
            number(candidate.get("evidence_strength")),
            candidate.get("evidence_level") or "-",
        ))
        print("       family: {}".format(candidate.get("family") or "-"))

        if not nearest_known:
            print("       supported by {} of the databases: {}".format(
                candidate.get("independent_sources")
                if candidate.get("independent_sources") is not None
                else len(candidate.get("supporting_databases") or []),
                ", ".join(candidate.get("supporting_databases") or [])
                or "none",
            ))

        for key, entry in sorted(
            (candidate.get("per_database") or {}).items()
        ):
            print("         {:<5} support {:<8} agreeing metrics {}".format(
                key,
                number(entry.get("support")),
                entry.get("family_agreement"),
            ))

        class_evidence = candidate.get("class_evidence") or {}

        if class_evidence.get("support") is not None:
            print("       class distance: support {} ({}x the class's own "
                  "spread, {} independent measurement(s))".format(
                      number(class_evidence.get("support")),
                      number(class_evidence.get("within_class_ratio")),
                      class_evidence.get("n_independent"),
                  ))

        elif class_evidence.get("reason"):
            print("       class distance: {}".format(
                class_evidence["reason"]))

    # ---- the gates ---------------------------------------------------
    print()
    print("  DECISION GATES")
    print()
    print("    Each step of the ladder, in the order it was walked.")
    print("    NOT_REACHED means the ladder had already answered.")
    print()

    for gate in decision.get("gates") or []:
        print("    {:<20} {}".format(gate.get("gate"), gate.get("verdict")))

        for line in textwrap.wrap(str(gate.get("detail") or ""), 58):
            print("        {}".format(line))

    if not decision.get("gates"):
        print("    This decision was recorded before the gates were kept.")

    # ---- confidence and abstention -----------------------------------
    print()
    print("  CONFIDENCE   {}".format(decision.get("confidence")))
    print()

    reasons = decision.get("confidence_reasons")
    level = decision.get("level")

    if reasons:
        print("    What counted against it:")
        print()

        for reason in reasons:
            print("    -{}  {}".format(reason["penalty"], reason["code"]))

            for line in textwrap.wrap(str(reason.get("detail") or ""), 58):
                print("        {}".format(line))

    elif reasons is not None:
        print("    Nothing counted against it.")

    else:
        print("    This decision was recorded before the penalties were "
              "kept.")

    # WHY IT IS NOT HIGHER. The penalty list alone cannot say: the
    # LEVEL caps the confidence before any penalty is counted, so a
    # clean AMBIGUOUS_SET with nothing against it still reads MEDIUM -
    # and "nothing counted against it" beside "MEDIUM" looks like a
    # contradiction until the ceiling is stated.
    if reasons is not None and level != "KNOWN_MATERIAL":
        print()

        ceiling = (
            "An UNKNOWN conclusion has named nothing, so its confidence "
            "is NONE whatever the evidence looked like - there is no "
            "answer for it to be confident about."
            if level == "UNKNOWN" else
            "HIGH confidence is reachable only from a KNOWN_MATERIAL "
            "conclusion. This one is {}, so MEDIUM is the ceiling "
            "however clean the evidence is.".format(level)
        )

        for line in textwrap.wrap(ceiling, 58):
            print("    {}".format(line))

    unknown = evidence.get("unknown_detection") or {}

    if unknown.get("reasons"):
        print()
        print("  ABSTENTION / UNCERTAINTY")
        print()
        print("    {} severe and {} moderate doubt(s); {} moderate ones "
              "force UNKNOWN.".format(
                  unknown.get("severe"), unknown.get("moderate"),
                  (unknown.get("thresholds") or {}).get(
                      "moderate_reasons_for_unknown", 2),
              ))
        print()

        for reason in unknown["reasons"]:
            print("    [{}] {}".format(reason["severity"], reason["code"]))

            for line in textwrap.wrap(str(reason.get("detail") or ""), 58):
                print("        {}".format(line))

    # ---- how the metrics read ----------------------------------------
    print()
    print("  READING THE NUMBERS")
    print()

    for metric, (direction, note) in sorted(METRIC_DIRECTION.items()):
        if metric not in ("cosine", "rmse", "pearson_r"):
            continue

        print("    {:<12} {:<18} {}".format(metric, direction, note))

    print("    {:<12} {}".format(
        "margin", "distance to the runner-up in that same metric"))
    print("    {:<12} {}".format(
        "strength", "0..1 fused support; not a probability"))

    # ---- explanation and provenance ----------------------------------
    print()
    print("  EXPLANATION")
    print()

    for line in textwrap.wrap(decision.get("explanation") or "", 66):
        print("    {}".format(line))

    print()
    print("  PROVENANCE")
    print()

    provenance = decision.get("provenance") or {}

    for key in ("decision_model_version", "evidence_schema_version",
                "calibration_id", "legacy_calibration_id",
                "acquisition_profile_id", "class_statistics_snapshot"):
        print("    {:<28} {}".format(key, provenance.get(key)))

    for key, entry in sorted(
        (provenance.get("database_versions") or {}).items()
    ):
        print("    {:<28} {} ({}, {} materials)".format(
            key, entry.get("version"), entry.get("status"),
            entry.get("materials"),
        ))


# ======================================================================
# ONE SCIENTIFIC RESULT, ONE RENDERER
# ======================================================================
# The Sensor + Analysis Test and a production measurement produce the
# SAME object: an AnalysisRun. They used to render it with two separate
# stretches of code, and the two had already drifted - the sensor test
# printed the calibration in force, the full spectral table and, most
# importantly, DATABASE COMPARISON, while the production screen printed
# neither the calibration nor a single word about what DB1, DB2 and DB3
# had each said. An operator running a real measurement therefore saw
# less about it than one running a bench test.
#
# `print_science_result` is now the only place a completed analysis is
# rendered. Both screens call it, so a change to the spectral table or
# to the database block reaches both or neither, and
# `regression/test_science_display.py` asserts that the two call it
# rather than reimplementing it.
#
# What is NOT here: everything mission-specific. Sample ID, slot,
# measurement id, the movement result, whether the carousel came home,
# what was persisted where. Those belong to the workflow that produced
# the acquisition and are printed by it, above this.

METRIC_DIRECTION = {
    "cosine": ("higher is better", "1.0 is an identical shape"),
    "rmse": ("lower is better", "0.0 is an identical spectrum"),
    "pearson_r": ("higher is better", "+1.0 is perfect correlation"),
    "mae": ("lower is better", "mean absolute error"),
    "euclidean": ("lower is better", "distance in reflectance units"),
    "spectral_angle_deg": ("lower is better", "0 deg is identical shape"),
}

# How many discounted materials to name before summarising. Enough
# to recognise the pattern, not enough to bury the candidates under
# it - DB3 routinely discounts a dozen.
DISCOUNTED_SHOWN = 3

METRIC_NOTE = (
    "cosine and pearson_r: HIGHER is better.  rmse: LOWER is better.  "
    "margin is the distance to the runner-up in that same metric - a "
    "large score with a tiny margin is not an identification."
)


def print_calibration_line(result):
    """Which calibrations the numbers below were derived under."""
    calibration = (result or {}).get("calibration") or {}

    print("Active calibration:  {}".format(
        calibration.get("calibration_id") or "NONE"))
    print("Legacy calibration:  {}   (DB1 is compared under this and "
          "nothing else)".format(
              calibration.get("legacy_calibration_id") or "NONE"))


def print_science_result(result, settings=None, show_calibration=True,
                         show_spectra=True):
    """
    A completed AnalysisRun, in full, for any acquisition source.

    The order is the order the evidence was produced in, which is also
    the order an operator reads it in: what the instrument was set to,
    what it measured, whether the measurement is usable, what each
    library says about it separately, and only then what the Decision
    Model concluded from all of that.

    `settings` is what the ESP32 reported for THIS acquisition, read
    back from the silicon. Passed in rather than dug out of the run
    because the acquisition knows it first-hand and an AnalysisRun
    carries it only as provenance.
    """
    result = result or {}

    if show_calibration:
        print()
        print("CALIBRATION")
        print()
        print_calibration_line(result)

    print()
    print("SETTINGS")
    print()
    print_settings_block(
        settings
        or ((result.get("evidence") or {}).get("acquisition") or {}).get(
            "sensor_settings"
        )
    )

    print()
    print("MEASUREMENT QUALITY")
    print()

    if result.get("quality"):
        print_quality(result["quality"])

    else:
        print("  not available: {}".format(
            (result.get("error") or {}).get(
                "message", "the analysis did not reach the quality stage")
        ))

    if show_spectra:
        print()
        print("FULL SPECTRAL DATA")
        print()
        print_triad_table(
            (result.get("evidence") or {}).get("raw"),
            (result.get("representations") or {}).get("normalized"),
        )

    print()
    print(RULE)
    print()
    print("DATABASE COMPARISON")
    print()
    print_database_results(result.get("database_results"))

    print()
    print(RULE)
    print()

    if result.get("evidence"):
        print_evidence_summary(result["evidence"])
        print()
        print_decision(result.get("decision"))

    else:
        print("DECISION MODEL")
        print()
        print("  Not run: {}".format(
            (result.get("error") or {}).get("message")
            or "no evidence package could be built"
        ))


def offer_decision_detail(result):
    """'Why?' - the full evidence, on request."""
    decision = (result or {}).get("decision")

    if not decision:
        return

    print()

    if choose("[w] Why?  [Enter] continue").strip().lower() != "w":
        return

    # THE DETAIL ONLY. `print_decision(detail=True)` starts by drawing
    # the compact block again, and the caller has just drawn it - so
    # the operator got the level, the confidence and the candidate list
    # twice, one screen apart, and had to work out that they were the
    # same thing.
    print()
    print(RULE)
    print_decision_detail(decision)
    print()
    pause()


# ----------------------------------------------------------------------
# ground truth capture
# ----------------------------------------------------------------------


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


def print_servo_block(servo):
    """The servo half of a status report, in the active backend's terms."""
    backend = servo.get("backend") or {}

    print("  Servo:         {}".format(servo.get("label")))

    # `connected`, NOT `selected`. ServoLink.status() has never sent a
    # `selected` key, so this early return was taken on EVERY call and
    # the whole block below - link, mode, encoder, voltage, temperature
    # - was unreachable. Against a live answering servo this function
    # printed the label and then the literal word "None", because
    # `message` is only present when nothing is connected.
    if not servo.get("connected"):
        print("  {}".format(
            servo.get("message") or "Not connected."))

        return

    print("  Feedback:      encoder, movement verified")

    print("  Link:          id {} on UART{}, {} baud".format(
        backend.get("id"), backend.get("uart_id"), backend.get("baud")
    ))
    print("  Wiring:        TX GPIO{} -> driver TX, RX GPIO{} -> driver "
          "RX, GND".format(backend.get("tx_pin"), backend.get("rx_pin")))
    print("  Power:         external supply at the driver board")
    print("  Mode:          {} ({})".format(
        backend.get("mode_name"), backend.get("mode")
    ))

    if backend.get("mode_correct") is False:
        print("  ** WRONG MODE - run SERVICE: write servo "
              "configuration **")

    # THE LOGICAL ANGLE FIRST, THE RAW COUNT UNDERNEATH IT, LABELLED.
    #
    # This line used to read "Encoder: 2 cnt / 0.18 deg" and that was
    # the carousel's position as far as any operator could tell. It was
    # not: 0.18 deg is two encoder counts of a servo-frame value with an
    # arbitrary offset, and a carousel that has just been re-synchronized
    # is at 0.0 deg by definition however that value reads.
    angle = backend.get("angle_deg")

    print("  Carousel:      {}".format(
        "no origin - re-sync to set 0 deg" if angle is None
        else "{:+.1f} deg from origin".format(angle)
    ))
    print("  Encoder raw:   {} cnt   origin {} cnt".format(
        backend.get("position_counts"), backend.get("origin_counts"),
    ))
    print("  Moving:        {}".format(backend.get("moving")))
    print("  Torque:        {}".format(backend.get("torque_enabled")))
    print("  Voltage:       {} V".format(backend.get("voltage_v")))
    print("  Temperature:   {} C".format(backend.get("temperature_c")))
    print("  Load:          {} (0.1%)".format(backend.get("load_permille")))
    print("  Current:       {} mA".format(backend.get("current_ma")))

    flags = backend.get("status_flags")

    if flags:
        print("  ** SERVO ALARM: {} **".format(", ".join(flags)))

    bus = backend.get("bus") or {}

    if bus.get("retry") or bus.get("timeout") or bus.get("checksum"):
        print("  Bus:           {} tx, {} rx, {} retries, {} timeouts, "
              "{} checksum".format(
                  bus.get("tx"), bus.get("rx"), bus.get("retry"),
                  bus.get("timeout"), bus.get("checksum"),
              ))


def print_servo_telemetry(backend):
    """
    Everything the servo reports about itself. For DIAGNOSTICS only.

    The status block above is deliberately compact - it is read while
    deciding whether to move a mechanism. This is the engineering view,
    and it is richer on purpose: §9.

    THE BUS COUNTERS ARE ALWAYS SHOWN HERE, unlike in the status block
    where they appear only when something has already gone wrong. A
    clean count is evidence too - it is how you tell "the servo is
    answering wrongly" from "the servo is barely answering at all", and
    that distinction is the whole question when an encoder disagrees
    with what the mechanism visibly did.

    Every field is dropped when the firmware did not report it, so this
    never invents a zero for something it could not read.
    """
    if not backend:
        return

    print()
    print("SERVO TELEMETRY")
    print()

    ui_status.print_fields((
        ("Servo ID", backend.get("id")),
        ("UART", "UART{} TX GPIO{} RX GPIO{} @ {} baud".format(
            backend.get("uart_id"), backend.get("tx_pin"),
            backend.get("rx_pin"), backend.get("baud"),
        )),
        ("Mode", "{} ({}){}".format(
            backend.get("mode_name"), backend.get("mode"),
            "" if backend.get("mode_correct") is not False
            else "  ** EXPECTED {} **".format(backend.get("expected_mode")),
        )),
        ("Torque", backend.get("torque_enabled")),
        ("Moving", backend.get("moving")),

        # THE CAROUSEL COORDINATE AND THE SERVO REGISTERS, IN THAT
        # ORDER AND NEVER CONFLATED.
        #
        # `Carousel` is the logical angle from the operator's own
        # origin, so it reads +0.0 deg the instant a re-sync completes
        # whatever the raw count is. Everything below it is servo-frame
        # engineering data, kept because H-002 was diagnosed from
        # exactly these numbers and the next such problem will be too.
        ("Carousel", "no origin captured"
         if backend.get("angle_deg") is None
         else "{:+.1f} deg from origin".format(backend["angle_deg"])),
        ("Encoder", None if backend.get("position_counts") is None
         else "{} cnt  (measured shaft position)".format(
             backend.get("position_counts"))),
        ("Origin", None if backend.get("origin_counts") is None
         else "{} cnt".format(backend.get("origin_counts"))),

        # The two registers the measurement is derived from. Reported
        # so the derivation can be checked rather than trusted: 67
        # minus 56 is the position, 56 alone is how far the mechanism
        # is from where it was told to be.
        ("Trajectory", None if backend.get("trajectory_counts") is None
         else "{} cnt  (reg 67, commanded, open loop)".format(
             backend.get("trajectory_counts"))),
        ("Follow err", None
         if backend.get("following_error_counts") is None
         else "{} cnt / {} deg  (reg 56{})".format(
             backend.get("following_error_counts"),
             backend.get("following_error_deg"),
             "" if backend.get("position_raw") is None
             else ", raw 0x{:04X}".format(backend["position_raw"]))),
        ("Per rev", backend.get("counts_per_rev")),
        ("Speed", None if backend.get("speed_steps_per_s") is None
         else "{} steps/s".format(backend.get("speed_steps_per_s"))),
        ("Load", None if backend.get("load_permille") is None
         else "{} (0.1%)".format(backend.get("load_permille"))),
        ("Voltage", None if backend.get("voltage_v") is None
         else "{} V".format(backend.get("voltage_v"))),
        ("Current", None if backend.get("current_ma") is None
         else "{} mA".format(backend.get("current_ma"))),
        ("Temp", None if backend.get("temperature_c") is None
         else "{} C".format(backend.get("temperature_c"))),
        ("Flags", ", ".join(backend.get("status_flags") or []) or None),
    ))

    bus = backend.get("bus") or {}

    if bus:
        print()
        print(ui_status.field("Bus", "{} tx / {} rx".format(
            bus.get("tx"), bus.get("rx"))))
        print(ui_status.field("Errors", "{} retries, {} timeouts, "
                              "{} checksum".format(
                                  bus.get("retry"), bus.get("timeout"),
                                  bus.get("checksum"))))

    # THE LAST MOVEMENT, WITH ITS OWN EVIDENCE. What was asked for and
    # what the encoder read, kept side by side - which is the exact
    # comparison a disagreement between the encoder and the mechanism
    # has to be argued from.
    last = backend.get("last_move")

    if isinstance(last, dict) and last:
        print()
        print("LAST MOVEMENT")
        print()

        ui_status.print_fields((
            ("Requested", last.get("requested_counts")),
            ("Encoder", None if last.get("start_position") is None
             else "{} -> {} cnt".format(
                 last.get("start_position"), last.get("actual_position"))),
            ("Expected", last.get("expected_position")),
            ("Error", last.get("position_error")),

            # WHICH HALF RAN. The trajectory is open loop, so a
            # movement where it advanced the full commanded distance
            # and the encoder did not is a MECHANICAL fault; one where
            # neither advanced never reached the servo at all. Two
            # completely different next actions, and until these were
            # reported separately they arrived as the same line.
            ("Trajectory", None if last.get("trajectory_travelled") is None
             else "{:+d} cnt commanded profile".format(
                 last.get("trajectory_travelled"))),
            ("Measured", None if last.get("measured_travel") is None
             else "{:+d} cnt / {} deg actually travelled".format(
                 last.get("measured_travel"),
                 last.get("measured_travel_deg"))),
            ("Verified", last.get("verification")),
            ("Unverified", last.get("unverified_reason")),
            ("Elapsed", None if last.get("elapsed_ms") is None
             else "{} ms".format(last.get("elapsed_ms"))),
        ))


SCAN_ADVICE = {
    "SERVO_FOUND": (
        "A servo answered. If the settings above differ from the ones in",
        "ESP32/config.py, change config.py to match and upload it again -",
        "the servo is telling you what it actually uses.",
    ),
    "WRONG_ID": (
        "The bus works. Only the ID is wrong, and that is a one-line",
        "change in ESP32/config.py (ST3215_SERVO_ID).",
    ),
    "ECHO_ONLY": (
        "The ESP32 side is proven good: it transmits and hears itself.",
        "What has NOT been proven is that the servo has power. A driver",
        "board with its LED lit only means the board is powered; measure",
        "at the servo's own connector, with the servo plugged in, while",
        "the scan runs. Then sweep the full ID range - a servo whose ID",
        "was changed answers to nothing else.",
    ),
    "CORRUPT_TRAFFIC": (
        "Frames arrive but decode wrong. That is nearly always the common",
        "ground: the ESP32 GND and the servo supply GND must be joined,",
        "even though the servo takes its power from elsewhere.",
    ),
    "NOISE_ONLY": (
        "Something transmits, nothing decodes. Check the ground first,",
        "then whether anything else is driving the same pins.",
    ),
    "SILENT_BUS": (
        "The bus is completely silent, in both pin orders, at all eight",
        "baud rates. Nothing is transmitting, so this is power or",
        "connection - not configuration, and not the firmware.",
        "",
        "Measure 6-12.6 V at the SERVO connector while the scan runs, not",
        "at the supply terminals: a supply reading 9.3 V with the servo",
        "unplugged proves only that the supply works. Then confirm the",
        "servo lead is seated in the driver board's bus port, and that",
        "the driver board's own logic supply is present.",
    ),
}



def print_bus_scan(report):
    """The scan, as a table of what was heard at every combination."""
    scanned = report.get("scanned") or {}

    print("Scanned:")
    print("  UART{}   TX GPIO{}   RX GPIO{}".format(
        scanned.get("uart_id"),
        scanned.get("configured_tx_pin"),
        scanned.get("configured_rx_pin"),
    ))
    print("  {} baud rate(s), {} servo ID(s), {} probe(s)".format(
        len(scanned.get("bauds") or []),
        scanned.get("id_count"),
        scanned.get("probes"),
    ))
    print("  Configured: ID {} at {} baud".format(
        scanned.get("configured_id"), scanned.get("configured_baud")
    ))
    print()

    print("{:<20} {:>9} {:>7} {:>6} {:>6}  {}".format(
        "Pin order", "Baud", "Bytes", "Echo", "Bad", "Answered"
    ))

    for probe in report.get("probes") or []:
        answered = probe.get("ids_answered") or []
        others = probe.get("other_ids") or []

        if answered:
            verdict = "ID {}".format(
                ", ".join(str(value) for value in answered)
            )
        elif others:
            verdict = "other ID {}".format(
                ", ".join(str(value) for value in others)
            )
        else:
            verdict = "-"

        print("{:<20} {:>9} {:>7} {:>6} {:>6}  {}".format(
            str(probe.get("pin_order"))[:20],
            probe.get("baud"),
            probe.get("bytes"),
            "YES" if probe.get("echo") else "-",
            probe.get("checksum_errors"),
            verdict,
        ))

    print()
    print("  Bytes = bytes received, Echo = our own transmission came")
    print("  back, Bad = frames that failed their checksum.")

    print()
    print("RESULT: {}".format(report.get("result")))
    print()

    for line in textwrap.wrap(str(report.get("diagnosis") or ""), 66):
        print("  {}".format(line))

    # THE SCAN COSTS THE CONNECTION, AND THAT HAS TO BE ON SCREEN.
    #
    # A scan reopens UART2 at eight baud rates in both pin orders, so a
    # servo that was connected is released first and the carousel
    # position goes with it. The firmware does that correctly and
    # reports it in `released_servo` - but this screen used to drop the
    # field, so an operator who ran a scan from Tools mid-session was
    # told "MOVES NOTHING", and then found the origin they had aligned
    # by hand was gone, with nothing on screen to say why.
    released = report.get("released_servo")

    if released:
        print()
        print("  THE SERVO WAS RELEASED so the scan could reopen UART2.")

        message = released.get("message") if isinstance(released, dict) \
            else None

        if message:
            print("  {}".format(message))

        print("  The carousel position went with it: connect the servo")
        print("  again and re-declare Slot 1 before the next measurement.")

    for difference in report.get("differences") or []:
        print()

        for line in textwrap.wrap("- {}".format(difference), 66):
            print("  {}".format(line))

    advice = SCAN_ADVICE.get(report.get("result"))

    if advice:
        print()
        print("WHAT TO DO")
        print()

        for line in advice:
            print("  {}".format(line))

    if report.get("released_servo"):
        print()
        print("The ST3215 backend was released so the scan could reopen")
        print("UART2. Select the servo again once it answers.")
