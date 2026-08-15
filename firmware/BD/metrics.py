"""
Spectral comparison metrics and rank aggregation.

Why not cosine alone
--------------------
Cosine similarity measures the ANGLE between two vectors and is blind to
their length. Reflectance is non-negative, so every material in the
library sits in the same corner of the space and almost any pair scores
95-100%. Worse, a spectrum at 0.2 everywhere and one at 1.0 everywhere
are *identical* to cosine - a perfect 100% match between a dark sample
and a bright one.

So three metrics are computed for every material:

    cosine    shape, ignores brightness      higher is better
    RMSE      absolute agreement in          lower is better
              reflectance units
    Pearson   correlation of the variation   higher is better
              about each mean

They answer different questions and they disagree in informative ways.
The three RANKS are combined - ranks, not scores, because the metrics
are on incomparable scales and a weighted blend of them would be an
invented number dressed up as a confidence.

Nothing here converts a metric into a probability, a certainty or a
composition.
"""

import math

import config
from channels import CHANNELS


def _pairs(measured, reference, channels=None):
    """Finite (a, b) values present in both spectra."""
    channels = channels or CHANNELS
    pairs = []

    for channel in channels:
        a = (measured or {}).get(channel)
        b = (reference or {}).get(channel)

        for value in (a, b):
            if isinstance(value, bool) or not isinstance(
                value, (int, float)
            ):
                break

            if math.isnan(value) or math.isinf(value):
                break

        else:
            pairs.append((float(a), float(b)))

    return pairs


def cosine_similarity_percent(measured, reference, channels=None):
    """
    Angle between the two spectra, as a percentage.

    Shape only. Not a probability, not a concentration, not a
    confidence.
    """
    pairs = _pairs(measured, reference, channels)

    if not pairs:
        return None

    dot = sum(a * b for a, b in pairs)
    norm_a = math.sqrt(sum(a * a for a, _b in pairs))
    norm_b = math.sqrt(sum(b * b for _a, b in pairs))

    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0

    return max(0.0, min(100.0, (dot / (norm_a * norm_b)) * 100.0))


def rmse(measured, reference, channels=None):
    """
    Root mean squared error in reflectance units. Lower is better.

    This is the metric cosine cannot provide: it keeps the magnitude, so
    two spectra of the same shape at different brightness are correctly
    reported as different. Deliberately NOT rescaled into a percentage -
    it is already in the units of the thing being compared.
    """
    pairs = _pairs(measured, reference, channels)

    if not pairs:
        return None

    total = sum((a - b) ** 2 for a, b in pairs)

    return math.sqrt(total / float(len(pairs)))


def pearson_r(measured, reference, channels=None):
    """
    Correlation of the variation about each spectrum's own mean.

    Range -1 to +1. Undefined - and returned as None - when either
    spectrum is constant, because a flat vector has no variation to
    correlate. That is a real mathematical answer, not a failure.
    """
    pairs = _pairs(measured, reference, channels)

    if len(pairs) < 3:
        return None

    n = float(len(pairs))
    mean_a = sum(a for a, _b in pairs) / n
    mean_b = sum(b for _a, b in pairs) / n

    covariance = sum((a - mean_a) * (b - mean_b) for a, b in pairs)
    variance_a = sum((a - mean_a) ** 2 for a, _b in pairs)
    variance_b = sum((b - mean_b) ** 2 for _a, b in pairs)

    if variance_a <= 0.0 or variance_b <= 0.0:
        return None

    return covariance / math.sqrt(variance_a * variance_b)


# ----------------------------------------------------------------------
# ranking
# ----------------------------------------------------------------------

def _rank(entries, key, higher_is_better):
    """
    Competition ranking (1, 2, 2, 4) over one metric.

    Entries whose metric is undefined are ranked last, together, rather
    than being dropped - a material that could not be compared is still
    part of the library.
    """
    scored = [entry for entry in entries if entry[key] is not None]
    unscored = [entry for entry in entries if entry[key] is None]

    scored.sort(key=lambda entry: entry[key], reverse=higher_is_better)

    rank_key = "{}_rank".format(key)
    position = 0
    previous = None

    for index, entry in enumerate(scored):
        if entry[key] != previous:
            position = index + 1
            previous = entry[key]

        entry[rank_key] = position

    for entry in unscored:
        entry[rank_key] = len(scored) + 1

    return entries


