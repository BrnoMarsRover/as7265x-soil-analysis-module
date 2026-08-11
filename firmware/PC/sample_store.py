"""
Persistent scientific sample storage on the main computer.

This is the authoritative archive of the competition run. The ESP32
stores no science history at all: it forgets everything on reset except
which slots physically hold soil, and even that is only a convenience.

Layout
------
    data/samples.json        index of every sample (small)
    data/samples/S001.json   one complete scientific record per sample

The records are split on purpose. A full record holds three 18-channel
spectra plus a match against every database material; the index stays
small enough to load at any time, and one record is read only when it
is actually opened.

Paths come from this file's location, so the application finds its data
whatever directory it was started from.
"""

import json
import os
from pathlib import Path


PC_DIR = Path(__file__).resolve().parent

DATA_DIR = PC_DIR / "data"
INDEX_PATH = DATA_DIR / "samples.json"
RECORD_DIR = DATA_DIR / "samples"

INDEX_VERSION = 1

# Sample IDs double as filenames, so the character set is restricted.
SAMPLE_ID_MAX_LENGTH = 24
SAMPLE_ID_ALLOWED_EXTRA = "-_"

# ----------------------------------------------------------------------
# Sample lifecycle.
#
#   EMPTY          no sample assigned to this slot
#   READY_TO_LOAD  a Sample ID exists and the slot is at the loader
#   LOADED         the rover arm has physically deposited soil
#   MEASURED       spectrum acquired and analysed
#
# MEASURED is NOT the same as EMPTY. The soil physically stays in the
# slot until the operator clears it.
# ----------------------------------------------------------------------
STATE_EMPTY = "EMPTY"
STATE_READY_TO_LOAD = "READY_TO_LOAD"
STATE_LOADED = "LOADED"
STATE_MEASURED = "MEASURED"

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


def validate_sample_id(sample_id):
    """
    Check a Sample ID.

    The Sample ID is the scientific identity of the soil and has nothing
    to do with the physical slot number: S001, D1 and ROCK-07 are all
    valid and may sit in any slot.
    """
    if not isinstance(sample_id, str):
        raise StorageError("Sample ID must be text.", "INVALID_SAMPLE_ID")

    sample_id = sample_id.strip()

    if not sample_id:
        raise StorageError("Sample ID must not be empty.", "INVALID_SAMPLE_ID")

    if len(sample_id) > SAMPLE_ID_MAX_LENGTH:
        raise StorageError(
            "Sample ID must be at most {} characters.".format(
                SAMPLE_ID_MAX_LENGTH
            ),
            "INVALID_SAMPLE_ID",
        )

    for character in sample_id:
        if character.isalnum() and character.isascii():
            continue

        if character in SAMPLE_ID_ALLOWED_EXTRA:
            continue

        raise StorageError(
            "Sample ID may only contain letters, digits and '{}'.".format(
                SAMPLE_ID_ALLOWED_EXTRA
            ),
            "INVALID_SAMPLE_ID",
        )

    return sample_id


def blank_metadata(*sources):
    """
    Merge metadata dictionaries over a template of the known fields.

    Unknown keys are preserved so mission-specific context can be added
    without a code change; anything absent stays null.
    """
    result = {key: None for key in METADATA_KEYS}

    for source in sources:
        if not isinstance(source, dict):
            continue

        for key, value in source.items():
            result[key] = value

    return result


# ----------------------------------------------------------------------
# atomic file helpers
# ----------------------------------------------------------------------

