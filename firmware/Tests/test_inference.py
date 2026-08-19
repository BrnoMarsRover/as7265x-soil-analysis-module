"""
Feature space, database registry, projection, mixture and inference.

These cover the subsystems added to make DB1/DB2/DB3 work as three
independent sources. The emphasis is on the failures that would be
silent: an 18-band spectrum matched against a 54-feature library by
index, a projected reference passed off as a measurement, a mixture
fitted so freely that it explains anything, a classifier that always
names a winner.

Where possible the tests use KNOWN ANSWERS - a flat spectrum must project
flat, an exact library material must rank first, a 0.7/0.3 mixture must
come back as 0.7/0.3 - rather than merely asserting that nothing crashed.
"""

import math
import random
import sys

import support
from support import Checks

support.add_project_root()
sys.path.insert(0, str(support.REPO / "research"))

from BD.channels import (                        # noqa: E402
    AS7265X_18,
    AS7265X_54_MULTIILLUM,
    CHANNELS,
    WAVELENGTHS,
    FeatureSpaceError,
    feature_count,
    feature_ids,
    project_to_18,
    require_compatible,
    split_feature,
)
from BD.registry import (                        # noqa: E402
    MEASURED,
    REFERENCE_PROJECTED,
    STATUS_EMPTY,
    STATUS_READY,
    DatabaseRegistry,
    validate_materials,
)
from Measurements import inference, mixture      # noqa: E402
from research import spectral_projection as projection  # noqa: E402


