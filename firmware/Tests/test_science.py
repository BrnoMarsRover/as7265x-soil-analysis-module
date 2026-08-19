"""
BD science layer tests.

Pure data processing, so there is no hardware to stub. The protected
files are opened read-only and their SHA256 is checked before and after
the whole run: a test that quietly rewrites DB1.json or
calibration_legacy.json would invalidate every measurement ever taken against
them.
"""

import hashlib
import json
import math
import shutil
import sys
import tempfile
from pathlib import Path

import support
from support import Checks

support.add_project_root()

from BD import config as bd_config
from Measurements import config                       # noqa: E402
from Measurements import metrics                      # noqa: E402
from Measurements import quality                      # noqa: E402
from Measurements import analysis as sample_analysis              # noqa: E402
from BD.databases import (              # noqa: E402
    DatabaseError,
    MaterialDatabase,
    References,
)

# ARCHITECTURE.md moved every comparison metric out of the database module. BD
# loads spectra; Measurements compares them. These tests therefore
# exercise the authoritative implementation rather than the copy that used
# to live beside the loader.
from Measurements.metrics import cosine_similarity_percent  # noqa: E402

CHANNELS = sample_analysis.CHANNELS


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def flat(value):
    return {channel: value for channel in CHANNELS}


def main_tests():
    checks = Checks("BD science")

    def archive_state():
        """
        The measured sample archive is untracked AND gitignored, so git
        cannot restore it. It is the only copy of real competition data,
        which makes it the most fragile file in the repository - guard it
        exactly like the protected reference data.
        """
        path = bd_config.SAMPLES_FILE

        if not path.exists():
            return "ABSENT"

        return "{}:{}".format(path.stat().st_size, sha256(path))

    before = {
        "database.json": sha256(bd_config.DATABASE_FILE),
        "references.json": sha256(bd_config.REFERENCES_FILE),
        "samples.json": archive_state(),
    }

    # ==================================================================
    checks.section("1. channel validation")

    checks.equal(len(CHANNELS), 18, "exactly 18 channels are defined")
    checks.equal(
        list(CHANNELS),
        list("ABCDEFGHIJKL") + list("RSTUVW"),
        "channels are the AS7265x letters in wavelength order",
    )
    checks.equal(
        sample_analysis.WAVELENGTHS["A"], 410, "channel A is 410 nm"
    )
    checks.equal(
        sample_analysis.WAVELENGTHS["W"], 940, "channel W is 940 nm"
    )

    checks.equal(
        sample_analysis.validate_spectrum(flat(1.0)),
        [],
        "a complete spectrum reports nothing missing",
    )

    partial = flat(1.0)
    del partial["K"]
    partial["R"] = None
    partial["S"] = "12.0"
    partial["T"] = True

    missing = sample_analysis.validate_spectrum(partial)

    checks.equal(
        sorted(missing), ["K", "R", "S", "T"],
        "absent, null, string and boolean channels are all rejected",
    )
    checks.equal(
        sample_analysis.validate_spectrum(None),
        list(CHANNELS),
        "a non-mapping is entirely missing",
    )

    checks.raises(
        sample_analysis.AnalysisError,
        lambda: sample_analysis.require_spectrum(partial),
        "require_spectrum raises on an incomplete spectrum",
    )

    # ==================================================================
    checks.section("2. dark correction: C = S - D")

    sample = dict(flat(10.0))
    dark = dict(flat(2.0))

    corrected = sample_analysis.dark_correct(sample, dark)

    checks.equal(len(corrected), 18, "all 18 channels are corrected")
    checks.close(corrected["A"], 8.0, "10 - 2 = 8")

    sample["B"] = 1.0
    dark["B"] = 3.5

    corrected = sample_analysis.dark_correct(sample, dark)

    checks.close(
        corrected["B"], -2.5,
        "a reading below dark stays negative, not clamped to zero",
    )

    # ==================================================================
    checks.section("3. normalization: R = (S - D) / (W - D)")

    sample = flat(60.0)
    dark = flat(10.0)
    white = flat(110.0)

    normalized = sample_analysis.normalize(sample, dark, white)

    checks.close(
        normalized["A"], 0.5, "(60-10)/(110-10) = 0.5"
    )
    checks.ok(
        all(abs(value - 0.5) < 1e-9 for value in normalized.values()),
        "every channel normalizes identically for a flat spectrum",
    )

    sample = dict(flat(60.0))
    dark = dict(flat(10.0))
    white = dict(flat(110.0))

    white["C"] = 10.0  # White == Dark

    normalized = sample_analysis.normalize(sample, dark, white)

    checks.close(
        normalized["C"], 0.0,
        "a zero denominator yields 0.0 instead of raising",
    )
    checks.close(
        normalized["D"], 0.5, "the other channels are unaffected"
    )

    sample["E"] = 5.0

    normalized = sample_analysis.normalize(sample, dark, white)

    checks.close(
        normalized["E"], -0.05,
        "reflectance below the dark reference stays negative",
    )

    # ==================================================================
    checks.section("4. cosine similarity")

    vector = {
        channel: float(index + 1)
        for index, channel in enumerate(CHANNELS)
    }

    checks.close(
        cosine_similarity_percent(vector, vector), 100.0,
        "a spectrum is 100% similar to itself",
        tolerance=1e-6,
    )
    checks.close(
        cosine_similarity_percent(
            vector, {c: v * 7.5 for c, v in vector.items()}
        ),
        100.0,
        "similarity ignores overall scale",
        tolerance=1e-6,
    )
    checks.close(
        cosine_similarity_percent(flat(0.0), vector), 0.0,
        "an all-zero spectrum scores zero rather than dividing by zero",
    )

    first = flat(0.0)
    second = flat(0.0)
    first["A"] = 1.0
    second["B"] = 1.0

    checks.close(
        cosine_similarity_percent(first, second), 0.0,
        "orthogonal spectra score zero",
    )

    angled = flat(0.0)
    angled["A"] = 1.0
    angled["B"] = 1.0

    checks.close(
        cosine_similarity_percent(first, angled),
        100.0 / math.sqrt(2.0),
        "a 45 degree angle scores 70.7%",
        tolerance=1e-6,
    )

    # ==================================================================
    checks.section("5. the real protected data")

    references = References()
    database = MaterialDatabase()

    checks.equal(
        references.white_missing, [], "the fixed White has all 18 channels"
    )
    checks.equal(
        references.dark_missing, [], "the fixed Dark has all 18 channels"
    )
    checks.equal(
        references.calibration_id,
        "FREYA_COMPETITION_2026_CAL_V1",
        "the calibration ID is recorded",
    )
    checks.equal(
        references.zero_denominator_channels(),
        [],
        "no channel has White == Dark",
    )

    checks.equal(database.count(), 23, "23 reference materials are loaded")
    checks.equal(
        database.incomplete_materials(), {},
        "every material has a complete 18-channel spectrum",
    )

    # ==================================================================
    checks.section("6. comparison covers the whole library")

    normalized = sample_analysis.normalize(
        {c: references.white[c] * 0.4 for c in CHANNELS},
        references.dark,
        references.white,
    )

    matches = metrics.compare_all(normalized, database.materials)

    checks.equal(
        len(matches), database.count(),
        "every material is compared, not a top-N slice",
    )
    checks.equal(matches[0]["rank"], 1, "matches are ranked from 1")
    checks.ok(
        all(
            matches[i]["rank_score"] <= matches[i + 1]["rank_score"]
            for i in range(len(matches) - 1)
        ),
        "matches are sorted by combined family rank, best first",
    )
    checks.ok(
        all(
            0.0 <= match["similarity_percent"] <= 100.0
            for match in matches
        ),
        "every score is a percentage",
    )

    # A spectrum copied straight out of the database must find itself.
    target = database.names()[0]
    self_matches = metrics.compare_all(
        database.materials[target], database.materials
    )

    checks.equal(
        self_matches[0]["material"], target,
        "a database spectrum matches its own entry first",
    )
    checks.close(
        self_matches[0]["similarity_percent"], 100.0,
        "and scores 100%",
        tolerance=0.01,
    )

    checks.section("7. three metrics, because cosine alone lies")

    # The heart of the problem. Two spectra with identical shape at
    # completely different brightness: cosine calls them a perfect
    # match, RMSE and the ranking do not.
    dim = flat(0.2)
    bright = flat(1.0)

    checks.close(
        metrics.cosine_similarity_percent(dim, bright), 100.0,
        "cosine scores a dim and a bright spectrum as identical",
        tolerance=1e-6,
    )
    checks.close(
        metrics.rmse(dim, bright), 0.8,
        "but RMSE sees the whole 0.8 difference",
        tolerance=1e-9,
    )
    checks.ok(
        metrics.pearson_r(dim, bright) is None,
        "and Pearson is undefined for two constant spectra",
    )

    checks.close(
        metrics.rmse(dim, dict(dim)), 0.0,
        "RMSE of a spectrum against itself is zero",
    )

    ramp = {c: float(i + 1) for i, c in enumerate(CHANNELS)}
    doubled = {c: value * 2.0 for c, value in ramp.items()}
    inverted = {c: -value for c, value in ramp.items()}

    checks.close(
        metrics.pearson_r(ramp, doubled), 1.0,
        "Pearson is +1 for a perfectly scaled spectrum",
        tolerance=1e-9,
    )
    checks.close(
        metrics.pearson_r(ramp, inverted), -1.0,
        "and -1 for an inverted one",
        tolerance=1e-9,
    )
    checks.ok(
        metrics.pearson_r(ramp, flat(3.0)) is None,
        "a zero-variance reference gives no correlation, not a crash",
    )
    checks.ok(
        metrics.cosine_similarity_percent({}, ramp) is None,
        "an empty spectrum has no cosine",
    )

    # Ranking across a small library.
    library = {
        "exact": dict(ramp),
        "scaled": doubled,
        "offset": {c: value + 5.0 for c, value in ramp.items()},
    }

    ranked = metrics.compare_all(ramp, library)

    checks.equal(len(ranked), 3, "every material is ranked")
    checks.equal(ranked[0]["material"], "exact", "the exact match wins")
    checks.equal(ranked[0]["combined_rank"], 1, "at combined rank 1")
    checks.equal(ranked[0]["rmse"], 0.0, "with zero RMSE")
    checks.equal(ranked[0]["rmse_rank"], 1, "and first on RMSE")

    for entry in ranked:
        for key in ("cosine_rank", "rmse_rank", "pearson_rank",
                    "combined_rank", "rank_score"):
            checks.ok(
                entry.get(key) is not None,
                "{} keeps its {}".format(entry["material"], key),
            )

    checks.ok(
        all(
            ranked[i]["rank_score"] <= ranked[i + 1]["rank_score"]
            for i in range(len(ranked) - 1)
        ),
        "the combined ordering is by rank score",
    )

    agreement = metrics.metric_agreement(ranked)

    checks.ok(agreement["agree"], "all three metrics agree on the exact match")
    checks.equal(
        agreement["rmse_best"], "exact", "RMSE names the winner"
    )

    # Deliberate disagreement: shape says one thing, magnitude another.
    # A ramp scaled up has the SAME shape at a different brightness; a
    # flat spectrum near the same level has the right magnitude and the
    # wrong shape. Cosine and RMSE must pick different winners.
    measured = {c: value * 0.1 for c, value in ramp.items()}

    disagreeing = metrics.compare_all(measured, {
        "same_shape_brighter": ramp,
        "right_level_flat": flat(
            sum(measured.values()) / float(len(measured))
        ),
    })
    disagreement = metrics.metric_agreement(disagreeing)

    checks.equal(
        disagreement["cosine_best"], "same_shape_brighter",
        "cosine prefers the identically shaped spectrum",
    )
    checks.equal(
        disagreement["rmse_best"], "right_level_flat",
        "RMSE prefers the one at a similar level",
    )
    checks.equal(
        disagreement["metrics_favouring_best"], 2,
        "and the split is counted rather than hidden",
    )

    # ==================================================================
    checks.section("8. interpretation")

    empty = sample_analysis.interpret([])

    checks.equal(
        empty["status"], sample_analysis.STATUS_NO_DATABASE,
        "an empty match list is NO_DATABASE",
    )
    checks.ok(
        empty["best_match"] is None, "with no invented best match"
    )

    def entry(name, cosine, rmse_value, pearson, ranks, rank_score):
        return {
            "material": name,
            "cosine_similarity_percent": cosine,
            "similarity_percent": cosine,
            "rmse": rmse_value,
            "pearson_r": pearson,
            # Family rank keys are authoritative after ARCHITECTURE.md; the
            # cosine_/rmse_/pearson_ names are kept as aliases.
            "angular_rank": ranks[0],
            "magnitude_rank": ranks[1],
            "centered_shape_rank": ranks[2],
            "cosine_rank": ranks[0],
            "rmse_rank": ranks[1],
            "pearson_rank": ranks[2],
            "rank_score": rank_score,
            "combined_rank": 0,
        }

    strong_matches = [
        entry("Kaolin", 99.0, 0.02, 0.98, (1, 1, 1), 1.0),
        entry("Talc", 90.0, 0.30, 0.70, (2, 2, 2), 2.0),
    ]

    strong = sample_analysis.interpret(
        strong_matches, metrics.metric_agreement(strong_matches)
    )

    checks.equal(
        strong["status"], sample_analysis.STATUS_STRONG,
        "a clear leader on all three metrics is a match",
    )
    checks.equal(strong["best_match"], "Kaolin", "best match named")
    checks.equal(strong["second_match"], "Talc", "second match named")
    checks.close(strong["score_difference"], 9.0, "the cosine gap is reported")
    checks.close(strong["best_rmse"], 0.02, "and the winner's RMSE")
    checks.close(strong["best_pearson_r"], 0.98, "and its correlation")

    ambiguous_matches = [
        entry("Kaolin", 99.0, 0.02, 0.98, (1, 1, 1), 1.0),
        entry("Talc", 98.5, 0.021, 0.97, (2, 2, 2), 1.2),
    ]

    ambiguous = sample_analysis.interpret(
        ambiguous_matches, metrics.metric_agreement(ambiguous_matches)
    )

    checks.equal(
        ambiguous["status"], sample_analysis.STATUS_AMBIGUOUS,
        "a runner-up this close is ambiguous",
    )

    weak_matches = [
        entry("Kaolin", 40.0, 0.90, 0.10, (1, 1, 1), 1.0),
        entry("Talc", 10.0, 1.50, 0.05, (2, 2, 2), 2.0),
    ]

    weak = sample_analysis.interpret(
        weak_matches, metrics.metric_agreement(weak_matches)
    )

    checks.equal(
        weak["status"], sample_analysis.STATUS_WEAK,
        "a low best score is a poor match",
    )

    disagree_matches = [
        entry("Kaolin", 99.0, 0.90, 0.20, (1, 3, 3), 2.33),
        entry("Talc", 80.0, 0.05, 0.99, (3, 1, 1), 1.67),
    ]

    disagree = sample_analysis.interpret(
        disagree_matches, metrics.metric_agreement(disagree_matches)
    )

    checks.equal(
        disagree["status"], sample_analysis.STATUS_METRICS_DISAGREE,
        "metrics pointing at different materials is reported, not hidden",
    )

    # Quality gates the interpretation, before any classification.
    gated = sample_analysis.interpret(
        strong_matches,
        metrics.metric_agreement(strong_matches),
        {"status": "FAIL", "failures": ["reflectance far out of range"]},
    )

    checks.equal(
        gated["status"], sample_analysis.STATUS_QUALITY_FAIL,
        "a failed measurement is never given a confident identification",
    )
    checks.ok(
        gated["best_match"] is None,
        "and no best match is reported at all",
    )

    warned = sample_analysis.interpret(
        strong_matches,
        metrics.metric_agreement(strong_matches),
        {"status": "WARNING", "warnings": ["2 channels out of range"]},
    )

    checks.equal(
        warned["status"], sample_analysis.STATUS_QUALITY_WARNING,
        "a warning downgrades a strong match rather than being dropped",
    )

    for result in (strong, ambiguous, weak, disagree):
        conclusion = result["automatic_conclusion"].lower()

        checks.ok(
            "% probability" not in conclusion
            and "probability of" not in conclusion,
            "the conclusion claims no probability",
        )
        checks.ok(
            "definitely" not in conclusion
            and "% of" not in conclusion
            and "consists of" not in conclusion
            and "the sample contains" not in conclusion,
            "the conclusion claims no composition or certainty",
        )
        checks.ok(
            "not trained to estimate mixture composition" in conclusion,
            "and states the mixture limitation explicitly",
        )

    # ==================================================================
    checks.section("8b. the whole pipeline")

    raw_white = {c: references.white[c] * 0.35 + references.dark[c]
                 for c in CHANNELS}
    settings = {"integration_cycles": 100, "gain": 2, "gain_x": "16x",
                "measurement_mode": 3}

    acquisition = {
        "protocol_version": 2,
        "repeats": 3,
        "illuminations": {
            "white": {"illumination": "white",
                      "acquisitions": [raw_white, raw_white, raw_white]},
            "uv": {"illumination": "uv",
                   "acquisitions": [flat(9.0), flat(9.1)]},
            "ir": {"illumination": "ir",
                   "acquisitions": [flat(12.0), flat(12.2)]},
        },
    }

    result = sample_analysis.analyze(
        acquisition, references, database, settings
    )

    measurement = result["measurement"]

    checks.equal(result["analysis_status"], "OK", "analysis succeeds")
    checks.equal(
        sorted(measurement["raw"].keys()), ["ir", "uv", "white"],
        "all three illuminations are stored - 54 features",
    )

    for name in ("white", "uv", "ir"):
        checks.equal(
            len(measurement["raw"][name]), 18,
            "{} raw has 18 channels".format(name),
        )

    checks.equal(
        len(measurement["legacy_database_normalized"]["white"]), 18,
        "the legacy normalization covers all 18 channels",
    )
    checks.close(
        measurement["legacy_database_normalized"]["white"]["A"], 0.35,
        "and reproduces the expected reflectance",
        tolerance=1e-4,
    )
    checks.equal(
        measurement["normalized"], measurement[
            "legacy_database_normalized"]["white"],
        "the historical key still points at the legacy normalization",
    )
    checks.equal(
        measurement["active_normalized"], {},
        "with no active calibration there is no active normalization",
    )
    checks.equal(
        result["calibration"]["legacy_database_calibration_id"],
        "FREYA_COMPETITION_2026_CAL_V1",
        "the record names the calibration the comparison was valid under",
    )
    checks.ok(
        result["calibration"]["active_calibration_id"] is None,
        "and honestly reports that no active calibration was used",
    )
    checks.equal(
        len(result["reference_matches"]), 23,
        "all 23 materials are compared",
    )
    checks.ok(
        result["reference_matches"][0]["rmse"] is not None,
        "with RMSE alongside cosine",
    )
    checks.equal(
        result["quality"]["status"], "PASS", "quality passes on clean data"
    )
    checks.equal(
        len(measurement["statistics"]["white"]["channels"]), 18,
        "per-channel statistics are kept for every channel",
    )

    checks.raises(
        sample_analysis.AnalysisError,
        lambda: sample_analysis.analyze(
            {"raw": {"A": 1.0}}, references, database, None
        ),
        "an incomplete spectrum is refused before any analysis",
    )

    # An archived record from before this release still analyses.
    legacy_result = sample_analysis.analyze(
        {"raw": raw_white}, references, database, settings
    )

    checks.equal(
        legacy_result["analysis_status"], "OK",
        "a bare 18-channel white spectrum still analyses",
    )
    checks.equal(
        len(legacy_result["measurement"]["raw"]["white"]), 18,
        "and is stored under the white illumination",
    )

    # A broken database must not cost us the spectra.
    class BrokenDatabase:
        path = "broken"
        materials = {"x": flat(0.5)}

        def count(self):
            return 0

        def compare(self, _normalized):
            raise RuntimeError("database exploded")

    survived = sample_analysis.analyze(
        acquisition, references, BrokenDatabase()
    )

    checks.ok(
        survived["analysis_status"] in ("OK", "FAILED"),
        "a database fault is handled either way",
    )
    checks.equal(
        len(survived["measurement"]["raw"]["white"]), 18,
        "the spectra survive intact",
    )

    # ==================================================================
    # ==================================================================
    checks.section("9. tests never write to the real data")

    with tempfile.TemporaryDirectory() as directory:
        copy = Path(directory) / "database.json"
        shutil.copy(bd_config.DATABASE_FILE, copy)

        payload = json.loads(copy.read_text(encoding="utf-8"))

        # A full database wraps its materials in a document that also
        # carries the schema version and source hash, so an added entry
        # goes inside "materials" - not at the top level beside them.
        payload["materials"]["Test Material"] = {
            "channels": {
                channel: {"reflectance_as_supplied": 0.5}
                for channel in CHANNELS
            }
        }
        copy.write_text(json.dumps(payload), encoding="utf-8")

        mutable = MaterialDatabase(copy)

        checks.equal(
            mutable.count(), 24, "a temporary copy can be extended"
        )
        checks.ok(
            "Test Material" in mutable.names(),
            "and the addition is visible there",
        )

    checks.equal(
        MaterialDatabase().count(), 23,
        "the real database still holds exactly 23 materials",
    )

    checks.ok(
        not hasattr(MaterialDatabase, "save_db")
        and not hasattr(MaterialDatabase, "add_material"),
        "the database class has no write path at all",
    )

    checks.raises(
        DatabaseError,
        lambda: MaterialDatabase(Path(directory) / "gone.json"),
        "a missing database file raises DatabaseError",
    )

    after = {
        "database.json": sha256(bd_config.DATABASE_FILE),
        "references.json": sha256(bd_config.REFERENCES_FILE),
        "samples.json": archive_state(),
    }

    for name in before:
        checks.equal(
            after[name], before[name],
            "{} is byte-identical after the whole test run".format(name),
        )

    return checks.report()


if __name__ == "__main__":
    sys.exit(main_tests())
