"""
The database registry — DB1, DB2 and DB3 as three independent sources.

One loader, one validator, one status report, three databases that are
never pooled. They answer different questions:

    DB1  MEASURED             this instrument, 18 bands, historical
    DB2  MEASURED             this instrument, 54 features, WHITE/UV/IR
    DB3  REFERENCE_PROJECTED  external laboratory spectra, projected

A cosine of 0.97 against DB1 means "this looks like something we
measured here". The same number against DB3 means "this looks like a
laboratory spectrum of that mineral, after passing it through a model of
our sensor". Those are different claims, so the registry keeps the
databases apart and the analysis scores each one separately.

Layer rule: BD must never import Measurements. This module loads and
validates; it computes no similarity.
"""

import json

from BD import config
from BD.channels import (
    AS7265X_18,
    AS7265X_54_MULTIILLUM,
    FeatureSpaceError,
    feature_count,
    feature_ids,
)
from BD.databases import DatabaseError, MaterialDatabase

# Status of one database, in increasing order of usefulness.
STATUS_MISSING = "MISSING"          # no files on disk at all
STATUS_EMPTY = "EMPTY"              # declared, no materials yet
STATUS_INVALID = "INVALID"          # present but failed validation
STATUS_READY = "READY"              # loaded and usable

# What kind of evidence a database provides. Never mix these.
MEASURED = "MEASURED"
REFERENCE_PROJECTED = "REFERENCE_PROJECTED"


DEFINITIONS = {
    "DB1": {
        "database_id": "DB1",
        "version": "measured18-v1",
        "evidence": MEASURED,
        "feature_space": AS7265X_18,
        "file": config.DB1_FILE,
        "protected": True,
        "description": "Historical session measured on this instrument. "
                       "18 spectral bands, one shared Dark and White.",
    },
    "DB2": {
        "database_id": "DB2",
        "version": "measured54-v1",
        "evidence": MEASURED,
        "feature_space": AS7265X_54_MULTIILLUM,
        "file": config.DB2_FILE,
        "protected": False,
        "description": "Materials measured on this instrument under "
                       "WHITE, UV and IR. 18 bands x 3 illuminations.",
    },
    "DB3": {
        "database_id": "DB3",
        "version": "reference-v1",
        "evidence": REFERENCE_PROJECTED,
        "feature_space": AS7265X_18,
        "file": config.DB3_FILE,
        "protected": False,
        "description": "External laboratory spectra projected into the "
                       "AS7265x bands. Never measured on this instrument.",
    },
}


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    except (OSError, ValueError):
        return None


def validate_materials(document, feature_space, evidence):
    """
    Structural validation of a materials document.

    Returns a list of problems; empty means the document is usable. This
    is SHAPE validation — whether the numbers are scientifically sound is
    Measurements' job, and whether they are trustworthy is provenance's.
    """
    problems = []

    if not isinstance(document, dict):
        return ["materials document is not an object"]

    materials = document.get("materials")

    if not isinstance(materials, dict):
        return ["materials document has no 'materials' object"]

    if not materials:
        return ["materials object is empty"]

    declared = document.get("feature_space", feature_space)

    if declared != feature_space:
        problems.append(
            "document declares feature space {} but this database is {}"
            .format(declared, feature_space)
        )

    expected_features = set(feature_ids(feature_space))

    for name, entry in materials.items():
        if not isinstance(entry, dict):
            problems.append("{}: entry is not an object".format(name))
            continue

        # Every record must say what kind of evidence it is, so a
        # projected spectrum can never be mistaken for a measured one.
        kind = entry.get("measurement_type")

        if kind is not None and kind != evidence:
            problems.append(
                "{}: measurement_type {} does not match the database's {}"
                .format(name, kind, evidence)
            )

        # REFERENCE_PROJECTED records must carry provenance. A reference
        # spectrum with no source is indistinguishable from an invention.
        if evidence == REFERENCE_PROJECTED:
            provenance = entry.get("provenance")

            if not isinstance(provenance, dict) or not provenance.get(
                "source_dataset"
            ):
                problems.append(
                    "{}: reference record has no source provenance"
                    .format(name)
                )

        channels = entry.get("channels")

        if isinstance(channels, dict):
            present = set(channels)
        elif isinstance(entry.get("features"), dict):
            present = set(entry["features"])
        else:
            present = {
                key for key in entry
                if key in expected_features
            }

        missing = expected_features - present

        if missing and len(missing) == len(expected_features):
            problems.append(
                "{}: no recognisable features for {}".format(
                    name, feature_space
                )
            )

    return problems


