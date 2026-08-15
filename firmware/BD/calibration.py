"""
Calibration handling: the legacy database calibration and the active
full-spectral calibration, kept permanently apart.

Why two
-------
The spectra in database.json were normalized against one specific
White/Dark pair. That pair is not a setting, it is part of the database:
normalize the library against a newer White and every stored number
quietly means something else.

So the legacy calibration is frozen and is the ONLY one ever used to
compare against database.json. A new full calibration - Dark plus a
white reference under each of WHITE, UV and IR - describes the
instrument as it is now, and is used for the scientific record and for
measurement quality.

Every new measurement is normalized BOTH ways. That is precisely what
lets the operator recalibrate the instrument without remeasuring the
material library.

Storage
-------
    calibrations/FREYA_FULL_SPECTRAL_CAL_<stamp>.json   immutable
    calibrations/active.json                            pointer

Calibration files are never rewritten and never deleted by this module.
Only the pointer changes.
"""

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

import config
from channels import CHANNELS
from sample_analysis import copy_channels, validate_spectrum

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


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False

    return not (math.isnan(value) or math.isinf(value))


def new_calibration_id():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    return "{}_{}".format(config.CALIBRATION_ID_PREFIX, stamp)


def _write_json(path, payload):
    """Atomic write, so an interrupted save cannot truncate a file."""
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


# The legacy calibration - the White/Dark pair database.json was built
# against - is database.References. It lives there because it is loaded
# from references.json alongside the library it belongs to, and it is
# deliberately the one calibration class with no save path at all.


# ----------------------------------------------------------------------
# full spectral calibration
# ----------------------------------------------------------------------

def build_calibration(dark_block, white_blocks, sensor_settings,
                      repeats, notes=None):
    """
    Assemble a calibration document from aggregated acquisition blocks.

    `dark_block` and each entry of `white_blocks` are what
    statistics.aggregate_block produced: {"spectrum", "statistics",
    "acquisitions"}. Nothing is invented here - the arrays are the
    measurements, and the statistics travel with them.
    """
    missing = [name for name in ILLUMINATIONS if name not in white_blocks]

    if missing:
        raise CalibrationError(
            "CALIBRATION_INCOMPLETE",
            "White reference missing for: {}.".format(", ".join(missing)),
            {"missing_illuminations": missing},
        )

    return {
        "schema_version": config.CALIBRATION_SCHEMA_VERSION,
        "calibration_id": new_calibration_id(),
        "kind": "FULL",
        "created_at": utc_now(),
        "science_version": config.SCIENCE_VERSION,
        "repeats": repeats,
        "notes": notes,

        "sensor_settings": sensor_settings,

        "dark": {
            "aggregated": dark_block["spectrum"],
            "statistics": dark_block["statistics"],
            "acquisitions": dark_block.get("acquisitions"),
        },

        "white_reference": {
            name: {
                "aggregated": white_blocks[name]["spectrum"],
                "statistics": white_blocks[name]["statistics"],
                "acquisitions": white_blocks[name].get("acquisitions"),
            }
            for name in ILLUMINATIONS
        },

        "validation": None,
    }


