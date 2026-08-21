"""
Where scientific data lives.

BD remembers. This file names the files; it holds no thresholds and no
metric settings, which are scientific judgements and live in
Science/config.py.

    BD/
    ├── calibration/     dark and white references, and the conditions
    │                    they were taken under
    ├── DB1/             measured here, 18 bands, 23 materials, legacy
    ├── DB2/             measured here, 54 features (WHITE/UV/IR)
    ├── DB3/             external spectra projected to our bands
    ├── training/        labelled records and the decision history, for
    │                    OFFLINE model development only
    ├── models/          validated model artifacts and the registry
    └── samples/         completed Sample scientific records

ONE DIRECTORY PER THING it answers a question about. The layout used to
be a single `data/` folder with eleven files in it, which said nothing
about which of them were reference libraries, which were calibration,
and which were the run's own output - a distinction that matters
enormously, because two of those are read-only scientific evidence and
one is the only thing this system writes.

BD IS THE AUTHORITATIVE RECORD STORE. Completed Sample, Measurement and
AnalysisRun records live in BD/samples/, not beside the PC client. The
PC orchestrates saving and reading them; it does not own them. A
laptop is a place a program runs, not a place scientific evidence
lives.

Each database carries its own metadata, provenance and audit INSIDE it
- there are no side-car manifests to fall out of step with the data
they describe.

Layer rule: BD must never import Science. Storage has to be able to
validate the shape of a record without depending on the layer that
interprets it.
"""

from pathlib import Path

BD_DIR = Path(__file__).resolve().parent

SAMPLE_SCHEMA_VERSION = 4
CALIBRATION_SCHEMA_VERSION = 2
STORAGE_LAYOUT_VERSION = 6

# ----------------------------------------------------------------------
# THE THREE DATABASES
# ----------------------------------------------------------------------
# Three databases, permanently separate. They answer different
# questions and must never be pooled: a cosine of 0.97 against DB1
# means "this looks like something we measured here", while the same
# number against DB3 means "this looks like a laboratory spectrum,
# after modelling our sensor". Exactly one copy of each - no backups,
# no legacy duplicates.

DB1_DIR = BD_DIR / "DB1"
DB2_DIR = BD_DIR / "DB2"
DB3_DIR = BD_DIR / "DB3"

DB1_FILE = DB1_DIR / "DB1.json"
DB2_FILE = DB2_DIR / "DB2.json"
DB3_FILE = DB3_DIR / "DB3.json"

# The verbatim historical record DB1 is generated from. Never edited;
# its SHA256 is stored inside DB1.json so the derivation stays
# checkable.
DB1_SOURCE = DB1_DIR / "DB1_source.txt"

# Bench names for materials that already exist in the libraries. A NAME
# table only - it can never create a material or change a spectrum.
OPERATOR_ALIASES_FILE = DB1_DIR / "operator_aliases.json"

# ----------------------------------------------------------------------
# CALIBRATION
# ----------------------------------------------------------------------
# LEGACY   the White/Dark DB1 was measured against. Immutable, and the
#          only calibration ever used to compare against DB1.
# ACTIVE   a full Dark + WHITE/UV/IR calibration made by the operator,
#          used for the scientific record and quality control.
#
# A measurement is normalized BOTH ways, which is what lets the
# instrument be recalibrated without remeasuring the material library.

CALIBRATION_DIR = BD_DIR / "calibration"

REFERENCES_FILE = CALIBRATION_DIR / "calibration_legacy.json"

# ONE library file holds every calibration ever made - each with its
# own timestamp, sensor settings, repeats, statistics and raw
# acquisitions - together with the id of the one currently in force.
# The operator picks from that list instead of being pushed into making
# a new calibration after every restart.
CALIBRATION_LIBRARY_FILE = CALIBRATION_DIR / "calibrations.json"
CALIBRATION_LIBRARY_LAYOUT = "library-v1"

# Read once, on the first run after an upgrade, so calibrations made
# under the older one-file-per-calibration layout are carried into the
# library rather than lost. Never written again.
LEGACY_CALIBRATION_POINTER = CALIBRATION_DIR / "calibration_active.json"

LEGACY_CALIBRATION_ID = "FREYA_COMPETITION_2026_CAL_V1"
CALIBRATION_ID_PREFIX = "FREYA_FULL_SPECTRAL_CAL"

# HOW a measurement was made - sensor settings, illumination, geometry,
# procedure, hardware revision. Three different things that are easily
# muddled into one:
#
#   profile      the conditions       "16x gain, 100 cycles, 25 mA,
#                                      this chamber, this distance"
#   calibration  what the REFERENCE   "under those conditions the white
#                read under them       target read 2311 counts on R730"
#   measurement  what the SAMPLE      "under those conditions this soil
#                read under them       read 3116 counts on R730"
#
# A calibration is only valid for a measurement taken under the same
# profile. Applying one across profiles is a research operation, never
# a silent default.
ACQUISITION_PROFILES_FILE = CALIBRATION_DIR / "acquisition_profiles.json"
ACQUISITION_PROFILE_SCHEMA_VERSION = 1

# ----------------------------------------------------------------------
# TRAINING DATA AND THE DECISION HISTORY
# ----------------------------------------------------------------------
# Labelled records for OFFLINE model development, and the history of
# what was measured, what the Decision Model concluded and what the
# sample actually turned out to be.
#
# NOTHING HERE TRAINS AT RUNTIME. A field sample the operator labelled
# is a candidate training record, not ground truth, and it becomes
# training data only through validation - which happens offline, in
# research/, and produces a versioned model artifact.
#
# SQLite rather than JSON because this one grows without bound and is
# queried by selection, join and aggregate: "every VERIFIED observation
# of a carbonate under profile P, excluding session S".
#
# It is NOT a fourth reference database. DB1 and DB2 say what a
# material looks like; this says what the system has SEEN and how often
# it was right. Nothing here may modify a measured reference spectrum.

TRAINING_DIR = BD_DIR / "training"
DECISION_LEARNING_DB = TRAINING_DIR / "decision_learning.sqlite3"
DECISION_LEARNING_SCHEMA_VERSION = 2

# The verbatim, human-readable seed the database is built from. Kept
# beside the binary so the history stays auditable and rebuildable.
DECISION_LEARNING_SEED = TRAINING_DIR / "seed_observations.json"

# ----------------------------------------------------------------------
# MODELS
# ----------------------------------------------------------------------
# Validated Decision Model artifacts and the registry that says which
# one is ACTIVE. A model is data: it is produced offline, versioned,
# and referenced by every conclusion it made, so a result from six
# months ago can be reproduced with the model that produced it.
MODELS_DIR = BD_DIR / "models"
MODEL_REGISTRY_FILE = MODELS_DIR / "registry.json"

# ----------------------------------------------------------------------
# COMPLETED SAMPLE RECORDS
# ----------------------------------------------------------------------
# The run's scientific output, and the only thing this system writes.
#
#     Sample  --1:N-->  Measurement  --1:N-->  AnalysisRun
#
# RAW inside a Measurement is written once and never again. A new
# Science version does not rewrite it; it adds an AnalysisRun.
SAMPLES_DIR = BD_DIR / "samples"
SAMPLES_FILE = SAMPLES_DIR / "samples.json"
