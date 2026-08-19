"""
Measurement quality control, run BEFORE any classification.

A spectrum that the optics never really produced will still rank
cheerfully against 22 reference materials and hand back a confident
answer. This module exists to stop that: it inspects the measurement
itself and returns PASS, WARNING or FAIL with explicit reasons, and the
caller refuses to interpret a FAIL.

Nothing here repairs data. No value is clamped, no channel is filled in.
Channels that cannot be trusted are NAMED so the comparison can exclude
them, and everything else is reported for the operator to judge.

Checks
------
    illumination      is there enough White-Dark signal to divide by
    validity          are the channels present and finite
    reflectance       is R inside a physically sensible range
    triad boundary    do the three detectors agree at their seams
    repeatability     did the repeats actually repeat
    distance          optional VL53L4CD gate, if a reading is supplied
"""

import math

from Measurements import config
from BD.channels import CHANNELS

PASS = "PASS"
WARNING = "WARNING"
FAIL = "FAIL"

# Worst-wins ordering.
_SEVERITY = {PASS: 0, WARNING: 1, FAIL: 2}


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False

    return not (math.isnan(value) or math.isinf(value))


class QualityReport:
    """Accumulates checks and settles on the worst outcome seen."""

    def __init__(self):
        self.checks = []
        self.invalid_channels = set()

    def add(self, name, status, message, **details):
        self.checks.append({
            "check": name,
            "status": status,
            "message": message,
            "details": details,
        })

        return status

    def mark_invalid(self, channels):
        self.invalid_channels.update(channels)

    @property
    def status(self):
        if not self.checks:
            return PASS

        return max(
            (check["status"] for check in self.checks),
            key=lambda status: _SEVERITY[status],
        )

    def reasons(self, status):
        return [
            check["message"] for check in self.checks
            if check["status"] == status
        ]

    def as_dict(self):
        return {
            "status": self.status,
            "checks": self.checks,
            "warnings": self.reasons(WARNING),
            "failures": self.reasons(FAIL),
            "invalid_channels": sorted(self.invalid_channels),
            "usable_channels": [
                channel for channel in CHANNELS
                if channel not in self.invalid_channels
            ],
        }


def check_illumination(report, white, dark, illumination="white"):
    """
    Is there enough light to divide by?

    (White - Dark) is the denominator of the reflectance. Where it is
    essentially zero the channel is not measuring anything, and the
    result of dividing by it is noise amplified without limit.
    """
    dead = []
    weak = []

    for channel in CHANNELS:
        w = white.get(channel)
        d = dark.get(channel)

        if not _finite(w) or not _finite(d):
            dead.append(channel)

            continue

        denominator = float(w) - float(d)

        if denominator < config.MIN_DENOMINATOR:
            dead.append(channel)
        elif denominator < config.WEAK_DENOMINATOR:
            weak.append(channel)

    report.mark_invalid(dead)

    if len(dead) > config.MAX_WEAK_CHANNELS_FAIL:
        return report.add(
            "illumination", FAIL,
            "{} illumination reaches {} of 18 channels below the usable "
            "minimum.".format(illumination.upper(), len(dead)),
            illumination=illumination, dead_channels=dead,
            weak_channels=weak,
        )

    if dead or len(weak) > config.MAX_WEAK_CHANNELS_WARNING:
        return report.add(
            "illumination", WARNING,
            "{} illumination is weak: {} unusable, {} marginal "
            "channel(s).".format(illumination.upper(), len(dead), len(weak)),
            illumination=illumination, dead_channels=dead,
            weak_channels=weak,
        )

    return report.add(
        "illumination", PASS,
        "All 18 channels have usable {} illumination.".format(
            illumination.upper()
        ),
        illumination=illumination,
    )


