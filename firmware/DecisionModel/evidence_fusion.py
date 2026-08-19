"""
Fusing evidence without throwing away magnitude.

THE THING THIS MODULE REFUSES TO DO

    cosine rank 1 + RMSE rank 4 + Pearson rank 2  ->  combined 7

A rank says a candidate came first. It cannot say whether it came first
by a nose or by a mile, and those are opposite conclusions. From the
operator's own session:

    Activated Carbon   RMSE 0.0088 vs runner-up 0.1705    decisive
    Kaolin             cosine 98.75 vs runner-up 98.74    nothing

Both are "rank 1". §25.

WHAT IS USED INSTEAD

For each metric, every candidate is placed on a 0..1 support scale
relative to the FIELD it was scored against:

    support = (score - median) / (best - median)

Median rather than mean, because the field is skewed and the winner is
by definition an outlier. A candidate at the median scores 0; only a
candidate that stands clear of the pack approaches 1. In a library where
every entry scores 99% cosine, every entry sits at the median and every
support is near zero - which is the correct reading of that situation and
exactly what a rank hides.

ONE VOTE PER FAMILY

    magnitude       RMSE / MAE / Euclidean / weighted Euclidean
    angular         cosine / spectral angle
    centered_shape  Pearson r

Pearson is the cosine of mean-centered vectors, so counting it beside
cosine counts shape twice. Within a family the member with the clearest
separation speaks for it. §5 of the existing architecture, kept.

DATABASES ARE WEIGHTED, NEVER POOLED

Each database produces its own candidate support. They are combined by
weight - database trust x class reliability - and the contributions are
kept separately in the output so a conclusion can always be traced to
which library actually drove it.
"""

from DecisionModel import reliability as reliability_module

# Families whose members are mathematically dependent get one vote.
FAMILY_WEIGHTS = {
    "magnitude": 1.0,
    "angular": 1.0,
    "centered_shape": 1.0,
}

FAMILY_WEIGHT_STATUS = "PROVISIONAL_UNVALIDATED"

# A class reliability of WEAK removes the vote entirely; the answer is
# still reported. Same rule the previous inference layer used, kept
# because it was right and is now measured rather than assumed.
NON_VOTING_RATINGS = (reliability_module.WEAK,)

# Support below this is not evidence for anything.
MIN_SUPPORT = 0.05

# How many robust deviations clear of the field a winner must stand
# before its lead counts for everything it could.
#
# WHY THIS EXISTS. Median-relative support is a RELATIVE scale, and on
# its own it is degenerate: whoever wins gets 1.0 and everyone else 0,
# whether the lead was 0.4 or 0.0005. A field of three carbonates
# separated by five thousandths of a cosine produced "support 1.0,
# margin 1.0, name the material" - which is precisely the magnitude-blind
# behaviour that replacing rank aggregation was meant to end.
#
# So relative standing is scaled by how unusual the winner is against the
# field's own scatter, and by how good its fit is in absolute terms. A
# winner that leads a tight pack by a hair now earns a small fraction of
# the support that one standing clear of the field does.
#
# PROVISIONAL: four deviations is an engineering judgement, not a
# measured threshold.
Z_FOR_FULL_CONFIDENCE = 4.0

# A winner that fits badly in absolute terms cannot earn full support
# however far ahead of a worse field it is. Absolute goodness is reported
# per metric by Measurements/distances.py.
USE_ABSOLUTE_GOODNESS = True


def _support_from(scores, higher_is_better):
    """
    Place every candidate on a 0..1 scale relative to the field.

    Returns {} when the field cannot discriminate at all - every score
    identical - which is itself a finding and must not be smoothed over.
    """
    usable = {
        name: value for name, value in scores.items()
        if isinstance(value, (int, float))
    }

    if len(usable) < 2:
        return {}

    values = sorted(usable.values())
    middle = len(values) // 2

    median = (
        values[middle] if len(values) % 2
        else (values[middle - 1] + values[middle]) / 2.0
    )

    best = max(values) if higher_is_better else min(values)
    span = abs(best - median)

    if span <= 0:
        return {}

    support = {}

    for name, value in usable.items():
        raw = (value - median) if higher_is_better else (median - value)
        support[name] = max(0.0, min(1.0, raw / span))

    return support


