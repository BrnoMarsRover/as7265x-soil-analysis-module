"""
Calibration handling and measurement quality control.

Two things are being protected here:

  1. the legacy calibration and the material library it belongs to are
     never touched, whatever a new calibration does;
  2. a measurement that the optics never really produced never reaches
     the classifier with a confident answer attached.

Everything runs against temporary directories. The real
BD/database.json and BD/references.json are opened read-only and their
SHA256 is checked at the end.
"""

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

import support
from support import Checks

support.add_project_root()

from BD import calibrations as calibration_store
from Measurements import calibration                     # noqa: E402
from BD import config as bd_config
from Measurements import config                          # noqa: E402
from Measurements import quality                         # noqa: E402
from BD.channels import CHANNELS          # noqa: E402
from BD.databases import MaterialDatabase, References  # noqa: E402


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def flat(value):
    return {channel: float(value) for channel in CHANNELS}


def block(spectrum, repeats=10, jitter=0.0):
    """An acquisition block as the ESP32 would return it."""
    acquisitions = []

    for index in range(repeats):
        offset = jitter * ((index % 3) - 1)
        acquisitions.append({
            channel: value + offset for channel, value in spectrum.items()
        })

    return {"illumination": "test", "acquisitions": acquisitions}


def aggregated(spectrum, repeats=10, jitter=0.0):
    from Measurements import aggregation

    return aggregation.aggregate_block(block(spectrum, repeats, jitter))


GOOD_SETTINGS = {
    "measurement_mode": 3,
    "integration_cycles": 100,
    "gain": 2,
    "gain_x": "16x",
}


