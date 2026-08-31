"""
Build the learning seed from the DB2 source snapshot.

    py firmware/research/training/build_seed.py
    py firmware/research/training/build_seed.py --check

Reads  firmware/BD/DB2/DB2_source.txt        (verbatim, never edited)
Writes firmware/BD/training/seed_observations.json

ONE SET OF NUMBERS, TWO USES
----------------------------
DB2 and the learning seed describe the same 22 acquisitions. DB2 asks
"what does this material look like"; the seed asks "what has the system
seen, and what did it conclude". Transcribing the counts twice would
eventually produce two different answers to "what did channel R read for
Kaolin", so both are generated from the same snapshot and the raw counts
in them are the same numbers by construction.

WHAT THE SEED CARRIES THAT DB2 DOES NOT
---------------------------------------
    the operator's label      what the container said
    the ground truth          what the sample actually was
    the prediction            what a model concluded at the time

The last of those is history, not truth. It is written to the
`predictions` table under the version of the model that produced it, and
`BD.decision_learning` refuses to let it become a label. That refusal is
the whole point: a system trained on its own output entrenches its own
mistakes instead of correcting them.

WHY PREDICTIONS ARE TRANSCRIBED RATHER THAN RE-RUN
--------------------------------------------------
Re-running today's model over the stored raw would record what today's
model says, under the label of what the model said on 2026-08-17. Those
are different claims, and the difference between them is the only
evidence that anything improved. So what the terminal actually printed
is transcribed, and re-analysis writes a NEW prediction row.
"""

import argparse
import json
import sys
from pathlib import Path

FIRMWARE_ROOT = Path(__file__).resolve().parents[2]

if str(FIRMWARE_ROOT) not in sys.path:
    sys.path.insert(0, str(FIRMWARE_ROOT))

from BD import config                                          # noqa: E402
from BD.channels import CHANNELS, ILLUMINATIONS                # noqa: E402
from research.build_db2 import SOURCE, parse_source            # noqa: E402
from research.material_identity import identity, resolve_label  # noqa: E402

# THE SEED IS AN IMPORT INPUT, NOT A STORE.
#
# It used to be written into BD/training/, beside the learning database
# it is imported into - so BD held the same twenty-two observations
# twice, in two formats, with nothing keeping them in step. It lives
# with the tooling that produces and consumes it; the database records
# the seed's id and hash in its own meta table, so the provenance
# travels inside the one canonical store.
SEED_DIR = Path(__file__).resolve().parent / "data"
OUTPUT = SEED_DIR / "seed_observations.json"

SEED_ID = "FREYA_SESSION_20260817"
SESSION_ID = "SESSION_20260817"
MEASURED_AT = "2026-08-17"
CALIBRATION_ID = "FREYA_FULL_SPECTRAL_CAL_20260817_173914"
LEGACY_CALIBRATION_ID = "FREYA_COMPETITION_2026_CAL_V1"

LEGACY_MODEL = "LEGACY_ANALYSIS_V2"
CURRENT_MODEL = "FREYA_DECISION_V001"

# ----------------------------------------------------------------------
# WHAT THE PIPELINE CONCLUDED, transcribed from the operator's terminal.
#
# Two different pipelines ran over this session. The first twelve
# materials were measured under the client that printed a LEGACY
# DATABASE COMPARISON and a RESULT block; the last ten were measured
# after the 6.0.0 deployment, whose client prints a DECISION MODEL
# block instead. Both are recorded under the model version that produced
# them, and neither is allowed anywhere near the ground-truth table.
# ----------------------------------------------------------------------

# key -> what LEGACY_ANALYSIS_V2 said. Preserved from the previous seed.
LEGACY_PREDICTIONS = {
    "Activated Carbon": {
        "level": "AMBIGUOUS_SET",
        "material_key": "Epsom Salt (Magnesium Sulfate)",
        "status": "METRICS_DISAGREE",
        "hardware_qc": "PASS",
        "note": "RMSE placed Activated Carbon first by a factor of 19 over "
                "the runner-up; rank aggregation reduced that to one vote "
                "and the combined ranking chose Epsom Salt.",
    },
}

