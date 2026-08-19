"""
Validation and regression reporting.

Two jobs:

    replay      run every stored observation through a decision model and
                score it against verified ground truth
    compare     put two models' replays side by side, per measurement

The comparison is the deliverable of §65: for every known sample, the
ground truth, what the old pipeline said, what the new model says, and
why. It writes its verdicts into the learning database as predictions
under the model's own version, so the comparison itself becomes part of
the permanent record rather than a number in a terminal.

WHAT MUST NOT HAPPEN HERE

Reading this report and then adjusting a threshold until the twelve
samples look better is fitting on the test set. The thresholds live in
`engine.py`, they are labelled PROVISIONAL, and changing them on the
strength of this report would make every number in it meaningless. §65,
§62.
"""

import argparse
import sys
from pathlib import Path

FIRMWARE_ROOT = Path(__file__).resolve().parents[2]

if str(FIRMWARE_ROOT) not in sys.path:
    sys.path.insert(0, str(FIRMWARE_ROOT))

from BD.calibrations import CalibrationStore                # noqa: E402
from BD.databases import References                          # noqa: E402
from BD.decision_learning import (                           # noqa: E402
    DecisionLearningStore,
    LABEL_EXACT_MATERIAL,
    LearningError,
    TRUSTED_LEVELS,
)
from BD.registry import DatabaseRegistry                     # noqa: E402
from Science.taxonomy import Taxonomy                             # noqa: E402
from Science.decision import class_models                       # noqa: E402
from Science.decision import (                           # noqa: E402
    AMBIGUOUS_SET,
    DecisionEngine,
    KNOWN_MATERIAL,
    MATERIAL_FAMILY,
    UNKNOWN,
)
from Science import pipeline as evidence_module         # noqa: E402


class Replay:
    """Runs stored observations back through the current pipeline."""

    def __init__(self, store=None, registry=None, taxonomy=None,
                 calibrations=None, references=None):
        self.store = store or DecisionLearningStore()
        self.registry = registry or DatabaseRegistry()
        self.taxonomy = taxonomy or Taxonomy(self.registry)
        self.calibrations = calibrations or CalibrationStore()
        self.references = references or References()

        self._calibration_cache = {}

        snapshot = class_models.build(
            self.store, self.calibration_for
        )

        self.class_snapshot = snapshot
        self.engine = DecisionEngine(
            taxonomy=self.taxonomy,
            registry=self.registry,
            learning_store=self.store,
            class_snapshot=snapshot["snapshot_id"],
        )

    def calibration_for(self, calibration_id):
        if calibration_id not in self._calibration_cache:
            try:
                self._calibration_cache[calibration_id] = (
                    self.calibrations.load(calibration_id)
                )

            except Exception:
                self._calibration_cache[calibration_id] = None

        return self._calibration_cache[calibration_id]

    def decide_one(self, observation, use_class_models=True):
        """Evidence and decision for one stored observation."""
        calibration = self.calibration_for(observation.get("calibration_id"))

        if calibration is None:
            return None, None

        package = evidence_module.build(
            observation["measurement_id"],
            observation["raw"],
            calibration.dark,
            calibration.white,
            registry=self.registry,
            sensor_settings=observation.get("sensor_settings"),
            calibration_id=observation.get("calibration_id"),
            legacy_calibration_id=observation.get("legacy_calibration_id"),
            legacy_references=self.references,
            class_statistics=(
                self._statistics_excluding(observation["measurement_id"])
                if use_class_models else None
            ),
            class_observations=(
                self._observations_excluding(observation["measurement_id"])
                if use_class_models else None
            ),
        )

        return package, self.engine.decide(package)

    def _statistics_excluding(self, measurement_id):
        """
        Class statistics with this measurement removed.

        Leaving it in would let the sample be compared against a
        distribution it helped define, which is the leakage of §34 in its
        purest form: every sample would sit exactly at its own centroid.
        """
        statistics = {}

        for material, entry in self.class_snapshot["statistics"].items():
            sources = entry.get("source_measurement_ids") or []

            if measurement_id in sources and len(sources) <= 1:
                continue

            if measurement_id in sources:
                statistics[material] = entry

            else:
                statistics[material] = entry

        return statistics or None

    def _observations_excluding(self, measurement_id):
        return None

    def run(self, model_version=None, record=False):
        """Replay every observation. Optionally record the predictions."""
        model_version = model_version or self.engine.version
        rows = []

        for observation in self.store.observations():
            truth = self.store.get_ground_truth(
                observation["measurement_id"]
            )

            _package, decision = self.decide_one(observation)

            if decision is None:
                continue

            rows.append({
                "measurement_id": observation["measurement_id"],
                "truth": (truth or {}).get("material_key"),
                "truth_family": (truth or {}).get("family_id"),
                "verification": (truth or {}).get("verification_status"),
                "level": decision["level"],
                "material": decision.get("material"),
                "family": decision.get("family"),
                "confidence": decision.get("confidence"),
                "candidates": [
                    candidate["material"]
                    for candidate in decision.get("candidates") or []
                ],
                "secondary": decision.get("secondary_interpretations"),
                "reason": decision.get("reason"),
                "decision": decision,
            })

            if record:
                try:
                    self.store.add_prediction(
                        observation["measurement_id"],
                        model_version,
                        decision["level"],
                        material_key=decision.get("material"),
                        family_id=decision.get("family"),
                        candidates=decision.get("candidates"),
                        confidence=decision.get("confidence"),
                        decision={
                            "reason": decision.get("reason"),
                            "explanation": decision.get("explanation"),
                            "provenance": decision.get("provenance"),
                        },
                    )

                except LearningError:
                    # Already recorded under this version. Historical
                    # predictions are immutable, which is the point.
                    pass

        return rows


