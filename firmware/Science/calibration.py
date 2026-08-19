"""
Full spectral calibration: construction and scientific validation.

Storing calibrations, versioning them and tracking which one is active is
BD/calibrations.py. This module decides what a calibration IS and whether
it is good enough to use — the scientific half of the split.

Why two calibrations exist at all
---------------------------------
The spectra in DB1 were normalized against one specific White/Dark pair.
That pair is not a setting, it is part of the database: normalize the
library against a newer White and every stored number quietly means
something else.

So the legacy calibration is frozen and is the ONLY one ever used to
compare against DB1. A new full calibration — Dark plus a white reference
under each of WHITE, UV and IR — describes the instrument as it is now.

Every new measurement is normalized BOTH ways. That is precisely what lets
the instrument be recalibrated without remeasuring the material library.
"""

from datetime import datetime, timezone

from BD import config as bd_config
from BD.channels import CHANNELS, ILLUMINATIONS, validate_spectrum
from Science import config


class CalibrationBuildError(Exception):
    """A calibration document cannot be assembled from what was supplied."""

    def __init__(self, code, message, details=None):
        super().__init__(message)

        self.code = code
        self.message = message
        self.details = details or {}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def new_calibration_id():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    return "{}_{}".format(bd_config.CALIBRATION_ID_PREFIX, stamp)


def build_calibration(dark_block, white_blocks, sensor_settings,
                      repeats, notes=None):
    """
    Assemble a calibration document from aggregated acquisition blocks.

    `dark_block` and each entry of `white_blocks` are what
    Science.preprocessing.aggregate_block produced:
    {"spectrum", "statistics", "acquisitions"}.

    The document is a BD record — its schema version comes from BD, which
    owns the storage contract. This is the permitted Science -> BD
    direction; the reverse is forbidden.
    """
    missing = [name for name in ILLUMINATIONS if name not in white_blocks]

    if missing:
        raise CalibrationBuildError(
            "CALIBRATION_INCOMPLETE",
            "White reference missing for: {}.".format(", ".join(missing)),
            {"missing_illuminations": missing},
        )

    return {
        "schema_version": bd_config.CALIBRATION_SCHEMA_VERSION,
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

    if version != bd_config.CALIBRATION_SCHEMA_VERSION:
        fail(
            "CALIBRATION_BAD_SCHEMA",
            "Schema version {} is not the expected {}.".format(
                version, bd_config.CALIBRATION_SCHEMA_VERSION
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
            # range, so a few dead channels are expected there. This is
            # exactly the observation that decides how many of the 54
            # candidate observations are real features — see
            # Documentation/DATABASES.md, H2.
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
    # A calibration taken at different acquisition settings does not
    # describe the same instrument. These expectations are named in
    # Science/config.py rather than hardcoded here, and they mirror
    # ESP32/config.py deliberately: the host cannot import device
    # firmware, so a test asserts the two stay in step.
    settings = sensor_settings or document.get("sensor_settings") or {}

    expected = {
        "measurement_mode": config.EXPECTED_MEASUREMENT_MODE,
        "integration_cycles": config.EXPECTED_INTEGRATION_CYCLES,
        "gain": config.EXPECTED_GAIN,
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
