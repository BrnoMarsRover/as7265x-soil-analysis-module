"""
Import historical measurements into the learning database.

WITH A PREVIEW, ALWAYS

Importing twelve spectra against twelve names is exactly where a
silent off-by-one becomes twelve verified labels attached to the wrong
materials - and a wrong VERIFIED label is the one error the system can
never detect later, because everything downstream trusts it. So the
importer runs in preview by default: it resolves every name through the
taxonomy, shows what it would write, and writes nothing until told to.
§52.

WHAT IS AND IS NOT IMPORTED

    imported   the raw counts, the calibration and profile in force, the
               operator's label with its provenance, and what the
               previous pipeline concluded
    not        anything derived. Reflectance is re-computed from raw
               whenever it is needed, so a later calibration change
               cannot leave a stale number behind. §8

DB1, DB2 and DB3 are not touched, read or written by this operation.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

FIRMWARE_ROOT = Path(__file__).resolve().parents[2]

if str(FIRMWARE_ROOT) not in sys.path:
    sys.path.insert(0, str(FIRMWARE_ROOT))

from BD import config as bd_config                        # noqa: E402
from BD.acquisition_profiles import (                     # noqa: E402
    AcquisitionProfileStore,
    from_measurement,
)
from BD.decision_learning import (                        # noqa: E402
    DecisionLearningStore,
    LABEL_EXACT_MATERIAL,
    LearningError,
    VERIFIED,
)
from Science.taxonomy import Taxonomy, TaxonomyError           # noqa: E402


SEED_FILE = Path(__file__).resolve().parent / "data" / \
    "seed_observations.json"


def load_seed(path=None):
    """
    The bootstrap seed, from the research tree.

    It is not stored under BD/. Every row in it is imported into the
    learning database, and keeping the JSON beside that database meant
    BD/training/ held one truth twice - so the seed lives with the
    tooling and the DATABASE records where it came from. See
    `DecisionLearningStore.record_seed_provenance`.
    """
    path = Path(path or SEED_FILE)

    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def seed_hash(path=None):
    """SHA256 of the seed file, so the database can name its own source."""
    path = Path(path or SEED_FILE)

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _spectrum(values, channel_order):
    return {
        channel: float(value)
        for channel, value in zip(channel_order, values)
    }


def plan(seed, taxonomy=None, store=None):
    """
    Resolve everything and report what WOULD be written. Writes nothing.

    Any observation whose label does not resolve, or whose raw block is
    the wrong length, is refused - not skipped quietly, not guessed.
    """
    taxonomy = taxonomy or Taxonomy()

    channel_order = seed["provenance"]["channel_order"]
    entries = []
    problems = []

    for observation in seed["observations"]:
        measurement_id = observation["measurement_id"]

        try:
            identity = taxonomy.resolve(observation["operator_label"])

        except TaxonomyError as error:
            problems.append({
                "measurement_id": measurement_id,
                "problem": error.message,
            })

            continue

        declared = observation.get("material_key")

        if declared and declared != identity.key:
            problems.append({
                "measurement_id": measurement_id,
                "problem": "the label '{}' resolves to {}, but the seed "
                           "declares {}. Refusing rather than choosing."
                           .format(observation["operator_label"],
                                   identity.key, declared),
            })

            continue

        raw = {}
        bad = False

        for lamp, values in observation["raw"].items():
            if len(values) != len(channel_order):
                problems.append({
                    "measurement_id": measurement_id,
                    "problem": "{} block has {} values, expected {}".format(
                        lamp, len(values), len(channel_order)
                    ),
                })
                bad = True

                continue

            raw[lamp] = _spectrum(values, channel_order)

        if bad:
            continue

        already = (
            store.get_observation(measurement_id)
            if store is not None else None
        )

        entries.append({
            "measurement_id": measurement_id,
            "operator_label": observation["operator_label"],
            "material_key": identity.key,
            "material_id": identity.material_id,
            "family_id": identity.family_id,
            "raw": raw,
            "previous_prediction": observation.get("previous_prediction"),
            "observed_prediction": observation.get("observed_prediction"),
            "already_present": already is not None,
        })

    return {
        "seed_id": seed.get("seed_id"),
        "session_id": (seed.get("provenance") or {}).get("session_id"),
        "entries": entries,
        "problems": problems,
        "importable": [
            entry for entry in entries if not entry["already_present"]
        ],
        "already_present": [
            entry for entry in entries if entry["already_present"]
        ],
    }


def print_preview(preview):
    print("SEED IMPORT PREVIEW - nothing has been written")
    print()
    print("Seed:    {}".format(preview["seed_id"]))
    print("Session: {}".format(preview["session_id"]))
    print()
    print("{:<24} {:<30} {:<32} {}".format(
        "measurement", "operator label", "resolves to", "state"
    ))

    for entry in preview["entries"]:
        print("{:<24} {:<30} {:<32} {}".format(
            entry["measurement_id"],
            entry["operator_label"][:30],
            entry["material_key"][:32],
            "already imported" if entry["already_present"] else "would import",
        ))

    if preview["problems"]:
        print()
        print("REFUSED")

        for problem in preview["problems"]:
            print("  {}: {}".format(
                problem["measurement_id"], problem["problem"]
            ))

    print()
    print("{} to import, {} already present, {} refused.".format(
        len(preview["importable"]),
        len(preview["already_present"]),
        len(preview["problems"]),
    ))


def apply(seed, preview, store, profile_store=None, seed_path=None):
    """Write the previewed entries. Nothing not previewed is written."""
    profile_store = profile_store or AcquisitionProfileStore()

    settings = seed.get("sensor_settings") or {}
    provenance = seed.get("provenance") or {}
    policy = seed.get("label_policy") or {}

    profile = profile_store.ensure(from_measurement(
        settings,
        label="Session of {}, as recorded in the seed".format(
            provenance.get("measured_at")
        ),
        notes="Geometry and mechanical revisions are null: they were not "
              "recorded during this session.",
    ))

    previous = seed.get("previous_pipeline") or {}
    written = []
    skipped = []

    for entry in preview["importable"]:
        store.add_observation(
            entry["measurement_id"],
            entry["raw"],
            created_at=provenance.get("measured_at"),
            session_id=provenance.get("session_id"),
            sample_group="{}::{}".format(
                provenance.get("session_id"), entry["material_key"]
            ),
            independent_measurement=True,
            acquisition_profile_id=profile["profile_id"],
            calibration_id=provenance.get("calibration_id"),
            legacy_calibration_id=provenance.get("legacy_calibration_id"),
            sensor_settings=settings,
            origin="IMPORTED_HISTORICAL",
            notes="Imported from {}".format(seed.get("seed_id")),
        )

        store.add_ground_truth(
            entry["measurement_id"],
            LABEL_EXACT_MATERIAL,
            material_key=entry["material_key"],
            material_id=entry["material_id"],
            family_id=entry["family_id"],
            verification_status=policy.get("verification_status", VERIFIED),
            verification_source=policy.get(
                "verification_source", "operator_known_reference_material"
            ),
            certainty=policy.get("certainty", 1.0),
            note=policy.get("why"),
        )

        # Two pipelines ran over this session and both left a record.
        # Each goes in under the version of the model that produced it,
        # never merged: the whole reason to keep an old prediction is to
        # be able to say what changed.
        for prediction, version, confidence_key in (
            (entry.get("previous_prediction"),
             previous.get("model_version", "LEGACY_ANALYSIS"), "status"),
            (entry.get("observed_prediction"),
             (seed.get("observed_pipeline") or {}).get(
                 "model_version", "OBSERVED_MODEL"), "confidence"),
        ):
            if not prediction:
                continue

            try:
                store.add_prediction(
                    entry["measurement_id"],
                    prediction.get("model_version") or version,
                    prediction.get("level", "UNKNOWN"),
                    material_key=prediction.get("material_key"),
                    family_id=prediction.get("family_id"),
                    candidates=prediction.get("candidates"),
                    confidence=prediction.get(confidence_key),
                    decision=prediction,
                )

            except LearningError as error:
                # A prediction already on file for that model version is
                # not a reason to abandon the import: predictions are
                # immutable and the existing one wins. Reported, not
                # silently swallowed.
                if error.code != "PREDICTION_EXISTS":
                    raise

                skipped.append(
                    "{}: {}".format(entry["measurement_id"], error.code)
                )

        written.append(entry["measurement_id"])

    # The database says where it came from, in the database. This is
    # what replaced keeping a second copy of the seed beside it.
    store.record_seed_provenance(
        seed.get("seed_id"),
        str(seed_path or SEED_FILE),
        seed_hash(seed_path) if (seed_path or SEED_FILE).exists() else None,
        observations=len(written),
    )

    return {
        "written": written,
        "skipped": skipped,
        "acquisition_profile_id": profile["profile_id"],
        "count": len(written),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Import historical measurements into the decision "
                    "learning database. Previews by default."
    )
    parser.add_argument("--seed", default=None, help="seed JSON file")
    parser.add_argument("--database", default=None, help="learning database")
    parser.add_argument(
        "--apply", action="store_true",
        help="actually write. Without this nothing is written.",
    )

    arguments = parser.parse_args(argv)

    seed = load_seed(arguments.seed)
    store = DecisionLearningStore(arguments.database)

    preview = plan(seed, store=store)
    print_preview(preview)

    if preview["problems"]:
        print()
        print("Refusing to import while anything is unresolved. Fix the "
              "seed or the alias table first.")

        return 2

    if not arguments.apply:
        print()
        print("Preview only. Re-run with --apply to write.")

        return 0

    try:
        result = apply(seed, preview, store,
                       seed_path=Path(arguments.seed)
                       if arguments.seed else SEED_FILE)

    except LearningError as error:
        print()
        print("IMPORT FAILED: {} - {}".format(error.code, error.message))

        return 1

    print()
    print("Imported {} observation(s) under profile {}.".format(
        result["count"], result["acquisition_profile_id"]
    ))
    print()

    status = store.status()

    print("Learning database now holds {} observation(s), {} verified "
          "label(s), {} prediction(s).".format(
              status["observations"],
              status["by_verification"]["VERIFIED"],
              status["predictions"],
          ))

    return 0


if __name__ == "__main__":
    sys.exit(main())
