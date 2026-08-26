"""
The mathematics, checked as properties rather than as examples.

WHY PROPERTIES

`test_science.py` checks worked examples: this spectrum against that
reference gives this cosine. That catches an arithmetic slip, and it
cannot catch a formula that is wrong in a way the chosen example does
not reveal - a similarity that is not symmetric, a distance that can be
negative, a normalization that quietly rescales.

So this generates many valid spectra and asserts the things that must
be true of ALL of them:

    67   sim(x, x) = 1, sim(a, b) = sim(b, a), distances are non-negative
    68   two independent routes to the same number agree
    69   very small, very large, subnormal and negative zero
    70   a float that goes to JSON and comes back is still the same float
    66   and structurally invalid input is rejected, never averaged

THE RULE THIS FILE DEFENDS

    Never produce a believable material identification from data whose
    structure is wrong.

A metric that returns a number for a spectrum with a missing channel,
a duplicated channel or a NaN in it is worse than one that raises,
because the number reaches a screen and the operator reads a mineral
name off it.

WHAT IS FAKED

Nothing. Science is pure arithmetic over dictionaries; it is called
directly.
"""

import json
import math
import random
import sys
from pathlib import Path

_TESTS_DIR = next(
    p for p in Path(__file__).resolve().parents if p.name == "Tests"
)

if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

_SOFTWARE_DIR = _TESTS_DIR / "software"

if str(_SOFTWARE_DIR) not in sys.path:
    sys.path.insert(0, str(_SOFTWARE_DIR))

import support

support.add_project_root()

from BD.channels import CHANNELS as ALL_CHANNELS            # noqa: E402

from Science import metrics, preprocessing                  # noqa: E402

checks = support.Checks("science-properties")

CHANNELS = list(ALL_CHANNELS)


# ----------------------------------------------------------------------
# generators
# ----------------------------------------------------------------------

def spectrum(rng, low=0.01, high=1.0):
    """A structurally valid spectrum: every channel, finite, positive."""
    return {channel: rng.uniform(low, high) for channel in CHANNELS}


def scaled(source, factor):
    return {channel: value * factor for channel, value in source.items()}


SEEDS = tuple(range(40))


# ======================================================================
checks.section("67. cosine similarity, as a property")

# sim(x, x) = 1 for every valid non-zero x. If this fails for any
# generated vector, the metric cannot be trusted for any of them.

failures = []

for seed in SEEDS:
    rng = random.Random(seed)
    x = spectrum(rng)

    value = metrics.cosine(metrics.paired(x, x))

    if value is None or abs(value - 1.0) > 1e-12:
        failures.append("seed {}: sim(x,x) = {}".format(seed, value))

checks.equal(failures, [],
             "sim(x, x) = 1 for all {} generated spectra".format(len(SEEDS)))

# Symmetry. A similarity that depends on which argument came first
# would make a comparison depend on the order of the library.
failures = []

for seed in SEEDS:
    rng = random.Random(seed + 1000)
    a, b = spectrum(rng), spectrum(rng)

    forward = metrics.cosine(metrics.paired(a, b))
    backward = metrics.cosine(metrics.paired(b, a))

    if forward is None or backward is None:
        failures.append("seed {}: None".format(seed))

    elif abs(forward - backward) > 1e-12:
        failures.append("seed {}: {} vs {}".format(seed, forward, backward))

checks.equal(failures, [],
             "sim(a, b) = sim(b, a) for all {} generated pairs".format(
                 len(SEEDS)))

# BOUNDED, AND THE BOUND IS EXACT.
#
# This is a hard limit, not a tolerance: a similarity greater than 1 is
# not a rounding artefact to be forgiven, it is a value arccos cannot
# take and a percentage above 100.
#
# The first version of this case used `abs(value - 1.0) > 1e-12` and
# missed the real failure by four orders of magnitude. Removing the
# clamp from `cosine` makes sim(x, x) come out at
# 1.0000000000000002 - ONE ULP over - and a 1e-12 tolerance waves that
# through. The mutation campaign caught it: "science: let cosine leave
# [-1, 1]" survived, which is the whole reason that campaign exists.
#
# The magnitudes matter too. Rounding only accumulates far enough to
# cross the bound when the values are large, and raw AS7265X counts
# ARE large - hundreds of thousands - so the generator below covers
# that range rather than the tidy 0-to-1 one.
out_of_range = []

