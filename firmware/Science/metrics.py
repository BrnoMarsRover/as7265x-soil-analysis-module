"""
Distance to individual references — six metrics, and the MARGINS.

WHY THIS REPLACES RANK AGGREGATION

The old comparison reported a rank triple: cosine 1st, RMSE 4th, Pearson
2nd. A rank throws away the only thing that distinguishes a decisive win
from a coin toss. From the operator's own session:

    Activated Carbon   RMSE 0.0088   runner-up 0.1705   19x separation
    Kaolin             cosine 98.75  runner-up 98.74    0.01 points

Both arrive at a rank-based decision layer as "1", and the first is
overwhelming evidence while the second is none at all. §25.

So every metric here reports, for one measurement against one library:

    scores          every candidate
    winner          and the runner-up
    absolute_margin winner minus runner-up, in the metric's own units
    relative_margin the same, scaled by the spread of all candidates
    z_separation    how many robust deviations clear of the field
    spread          median, MAD and range across candidates

`relative_margin` and `z_separation` are the useful ones, because they
answer "is this winner unusual?" rather than "did it come first?" - in a
library where every candidate scores 99% cosine, coming first means
nothing and the margin says so.

ONE IMPLEMENTATION OF EACH METRIC

Two metric modules used to exist side by side: one taking whole spectra
and one taking pre-paired channel values, each with its own cosine, its
own Pearson and its own idea of what an undefined result was. They
disagreed on the edges - a zero-norm spectrum scored 0.0 similarity in
one and None in the other, which is the difference between "definitely
not this material" and "cannot say". They are one module now, and the
honest answer won: an undefined metric is None.

Everything is computed from `paired()`, which is also what makes
per-channel weights possible at all.

METRIC FAMILIES

    magnitude    RMSE, MAE, Euclidean, weighted Euclidean   sees brightness
    angular      cosine, spectral angle                     shape only
    centered     Pearson r                                  shape only

Pearson is the cosine of mean-centered vectors, so it is a second shape
metric, not a third opinion. They are labelled by family here and the
decision layer never gives two members of one family two votes.
"""

import math

from BD.channels import CHANNELS
from Science import config

# Metric family, and whether a bigger score is better.
METRICS = {
    "rmse": {"family": "magnitude", "higher_is_better": False},
    "mae": {"family": "magnitude", "higher_is_better": False},
    "euclidean": {"family": "magnitude", "higher_is_better": False},
    "weighted_euclidean": {"family": "magnitude", "higher_is_better": False},
    "cosine": {"family": "angular", "higher_is_better": True},
    "spectral_angle_deg": {"family": "angular", "higher_is_better": False},
    "pearson_r": {"family": "centered_shape", "higher_is_better": True},
}

FAMILIES = ("magnitude", "angular", "centered_shape")


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False

    return not (math.isnan(value) or math.isinf(value))


def paired(measured, reference, channels=None):
    """Channels where both sides have a finite value, in fixed order."""
    channels = channels or CHANNELS

    pairs = []

    for channel in channels:
        left = measured.get(channel)
        right = reference.get(channel)

        if _finite(left) and _finite(right):
            pairs.append((channel, float(left), float(right)))

    return pairs


def _defined(compute):
    """
    A distance, or None if it does not exist as a real number.

    `paired()` already drops a channel whose INPUT is not finite. This
    is the other half: finite inputs can still produce a result that is
    not.

    Squaring overflows above about 1.3e154, and Python's `**` raises
    OverflowError rather than returning infinity - so `rmse` and
    `euclidean` raised out of the metric, while `mae`, which only adds,
    returned a bare `inf`. Both are wrong in the same way and neither
    is this module's vocabulary: everywhere else, a value that is not a
    real number is None, and `_finite` exists to say so.

    An `inf` is the worse of the two. It is a NUMBER, so it ranks, it
    formats, and it reaches a metric table on an operator's screen as
    though it were a measurement.

    Values this large do not come from the sensor - a calibrated
    AS7265x channel is five figures - so this guards corrupted data
    rather than physics. It cannot change any result in range: a
    finite answer is returned exactly as computed.
    """
    try:
        value = compute()

    except (OverflowError, ValueError, ZeroDivisionError):
        return None

    return value if _finite(value) else None


