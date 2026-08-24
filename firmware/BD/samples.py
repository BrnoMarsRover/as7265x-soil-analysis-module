"""
The authoritative store for completed scientific records.

THREE LAYERS, NEVER COLLAPSED

    Sample          the physical material
      1:N
    Measurement     one completed physical acquisition of it
      1:N
    AnalysisRun     one interpretation of that acquisition

Each layer answers a question the others cannot.

A **Sample** is soil in a slot: an identity, where it came from, why it
was collected. It is not a spectrum.

A **Measurement** is one acquisition: RAW counts grouped by
illumination, the instrument settings that produced them, the
calibration in force, and whether the acquisition succeeded. A Sample
may have several - measured three times, measured again after the
carousel was re-synchronized, measured a week later. The schema has
supported that from the first line, because a store where
`sample.measurement` is singular cannot be taught repeatability
afterwards without rewriting every record ever saved.

An **AnalysisRun** is what Science made of one Measurement. A
Measurement may have several of those too: Science v1 and Science v2,
DB2 before and after new references were added, a re-analysis after a
better calibration was chosen. They accumulate. Producing A002 never
touches A001, because the whole point of storing A001 was to be able to
see what was concluded at the time and why.

RAW IS IMMUTABLE

Once a Measurement with a successful acquisition is persisted, its
`raw` block is never written again. Not by normalization, not by a new
calibration, not by re-analysis, not by an edit. `add_measurement`
refuses to overwrite one and `add_analysis_run` cannot reach it. This
is enforced here rather than promised in a comment, because it is the
one property that makes every other record in this file worth keeping:
the derived numbers can always be recomputed, and the counts the
detector reported cannot.

ACQUISITION FAILURE IS NOT POOR QUALITY

A Measurement whose acquisition failed is stored with
`acquisition_status = FAILED` and no `raw`. It is an operational
record, and it is deliberately not a successful measurement full of
zeros - a spectrum of zeros is indistinguishable from a genuinely dark
one, and inventing it would put a fact into the scientific record that
never happened.

Layer rule: BD must never import Science. This module stores and
validates the SHAPE of a record; what the numbers mean is Science's.
"""

import copy
import json
import os
import re
import tempfile
from datetime import datetime, timezone

from BD import config

BD_DIR = config.BD_DIR
ARCHIVE_PATH = config.SAMPLES_FILE

ARCHIVE_VERSION = config.SAMPLE_SCHEMA_VERSION

# Sample IDs appear in filenames and in reports, so the character set is
# restricted.
SAMPLE_ID_MAX_LENGTH = 24
SAMPLE_ID_ALLOWED_EXTRA = "-_"

# ----------------------------------------------------------------------
# Sample lifecycle
#
#   EMPTY          no sample assigned to this slot
#   READY_TO_LOAD  a Sample ID exists and the slot is at the loader
#   LOADED         the rover arm has physically deposited soil
#   MEASURED       at least one successful acquisition exists
#
# MEASURED is NOT the same as EMPTY. The soil physically stays in the
# slot until the operator clears it.
# ----------------------------------------------------------------------
STATE_EMPTY = "EMPTY"
STATE_READY_TO_LOAD = "READY_TO_LOAD"
STATE_LOADED = "LOADED"
STATE_MEASURED = "MEASURED"

STATES = (STATE_EMPTY, STATE_READY_TO_LOAD, STATE_LOADED, STATE_MEASURED)

# Acquisition outcomes. SUCCESS is the only one that carries RAW.
ACQUISITION_SUCCESS = "SUCCESS"
ACQUISITION_FAILED = "FAILED"

# Mission metadata the software cannot determine by itself. Every field
# is optional; an unrecorded observation stays null rather than being
# invented to satisfy a prompt.
METADATA_FIELDS = (
    ("task", "Task"),
    ("hypothesis", "Hypothesis"),
    ("location", "Location"),
    ("map_point", "Map point"),
    ("note", "Note"),
    ("photo_reference", "Photo reference"),
)

METADATA_KEYS = tuple(key for key, _label in METADATA_FIELDS)


class StorageError(Exception):
    """Sample data could not be read or written."""

    def __init__(self, message, code="SAMPLE_SAVE_ERROR"):
        super().__init__(message)

        self.message = str(message)
        self.code = str(code)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


# ----------------------------------------------------------------------
# identifiers
# ----------------------------------------------------------------------

