"""
Spectral comparison metrics, grouped into evidence families.

    magnitude       RMSE, MAE                sees brightness
    angular         cosine, spectral angle   blind to brightness
    centered_shape  Pearson r                blind to brightness + offset

WHY FAMILIES INSTEAD OF METRICS
-------------------------------
It is easy to implement five similarity metrics, average their opinions
and present the result as a five-way agreement. It would also be wrong,
because the metrics are not independent:

    SAM  = arccos(cosine)           -> rank-identical to cosine
    RMSE = Euclidean / sqrt(N)      -> rank-identical to Euclidean
    cosine(x, kx) = pearson(x, kx) = 1   -> both blind to brightness

So metrics are grouped into families, each family nominates ONE ranking
statistic, and only families vote. Every individual metric value is still
computed and preserved, so a result stays auditable — what changes is
that correlated metrics can no longer masquerade as corroboration.

Families are combined by RANK, not by blending scores: a cosine of 0.99,
an RMSE of 0.03 and an r of 0.87 are on incomparable scales, and a
weighted blend would be an invented confidence.

Measured on the real 23-material DB1: 45% of material pairs score cosine
>= 0.99. Never present a cosine as a probability or a composition.

See Documentation/ARCHITECTURE.md.
"""

import math

from BD.channels import CHANNELS
from Measurements import config


# ----------------------------------------------------------------------
# shared pairing
#
# One implementation, so every metric compares exactly the same channels
# of exactly the same two spectra. A metric that silently dropped a
# different channel set from its neighbour would make family disagreement
# meaningless.
# ----------------------------------------------------------------------

def finite_pairs(measured, reference, channels=None):
    """
    Finite (a, b) values present and usable in BOTH spectra.

    Booleans are rejected explicitly: True is an int in Python and would
    otherwise sail through as 1.0.
    """
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


# ----------------------------------------------------------------------
# MAGNITUDE family — absolute agreement in reflectance units
#
# The family that survives a brightness change. Every other family here
# is scale-invariant, so dropping this one makes the system blind to the
# difference between a dim sample and a bright one of the same shape.
#
# Euclidean distance is deliberately NOT implemented: on a fixed
# 18-channel vector RMSE = Euclidean / sqrt(18), a strictly increasing
# transform, so it produces an identical ranking. Tests assert this.
# ----------------------------------------------------------------------

def rmse(measured, reference, channels=None):
    """Root mean squared error in reflectance units. Lower is better."""
    pairs = finite_pairs(measured, reference, channels)

    if not pairs:
        return None

    return math.sqrt(
        sum((a - b) ** 2 for a, b in pairs) / float(len(pairs))
    )


def mae(measured, reference, channels=None):
    """
    Mean absolute error. Lower is better.

    Reported alongside RMSE, not as a second vote. A large gap between
    the two is itself informative: it means the disagreement is
    concentrated in a few channels rather than spread across all of them.
    """
    pairs = finite_pairs(measured, reference, channels)

    if not pairs:
        return None

    return sum(abs(a - b) for a, b in pairs) / float(len(pairs))


# ----------------------------------------------------------------------
# ANGULAR family — the angle between two spectra, ignoring their length
#
# SAM is a REPARAMETERIZATION of the cosine, reported because degrees
# read more naturally than a similarity percentage. It carries no ranking
# information the cosine does not already carry, so the family votes once.
# ----------------------------------------------------------------------

def cosine_similarity(measured, reference, channels=None):
    """Cosine of the angle between the two spectra."""
    pairs = finite_pairs(measured, reference, channels)

    if not pairs:
        return None

    dot = sum(a * b for a, b in pairs)
    norm_a = math.sqrt(sum(a * a for a, _b in pairs))
    norm_b = math.sqrt(sum(b * b for _a, b in pairs))

    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0

    return max(-1.0, min(1.0, dot / (norm_a * norm_b)))


def cosine_similarity_percent(measured, reference, channels=None):
    """
    Cosine similarity as a percentage.

    Shape only. Not a probability, not a concentration, not a confidence.
    """
    value = cosine_similarity(measured, reference, channels)

    if value is None:
        return None

    return max(0.0, min(100.0, value * 100.0))


def spectral_angle_degrees(measured, reference, channels=None):
    """Spectral Angle Mapper, in degrees. Lower is better."""
    value = cosine_similarity(measured, reference, channels)

    if value is None:
        return None

    return math.degrees(math.acos(max(-1.0, min(1.0, value))))


# ----------------------------------------------------------------------
# CENTERED SHAPE family — correlation of the variation about each mean
#
# Pearson is the cosine of the mean-centered vectors. Centering is a real
# transformation and changes the answer: on DB1, cosine and Pearson
# nominate a different nearest material for 12 of 22 probes.
#
# It does NOT recover magnitude. pearson(x, kx) = 1 exactly like cosine.
# ----------------------------------------------------------------------

