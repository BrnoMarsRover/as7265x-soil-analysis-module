"""
Stored records: browsing Samples, and recording what one turned out to be.

Reading the archive, and the one screen that writes to the learning
history.

GROUND TRUTH IS ENTERED, NEVER INFERRED. What a sample actually was is
something a person knows and the instrument does not, so it is asked
for explicitly and stored with how it was established. A field sample
the operator labelled from the Decision Model's own answer would be the
model marking its own homework, and nothing here creates a training
record from a conclusion.

Nothing recorded here changes a model at runtime. It becomes a
candidate training record; validation and training happen offline, in
research/, and produce a versioned artifact.
"""

from BD.decision_learning import LearningError
from BD.samples import (
    ACQUISITION_FAILED,
    ACQUISITION_SUCCESS,
    StorageError,
    analysis_runs_of,
    latest_analysis_run,
    latest_measurement,
    measurements_of,
)

from serial_link import DeviceError, LinkError

from workflow.display import (
    print_agreement,
    print_decision,
    print_matches,
    print_metric_table,
    print_processing_table,
    print_quality,
    print_result_block,
    print_settings_block,
    print_spectrum_table,
    print_system_status,
    print_triad_table,
    report_failure,
    report_link_error,
)
from workflow.prompts import (
    ask_float,
    RULE,
    ask,
    ask_int,
    banner,
    choose,
    confirm,
    number,
    pause,
    score,
)

import json
import sys
import textwrap

from BD import config as bd_config
from Science import pipeline
from serial_link import utc_timestamp

from BD.samples import METADATA_FIELDS, STATE_MEASURED


def capture_ground_truth(mission, measurement_id, result, save_prompt=True):
    """
    Ask what the sample actually was, and record it if the operator knows.

    NEVER REQUIRED. A rover measurement of unknown soil is the normal
    case, and refusing to store it without a label would throw away the
    observations the system most needs. The question is offered, and "I
    do not know" is a first-class answer that is stored as such. §59.

    The operator's answer is the ONLY source of ground truth. The model's
    own conclusion is never offered as a default, never pre-selected, and
    the learning store refuses it outright if anything tries. §46.
    """
    if mission.learning is None:
        return None

    if save_prompt:
        print()

        if not confirm("Save this measurement to the learning history?"):
            return None

    print()
    print("Ground truth available?")
    print()
    print("  [1] Yes - exact material known")
    print("  [2] Material family known")
    print("  [3] Known prepared mixture")
    print("  [4] Unknown sample")
    print("  [5] Save the measurement without a label")
    print()

    answer = choose("Select")

    if answer not in ("1", "2", "3", "4", "5"):
        print("Not saved.")

        return None

    label_type = {
        "1": "EXACT_MATERIAL",
        "2": "MATERIAL_FAMILY",
        "3": "PREPARED_MIXTURE",
        "4": "UNKNOWN_SAMPLE",
        "5": "NO_LABEL",
    }[answer]

    material = None
    family = None
    mixture = None

    if answer == "1":
        material = ask_material(mission)

        if material is None:
            print("Cancelled; nothing was saved.")

            return None

        family = material.family_id

    elif answer == "2":
        family = ask_family(mission)

        if family is None:
            print("Cancelled; nothing was saved.")

            return None

    elif answer == "3":
        mixture = ask_mixture(mission)

        if not mixture:
            print("Cancelled; nothing was saved.")

            return None

    status, source, certainty = ask_verification(answer)

    try:
        record = mission.record_observation(
            measurement_id, result,
            label_type=label_type,
            material=material,
            family_id=family,
            mixture=mixture,
            verification_status=status,
            verification_source=source,
            certainty=certainty,
        )

    except Exception as error:
        print()
        print("NOT SAVED: {}".format(error))

        return None

    print()
    print("Saved to the learning history as {}.".format(measurement_id))
    print("The reference databases were not modified.")

    return record


def ask_material(mission):
    """Resolve a material name through the controlled vocabulary."""
    taxonomy = mission.taxonomy

    if taxonomy is None:
        print("The material vocabulary is not loaded.")

        return None

    while True:
        text = ask("Material (name, alias or blank to cancel)")

        if not text:
            return None

        identity = taxonomy.get(text)

        if identity is not None:
            print("  -> {} ({})".format(
                identity.display_name, identity.family_id or "no family"
            ))

            if confirm("Is that right?"):
                return identity

            continue

        suggestions = taxonomy.suggest(text)

        print()
        print("'{}' is not a known material.".format(text))

        if suggestions:
            print("Did you mean:")

            for index, name in enumerate(suggestions, start=1):
                print("  [{}] {}".format(index, name))

            choice = ask("Number, or another name")

            if choice.isdigit() and 1 <= int(choice) <= len(suggestions):
                return taxonomy.get(suggestions[int(choice) - 1])

        print()
        print("A ground-truth label is never guessed: an unknown name is")
        print("refused rather than attached to the nearest match.")


