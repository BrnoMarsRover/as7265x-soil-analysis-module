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

from BD.decision_learning import LearningError, classify_prediction
from BD.samples import (
    ACQUISITION_FAILED,
    ACQUISITION_SUCCESS,
    StorageError,
    analysis_runs_of,
    latest_analysis_run,
    latest_measurement,
    measurements_of,
    successful_measurements,
    summary_of,
)

from serial_link import DeviceError, LinkError

from workflow import materials

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

from BD.samples import (
    METADATA_FIELDS,
    STATE_MEASURED,
    validate_sample_id,
)


def save_acquisition_as_sample(mission, data, result):
    """
    Turn a bench acquisition into a PC ARCHIVE record.

    STRAIGHT TO THE ARCHIVE, and that is not an exception to the
    no-implicit-writes rule - it IS the explicit import. The operator
    reached this by choosing "Save as a Sample in the archive" from the
    disposition menu, on a screen that has just said "THIS MEASUREMENT
    IS NOT SAVED YET". A bench acquisition belongs to no carousel slot
    and to no run, so there is no session for it to sit in.

    RAW FIRST, exactly as menu_measure does it: the acquisition is
    stored before Science is asked anything, so a failure in the
    analysis costs an analysis and not the experiment. §58.

    THE ANALYSIS IS RUN AGAIN against the STORED measurement rather than
    the test's copy being filed under a new name. The test analysed a
    record whose measurement_id was "SENSOR_TEST" and whose sample_id
    was None; storing that result would put an AnalysisRun in the
    archive naming a measurement that does not exist. Re-running is pure
    arithmetic on the same numbers and costs no hardware time.

    Returns (sample_id, measurement_id), or (None, None) if nothing was
    saved.
    """
    blocks = (data or {}).get("illuminations") or {}

    if not blocks:
        print()
        print("This acquisition carries no spectra, so there is nothing")
        print("to save.")

        return None, None

    print()
    print("SAVE AS A SAMPLE")
    print()
    print("This measurement was taken at the bench, so it belongs to no")
    print("carousel slot. The Sample is created with no slot and is")
    print("marked as a sensor-test acquisition in its metadata.")
    print()

    raw_id = ask("Sample ID (blank = cancel)")

    if not raw_id:
        print("Cancelled; nothing was saved.")

        return None, None

    try:
        sample_id = validate_sample_id(raw_id)

    except StorageError as error:
        print(error.message)

        return None, None

    # An existing ID is a handle to physical material. Adding a bench
    # measurement to somebody else's Sample would attach this spectrum
    # to the wrong specimen, which is the one mistake §59 says must
    # never happen. Checked against the ARCHIVE, which is where this
    # is about to write.
    if mission.archive.has_sample(sample_id):
        print()
        print("Sample {} already exists.".format(sample_id))

        if not confirm("Add this measurement to that existing Sample?"):
            print("Nothing was saved.")

            return None, None

    else:
        metadata = ask_metadata() or {}

        # `note`, not a key of our own. BD.blank_metadata keeps only the
        # six declared METADATA_FIELDS and silently drops anything else,
        # so an "origin" key would have recorded nothing at all while
        # this code claimed it had. Only filled when the operator left
        # the field empty - what they typed is never overwritten.
        if not metadata.get("note"):
            metadata["note"] = "bench sensor test, no carousel slot"

        try:
            mission.archive.create(
                sample_id, None, utc_timestamp(), metadata
            )

        except StorageError as error:
            print()
            print("NOT SAVED: {}".format(error.message))

            return None, None

        print()
        print("Sample {} created with no carousel slot.".format(sample_id))

    # ---- BD: PERSIST RAW, before Science is asked anything ------------
    fields = mission.measurement_from_acquisition(data, sample_id)

    try:
        measurement = mission.archive.add_measurement(
            sample_id, **fields
        )

    except StorageError as error:
        print()
        print("!! COULD NOT SAVE THE MEASUREMENT: {}".format(error.message))
        print("   Nothing downstream was attempted.")

        return None, None

    measurement_id = measurement["measurement_id"]

    print()
    print("Saving RAW to BD............... PASS  ({} / {})".format(
        sample_id, measurement_id))

    # ---- Science: analyse what is now safely stored -------------------
    run = mission.analyse_measurement(measurement)
    analysis_status = run.get("analysis_status")
    run_id = None

    try:
        stored_run = mission.archive.add_analysis_run(
            sample_id, measurement_id, run
        )
        run_id = stored_run["analysis_run_id"]

    except StorageError as error:
        print("!! Could not save the analysis: {}".format(error.message))
        print("   RAW is stored and can be re-analysed.")

    if analysis_status == "FAILED":
        print()
        print("!! ANALYSIS FAILED: {}".format(
            (run.get("error") or {}).get("message", "no reason given")))
        print("   RAW IS SAFE and can be analysed again.")

    decision = run.get("decision")

    if decision:
        try:
            mission.archive.set_conclusion(sample_id, {
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

    print()
    print("Sample:         {}".format(sample_id))
    print("Measurement:    {}".format(measurement_id))
    print("AnalysisRun:    {}".format(run_id or "NOT SAVED"))
    print("RAW saved:      YES")
    print("Analysis:       {}".format(analysis_status))

    return sample_id, measurement_id


def offer_measurement_disposition(mission, data, result,
                                  measurement_id=None):
    """
    What becomes of a bench measurement: learning, archive, or nothing.

    The sensor test used to end by offering the learning history and
    nothing else, so a measurement worth keeping as a Sample had to be
    taken again through the mission workflow - and the operator was
    asked about ground truth every single time, including for the
    throwaway runs that make up most sensor testing.

    Three outcomes, asked once and plainly. The menu repeats so one
    measurement can go to both stores, and EXIT is always available and
    always means nothing further is written.
    """
    if measurement_id is None:
        measurement_id = "TEST_{}".format(
            utc_timestamp().replace(":", "").replace("-", "")[:15]
        )

    saved_to_learning = False
    saved_sample = None

    while True:
        print()
        print(RULE)
        print()
        print("THIS MEASUREMENT IS NOT SAVED YET")
        print()

        if mission.learning is None:
            print("[1] Save to the learning history      UNAVAILABLE")
            print("    {}".format(
                mission.learning_error or "the learning database is not open"
            ))
        elif saved_to_learning:
            print("[1] Save to the learning history      ALREADY SAVED")
            print("    Stored as {}.".format(measurement_id))
        else:
            print("[1] Save to the learning history (seed observation)")
            print("    Records what the sample ACTUALLY is, for the")
            print("    Decision Model. Training happens offline.")

        print()

        if saved_sample:
            print("[2] Save as a Sample in the PC archive  ALREADY SAVED")
            print("    Stored as {}.".format(saved_sample))
        else:
            print("[2] Save as a Sample in the PC archive")
            print("    Creates a Sample record IN THE PERMANENT PC")
            print("    ARCHIVE: RAW first, then the analysis beside it.")

        print()
        print("[3] Exit without saving anything")
        print()

        selection = choose("Select")

        if selection == "3" or not selection:
            if not saved_to_learning and not saved_sample:
                print()
                print("NOTHING SAVED.")

            return saved_sample, saved_to_learning

        if selection == "1":
            if mission.learning is None:
                print()
                print("The learning database is not available.")

                continue

            if saved_to_learning:
                print()
                print("Already saved to the learning history.")

                continue

            if not (result or {}).get("evidence"):
                print()
                print("No evidence package was built - there is no active")
                print("calibration - so the observation would be stored")
                print("with its RAW spectra and no derived features.")

                if not confirm("Save it anyway?"):
                    continue

            record = capture_ground_truth(
                mission, measurement_id, result, save_prompt=False
            )

            saved_to_learning = record is not None

            continue

        if selection == "2":
            if saved_sample:
                print()
                print("Already saved as {}.".format(saved_sample))

                continue

            sample_id, stored_id = save_acquisition_as_sample(
                mission, data, result
            )

            if sample_id is not None:
                saved_sample = sample_id

                # The archive and the learning history should name the
                # SAME measurement. Once RAW is stored under a real
                # measurement_id, that is the id worth learning from.
                if not saved_to_learning and stored_id:
                    measurement_id = stored_id

                elif saved_to_learning and stored_id != measurement_id:
                    # ORDER MATTERS AND THE OPERATOR SHOULD KNOW.
                    # An observation already written cannot be renamed,
                    # so saving to the learning history FIRST and to the
                    # archive second leaves two records of one physical
                    # measurement under two ids, with nothing joining
                    # them. Saying so is better than a silent orphan.
                    print()
                    print("Note: the learning history already holds this")
                    print("measurement as {}, and the archive now holds".format(
                        measurement_id))
                    print("it as {}. They are the same acquisition but".format(
                        stored_id))
                    print("carry different ids - save to the archive first")
                    print("next time and both will use the archive's.")

            continue

        print()
        print("Unknown option.")


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

    # WHAT MAY AND MAY NOT BE SAVED, in four lines. §19.
    #
    # The distinction this screen turns on is not obvious from the
    # options: [1] and [3] are things the operator KNOWS, [4] and [5]
    # are honest admissions that they do not. The one mistake that
    # cannot be undone is entering the Decision Model's answer as
    # though it were a fact, so it is named here rather than assumed.
    print()
    print("GROUND TRUTH - what the sample ACTUALLY is")
    print()
    print("  Save only what you know independently of the instrument:")
    print("  a labelled container, or a mixture you weighed yourself.")
    print("  A Decision Model estimate is NOT ground truth - record an")
    print("  unidentified sample as [4], which is trained on by nothing.")
    print()
    print("  [1] Exact material known")
    print("  [2] Material family known")
    print("  [3] Known prepared mixture")
    print("      Materials you weighed and mixed yourself, with their")
    print("      proportions - including what you mixed them INTO.")
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

    # Asked for every label, including "unknown sample". An unlabelled
    # field measurement whose distance and packing ARE recorded still
    # supports learning how those change a spectrum - and a mixture with
    # no presentation recorded cannot be compared with the next one.
    context = None

    if confirm("Record how the sample was presented (distance, mass, "
               "packing)?"):
        context = ask_sample_context(mission)

    try:
        record = mission.record_observation(
            measurement_id, result,
            label_type=label_type,
            material=material,
            family_id=family,
            mixture=mixture,
            sample_context=context,
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

    if mixture:
        print()
        # The `.format()` was on the LAST line and the `{}` on the
        # first, so the operator was told their mixture had "{} part(s)"
        # at the one moment they were checking it had been stored right.
        print("Recorded as a prepared mixture of {} part(s). The "
              "stored".format(len(mixture)))
        print("proportions are what you WEIGHED; nothing in the runtime")
        print("pipeline reads them. They are what an unmixing model will")
        print("be scored against when there are enough of them.")

        summary = mission.mixture_readiness()

        if summary:
            print()
            print("  {}".format(summary))

    return record


def ask_material(mission, prompt="Select material", allow_matrix=False):
    """
    Resolve a material through the controlled vocabulary.

    `allow_matrix` lets the caller accept a MatrixChoice as well as a
    MaterialIdentity. Off by default, because most screens are asking
    "which library material is this", and soil is not an answer to that
    question. The prepared-mixture screen turns it on, because there it
    is the commonest answer there is.

    THE LIST IS SHOWN. This used to be a bare free-text field that
    refused what it could not resolve without ever saying what it would
    accept, so an operator had to know the canonical spelling before
    they could enter one. `workflow.materials` owns the picker now -
    §11, §13 - and this stays as the name every caller already uses.
    """
    return materials.select_material(
        mission, prompt=prompt, allow_matrix=allow_matrix
    )


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
    Components of a PREPARED mixture, with their prepared proportions.

    Only for a mixture the operator actually MADE and knows the
    composition of. A mixture guessed from a spectrum is not ground
    truth, and there is deliberately no way to enter one - which is the
    same reason production Science estimates no composition at all.

    TWO KINDS OF INGREDIENT, AND THE DIFFERENCE MATTERS

        component   a library material, weighed in deliberately.
                    "10 % Iron(III) Oxide Red"
        matrix      what it was mixed INTO - ordinary soil, sand, the
                    local regolith. A real substance with a real mass
                    and no reference spectrum anywhere.

    The matrix is asked for separately rather than being inferred from
    "whatever is left", because it is usually most of the sample and
    most of the signal. A model asked to find 10 % hematite in garden
    soil is being asked to find it AGAINST that soil, and an evaluation
    that does not know what the other 90 % was cannot say whether a miss
    was the model's fault or the matrix's.

    PERCENT IN, FRACTION STORED. The operator mixes by percent and the
    store keeps mass fractions; converting here means the 0.10-vs-10
    mistake is made in one place and caught immediately, rather than
    becoming a training example that says a sample was 1000 % hematite.
    """
    components = []

    print()
    print("PREPARED MIXTURE")
    print()
    print("Two kinds of ingredient, and the difference matters:")
    print()
    print("  COMPONENT  a library material you weighed in")
    print("             -> Activated Carbon 30 %")
    print("  MATRIX     what you mixed it INTO. Ordinary soil or sand:")
    print("             real, usually most of the sample, and in no")
    print("             library. Pick [s] at the list, or just type it.")
    print()
    print("Percentages by mass. Cancel at the material list finishes the")
    print("ingredient list.")

    while True:
        _print_mixture_table(components)

        identity = ask_material(
            mission,
            prompt="Ingredient {} - select material or matrix".format(
                len(components) + 1
            ),
            allow_matrix=True,
        )

        if identity is None:
            break

        # A MatrixChoice is not a MaterialIdentity and is deliberately
        # not made to look like one: it has a label and no key, so it
        # cannot become a library entry with a spectrum it has not got.
        is_matrix = isinstance(identity, materials.MatrixChoice)

        if is_matrix:
            duplicate = any(
                (part.get("matrix_label") or "").strip().lower()
                == identity.label.lower()
                for part in components
            )
        else:
            duplicate = any(
                part.get("material_key") == identity.key
                for part in components
            )

        if duplicate:
            print()
            print("{} is already in the mixture. One ingredient, one "
                  "proportion.".format(identity.display_name))

            continue

        percent = ask_float(
            "Percent of {} by mass (0-100)".format(identity.display_name),
            0.0, 100.0,
        )

        if percent is None:
            print("Ingredient dropped.")

            continue

        if is_matrix:
            components.append({
                "role": "MATRIX",
                "material_key": None,
                "matrix_label": identity.label,
                "prepared_mass_fraction": percent / 100.0,
            })

        else:
            components.append({
                "role": "COMPONENT",
                "material_key": identity.key,
                "material_id": identity.material_id,
                "family_id": identity.family_id,
                "prepared_mass_fraction": percent / 100.0,
            })

    if not components:
        return None

    # -- the matrix ----------------------------------------------------
    weighed = sum(
        part["prepared_mass_fraction"] for part in components
    )
    remainder = 1.0 - weighed

    print()

    # KEPT AS A BACKSTOP, NOT AS THE ONLY WAY IN. The matrix is now
    # selectable in the ingredient list with its own proportion, so this
    # only fires when the numbers do not add up and nothing has claimed
    # the difference - which is exactly the moment it is useful.
    has_matrix = any(part.get("role") == "MATRIX" for part in components)

    if remainder > 1e-9 and not has_matrix:
        print("The named components account for {:.1f} %. What is the "
              "remaining {:.1f} %?".format(weighed * 100.0,
                                           remainder * 100.0))
        print()
        print("If it is ordinary soil, sand or anything else that is not")
        print("in the libraries, name it here. That is the matrix, and")
        print("what it was is part of the measurement.")
        print()

        label = ask("Matrix (blank = the mixture is only what is listed)")

        if label:
            components.append({
                "role": "MATRIX",
                "material_key": None,
                "matrix_label": label,
                "prepared_mass_fraction": remainder,
            })

    # -- confirm before it becomes a training example ------------------
    print()
    _print_mixture_table(components)

    total = sum(
        part.get("prepared_mass_fraction") or 0.0 for part in components
    )

    if abs(total - 1.0) > 0.005:
        # BOTH DIRECTIONS, NAMED CORRECTLY. This printed "the missing
        # mass was in the cup" for every failure, including the ones
        # that add up to MORE than 100 % - where nothing is missing and
        # something has been counted twice. §17.
        print("Composition totals {:.2f} %. Expected 100 %.".format(
            total * 100.0
        ))
        print()

        if total < 1.0:
            print("The unaccounted {:.2f} % was still in the cup and in the"
                  .format((1.0 - total) * 100.0))
            print("measurement. Attributing it to the components that WERE")
            print("listed would make every one of them wrong.")
            print()
            print("If it was soil or sand, name it as the matrix.")

        else:
            print("That is {:.2f} % more than the sample can contain, so at"
                  .format((total - 1.0) * 100.0))
            print("least one proportion is wrong or counted twice.")

        print()

        if not confirm("Re-enter the mixture?"):
            return None

        return ask_mixture(mission)

    if not confirm("Is that what you mixed?"):
        print("Cancelled; nothing was saved.")

        return None

    return components


# What each role MEANS, said where the numbers are read.
#
# The two are not interchangeable and the record does not treat them as
# interchangeable: a COMPONENT is a library material with a reference
# spectrum that a model can be scored against, and a MATRIX is a real
# substance with a real mass and NO reference spectrum anywhere. A table
# that showed "component / matrix" in lower case left the operator to
# infer that difference, and the whole value of the record depends on
# it being deliberate.
ROLE_NOTE = {
    "COMPONENT": "reference component",
    "MATRIX": "matrix / no reference spectrum",
}


def _print_mixture_table(components):
    if not components:
        return

    print()
    print("  {:<30} {:>8}  {}".format("ingredient", "percent", "role"))

    for part in components:
        name = part.get("material_key") or part.get("matrix_label") or "?"
        fraction = part.get("prepared_mass_fraction")
        role = part.get("role", "COMPONENT")

        print("  {:<30} {:>7.2f}%  [{}]".format(
            name[:30],
            (fraction or 0.0) * 100.0,
            ROLE_NOTE.get(role, role.lower()),
        ))

    total = sum(
        part.get("prepared_mass_fraction") or 0.0 for part in components
    )

    print("  {:<30} {:>7.2f}%".format("total", total * 100.0))
    print()


def ask_sample_context(mission):
    """
    How the sample physically sat in front of the sensor.

    THE VARIABLES THE OPERATOR CHANGES AND THE PROFILE DOES NOT.
    An acquisition profile records gain, integration time and lamp
    current - the instrument's settings. It does not record that this
    cup held 5 g of loose damp powder at 40 mm and the last one held
    20 g tamped dry at 25 mm, and those move the spectrum at least as
    much as the material does.

    Without them a model trying to learn a material learns the
    operator's habits instead, and the first time the habits change it
    is wrong about everything.

    EVERY ANSWER IS OPTIONAL AND BLANK MEANS NOT RECORDED. Never a
    default: two measurements that both say "distance unknown" are not
    thereby known to have been taken at the same distance, and a model
    that fills an unrecorded distance with 30 mm learns a relationship
    to a number nobody measured.
    """
    print()
    print("SAMPLE PRESENTATION")
    print()
    print("How the sample sat in front of the sensor. Every answer is")
    print("optional; blank records NOT RECORDED, which is honest and")
    print("useful. A guessed number is neither.")

    context = {}

    print()
    distance = ask_float("Sensor-to-sample distance in mm", 0.0, 1000.0)

    if distance is not None:
        context["sensor_to_sample_mm"] = distance

    mass = ask_float("Sample mass in g", 0.0, 10000.0)

    if mass is not None:
        context["sample_mass_g"] = mass

    depth = ask_float("Sample depth in the cup, mm", 0.0, 500.0)

    if depth is not None:
        context["sample_depth_mm"] = depth

    packing = _ask_state(
        "How was it packed?",
        ("LOOSE", "TAMPED", "PRESSED"),
        ("poured in, not compacted",
         "pressed down by hand",
         "compacted deliberately"),
    )

    if packing:
        context["packing"] = packing

    moisture = _ask_state(
        "How wet was it?",
        ("OVEN_DRY", "AIR_DRY", "DAMP", "WET"),
        ("dried in an oven",
         "as it comes out of the container",
         "visibly moist, holds together",
         "free water present"),
    )

    if moisture:
        context["moisture"] = moisture

    grain = ask("Grain size or preparation (blank = not recorded)")

    if grain:
        context["grain_size"] = grain

    container = ask("Sample container (blank = not recorded)")

    if container:
        context["container"] = container

    note = ask("Anything else worth recording (blank = nothing)")

    if note:
        context["note"] = note

    return context or None


def _ask_state(question, states, descriptions):
    """One controlled-vocabulary answer, or None for not recorded."""
    print()
    print(question)
    print()

    for index, (state, description) in enumerate(
        zip(states, descriptions), start=1
    ):
        print("  [{}] {:<10} {}".format(index, state, description))

    print("  [Enter] not recorded")
    print()

    choice = choose("Select")

    if choice.isdigit() and 1 <= int(choice) <= len(states):
        return states[int(choice) - 1]

    return None


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


# ======================================================================
# DECISION LEARNING - MATERIAL DATASETS
# ======================================================================
# WHAT THE OLD SCREEN GOT WRONG.
#
# It led with a CONFUSION HISTORY: one row per (measurement, model
# version) pair, model names truncated to twelve characters, no
# measurement id, no composition, no matrix, no percentage, and `None`
# rendered as the word None for four different outcomes that are not
# the same thing. Thirty-five predictions over twenty-three
# observations filled the screen, and the material an operator was
# actually studying was scattered through it.
#
# WHAT THE EXPERIMENTS ACTUALLY ARE.
#
# Material-centric. A bench session is "Activated Carbon: pure, then
# 90, 70, 50, 30 and 10 percent in soil, twice each" - ONE dataset with
# twelve observations in it. So the top level of this screen is the
# list of materials the operator has established real truth for, and
# everything else is inside one of them.
#
# WHAT FOUNDS A DATASET, AND WHAT DOES NOT.
#
# Only human-established truth. A model that predicted Activated
# Carbon does not put an observation into the Activated Carbon dataset
# - that is the system teaching itself, and the whole architecture
# exists to prevent it. UNKNOWN_SAMPLE, NO_LABEL, family-only labels
# and mixtures whose target cannot be established are all kept, all
# inspectable, and none of them appears as a material dataset.
#
# DATA RETENTION IS NOT OPERATOR PRESENTATION. Every historical
# prediction from every model version is still in the database and is
# still readable, one screen down. It simply does not get a top-level
# row any more.

# How many ungrouped rows to name per reason before summarising.
# Enough to recognise what is in there; the material datasets are
# what the screen is for.
UNGROUPED_SHOWN = 8

OUTCOME_LABEL = {
    "EXACT": "CORRECT - named the material",
    "FAMILY": "FAMILY ONLY - right family, no material",
    "WRONG": "WRONG",
    "ABSTAINED": "ABSTAINED - no material answer",
}

UNGROUPED_TITLE = {
    "AMBIGUOUS_MIXTURE": "Ambiguous mixtures - target material not "
                         "recorded",
    "FAMILY_ONLY": "Family-level truth - no material was established",
    "UNKNOWN_SAMPLE": "Unknown samples - the operator recorded not "
                      "knowing",
    "NO_LABEL": "Unlabelled observations - no truth was recorded",
}

UNGROUPED_NOTE = {
    "AMBIGUOUS_MIXTURE":
        "More than one library component, and nothing recorded about "
        "which was under study. The system will not guess: assigning "
        "one would put a scientific claim in the database that nobody "
        "ever made. Re-label one with an explicit target to move it "
        "into that material's dataset.",
    "FAMILY_ONLY":
        "The truth is a family, not a material. Real evidence, but it "
        "cannot found a material dataset.",
    "UNKNOWN_SAMPLE":
        "The operator recorded that they did not know what this was. "
        "That is a useful record and is deliberately not a label - "
        "whatever a model said about it is a prediction, never truth.",
    "NO_LABEL":
        "No ground truth was recorded at all. The RAW spectra are "
        "kept and can be labelled later.",
}


def _fraction(value):
    return "-" if value is None else "{:.1f}%".format(value * 100.0)


def _composition(record):
    """
    One line saying what was actually in the cup.

    The matrix is never dropped and never rendered as "the rest". Two
    Activated Carbon 20% measurements, one in garden soil and one in a
    regolith simulant, are not the same experiment and this is the line
    that says so.
    """
    if record.get("experiment_type") == "PURE_MATERIAL":
        return "{} 100%".format(record.get("target_material_key"))

    parts = []

    for component in record.get("components") or []:
        name = (
            component.get("material_key")
            or component.get("matrix_label")
            or "?"
        )
        parts.append("{} {}".format(
            name, _fraction(component.get("prepared_mass_fraction"))
        ))

    return " + ".join(parts) or "composition not recorded"


def _matrix_line(record):
    matrices = record.get("matrix")

    if not matrices:
        return "-"

    return ", ".join(
        "{} {}".format(
            entry.get("label") or entry.get("material_key") or "?",
            _fraction(entry.get("fraction")),
        )
        for entry in matrices
    )


def _context_line(context):
    """Distance, packing, moisture - whichever were actually recorded."""
    if not context:
        return "-"

    parts = []

    for key, label, unit in (
        ("sensor_to_sample_mm", "d", " mm"),
        ("sample_mass_g", "m", " g"),
        ("packing", "", ""),
        ("moisture", "", ""),
    ):
        value = context.get(key)

        if value in (None, "", "UNKNOWN"):
            continue

        parts.append("{}{}{}".format(
            "{}=".format(label) if label else "", value, unit))

    return ", ".join(parts) or "-"


def learning_unavailable(mission, title):
    """
    Say the learning database is missing, and mean it.

    Checked at the top of EVERY learning screen, not only the one that
    happens to be the usual way in. A rover measurement must still work
    when the history is unavailable, so `Mission` records the failure
    and carries on - which means any of these screens can be entered
    with `mission.learning` set to None, and an AttributeError there
    would look like a program fault rather than a missing file.
    """
    if mission.learning is not None:
        return False

    banner(title)

    print("The learning database is not available:")
    print("  {}".format(mission.learning_error or "no reason recorded"))
    print()
    print("Measuring still works. Nothing is lost: an observation can be")
    print("saved to the history later, from the Sample it was taken of.")
    print()
    pause()

    return True


def menu_learning_history(mission):
    """
    The Decision Learning database, organised the way the work is.

    Materials first. Everything else - the mixtures gap report, model
    performance, the observations with no material truth - is one
    keystroke away and does not compete with them for the screen.
    """
    if learning_unavailable(mission, "DECISION LEARNING DATABASE"):
        return

    while True:
        banner("DECISION LEARNING DATABASE")

        status = mission.learning.status()
        datasets = mission.learning.material_datasets()
        ungrouped = mission.learning.ungrouped_observations()

        print("Database: {}".format(status["file"]))
        print("Observations: {}    Labelled: {}    Predictions: {}".format(
            status["observations"], status["labelled"],
            status["predictions"],
        ))
        print()
        print("MATERIAL DATASETS  (operator-established truth only)")
        print()

        if datasets:
            print("{:<4} {:<34} {:>5} {:>7} {:>10}".format(
                "#", "Material", "Obs", "Pure", "Mixtures"))

            for index, entry in enumerate(datasets, start=1):
                print("{:<4} {:<34} {:>5} {:>7} {:>10}".format(
                    index,
                    entry["material_key"][:34],
                    entry["observations"],
                    entry["pure"],
                    entry["mixtures"],
                ))

        else:
            print("  Nothing yet. A material dataset appears when YOU say")
            print("  what a sample was - a known material, or a mixture you")
            print("  weighed. A model's prediction never creates one.")

        stray = sum(len(rows) for rows in ungrouped.values())

        print()
        print("Not a material dataset: {} observation(s)  [u]".format(stray))
        print()
        print("[number] Open a material dataset")
        print("[u] Observations with no material truth")
        print("[m] Model performance")
        print("[x] Prepared mixtures - what is still missing")
        print("[c] Sample presentation coverage")
        print("[0] Back")

        selection = choose()

        if selection == "0" or not selection:
            return

        if selection == "u":
            menu_ungrouped_observations(mission, ungrouped)

        elif selection == "m":
            menu_model_performance(mission)

        elif selection == "x":
            print_mixture_gaps(status)

        elif selection == "c":
            print_context_coverage(status)

        else:
            try:
                entry = datasets[int(selection) - 1]

            except (ValueError, IndexError):
                print("Not a listed number.")

                continue

            menu_material_dataset(mission, entry["material_key"])


def menu_material_dataset(mission, material_key):
    """
    One material's experiments: the pure reference and every mixture.

    THE 100% CASE BELONGS HERE. A sample labelled "this is Activated
    Carbon" is the pure end of the Activated Carbon dataset, not a
    separate kind of record - but it is still marked PURE, because a
    pure material and a prepared mixture are different experiment types
    and averaging them would hide the concentration series that is the
    entire point of collecting them.
    """
    if learning_unavailable(
        mission, "{} - LEARNING DATA".format(material_key.upper())
    ):
        return

    while True:
        banner("{} - LEARNING DATA".format(material_key.upper()))

        records = mission.learning.material_dataset(material_key)

        pure = [r for r in records if r["experiment_type"] == "PURE_MATERIAL"]
        mixtures = [r for r in records
                    if r["experiment_type"] == "PREPARED_MIXTURE"]

        print("Observations: {}   ({} pure, {} prepared mixture(s))".format(
            len(records), len(pure), len(mixtures)))
        print()

        if not records:
            print("  Nothing recorded for this material.")
            print()
            pause()

            return

        print("{:<4} {:<24} {:>8}  {:<40}".format(
            "#", "Measurement", "Target", "Composition as prepared"))
        print("{:<4} {:<24} {:>8}  {:<40}".format(
            "", "", "", "matrix / context"))
        print()

        for index, record in enumerate(records, start=1):
            print("{:<4} {:<24} {:>8}  {}".format(
                index,
                str(record["measurement_id"]),
                _fraction(record.get("target_fraction")),
                _composition(record),
            ))

            matrix = _matrix_line(record)
            context = _context_line(record.get("context"))

            if matrix != "-" or context != "-":
                print("{:<4} {:<24} {:>8}  matrix {} | {}".format(
                    "", "", "", matrix, context))

        # THE CONCENTRATION SERIES, which is what the dataset is for.
        fractions = sorted(
            record["target_fraction"] for record in records
            if record.get("target_fraction") is not None
        )

        if fractions:
            print()
            print("Concentrations measured: {}".format(
                ", ".join(_fraction(value) for value in fractions)))

        matrices = sorted({
            entry.get("label") or entry.get("material_key") or "?"
            for record in records
            for entry in (record.get("matrix") or [])
        })

        if matrices:
            print("Matrices used:           {}".format(", ".join(matrices)))

        print()
        print("[number] Open one observation")
        print("[m] How the models have done on this material")
        print("[0] Back")

        selection = choose()

        if selection == "0" or not selection:
            return

        if selection == "m":
            menu_model_performance(mission, material_key=material_key)

            continue

        try:
            record = records[int(selection) - 1]

        except (ValueError, IndexError):
            print("Not a listed number.")

            continue

        print_observation_detail(mission, record["measurement_id"])


def print_observation_detail(mission, measurement_id):
    """
    One observation, in full.

    GROUND TRUTH AND PREDICTIONS ARE IN SEPARATE BLOCKS and are never
    merged into an "identity" field. Seeing them side by side is the
    point: it is how a person notices the model was wrong, and merging
    them is how a system starts training on its own output.
    """
    if learning_unavailable(mission, "OBSERVATION {}".format(measurement_id)):
        return

    detail = mission.learning.observation_detail(measurement_id)

    if detail is None:
        print("No observation {}.".format(measurement_id))
        print()
        pause()

        return

    banner("OBSERVATION {}".format(measurement_id))

    observation = detail["observation"] or {}
    truth = detail["ground_truth"] or {}

    print("WHEN AND HOW")
    print()
    print("  Recorded:        {}".format(observation.get("created_at")))
    print("  Session:         {}".format(
        observation.get("session_id") or "-"))
    print("  Origin:          {}".format(observation.get("origin")))
    print("  Profile:         {}".format(
        observation.get("acquisition_profile_id") or "-"))
    print("  Calibration:     {}".format(
        observation.get("calibration_id") or "-"))
    print("  Legacy cal:      {}".format(
        observation.get("legacy_calibration_id") or "-"))
    print("  RAW hash:        {}".format(
        str(observation.get("raw_hash") or "-")[:32]))

    print()
    print("GROUND TRUTH  (established by the operator, never by a model)")
    print()
    print("  Label type:      {}".format(truth.get("label_type") or "-"))
    print("  Target material: {}".format(
        detail.get("target_material_key") or "NOT ESTABLISHED"))
    print("  Target known by: {}".format(
        detail.get("target_source") or "not derivable - ambiguous"))
    print("  Target fraction: {}".format(
        _fraction(detail.get("target_fraction"))))
    print("  Verified as:     {} ({})".format(
        truth.get("verification_status") or "-",
        truth.get("verification_source") or "-",
    ))

    if truth.get("note"):
        print()

        for line in textwrap.wrap("  Note: {}".format(truth["note"]), 66):
            print("  {}".format(line))

    components = detail.get("components") or []

    if components:
        print()
        print("  COMPOSITION AS PREPARED")
        print()
        print("    {:<30} {:>8} {:>9}  {}".format(
            "component", "fraction", "mass", "role"))

        for component in components:
            print("    {:<30} {:>8} {:>9}  {}".format(
                str(component.get("material_key")
                    or component.get("matrix_label") or "?")[:30],
                _fraction(component.get("prepared_mass_fraction")),
                "-" if component.get("mass_g") is None
                else "{:.2f} g".format(component["mass_g"]),
                component.get("role"),
            ))

    context = detail.get("context")

    print()
    print("SAMPLE PRESENTATION")
    print()

    if context:
        for key, label in (
            ("sensor_to_sample_mm", "Distance (mm)"),
            ("sample_mass_g", "Mass (g)"),
            ("sample_depth_mm", "Depth (mm)"),
            ("packing", "Packing"),
            ("moisture", "Moisture"),
            ("grain_size", "Grain size"),
            ("container", "Container"),
            ("ambient_light", "Ambient light"),
            ("substrate", "Substrate"),
        ):
            value = context.get(key)

            if value is not None:
                print("  {:<18} {}".format(label, value))

    else:
        print("  Nothing recorded. Until distance and packing are known, a")
        print("  difference between two measurements of one material")
        print("  cannot be attributed to either of them.")

    print()
    print("RAW SPECTRA")
    print()

    raw = observation.get("raw") or {}

    if raw:
        print_triad_table(raw)

    else:
        print("  no RAW stored with this observation")

    print()
    print("MODEL PREDICTIONS  (what a model said - never ground truth)")
    print()

    predictions = detail.get("predictions") or []

    if not predictions:
        print("  No model has been run against this observation.")

    for prediction in predictions:
        outcome = classify_prediction({
            "level": prediction.get("level"),
            "predicted_material": prediction.get("material_key"),
            "predicted_family": prediction.get("family_id"),
            "truth_material": truth.get("material_key")
            or detail.get("target_material_key"),
            "truth_family": truth.get("family_id"),
        })

        print("  {}".format(prediction.get("model_version")))
        print("    predicted at: {}".format(prediction.get("predicted_at")))
        print("    level:        {}".format(prediction.get("level")))
        print("    material:     {}".format(
            prediction.get("material_key") or "none named"))
        print("    family:       {}".format(
            prediction.get("family_id") or "none named"))
        print("    confidence:   {}".format(
            prediction.get("confidence") or "-"))
        print("    outcome:      {}".format(
            OUTCOME_LABEL.get(outcome, outcome)))
        print()

    print()
    pause()


def menu_ungrouped_observations(mission, ungrouped=None):
    """
    Everything that is real evidence but not a material dataset.

    Kept, inspectable, and deliberately out of the main list. Four
    reasons, each with a different remedy, so a screen that lumped them
    together would be telling the operator less than the database
    knows.
    """
    if learning_unavailable(mission, "OBSERVATIONS WITH NO MATERIAL TRUTH"):
        return

    ungrouped = ungrouped or mission.learning.ungrouped_observations()

    banner("OBSERVATIONS WITH NO MATERIAL TRUTH")

    print("None of these is deleted and none is hidden. They are simply")
    print("not material datasets, because no material truth was")
    print("established for them.")

    for reason in ("AMBIGUOUS_MIXTURE", "FAMILY_ONLY", "UNKNOWN_SAMPLE",
                   "NO_LABEL"):
        rows = ungrouped.get(reason) or []

        print()
        print("{}  ({})".format(UNGROUPED_TITLE[reason], len(rows)))

        if not rows:
            continue

        print()

        for line in textwrap.wrap(UNGROUPED_NOTE[reason], 66):
            print("  {}".format(line))

        print()

        for record in rows[:UNGROUPED_SHOWN]:
            parts = [str(record.get("measurement_id"))[:24]]

            if reason == "AMBIGUOUS_MIXTURE":
                parts.append(" + ".join(
                    "{} {}".format(
                        component.get("material_key")
                        or component.get("matrix_label") or "?",
                        _fraction(component.get("prepared_mass_fraction")),
                    )
                    for component in record.get("components") or []
                ))

            elif reason == "FAMILY_ONLY":
                parts.append("family {}".format(record.get("family_id")))

            print("    {}".format("   ".join(parts)))

        if len(rows) > UNGROUPED_SHOWN:
            print("    ... and {} more".format(len(rows) - UNGROUPED_SHOWN))

    print()
    pause()


def menu_model_performance(mission, material_key=None):
    """
    How each model version has done against operator-established truth.

    NOT AN ACCURACY. Four levels, scored as four outcomes, because they
    are not interchangeable: abstaining on a genuinely ambiguous sample
    is correct behaviour, and one percentage that mixes it with a wrong
    answer would reward a model for guessing.

    Every model version that has ever run is still here. What changed
    is that they no longer each get a top-level row on the main screen.
    """
    if learning_unavailable(mission, "MODEL PERFORMANCE"):
        return

    while True:
        banner("MODEL PERFORMANCE{}".format(
            " - {}".format(material_key.upper()) if material_key else ""))

        models = mission.learning.model_performance(
            material_key=material_key)

        if not models:
            print("No model has been scored against established truth yet.")
            print()
            print("A model is scored only where the operator recorded what")
            print("the sample was. A prediction against an unlabelled")
            print("sample has nothing to be right or wrong about.")
            print()
            pause()

            return

        print("Scored against VERIFIED operator truth only.")
        print()
        print("{:<4} {:<24} {:>5} {:>7} {:>8} {:>7} {:>11}".format(
            "#", "Model version", "Obs", "Exact", "Family", "Wrong",
            "Abstained"))

        for index, entry in enumerate(models, start=1):
            print("{:<4} {:<24} {:>5} {:>7} {:>8} {:>7} {:>11}".format(
                index,
                entry["model_version"][:24],
                entry["observations"],
                entry["exact"],
                entry["family"],
                entry["wrong"],
                entry["abstained"],
            ))

        print()
        print("  Exact      named the material, and it was right")
        print("  Family     named the right family, without the material")
        print("  Wrong      named something else")
        print("  Abstained  gave no material answer. On a genuinely")
        print("             ambiguous sample this is correct behaviour,")
        print("             which is why it is not counted as a miss.")
        print()
        print("[number] Every scored observation for one model")
        print("[0] Back")

        selection = choose()

        if selection == "0" or not selection:
            return

        try:
            entry = models[int(selection) - 1]

        except (ValueError, IndexError):
            print("Not a listed number.")

            continue

        print_model_rows(entry)


def print_model_rows(entry):
    """
    The row-level history for one model, with the context to read it.

    THE DETAIL THE OLD TABLE LEFT OUT. Measurement id, the established
    truth, what the model actually returned and at which level, its
    confidence, and the outcome named rather than implied. `None` is
    never printed as a verdict: ABSTAINED, FAMILY ONLY and WRONG are
    three different things and the model returned one of them.
    """
    banner("{}".format(entry["model_version"]))

    # TWO LINES PER ROW, not one truncated to death.
    #
    # The old confusion table cut the model version to twelve characters
    # and the material names to twenty-six, on one line, and the result
    # was unreadable: FREYA_DECISI and LEGACY_ANALY are not names an
    # operator can act on, and MATERIAL_F is not a level. Nothing here
    # is abbreviated - the second line simply carries what will not fit
    # beside the first.
    for row in entry["rows"]:
        said = (
            row.get("predicted_material")
            or (row.get("predicted_family")
                and "family {}".format(row["predicted_family"]))
            or "no material answer"
        )

        print("{:<26} {}".format(
            str(row.get("measurement_id")),
            OUTCOME_LABEL.get(row.get("outcome"), row.get("outcome")),
        ))
        print("{:<26}   truth: {}".format(
            "", row.get("target") or row.get("truth_material")))

        # WHAT WAS ACTUALLY IN THE CUP. A wrong answer on 10% carbon in
        # soil and a wrong answer on the pure material are not the same
        # result, and the old table showed neither.
        if row.get("components"):
            print("{:<26}   as prepared: {}".format("", " + ".join(
                "{} {}".format(
                    component.get("material_key")
                    or component.get("matrix_label") or "?",
                    _fraction(component.get("prepared_mass_fraction")),
                )
                for component in row["components"]
            )))

        print("{:<26}   said:  {}  [{}{}]".format(
            "", said, row.get("level") or "-",
            ", confidence {}".format(row["confidence"])
            if row.get("confidence") else "",
        ))
        print()

    print("Predictions are immutable and are kept per model version. A")
    print("newer model re-analysing an old measurement adds a row; it")
    print("never replaces one.")
    print()
    pause()


def print_mixture_gaps(status):
    """
    Prepared mixtures, as a gap report rather than a total.

    "4 mixtures" reads like progress. The useful question at the bench
    is what is still missing before any of it can be validated.
    """
    banner("PREPARED MIXTURES")

    mixtures = status.get("mixtures") or {}

    if not mixtures.get("mixtures"):
        print("None yet.")
        print()
        print("A prepared mixture is the only thing that can ever turn a")
        print("spectral contribution into a percentage. Mix a known mass")
        print("of a library material into a known mass of soil, measure")
        print("it, and save it as a KNOWN PREPARED MIXTURE.")
        print()
        pause()

        return

    print("{} mixture(s) over {} material(s)".format(
        mixtures["mixtures"], mixtures["materials_spiked"]))
    print()
    print("{:<34} {:>5} {:>10} {:>10}".format(
        "material", "n", "lowest", "highest"))

    for material, entry in sorted(mixtures["by_material"].items()):
        print("{:<34} {:>5} {:>9.1f}% {:>9.1f}%".format(
            material[:34], entry["n"],
            (entry["lowest_fraction"] or 0.0) * 100.0,
            (entry["highest_fraction"] or 0.0) * 100.0,
        ))

    if mixtures.get("matrices"):
        print()
        print("mixed into: {}".format(", ".join(
            "{} (x{})".format(name, count)
            for name, count in sorted(mixtures["matrices"].items())
        )))

    print()
    print("Score them with:")
    # sys.executable, not `py`: the launcher exists only on Windows
    # and the operator client runs on the Linux main computer.
    print("  {} firmware/research/training/evaluate_mixtures.py".format(
        sys.executable))
    print()
    pause()


def print_context_coverage(status):
    banner("SAMPLE PRESENTATION COVERAGE")

    context = status.get("sample_context") or {}

    print("Distance, mass and packing recorded on {} of {} "
          "observation(s).".format(
              context.get("with_any_context", 0),
              context.get("observations", 0),
          ))

    by_field = context.get("by_field") or {}

    if any(by_field.values()):
        print()

        for field, count in sorted(by_field.items()):
            if count:
                print("  {:<24} {}".format(field, count))

    else:
        print()
        print("Nothing yet. Until distance and packing are recorded, a")
        print("difference between two measurements of one material")
        print("cannot be attributed to either of them.")

    print()
    pause()


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



# ======================================================================
# THREE STORES, NEVER IMPLICIT
# ======================================================================
# A Sample can be in up to three places at once, and until this release
# the screen showed one of them and called it "the sample database".
#
#     ESP32     the device's own buffer. One acquisition per slot, in
#               RAM, gone when the board resets - which happens every
#               time the serial port is opened.
#     SESSION   the PC's working set for the run in progress. Written
#               by Prepare and Measure.
#     ARCHIVE   the PC's permanent record. Written ONLY by an explicit
#               import.
#
# Every table below names all three, and every destructive operation
# names exactly one.

STORE_ESP32 = "ESP32"
STORE_SESSION = "SESSION"
STORE_ARCHIVE = "ARCHIVE"

# WHAT EACH STORE IS, IN ONE SENTENCE THE OPERATOR CAN ACT ON.
#
# The test these have to pass is: after reading this, can the operator
# answer "if I close this program right now, where does s01 still
# exist?" Both of the first two entries used to fail it. The ESP32 was
# described as a RAM buffer emptied by opening the serial port -
# neither half is true any more, and the second half was never true of
# this client, which holds DTR and RTS low precisely so the board does
# not reset. The session was described as surviving a restart, which is
# what it did when it was written to samples.json and is exactly what
# it must not do now.
STORE_NOTES = (
    (STORE_ESP32, "the device's own store, one acquisition per slot, on "
                  "the ESP32's filesystem. It SURVIVES a board reset, a "
                  "power cut and a restart of this program. Only a "
                  "delete here removes it."),
    (STORE_SESSION, "this run's working set, in THIS PROGRAM'S MEMORY. "
                    "Prepare and Measure write here. It is NOT a file: "
                    "closing this program discards it, and nothing in "
                    "it is saved on the PC until you import or archive "
                    "it. The measurement itself is safe - the device "
                    "still has it."),
    (STORE_ARCHIVE, "the permanent PC record, in samples.json on this "
                    "computer. The only PC store that survives closing "
                    "this program, and nothing arrives here except by "
                    "an explicit import or archive."),
)


def esp32_index(mission):
    """
    What the device is holding, or why we could not ask.

    Never raises. An unreachable ESP32 must not stop the operator from
    looking at, or deleting from, the PC's own stores - and it must not
    be reported as an empty device either, because "no samples" and "no
    answer" call for opposite responses.
    """
    try:
        index = mission.link.list_saved_samples()

    except (LinkError, TimeoutError, DeviceError) as error:
        return {
            "reachable": False,
            "entries": [],
            "by_id": {},
            "error": getattr(error, "message", None) or str(error),
        }

    entries = [
        entry for entry in (index.get("samples") or [])
        if entry.get("sample_id")
    ]

    return {
        "reachable": True,
        "entries": entries,
        # What the device says about its OWN storage, carried through
        # rather than assumed by the screen that prints it.
        "durable": bool(index.get("durable")),
        "storage": index.get("storage"),
        "by_id": {entry["sample_id"]: entry for entry in entries},
        "error": None,
    }


def storage_rows(mission, device=None):
    """
    One row per Sample ID, saying which of the three stores holds it.

    The union, not any one store's list. A Sample the ESP32 has and the
    PC does not is exactly the row an operator needs to see before
    pressing anything destructive, and it is the row the previous
    screen could not draw at all.
    """
    device = device if device is not None else esp32_index(mission)

    in_session = {
        record.get("sample_id"): record
        for record in mission.session._records()
    }
    in_archive = {
        record.get("sample_id"): record
        for record in mission.archive._records()
    }

    order = []
    seen = set()

    for sample_id in (
        list(device["by_id"]) + list(in_session) + list(in_archive)
    ):
        if sample_id not in seen:
            seen.add(sample_id)
            order.append(sample_id)

    rows = []

    for sample_id in order:
        # The SESSION copy describes the run in progress, so it is the
        # one the summary columns describe when both exist. The archive
        # copy is still carried on the row, and `open_sample` asks which
        # one to read rather than choosing.
        record = in_session.get(sample_id) or in_archive.get(sample_id)
        summary = summary_of(record) if record else {}
        on_device = device["by_id"].get(sample_id)

        rows.append({
            "sample_id": sample_id,
            "on_esp32": on_device is not None,
            "in_session": sample_id in in_session,
            "in_archive": sample_id in in_archive,
            "device_entry": on_device,
            "session_record": in_session.get(sample_id),
            "archive_record": in_archive.get(sample_id),
            "slot_id": (
                summary.get("slot_id")
                or (on_device or {}).get("slot_id")
            ),
            "state": summary.get("state"),
            "measurement_count": summary.get("measurement_count", 0),
            "analysis_run_count": summary.get("analysis_run_count", 0),
            # A DECISION THAT NAMED NOTHING IS STILL A DECISION.
            #
            # `interpretation` is the material or the family, and an
            # AMBIGUOUS_SET conclusion has neither - so the column read
            # "-" for a measurement the model had thought hard about,
            # which is indistinguishable from one that was never
            # analysed. The level fills in, because "AMBIGUOUS_SET" and
            # "not analysed" are different things.
            "interpretation": (
                summary.get("interpretation")
                or summary.get("decision_status")
            ),
            "decision_status": summary.get("decision_status"),
        })

    return rows


def _mark(present):
    return "yes" if present else " - "


def print_storage_table(rows, device):
    """
    The union table. Location is a column, never an assumption.

    The three store columns come BEFORE the scientific ones on purpose:
    the first question at this screen is "where is it", and every
    destructive option below the table is answered by these columns.
    """
    print("{:<4} {:<12} {:<5} {:<6} {:<8} {:<8} {:<13} {:>3} {:>3} "
          "{:<18}".format(
              "#", "Sample", "Slot", "ESP32", "SESSION", "ARCHIVE",
              "State", "M", "A", "Interpretation"))

    if not rows:
        print()
        print("  No Sample is held in any of the three stores.")

    for index, row in enumerate(rows, start=1):
        print("{:<4} {:<12} {:<5} {:<6} {:<8} {:<8} {:<13} {:>3} {:>3} "
              "{:<18}".format(
                  index,
                  str(row["sample_id"])[:12],
                  str(row["slot_id"] or "-"),
                  _mark(row["on_esp32"]),
                  _mark(row["in_session"]),
                  _mark(row["in_archive"]),
                  str(row["state"] or "-")[:13],
                  row["measurement_count"],
                  row["analysis_run_count"],
                  str(row["interpretation"] or "-")[:18],
              ))

    print()

    if device["reachable"]:
        # THE DEVICE SAYS WHETHER IT IS DURABLE. `list_saved_samples`
        # carries `durable`, and a board whose filesystem is
        # unavailable is holding those acquisitions in RAM - which is
        # exactly the case where telling the operator they survive a
        # reset would be the most expensive sentence on the screen.
        if device.get("durable"):
            print("ESP32:   {} acquisition(s) held  - on the device's own "
                  "filesystem; survives a reset.".format(
                      len(device["entries"])))

        else:
            print("ESP32:   {} acquisition(s) held  - IN RAM ONLY on this "
                  "board; a reset would lose them.".format(
                      len(device["entries"])))
    else:
        # NOT "0". An unreachable device is not an empty one, and the
        # difference decides whether "delete all from ESP32" has
        # anything to do.
        print("ESP32:   NOT REACHABLE - {}".format(
            device["error"] or "no answer"))
        print("         The device's own copies are neither shown nor "
              "counted above.")

    # EACH COLUMN SAYS WHAT IT SURVIVES, on the screen where the
    # deletes are. The three headings are ESP32 / SESSION / ARCHIVE and
    # nothing about those words tells an operator that closing the
    # client empties the middle one - so the counts say it, every time,
    # rather than only inside [s].
    print("SESSION: {} sample(s)  - THIS PROGRAM'S MEMORY. Closing the "
          "client discards it.".format(
              sum(1 for row in rows if row["in_session"])))
    print("ARCHIVE: {} sample(s)  - kept on this PC. Reached only by an "
          "import.".format(
              sum(1 for row in rows if row["in_archive"])))


def pick_row(rows, predicate, prompt, empty_message):
    """
    Choose one Sample from the rows that satisfy a storage predicate.

    The filter is the point: "delete from the ESP32" offers only rows
    the ESP32 actually has, so the operator cannot select a Sample that
    the chosen operation could not act on and then be told so
    afterwards.
    """
    eligible = [row for row in rows if predicate(row)]

    if not eligible:
        print()
        print(empty_message)

        return None

    print()

    for index, row in enumerate(eligible, start=1):
        print("  [{}] {:<14} ESP32 {}   SESSION {}   ARCHIVE {}".format(
            index, str(row["sample_id"])[:14],
            _mark(row["on_esp32"]), _mark(row["in_session"]),
            _mark(row["in_archive"]),
        ))

    print()
    answer = ask("{} (blank = cancel)".format(prompt))

    if not answer:
        return None

    try:
        chosen = eligible[int(answer) - 1]

    except (ValueError, IndexError):
        print("Not a listed number.")

        return None

    return chosen


def _elsewhere(row, keep):
    """What is left after deleting from `keep`, as an operator sentence."""
    remaining = []

    for name, present in (
        (STORE_ESP32, row["on_esp32"]),
        (STORE_SESSION, row["in_session"]),
        (STORE_ARCHIVE, row["in_archive"]),
    ):
        if present and name != keep:
            remaining.append(name)

    return remaining


def confirm_deletion(target, sample_ids, remaining, extra=None):
    """
    A destructive confirmation that names its storage and its survivors.

    THE SURVIVORS ARE HALF THE INFORMATION. "Delete s01?" is a question
    an operator cannot answer; "delete the ESP32 copy of s01, leaving
    the PC archive copy" is one they can. Where nothing survives, this
    says THAT, in the same place, in the same words.
    """
    print()
    print(RULE)
    print()
    print("Target:        {}".format(target))
    print("Samples:       {}".format(len(sample_ids)))

    for sample_id in sample_ids:
        print("                 {}".format(sample_id))

    if remaining:
        print("Left intact:   {}".format(", ".join(remaining)))
    else:
        print("Left intact:   NOTHING - this is the last copy of {}".format(
            "these samples" if len(sample_ids) != 1 else "this sample"))

    print("Untouched:     Decision Learning, DB1, DB2, DB3, calibrations")

    for line in extra or []:
        print("               {}".format(line))

    print()

    answer = ask("Delete {} from {}? [y/N]".format(
        "all {} sample(s)".format(len(sample_ids))
        if len(sample_ids) != 1 else sample_ids[0],
        target,
    ))

    return answer.strip().lower() in ("y", "yes")


# ======================================================================
# the screen
# ======================================================================


def menu_sample_database(mission):
    """
    View and manage Samples across all three stores.

    Measurement fields are read-only here: raw, dark_corrected,
    normalized, sensor settings, matches, analysis and calibration are
    scientific results, not editable text. Metadata may be corrected.

    EVERY DESTRUCTIVE OPTION NAMES ITS STORE. There is deliberately no
    "Delete sample": the previous screen had one, it removed the PC
    record and left the device copy alone, and no operator could have
    known that from the menu.
    """
    device = esp32_index(mission)

    while True:
        banner("SAMPLE DATABASE")

        rows = storage_rows(mission, device)
        print_storage_table(rows, device)

        print()
        print("[1] Open a sample")
        print()
        print("COPY - never moves, never overwrites")
        print("[2] Import ALL samples from ESP32 to the PC archive")
        print("[3] Archive one SESSION sample to the PC archive")
        print()
        print("EDIT - the PC archive")
        print("[4] Edit metadata")
        print("[5] Rename")
        print()
        print("DELETE - each names exactly one store")
        print("[6] Delete a sample from the ESP32")
        print("[7] Delete a sample from the PC session")
        print("[8] Delete a sample from the PC archive")
        print("[9] Delete ALL samples from the ESP32")
        print("[10] Clear the PC session working set")
        print()
        print("[s] What these three stores are")
        print("[r] Refresh   [0] Back")

        selection = choose()

        try:
            if selection == "0":
                return

            if selection in ("r", "R"):
                # Both stores re-read, and the device asked again. A
                # refresh that only reloaded the PC would leave the
                # ESP32 column showing what the device held a minute
                # ago, which is the column the destructive options
                # below are read from.
                mission.samples.load()
                device = esp32_index(mission)

                continue

            if not selection:
                continue

            if selection == "s":
                print_store_help()

                continue

            if selection == "1":
                open_sample(mission, rows)

            elif selection == "2":
                import_esp32_samples(mission)
                mission.samples.load()
                device = esp32_index(mission)

            elif selection == "3":
                archive_session_sample(mission, rows)
                device = esp32_index(mission)

            elif selection == "4":
                edit_archive_metadata(mission, rows)

            elif selection == "5":
                rename_archive_sample(mission, rows)

            elif selection == "6":
                delete_one_from_esp32(mission, rows)
                device = esp32_index(mission)

            elif selection == "7":
                delete_one_from_collection(
                    mission, rows, mission.session, STORE_SESSION,
                    "in_session",
                )

            elif selection == "8":
                delete_one_from_collection(
                    mission, rows, mission.archive, STORE_ARCHIVE,
                    "in_archive",
                )

            elif selection == "9":
                delete_all_esp32_samples(mission)
                device = esp32_index(mission)

            elif selection == "10":
                clear_session(mission, rows)

            elif selection:
                print("Unknown option.")

        except StorageError as error:
            print()
            print("Storage error: {} ({})".format(error.message, error.code))


def print_store_help():
    """What the three stores are, in the operator's own vocabulary."""
    print()
    print(RULE)
    print()

    for name, description in STORE_NOTES:
        print("{}".format(name))

        for line in textwrap.wrap(description, 62):
            print("   {}".format(line))

        print()

    print("A sample is only in the stores its row says it is in.")
    print("Import COPIES; it never moves and never overwrites.")
    print("Every delete names one store and touches only that one.")
    print()
    pause()


def open_sample(mission, rows):
    """
    Read one Sample, from a store the operator chooses.

    Asked rather than guessed, because the two PC copies of one Sample
    ID can legitimately differ - a second measurement taken after the
    first was archived is exactly that - and showing one while the
    operator believes they are looking at the other is how a conflict
    goes unnoticed.
    """
    row = pick_row(
        rows,
        lambda entry: entry["in_session"] or entry["in_archive"],
        "Sample number",
        "No Sample is held on the PC yet. Import from the ESP32, or "
        "measure one.",
    )

    if row is None:
        return

    if row["in_session"] and row["in_archive"]:
        print()
        print("{} is in BOTH PC stores.".format(row["sample_id"]))
        print("  [1] the SESSION copy")
        print("  [2] the ARCHIVE copy")

        answer = choose("Which")

        record = (
            row["archive_record"] if answer == "2" else row["session_record"]
        )
        source = STORE_ARCHIVE if answer == "2" else STORE_SESSION

    elif row["in_archive"]:
        record, source = row["archive_record"], STORE_ARCHIVE

    else:
        record, source = row["session_record"], STORE_SESSION

    print()
    print("Reading the {} copy.".format(source))

    print_full_sample(record)


def edit_archive_metadata(mission, rows):
    row = pick_row(
        rows, lambda entry: entry["in_archive"], "Sample number",
        "The PC archive is empty. Metadata is edited on archived "
        "records; import first.",
    )

    if row is None:
        return

    metadata = ask_metadata()

    if metadata:
        mission.archive.update_metadata(row["sample_id"], metadata)
        print("Metadata updated on the PC archive copy.")

    else:
        print("Nothing entered; metadata unchanged.")


def rename_archive_sample(mission, rows):
    row = pick_row(
        rows, lambda entry: entry["in_archive"], "Sample number",
        "The PC archive is empty.",
    )

    if row is None:
        return

    new_id = ask("New Sample ID (blank = cancel)")

    if not new_id:
        return

    mission.archive.rename(row["sample_id"], new_id)

    print("Renamed {} -> {} IN THE PC ARCHIVE.".format(
        row["sample_id"], new_id))

    if row["in_session"] or row["on_esp32"]:
        # Said plainly rather than propagated. Renaming across stores
        # would be a write to a store the operator did not name, and
        # the ESP32 has no rename at all.
        print("The SESSION and ESP32 copies still use {}.".format(
            row["sample_id"]))


# ======================================================================
# IMPORT - the one door into the PC archive
# ======================================================================


def import_esp32_samples(mission):
    """
    Copy every acquisition the ESP32 holds into the PC ARCHIVE.

    THIS IS THE ONLY WAY A DEVICE MEASUREMENT REACHES THE ARCHIVE.
    Nothing else in the program writes there on the operator's behalf,
    which is what makes this button mean what it says.

    A COPY, NEVER A MOVE: the ESP32 keeps its records and this command
    is deliberately not chained to a delete. An ID that already exists
    on the PC is never overwritten.

        ID not in the archive              -> IMPORT
        ID in the archive, same spectrum   -> SKIP, already have it
        ID in the archive, different data  -> CONFLICT, left untouched

    Running it twice therefore transfers nothing the second time, and a
    run interrupted half way can simply be run again: every sample is
    committed on its own, so the ones that landed stay landed.

    It writes to the archive and to nothing else. The Decision Learning
    database, the material libraries and the White/Dark references are
    never opened for writing anywhere in this program.
    """
    banner("IMPORT ESP32 SAMPLES TO THE PC ARCHIVE")

    device = esp32_index(mission)

    if not device["reachable"]:
        print("Reading Sample index............ FAIL")
        print()
        print("  {}".format(device["error"]))
        print()
        print("NOTHING WAS IMPORTED. The PC archive is unchanged.")
        print()
        pause()

        return {"imported": [], "skipped": [], "conflicts": [],
                "failed": [], "reachable": False}

    print("Connecting to ESP32............. PASS")
    print("Reading Sample index............ PASS")
    print()

    entries = device["entries"]

    print("Source: ESP32          {} acquisition(s)".format(len(entries)))
    print("Target: PC archive     {} sample(s) before".format(
        mission.archive.count()))
    print()

    if not entries:
        print("The ESP32 is holding no acquisitions. Its buffer is RAM")
        print("only and is empty after a reset.")
        print()
        pause()

        return {"imported": [], "skipped": [], "conflicts": [],
                "failed": [], "reachable": True}

    imported = []
    skipped = []
    conflicts = []
    failed = []

    for entry in entries:
        sample_id = entry.get("sample_id")

        try:
            payload = mission.link.get_saved_sample(sample_id)

        except (LinkError, TimeoutError, DeviceError) as error:
            # A CONNECTION FAILURE PART WAY THROUGH IS NOT A ROLLBACK.
            # Everything already committed is valid and stays; this one
            # is reported and the loop carries on, so a flaky link
            # costs the samples it actually dropped and no others.
            failed.append(sample_id)
            print("{:<14}FAILED            {}".format(
                sample_id, getattr(error, "message", None) or error))

            continue

        device_raw = _white_spectrum(payload.get("measurement"))

        if not device_raw:
            # An entry the device lists but cannot produce a spectrum
            # for is a corrupt or incomplete record. Refused rather
            # than stored as an empty measurement that would look like
            # a dark reading forever after.
            failed.append(sample_id)
            print("{:<14}NO SPECTRUM       refused".format(sample_id))

            continue

        existing = mission.archive.get_sample(sample_id)

        if existing is not None:
            stored_raw = _white_spectrum(
                latest_measurement(existing, successful_only=True)
                or existing.get("measurement")
            )

            if _same_spectrum(stored_raw, device_raw):
                skipped.append(sample_id)
                print("{:<14}already archived  SKIP".format(sample_id))

            else:
                conflicts.append(sample_id)
                print("{:<14}DIFFERENT DATA    CONFLICT".format(sample_id))

            continue

        try:
            _import_retained(mission, sample_id, entry, payload)

            imported.append(sample_id)
            print("{:<14}imported          PASS".format(sample_id))

        except StorageError as error:
            failed.append(sample_id)
            print("{:<14}FAILED            {}".format(
                sample_id, error.message))

    print()
    print("Imported:   {}".format(len(imported)))
    print("Skipped:    {}  (identical copy already in the archive)".format(
        len(skipped)))
    print("Conflicts:  {}".format(len(conflicts)))
    print("Failed:     {}".format(len(failed)))

    if conflicts:
        print()
        print("CONFLICTS - the archive already holds these IDs with")
        print("DIFFERENT measurement data. NOTHING WAS OVERWRITTEN. The")
        print("archived record is a scientific result and this command")
        print("will not replace one. Rename the archived copy, or delete")
        print("it deliberately, if the device copy is the one you want:")

        for sample_id in conflicts:
            print("  {}".format(sample_id))

    print()
    print("ESP32:       NOT MODIFIED - it still holds every acquisition")
    print("PC session:  NOT MODIFIED")
    print("PC archive:  {} sample(s) now, in {}".format(
        mission.archive.count(), mission.archive.path))
    print()
    pause()

    return {
        "imported": imported,
        "skipped": skipped,
        "conflicts": conflicts,
        "failed": failed,
        "reachable": True,
    }


def archive_session_sample(mission, rows):
    """
    Copy one measured SESSION sample into the PC archive.

    The second explicit import, and the one that matters when the ESP32
    has already been reset: the device's buffer is RAM and the session
    file is not, so this is how the science of a finished run is kept
    without the board still being powered.

    A COPY. The session keeps its record, so a run in progress is not
    disturbed by archiving part of it.
    """
    row = pick_row(
        rows,
        lambda entry: entry["in_session"],
        "Session sample number",
        "The session working set is empty.",
    )

    if row is None:
        return None

    sample_id = row["sample_id"]
    record = row["session_record"]

    if row["in_archive"]:
        print()
        print("The archive already holds {}.".format(sample_id))
        print("Nothing was overwritten - an archived record is a")
        print("scientific result, and this command will not replace one.")
        print()
        print("Rename the archived copy first if you want both.")
        print()
        pause()

        return None

    if not successful_measurements(record):
        print()
        print("{} has no successful measurement. There is no spectrum "
              "to archive.".format(sample_id))
        print()
        pause()

        return None

    stored = mission.archive.adopt(record)

    print()
    print("Copied {} into the PC archive.".format(sample_id))
    print("  measurements: {}".format(len(measurements_of(stored))))
    print("  the SESSION copy is untouched and still in slot {}".format(
        stored.get("slot_id") or "-"))
    print()
    pause()

    return stored


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
    Store one retained ESP32 acquisition in the PC ARCHIVE.

    THE SAME ORDER AS A LIVE MEASUREMENT: the Sample and its RAW are
    written first, and only then is Science asked anything. An import
    that cannot be analysed still lands the spectrum, which is the
    entire reason the device keeps a buffer at all.

    Nothing is invented. The device records no timestamps and no
    metadata, so those stay null and the record says why rather than
    being filled with plausible values.
    """
    measurement = payload.get("measurement") or {}
    slot_id = payload.get("slot_id") or entry.get("slot_id")

    if not mission.archive.has_sample(sample_id):
        mission.archive.create(sample_id, slot_id)

    fields = mission.measurement_from_acquisition(measurement, sample_id)

    acquisition = fields.get("acquisition") or {}
    acquisition["origin"] = "esp32_buffer"
    acquisition["esp_uptime_ms"] = measurement.get("esp_uptime_ms")
    acquisition["note"] = (
        "Copied from the ESP32 acquisition buffer by an explicit import. "
        "The device records no wall-clock time and no metadata, so "
        "created_at, loaded_at and the sample metadata are unknown "
        "rather than assumed."
    )
    fields["acquisition"] = acquisition

    if not fields.get("raw"):
        # An entry with no spectrum is an operational record, not a
        # measurement. Stored as the failure it is.
        return mission.archive.add_measurement(
            sample_id,
            acquisition_status=ACQUISITION_FAILED,
            acquisition=acquisition,
            error={"code": "NO_RAW_IN_DEVICE_BUFFER",
                   "message": "the retained entry carried no spectrum"},
        )

    stored = mission.archive.add_measurement(sample_id, **fields)

    run = mission.analyse_measurement(stored)

    # AN ANALYSIS THAT FAILS DOES NOT FAIL THE IMPORT. RAW is already
    # in the archive at this point and is the irreplaceable half; the
    # run records its own status and can be redone from the stored
    # spectrum at any time.
    mission.archive.add_analysis_run(
        sample_id, stored["measurement_id"], run
    )

    decision = run.get("decision")

    if decision:
        mission.archive.set_conclusion(sample_id, {
            "interpretation": decision.get("material")
            or decision.get("family"),
            "level": decision.get("level"),
            "status": decision.get("level"),
            "confidence": decision.get("confidence"),
            "from_measurement": stored["measurement_id"],
            "decision_model_version": (
                run.get("versions") or {}
            ).get("decision_model"),
        })

    return stored


# ======================================================================
# DELETE - one store at a time, named every time
# ======================================================================


def delete_one_from_esp32(mission, rows):
    """
    Delete the ESP32's copy of one Sample. Nothing else, anywhere.

    The device DOES have a per-sample delete - `delete_saved_sample`,
    named by Sample ID - so this asks for exactly the record the
    operator chose. It used to have to clear the whole slot, which
    meant an operator who wanted one device copy gone had to reason
    about which slot it had come from.

    The device's answer is not taken on trust: the index is read back
    afterwards and a device that reports success while still holding
    the sample is reported as a FAILED VERIFICATION, not a delete.
    """
    row = pick_row(
        rows,
        lambda entry: entry["on_esp32"],
        "Sample number",
        "The ESP32 is holding no acquisitions, or could not be reached.",
    )

    if row is None:
        return

    sample_id = row["sample_id"]
    remaining = _elsewhere(row, STORE_ESP32)

    if not confirm_deletion(
        "ESP32 (the device's own buffer)", [sample_id], remaining,
        extra=["PC session and PC archive: untouched"],
    ):
        print("Cancelled. Nothing was deleted.")
        print()
        pause()

        return

    try:
        data = mission.link.delete_saved_sample(sample_id)

    except (LinkError, TimeoutError, DeviceError) as error:
        print()
        print("DELETE FAILED on the device:")
        report_failure(error)
        print()
        print("Nothing on the PC was touched.")
        print()
        pause()

        return

    print()
    print("Device reported: {} deleted".format(data.get("deleted_count", 0)))

    # Do not trust the return code. Ask the device what it still holds.
    after = esp32_index(mission)

    if not after["reachable"]:
        print()
        print("DELETE VERIFICATION FAILED - could not read the device back:")
        print("  {}".format(after["error"]))

    elif sample_id in after["by_id"]:
        print()
        print("DELETE VERIFICATION FAILED")
        print("The device reported success and still holds {}.".format(
            sample_id))

    else:
        print()
        print("ESP32:       {} is gone".format(sample_id))
        print("PC session:  NOT MODIFIED")
        print("PC archive:  NOT MODIFIED")
        print("Slot state:  NOT MODIFIED - soil may still be in the slot")

    print()
    pause()


def delete_one_from_collection(mission, rows, collection, store_name,
                               presence_key):
    """
    Delete one Sample from ONE PC collection.

    Session and archive share this because the operation is identical
    and the WORDING is what differs - and the wording is the safety
    feature. `store_name` appears in the confirmation, in the survivor
    list and in the result, so there is no point at which the operator
    is looking at a screen that does not say which store they are
    emptying.
    """
    row = pick_row(
        rows,
        lambda entry: entry[presence_key],
        "Sample number",
        "The PC {} holds no samples.".format(store_name.lower()),
    )

    if row is None:
        return

    sample_id = row["sample_id"]
    remaining = _elsewhere(row, store_name)

    record = collection.get_sample(sample_id) or {}
    measurements = len(measurements_of(record))

    if not confirm_deletion(
        "PC {} ({})".format(store_name, collection.path),
        [sample_id], remaining,
        extra=[
            "{} measurement(s) and their analyses go with it".format(
                measurements),
            "Clearing a physical slot is a different thing entirely",
        ],
    ):
        print("Cancelled. Nothing was deleted.")
        print()
        pause()

        return

    collection.delete(sample_id)

    print()
    print("PC {}: {} deleted".format(store_name, sample_id))

    for name in (STORE_ESP32, STORE_SESSION, STORE_ARCHIVE):
        if name == store_name:
            continue

        print("{:<12} {}".format(
            name + ":",
            "still holds {}".format(sample_id)
            if name in remaining else "did not hold it",
        ))

    print("Learning DB: NOT MODIFIED")
    print()
    pause()


def delete_all_esp32_samples(mission):
    """
    Delete every retained acquisition held on the ESP32.

    Destructive and deliberately narrow. It removes the device's own
    records and NOTHING else: the PC session, the PC archive, the
    physical slot states, the Decision Learning database and the
    material libraries are all untouched.

    Import first if the data matters. This is deliberately not chained
    to the import, so the operator can verify the copy before
    destroying the original - and the confirmation below names every
    device sample the PC does not already have.
    """
    banner("DELETE ALL SAMPLES FROM THE ESP32")

    device = esp32_index(mission)

    if not device["reachable"]:
        print("Could not read the device:")
        print()
        print("  {}".format(device["error"]))
        print()
        print("NOTHING WAS DELETED. An unreachable device is not an empty")
        print("one, and this command will not report success over a link")
        print("it could not use.")
        print()
        pause()

        return

    entries = device["entries"]

    if not entries:
        print("ESP32 Sample storage is already empty.")
        print()
        print("Its buffer is RAM only: a board reset empties it, and")
        print("opening the serial port resets the board. If you measured")
        print("this session, the spectrum is in the PC session - the")
        print("SESSION column on the Sample Database screen says so.")
        print()
        pause()

        return

    print("ESP32 Samples: {}".format(len(entries)))
    print()

    on_pc = []
    only_on_device = []

    for entry in entries:
        sample_id = entry["sample_id"]

        stores = []

        if mission.session.has_sample(sample_id):
            stores.append(STORE_SESSION)

        if mission.archive.has_sample(sample_id):
            stores.append(STORE_ARCHIVE)

        print("  {:<14}{}".format(
            sample_id,
            "also in " + ", ".join(stores) if stores
            else "ONLY ON THE DEVICE",
        ))

        (on_pc if stores else only_on_device).append(sample_id)

    extra = []

    if only_on_device:
        extra.append(
            "!! {} of these exist NOWHERE ELSE: {}".format(
                len(only_on_device), ", ".join(only_on_device))
        )
        extra.append(
            "   Import them first, or their spectra are gone for good."
        )

    if not confirm_deletion(
        "ESP32 (the device's own buffer)",
        [entry["sample_id"] for entry in entries],
        [STORE_SESSION, STORE_ARCHIVE] if on_pc else [],
        extra=extra + [
            "PC session, PC archive and slot occupancy: untouched",
        ],
    ):
        print("Cancelled. Nothing was deleted.")
        print()
        pause()

        return

    print()
    print("Deleting...")

    try:
        data = mission.link.delete_saved_samples()

    except (LinkError, TimeoutError, DeviceError) as error:
        report_failure(error)
        print()
        print("Nothing on the PC was touched.")
        print()
        pause()

        return

    print("Device reported: {} deleted".format(data.get("deleted_count", 0)))

    # Do not trust the return code. Ask the device what it still holds.
    after = esp32_index(mission)

    if not after["reachable"]:
        print()
        print("DELETE VERIFICATION FAILED - could not read the device back:")
        print("  {}".format(after["error"]))

    elif after["entries"]:
        print()
        print("DELETE VERIFICATION FAILED")
        print("The device reported success but still holds {} "
              "record(s):".format(len(after["entries"])))

        for entry in after["entries"]:
            print("  {}".format(entry.get("sample_id")))

    else:
        print()
        print("ESP32:       now empty  (verified by reading it back)")
        print("PC session:  {} sample(s), NOT MODIFIED".format(
            mission.session.count()))
        print("PC archive:  {} sample(s), NOT MODIFIED".format(
            mission.archive.count()))
        print("Slot state:  NOT MODIFIED - soil may still be in the slots")
        print("Learning DB: NOT MODIFIED")

    print()
    pause()


def clear_session(mission, rows):
    """
    End the run: empty the PC session working set.

    The one place the operator is told, before anything is destroyed,
    exactly which session samples have no archived copy - because the
    session is where a measurement lives between being taken and being
    imported, and that is precisely the window in which clearing it
    loses science.
    """
    banner("CLEAR THE PC SESSION WORKING SET")

    session_rows = [row for row in rows if row["in_session"]]

    if not session_rows:
        print("The session working set is already empty.")
        print()
        pause()

        return

    print("The session holds the run in progress. Clearing it does not")
    print("touch the PC archive, the ESP32 or the Decision Learning")
    print("database.")
    print()

    # WHAT COUNTS AS A SURVIVING COPY HERE, AND WHAT DOES NOT.
    #
    # The archive does. The ESP32 does NOT: its buffer is RAM, a board
    # reset empties it, and opening the serial port resets the board -
    # so "the device still has it" is not a reason to believe the
    # science is safe, and offering it as one is exactly how a spectrum
    # gets lost between two screens that each thought the other had it.
    # It is still SHOWN, because it is true and it is worth knowing;
    # it just does not make a sample archived.
    unarchived = []

    for row in session_rows:
        elsewhere = _elsewhere(row, STORE_SESSION)

        print("  {:<14}{}".format(
            row["sample_id"],
            "also in " + ", ".join(elsewhere) if elsewhere
            else "ONLY IN THE SESSION",
        ))

        if not row["in_archive"]:
            unarchived.append(row["sample_id"])

    extra = []

    if unarchived:
        extra.append("!! {} of these are NOT in the PC archive: {}".format(
            len(unarchived), ", ".join(unarchived)))
        extra.append("   Archive them first ([3]), or they are gone.")
        extra.append("   A copy on the ESP32 does not count: its buffer")
        extra.append("   is RAM and the next reset empties it.")

    if not confirm_deletion(
        "PC SESSION ({})".format(mission.session.path),
        [row["sample_id"] for row in session_rows],
        [STORE_ARCHIVE] if len(unarchived) < len(session_rows) else [],
        extra=extra,
    ):
        print("Cancelled. Nothing was deleted.")
        print()
        pause()

        return

    removed = mission.session.clear()

    print()
    print("PC session:  cleared, {} sample(s) removed".format(len(removed)))
    print("PC archive:  {} sample(s), NOT MODIFIED".format(
        mission.archive.count()))
    print("ESP32:       NOT MODIFIED")
    print("Learning DB: NOT MODIFIED")
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



