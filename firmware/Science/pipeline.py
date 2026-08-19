"""
The EvidencePackage — everything the measurement layer knows, and no
conclusion about what the sample is.

This is the contract between deterministic mathematics and
interpretation. Science answers:

    what did the detector report
    how good was the measurement
    which of the 54 features can be trusted, and for what
    how far is it from every individual reference
    how far is it from every class distribution
    what do the derived representations look like

Science does NOT answer "this is Bentonite". That is
Science.decision's job, and separating them is the whole point: the
mathematics is deterministic and testable, the interpretation is
learned and versioned, and mixing them makes both unauditable. §10.

WHY QUALITY IS SPLIT IN TWO

`hardware_qc` asks whether the instrument worked. `normalization` asks
whether the reflectance derived from a working measurement is
meaningful. Six of twelve measurements on 2026-08-17 had perfect
hardware and ill-conditioned normalization, and reporting one verdict for
both threw away every one of them. §11.

    HARDWARE_QC_FAIL         the counts are not a measurement
    NORMALIZATION_WARNING    the counts are fine, the division is not

A NORMALIZATION_WARNING never suppresses the raw evidence, and never
stops a decision being attempted - it lowers the weight of the
representations that depend on the division, which is what it actually
implies.
"""

from BD import config as bd_config
from BD.channels import (
    AS7265X_18,
    AS7265X_54_MULTIILLUM,
    CHANNELS,
    ILLUMINATIONS,
    channel_wavelengths,
    combine_illuminations,
    FeatureSpaceError,
)
from datetime import datetime, timezone

from Science import (
    comparison,
    config,
    decision,
    features as feature_module,
    metrics,
    preprocessing,
    quality,
)

EVIDENCE_SCHEMA_VERSION = 1

# Bumped when the shape of a stored AnalysisRun changes, so a record
# written months ago can still be read by the code that reads it.
ANALYSIS_RUN_SCHEMA_VERSION = 1


def _utc_now():
    return datetime.now(timezone.utc).isoformat()

# The two verdicts that used to be one.
HARDWARE_PASS = "PASS"
HARDWARE_WARNING = "WARNING"
HARDWARE_FAIL = "HARDWARE_QC_FAIL"

NORMALIZATION_OK = "OK"
NORMALIZATION_WARNING = "NORMALIZATION_WARNING"
NORMALIZATION_UNUSABLE = "NORMALIZATION_UNUSABLE"

# Which QC checks are statements about the hardware, and which are
# statements about the reference division. Assigning them was the
# substance of the fix: `reflectance` moved from the first list to the
# second, and that alone recovers six measurements.
HARDWARE_CHECKS = ("validity", "repeatability", "triad_boundary")
NORMALIZATION_CHECKS = ("illumination", "reflectance", "distance")


class EvidenceError(Exception):
    """An evidence package cannot be assembled."""

    def __init__(self, code, message, details=None):
        super().__init__(message)

        self.code = code
        self.message = message
        self.details = details or {}


def split_quality(report):
    """
    Separate the hardware verdict from the normalization verdict.

    Reuses `Science/quality.py` unchanged - the checks were always
    right, it was the single combined verdict that was wrong.
    """
    checks = (report or {}).get("checks") or []

    hardware = []
    normalization = []

    for check in checks:
        name = check.get("check")

        if name in NORMALIZATION_CHECKS:
            normalization.append(check)
        else:
            hardware.append(check)

    def worst(entries):
        if any(entry.get("status") == "FAIL" for entry in entries):
            return "FAIL"

        if any(entry.get("status") == "WARNING" for entry in entries):
            return "WARNING"

        return "PASS"

    hardware_status = worst(hardware)
    normalization_status = worst(normalization)

    return {
        "hardware": {
            "status": (
                HARDWARE_FAIL if hardware_status == "FAIL"
                else HARDWARE_WARNING if hardware_status == "WARNING"
                else HARDWARE_PASS
            ),
            "checks": hardware,
            "usable": hardware_status != "FAIL",
        },
        "normalization": {
            "status": (
                NORMALIZATION_UNUSABLE if normalization_status == "FAIL"
                else NORMALIZATION_WARNING
                if normalization_status == "WARNING"
                else NORMALIZATION_OK
            ),
            "checks": normalization,
            "note": "A normalization problem does not invalidate the raw "
                    "measurement. It lowers the weight of every "
                    "representation derived by dividing by the reference.",
        },
        "combined_legacy_status": (report or {}).get("status"),
        "usable_channels": (report or {}).get("usable_channels"),
    }