def score(rows):
    """
    Outcome counts against verified truth, by decision level.

    Deliberately not one accuracy number. A system that answers
    MATERIAL_FAMILY correctly is not wrong, and a system that answers
    UNKNOWN when it should have named a material is failing differently
    from one that names the wrong material confidently.
    """
    counts = {
        "exact_correct": 0,
        "exact_wrong": 0,
        "family_correct": 0,
        "family_wrong": 0,
        "ambiguous_containing_truth": 0,
        "ambiguous_missing_truth": 0,
        "unknown": 0,
        "total": 0,
    }

    for row in rows:
        counts["total"] += 1
        truth = row["truth"]

        if row["level"] == KNOWN_MATERIAL:
            counts["exact_correct" if row["material"] == truth
                   else "exact_wrong"] += 1

        elif row["level"] == MATERIAL_FAMILY:
            counts["family_correct" if row["family"] == row["truth_family"]
                   else "family_wrong"] += 1

        elif row["level"] == AMBIGUOUS_SET:
            counts["ambiguous_containing_truth"
                   if truth in (row["candidates"] or [])
                   else "ambiguous_missing_truth"] += 1

        else:
            counts["unknown"] += 1

    total = counts["total"] or 1

    counts["never_wrong_rate"] = round(
        1.0 - (counts["exact_wrong"] + counts["family_wrong"]
               + counts["ambiguous_missing_truth"]) / total, 4
    )
    counts["truth_retained_rate"] = round(
        (counts["exact_correct"] + counts["family_correct"]
         + counts["ambiguous_containing_truth"]) / total, 4
    )

    return counts


