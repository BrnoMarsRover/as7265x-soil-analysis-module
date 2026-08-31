"""
Decision Learning, organised the way the experiments are.

THE DEFECT THIS SUITE EXISTS FOR

The learning screen led with a CONFUSION HISTORY: one row per
(measurement, model version) pair, model names truncated to twelve
characters, no measurement id, no composition, no matrix, no
percentage, and the bare word `None` standing in for four different
outcomes that are not the same thing. Thirty-five predictions over
twenty-three observations filled the screen, and the material the
operator was actually studying was scattered through it.

WHAT THE EXPERIMENTS ARE

Material-centric. "Activated Carbon: pure, then 90, 70, 50, 30 and 10
percent in soil" is ONE dataset with several observations in it.

WHAT MAY AND MAY NOT FOUND ONE

    may       an EXACT_MATERIAL label the operator recorded
              a PREPARED_MIXTURE with exactly one library component

    may NOT   a model prediction, at any confidence
              UNKNOWN_SAMPLE
              NO_LABEL
              a family-only label
              a mixture with two library components and no recorded
              target

The last one is the important one: guessing which of two components an
old experiment was about would put a scientific claim in the database
that nobody ever made.

Run:  py test_material_datasets.py
"""

import sys
import tempfile
from pathlib import Path

_TESTS_DIR = next(
    p for p in Path(__file__).resolve().parents if p.name == "Tests"
)

if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

_SOFTWARE_DIR = _TESTS_DIR / "software"

if str(_SOFTWARE_DIR) not in sys.path:
    sys.path.insert(0, str(_SOFTWARE_DIR))

import support                                              # noqa: E402

support.add_project_root()
support.add_path("PC")

from BD.channels import CHANNELS                            # noqa: E402
from BD.decision_learning import (                          # noqa: E402
    DecisionLearningStore,
    LABEL_EXACT_MATERIAL,
    LABEL_FAMILY,
    LABEL_NONE,
    LABEL_PREPARED_MIXTURE,
    LABEL_UNKNOWN_SAMPLE,
    OPERATOR_ASSERTED,
    ROLE_COMPONENT,
    ROLE_MATRIX,
    TARGET_EXACT_LABEL,
    TARGET_OPERATOR_STATED,
    TARGET_SOLE_COMPONENT,
    VERIFIED,
    classify_prediction,
    derive_target,
)

checks = support.Checks("material-datasets")

SOURCE = "operator_known_reference_material"


def spectrum(offset=0.0):
    return {
        channel: 100.0 + offset + index
        for index, channel in enumerate(CHANNELS)
    }


def fresh():
    directory = Path(tempfile.mkdtemp(prefix="freya-learn-"))

    return DecisionLearningStore(directory / "decision_learning.sqlite3")


def observe(store, measurement_id, offset=0.0):
    store.add_observation(
        measurement_id, {"white": spectrum(offset)},
        created_at="2026-08-01T00:00:0{}+00:00".format(int(offset) % 10),
    )


# ======================================================================
checks.section("the target is derived only where it is provable")

CASES = (
    ("an exact material label",
     LABEL_EXACT_MATERIAL, "Activated Carbon", [],
     ("Activated Carbon", TARGET_EXACT_LABEL)),

    ("one library component in a matrix",
     LABEL_PREPARED_MIXTURE, None,
     [{"role": ROLE_COMPONENT, "material_key": "Activated Carbon"},
      {"role": ROLE_MATRIX, "matrix_label": "garden soil"}],
     ("Activated Carbon", TARGET_SOLE_COMPONENT)),

    ("TWO library components and no recorded target",
     LABEL_PREPARED_MIXTURE, None,
     [{"role": ROLE_COMPONENT, "material_key": "Activated Carbon"},
      {"role": ROLE_COMPONENT, "material_key": "Iron(III) Oxide Red"},
      {"role": ROLE_MATRIX, "matrix_label": "soil"}],
     (None, None)),

    ("a family-level label",
     LABEL_FAMILY, None, [], (None, None)),

    ("an unknown sample",
     LABEL_UNKNOWN_SAMPLE, None, [], (None, None)),

    ("no label at all",
     LABEL_NONE, None, [], (None, None)),
)