# key -> what FREYA_DECISION_V001 said, from the DECISION MODEL blocks.
CURRENT_PREDICTIONS = {
    "Green Clay (Illite)": {
        "level": "MATERIAL_FAMILY",
        "material_key": None,
        "family_id": "phyllosilicate_clay",
        "confidence": "LOW",
        "candidates": [
            {"material_key": "Green Clay (Illite)", "support": "MEDIUM",
             "family_id": "phyllosilicate_clay"},
            {"material_key": "Magnesium Oxide", "support": "MEDIUM",
             "family_id": "oxide"},
        ],
        "normalization": "OK",
        "hardware_qc": "PASS",
        "note": "the phyllosilicate_clay family carries 70% of the "
                "candidate evidence, but no member is separable from the "
                "others",
    },
    "Tartaric Acid": {
        "level": "UNKNOWN",
        "material_key": None,
        "family_id": None,
        "confidence": "NONE",
        "candidates": [
            {"material_key": "Carbon Black GDS68", "support": None,
             "family_id": "carbonaceous"},
            {"material_key": "Talc", "support": None,
             "family_id": "phyllosilicate"},
            {"material_key": "Magnesium Carbonate", "support": None,
             "family_id": "carbonate"},
        ],
        "normalization": "OK",
        "hardware_qc": "PASS",
        "note": "the best candidate stands out from the field by only 0.24 "
                "on a 0-1 scale; in a library where everything scores "
                "alike, that is not a match",
    },
    "Red Clay": {
        "level": "MATERIAL_FAMILY",
        "material_key": None,
        "family_id": "phyllosilicate_clay",
        "confidence": "LOW",
        "candidates": [
            {"material_key": "Pink Clay", "support": "HIGH",
             "family_id": "phyllosilicate_clay"},
            {"material_key": "Red Clay", "support": "HIGH",
             "family_id": "phyllosilicate_clay"},
        ],
        "normalization": "OK",
        "hardware_qc": "PASS",
        "note": "the phyllosilicate_clay family carries 85% of the "
                "candidate evidence, but no member is separable from the "
                "others. DB1 ranked Red Clay first on cosine and Pearson.",
    },
    "Pink Clay": {
        "level": "KNOWN_MATERIAL",
        "material_key": "Pink Clay",
        "family_id": "phyllosilicate_clay",
        "confidence": "MEDIUM",
        "candidates": [
            {"material_key": "Pink Clay", "support": "MEDIUM",
             "family_id": "phyllosilicate_clay"},
            {"material_key": "Carnallite HS430.1B", "support": "LOW",
             "family_id": "halide"},
            {"material_key": "Red Clay", "support": "LOW",
             "family_id": "phyllosilicate_clay"},
        ],
        "normalization": "OK",
        "hardware_qc": "PASS",
        "note": "leads by 38% of its own strength with 1 independent "
                "source supporting it. The one correct KNOWN_MATERIAL "
                "call of the session.",
    },
    "Ascorbic Acid": {
        "level": "UNKNOWN",
        "material_key": None,
        "family_id": None,
        "confidence": "NONE",
        "candidates": [
            {"material_key": "Riebeckite NMNH122689 Amph", "support": None,
             "family_id": "silicate_mafic"},
            {"material_key": "Aluminum Sulfate", "support": None,
             "family_id": "sulfate"},
            {"material_key": "Calcium Carbonate (Chalk)", "support": None,
             "family_id": "carbonate"},
        ],
        "normalization": "NORMALIZATION_UNUSABLE",
        "hardware_qc": "PASS",
        "note": "the best candidate stands out from the field by only 0.17 "
                "on a 0-1 scale. 27 of 54 features had too little "
                "reference range to divide by.",
    },
    "Potassium Nitrate": {
        "level": "UNKNOWN",
        "material_key": None,
        "family_id": None,
        "confidence": "NONE",
        "candidates": [
            {"material_key": "Potassium Nitrate", "support": None,
             "family_id": "nitrate"},
            {"material_key": "Riebeckite NMNH122689 Amph", "support": None,
             "family_id": "silicate_mafic"},
            {"material_key": "Calcium Carbonate (Chalk)", "support": None,
             "family_id": "carbonate"},
        ],
        "normalization": "NORMALIZATION_UNUSABLE",
        "hardware_qc": "PASS",
        "note": "the correct material was ranked first and still not "
                "named: it stood out from the field by only 0.19 on a "
                "0-1 scale.",
    },
    "Borax (Sodium Tetraborate)": {
        "level": "UNKNOWN",
        "material_key": None,
        "family_id": None,
        "confidence": "NONE",
        "candidates": [
            {"material_key": "Magnesium Carbonate", "support": None,
             "family_id": "carbonate"},
            {"material_key": "Carbon Black GDS68", "support": None,
             "family_id": "carbonaceous"},
            {"material_key": "Talc", "support": None,
             "family_id": "phyllosilicate"},
        ],
        "normalization": "NORMALIZATION_UNUSABLE",
        "hardware_qc": "PASS",
        "note": "measured twice in the session; both runs concluded "
                "UNKNOWN with the same three candidates.",
    },
    "Sulfur Powder": {
        "level": "AMBIGUOUS_SET",
        "material_key": None,
        "family_id": None,
        "confidence": "LOW",
        "candidates": [
            {"material_key": "Sulfur Powder", "support": "MEDIUM",
             "family_id": "native_element"},
            {"material_key": "Kaolin (White Clay)", "support": "LOW",
             "family_id": "phyllosilicate_clay"},
        ],
        "normalization": "NORMALIZATION_UNUSABLE",
        "hardware_qc": "PASS",
        "note": "2 candidates lie within the leader's own margin; the "
                "correct one leads.",
    },
    "Iron(II,III) Oxide Black": {
        "level": "MATERIAL_FAMILY",
        "material_key": None,
        "family_id": "iron_oxide",
        "confidence": "LOW",
        "candidates": [
            {"material_key": "Activated Carbon", "support": "LOW",
             "family_id": "carbonaceous"},
            {"material_key": "Iron(II,III) Oxide Black", "support": "LOW",
             "family_id": "iron_oxide"},
        ],
        "normalization": "OK",
        "hardware_qc": "PASS",
        "note": "the iron_oxide family carries 53% of the candidate "
                "evidence. Two near-black powders are hard to separate by "
                "reflectance alone, which is the expected failure.",
    },
    "Iron(III) Oxide Red": {
        "level": "AMBIGUOUS_SET",
        "material_key": None,
        "family_id": None,
        "confidence": "LOW",
        "candidates": [
            {"material_key": "Iron(III) Oxide Red", "support": "MEDIUM",
             "family_id": "iron_oxide"},
            {"material_key": "Red Clay", "support": "MEDIUM",
             "family_id": "phyllosilicate_clay"},
        ],
        "normalization": "OK",
        "hardware_qc": "PASS",
        "note": "2 candidates lie within the leader's own margin. Red "
                "clay is iron-oxide-bearing, so this confusion is "
                "mineralogically real rather than an instrument fault.",
    },
}


