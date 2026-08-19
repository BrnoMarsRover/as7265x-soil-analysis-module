"""
Spectral processing: validation, dark correction, normalization,
database comparison and automatic interpretation.

Pure data processing. Every function takes plain dictionaries and
returns plain dictionaries. Nothing here touches a sensor, a serial
port, the carousel, or any notion of a "current" measurement.

That is deliberate. The previous design hung normalization off a
long-lived measurement object whose sample_data could be None while a
spectrum existed elsewhere, which produced

    'NoneType' object has no attribute 'sample_data'

whenever the two disagreed. With no shared object there is nothing to
disagree.

Scientific constraint, enforced by the wording produced here: this is
COMPARATIVE SPECTRAL CLASSIFICATION against a small reference library.
It never claims chemical identification. The numbers are similarity and
error metrics between reflectance vectors - not composition, not
probability, not certainty.
"""

from BD import config as bd_config
from Measurements import aggregation, config, metrics, quality


# Attached to every conclusion. The library holds PURE reference
# materials, so a mixture cannot be decomposed by comparing against it -
# and the honest thing is to say so every time rather than let a
# confident-looking ranking imply otherwise.
MIXTURE_CAVEAT = (
    "The reference library contains pure materials and is not trained "
    "to estimate mixture composition."
)


# Re-exported so every existing importer keeps working. The definitions
# live in BD/schemas/channels.py — the 18-channel vocabulary is the record
# schema, and BD must be able to validate record shape without importing
# this layer. See ARCHITECTURE.md.
from BD.channels import (  # noqa: E402,F401
    CHANNELS,
    WAVELENGTHS,
    channel_wavelengths,
    copy_channels,
    round_channels,
    validate_spectrum,
)

# Interpretation outcomes.
STATUS_STRONG = "REFERENCE_MATCH"
STATUS_AMBIGUOUS = "AMBIGUOUS"
STATUS_WEAK = "POOR_MATCH"
STATUS_NO_DATABASE = "NO_DATABASE"
STATUS_METRICS_DISAGREE = "METRICS_DISAGREE"
STATUS_QUALITY_WARNING = "MEASUREMENT_QUALITY_WARNING"
STATUS_QUALITY_FAIL = "MEASUREMENT_QUALITY_FAIL"

# Historical spellings, so records written before this release still
# compare equal to the constants.
LEGACY_STATUS_ALIASES = {
    "STRONG_REFERENCE_MATCH": STATUS_STRONG,
    "WEAK_REFERENCE_MATCH": STATUS_WEAK,
}


class AnalysisError(Exception):
    """The spectrum cannot be processed."""

    def __init__(self, code, message, details=None):
        super().__init__(message)

        self.code = code
        self.message = message
        self.details = details or {}


# ----------------------------------------------------------------------
# channel helpers
# ----------------------------------------------------------------------

def require_spectrum(data, what="spectrum"):
    """Validate or raise. Used at the boundary where RAW arrives."""
    missing = validate_spectrum(data)

    if missing:
        raise AnalysisError(
            "INCOMPLETE_SPECTRUM",
            "{} has {}/{} usable channels; missing {}.".format(
                what,
                len(CHANNELS) - len(missing),
                len(CHANNELS),
                ",".join(missing),
            ),
            {"missing_channels": missing},
        )

    return data


# ----------------------------------------------------------------------
# the two formulas
# ----------------------------------------------------------------------

def dark_correct(sample, dark, decimals=None):
    """
    C = S - D, per channel.

    Negative results are KEPT. A channel reading below the stored dark
    reference is real information about noise or drift; clamping it to
    zero would hide that from the operator.
    """
    if decimals is None:
        decimals = config.RAW_DECIMALS

    sample = copy_channels(sample)
    dark = copy_channels(dark)

    return {
        channel: round(sample[channel] - dark[channel], decimals)
        for channel in CHANNELS
    }


def normalize(sample, dark, white, decimals=None):
    """
    R = (S - D) / (W - D), per channel.

    Where White equals Dark the reflectance is undefined; that channel
    is reported as 0.0 rather than raising, and
    References.zero_denominator_channels() names which ones so the
    result can be read honestly.
    """
    if decimals is None:
        decimals = config.NORMALIZED_DECIMALS

    sample = copy_channels(sample)
    dark = copy_channels(dark)
    white = copy_channels(white)

    result = {}

    for channel in CHANNELS:
        denominator = white[channel] - dark[channel]

        if denominator == 0.0:
            result[channel] = 0.0
        else:
            result[channel] = round(
                (sample[channel] - dark[channel]) / denominator,
                decimals,
            )

    return result


