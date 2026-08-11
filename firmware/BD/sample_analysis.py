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
It never claims chemical identification. The percentages are cosine
similarity between 18-channel reflectance vectors - not composition,
not probability, not certainty.
"""

import config


# The 18 AS7265x channels, in wavelength order.
CHANNELS = (
    "A", "B", "C", "D", "E", "F",
    "G", "H", "I", "J", "K", "L",
    "R", "S", "T", "U", "V", "W",
)

WAVELENGTHS = {
    "A": 410, "B": 435, "C": 460, "D": 485, "E": 510, "F": 535,
    "G": 560, "H": 585, "I": 610, "J": 645, "K": 680, "L": 705,
    "R": 730, "S": 760, "T": 810, "U": 860, "V": 900, "W": 940,
}

# Interpretation outcomes.
STATUS_STRONG = "STRONG_REFERENCE_MATCH"
STATUS_AMBIGUOUS = "AMBIGUOUS"
STATUS_WEAK = "WEAK_REFERENCE_MATCH"
STATUS_NO_DATABASE = "NO_DATABASE"


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

def validate_spectrum(data):
    """
    Channels missing or unusable in a spectrum.

    An empty list means all 18 channels are present and numeric.
    """
    if not isinstance(data, dict):
        return list(CHANNELS)

    missing = []

    for channel in CHANNELS:
        value = data.get(channel)

        if value is None or isinstance(value, bool):
            missing.append(channel)

        elif not isinstance(value, (int, float)):
            missing.append(channel)

    return missing


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


def copy_channels(data):
    """Full 18-channel float copy, defaulting anything missing to 0.0."""
    result = {}
    data = data or {}

    for channel in CHANNELS:
        try:
            result[channel] = float(data.get(channel, 0.0))

        except (TypeError, ValueError):
            result[channel] = 0.0

    return result


def round_channels(data, decimals):
    """Rounded 18-channel copy, used when writing a record to disk."""
    return {
        channel: round(value, decimals)
        for channel, value in copy_channels(data).items()
    }


def channel_wavelengths():
    """Channel-to-nanometre map stored alongside every measurement."""
    return {channel: WAVELENGTHS[channel] for channel in CHANNELS}


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


def interpret(matches):
    """
    A compact, conservative reading of the full match list.

    Because cosine similarity between non-negative reflectance vectors
    is high for almost any pair, the informative quantity is the GAP
    between the best and second-best candidate, not the absolute score.
    """
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

    best_name = matches[0]["material"]
    best_score = matches[0]["similarity_percent"]

    if len(matches) > 1:
        second_name = matches[1]["material"]
        second_score = matches[1]["similarity_percent"]
        difference = round(best_score - second_score, 2)

    else:
        second_name = None
        second_score = None
        difference = None

    minimum = config.MIN_SIMILARITY_PERCENT
    margin = config.AMBIGUITY_MARGIN_PERCENT

    if best_score < minimum:
        status = STATUS_WEAK
        confidence = "LOW"
        conclusion = (
            "No strong spectral similarity was found in the current "
            "reference database. The closest reference is {} at {:.1f}% "
            "cosine similarity, which is below the {:.1f}% support "
            "threshold. The sample may not be sufficiently represented "
            "by the available reference materials.".format(
                best_name, best_score, minimum
            )
        )

    elif difference is not None and difference < margin:
        status = STATUS_AMBIGUOUS
        confidence = "MODERATE"
        conclusion = (
            "The measured spectrum shows the highest cosine similarity "
            "to {} ({:.1f}%). {} is the second closest reference "
            "({:.1f}%). Because the score difference is only {:.1f} "
            "percentage points, the classification is considered "
            "ambiguous. The sample is most consistent with {}/{} "
            "reference material within the current database.".format(
                best_name, best_score, second_name, second_score,
                difference, best_name, second_name,
            )
        )

    elif second_name is None:
        status = STATUS_STRONG
        confidence = "HIGH"
        conclusion = (
            "The measured spectrum shows {:.1f}% cosine similarity to "
            "{}, the only reference material in the current database. A "
            "single-entry database cannot distinguish between "
            "candidates.".format(best_score, best_name)
        )

    else:
        status = STATUS_STRONG
        confidence = "HIGH"
        conclusion = (
            "The measured spectrum shows the highest cosine similarity "
            "to {} ({:.1f}%), ahead of {} ({:.1f}%) by {:.1f} percentage "
            "points. Within the current reference database the sample is "
            "most consistent with {}. This is a spectral similarity "
            "result, not a chemical identification.".format(
                best_name, best_score, second_name, second_score,
                difference, best_name,
            )
        )

    return {
        "best_match": best_name,
        "best_similarity": best_score,
        "second_match": second_name,
        "second_similarity": second_score,
        "score_difference": difference,
        "status": status,
        "confidence": confidence,
        "automatic_conclusion": conclusion,
        "thresholds": _thresholds(),
    }


# ----------------------------------------------------------------------
# the whole pipeline, in one call
# ----------------------------------------------------------------------

def analyze(raw, references, database, sensor_settings=None):
    """
    RAW spectrum in, complete scientific result out.

    The single entry point used by both Measure Sample and Sensor Test,
    so a passing sensor test really does exercise the measurement
    pipeline.

        validate -> C = S - D -> R = (S-D)/(W-D) -> compare all -> interpret

    A database failure must not cost us the spectrum: the comparison is
    isolated, and dark_corrected/normalized are returned either way with
    analysis_status = FAILED.
    """
    require_spectrum(raw, "RAW spectrum")

    dark_corrected = dark_correct(raw, references.dark)
    normalized = normalize(raw, references.dark, references.white)

    analysis_status = "OK"
    analysis_error = None

    try:
        matches = database.compare(normalized) if database else []
        analysis = interpret(matches)

    except Exception as error:
        matches = []
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
                "The spectrum was acquired successfully, but the "
                "comparison against the reference database failed ({}). "
                "The measured data is intact.".format(analysis_error)
            ),
            "error": analysis_error,
        }

    return {
        "measurement": {
            "wavelengths": channel_wavelengths(),
            "raw": round_channels(raw, config.RAW_DECIMALS),
            "dark_corrected": dark_corrected,
            "normalized": normalized,
            "sensor_settings": sensor_settings,
        },
        "calibration": {
            "calibration_id": references.calibration_id,
            "source": str(references.path),
            "equation": "R = (Sample - Dark) / (White - Dark)",
            "mode": "fixed_stored_references",
            "runtime_recalibration": "DISABLED",
            "zero_denominator_channels":
                references.zero_denominator_channels(),
        },
        "database": {
            "file": str(database.path) if database else None,
            "material_count": database.count() if database else 0,
            "metric": "cosine_similarity",
            "compared_against_all": True,
            "science_version": config.SCIENCE_VERSION,
        },
        "reference_matches": matches,
        "analysis": analysis,
        "analysis_status": analysis_status,
        "analysis_error": analysis_error,
    }