def rmse(pairs):
    if not pairs:
        return None

    return _defined(lambda: math.sqrt(
        sum((left - right) ** 2 for _c, left, right in pairs) / len(pairs)
    ))


def mae(pairs):
    if not pairs:
        return None

    return _defined(lambda: sum(
        abs(left - right) for _c, left, right in pairs) / len(pairs))


def euclidean(pairs):
    if not pairs:
        return None

    return _defined(lambda: math.sqrt(
        sum((left - right) ** 2 for _c, left, right in pairs)))


def weighted_euclidean(pairs, weights):
    """
    Euclidean distance with per-channel weights.

    The weights come from measured channel reliability, so a channel
    whose reflectance is quantization noise contributes in proportion to
    how much it is worth - rather than being either fully counted or
    fully deleted.
    """
    if not pairs:
        return None

    # THE WHOLE COMPUTATION INSIDE THE GUARD, not just the return: the
    # squares are accumulated in the loop, so that is where an overflow
    # happens and where it has to be caught.
    def compute():
        total = 0.0
        total_weight = 0.0

        for channel, left, right in pairs:
            weight = float((weights or {}).get(channel, 1.0))

            if weight <= 0:
                continue

            total += weight * (left - right) ** 2
            total_weight += weight

        if total_weight <= 0:
            return None

        return math.sqrt(total / total_weight)

    return _defined(compute)


def cosine(pairs):
    if not pairs:
        return None

    def compute():
        dot = sum(left * right for _c, left, right in pairs)
        left_norm = math.sqrt(sum(left * left for _c, left, _r in pairs))
        right_norm = math.sqrt(sum(right * right for _c, _l, right in pairs))

        if left_norm <= 0 or right_norm <= 0:
            return None

        return max(-1.0, min(1.0, dot / (left_norm * right_norm)))

    return _defined(compute)


def spectral_angle_degrees(pairs):
    """arccos(cosine). Rank-identical to cosine, reported in degrees."""
    value = cosine(pairs)

    if value is None:
        return None

    return math.degrees(math.acos(max(-1.0, min(1.0, value))))


def pearson_r(pairs):
    """
    Correlation about each spectrum's own mean. Range -1 to +1.

    Undefined - returned as None - below three channels or when either
    spectrum is constant. Two points always correlate at exactly +/-1
    whatever they are, and a flat vector has no variation to correlate;
    both are real mathematical answers, not failures.
    """
    if len(pairs) < 3:
        return None

    def compute():
        n = float(len(pairs))
        left_mean = sum(left for _c, left, _r in pairs) / n
        right_mean = sum(right for _c, _l, right in pairs) / n

        covariance = sum(
            (left - left_mean) * (right - right_mean)
            for _c, left, right in pairs
        )
        left_variance = sum(
            (left - left_mean) ** 2 for _c, left, _r in pairs)
        right_variance = sum(
            (right - right_mean) ** 2 for _c, _l, right in pairs)

        if left_variance <= 0 or right_variance <= 0:
            return None

        return covariance / math.sqrt(left_variance * right_variance)

    return _defined(compute)


def all_metrics(measured, reference, channels=None, weights=None):
    """Every metric for one measurement against one reference."""
    pairs = paired(measured, reference, channels)

    return {
        "channels_compared": len(pairs),
        "rmse": rmse(pairs),
        "mae": mae(pairs),
        "euclidean": euclidean(pairs),
        "weighted_euclidean": weighted_euclidean(pairs, weights),
        "cosine": cosine(pairs),
        "spectral_angle_deg": spectral_angle_degrees(pairs),
        "pearson_r": pearson_r(pairs),
    }


