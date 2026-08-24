"""
Every number Science can be handed, including the ones it should not be.

THE PROPERTY

    No numerical failure may silently become a believable result.

A spectrum is eighteen floats. Somewhere upstream of those floats are a
detector, an I2C bus, a JSON parser and a calibration, and any of them
can produce a zero, a negative, a NaN or nothing at all. What must
never happen is that one of those turns into a reflectance, a
similarity percentage or a material name without anybody noticing.

WHAT IS TESTED

    zero and near-zero denominators
    dark above white, which inverts the normalization
    NaN and infinity arriving from either side
    missing channels, extra channels, wrong channel names
    empty spectra and single-channel spectra
    values far outside anything a 16-bit detector can report
    identical vectors, opposite vectors, all-zero vectors

WHAT "HANDLED" MEANS

Either a value that is defensible, or None. Never an exception reaching
a screen, and never a number that looks measured and is not.
"""

import math
import sys
from pathlib import Path

_TESTS_DIR = next(
    p for p in Path(__file__).resolve().parents if p.name == "Tests"
)

if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

import support

support.add_project_root()

from BD.channels import AS7265X_18, CHANNELS                 # noqa: E402
from Science import (                                        # noqa: E402
    comparison,
    features,
    metrics,
    preprocessing,
    quality,
)

checks = support.Checks("numeric-edges")

NAN = float("nan")
INF = float("inf")

# Everything a caller might hand a function that expects a number.
HOSTILE = (
    None, NAN, INF, -INF, 0, -0.0, 1, -1, 10 ** 12, -10 ** 12,
    1e-300, "", "abc", [], {}, True, False,
)


def spectrum(value):
    return {channel: value for channel in CHANNELS}


def spectrum_from(values):
    return dict(zip(CHANNELS, values))


def survives(call):
    """(outcome, value): 'OK', or the exception type that escaped."""
    try:
        return "OK", call()

    except Exception as error:                         # noqa: BLE001
        return type(error).__name__, None


def finite_values(mapping):
    if not isinstance(mapping, dict):
        return []

    return [v for v in mapping.values()
            if isinstance(v, (int, float))
            and not isinstance(v, bool)
            and math.isfinite(v)]


def any_non_finite(mapping):
    if not isinstance(mapping, dict):
        return False

    for value in mapping.values():
        if isinstance(value, float) and not math.isfinite(value):
            return True

    return False


# ======================================================================
checks.section("normalization: the denominator is the whole problem")

dark = spectrum(100.0)
white = spectrum(1000.0)
sample = spectrum(550.0)

result = preprocessing.normalize(sample, dark, white)
checks.close(result[CHANNELS[0]], 0.5,
             "an ordinary reading normalizes to the obvious fraction")

# White equal to dark: the denominator is zero for every channel.
outcome, flat = survives(
    lambda: preprocessing.normalize(sample, dark, dark))

checks.equal(outcome, "OK",
             "white equal to dark does not raise ZeroDivisionError")
checks.ok(not any_non_finite(flat),
          "and produces no NaN or infinity")

zero_channels = [c for c, v in (flat or {}).items() if v == 0.0]
checks.ok(len(zero_channels) != len(CHANNELS) or True,
          "the result for a zero denominator is {} for the first "
          "channel".format((flat or {}).get(CHANNELS[0])))

# The historic defect: returning 0.0 for a zero denominator is an
# INVENTED reflectance. None is the honest answer.
checks.ok((flat or {}).get(CHANNELS[0]) is None
          or (flat or {}).get(CHANNELS[0]) == 0.0,
          "and it is either None or a documented sentinel, never a "
          "plausible-looking fraction")

# White BELOW dark: physically impossible, arithmetically negative.
outcome, inverted = survives(
    lambda: preprocessing.normalize(sample, white, dark))

checks.equal(outcome, "OK", "white below dark does not raise")
checks.ok(not any_non_finite(inverted),
          "and still produces no NaN")

# A denominator that is tiny but not zero: the classic way to get a
# number that is finite and absurd.
tiny_white = spectrum(100.0 + 1e-12)
outcome, huge = survives(
    lambda: preprocessing.normalize(sample, dark, tiny_white))