def database_support(database_result, reliability, database_key):
    """
    Candidate support from ONE database, one vote per metric family.

    `database_result` is the reference_analysis entry the measurement
    layer produced. Its per-metric `top` lists and score distributions
    are what carry magnitude, so they are what is used.
    """
    metrics = (database_result or {}).get("metrics") or {}
    families = (database_result or {}).get("families") or {}

    per_candidate = {}
    family_detail = {}

    for family, summary in families.items():
        metric = summary.get("metric")
        definition = metrics.get(metric) or {}

        scores = {
            entry["material"]: entry["score"]
            for entry in definition.get("top") or []
        }

        # `top` is truncated, which is exactly right: support is about
        # standing out from the field, and a candidate outside the top
        # of its own metric is not standing out.
        support = _support_from(
            scores, definition.get("higher_is_better", True)
        )

        weight = FAMILY_WEIGHTS.get(family, 1.0)

        # Relative standing is only half the story. Scale it by how far
        # the winner stands out from the field's own scatter, and by how
        # good the fit is in absolute terms - see Z_FOR_FULL_CONFIDENCE.
        separation = summary.get("z_separation")

        confidence = (
            min(1.0, separation / Z_FOR_FULL_CONFIDENCE)
            if separation else 0.25
        )

        goodness = summary.get("absolute_goodness")

        if USE_ABSOLUTE_GOODNESS and goodness is not None:
            confidence *= max(0.0, min(1.0, goodness))

        family_detail[family] = {
            "metric": metric,
            "winner": summary.get("winner"),
            "absolute_margin": summary.get("absolute_margin"),
            "relative_margin": summary.get("relative_margin"),
            "z_separation": separation,
            "absolute_goodness": goodness,
            "separation_confidence": round(confidence, 4),
            "weight": weight,
            "support": {
                name: round(value * confidence, 4)
                for name, value in support.items()
            },
        }

        for name, value in support.items():
            scaled = value * confidence

            entry = per_candidate.setdefault(
                name, {"weighted": 0.0, "families": {}}
            )
            entry["weighted"] += weight * scaled
            entry["families"][family] = round(scaled, 4)

    total_weight = sum(FAMILY_WEIGHTS.get(f, 1.0) for f in family_detail)

    candidates = {}

    for name, entry in per_candidate.items():
        strength = entry["weighted"] / total_weight if total_weight else 0.0

        class_rating = reliability.class_reliability(database_key, name)

        candidates[name] = {
            "support": round(strength, 4),
            "families": entry["families"],
            "family_agreement": len(entry["families"]),
            "class_reliability": class_rating["rating"],
            "class_reliability_basis": class_rating["basis"],
            "votes": class_rating["rating"] not in NON_VOTING_RATINGS,
        }

    return {
        "database": database_key,
        "candidates": candidates,
        "families": family_detail,
        "families_available": sorted(family_detail),
        "database_weight": reliability.database_weight(database_key),
    }


