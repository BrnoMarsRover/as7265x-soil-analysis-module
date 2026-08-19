"""
The measurement evidence layer: representations, reliability, distances,
class distance and the EvidencePackage.

The rule under test throughout: this layer produces EVIDENCE and never a
semantic conclusion. It may say "the reference left 8.5 counts of
headroom here"; it may not say "this is Bentonite".
"""

import math
import sys

import support
from support import Checks

sys.path.insert(0, str(support.REPO))

from BD.channels import CHANNELS, WAVELENGTHS          # noqa: E402
from Measurements import (                             # noqa: E402
    channel_reliability,
    class_distance,
    distances,
    evidence as evidence_module,
    preprocessing,
    spectral_features,
)


def flat(value):
    return {channel: value for channel in CHANNELS}


def ramp(start, step):
    return {
        channel: start + index * step
        for index, channel in enumerate(CHANNELS)
    }


def main_tests():
    checks = Checks("Measurement evidence")

    # ==================================================================
    checks.section("1. representations are deterministic and unclipped")

    dark = flat(2.0)
    white = flat(102.0)
    sample = flat(52.0)

    corrected = preprocessing.dark_correct(sample, dark)
    normalized = preprocessing.normalize(sample, dark, white)

    checks.equal(corrected["A"], 50.0, "C = S - D")
    checks.equal(normalized["A"], 0.5, "R = (S - D) / (W - D)")

    # The single most important behaviour in this module: a sample
    # brighter than the reference is REAL DATA, not an error.
    bright = preprocessing.normalize(flat(202.0), dark, white)

    checks.equal(bright["A"], 2.0, "reflectance above 1.0 is kept exactly")

    below = preprocessing.normalize(flat(0.0), dark, white)

    checks.equal(below["A"], -0.02, "and reflectance below 0 is kept too")

    conditioning = preprocessing.conditioning(dark, white)

    checks.equal(
        conditioning["A"]["reference_minus_dark"], 100.0,
        "conditioning reports the dynamic range the reference left",
    )
    checks.ok(
        conditioning["A"]["usable"], "and calls 100 counts usable"
    )

    thin = preprocessing.conditioning(flat(0.0), flat(2.0))

    checks.ok(
        thin["A"]["defined"],
        "two counts of headroom still DEFINES a quotient",
    )
    checks.ok(
        not thin["A"]["usable"],
        "but does not make it usable - and 'defined' and 'usable' are "
        "different questions",
    )
    checks.equal(
        thin["A"]["quantization_step"], 0.5,
        "and one count of noise is half a unit of reflectance there",
    )

    # SNV and unit vector delete brightness on purpose.
    scaled = {channel: value * 3.0 for channel, value in ramp(1.0, 1.0).items()}

    checks.close(
        preprocessing.unit_vector(ramp(1.0, 1.0))["A"],
        preprocessing.unit_vector(scaled)["A"],
        "the unit vector is invariant to overall brightness",
        tolerance=1e-9,
    )
    checks.close(
        preprocessing.snv(ramp(1.0, 1.0))["A"],
        preprocessing.snv(scaled)["A"],
        "and so is SNV",
        tolerance=1e-9,
    )
    checks.ok(
        preprocessing.snv(flat(5.0))["A"] == 0.0,
        "a flat spectrum has no SNV shape, and does not divide by zero",
    )

    # ==================================================================
    checks.section("2. the derivative uses real wavelengths")

    # A perfectly linear ramp IN WAVELENGTH must differentiate to a
    # constant. It only does so if the uneven band spacing is honoured:
    # 410->435 is 25 nm and 760->810 is 50.
    linear = {
        channel: 2.0 * WAVELENGTHS[channel] + 7.0 for channel in CHANNELS
    }

    derivative = spectral_features.first_derivative(linear)

    checks.close(
        derivative["C"], 2.0,
        "a line in wavelength differentiates to its slope in the visible",
        tolerance=1e-6,
    )
    checks.close(
        derivative["T"], 2.0,
        "and to the SAME slope in the NIR, where the bands are 50 nm apart",
        tolerance=1e-6,
    )

    naive_visible = linear["D"] - linear["C"]
    naive_nir = linear["U"] - linear["T"]

    checks.ok(
        abs(naive_visible - naive_nir) > 10.0,
        "a plain x[i+1]-x[i] difference would have reported two different "
        "slopes for one straight line",
    )

    checks.close(
        spectral_features.second_derivative(linear)["E"], 0.0,
        "and the second derivative of a line is zero",
        tolerance=1e-6,
    )

    energy = spectral_features.block_energy(flat(2.0))

    checks.equal(
        energy["uv_die"]["channels"], 6, "energy is reported per detector die"
    )
    checks.equal(energy["total"]["mean"], 2.0, "and over the whole array")

    ratios = spectral_features.cross_illumination({
        "white": flat(10.0), "uv": flat(5.0), "ir": flat(2.0),
    })

    checks.close(
        ratios["uv_over_white"]["ratio"]["values"]["A"], 0.5,
        "UV/WHITE is a per-channel ratio",
        tolerance=1e-5,
    )
    checks.equal(
        ratios["uv_over_white"]["ratio"]["guarded_channels"], [],
        "with nothing guarded when the denominator is healthy",
    )

    zero_denominator = spectral_features.cross_illumination({
        "white": flat(0.0), "uv": flat(5.0), "ir": flat(1.0),
    })

    checks.equal(
        len(zero_denominator["uv_over_white"]["ratio"]["guarded_channels"]),
        18,
        "a zero denominator is guarded on every channel rather than "
        "producing infinity",
    )

    # ==================================================================
    checks.section("3. raw validity and normalized validity are separate")

    # The real case from 2026-08-17: IR at 460 nm. The count is fine and
    # the reflectance is meaningless, and the old pipeline could only say
    # one thing about both.
    entry = channel_reliability.channel_report(
        raw_value=57.9, dark_value=77.9, reference_value=86.4
    )

    checks.ok(entry["raw_valid"], "a 57.9-count reading is a valid COUNT")
    checks.ok(
        not entry["normalized_valid"],
        "but the reflectance from an 8.5-count reference is not usable",
    )
    checks.ok(
        any("counts above dark" in reason for reason in entry["reasons"]),
        "and the reason says so in counts",
    )

    healthy = channel_reliability.channel_report(
        raw_value=500.0, dark_value=2.0, reference_value=2000.0
    )

    checks.ok(healthy["normalized_valid"], "a healthy channel is usable")
    checks.equal(
        healthy["normalized_reliability"], 1.0,
        "and scores full reliability",
    )

    saturated = channel_reliability.channel_report(
        raw_value=70000.0, dark_value=2.0, reference_value=2000.0
    )

    checks.ok(
        not saturated["raw_valid"],
        "a saturated reading is not a measurement at all",
    )

    noisy = channel_reliability.channel_report(
        raw_value=500.0, dark_value=2.0, reference_value=2000.0,
        sample_cv=0.40,
    )

    checks.ok(
        noisy["normalized_reliability"] < healthy["normalized_reliability"],
        "repeats that do not repeat lower reliability",
    )
    checks.ok(
        noisy["raw_valid"],
        "without making the raw count invalid",
    )

    # The gate: no amount of repeatability rescues a two-count reference.
    starved = channel_reliability.channel_report(
        raw_value=3.0, dark_value=0.0, reference_value=2.0, sample_cv=0.0,
        reference_cv=0.0,
    )

    checks.equal(
        starved["normalized_reliability"], 0.0,
        "a starved reference gives zero reliability however well it "
        "repeats - reference range gates, it does not average",
    )

    report = channel_reliability.assess(
        {"white": flat(500.0), "uv": flat(3.0), "ir": flat(500.0)},
        flat(0.0),
        {"white": flat(2000.0), "uv": flat(2.0), "ir": flat(2000.0)},
    )

    checks.equal(report["features_total"], 54, "all 54 features are assessed")
    checks.equal(
        report["raw_valid_total"], 54, "and all 54 raw counts are valid"
    )
    checks.equal(
        report["normalized_valid_total"], 36,
        "while only the 36 with a usable reference support reflectance",
    )
    checks.equal(
        len(channel_reliability.usable_channels(report, "uv")), 0,
        "so a UV comparison has no usable channels at all",
    )
    checks.ok(
        "not hardware" in channel_reliability.summarize(report),
        "and the summary says the difference is normalization",
    )

    # ==================================================================
    checks.section("4. distances keep the magnitude of a win")

    # A winner that stands clear of the pack, and one that does not.
    # Both are "rank 1"; only one of them is evidence.
    decisive_field = {
        "decisive": flat(0.500),
        "runner": flat(0.100),
        "far": flat(0.090),
        "further": flat(0.080),
    }

    photo_finish = {
        "decisive": flat(0.500),
        "runner": flat(0.499),
        "far": flat(0.498),
        "further": flat(0.497),
    }

    wide = distances.compare_library(flat(0.500), decisive_field)
    narrow = distances.compare_library(flat(0.500), photo_finish)

    wide_rmse = wide["metrics"]["rmse"]
    narrow_rmse = narrow["metrics"]["rmse"]

    checks.equal(wide_rmse["winner"], "decisive", "the winner is found")
    checks.equal(narrow_rmse["winner"], "decisive", "in both fields")

    checks.ok(
        wide_rmse["z_separation"] > 10 * narrow_rmse["z_separation"],
        "and standing clear of the field scores an order of magnitude "
        "more separation than a photo finish - which a rank of 1 could "
        "never express",
    )

    # Separation is not the same question as goodness, and both are kept.
    poor_winner = distances.compare_library(flat(0.500), {
        "best_of_a_bad_lot": flat(0.200),
        "worse": flat(0.100),
        "worst": flat(0.090),
    })

    checks.ok(
        poor_winner["metrics"]["rmse"]["z_separation"] > 3,
        "a winner can stand clear of its field",
    )
    checks.ok(
        poor_winner["metrics"]["rmse"]["absolute_goodness"] < 0.6,
        "and still be a poor fit in absolute terms - which is why "
        "absolute goodness is reported beside the margin",
    )
    checks.equal(
        wide_rmse["absolute_goodness"], 1.0,
        "while an exact match scores full absolute goodness",
    )

    checks.equal(
        set(distances.METRICS) - {
            "rmse", "mae", "euclidean", "weighted_euclidean", "cosine",
            "spectral_angle_deg", "pearson_r",
        },
        set(),
        "six metrics plus the weighted variant are reported",
    )

    checks.equal(
        distances.METRICS["pearson_r"]["family"], "centered_shape",
        "Pearson is labelled a shape metric, not an independent third "
        "opinion",
    )
    checks.equal(
        distances.METRICS["cosine"]["family"],
        distances.METRICS["spectral_angle_deg"]["family"],
        "and cosine and spectral angle share one family, being the same "
        "measurement",
    )

    checks.equal(
        len(wide["families"]), 3,
        "so three families vote, not seven metrics",
    )

    weighted = distances.all_metrics(
        flat(0.5), flat(0.4), CHANNELS,
        {channel: (1.0 if channel in ("A", "B") else 0.0)
         for channel in CHANNELS},
    )

    checks.close(
        weighted["weighted_euclidean"], 0.1,
        "weighted distance uses only the channels with weight",
        tolerance=1e-9,
    )

    # ==================================================================
    checks.section("5. class distance refuses what it cannot estimate")

    observations = [
        {channel: 0.50 + 0.01 * index for channel in CHANNELS}
        for index in range(3)
    ]

    statistics = class_distance.build_class_statistics(observations)

    checks.equal(statistics["n_independent"], 3, "three observations")
    checks.ok(statistics["supports_variance"], "support a per-feature spread")
    checks.ok(
        not statistics["supports_covariance"],
        "but not an 18x18 covariance",
    )
    checks.ok(
        "singular" in statistics["covariance_refused"],
        "and the refusal explains that the matrix would be singular",
    )
    checks.ok(
        class_distance.mahalanobis_distance(flat(0.5), statistics) is None,
        "so Mahalanobis returns nothing rather than a confident number",
    )
    checks.ok(
        class_distance.standardized_distance(flat(0.5), statistics)
        is not None,
        "while standardized Euclidean, which needs less, still works",
    )

    many = [
        {
            channel: 0.50 + 0.01 * index + 0.001 * position
            for position, channel in enumerate(CHANNELS)
        }
        for index in range(8)
    ]

    rich = class_distance.build_class_statistics(many)

    checks.ok(
        rich["supports_covariance"],
        "eight independent observations do support a covariance",
    )
    checks.ok(
        rich["covariance"]["shrinkage"] > 0,
        "which is shrunk towards the diagonal rather than used raw",
    )
    checks.ok(
        class_distance.mahalanobis_distance(flat(0.52), rich) is not None,
        "and the shrunk matrix inverts",
    )

    near = class_distance.nearest_centroid_distance(flat(0.51), statistics)
    far = class_distance.nearest_centroid_distance(flat(5.0), statistics)

    checks.ok(far > near, "distance to the centroid grows with distance")

    knn = class_distance.knn_evidence(
        flat(0.51), {"target": observations, "other": [flat(9.0)]}, k=2
    )

    checks.equal(
        knn["nearest_material"], "target",
        "k-nearest-neighbour finds the closest REAL observation",
    )

    # ==================================================================
    checks.section("6. the evidence package concludes nothing")

    package = evidence_module.build(
        "TEST_MEASUREMENT",
        {"white": flat(500.0), "uv": flat(50.0), "ir": flat(400.0)},
        flat(1.0),
        {"white": flat(1000.0), "uv": flat(900.0), "ir": flat(800.0)},
    )

    checks.equal(
        package["schema_version"], evidence_module.EVIDENCE_SCHEMA_VERSION,
        "the package is versioned",
    )

    for section in ("measurement", "acquisition", "raw", "quality",
                    "channel_reliability", "representations",
                    "feature_summary", "reference_analysis",
                    "class_analysis", "warnings"):
        checks.ok(section in package, "it carries the {} section".format(
            section
        ))

    text = repr(package)

    for forbidden in ("best_match", "automatic_conclusion", "KNOWN_MATERIAL",
                      "identified"):
        checks.ok(
            forbidden not in text,
            "and never the word '{}' - that is the decision layer's "
            "vocabulary".format(forbidden),
        )

    # ==================================================================
    checks.section("7. hardware QC and normalization are split")

    combined = {
        "status": "FAIL",
        "checks": [
            {"check": "validity", "status": "PASS", "message": ""},
            {"check": "reflectance", "status": "FAIL",
             "message": "18 channels out of range"},
            {"check": "repeatability", "status": "PASS", "message": ""},
        ],
    }

    split = evidence_module.split_quality(combined)

    checks.equal(
        split["hardware"]["status"], "PASS",
        "a reflectance failure does NOT fail the hardware",
    )
    checks.equal(
        split["normalization"]["status"], "NORMALIZATION_UNUSABLE",
        "it fails the normalization instead",
    )
    checks.ok(
        split["hardware"]["usable"],
        "so the measurement remains usable - this is the change that "
        "recovers the six suppressed measurements of 2026-08-17",
    )

    broken_hardware = {
        "status": "FAIL",
        "checks": [
            {"check": "validity", "status": "FAIL", "message": "no data"},
            {"check": "reflectance", "status": "PASS", "message": ""},
        ],
    }

    split = evidence_module.split_quality(broken_hardware)

    checks.equal(
        split["hardware"]["status"], "HARDWARE_QC_FAIL",
        "while a validity failure genuinely fails the hardware",
    )
    checks.ok(
        not split["hardware"]["usable"], "and that one is not usable"
    )

    return checks.report()


if __name__ == "__main__":
    sys.exit(main_tests())
