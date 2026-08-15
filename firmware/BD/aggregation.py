"""
Aggregation of repeated acquisitions.

Deliberately small and dependency-free: mean, median, standard
deviation, min, max and coefficient of variation, computed with the
standard library. There is no numpy here and there does not need to be
- an 18-channel spectrum repeated ten times is 180 numbers.

Nothing is ever silently discarded. An outlier is recorded with the
reason it was rejected, and the individual acquisitions travel with the
result so a number can always be traced back to the readings that
produced it.
"""

import math

import config
from channels import CHANNELS


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False

    return not (math.isnan(value) or math.isinf(value))


def median(values):
    if not values:
        return None

    ordered = sorted(values)
    middle = len(ordered) // 2

    if len(ordered) % 2:
        return float(ordered[middle])

    return (ordered[middle - 1] + ordered[middle]) / 2.0


def mean(values):
    return sum(values) / float(len(values)) if values else None


def stdev(values):
    """Sample standard deviation; 0.0 for a single reading."""
    if len(values) < 2:
        return 0.0

    average = mean(values)
    variance = sum((value - average) ** 2 for value in values)

    return math.sqrt(variance / float(len(values) - 1))


def coefficient_of_variation(values):
    """
    Relative spread, as a fraction.

    Undefined when the mean is at or near zero - a dark channel reading
    0.001 +/- 0.001 is not "100% unstable", it is just dark. Returns
    None there rather than a meaningless number.
    """
    average = mean(values)

    if average is None or abs(average) < config.CV_MINIMUM_MEAN:
        return None

    return stdev(values) / abs(average)


def channel_series(acquisitions, channel):
    """Every finite reading of one channel across the repeats."""
    series = []

    for spectrum in acquisitions or []:
        value = (spectrum or {}).get(channel)

        if _finite(value):
            series.append(float(value))

    return series


def reject_outliers(values):
    """
    Median-absolute-deviation rejection.

    MAD rather than standard deviation because a single wild reading
    inflates the standard deviation enough to hide itself. Returns
    (kept, rejected); with too few readings to judge, nothing is
    rejected.
    """
    if len(values) < config.OUTLIER_MINIMUM_SAMPLES:
        return list(values), []

    centre = median(values)
    deviations = [abs(value - centre) for value in values]
    mad = median(deviations)

    if mad is None or mad <= 0:
        # No spread to speak of: nothing can be called an outlier.
        return list(values), []

    # 1.4826 scales MAD to a standard-deviation equivalent for normally
    # distributed data, so the threshold reads like a sigma count.
    limit = config.OUTLIER_MAD_THRESHOLD * 1.4826 * mad

    kept = []
    rejected = []

    for value in values:
        if abs(value - centre) > limit:
            rejected.append(value)
        else:
            kept.append(value)

    # Never reject everything, whatever the data looks like.
    if not kept:
        return list(values), []

    return kept, rejected


def summarize_channel(values):
    """Full statistics for one channel's repeated readings."""
    if not values:
        return {
            "n": 0, "accepted": 0, "rejected": 0,
            "value": None, "median": None, "mean": None,
            "stdev": None, "min": None, "max": None, "cv": None,
        }

    kept, rejected = reject_outliers(values)

    return {
        "n": len(values),
        "accepted": len(kept),
        "rejected": len(rejected),
        "rejected_values": [round(value, 4) for value in rejected],

        # The robust estimator. Median, because one bad conversion in a
        # short block should not move the answer at all.
        "value": round(median(kept), 4),

        "median": round(median(kept), 4),
        "mean": round(mean(kept), 4),
        "stdev": round(stdev(kept), 4),
        "min": round(min(kept), 4),
        "max": round(max(kept), 4),
        "cv": (
            round(coefficient_of_variation(kept), 5)
            if coefficient_of_variation(kept) is not None else None
        ),
    }


def aggregate_block(block):
    """
    Turn one repeated-acquisition block into a spectrum plus statistics.

    `block` is what the ESP32 returns from acquire_block: an
    `acquisitions` list of raw 18-channel spectra.

    Returns {"spectrum": {...18}, "statistics": {...}} where `spectrum`
    holds the robust per-channel estimate used by everything downstream.
    """
    acquisitions = (block or {}).get("acquisitions") or []

    spectrum = {}
    per_channel = {}
    missing = []

    for channel in CHANNELS:
        series = channel_series(acquisitions, channel)
        summary = summarize_channel(series)

        per_channel[channel] = summary

        if summary["value"] is None:
            missing.append(channel)
            spectrum[channel] = None
        else:
            spectrum[channel] = summary["value"]

    usable = [
        summary["cv"] for summary in per_channel.values()
        if summary["cv"] is not None
    ]

    total_rejected = sum(
        summary["rejected"] for summary in per_channel.values()
    )

    return {
        "spectrum": spectrum,
        "statistics": {
            "illumination": (block or {}).get("illumination"),
            "repeats": len(acquisitions),
            "estimator": "median",
            "channels": per_channel,
            "missing_channels": missing,
            "rejected_total": total_rejected,
            "mean_cv": round(mean(usable), 5) if usable else None,
            "max_cv": round(max(usable), 5) if usable else None,
            "unstable_channels": sorted(
                channel for channel, summary in per_channel.items()
                if summary["cv"] is not None
                and summary["cv"] > config.CV_WARNING
            ),
        },
        "acquisitions": acquisitions,
    }
