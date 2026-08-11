# sample_analysis.py
# Spectrum bookkeeping, database comparison and automatic interpretation.
#
# Scientific constraint, deliberately enforced by the wording produced
# here: this module performs COMPARATIVE SPECTRAL CLASSIFICATION against
# a small reference library. It never claims chemical identification.
# The percentages are cosine similarity between 18-channel reflectance
# vectors - not composition, not probability, not certainty.

import config


# The 18 nominal AS7265x channels, in wavelength order.
CHANNELS = (
    "A", "B", "C", "D", "E", "F",
    "G", "H", "I", "J", "K", "L",
    "R", "S", "T", "U", "V", "W"
)

WAVELENGTHS = {
    "A": 410,
    "B": 435,
    "C": 460,
    "D": 485,
    "E": 510,
    "F": 535,
    "G": 560,
    "H": 585,
    "I": 610,
    "J": 645,
    "K": 680,
    "L": 705,
    "R": 730,
    "S": 760,
    "T": 810,
    "U": 860,
    "V": 900,
    "W": 940
}

# Decimal places kept when storing spectra. Enough to preserve the
# sensor resolution while keeping each record small on flash.
RAW_DECIMALS = 4
NORMALIZED_DECIMALS = 6

# Interpretation outcomes.
STATUS_STRONG = "STRONG_REFERENCE_MATCH"
STATUS_AMBIGUOUS = "AMBIGUOUS"
STATUS_WEAK = "WEAK_REFERENCE_MATCH"
STATUS_NO_DATABASE = "NO_DATABASE"


# ----------------------------------------------------------------------
# channel helpers
# ----------------------------------------------------------------------

def validate_channels(data):
    """
    Return the list of channels missing or unusable in a spectrum.

    An empty list means all 18 AS7265x channels are present and numeric.
    """
    missing = []

    if not isinstance(data, dict):
        return list(CHANNELS)

    for channel in CHANNELS:
        value = data.get(channel)

        if value is None:
            missing.append(channel)
            continue

        if isinstance(value, bool) or not isinstance(value, (int, float)):
            missing.append(channel)

    return missing


def copy_channels(data):
    """Full 18-channel float copy, defaulting anything missing to 0.0."""
    result = {}

    if data is None:
        data = {}

    for channel in CHANNELS:
        try:
            result[channel] = float(data.get(channel, 0.0))

        except (TypeError, ValueError):
            result[channel] = 0.0

    return result


def round_channels(data, decimals):
    """Rounded 18-channel copy, used when writing a record to flash."""
    result = {}

    if data is None:
        data = {}

    for channel in CHANNELS:
        try:
            result[channel] = round(
                float(data.get(channel, 0.0)),
                decimals
            )

        except (TypeError, ValueError):
            result[channel] = 0.0

    return result


def dark_correct(sample, dark, decimals=RAW_DECIMALS):
    """
    Per-channel Sample - Dark.

    Negative results are kept as they are. A channel reading below the
    stored dark reference is real information about noise or drift, and
    silently clamping it to zero would hide that from the operator.
    """
    result = {}

    for channel in CHANNELS:
        try:
            sample_value = float(sample.get(channel, 0.0))

        except (TypeError, ValueError):
            sample_value = 0.0

        try:
            dark_value = float(dark.get(channel, 0.0))

        except (TypeError, ValueError):
            dark_value = 0.0

        result[channel] = round(
            sample_value - dark_value,
            decimals
        )

    return result


def channel_wavelengths():
    """Channel-to-nanometre map stored alongside every measurement."""
    result = {}

    for channel in CHANNELS:
        result[channel] = WAVELENGTHS[channel]

    return result


# ----------------------------------------------------------------------
# database comparison
# ----------------------------------------------------------------------

def build_matches(database, normalized):
    """
    Compare the normalized spectrum against EVERY material in the
    database, sorted best first.

    Reuses the existing MaterialDatabase cosine-similarity implementation
    unchanged; only the result count is widened from the default top-N to
    the whole library.
    """
    if database is None:
        return []

    entries = getattr(database, "data", None)

    if not entries:
        return []

    raw_matches = database.find_matches(
        normalized,
        top_n=len(entries)
    )

    matches = []
    rank = 1

    for name, similarity in raw_matches:
        matches.append({
            "rank": rank,
            "material": name,
            "similarity_percent": round(similarity, 2)
        })

        rank += 1

    return matches


# ----------------------------------------------------------------------
# automatic interpretation
# ----------------------------------------------------------------------