# ----------------------------------------------------------------------
# automatic interpretation
# ----------------------------------------------------------------------

def _thresholds():
    """Thresholds recorded with the result so old records stay readable."""
    return {
        "min_similarity_percent": config.MIN_SIMILARITY_PERCENT,
        "ambiguity_margin_percent": config.AMBIGUITY_MARGIN_PERCENT,
        "metric": "cosine_similarity",
        "note": (
            "Heuristic competition-support thresholds. Tune with "
            "experimental data. Similarity is not probability."
        ),
    }


def interpret(matches, agreement=None, quality=None):
    """
    A conservative reading of the ranked comparison.

    Three things have to hold before a result is called a match: the
    measurement has to be sound, the three metrics have to point the
    same way, and the winner has to be clearly ahead of the runner-up.
    Any one of them failing downgrades the answer rather than being
    averaged away.
    """
    quality_status = (quality or {}).get("status")

    if quality_status == "FAIL":
        return {
            "best_match": None,
            "best_similarity": None,
            "second_match": None,
            "second_similarity": None,
            "score_difference": None,
            "status": STATUS_QUALITY_FAIL,
            "confidence": "NONE",
            "automatic_conclusion": (
                "The measurement did not pass quality control, so no "
                "reference comparison is reported. The acquired spectra "
                "are stored and can be re-examined. Reasons: {}".format(
                    "; ".join((quality or {}).get("failures") or ["unknown"])
                )
            ),
            "thresholds": _thresholds(),
        }

    if not matches:
        return {
            "best_match": None,
            "best_similarity": None,
            "second_match": None,
            "second_similarity": None,
            "score_difference": None,
            "status": STATUS_NO_DATABASE,
            "confidence": "NONE",
            "automatic_conclusion": (
                "No reference material database was available, so no "
                "spectral comparison could be performed. The measured "
                "spectrum has been stored and can be compared later."
            ),
            "thresholds": _thresholds(),
        }

    best = matches[0]
    best_name = best["material"]
    best_score = best.get("cosine_similarity_percent")

    if len(matches) > 1:
        second = matches[1]
        second_name = second["material"]
        second_score = second.get("cosine_similarity_percent")
        difference = (
            round(best_score - second_score, 2)
            if best_score is not None and second_score is not None else None
        )
        rank_separation = round(
            second.get("rank_score", 0) - best.get("rank_score", 0), 3
        )

    else:
        second_name = None
        second_score = None
        difference = None
        rank_separation = None

    agreement = agreement or {}
    metrics_agree = agreement.get("agree")

    detail = (
        "cosine {} %, RMSE {}, Pearson r {}".format(
            best_score, best.get("rmse"), best.get("pearson_r")
        )
    )

    # -- the three ways a result stops being a clean match -------------
    if metrics_agree is False:
        status = STATUS_METRICS_DISAGREE
        confidence = "LOW"
        conclusion = (
            "The comparison metrics disagree. By spectral shape the "
            "closest reference is {}; by absolute reflectance error it "
            "is {}; by correlation it is {}. Shape and magnitude are "
            "pointing at different materials, which usually means the "
            "sample is not well represented by any single reference in "
            "the current library.".format(
                agreement.get("cosine_best"),
                agreement.get("rmse_best"),
                agreement.get("pearson_best"),
            )
        )

    elif best_score is not None and best_score < config.MIN_SIMILARITY_PERCENT:
        status = STATUS_WEAK
        confidence = "LOW"
        conclusion = (
            "No reference in the current library is a good fit. The "
            "closest is {} ({}), below the {:.1f}% support threshold. "
            "The sample is probably not represented by the available "
            "reference materials.".format(
                best_name, detail, config.MIN_SIMILARITY_PERCENT
            )
        )

    elif (
        rank_separation is not None
        and rank_separation < config.MIN_RANK_SEPARATION
    ):
        status = STATUS_AMBIGUOUS
        confidence = "MODERATE"
        conclusion = (
            "The measured spectrum is most consistent with {} ({}), but "
            "{} is nearly as close across all three metrics. The "
            "comparison cannot separate them, so the result is reported "
            "as ambiguous.".format(best_name, detail, second_name)
        )

    elif (
        difference is not None
        and difference < config.AMBIGUITY_MARGIN_PERCENT
    ):
        status = STATUS_AMBIGUOUS
        confidence = "MODERATE"
        conclusion = (
            "The measured spectrum is most consistent with {} ({}). {} "
            "is the second closest and only {:.1f} percentage points "
            "behind on cosine similarity, so the classification is "
            "reported as ambiguous.".format(
                best_name, detail, second_name, difference
            )
        )

    else:
        status = STATUS_STRONG
        confidence = "HIGH"
        conclusion = (
            "The measured spectrum is most consistent with {} ({}), "
            "ranked first by all three metrics. This is a comparative "
            "spectral result against the reference library, not a "
            "chemical identification.".format(best_name, detail)
        )

    # A warning never turns a match into a failure, but it must not be
    # quietly dropped either.
    if quality_status == "WARNING" and status == STATUS_STRONG:
        status = STATUS_QUALITY_WARNING
        confidence = "MODERATE"
        conclusion = (
            "{} Measurement quality raised warnings: {}".format(
                conclusion,
                "; ".join((quality or {}).get("warnings") or ["unknown"]),
            )
        )

    conclusion = "{} {}".format(conclusion, MIXTURE_CAVEAT)

    return {
        "best_match": best_name,
        "best_similarity": best_score,
        "best_rmse": best.get("rmse"),
        "best_pearson_r": best.get("pearson_r"),
        "second_match": second_name,
        "second_similarity": second_score,
        "score_difference": difference,
        "rank_separation": rank_separation,
        "metric_agreement": agreement,
        "status": status,
        "confidence": confidence,
        "automatic_conclusion": conclusion,
        "thresholds": _thresholds(),
    }