def build(measurement_id, raw_blocks, dark, white_by_illumination,
          registry=None, sensor_settings=None, acquisition_profile=None,
          calibration_id=None, legacy_calibration_id=None,
          legacy_references=None, sample_statistics=None,
          reference_statistics=None, class_statistics=None,
          class_observations=None, distance_mm=None, feature_sources=None):
    """
    Assemble the complete evidence package.

    `legacy_references` is the frozen White/Dark DB1 was built against.
    It is passed separately and used ONLY for DB1, because DB1 may never
    be compared under any other calibration - see DATABASES.md.

    Every section is independent: a failure to build one (an empty
    database, absent class statistics) leaves that section marked
    unavailable and the rest intact.
    """
    representations = preprocessing.build_representations(
        raw_blocks, dark, white_by_illumination
    )

    features = feature_module.build_features(representations)

    reliability = quality.assess_channels(
        raw_blocks, dark, white_by_illumination,
        sample_statistics, reference_statistics,
    )

    white_normalized = (representations.get("normalized") or {}).get("white")

    quality_report = None

    if white_normalized and white_by_illumination.get("white"):
        quality_report = quality.assess(
            white_normalized,
            white_by_illumination["white"],
            dark,
            (sample_statistics or {}).get("white"),
            distance_mm,
            "white",
        )

    verdicts = split_quality(quality_report)

    package = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "science_version": config.SCIENCE_VERSION,
        "reliability_version": quality.RELIABILITY_VERSION,

        "measurement": {
            "measurement_id": measurement_id,
            "wavelengths": channel_wavelengths(),
            "illuminations": list(representations.get("available") or []),
        },

        "acquisition": {
            "acquisition_profile_id": (
                (acquisition_profile or {}).get("profile_id")
            ),
            "acquisition_profile_fingerprint": (
                (acquisition_profile or {}).get("fingerprint")
            ),
            "calibration_id": calibration_id,
            "legacy_calibration_id": legacy_calibration_id,
            "sensor_settings": sensor_settings or {},
        },

        "raw": {
            illumination: dict(spectrum)
            for illumination, spectrum in (
                representations.get("raw") or {}
            ).items()
        },

        "quality": verdicts,
        "channel_reliability": reliability,
        "representations": representations,
        "feature_summary": features,

        "reference_analysis": {},
        "class_analysis": {},
        "transfer_analysis": {
            "available": False,
            "reason": "no validated calibration transfer model is active",
        },

        "warnings": [],
    }

    package["warnings"].extend(_warnings_from(verdicts, reliability))

    package["reference_analysis"] = _reference_analysis(
        registry, representations, reliability, legacy_references,
        raw_blocks, dark, feature_sources,
    )

    package["class_analysis"] = _class_analysis(
        representations, reliability, class_statistics, class_observations
    )

    return package


def _warnings_from(verdicts, reliability):
    warnings = []

    if verdicts["hardware"]["status"] == HARDWARE_FAIL:
        warnings.append({
            "code": "HARDWARE_QC_FAIL",
            "message": "The instrument did not produce a usable "
                       "measurement. No representation of it is "
                       "trustworthy.",
        })

    if verdicts["normalization"]["status"] != NORMALIZATION_OK:
        warnings.append({
            "code": verdicts["normalization"]["status"],
            "message": "The raw counts are valid but the reference "
                       "division is poorly conditioned. Reflectance-based "
                       "evidence is down-weighted; raw evidence is not.",
        })

    total = reliability.get("features_total", 0)
    raw_valid = reliability.get("raw_valid_total", 0)
    normalized_valid = reliability.get("normalized_valid_total", 0)

    if total and normalized_valid < total:
        warnings.append({
            "code": "LOW_NORMALIZED_RELIABILITY",
            "message": quality.summarize(reliability),
            "raw_valid": raw_valid,
            "normalized_valid": normalized_valid,
            "features": total,
        })

    return warnings


