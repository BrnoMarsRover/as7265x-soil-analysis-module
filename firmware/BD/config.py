"""
Where scientific data lives.

BD remembers. This file names the files; it holds no thresholds and no
metric settings, which are scientific judgements and live in
Measurements/config.py.

    BD/data/DB1.json                 measured here, 18 bands, 23 materials
    BD/data/DB1_source.txt           the verbatim source DB1 is built from
    BD/data/DB2.json                 measured here, 54 features (WHITE/UV/IR)
    BD/data/DB3.json                 external spectra projected to our bands
    BD/data/calibration_legacy.json  the White/Dark DB1 was measured against
    BD/data/calibrations.json        every calibration ever made, and which
                                     one of them is active
    BD/data/samples.json             samples measured during a run

One file per thing. Each database carries its own metadata, provenance and
audit inside it - there are no side-car manifests to fall out of step with
the data they describe.

Layer rule: BD must never import Measurements.
"""

from pathlib import Path

BD_DIR = Path(__file__).resolve().parent
DATA_DIR = BD_DIR / "data"

SAMPLE_SCHEMA_VERSION = 3
CALIBRATION_SCHEMA_VERSION = 2
STORAGE_LAYOUT_VERSION = 5

# ----------------------------------------------------------------------
# THE THREE DATABASES
# ----------------------------------------------------------------------
# Three databases, permanently separate. They answer different questions
# and must never be pooled: a cosine of 0.97 against DB1 means "this looks
# like something we measured here", while the same number against DB3
# means "this looks like a laboratory spectrum, after modelling our
# sensor". Exactly one copy of each - no backups, no legacy duplicates.

DB1_FILE = DATA_DIR / "DB1.json"
DB2_FILE = DATA_DIR / "DB2.json"
DB3_FILE = DATA_DIR / "DB3.json"

# The verbatim historical record DB1 is generated from. Never edited; its
# SHA256 is stored inside DB1.json so the derivation stays checkable.
DB1_SOURCE = DATA_DIR / "DB1_source.txt"

# Historical name. "The material database" means DB1 until DB2 exists.
DATABASE_FILE = DB1_FILE

# ----------------------------------------------------------------------
# CALIBRATIONS
# ----------------------------------------------------------------------
# LEGACY   the White/Dark DB1 was measured against. Immutable, and the
#          only calibration ever used to compare against DB1.
# ACTIVE   a full Dark + WHITE/UV/IR calibration made by the operator,
#          used for the scientific record and quality control.
#
# A measurement is normalized BOTH ways, which is what lets the instrument
# be recalibrated without remeasuring the material library.

REFERENCES_FILE = DATA_DIR / "calibration_legacy.json"

# ONE library file holds every calibration ever made - each with its own
# timestamp, sensor settings, repeats, statistics and raw acquisitions -
# together with the id of the one currently in force. The operator picks
# from that list instead of being pushed into making a new calibration
# after every restart.
#
# It replaces the earlier layout of one file per calibration plus a
# separate calibration_active.json pointer, which spread one fact across
# two files and made "which calibrations do I have?" a directory listing.
CALIBRATION_LIBRARY_FILE = DATA_DIR / "calibrations.json"
CALIBRATION_LIBRARY_LAYOUT = "library-v1"

# Read once, on the first run after the upgrade, so calibrations made
# under the old layout are carried into the library rather than lost.
# Never written again. See BD/calibrations.py::CalibrationStore._migrate.
LEGACY_CALIBRATION_POINTER = DATA_DIR / "calibration_active.json"
CALIBRATION_DIR = DATA_DIR

LEGACY_CALIBRATION_ID = "FREYA_COMPETITION_2026_CAL_V1"
CALIBRATION_ID = LEGACY_CALIBRATION_ID
CALIBRATION_ID_PREFIX = "FREYA_FULL_SPECTRAL_CAL"

# ----------------------------------------------------------------------
# ACQUISITION PROFILES
# ----------------------------------------------------------------------
# HOW a measurement was made - sensor settings, illumination, geometry,
# procedure, hardware revision. Three different things that used to be
# muddled into one:
#
#   profile      the conditions       "16x gain, 100 cycles, 25 mA, this
#                                      chamber, this distance"
#   calibration  what the REFERENCE   "under those conditions the white
#                read under them       target read 2311 counts on R730"
#   measurement  what the SAMPLE      "under those conditions this soil
#                read under them       read 3116 counts on R730"
#
# A calibration is only valid for a measurement taken under the same
# profile. Applying one across profiles is a research operation, never a
# silent default. See Documentation/DECISION_ARCHITECTURE.md.
ACQUISITION_PROFILES_FILE = DATA_DIR / "acquisition_profiles.json"

# Bench names for materials that already exist in the libraries. A NAME
# table only - it can never create a material or change a spectrum.
OPERATOR_ALIASES_FILE = DATA_DIR / "operator_aliases.json"
ACQUISITION_PROFILE_SCHEMA_VERSION = 1

# ----------------------------------------------------------------------
# DECISION LEARNING DATABASE
# ----------------------------------------------------------------------
# The history of what was measured, what the Decision Model concluded and
# what the sample actually was. PC-side only - it never goes near the
# ESP32.
#
# SQLite rather than JSON because this one grows without bound and is
# queried by selection, join and aggregate: "every VERIFIED observation of
# a carbonate under profile P, excluding session S".
#
# It is NOT a fourth reference database. DB1 and DB2 say what a material
# looks like; this says what the system has SEEN and how often it was
# right. Nothing here may modify a measured reference spectrum.
DECISION_LEARNING_DIR = DATA_DIR / "decision_learning"
DECISION_LEARNING_DB = DECISION_LEARNING_DIR / "decision_learning.sqlite3"
DECISION_LEARNING_SCHEMA_VERSION = 1

# The verbatim, human-readable seed the database is built from. Kept
# beside the binary so the history stays auditable and rebuildable.
DECISION_LEARNING_SEED = DECISION_LEARNING_DIR / "seed_observations.json"

# ----------------------------------------------------------------------
# MEASURED SAMPLES
# ----------------------------------------------------------------------
# The run's scientific output, and the only file this system writes.
SAMPLES_FILE = DATA_DIR / "samples.json"
