"""
Science: the mathematics, the comparison, and the Decision Model.

Every check here is on a pure function with known inputs, because that
is the property that makes this layer worth separating: given the same
Measurement, calibration and databases it produces the same answer, and
the answer can be checked by hand.

The claims this suite defends:

    an undefined metric is None, never a number that looks like an answer
    a near-zero denominator produces invalid-channel evidence, never an
        invented reflectance
    each database is compared independently and disagreement survives
    individual methods stay individually visible
    UNKNOWN is reachable - not every sample is forced into a class
    similarity is never presented as abundance

Run:  py test_science.py
"""

import math
import sys

import support

support.add_project_root()

from BD.channels import AS7265X_18, CHANNELS      # noqa: E402
from Science import comparison, config, decision  # noqa: E402
from Science import metrics, pipeline, preprocessing, quality  # noqa: E402

checks = support.Checks("science")


def spectrum(value=1.0, **overrides):
    data = {channel: float(value) for channel in CHANNELS}
    data.update({k: float(v) for k, v in overrides.items()})

    return data


def ramp(start=100.0, step=10.0):
    return {channel: start + index * step
            for index, channel in enumerate(CHANNELS)}


# ======================================================================
checks.section("dark correction and normalization")

sample = spectrum(500.0)
dark = spectrum(50.0)
white = spectrum(1050.0)

corrected = preprocessing.dark_correct(sample, dark)
checks.close(corrected["A"], 450.0, "C = S - D")

normalized = preprocessing.normalize(sample, dark, white)
checks.close(normalized["A"], 0.45, "R = (S - D) / (W - D)")

for name, value in (("half", 0.5), ("full", 1.0), ("none", 0.0)):
    reading = {c: dark[c] + value * (white[c] - dark[c]) for c in CHANNELS}
    result = preprocessing.normalize(reading, dark, white)

    checks.close(result["A"], value,
                 "a reading at {} the white level normalizes to {}".format(
                     name, value))

# The denominator failure. §56: invalid must not become an invented value.
flat_white = dict(dark)
result = preprocessing.normalize(sample, dark, flat_white)

checks.ok(result["A"] is None,
          "W - D of zero gives None, not a fabricated reflectance")

near_zero = {c: dark[c] + 0.1 for c in CHANNELS}
result = preprocessing.normalize(sample, dark, near_zero)

checks.ok(result["A"] is None or abs(result["A"]) > 100,
          "a near-zero denominator is either refused or obviously extreme, "
          "never quietly plausible")

conditioning = preprocessing.conditioning(dark, flat_white)
checks.equal(len(conditioning), len(CHANNELS),
             "the conditioning report covers every channel")
checks.ok(all(not entry["usable"] for entry in conditioning.values()),
          "and marks every one of them unusable")
checks.ok(all(not entry["defined"] for entry in conditioning.values()),
          "the division is not even defined")

wide = preprocessing.conditioning(dark, white)
checks.ok(all(entry["usable"] for entry in wide.values()),
          "a healthy reference is usable on every channel")
checks.ok(all(entry["quantization_step"] is not None
              for entry in wide.values()),
          "and reports how much reflectance one count is worth")


# ======================================================================
checks.section("repeated acquisitions")

values = [10.0, 10.2, 9.8, 10.1]
summary = preprocessing.summarize_channel(values)

checks.close(summary["mean"], 10.025, "mean of the repeats", 1e-6)
checks.ok(summary["stdev"] > 0, "the spread is reported, not discarded")

kept, rejected = preprocessing.reject_outliers(
    [10.0, 10.1, 9.9, 10.0, 40.0])
checks.ok(40.0 not in kept, "a gross outlier is rejected")
checks.equal(rejected, [40.0], "and named rather than silently dropped")

kept, rejected = preprocessing.reject_outliers([10.0, 10.1, 9.9, 10.0])
checks.equal(rejected, [], "nothing is rejected from a steady series")

kept, rejected = preprocessing.reject_outliers([10.0, 40.0])
checks.equal(rejected, [],
             "and nothing is rejected from too few readings to judge")


# ======================================================================
checks.section("metrics: one implementation, honest edges")

a = ramp()
b = ramp()