def compare_all(normalized, materials, channels=None):
    """
    Every material, scored on all three metrics and ranked.

    `materials` maps name -> reference spectrum. Returns the full list
    ordered by combined rank, best first; every individual metric and
    every individual rank is preserved.
    """
    entries = []

    for name, reference in (materials or {}).items():
        entries.append({
            "material": name,
            "cosine": cosine_similarity_percent(
                normalized, reference, channels
            ),
            "rmse": rmse(normalized, reference, channels),
            "pearson": pearson_r(normalized, reference, channels),
        })

    if not entries:
        return []

    _rank(entries, "cosine", higher_is_better=True)
    _rank(entries, "rmse", higher_is_better=False)
    _rank(entries, "pearson", higher_is_better=True)

    weights = config.METRIC_WEIGHTS
    total_weight = sum(weights.values()) or 1.0

    for entry in entries:
        entry["rank_score"] = round(
            (
                entry["cosine_rank"] * weights["cosine"]
                + entry["rmse_rank"] * weights["rmse"]
                + entry["pearson_rank"] * weights["pearson"]
            ) / total_weight,
            3,
        )

    entries.sort(key=lambda entry: (entry["rank_score"], entry["material"]))

    for position, entry in enumerate(entries, start=1):
        entry["combined_rank"] = position

        # Presentation-friendly rounding, applied last so the ordering
        # above used the full precision.
        entry["cosine_similarity_percent"] = (
            round(entry["cosine"], 2) if entry["cosine"] is not None else None
        )
        entry["rmse"] = (
            round(entry["rmse"], 5) if entry["rmse"] is not None else None
        )
        entry["pearson_r"] = (
            round(entry["pearson"], 4)
            if entry["pearson"] is not None else None
        )

        del entry["cosine"]
        del entry["pearson"]

        # The historical field name, kept so older readers and the
        # compact table keep working.
        entry["rank"] = position
        entry["similarity_percent"] = entry["cosine_similarity_percent"]

    return entries


def metric_agreement(entries):
    """
    Whether the three metrics point at the same material.

    Disagreement is reported, never hidden: it usually means the shape
    and the magnitude are telling different stories, which is exactly
    what a single-metric result would have concealed.
    """
    if not entries:
        return {"agree": None, "reason": "no candidates"}

    best = entries[0]

    winners = {}

    for key, rank_key in (
        ("cosine", "cosine_rank"),
        ("rmse", "rmse_rank"),
        ("pearson", "pearson_rank"),
    ):
        leader = min(entries, key=lambda entry: entry[rank_key])
        winners[key] = leader["material"]

    tolerance = config.METRIC_AGREEMENT_RANK_TOLERANCE

    # Two conditions, because either alone is too easy to satisfy.
    # Tolerance alone passes trivially on a tiny library where every
    # rank is small; a majority alone ignores how badly the dissenting
    # metric ranks the winner.
    within_tolerance = all(
        best[rank_key] <= tolerance
        for rank_key in ("cosine_rank", "rmse_rank", "pearson_rank")
    )

    majority = sum(
        1 for winner in winners.values() if winner == best["material"]
    )

    agree = within_tolerance and majority >= 2

    return {
        "agree": agree,
        "within_tolerance": within_tolerance,
        "metrics_favouring_best": majority,
        "combined_best": best["material"],
        "cosine_best": winners["cosine"],
        "rmse_best": winners["rmse"],
        "pearson_best": winners["pearson"],
        "best_ranks": {
            "cosine": best["cosine_rank"],
            "rmse": best["rmse_rank"],
            "pearson": best["pearson_rank"],
        },
        "tolerance": tolerance,
    }
