"""
The device's own store for acquisitions the operator has not imported.

WHY THE DEVICE STORES ANYTHING AT ALL

The ownership model this module exists to make true is:

    MEASURE
        |
    the ESP32 owns the not-yet-imported measurement
        |
    explicit Import
        |
    the PC owns an archived copy

An owner that forgets everything when it reboots is not an owner. The
retained acquisitions used to live only in `Carousel.slots[n]`, which
is RAM: a brownout, a watchdog reset, a firmware deployment or a stray
`machine.reset()` destroyed the only copy of a spectrum that cannot be
reacquired once the soil has been tipped out of the cup.

The PC used to compensate for that by persisting its own working set,
which solved the data-loss problem by breaking the ownership model
instead - a measurement became a stored PC file the instant it was
taken, and "Import" no longer decided anything. Fixing that on the PC
alone would have left the acquisition with no durable home anywhere.
So it gets one here, on the computer that owns it.

WHAT THIS IS NOT

It is not an archive and it is not a database. It holds at most one
acquisition per physical slot - four - because the carousel has four
cups and a spectrum belongs to the cup it was taken from. It has no
history, no versions and no queries. The PC archive is the scientific
record; this is the last thing the device measured, kept until someone
takes it away.

HOW A WRITE SURVIVES A POWER CUT

Write the whole record to a temporary file, flush it, then rename it
over the real one. Rename is atomic on the device filesystem, so a
power cut leaves either the previous complete record or the new
complete record on the disk, and never half of either. Measured on the
board: littlefs2, and `os.rename` over an existing path succeeds.

A record is read back and parsed before the write is called a success.
A file that survived the rename but does not parse is worse than no
file, because the PC would import it.

WHY ONE FILE PER SLOT

A single file holding all four would mean every measurement rewrites
the other three, so one bad write could cost four spectra instead of
one. Four files also make the failure granular: a corrupt slot 2 is
discarded on load and slots 1, 3 and 4 come back.

FLASH WEAR IS NOT A CONCERN HERE, and it is worth saying why rather
than leaving it to be rediscovered. A write happens once per completed
measurement and once per delete. A competition run is tens of
measurements; the device filesystem is 2 MB of wear-levelled flash
rated for tens of thousands of cycles per block. The store would have
to be written continuously for years to matter.

A PERSISTENCE FAILURE NEVER COSTS THE MEASUREMENT. By the time this is
called the spectrum already exists in RAM and is already on its way
back to the PC in the response. If the filesystem is full or broken,
the acquisition is still retained in RAM, still listed, still
fetchable, still deletable - and the response says, in a field the
operator's screen prints, that this one is not protected against a
reset. Refusing the measurement because the disk is full would throw
away the science to protect the copy of it.
"""

import json
import os

import config


# Data, not code. A firmware deployment cleans the device filesystem so
# that no stale module can shadow a new one; this directory holds no
# modules, so it is preserved across a clean unless the operator asks
# for it to go. Keeping it out of the root also means the manifest
# check has one name to know about instead of four.
#
# READ FROM CONFIG ON EVERY CALL, never captured in a module-level
# constant. On the device this is a fixed path and the indirection
# costs nothing; on the HOST, where the software suite imports these
# modules and drives them against fakes, "/retained" is a real
# absolute path on somebody's disk. Binding it at import time meant a
# test could not redirect it, and the first run of this feature wrote
# two spectra to the root of the developer's drive - which the next
# test then read back as though the device remembered them.


def _directory():
    return config.RETAINED_DIR


def _record_path(slot_id):
    return "{}/slot{}.json".format(_directory(), slot_id)


def _temp_path(slot_id):
    return "{}/slot{}.tmp".format(_directory(), slot_id)


# The shape written to the disk. Bumped only if the record layout
# changes; a file at any other version is discarded on load rather than
# guessed at, because a misread spectrum is worse than a missing one.
STORE_VERSION = 1


def _exists(path):
    try:
        os.stat(path)

        return True

    except OSError:
        return False


def _ensure_directory():
    directory = _directory()

    if _exists(directory):
        return

    os.mkdir(directory)


def _remove(path):
    """Remove a path, treating "it was not there" as success."""
    try:
        os.remove(path)

    except OSError:
        # The only thing os.remove raises, on the device and on the
        # host, and "it was not there" is the case this exists for.
        pass


def available():
    """
    Whether the device filesystem can hold retained acquisitions.

    Reported rather than assumed, so `get_status` can tell the operator
    that this board is running without durable retention instead of
    letting them find out after a reset.
    """
    try:
        _ensure_directory()

        return True

    except Exception:                                  # noqa: BLE001
        return False


def _valid(payload, slot_id):
    """
    Whether a file read back from the disk is a record we may use.

    Deliberately strict. Anything that fails here is discarded, and a
    discarded slot simply has no retained acquisition - which is a
    state the whole system already handles. Repairing a damaged
    scientific record by guessing is not on the list of options.
    """
    if not isinstance(payload, dict):
        return False

    if payload.get("store_version") != STORE_VERSION:
        return False

    if payload.get("slot_id") != slot_id:
        return False

    measurement = payload.get("measurement")

    if not isinstance(measurement, dict):
        return False

    # An acquisition with no illumination blocks is not an acquisition.
    # It is what a truncated write looks like after it has still
    # managed to be valid JSON.
    if not measurement.get("illuminations"):
        return False

    return True


