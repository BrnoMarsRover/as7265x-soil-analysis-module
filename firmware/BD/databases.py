"""
Reference material databases and the legacy White/Dark references.

PERSISTENCE ONLY. This module loads, validates the structure of, and
reports on scientific data files. It computes no similarity, no
reflectance and no ranking — those moved to Measurements/metrics/ and
Measurements/analysis.py when the layers were separated (see Documentation/ARCHITECTURE.md).

Both files here are PROTECTED SCIENTIFIC DATA and are opened read-only.
There is deliberately no save/write path in this module: normal operation
must never be able to modify DB1 or the legacy references, and a bug
cannot corrupt what it cannot open for writing.

Layer rule: BD must never import Measurements.
"""

import json

from BD import config
from BD.channels import CHANNELS, copy_channels, validate_spectrum


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
# the legacy White / Dark
# ----------------------------------------------------------------------

class References:
    """
    The one White and one Dark accepted before the competition.

    Normal operation never measures a new White or Dark through this
    class, and the operator is never asked to recalibrate it. Every
    comparison against DB1 is normalized against this same immutable
    pair, which is what makes DB1 mean anything at all.

    A NEW full spectral calibration does not replace this — it lives
    alongside it. See BD/repositories/calibrations.py.
    """

    kind = "LEGACY"
    protected = True

    def __init__(self, path=None):
        self.path = path or config.REFERENCES_FILE
        self.calibration_id = config.LEGACY_CALIBRATION_ID

        data = _load_json(self.path, "references.json")

        self.white, self.white_missing = self._extract(data, "white")
        self.dark, self.dark_missing = self._extract(data, "dark")

        problems = []

        if self.white_missing:
            problems.append(
                "white is missing channels {}".format(
                    ",".join(self.white_missing)
                )
            )

        if self.dark_missing:
            problems.append(
                "dark is missing channels {}".format(
                    ",".join(self.dark_missing)
                )
            )

        if problems:
            raise DatabaseError(
                "REFERENCES_INCOMPLETE",
                "{}: {}".format(self.path, "; ".join(problems)),
            )

    @staticmethod
    def _extract(data, key):
        values = data.get(key)

        if values is None:
            return {}, list(CHANNELS)

        missing = validate_spectrum(values)

        return copy_channels(values), missing

    def zero_denominator_channels(self):
        """
        Channels where White - Dark is zero.

        Reflectance is undefined there; normalize() reports 0.0 for
        them, and the operator deserves to know which ones.
        """
        return [
            channel for channel in CHANNELS
            if (self.white[channel] - self.dark[channel]) == 0.0
        ]

    def status(self):
        return {
            "kind": self.kind,
            "file": str(self.path),
            "calibration_id": self.calibration_id,
            "protected": True,
            "database": str(config.DATABASE_FILE),
            "illuminations": ["white"],
            "white_channels": len(CHANNELS) - len(self.white_missing),
            "dark_channels": len(CHANNELS) - len(self.dark_missing),
            "channels_required": len(CHANNELS),
            "zero_denominator_channels": self.zero_denominator_channels(),
            "read_only": True,
            "note": "Immutable. The only calibration ever used to compare "
                    "a measurement against DB1.",
        }


# ----------------------------------------------------------------------
# reference materials
# ----------------------------------------------------------------------

def _material_spectrum(entry):
    """
    The comparable spectrum of one library entry, plus its provenance.

    Three shapes are accepted, so one loader serves all three databases
    and the old flat form still reads.

    DB1/DB2 — full record, raw measurements preserved per channel:

        {"channels": {"A": {"reflectance_as_supplied": 0.1, ...}, ...},
         "canonical_name": ..., "material_class": ...}

    Aggregated form, which a repeat-measured library produces:

        {"mean": {...}, "std": {...}, "n": 10}

    Flat form, the original layout:

        {"A": 0.1, ..., "W": 0.2}

    Only the comparable reflectance vector is returned for matching; the
    rest is carried as metadata. Nothing is fabricated — an entry with no
    usable spectrum is reported as incomplete rather than filled in.
    """
    if not isinstance(entry, dict):
        return None, {}

    # DB1/DB2 full record.
    if "channels" in entry and isinstance(entry["channels"], dict):
        spectrum = {}

        for channel, cell in entry["channels"].items():
            if not isinstance(cell, dict):
                continue

            # as_supplied is the historical number; recomputed is derived
            # from the raw measurements. Prefer the historical value so
            # the library means exactly what it meant when measured.
            value = cell.get("reflectance_as_supplied")

            if value is None:
                value = cell.get("reflectance")

            if value is not None:
                spectrum[channel] = value

        metadata = {
            key: entry[key] for key in (
                "canonical_name", "aliases", "chemical_formula",
                "material_class", "measurement_type", "calibration_id",
                "quality_flags", "acquisition_settings",
            )
            if key in entry
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
    Measurements/metrics/, which is handed `self.materials` and returns a
    ranking; this class deliberately has no opinion about similarity.
    """

    # Which database this instance represents. DB1 by default; the same
    # loader serves DB2 and DB3, so a result can always say which library
    # produced it and scores are never silently pooled across them.
    layer = "DB1"
    protected = True

    def __init__(self, path=None, layer=None, protected=None):
        self.path = path or config.DATABASE_FILE

        if layer is not None:
            self.layer = layer

        if protected is not None:
            self.protected = protected

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

        # The library was normalized against ONE White/Dark pair, and
        # comparing it against anything else changes what every stored
        # number means. Recorded here so a result can always say which
        # calibration its comparison was valid under.
        self.calibration_id = config.LEGACY_CALIBRATION_ID

    def count(self):
        return len(self.materials)

    def names(self):
        return sorted(self.materials.keys())

    def incomplete_materials(self):
        """
        Materials whose spectrum is not a full, numeric 18 channels.

        Reported rather than silently repaired: a reference entry with
        holes in it is a data problem for a human to look at, not
        something this module should paper over.
        """
        report = {}

        for name, spectrum in self.materials.items():
            missing = validate_spectrum(spectrum)

            if missing:
                report[name] = missing

        return report

    def status(self):
        return {
            "layer": self.layer,
            "file": str(self.path),
            "material_count": self.count(),
            "calibration_id": self.calibration_id,
            "illumination": "white",
            "read_only": True,
            "protected": self.protected,
            "note": "18 white-illumination reference features per "
                    "material. UV and IR are recorded on new samples but "
                    "have no reference data to compare against yet.",
        }