def interpret(matches):
    """
    Produce a compact, conservative reading of the match list.

    The thresholds come from config.py and are heuristic competition
    support values, not validated scientific criteria. Because cosine
    similarity between non-negative reflectance vectors is high for
    almost any pair, the informative quantity is the GAP between the
    best and second-best candidate rather than the absolute score.
    """
    if not matches:
        return {
            "best_match": None,
            "best_match_score": None,
            "second_match": None,
            "second_match_score": None,
            "score_difference": None,
            "status": STATUS_NO_DATABASE,
            "confidence": "NONE",
            "automatic_conclusion": (
                "No reference material database was available, so no "
                "spectral comparison could be performed. The measured "
                "spectrum has been stored and can be compared later."
            ),
            "thresholds": _thresholds()
        }

    best = matches[0]
    best_name = best["material"]
    best_score = best["similarity_percent"]

    if len(matches) > 1:
        second = matches[1]
        second_name = second["material"]
        second_score = second["similarity_percent"]
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
                best_name,
                best_score,
                minimum
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
                best_name,
                best_score,
                second_name,
                second_score,
                difference,
                best_name,
                second_name
            )
        )

    else:
        status = STATUS_STRONG
        confidence = "HIGH"

        if second_name is None:
            conclusion = (
                "The measured spectrum shows {:.1f}% cosine similarity "
                "to {}, the only reference material in the current "
                "database. A single-entry database cannot distinguish "
                "between candidates.".format(
                    best_score,
                    best_name
                )
            )

        else:
            conclusion = (
                "The measured spectrum shows the highest cosine "
                "similarity to {} ({:.1f}%), ahead of {} ({:.1f}%) by "
                "{:.1f} percentage points. Within the current reference "
                "database the sample is most consistent with {}. This is "
                "a spectral similarity result, not a chemical "
                "identification.".format(
                    best_name,
                    best_score,
                    second_name,
                    second_score,
                    difference,
                    best_name
                )
            )

    return {
        "best_match": best_name,
        "best_match_score": best_score,
        "second_match": second_name,
        "second_match_score": second_score,
        "score_difference": difference,
        "status": status,
        "confidence": confidence,
        "automatic_conclusion": conclusion,
        "thresholds": _thresholds()
    }


def _thresholds():
    """Thresholds recorded with the result so old records stay readable."""
    return {
        "min_similarity_percent": config.MIN_SIMILARITY_PERCENT,
        "ambiguity_margin_percent": config.AMBIGUITY_MARGIN_PERCENT,
        "metric": "cosine_similarity",
        "note": (
            "Heuristic competition-support thresholds. Tune with "
            "experimental data. Similarity is not probability."
        )
    }


# ----------------------------------------------------------------------
# record assembly
# ----------------------------------------------------------------------

def build_record(
    sample_id,
    slot_id,
    timestamps,
    metadata,
    raw,
    dark_reference,
    normalized,
    matches,
    analysis,
    sensor_settings,
    database_material_count
):
    """Assemble the complete scientific record written to flash."""
    return {
        "sample_id": sample_id,
        "slot_id": slot_id,
        "firmware": {
            "name": config.FIRMWARE_NAME,
            "version": config.FIRMWARE_VERSION
        },
        "timestamps": timestamps,
        "metadata": metadata,
        "measurement": {
            "channels_nm": channel_wavelengths(),
            "raw": round_channels(raw, RAW_DECIMALS),
            "dark_corrected": dark_correct(raw, dark_reference),
            "normalized": round_channels(
                normalized,
                NORMALIZED_DECIMALS
            ),
            # Calibration provenance. The competition uses one immutable
            # reference set, so every sample points at the same source
            # rather than carrying its own white/dark copy.
            "calibration": {
                "mode": "fixed",
                "source": config.REFERENCES_FILE,
                "calibration_id": config.CALIBRATION_ID,
                "white_reference": "competition_fixed_white",
                "dark_reference": "competition_fixed_dark",
                "reference_file": config.REFERENCES_FILE,
                "white_key": "white",
                "dark_key": "dark",
                "equation": "R = (Sample - Dark) / (White - Dark)",
                "runtime_recalibration": "DISABLED"
            },
            "sensor_settings": sensor_settings
        },
        "reference_matches": matches,
        "analysis": analysis,
        "database": {
            "file": config.DATABASE_FILE,
            "material_count": database_material_count,
            "metric": "cosine_similarity",
            "compared_against_all": True
        }
    }


def sensor_settings():
    """Acquisition settings actually applied to the AS7265x."""
    gain = config.SENSOR_GAIN
    current = config.ONBOARD_LED_CURRENT

    return {
        "led_current": current,
        "led_current_ma": config.SENSOR_LED_CURRENT_NAMES.get(
            current,
            "unknown"
        ),
        "integration_cycles": config.SENSOR_INTEGRATION_CYCLES,
        "gain": gain,
        "gain_x": config.SENSOR_GAIN_NAMES.get(gain, "unknown"),
        "illumination": "onboard_white_led"
    }