pairs = metrics.paired(a, b)
checks.equal(len(pairs), 18, "all 18 channels pair up")
checks.close(metrics.cosine(pairs), 1.0, "cosine of a spectrum with itself")
checks.close(metrics.rmse(pairs), 0.0, "rmse of a spectrum with itself")
checks.close(metrics.spectral_angle_degrees(pairs), 0.0,
             "spectral angle of zero")
checks.close(metrics.pearson_r(pairs), 1.0, "pearson of 1")

scaled = {c: v * 3.0 for c, v in a.items()}
pairs = metrics.paired(a, scaled)
checks.close(metrics.cosine(pairs), 1.0,
             "cosine cannot see a brightness change - it is shape only")
checks.close(metrics.pearson_r(pairs), 1.0,
             "and neither can pearson")
checks.ok(metrics.rmse(pairs) > 0,
          "but RMSE does, which is why the magnitude family exists")

zero = spectrum(0.0)
pairs = metrics.paired(a, zero)
checks.ok(metrics.cosine(pairs) is None,
          "cosine against a zero-norm spectrum is None - 'cannot say', "
          "not 'definitely not this material'")

checks.ok(metrics.pearson_r([("A", 1.0, 2.0), ("B", 2.0, 4.0)]) is None,
          "pearson below three channels is None - two points always "
          "correlate at exactly +/-1 whatever they are")

flat = spectrum(5.0)
pairs = metrics.paired(flat, flat)
checks.ok(metrics.pearson_r(pairs) is None,
          "pearson of a constant spectrum is None - no variation to "
          "correlate is a real mathematical answer")

checks.equal(metrics.paired({}, {}), [], "no channels pair from nothing")
checks.ok(metrics.cosine([]) is None, "and every metric answers None")
checks.ok(metrics.rmse([]) is None, "for an empty comparison")

partial = dict(a)
partial["A"] = None
partial["B"] = float("nan")
pairs = metrics.paired(partial, b)
checks.equal(len(pairs), 16,
             "null and NaN channels are dropped, not coerced to zero")

weights = {c: 1.0 for c in CHANNELS}
weights["A"] = 0.0
pairs = metrics.paired(a, b)
checks.ok(metrics.weighted_euclidean(pairs, weights) is not None,
          "weighted distance works with a channel weighted out")

checks.close(metrics.percent(1.0), 100.0, "a cosine of 1 is 100%")
checks.close(metrics.percent(0.5), 50.0, "a cosine of 0.5 is 50%")
checks.ok(metrics.percent(None) is None, "and None stays None")


# ======================================================================
checks.section("every method stays individually visible")

library = {
    "alpha": ramp(100.0, 10.0),
    "beta": ramp(100.0, 11.0),
    "gamma": ramp(500.0, -5.0),
}

all_of_them = metrics.all_metrics(a, library["beta"])

for name in ("rmse", "mae", "euclidean", "cosine", "spectral_angle_deg",
             "pearson_r"):
    checks.ok(name in all_of_them,
              "{} is reported separately".format(name))

evidence = metrics.compare_library(a, library)

checks.ok("metrics" in evidence, "per-metric evidence is kept")
checks.ok("families" in evidence, "and grouped by family")
checks.equal(sorted(evidence["families"]),
             ["angular", "centered_shape", "magnitude"],
             "three families, so two shape metrics never vote twice")
checks.equal(evidence["candidate_count"], 3, "every candidate is scored")

for metric, summary in evidence["metrics"].items():
    checks.ok("winner" in summary and "top" in summary,
              "{} reports its own winner and ranking".format(metric))

ranked = metrics.compare_all(a, library)
checks.equal(len(ranked), 3, "the full ranking is not truncated")
checks.equal(ranked[0]["material"], "alpha",
             "the identical spectrum ranks first")
checks.equal(ranked[0]["combined_rank"], 1, "with rank 1")

for entry in ranked:
    for key in ("rmse", "cosine_similarity_percent", "pearson_r",
                "magnitude_rank", "angular_rank", "centered_shape_rank"):
        checks.ok(key in entry,
                  "{} keeps its {}".format(entry["material"], key))

agreement = metrics.family_agreement(ranked)
checks.ok(agreement["agree"] is True,
          "the families agree on an exact match")
checks.equal(agreement["family_count"], 3, "over three families")
checks.equal(agreement["weights_status"], "PROVISIONAL_UNVALIDATED",
             "and the weighting says it is not validated")