def validate_sample_id(sample_id):
    """
    Check a Sample ID and return it stripped.

    Raises rather than silently correcting: an ID the operator typed is
    the handle for a physical thing, and quietly changing it would put
    the record under a name nobody looks for.
    """
    if sample_id is None:
        raise StorageError("A Sample ID is required.", "INVALID_SAMPLE_ID")

    text = str(sample_id).strip()

    if not text:
        raise StorageError("A Sample ID is required.", "INVALID_SAMPLE_ID")

    if len(text) > SAMPLE_ID_MAX_LENGTH:
        raise StorageError(
            "Sample ID '{}' is longer than {} characters.".format(
                text, SAMPLE_ID_MAX_LENGTH
            ),
            "INVALID_SAMPLE_ID",
        )

    for character in text:
        if not (character.isalnum() or character in SAMPLE_ID_ALLOWED_EXTRA):
            raise StorageError(
                "Sample ID '{}' contains '{}'. Letters, digits, '-' and "
                "'_' only.".format(text, character),
                "INVALID_SAMPLE_ID",
            )

    return text


def _next_id(existing, prefix, width=3):
    """
    The next free `prefix000` identifier for a record's children.

    Derived from what is already stored rather than from a counter, so
    two processes cannot both believe they own M002 and so a record
    remains self-describing after it is copied somewhere else.
    """
    highest = 0
    pattern = re.compile(r"^%s(\d+)$" % re.escape(prefix))

    for identifier in existing:
        match = pattern.match(str(identifier or ""))

        if match:
            highest = max(highest, int(match.group(1)))

    return "{}{:0{}d}".format(prefix, highest + 1, width)


def blank_metadata(*sources):
    """
    Every metadata key present, filled from the first source that has it.

    Absent stays None. A field nobody recorded must read as unrecorded,
    not as an empty string that looks like an answer.
    """
    metadata = {}

    for key in METADATA_KEYS:
        value = None

        for source in sources:
            if not source:
                continue

            candidate = source.get(key)

            if candidate not in (None, ""):
                value = candidate

                break

        metadata[key] = value

    return metadata


# ----------------------------------------------------------------------
# record construction
# ----------------------------------------------------------------------

def new_sample(sample_id, slot_id, created_at=None, metadata=None):
    """An empty Sample record: identity and metadata, no measurements."""
    return {
        "schema_version": ARCHIVE_VERSION,
        "sample_id": validate_sample_id(sample_id),
        "slot_id": int(slot_id) if slot_id is not None else None,
        "state": STATE_READY_TO_LOAD,
        "timestamps": {
            "created_at": created_at or utc_now(),
            "loaded_at": None,
            "measured_at": None,
        },
        "metadata": blank_metadata(metadata),

        "measurements": [],

        # A VIEW of the latest selected successful AnalysisRun, never a
        # replacement for one. Updating it deletes no history.
        "conclusion": None,
    }


def new_measurement(measurement_id, sample_id, slot_id, raw=None,
                    acquisition=None, acquisition_status=ACQUISITION_SUCCESS,
                    calibration_id=None, hardware=None, statistics=None,
                    quality=None, error=None, timestamp=None):
    """
    One completed physical acquisition.

    `raw` is the acquisition grouped BY ILLUMINATION:

        {"white": {...18 channels...},
         "uv":    {...18 channels...},
         "red":   {...18 channels...}}

    The grouping is preserved because it is what was actually measured.
    A flat 54-element vector is a CALCULATED representation of this and
    belongs to an AnalysisRun, not here - once the three blocks are
    concatenated and the boundaries forgotten, nothing can tell which
    lamp produced which number, and every per-illumination quality
    judgement becomes impossible.

    A legacy 18-channel Measurement stores `{"white": {...}}` and stays
    exactly as valid; it simply has one illumination.
    """
    record = {
        "measurement_id": measurement_id,
        "sample_id": sample_id,
        "slot_id": int(slot_id) if slot_id is not None else None,
        "timestamp": timestamp or utc_now(),

        "acquisition_status": acquisition_status,
        "acquisition": dict(acquisition or {}),

        # The calibration this acquisition must be interpreted under.
        # An id, not a copy: the calibration is a record in its own
        # right and duplicating it here would create two versions of one
        # fact.
        "calibration_id": calibration_id,

        "hardware": dict(hardware or {}),
        "statistics": statistics,
        "quality": quality,

        "analysis_runs": [],
    }

    if acquisition_status == ACQUISITION_SUCCESS:
        if not raw:
            raise StorageError(
                "A successful Measurement must carry RAW. Storing a "
                "zero-filled spectrum instead would put an acquisition "
                "into the record that never happened.",
                "MISSING_RAW",
            )

        # A DEEP COPY, not the caller's object.
        #
        # `record["raw"] = raw` stored a live reference to a dictionary
        # the caller still owns, so anything that touched that
        # dictionary afterwards - a normalization done in place, a unit
        # conversion, a debug line - silently edited the archive's copy
        # of what the instrument reported. RAW is the one thing in this
        # project that must be exactly what came off the sensor, and
        # "immutable" cannot mean "immutable unless somebody keeps the
        # reference".
        #
        # dict(raw) is not enough: raw is nested one level, illumination
        # -> channel -> value, and a shallow copy shares the inner
        # dictionaries.
        record["raw"] = copy.deepcopy(raw)

    else:
        # Deliberately no `raw` key at all, rather than null: a reader
        # asking "is there RAW?" gets the same answer whichever way it
        # asks, and no arithmetic can reach a placeholder.
        record["error"] = error

    return record