MAGNITUDES = (
    (0.01, 1.0, "reflectance scale"),
    (1.0, 65535.0, "raw counts"),
    (14000.0, 970000.0, "large raw counts"),
    (1e-6, 1e-3, "very small"),
)

for low, high, label in MAGNITUDES:
    for seed in SEEDS:
        rng = random.Random(seed + 2000)
        a = spectrum(rng, low, high)
        b = spectrum(rng, low, high)

        for left, right, what in ((a, a, "sim(x,x)"),
                                  (a, b, "sim(a,b)")):
            value = metrics.cosine(metrics.paired(left, right))

            if value is None:
                continue

            if value > 1.0 or value < -1.0:
                out_of_range.append(
                    "{} {} at {}: {!r}".format(
                        what, label, seed, value))

checks.equal(out_of_range[:5], [],
             "no cosine EXCEEDS 1.0 or falls below -1.0, across {} "
             "magnitude ranges - an exact bound, not a tolerance".format(
                 len(MAGNITUDES)))

# And the specific shape that finds it, stated so it cannot be
# generated away: a spectrum of large raw counts, against itself.
HEAVY_VALUES = (
    810217.2359965895, 902165.9504395827, 310147.5693193326,
    966606.3677707588, 14041.700164018956, 523311.9012345678,
    77123.45678901234, 640221.7654321098, 199382.1357924680,
    883014.6802468135, 402911.3579135790, 715600.2468013579,
    58234.90123456789, 934102.8642097531, 261745.5309876543,
    490338.1975308642, 812004.4321098765, 300441.6298765432,
)

heavy = dict(zip(CHANNELS, HEAVY_VALUES))

self_similarity = metrics.cosine(metrics.paired(heavy, heavy))

checks.ok(self_similarity is not None and self_similarity <= 1.0,
          "and a spectrum of large raw counts compared with itself is "
          "at most 1.0 ({!r}) - without the clamp this is "
          "1.0000000000000002".format(self_similarity))

checks.ok(abs(self_similarity - 1.0) < 1e-9,
          "while still being 1.0 to any scientifically useful precision")

# SCALE INVARIANCE. Cosine measures direction, so doubling a spectrum
# must not change what it resembles - this is the property that lets a
# brighter exposure of the same soil still match its reference.
failures = []

for seed in SEEDS:
    rng = random.Random(seed + 3000)
    a, b = spectrum(rng), spectrum(rng)

    base = metrics.cosine(metrics.paired(a, b))

    for factor in (0.5, 2.0, 10.0, 1000.0):
        scaled_value = metrics.cosine(metrics.paired(scaled(a, factor), b))

        if base is None or scaled_value is None:
            failures.append("seed {} factor {}: None".format(seed, factor))

        elif abs(base - scaled_value) > 1e-9:
            failures.append("seed {} factor {}: {} vs {}".format(
                seed, factor, base, scaled_value))

checks.equal(failures, [],
             "and scaling a spectrum does not change its cosine - "
             "brightness is not identity")


# ======================================================================
checks.section("67. distances are non-negative, and zero only on identity")

for name, function in (("rmse", metrics.rmse),
                       ("mae", metrics.mae),
                       ("euclidean", metrics.euclidean)):
    negative = []
    non_zero_identity = []
    asymmetric = []

    for seed in SEEDS:
        rng = random.Random(seed + 4000)
        a, b = spectrum(rng), spectrum(rng)

        value = function(metrics.paired(a, b))

        if value is not None and value < 0:
            negative.append("seed {}: {}".format(seed, value))

        identity = function(metrics.paired(a, a))

        if identity is None or abs(identity) > 1e-12:
            non_zero_identity.append("seed {}: {}".format(seed, identity))

        forward = function(metrics.paired(a, b))
        backward = function(metrics.paired(b, a))

        if forward is not None and backward is not None:
            if abs(forward - backward) > 1e-12:
                asymmetric.append("seed {}".format(seed))

    checks.equal(negative, [],
                 "{} is never negative".format(name))

    checks.equal(non_zero_identity, [],
                 "{}(x, x) = 0 exactly".format(name))

    checks.equal(asymmetric, [],
                 "{}(a, b) = {}(b, a)".format(name, name))