def _median(values):
    if not values:
        return None

    ordered = sorted(values)
    middle = len(ordered) // 2

    if len(ordered) % 2:
        return ordered[middle]

    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _mad(values, centre=None):
    """Median absolute deviation - a spread that one outlier cannot move."""
    if not values:
        return None

    centre = _median(values) if centre is None else centre

    return _median([abs(value - centre) for value in values])


def absolute_goodness(metric, winner_score, measured_scale):
    """
    How good the winner is in its own right, independent of the field.

    Margin answers "did it beat the others"; this answers "is it actually
    a good fit". Both are needed: a winner that beats a field of terrible
    candidates is still a terrible candidate, and a rank cannot tell you
    which situation you are in. §25.

    Error metrics are scaled by the magnitude of the measurement itself,
    because an RMSE of 0.01 means something quite different against a
    reflectance of 0.9 than against one of 0.02. Similarity metrics are
    already bounded and are reported as they stand - including the fact
    that cosine sits near 1.0 for almost everything, which is exactly why
    it is weak evidence on its own.
    """
    if winner_score is None:
        return None

    definition = METRICS.get(metric) or {}

    if definition.get("higher_is_better"):
        return round(max(0.0, min(1.0, winner_score)), 6)

    if metric == "spectral_angle_deg":
        # 0 deg is identical, 90 deg is orthogonal.
        return round(max(0.0, 1.0 - (winner_score / 90.0)), 6)

    if not measured_scale or measured_scale <= 0:
        return None

    return round(max(0.0, 1.0 - (winner_score / measured_scale)), 6)


def _scale_of(measured, channels=None):
    """RMS of the measurement - the natural scale for an error metric."""
    values = [
        float(measured[channel])
        for channel in (channels or CHANNELS)
        if _finite(measured.get(channel))
    ]

    if not values:
        return None

    return math.sqrt(sum(value * value for value in values) / len(values))


def separation(scores, higher_is_better):
    """
    Turn one metric's scores over all candidates into an evidence summary.

    This is the function that makes a rank unnecessary. `z_separation`
    uses the median and MAD rather than mean and standard deviation,
    because the winner is by definition an outlier and must not be
    allowed to inflate the spread it is being judged against.
    """
    usable = [
        (name, value) for name, value in scores.items() if _finite(value)
    ]

    if not usable:
        return {
            "winner": None, "runner_up": None, "winner_score": None,
            "runner_up_score": None, "absolute_margin": None,
            "relative_margin": None, "z_separation": None,
            "candidates": 0, "median": None, "mad": None,
        }

    ordered = sorted(
        usable, key=lambda item: item[1], reverse=bool(higher_is_better)
    )

    winner, winner_score = ordered[0]
    runner_up, runner_up_score = (
        ordered[1] if len(ordered) > 1 else (None, None)
    )

    values = [value for _name, value in usable]
    median = _median(values)
    mad = _mad(values, median)
    span = max(values) - min(values)

    absolute_margin = None
    relative_margin = None
    z_separation = None

    if runner_up_score is not None:
        absolute_margin = abs(winner_score - runner_up_score)

        if span > 0:
            relative_margin = absolute_margin / span

        if mad and mad > 0:
            z_separation = abs(winner_score - median) / mad

    return {
        "winner": winner,
        "winner_score": winner_score,
        "runner_up": runner_up,
        "runner_up_score": runner_up_score,
        "absolute_margin": absolute_margin,
        "relative_margin": relative_margin,
        "z_separation": z_separation,
        "candidates": len(usable),
        "median": median,
        "mad": mad,
        "range": span,
    }