def ask_family(mission):
    families = sorted(mission.taxonomy.families()) if mission.taxonomy else []

    if not families:
        return None

    print()

    for index, name in enumerate(families, start=1):
        print("  [{}] {}".format(index, name))

    choice = ask("Family number (blank = cancel)")

    if choice.isdigit() and 1 <= int(choice) <= len(families):
        return families[int(choice) - 1]

    return None


def ask_mixture(mission):
    """
    Components of a PREPARED mixture, with their prepared fractions.

    Only for a mixture the operator actually MADE and knows the
    composition of. A mixture guessed from a spectrum is not ground
    truth, and there is deliberately no way to enter one - which is the
    same reason production Science estimates no composition at all.

    What this records is a physical fact about a sample somebody mixed.
    It is stored as a training record and nothing in the runtime
    pipeline consumes it; it exists so that an unmixing model, if one
    is ever validated, has real prepared mixtures to be validated
    against.
    """
    components = []

    print()
    print("Enter each component of the PREPARED mixture. Blank to finish.")

    while True:
        identity = ask_material(mission)

        if identity is None:
            break

        fraction = ask_float(
            "Prepared mass fraction of {} (0-1)".format(
                identity.display_name
            ),
            0.0, 1.0,
        )

        if fraction is None:
            break

        components.append({
            "material_key": identity.key,
            "material_id": identity.material_id,
            "prepared_mass_fraction": fraction,
        })

    return components or None


def ask_verification(answer):
    """How much the label is worth, asked rather than assumed."""
    if answer in ("4", "5"):
        return "UNKNOWN", "operator", None

    print()
    print("How is that known?")
    print()
    print("  [1] VERIFIED - a labelled reference material, measured")
    print("      deliberately")
    print("  [2] OPERATOR_ASSERTED - believed, but not from a labelled")
    print("      container")
    print("  [3] UNVERIFIED - a guess. Recorded, never trained on.")
    print()

    choice = choose("Select") or "2"

    return {
        "1": ("VERIFIED", "operator_known_reference_material", 1.0),
        "2": ("OPERATOR_ASSERTED", "operator_assertion", None),
        "3": ("UNVERIFIED", "operator_guess", None),
    }.get(choice, ("OPERATOR_ASSERTED", "operator_assertion", None))


def menu_learning_history(mission):
    """What the system has seen, and how often it has been right."""
    banner("DECISION LEARNING HISTORY")

    if mission.learning is None:
        print("The learning database is not available:")
        print("  {}".format(mission.learning_error))
        print()
        pause()

        return

    status = mission.learning.status()

    print("Database: {}".format(status["file"]))
    print()
    print("Observations:      {}".format(status["observations"]))
    print("Labelled:          {}".format(status["labelled"]))

    for level, count in status["by_verification"].items():
        print("  {:<18} {}".format(level, count))

    print("Predictions:       {}".format(status["predictions"]))
    print("Model versions:    {}".format(
        ", ".join(status["model_versions"]) or "-"
    ))

    print()
    print("VERIFIED MATERIALS")
    print()

    if status["verified_materials"]:
        for material, count in sorted(status["verified_materials"].items()):
            print("  {:<34} {} independent measurement(s)".format(
                material[:34], count
            ))

    else:
        print("  none yet")

    print()
    print("CONFUSION HISTORY  (verified truth vs what a model said)")
    print()

    rows = mission.learning.confusion()

    if rows:
        for row in rows:
            outcome = (
                "correct" if row["predicted"] == row["actual"]
                else "no answer" if not row["predicted"] else "WRONG"
            )

            print("  {:<12} {:<26} -> {:<26} {}".format(
                row["model_version"][:12],
                str(row["actual"])[:26],
                str(row["predicted"])[:26],
                outcome,
            ))

    else:
        print("  no model has been scored against verified truth yet")

    print()
    print("A prediction is never ground truth. Retraining is an explicit")
    print("command, never a consequence of measuring.")
    print()
    pause()


