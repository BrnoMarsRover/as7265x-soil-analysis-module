"""
Science configuration for the BD layer.

Runs on the main computer only. Nothing here ever reaches the ESP32,
and nothing here describes hardware: GPIO pins, gain and servo timing
live in firmware/ESP32/config.py.

Paths are derived from this file's own location, so the PC application
finds the protected data whatever directory it was started from.
"""

from pathlib import Path

SCIENCE_VERSION = "3.0.0"

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

# Identifies the single immutable calibration set for this competition
# version. Stored with every sample so a record can always be traced
# back to the references it was normalized against.
CALIBRATION_ID = "FREYA_COMPETITION_2026_CAL_V1"

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