class DatabaseHandle:
    """One database: its definition, status and (if READY) its contents."""

    def __init__(self, key, definition):
        self.key = key
        self.definition = definition

        self.database_id = definition["database_id"]
        self.version = definition["version"]
        self.evidence = definition["evidence"]
        self.feature_space = definition["feature_space"]
        self.protected = definition["protected"]

        self.status = STATUS_MISSING
        self.problems = []
        self.materials = {}
        self.metadata = {}
        self.manifest = {}
        self.database = None

        self._load()

    def _load(self):
        path = self.definition["file"]

        if not path.exists():
            # A database that has not been created yet is a legitimate
            # state, not a failure: DB2 is empty until the hardware
            # measures it, DB3 until spectra are imported.
            self.status = STATUS_MISSING
            self.problems = [
                "{} does not exist yet".format(path.name)
            ]

            return

        document = _read_json(path)

        if document is None:
            self.status = STATUS_INVALID
            self.problems = [
                "{} is present but could not be parsed".format(path.name)
            ]

            return

        self.manifest = {
            key: value for key, value in document.items()
            if key != "materials"
        }

        if not document.get("materials"):
            self.status = STATUS_EMPTY
            self.problems = [
                document.get("why_empty")
                or "{} declares no materials yet".format(self.database_id)
            ]

            return

        problems = validate_materials(
            document, self.feature_space, self.evidence
        )

        if problems:
            self.status = STATUS_INVALID
            self.problems = problems

            return

        try:
            self.database = MaterialDatabase(
                path, layer=self.key, protected=self.protected
            )

        except DatabaseError as error:
            self.status = STATUS_INVALID
            self.problems = ["{}: {}".format(error.code, error.message)]

            return

        self.materials = self.database.materials
        self.metadata = self.database.material_metadata
        self.status = STATUS_READY
        self.problems = []

    @property
    def ready(self):
        return self.status == STATUS_READY

    def count(self):
        return len(self.materials)

    # ------------------------------------------------------------------
    # measured discriminability
    #
    # A database may record how well the sensor separates each material
    # class, measured by leave-one-out retrieval and written in by
    # research/analyse_discriminability.py. BD stores and serves that
    # number; deciding what to DO with it is the science layer's job.
    # ------------------------------------------------------------------

    RELIABILITY_UNRATED = "UNRATED"

    @property
    def discriminability(self):
        return (self.manifest or {}).get("discriminability") or {}

    def class_reliability(self, material):
        """
        How much a match naming this material is worth, per its class.

        Returns (level, detail). UNRATED means the database carries no
        measurement for that class - which is not the same as measuring
        it and finding it poor, so the caller must be able to tell them
        apart.
        """
        block = self.discriminability

        if not block:
            return self.RELIABILITY_UNRATED, {}

        material_class = (self.metadata.get(material) or {}).get(
            "material_class"
        )

        if not material_class:
            return self.RELIABILITY_UNRATED, {}

        entry = (block.get("by_class") or {}).get(material_class)

        if not entry:
            return self.RELIABILITY_UNRATED, {"material_class": material_class}

        detail = dict(entry)
        detail["material_class"] = material_class
        detail["analysis_version"] = block.get("analysis_version")

        return entry.get("level", self.RELIABILITY_UNRATED), detail

    def status_report(self):
        block = self.discriminability

        return {
            "key": self.key,
            "database_id": self.database_id,
            "version": self.version,
            "evidence": self.evidence,
            "feature_space": self.feature_space,
            "expected_features": feature_count(self.feature_space),
            "status": self.status,
            "material_count": self.count(),
            "protected": self.protected,
            "description": self.definition["description"],
            "problems": self.problems,
            "discriminability_analysis": block.get("analysis_version"),
            "classes_rated": len(block.get("by_class") or {}),
        }


class DatabaseRegistry:
    """
    All three databases, loaded once.

    Loaded once on purpose: an analysis compares a sample against every
    database, and re-reading them per comparison would be both slow and a
    source of inconsistency if a file changed mid-run.
    """

    def __init__(self, definitions=None):
        self.definitions = definitions or DEFINITIONS
        self.databases = {
            key: DatabaseHandle(key, definition)
            for key, definition in self.definitions.items()
        }

    def __getitem__(self, key):
        return self.databases[key]

    def get(self, key):
        return self.databases.get(key)

    def ready_databases(self):
        """Databases that can actually be compared against, in key order."""
        return {
            key: handle
            for key, handle in sorted(self.databases.items())
            if handle.ready
        }

    def compatible_with(self, feature_space):
        """
        Databases a measurement in this feature space may be compared to.

        A 54-feature measurement can also be compared against an 18-band
        library, because it CONTAINS its 18 WHITE bands — the analysis
        narrows it explicitly and says so. The reverse never happens.
        """
        usable = {}

        for key, handle in sorted(self.databases.items()):
            if not handle.ready:
                continue

            if handle.feature_space == feature_space:
                usable[key] = ("DIRECT", handle)

            elif (
                feature_space == AS7265X_54_MULTIILLUM
                and handle.feature_space == AS7265X_18
            ):
                usable[key] = ("PROJECTED_TO_18", handle)

        return usable

    def status_report(self):
        return {
            key: handle.status_report()
            for key, handle in sorted(self.databases.items())
        }

    def summary(self):
        """One line per database, for the operator's status screen."""
        lines = []

        for key, handle in sorted(self.databases.items()):
            lines.append(
                "{:<4} {:<24} {:<20} {:<8} {} materials".format(
                    key,
                    handle.database_id,
                    handle.feature_space,
                    handle.status,
                    handle.count(),
                )
            )

        return "\n".join(lines)