def load_all():
    """
    Every retained acquisition still on the disk, by slot.

    Called once, at startup. A slot whose file is missing, unreadable,
    unparseable or the wrong shape is left out and its file is removed,
    so a single bad record cannot make the board fail to boot and
    cannot come back to be read again on the next start.

    Returns {slot_id: measurement}. Never raises: the carousel has to
    be able to come up even on a filesystem that has gone bad, because
    a module that will not start is a module that cannot be diagnosed.
    """
    found = {}

    try:
        _ensure_directory()

    except Exception:                                  # noqa: BLE001
        return found

    for slot_id in range(1, config.CAROUSEL_SLOT_COUNT + 1):
        path = _record_path(slot_id)

        # A temporary file left behind is a write that was interrupted
        # before its rename. The real file is therefore still the
        # previous complete record, and the leftover is rubbish.
        _remove(_temp_path(slot_id))

        if not _exists(path):
            continue

        try:
            with open(path, "r") as stream:
                payload = json.load(stream)

        except Exception:                              # noqa: BLE001
            # Anything at all. A record that cannot be read is a record
            # the device does not have, and the whole point of loading
            # here is that a bad one must not stop the board booting.
            _remove(path)

            continue

        if not _valid(payload, slot_id):
            _remove(path)

            continue

        found[slot_id] = payload["measurement"]

    return found


def save(slot_id, measurement):
    """
    Persist ONE slot's acquisition, atomically.

    Returns None on success, or a short string saying what went wrong.
    It does not raise, and the caller does not abort the measurement on
    a failure: see the module docstring. The string is carried back to
    the operator so a board that is silently running without durable
    retention is never mistaken for one that is not.
    """
    path = _record_path(slot_id)
    temporary = _temp_path(slot_id)

    payload = {
        "store_version": STORE_VERSION,
        "slot_id": slot_id,
        "measurement": measurement,
    }

    try:
        _ensure_directory()

        with open(temporary, "w") as stream:
            json.dump(payload, stream)

        # PROVE IT PARSES BEFORE IT BECOMES THE RECORD. A filesystem
        # that ran out of space mid-write can return from json.dump
        # without an error and leave a truncated file behind; renaming
        # that over a good record would replace a spectrum with a
        # fragment.
        with open(temporary, "r") as stream:
            written = json.load(stream)

        if not _valid(written, slot_id):
            _remove(temporary)

            return "the record did not read back intact"

        # ATOMIC ON THE DEVICE, and that is the case that matters.
        # Measured on the board: littlefs2, where renaming onto an
        # existing name replaces it in one operation - so a power cut
        # leaves either the whole previous record or the whole new one.
        #
        # THE FALLBACK IS FOR THE HOST. The software suite imports this
        # module and runs it on Windows, where rename onto an existing
        # name raises EEXIST; without this the second write to a slot
        # silently failed and the store kept serving the first one.
        # Guarded on the destination actually existing, so a rename
        # that failed for any OTHER reason still propagates instead of
        # being answered by deleting a good record.
        try:
            os.rename(temporary, path)

        except OSError:
            if not _exists(path):
                raise

            _remove(path)
            os.rename(temporary, path)

    # EVERY failure, not just the ones that were easy to name.
    #
    # It caught OSError and ValueError, which covers a full disk and a
    # corrupt read-back and misses the rest. A MemoryError raised while
    # serializing the record - the board is small, and this is the
    # largest structure it ever writes - escaped into `mark_occupied`,
    # out of `handle_measure_raw`, and turned a completed acquisition
    # into a failed command. That is the exact inversion this module's
    # docstring promises never to make: the spectrum already exists,
    # and losing it to protect the backup of it is the worst available
    # outcome.
    except Exception as error:                         # noqa: BLE001
        _remove(temporary)

        return "could not store the acquisition: {}".format(error)

    return None


def drop(slot_id):
    """
    Delete ONE slot's persisted acquisition.

    Missing is success. The caller has already removed the RAM copy,
    and a delete that refuses because the file was not there would turn
    a board with no durable retention into a board that cannot forget.
    """
    _remove(_record_path(slot_id))
    _remove(_temp_path(slot_id))


def clear():
    """Delete every persisted acquisition. Physical slot state is not touched."""
    for slot_id in range(1, config.CAROUSEL_SLOT_COUNT + 1):
        drop(slot_id)


def status():
    """
    What the store holds, for get_status.

    Cheap: it stats four paths and never opens one. get_status is
    polled on every screen refresh, and reading four spectra to answer
    "is anything retained" would put kilobytes on the wire each time.
    """
    persisted = []

    for slot_id in range(1, config.CAROUSEL_SLOT_COUNT + 1):
        if _exists(_record_path(slot_id)):
            persisted.append(slot_id)

    return {
        "available": available(),
        "directory": _directory(),
        "store_version": STORE_VERSION,
        "persisted_slots": persisted,
        "count": len(persisted),
    }
