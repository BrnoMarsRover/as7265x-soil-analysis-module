"""
Acquisition profiles: HOW a measurement was made.

Three things were being muddled into one, and separating them is what
makes "is this calibration valid for this measurement?" answerable:

    PROFILE       the conditions        16x gain, 100 cycles, 25 mA,
                                        this chamber, this distance
    CALIBRATION   what the REFERENCE    under those conditions the white
                  read under them       target read 2311 on R730
    MEASUREMENT   what the SAMPLE       under those conditions this soil
                  read under them       read 3116 on R730

A calibration describes the instrument only under the profile it was
taken with. Applying it across profiles is a research operation with its
own validation (Science.decision/calibration_transfer.py), never a silent
default.

THE FINGERPRINT

Compatibility is decided by a fingerprint over the fields that actually
change what a count means:

    sensor        measurement_mode, gain, integration_cycles
    illumination  the three LED currents and the order they fire in
    geometry      sensor-to-sample distance and angle
    hardware      PCB, sensor module, illumination and firmware revisions

Deliberately EXCLUDED, with reasons:

    repeats       averaging more readings does not change what one
                  reading means; the statistics travel with the record
    operator,     provenance, not physics
    notes, time
    environment   recorded because it may explain a drift, but the
                  system has no measurement of how temperature or
                  humidity affect these counts, and asserting one by
                  putting them in the fingerprint would be inventing a
                  relationship

UNKNOWN IS NULL. A field nobody measured is stored as null and reported
as unknown - never defaulted to a plausible number. Two profiles that
both say "distance unknown" are not thereby known to match, and the
comparison says so.

Layer rule: BD must never import Science or Science.decision.
"""

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from BD import config

# The fields the fingerprint covers, in a fixed order. Adding one changes
# every fingerprint, so the layout version below moves with it.
FINGERPRINT_FIELDS = (
    ("sensor", "measurement_mode"),
    ("sensor", "gain"),
    ("sensor", "gain_x"),
    ("sensor", "integration_cycles"),
    ("illumination", "white_current_ma"),
    ("illumination", "uv_current_ma"),
    ("illumination", "ir_current_ma"),
    ("illumination", "order"),
    ("geometry", "sensor_to_sample_distance_mm"),
    ("geometry", "sensor_angle_deg"),
    ("hardware", "pcb_revision"),
    ("hardware", "sensor_module_id"),
    ("hardware", "illumination_revision"),
    ("hardware", "firmware_version"),
)

FINGERPRINT_VERSION = 1

COMPATIBLE = "COMPATIBLE"
INCOMPATIBLE = "INCOMPATIBLE"
UNKNOWN_FIELDS = "COMPATIBLE_WITH_UNKNOWNS"


class ProfileError(Exception):
    """An acquisition profile is missing or malformed."""

    def __init__(self, code, message, details=None):
        super().__init__(message)

        self.code = code
        self.message = message
        self.details = details or {}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def _write_json(path, payload):
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


def blank_profile():
    """
    The full shape, with every unmeasured field explicitly null.

    Written out in full rather than left absent: a missing key reads as
    an oversight, an explicit null reads as "nobody measured this", and
    only the second is true.
    """
    return {
        "schema_version": config.ACQUISITION_PROFILE_SCHEMA_VERSION,
        "profile_id": None,
        "created_at": None,
        "label": None,

        "sensor": {
            "device": "AS7265x",
            "measurement_mode": None,
            "gain": None,
            "gain_x": None,
            "integration_cycles": None,
        },

        "illumination": {
            "white_current_ma": None,
            "uv_current_ma": None,
            "ir_current_ma": None,
            "order": ["white", "uv", "ir"],
            "warmup_ms": None,
            "settling_ms": None,
        },

        "geometry": {
            "sensor_to_sample_distance_mm": None,
            "sensor_angle_deg": None,
            "illumination_angle_deg": None,
        },

        "mechanical": {
            "chamber_id": None,
            "chamber_revision": None,
            "carousel_revision": None,
            "sample_holder_revision": None,
            "sensor_mount_revision": None,
            "illumination_mount_revision": None,
        },

        "hardware": {
            "pcb_revision": None,
            "sensor_module_id": None,
            "illumination_revision": None,
            "firmware_version": None,
            "git_commit": None,
        },

        "environment": {
            "temperature_c": None,
            "humidity_percent": None,
            "ambient_light_condition": None,
        },

        "procedure": {
            "raw_definition": "one-shot conversion, counts as reported by "
                              "the AS7265x calibrated registers",
            "averaging_method": None,
            "estimator": None,
        },

        "notes": None,
    }


