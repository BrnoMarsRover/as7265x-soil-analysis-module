"""
The reference material databases: DB1, DB2 and DB3.

PERSISTENCE ONLY. This module loads, validates the structure of, and
reports on scientific data files. It computes no similarity, no
reflectance and no ranking — those moved to Science/metrics/ and
Science/analysis.py when the layers were separated (see Documentation/ARCHITECTURE.md).

Everything here is PROTECTED SCIENTIFIC DATA and is opened read-only.
There is deliberately no save/write path in this module: normal operation
must never be able to modify DB1, DB2 or DB3, and a bug cannot corrupt
what it cannot open for writing.

Layer rule: BD must never import Science.
"""

import json

from BD import config
from BD.channels import (
    AS7265X_18,
    AS7265X_54_MULTIILLUM,
    CHANNELS,
    ILLUMINATIONS,
    copy_channels,
    feature_ids,
    validate_spectrum,
)


class DatabaseError(Exception):
    """A protected data file is missing, unreadable or incomplete."""

    def __init__(self, code, message):
        super().__init__(message)

        self.code = code
        self.message = message


def _load_json(path, what):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)

    except FileNotFoundError:
        raise DatabaseError(
            "FILE_NOT_FOUND",
            "{} not found at {}.".format(what, path),
        )

    except OSError as error:
        raise DatabaseError(
            "FILE_UNREADABLE",
            "{} at {} could not be read: {}".format(what, path, error),
        )

    except ValueError as error:
        raise DatabaseError(
            "FILE_INVALID_JSON",
            "{} at {} is not valid JSON: {}".format(what, path, error),
        )

    if not isinstance(data, dict):
        raise DatabaseError(
            "FILE_INVALID_STRUCTURE",
            "{} at {} must contain a JSON object.".format(what, path),
        )

    return data


# ----------------------------------------------------------------------
# THE LEGACY WHITE / DARK IS NOT HERE
# ----------------------------------------------------------------------
# It used to be: a `References` class in this module, reading its own
# a `References` class reading its own file. It is a CALIBRATION -
# the immutable
# White/Dark DB1 was measured against - and it now lives where every
# other calibration lives, as a protected record inside the one
# calibration database:
#
#     BD.calibrations.CalibrationStore().legacy()  ->  LegacyCalibration
#
# Same numbers, same immutability, same `.white` / `.dark` /
# `.calibration_id` / `.status()`. One fewer file that could disagree
# with the calibration subsystem it belongs to.

# ----------------------------------------------------------------------
# reference materials
# ----------------------------------------------------------------------

_METADATA_KEYS = (
    "canonical_name", "aliases", "chemical_formula", "material_class",
    "measurement_type", "calibration_id", "quality_flags",
    "acquisition_settings", "display_name", "name_en", "name_cs",
    "operator_label", "material_id",
)


def _cell_reflectance(cell):
    """
    The comparable reflectance of one stored cell.

    as_supplied is the number the instrument printed; recomputed is
    derived from the raw counts beside it. The supplied value wins, so
    the library means exactly what it meant when it was measured — the
    recomputed one exists to prove the two agree, not to replace it.
    """
    if not isinstance(cell, dict):
        return None

    value = cell.get("reflectance_as_supplied")

    if value is None:
        value = cell.get("reflectance")

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None

    return value


def _material_spectrum(entry):
    """
    The comparable spectrum of one library entry, plus its provenance.

    Four shapes are accepted, so one loader serves all three databases
    and the old flat form still reads.

    DB1 — full record, raw measurements preserved per channel:

        {"channels": {"A": {"reflectance_as_supplied": 0.1, ...}, ...},
         "canonical_name": ..., "material_class": ...}

    DB2 — the same, but 54 features keyed <illumination>:<channel>:

        {"features": {"white:A": {"reflectance_as_supplied": 0.1, ...},
                      "uv:A": {...}, "ir:A": {...}, ...}}

    Aggregated form, which a repeat-measured library produces:

        {"mean": {...}, "std": {...}, "n": 10}

    Flat form, the original layout:

        {"A": 0.1, ..., "W": 0.2}

    Only the comparable reflectance vector is returned for matching; the
    rest is carried as metadata. Nothing is fabricated — an entry with no
    usable spectrum is reported as incomplete rather than filled in, and
    a feature whose reflectance is undefined (the reference equalled the
    dark there) is LEFT OUT rather than defaulted to zero. A missing
    feature is visible to the comparison; a fabricated 0.0 is not.
    """
    if not isinstance(entry, dict):
        return None, {}

    # DB1 full record (18 channels) and DB2 full record (54 features)
    # differ only in what the cells are keyed by, so one branch reads
    # both and the key set decides the feature space.
    for container in ("channels", "features"):
        cells = entry.get(container)

        if not isinstance(cells, dict):
            continue

        spectrum = {}

        for feature, cell in cells.items():
            value = _cell_reflectance(cell)

            if value is not None:
                spectrum[feature] = value

        metadata = {
            key: entry[key] for key in _METADATA_KEYS if key in entry
        }

        return (spectrum or None), metadata

    if "mean" in entry and isinstance(entry["mean"], dict):
        metadata = {
            key: entry[key] for key in
            ("std", "n", "illumination", "calibration_id")
            if key in entry
        }

        return entry["mean"], metadata

    return entry, {}


