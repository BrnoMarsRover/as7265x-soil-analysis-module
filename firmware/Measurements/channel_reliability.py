"""
Per illumination x channel reliability — 54 verdicts, not one.

THE DISTINCTION THIS MODULE EXISTS FOR

    raw_valid                is the COUNT a real measurement?
    normalized_reliability   is the REFLECTANCE derived from it meaningful?

They are not the same question, and treating them as one is what made the
instrument throw away six of twelve good measurements on 2026-08-17.

Worked example from that session, IR illumination, channel C460:

    raw sample      57.9 counts     a perfectly good reading
    raw dark        77.9 counts
    raw reference   86.4 counts
    reference-dark   8.5 counts     the whole dynamic range available
    reflectance     -2.33           "impossible"

Nothing failed. The IR lamp barely reaches 460 nm, so the reference had
almost no headroom over the dark, and dividing by 8.5 counts turns the
dark-offset uncertainty into a reflectance swing of several units. The
COUNT is valid. The REFLECTANCE is not usable. The old pipeline saw only
the reflectance, called the measurement a hardware failure, and reported
nothing at all.

So each of the 54 gets:

    raw_valid                   bool
    normalized_valid            bool
    normalized_reliability      0.0 .. 1.0
    weight                      what a distance metric should use
    reasons                     why, in words, always

WHAT DRIVES THE NUMBER

    reference dynamic range     reference minus dark, in counts
    reference repeatability     CV of the calibration block
    sample repeatability        CV of the sample's own repeats
    signal level                is the sample above the noise floor?
    saturation                  is it against the ceiling?

None of these is a threshold on the RESULT: a channel is never deleted,
its value is never altered, and a reflectance of -2.33 is stored exactly
as computed. Reliability travels beside the number, and the decision
layer weighs it.
"""

import math

from BD.channels import CHANNELS, ILLUMINATIONS
from Measurements import config

# Reference headroom, in counts, below which the division is dominated by
# quantization rather than by the sample. Continuous rather than a cliff:
# the reliability ramps between the two. Defined once, in
# Measurements/config.py, and shared with preprocessing.conditioning.
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


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False

    return not (math.isnan(value) or math.isinf(value))


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


def assess(raw_blocks, dark, white_by_illumination,
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
