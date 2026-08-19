"""
Derived spectral features: derivatives, energies and illumination ratios.

Everything here is arithmetic on a spectrum. None of it decides what the
sample is.

THE DERIVATIVE IS PER NANOMETRE, NOT PER CHANNEL

The AS7265x bands are not evenly spaced:

    410 435 460 485 510 535 560 585 610 645 680 705 730 760 810 860 900 940
      25  25  25  25  25  25  25  25  35  35  25  25  30  50  50  40  40

`x[i+1] - x[i]` therefore measures two different things in the visible
and in the NIR: the same physical slope reads 50 units per step at 810 nm
and 25 at 510 nm. Dividing by the real wavelength gap makes the number a
slope per nanometre, which is comparable across the array and across
instruments.

The interior points use the CENTRAL difference over the true spacing,

    f'(x_i) = (f_{i+1} - f_{i-1}) / (l_{i+1} - l_{i-1})

which is the standard second-order estimate on a non-uniform grid; the
two ends use the one-sided difference, because there is nothing beyond
them and extrapolating would be inventing a band.

THE SECOND DERIVATIVE IS EXPERIMENTAL

It is the classic way to pull a weak absorption feature off a sloping
baseline, and it is also a very effective noise amplifier: differencing
twice across bands 25-50 nm apart, on 18 sparse channels, may be
measuring the detector rather than the mineral. It is computed, labelled
`experimental`, and no decision rule uses it until validation on real
repeats says it carries signal.
"""

import math

from BD.channels import CHANNELS, ILLUMINATIONS, WAVELENGTHS

# Guard for ratio and log-ratio features. The UV lamp genuinely reads
# zero on the long-wavelength channels, and 0/0 must not become infinity
# or NaN - it must become a number with a reliability flag beside it.
EPSILON = 1e-6

# Wavelength blocks used for the energy summary. These follow the three
# physical detector dies, so a block is a thing the hardware actually has
# rather than an arbitrary slice of the array.
BLOCKS = {
    "uv_die": ("A", "B", "C", "D", "E", "F"),
    "visible_die": ("G", "H", "I", "J", "K", "L"),
    "nir_die": ("R", "S", "T", "U", "V", "W"),
}


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False

    return not (math.isnan(value) or math.isinf(value))


def _ordered(spectrum):
    """(channel, wavelength, value) in wavelength order, finite only."""
    points = []

    for channel in CHANNELS:
        value = spectrum.get(channel)

        if _finite(value):
            points.append((channel, float(WAVELENGTHS[channel]), float(value)))

    points.sort(key=lambda item: item[1])

    return points


def first_derivative(spectrum, decimals=8):
    """
    d(value)/d(lambda), in units per nanometre.

    Central difference over the true wavelength gap on the interior,
    one-sided at the two ends. Channels that are not finite are skipped
    entirely rather than interpolated: an absent band is absent, and
    filling it in would invent a measurement.
    """
    points = _ordered(spectrum)

    if len(points) < 2:
        return {channel: 0.0 for channel in CHANNELS}

    result = {channel: 0.0 for channel in CHANNELS}

    for index, (channel, wavelength, value) in enumerate(points):
        if index == 0:
            _next_channel, next_wavelength, next_value = points[1]
            slope = (next_value - value) / (next_wavelength - wavelength)

        elif index == len(points) - 1:
            _previous_channel, previous_wavelength, previous_value = points[
                index - 1
            ]
            slope = (value - previous_value) / (
                wavelength - previous_wavelength
            )

        else:
            _before, before_wavelength, before_value = points[index - 1]
            _after, after_wavelength, after_value = points[index + 1]

            span = after_wavelength - before_wavelength

            slope = (after_value - before_value) / span if span else 0.0

        result[channel] = round(slope, decimals)

    return result


def second_derivative(spectrum, decimals=10):
    """
    EXPERIMENTAL. The derivative of the derivative, same grid rules.

    Reported so it can be validated, not because it is trusted. See the
    module docstring.
    """
    return first_derivative(first_derivative(spectrum), decimals=decimals)