def new_analysis_run(analysis_run_id, measurement_id, result):
    """
    One interpretation of one Measurement.

    `result` is whatever Science produced - it is stored as it came,
    including its own version block. BD does not summarise it, because
    a summary written by the storage layer is a scientific judgement
    made in the wrong place.
    """
    run = dict(result or {})

    run["analysis_run_id"] = analysis_run_id
    run["measurement_id"] = measurement_id
    run.setdefault("created_at", utc_now())

    return run


# ----------------------------------------------------------------------
# reading a record
# ----------------------------------------------------------------------

def measurements_of(record):
    return list((record or {}).get("measurements") or [])


def successful_measurements(record):
    return [
        m for m in measurements_of(record)
        if m.get("acquisition_status") == ACQUISITION_SUCCESS
    ]


def latest_measurement(record, successful_only=True):
    """The most recent Measurement, or None. Order is insertion order."""
    candidates = (successful_measurements(record) if successful_only
                  else measurements_of(record))

    return candidates[-1] if candidates else None


def analysis_runs_of(measurement):
    return list((measurement or {}).get("analysis_runs") or [])


def latest_analysis_run(measurement):
    runs = analysis_runs_of(measurement)

    return runs[-1] if runs else None


def find_measurement(record, measurement_id):
    for measurement in measurements_of(record):
        if measurement.get("measurement_id") == measurement_id:
            return measurement

    return None


def summary_of(record):
    """The compact row an operator list shows. Derived, never stored."""
    measurements = measurements_of(record)
    successful = successful_measurements(record)

    latest = successful[-1] if successful else None
    runs = analysis_runs_of(latest) if latest else []
    conclusion = record.get("conclusion") or {}

    return {
        "sample_id": record.get("sample_id"),
        "slot_id": record.get("slot_id"),
        "state": record.get("state"),
        "created_at": (record.get("timestamps") or {}).get("created_at"),
        "measured_at": (record.get("timestamps") or {}).get("measured_at"),
        "measurement_count": len(measurements),
        "successful_measurement_count": len(successful),
        "failed_measurement_count": len(measurements) - len(successful),
        "analysis_run_count": sum(
            len(analysis_runs_of(m)) for m in measurements
        ),
        "latest_measurement_id": (
            latest.get("measurement_id") if latest else None
        ),
        "latest_analysis_run_id": (
            runs[-1].get("analysis_run_id") if runs else None
        ),
        "interpretation": conclusion.get("interpretation"),
        "confidence": conclusion.get("confidence"),
        "decision_status": conclusion.get("status"),
    }


# ----------------------------------------------------------------------
# the archive
# ----------------------------------------------------------------------

def _write_json(path, payload):
    """
    Write atomically: a crash mid-write must not destroy the archive.

    The temporary file is created in the SAME directory so the
    replacement is a rename within one filesystem, which is what makes
    it atomic.
    """
    # INSIDE THE TRY, both of them.
    #
    # Creating the directory and creating the temporary file were
    # outside it, so the two failures that actually happen on a rover -
    # a full disk and a read-only directory - escaped as raw OSErrors
    # while every screen in the project catches StorageError. A save
    # that failed because the card was full crashed the operator
    # client instead of saying "could not save".
    try:
        path.parent.mkdir(parents=True, exist_ok=True)

        handle, temporary = tempfile.mkstemp(
            dir=str(path.parent), prefix=".samples-", suffix=".tmp"
        )

    except OSError as error:
        raise StorageError(
            "Could not prepare a temporary file beside {}: {}".format(
                path, error
            )
        )

    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

        os.replace(temporary, str(path))

    except Exception as error:
        try:
            os.unlink(temporary)

        except OSError:
            pass

        raise StorageError(
            "Could not write {}: {}".format(path, error)
        )