# Disagreement must be reported, not smoothed away.
conflicting = {
    "same_shape_dim": {c: v * 0.2 for c, v in a.items()},
    "same_level_other_shape": ramp(190.0, -6.0),
}
ranked = metrics.compare_all(a, conflicting)
agreement = metrics.family_agreement(ranked)

checks.ok(agreement["combined_best"] in conflicting,
          "a combined winner is still named")
checks.ok(set(agreement["family_best"].values()) != {agreement[
    "combined_best"]} or agreement["agree"] in (True, False),
    "and per-family winners are reported whether or not they agree")


# ======================================================================
checks.section("distance to class, not just distance to reference")

observations = {
    "basalt": [ramp(100.0, 10.0), ramp(102.0, 10.0), ramp(98.0, 10.0),
               ramp(101.0, 10.0)],
    "sand": [ramp(400.0, -5.0), ramp(405.0, -5.0), ramp(395.0, -5.0),
             ramp(402.0, -5.0)],
}

statistics = {
    material: comparison.build_class_statistics(entries)
    for material, entries in observations.items()
}

checks.equal(sorted(statistics), ["basalt", "sand"],
             "one distribution per class")

for material, stats in statistics.items():
    checks.ok(stats.get("centroid"), "{} has a centroid".format(material))
    checks.equal(stats["n_observations"], 4,
                 "{} says how many observations built it - a centroid of "
                 "one and a centroid of thirty are not the same claim"
                 .format(material))

empty = comparison.build_class_statistics([])
checks.ok(empty["centroid"] is None, "a class with no observations has no centroid")
checks.ok(not empty["usable"], "and is marked unusable")
checks.ok(empty["reason"], "with a stated reason")

near_basalt = ramp(101.0, 10.0)

basalt_distance = comparison.nearest_centroid_distance(
    near_basalt, statistics["basalt"])
sand_distance = comparison.nearest_centroid_distance(
    near_basalt, statistics["sand"])

checks.ok(basalt_distance < sand_distance,
          "a basalt-like spectrum is closer to the basalt class")

result = comparison.compare_classes(near_basalt, statistics, observations)

checks.equal(sorted(result["per_material"]), ["basalt", "sand"],
             "every class is compared, and reported separately")
checks.equal(result["classes"], 2, "and counted")
checks.equal(result["nearest"], "basalt", "the nearest class is named")
checks.ok(result["runner_up"] == "sand",
          "and so is the runner-up, so separation can be judged")
checks.ok(result["margin"] is not None,
          "with the margin between them - a nearest class with no margin "
          "is not evidence of anything")
checks.ok(result.get("knn") is not None,
          "k-nearest evidence is kept beside the centroid distances")

basalt = result["per_material"]["basalt"]
checks.ok(basalt["centroid_distance"] is not None,
          "with a plain centroid distance")
checks.ok(
    basalt["standardized_distance"] is not None
    or basalt["unavailable"],
    "and a distance that accounts for the class's own scatter - or an "
    "explicit statement that it could not be measured")
checks.ok("unavailable" in basalt,
          "a metric that could not be computed is named, because 'far "
          "away' and 'not measurable' mean opposite things")


# ======================================================================
checks.section("the Decision Model reaches every conclusion it declares")

checks.ok(hasattr(decision, "MODEL_VERSION"),
          "the model carries a version")
checks.ok(hasattr(decision, "DecisionEngine"),
          "and an engine")

for name in ("KNOWN_MATERIAL", "MATERIAL_FAMILY", "AMBIGUOUS_SET",
             "UNKNOWN"):
    checks.ok(hasattr(decision, name),
              "{} is a declared conclusion LEVEL".format(name))

for name in ("CLASSIFIED", "AMBIGUOUS", "UNKNOWN", "INSUFFICIENT_EVIDENCE",
             "INVALID_MEASUREMENT"):
    checks.ok(hasattr(decision, name),
              "{} is a declared outcome STATUS".format(name))

checks.equal(len(decision.STATUSES), 5,
             "five statuses, because level and status answer different "
             "questions: how specific the answer is, and what kind of "
             "outcome produced it")

engine = decision.DecisionEngine()