def _canonical(value):
    """
    One representation per physical value, so equal conditions hash equal.

    25 and 25.0 are the same lamp current. They are not the same JSON,
    and the fingerprint is a hash of JSON - so a profile built from a
    seed file that wrote the current as an integer got a different id
    from one built from the same current after `_milliamps` turned it
    into a float. That split a single measurement session across two
    acquisition profiles, which is exactly the mistake the fingerprint
    exists to prevent.

    An integral float folds to its integer. Nothing else is touched: 25.5
    stays 25.5, and a genuinely different current still hashes
    differently.
    """
    if isinstance(value, bool):
        return value

    if isinstance(value, float) and value.is_integer():
        return int(value)

    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]

    return value


def _fingerprint_values(profile):
    values = []

    for section, field in FINGERPRINT_FIELDS:
        value = _canonical((profile.get(section) or {}).get(field))

        values.append([section, field, value])

    return values


def fingerprint(profile):
    """A stable hash of the fields that change what a count means."""
    payload = {
        "version": FINGERPRINT_VERSION,
        "fields": _fingerprint_values(profile),
    }

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


def unknown_fingerprint_fields(profile):
    """Which compatibility-relevant fields nobody has recorded."""
    return [
        "{}.{}".format(section, field)
        for section, field, value in _fingerprint_values(profile)
        if value is None
    ]


def compare(left, right):
    """
    Are two profiles the same acquisition condition?

    Returns a verdict plus the fields that differ. A field that is
    unknown on BOTH sides is not a difference, but it is not evidence of
    a match either - that is what COMPATIBLE_WITH_UNKNOWNS means, and it
    is why the verdict has three values instead of two.
    """
    differences = []
    unknowns = []

    for section, field in FINGERPRINT_FIELDS:
        name = "{}.{}".format(section, field)

        a = (left.get(section) or {}).get(field)
        b = (right.get(section) or {}).get(field)

        if a is None and b is None:
            unknowns.append(name)

            continue

        if a != b:
            differences.append({"field": name, "left": a, "right": b})

    if differences:
        status = INCOMPATIBLE
    elif unknowns:
        status = UNKNOWN_FIELDS
    else:
        status = COMPATIBLE

    return {
        "status": status,
        "differences": differences,
        "unknown_on_both_sides": unknowns,
        "left_fingerprint": fingerprint(left),
        "right_fingerprint": fingerprint(right),
    }


def _milliamps(value):
    """
    A lamp current as a number, from either a number or "25mA".

    The firmware reports the current as a LABEL for the register code
    it wrote, because that is what an operator needs to read. A profile
    field that is compared for equality needs the number.
    """
    if value is None:
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().lower()

    for suffix in ("ma", "m a"):
        if text.endswith(suffix):
            text = text[:-len(suffix)].strip()

            break

    try:
        return float(text)

    except ValueError:
        return None