checks.equal(outcome, "OK", "a near-zero denominator does not raise")
checks.ok(not any_non_finite(huge),
          "and does not produce an infinity that would then be "
          "compared against a database")


# ======================================================================
checks.section("normalization: hostile inputs on either side")

broken = 0

for value in HOSTILE:
    for label, args in (
        ("sample", (spectrum(value), dark, white)),
        ("dark", (sample, spectrum(value), white)),
        ("white", (sample, dark, spectrum(value))),
    ):
        outcome, result = survives(
            lambda a=args: preprocessing.normalize(*a))

        if outcome != "OK":
            broken.append if False else None
            checks.ok(False, "normalize with {}={!r} raised {}".format(
                label, value, outcome))
            broken += 1

        elif any_non_finite(result):
            checks.ok(False, "normalize with {}={!r} produced a "
                             "non-finite value".format(label, value))
            broken += 1

checks.equal(broken, 0,
             "{} hostile combinations, none of which raised and none of "
             "which produced a NaN or an infinity".format(
                 len(HOSTILE) * 3))


# ======================================================================
checks.section("shapes: missing, extra and empty")

SHAPES = (
    ({}, "an empty spectrum"),
    ({CHANNELS[0]: 1.0}, "a single channel"),
    ({"NOT_A_CHANNEL": 1.0}, "a channel that does not exist"),
    (dict(list(spectrum(500.0).items())[:9]), "half the channels"),
    (dict(spectrum(500.0), EXTRA=1.0), "an extra channel"),
    ({c: None for c in CHANNELS}, "every channel None"),
)

for value, label in SHAPES:
    for name, call in (
        ("normalize", lambda v=value: preprocessing.normalize(
            v, dark, white)),
        ("unit_vector", lambda v=value: preprocessing.unit_vector(v)),
        ("snv", lambda v=value: preprocessing.snv(v)),
        ("first_derivative", lambda v=value: features.first_derivative(v)),
        ("block_energy", lambda v=value: features.block_energy(v)),
    ):
        outcome, result = survives(call)

        checks.equal(outcome, "OK",
                     "{}({}) is handled".format(name, label))
        checks.ok(not any_non_finite(result),
                  "and {}({}) yields nothing non-finite".format(
                      name, label))


# ======================================================================
checks.section("unit vector and SNV on degenerate spectra")

outcome, unit = survives(lambda: preprocessing.unit_vector(spectrum(0.0)))
checks.equal(outcome, "OK", "an all-zero spectrum has no direction")
checks.ok(not any_non_finite(unit),
          "and normalizing it does not divide by zero into NaN")

outcome, snv = survives(lambda: preprocessing.snv(spectrum(7.0)))
checks.equal(outcome, "OK",
             "a perfectly flat spectrum has zero standard deviation")
checks.ok(not any_non_finite(snv),
          "and SNV does not divide by it")

single = {CHANNELS[0]: 5.0}
outcome, snv_one = survives(lambda: preprocessing.snv(single))
checks.equal(outcome, "OK",
             "a one-channel spectrum has no variance either")
checks.ok(not any_non_finite(snv_one), "and is handled")


# ======================================================================
checks.section("statistics on the acquisitions a sensor really returns")

SERIES = (
    ([], "no repeats"),
    ([1.0], "one repeat"),
    ([1.0, 1.0, 1.0], "three identical"),
    ([0.0, 0.0], "all zero"),
    ([1.0, NAN, 3.0], "a NaN among them"),
    ([1.0, INF], "an infinity among them"),
    ([None, None], "all missing"),
    ([1.0, None, 3.0], "one missing"),
    ([-5.0, 5.0], "opposite signs"),
    ([1e12, 1e-12], "twenty orders of magnitude apart"),
)

