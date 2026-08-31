"""
Where scientific data lives.

BD remembers. This file names the files; it holds no thresholds and no
metric settings, which are scientific judgements and live in
Science/config.py.

    BD/
    ├── calibration/  calibration.json  every calibration, the LEGACY
    │                                   White/Dark, which one is ACTIVE,
    │                                   and the profiles they were taken
    │                                   under
    ├── DB1/          DB1.json          18 bands, measured  PROTECTED
    ├── DB2/          DB2.json          54 features, measured  PROTECTED
    ├── DB3/          DB3.json          external spectra projected
    ├── models/       registry.json     model artifacts, and which one
    │                                   is ACTIVE
    ├── samples/      samples.json      the PC session and the PC archive
    └── training/     decision_learning.sqlite3
                                        observations, ground truth and
                                        predictions - OFFLINE only

ONE DIRECTORY PER THING it answers a question about, and ONE
AUTHORITATIVE FILE INSIDE IT. The layout used to be a single `data/`
folder with eleven files in it, which said nothing about which of them
were reference libraries, which were calibration, and which were the
run's own output. Splitting it into directories fixed that and left a
second problem behind: several of the directories held the same truth
more than once - an active-calibration pointer beside the library that
already recorded it, a JSON seed beside the database it had already
been imported into, a schema backup beside the archive.

A subsystem with two persistent files can have two answers. Every one
of those duplicates has been consolidated into the subsystem's own
canonical store, and the redundant files are DELETED by the migration
rather than left as a fallback read path.

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

SAMPLE_SCHEMA_VERSION = 6
CALIBRATION_SCHEMA_VERSION = 3
STORAGE_LAYOUT_VERSION = 7

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

# ONE FILE, ONE SUBSYSTEM.
#
# Calibration used to be four files that had to agree with each other:
# calibrations.json (the library), calibration_active.json (which one is
# in force), calibration_legacy.json (the frozen DB1 reference) and
# acquisition_profiles.json (the conditions a calibration is valid
# under). Three of those were persisted VIEWS of one truth - the
# active pointer duplicated a field already inside the library, and the
# profiles are the conditions the calibrations in the same file were
# taken under. Files that must be kept in step are files that can fall
# out of step.
#
#     {
#       "schema_version": 3,
#       "storage_layout": "calibration-v2",
#       "active_calibration_id": ...,
#       "legacy_calibration_id": "FREYA_COMPETITION_2026_CAL_V1",
#       "calibrations":         [ every calibration ever made,
#                                 including the protected LEGACY one ],
#       "acquisition_profiles": [ every set of conditions seen ],
#       "provenance":           { where this came from }
#     }
#
# LEGACY is a RECORD in that list, not a separate file. It is still a
# distinct scientific concept - the immutable White/Dark DB1 was
# measured against, and the only calibration ever used to compare
# against DB1 - and it is still refused every write. Being protected is
# a property of the record, not of which file it happens to sit in.
CALIBRATION_FILE = CALIBRATION_DIR / "calibration.json"
CALIBRATION_STORAGE_LAYOUT = "calibration-v2"

LEGACY_CALIBRATION_ID = "FREYA_COMPETITION_2026_CAL_V1"
CALIBRATION_ID_PREFIX = "FREYA_FULL_SPECTRAL_CAL"

ACQUISITION_PROFILE_SCHEMA_VERSION = 1

# ----------------------------------------------------------------------
# MIGRATION INPUTS ONLY
# ----------------------------------------------------------------------
# The four files the calibration database was consolidated from. They
# are READ once, on the first run after the upgrade, and DELETED after
# the consolidated file has been written and read back. Nothing in
# normal operation opens them, and no code may write them: a fallback
# read path that survives migration is a second source of truth wearing
# a different name.
MIGRATION_CALIBRATION_LIBRARY = CALIBRATION_DIR / "calibrations.json"
MIGRATION_CALIBRATION_LEGACY = CALIBRATION_DIR / "calibration_legacy.json"
MIGRATION_CALIBRATION_POINTER = CALIBRATION_DIR / "calibration_active.json"
MIGRATION_ACQUISITION_PROFILES = (
    CALIBRATION_DIR / "acquisition_profiles.json"
)

# One-file-per-calibration, the layout before even those. Still read on
# migration so an instrument upgraded from very far back keeps every
# calibration it ever made.
MIGRATION_CALIBRATION_GLOB = "{}*.json".format(CALIBRATION_ID_PREFIX)

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
DECISION_LEARNING_SCHEMA_VERSION = 3

# THE SEED IS NOT A RUNTIME STORE.
#
# `seed_observations.json` used to sit beside the database, and every
# row in it is also a row in the database - so BD/training/ held the
# same twenty-two observations twice, in two formats, with nothing to
# keep them in step. It is a BOOTSTRAP INPUT: generated by
# research/training/build_seed.py from the DB2 source snapshot, and
# imported once by research/training/import_seed.py.
#
# So it lives with the tooling that produces and consumes it,
# `research/training/data/seed_observations.json`, and the database
# records the seed's id and content hash in its own `meta` table. BD
# keeps ONE canonical learning store and the provenance travels inside
# it.

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

# TWO COLLECTIONS, ONE PERSISTENT. DIFFERENT LIFETIMES, AND ONLY ONE
# OF THEM IS ON THE DISK.
#
#   session   the working set of the run in progress. Created by
#             Prepare Sample, completed by Measure Sample. It lives in
#             THIS PROCESS'S MEMORY and nowhere else: closing the
#             client ends it, and samples.json never contains it.
#
#   archive   the permanent PC record. The only collection written to
#             samples.json, and NOTHING reaches it except an explicit
#             operator import: from the ESP32, or from the session
#             working set. A measurement never lands here on its own.
#
# WHY THE SESSION IS NOT ON DISK.
#
# Because "saved on the PC" is decided by the disk, not by the label.
# An earlier release persisted the session inside samples.json to
# protect RAW from a crash, reasoning that opening the serial port
# resets the ESP32 and takes the only other copy with it. That reason
# was measured and found false: SerialLink.open() holds DTR and RTS low
# specifically so the board does NOT reset, and the board's uptime is
# monotonic across repeated open/close cycles. A PC client can restart
# as often as it likes and the device's retained acquisition is still
# there to be imported.
#
# So persisting the session bought no protection that the ESP32 was not
# already giving, and cost the one property the split exists for: that
# a measurement becomes persistently stored PC science ONLY when the
# operator imports it. The device owns the un-imported measurement -
# durably, on its own filesystem - and the PC holds a transient working
# copy until the operator decides to keep it.
#
# They are two collections and not two stores, because a Sample may
# legitimately exist in both with different content and the import has
# to be able to see that - which is exactly what a conflict is.
SESSION_COLLECTION = "session"
ARCHIVE_COLLECTION = "archive"

SAMPLE_COLLECTIONS = (SESSION_COLLECTION, ARCHIVE_COLLECTION)
