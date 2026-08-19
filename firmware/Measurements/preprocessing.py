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
from Measurements import config

# The reference-conditioning thresholds live in Measurements/config.py so
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

    Where the denominator is zero the reflectance is undefined and the
    channel is reported as 0.0 rather than raising; `conditioning()`
    names which channels those were, so the result can be read honestly.
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
            result[channel] = 0.0

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