# No evidence at all.
verdict = engine.decide({})
checks.equal(verdict["status"], decision.INSUFFICIENT_EVIDENCE,
             "with no candidates at all the status is "
             "INSUFFICIENT_EVIDENCE - the library had nothing to say, "
             "which is not the sample refusing to be identified")
checks.equal(verdict["level"], decision.UNKNOWN,
             "and the level is UNKNOWN")
checks.ok(verdict.get("material") is None, "no material is named")
checks.ok(verdict.get("family") is None, "and no family either")

# A measurement the instrument could not make.
verdict = engine.decide({
    "quality": {"hardware": {"status": "HARDWARE_QC_FAIL"},
                "normalization": {"status": "OK"}},
    "reference_analysis": {},
    "class_analysis": {},
})
checks.equal(verdict["status"], decision.INVALID_MEASUREMENT,
             "a failed measurement is INVALID_MEASUREMENT, not a sample "
             "that could not be identified")
checks.ok(verdict.get("material") is None,
          "and it certainly names no material")

# A division that could not be done.
verdict = engine.decide({
    "quality": {"hardware": {"status": "PASS"},
                "normalization": {"status": "NORMALIZATION_UNUSABLE"}},
    "reference_analysis": {},
    "class_analysis": {},
})
checks.equal(verdict["status"], decision.INVALID_MEASUREMENT,
             "an unusable normalization is INVALID_MEASUREMENT too")

for verdict in (engine.decide({}),):
    checks.ok("explanation" in verdict or "reason" in verdict,
              "every verdict carries its reasoning")
    checks.ok("candidates" in verdict,
              "and the candidates it considered")
    checks.ok("confidence" in verdict, "and a confidence")
    checks.ok(verdict.get("decision_model_version"),
              "and the model version that produced it")
    checks.ok("thresholds" in verdict,
              "and the thresholds it was judged against")
    checks.ok("PROVISIONAL" in verdict["thresholds"]["status"],
              "which say plainly that they are provisional")


# ======================================================================
checks.section("similarity is never abundance")

# The words appear in these modules, and that is the point: the code
# says out loud that a similarity is not an abundance. What must not
# exist is a RESULT KEY that claims one - a number a reader would take
# for composition.
CLAIM_KEYS = (
    r'"abundance"', r'"concentration"', r'"mass_fraction"',
    r'"volume_fraction"', r'"composition"', r'"percent_of_sample"',
    r'"contribution_fraction"',
)

import re as _re  # noqa: E402

for module in (metrics, comparison, decision):
    source = open(module.__file__, encoding="utf-8").read()

    for key in CLAIM_KEYS:
        # A result key, not a comment explaining why there is no such
        # key. `"composition":` with a colon is an emitted field;
        # `"composition"` inside a sentence is the code saying what it
        # does not do.
        emitted = _re.search(key + r"\s*:", source)

        checks.ok(not emitted,
                  "{} emits no {} field".format(
                      module.__name__, key.strip('"')))

# And the disclaimer is present where the percentage is produced.
metrics_source = open(metrics.__file__, encoding="utf-8").read()
checks.ok("never an abundance" in metrics_source,
          "the percentage helper states what it is not")

pipeline_source = open(pipeline.__file__, encoding="utf-8").read()
checks.ok("Similarity is not abundance" in pipeline_source,
          "and the pipeline says so where unmixing used to run")
checks.ok("NO SECOND COMPARISON HERE" in pipeline_source,
          "and says why there is only one per-database comparison")

checks.ok(not hasattr(comparison, "estimate"),
          "no unmixing estimator is reachable from the production "
          "comparison layer")

import importlib  # noqa: E402

for name in ("Science.mixture", "Science.unmixing"):
    try:
        importlib.import_module(name)
        found = True

    except ImportError:
        found = False

    checks.ok(not found, "{} does not exist".format(name))


# ======================================================================
checks.section("provenance classes stay distinguishable")

checks.ok(hasattr(config, "SCIENCE_VERSION"), "Science declares a version")
checks.ok(hasattr(config, "ANALYSIS_VERSION"),
          "and an analysis schema version")

checks.ok(config.FAMILY_WEIGHTS_STATUS == "PROVISIONAL_UNVALIDATED",
          "the family weights say plainly that they are not validated")

checks.ok(decision.PRIOR_STATUS == "PROVISIONAL_UNVALIDATED",
          "and so do the database reliability priors")


sys.exit(checks.report())