class MaterialDatabase:
    """
    Read-only library of reference material reflectance spectra.

    Loading and reporting only. Comparison against these spectra is
    Science/metrics/, which is handed `self.materials` and returns a
    ranking; this class deliberately has no opinion about similarity.
    """

    # Which database this instance represents. DB1 by default; the same
    # loader serves DB2 and DB3, so a result can always say which library
    # produced it and scores are never silently pooled across them.
    layer = "DB1"
    protected = True
    feature_space = AS7265X_18

    def __init__(self, path=None, layer=None, protected=None,
                 feature_space=None):
        self.path = path or config.DB1_FILE

        if layer is not None:
            self.layer = layer

        if protected is not None:
            self.protected = protected

        if feature_space is not None:
            self.feature_space = feature_space

        document = _load_json(self.path, str(self.path))

        if not document:
            raise DatabaseError(
                "DATABASE_EMPTY",
                "{} contains no reference materials.".format(self.path),
            )

        # A full database wraps its materials in a document carrying the
        # schema version, source hash and calibration ID. The flat form is
        # the material map itself.
        if isinstance(document.get("materials"), dict):
            raw = document["materials"]
            self.document_metadata = {
                key: value for key, value in document.items()
                if key != "materials"
            }
        else:
            raw = document
            self.document_metadata = {}

        self.materials = {}
        self.material_metadata = {}

        for name, entry in raw.items():
            spectrum, metadata = _material_spectrum(entry)

            if spectrum is None:
                continue

            self.materials[name] = spectrum
            self.material_metadata[name] = metadata

        if not self.materials:
            raise DatabaseError(
                "DATABASE_EMPTY",
                "{} holds no usable reference spectra.".format(self.path),
            )

        # Every stored number means "reflectance against THIS reference",
        # and comparing the library against a different one changes what
        # all of them mean. Recorded here so a result can always say
        # which calibration its comparison was valid under.
        #
        # DB1 was normalized against the immutable legacy White/Dark and
        # says nothing about it, so the legacy id is the correct answer
        # for it. DB2 was measured under a full calibration and names it
        # in the document; taking the legacy id there would label 54
        # features with the id of a calibration that never touched them.
        self.calibration_id = (
            self.document_metadata.get("calibration_id")
            or config.LEGACY_CALIBRATION_ID
        )

    def count(self):
        return len(self.materials)

    def names(self):
        return sorted(self.materials.keys())

    def incomplete_materials(self):
        """
        Materials whose spectrum is not a full, numeric set of features.

        Reported rather than silently repaired: a reference entry with
        holes in it is a data problem for a human to look at, not
        something this module should paper over. What counts as complete
        depends on the feature space — 18 channels for DB1 and DB3, 54
        (illumination, channel) pairs for DB2.
        """
        report = {}
        expected = feature_ids(self.feature_space)

        for name, spectrum in self.materials.items():
            missing = [
                feature for feature in expected
                if not isinstance(spectrum.get(feature), (int, float))
                or isinstance(spectrum.get(feature), bool)
            ]

            if missing:
                report[name] = missing

        return report

    def status(self):
        multi = self.feature_space == AS7265X_54_MULTIILLUM

        return {
            "layer": self.layer,
            "file": str(self.path),
            "material_count": self.count(),
            "calibration_id": self.calibration_id,
            "feature_space": self.feature_space,
            "illumination": (
                list(ILLUMINATIONS) if multi else "white"
            ),
            "read_only": True,
            "protected": self.protected,
            "note": (
                "54 reference features per material: 18 bands under each "
                "of WHITE, UV and IR, every one measured on this "
                "instrument."
                if multi else
                "18 white-illumination reference features per material."
            ),
        }