def _read_json(path, default=None):
    if not path.exists():
        return default

    try:
        with open(str(path), encoding="utf-8") as stream:
            return json.load(stream)

    except (OSError, ValueError) as error:
        raise StorageError(
            "Could not read {}: {}".format(path, error), "SAMPLE_READ_ERROR"
        )


class SampleStore:
    """
    Every Sample this instrument has produced, in one archive.

    Loaded once, written whole. The archive is small - a run produces a
    handful of Samples - and one file that is either entirely the old
    version or entirely the new one is worth more than a directory of
    fragments that can be half-updated.
    """

    def __init__(self, archive_path=None):
        self.path = archive_path or ARCHIVE_PATH
        self.data = None
        self.error = None

        # The last state that reached the disk. `_write` restores from
        # it when a save fails, so what the screens read never contains
        # a Sample the archive does not.
        self._durable = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def load(self):
        payload = _read_json(self.path)

        if payload is None:
            self.data = {
                "schema_version": ARCHIVE_VERSION,
                "samples": [],
            }
            self._durable = copy.deepcopy(self.data)

            return self

        if not isinstance(payload, dict) or "samples" not in payload:
            raise StorageError(
                "{} is not a Sample archive.".format(self.path),
                "SAMPLE_READ_ERROR",
            )

        version = payload.get("schema_version") or payload.get("version")

        if version != ARCHIVE_VERSION:
            # The archive is the run's only irreplaceable output and it
            # is deliberately not in version control, so the original is
            # copied aside BEFORE the migrated version replaces it. A
            # migration that turns out to be wrong then costs nothing.
            backup = self.path.with_name(
                "{}.schema{}.backup.json".format(self.path.stem, version)
            )

            if not backup.exists():
                _write_json(backup, payload)

            payload = migrate(payload, version)
            self.data = payload
            self._write()

            return self

        self.data = payload
        self._durable = copy.deepcopy(payload)

        return self

    def _require_ready(self):
        if self.data is None:
            self.load()

        return self.data

    def _records(self):
        return self._require_ready().setdefault("samples", [])

    def _write(self):
        """
        Persist, or leave memory exactly as the disk is.

        WHY THE SNAPSHOT.

        Every mutator here follows the same shape: change `self.data`,
        then call this. If the write fails, the caller gets a
        StorageError - and used to be left holding a store whose
        in-memory archive contained a Sample, a measurement or a state
        change that is not on disk and never will be.

        That divergence is worse than the failed write. The screens
        read the store, so the operator is shown a Sample the archive
        does not contain; the workflow's rule that RAW is persisted
        BEFORE Science runs becomes a rule about a record that only
        exists in memory; and the next successful save writes the
        phantom out as though it had been there all along.

        So a failed write restores what was last durable. The operator
        sees the failure AND a store that matches the file, which is
        the only pair of facts they can act on.
        """
        data = self._require_ready()

        data["schema_version"] = ARCHIVE_VERSION
        data["updated_at"] = utc_now()

        try:
            _write_json(self.path, data)

        except Exception:
            # Back to the last state that reached the disk. Taking the
            # snapshot HERE would be too late - the mutation has
            # already been applied to `data` by the caller - so what is
            # restored is the copy made after the previous successful
            # write.
            if self._durable is not None:
                self.data = copy.deepcopy(self._durable)

            raise

        self._durable = copy.deepcopy(data)

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------

    def count(self):
        return len(self._records())

    def summaries(self):
        return [summary_of(record) for record in self._records()]

    def has_sample(self, sample_id):
        return self.get_sample(sample_id) is not None

    def get_sample(self, sample_id):
        for record in self._records():
            if record.get("sample_id") == sample_id:
                return record

        return None

    def require_sample(self, sample_id):
        record = self.get_sample(sample_id)

        if record is None:
            raise StorageError(
                "No Sample '{}' in the archive.".format(sample_id),
                "SAMPLE_NOT_FOUND",
            )

        return record

    def get_state(self, sample_id):
        record = self.get_sample(sample_id)

        return (record or {}).get("state", STATE_EMPTY)

    def active_samples(self):
        """Sample id by slot, for the slots that currently hold one."""
        by_slot = {}

        for record in self._records():
            if record.get("state") in (STATE_READY_TO_LOAD, STATE_LOADED,
                                       STATE_MEASURED):
                by_slot[record.get("slot_id")] = record.get("sample_id")

        return by_slot

    # ------------------------------------------------------------------
    # writing
    # ------------------------------------------------------------------

    def create(self, sample_id, slot_id, created_at=None, metadata=None):
        sample_id = validate_sample_id(sample_id)

        if self.has_sample(sample_id):
            raise StorageError(
                "Sample '{}' already exists. Sample IDs are permanent "
                "handles for physical material and are never "
                "reused.".format(sample_id),
                "DUPLICATE_SAMPLE_ID",
            )

        record = new_sample(sample_id, slot_id, created_at, metadata)

        self._records().append(record)
        self._write()

        return record

    def set_state(self, sample_id, state, timestamp_key=None,
                  timestamp=None):
        if state not in STATES:
            raise StorageError(
                "Unknown Sample state '{}'.".format(state), "INVALID_STATE"
            )

        record = self.require_sample(sample_id)
        record["state"] = state

        if timestamp_key:
            record.setdefault("timestamps", {})[timestamp_key] = (
                timestamp or utc_now()
            )

        self._write()

        return record

    def update_metadata(self, sample_id, metadata):
        record = self.require_sample(sample_id)
        record["metadata"] = blank_metadata(
            metadata, record.get("metadata")
        )

        self._write()

        return record

    def add_measurement(self, sample_id, raw=None, **fields):
        """
        Append a Measurement. THE FIRST THING THAT HAPPENS AFTER AN
        ACQUISITION, and before Science is asked anything at all.

        Persisting RAW first is what makes a Science failure survivable:
        an exception in the pipeline, a database that will not load or a
        model that raises then costs an analysis, not an experiment that
        cannot be repeated because the material has already been tipped
        out of the slot.

        Returns the stored Measurement, with the id it was given.
        """
        record = self.require_sample(sample_id)

        measurement_id = _next_id(
            [m.get("measurement_id") for m in measurements_of(record)], "M"
        )

        measurement = new_measurement(
            measurement_id, sample_id, record.get("slot_id"), raw=raw,
            **fields
        )

        record.setdefault("measurements", []).append(measurement)

        if measurement["acquisition_status"] == ACQUISITION_SUCCESS:
            record["state"] = STATE_MEASURED
            record.setdefault("timestamps", {})["measured_at"] = (
                measurement["timestamp"]
            )

        self._write()

        return measurement

    def add_analysis_run(self, sample_id, measurement_id, result):
        """
        Append an AnalysisRun to a Measurement. NEVER replaces one.

        Re-analysing under a new Science version, a new calibration or a
        new database produces another run beside the existing ones. That
        is what makes "what did we conclude in August, and why?" a
        question with an answer.
        """
        record = self.require_sample(sample_id)
        measurement = find_measurement(record, measurement_id)

        if measurement is None:
            raise StorageError(
                "Sample '{}' has no Measurement '{}'.".format(
                    sample_id, measurement_id
                ),
                "MEASUREMENT_NOT_FOUND",
            )

        run_id = _next_id(
            [r.get("analysis_run_id") for r in analysis_runs_of(measurement)],
            "A",
        )

        run = new_analysis_run(run_id, measurement_id, result)

        measurement.setdefault("analysis_runs", []).append(run)

        self._write()

        return run

    def set_conclusion(self, sample_id, conclusion):
        """
        Update the Sample's CURRENT conclusion.

        A view derived from the latest selected successful AnalysisRun,
        and nothing more. It deletes no Measurement, no AnalysisRun and
        no previous conclusion - all of which remain exactly where they
        were, which is the only reason overwriting this field is safe.
        """
        record = self.require_sample(sample_id)
        record["conclusion"] = conclusion

        self._write()

        return record

    def rename(self, old_id, new_id):
        new_id = validate_sample_id(new_id)
        record = self.require_sample(old_id)

        if self.has_sample(new_id):
            raise StorageError(
                "Sample '{}' already exists.".format(new_id),
                "DUPLICATE_SAMPLE_ID",
            )

        record["sample_id"] = new_id

        for measurement in measurements_of(record):
            measurement["sample_id"] = new_id

        self._write()

        return record

    def delete(self, sample_id):
        """
        Remove a Sample entirely.

        Deliberately blunt and deliberately rare: this destroys RAW that
        cannot be reacquired once the material is gone. It exists for
        the mistyped ID created seconds ago, not for tidying up.
        """
        records = self._records()

        for index, record in enumerate(records):
            if record.get("sample_id") == sample_id:
                removed = records.pop(index)
                self._write()

                return removed

        raise StorageError(
            "No Sample '{}' in the archive.".format(sample_id),
            "SAMPLE_NOT_FOUND",
        )

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------

    def status(self):
        records = self._records()

        return {
            "path": str(self.path),
            "schema_version": ARCHIVE_VERSION,
            "samples": len(records),
            "measurements": sum(
                len(measurements_of(r)) for r in records
            ),
            "analysis_runs": sum(
                len(analysis_runs_of(m))
                for r in records for m in measurements_of(r)
            ),
        }