def from_measurement(sensor_settings, firmware_version=None, label=None,
                     geometry=None, notes=None):
    """
    Build a profile from what the instrument actually reported.

    Only fields the instrument really told us are filled. Geometry and
    mechanical revisions stay null until somebody measures and records
    them - which is the honest state today.
    """
    settings = sensor_settings or {}
    profile = blank_profile()

    profile["label"] = label
    profile["notes"] = notes

    profile["sensor"].update({
        "measurement_mode": settings.get("measurement_mode"),
        "gain": settings.get("gain"),
        "gain_x": settings.get("gain_x"),
        "integration_cycles": settings.get("integration_cycles"),
    })

    # THE INSTRUMENT REPORTS THE CURRENTS UNDER A DIFFERENT NAME.
    #
    # This asked for settings["white_current_ma"] and the ESP32 has
    # never sent a key of that name. It sends
    #
    #     "led_currents_ma": {"white": "25mA", "uv": "25mA",
    #                         "ir": "25mA"}
    #
    # so all three currents were recorded as null in every profile
    # built from a real acquisition, and all three are FINGERPRINT
    # fields: two acquisitions taken at 25 mA and at 100 mA produced
    # the same fingerprint and compared as COMPATIBLE_WITH_UNKNOWNS.
    # The lamp current is one of the few things that genuinely changes
    # what a count means.
    currents = settings.get("led_currents_ma") or {}

    profile["illumination"].update({
        "white_current_ma": _milliamps(
            settings.get("white_current_ma", currents.get("white"))),
        "uv_current_ma": _milliamps(
            settings.get("uv_current_ma", currents.get("uv"))),
        "ir_current_ma": _milliamps(
            settings.get("ir_current_ma", currents.get("ir"))),
    })

    profile["hardware"]["firmware_version"] = firmware_version

    if geometry:
        profile["geometry"].update({
            key: value for key, value in geometry.items()
            if key in profile["geometry"]
        })

    return profile


class AcquisitionProfileStore:
    """
    Every acquisition condition the instrument has been used in.

    Append only. A profile is identified by its fingerprint, so asking
    for the same conditions twice returns the same profile rather than
    filling the file with duplicates.
    """

    def __init__(self, path=None):
        self.path = Path(path or config.ACQUISITION_PROFILES_FILE)

    def _load(self):
        document = _read_json(self.path)

        if not isinstance(document, dict):
            return {
                "schema_version": config.ACQUISITION_PROFILE_SCHEMA_VERSION,
                "fingerprint_version": FINGERPRINT_VERSION,
                "updated_at": utc_now(),
                "profiles": [],
            }

        document.setdefault("profiles", [])

        return document

    def _save(self, document):
        document["schema_version"] = config.ACQUISITION_PROFILE_SCHEMA_VERSION
        document["fingerprint_version"] = FINGERPRINT_VERSION
        document["updated_at"] = utc_now()

        _write_json(self.path, document)

    def count(self):
        return len(self._load()["profiles"])

    def all(self):
        return list(self._load()["profiles"])

    def get(self, profile_id):
        for profile in self._load()["profiles"]:
            if profile.get("profile_id") == profile_id:
                return profile

        return None

    def require(self, profile_id):
        profile = self.get(profile_id)

        if profile is None:
            raise ProfileError(
                "PROFILE_NOT_FOUND",
                "No acquisition profile {}.".format(profile_id),
                {"profile_id": profile_id},
            )

        return profile

    def find_by_fingerprint(self, value):
        for profile in self._load()["profiles"]:
            if profile.get("fingerprint") == value:
                return profile

        return None

    def ensure(self, profile):
        """
        Return the stored profile for these conditions, creating it once.

        Identity is the fingerprint, not the label: the same conditions
        described twice in different words are the same profile, and two
        different gains are two profiles however they are labelled.
        """
        profile = dict(profile)
        value = fingerprint(profile)

        existing = self.find_by_fingerprint(value)

        if existing is not None:
            return existing

        document = self._load()

        profile["fingerprint"] = value
        profile["fingerprint_version"] = FINGERPRINT_VERSION
        profile["created_at"] = profile.get("created_at") or utc_now()
        profile["profile_id"] = "PROFILE_{}".format(value[:12].upper())
        profile["unknown_fields"] = unknown_fingerprint_fields(profile)

        document["profiles"].append(profile)
        self._save(document)

        return profile

    def status(self):
        profiles = self.all()

        return {
            "file": str(self.path),
            "count": len(profiles),
            "fingerprint_version": FINGERPRINT_VERSION,
            "profiles": [
                {
                    "profile_id": profile.get("profile_id"),
                    "label": profile.get("label"),
                    "created_at": profile.get("created_at"),
                    "unknown_fields": len(profile.get("unknown_fields") or []),
                }
                for profile in profiles
            ],
        }
