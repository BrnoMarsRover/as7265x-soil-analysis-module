# sample_store.py
# Persistent scientific sample storage on the ESP32 flash filesystem.
#
# This is the authoritative record of the competition run. It survives
# reboot. The physical carousel occupancy in carousel.py does not, and
# the two are deliberately never reconciled: after a reset the operator
# must look at the real mechanism and re-declare it.
#
# Layout
# ------
#     samples.json          index of every measured sample (small)
#     samples/S001.json     one complete scientific record per sample
#
# The records are split across files on purpose. A full record holds
# three 18-channel spectra plus a match against every database material,
# roughly 3 kB of JSON. Keeping thirty of those in one file would mean
# parsing ~90 kB into an ESP32 heap on every append, which is a reliable
# way to hit MemoryError halfway through a competition. The index stays
# small enough to load at any time, and a single record is only read
# when it is actually requested.
#
# Neither file is shipped with the firmware, so uploading new code can
# never overwrite collected competition data.

import json
import os

import config


class StorageError(Exception):
    """
    Sample data could not be read or written.

    Carries a machine-readable code so the PC can tell a missing sample
    from a corrupted database from a failed write.
    """

    def __init__(self, message, code="SAMPLE_SAVE_ERROR"):
        # super() - MicroPython cannot call Exception.__init__ unbound.
        super().__init__(message)

        self.message = str(message)
        self.code = str(code)


INDEX_VERSION = 1


# ----------------------------------------------------------------------
# filesystem helpers
# ----------------------------------------------------------------------

def _exists(path):
    try:
        os.stat(path)
        return True

    except OSError:
        return False


def _remove_quiet(path):
    try:
        os.remove(path)

    except OSError:
        pass


def _safe_write_json(path, obj):
    """
    Write JSON so that an interrupted write cannot destroy the old file.

        1. write everything to <path>.tmp
        2. move the current file aside to <path>.bak
        3. rename the temporary file into place
        4. drop the backup

    If power is lost between steps 2 and 3 the file is missing but the
    backup is intact, and _read_json below recovers from it.
    """
    tmp = path + ".tmp"
    bak = path + ".bak"

    _remove_quiet(tmp)

    try:
        with open(tmp, "w") as handle:
            json.dump(obj, handle)

    except OSError as error:
        _remove_quiet(tmp)

        raise StorageError(
            "could not write {}: {}".format(path, error)
        )

    try:
        _remove_quiet(bak)

        if _exists(path):
            os.rename(path, bak)

        os.rename(tmp, path)

        _remove_quiet(bak)

    except OSError as error:
        raise StorageError(
            "could not replace {}: {}".format(path, error)
        )


def _read_json(path, default):
    """Read JSON, falling back to the interrupted-write backup."""
    for candidate in (path, path + ".bak"):
        try:
            with open(candidate, "r") as handle:
                return json.load(handle)

        except (OSError, ValueError):
            continue

    return default


def validate_sample_id(sample_id):
    """
    Check a Sample ID coming from the PC.

    Sample IDs double as filenames, so the character set is restricted.
    A Sample ID is the scientific identity of the soil and has nothing
    to do with the physical slot number: S001, D1 and AB1 are all valid
    and may sit in any slot.
    """
    if not isinstance(sample_id, str):
        raise StorageError("Sample ID must be a string.")

    sample_id = sample_id.strip()

    if not sample_id:
        raise StorageError("Sample ID must not be empty.")

    if len(sample_id) > config.SAMPLE_ID_MAX_LENGTH:
        raise StorageError(
            "Sample ID must be at most {} characters.".format(
                config.SAMPLE_ID_MAX_LENGTH
            )
        )

    for character in sample_id:
        is_alnum = (
            ("0" <= character <= "9")
            or ("a" <= character <= "z")
            or ("A" <= character <= "Z")
        )

        if is_alnum:
            continue

        if character in config.SAMPLE_ID_ALLOWED_EXTRA:
            continue

        raise StorageError(
            "Sample ID may only contain letters, digits and '{}'.".format(
                config.SAMPLE_ID_ALLOWED_EXTRA
            )
        )

    return sample_id