def print_sample_table(store):
    summaries = store.summaries()

    if not summaries:
        print("No samples have been saved yet.")

        return summaries

    # Measurements and runs are counted separately, because a Sample
    # with three measurements and a Sample with one are not the same
    # record and a single "MEASURED" hides the difference. The
    # interpretation column shows the CURRENT conclusion and the status
    # that produced it - an AMBIGUOUS "Pink Clay" is not a
    # classification, and a bare material name would read as one.
    print("{:<4} {:<12} {:<5} {:<13} {:>3} {:>3} {:<20} {:<12}".format(
        "#", "Sample", "Slot", "State", "M", "A", "Interpretation",
        "Decision"
    ))

    for index, entry in enumerate(summaries, start=1):
        print("{:<4} {:<12} {:<5} {:<13} {:>3} {:>3} {:<20} {:<12}".format(
            index,
            str(entry.get("sample_id"))[:12],
            str(entry.get("slot_id") or "-"),
            str(entry.get("state"))[:13],
            entry.get("measurement_count", 0),
            entry.get("analysis_run_count", 0),
            str(entry.get("interpretation") or "-")[:20],
            str(entry.get("decision_status") or "-")[:12],
        ))

    return summaries


def print_full_sample(record):
    """
    One Sample, in full: its measurements and what each was made of.

    THE SHAPE OF THIS SCREEN IS THE SHAPE OF THE RECORD. A Sample holds
    measurements; each measurement holds its own RAW and its own
    analysis runs. Flattening that for display would hide the two
    things the model exists to make visible - that a sample can be
    measured more than once, and that one measurement can be analysed
    more than once, with different answers.
    """
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

    conclusion = record.get("conclusion")

    if conclusion:
        print()
        print("CURRENT CONCLUSION")
        print("  {:<18}{}".format(
            "Interpretation:", conclusion.get("interpretation") or "-"))
        print("  {:<18}{}".format(
            "Decision status:", conclusion.get("status") or "-"))
        print("  {:<18}{}".format(
            "Confidence:", conclusion.get("confidence") or "-"))
        print("  {:<18}{} / {}".format(
            "Derived from:",
            conclusion.get("from_measurement") or "-",
            conclusion.get("from_analysis_run") or "-"))
        print()
        print("  A VIEW of the run named above, not a replacement for it.")
        print("  Every measurement and every run below is still here.")

    measurements = measurements_of(record)

    print()
    print("MEASUREMENTS ({})".format(len(measurements)))

    if not measurements:
        print()
        print("No spectrum has been acquired for this sample yet.")
        print()
        pause()

        return

    for measurement in measurements:
        _print_measurement(measurement)

    print()
    pause()


def _print_measurement(measurement):
    """One Measurement: how it was taken, its RAW, and its analysis runs."""
    acquisition = measurement.get("acquisition") or {}
    runs = analysis_runs_of(measurement)

    print()
    print("-" * 60)
    print("{}   {}   slot {}".format(
        measurement.get("measurement_id"),
        measurement.get("acquisition_status"),
        measurement.get("slot_id"),
    ))
    print("-" * 60)
    print("  Taken:        {}".format(measurement.get("timestamp")))
    print("  Calibration:  {}".format(
        measurement.get("calibration_id") or "-"))
    print("  Firmware:     {}".format(
        acquisition.get("firmware_version") or "-"))
    print("  Illumination: {}".format(
        ", ".join(acquisition.get("illuminations") or []) or "-"))

    if measurement.get("acquisition_status") != ACQUISITION_SUCCESS:
        error = measurement.get("error") or {}

        print()
        print("  ACQUISITION FAILED - {} {}".format(
            error.get("code", "?"), error.get("message", "")))
        print("  There is no RAW, deliberately: a spectrum of zeros")
        print("  cannot be told apart from a genuinely dark reading.")

        return

    print()
    print("  SENSOR SETTINGS")
    print()
    print_settings_block(acquisition.get("sensor_settings"))

    print()
    print("  RAW, BY ILLUMINATION")
    print()
    # The reflectance from the LATEST run, where there is one. RAW is
    # the measurement; reflectance is an interpretation of it, and the
    # table keeps them in that relationship.
    latest = latest_analysis_run(measurement)

    print_triad_table(
        measurement.get("raw"),
        ((latest or {}).get("representations") or {}).get("normalized"),
    )

    print()
    print("  ANALYSIS RUNS ({})".format(len(runs)))

    if not runs:
        print()
        print("  None yet. The RAW above is stored and can be analysed")
        print("  at any time - re-measuring is not necessary.")

        return

    for run in runs:
        _print_analysis_run(run)