def pearson_r(measured, reference, channels=None):
    """
    Correlation about each spectrum's own mean. Range -1 to +1.

    Undefined - returned as None - when either spectrum is constant,
    because a flat vector has no variation to correlate. That is a real
    mathematical answer, not a failure.
    """
    pairs = finite_pairs(measured, reference, channels)

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

    return covariance / (variance_a * variance_b) ** 0.5


# ----------------------------------------------------------------------
# ranking
# ----------------------------------------------------------------------

# Family -> (result key of its ranking statistic, higher_is_better).
# Adding a metric to an existing family must NOT add a row here: that is
# precisely the double-counting this structure exists to prevent.
FAMILIES = {
    "magnitude": ("rmse", False),
    "angular": ("cosine_similarity_percent", True),
    "centered_shape": ("pearson_r", True),
}


def _rank(entries, key, higher_is_better, rank_key):
    """
    Competition ranking (1, 2, 2, 4) over one statistic.

    Entries whose statistic is undefined rank last, together, rather than
    being dropped: a material that could not be compared is still part of
    the library and must not silently vanish.
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


def compare_all(normalized, materials, channels=None):
    """
    Every material, scored on every metric and ranked by family.

    `materials` maps name -> reference spectrum. Returns the full list
    ordered by combined family rank, best first. Deliberately not
    truncated to a top-N: the complete ranking is stored with the sample
    so a result can be re-examined later, and the interface decides how
    many rows to show.
    """
    entries = []

    for name, reference in (materials or {}).items():
        entries.append({
            "material": name,
            "rmse": rmse(normalized, reference, channels),
            "mae": mae(normalized, reference, channels),
            "cosine_similarity_percent": cosine_similarity_percent(
                normalized, reference, channels
            ),
            "spectral_angle_deg": spectral_angle_degrees(
                normalized, reference, channels
            ),
            "pearson_r": pearson_r(normalized, reference, channels),
        })

    if not entries:
        return []

    weights = config.FAMILY_WEIGHTS
    total_weight = sum(weights.values()) or 1.0

    for family, (key, higher_is_better) in FAMILIES.items():
        _rank(entries, key, higher_is_better, "{}_rank".format(family))

    for entry in entries:
        entry["rank_score"] = round(
            sum(
                entry["{}_rank".format(family)] * weights.get(family, 0.0)
                for family in FAMILIES
            ) / total_weight,
            3,
        )

    entries.sort(key=lambda entry: (entry["rank_score"], entry["material"]))

    for position, entry in enumerate(entries, start=1):
        entry["combined_rank"] = position

        # Presentation rounding, applied last so the ordering above used
        # full precision.
        for key, digits in (
            ("rmse", 5), ("mae", 5),
            ("cosine_similarity_percent", 2),
            ("spectral_angle_deg", 3),
            ("pearson_r", 4),
        ):
            if entry[key] is not None:
                entry[key] = round(entry[key], digits)

        # Historical field names, kept so stored records and the compact
        # table keep working.
        entry["rank"] = position
        entry["similarity_percent"] = entry["cosine_similarity_percent"]
        entry["cosine_rank"] = entry["angular_rank"]
        entry["rmse_rank"] = entry["magnitude_rank"]
        entry["pearson_rank"] = entry["centered_shape_rank"]

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

    for family in FAMILIES:
        rank_key = "{}_rank".format(family)

        # min() returns the FIRST minimum, and `entries` arrives sorted by
        # combined rank - which would quietly bias every tie toward the
        # combined winner and inflate apparent agreement. Break ties on
        # material name so the answer cannot depend on list order.
        leader = min(
            entries, key=lambda entry: (entry[rank_key], entry["material"])
        )
        winners[family] = leader["material"]

    tolerance = config.FAMILY_AGREEMENT_RANK_TOLERANCE

    # Two conditions, because either alone is too easy to satisfy.
    # Tolerance alone passes trivially on a small library where every rank
    # is small; a majority alone ignores how badly the dissenting family
    # ranks the winner.
    within_tolerance = all(
        best["{}_rank".format(family)] <= tolerance for family in FAMILIES
    )

    majority = sum(
        1 for winner in winners.values() if winner == best["material"]
    )

    return {
        "agree": within_tolerance and majority >= 2,
        "within_tolerance": within_tolerance,
        "families_favouring_best": majority,
        "family_count": len(FAMILIES),
        "combined_best": best["material"],
        "family_best": dict(winners),
        "best_ranks": {
            family: best["{}_rank".format(family)] for family in FAMILIES
        },
        "tolerance": tolerance,
        "weights_status": config.FAMILY_WEIGHTS_STATUS,

        # Historical keys.
        "metrics_favouring_best": majority,
        "cosine_best": winners["angular"],
        "rmse_best": winners["magnitude"],
        "pearson_best": winners["centered_shape"],
    }


# Historical name, kept so existing callers keep working.
metric_agreement = family_agreement