# ----------------------------------------------------------------------
# migration
# ----------------------------------------------------------------------

def migrate(payload, version):
    """
    Bring an archive written before the three-layer model up to date.

    ONE-WAY AND LOSSLESS. Every field of the old flat record is carried
    into the new shape; nothing is dropped, nothing is recomputed, and
    no number is changed. What was one implicit measurement becomes
    M001, and what was one implicit analysis becomes A001 beneath it -
    which is what those records always meant, said out loud.

    This is not a compatibility layer. It runs once, the archive is
    rewritten in the current shape, and the old shape is never produced
    again.
    """
    samples = []

    for old in payload.get("samples") or []:
        record = new_sample(
            old.get("sample_id"),
            old.get("slot_id"),
            (old.get("timestamps") or {}).get("created_at"),
            old.get("metadata"),
        )
        record["state"] = old.get("state", STATE_READY_TO_LOAD)
        record["timestamps"] = dict(old.get("timestamps") or {})

        legacy = old.get("measurement")

        if legacy:
            # The old record stored raw / dark_corrected / normalized
            # side by side. Only `raw` is MEASURED; the other two were
            # CALCULATED from it and belong to an AnalysisRun, so they
            # are carried there rather than promoted into RAW.
            raw = legacy.get("raw")

            measurement = new_measurement(
                "M001",
                record["sample_id"],
                old.get("slot_id"),
                raw={"white": raw} if raw else None,
                acquisition={
                    "sensor_settings": legacy.get("sensor_settings"),
                    "wavelengths": legacy.get("wavelengths"),
                    "illuminations": ["white"],
                    "migrated_from_schema": version,
                },
                acquisition_status=(
                    ACQUISITION_SUCCESS if raw else ACQUISITION_FAILED
                ),
                calibration_id=(old.get("calibration") or {}).get(
                    "calibration_id"
                ),
                hardware=old.get("hardware"),
                timestamp=(old.get("timestamps") or {}).get("measured_at"),
                error=None if raw else {"code": "NO_RAW_IN_LEGACY_RECORD"},
            )

            if old.get("analysis") or old.get("reference_matches"):
                measurement["analysis_runs"] = [{
                    "analysis_run_id": "A001",
                    "measurement_id": "M001",
                    "created_at": (old.get("timestamps") or {}).get(
                        "measured_at"
                    ),
                    "migrated_from_schema": version,
                    "analysis_status": old.get("analysis_status"),
                    "analysis_error": old.get("analysis_error"),
                    "representations": {
                        "dark_corrected": legacy.get("dark_corrected"),
                        "normalized": legacy.get("normalized"),
                    },
                    "legacy_analysis": old.get("analysis"),
                    "reference_matches": old.get("reference_matches"),
                    "database": old.get("database"),
                    "calibration": old.get("calibration"),
                }]

            record["measurements"] = [measurement]

        if old.get("conclusion") or old.get("best_match"):
            record["conclusion"] = {
                "interpretation": old.get("best_match"),
                "similarity_percent": old.get("best_similarity"),
                "status": old.get("status"),
                "text": old.get("conclusion"),
                "from_analysis_run": "A001",
                "migrated_from_schema": version,
            }

        samples.append(record)

    return {
        "schema_version": ARCHIVE_VERSION,
        "migrated_from_schema": version,
        "migrated_at": utc_now(),
        "samples": samples,
    }
