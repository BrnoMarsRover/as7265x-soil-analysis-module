"""
The measurement workflow: choose a slot, prepare it, confirm it, measure.

THE ORDER IN menu_measure IS THE POINT OF THIS MODULE.

    acquire  ->  PERSIST RAW  ->  analyse  ->  persist the AnalysisRun

RAW is written to BD before Science is asked anything at all. The
earlier arrangement analysed first and saved the result, which meant a
database that would not load, an exception in the pipeline or a model
that raised destroyed the measurement along with the analysis - and the
material had already been through the instrument. Saving first turns
every one of those from a lost experiment into a failed analysis that
can be re-run against RAW that is still there.

A failed acquisition is stored too, as a Measurement with
`acquisition_status = FAILED` and no RAW. It is an operational record.
It is emphatically not a successful measurement full of zeros.
"""

from BD.samples import (
    ACQUISITION_FAILED,
    ACQUISITION_SUCCESS,
    METADATA_FIELDS,
    STATE_EMPTY,
    STATE_LOADED,
    STATE_MEASURED,
    STATE_READY_TO_LOAD,
    StorageError,
    validate_sample_id,
)
from BD.channels import ILLUMINATIONS

from serial_link import DeviceError, LinkError

from workflow.records import ask_metadata
from workflow.display import (
    offer_decision_detail,
    print_agreement,
    print_cross_database,
    print_decision,
    print_evidence_summary,
    print_metric_table,
    print_quality,
    print_result_block,
    print_settings_block,
    print_triad_table,
    report_failure,
    report_link_error,
    report_return_move,
)
from workflow.prompts import (
    RULE,
    ask,
    ask_int,
    banner,
    choose,
    confirm,
    pause,
)

SLOT_COUNT = 4

import json
import sys
import textwrap

from serial_link import utc_timestamp

from workflow.records import capture_ground_truth

from Science import pipeline