def check_validity(report, normalized):
    """Enough finite channels left to classify anything."""
    invalid = [
        channel for channel in CHANNELS
        if not _finite(normalized.get(channel))
    ]

    report.mark_invalid(invalid)

    usable = len(CHANNELS) - len(report.invalid_channels)

    if usable < config.MIN_VALID_CHANNELS:
        return report.add(
            "validity", FAIL,
            "Only {} of 18 channels are usable; at least {} are needed "
            "to compare against the library.".format(
                usable, config.MIN_VALID_CHANNELS
            ),
            usable=usable, invalid_channels=sorted(report.invalid_channels),
        )

    if invalid:
        return report.add(
            "validity", WARNING,
            "{} channel(s) are not finite and were excluded.".format(
                len(invalid)
            ),
            invalid_channels=invalid,
        )

    return report.add(
        "validity", PASS, "All 18 channels are finite.", usable=usable
    )


def check_reflectance(report, normalized):
    """
    Is the reflectance physically sensible?

    Above 1.0 the sample returned more light than the white reference;
    below 0 it returned less than the dark. A little of either is
    measurement uncertainty. A lot means the calibration no longer
    describes the optical geometry - usually the sample distance moved.

    Values are reported, never clamped.
    """
    high = []
    low = []
    extreme = []

    for channel in CHANNELS:
        value = normalized.get(channel)

        if not _finite(value):
            continue

        value = float(value)

        if value > config.REFLECTANCE_FAIL_MAX:
            extreme.append(channel)
        elif value > config.REFLECTANCE_WARNING_MAX:
            high.append(channel)
        elif value < config.REFLECTANCE_FAIL_MIN:
            extreme.append(channel)
        elif value < config.REFLECTANCE_WARNING_MIN:
            low.append(channel)

    total = float(len(CHANNELS))
    out_of_range = len(high) + len(low) + len(extreme)
    fraction = out_of_range / total

    if extreme or fraction > config.MAX_FRACTION_OUT_OF_RANGE_FAIL:
        return report.add(
            "reflectance", FAIL,
            "{} channel(s) are far outside the expected 0-1 reflectance "
            "range. The calibration probably does not describe the "
            "current geometry.".format(out_of_range),
            extreme_channels=extreme, high_channels=high,
            low_channels=low, fraction=round(fraction, 3),
        )

    if fraction > config.MAX_FRACTION_OUT_OF_RANGE_WARNING:
        return report.add(
            "reflectance", WARNING,
            "{} channel(s) fall outside the expected reflectance "
            "range.".format(out_of_range),
            high_channels=high, low_channels=low,
            fraction=round(fraction, 3),
        )

    return report.add(
        "reflectance", PASS,
        "Reflectance is within the expected range.",
        fraction=round(fraction, 3),
    )


def check_triad_boundaries(report, normalized):
    """
    Do the three detectors agree where they meet?

    The 18 channels come from three separate devices. F535 -> G560 and
    L705 -> R730 are the seams. A mismatch in gain, illumination or
    calibration between devices shows up there as a step no real
    spectrum has.

    Real spectra do have steep slopes, so the thresholds are loose - the
    point is to catch a discontinuity, not a feature.
    """
    findings = []
    worst = PASS

    for left, right in config.BOUNDARY_PAIRS:
        a = normalized.get(left)
        b = normalized.get(right)

        if not _finite(a) or not _finite(b):
            continue

        a = abs(float(a))
        b = abs(float(b))

        smaller = min(a, b)
        larger = max(a, b)

        if smaller < 1e-6:
            # Nothing meaningful to divide; a step from zero is not
            # evidence of a device mismatch.
            continue

        ratio = larger / smaller

        finding = {
            "boundary": "{}->{}".format(left, right),
            "ratio": round(ratio, 3),
            left: round(float(normalized[left]), 6),
            right: round(float(normalized[right]), 6),
        }

        if ratio > config.BOUNDARY_FAIL_RATIO:
            finding["status"] = FAIL
            worst = FAIL

        elif ratio > config.BOUNDARY_WARNING_RATIO:
            finding["status"] = WARNING

            if worst == PASS:
                worst = WARNING

        else:
            finding["status"] = PASS

        findings.append(finding)

    if worst == FAIL:
        return report.add(
            "triad_boundary", FAIL,
            "A large discontinuity between detector devices suggests the "
            "three detectors are not consistently calibrated.",
            boundaries=findings,
        )

    if worst == WARNING:
        return report.add(
            "triad_boundary", WARNING,
            "A step is present at a detector boundary.",
            boundaries=findings,
        )

    return report.add(
        "triad_boundary", PASS,
        "The detector boundaries are continuous.",
        boundaries=findings,
    )