def _write_json(path, obj):
    """
    Write JSON so an interrupted write cannot destroy the old file.

        1. write everything to <path>.tmp
        2. replace <path> with it in one atomic rename

    os.replace is atomic on both Windows and POSIX, so the file on disk
    is always either the complete old version or the complete new one.
    """
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(obj, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(tmp, path)

    except OSError as error:
        try:
            tmp.unlink(missing_ok=True)

        except OSError:
            pass

        raise StorageError("Could not write {}: {}".format(path, error))


def _read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    except (OSError, ValueError):
        return default


class SampleStore:
    """Persistent index plus one file per sample."""

    def __init__(self, index_path=None, record_dir=None):
        self.index_path = Path(index_path or INDEX_PATH)
        self.record_dir = Path(record_dir or RECORD_DIR)

        self.ready = False
        self.error = None
        self.index = {"version": INDEX_VERSION, "samples": []}

        self.load()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def load(self):
        """
        Load the index, creating an empty one in memory if none exists.

        A missing index is normal on a fresh install and is not an
        error. A PRESENT but unparseable index is a different situation
        entirely: it is reported and every write is refused, because
        overwriting it would destroy data that is probably still
        recoverable by hand.
        """
        self.error = None

        if not self.index_path.exists():
            self.index = {"version": INDEX_VERSION, "samples": []}
            self.ready = True

            return self.index

        data = _read_json(self.index_path)

        if not isinstance(data, dict) or not isinstance(
            data.get("samples"), list
        ):
            self.index = {"version": INDEX_VERSION, "samples": []}
            self.ready = False
            self.error = (
                "{} exists but could not be parsed as a sample index. It "
                "has NOT been modified - recover or move it aside by hand "
                "before measuring again.".format(self.index_path)
            )

            return self.index

        data.setdefault("version", INDEX_VERSION)
        self.index = data
        self.ready = True

        return self.index

    def _require_ready(self):
        if not self.ready:
            raise StorageError(
                self.error or "{} could not be parsed.".format(
                    self.index_path
                ),
                "SAMPLES_INDEX_INVALID",
            )

    def _record_path(self, sample_id):
        return self.record_dir / "{}.json".format(sample_id)

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------

    def count(self):
        return len(self.index.get("samples", []))

    def summaries(self):
        return list(self.index.get("samples", []))

    def find_summary(self, sample_id):
        for entry in self.index.get("samples", []):
            if entry.get("sample_id") == sample_id:
                return entry

        return None

    def has_sample(self, sample_id):
        return self.find_summary(sample_id) is not None

    def get_state(self, sample_id):
        summary = self.find_summary(sample_id)

        return summary.get("state") if summary else None

    def get_sample(self, sample_id):
        """Full scientific record for one Sample ID, or None."""
        return _read_json(self._record_path(validate_sample_id(sample_id)))

    def active_samples(self):
        """
        Sample ID per slot for everything not yet cleared.

        This is what makes the PC authoritative: after a restart the
        main screen is rebuilt from here, not from the ESP32.
        """
        by_slot = {}

        for entry in self.summaries():
            if entry.get("state") in (STATE_EMPTY, None):
                continue

            slot_id = entry.get("slot_id")

            if slot_id:
                by_slot[int(slot_id)] = entry

        return by_slot

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------

    @staticmethod
    def _summarize(record):
        analysis = record.get("analysis") or {}
        timestamps = record.get("timestamps") or {}

        return {
            "sample_id": record.get("sample_id"),
            "slot_id": record.get("slot_id"),
            "state": record.get("state"),
            "measured": bool(record.get("measurement")),
            "created_at": timestamps.get("created_at"),
            "measured_at": timestamps.get("measured_at"),
            "best_match": analysis.get("best_match"),
            "best_similarity": analysis.get("best_similarity"),
            "status": analysis.get("status"),
            "analysis_status": record.get("analysis_status"),
        }

    def save(self, record):
        """
        Persist one sample record and refresh its index entry.

        The record file is written first and the index second, so a
        failure never leaves the index pointing at a file that does not
        exist.
        """
        self._require_ready()

        sample_id = validate_sample_id(record.get("sample_id"))
        record["sample_id"] = sample_id

        _write_json(self._record_path(sample_id), record)

        summary = self._summarize(record)
        samples = self.index.setdefault("samples", [])

        for position, entry in enumerate(samples):
            if entry.get("sample_id") == sample_id:
                samples[position] = summary

                break

        else:
            samples.append(summary)

        _write_json(self.index_path, self.index)

        return summary

    def create(self, sample_id, slot_id, created_at, metadata=None):
        """Open a new record at READY_TO_LOAD. Nothing scientific yet."""
        sample_id = validate_sample_id(sample_id)

        record = self.get_sample(sample_id) or {}

        record.update({
            "sample_id": sample_id,
            "slot_id": int(slot_id),
            "state": STATE_READY_TO_LOAD,
        })

        timestamps = record.get("timestamps")

        if not isinstance(timestamps, dict):
            timestamps = {}

        timestamps.setdefault("created_at", created_at)
        timestamps["loaded_at"] = timestamps.get("loaded_at")
        timestamps["measured_at"] = timestamps.get("measured_at")

        record["timestamps"] = timestamps
        record["metadata"] = blank_metadata(record.get("metadata"), metadata)

        self.save(record)

        return record

    def set_state(self, sample_id, state, timestamp_key=None, timestamp=None):
        """Advance the lifecycle, optionally stamping the transition."""
        record = self.get_sample(sample_id)

        if record is None:
            raise StorageError(
                "No stored sample with ID {}.".format(sample_id),
                "SAMPLE_NOT_FOUND",
            )

        record["state"] = state

        if timestamp_key:
            timestamps = record.get("timestamps")

            if not isinstance(timestamps, dict):
                timestamps = {}

            timestamps[timestamp_key] = timestamp
            record["timestamps"] = timestamps

        self.save(record)

        return record

    def update_metadata(self, sample_id, metadata):
        """
        Merge mission metadata into an existing sample.

        Spectral data, matches and analysis are never touched here: this
        only adds the human context collected around the measurement.
        """
        record = self.get_sample(sample_id)

        if record is None:
            raise StorageError(
                "No stored sample with ID {}.".format(sample_id),
                "SAMPLE_NOT_FOUND",
            )

        if not isinstance(metadata, dict):
            raise StorageError(
                "Metadata must be a mapping.", "SAMPLE_UPDATE_FAILED"
            )

        record["metadata"] = blank_metadata(record.get("metadata"), metadata)

        self.save(record)

        return record

    def rename(self, old_id, new_id):
        """
        Change a Sample ID, keeping every scientific value intact.

        The record is written under the new name before the old file is
        removed, so an interruption leaves both rather than neither.
        """
        self._require_ready()

        old_id = validate_sample_id(old_id)
        new_id = validate_sample_id(new_id)

        record = self.get_sample(old_id)

        if record is None:
            raise StorageError(
                "No stored sample with ID {}.".format(old_id),
                "SAMPLE_NOT_FOUND",
            )

        if new_id == old_id:
            return record

        if self.find_summary(new_id) is not None:
            raise StorageError(
                "Sample ID {} already exists.".format(new_id),
                "SAMPLE_ID_ALREADY_EXISTS",
            )

        record["sample_id"] = new_id
        _write_json(self._record_path(new_id), record)

        samples = self.index.setdefault("samples", [])

        for position, entry in enumerate(samples):
            if entry.get("sample_id") == old_id:
                samples[position] = self._summarize(record)

                break

        _write_json(self.index_path, self.index)

        # Only now is the old copy redundant.
        try:
            self._record_path(old_id).unlink(missing_ok=True)

        except OSError:
            pass

        return record

    def delete(self, sample_id):
        """
        Permanently remove one sample.

        The index is rewritten first: it is the authoritative list, so a
        record file left behind by an interrupted delete is harmless,
        while an index entry pointing at a deleted file is not.
        """
        self._require_ready()

        sample_id = validate_sample_id(sample_id)
        summary = self.find_summary(sample_id)

        if summary is None:
            raise StorageError(
                "No stored sample with ID {}.".format(sample_id),
                "SAMPLE_NOT_FOUND",
            )

        previous = list(self.index.get("samples", []))

        self.index["samples"] = [
            entry for entry in previous
            if entry.get("sample_id") != sample_id
        ]

        try:
            _write_json(self.index_path, self.index)

        except StorageError:
            # Put the entry back: nothing was actually deleted.
            self.index["samples"] = previous

            raise

        try:
            self._record_path(sample_id).unlink(missing_ok=True)

        except OSError:
            pass

        return summary

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------

    def status(self):
        return {
            "ready": self.ready,
            "error": self.error,
            "index_file": str(self.index_path),
            "record_dir": str(self.record_dir),
            "samples_saved": self.count(),
        }