from BD import config as bd_config


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
    The full measurement cycle.

        refuse what can be refused before anything moves
          -> ESP32: 180 deg out, acquire WHITE/UV/IR, 180 deg back
          -> BD: PERSIST THE MEASUREMENT, RAW AND ALL
          -> Science: analyse it
          -> BD: persist the AnalysisRun
          -> update the Sample's current conclusion

    THE THIRD STEP COMES BEFORE THE FOURTH ON PURPOSE. Persisting RAW
    the instant it arrives means that everything after it - a database
    that will not load, an exception in the pipeline, a decision model
    that raises, a laptop that loses power - costs an ANALYSIS and not
    an EXPERIMENT. The soil has already been through the instrument;
    the counts the detector reported cannot be obtained again from a
    slot that has been emptied, and every derived number can.

    The Sample record opened by Prepare is COMPLETED here. No second
    Sample ID is ever created.
    """
    carousel = status.get("carousel") or {}
    slot_id = carousel.get("selected_slot")
    entry = mission.entry_for(view, slot_id) if slot_id else None

    # ---- everything that can be refused before the mechanism moves ---
    if not entry or entry["state"] == STATE_EMPTY:
        print("No sample is prepared in the selected slot.")

        return

    if entry["state"] == STATE_READY_TO_LOAD:
        print("Slot {} does not contain confirmed soil yet. Use [3] "
              "Confirm Sample Loaded first.".format(slot_id))

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

    sample_id = entry["sample_id"]
    repeat = entry["state"] == STATE_MEASURED

    banner("MEASURE SAMPLE")

    print("Slot {} / sample {}".format(slot_id, sample_id))

    if repeat:
        # Not refused. A second acquisition of the same physical sample
        # is a repeatability measurement and one of the main reasons a
        # Sample holds several Measurements - it is announced, not
        # blocked, and it overwrites nothing.
        existing = len(mission.store.get_sample(sample_id)
                       .get("measurements") or [])

        print()
        print("This sample already has {} measurement(s). This one is "
              "added beside them; nothing is overwritten.".format(existing))

    print()
    print("Checking Sample................ PASS")
    print("Checking carousel.............. PASS")
    print()
    print("The carousel will swing 180 deg to the scanner, acquire "
          "WHITE, UV and IR spectra, then swing 180 deg back so the "
          "sample ends where it started. This takes a few seconds.")
    print()
    sys.stdout.flush()

    # ---- ESP32: out, acquire, back -----------------------------------
    try:
        data = mission.link.measure_raw(slot_id, sample_id)

    except (DeviceError, LinkError) as error:
        print()
        print("Measurement failed before any spectrum was obtained.")

        report_link_error(error)
        report_return_move((error.data or {}).get("return_move"))

        # An acquisition that failed is an operational fact, and it is
        # recorded as one: a Measurement with acquisition_status FAILED
        # and NO raw block. Deliberately not a successful measurement
        # full of zeros - a spectrum of zeros cannot be told apart from
        # a genuinely dark one, and inventing it would put an
        # acquisition into the scientific record that never happened.
        try:
            failed = mission.store.add_measurement(
                sample_id,
                acquisition_status=ACQUISITION_FAILED,
                acquisition={
                    "firmware_version": mission.firmware_version,
                },
                error={"code": error.code, "message": error.message},
            )

            print()
            print("Recorded as {} ({}). Sample {} remains {}; no "
                  "spectrum was saved because none was "
                  "obtained.".format(
                      failed["measurement_id"], ACQUISITION_FAILED,
                      sample_id, STATE_LOADED,
                  ))

        except StorageError as storage_error:
            print()
            print("!! Could not even record the failure: {}".format(
                storage_error.message))

        print()
        pause()

        return

    blocks = data.get("illuminations") or {}

    print("Measuring {}.................. PASS".format(sample_id))

    for name in ILLUMINATIONS:
        block = blocks.get(name) or {}

        if not block:
            continue

        print("  {:<6} {} repeat(s), {}/18 channels".format(
            name.upper(),
            block.get("repeats", "-"),
            len((block.get("acquisitions") or [{}])[0]),
        ))

    # The acquisition succeeded. Whether the carousel made it home is a
    # separate outcome and must not affect what gets saved.
    report_return_move(data.get("return_move"))

    # ---- BD: PERSIST RAW, before Science is asked anything -----------
    fields = mission.measurement_from_acquisition(data, sample_id)

    try:
        measurement = mission.store.add_measurement(sample_id, **fields)

    except StorageError as error:
        # Nothing downstream is attempted. Running an analysis whose
        # RAW could not be stored would produce a conclusion about a
        # measurement that no longer exists anywhere.
        print()
        print("!! COULD NOT SAVE THE MEASUREMENT: {}".format(error.message))
        print("   The analysis was NOT run. Fix the archive and use")
        print("   Tools -> Sync ESP32 Acquisitions to BD - the ESP32 is")
        print("   still holding this acquisition.")
        print()
        pause()

        return

    measurement_id = measurement["measurement_id"]

    print()
    print("Saving RAW to BD............... PASS  ({} / {})".format(
        sample_id, measurement_id))

    # ---- Science: analyse what is now safely stored -------------------
    run = mission.analyse_measurement(measurement)

    analysis_status = run.get("analysis_status")

    try:
        stored_run = mission.store.add_analysis_run(
            sample_id, measurement_id, run
        )
        run_id = stored_run["analysis_run_id"]

    except StorageError as error:
        run_id = None

        print()
        print("!! Could not save the analysis: {}".format(error.message))
        print("   RAW is stored and can be re-analysed.")

    if analysis_status == "FAILED":
        print()
        print("!! ANALYSIS FAILED: {}".format(
            (run.get("error") or {}).get("message", "no reason given")))
        print("   RAW IS SAFE. Measurement {} can be analysed again "
              "once the cause is fixed - re-measuring the sample is "
              "not necessary.".format(measurement_id))

    elif analysis_status == "PARTIAL":
        print()
        print("Analysis PARTIAL: {} did not complete.".format(
            ", ".join(run.get("failed_stages") or [])))
        print("Everything that did complete is stored.")

    # ---- the Sample's current conclusion ------------------------------
    decision = run.get("decision")

    if decision:
        try:
            mission.store.set_conclusion(sample_id, {
                "interpretation": decision.get("material")
                or decision.get("family"),
                "level": decision.get("level"),
                "status": decision.get("level"),
                "confidence": decision.get("confidence"),
                "from_measurement": measurement_id,
                "from_analysis_run": run_id,
                "decision_model_version": (
                    run.get("versions") or {}
                ).get("decision_model"),
            })

        except StorageError as error:
            print("!! Could not update the conclusion: {}".format(
                error.message))

    # ---- report -------------------------------------------------------
    banner("MEASUREMENT COMPLETE")

    print("Sample ID:      {}".format(sample_id))
    print("Physical slot:  {}".format(slot_id))
    print("Measurement:    {}".format(measurement_id))
    print("AnalysisRun:    {}".format(run_id or "NOT SAVED"))
    print("RAW saved:      YES")
    print("Analysis:       {}".format(analysis_status))
    print("Home position:  {}".format(
        "RESTORED" if data.get("home_restored") else "NOT RESTORED"
    ))

    print()
    print("SETTINGS")
    print()
    print_settings_block(
        (measurement.get("acquisition") or {}).get("sensor_settings")
    )

    print()
    print("RAW, BY ILLUMINATION")
    print()
    print_triad_table(
        measurement.get("raw"),
        (run.get("representations") or {}).get("normalized"),
    )

    quality_report = run.get("quality")

    print()
    print("MEASUREMENT QUALITY")
    print()

    if quality_report:
        print_quality(quality_report)

    else:
        print("  not available: {}".format(
            (run.get("error") or {}).get("message", "analysis incomplete")))

    evidence = run.get("evidence")

    if evidence:
        print()
        print("EVIDENCE")
        print()
        print_evidence_summary(evidence)

    if run.get("decision"):
        print()
        print("DECISION")
        print()
        print_decision(run["decision"])
        offer_decision_detail(run)

    if run_id and run.get("decision"):
        capture_ground_truth(mission, measurement_id, run)

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