for label, label_type, material_key, components, expected in CASES:
    checks.equal(derive_target(label_type, material_key, components),
                 expected,
                 "{} -> {}".format(label, expected[0] or "AMBIGUOUS"))


# ======================================================================
checks.section("a pure material is the 100% case of its own dataset")

store = fresh()

observe(store, "M-PURE-1")
store.add_ground_truth(
    "M-PURE-1", LABEL_EXACT_MATERIAL,
    material_key="Activated Carbon", family_id="carbonaceous",
    verification_status=VERIFIED, verification_source=SOURCE,
)

datasets = {entry["material_key"]: entry
            for entry in store.material_datasets()}

checks.equal(sorted(datasets), ["Activated Carbon"],
             "an exact material founds its own dataset")
checks.equal(datasets["Activated Carbon"]["pure"], 1,
             "counted as a PURE observation")
checks.equal(datasets["Activated Carbon"]["mixtures"], 0,
             "and not as a mixture")

record = store.material_dataset("Activated Carbon")[0]

checks.equal(record["experiment_type"], "PURE_MATERIAL",
             "the record says which experiment type it is")
checks.equal(record["target_fraction"], 1.0,
             "and its target fraction is 1.0 - it IS the sample, which "
             "is a fact about the label and not an assumption")
checks.equal(record["matrix"], None, "with no matrix")


# ======================================================================
checks.section("a prepared mixture joins the same dataset, in full")

observe(store, "M-MIX-50", offset=1)
store.add_ground_truth(
    "M-MIX-50", LABEL_PREPARED_MIXTURE,
    mixture=[
        {"role": ROLE_COMPONENT, "material_key": "Activated Carbon",
         "prepared_mass_fraction": 0.5, "mass_g": 10.0},
        {"role": ROLE_MATRIX, "matrix_label": "garden soil",
         "prepared_mass_fraction": 0.5, "mass_g": 10.0},
    ],
    verification_status=VERIFIED, verification_source=SOURCE,
)

datasets = {entry["material_key"]: entry
            for entry in store.material_datasets()}

checks.equal(sorted(datasets), ["Activated Carbon"],
             "the mixture joins the SAME dataset as the pure material")
checks.equal(datasets["Activated Carbon"]["observations"], 2,
             "which now has two observations")
checks.equal(datasets["Activated Carbon"]["pure"], 1,
             "one pure")
checks.equal(datasets["Activated Carbon"]["mixtures"], 1,
             "and one mixture - kept apart, because they are different "
             "experiment types")

mixture = next(r for r in store.material_dataset("Activated Carbon")
               if r["experiment_type"] == "PREPARED_MIXTURE")

checks.equal(mixture["target_fraction"], 0.5,
             "the target's own fraction is what was weighed")
checks.equal(mixture["matrix"],
             [{"label": "garden soil", "material_key": None,
               "fraction": 0.5, "mass_g": 10.0}],
             "and the MATRIX is retained in full - not as 'the rest', "
             "because 20% carbon in garden soil and 20% in a regolith "
             "simulant are not the same experiment")

checks.equal(len(mixture["components"]), 2,
             "the exact composition is still queryable, component by "
             "component")


# ======================================================================
checks.section("a concentration series stays a series")

for index, fraction in enumerate((0.1, 0.3, 0.7, 0.9), start=1):
    measurement_id = "M-MIX-{:02d}".format(int(fraction * 100))

    observe(store, measurement_id, offset=index + 1)
    store.add_ground_truth(
        measurement_id, LABEL_PREPARED_MIXTURE,
        mixture=[
            {"role": ROLE_COMPONENT, "material_key": "Activated Carbon",
             "prepared_mass_fraction": fraction},
            {"role": ROLE_MATRIX, "matrix_label": "garden soil",
             "prepared_mass_fraction": round(1.0 - fraction, 4)},
        ],
        verification_status=VERIFIED, verification_source=SOURCE,
    )

records = store.material_dataset("Activated Carbon")

checks.equal(len(records), 6,
             "all six observations are in the one dataset")

fractions = sorted(
    record["target_fraction"] for record in records
    if record["target_fraction"] is not None
)

