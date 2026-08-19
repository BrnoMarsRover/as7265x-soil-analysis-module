"""
The decision layer and the learning database.

Four rules are tested here that the whole design rests on, and each of
them is a rule about what the system must REFUSE to do:

    a prediction can never become ground truth
    a historical prediction can never be rewritten
    a raw measurement can never be edited
    a decision can never modify a reference database

Plus the four decision levels, the hierarchy that reaches them, and the
group-aware validation that stops a model scoring itself on data it has
already seen.
"""

import json
import sys
import tempfile
from pathlib import Path

import support
from support import Checks

sys.path.insert(0, str(support.REPO))

from BD import decision_learning                        # noqa: E402
from BD.acquisition_profiles import (                   # noqa: E402
    AcquisitionProfileStore,
    blank_profile,
    compare,
    fingerprint,
)
from BD.channels import CHANNELS                        # noqa: E402
from BD.decision_learning import (                      # noqa: E402
    DecisionLearningStore,
    LABEL_EXACT_MATERIAL,
    LABEL_FAMILY,
    LABEL_UNKNOWN_SAMPLE,
    LearningError,
    OPERATOR_ASSERTED,
    UNVERIFIED,
    VERIFIED,
)
from DecisionModel import (                             # noqa: E402
    engine as engine_module,
    evidence_fusion,
    hierarchy,
    model_registry,
    unknown_detection,
)
from DecisionModel.training import cross_validation     # noqa: E402


def flat(value):
    return {channel: value for channel in CHANNELS}


def distances_median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2

    if not ordered:
        return None

    if len(ordered) % 2:
        return ordered[middle]

    return (ordered[middle - 1] + ordered[middle]) / 2.0


class FakeTaxonomy:
    def __init__(self, families):
        self.families = families

    def family_of(self, material):
        return self.families.get(material)


class FakeReliability:
    """Everything trusted equally, so the fusion maths is what is tested."""

    def database_weight(self, key):
        return {"DB1": 0.8, "DB2": 1.0, "DB3": 0.4}.get(key, 0.5)

    def class_reliability(self, key, material):
        return {"rating": "UNRATED", "basis": "TEST"}

    def confusions_for(self, material, limit=4):
        return []

    def status(self):
        return {"test": True}


def reference_analysis(scores, database="DB1"):
    """
    A minimal reference_analysis block with one metric family.

    The separation statistics are COMPUTED from the scores rather than
    hardcoded, because they are what the decision layer weighs: a helper
    that stamped the same z_separation on a decisive field and a photo
    finish would test nothing at all.
    """
    ordered = sorted(scores.items(), key=lambda item: -item[1])
    values = sorted(score for _name, score in ordered)

    median = distances_median(values)
    mad = distances_median([abs(value - median) for value in values])

    winner_score = ordered[0][1]

    return {
        "databases": {
            database: {
                "available": True,
                "database": database,
                "metrics": {
                    "cosine": {
                        "family": "angular",
                        "higher_is_better": True,
                        "winner": ordered[0][0],
                        "winner_score": ordered[0][1],
                        "top": [
                            {"material": name, "score": score}
                            for name, score in ordered
                        ],
                    },
                },
                "families": {
                    "angular": {
                        "metric": "cosine",
                        "winner": ordered[0][0],
                        "winner_score": winner_score,
                        "absolute_goodness": winner_score,
                        "absolute_margin": (
                            ordered[0][1] - ordered[1][1]
                            if len(ordered) > 1 else None
                        ),
                        "relative_margin": (
                            (ordered[0][1] - ordered[1][1])
                            / (values[-1] - values[0])
                            if len(ordered) > 1 and values[-1] > values[0]
                            else 1.0
                        ),
                        "z_separation": (
                            abs(winner_score - median) / mad if mad else None
                        ),
                    },
                },
            },
        },
    }


def evidence_for(scores, database="DB1", hardware="PASS",
                 normalization="OK", reliable=54):
    return {
        "schema_version": 1,
        "measurement": {"measurement_id": "TEST"},
        "acquisition": {},
        "quality": {
            "hardware": {"status": hardware, "usable": hardware == "PASS"},
            "normalization": {"status": normalization},
        },
        "channel_reliability": {
            "features_total": 54,
            "raw_valid_total": 54,
            "normalized_valid_total": reliable,
        },
        "reference_analysis": reference_analysis(scores, database),
        "class_analysis": {"available": False, "reason": "none yet"},
        "warnings": [],
    }