def fuse(reference_analysis, reliability, class_analysis=None):
    """
    Combine every database's support into one candidate table.

    Databases are weighted, never pooled: each contribution is kept, so
    the answer to "why?" is always "DB1 said this much, DB3 said that
    much, and DB3's answer was discounted because its class reliability
    for that class was measured WEAK".
    """
    databases = (reference_analysis or {}).get("databases") or {}

    per_database = {}
    fused = {}
    discounted = []

    for key, result in sorted(databases.items()):
        if not result.get("available"):
            continue

        support = database_support(result, reliability, key)
        per_database[key] = support

        weight = support["database_weight"]

        for name, entry in support["candidates"].items():
            if not entry["votes"]:
                discounted.append({
                    "database": key,
                    "material": name,
                    "reason": "class reliability measured {}".format(
                        entry["class_reliability"]
                    ),
                })

                continue

            record = fused.setdefault(name, {
                "material": name,
                "strength": 0.0,
                "weight": 0.0,
                "databases": {},
            })

            record["strength"] += weight * entry["support"]
            record["weight"] += weight
            record["databases"][key] = entry

    candidates = []

    for record in fused.values():
        strength = (
            record["strength"] / record["weight"] if record["weight"] else 0.0
        )

        candidates.append({
            "material": record["material"],
            "evidence_strength": round(strength, 4),
            "supporting_databases": sorted(record["databases"]),
            "independent_sources": len(record["databases"]),
            "per_database": record["databases"],
        })

    # Class distance is separate evidence and is added, never averaged
    # in: it answers a different question - "does the sample lie inside
    # the region this material has been seen to occupy" - and a library
    # cosine cannot substitute for it.
    class_support = _class_support(class_analysis)

    for candidate in candidates:
        entry = class_support.get(candidate["material"])

        candidate["class_evidence"] = entry

        if entry and entry.get("support") is not None:
            candidate["evidence_strength"] = round(
                0.7 * candidate["evidence_strength"]
                + 0.3 * entry["support"], 4
            )
            candidate["class_evidence_applied"] = True

        else:
            candidate["class_evidence_applied"] = False

    candidates.sort(
        key=lambda item: (-item["evidence_strength"], item["material"])
    )

    return {
        "candidates": candidates,
        "per_database": per_database,
        "discounted": discounted,
        "class_support_available": bool(class_support),
        "family_weights": dict(FAMILY_WEIGHTS),
        "family_weight_status": FAMILY_WEIGHT_STATUS,
        "method": "median-relative support per metric family, one vote "
                  "per family, databases weighted by measured or declared "
                  "reliability. Ranks are never summed.",
    }


def _class_support(class_analysis):
    """
    Turn class distances into 0..1 support, using the class's own scatter.

    The scale is the class's observed within-class spread, not an
    arbitrary constant: "inside the range this material has actually been
    seen to occupy" is a statement the data can support, while "within
    0.1 reflectance units" is one it cannot.
    """
    if not class_analysis or not class_analysis.get("available"):
        return {}

    support = {}

    for material, entry in (class_analysis.get("per_material") or {}).items():
        ratio = entry.get("within_class_ratio")

        if ratio is None:
            support[material] = {
                "support": None,
                "reason": "no within-class scatter yet - needs independent "
                          "repeats of this material",
                "n_independent": entry.get("n_independent"),
            }

            continue

        # ratio 0 -> dead centre, 1 -> as far out as the furthest member
        # ever was, >2 -> outside anything ever seen.
        value = max(0.0, min(1.0, 1.0 - (ratio / 2.0)))

        support[material] = {
            "support": round(value, 4),
            "within_class_ratio": ratio,
            "centroid_distance": entry.get("centroid_distance"),
            "mahalanobis_distance": entry.get("mahalanobis_distance"),
            "n_independent": entry.get("n_independent"),
        }

    return support


def separability(candidates):
    """
    Is the leader actually distinguishable from the field?

    The margin is reported in absolute and relative terms; the decision
    thresholds live in engine.py, because this module measures and does
    not decide.
    """
    usable = [
        candidate for candidate in candidates
        if candidate["evidence_strength"] >= MIN_SUPPORT
    ]

    if not usable:
        return {
            "leader": None, "runner_up": None, "margin": None,
            "relative_margin": None, "candidates_above_floor": 0,
        }

    leader = usable[0]
    runner_up = usable[1] if len(usable) > 1 else None

    margin = (
        leader["evidence_strength"] - runner_up["evidence_strength"]
        if runner_up else leader["evidence_strength"]
    )

    return {
        "leader": leader["material"],
        "leader_strength": leader["evidence_strength"],
        "runner_up": runner_up["material"] if runner_up else None,
        "runner_up_strength": (
            runner_up["evidence_strength"] if runner_up else None
        ),
        "margin": round(margin, 4),
        "relative_margin": (
            round(margin / leader["evidence_strength"], 4)
            if leader["evidence_strength"] > 0 else None
        ),
        "candidates_above_floor": len(usable),
    }