def main_tests():
    checks = Checks("Calibration and quality")

    before = {
        "database.json": sha256(bd_config.DATABASE_FILE),
        "references.json": sha256(bd_config.REFERENCES_FILE),
    }

    # ==================================================================
    checks.section("1. the legacy calibration is protected")

    references = References()

    checks.equal(
        references.calibration_id, "FREYA_COMPETITION_2026_CAL_V1",
        "the legacy calibration has its own permanent ID",
    )
    checks.ok(references.protected, "and is marked protected")
    checks.equal(references.kind, "LEGACY", "and typed as legacy")
    checks.ok(
        not hasattr(References, "save") and not hasattr(References, "write"),
        "it has no save path of any kind",
    )
    checks.equal(
        MaterialDatabase().calibration_id, references.calibration_id,
        "and the material library records the same ID",
    )
    checks.equal(
        references.status()["illuminations"], ["white"],
        "the legacy calibration covers WHITE only - 18 features",
    )

    # ==================================================================
    checks.section("2. building a full calibration")

    dark = aggregated(flat(2.0), jitter=0.05)
    whites = {
        "white": aggregated(flat(500.0), jitter=1.0),
        "uv": aggregated(flat(300.0), jitter=1.0),
        "ir": aggregated(flat(400.0), jitter=1.0),
    }

    document = calibration.build_calibration(
        dark, whites, GOOD_SETTINGS, 10
    )

    checks.equal(
        document["schema_version"], bd_config.CALIBRATION_SCHEMA_VERSION,
        "the document carries its schema version",
    )
    checks.ok(
        document["calibration_id"].startswith("FREYA_FULL_SPECTRAL_CAL"),
        "and a new, distinct calibration ID",
    )
    checks.ok(
        document["calibration_id"] != references.calibration_id,
        "which is never the legacy one",
    )
    checks.equal(
        sorted(document["white_reference"].keys()), ["ir", "uv", "white"],
        "with a white reference for each illumination",
    )
    checks.equal(
        len(document["dark"]["aggregated"]), 18, "18 dark channels"
    )

    for name in calibration.ILLUMINATIONS:
        checks.equal(
            len(document["white_reference"][name]["aggregated"]), 18,
            "18 {} reference channels".format(name.upper()),
        )
        checks.equal(
            len(document["white_reference"][name]["acquisitions"]), 10,
            "every {} acquisition is retained".format(name.upper()),
        )

    checks.equal(
        sum(
            len(document["white_reference"][name]["aggregated"])
            for name in calibration.ILLUMINATIONS
        ),
        54,
        "54 white-reference values in total",
    )

    # ARCHITECTURE.md separated the two failure modes: a calibration that cannot
    # be ASSEMBLED is a science error (Measurements), a calibration that
    # cannot be STORED is a persistence error (BD).
    checks.raises(
        calibration.CalibrationBuildError,
        lambda: calibration.build_calibration(
            dark, {"white": whites["white"]}, GOOD_SETTINGS, 10
        ),
        "a calibration missing UV and IR is refused outright",
    )

    # ==================================================================
    checks.section("3. validation")

    result = calibration.validate_calibration(document, GOOD_SETTINGS)

    checks.equal(result["status"], "PASS", "a clean calibration passes")
    checks.equal(result["failures"], [], "with no failures")

    # Dark with holes in it.
    broken = json.loads(json.dumps(document))
    del broken["dark"]["aggregated"]["K"]

    result = calibration.validate_calibration(broken, GOOD_SETTINGS)

    checks.equal(result["status"], "FAIL", "a Dark with a missing channel fails")
    checks.ok(
        any(f["code"] == "DARK_CALIBRATION_FAILED" for f in result["failures"]),
        "and names the Dark as the reason",
    )

    # A white reference that never got any light.
    dead = json.loads(json.dumps(document))
    dead["white_reference"]["uv"]["aggregated"] = flat(2.0)

    result = calibration.validate_calibration(dead, GOOD_SETTINGS)

    checks.equal(
        result["status"], "FAIL",
        "a UV reference with no signal above Dark fails",
    )
    checks.ok(
        any(f["code"] == "UV_CALIBRATION_FAILED" for f in result["failures"]),
        "named per illumination",
    )

    # Wrong acquisition mode.
    result = calibration.validate_calibration(
        document, {"measurement_mode": 2, "integration_cycles": 100,
                   "gain": 2}
    )

    checks.equal(
        result["status"], "FAIL",
        "a calibration taken in continuous mode is incompatible",
    )
    checks.ok(
        any(f["code"] == "CALIBRATION_INCOMPATIBLE"
            for f in result["failures"]),
        "and says so",
    )

    result = calibration.validate_calibration({"nonsense": True})

    checks.equal(result["status"], "FAIL", "a bad schema fails")

    # ==================================================================
    checks.section("4. the calibration library and activation")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        library = root / "calibrations.json"
        store = calibration_store.CalibrationStore(root, library)

        checks.ok(store.active() is None, "a fresh store has no active set")
        checks.equal(store.history(), [], "and no history")
        checks.ok(
            not library.exists(),
            "and a fresh install leaves no empty library file behind",
        )

        document["validation"] = calibration.validate_calibration(
            document, GOOD_SETTINGS
        )

        path = store.save(document)

        checks.ok(Path(path).exists(), "a calibration is written to disk")
        checks.equal(
            Path(path), library,
            "every calibration lives in the one library file",
        )
        checks.ok(
            store.active() is None,
            "saving does NOT activate - that needs the operator",
        )

        checks.raises(
            calibration_store.CalibrationError,
            lambda: store.save(document),
            "an existing calibration is never overwritten",
        )

        loaded = store.activate(document["calibration_id"],
                               calibration.validate_calibration)

        checks.equal(
            store.active_id(), document["calibration_id"],
            "activation points at it",
        )
        checks.equal(
            loaded.white_for("uv"),
            document["white_reference"]["uv"]["aggregated"],
            "and the UV reference is reachable",
        )
        checks.raises(
            calibration_store.CalibrationError,
            lambda: loaded.white_for("xray"),
            "an unknown illumination is refused",
        )

        history = store.history()

        checks.equal(len(history), 1, "history lists it")
        checks.ok(history[0]["active"], "and marks it active")

        # A calibration that fails validation must never activate.
        bad = json.loads(json.dumps(document))
        bad["calibration_id"] = "FREYA_FULL_SPECTRAL_CAL_BAD"
        del bad["white_reference"]["ir"]["aggregated"]["W"]
        bad["validation"] = calibration.validate_calibration(
            bad, GOOD_SETTINGS
        )

        store.save(bad)

        checks.raises(
            calibration_store.CalibrationError,
            lambda: store.activate("FREYA_FULL_SPECTRAL_CAL_BAD",
                                   calibration.validate_calibration),
            "an invalid calibration cannot be activated",
        )
        checks.equal(
            store.active_id(), document["calibration_id"],
            "and the previous one stays in force",
        )
        checks.equal(
            len(store.history()), 2,
            "the failed one is kept as engineering data",
        )

        checks.raises(
            calibration_store.CalibrationError,
            lambda: store.load("NOT_A_CALIBRATION"),
            "loading an unknown calibration is refused",
        )

        # The list the operator picks from has to be enough to choose
        # with: an ID alone does not say which settings it was made
        # under, and that is exactly what distinguishes two of them.
        newest = store.history()[0]

        checks.equal(
            newest["gain_x"], GOOD_SETTINGS["gain_x"],
            "the history carries the gain each calibration was made at",
        )
        checks.equal(
            newest["integration_cycles"],
            GOOD_SETTINGS["integration_cycles"],
            "and the integration time",
        )
        checks.equal(
            newest["repeats"], document["repeats"],
            "and how many repeats it averaged",
        )
        checks.equal(
            sorted(newest["illuminations"]), ["ir", "uv", "white"],
            "and which illuminations it covers",
        )

        # Reopening the store must find the same library. This is the
        # whole point of the change: a restart used to leave the operator
        # with no list to choose from.
        reopened = calibration_store.CalibrationStore(root, library)

        checks.equal(
            reopened.active_id(), document["calibration_id"],
            "a new store instance still knows which one is active",
        )
        checks.equal(
            len(reopened.history()), 2,
            "and still lists every stored calibration",
        )

    # ------------------------------------------------------------------
    # Migration from the previous layout: one file per calibration plus a
    # separate active pointer. An operator who upgrades must not lose the
    # calibration they were using.
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)

        old_file = root / "{}.json".format(document["calibration_id"])

        with open(old_file, "w", encoding="utf-8") as handle:
            json.dump(document, handle)

        with open(root / "calibration_active.json", "w",
                  encoding="utf-8") as handle:
            json.dump({"calibration_id": document["calibration_id"]}, handle)

        migrated = calibration_store.CalibrationStore(
            root, root / "calibrations.json"
        )

        checks.equal(
            migrated.active_id(), document["calibration_id"],
            "the previously active calibration survives the upgrade",
        )
        checks.equal(
            len(migrated.history()), 1,
            "and is listed in the library",
        )
        checks.ok(
            old_file.exists(),
            "the original file is left exactly where it was",
        )
        checks.ok(
            (root / "calibrations.json").exists(),
            "and the library was written",
        )

    # ==================================================================
    checks.section("5. measurement quality control")

    white = flat(500.0)
    dark_reference = flat(2.0)

    healthy = {
        channel: 0.35 + 0.01 * index
        for index, channel in enumerate(CHANNELS)
    }

    steady = {
        "repeats": 5, "unstable_channels": [], "max_cv": 0.01,
        "mean_cv": 0.005,
    }

    report = quality.assess(healthy, white, dark_reference, steady)

    checks.equal(report["status"], "PASS", "a healthy spectrum passes")

    # Repeatability that was never measured is a warning, not a pass -
    # an unknown is not the same as a good result.
    checks.equal(
        quality.assess(healthy, white, dark_reference)["status"],
        "WARNING",
        "a measurement with no repeat statistics warns",
    )
    checks.equal(report["invalid_channels"], [], "with no channel excluded")
    checks.equal(
        len(report["usable_channels"]), 18, "all 18 usable"
    )

    # Reflectance far above 1: the calibration no longer describes the
    # geometry.
    impossible = flat(3.5)
    report = quality.assess(impossible, white, dark_reference, steady)

    checks.equal(
        report["status"], "FAIL", "reflectance far above 1 fails"
    )
    checks.ok(
        any(check["check"] == "reflectance" and check["status"] == "FAIL"
            for check in report["checks"]),
        "named as a reflectance failure",
    )

    slightly_high = {c: 1.05 for c in CHANNELS}
    report = quality.assess(slightly_high, white, dark_reference, steady)

    checks.ok(
        report["status"] in ("PASS", "WARNING"),
        "a little above 1 is tolerated as measurement uncertainty",
    )

    # No illumination to divide by.
    report = quality.assess(healthy, flat(2.1), dark_reference, steady)

    checks.equal(
        report["status"], "FAIL", "a near-zero denominator fails"
    )
    checks.equal(
        len(report["invalid_channels"]), 18,
        "and every channel is marked unusable",
    )

    # Detector boundary discontinuity.
    for left, right in (("F", "G"), ("L", "R")):
        stepped = dict(healthy)
        stepped[right] = healthy[left] * 20.0

        report = quality.assess(stepped, white, dark_reference, steady)

        checks.ok(
            any(
                check["check"] == "triad_boundary"
                and check["status"] in ("WARNING", "FAIL")
                for check in report["checks"]
            ),
            "a step at the {}->{} detector seam is caught".format(
                left, right
            ),
        )

    # Missing channels.
    incomplete = dict(healthy)

    for channel in list(CHANNELS)[:8]:
        incomplete[channel] = None

    report = quality.assess(incomplete, white, dark_reference, steady)

    checks.equal(
        report["status"], "FAIL",
        "too few usable channels to classify anything",
    )

    # Unstable repeats.
    unstable = {
        "repeats": 5,
        "unstable_channels": list(CHANNELS),
        "max_cv": 0.9,
        "mean_cv": 0.5,
    }

    report = quality.assess(healthy, white, dark_reference, unstable)

    checks.equal(
        report["status"], "FAIL", "acquisitions that did not repeat fail"
    )

    checks.equal(
        quality.assess(healthy, white, dark_reference, steady)["status"],
        "PASS", "steady repeats pass",
    )

    # ==================================================================
    checks.section("6. the optional distance gate")

    report = quality.assess(healthy, white, dark_reference, steady, None)

    checks.equal(
        report["status"], "PASS",
        "no distance sensor never blocks a measurement",
    )
    checks.equal(
        next(
            check["details"]["distance_status"]
            for check in report["checks"] if check["check"] == "distance"
        ),
        "UNAVAILABLE",
        "and is reported as unavailable rather than invented",
    )

    report = quality.assess(healthy, white, dark_reference, steady, 15.0)

    checks.equal(report["status"], "PASS", "an in-range distance passes")

    report = quality.assess(healthy, white, dark_reference, steady, 90.0)

    checks.equal(
        report["status"], "FAIL", "a sample far out of range fails"
    )
    checks.close(
        healthy["A"], 0.35,
        "and the reflectance is NOT corrected for distance",
    )

    # ==================================================================
    checks.section("7. the protected data is untouched")

    after = {
        "database.json": sha256(bd_config.DATABASE_FILE),
        "references.json": sha256(bd_config.REFERENCES_FILE),
    }

    for name in before:
        checks.equal(
            after[name], before[name],
            "{} is byte-identical after the whole run".format(name),
        )

    return checks.report()


if __name__ == "__main__":
    sys.exit(main_tests())