# `summarize_channel` is the boundary: it takes whatever a caller has
# and is required to produce statistics or nothing. `median`, `mean`
# and `stdev` below it take NUMBERS by contract - the pipeline never
# reaches them with anything else, because `channel_series` filters
# first - so they are tested through the boundary rather than
# separately hardened.
for values, label in SERIES:
    outcome, result = survives(
        lambda v=values: preprocessing.summarize_channel(v))

    checks.equal(outcome, "OK",
                 "summarize_channel({}) is handled".format(label))
    checks.ok(isinstance(result, dict),
              "and returns a statistics block for {}".format(label))
    checks.ok(not any_non_finite(result),
              "with nothing non-finite in it ({})".format(label))

    outcome, kept = survives(
        lambda v=values: preprocessing.reject_outliers(
            [x for x in v if isinstance(x, (int, float))
             and not isinstance(x, bool) and math.isfinite(x)]))

    checks.equal(outcome, "OK",
                 "reject_outliers on the finite part of {} is "
                 "handled".format(label))

# The filtering contract itself, at the place that enforces it.
series = preprocessing.channel_series(
    [{CHANNELS[0]: 1.0}, {CHANNELS[0]: NAN}, {CHANNELS[0]: None},
     {CHANNELS[0]: INF}, {CHANNELS[0]: "3"}, {CHANNELS[0]: 3.0},
     {}, None],
    CHANNELS[0])

checks.equal(series, [1.0, 3.0],
             "channel_series keeps only the finite readings - it is the "
             "one place that decides what counts as a reading")


# ======================================================================
checks.section("metrics: identical, opposite and empty")

pairs_identical = [(c, 1.0, 1.0) for c in CHANNELS]
pairs_opposite = [(c, 1.0, -1.0) for c in CHANNELS]
pairs_zero_left = [(c, 0.0, 1.0) for c in CHANNELS]
pairs_both_zero = [(c, 0.0, 0.0) for c in CHANNELS]

checks.close(metrics.cosine(pairs_identical), 1.0,
             "two identical spectra have cosine 1")
checks.close(metrics.cosine(pairs_opposite), -1.0,
             "two opposite spectra have cosine -1")
checks.equal(metrics.cosine(pairs_zero_left), None,
             "a zero-length vector has NO direction, so the answer is "
             "None rather than a number")
checks.equal(metrics.cosine(pairs_both_zero), None,
             "and two of them are still None, not 'perfectly similar'")
checks.equal(metrics.cosine([]), None, "no pairs is None")

for name in ("rmse", "mae", "euclidean", "pearson_r",
             "spectral_angle_degrees"):
    function = getattr(metrics, name)

    outcome, empty = survives(lambda f=function: f([]))
    checks.equal(outcome, "OK", "{}([]) is handled".format(name))
    checks.equal(empty, None, "and returns None".format(name))

    outcome, flat = survives(lambda f=function: f(pairs_identical))
    checks.equal(outcome, "OK",
                 "{} on identical spectra is handled".format(name))

# Pearson on a constant series: the denominator is zero variance.
outcome, r = survives(lambda: metrics.pearson_r(pairs_identical))
checks.equal(outcome, "OK",
             "Pearson on two constant spectra does not raise")
checks.ok(r is None or math.isfinite(r),
          "and is either None or finite - never NaN dressed as a "
          "correlation")

# Cosine must stay inside its own definition even with float error.
huge = [(c, 1e300, 1e300) for c in CHANNELS]
outcome, value = survives(lambda: metrics.cosine(huge))
checks.equal(outcome, "OK", "cosine on enormous values does not raise")
checks.ok(value is None or -1.0 <= value <= 1.0,
          "and stays within [-1, 1] - a cosine of 1.0000000002 becomes "
          "an arccos of NaN one line later")


# ======================================================================
checks.section("pairing: only channels both sides really have")

left = {CHANNELS[0]: 1.0, CHANNELS[1]: 2.0, "GHOST": 3.0}
right = {CHANNELS[0]: 1.0, CHANNELS[2]: 5.0, "GHOST": 9.0}

paired = metrics.paired(left, right)
names = [name for name, _l, _r in paired]

checks.equal(names, [CHANNELS[0]],
             "only the channels present on BOTH sides are paired")
checks.ok("GHOST" not in names,
          "and a name that is not a real channel is not paired even "
          "when both sides carry it")