class SampleStore:
    """Persistent index plus one file per measured sample."""

    def __init__(self, index_path=None, record_dir=None):
        if index_path is None:
            index_path = config.SAMPLE_INDEX_FILE

        if record_dir is None:
            record_dir = config.SAMPLE_RECORD_DIR

        self.index_path = index_path
        self.record_dir = record_dir

        self.ready = False
        self.error = None

        self.index = {"version": INDEX_VERSION, "samples": []}

        self.load()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def load(self):
        """
        Load the index, creating an empty one in RAM if none exists.

        A missing index is normal on a fresh board and is not an error.
        Nothing is written to flash until the first sample is saved.
        """
        self.error = None

        # "No file at all" and "file present but unreadable" are very
        # different situations. Treating a damaged index as an empty one
        # would show the operator a blank database and let the next
        # write overwrite data that might still be recoverable by hand.
        present = (
            _exists(self.index_path)
            or _exists(self.index_path + ".bak")
        )

        data = _read_json(self.index_path, None)

        if data is None and not present:
            # Fresh board: nothing has been measured yet.
            self.index = {"version": INDEX_VERSION, "samples": []}
            self.ready = True

            return self.index

        if data is None or not isinstance(data, dict) or not isinstance(
            data.get("samples"), list
        ):
            # Do not delete anything that looks damaged; report it and
            # start a fresh in-RAM index so the module stays usable.
            self.index = {"version": INDEX_VERSION, "samples": []}
            self.ready = False
            self.error = (
                "{} is present but could not be parsed as a sample "
                "index. It has NOT been modified - recover or remove it "
                "by hand before measuring again.".format(self.index_path)
            )

            return self.index

        self.index = data
        self.index.setdefault("version", INDEX_VERSION)
        self.ready = True

        return self.index

    def _ensure_record_dir(self):
        if _exists(self.record_dir):
            return

        try:
            os.mkdir(self.record_dir)

        except OSError as error:
            raise StorageError(
                "could not create {}: {}".format(
                    self.record_dir,
                    error
                )
            )

    def _record_path(self, sample_id):
        return "{}/{}.json".format(self.record_dir, sample_id)

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------

    def count(self):
        return len(self.index.get("samples", []))

    def summaries(self):
        """Compact information about every persistently stored sample."""
        return list(self.index.get("samples", []))

    def find_summary(self, sample_id):
        for entry in self.index.get("samples", []):
            if entry.get("sample_id") == sample_id:
                return entry

        return None

    def has_sample(self, sample_id):
        return self.find_summary(sample_id) is not None

    def get_sample(self, sample_id):
        """Full scientific record for one Sample ID, or None."""
        sample_id = validate_sample_id(sample_id)

        record = _read_json(self._record_path(sample_id), None)

        if record is None:
            return None

        return record

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------

    def _summarize(self, record):
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
            "best_match_score": analysis.get("best_match_score"),
            "status": analysis.get("status"),
            "confidence": analysis.get("confidence"),
            "record": self._record_path(record.get("sample_id"))
        }

    def get_state(self, sample_id):
        """Lifecycle state of a stored sample, or None if unknown."""
        summary = self.find_summary(sample_id)

        if summary is None:
            return None

        return summary.get("state")

    def _write_index(self):
        _safe_write_json(self.index_path, self.index)
        self.ready = True
        self.error = None

    def add_sample(self, record):
        """
        Persist one complete measured sample.

        The record file is written first and the index second, so a
        failure never leaves the index pointing at a file that does not
        exist. If anything fails, StorageError is raised and the caller
        must not report the measurement as saved.
        """
        self._require_ready()

        sample_id = validate_sample_id(record.get("sample_id"))
        record["sample_id"] = sample_id

        self._ensure_record_dir()

        _safe_write_json(self._record_path(sample_id), record)

        summary = self._summarize(record)

        samples = self.index.setdefault("samples", [])

        replaced = False

        for position in range(len(samples)):
            if samples[position].get("sample_id") == sample_id:
                samples[position] = summary
                replaced = True

                break

        if not replaced:
            samples.append(summary)

        self._write_index()

        return summary

    def update_metadata(self, sample_id, metadata):
        """
        Merge mission metadata into an already measured sample.

        Spectral data, matches and analysis are never touched here: this
        only adds the human/external context that the PC collects after
        the measurement.
        """
        self._require_ready()

        sample_id = validate_sample_id(sample_id)

        record = self.get_sample(sample_id)

        if record is None:
            return None

        if not isinstance(metadata, dict):
            raise StorageError(
                "metadata must be a JSON object",
                code="SAMPLE_UPDATE_FAILED",
            )

        current = record.get("metadata")

        if not isinstance(current, dict):
            current = {}

        for key in metadata:
            current[key] = metadata[key]

        record["metadata"] = current

        _safe_write_json(self._record_path(sample_id), record)

        return record

    def _require_ready(self):
        """
        Refuse every write while the database looks damaged.

        Overwriting a corrupted samples.json would destroy competition
        data that might still be recoverable by hand.
        """
        if not self.ready:
            raise StorageError(
                self.error
                or "{} is present but could not be parsed.".format(
                    self.index_path
                ),
                code="SAMPLES_JSON_INVALID",
            )

    def rename_sample(self, old_id, new_id):
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
                code="SAMPLE_NOT_FOUND",
            )

        if new_id == old_id:
            return record

        if self.find_summary(new_id) is not None:
            raise StorageError(
                "Sample ID {} already exists.".format(new_id),
                code="SAMPLE_ID_ALREADY_EXISTS",
            )

        record["sample_id"] = new_id

        self._ensure_record_dir()
        _safe_write_json(self._record_path(new_id), record)

        samples = self.index.setdefault("samples", [])

        for position in range(len(samples)):
            if samples[position].get("sample_id") == old_id:
                samples[position] = self._summarize(record)

                break

        self._write_index()

        # Only now is the old copy redundant.
        _remove_quiet(self._record_path(old_id))

        return record

    def delete_sample(self, sample_id):
        """
        Permanently remove one sample from the persistent database.

        The index is rewritten first: it is the authoritative list, so a
        record file left behind by an interrupted delete is harmless.
        """
        self._require_ready()

        sample_id = validate_sample_id(sample_id)

        summary = self.find_summary(sample_id)

        if summary is None:
            raise StorageError(
                "No stored sample with ID {}.".format(sample_id),
                code="SAMPLE_NOT_FOUND",
            )

        samples = self.index.setdefault("samples", [])

        self.index["samples"] = [
            entry for entry in samples
            if entry.get("sample_id") != sample_id
        ]

        try:
            self._write_index()

        except StorageError as error:
            # Put the entry back: nothing was actually deleted.
            self.index["samples"] = samples

            raise StorageError(
                "Could not write updated {}: {}".format(
                    self.index_path, error.message
                ),
                code="SAMPLE_DELETE_FAILED",
            )

        _remove_quiet(self._record_path(sample_id))

        return summary

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------

    def status(self):
        return {
            "ready": self.ready,
            "error": self.error,
            "index_file": self.index_path,
            "record_dir": self.record_dir,
            "samples_saved": self.count()
        }
