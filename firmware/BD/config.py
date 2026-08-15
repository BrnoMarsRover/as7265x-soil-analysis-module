"""
Science configuration for the BD layer.

Runs on the main computer only. Nothing here ever reaches the ESP32,
and nothing here describes hardware: GPIO pins, gain and servo timing
live in firmware/ESP32/config.py.

Paths are derived from this file's own location, so the PC application
finds the protected data whatever directory it was started from.
"""

from pathlib import Path

SCIENCE_VERSION = "4.0.0"

# Bumped when the stored Sample record gains or changes fields.
SAMPLE_SCHEMA_VERSION = 3

# Bumped when the calibration file layout changes.
CALIBRATION_SCHEMA_VERSION = 2

BD_DIR = Path(__file__).resolve().parent

# ----------------------------------------------------------------------
# PROTECTED SCIENTIFIC DATA - READ ONLY
# ----------------------------------------------------------------------
# Neither file is ever written by this software. references.json holds
# the one fixed competition White and Dark; database.json holds the
# reference material spectra. Regenerating either would silently
# invalidate every measurement taken against it.

REFERENCES_FILE = BD_DIR / "references.json"
DATABASE_FILE = BD_DIR / "database.json"

# ----------------------------------------------------------------------
# THE TWO CALIBRATIONS
# ----------------------------------------------------------------------
# The material spectra in database.json were normalized against ONE
# specific White/Dark pair. That pair is therefore part of the database:
# re-normalizing the library against a newer White would silently change
# what every stored number means.
#
# So there are two calibrations, permanently:
#
#   LEGACY   references.json, id FREYA_COMPETITION_2026_CAL_V1.
#            Immutable. The ONLY calibration ever used to compare a
#            measurement against database.json.
#
#   ACTIVE   a full Dark + WHITE/UV/IR calibration created by the
#            operator. Used for the new 54-feature scientific record,
#            measurement QC and any future model.
#
# A new measurement is normalized BOTH ways. That is what lets the
# operator recalibrate the instrument without remeasuring the library.

LEGACY_CALIBRATION_ID = "FREYA_COMPETITION_2026_CAL_V1"

# Historical name, kept so older code and records still resolve.
CALIBRATION_ID = LEGACY_CALIBRATION_ID

# Immutable, one file per calibration. Nothing here is ever overwritten.
CALIBRATION_DIR = BD_DIR / "calibrations"

# Names the calibration currently in force. Small and rewritable; the
# calibration files it points at are not.
ACTIVE_CALIBRATION_POINTER = CALIBRATION_DIR / "active.json"

CALIBRATION_ID_PREFIX = "FREYA_FULL_SPECTRAL_CAL"

# ----------------------------------------------------------------------
# STORED PRECISION
# ----------------------------------------------------------------------
# Enough to preserve the sensor resolution without bloating a record.
RAW_DECIMALS = 4
NORMALIZED_DECIMALS = 6

# ----------------------------------------------------------------------
# AUTOMATIC INTERPRETATION THRESHOLDS
# ----------------------------------------------------------------------
# HEURISTIC COMPETITION-SUPPORT THRESHOLDS - NOT SCIENTIFICALLY VALIDATED.
#
# The metric is cosine similarity between 18-channel reflectance
# vectors. Reflectance is non-negative, so every material in the
# database tends to score high; the useful information is the DIFFERENCE
# between the top candidates, not the absolute number.
#
# Tune both against real measurements of known materials.

# Below this best-match score the result is reported as a weak match.
MIN_SIMILARITY_PERCENT = 85.0

# If best and second-best are closer than this many percentage points,
# the classification is reported as ambiguous.
AMBIGUITY_MARGIN_PERCENT = 1.5

# ----------------------------------------------------------------------
# MEASUREMENT QUALITY THRESHOLDS
# ----------------------------------------------------------------------
# Every one of these is a judgement call, not a physical constant. They
# exist here, named and commented, so that tuning them is a deliberate
# act rather than an edit buried in a function.