def check_repeatability(report, statistics, illumination="white"):
    """Did the repeats actually repeat?"""
    if not statistics:
        return report.add(
            "repeatability", WARNING,
            "No repeat statistics were recorded for the {} "
            "block.".format(illumination.upper()),
            illumination=illumination,
        )

    repeats = statistics.get("repeats") or 0

    if repeats < 2:
        return report.add(
            "repeatability", WARNING,
            "Only {} acquisition(s): repeatability cannot be "
            "assessed.".format(repeats),
            illumination=illumination, repeats=repeats,
        )

    unstable = statistics.get("unstable_channels") or []
    max_cv = statistics.get("max_cv")

    if (
        len(unstable) > config.MAX_UNSTABLE_CHANNELS_FAIL
        or (max_cv is not None and max_cv > config.CV_FAIL)
    ):
        return report.add(
            "repeatability", FAIL,
            "The {} acquisitions did not repeat: {} unstable channel(s), "
            "worst CV {}.".format(
                illumination.upper(), len(unstable), max_cv
            ),
            illumination=illumination, unstable_channels=unstable,
            max_cv=max_cv, mean_cv=statistics.get("mean_cv"),
        )

    if len(unstable) > config.MAX_UNSTABLE_CHANNELS_WARNING:
        return report.add(
            "repeatability", WARNING,
            "{} channel(s) varied more than {:.0%} across the {} "
            "repeats.".format(
                len(unstable), config.CV_WARNING, illumination.upper()
            ),
            illumination=illumination, unstable_channels=unstable,
            max_cv=max_cv, mean_cv=statistics.get("mean_cv"),
        )

    return report.add(
        "repeatability", PASS,
        "The {} acquisitions repeated within tolerance.".format(
            illumination.upper()
        ),
        illumination=illumination, repeats=repeats,
        max_cv=max_cv, mean_cv=statistics.get("mean_cv"),
    )


def check_distance(report, distance_mm):
    """
    Optional geometry gate.

    A VL53L4CD is planned but not fitted. When no reading is supplied
    this reports UNAVAILABLE and changes nothing - it must never block a
    measurement on hardware that does not exist.

    Distance is a gate, not a correction: reflectance is never scaled by
    it.
    """
    if distance_mm is None:
        report.checks.append({
            "check": "distance",
            "status": PASS,
            "message": "No distance sensor reading; geometry not verified.",
            "details": {"distance_status": "UNAVAILABLE"},
        })

        return PASS

    if not _finite(distance_mm):
        return report.add(
            "distance", WARNING,
            "The distance reading was not a usable number.",
            distance_status="INVALID", distance_mm=distance_mm,
        )

    low = config.DISTANCE_EXPECTED_MIN_MM
    high = config.DISTANCE_EXPECTED_MAX_MM

    if not (low <= float(distance_mm) <= high):
        return report.add(
            "distance", FAIL,
            "Sample distance {:.1f} mm is outside the calibrated "
            "{:.0f}-{:.0f} mm range.".format(float(distance_mm), low, high),
            distance_status="FAIL", distance_mm=distance_mm,
            expected_min_mm=low, expected_max_mm=high,
        )

    return report.add(
        "distance", PASS,
        "Sample distance {:.1f} mm is within range.".format(
            float(distance_mm)
        ),
        distance_status="PASS", distance_mm=distance_mm,
    )


def assess(normalized, white_reference, dark_reference,
           statistics=None, distance_mm=None, illumination="white"):
    """
    Run every check and return one report.

    `normalized` is the reflectance being classified; the references are
    the pair it was normalized against.
    """
    report = QualityReport()

    check_illumination(report, white_reference, dark_reference, illumination)
    check_validity(report, normalized)
    check_reflectance(report, normalized)
    check_triad_boundaries(report, normalized)
    check_repeatability(report, statistics, illumination)
    check_distance(report, distance_mm)

    return report.as_dict()