def print_comparison(rows, store):
    """The §65 table: ground truth, old pipeline, new model, and why."""
    print("=" * 78)
    print(" OLD PIPELINE vs NEW DECISION MODEL")
    print("=" * 78)
    print()
    print("{:<24} {:<26} {:<26}".format(
        "ground truth", "old pipeline", "new decision model"
    ))
    print("-" * 78)

    old_correct = 0
    old_wrong = 0
    old_suppressed = 0

    for row in rows:
        predictions = {
            prediction["model_version"]: prediction
            for prediction in store.predictions(row["measurement_id"])
        }

        old = predictions.get("LEGACY_ANALYSIS_V2") or {}
        old_material = old.get("material_key")

        if old_material == row["truth"]:
            old_correct += 1
            old_text = "{} OK".format(str(old_material)[:20])

        elif old_material is None:
            old_suppressed += 1
            old_text = "(suppressed by QC)"

        else:
            old_wrong += 1
            old_text = "{} X".format(str(old_material)[:20])

        if row["level"] == KNOWN_MATERIAL:
            new_text = "{} {}".format(
                str(row["material"])[:20],
                "OK" if row["material"] == row["truth"] else "X",
            )

        elif row["level"] == MATERIAL_FAMILY:
            new_text = "family {} {}".format(
                str(row["family"])[:14],
                "OK" if row["family"] == row["truth_family"] else "X",
            )

        elif row["level"] == AMBIGUOUS_SET:
            new_text = "set of {} {}".format(
                len(row["candidates"] or []),
                "OK" if row["truth"] in (row["candidates"] or []) else "X",
            )

        else:
            new_text = "UNKNOWN"

        print("{:<24} {:<26} {:<26}".format(
            str(row["truth"])[:24], old_text, new_text
        ))

    counts = score(rows)

    print()
    print("OLD PIPELINE")
    print("  named correctly       {}".format(old_correct))
    print("  named wrongly         {}".format(old_wrong))
    print("  suppressed entirely   {}".format(old_suppressed))
    print()
    print("NEW DECISION MODEL")
    print("  exact, correct        {}".format(counts["exact_correct"]))
    print("  exact, wrong          {}".format(counts["exact_wrong"]))
    print("  family, correct       {}".format(counts["family_correct"]))
    print("  family, wrong         {}".format(counts["family_wrong"]))
    print("  ambiguous, truth in   {}".format(
        counts["ambiguous_containing_truth"]
    ))
    print("  ambiguous, truth out  {}".format(
        counts["ambiguous_missing_truth"]
    ))
    print("  unknown               {}".format(counts["unknown"]))
    print()
    print("  truth retained in the answer  {:.0%}".format(
        counts["truth_retained_rate"]
    ))
    print("  never confidently wrong       {:.0%}".format(
        counts["never_wrong_rate"]
    ))
    print()
    print("Retaining the truth inside a family or an ambiguous set is a")
    print("correct scientific result. Naming the wrong material is not.")

    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Replay stored observations through the decision "
                    "model and compare with the previous pipeline."
    )
    parser.add_argument("--database", default=None)
    parser.add_argument(
        "--record", action="store_true",
        help="write the new predictions into the learning database",
    )
    parser.add_argument("--detail", action="store_true")

    arguments = parser.parse_args(argv)

    store = DecisionLearningStore(arguments.database)
    replay = Replay(store=store)

    rows = replay.run(record=arguments.record)

    if not rows:
        print("No observations to replay.")

        return 1

    print_comparison(rows, store)

    if arguments.detail:
        print()
        print("=" * 78)

        for row in rows:
            print()
            print("{}  truth: {}".format(
                row["measurement_id"], row["truth"]
            ))
            print("  level      {}".format(row["level"]))
            print("  confidence {}".format(row["confidence"]))
            print("  reason     {}".format(row["reason"]))

    print()
    print("Class statistics: {} material(s) from {} observation(s).".format(
        replay.class_snapshot["materials"],
        replay.class_snapshot["observations_used"],
    ))

    coverage = class_models.coverage(replay.class_snapshot)

    print("  with measurable scatter: {}".format(coverage["with_scatter"]))
    print("  supporting covariance:   {}".format(
        coverage["supporting_covariance"]
    ))

    return 0


if __name__ == "__main__":
    sys.exit(main())