checks.equal(fractions, [0.1, 0.3, 0.5, 0.7, 0.9, 1.0],
             "every concentration is distinct and preserved - the "
             "series is the point of collecting them")

checks.equal(len({record["measurement_id"] for record in records}), 6,
             "and they stay six separate observations, not an average")


# ======================================================================
checks.section("two matrices are two experiments")

observe(store, "M-MIX-SAND", offset=20)
store.add_ground_truth(
    "M-MIX-SAND", LABEL_PREPARED_MIXTURE,
    mixture=[
        {"role": ROLE_COMPONENT, "material_key": "Activated Carbon",
         "prepared_mass_fraction": 0.5},
        {"role": ROLE_MATRIX, "matrix_label": "Mars-yard regolith",
         "prepared_mass_fraction": 0.5},
    ],
    verification_status=VERIFIED, verification_source=SOURCE,
)

matrices = sorted({
    entry["label"]
    for record in store.material_dataset("Activated Carbon")
    for entry in (record["matrix"] or [])
})

checks.equal(matrices, ["Mars-yard regolith", "garden soil"],
             "both matrices are kept, by name")

fifties = [
    record for record in store.material_dataset("Activated Carbon")
    if record["target_fraction"] == 0.5
]

checks.equal(len(fifties), 2,
             "two Activated Carbon 50% observations exist")
checks.ok(
    fifties[0]["matrix"] != fifties[1]["matrix"],
    "and they are distinguishable by their matrix - flattening them "
    "into 'Activated Carbon 50%' would make two different experiments "
    "look like a repeat")


# ======================================================================
checks.section("a model prediction NEVER founds a dataset")

observe(store, "M-UNKNOWN", offset=30)
store.add_ground_truth(
    "M-UNKNOWN", LABEL_UNKNOWN_SAMPLE,
    note="field soil, composition not established",
)
store.add_prediction(
    "M-UNKNOWN", "FREYA_DECISION_V001", "KNOWN_MATERIAL",
    material_key="Kaolin (White Clay)", confidence="HIGH",
)

datasets = {entry["material_key"]: entry
            for entry in store.material_datasets()}

checks.ok("Kaolin (White Clay)" not in datasets,
          "a HIGH-confidence prediction of Kaolin does NOT create a "
          "Kaolin dataset - that would be the model teaching itself")

checks.equal(sorted(datasets), ["Activated Carbon"],
             "the only dataset is still the one the operator established")

ungrouped = store.ungrouped_observations()

checks.equal([r["measurement_id"] for r in ungrouped["UNKNOWN_SAMPLE"]],
             ["M-UNKNOWN"],
             "the observation is kept, under UNKNOWN_SAMPLE")
checks.equal(len(ungrouped["UNKNOWN_SAMPLE"][0]["predictions"]), 1,
             "with the prediction still attached and inspectable")


# ======================================================================
checks.section("an unlabelled observation is kept, and is not a dataset")

observe(store, "M-NOLABEL", offset=40)

ungrouped = store.ungrouped_observations()
unlabelled = [r["measurement_id"] for r in ungrouped["NO_LABEL"]]

checks.ok("M-NOLABEL" in unlabelled,
          "an observation with no ground_truth row at all is still "
          "listed - its RAW is kept and can be labelled later")

checks.equal(sorted(entry["material_key"]
                    for entry in store.material_datasets()),
             ["Activated Carbon"],
             "and it founds nothing")


# ======================================================================
checks.section("an ambiguous legacy mixture is never silently assigned")

observe(store, "M-AMBIGUOUS", offset=50)
store.add_ground_truth(
    "M-AMBIGUOUS", LABEL_PREPARED_MIXTURE,
    mixture=[
        {"role": ROLE_COMPONENT, "material_key": "Activated Carbon",
         "prepared_mass_fraction": 0.25},
        {"role": ROLE_COMPONENT, "material_key": "Iron(III) Oxide Red",
         "prepared_mass_fraction": 0.25},
        {"role": ROLE_MATRIX, "matrix_label": "soil",
         "prepared_mass_fraction": 0.5},
    ],
    verification_status=VERIFIED, verification_source=SOURCE,
)

truth = store.get_ground_truth("M-AMBIGUOUS")

