"""
Persistent scientific sample storage on the main computer.

This is the authoritative archive of the competition run, and it lives
beside the science data it was produced against:

    firmware/BD/database.json     reference materials     READ ONLY
    firmware/BD/references.json   fixed White/Dark        READ ONLY
    firmware/BD/samples.json      measured Samples        READ/WRITE

One file holds COMPLETE records - all three 18-channel spectra, the
White and Dark actually used, the comparison against every material, and
the full analysis - so a measurement can be reproduced from the archive
alone without needing the reference files to be unchanged.

Paths come from this file's location, so the application finds its data
whatever directory it was started from.
"""

import json
import os
from pathlib import Path


PC_DIR = Path(__file__).resolve().parent
BD_DIR = PC_DIR.parent / "BD"

ARCHIVE_PATH = BD_DIR / "samples.json"

# Retired: the split index-plus-one-file-per-sample layout under
# firmware/PC/data. load() migrates it automatically and leaves the old
# files alone.
LEGACY_DATA_DIR = PC_DIR / "data"
LEGACY_INDEX_PATH = LEGACY_DATA_DIR / "samples.json"
LEGACY_RECORD_DIR = LEGACY_DATA_DIR / "samples"

ARCHIVE_VERSION = 2

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


def summary_of(record):
    """
    The compact fields the Sample Database table shows.

    Reads the full schema first and falls back to the flat fields an
    older short record used, so archives written before this layout keep
    listing correctly instead of showing blanks.
    """
    record = record or {}
    analysis = record.get("analysis") or {}
    timestamps = record.get("timestamps") or {}
    measurement = record.get("measurement") or {}

    def pick(*names):
        for name in names:
            if analysis.get(name) is not None:
                return analysis[name]

            if record.get(name) is not None:
                return record[name]

        return None

    return {
        "sample_id": record.get("sample_id"),
        "slot_id": record.get("slot_id"),
        "state": record.get("state"),
        "measured": bool(measurement) or bool(record.get("measured")),
        "created_at": timestamps.get("created_at")
        or record.get("created_at"),
        "measured_at": timestamps.get("measured_at")
        or record.get("measured_at"),
        "best_match": pick("best_match"),
        "best_similarity": pick("best_similarity", "best_match_score"),
        "status": pick("status"),
        "analysis_status": record.get("analysis_status"),
    }