def block_energy(spectrum):
    """
    Robust level per detector die, and over the whole array.

    Sum, mean and root-mean-square are all reported: a sum is dominated
    by the brightest band, an RMS by the spread, and which of them is
    informative depends on the material. Choosing one here would be a
    decision, and decisions do not live in this layer.
    """
    report = {}

    for name, channels in BLOCKS.items():
        values = [
            float(spectrum[channel]) for channel in channels
            if _finite(spectrum.get(channel))
        ]

        if not values:
            report[name] = {
                "channels": 0, "sum": None, "mean": None, "rms": None,
                "max": None,
            }

            continue

        report[name] = {
            "channels": len(values),
            "sum": round(sum(values), 6),
            "mean": round(sum(values) / len(values), 6),
            "rms": round(
                math.sqrt(sum(value * value for value in values)
                          / len(values)), 6
            ),
            "max": round(max(values), 6),
        }

    values = [
        float(spectrum[channel]) for channel in CHANNELS
        if _finite(spectrum.get(channel))
    ]

    report["total"] = {
        "channels": len(values),
        "sum": round(sum(values), 6) if values else None,
        "mean": round(sum(values) / len(values), 6) if values else None,
        "rms": round(
            math.sqrt(sum(value * value for value in values) / len(values)), 6
        ) if values else None,
        "max": round(max(values), 6) if values else None,
    }

    return report


def ratio(numerator, denominator, epsilon=EPSILON, decimals=6):
    """
    Per-channel ratio with a guarded denominator.

    `guarded` names the channels where the denominator was small enough
    for epsilon to matter, so the caller can see which numbers are
    ratios and which are artefacts of the guard.
    """
    values = {}
    guarded = []

    for channel in CHANNELS:
        top = numerator.get(channel)
        bottom = denominator.get(channel)

        if not _finite(top) or not _finite(bottom):
            values[channel] = None

            continue

        if abs(bottom) < epsilon:
            guarded.append(channel)

        values[channel] = round(top / (bottom + epsilon), decimals)

    return {"values": values, "guarded_channels": guarded}


def log_ratio(numerator, denominator, epsilon=EPSILON, decimals=6):
    """
    log(a + eps) - log(b + eps).

    A ratio's distribution is wildly skewed - halving and doubling are
    the same size of change but land at 0.5 and 2.0 - and taking logs
    makes them symmetric, which is what any distance metric assumes.
    Negative inputs have no logarithm, so those channels report None
    rather than a fabricated value.
    """
    values = {}
    undefined = []

    for channel in CHANNELS:
        top = numerator.get(channel)
        bottom = denominator.get(channel)

        if not _finite(top) or not _finite(bottom):
            values[channel] = None

            continue

        if top + epsilon <= 0 or bottom + epsilon <= 0:
            values[channel] = None
            undefined.append(channel)

            continue

        values[channel] = round(
            math.log(top + epsilon) - math.log(bottom + epsilon), decimals
        )

    return {"values": values, "undefined_channels": undefined}


def cross_illumination(blocks):
    """
    UV/WHITE, IR/WHITE and UV/IR, as ratios and as log-ratios.

    These are the only features that use more than one lamp at once, and
    they are the reason the instrument fires three: a material that looks
    identical under white light may differ in how much it fluoresces or
    absorbs in the near-UV.

    Whether they carry usable signal on THIS instrument is an open
    question - the UV lamp is weak above 610 nm - so they are computed
    with their guard flags attached and the decision layer weighs them by
    measured channel reliability, not by assumption.
    """
    report = {}

    pairs = (
        ("uv_over_white", "uv", "white"),
        ("ir_over_white", "ir", "white"),
        ("uv_over_ir", "uv", "ir"),
    )

    for name, top_name, bottom_name in pairs:
        top = blocks.get(top_name)
        bottom = blocks.get(bottom_name)

        if not top or not bottom:
            report[name] = {
                "available": False,
                "reason": "needs both {} and {}".format(
                    top_name.upper(), bottom_name.upper()
                ),
            }

            continue

        report[name] = {
            "available": True,
            "ratio": ratio(top, bottom),
            "log_ratio": log_ratio(top, bottom),
        }

    return report


def build_features(representations):
    """
    Every derived feature, from the representations that exist.

    Derivatives are taken on the NORMALIZED spectrum where one exists and
    on the dark-corrected counts otherwise, and the record says which -
    a derivative of counts and a derivative of reflectance are not the
    same quantity and must not be compared with each other.
    """
    normalized = representations.get("normalized") or {}
    corrected = representations.get("dark_corrected") or {}

    features = {
        "derivative_basis": {},
        "first_derivative": {},
        "second_derivative_experimental": {},
        "energy": {},
        "cross_illumination": {},
    }

    for illumination in ILLUMINATIONS:
        source = normalized.get(illumination)
        basis = "normalized"

        if not source:
            source = corrected.get(illumination)
            basis = "dark_corrected"

        if not source:
            continue

        features["derivative_basis"][illumination] = basis
        features["first_derivative"][illumination] = first_derivative(source)
        features["second_derivative_experimental"][illumination] = (
            second_derivative(source)
        )
        features["energy"][illumination] = block_energy(source)

    features["cross_illumination"] = cross_illumination(
        corrected if corrected else normalized
    )
    features["cross_illumination_basis"] = (
        "dark_corrected" if corrected else "normalized"
    )

    return features
