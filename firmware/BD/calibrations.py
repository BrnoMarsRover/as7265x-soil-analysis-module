"""
The calibration database: ONE file, one source of truth.

PERSISTENCE ONLY. Building a calibration document and deciding whether it
is scientifically acceptable both moved to Science/calibration.py when
the layers were separated (see Documentation/ARCHITECTURE.md).

That split has one consequence worth stating plainly. `activate()` must
not promote a calibration that failed validation — but validation is
science, and BD -> Science is the forbidden dependency edge. So the
validator is INJECTED: the caller supplies it. BD still refuses to
activate anything the validator rejects, and BD still imports nothing
scientific.

WHY THIS IS ONE FILE NOW
------------------------
It used to be four, and three of them were persisted VIEWS of the
fourth:

    calibrations.json           the library
    calibration_active.json     which one is in force — a field the
                                library already had
    calibration_legacy.json     the frozen White/Dark DB1 was measured
                                against — a calibration, kept outside
                                the list of calibrations
    acquisition_profiles.json   the conditions those calibrations were
                                taken under, and the thing that decides
                                whether one may be applied to a
                                measurement at all

Four files that have to agree are four files that can disagree, and the
program had a read path for each of them. They are now one document:

    {
      "schema_version": 3,
      "storage_layout": "calibration-v2",
      "active_calibration_id": ...,
      "activated_at": ...,
      "legacy_calibration_id": "FREYA_COMPETITION_2026_CAL_V1",
      "calibrations":         [ every calibration, LEGACY included ],
      "acquisition_profiles": [ every set of conditions seen ],
      "provenance":           { what this was built from }
    }

LEGACY IS STILL A DIFFERENT SCIENTIFIC THING
--------------------------------------------
It is the immutable White/Dark that DB1 was measured against, and the
only calibration a DB1 comparison may ever use. That has not changed.
What changed is that being protected is now a property OF THE RECORD —
`kind: "LEGACY"`, `protected: true` — instead of a property of which
file it happened to live in. `save()`, `activate()` and every other
write refuse it by name.

A stored calibration is never rewritten and never deleted by this
module. Saving appends; activating changes one field.

Layer rule: BD must never import Science.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from BD import config
from BD.channels import CHANNELS, copy_channels, validate_spectrum

ILLUMINATIONS = ("white", "uv", "ir")

KIND_FULL = "FULL"
KIND_LEGACY = "LEGACY"


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
    """
    Atomic write, so an interrupted save cannot truncate the database.

    EVERY FAILURE BECOMES A CalibrationError.

    The atomic part was already right - temporary file, fsync, rename.
    What was missing is that a failure escaped as a raw OSError, and
    the screens catch CalibrationError. A full disk while saving a
    freshly measured calibration therefore took down the operator
    client with a traceback, and the calibration went with it: it is a
    multi-minute procedure with physical white and dark references in
    the carousel, and it cannot be recovered by restarting the
    program.

    The temporary file is removed on the way out, so a failing disk
    does not leave a trail of .tmp files beside the database.
    """
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(tmp, path)

    except OSError as error:
        try:
            os.unlink(tmp)

        except OSError:
            pass

        raise CalibrationError(
            "CALIBRATION_SAVE_ERROR",
            "Could not write {}: {}".format(path, error),
        )


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    except (OSError, ValueError):
        return None


# ----------------------------------------------------------------------
# record models
# ----------------------------------------------------------------------

class FullCalibration:
    """
    An active full-spectral calibration: Dark plus WHITE/UV/IR.

    A RECORD MODEL. Everything here is field access over a stored
    document — no thresholds, no judgements. Whether the numbers are
    scientifically acceptable is Science/calibration.py.
    """

    kind = KIND_FULL
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


class LegacyCalibration:
    """
    The one White and one Dark accepted before the competition.

    Normal operation never measures a new White or Dark through this
    class, and the operator is never asked to recalibrate it. Every
    comparison against DB1 is normalized against this same immutable
    pair, which is what makes DB1 mean anything at all.

    A NEW full spectral calibration does not replace this — both are
    records in the same database and both are used, for different
    databases.

    Single illumination: `white` is a flat 18-channel spectrum, not a
    dict keyed by illumination, because that is what was measured.
    """

    kind = KIND_LEGACY
    protected = True

    def __init__(self, document, path=None):
        self.document = document or {}
        self.path = Path(path) if path else None

        self.calibration_id = (
            self.document.get("calibration_id")
            or config.LEGACY_CALIBRATION_ID
        )

        self.white, self.white_missing = self._extract("white")
        self.dark, self.dark_missing = self._extract("dark")

        problems = []

        if self.white_missing:
            problems.append("white is missing channels {}".format(
                ",".join(self.white_missing)
            ))

        if self.dark_missing:
            problems.append("dark is missing channels {}".format(
                ",".join(self.dark_missing)
            ))

        if problems:
            raise CalibrationError(
                "REFERENCES_INCOMPLETE",
                "The LEGACY calibration in {} is incomplete: {}".format(
                    self.path, "; ".join(problems)
                ),
            )

    def _extract(self, key):
        values = self.document.get(key)

        if values is None:
            return {}, list(CHANNELS)

        missing = validate_spectrum(values)

        return copy_channels(values), missing

    def white_for(self, illumination):
        """
        LEGACY has one illumination and says so.

        Answering `white` for a UV request would silently normalize a UV
        acquisition against a white-light reference, which is not a
        calibration error the numbers would ever reveal.
        """
        if illumination != "white":
            raise CalibrationError(
                "CALIBRATION_INCOMPATIBLE",
                "The LEGACY calibration was measured under white "
                "illumination only; it has no '{}' reference.".format(
                    illumination
                ),
                {"illumination": illumination},
            )

        return self.white

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
            "file": str(self.path) if self.path else None,
            "calibration_id": self.calibration_id,
            "protected": True,
            "database": str(config.DB1_FILE),
            "illuminations": ["white"],
            "white_channels": len(CHANNELS) - len(self.white_missing),
            "dark_channels": len(CHANNELS) - len(self.dark_missing),
            "channels_required": len(CHANNELS),
            "zero_denominator_channels": self.zero_denominator_channels(),
            "read_only": True,
            "note": "Immutable. The only calibration ever used to compare "
                    "a measurement against DB1.",
        }


def summarize(document):
    """
    One line of the selection list, from the document itself.

    Enough to choose between two calibrations without opening either:
    when it was made, under what sensor settings, how many repeats it
    averaged, and what its validation said at the time.
    """
    settings = document.get("sensor_settings") or {}
    kind = document.get("kind", KIND_FULL)

    if kind == KIND_LEGACY:
        illuminations = ["white"]
    else:
        illuminations = [
            name for name in ILLUMINATIONS
            if (document.get("white_reference") or {}).get(name)
        ]

    return {
        "calibration_id": document.get("calibration_id"),
        "created_at": document.get("created_at"),
        "kind": kind,
        "validation": (document.get("validation") or {}).get("status"),
        "repeats": document.get("repeats"),
        "notes": document.get("notes"),
        "protected": bool(document.get("protected")),
        "sensor_settings": settings,
        "gain_x": settings.get("gain_x"),
        "integration_cycles": settings.get("integration_cycles"),
        "measurement_mode": settings.get("measurement_mode"),
        "illuminations": illuminations,
    }


# ----------------------------------------------------------------------
# the one document
# ----------------------------------------------------------------------

class CalibrationDatabase:
    """
    The single persistent calibration document, and the only writer of it.

    Both `CalibrationStore` (calibrations, active, legacy) and
    `AcquisitionProfileStore` (the conditions) are views over this. They
    are separate APIs because they answer separate questions; they share
    one file because a profile and the calibration taken under it are
    one subsystem, and two files would need keeping in step.

    Read-modify-write per operation. This is a single-process operator
    client writing a document of a few tens of kilobytes a handful of
    times per run; a lock would buy nothing and hide the fact that
    every write is atomic already.
    """

    def __init__(self, path=None, directory=None):
        if path is not None:
            self.path = Path(path)
            self.directory = self.path.parent

        else:
            self.directory = Path(directory or config.CALIBRATION_DIR)
            self.path = self.directory / config.CALIBRATION_FILE.name

        # What the migration found, for the report. None until a load
        # has happened; [] when the file was already canonical.
        self.migrated = None

    # -- the empty document --------------------------------------------

    def _empty(self):
        return {
            "schema_version": config.CALIBRATION_SCHEMA_VERSION,
            "storage_layout": config.CALIBRATION_STORAGE_LAYOUT,
            "updated_at": utc_now(),
            "active_calibration_id": None,
            "activated_at": None,
            "legacy_calibration_id": config.LEGACY_CALIBRATION_ID,
            "calibrations": [],
            "acquisition_profiles": [],
            "fingerprint_version": None,
        }

    # -- migration ------------------------------------------------------

    def _legacy_record(self, references):
        """
        The pre-consolidation calibration_legacy.json, as a record.

        Its content is copied VERBATIM. The wrapper around it - the id,
        the kind, the protected flag - is what the database needs to
        treat it as the calibration it always was; the two spectra are
        untouched, and their equality with the original file is what
        the migration test checks.
        """
        return {
            "schema_version": config.CALIBRATION_SCHEMA_VERSION,
            "calibration_id": config.LEGACY_CALIBRATION_ID,
            "kind": KIND_LEGACY,
            "protected": True,
            "read_only": True,
            "created_at": None,
            "white": references.get("white"),
            "dark": references.get("dark"),
            "illuminations": ["white"],
            "notes": "The White and Dark accepted before the competition, "
                     "and the only calibration a DB1 comparison may use. "
                     "Immutable: no write path in this module can reach "
                     "it.",
        }

    def _migrate(self):
        """
        Build the one document from whatever earlier layout is present.

        Reads, in order of how recent the layout is:

            calibrations.json           the library
            calibration_active.json     which one was in force
            calibration_legacy.json     the protected White/Dark
            acquisition_profiles.json   the conditions
            FREYA_FULL_SPECTRAL_CAL_*.json   one file per calibration,
                                        the layout before all of those

        Nothing is invented and nothing is dropped. The sources are
        deleted by `_retire_sources` only AFTER the new document has
        been written and read back successfully.
        """
        document = self._empty()
        sources = []
        imported = []

        library = _read_json(self.directory / (
            config.MIGRATION_CALIBRATION_LIBRARY.name
        ))

        if isinstance(library, dict):
            sources.append(config.MIGRATION_CALIBRATION_LIBRARY.name)

            for entry in library.get("calibrations") or []:
                if isinstance(entry, dict) and entry.get("calibration_id"):
                    document["calibrations"].append(dict(entry))
                    imported.append(entry["calibration_id"])

            document["active_calibration_id"] = library.get("active")
            document["activated_at"] = library.get("activated_at")

            if library.get("migrated_from"):
                document.setdefault("earlier_provenance", []).append(
                    library["migrated_from"]
                )

        # One file per calibration - the layout before the library.
        for path in sorted(
            self.directory.glob(config.MIGRATION_CALIBRATION_GLOB)
        ):
            entry = _read_json(path)

            if not isinstance(entry, dict):
                continue

            calibration_id = entry.get("calibration_id")

            if not calibration_id or calibration_id in imported:
                continue

            entry = dict(entry)
            entry["imported_from"] = path.name

            document["calibrations"].append(entry)
            imported.append(calibration_id)
            sources.append(path.name)

        # The active pointer. Only consulted when the library did not
        # already say - it never held anything the library did not.
        if not document["active_calibration_id"]:
            pointer = _read_json(
                self.directory
                / config.MIGRATION_CALIBRATION_POINTER.name
            )

            if isinstance(pointer, dict):
                sources.append(config.MIGRATION_CALIBRATION_POINTER.name)

                if pointer.get("calibration_id") in imported:
                    document["active_calibration_id"] = pointer[
                        "calibration_id"
                    ]
                    document["activated_at"] = pointer.get("activated_at")

        elif (self.directory
              / config.MIGRATION_CALIBRATION_POINTER.name).exists():
            sources.append(config.MIGRATION_CALIBRATION_POINTER.name)

        # The protected LEGACY White/Dark.
        references = _read_json(
            self.directory / config.MIGRATION_CALIBRATION_LEGACY.name
        )

        if isinstance(references, dict):
            sources.append(config.MIGRATION_CALIBRATION_LEGACY.name)
            document["calibrations"].append(
                self._legacy_record(references)
            )
            imported.append(config.LEGACY_CALIBRATION_ID)

        # The acquisition profiles.
        profiles = _read_json(
            self.directory / config.MIGRATION_ACQUISITION_PROFILES.name
        )

        if isinstance(profiles, dict):
            sources.append(config.MIGRATION_ACQUISITION_PROFILES.name)
            document["acquisition_profiles"] = list(
                profiles.get("profiles") or []
            )
            document["fingerprint_version"] = profiles.get(
                "fingerprint_version"
            )

        document["calibrations"].sort(
            key=lambda entry: (
                entry.get("kind") == KIND_LEGACY,
                entry.get("created_at") or "",
            ),
            reverse=True,
        )

        if sources:
            document["provenance"] = {
                "migrated_at": utc_now(),
                "from_layout": "calibrations.json + calibration_active.json "
                               "+ calibration_legacy.json + "
                               "acquisition_profiles.json",
                "sources": sources,
                "calibrations_imported": imported,
                "profiles_imported": len(document["acquisition_profiles"]),
                "note": "The source files were deleted after this document "
                        "was written and read back. Nothing reads them any "
                        "more, and keeping them would be a second source "
                        "of truth.",
            }

        self.migrated = sources

        return document

    def _retire_sources(self):
        """
        Delete the files the document was consolidated from.

        Called only after the new document has been written AND read
        back with its calibrations intact. A migration that half
        succeeded leaves the sources where they are, so it can simply
        be run again.
        """
        names = [
            config.MIGRATION_CALIBRATION_LIBRARY.name,
            config.MIGRATION_CALIBRATION_LEGACY.name,
            config.MIGRATION_CALIBRATION_POINTER.name,
            config.MIGRATION_ACQUISITION_PROFILES.name,
        ]

        paths = [self.directory / name for name in names]
        paths.extend(
            sorted(self.directory.glob(config.MIGRATION_CALIBRATION_GLOB))
        )

        removed = []

        for path in paths:
            try:
                if path.is_file():
                    path.unlink()
                    removed.append(path.name)

            except OSError:
                # A file that will not delete is not a reason to fail
                # the run: the canonical document is already written and
                # is the only thing anything reads.
                continue

        return removed

    # -- load / save ----------------------------------------------------

    def load(self):
        """The document, migrating the earlier layout once if needed."""
        document = _read_json(self.path)

        if isinstance(document, dict) and "calibrations" in document:
            self.migrated = self.migrated or []

            document.setdefault("calibrations", [])
            document.setdefault("acquisition_profiles", [])
            document.setdefault(
                "legacy_calibration_id", config.LEGACY_CALIBRATION_ID
            )

            if not isinstance(document["calibrations"], list):
                raise CalibrationError(
                    "CALIBRATION_BAD_SCHEMA",
                    "{} has no 'calibrations' list.".format(self.path),
                )

            return document

        document = self._migrate()

        if not (document["calibrations"] or document["acquisition_profiles"]):
            # A fresh install. Nothing to migrate and nothing worth
            # writing - an empty file on disk says less than no file.
            return document

        self.save(document)

        verified = _read_json(self.path)

        if (isinstance(verified, dict)
                and len(verified.get("calibrations") or [])
                == len(document["calibrations"])):
            self._retire_sources()

        return document

    def save(self, document):
        document["schema_version"] = config.CALIBRATION_SCHEMA_VERSION
        document["storage_layout"] = config.CALIBRATION_STORAGE_LAYOUT
        document["updated_at"] = utc_now()

        _write_json(self.path, document)

    # -- shared lookups -------------------------------------------------

    @staticmethod
    def find(document, calibration_id):
        for entry in document.get("calibrations") or []:
            if entry.get("calibration_id") == calibration_id:
                return entry

        return None

    def status(self):
        document = self.load()

        return {
            "file": str(self.path),
            "schema_version": document.get("schema_version"),
            "storage_layout": document.get("storage_layout"),
            "calibrations": len(document.get("calibrations") or []),
            "acquisition_profiles": len(
                document.get("acquisition_profiles") or []
            ),
            "active_calibration_id": document.get("active_calibration_id"),
            "legacy_calibration_id": document.get("legacy_calibration_id"),
            "migrated_from": (document.get("provenance") or {}).get("sources"),
        }


def read_legacy_calibration(path=None):
    """
    The protected White/Dark, READ-ONLY. No migration, no writes.

    For callers that want the numbers and nothing else - a report, a
    research script, a test. `CalibrationStore()` would also answer,
    and would also migrate an older layout on the way past, which
    means constructing one is a potential WRITE to the production
    tree. That is exactly what `Tests/software/audit/test_quality.py`
    refuses to let a test do, and it is right to.

    Raises rather than returning None when the calibration database is
    absent or holds no LEGACY record: a DB1 comparison without it is
    not a degraded comparison, it is a different measurement.
    """
    document = _read_json(Path(path or config.CALIBRATION_FILE))

    if not isinstance(document, dict):
        raise CalibrationError(
            "CALIBRATION_NOT_FOUND",
            "No calibration database at {}.".format(
                path or config.CALIBRATION_FILE),
        )

    record = CalibrationDatabase.find(
        document,
        document.get("legacy_calibration_id")
        or config.LEGACY_CALIBRATION_ID,
    )

    if record is None:
        raise CalibrationError(
            "REFERENCES_INCOMPLETE",
            "The calibration database holds no LEGACY White/Dark.",
        )

    return LegacyCalibration(record, path or config.CALIBRATION_FILE)


# ----------------------------------------------------------------------
# the calibrations view
# ----------------------------------------------------------------------

class CalibrationStore:
    """
    Every calibration in one database, plus which one is active.

    Saving appends and never overwrites: an ID that already exists is a
    bug, not something to replace silently. Activation changes one field
    and leaves every stored document untouched. The LEGACY record is
    refused by every write path by name.
    """

    def __init__(self, directory=None, path=None, database=None):
        self.database = database or CalibrationDatabase(
            path=path, directory=directory
        )
        self.directory = self.database.directory

    # -- the file ------------------------------------------------------

    @property
    def path(self):
        return self.database.path

    @property
    def migrated(self):
        return self.database.migrated

    def path_for(self, _calibration_id=None):
        """Every calibration lives in the one database file."""
        return self.database.path

    # -- writing --------------------------------------------------------

    def save(self, document):
        """
        Append a calibration to the database.

        Refuses to overwrite: an ID that already exists is a bug, not
        something to silently replace.
        """
        calibration_id = document.get("calibration_id")

        if not calibration_id:
            raise CalibrationError(
                "CALIBRATION_BAD_SCHEMA", "Calibration has no ID."
            )

        if calibration_id == config.LEGACY_CALIBRATION_ID:
            raise CalibrationError(
                "CALIBRATION_PROTECTED",
                "{} is the LEGACY calibration DB1 was measured against. "
                "It is immutable and cannot be written.".format(
                    calibration_id
                ),
            )

        data = self.database.load()

        if self.database.find(data, calibration_id) is not None:
            raise CalibrationError(
                "CALIBRATION_ALREADY_EXISTS",
                "{} is already in {}; calibrations are immutable.".format(
                    calibration_id, self.database.path
                ),
            )

        data["calibrations"].append(document)
        data["calibrations"].sort(
            key=lambda entry: (
                entry.get("kind") == KIND_LEGACY,
                entry.get("created_at") or "",
            ),
            reverse=True,
        )

        self.database.save(data)

        return self.database.path

    # -- reading --------------------------------------------------------

    def load(self, calibration_id):
        data = self.database.load()
        document = self.database.find(data, calibration_id)

        if document is None:
            raise CalibrationError(
                "CALIBRATION_NOT_FOUND",
                "No calibration named {} in {}.".format(
                    calibration_id, self.database.path
                ),
            )

        return self._model(document)

    def _model(self, document):
        if document.get("kind") == KIND_LEGACY:
            return LegacyCalibration(document, self.database.path)

        return FullCalibration(document, self.database.path)

    def count(self):
        """Selectable calibrations - LEGACY is not one of them."""
        return len(self._selectable(self.database.load()))

    @staticmethod
    def _selectable(data):
        return [
            entry for entry in data.get("calibrations") or []
            if entry.get("kind") != KIND_LEGACY
        ]

    def history(self):
        """
        Every calibration the operator may choose from, newest first.

        LEGACY is excluded: it is never activated, it is applied to DB1
        automatically and to nothing else, and offering it in a
        selection list would invite exactly the mistake it exists to
        make impossible. `legacy()` returns it by name.
        """
        data = self.database.load()
        active = data.get("active_calibration_id")

        entries = [summarize(entry) for entry in self._selectable(data)]

        entries.sort(key=lambda entry: entry.get("created_at") or "",
                     reverse=True)

        for entry in entries:
            entry["file"] = str(self.database.path)
            entry["active"] = entry["calibration_id"] == active

        return entries

    def legacy(self):
        """
        The protected White/Dark, or None if this database has none.

        Returned as a `LegacyCalibration`, which raises rather than
        answering a UV or IR request: it was measured under one lamp.
        """
        data = self.database.load()

        document = self.database.find(
            data,
            data.get("legacy_calibration_id")
            or config.LEGACY_CALIBRATION_ID,
        )

        if document is None:
            return None

        return LegacyCalibration(document, self.database.path)

    # -- active calibration --------------------------------------------

    def active_id(self):
        return self.database.load().get("active_calibration_id")

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

        if calibration_id == config.LEGACY_CALIBRATION_ID:
            raise CalibrationError(
                "CALIBRATION_PROTECTED",
                "The LEGACY calibration is applied to DB1 and to nothing "
                "else. It is never the active calibration, because a "
                "measurement normalized against it under UV or IR would "
                "be normalized against a white-light reference.",
            )

        data = self.database.load()
        document = self.database.find(data, calibration_id)

        if document is None:
            raise CalibrationError(
                "CALIBRATION_NOT_FOUND",
                "No calibration named {} in {}.".format(
                    calibration_id, self.database.path
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

        data["active_calibration_id"] = calibration_id
        data["activated_at"] = utc_now()

        self.database.save(data)

        return FullCalibration(document, self.database.path)

    def active(self):
        """The active calibration, or None if none has been chosen."""
        data = self.database.load()
        calibration_id = data.get("active_calibration_id")

        if not calibration_id:
            return None

        document = self.database.find(data, calibration_id)

        if document is None:
            return None

        return FullCalibration(document, self.database.path)

    def status(self):
        return self.database.status()