checks.equal(truth["target_material_key"], None,
             "a mixture of two library materials has NO derived target")
checks.equal(truth["target_source"], None,
             "and no source is claimed for one")

carbon = [r["measurement_id"]
          for r in store.material_dataset("Activated Carbon")]

checks.ok("M-AMBIGUOUS" not in carbon,
          "it does not appear in the Activated Carbon dataset")

ungrouped = store.ungrouped_observations()

checks.equal([r["measurement_id"] for r in ungrouped["AMBIGUOUS_MIXTURE"]],
             ["M-AMBIGUOUS"],
             "it is listed as AMBIGUOUS_MIXTURE, so an operator can see "
             "what is waiting to be resolved")
checks.equal(len(ungrouped["AMBIGUOUS_MIXTURE"][0]["components"]), 3,
             "with its full composition intact")


# ======================================================================
checks.section("an operator CAN state the target, and only an operator")

store.add_ground_truth(
    "M-AMBIGUOUS", LABEL_PREPARED_MIXTURE,
    mixture=[
        {"role": ROLE_COMPONENT, "material_key": "Activated Carbon",
         "prepared_mass_fraction": 0.25},
        {"role": ROLE_COMPONENT, "material_key": "Iron(III) Oxide Red",
         "prepared_mass_fraction": 0.25},
        {"role": ROLE_MATRIX, "matrix_label": "soil",
         "prepared_mass_fraction": 0.5},
    ],
    target_material_key="Activated Carbon",
    verification_status=VERIFIED, verification_source=SOURCE,
    replace=True,
)

truth = store.get_ground_truth("M-AMBIGUOUS")

checks.equal(truth["target_material_key"], "Activated Carbon",
             "a stated target is recorded")
checks.equal(truth["target_source"], TARGET_OPERATOR_STATED,
             "and is marked as OPERATOR_STATED, not as a derivation")

carbon = [r["measurement_id"]
          for r in store.material_dataset("Activated Carbon")]

checks.ok("M-AMBIGUOUS" in carbon,
          "and it now belongs to the Activated Carbon dataset")

# The label source rule still holds, and it is what stops a model
# doing the same thing.
try:
    store.add_ground_truth(
        "M-NOLABEL", LABEL_EXACT_MATERIAL,
        material_key="Kaolin (White Clay)",
        verification_status=VERIFIED,
        verification_source="decision_model FREYA_DECISION_V001",
    )
    refused = False

except Exception as error:                             # noqa: BLE001
    refused = getattr(error, "code", "") == \
        "PREDICTION_IS_NOT_GROUND_TRUTH"

checks.ok(refused,
          "a label whose source names a model is refused outright, so "
          "no prediction can reach a material dataset by the back door")


# ======================================================================
checks.section("OPERATOR_ASSERTED counts, UNVERIFIED does not")

observe(store, "M-ASSERTED", offset=60)
store.add_ground_truth(
    "M-ASSERTED", LABEL_EXACT_MATERIAL,
    material_key="Talc", family_id="phyllosilicate",
    verification_status=OPERATOR_ASSERTED,
    verification_source="operator bench note",
)

trainable = {entry["material_key"]
             for entry in store.material_datasets()}
verified_only = {entry["material_key"]
                 for entry in store.material_datasets(levels=(VERIFIED,))}

checks.ok("Talc" in trainable,
          "an OPERATOR_ASSERTED label appears in the default view")
checks.ok("Talc" not in verified_only,
          "and is excluded when only VERIFIED is asked for - the trust "
          "ladder is still the trust ladder")


# ======================================================================
checks.section("predictions are scored as four outcomes, not two")

OUTCOMES = (
    ("named the right material",
     {"level": "KNOWN_MATERIAL", "predicted_material": "Activated Carbon",
      "truth_material": "Activated Carbon"},
     "EXACT"),

    ("named the right family only",
     {"level": "MATERIAL_FAMILY", "predicted_material": None,
      "predicted_family": "carbonaceous",
      "truth_material": "Activated Carbon",
      "truth_family": "carbonaceous"},
     "FAMILY"),

    ("named something else",
     {"level": "KNOWN_MATERIAL", "predicted_material": "Kaolin",
      "truth_material": "Activated Carbon",
      "truth_family": "carbonaceous"},
     "WRONG"),

    ("abstained",
     {"level": "UNKNOWN", "predicted_material": None,
      "predicted_family": None, "truth_material": "Activated Carbon"},
     "ABSTAINED"),

    ("answered AMBIGUOUS_SET without naming anything",
     {"level": "AMBIGUOUS_SET", "predicted_material": None,
      "predicted_family": None, "truth_material": "Activated Carbon"},
     "ABSTAINED"),
)