# --- illumination strength -------------------------------------------
# White minus Dark is the denominator of the reflectance. Below this the
# channel is not measuring anything: the lamp does not reach it, or the
# detector does not respond there.
MIN_DENOMINATOR = 1.0

# Below this the channel is weak but still arguably usable.
WEAK_DENOMINATOR = 5.0

# How many of the 18 channels may be weak before the whole measurement
# is only a warning, and before it fails outright.
MAX_WEAK_CHANNELS_WARNING = 3
MAX_WEAK_CHANNELS_FAIL = 6

# Fewer usable channels than this and there is nothing to classify.
MIN_VALID_CHANNELS = 12

# --- reflectance sanity ----------------------------------------------
# Reflectance above 1.0 means the sample returned more light than the
# white reference. A little is measurement uncertainty; a lot means the
# calibration no longer describes the optical geometry.
#
# Values are NEVER clamped - these only classify the result.
REFLECTANCE_WARNING_MAX = 1.15
REFLECTANCE_FAIL_MAX = 2.0

# Reflectance below this is more than noise around zero.
REFLECTANCE_WARNING_MIN = -0.05
REFLECTANCE_FAIL_MIN = -0.25

# Fraction of channels allowed outside the expected range.
MAX_FRACTION_OUT_OF_RANGE_WARNING = 0.15
MAX_FRACTION_OUT_OF_RANGE_FAIL = 0.35

# --- triad boundary --------------------------------------------------
# The 18 channels come from three separate detectors. F535->G560 and
# L705->R730 are the seams between them, so a mismatch in gain or
# illumination shows up there first as a step that no real spectrum has.
#
# Expressed as a ratio between the two channels either side. Real
# spectra do have slopes, so the thresholds are deliberately loose.
BOUNDARY_WARNING_RATIO = 3.0
BOUNDARY_FAIL_RATIO = 8.0

BOUNDARY_PAIRS = (("F", "G"), ("L", "R"))

# --- repeatability ---------------------------------------------------
# Coefficient of variation across the repeats of one channel.
CV_WARNING = 0.05
CV_FAIL = 0.20

# A CV is meaningless when the mean is essentially zero.
CV_MINIMUM_MEAN = 1.0

# How many channels may exceed CV_WARNING before it matters.
MAX_UNSTABLE_CHANNELS_WARNING = 3
MAX_UNSTABLE_CHANNELS_FAIL = 8

# --- outlier rejection -----------------------------------------------
# Median-absolute-deviation multiplier. Below the minimum sample count
# there is not enough data to call anything an outlier.
OUTLIER_MAD_THRESHOLD = 3.5
OUTLIER_MINIMUM_SAMPLES = 4

# --- optional distance gate ------------------------------------------
# A VL53L4CD is planned but not fitted. When a distance is supplied it
# is a PASS/FAIL gate only - reflectance is never "corrected" for it.
DISTANCE_EXPECTED_MIN_MM = 8.0
DISTANCE_EXPECTED_MAX_MM = 25.0

# ----------------------------------------------------------------------
# REFERENCE COMPARISON
# ----------------------------------------------------------------------
# Cosine similarity alone is not enough. Reflectance vectors are
# non-negative, so almost any two materials score 95-100%: cosine sees
# the shape and ignores the magnitude entirely. Two spectra of the same
# shape at completely different brightness are identical to it.
#
# So three metrics are computed for every material and their RANKS are
# combined. Ranks, not scores, because the three are not on comparable
# scales and inventing a weighted "confidence" would be exactly the kind
# of fake number this module is supposed to avoid.

# Rank weights for the combined ordering.
METRIC_WEIGHTS = {"cosine": 1.0, "rmse": 1.0, "pearson": 1.0}

# The metrics "agree" when their best candidates are the same material,
# or at least all inside this many places of the combined winner.
METRIC_AGREEMENT_RANK_TOLERANCE = 2

# A strong result needs the runner-up to be at least this far behind in
# combined rank score.
MIN_RANK_SEPARATION = 1.0