def compare_library(measured, materials, channels=None, weights=None,
                    top=8):
    """
    One measurement against a whole library, every metric kept apart.

    Returns per-metric evidence plus a per-candidate table. NOTHING here
    combines the metrics: that is a decision, and decisions belong to
    Science.decision. §10.
    """
    per_material = {}

    for name, reference in materials.items():
        per_material[name] = all_metrics(
            measured, reference, channels, weights
        )

    evidence = {}
    scale = _scale_of(measured, channels)

    for metric, definition in METRICS.items():
        scores = {
            name: values.get(metric) for name, values in per_material.items()
        }

        summary = separation(scores, definition["higher_is_better"])
        summary["family"] = definition["family"]
        summary["higher_is_better"] = definition["higher_is_better"]
        summary["absolute_goodness"] = absolute_goodness(
            metric, summary.get("winner_score"), scale
        )
        summary["top"] = _top_candidates(
            scores, definition["higher_is_better"], top
        )

        evidence[metric] = summary

    return {
        "metrics": evidence,
        "families": _family_view(evidence),
        "measurement_scale": scale,
        "candidate_count": len(per_material),
        "channels_compared": (
            max(
                (values["channels_compared"]
                 for values in per_material.values()),
                default=0,
            )
        ),
        "per_material": per_material,
    }


def _top_candidates(scores, higher_is_better, limit):
    usable = [
        (name, value) for name, value in scores.items() if _finite(value)
    ]

    ordered = sorted(
        usable, key=lambda item: item[1], reverse=bool(higher_is_better)
    )

    return [
        {"material": name, "score": value}
        for name, value in ordered[:limit]
    ]


def _family_view(evidence):
    """
    One entry per family, so nobody can count two shape metrics twice.

    Within a family the member with the clearest separation is reported,
    because that is the family's best evidence - not its average, which
    would let a redundant member dilute it.
    """
    view = {}

    for family in FAMILIES:
        members = [
            (metric, summary) for metric, summary in evidence.items()
            if summary.get("family") == family
        ]

        if not members:
            continue

        ranked = sorted(
            members,
            key=lambda item: (
                item[1].get("z_separation") or 0.0,
                item[1].get("relative_margin") or 0.0,
            ),
            reverse=True,
        )

        metric, summary = ranked[0]

        view[family] = {
            "metric": metric,
            "winner": summary.get("winner"),
            "winner_score": summary.get("winner_score"),
            "absolute_goodness": summary.get("absolute_goodness"),
            "absolute_margin": summary.get("absolute_margin"),
            "relative_margin": summary.get("relative_margin"),
            "z_separation": summary.get("z_separation"),
            "members": [name for name, _summary in members],
        }

    return view


# ======================================================================
# ranked comparison
# ======================================================================
# compare_library() above keeps every metric apart and combines nothing,
# which is what the decision layer needs. This section answers the other
# question - "put the library in order for a person to read" - and it
# does combine, by RANK rather than by score.
#
# Ranks, not scores, because the metrics have incomparable units: an
# RMSE of 0.03 and a cosine of 99.2% cannot be averaged, but "first on
# magnitude, second on shape" can.

# Family -> (the result key of its ranking statistic, higher_is_better).
# Adding a metric to an existing family must NOT add a row here: that is
# precisely the double-counting this structure exists to prevent.
RANKING_FAMILIES = {
    "magnitude": ("rmse", False),
    "angular": ("cosine_similarity_percent", True),
    "centered_shape": ("pearson_r", True),
}


def percent(cosine_value):
    """
    Cosine similarity as a percentage.

    SHAPE ONLY. Not a probability, not a concentration, not a
    confidence, and never an abundance. 92% here means the angle
    between two reflectance vectors is small - nothing whatsoever
    about how much of a material is present.
    """
    if cosine_value is None:
        return None

    return max(0.0, min(100.0, cosine_value * 100.0))


def _rank(entries, key, higher_is_better, rank_key):
    """
    Competition ranking (1, 2, 2, 4) over one statistic.

    Entries whose statistic is undefined rank last, together, rather
    than being dropped: a material that could not be compared is still
    part of the library and must not silently vanish.
    """
    scored = [entry for entry in entries if entry.get(key) is not None]
    unscored = [entry for entry in entries if entry.get(key) is None]

    scored.sort(key=lambda entry: entry[key], reverse=higher_is_better)

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