for label, row, expected in OUTCOMES:
    checks.equal(classify_prediction(row), expected,
                 "a model that {} scores {}".format(label, expected))


# ======================================================================
checks.section("model performance is per version, and keeps them all")

store.add_prediction(
    "M-PURE-1", "FREYA_DECISION_V001", "KNOWN_MATERIAL",
    material_key="Activated Carbon", confidence="HIGH",
)
store.add_prediction(
    "M-PURE-1", "LEGACY_ANALYSIS_V2", "UNKNOWN",
)
store.add_prediction(
    "M-MIX-50", "FREYA_DECISION_V001", "MATERIAL_FAMILY",
    family_id="carbonaceous",
)

models = {entry["model_version"]: entry
          for entry in store.model_performance()}

checks.equal(sorted(models),
             ["FREYA_DECISION_V001", "LEGACY_ANALYSIS_V2"],
             "both model versions are scored, separately")

checks.equal(models["FREYA_DECISION_V001"]["exact"], 1,
             "V001 got one exact")
checks.equal(models["LEGACY_ANALYSIS_V2"]["abstained"], 1,
             "and the legacy model abstained once - which is not "
             "counted as a miss, because on a genuinely ambiguous "
             "sample abstaining is correct behaviour")

checks.equal(len(store.predictions("M-PURE-1")), 2,
             "both historical predictions for one measurement survive")

carbon = store.material_dataset("Activated Carbon")
pure = next(r for r in carbon if r["measurement_id"] == "M-PURE-1")

checks.equal(len(pure["predictions"]), 2,
             "and both are visible on the observation")
checks.equal(len([r for r in carbon
                  if r["measurement_id"] == "M-PURE-1"]), 1,
             "while the observation itself appears ONCE - two "
             "predictions do not make two observations")


# ======================================================================
checks.section("a scored row carries what was in the cup")

# "Activated Carbon -> Epsom Salt, WRONG" reads as a model failure until
# you know the sample was 50% carbon in soil, at which point it reads as
# a hard case. The old confusion table had no column for composition and
# there was no way to tell the two apart.
rows = {
    row["measurement_id"]: row
    for entry in store.model_performance()
    if entry["model_version"] == "FREYA_DECISION_V001"
    for row in entry["rows"]
}

checks.ok("M-MIX-50" in rows,
          "the mixture observation is scored")

if "M-MIX-50" in rows:
    row = rows["M-MIX-50"]

    checks.equal(len(row["components"]), 2,
                 "and the row carries the composition as prepared")
    checks.equal(row["matrix"][0]["label"], "garden soil",
                 "including the matrix it was mixed into")

checks.equal(rows["M-PURE-1"]["components"], [],
             "a pure material carries no component list - it is not a "
             "one-component mixture and must not count as one")


# ======================================================================
checks.section("one observation, in full, in one call")

detail = store.observation_detail("M-MIX-50")

checks.equal(detail["target_material_key"], "Activated Carbon",
             "the detail names the target")
checks.equal(detail["target_source"], TARGET_SOLE_COMPONENT,
             "and how it was established")
checks.equal(detail["target_fraction"], 0.5, "and the prepared fraction")
checks.equal(detail["matrix"][0]["label"], "garden soil",
             "and the matrix")
checks.ok(detail["observation"]["raw"], "with the RAW spectra")
checks.ok(detail["observation"]["raw_hash"], "and their hash")

checks.ok("material_key" in (detail["ground_truth"] or {}),
          "ground truth is its own block")
checks.ok(isinstance(detail["predictions"], list),
          "and predictions are another - they are never merged into one "
          "identity field")


store.close()

sys.exit(checks.report())
