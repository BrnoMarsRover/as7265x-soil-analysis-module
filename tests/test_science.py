"""
BD science layer tests.

Pure data processing, so there is no hardware to stub. The protected
files are opened read-only and their SHA256 is checked before and after
the whole run: a test that quietly rewrites database.json or
references.json would invalidate every measurement ever taken against
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

support.add_path("BD")

import config                       # noqa: E402
import sample_analysis              # noqa: E402
from database import (              # noqa: E402
    DatabaseError,
    MaterialDatabase,
    References,
    cosine_similarity_percent,
)

CHANNELS = sample_analysis.CHANNELS


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def flat(value):
    return {channel: value for channel in CHANNELS}


def main_tests():
    checks = Checks("BD science")

    before = {
        "database.json": sha256(config.DATABASE_FILE),
        "references.json": sha256(config.REFERENCES_FILE),
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

    checks.equal(database.count(), 22, "22 reference materials are loaded")
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

    matches = database.compare(normalized)

    checks.equal(
        len(matches), database.count(),
        "every material is compared, not a top-N slice",
    )
    checks.equal(matches[0]["rank"], 1, "matches are ranked from 1")
    checks.ok(
        all(
            matches[i]["similarity_percent"]
            >= matches[i + 1]["similarity_percent"]
            for i in range(len(matches) - 1)
        ),
        "matches are sorted best first",
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
    self_matches = database.compare(database.materials[target])

    checks.equal(
        self_matches[0]["material"], target,
        "a database spectrum matches its own entry first",
    )
    checks.close(
        self_matches[0]["similarity_percent"], 100.0,
        "and scores 100%",
        tolerance=0.01,
    )

    # ==================================================================
    checks.section("7. interpretation")

    empty = sample_analysis.interpret([])

    checks.equal(
        empty["status"], sample_analysis.STATUS_NO_DATABASE,
        "an empty match list is NO_DATABASE",
    )
    checks.ok(
        empty["best_match"] is None, "with no invented best match"
    )

    strong = sample_analysis.interpret([
        {"rank": 1, "material": "Kaolin", "similarity_percent": 99.0},
        {"rank": 2, "material": "Talc", "similarity_percent": 90.0},
    ])

    checks.equal(
        strong["status"], sample_analysis.STATUS_STRONG,
        "a clear leader is a strong match",
    )
    checks.close(strong["score_difference"], 9.0, "the gap is reported")
    checks.equal(strong["best_match"], "Kaolin", "best match named")
    checks.equal(strong["second_match"], "Talc", "second match named")

    ambiguous = sample_analysis.interpret([
        {"rank": 1, "material": "Kaolin", "similarity_percent": 99.0},
        {"rank": 2, "material": "Talc", "similarity_percent": 98.5},
    ])

    checks.equal(
        ambiguous["status"], sample_analysis.STATUS_AMBIGUOUS,
        "a 0.5 point gap is ambiguous",
    )

    weak = sample_analysis.interpret([
        {"rank": 1, "material": "Kaolin", "similarity_percent": 40.0},
        {"rank": 2, "material": "Talc", "similarity_percent": 10.0},
    ])

    checks.equal(
        weak["status"], sample_analysis.STATUS_WEAK,
        "a low best score is a weak match",
    )

    for result in (strong, ambiguous, weak):
        conclusion = result["automatic_conclusion"].lower()

        checks.ok(
            "chemical identification" not in conclusion
            or "not a chemical identification" in conclusion,
            "the conclusion claims no chemical identification",
        )
        checks.ok(
            "probability" not in conclusion and "composition" not in conclusion,
            "the conclusion claims neither probability nor composition",
        )

    # ==================================================================
    checks.section("8. the whole pipeline")

    raw = {c: references.white[c] * 0.35 + references.dark[c] for c in CHANNELS}
    settings = {"integration_cycles": 100, "gain": 2, "gain_x": "16x"}

    result = sample_analysis.analyze(raw, references, database, settings)

    checks.equal(result["analysis_status"], "OK", "analysis succeeds")
    checks.equal(
        len(result["measurement"]["raw"]), 18, "raw is stored"
    )
    checks.equal(
        len(result["measurement"]["dark_corrected"]), 18,
        "dark-corrected is stored separately",
    )
    checks.equal(
        len(result["measurement"]["normalized"]), 18,
        "normalized is stored separately",
    )
    checks.ok(
        result["measurement"]["raw"] != result["measurement"]["normalized"],
        "raw is never overwritten by its derived form",
    )
    checks.close(
        result["measurement"]["normalized"]["A"], 0.35,
        "the pipeline reproduces the expected reflectance",
        tolerance=1e-4,
    )
    checks.equal(
        result["measurement"]["sensor_settings"], settings,
        "the sensor settings travel with the measurement",
    )
    checks.equal(
        result["calibration"]["equation"],
        "R = (Sample - Dark) / (White - Dark)",
        "the formula is recorded with the result",
    )
    checks.equal(
        len(result["reference_matches"]), 22,
        "all 22 materials are in the stored result",
    )

    checks.raises(
        sample_analysis.AnalysisError,
        lambda: sample_analysis.analyze(
            {"A": 1.0}, references, database, None
        ),
        "an incomplete spectrum is refused before any analysis",
    )

    # A broken database must not cost us the spectrum.
    class BrokenDatabase:
        path = "broken"

        def count(self):
            return 0

        def compare(self, _normalized):
            raise RuntimeError("database exploded")

    survived = sample_analysis.analyze(raw, references, BrokenDatabase())

    checks.equal(
        survived["analysis_status"], "FAILED",
        "a database failure is reported honestly",
    )
    checks.ok(
        "database exploded" in survived["analysis_error"],
        "the exact error is preserved",
    )
    checks.equal(
        len(survived["measurement"]["normalized"]), 18,
        "the spectrum survives the failure intact",
    )

    # ==================================================================
    checks.section("9. tests never write to the real data")

    with tempfile.TemporaryDirectory() as directory:
        copy = Path(directory) / "database.json"
        shutil.copy(config.DATABASE_FILE, copy)

        payload = json.loads(copy.read_text(encoding="utf-8"))
        payload["Test Material"] = flat(0.5)
        copy.write_text(json.dumps(payload), encoding="utf-8")

        mutable = MaterialDatabase(copy)

        checks.equal(
            mutable.count(), 23, "a temporary copy can be extended"
        )
        checks.ok(
            "Test Material" in mutable.names(),
            "and the addition is visible there",
        )

    checks.equal(
        MaterialDatabase().count(), 22,
        "the real database still holds exactly 22 materials",
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
        "database.json": sha256(config.DATABASE_FILE),
        "references.json": sha256(config.REFERENCES_FILE),
    }

    for name in before:
        checks.equal(
            after[name], before[name],
            "{} is byte-identical after the whole test run".format(name),
        )

    return checks.report()


if __name__ == "__main__":
    sys.exit(main_tests())