def _reference_analysis(registry, representations, reliability,
                        legacy_references, raw_blocks, dark,
                        feature_sources):
    """
    Distance to every individual reference, per database.

    DB1 is compared under the LEGACY calibration and nothing else. DB2
    and DB3 are compared under the active one. Which was used is recorded
    in every result.
    """
    if registry is None:
        return {
            "available": False,
            "reason": "no database registry was supplied",
        }

    normalized = representations.get("normalized") or {}
    white_active = normalized.get("white")

    white_legacy = None

    if legacy_references is not None and raw_blocks.get("white"):
        white_legacy = preprocessing.normalize(
            raw_blocks["white"], legacy_references.dark,
            legacy_references.white,
        )

    weights = quality.weights(reliability, "white")
    usable = quality.usable_channels(reliability, "white") \
        or list(CHANNELS)

    analysis = {"available": True, "databases": {}}

    sources = dict(feature_sources or {})

    for key, handle in sorted(registry.databases.items()):
        entry = {
            "database": key,
            "database_id": handle.database_id,
            "version": handle.version,
            "evidence": handle.evidence,
            "feature_space": handle.feature_space,
            "status": handle.status,
        }

        if not handle.ready:
            entry["available"] = False
            entry["reason"] = (handle.problems or ["not available"])[0]
            analysis["databases"][key] = entry

            continue

        override = sources.get(key) or {}

        if key == "DB1":
            measured = override.get("features", white_legacy)
            normalization = override.get(
                "normalization",
                "legacy:{}".format(
                    legacy_references.calibration_id
                    if legacy_references is not None else "unknown"
                ),
            )

        elif handle.feature_space == AS7265X_54_MULTIILLUM:
            try:
                measured = override.get(
                    "features", combine_illuminations(normalized)
                )

            except FeatureSpaceError as error:
                entry["available"] = False
                entry["reason"] = error.message
                analysis["databases"][key] = entry

                continue

            normalization = override.get("normalization", "active")

        else:
            measured = override.get("features", white_active)
            normalization = override.get("normalization", "active")

        if not measured:
            entry["available"] = False
            entry["reason"] = (
                "no normalized spectrum is available for this database"
            )
            analysis["databases"][key] = entry

            continue

        comparison_channels = (
            None if handle.feature_space == AS7265X_54_MULTIILLUM else usable
        )
        comparison_weights = (
            None if handle.feature_space == AS7265X_54_MULTIILLUM else weights
        )

        entry.update(metrics.compare_library(
            measured, handle.materials, comparison_channels,
            comparison_weights,
        ))

        entry["available"] = True
        entry["normalization"] = normalization
        entry["channels_used"] = (
            list(comparison_channels) if comparison_channels else None
        )

        analysis["databases"][key] = entry

    return analysis


def _class_analysis(representations, reliability, class_statistics,
                    class_observations):
    """Distance to class distributions, where any exist yet."""
    if not class_statistics:
        return {
            "available": False,
            "reason": "no class statistics have been built yet - they "
                      "need independent verified measurements, which "
                      "accumulate in the decision learning database",
            "classes": 0,
        }

    normalized = representations.get("normalized") or {}
    white = normalized.get("white")

    if not white:
        return {
            "available": False,
            "reason": "no normalized WHITE spectrum to place in the class "
                      "space",
            "classes": len(class_statistics),
        }

    report = comparison.compare_classes(
        white, class_statistics, class_observations
    )

    report["available"] = True
    report["feature_space"] = AS7265X_18
    report["basis"] = "normalized WHITE reflectance"

    return report


def feature_vector(package, illumination="white",
                   representation="normalized"):
    """One named representation, ready for a metric. Never a conclusion."""
    return (
        (package.get("representations") or {})
        .get(representation, {})
        .get(illumination)
    )


def summary(package):
    """The compact operator view, built only from what is in the package."""
    quality_block = package.get("quality") or {}
    reliability = package.get("channel_reliability") or {}

    return {
        "measurement_id": (package.get("measurement") or {}).get(
            "measurement_id"
        ),
        "hardware_qc": (quality_block.get("hardware") or {}).get("status"),
        "normalization": (quality_block.get("normalization") or {}).get(
            "status"
        ),
        "raw_valid_features": reliability.get("raw_valid_total"),
        "normalized_valid_features": reliability.get(
            "normalized_valid_total"
        ),
        "features_total": reliability.get("features_total"),
        "illuminations": (package.get("measurement") or {}).get(
            "illuminations"
        ),
        "databases": sorted(
            (package.get("reference_analysis") or {}).get("databases", {})
        ),
        "class_analysis": (package.get("class_analysis") or {}).get(
            "available", False
        ),
        "warnings": [
            warning.get("code") for warning in package.get("warnings") or []
        ],
    }