# The triangle inequality, for the one metric that claims to be a
# metric in the mathematical sense.
violations = []

for seed in SEEDS:
    rng = random.Random(seed + 5000)
    a, b, c = spectrum(rng), spectrum(rng), spectrum(rng)

    ab = metrics.euclidean(metrics.paired(a, b))
    bc = metrics.euclidean(metrics.paired(b, c))
    ac = metrics.euclidean(metrics.paired(a, c))

    if None in (ab, bc, ac):
        continue

    if ac > ab + bc + 1e-9:
        violations.append("seed {}: {} > {} + {}".format(seed, ac, ab, bc))

checks.equal(violations, [],
             "and euclidean satisfies the triangle inequality")


# ======================================================================
checks.section("67. spectral angle agrees with cosine, always")

# spectral_angle_degrees is documented as rank-identical to cosine. If
# the two ever disagreed on ORDER, one of the two rankings a screen can
# show would be wrong.

disagreements = []

for seed in SEEDS:
    rng = random.Random(seed + 6000)
    measured = spectrum(rng)
    library = [spectrum(rng) for _ in range(6)]

    by_cosine = sorted(
        range(len(library)),
        key=lambda index: metrics.cosine(
            metrics.paired(measured, library[index])),
        reverse=True,
    )
    by_angle = sorted(
        range(len(library)),
        key=lambda index: metrics.spectral_angle_degrees(
            metrics.paired(measured, library[index])),
    )

    if by_cosine != by_angle:
        disagreements.append("seed {}: {} vs {}".format(
            seed, by_cosine, by_angle))

checks.equal(disagreements, [],
             "cosine and spectral angle rank a library identically, over "
             "{} generated libraries".format(len(SEEDS)))


# ======================================================================
checks.section("68. two independent routes to the same number")

# A hand-written reference implementation beside the production one.
# One example can pass by luck; agreement across generated input is
# what makes the formula rather than the example the thing under test.

def reference_cosine(a, b):
    """Textbook cosine, written from the definition."""
    keys = [key for key in CHANNELS if key in a and key in b]
    dot = sum(a[key] * b[key] for key in keys)
    left = math.sqrt(sum(a[key] ** 2 for key in keys))
    right = math.sqrt(sum(b[key] ** 2 for key in keys))

    if left == 0 or right == 0:
        return None

    return dot / (left * right)


def reference_rmse(a, b):
    keys = [key for key in CHANNELS if key in a and key in b]

    if not keys:
        return None

    return math.sqrt(
        sum((a[key] - b[key]) ** 2 for key in keys) / len(keys))


def reference_mae(a, b):
    keys = [key for key in CHANNELS if key in a and key in b]

    if not keys:
        return None

    return sum(abs(a[key] - b[key]) for key in keys) / len(keys)


DIFFERENTIAL = (
    ("cosine", metrics.cosine, reference_cosine, 1e-12),
    ("rmse", metrics.rmse, reference_rmse, 1e-12),
    ("mae", metrics.mae, reference_mae, 1e-12),
)

for name, production, reference, tolerance in DIFFERENTIAL:
    mismatches = []

    for seed in SEEDS:
        rng = random.Random(seed + 7000)
        a, b = spectrum(rng), spectrum(rng)

        produced = production(metrics.paired(a, b))
        expected = reference(a, b)

        if produced is None and expected is None:
            continue

        if produced is None or expected is None:
            mismatches.append("seed {}: {} vs {}".format(
                seed, produced, expected))

        elif abs(produced - expected) > tolerance:
            mismatches.append("seed {}: {} vs {}".format(
                seed, produced, expected))

    checks.equal(mismatches, [],
                 "{} agrees with an independent implementation over {} "
                 "generated pairs".format(name, len(SEEDS)))

