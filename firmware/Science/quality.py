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

from Science import config
from BD.channels import CHANNELS, ILLUMINATIONS

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

# ====================================================================
# per-channel reliability
# ====================================================================
# The quality report above answers 'is this measurement usable?'.
# This answers the finer question 'which CHANNELS of it are worth
# believing, and how much?' - and produces the weights that let a
# noisy channel contribute in proportion to what it is worth instead
# of being either fully counted or fully deleted.
# 
# One module, because a channel-level verdict and a measurement-level
# verdict are the same judgement at two scales, made from the same
# evidence and the same thresholds.

# Reference headroom, in counts, below which the division is dominated by
# quantization rather than by the sample. Continuous rather than a cliff:
# the reliability ramps between the two. Defined once, in
# Science/config.py, and shared with preprocessing.conditioning.
REFERENCE_RANGE_UNUSABLE = config.REFERENCE_RANGE_UNUSABLE
REFERENCE_RANGE_POOR = config.REFERENCE_RANGE_POOR
REFERENCE_RANGE_GOOD = config.REFERENCE_RANGE_GOOD

# Repeat-to-repeat coefficient of variation. Above the second value the
# channel is not repeating and its value is one draw from a wide
# distribution.
CV_GOOD = 0.02
CV_POOR = 0.15

# A sample reading this far below one count is at the noise floor: the
# detector reports zero and the true value is somewhere under it.
SIGNAL_FLOOR_COUNTS = 1.0

# The AS7265x calibrated registers saturate here. A reading at the
# ceiling is a lower bound, not a measurement.
SATURATION_COUNTS = 65000.0

# Reliability at or below this is treated as no evidence at all.
UNUSABLE_RELIABILITY = 0.15




def _ramp(value, low, high):
    """0.0 at or below `low`, 1.0 at or above `high`, linear between."""
    if value is None or not _finite(value):
        return 0.0

    if value <= low:
        return 0.0

    if value >= high:
        return 1.0

    return (value - low) / float(high - low)


def _falling_ramp(value, good, poor):
    """1.0 at or below `good`, 0.0 at or above `poor`."""
    if value is None or not _finite(value):
        return 1.0

    if value <= good:
        return 1.0

    if value >= poor:
        return 0.0

    return 1.0 - (value - good) / float(poor - good)


def channel_report(raw_value, dark_value, reference_value,
                   sample_cv=None, reference_cv=None):
    """
    One illumination x channel verdict.

    Every input may be None; a missing input lowers confidence and is
    named in the reasons rather than being assumed benign.
    """
    reasons = []

    raw_valid = _finite(raw_value)

    if not raw_valid:
        reasons.append("no finite raw reading")

    saturated = raw_valid and raw_value >= SATURATION_COUNTS

    if saturated:
        raw_valid = False
        reasons.append("at or above the saturation ceiling")

    at_floor = raw_valid and abs(raw_value) < SIGNAL_FLOOR_COUNTS

    if at_floor:
        reasons.append("sample is at the noise floor")

    headroom = None

    if _finite(reference_value) and _finite(dark_value):
        headroom = reference_value - dark_value

    if headroom is None:
        reference_score = 0.0
        reasons.append("no reference for this illumination")

    elif headroom < REFERENCE_RANGE_UNUSABLE:
        reference_score = 0.0
        reasons.append(
            "reference is {:.1f} counts above dark - nothing to divide by"
            .format(headroom)
        )

    else:
        reference_score = _ramp(
            headroom, REFERENCE_RANGE_POOR, REFERENCE_RANGE_GOOD
        )

        if headroom < REFERENCE_RANGE_POOR:
            reasons.append(
                "reference is only {:.1f} counts above dark, so one count "
                "of sample noise moves the reflectance by {:.2f}".format(
                    headroom, 1.0 / headroom
                )
            )

    sample_score = _falling_ramp(sample_cv, CV_GOOD, CV_POOR)
    reference_repeat_score = _falling_ramp(reference_cv, CV_GOOD, CV_POOR)

    if sample_cv is not None and sample_cv > CV_POOR:
        reasons.append(
            "sample repeats vary by {:.1%}".format(sample_cv)
        )

    if reference_cv is not None and reference_cv > CV_POOR:
        reasons.append(
            "reference repeats vary by {:.1%}".format(reference_cv)
        )

    level_score = 0.0 if at_floor else 1.0

    if raw_valid:
        # The reference range GATES the rest rather than being averaged
        # with it. That is the difference between a formula and a
        # statement about physics: if the reference left two counts of
        # headroom, no amount of repeatability makes the quotient
        # meaningful, and a weighted sum would still have awarded it 0.45
        # for repeating its own noise consistently.
        supporting = (
            0.45 * sample_score
            + 0.35 * reference_repeat_score
            + 0.20 * level_score
        )

        reliability = reference_score * (0.4 + 0.6 * supporting)

    else:
        reliability = 0.0

    reliability = round(max(0.0, min(1.0, reliability)), 4)

    return {
        "raw_valid": bool(raw_valid),
        "raw_value": raw_value if _finite(raw_value) else None,
        "saturated": bool(saturated),
        "at_noise_floor": bool(at_floor),
        "reference_minus_dark": (
            round(headroom, 4) if headroom is not None else None
        ),
        "sample_cv": sample_cv,
        "reference_cv": reference_cv,
        "normalized_reliability": reliability,
        "normalized_valid": bool(raw_valid and reliability
                                 > UNUSABLE_RELIABILITY),
        "weight": reliability,
        "reasons": reasons,
    }


