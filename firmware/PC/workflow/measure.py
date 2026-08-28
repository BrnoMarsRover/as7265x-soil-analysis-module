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

# `ui_status`, not `status`: this module binds a local `status` to the
# get_status dict, the shadowing hazard display.py documents.
from workflow import status as ui_status

from workflow.display import (
    offer_decision_detail,
    print_agreement,
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


# ======================================================================
# measurement recovery
# ======================================================================
# WHY A FAILED MEASUREMENT DOES NOT SIMPLY RETURN.
#
# It used to. `menu_measure` printed a correct, compact failure block,
# called `pause()`, and returned - and the main loop's very next
# iteration re-read the status, saw `position_valid == False`, and drew
# the context-free startup screen. Every screen rendered correctly.
# The operator still lost the sample id, the slot, the LOADED state,
# which stage had failed and whether a spectrum existed, because the
# only screen that had ever shown them was now two screens back.
#
# On a bench that is annoying. During a competition run, with a sample
# already committed to a slot and a carousel in an unknown position, it
# is the difference between recovering and starting again.
#
# So a failure that leaves anything worth acting on holds the operator
# HERE, in context, until they choose to leave.
#
# WHAT THIS SCREEN MAY NOT OFFER.
#
# There is no "retry the movement". Carousel movement is RELATIVE, and
# a movement whose acknowledgement was lost may already have happened;
# re-sending it would turn one 180 degree sweep into two. Every action
# here is a pure read, a diagnostic, or a re-declaration of the origin
# that moves nothing. Recovering position is the operator's eyes plus
# re-sync, never a resend.


def _recovery_state(mission):
    """
    The live state behind the recovery screen, or a reason there is none.

    Re-read every time round, because the whole point is to show what
    is true NOW - after a diagnostic, after a re-sync - rather than
    what was true when the measurement failed.
    """
    try:
        return mission.hardware_status(), None

    except (DeviceError, LinkError, TimeoutError) as error:
        return None, error


def measurement_recovery(mission, sample_id, slot_id, stage,
                         spectrum_state, recorded):
    """
    Hold the operator in the failed measurement's context.

    Returns when they choose to leave: explicitly, or by going back to
    the sample once the carousel position is trustworthy again.
    """
    while True:
        status, error = _recovery_state(mission)

        print()
        print(RULE)
        print("MEASUREMENT RECOVERY")
        print(RULE)

        if status is None:
            # The link itself is gone. Say so and offer the only two
            # things that are still meaningful.
            ui_status.print_fields((
                ("Sample", "{} / Slot {}".format(sample_id, slot_id)),
                ("Stage", stage or "unknown"),
                ("Spectrum", spectrum_state),
                ("Link", "UNREACHABLE - {}".format(error)),
            ))

            print()
            print("[1] Try the module again")
            print("[0] Back to the main menu")

            if choose() == "1":
                continue

            return

        carousel = status.get("carousel") or {}
        entry = mission.store.get_sample(sample_id) or {}

        ui_status.print_fields((
            ("Sample", "{} / Slot {} / {}".format(
                sample_id, slot_id, entry.get("state", "?"))),
            ("Stage", stage or "unknown"),
            ("Servo", ui_status.servo_link(status)),
            ("Carousel", ui_status.carousel_label(status)),
            ("Sensor", ui_status.sensor_label(status)),
            ("Spectrum", spectrum_state),
            ("Recorded", recorded),
        ))

        # The options are built from the state, so the screen can never
        # offer a recovery that does not apply - and never offers a
        # movement.
        online = ui_status.servo_online(status)
        synchronized = bool(carousel.get("position_valid"))

        actions = [("1", "Refresh hardware state")]

        if not online:
            actions.append(("2", "Carousel Setup - connect the servo"))

        elif not synchronized:
            actions.append(("2", "Re-sync carousel (nothing moves)"))

        actions.append(("3", "Servo diagnostics"))
        actions.append(("4", "Sensor diagnostics"))

        if synchronized:
            actions.append(("5", "Back to this sample"))

        actions.append(("0", "Abort to the main menu"))

        print()

        for key, label in actions:
            print("[{}] {}".format(key, label))

        selection = choose()
        offered = {key for key, _label in actions}

        if selection not in offered:
            if selection:
                print("Unknown option.")

            continue

        if selection == "0":
            return

        if selection == "1":
            continue

        # Imported here rather than at module scope. `workflow.carousel`
        # and `workflow.calibration` are peers of this module that
        # `screen` imports alongside it; pulling them in lazily keeps
        # the import graph a tree no matter which of the three grows an
        # import of another later.
        from workflow import calibration as calibration_menus
        from workflow import carousel as carousel_menus

        try:
            if selection == "2":
                if not online:
                    carousel_menus.menu_initial_calibration(mission)

                else:
                    carousel_menus.menu_resync(mission)

            elif selection == "3":
                carousel_menus.menu_servo_diagnostics(mission)

            elif selection == "4":
                # The sensor test re-initialises the AS7265x and takes a
                # reading. It illuminates; it moves NOTHING, which is
                # what makes it safe to offer while the carousel
                # position is unknown.
                calibration_menus.menu_sensor_test(mission)

            elif selection == "5":
                # Position is trustworthy again and the sample is still
                # where it was. Nothing further is owed here.
                print()
                print("Carousel synchronized. Slot {} is selectable "
                      "again.".format(slot_id))
                print()
                pause()

                return

        except (DeviceError, LinkError, TimeoutError) as failure:
            # A recovery action failing must not throw the operator out
            # of recovery - that is the defect this screen exists for.
            print()
            print("That did not work: {}".format(
                getattr(failure, "message", None) or failure))
            print()
            pause()

        except StorageError as failure:
            print()
            print("Storage error: {} ({})".format(
                failure.message, failure.code))
            print()
            pause()


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
        # ONE BLOCK, NOT FOUR PARAGRAPHS. §3.
        #
        # WHICH STAGE, not just "it failed". The firmware names the
        # stage it was in, and that goes in the title where it is read
        # first - it used to be a sentence of prose above three more
        # paragraphs that between them said "no spectrum was saved"
        # twice and described the carousel state in two different
        # wordings.
        #
        # Everything the operator needs is now on one screen in the
        # order they need it: what failed and where, what the encoder
        # saw, where the carousel is, what happened to the spectrum,
        # what state the sample is left in, what to do next.
        data = error.data or {}
        stage = data.get("phase")

        # THE STAGE STILL SAYS WHERE THE SAMPLE IS. One line, not a
        # paragraph - but not dropped either. The bare enum name in the
        # title says which stage failed; it does NOT say that an
        # ACQUISITION failure leaves the sample sitting at the scanner
        # rather than at the loading hole, and that is a physical fact
        # the operator acts on. RF-005C exists because these three need
        # opposite responses.
        STAGE_LINE = {
            "PRECHECK": "refused before anything moved",
            "MOVE_TO_SCANNER": "failed moving the sample to the scanner",
            "ACQUISITION": "sample reached the scanner; acquisition failed",
        }

        # An acquisition that failed is an operational fact, and it is
        # recorded as one: a Measurement with acquisition_status FAILED
        # and NO raw block. Deliberately not a successful measurement
        # full of zeros - a spectrum of zeros cannot be told apart from
        # a genuinely dark one, and inventing it would put an
        # acquisition into the scientific record that never happened.
        #
        # Recorded BEFORE the report is printed, so the block can say
        # which record it landed in rather than promising one.
        recorded = None
        storage_failure = None

        try:
            failed = mission.store.add_measurement(
                sample_id,
                acquisition_status=ACQUISITION_FAILED,
                acquisition={
                    "firmware_version": mission.firmware_version,
                },
                error={"code": error.code, "message": error.message},
            )
            recorded = failed["measurement_id"]

        except StorageError as storage_error:
            storage_failure = storage_error.message

        extra = [
            ("Spectrum", "NOT ACQUIRED - none was saved"),
            ("Sample", "{} remains {}".format(sample_id, STATE_LOADED)),
            ("Recorded", "{} ({})".format(recorded, ACQUISITION_FAILED)
             if recorded else "NOT RECORDED - {}".format(storage_failure)),
        ]

        ui_status.print_failure(
            error.code, data, message=error.message,
            title="MEASUREMENT FAILED{}".format(
                " - {}".format(stage) if stage else ""
            ),
            lead=[("Stage", STAGE_LINE.get(stage))],
            extra=extra,
        )

        report_return_move(data.get("return_move"))

        # STAY IN CONTEXT. §11.
        #
        # This used to `pause()` and return, and the main loop then drew
        # the startup screen over the top of it - so the sample, the
        # slot, the stage and the missing spectrum were gone one
        # keypress after they were printed. The operator is now held
        # here, with that context on screen, until they choose to leave.
        measurement_recovery(
            mission, sample_id, slot_id, stage,
            spectrum_state="NOT ACQUIRED - none was saved",
            recorded=("{} ({})".format(recorded, ACQUISITION_FAILED)
                      if recorded
                      else "NOT RECORDED - {}".format(storage_failure)),
        )

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
        # A GLOBALLY UNIQUE OBSERVATION ID, NOT THE PER-SAMPLE ONE.
        #
        # `measurement_id` is allocated from the SAMPLE's own list -
        # `_next_id(measurements_of(record), "M")` - so the first
        # measurement of every sample is M001. The learning store keys
        # its observations table on the id it is given, GLOBALLY.
        #
        # So the first sample labelled worked and the second was
        # refused with "M001 is already recorded. ... immutable ...",
        # which reads like a database fault and is an id collision. On
        # a bench whose learning history already carries an M001 it
        # failed on the very first sample. Either way the mixture the
        # operator had just weighed and typed in was silently not
        # saved, at the last keystroke.
        #
        # Qualifying with the sample makes it unique and keeps the
        # record self-describing: the observation names the sample it
        # came from, which is the linkage the observations table has no
        # column for.
        capture_ground_truth(
            mission, "{}/{}".format(sample_id, measurement_id), run)

    print()
    pause()

    # THE ACQUISITION SUCCEEDED AND THE CAROUSEL DID NOT COME HOME. §13F.
    #
    # These are two independent outcomes and this is the case where they
    # diverge: real spectra exist, are saved and are analysed, while the
    # carousel is somewhere nobody can name. Falling through here sent
    # the operator to the startup screen, which says only POSITION
    # UNKNOWN - so the one screen that knew a measurement had SUCCEEDED
    # was the screen they had just left.
    #
    # Recovery is entered with `Spectrum: ACQUIRED`, which is the whole
    # point: what to do next is different when the science is already
    # safe.
    if not data.get("home_restored"):
        measurement_recovery(
            mission, sample_id, slot_id, "RETURN_TO_LOADER",
            spectrum_state="ACQUIRED and saved - the science is safe",
            recorded="{} / {}".format(
                measurement_id, run_id or "no analysis"),
        )


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
