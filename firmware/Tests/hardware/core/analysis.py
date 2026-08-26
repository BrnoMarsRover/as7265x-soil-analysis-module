"""
The numbers that turn "it moved" into a measurement.

Two rules, both learned from `hardware_validation.py`, and both kept:

PERCENTILES ARE NEAREST-RANK, so the value REALLY OCCURRED. Interpolating
invents a latency nobody measured, and a p95 that no movement ever took
is a poor thing to put in a qualification report.

A FAILURE RATE IS REPORTED WITH THE FIRST FAILING ITERATION. "998 of
1000" is a number; "998 of 1000, first failure at iteration 3" and "998
of 1000, first failure at iteration 987" describe two completely
different faults - one intermittent from the start, one that appears
only after the hardware warms up.
"""

import math

# NOT NAMED `statistics.py`, deliberately. This module was called that
# for exactly as long as it took `Tests/software/static/test_static_api.py`
# to notice: a module of that name inside a package makes `import
# statistics` ambiguous - to the repository-wide static check, and to a
# reader - because the most specific match is the local file rather than
# the standard library. The check exists to catch precisely that kind of
# wrong-module resolution, and it caught it here.
import statistics as _stdlib


def summarize(values):
    """
    n / mean / median / sd / min / max / range / p95 / p99 / worst.

    `None` entries are dropped rather than counted as zero: a movement
    whose position could not be read did not close with zero error, and
    averaging it in would improve the result by losing the evidence.
    Returns None for an empty series, which callers must handle - an
    empty distribution is not a distribution of zeros.
    """
    numbers = [float(v) for v in values if v is not None]

    if not numbers:
        return None

    ordered = sorted(numbers)

    result = {
        "n": len(numbers),
        "dropped": len(list(values)) - len(numbers),
        "mean": round(_stdlib.fmean(numbers), 6),
        "median": round(_stdlib.median(ordered), 6),
        "min": round(ordered[0], 6),
        "max": round(ordered[-1], 6),
        "range": round(ordered[-1] - ordered[0], 6),
        "sd": (round(_stdlib.stdev(numbers), 6)
               if len(numbers) > 1 else 0.0),
        "p95": percentile(ordered, 0.95),
        "p99": percentile(ordered, 0.99),
        "worst_abs": round(max(abs(v) for v in numbers), 6),
    }

    if result["mean"]:
        result["cv_pct"] = round(
            100.0 * result["sd"] / abs(result["mean"]), 4)

    return result


def percentile(ordered, fraction):
    """
    Nearest-rank percentile of an ALREADY SORTED series.

    Nearest-rank, not interpolated: every value this returns is a value
    that was actually observed.
    """
    if not ordered:
        return None

    rank = max(1, int(math.ceil(fraction * len(ordered))))

    return round(float(ordered[rank - 1]), 6)


def failure_rate(outcomes):
    """
    Turn a sequence of per-iteration booleans into a verdict.

    `passed` is only true when EVERY iteration passed. One unexplained
    failure in a hundred is not a pass; it is an intermittent fault with
    a hundred-iteration sample, and the difference is the whole reason
    an endurance campaign exists.
    """
    outcomes = [bool(o) for o in outcomes]
    failures = [i + 1 for i, ok in enumerate(outcomes) if not ok]

    return {
        "iterations": len(outcomes),
        "passed": len(outcomes) - len(failures),
        "failed": len(failures),
        "failure_rate_pct": (
            round(100.0 * len(failures) / len(outcomes), 4)
            if outcomes else None
        ),
        "first_failure_iteration": failures[0] if failures else None,
        "failing_iterations": failures[:50],
        "all_passed": bool(outcomes) and not failures,
    }


def outliers(values, sigma=3.0):
    """
    Indices further than `sigma` standard deviations from the mean.

    A diagnostic aid, not a filter. Nothing in this framework ever drops
    an outlier from a result: an acquisition that took ten times as long
    as the others is the interesting one.
    """
    numbers = [v for v in values if v is not None]

    if len(numbers) < 3:
        return []

    mean = _stdlib.fmean(numbers)
    sd = _stdlib.stdev(numbers)

    if sd == 0:
        return []

    return [
        {"index": index, "value": value,
         "sigma": round((value - mean) / sd, 3)}
        for index, value in enumerate(values)
        if value is not None and abs(value - mean) > sigma * sd
    ]


def counts_to_degrees(counts, counts_per_rev):
    """Encoder counts as an angle. One place, so H-002 has one formula."""
    if counts is None or not counts_per_rev:
        return None

    return round(360.0 * float(counts) / float(counts_per_rev), 4)


def degrees_to_counts(degrees, counts_per_rev):
    """The inverse, rounded the way the firmware rounds it."""
    if degrees is None or not counts_per_rev:
        return None

    return int(round(float(degrees) * float(counts_per_rev) / 360.0))


def centred_error(delta, counts_per_rev):
    """
    A circular difference expressed in (-half, +half].

    The same arithmetic the firmware uses, reimplemented here on
    purpose: this is the one place where the test side must be able to
    check the firmware's own answer rather than trust it. Software has
    already verified the firmware's version at all 4096 positions; what
    H-002 asks is whether the NUMBERS GOING IN are what the mechanism
    did, and that needs an independent calculation.
    """
    if delta is None or not counts_per_rev:
        return None

    half = counts_per_rev // 2
    wrapped = int(delta) % int(counts_per_rev)

    if wrapped > half:
        wrapped -= int(counts_per_rev)

    return wrapped


def byte_order_interpretations(raw_value):
    """
    The same 16-bit register read four plausible ways.

    Purely diagnostic, and only for H-002. If the encoder reports 2
    counts where 2048 were commanded, one hypothesis on the list is that
    the two bytes of the position register are being assembled in the
    wrong order somewhere; showing the alternatives beside the value
    lets an operator recognise 2048 (0x0800) hiding inside 8 (0x0008)
    without guessing at hex in their head.

    This proves nothing on its own and is never used to decide a
    verdict. It is a hint printed next to the raw number.
    """
    if raw_value is None:
        return None

    value = int(raw_value) & 0xFFFF
    low = value & 0xFF
    high = (value >> 8) & 0xFF

    return {
        "as_read": value,
        "hex": "0x{:04X}".format(value),
        "byte_swapped": ((low << 8) | high),
        "low_byte_only": low,
        "high_byte_only": high,
        "note": "diagnostic only - none of these is a measurement",
    }