def assess_channels(raw_blocks, dark, white_by_illumination,
           sample_statistics=None, reference_statistics=None):
    """
    The full 54-entry reliability map.

    `sample_statistics` and `reference_statistics` are the per-channel
    aggregation summaries ({illumination: {"channels": {channel: {...}}}}),
    used for their `cv`. Both are optional: without them the repeatability
    terms are treated as unknown-but-not-bad, and the reasons say so.
    """
    dark = dark or {}
    white_by_illumination = white_by_illumination or {}
    sample_statistics = sample_statistics or {}
    reference_statistics = reference_statistics or {}

    report = {"channels": {}, "by_illumination": {}}

    for illumination in ILLUMINATIONS:
        block = raw_blocks.get(illumination)

        if not block:
            continue

        white = white_by_illumination.get(illumination) or {}

        sample_channels = (
            (sample_statistics.get(illumination) or {}).get("channels") or {}
        )
        reference_channels = (
            (reference_statistics.get(illumination) or {}).get("channels")
            or {}
        )

        usable_raw = 0
        usable_normalized = 0
        total_weight = 0.0

        for channel in CHANNELS:
            entry = channel_report(
                block.get(channel),
                dark.get(channel),
                white.get(channel),
                (sample_channels.get(channel) or {}).get("cv"),
                (reference_channels.get(channel) or {}).get("cv"),
            )

            report["channels"][
                "{}:{}".format(illumination, channel)
            ] = entry

            usable_raw += 1 if entry["raw_valid"] else 0
            usable_normalized += 1 if entry["normalized_valid"] else 0
            total_weight += entry["weight"]

        report["by_illumination"][illumination] = {
            "raw_valid_channels": usable_raw,
            "normalized_valid_channels": usable_normalized,
            "mean_reliability": round(total_weight / len(CHANNELS), 4),
            "channels": len(CHANNELS),
        }

    report["features_total"] = len(report["channels"])
    report["raw_valid_total"] = sum(
        1 for entry in report["channels"].values() if entry["raw_valid"]
    )
    report["normalized_valid_total"] = sum(
        1 for entry in report["channels"].values()
        if entry["normalized_valid"]
    )

    return report


def usable_channels(reliability, illumination="white"):
    """The channels a comparison under this lamp may use."""
    return [
        channel for channel in CHANNELS
        if (
            reliability.get("channels", {})
            .get("{}:{}".format(illumination, channel), {})
            .get("normalized_valid")
        )
    ]


def weights(reliability, illumination="white"):
    """Per-channel weights for a weighted distance, same order as CHANNELS."""
    return {
        channel: (
            reliability.get("channels", {})
            .get("{}:{}".format(illumination, channel), {})
            .get("weight", 0.0)
        )
        for channel in CHANNELS
    }


def summarize(reliability):
    """One paragraph an operator can read, built only from the numbers."""
    total = reliability.get("features_total", 0)
    raw_valid = reliability.get("raw_valid_total", 0)
    normalized_valid = reliability.get("normalized_valid_total", 0)

    lines = [
        "{}/{} raw features are valid measurements.".format(raw_valid, total),
        "{}/{} of them also support a usable reflectance.".format(
            normalized_valid, total
        ),
    ]

    if raw_valid > normalized_valid:
        lines.append(
            "The difference is normalization, not hardware: {} feature(s) "
            "were measured correctly but their reference left too little "
            "dynamic range to divide by.".format(raw_valid - normalized_valid)
        )

    return " ".join(lines)


# Kept beside the thresholds it uses, so the two cannot drift apart.
RELIABILITY_VERSION = "{}-channel-reliability-1".format(
    config.SCIENCE_VERSION
)