# normalize, against the definition R = (S - D) / (W - D).
mismatches = []

for seed in SEEDS:
    rng = random.Random(seed + 8000)
    dark = spectrum(rng, 0.0, 0.1)
    white = {channel: dark[channel] + rng.uniform(0.5, 1.0)
             for channel in CHANNELS}
    sample = {channel: dark[channel] + rng.uniform(0.0, 0.5)
              for channel in CHANNELS}

    produced = preprocessing.normalize(sample, dark, white, decimals=12)

    for channel in CHANNELS:
        expected = ((sample[channel] - dark[channel])
                    / (white[channel] - dark[channel]))

        if produced[channel] is None:
            mismatches.append("seed {} {}: None".format(seed, channel))

        elif abs(produced[channel] - expected) > 1e-9:
            mismatches.append("seed {} {}: {} vs {}".format(
                seed, channel, produced[channel], expected))

checks.equal(mismatches, [],
             "normalize matches R = (S - D) / (W - D) channel by channel, "
             "over {} generated calibrations".format(len(SEEDS)))


# ======================================================================
checks.section("69. float edge cases that are still real numbers")

EDGES = (
    (5e-324, "the smallest subnormal"),
    (2.2250738585072014e-308, "the smallest normal double"),
    (1e-30, "very small but ordinary"),
    (1.7976931348623157e308, "the largest finite double"),
    (1e30, "very large but ordinary"),
    (-0.0, "negative zero"),
    (0.1 + 0.2, "a value that is not exactly 0.3"),
)

for value, label in EDGES:
    a = {channel: value for channel in CHANNELS}

    result = metrics.cosine(metrics.paired(a, a))

    # A vector of identical finite values has cosine 1 with itself,
    # unless the squares underflow or overflow - both of which are
    # honest Nones rather than wrong numbers.
    checks.ok(result is None or abs(result - 1.0) < 1e-9,
              "a spectrum of {} gives either 1.0 or an honest None "
              "({})".format(label, result))

    checks.ok(result is None or not math.isnan(result),
              "  and never NaN")

# Negative zero must behave as zero, not as a distinct value.
zeros = {channel: 0.0 for channel in CHANNELS}
negative_zeros = {channel: -0.0 for channel in CHANNELS}

checks.equal(metrics.cosine(metrics.paired(zeros, negative_zeros)), None,
             "a zero-magnitude spectrum has no direction, so the cosine "
             "is None rather than a number")

checks.equal(metrics.rmse(metrics.paired(zeros, negative_zeros)), 0.0,
             "and -0.0 differs from 0.0 by exactly nothing")

# OVERFLOW. Squaring overflows above about 1.3e154, and Python's `**`
# RAISES rather than returning infinity. Before this was guarded,
# `rmse` and `euclidean` raised OverflowError out of the metric and
# `mae`, which only adds, returned a bare `inf` - a number that ranks,
# formats, and reaches a metric table looking like a measurement.
#
# Values this large cannot come from the sensor; they come from a
# corrupted frame. The rule is the module's own: a value that is not a
# real number is None.
huge = {channel: 1e308 for channel in CHANNELS}

for name, function in (("rmse", metrics.rmse),
                       ("mae", metrics.mae),
                       ("euclidean", metrics.euclidean),
                       ("cosine", metrics.cosine)):
    try:
        result = function(metrics.paired(huge, zeros))
        raised = None

    except Exception as error:                             # noqa: BLE001
        result, raised = None, type(error).__name__

    checks.ok(raised is None,
              "{} does not raise on a spectrum of 1e308 ({})".format(
                  name, raised or "returned cleanly"))

    checks.ok(result is None or math.isfinite(result),
              "  and returns None or a finite number, never inf ({})"
              .format(result))

# The whole metric table, which is what a comparison actually calls.
try:
    table = metrics.all_metrics(huge, zeros)
    raised = None

except Exception as error:                                 # noqa: BLE001
    table, raised = None, type(error).__name__