# ----------------------------------------------------------------------
# the whole pipeline, in one call
# ----------------------------------------------------------------------

def _blocks_from(acquisition):
    """
    Aggregate whatever the ESP32 sent into one spectrum per lamp.

    Accepts both shapes: the WHITE/UV/IR protocol with repeats, and a
    bare 18-channel dict from an older single-spectrum acquisition. A
    stored record from before this release therefore still analyses.
    """
    if not isinstance(acquisition, dict):
        raise AnalysisError(
            "INCOMPLETE_SPECTRUM", "No acquisition data was supplied."
        )

    illuminations = acquisition.get("illuminations")

    if isinstance(illuminations, dict):
        return {
            name: aggregation.aggregate_block(block)
            for name, block in illuminations.items()
        }

    # Legacy: one white spectrum, no repeats.
    raw = acquisition.get("raw", acquisition)

    require_spectrum(raw, "RAW spectrum")

    return {
        "white": {
            "spectrum": copy_channels(raw),
            "statistics": {
                "illumination": "white", "repeats": 1,
                "estimator": "single", "channels": {},
                "missing_channels": [], "rejected_total": 0,
                "mean_cv": None, "max_cv": None, "unstable_channels": [],
            },
            "acquisitions": [copy_channels(raw)],
        }
    }