# ======================================================================
# the entry point
# ======================================================================
# ONE function turns a stored RAW Measurement into a complete
# AnalysisRun. Everything above is a stage of it.
#
#     RAW Measurement
#       -> schema / acquisition validation
#       -> calibration selection
#       -> dark correction
#       -> white normalization
#       -> quality evaluation
#       -> feature extraction
#       -> DB1, DB2, DB3, each independently
#       -> individual metric evidence
#       -> distance to reference, distance to class
#       -> cross-method and cross-database agreement
#       -> Decision Model
#       -> AnalysisRun
#
# FAILURE ISOLATION IS THE POINT OF THE STRUCTURE
#
# Each stage is attempted separately and records its own status. A
# database that will not load, a class snapshot that is absent, a
# decision model that raises - none of them may discard the evidence
# the earlier stages already produced. A run that got as far as DB1 and
# DB2 evidence and then failed is worth vastly more than no run:
#
#     analysis_status = PARTIAL
#     DB1  OK      evidence kept
#     DB2  OK      evidence kept
#     DB3  FAILED  reason recorded
#
# And RAW is never touched by any of this. The Measurement is read.

ANALYSIS_OK = "OK"
ANALYSIS_PARTIAL = "PARTIAL"
ANALYSIS_FAILED = "FAILED"

DECISION_OK = "OK"
DECISION_FAILED = "FAILED"
DECISION_SKIPPED = "SKIPPED"


def _stage(record, name, function, *args, **kwargs):
    """
    Run one stage, and record what happened either way.

    A stage that raises is recorded as FAILED with its reason and
    returns None. It never propagates: the caller decides what a
    missing stage means, and every stage that already succeeded keeps
    its result.
    """
    try:
        value = function(*args, **kwargs)

    except Exception as error:
        record[name] = {
            "status": ANALYSIS_FAILED,
            "error": {
                "type": type(error).__name__,
                "message": str(error),
            },
        }

        return None

    record[name] = {"status": ANALYSIS_OK}

    return value