checks.ok(raised is None,
          "all_metrics survives a corrupted spectrum ({})".format(
              raised or "returned cleanly"))

if table:
    non_finite = {
        key: value for key, value in table.items()
        if isinstance(value, float) and not math.isfinite(value)
    }

    checks.equal(non_finite, {},
                 "and not one metric in the table is inf or NaN - every "
                 "undefined value is None")


# ======================================================================
checks.section("70. a float that goes to JSON and comes back")

# Spectra travel as JSON twice: over the wire, and into the archive. A
# rounding at either boundary is a scientific loss that no later stage
# can detect.

worst = 0.0
failures = []

for seed in SEEDS:
    rng = random.Random(seed + 9000)
    original = spectrum(rng, 1e-6, 1e6)

    restored = json.loads(json.dumps(original))

    for channel in CHANNELS:
        if restored[channel] != original[channel]:
            failures.append("seed {} {}: {!r} != {!r}".format(
                seed, channel, restored[channel], original[channel]))

        worst = max(worst,
                    abs(restored[channel] - original[channel]))

checks.equal(failures, [],
             "every generated spectral value survives JSON exactly - "
             "{} values, worst difference {}".format(
                 len(SEEDS) * len(CHANNELS), worst))

# The specific values that break naive serializers.
for value in (0.1, 1/3, 1e-300, 1e300, 5e-324, math.pi,
              1234567.8901234567, -0.0):
    restored = json.loads(json.dumps({"v": value}))["v"]

    checks.ok(restored == value,
              "{!r} survives JSON round-trip exactly".format(value))

# And through the metric, so the comparison a screen shows is computed
# on the same numbers that were stored.
rng = random.Random(12345)
measured = spectrum(rng)
reference = spectrum(rng)

direct = metrics.cosine(metrics.paired(measured, reference))
through_json = metrics.cosine(metrics.paired(
    json.loads(json.dumps(measured)),
    json.loads(json.dumps(reference)),
))

checks.equal(direct, through_json,
             "and a cosine computed before and after a JSON round trip "
             "is bit-identical")


# ======================================================================
checks.section("66. structurally invalid spectra are refused, not averaged")

# Each of these is a spectrum that is WRONG in a way that arithmetic
# would happily consume. The rule: reject, or mark unavailable. Never a
# plausible number.

rng = random.Random(999)
good = spectrum(rng)

MALFORMED = []

missing = dict(good)
del missing[CHANNELS[0]]
MALFORMED.append((missing, "one channel missing"))

nan_channel = dict(good)
nan_channel[CHANNELS[3]] = float("nan")
MALFORMED.append((nan_channel, "one channel NaN"))

inf_channel = dict(good)
inf_channel[CHANNELS[4]] = float("inf")
MALFORMED.append((inf_channel, "one channel infinite"))

neg_inf = dict(good)
neg_inf[CHANNELS[5]] = float("-inf")
MALFORMED.append((neg_inf, "one channel negative infinity"))

string_channel = dict(good)
string_channel[CHANNELS[6]] = "0.5"
MALFORMED.append((string_channel, "one channel a string"))

none_channel = dict(good)
none_channel[CHANNELS[7]] = None
MALFORMED.append((none_channel, "one channel null"))

list_channel = dict(good)
list_channel[CHANNELS[8]] = [0.5]
MALFORMED.append((list_channel, "one channel a list"))

MALFORMED.append(({}, "an empty spectrum"))
MALFORMED.append(({CHANNELS[0]: 0.5}, "a single channel"))

for bad, label in MALFORMED:
    try:
        pairs = metrics.paired(bad, good)
        value = metrics.cosine(pairs)
        crashed = None

    except Exception as error:                             # noqa: BLE001
        pairs, value, crashed = None, None, type(error).__name__

    # Either it refused, or it computed over the VALID channels only -
    # and in the second case the count must show the loss.
    if crashed is None and value is not None:
        checks.ok(len(pairs) < len(CHANNELS),
                  "{}: compared over {} of {} channels, so the loss is "
                  "visible in the pair count".format(
                      label, len(pairs), len(CHANNELS)))

    else:
        checks.ok(True,
                  "{}: refused ({})".format(
                      label, crashed or "no value produced"))

    # THE NEGATIVE THAT MATTERS: no invalid channel contributed a
    # number to the result.
    if pairs:
        finite = all(
            isinstance(left, (int, float)) and math.isfinite(left)
            and isinstance(right, (int, float)) and math.isfinite(right)
            for _channel, left, right in pairs
        )

        checks.ok(finite,
                  "  and every pair that was used is finite - nothing "
                  "invalid reached the arithmetic")

