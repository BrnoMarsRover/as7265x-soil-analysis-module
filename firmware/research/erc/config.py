"""
Mission configuration — where the science files live, and what the ERC
rules require numerically.

Two kinds of constant live here and they are deliberately separated:

    PATHS            our choice, changeable at will
    OFFICIAL LIMITS  the organiser's choice, changeable only by them

Anything in the second group is mirrored from the requirement registry so
that the rules exist in exactly one authoritative place. The mirror is
asserted by test, so the two cannot drift apart.

Layer rule: Science may import BD, Science and Science.decision.
Nothing below may import Science.
"""

from pathlib import Path

ERC_DIR = Path(__file__).resolve().parent
DATA_DIR = ERC_DIR / "data"
FIRMWARE_ROOT = ERC_DIR.parent.parent
REPO_ROOT = FIRMWARE_ROOT.parent

# ----------------------------------------------------------------------
# SOURCE DOCUMENTS
# ----------------------------------------------------------------------
# The authoritative competition inputs. Read-only, always. The map image
# in particular is the spatial source of truth and is NEVER rewritten -
# annotation produces a separate SVG overlay that references it.

COMPETITION_DIR = REPO_ROOT / "Photos" / "competition 20262027"

MARS_YARD_IMAGE = COMPETITION_DIR / "mars_yard_2026.png"
MARS_YARD_ZONES_IMAGE = COMPETITION_DIR / "MarsYard_zones.png"
SCIENCE_RULES_PDF = COMPETITION_DIR / "Science_Task_Rules_Updated.pdf"
SCIENCE_RUBRIC_XLSX = COMPETITION_DIR / "Exact_Science_Task_Rubric.xlsx"

# Deep Sampling stratigraphy. Recorded because it documents what the yard
# is physically made of, which is geological context. It belongs to a
# DIFFERENT sub-task and its sampling location is organiser-defined, so it
# is never evidence for Scientific Exploration.
SOIL_PROFILE_IMAGE = COMPETITION_DIR / "Soil.png"

# ----------------------------------------------------------------------
# SCIENCE DATA
# ----------------------------------------------------------------------

MARS_YARD_POINTS_FILE = DATA_DIR / "mars_yard_points.json"
REQUIREMENTS_FILE = DATA_DIR / "erc_science_requirements.json"
SCIENCE_PLAN_FILE = DATA_DIR / "science_plan.json"
PLANNED_SITES_FILE = DATA_DIR / "planned_sites.json"
SCIENCE_RUN_FILE = DATA_DIR / "science_run.json"
SOURCE_MANIFEST_FILE = DATA_DIR / "source_manifest.json"
AI_POLISH_PROMPT_FILE = DATA_DIR / "AI_POLISH_PROMPT.md"

# Generated output. Kept apart from inputs so that "delete everything the
# software produced" is one directory, not a hunt.
OUTPUT_DIR = DATA_DIR / "output"

# ----------------------------------------------------------------------
# SCHEMA VERSIONS
# ----------------------------------------------------------------------

MARS_YARD_SCHEMA_VERSION = 1
REQUIREMENTS_SCHEMA_VERSION = 1
SCIENCE_PLAN_SCHEMA_VERSION = 1
SCIENCE_RUN_SCHEMA_VERSION = 1
ANALYSIS_VERSION = "SCIENCE_ANALYSIS_V001"

# ----------------------------------------------------------------------
# THE FOUR SITES
# ----------------------------------------------------------------------
# The number four is a mission design decision, not a rule from the
# organiser: the rules never say how many sites to visit. It is fixed
# because the hypothesis is built around a four-site comparison, and
# changing it silently would invalidate the prediction criteria.

PLANNED_SITE_COUNT = 4

# Object types a scientific site may be placed on. Starting locations are
# excluded on purpose: S1-S9 are where a run begins, and choosing them as
# science targets would be choosing a number rather than a rock. The deep
# sampling point is excluded because it belongs to another sub-task.
SITE_ELIGIBLE_TYPES = ("LANDMARK", "NAVIGATION_WAYPOINT")

# How many measurements one sample should receive by default. Repeats are
# what make within-site spread measurable, and without spread no
# between-site difference can be called reproducible. Three is the
# smallest number that gives a spread at all; it is a floor, not a result.
DEFAULT_REPEATS_PER_SAMPLE = 3
MIN_REPEATS_FOR_SPREAD = 2

# ----------------------------------------------------------------------
# OFFICIAL ERC LIMITS
# ----------------------------------------------------------------------
# Mirrored from erc_science_requirements.json -> report_limits. The
# registry is authoritative; these names exist so call sites read well.
# Tests/test_science_requirements.py asserts they still agree.

PLANNING_SUBJECT_MAX_CHARS = 500
PLANNING_IMPORTANCE_MAX_CHARS = 1000
PLANNING_HYPOTHESIS_MAX_CHARS = 500
PLANNING_PREDICTIONS_MAX_CHARS = 1500
PLANNING_FIGURES_MAX = 1

EXPLORATION_REPORT_MAX_CHARS = 3000
EXPLORATION_FIGURES_MAX = 3
EXPLORATION_REQUIRED_ROVER_PHOTOGRAPHS = 3

USO_MAX_OBJECTS = 3
USO_DESCRIPTION_MAX_CHARS = 350

REPORT_DEADLINE_HOURS = 2.5

# Character counting is defined once. Every limit above is "with spaces",
# which is simply len() of the string - but naming it stops someone
# "helpfully" stripping whitespace first and under-counting.
def count_characters(text):
    """Characters including spaces, exactly as the rules count them."""
    if text is None:
        return 0

    return len(text)


# ----------------------------------------------------------------------
# TEAM IDENTITY
# ----------------------------------------------------------------------
# The rules require {[name of the team]_[report_name]} and deduct 5% if
# the naming is wrong. No team name is defined anywhere in this project,
# and inventing one would produce a confidently mis-named submission.
# It stays None until a human sets it.

TEAM_NAME = None
TEAM_NAME_STATUS = "NOT_CONFIGURED"
