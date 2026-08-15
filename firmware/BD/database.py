"""
Reference material database and the fixed White/Dark references.

Both files are PROTECTED SCIENTIFIC DATA and are opened read-only.
There is deliberately no save/write path in this module: normal
operation must never be able to modify database.json or
references.json, and a bug cannot corrupt what it cannot open for
writing.

Pure data processing. Nothing here knows about I2C, serial ports,
the carousel or a sensor object.
"""

import json
import math

import config
from sample_analysis import CHANNELS, copy_channels, validate_spectrum


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
# fixed White / Dark
# ----------------------------------------------------------------------

class References:
    """
    The one White and one Dark accepted before the competition.

    Normal operation never measures a new White or Dark, and the
    operator is never asked to calibrate. Every sample is normalized
    against this same immutable pair, which is what makes two samples
    measured hours apart comparable.
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
                    "a measurement against database.json.",
        }


# ----------------------------------------------------------------------
# reference materials
# ----------------------------------------------------------------------

def cosine_similarity_percent(measured, reference):
    """
    Cosine similarity between two 18-channel vectors, as a percentage.

    This is a SPECTRAL SIMILARITY measure. It is not a probability, not
    a composition, and not an identification certainty.
    """
    dot = 0.0
    norm_measured = 0.0
    norm_reference = 0.0

    for channel in CHANNELS:
        a = float(measured.get(channel, 0.0) or 0.0)
        b = float(reference.get(channel, 0.0) or 0.0)

        dot += a * b
        norm_measured += a * a
        norm_reference += b * b

    if norm_measured == 0.0 or norm_reference == 0.0:
        return 0.0

    similarity = dot / (math.sqrt(norm_measured) * math.sqrt(norm_reference))

    return max(0.0, min(100.0, similarity * 100.0))


def _material_spectrum(entry):
    """
    The comparable spectrum of one library entry.

    Two shapes are accepted. The legacy one, which is what
    database.json holds today:

        "Material": {"A": 0.1, ..., "W": 0.2}

    and a richer one that a future remeasurement could produce without
    breaking anything:

        "Material": {"mean": {...}, "std": {...}, "n": 10,
                     "illumination": "white", "calibration_id": "..."}

    Only the mean is compared; the rest is carried for provenance.
    Nothing is fabricated - an entry that has no usable spectrum is
    reported as incomplete rather than filled in.
    """
    if not isinstance(entry, dict):
        return None, {}

    if "mean" in entry and isinstance(entry["mean"], dict):
        metadata = {
            key: entry[key] for key in
            ("std", "n", "illumination", "calibration_id")
            if key in entry
        }

        return entry["mean"], metadata

    return entry, {}


class MaterialDatabase:
    """Read-only library of reference material reflectance spectra."""

    def __init__(self, path=None):
        self.path = path or config.DATABASE_FILE

        raw = _load_json(self.path, "database.json")

        if not raw:
            raise DatabaseError(
                "DATABASE_EMPTY",
                "{} contains no reference materials.".format(self.path),
            )

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

    def compare(self, normalized):
        """
        Compare against EVERY material, best first.

        Deliberately not truncated to a top-N: the complete ranking is
        stored with the sample so a result can be re-examined later. The
        UI decides how many rows to show.
        """
        scored = [
            (name, cosine_similarity_percent(normalized, spectrum))
            for name, spectrum in self.materials.items()
        ]

        scored.sort(key=lambda entry: entry[1], reverse=True)

        return [
            {
                "rank": rank,
                "material": name,
                "similarity_percent": round(similarity, 2),
            }
            for rank, (name, similarity) in enumerate(scored, start=1)
        ]

    def status(self):
        return {
            "file": str(self.path),
            "material_count": self.count(),
            "metrics": ["cosine_similarity", "rmse", "pearson_r"],
            "calibration_id": self.calibration_id,
            "illumination": "white",
            "read_only": True,
            "note": "18 white-illumination reference features per "
                    "material. UV and IR are recorded on new samples but "
                    "have no reference data to compare against yet.",
        }