# AN UNKNOWN CHANNEL IS A SEPARATE CASE, and it belongs here rather
# than in the list above because the right answer is different: it is
# not a loss to be made visible, it is a field that cannot contribute
# at all. `paired()` reads by NAME from the fixed eighteen, so a
# nineteenth is not compared, not averaged and not counted.
extra = dict(good)
extra["ZZ"] = 0.5
extra["W_extra"] = 1e9

with_extra = metrics.paired(extra, good)

checks.equal(len(with_extra), len(CHANNELS),
             "an unknown channel does not reduce the comparison - all "
             "18 real channels are still compared")

checks.equal(metrics.cosine(with_extra),
             metrics.cosine(metrics.paired(good, good)),
             "and it changes the result by exactly nothing - a field "
             "the instrument does not have cannot influence a verdict")

checks.ok(all(channel in CHANNELS for channel, _l, _r in with_extra),
          "and every compared channel is one of the known 18")

# A duplicated channel cannot happen in a dict, which is worth stating:
# the structure itself makes that class of malformation impossible.
checks.equal(len({"A": 1, "A": 2}), 1,
             "a duplicated channel cannot exist in a spectrum - the "
             "dictionary makes it structurally impossible")

# Channel ORDER must not change any result: a dict is unordered as far
# as the science is concerned.
rng = random.Random(4242)
a, b = spectrum(rng), spectrum(rng)

shuffled_keys = list(CHANNELS)
random.Random(7).shuffle(shuffled_keys)

reordered = {key: a[key] for key in shuffled_keys}

checks.equal(metrics.cosine(metrics.paired(a, b)),
             metrics.cosine(metrics.paired(reordered, b)),
             "and reordering the channels changes nothing - the science "
             "reads by name, never by position")


# ======================================================================
checks.section("66. normalize refuses what it cannot define")

rng = random.Random(555)
dark = spectrum(rng, 0.0, 0.1)

# W == D: the denominator is zero, so the reflectance does not exist.
white_equals_dark = dict(dark)
sample = spectrum(rng)

result = preprocessing.normalize(sample, dark, white_equals_dark)

checks.ok(all(value is None for value in result.values()),
          "where white equals dark, EVERY channel is None - not 0.0, "
          "which would be a measurement claiming no reflectance")

# One channel non-finite in each of the three inputs.
for label, corrupt in (
    ("the sample", "sample"),
    ("the dark reference", "dark"),
    ("the white reference", "white"),
):
    for value, kind in ((float("nan"), "NaN"),
                        (float("inf"), "infinity")):
        good_dark = spectrum(rng, 0.0, 0.1)
        good_white = {channel: good_dark[channel] + 0.7
                      for channel in CHANNELS}
        good_sample = {channel: good_dark[channel] + 0.3
                       for channel in CHANNELS}

        inputs = {"sample": good_sample, "dark": good_dark,
                  "white": good_white}
        inputs[corrupt] = dict(inputs[corrupt])
        inputs[corrupt][CHANNELS[2]] = value

        result = preprocessing.normalize(
            inputs["sample"], inputs["dark"], inputs["white"])

        checks.ok(result[CHANNELS[2]] is None,
                  "{} in {} makes that channel None, not a "
                  "number".format(kind, label))

        others = [result[channel] for channel in CHANNELS
                  if channel != CHANNELS[2]]

        checks.ok(all(value is not None for value in others),
                  "  and the other 17 channels are unaffected - one bad "
                  "channel does not discard a spectrum")


sys.exit(checks.report())