def main_tests():
    checks = Checks("Inference and databases")
    registry = DatabaseRegistry()
    db1 = registry["DB1"]

    # ==================================================================
    checks.section("1. feature space is explicit and enforced")

    checks.equal(feature_count(AS7265X_18), 18, "AS7265X_18 has 18 features")
    checks.equal(
        feature_count(AS7265X_54_MULTIILLUM), 54,
        "AS7265X_54_MULTIILLUM has 54 features - 18 bands x 3 illuminations",
    )
    checks.equal(
        feature_ids(AS7265X_54_MULTIILLUM)[0], "white:A",
        "54-feature ids are illumination-qualified",
    )
    checks.equal(
        split_feature("ir:W"), ("ir", "W"), "a feature id splits back"
    )

    # THE guard: 18 and 54 must never be compared by index.
    checks.raises(
        FeatureSpaceError,
        lambda: require_compatible(AS7265X_18, AS7265X_54_MULTIILLUM),
        "comparing 18 against 54 raises rather than lining up the first 18",
    )
    checks.equal(
        require_compatible(AS7265X_18, AS7265X_18), AS7265X_18,
        "matching spaces are accepted",
    )

    # Narrowing 54 -> 18 is real: the measurement contains those bands.
    fifty_four = {fid: 0.5 for fid in feature_ids(AS7265X_54_MULTIILLUM)}

    for index, channel in enumerate(CHANNELS):
        fifty_four["white:" + channel] = 0.1 * index

    narrowed = project_to_18(fifty_four, AS7265X_54_MULTIILLUM)

    checks.equal(len(narrowed), 18, "54 narrows to its 18 WHITE bands")
    checks.close(
        narrowed["C"], 0.2, "and keeps the WHITE values, not UV or IR"
    )

    # ==================================================================
    checks.section("2. three databases, independent and labelled")

    checks.equal(
        sorted(registry.databases), ["DB1", "DB2", "DB3"],
        "exactly three databases are registered",
    )
    checks.equal(db1.status, STATUS_READY, "DB1 is READY")
    checks.equal(db1.count(), 23, "DB1 holds the 23 measured materials")
    checks.equal(db1.evidence, MEASURED, "DB1 is MEASURED evidence")
    checks.equal(
        db1.feature_space, AS7265X_18, "DB1 lives in the 18-band space"
    )

    checks.equal(
        registry["DB2"].feature_space, AS7265X_54_MULTIILLUM,
        "DB2 is declared in the 54-feature space",
    )
    checks.equal(
        registry["DB2"].status, STATUS_EMPTY,
        "DB2 is EMPTY - declared but not measured yet",
    )
    checks.equal(
        registry["DB3"].evidence, REFERENCE_PROJECTED,
        "DB3 is REFERENCE_PROJECTED, never MEASURED",
    )
    checks.ok(
        registry["DB2"].problems,
        "an empty database explains why it is empty",
    )

    # DB3 was populated from the USGS Spectral Library. It is REFERENCE
    # evidence, never MEASURED, and every record must carry provenance -
    # the validator refuses the database otherwise.
    checks.equal(
        registry["DB3"].status, STATUS_READY,
        "DB3 is populated from external spectra",
    )
    checks.ok(
        registry["DB3"].count() >= 50,
        "with a useful number of materials ({})".format(
            registry["DB3"].count()
        ),
    )
    checks.ok(
        all(
            (registry["DB3"].metadata[name] or {}).get("measurement_type")
            == REFERENCE_PROJECTED
            for name in registry["DB3"].materials
        ),
        "and every record is labelled REFERENCE_PROJECTED, never MEASURED",
    )

    # An 18-band sample must not reach the 54-feature library at all.
    compatible = registry.compatible_with(AS7265X_18)
    checks.ok(
        "DB2" not in compatible,
        "an 18-band measurement is not offered DB2",
    )

    compatible54 = registry.compatible_with(AS7265X_54_MULTIILLUM)
    checks.equal(
        compatible54.get("DB1", (None,))[0], "PROJECTED_TO_18",
        "a 54-feature measurement reaches DB1 by explicit narrowing",
    )

    # ==================================================================
    checks.section("3. database validation rejects bad documents")

    checks.ok(
        validate_materials({}, AS7265X_18, MEASURED),
        "a document with no materials is rejected",
    )
    checks.ok(
        validate_materials(
            {"materials": {"X": {"measurement_type": "MEASURED",
                                 "channels": {}}}},
            AS7265X_18, REFERENCE_PROJECTED,
        ),
        "a MEASURED record inside a reference database is rejected",
    )
    checks.ok(
        validate_materials(
            {"materials": {"X": {
                "measurement_type": "REFERENCE_PROJECTED",
                "channels": {c: {"reflectance_as_supplied": 0.5}
                             for c in CHANNELS}}}},
            AS7265X_18, REFERENCE_PROJECTED,
        ),
        "a reference record with no provenance is rejected",
    )
    checks.equal(
        validate_materials(
            {"materials": {"X": {
                "measurement_type": "REFERENCE_PROJECTED",
                "provenance": {"source_dataset": "USGS"},
                "channels": {c: {"reflectance_as_supplied": 0.5}
                             for c in CHANNELS}}}},
            AS7265X_18, REFERENCE_PROJECTED,
        ),
        [],
        "a properly sourced reference record passes",
    )

    # ==================================================================
    checks.section("4. projection: known answers, no extrapolation")

    wavelengths = [float(nm) for nm in range(350, 1051)]

    # A flat spectrum must project flat. This is what proves the
    # normalisation denominator is right; a missing divisor would scale
    # every band by the response integral instead.
    bands, report = projection.project(wavelengths, [0.42] * len(wavelengths))

    checks.equal(len(bands), 18, "a fully covering source gives 18 bands")
    checks.equal(report["coverage_status"], "FULL_18CH", "reported as full")
    checks.ok(
        max(abs(value - 0.42) for value in bands.values()) < 1e-9,
        "a flat spectrum projects flat to machine precision",
    )

    # On a linear ramp a symmetric response returns the centre value.
    ramp = [0.001 * nm for nm in wavelengths]
    bands, _ = projection.project(wavelengths, ramp)

    checks.ok(
        max(
            abs(bands[c] - 0.001 * WAVELENGTHS[c]) for c in CHANNELS
        ) < 1e-4,
        "a linear ramp projects to the value at each band centre",
    )

    # Coverage: a 500-700 nm source cannot produce a 410 nm band.
    narrow = [float(nm) for nm in range(500, 701)]
    bands, report = projection.project(narrow, [0.5] * len(narrow))

    checks.equal(
        report["coverage_status"], "PARTIAL", "partial coverage is reported"
    )
    checks.ok("A" in report["uncovered_bands"], "410 nm is not invented")
    checks.ok("A" not in bands, "and is absent from the result entirely")
    checks.ok(
        report["approximate"] is True,
        "the projection is flagged as an approximation",
    )
    checks.equal(
        report["projection_method"], "GAUSSIAN_APPROXIMATION",
        "and names the model used, so it can be replaced later",
    )

    checks.raises(
        projection.ProjectionError,
        lambda: projection.project([500.0, 400.0], [0.1, 0.2]),
        "a non-monotonic source spectrum is rejected",
    )
    checks.raises(
        projection.ProjectionError,
        lambda: projection.build_record(
            "X", wavelengths, [0.4] * len(wavelengths), {"license": "cc0"}
        ),
        "a DB3 record without source provenance is refused",
    )

    record = projection.build_record(
        "Example", wavelengths, [0.4] * len(wavelengths),
        {"source_dataset": "USGS Spectral Library Version 7",
         "source_record_id": "splib07a_Example", "license": "public domain"},
    )
    checks.equal(
        record["measurement_type"], "REFERENCE_PROJECTED",
        "a projected record can never claim to be MEASURED",
    )

    # ==================================================================
    checks.section("5. mixture: known answer, and refusal to overfit")

    first = {c: 0.2 + 0.03 * i for i, c in enumerate(CHANNELS)}
    second = {c: 0.9 - 0.04 * i for i, c in enumerate(CHANNELS)}
    library = {"First": first, "Second": second,
               "Flat": {c: 0.5 for c in CHANNELS}}

    blend = {c: 0.7 * first[c] + 0.3 * second[c] for c in CHANNELS}
    result = mixture.estimate(blend, library, list(library))

    checks.equal(
        result["status"], "MIXTURE_PLAUSIBLE", "a real blend is detected"
    )

    weights = {
        component["material"]: component["normalized_contribution"]
        for component in result["components"]
    }
    checks.close(
        weights.get("First", 0), 0.7, "the 0.7 component is recovered",
        tolerance=0.02,
    )
    checks.close(
        weights.get("Second", 0), 0.3, "the 0.3 component is recovered",
        tolerance=0.02,
    )
    checks.ok(
        all(
            component["spectral_contribution"] >= 0
            for component in result["components"]
        ),
        "contributions are non-negative",
    )
    checks.ok(
        "not mass fraction" in result["caveat"],
        "the result states that contribution is not concentration",
    )

    # A pure material must NOT be dressed up as a mixture.
    pure = mixture.estimate(first, library, list(library))
    checks.equal(
        pure["status"], "SINGLE_COMPONENT",
        "a single material is reported as one component, not a blend",
    )

    # Overfitting guard: near-identical endmembers must be screened out.
    twins = {
        "First": first,
        "FirstAgain": {c: v * 1.0001 for c, v in first.items()},
        "Second": second,
    }
    selected, _vectors = mixture.select_endmembers(
        list(twins), twins, list(CHANNELS)
    )
    checks.ok(
        "FirstAgain" not in selected,
        "a near-collinear duplicate endmember is screened out",
    )

    # ==================================================================
    checks.section("6. inference: sanity against known ground truth")

    target = "Sulfur Powder"
    exact = inference.infer(
        dict(db1.materials[target]), AS7265X_18, registry
    )

    checks.equal(
        exact["consensus"]["material"], target,
        "an exact library spectrum identifies itself",
    )

    random.seed(4)
    noisy = {
        c: db1.materials[target][c] * (1 + random.uniform(-0.02, 0.02))
        for c in CHANNELS
    }
    perturbed = inference.infer(noisy, AS7265X_18, registry)

    checks.equal(
        perturbed["consensus"]["material"], target,
        "and survives 2% noise",
    )

    # ==================================================================
    checks.section(
        "6b. each database sees the normalization it is entitled to"
    )

    # The databases are not normalized alike, and no single vector serves
    # all three. DB1 may only ever be compared against the frozen legacy
    # White/Dark it was built with; DB3 was never measured on this
    # instrument, so the honest comparison is against the current
    # calibration. Passing one vector for both would silently make one of
    # them wrong, which is exactly the mistake feature_sources prevents.

    legacy_view = dict(db1.materials[target])
    current_view = {
        channel: value * 1.35 for channel, value in legacy_view.items()
    }

    split = inference.infer(
        current_view, AS7265X_18, registry,
        feature_sources={
            "DB1": {
                "features": legacy_view,
                "feature_space": AS7265X_18,
                "normalization": "legacy:TEST_LEGACY_CAL",
            },
            "DB3": {
                "features": current_view,
                "feature_space": AS7265X_18,
                "normalization": "active:TEST_ACTIVE_CAL",
            },
        },
    )

    by_key = {entry["database"]: entry for entry in split["database_results"]}

    checks.equal(
        by_key["DB1"]["normalization"], "legacy:TEST_LEGACY_CAL",
        "DB1 records which calibration it was compared under",
    )
    checks.equal(
        by_key["DB3"]["normalization"], "active:TEST_ACTIVE_CAL",
        "and DB3 records its own, which is a different one",
    )
    checks.equal(
        by_key["DB1"]["best_material"], target,
        "DB1 still identifies its own spectrum exactly - it was given "
        "the legacy vector, not the rescaled one",
    )

    # The vectors really are different: RMSE is magnitude-sensitive, so
    # a 35% rescale changes it. If feature_sources were ignored, DB1
    # would have been scored against the rescaled vector instead.
    pooled = inference.infer(current_view, AS7265X_18, registry)
    pooled_db1 = {
        entry["database"]: entry for entry in pooled["database_results"]
    }["DB1"]

    checks.ok(
        pooled_db1["best_rmse"] > by_key["DB1"]["best_rmse"],
        "and scoring DB1 against the wrong normalization is measurably "
        "worse - the split is not cosmetic",
    )
    checks.ok(
        pooled_db1["normalization"] is None,
        "a caller that names no sources gets no normalization claim",
    )

    # Out of distribution: the system must be allowed to say UNKNOWN.
    strange = {c: random.uniform(0.0, 0.02) for c in CHANNELS}
    strange["A"] = 3.0
    unknown = inference.infer(strange, AS7265X_18, registry)

    checks.equal(
        unknown["consensus"]["level"], inference.LEVEL_UNKNOWN,
        "a sample resembling nothing is reported UNKNOWN, not crowned",
    )
    checks.ok(
        unknown["consensus"]["material"] is None,
        "with no material named",
    )
    checks.ok(
        unknown["consensus"].get("nearest_material"),
        "though the nearest entry is still reported for the operator",
    )

    # ==================================================================
    checks.section("7. every database appears in the result")

    keys = [entry["database"] for entry in exact["database_results"]]

    checks.equal(
        sorted(keys), ["DB1", "DB2", "DB3"],
        "all three databases are reported, including the empty ones",
    )

    by_key = {entry["database"]: entry for entry in exact["database_results"]}

    checks.equal(by_key["DB1"]["status"], "OK", "DB1 produced a result")
    checks.equal(
        by_key["DB2"]["status"], STATUS_EMPTY,
        "DB2 is reported as EMPTY rather than omitted",
    )
    checks.ok(
        by_key["DB2"]["matches"] == [],
        "and contributes no matches",
    )
    checks.ok(
        all(
            entry.get("version") for entry in exact["database_results"]
        ),
        "each database result carries its version, for traceability",
    )
    checks.ok(
        exact["database_versions"] and exact["analysis_version"],
        "the result records the analysis and database versions used",
    )

    # ==================================================================
    checks.section("8. confidence is not similarity")

    checks.ok(
        "similarity" not in exact["confidence"]["basis"].split()[0],
        "confidence declares an evidence basis, not a score",
    )
    checks.ok(
        "not a calibrated probability"
        in exact["confidence"]["note"].lower(),
        "and refuses to be read as a probability",
    )

    factors = {
        factor["factor"] for factor in exact["confidence"]["factors"]
    }
    for expected in ("measurement_quality", "match_quality", "separation",
                     "metric_agreement", "database_availability"):
        checks.ok(
            expected in factors,
            "confidence accounts for {}".format(expected),
        )

    # A single available database must not yield full confidence.
    checks.ok(
        exact["confidence"]["level"] != inference.CONFIDENCE_HIGH,
        "one database alone cannot produce HIGH confidence - there is "
        "nothing to corroborate it",
    )

    # A failed measurement must collapse confidence entirely.
    failed = inference.infer(
        dict(db1.materials[target]), AS7265X_18, registry,
        quality={"status": "FAIL", "failures": ["synthetic"]},
    )
    checks.equal(
        failed["confidence"]["level"], inference.CONFIDENCE_NONE,
        "a quality FAIL drives confidence to NONE",
    )
    checks.equal(
        failed["consensus"]["level"], inference.LEVEL_UNKNOWN,
        "and no material is reported from a failed measurement",
    )

    # ==================================================================
    checks.section("9. measured class discriminability gates the vote")

    # DB3 names a nearest material for every sample, including for classes
    # the sensor cannot separate. research/analyse_discriminability.py
    # measures that per class; the confidence model must consume it rather
    # than treat every DB3 answer as corroboration.
    db3 = registry["DB3"]
    block = db3.discriminability

    checks.ok(block, "DB3 carries a measured discriminability block")
    checks.ok(
        block.get("analysis_version"),
        "which records the analysis version that produced it",
    )
    checks.ok(
        "PROVISIONAL" in block.get("threshold_status", ""),
        "and labels its thresholds provisional rather than validated",
    )

    by_class = block.get("by_class") or {}
    checks.ok(len(by_class) >= 10, "with a rating per material class")

    # sulfate is the class that produced the wrong answers - chalk, talc
    # and saltpetre all came back as anhydrite - so it must rate poorly.
    sulfate = by_class.get("sulfate") or {}
    checks.equal(
        sulfate.get("level"), "WEAK",
        "sulfate is measured WEAK - it is named often and is usually wrong",
    )
    checks.ok(
        sulfate.get("precision", 1.0) < 0.5,
        "its precision is below half ({})".format(sulfate.get("precision")),
    )

    # A class measured and found poor loses its vote.
    chalk = inference.infer(
        dict(db1.materials["Calcium Carbonate (Chalk)"]), AS7265X_18,
        registry,
    )
    chalk_db3 = next(
        entry for entry in chalk["database_results"]
        if entry["database"] == "DB3"
    )

    checks.equal(
        chalk_db3["class_reliability"], "WEAK",
        "DB3's answer for chalk is flagged with its measured reliability",
    )
    checks.ok(
        "DB3" not in (chalk["consensus"]["databases_voting"] or []),
        "and DB3 does not vote on the material",
    )
    checks.ok(
        chalk["consensus"]["discounted_for_low_reliability"],
        "the discount is recorded, not silent",
    )
    checks.ok(
        chalk_db3["best_material"] is not None,
        "while DB3's answer is still reported in full for the operator",
    )
    checks.equal(
        chalk["confidence"]["level"], inference.CONFIDENCE_LOW,
        "losing a source to poor discriminability lowers confidence",
    )

    # "Measured and poor" and "not measured" are different, and must not
    # be conflated - otherwise DB1, which carries no analysis at all,
    # would be silenced too.
    checks.ok(
        inference.RELIABILITY_WEAK in inference.NOT_CORROBORATING,
        "a class measured WEAK loses its vote",
    )
    checks.ok(
        inference.RELIABILITY_UNRATED not in inference.NOT_CORROBORATING,
        "an unmeasured class keeps its vote - unknown is not poor",
    )
    checks.ok(
        inference.RELIABILITY_INSUFFICIENT
        not in inference.NOT_CORROBORATING,
        "and neither is a class with too little data to rate",
    )

    db1_result = next(
        entry for entry in chalk["database_results"]
        if entry["database"] == "DB1"
    )
    checks.equal(
        db1_result["class_reliability"], inference.RELIABILITY_UNRATED,
        "DB1 has no discriminability analysis, so it is UNRATED",
    )
    checks.ok(
        "DB1" in (chalk["consensus"]["databases_voting"] or []),
        "and still votes",
    )

    factors = {
        factor["factor"]: factor["verdict"]
        for factor in chalk["confidence"]["factors"]
    }
    checks.equal(
        factors.get("class_discriminability"), "FAIL",
        "confidence records why the database was discounted",
    )

    return checks.report()


if __name__ == "__main__":
    sys.exit(main_tests())