def analyze(measurement, calibration, registry, taxonomy=None,
            learning_store=None, class_statistics=None,
            class_observations=None, class_snapshot=None,
            legacy_references=None, analysis_run_id=None,
            feature_sources=None):
    """
    One RAW Measurement in, one complete AnalysisRun out.

    DETERMINISTIC AND HARDWARE-INDEPENDENT. Nothing here opens a serial
    port, moves a servo, asks a question or writes a file. Given the
    same Measurement, calibration and databases it produces the same
    answer, which is what makes a stored conclusion reproducible at all.

    `measurement` is a stored Measurement record: its RAW acquisition
    grouped by illumination, its acquisition metadata, and the id of
    the calibration it was taken under. It is READ. Re-analysing a
    Measurement produces a NEW AnalysisRun and changes nothing about
    the Measurement or any run that came before it.

    Returns the AnalysisRun. It always contains a status; it never
    raises for a scientific outcome, only for a caller error such as a
    Measurement with no RAW in it at all.
    """
    raw = (measurement or {}).get("raw")

    if not raw:
        raise EvidenceError(
            "NO_RAW",
            "The measurement contains no RAW acquisition. There is "
            "nothing to analyse and nothing may be invented.",
        )

    dark = (calibration or {}).get("dark")
    white_by_illumination = (calibration or {}).get("white") or {}

    if not dark:
        raise EvidenceError(
            "NO_CALIBRATION",
            "No dark reference. Reflectance cannot be derived, and a "
            "measurement is not analysed against a calibration that "
            "does not exist.",
        )

    stages = {}

    run = {
        "analysis_run_id": analysis_run_id,
        "measurement_id": (measurement or {}).get("measurement_id"),
        "sample_id": (measurement or {}).get("sample_id"),
        "created_at": _utc_now(),

        # Provenance. A conclusion that cannot say which pipeline,
        # which model and which library version produced it cannot be
        # reproduced, and an unreproducible result is an anecdote.
        "versions": {
            "analysis_schema": ANALYSIS_RUN_SCHEMA_VERSION,
            "evidence_schema": EVIDENCE_SCHEMA_VERSION,
            "science": config.SCIENCE_VERSION,
            "analysis": config.ANALYSIS_VERSION,
            "reliability": quality.RELIABILITY_VERSION,
            "decision_model": decision.MODEL_VERSION,
            "esp32_firmware": (
                (measurement or {}).get("acquisition") or {}
            ).get("firmware_version"),
            "databases": {},
        },

        "calibration": {
            "calibration_id": (calibration or {}).get("calibration_id"),
            "calibration_version": (calibration or {}).get("version"),
            "calibration_hash": (calibration or {}).get("content_hash"),
            "legacy_calibration_id": getattr(
                legacy_references, "calibration_id", None
            ),
        },

        "stages": stages,
        "analysis_status": ANALYSIS_OK,
        "decision_status": DECISION_SKIPPED,
    }

    # --- evidence -----------------------------------------------------
    acquisition = (measurement or {}).get("acquisition") or {}

    evidence = _stage(
        stages, "evidence", build,
        measurement.get("measurement_id"),
        raw,
        dark,
        white_by_illumination,
        registry=registry,
        sensor_settings=acquisition.get("sensor_settings"),
        acquisition_profile=acquisition.get("profile_id"),
        calibration_id=(calibration or {}).get("calibration_id"),
        legacy_calibration_id=getattr(
            legacy_references, "calibration_id", None
        ),
        legacy_references=legacy_references,
        sample_statistics=(measurement or {}).get("statistics"),
        reference_statistics=(calibration or {}).get("statistics"),
        class_statistics=class_statistics,
        class_observations=class_observations,
        distance_mm=acquisition.get("distance_mm"),
        feature_sources=feature_sources,
    )

    if evidence is None:
        # Nothing downstream can run without evidence, but the RAW that
        # produced this attempt is untouched and the Measurement can be
        # analysed again by a later Science version.
        run["analysis_status"] = ANALYSIS_FAILED

        return run

    run["evidence"] = evidence
    run["quality"] = evidence.get("quality")
    run["channel_reliability"] = evidence.get("channel_reliability")
    run["representations"] = evidence.get("representations")
    run["features"] = evidence.get("feature_summary")
    run["reference_analysis"] = evidence.get("reference_analysis")
    run["class_analysis"] = evidence.get("class_analysis")

    # DB1, DB2 and DB3 as three separate results, each naming the
    # calibration it was compared under. Flattened out of the evidence
    # so a stored record can be read without knowing the evidence
    # package's shape.
    run["database_results"] = [
        dict(entry, database=key)
        for key, entry in sorted(
            (evidence.get("reference_analysis") or {})
            .get("databases", {}).items()
        )
    ]

    if registry is not None:
        run["versions"]["databases"] = {
            key: handle.version
            for key, handle in sorted(registry.databases.items())
        }

    # NO SECOND COMPARISON HERE.
    #
    # build() has already compared this measurement against every
    # database independently, under the calibration each one is
    # entitled to see, and kept the three results apart. Running a
    # second per-database comparison and reaching a second consensus
    # from it is what produced two answers in one record.
    #
    # The evidence goes to the Decision Model, which is the one place
    # allowed to combine it.
    #
    # AND NO MIXTURE ESTIMATION.
    #
    # An NNLS unmixing step used to run at this point and return
    # fractional contributions per endmember. It lives in
    # research/mixture.py now and is not part of the production
    # pipeline. The numbers it produces look exactly like composition
    # and are not: they are least-squares coefficients on a handful of
    # reference spectra chosen by a similarity ranking, with no
    # validated quantitative basis and nothing to check them against.
    # "80% basalt" is a claim about how much of a material is present,
    # and this instrument cannot make it. Similarity is not abundance.
    #
    # The mathematics is sound and the question is worth studying. It
    # comes back only with a validation study behind it.

    # --- the Decision Model -------------------------------------------
    # Runs on the evidence, not on the inference summary: fusing
    # evidence is its job, and handing it a conclusion to agree with
    # would be the circularity the whole architecture exists to avoid.
    conclusion = _stage(
        stages, "decision", decision.decide,
        evidence,
        taxonomy=taxonomy,
        registry=registry,
        learning_store=learning_store,
        class_snapshot=class_snapshot,
    )

    if conclusion is None:
        run["decision_status"] = DECISION_FAILED

    else:
        run["decision_status"] = DECISION_OK
        run["decision"] = conclusion

    failed = [
        name for name, entry in stages.items()
        if entry.get("status") == ANALYSIS_FAILED
    ]

    if failed:
        run["analysis_status"] = ANALYSIS_PARTIAL
        run["failed_stages"] = failed

    return run