def main_tests():
    checks = Checks("Decision model and learning history")

    # ==================================================================
    checks.section("1. the learning database refuses what it must")

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "learning.sqlite3"
        store = DecisionLearningStore(path)

        store.add_observation(
            "m1", {"white": flat(100.0)}, session_id="s1",
            sample_group="jar-1",
        )

        checks.equal(
            store.count_observations(), 1, "an observation is recorded"
        )
        checks.ok(store.verify_raw("m1"), "with a hash over its raw payload")

        checks.raises(
            LearningError,
            lambda: store.add_observation("m1", {"white": flat(1.0)}),
            "and a measurement can never be overwritten",
        )

        checks.raises(
            LearningError,
            lambda: store.add_observation("m2", {}),
            "an observation without raw spectra is refused outright",
        )

        # RULE 1. This is the one that stops the system learning its own
        # mistakes and growing more confident with every round.
        checks.raises(
            LearningError,
            lambda: store.add_ground_truth(
                "m1", LABEL_EXACT_MATERIAL, material_key="Talc",
                verification_status=VERIFIED,
                verification_source="decision_model_v3",
            ),
            "a label sourced from a MODEL is refused: a prediction is "
            "never ground truth",
        )

        for source in ("auto-classifier", "self-training", "inference run"):
            checks.raises(
                LearningError,
                lambda source=source: store.add_ground_truth(
                    "m1", LABEL_EXACT_MATERIAL, material_key="Talc",
                    verification_status=VERIFIED,
                    verification_source=source,
                ),
                "and so is '{}'".format(source),
            )

        store.add_ground_truth(
            "m1", LABEL_EXACT_MATERIAL, material_key="Bentonite",
            material_id="bentonite", family_id="phyllosilicate_clay",
            verification_status=VERIFIED,
            verification_source="operator_known_reference_material",
            certainty=1.0,
        )

        checks.equal(
            store.get_ground_truth("m1")["material_key"], "Bentonite",
            "an operator-verified label is accepted",
        )

        checks.raises(
            LearningError,
            lambda: store.add_ground_truth(
                "m1", LABEL_EXACT_MATERIAL, material_key="Talc",
                verification_status=VERIFIED,
                verification_source="operator_known_reference_material",
            ),
            "and is not silently replaced by a later one",
        )

        # RULE 2. Model versions accumulate; none overwrites another.
        store.add_prediction("m1", "V001", "KNOWN_MATERIAL",
                             material_key="Green Clay (Illite)")
        store.add_prediction("m1", "V002", "MATERIAL_FAMILY",
                             family_id="phyllosilicate_clay")

        checks.equal(
            len(store.predictions("m1")), 2,
            "two model versions leave two predictions",
        )
        checks.raises(
            LearningError,
            lambda: store.add_prediction("m1", "V001", "UNKNOWN"),
            "and a historical prediction cannot be rewritten",
        )

        # RULE 4. Only trusted labels train.
        store.add_observation("m3", {"white": flat(50.0)})
        store.add_ground_truth(
            "m3", LABEL_EXACT_MATERIAL, material_key="Talc",
            verification_status=OPERATOR_ASSERTED,
            verification_source="operator recollection",
        )

        checks.equal(
            [row["measurement_id"] for row in store.labelled()], ["m1"],
            "the default training set is VERIFIED only",
        )
        checks.equal(
            len(store.labelled(levels=(VERIFIED, OPERATOR_ASSERTED))), 2,
            "an operator assertion is included only when asked for",
        )
        checks.raises(
            LearningError,
            lambda: store.labelled(levels=(UNVERIFIED,)),
            "and UNVERIFIED can never be requested as a training label",
        )

        store.add_observation("m4", {"white": flat(10.0)})
        store.add_ground_truth("m4", LABEL_UNKNOWN_SAMPLE)

        checks.equal(
            store.get_ground_truth("m4")["verification_status"], "UNKNOWN",
            "'I do not know what this was' is recorded as UNKNOWN",
        )
        checks.equal(
            len(store.labelled()), 1,
            "and never appears in a training set",
        )

        store.add_observation("m5", {"white": flat(10.0)})
        store.add_ground_truth(
            "m5", LABEL_FAMILY, family_id="carbonate",
            verification_status=VERIFIED, verification_source="operator",
        )

        checks.equal(
            len(store.labelled(label_types=(LABEL_FAMILY,))), 1,
            "a family-only label is stored and is separately selectable",
        )

        confusion = store.confusion(model_version="V001")

        checks.equal(
            confusion[0]["actual"], "Bentonite",
            "confusion history joins truth to prediction",
        )
        checks.equal(
            confusion[0]["predicted"], "Green Clay (Illite)",
            "and keeps what the model actually said",
        )

        store.close()

    # ==================================================================
    checks.section("2. acquisition profiles decide compatibility")

    profile = blank_profile()
    profile["sensor"].update({
        "measurement_mode": 3, "gain": 2, "gain_x": "16x",
        "integration_cycles": 100,
    })
    profile["illumination"].update({
        "white_current_ma": 25, "uv_current_ma": 25, "ir_current_ma": 25,
    })

    same = json.loads(json.dumps(profile))
    different_gain = json.loads(json.dumps(profile))
    different_gain["sensor"]["gain_x"] = "64x"

    checks.equal(
        fingerprint(profile), fingerprint(same),
        "the same conditions fingerprint identically",
    )
    checks.ok(
        fingerprint(profile) != fingerprint(different_gain),
        "and a different gain is a different profile",
    )

    verdict = compare(profile, different_gain)

    checks.equal(
        verdict["status"], "INCOMPATIBLE",
        "so the two are incompatible",
    )
    checks.equal(
        verdict["differences"][0]["field"], "sensor.gain_x",
        "and the field that differs is named",
    )

    unknown_both = compare(profile, same)

    checks.equal(
        unknown_both["status"], "COMPATIBLE_WITH_UNKNOWNS",
        "two profiles that share unmeasured geometry are compatible only "
        "as far as anyone actually knows - which is a third verdict, not "
        "a yes",
    )
    checks.ok(
        "geometry.sensor_to_sample_distance_mm"
        in unknown_both["unknown_on_both_sides"],
        "and the unknown fields are listed rather than assumed equal",
    )

    repeats_differ = json.loads(json.dumps(profile))
    repeats_differ["illumination"]["warmup_ms"] = 500

    checks.equal(
        fingerprint(profile), fingerprint(repeats_differ),
        "a warm-up time does not change what a count MEANS, so it is "
        "outside the fingerprint",
    )

    with tempfile.TemporaryDirectory() as directory:
        store = AcquisitionProfileStore(Path(directory) / "profiles.json")

        first = store.ensure(profile)
        again = store.ensure(json.loads(json.dumps(profile)))

        checks.equal(
            first["profile_id"], again["profile_id"],
            "asking twice for the same conditions returns one profile",
        )
        checks.equal(store.count(), 1, "and stores it once")

        store.ensure(different_gain)

        checks.equal(store.count(), 2, "while different conditions are new")

    # ==================================================================
    checks.section("3. fusion keeps magnitude and never sums ranks")

    reliability = FakeReliability()

    decisive = evidence_fusion.fuse(
        reference_analysis({"A": 0.99, "B": 0.60, "C": 0.55, "D": 0.50}),
        reliability,
    )
    photo_finish = evidence_fusion.fuse(
        reference_analysis({"A": 0.990, "B": 0.989, "C": 0.988, "D": 0.987}),
        reliability,
    )

    checks.equal(
        decisive["candidates"][0]["material"], "A",
        "the leader is found in a decisive field",
    )
    checks.equal(
        photo_finish["candidates"][0]["material"], "A",
        "and in a field where everything scores alike",
    )

    decisive_margin = evidence_fusion.separability(decisive["candidates"])
    tight_margin = evidence_fusion.separability(photo_finish["candidates"])

    checks.ok(
        decisive_margin["margin"] > tight_margin["margin"],
        "but only the decisive one produces a real margin - the case a "
        "rank of 1 cannot distinguish",
    )

    checks.ok(
        "Ranks are never summed" in decisive["method"],
        "and the method says so explicitly",
    )

    # ==================================================================
    checks.section("4. the four decision levels are reachable")

    taxonomy = FakeTaxonomy({
        "Magnesium Carbonate": "carbonate",
        "Calcium Carbonate (Chalk)": "carbonate",
        "Sodium Bicarbonate": "carbonate",
        "Talc": "phyllosilicate",
        "Copper(II) Sulfate": "sulfate",
    })

    engine = engine_module.DecisionEngine(
        taxonomy=taxonomy, reliability=reliability
    )

    decisive_case = engine.decide(evidence_for({
        "Copper(II) Sulfate": 0.999,
        "Talc": 0.60,
        "Magnesium Carbonate": 0.55,
    }))

    checks.equal(
        decisive_case["level"], engine_module.KNOWN_MATERIAL,
        "a leader far clear of the field names a material",
    )
    checks.equal(
        decisive_case["material"], "Copper(II) Sulfate",
        "and names the right one",
    )

    family_case = engine.decide(evidence_for({
        "Magnesium Carbonate": 0.980,
        "Calcium Carbonate (Chalk)": 0.975,
        "Sodium Bicarbonate": 0.970,
    }))

    checks.ok(
        family_case["level"] in (
            engine_module.MATERIAL_FAMILY, engine_module.AMBIGUOUS_SET
        ),
        "three members of one family, none separable, does not name a "
        "material",
    )

    if family_case["level"] == engine_module.MATERIAL_FAMILY:
        checks.equal(
            family_case["family"], "carbonate",
            "and reports the family instead",
        )

    else:
        checks.equal(
            family_case["family"], "carbonate",
            "and the ambiguous set shares one family",
        )

    broken = engine.decide(evidence_for(
        {"Talc": 0.99, "Magnesium Carbonate": 0.5},
        hardware="HARDWARE_QC_FAIL",
    ))

    checks.equal(
        broken["level"], engine_module.UNKNOWN,
        "a hardware QC failure forces UNKNOWN however good the scores",
    )
    checks.equal(
        broken["confidence"], "NONE", "with no confidence"
    )

    # The change that recovers six real measurements: a normalization
    # warning must NOT do the same thing.
    warned = engine.decide(evidence_for(
        {"Copper(II) Sulfate": 0.999, "Talc": 0.60,
         "Magnesium Carbonate": 0.55},
        normalization="NORMALIZATION_WARNING",
    ))

    checks.ok(
        warned["level"] != engine_module.UNKNOWN,
        "a NORMALIZATION_WARNING does not suppress the conclusion",
    )
    checks.ok(
        engine_module.NORMALIZATION_WARNING
        in warned["secondary_interpretations"],
        "it is reported as a secondary interpretation instead",
    )
    checks.ok(
        warned["level"] in engine_module.LEVELS,
        "and the level is still one of the four",
    )

    for decision in (decisive_case, family_case, broken, warned):
        checks.ok(
            decision["level"] in engine_module.LEVELS,
            "every decision is one of the four levels",
        )
        checks.ok(
            bool(decision.get("explanation")),
            "and carries an explanation",
        )
        checks.ok(
            "probability" not in decision["explanation"].lower()
            or "not a" in decision["explanation"].lower(),
            "which never claims a probability",
        )

    checks.ok(
        "provenance" in decisive_case
        and "decision_model_version" in decisive_case["provenance"],
        "and provenance naming the model version",
    )

    # ==================================================================
    checks.section("5. out-of-distribution detection")

    fusion = evidence_fusion.fuse(
        reference_analysis({"Talc": 0.99, "Magnesium Carbonate": 0.98}),
        reliability,
    )
    separation = evidence_fusion.separability(fusion["candidates"])

    far_away = evidence_for({"Talc": 0.99, "Magnesium Carbonate": 0.98})
    far_away["class_analysis"] = {
        "available": True,
        "nearest": "Talc",
        "per_material": {
            "Talc": {"within_class_ratio": 8.0, "centroid_distance": 4.0},
        },
    }

    report = unknown_detection.assess(far_away, fusion, separation)

    checks.ok(
        any(reason["code"] == "OUTSIDE_KNOWN_CLASSES"
            for reason in report["reasons"]),
        "a sample far outside every class distribution is detected",
    )
    checks.ok(
        report["unknown_required"],
        "and that alone is enough to force UNKNOWN - a 99% cosine does "
        "not rescue it",
    )

    inside = evidence_for({"Talc": 0.99, "Magnesium Carbonate": 0.60})
    inside["class_analysis"] = {
        "available": True,
        "nearest": "Talc",
        "per_material": {
            "Talc": {"within_class_ratio": 0.4, "centroid_distance": 0.1},
        },
    }

    inside_report = unknown_detection.assess(
        inside,
        evidence_fusion.fuse(
            reference_analysis({"Talc": 0.99, "Magnesium Carbonate": 0.60}),
            reliability,
        ),
        separation,
    )

    checks.ok(
        not any(reason["code"] == "OUTSIDE_KNOWN_CLASSES"
                for reason in inside_report["reasons"]),
        "while a sample inside the class scatter is not",
    )

    # ==================================================================
    checks.section("6. validation cannot leak")

    observations = [
        {"measurement_id": "a1", "sample_group": "jar-A",
         "material_key": "Talc"},
        {"measurement_id": "a2", "sample_group": "jar-A",
         "material_key": "Talc"},
        {"measurement_id": "b1", "sample_group": "jar-B",
         "material_key": "Talc"},
        {"measurement_id": "c1", "sample_group": "jar-C",
         "material_key": "Bentonite"},
    ]

    folds = cross_validation.leave_one_group_out(observations)

    checks.equal(len(folds), 3, "one fold per physical sample, not per file")

    for fold in folds:
        overlap = set(fold.train_ids) & set(fold.test_ids)

        checks.equal(
            overlap, set(),
            "fold {} shares no measurement between train and test".format(
                fold.index
            ),
        )

    jar_a = [fold for fold in folds if fold.held_out == "jar-A"][0]

    checks.ok(
        "a1" not in jar_a.train_ids and "a2" not in jar_a.train_ids,
        "both acquisitions of one jar are held out together - splitting "
        "them would let the model score itself on a spectrum it has "
        "effectively already seen",
    )

    feasible = cross_validation.feasibility(observations)

    checks.ok(
        not feasible["supervised_validation_possible"],
        "a class with a single independent group cannot be validated",
    )
    checks.ok(
        "Bentonite" in feasible["classes_with_one_group"],
        "and the class in question is named",
    )
    checks.ok(
        "zero by construction" in feasible["reason"],
        "with the reason spelled out rather than a silent zero score",
    )

    # ==================================================================
    checks.section("7. model activation is a decision, not a consequence")

    with tempfile.TemporaryDirectory() as directory:
        registry = model_registry.ModelRegistry(
            Path(directory) / "registry.json"
        )

        registry.register("V001", "cold_start", status=model_registry.ACTIVE)
        registry.register("V002", "knn")

        checks.equal(
            registry.active()["version"], "V001", "one model is active"
        )
        checks.raises(
            model_registry.ModelRegistryError,
            lambda: registry.activate("V002"),
            "an unvalidated model cannot be activated",
        )

        registry.set_status("V002", model_registry.VALIDATED)
        registry.activate("V002")

        checks.equal(
            registry.active()["version"], "V002", "a validated one can"
        )
        checks.equal(
            registry.get("V001")["status"], model_registry.RETIRED,
            "and the previous model is retired, not deleted",
        )

        verdict = registry.compare_for_activation(
            {
                "balanced_accuracy": 0.82,
                "per_class_recall": {"Talc": 0.9, "Bentonite": 0.2},
                "unknown_detection_rate": 0.8,
            },
            {
                "balanced_accuracy": 0.78,
                "per_class_recall": {"Talc": 0.8, "Bentonite": 0.8},
                "unknown_detection_rate": 0.8,
            },
        )

        checks.equal(
            verdict["recommendation"], "REJECT",
            "a model that gains overall accuracy while losing a class is "
            "refused",
        )
        checks.equal(
            verdict["blocking"][0]["code"], "CLASS_RECALL_COLLAPSE",
            "and the reason names the collapse",
        )

        unsafe = registry.compare_for_activation(
            {
                "balanced_accuracy": 0.90,
                "per_class_recall": {"Talc": 0.9},
                "unknown_detection_rate": 0.3,
            },
            {
                "balanced_accuracy": 0.78,
                "per_class_recall": {"Talc": 0.9},
                "unknown_detection_rate": 0.8,
            },
        )

        checks.equal(
            unsafe["recommendation"], "REJECT",
            "and so is one that stops being able to answer UNKNOWN",
        )

    # ==================================================================
    checks.section("8. the decision layer cannot touch the databases")

    source = (support.REPO / "DecisionModel").glob("**/*.py")

    forbidden = ("DB1.json", "DB2.json", "DB3.json", "calibration_legacy")

    for path in sorted(source):
        text = path.read_text(encoding="utf-8")

        for token in forbidden:
            # A comment may name a file; an assignment to one is the
            # thing that must never appear.
            checks.ok(
                'open({}'.format(token) not in text
                and 'write' not in text.split(token)[0][-40:]
                if token in text else True,
                "{} never writes {}".format(path.name, token),
            )

    return checks.report()


if __name__ == "__main__":
    sys.exit(main_tests())