def compare_all(measured, materials, channels=None, weights=None):
    """
    Every material, scored on every metric and ranked by family.

    `materials` maps name -> reference spectrum. Returns the full list
    ordered by combined family rank, best first. Deliberately not
    truncated to a top-N: the complete ranking is stored with the
    AnalysisRun so a result can be re-examined later, and the interface
    decides how many rows to show.
    """
    entries = []

    for name, reference in (materials or {}).items():
        values = all_metrics(measured, reference, channels, weights)

        entries.append({
            "material": name,
            "channels_compared": values["channels_compared"],
            "rmse": values["rmse"],
            "mae": values["mae"],
            "euclidean": values["euclidean"],
            "weighted_euclidean": values["weighted_euclidean"],
            "cosine_similarity_percent": percent(values["cosine"]),
            "spectral_angle_deg": values["spectral_angle_deg"],
            "pearson_r": values["pearson_r"],
        })

    if not entries:
        return []

    family_weights = config.FAMILY_WEIGHTS
    total_weight = sum(family_weights.values()) or 1.0

    for family, (key, higher_is_better) in RANKING_FAMILIES.items():
        _rank(entries, key, higher_is_better, "{}_rank".format(family))

    for entry in entries:
        entry["rank_score"] = round(
            sum(
                entry["{}_rank".format(family)]
                * family_weights.get(family, 0.0)
                for family in RANKING_FAMILIES
            ) / total_weight,
            3,
        )

    entries.sort(key=lambda entry: (entry["rank_score"], entry["material"]))

    for position, entry in enumerate(entries, start=1):
        entry["combined_rank"] = position

        # Presentation rounding, applied last so the ordering above used
        # full precision.
        for key, digits in (
            ("rmse", 5), ("mae", 5), ("euclidean", 5),
            ("weighted_euclidean", 5),
            ("cosine_similarity_percent", 2),
            ("spectral_angle_deg", 3),
            ("pearson_r", 4),
        ):
            if entry.get(key) is not None:
                entry[key] = round(entry[key], digits)

    return entries


def family_agreement(entries):
    """
    Whether the evidence families point at the same material.

    Disagreement is reported, never hidden: it usually means shape and
    magnitude are telling different stories, which is exactly what a
    single-metric result would have concealed.
    """
    if not entries:
        return {"agree": None, "reason": "no candidates"}

    best = entries[0]
    winners = {}

    for family in RANKING_FAMILIES:
        rank_key = "{}_rank".format(family)

        # min() returns the FIRST minimum, and `entries` arrives sorted
        # by combined rank - which would quietly bias every tie toward
        # the combined winner and inflate apparent agreement. Break ties
        # on material name so the answer cannot depend on list order.
        leader = min(
            entries, key=lambda entry: (entry[rank_key], entry["material"])
        )
        winners[family] = leader["material"]

    tolerance = config.FAMILY_AGREEMENT_RANK_TOLERANCE

    # Two conditions, because either alone is too easy to satisfy.
    # Tolerance alone passes trivially on a small library where every
    # rank is small; a majority alone ignores how badly the dissenting
    # family ranks the winner.
    within_tolerance = all(
        best["{}_rank".format(family)] <= tolerance
        for family in RANKING_FAMILIES
    )

    majority = sum(
        1 for winner in winners.values() if winner == best["material"]
    )

    return {
        "agree": within_tolerance and majority >= 2,
        "within_tolerance": within_tolerance,
        "families_favouring_best": majority,
        "family_count": len(RANKING_FAMILIES),
        "combined_best": best["material"],
        "family_best": dict(winners),
        "best_ranks": {
            family: best["{}_rank".format(family)]
            for family in RANKING_FAMILIES
        },
        "tolerance": tolerance,
        "weights_status": config.FAMILY_WEIGHTS_STATUS,
    }
