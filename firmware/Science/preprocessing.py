"""
Spectral representations — deterministic, reversible, opinion-free.

One measurement is looked at several ways, because different comparisons
need different things and no single representation serves them all:

    RAW              what the detector reported. The source of truth.
    DARK_CORRECTED   S - D. Removes the detector's own offset.
    NORMALIZED       (S - D) / (W - D). Reflectance, and the only
                     representation that can be compared with a library.
    UNIT             x / ||x||. Shape with brightness removed.
    SNV              (x - mean) / stdev. Shape with brightness AND
                     offset removed - the standard scatter correction for
                     powders, where particle size changes overall level.

SNV DOES NOT REPLACE RAW. It is an additional view: applied to a spectrum
whose absolute level is the discriminating feature - a dark powder
against a bright one - it deletes exactly the evidence that mattered.
Both go into the evidence package and the decision layer chooses.

NOTHING IS CLIPPED. A reflectance above 1.0 means the sample returned
more light than the white reference did, which is real information about
geometry or scattering. Clamping it to 1.0 would silently manufacture
agreement with the library.

This module reaches no conclusion about what the sample is.
"""

import math

from BD.channels import CHANNELS, ILLUMINATIONS, copy_channels
from Science import config

# The reference-conditioning thresholds live in Science/config.py so
# that this module and channel_reliability cannot disagree about what a
# usable denominator is. Neither is a threshold on the RESULT - the
# reflectance is always computed and stored exactly as it comes out.
MIN_SAFE_DENOMINATOR = config.REFERENCE_RANGE_UNUSABLE
USABLE_DENOMINATOR = config.REFERENCE_RANGE_POOR

# Guard for ratio representations. Small enough not to distort a real
# ratio, large enough that 0/0 does not become infinity.
RATIO_EPSILON = 1e-6


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False

    return not (math.isnan(value) or math.isinf(value))


def dark_correct(sample, dark, decimals=None):
    """
    C = S - D, per channel.

    Negative results are KEPT. A channel reading below the stored dark
    reference is real information about noise or drift, and clamping it
    to zero hides a drifting detector from the operator.
    """
    if decimals is None:
        decimals = config.RAW_DECIMALS

    sample = copy_channels(sample)
    dark = copy_channels(dark)

    return {
        channel: round(sample[channel] - dark[channel], decimals)
        for channel in CHANNELS
    }


def normalize(sample, dark, white, decimals=None):
    """
    R = (S - D) / (W - D), per channel.

    AN UNDEFINED CHANNEL IS None, NOT ZERO.

    Where W - D is zero the reflectance does not exist, and 0.0 is not
    a way of saying so - it is a measurement. It says the channel
    reflects nothing, which is a strong scientific claim, and it is
    indistinguishable from a genuinely black channel. Every metric
    downstream would then quietly include it: a zero pulls a cosine, an
    RMSE and a class distance in a direction the instrument never
    reported.

    None propagates honestly instead. `paired()` drops it, the quality
    report counts it as an invalid channel, and nothing computes an
    average over a number that was never measured. `conditioning()`
    says WHY it happened, channel by channel.
    """
    if decimals is None:
        decimals = config.NORMALIZED_DECIMALS

    sample = copy_channels(sample)
    dark = copy_channels(dark)
    white = copy_channels(white)

    result = {}

    for channel in CHANNELS:
        denominator = white[channel] - dark[channel]

        if denominator == 0:
            result[channel] = None

            continue

        result[channel] = round(
            (sample[channel] - dark[channel]) / denominator, decimals
        )

    return result


def conditioning(dark, white):
    """
    How well posed the normalization is, channel by channel.

    This is a property of the CALIBRATION, not of any sample: it says how
    much dynamic range the reference left for the division. A denominator
    of two counts turns a one-count sample difference into a reflectance
    step of 0.5, which is quantization wearing a reflectance's clothing.
    """
    dark = copy_channels(dark)
    white = copy_channels(white)

    report = {}

    for channel in CHANNELS:
        denominator = white[channel] - dark[channel]

        report[channel] = {
            "reference_minus_dark": round(denominator, 4),
            "defined": denominator >= MIN_SAFE_DENOMINATOR,
            "usable": denominator >= USABLE_DENOMINATOR,
            "quantization_step": (
                round(1.0 / denominator, 6)
                if denominator >= RATIO_EPSILON else None
            ),
        }

    return report


def unit_vector(spectrum):
    """x / ||x||. Shape only; brightness deleted deliberately."""
    values = [
        spectrum.get(channel) for channel in CHANNELS
        if _finite(spectrum.get(channel))
    ]

    norm = math.sqrt(sum(value * value for value in values))

    if norm <= 0:
        return {channel: 0.0 for channel in CHANNELS}

    return {
        channel: round(spectrum[channel] / norm, 6)
        if _finite(spectrum.get(channel)) else 0.0
        for channel in CHANNELS
    }


def snv(spectrum):
    """
    Standard Normal Variate: (x - mean) / sample stdev.

    The standard scatter correction for powders, where particle size and
    packing change the overall level without changing the chemistry. It
    removes both level and scale, so it is blind to exactly the evidence
    that separates a dark powder from a bright one - which is why it is
    an ADDITIONAL representation and never a replacement.
    """
    values = [
        spectrum.get(channel) for channel in CHANNELS
        if _finite(spectrum.get(channel))
    ]

    if len(values) < 2:
        return {channel: 0.0 for channel in CHANNELS}

    mean = sum(values) / float(len(values))
    variance = sum((value - mean) ** 2 for value in values) / float(
        len(values) - 1
    )
    deviation = math.sqrt(variance)

    if deviation <= 0:
        return {channel: 0.0 for channel in CHANNELS}

    return {
        channel: round((spectrum[channel] - mean) / deviation, 6)
        if _finite(spectrum.get(channel)) else 0.0
        for channel in CHANNELS
    }


def build_representations(raw_blocks, dark, white_by_illumination,
                          decimals=None):
    """
    Every deterministic view of one measurement, in one structure.

    `raw_blocks` is {illumination: {channel: counts}}; `dark` is the one
    dark reference; `white_by_illumination` is the white reference for
    each lamp. Illuminations with no reference are still processed as far
    as they can be - raw and dark-corrected exist without a white - and
    are marked rather than dropped.
    """
    representations = {
        "raw": {},
        "dark_corrected": {},
        "normalized": {},
        "unit": {},
        "snv": {},
        "available": [],
        "missing_reference": [],
    }

    for illumination in ILLUMINATIONS:
        spectrum = raw_blocks.get(illumination)

        if not spectrum:
            continue

        representations["available"].append(illumination)
        representations["raw"][illumination] = copy_channels(spectrum)

        corrected = dark_correct(spectrum, dark, decimals)
        representations["dark_corrected"][illumination] = corrected

        white = (white_by_illumination or {}).get(illumination)

        if not white:
            representations["missing_reference"].append(illumination)

            continue

        normalized = normalize(spectrum, dark, white, decimals)

        representations["normalized"][illumination] = normalized
        representations["unit"][illumination] = unit_vector(normalized)
        representations["snv"][illumination] = snv(normalized)

    return representations

# ====================================================================
# repeated acquisitions
# ====================================================================
# A single reading is not a measurement. The instrument takes several
# and these turn them into one spectrum plus an honest statement of
# how much they varied - which is the only evidence there is that a
# channel is stable rather than merely present.
# 
# This lives with the corrections rather than in a module of its own
# because it is the same job: turning what the instrument produced
# into something comparable.

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
