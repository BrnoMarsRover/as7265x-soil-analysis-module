"""
Calibration storage: ONE library file holding every calibration made.

PERSISTENCE ONLY. Building a calibration document and deciding whether it
is scientifically acceptable both moved to Science/calibration.py when
the layers were separated (see Documentation/ARCHITECTURE.md).

That split has one consequence worth stating plainly. `activate()` must
not promote a calibration that failed validation — but validation is
science, and BD -> Science is the forbidden dependency edge. So the
validator is INJECTED: the caller supplies it. BD still refuses to
activate anything the validator rejects, and BD still imports nothing
scientific.

Storage
-------
    calibrations.json           every calibration + which one is active
    calibration_legacy.json     PROTECTED, separate, never listed here

One file, because "which calibrations do I have, and which am I using?"
is one question. The earlier layout answered it with a directory listing
plus a side-car pointer file, which is how an operator ended up making a
fresh calibration on every restart instead of reusing yesterday's.

The library holds the calibration DOCUMENTS in full — timestamp, sensor
settings, repeats, per-channel statistics and every individual
acquisition — so a stored calibration can be inspected, compared and
re-derived without any other file.

    {
      "schema_version": 2,
      "storage_layout": "library-v1",
      "active": "FREYA_FULL_SPECTRAL_CAL_20260817_173914",
      "activated_at": "...",
      "calibrations": [ <document>, <document>, ... ]
    }

A stored calibration is never rewritten and never deleted by this module.
Saving appends; activating changes one field.

Layer rule: BD must never import Science.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from BD import config
from BD.channels import CHANNELS, copy_channels, validate_spectrum

ILLUMINATIONS = ("white", "uv", "ir")


class CalibrationError(Exception):
    """A calibration is missing, malformed or incompatible."""

    def __init__(self, code, message, details=None):
        super().__init__(message)

        self.code = code
        self.message = message
        self.details = details or {}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def _write_json(path, payload):
    """Atomic write, so an interrupted save cannot truncate the library."""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")

    path.parent.mkdir(parents=True, exist_ok=True)

    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())

    os.replace(tmp, path)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    except (OSError, ValueError):
        return None


class FullCalibration:
    """
    An active full-spectral calibration: Dark plus WHITE/UV/IR.

    A RECORD MODEL. Everything here is field access over a stored
    document — no thresholds, no judgements. Whether the numbers are
    scientifically acceptable is Science/calibration.py.
    """

    kind = "FULL"
    protected = False

    def __init__(self, document, path=None):
        self.document = document
        self.path = Path(path) if path else None

        self.calibration_id = document.get("calibration_id")
        self.created_at = document.get("created_at")
        self.schema_version = document.get("schema_version")
        self.sensor_settings = document.get("sensor_settings") or {}
        self.repeats = document.get("repeats")
        self.notes = document.get("notes")

        self.dark = copy_channels(
            (document.get("dark") or {}).get("aggregated")
        )

        references = document.get("white_reference") or {}

        self.white = {
            name: copy_channels((references.get(name) or {}).get("aggregated"))
            for name in ILLUMINATIONS
        }

    def white_for(self, illumination):
        if illumination not in self.white:
            raise CalibrationError(
                "CALIBRATION_INCOMPATIBLE",
                "Active calibration has no white reference for "
                "'{}'.".format(illumination),
                {"illumination": illumination},
            )

        return self.white[illumination]

    def stored_validation(self):
        """The validation verdict recorded when this file was written."""
        return (self.document.get("validation") or {}).get("status")

    def status(self):
        return {
            "kind": self.kind,
            "calibration_id": self.calibration_id,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
            "file": str(self.path) if self.path else None,
            "protected": False,
            "repeats": self.repeats,
            "notes": self.notes,
            "sensor_settings": self.sensor_settings,
            "illuminations": list(ILLUMINATIONS),
            "dark_channels": len(CHANNELS) - len(
                validate_spectrum(self.dark)
            ),
            "white_channels": {
                name: len(CHANNELS) - len(validate_spectrum(self.white[name]))
                for name in ILLUMINATIONS
            },
            "channels_required": len(CHANNELS),
            "validation": self.stored_validation(),
        }


def summarize(document):
    """
    One line of the selection list, from the document itself.

    Enough to choose between two calibrations without opening either:
    when it was made, under what sensor settings, how many repeats it
    averaged, and what its validation said at the time.
    """
    settings = document.get("sensor_settings") or {}

    return {
        "calibration_id": document.get("calibration_id"),
        "created_at": document.get("created_at"),
        "kind": document.get("kind", "FULL"),
        "validation": (document.get("validation") or {}).get("status"),
        "repeats": document.get("repeats"),
        "notes": document.get("notes"),
        "protected": False,
        "sensor_settings": settings,
        "gain_x": settings.get("gain_x"),
        "integration_cycles": settings.get("integration_cycles"),
        "measurement_mode": settings.get("measurement_mode"),
        "illuminations": [
            name for name in ILLUMINATIONS
            if (document.get("white_reference") or {}).get(name)
        ],
    }


class CalibrationStore:
    """
    Every calibration in one library file, plus which one is active.

    Saving appends and never overwrites: an ID that already exists is a
    bug, not something to replace silently. Activation changes one field
    and leaves every stored document untouched.
    """

    def __init__(self, directory=None, library=None, legacy_pointer=None):
        self.directory = Path(directory or config.CALIBRATION_DIR)

        self.library_path = Path(
            library or (self.directory / config.CALIBRATION_LIBRARY_FILE.name)
        )

        # Read once, only if the library does not exist yet.
        self.legacy_pointer = Path(
            legacy_pointer
            or (self.directory / config.LEGACY_CALIBRATION_POINTER.name)
        )

        self.migrated = None

    # -- the library ---------------------------------------------------

    def _empty_library(self):
        return {
            "schema_version": config.CALIBRATION_SCHEMA_VERSION,
            "storage_layout": config.CALIBRATION_LIBRARY_LAYOUT,
            "updated_at": utc_now(),
            "active": None,
            "activated_at": None,
            "calibrations": [],
        }

    def _migrate(self):
        """
        Carry the previous layout into the library, once.

        Before this release each calibration was its own
        FREYA_FULL_SPECTRAL_CAL_*.json beside a calibration_active.json
        pointer. Those files are READ and left exactly where they are —
        nothing is moved or deleted — so an operator who upgrades keeps
        every calibration already made, including the active one.
        """
        library = self._empty_library()
        imported = []

        if self.directory.exists():
            pattern = "{}*.json".format(config.CALIBRATION_ID_PREFIX)

            for path in sorted(self.directory.glob(pattern)):
                document = _read_json(path)

                if not isinstance(document, dict):
                    continue

                if not document.get("calibration_id"):
                    continue

                document = dict(document)
                document["imported_from"] = str(path)

                library["calibrations"].append(document)
                imported.append(document["calibration_id"])

        pointer = _read_json(self.legacy_pointer)

        if isinstance(pointer, dict):
            active = pointer.get("calibration_id")

            if active in imported:
                library["active"] = active
                library["activated_at"] = pointer.get("activated_at")

        library["calibrations"].sort(
            key=lambda entry: entry.get("created_at") or "", reverse=True
        )

        if imported:
            library["migrated_from"] = {
                "layout": "one file per calibration + active pointer",
                "at": utc_now(),
                "imported": imported,
                "note": "The original files were left in place and are "
                        "no longer read.",
            }

        self.migrated = imported

        return library

    def _load_library(self):
        library = _read_json(self.library_path)

        if not isinstance(library, dict):
            library = self._migrate()

            if library["calibrations"]:
                # Only write on migration that actually found something.
                # A fresh install must not leave an empty file behind
                # that says nothing.
                _write_json(self.library_path, library)

            return library

        library.setdefault("calibrations", [])
        library.setdefault("active", None)

        if not isinstance(library["calibrations"], list):
            raise CalibrationError(
                "CALIBRATION_BAD_SCHEMA",
                "{} has no 'calibrations' list.".format(self.library_path),
            )

        return library

    def _save_library(self, library):
        library["schema_version"] = config.CALIBRATION_SCHEMA_VERSION
        library["storage_layout"] = config.CALIBRATION_LIBRARY_LAYOUT
        library["updated_at"] = utc_now()

        _write_json(self.library_path, library)

    def _find(self, library, calibration_id):
        for document in library["calibrations"]:
            if document.get("calibration_id") == calibration_id:
                return document

        return None

    # -- files ---------------------------------------------------------

    def path_for(self, _calibration_id=None):
        """Every calibration lives in the one library file."""
        return self.library_path

    def save(self, document):
        """
        Append a calibration to the library.

        Refuses to overwrite: an ID that already exists is a bug, not
        something to silently replace.
        """
        calibration_id = document.get("calibration_id")

        if not calibration_id:
            raise CalibrationError(
                "CALIBRATION_BAD_SCHEMA", "Calibration has no ID."
            )

        library = self._load_library()

        if self._find(library, calibration_id) is not None:
            raise CalibrationError(
                "CALIBRATION_ALREADY_EXISTS",
                "{} is already in {}; calibrations are immutable.".format(
                    calibration_id, self.library_path
                ),
            )

        library["calibrations"].append(document)
        library["calibrations"].sort(
            key=lambda entry: entry.get("created_at") or "", reverse=True
        )

        self._save_library(library)

        return self.library_path

    def load(self, calibration_id):
        library = self._load_library()
        document = self._find(library, calibration_id)

        if document is None:
            raise CalibrationError(
                "CALIBRATION_NOT_FOUND",
                "No calibration named {} in {}.".format(
                    calibration_id, self.library_path
                ),
            )

        return FullCalibration(document, self.library_path)

    def count(self):
        return len(self._load_library()["calibrations"])

    def history(self):
        """
        Every calibration in the library, newest first.

        This is the list the operator picks from, so each entry carries
        the settings it was made under rather than just an ID.
        """
        library = self._load_library()
        active = library.get("active")

        entries = [
            summarize(document) for document in library["calibrations"]
        ]

        entries.sort(key=lambda entry: entry.get("created_at") or "",
                     reverse=True)

        for entry in entries:
            entry["file"] = str(self.library_path)
            entry["active"] = entry["calibration_id"] == active

        return entries

    # -- active calibration --------------------------------------------

    def active_id(self):
        return self._load_library().get("active")

    def activate(self, calibration_id, validator):
        """
        Make a stored calibration the active one.

        `validator` is a callable taking the calibration document and
        returning {"status": PASS|WARNING|FAIL, ...} — normally
        Science.calibration.validate_calibration. It is required, not
        optional: a calibration that failed its checks must never become
        active, and BD cannot make that judgement itself without importing
        the science layer.
        """
        if not callable(validator):
            raise CalibrationError(
                "CALIBRATION_VALIDATOR_REQUIRED",
                "activate() requires a validator callable. Activating "
                "without validation would allow a calibration that failed "
                "its scientific checks to become active.",
            )

        library = self._load_library()
        document = self._find(library, calibration_id)

        if document is None:
            raise CalibrationError(
                "CALIBRATION_NOT_FOUND",
                "No calibration named {} in {}.".format(
                    calibration_id, self.library_path
                ),
            )

        result = validator(document)

        if result.get("status") == "FAIL":
            raise CalibrationError(
                "CALIBRATION_VALIDATION_FAILED",
                "{} did not pass validation and cannot be "
                "activated.".format(calibration_id),
                {"validation": result},
            )

        library["active"] = calibration_id
        library["activated_at"] = utc_now()

        self._save_library(library)

        return FullCalibration(document, self.library_path)

    def active(self):
        """The active calibration, or None if none has been chosen."""
        library = self._load_library()
        calibration_id = library.get("active")

        if not calibration_id:
            return None

        document = self._find(library, calibration_id)

        if document is None:
            return None

        return FullCalibration(document, self.library_path)