def analyze(acquisition, references, database, sensor_settings=None,
            active_calibration=None, distance_mm=None):
    """
    Acquisition in, complete scientific result out.

    The pipeline, in order:

        aggregate repeats
          -> normalize against the ACTIVE calibration   (science)
          -> normalize WHITE against the LEGACY one     (database)
          -> measurement quality control
          -> compare every material on 3 metrics
          -> rank aggregation
          -> conservative interpretation

    `references` is the legacy calibration - the White/Dark the material
    library was built against, and the only one ever used to compare
    against it. `active_calibration` is the current full calibration and
    may be None, in which case the active normalization is simply not
    produced and the legacy comparison still works.
    """
    blocks = _blocks_from(acquisition)

    if "white" not in blocks:
        raise AnalysisError(
            "INCOMPLETE_SPECTRUM",
            "No WHITE illumination block: the legacy database comparison "
            "needs one.",
            {"illuminations": sorted(blocks.keys())},
        )

    raw = {name: block["spectrum"] for name, block in blocks.items()}
    stats = {name: block["statistics"] for name, block in blocks.items()}

    # -- legacy path: the ONLY one the database may be compared with ---
    legacy_normalized = normalize(
        raw["white"], references.dark, references.white
    )
    legacy_dark_corrected = dark_correct(raw["white"], references.dark)

    # -- active path: the new scientific record ------------------------
    active_normalized = {}
    active_error = None

    if active_calibration is not None:
        for name, spectrum in raw.items():
            try:
                active_normalized[name] = normalize(
                    spectrum,
                    active_calibration.dark,
                    active_calibration.white_for(name),
                )

            except Exception as error:
                active_error = "{}: {}".format(type(error).__name__, error)

    # -- quality, before anything is classified ------------------------
    report = quality.assess(
        legacy_normalized,
        references.white,
        references.dark,
        stats.get("white"),
        distance_mm,
        "white",
    )

    # -- comparison ----------------------------------------------------
    analysis_status = "OK"
    analysis_error = None
    usable = report.get("usable_channels") or list(CHANNELS)

    try:
        if database is not None:
            matches = metrics.compare_all(
                legacy_normalized, database.materials, usable
            )
            agreement = metrics.metric_agreement(matches)

        else:
            matches = []
            agreement = {}

        analysis = interpret(matches, agreement, report)

    except Exception as error:
        matches = []
        agreement = {}
        analysis_status = "FAILED"
        analysis_error = "{}: {}".format(type(error).__name__, error)
        analysis = {
            "best_match": None,
            "best_similarity": None,
            "second_match": None,
            "second_similarity": None,
            "score_difference": None,
            "status": "ANALYSIS_FAILED",
            "confidence": "NONE",
            "automatic_conclusion": (
                "The spectra were acquired successfully, but the "
                "comparison against the reference library failed ({}). "
                "The measured data is intact.".format(analysis_error)
            ),
            "error": analysis_error,
        }

    return {
        "measurement": {
            "protocol_version": acquisition.get("protocol_version", 1),
            "sample_schema_version": bd_config.SAMPLE_SCHEMA_VERSION,
            "wavelengths": channel_wavelengths(),

            # Primary data: what the sensor returned, per illumination.
            "raw": raw,

            # Derived, against the ACTIVE calibration - the scientific
            # record and the basis of any future 54-feature model.
            "active_normalized": active_normalized,
            "active_normalization_error": active_error,

            # Derived, against the LEGACY calibration - the ONLY thing
            # the existing material library may be compared with.
            "legacy_database_normalized": {"white": legacy_normalized},
            "dark_corrected": legacy_dark_corrected,

            # Kept under the historical key so older readers and the
            # spectrum table keep working.
            "normalized": legacy_normalized,

            "statistics": stats,
            "repeats": acquisition.get("repeats"),
            "temperatures": acquisition.get("temperatures"),
            "sensor_settings": sensor_settings
            or acquisition.get("sensor_settings"),
        },

        "calibration": {
            "legacy_database_calibration_id": references.calibration_id,
            "legacy_source": str(references.path),
            "active_calibration_id": (
                active_calibration.calibration_id
                if active_calibration is not None else None
            ),
            "equation": "R = (Sample - Dark) / (White - Dark)",
            "mode": "dual_normalization",
            "runtime_recalibration": "DISABLED",
            "zero_denominator_channels":
                references.zero_denominator_channels(),

            # The legacy White and Dark actually used, snapshotted so
            # the archived sample can be re-derived exactly.
            "dark_reference": copy_channels(references.dark),
            "white_reference": copy_channels(references.white),
        },

        "database": {
            "file": str(database.path) if database else None,
            "material_count": database.count() if database else 0,
            "metrics": ["cosine_similarity", "rmse", "pearson_r"],
            "ranking": "combined rank aggregation",
            "compared_against_all": True,
            "compared_channels": usable,
            "calibration_id": references.calibration_id,
            "science_version": config.SCIENCE_VERSION,
        },

        "quality": report,
        "reference_matches": matches,
        "metric_agreement": agreement,
        "analysis": analysis,
        "analysis_status": analysis_status,
        "analysis_error": analysis_error,
    }
