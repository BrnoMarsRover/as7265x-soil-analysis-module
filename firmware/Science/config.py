"""
Scientific configuration for the Science layer.

Science understands. This file holds every number that represents a
scientific JUDGEMENT — quality thresholds, metric settings, interpretation
limits. Storage paths and schema versions are a different kind of decision
and live in BD/config.py.

Every threshold here is a judgement call, not a physical constant. They
are named and commented so that tuning one is a deliberate act rather than
an edit buried inside a function.

Layer rule: Science may consume BD records and schema constants.
BD must never import Science.
"""

SCIENCE_VERSION = "5.0.0"

# Bumped when an analysis result's shape or semantics change, so a stored
# record always says which engine produced it. Distinct from the
# measurement itself: the same acquisition may be re-analysed later by a
# newer engine without pretending it was remeasured.
ANALYSIS_VERSION = 2

# ----------------------------------------------------------------------
# STORED PRECISION
# ----------------------------------------------------------------------
# Enough to preserve the sensor resolution without bloating a record.
RAW_DECIMALS = 4
NORMALIZED_DECIMALS = 6

# ----------------------------------------------------------------------
# AUTOMATIC INTERPRETATION THRESHOLDS
# ----------------------------------------------------------------------
# HEURISTIC COMPETITION-SUPPORT THRESHOLDS — NOT SCIENTIFICALLY VALIDATED.
#
# Reflectance is non-negative, so every material in the library tends to
# score high on any angular metric; the useful information is the
# DIFFERENCE between the top candidates, not the absolute number.
#
# Measured on the real 22-material DB1 (Tests/science, reproducible):
#     45.0% of the 231 material pairs score cosine >= 0.99
#     82.3% score >= 0.95, median 0.9859
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

# --- reference conditioning ------------------------------------------
# How much room the reference left above the dark, in counts. ONE
# definition, used by preprocessing.conditioning and by
# channel_reliability alike: two modules with their own idea of "usable
# headroom" is how the same channel ends up usable in one report and
# unusable in the next.
#
#   below UNUSABLE  the quotient is not a measurement
#   below POOR      one count of noise moves the reflectance visibly
#   above GOOD      the division is well posed
REFERENCE_RANGE_UNUSABLE = 1.0
REFERENCE_RANGE_POOR = 10.0
REFERENCE_RANGE_GOOD = 100.0

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

# --- detector-die boundary -------------------------------------------
# The 18 channels come from three separate dies. F535->G560 and
# L705->R730 are the seams between them, so a mismatch in gain or
# illumination shows up there first as a step that no real spectrum has.
#
# Expressed as a ratio between the two channels either side. Real
# spectra do have slopes, so the thresholds are deliberately loose.
#
# NOT YET CALIBRATED against repeated measurements of known materials —
# see Documentation/DATABASES.md, H-QC-1.
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
# EXPECTED SENSOR CONFIGURATION
# ----------------------------------------------------------------------
# What a calibration must have been taken at to be compatible with this
# pipeline. Previously hardcoded inside validate_calibration(); named here
# so a deliberate change to ESP32/config.py has one obvious counterpart.
#
# These MIRROR ESP32/config.py and are duplicated on purpose: the host
# cannot import device firmware, and a calibration taken at different
# settings must be rejected rather than silently accepted. Tests assert
# the two stay in step.
EXPECTED_MEASUREMENT_MODE = 3      # one-shot
EXPECTED_INTEGRATION_CYCLES = 100
EXPECTED_GAIN = 2                  # 0b10 = 16x

# How many repeats the host REQUESTS from the device. A single reading is
# not a scientific measurement; calibration happens once and can afford to
# be slow, a rover sample cannot.
#
# Previously the client read these off BD/config.py via getattr() with a
# fallback — but they were only ever defined in ESP32/config.py, so the
# lookup always missed and silently used the fallback. Named here as the
# host-side default they actually are.
DEFAULT_CALIBRATION_REPEATS = 10
DEFAULT_SAMPLE_REPEATS = 3

# ----------------------------------------------------------------------
# REFERENCE COMPARISON — EVIDENCE FAMILIES
# ----------------------------------------------------------------------
# See Science/metrics/ and ARCHITECTURE.md. The short version:
#
# Several popular "different" metrics are mathematically dependent and
# must not be counted as independent opinions.
#
#   SAM = arccos(cosine)          -> strictly decreasing, RANK-IDENTICAL
#   RMSE = Euclidean / sqrt(N)    -> fixed N, RANK-IDENTICAL
#   cosine(x, kx) = 1             -> scale invariant
#   pearson(x, kx) = 1            -> ALSO scale invariant
#
# So cosine, SAM and Pearson are ALL blind to a pure brightness change.
# Only the magnitude family sees it. Grouping them as three equal votes
# silently weights shape 2:1 over magnitude — the exact failure the
# three-metric design was introduced to fix.
#
# Metrics are therefore grouped into families, one contribution per
# family, and the families are combined by RANK.

# Families that contribute to the combined ordering. Weights are
# deliberately equal and PROVISIONAL: no validation dataset yet exists
# that could justify anything else, and an invented weight is exactly the
# kind of fake confidence this module refuses to produce.
FAMILY_WEIGHTS = {
    "magnitude": 1.0,
    "angular": 1.0,
    "centered_shape": 1.0,
}

FAMILY_WEIGHTS_STATUS = "PROVISIONAL_UNVALIDATED"

# The families "agree" when their best candidates are the same material,
# or at least all inside this many places of the combined winner.
FAMILY_AGREEMENT_RANK_TOLERANCE = 2

# A strong result needs the runner-up to be at least this far behind in
# combined rank score.
MIN_RANK_SEPARATION = 1.0


# ----------------------------------------------------------------------
# MIXTURE ANALYSIS
# ----------------------------------------------------------------------
# 18 measurements can be reconstructed almost perfectly by a free
# combination of many library spectra. That result would be meaningless,
# so the problem is deliberately constrained.

# Never fit more than this many endmembers at once. Beyond a handful the
# solution stops being identifiable from 18 numbers.
MIXTURE_MAX_ENDMEMBERS = 4

# Two candidate spectra more similar than this carry the same
# information; fitting both makes the split between them arbitrary.
MIXTURE_COLLINEARITY_LIMIT = 0.995

# Contributions below this are noise in the fit, not components.
MIXTURE_MINIMUM_CONTRIBUTION = 0.02

# A mixture must reconstruct the spectrum at least this much better than
# the best single material before it may be called a mixture at all.
MIXTURE_MINIMUM_IMPROVEMENT = 0.15

# Whether to constrain the coefficients to sum to one. Off by default:
# the measured spectrum carries an overall brightness that a free scale
# absorbs, and forcing the sum would push that error into the components.
MIXTURE_SUM_TO_ONE = False