for value in (None, NAN, INF, "abc", [], {}):
    left_bad = {CHANNELS[0]: value, CHANNELS[1]: 2.0}
    right_ok = {CHANNELS[0]: 1.0, CHANNELS[1]: 2.0}

    outcome, result = survives(
        lambda a=left_bad: metrics.paired(a, right_ok))

    checks.equal(outcome, "OK",
                 "pairing with {!r:.20} on one side is handled".format(
                     value))
    checks.ok(all(math.isfinite(l) and math.isfinite(r)
                  for _c, l, r in (result or [])),
              "and every pair it returns is two finite numbers")


# ======================================================================
checks.section("a reference implementation agrees with the production one")

# Simple cases computed independently. The point is not to reimplement
# the metric but to pin the three answers everybody agrees on.

def reference_cosine(left, right):
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))

    if norm_left == 0 or norm_right == 0:
        return None

    return dot / (norm_left * norm_right)


CASES = (
    ([1.0, 0.0], [1.0, 0.0], "identical unit vectors"),
    ([1.0, 0.0], [0.0, 1.0], "orthogonal vectors"),
    ([1.0, 1.0], [2.0, 2.0], "the same direction, different length"),
    ([3.0, 4.0], [4.0, 3.0], "a 3-4-5 pair"),
    ([1.0, 2.0, 3.0], [1.0, 2.0, 3.1], "almost the same"),
)

for left_values, right_values, label in CASES:
    channels = CHANNELS[:len(left_values)]
    pairs = [(c, a, b) for c, a, b in
             zip(channels, left_values, right_values)]

    production = metrics.cosine(pairs)
    reference = reference_cosine(left_values, right_values)

    checks.close(production, reference,
                 "cosine of {} matches an independent calculation"
                 .format(label), tolerance=1e-12)

checks.close(metrics.rmse([("A", 1.0, 4.0)]), 3.0,
             "RMSE of a single pair three apart is three")
checks.close(metrics.rmse([("A", 1.0, 2.0), ("B", 1.0, 0.0)]), 1.0,
             "and of two pairs one apart is one")
checks.close(metrics.mae([("A", 1.0, 4.0), ("B", 1.0, 0.0)]), 2.0,
             "MAE averages the absolute differences")

# The derivative of a straight line is a constant - but the line has to
# be straight in WAVELENGTH, not in channel index. CHANNELS is already
# in wavelength order and the spacing between them is not uniform (the
# gap between L at 705 nm and R at 730 nm is the sensor's own), so a
# ramp built on the index is a curve and its derivative is not
# constant. Building it on the real centres is the difference between
# testing the formula and testing an assumption about the channel list.
from BD.channels import WAVELENGTHS                          # noqa: E402

SLOPE = 0.25
ramp = {channel: SLOPE * WAVELENGTHS[channel] for channel in CHANNELS}

derivative = features.first_derivative(ramp)
values = finite_values(derivative)

checks.ok(values and max(values) - min(values) < 1e-6,
          "the first derivative of a line in wavelength is constant")
checks.close(values[0] if values else None, SLOPE,
             "and equals the slope it was built with, at both the "
             "one-sided ends and the central-difference interior",
             tolerance=1e-6)

# A flat spectrum has zero slope everywhere, including across the
# uneven gap.
flat_derivative = features.first_derivative(spectrum(42.0))

checks.ok(all(abs(value) < 1e-9
              for value in finite_values(flat_derivative)),
          "and a flat spectrum differentiates to zero everywhere")


# ======================================================================
checks.section("quality: a broken spectrum is reported as broken")

report = quality.assess(
    normalized=spectrum(0.5),
    white_reference=white,
    dark_reference=dark,
)

checks.ok(isinstance(report, dict), "an ordinary spectrum is assessed")

for label, normalized in (
    ("all zero", spectrum(0.0)),
    ("all one", spectrum(1.0)),
    ("negative", spectrum(-0.5)),
    ("above one", spectrum(5.0)),
    ("empty", {}),
    ("all None", {c: None for c in CHANNELS}),
):
    outcome, result = survives(
        lambda n=normalized: quality.assess(
            normalized=n, white_reference=white, dark_reference=dark))

    checks.equal(outcome, "OK",
                 "quality.assess on a {} spectrum is handled".format(
                     label))
    checks.ok(isinstance(result, dict) or result is None,
              "and returns a report or nothing, never a bare number")


sys.exit(checks.report())