def build(previous=None):
    """Assemble the seed document from the snapshot."""
    references, materials = parse_source(SOURCE.read_text(encoding="utf-8"))

    # Whatever the previous seed said about the earlier pipeline is
    # preserved: it is a record of something that happened, and this
    # builder is not entitled to lose it.
    legacy = dict(LEGACY_PREDICTIONS)

    for observation in (previous or {}).get("observations", []):
        prediction = observation.get("previous_prediction")

        if prediction and observation.get("material_key"):
            legacy.setdefault(observation["material_key"], prediction)

    observations = []
    problems = []

    for order, (label, rows) in enumerate(materials.items(), start=1):
        name = resolve_label(label)

        if name is None:
            problems.append(
                "'{}' does not resolve to a known material".format(label)
            )

            continue

        record = identity(name)
        by_channel = {row["channel"]: row for row in rows}

        entry = {
            "measurement_id": "{}_{:02d}".format(SESSION_ID, order),
            "order": order,
            "operator_label": label,
            "material_key": name,
            "material_id": record["material_id"],
            "name_en": record["name_en"],
            "name_cs": record["name_cs"],
            "family_id": record["material_class"],
            "raw": {
                lamp: [by_channel[c]["raw"][lamp] for c in CHANNELS]
                for lamp in ILLUMINATIONS
            },
        }

        if name in legacy:
            entry["previous_prediction"] = legacy[name]

        if name in CURRENT_PREDICTIONS:
            entry["observed_prediction"] = dict(
                CURRENT_PREDICTIONS[name], model_version=CURRENT_MODEL
            )

        observations.append(entry)

    document = {
        "schema_version": 2,
        "seed_id": SEED_ID,
        "description": "The twenty-two known reference materials measured "
                       "on 2026-08-17, generated from the same verbatim "
                       "snapshot DB2 is built from. This file is the "
                       "human-readable, version-controllable source the "
                       "SQLite learning database is built from; the "
                       "database can be deleted and rebuilt from here at "
                       "any time.",
        "generated_from": {
            "file": str(SOURCE.relative_to(FIRMWARE_ROOT)).replace("\\", "/"),
            "builder": "research/training/build_seed.py",
            "note": "The raw counts here and the raw counts in DB2.json "
                    "come from one parse of one file. They cannot "
                    "disagree.",
        },
        "provenance": {
            "session_id": SESSION_ID,
            "measured_at": MEASURED_AT,
            "operator_statement": "Twenty-two labelled laboratory "
                                  "containers, measured one after another "
                                  "under one calibration without moving "
                                  "the sensor. The identity of each is "
                                  "known from its container.",
            "calibration_id": CALIBRATION_ID,
            "legacy_calibration_id": LEGACY_CALIBRATION_ID,
            "transcribed_from": "the FULL SPECTRAL DATA blocks of the "
                                "operator's terminal session",
            "raw_units": "counts, AS7265x calibrated registers, as "
                         "reported by the module",
            "channel_order": list(CHANNELS),
        },
        "sensor_settings": {
            "measurement_mode": 3,
            "integration_cycles": 100,
            "gain": 2,
            "gain_x": "16x",
            "white_current_ma": 25,
            "uv_current_ma": 25,
            "ir_current_ma": 25,
        },
        "independence_note": "Each material is one INDEPENDENT "
                             "measurement: a different physical sample, "
                             "loaded separately. The three acquisitions "
                             "inside each are sensor repeats and are "
                             "aggregated, not counted as independent. No "
                             "material has a second independent "
                             "measurement yet, so no class has a "
                             "measurable scatter.",
        "label_policy": {
            "verification_status": "VERIFIED",
            "verification_source": "operator_known_reference_material",
            "certainty": 1.0,
            "why": "Pure laboratory materials from labelled containers, "
                   "measured deliberately to be identified. This is the "
                   "operator asserting a fact about the physical world, "
                   "not a model asserting a fact about its own output.",
        },
        "previous_pipeline": {
            "model_version": LEGACY_MODEL,
            "description": "What the pipeline in force for the first "
                           "twelve materials concluded, recorded so a "
                           "later model can be compared against it rather "
                           "than against an impression.",
            "level_mapping": {
                "REFERENCE_MATCH": "KNOWN_MATERIAL",
                "METRICS_DISAGREE": "AMBIGUOUS_SET",
                "AMBIGUOUS": "AMBIGUOUS_SET",
                "MEASUREMENT_QUALITY_FAIL": "UNKNOWN",
            },
        },
        "observed_pipeline": {
            "model_version": CURRENT_MODEL,
            "description": "What the deployed decision model concluded for "
                           "the ten materials measured after the 6.0.0 "
                           "firmware deployment, transcribed from the "
                           "DECISION MODEL blocks of the terminal. History, "
                           "never a label.",
            "scoreboard": {
                "known_material": 1,
                "material_family": 3,
                "ambiguous_set": 2,
                "unknown": 4,
                "correct_when_it_committed": 1,
                "note": "Pink Clay is the only material it named, and it "
                        "named it correctly. Four of the ten were refused "
                        "outright, three of those because 27 of 54 "
                        "features had no usable reference range.",
            },
        },
        "observation_count": len(observations),
        "observations": observations,
    }

    return document, problems


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Regenerate the learning seed from the DB2 snapshot."
    )
    parser.add_argument("--check", action="store_true",
                        help="compare against the seed on disk; write "
                             "nothing")
    parser.add_argument("--output", default=None)

    arguments = parser.parse_args(argv)
    output = Path(arguments.output or OUTPUT)

    previous = None

    if output.exists():
        previous = json.loads(output.read_text(encoding="utf-8"))

    document, problems = build(previous)

    print("SEED BUILD")
    print()
    print("source: {}".format(SOURCE))
    print("output: {}".format(output))
    print()
    print("{:<24} {:<32} {:<24} {}".format(
        "measurement", "operator label", "material", "prediction on record"
    ))

    for entry in document["observations"]:
        which = (
            "V001 " + entry["observed_prediction"]["level"]
            if "observed_prediction" in entry
            else "LEGACY " + entry["previous_prediction"]["level"]
            if "previous_prediction" in entry
            else "-"
        )

        print("{:<24} {:<32} {:<24} {}".format(
            entry["measurement_id"],
            entry["operator_label"][:32],
            entry["material_key"][:24],
            which,
        ))

    if problems:
        print()
        print("REFUSED")

        for problem in problems:
            print("  {}".format(problem))
        print()
        print("Nothing written.")

        return 2

    print()
    print("{} observation(s).".format(document["observation_count"]))

    if previous:
        before = {o["measurement_id"] for o in previous["observations"]}
        after = {o["measurement_id"] for o in document["observations"]}

        # An id that changes meaning is the one failure this builder can
        # cause that nothing downstream would notice: the SQLite store
        # keys observations by it and refuses to overwrite, so a
        # reshuffled id would silently attach old spectra to a new name.
        moved = []

        for entry in document["observations"]:
            for old in previous["observations"]:
                if (old["measurement_id"] == entry["measurement_id"]
                        and old.get("material_key")
                        != entry["material_key"]):
                    moved.append(entry["measurement_id"])

        print("  already in the seed: {}".format(
            len(before & after)))
        print("  new:                 {}".format(
            len(after - before)))

        if moved:
            print()
            print("REFUSED: {} existing measurement id(s) would change "
                  "material: {}".format(len(moved), ", ".join(moved)))
            print("Nothing written.")

            return 2

    if arguments.check:
        print()
        print("Check only. Nothing was written.")

        return 0

    output.parent.mkdir(parents=True, exist_ok=True)

    with open(output, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    print()
    print("Wrote {}".format(output))
    print()
    print("Import it with:")
    print("  py firmware/research/training/import_seed.py --apply")

    return 0


if __name__ == "__main__":
    sys.exit(main())