def _print_analysis_run(run):
    """One AnalysisRun: what it concluded, and what it was built from."""
    versions = run.get("versions") or {}
    decision = run.get("decision") or {}

    print()
    print("  {}   analysis {}   decision {}".format(
        run.get("analysis_run_id"),
        run.get("analysis_status") or "-",
        run.get("decision_status") or "-",
    ))
    print("    Run at:     {}".format(run.get("created_at") or "-"))
    print("    Science:    {}   model: {}".format(
        versions.get("science") or "-",
        versions.get("decision_model") or "-",
    ))

    databases = versions.get("databases") or {}

    if databases:
        print("    Databases:  {}".format(", ".join(
            "{} {}".format(key, value)
            for key, value in sorted(databases.items())
        )))

    if run.get("failed_stages"):
        print("    Failed:     {}".format(", ".join(run["failed_stages"])))
        print("                everything the earlier stages produced is")
        print("                still stored below.")

    if run.get("error"):
        print("    Error:      {} {}".format(
            (run["error"] or {}).get("code", ""),
            (run["error"] or {}).get("message", "")))

    quality = run.get("quality") or {}

    if quality:
        print("    Quality:    hardware {} / normalization {}".format(
            (quality.get("hardware") or {}).get("status", "-"),
            (quality.get("normalization") or {}).get("status", "-"),
        ))

    for entry in run.get("database_results") or []:
        families = entry.get("families") or {}
        winners = ", ".join(
            "{}={}".format(name, (view or {}).get("winner") or "-")
            for name, view in sorted(families.items())
        )

        print("    {:<5} {:<12} {}".format(
            entry.get("database", "?"),
            entry.get("status", "-"),
            winners or "no candidate",
        ))

    if decision:
        print()
        print_decision(decision)

    # A record migrated from the flat schema keeps what it had.
    legacy = run.get("legacy_analysis")

    if legacy:
        print()
        print("    MIGRATED FROM SCHEMA {} - the original conclusion:"
              .format(run.get("migrated_from_schema")))
        print()
        print_result_block(legacy)

        matches = run.get("reference_matches") or []

        if matches:
            print()
            print("    ORIGINAL DATABASE COMPARISON ({} materials)"
                  .format(len(matches)))
            print()
            print_matches(matches)



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
            _import_retained(mission, sample_id, entry, payload)

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
    print("BD Sample archive updated: {}".format(mission.store.path))
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

    for channel in pipeline.CHANNELS:
        if channel not in first or channel not in second:
            return False

        try:
            if abs(float(first[channel]) - float(second[channel])) > 1e-4:
                return False

        except (TypeError, ValueError):
            return False

    return True


def _import_retained(mission, sample_id, entry, payload):
    """
    Store one retained ESP32 acquisition as a Sample and Measurement.

    THE SAME ORDER AS A LIVE MEASUREMENT: the Sample and its RAW are
    written to BD first, and only then is Science asked anything. An
    import that cannot be analysed still lands the spectrum, which is
    the entire reason the device keeps a buffer at all.

    Nothing is invented. The device records no timestamps and no
    metadata, so those stay null and the record says why rather than
    being filled with plausible values.
    """
    measurement = payload.get("measurement") or {}
    slot_id = payload.get("slot_id") or entry.get("slot_id")

    if not mission.store.has_sample(sample_id):
        mission.store.create(sample_id, slot_id)

    fields = mission.measurement_from_acquisition(measurement, sample_id)

    acquisition = fields.get("acquisition") or {}
    acquisition["origin"] = "esp32_buffer"
    acquisition["esp_uptime_ms"] = measurement.get("esp_uptime_ms")
    acquisition["note"] = (
        "Copied from the ESP32 acquisition buffer. The device records "
        "no wall-clock time and no metadata, so created_at, loaded_at "
        "and the sample metadata are unknown rather than assumed."
    )
    fields["acquisition"] = acquisition

    if not fields.get("raw"):
        # An entry with no spectrum is an operational record, not a
        # measurement. Stored as the failure it is.
        return mission.store.add_measurement(
            sample_id,
            acquisition_status=ACQUISITION_FAILED,
            acquisition=acquisition,
            error={"code": "NO_RAW_IN_DEVICE_BUFFER",
                   "message": "the retained entry carried no spectrum"},
        )

    stored = mission.store.add_measurement(sample_id, **fields)

    run = mission.analyse_measurement(stored)

    mission.store.add_analysis_run(
        sample_id, stored["measurement_id"], run
    )

    return stored



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