def validate_calibration(document, sensor_settings=None):
    """
    Everything that must be true before a calibration may be activated.

    Returns {"status": PASS|WARNING|FAIL, "warnings": [], "failures": []}.
    A FAIL blocks activation; a WARNING does not, but is shown.
    """
    warnings = []
    failures = []

    def fail(code, message, **details):
        failures.append(dict(code=code, message=message, details=details))

    def warn(code, message, **details):
        warnings.append(dict(code=code, message=message, details=details))

    if not isinstance(document, dict):
        return {
            "status": "FAIL",
            "warnings": [],
            "failures": [{
                "code": "CALIBRATION_BAD_SCHEMA",
                "message": "Calibration is not an object.",
                "details": {},
            }],
        }

    # -- schema --------------------------------------------------------
    version = document.get("schema_version")

    if version != config.CALIBRATION_SCHEMA_VERSION:
        fail(
            "CALIBRATION_BAD_SCHEMA",
            "Schema version {} is not the expected {}.".format(
                version, config.CALIBRATION_SCHEMA_VERSION
            ),
            found=version,
        )

    if not document.get("calibration_id"):
        fail("CALIBRATION_BAD_SCHEMA", "Calibration has no ID.")

    # -- dark ----------------------------------------------------------
    dark = ((document.get("dark") or {}).get("aggregated")) or {}
    dark_missing = validate_spectrum(dark)

    if dark_missing:
        fail(
            "DARK_CALIBRATION_FAILED",
            "Dark reference is missing channels: {}.".format(
                ",".join(dark_missing)
            ),
            missing=dark_missing,
        )

    # -- white references, one per illumination ------------------------
    references = document.get("white_reference") or {}

    for name in ILLUMINATIONS:
        block = references.get(name) or {}
        spectrum = block.get("aggregated") or {}
        spectrum_missing = validate_spectrum(spectrum)

        code = "{}_CALIBRATION_FAILED".format(name.upper())

        if spectrum_missing:
            fail(
                code,
                "{} white reference is missing channels: {}.".format(
                    name.upper(), ",".join(spectrum_missing)
                ),
                illumination=name,
                missing=spectrum_missing,
            )

            continue

        if dark_missing:
            continue

        # Denominator quality: how much light actually reaches each
        # channel under this lamp.
        weak = []
        dead = []

        for channel in CHANNELS:
            denominator = spectrum[channel] - dark[channel]

            if denominator < config.MIN_DENOMINATOR:
                dead.append(channel)
            elif denominator < config.WEAK_DENOMINATOR:
                weak.append(channel)

        if len(dead) > config.MAX_WEAK_CHANNELS_FAIL:
            fail(
                code,
                "{} illumination reaches almost nothing: {} channels "
                "below the minimum denominator.".format(
                    name.upper(), len(dead)
                ),
                illumination=name, dead_channels=dead,
            )

        elif dead:
            # UV and IR legitimately do not illuminate the whole visible
            # range, so a few dead channels are expected there.
            warn(
                "WEAK_ILLUMINATION",
                "{} illumination is below the minimum denominator on "
                "{} channel(s).".format(name.upper(), len(dead)),
                illumination=name, dead_channels=dead,
            )

        if len(weak) > config.MAX_WEAK_CHANNELS_WARNING:
            warn(
                "WEAK_ILLUMINATION",
                "{} illumination is weak on {} channel(s).".format(
                    name.upper(), len(weak)
                ),
                illumination=name, weak_channels=weak,
            )

        # Repeatability of the reference itself.
        statistics = block.get("statistics") or {}
        unstable = statistics.get("unstable_channels") or []

        if len(unstable) > config.MAX_UNSTABLE_CHANNELS_FAIL:
            fail(
                code,
                "{} white reference is not repeatable: {} unstable "
                "channels.".format(name.upper(), len(unstable)),
                illumination=name, unstable_channels=unstable,
            )

        elif len(unstable) > config.MAX_UNSTABLE_CHANNELS_WARNING:
            warn(
                "REFERENCE_REPEATABILITY",
                "{} white reference has {} unstable channel(s).".format(
                    name.upper(), len(unstable)
                ),
                illumination=name, unstable_channels=unstable,
            )

    # -- dark repeatability -------------------------------------------
    dark_statistics = (document.get("dark") or {}).get("statistics") or {}
    dark_unstable = dark_statistics.get("unstable_channels") or []

    if len(dark_unstable) > config.MAX_UNSTABLE_CHANNELS_FAIL:
        fail(
            "DARK_CALIBRATION_FAILED",
            "Dark reference is not repeatable: {} unstable "
            "channels.".format(len(dark_unstable)),
            unstable_channels=dark_unstable,
        )

    elif len(dark_unstable) > config.MAX_UNSTABLE_CHANNELS_WARNING:
        warn(
            "REFERENCE_REPEATABILITY",
            "Dark reference has {} unstable channel(s).".format(
                len(dark_unstable)
            ),
            unstable_channels=dark_unstable,
        )

    # -- sensor settings ----------------------------------------------
    settings = sensor_settings or document.get("sensor_settings") or {}

    expected = {
        "measurement_mode": 3,
        "integration_cycles": 100,
        "gain": 2,
    }

    for name, want in expected.items():
        found = settings.get(name)

        if found is None:
            warn(
                "SENSOR_SETTING_UNKNOWN",
                "Sensor setting '{}' was not recorded.".format(name),
                setting=name,
            )

        elif found != want:
            fail(
                "CALIBRATION_INCOMPATIBLE",
                "Calibration was taken at {} = {}, but the pipeline "
                "expects {}.".format(name, found, want),
                setting=name, found=found, expected=want,
            )

    # -- lamps off -----------------------------------------------------
    if document.get("bulbs_off") is False:
        fail(
            "CALIBRATION_VALIDATION_FAILED",
            "A lamp was still on when the calibration finished.",
        )

    status = "FAIL" if failures else ("WARNING" if warnings else "PASS")

    return {"status": status, "warnings": warnings, "failures": failures}


