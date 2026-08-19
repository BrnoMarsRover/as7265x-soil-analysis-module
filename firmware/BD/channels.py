"""
The 18 AS7265x channels — the domain vocabulary of the whole system.

This lives in BD/schemas rather than in Measurements for one structural
reason: BD must be able to validate the SHAPE of a stored record (are all
18 channels present and numeric?) without importing Measurements, because
BD -> Measurements is the forbidden dependency edge. Every other layer may
import BD, so putting the vocabulary here means it is defined exactly
once.

`Measurements/channels.py` re-exports these names so scientific code can
say `from BD.channels import CHANNELS` and read naturally. It is
a re-export, not a second definition.

Imports nothing, so it can never participate in a cycle.
"""

# In wavelength order. The letters are the AS7265x channel names; the
# gap between L and R is the sensor's own, not a mistake.
CHANNELS = (
    "A", "B", "C", "D", "E", "F",
    "G", "H", "I", "J", "K", "L",
    "R", "S", "T", "U", "V", "W",
)

# Nominal channel centres in nanometres, from the AS7265x documentation.
# Nominal: these are the manufacturer's stated centres, not a measured
# per-device calibration. See research/AS7265X_RESEARCH.md.
WAVELENGTHS = {
    "A": 410, "B": 435, "C": 460, "D": 485, "E": 510, "F": 535,
    "G": 560, "H": 585, "I": 610, "J": 645, "K": 680, "L": 705,
    "R": 730, "S": 760, "T": 810, "U": 860, "V": 900, "W": 940,
}

# Which of the three internal dies owns each channel. The seams between
# them are where a device mismatch shows up first.
CHANNEL_DEVICE = {
    "A": "uv", "B": "uv", "C": "uv", "D": "uv", "E": "uv", "F": "uv",
    "G": "visible", "H": "visible", "I": "visible",
    "J": "visible", "K": "visible", "L": "visible",
    "R": "nir", "S": "nir", "T": "nir",
    "U": "nir", "V": "nir", "W": "nir",
}

# The three illumination conditions a full acquisition uses. An
# observation is (channel, illumination) — 18 x 3 = 54 candidate
# observations, NOT 54 wavelengths.
ILLUMINATIONS = ("white", "uv", "ir")


# ----------------------------------------------------------------------
# FEATURE SPACES
#
# The sensor has 18 detectors. It does NOT have 54 wavelengths. What it
# can produce is 18 bands measured under each of three illuminations —
# 18 x 3 = 54 FEATURES, which is a different claim.
#
# Confusing the two is the mistake this module exists to prevent. A
# DB1 spectrum (18 values, one unrecorded illumination) and a DB2
# spectrum (54 values, three known illuminations) are not comparable, and
# a comparison between them must FAIL rather than silently line up the
# first 18 numbers of each.
#
# Every database, every measurement and every comparison declares its
# feature space, and the validator refuses mismatches.
# ----------------------------------------------------------------------

AS7265X_18 = "AS7265X_18"
AS7265X_54_MULTIILLUM = "AS7265X_54_MULTIILLUM"

FEATURE_SPACES = (AS7265X_18, AS7265X_54_MULTIILLUM)


class FeatureSpaceError(Exception):
    """Two feature spaces were compared that cannot be compared."""

    def __init__(self, code, message, details=None):
        super().__init__(message)

        self.code = code
        self.message = message
        self.details = details or {}


def feature_ids(feature_space):
    """
    The ordered feature identifiers of a space.

    AS7265X_18            -> ("A", ..., "W")
    AS7265X_54_MULTIILLUM -> ("white:A", ..., "ir:W")

    Order is fixed and part of the contract: a database stored in one
    order and compared in another would silently produce nonsense.
    """
    if feature_space == AS7265X_18:
        return CHANNELS

    if feature_space == AS7265X_54_MULTIILLUM:
        return tuple(
            "{}:{}".format(illumination, channel)
            for illumination in ILLUMINATIONS
            for channel in CHANNELS
        )

    raise FeatureSpaceError(
        "UNKNOWN_FEATURE_SPACE",
        "Unknown feature space {!r}. Known: {}.".format(
            feature_space, ", ".join(FEATURE_SPACES)
        ),
        {"feature_space": feature_space},
    )


def feature_count(feature_space):
    return len(feature_ids(feature_space))


def split_feature(feature_id):
    """('white:A') -> ('white', 'A'); ('A') -> (None, 'A')."""
    if ":" in feature_id:
        illumination, channel = feature_id.split(":", 1)

        return illumination, channel

    return None, feature_id


def require_compatible(left, right, what="comparison"):
    """
    Refuse to compare two different feature spaces.

    This is the guard that stops an 18-value spectrum being matched
    against a 54-value library by index. There is no automatic
    conversion: a DB1 record genuinely does not contain the UV and IR
    measurements, and pretending otherwise would invent data.
    """
    if left == right:
        return left

    raise FeatureSpaceError(
        "FEATURE_SPACE_INCOMPATIBLE",
        "{} needs matching feature spaces, but got {} and {}. An 18-band "
        "measurement does not contain the UV and IR features a 54-feature "
        "library expects, and they cannot be derived.".format(
            what, left, right
        ),
        {"left": left, "right": right, "operation": what},
    )


def project_to_18(features, feature_space, illumination="white"):
    """
    The 18-band WHITE view of a spectrum, for comparison against DB1/DB3.

    A 54-feature measurement legitimately CONTAINS its 18 WHITE bands, so
    narrowing is real, not invented. The reverse is not true and is not
    offered.
    """
    if feature_space == AS7265X_18:
        return dict(features)

    if feature_space == AS7265X_54_MULTIILLUM:
        return {
            channel: features["{}:{}".format(illumination, channel)]
            for channel in CHANNELS
            if "{}:{}".format(illumination, channel) in features
        }

    raise FeatureSpaceError(
        "UNKNOWN_FEATURE_SPACE",
        "Cannot project from unknown feature space {!r}.".format(
            feature_space
        ),
    )


def combine_illuminations(blocks):
    """
    Build the 54-feature vector from one 18-channel spectrum per lamp.

    The exact inverse of project_to_18, and the only way a 54-feature
    measurement is ever assembled. Every illumination must be present and
    complete: a partial vector would compare 54 features against a
    library that expects all of them, with the missing ones reading as
    absent rather than as measured.

    Raises FeatureSpaceError naming what was missing.
    """
    missing = []

    for illumination in ILLUMINATIONS:
        spectrum = blocks.get(illumination) or {}

        if not spectrum:
            missing.append(illumination)

            continue

        for channel in CHANNELS:
            if not isinstance(spectrum.get(channel), (int, float)):
                missing.append("{}:{}".format(illumination, channel))

    if missing:
        raise FeatureSpaceError(
            "INCOMPLETE_FEATURE_SPACE",
            "Cannot build {} : {} feature(s) missing.".format(
                AS7265X_54_MULTIILLUM, len(missing)
            ),
            {"missing": missing[:12], "missing_count": len(missing)},
        )

    return {
        "{}:{}".format(illumination, channel):
            float(blocks[illumination][channel])
        for illumination in ILLUMINATIONS
        for channel in CHANNELS
    }


def channel_wavelengths():
    """Channel-to-nanometre map stored alongside every measurement."""
    return {channel: WAVELENGTHS[channel] for channel in CHANNELS}


# ----------------------------------------------------------------------
# structural helpers
#
# These are SHAPE checks, not science: "does this dictionary carry 18
# usable numbers". Deciding whether those numbers are scientifically
# trustworthy is Measurements/quality.py, which is a different question.
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