class SampleStore:
    """
    One JSON archive holding every complete Sample record.

    firmware/BD/samples.json sits beside database.json and
    references.json - the measured half of the science data, and the
    only one of the three that is ever written.
    """

    def __init__(self, archive_path=None):
        self.archive_path = Path(archive_path or ARCHIVE_PATH)

        self.ready = False
        self.error = None
        self.migrated_from = None
        self.archive = {"version": ARCHIVE_VERSION, "samples": []}

        self.load()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def load(self):
        """
        Load the archive, creating an empty one in memory if none exists.

        A missing archive is normal on a fresh install and is not an
        error. A PRESENT but unparseable archive is a different
        situation entirely: it is reported and every write is refused,
        because overwriting it would destroy data that is probably still
        recoverable by hand.
        """
        self.error = None
        self.migrated_from = None

        if not self.archive_path.exists():
            migrated = self._load_legacy()

            self.archive = migrated or {
                "version": ARCHIVE_VERSION, "samples": []
            }
            self.ready = True

            return self.archive

        data = _read_json(self.archive_path)

        if not isinstance(data, dict) or not isinstance(
            data.get("samples"), list
        ):
            self.archive = {"version": ARCHIVE_VERSION, "samples": []}
            self.ready = False
            self.error = (
                "{} exists but could not be parsed as a Sample archive. It "
                "has NOT been modified - recover or move it aside by hand "
                "before measuring again.".format(self.archive_path)
            )

            return self.archive

        data.setdefault("version", ARCHIVE_VERSION)
        self.archive = data
        self.ready = True

        return self.archive

    def _load_legacy(self):
        """
        Pull records out of the retired PC/data layout, if it is there.

        Read-only: the old files are left exactly where they are, so a
        migration that goes wrong costs nothing. The full per-sample
        records are preferred; the index entry is the fallback for a
        sample whose record file has gone missing.
        """
        if self.archive_path != ARCHIVE_PATH:
            return None

        if not LEGACY_INDEX_PATH.exists():
            return None

        index = _read_json(LEGACY_INDEX_PATH)

        if not isinstance(index, dict) or not isinstance(
            index.get("samples"), list
        ):
            return None

        samples = []

        for entry in index["samples"]:
            sample_id = entry.get("sample_id")

            if not sample_id:
                continue

            record = _read_json(
                LEGACY_RECORD_DIR / "{}.json".format(sample_id)
            )

            samples.append(record if isinstance(record, dict) else entry)

        if not samples:
            return None

        self.migrated_from = str(LEGACY_INDEX_PATH)

        return {"version": ARCHIVE_VERSION, "samples": samples}

    def _require_ready(self):
        if not self.ready:
            raise StorageError(
                self.error or "{} could not be parsed.".format(
                    self.archive_path
                ),
                "SAMPLES_ARCHIVE_INVALID",
            )

    def _records(self):
        return self.archive.setdefault("samples", [])

    def _write(self):
        _write_json(self.archive_path, self.archive)

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------

    def count(self):
        return len(self.archive.get("samples", []))

    def summaries(self):
        """Compact rows for the Sample Database table."""
        return [summary_of(record) for record in self._records()]

    def find_summary(self, sample_id):
        record = self.get_sample(sample_id)

        return summary_of(record) if record is not None else None

    def has_sample(self, sample_id):
        return self.get_sample(sample_id) is not None

    def get_state(self, sample_id):
        record = self.get_sample(sample_id)

        return record.get("state") if record else None

    def get_sample(self, sample_id):
        """Full scientific record for one Sample ID, or None."""
        for record in self._records():
            if record.get("sample_id") == sample_id:
                return record

        return None

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

    def save(self, record):
        """
        Persist one COMPLETE sample record.

        Everything the record carries is written: all three 18-channel
        spectra, the White and Dark actually used, the comparison
        against every material and the whole analysis block. Nothing is
        summarised away - the compact table derives its columns from the
        stored record at read time instead.
        """
        self._require_ready()

        sample_id = validate_sample_id(record.get("sample_id"))
        record["sample_id"] = sample_id

        records = self._records()

        for position, existing in enumerate(records):
            if existing.get("sample_id") == sample_id:
                records[position] = record

                break

        else:
            records.append(record)

        self._write()

        return summary_of(record)

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
        """Change a Sample ID, keeping every scientific value intact."""
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

        if self.has_sample(new_id):
            raise StorageError(
                "Sample ID {} already exists.".format(new_id),
                "SAMPLE_ID_ALREADY_EXISTS",
            )

        record["sample_id"] = new_id

        self._write()

        return record

    def delete(self, sample_id):
        """Permanently remove one sample from the archive."""
        self._require_ready()

        sample_id = validate_sample_id(sample_id)
        record = self.get_sample(sample_id)

        if record is None:
            raise StorageError(
                "No stored sample with ID {}.".format(sample_id),
                "SAMPLE_NOT_FOUND",
            )

        summary = summary_of(record)
        previous = list(self._records())

        self.archive["samples"] = [
            entry for entry in previous
            if entry.get("sample_id") != sample_id
        ]

        try:
            self._write()

        except StorageError:
            # Put the record back: nothing was actually deleted.
            self.archive["samples"] = previous

            raise

        return summary

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------

    def status(self):
        return {
            "ready": self.ready,
            "error": self.error,
            "archive_file": str(self.archive_path),
            "samples_saved": self.count(),
            "migrated_from": self.migrated_from,
        }