class FullCalibration:
    """An active full-spectral calibration: Dark plus WHITE/UV/IR."""

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

    def zero_denominator_channels(self, illumination):
        white = self.white_for(illumination)

        return [
            channel for channel in CHANNELS
            if abs(white[channel] - self.dark[channel])
            < config.MIN_DENOMINATOR
        ]

    def validate(self, sensor_settings=None):
        return validate_calibration(self.document, sensor_settings)

    def status(self):
        return {
            "kind": self.kind,
            "calibration_id": self.calibration_id,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
            "file": str(self.path) if self.path else None,
            "protected": False,
            "repeats": self.repeats,
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
            "validation": (self.document.get("validation") or {}).get(
                "status"
            ),
        }


# ----------------------------------------------------------------------
# store
# ----------------------------------------------------------------------

class CalibrationStore:
    """
    Immutable calibration files plus a pointer to the active one.

    Saving never overwrites: each calibration gets its own file named
    after its ID. Activation only rewrites the pointer.
    """

    def __init__(self, directory=None, pointer=None):
        self.directory = Path(directory or config.CALIBRATION_DIR)
        self.pointer_path = Path(pointer or config.ACTIVE_CALIBRATION_POINTER)

    # -- files ---------------------------------------------------------

    def path_for(self, calibration_id):
        return self.directory / "{}.json".format(calibration_id)

    def save(self, document):
        """
        Write a calibration to its own immutable file.

        Refuses to overwrite: an ID that already exists is a bug, not
        something to silently replace.
        """
        calibration_id = document.get("calibration_id")

        if not calibration_id:
            raise CalibrationError(
                "CALIBRATION_BAD_SCHEMA", "Calibration has no ID."
            )

        path = self.path_for(calibration_id)

        if path.exists():
            raise CalibrationError(
                "CALIBRATION_ALREADY_EXISTS",
                "{} already exists; calibrations are immutable.".format(path),
            )

        _write_json(path, document)

        return path

    def load(self, calibration_id):
        document = _read_json(self.path_for(calibration_id))

        if document is None:
            raise CalibrationError(
                "CALIBRATION_NOT_FOUND",
                "No calibration named {}.".format(calibration_id),
            )

        return FullCalibration(document, self.path_for(calibration_id))

    def history(self):
        """Every calibration on disk, newest first, plus the legacy one."""
        entries = []

        if self.directory.exists():
            for path in sorted(self.directory.glob("*.json")):
                if path.name == self.pointer_path.name:
                    continue

                document = _read_json(path)

                if not isinstance(document, dict):
                    continue

                entries.append({
                    "calibration_id": document.get("calibration_id"),
                    "created_at": document.get("created_at"),
                    "kind": document.get("kind", "FULL"),
                    "validation": (document.get("validation") or {}).get(
                        "status"
                    ),
                    "file": str(path),
                    "protected": False,
                })

        entries.sort(key=lambda entry: entry.get("created_at") or "",
                     reverse=True)

        active = self.active_id()

        for entry in entries:
            entry["active"] = entry["calibration_id"] == active

        return entries

    # -- active pointer ------------------------------------------------

    def active_id(self):
        pointer = _read_json(self.pointer_path)

        if isinstance(pointer, dict):
            return pointer.get("calibration_id")

        return None

    def activate(self, calibration_id):
        """
        Point at a calibration. The file itself is not touched.

        Refuses anything that does not exist or does not validate - a
        calibration that failed its checks must never become active.
        """
        calibration = self.load(calibration_id)
        result = calibration.validate()

        if result["status"] == "FAIL":
            raise CalibrationError(
                "CALIBRATION_VALIDATION_FAILED",
                "{} did not pass validation and cannot be "
                "activated.".format(calibration_id),
                {"validation": result},
            )

        _write_json(self.pointer_path, {
            "calibration_id": calibration_id,
            "activated_at": utc_now(),
            "schema_version": config.CALIBRATION_SCHEMA_VERSION,
        })

        return calibration

    def active(self):
        """The active calibration, or None if none has been made yet."""
        calibration_id = self.active_id()

        if not calibration_id:
            return None

        try:
            return self.load(calibration_id)

        except CalibrationError:
            return None
