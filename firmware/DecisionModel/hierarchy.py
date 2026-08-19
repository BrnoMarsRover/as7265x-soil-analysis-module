"""
Family before material.

Twenty-three-way exact classification is the hardest question available
and the system asks it first only because it was never taught to ask
anything easier. The AS7265x measurably cannot separate most of these
minerals - 15.7% of DB3's material pairs score cosine >= 0.999 - but it
CAN often place a sample in a family, because families differ where the
sensor can see: iron electronic structure in the blue and near 900 nm.

So the ladder is:

    1. is the measurement usable at all
    2. is it inside the known spectral domain
    3. which family fits
    4. which materials in that family fit
    5. are those materials actually separable from each other
    6. is the evidence strong enough to name one

A family conclusion reached at step 3 is a real scientific result, not a
consolation prize. §21, §22.

The families come from the taxonomy the databases already carry - the
`material_class` field on every DB1 and DB3 entry - never from a list
invented here. §51.
"""

# A family needs this share of the candidate evidence before it is
# reported as the answer. PROVISIONAL.
FAMILY_SUPPORT_THRESHOLD = 0.45

# And this much more than the next family, or the families themselves are
# not separable and the honest answer is the candidate set.
FAMILY_MARGIN_THRESHOLD = 0.15

# How many candidates are considered when looking for a family. Beyond
# this the tail is noise: every library entry has SOME support.
FAMILY_CANDIDATE_DEPTH = 6


def family_support(candidates, taxonomy, depth=FAMILY_CANDIDATE_DEPTH):
    """
    Aggregate candidate evidence by family.

    Support is summed within a family, not averaged: three carbonates
    each with moderate support IS stronger evidence for "carbonate" than
    one carbonate with the same moderate support, and averaging would
    erase exactly that.
    """
    totals = {}

    for candidate in candidates[:depth]:
        material = candidate["material"]
        family = taxonomy.family_of(material) if taxonomy else None

        if not family:
            continue

        entry = totals.setdefault(family, {
            "family": family,
            "support": 0.0,
            "materials": [],
        })

        entry["support"] += candidate["evidence_strength"]
        entry["materials"].append({
            "material": material,
            "evidence_strength": candidate["evidence_strength"],
            "supporting_databases": candidate.get("supporting_databases"),
        })

    total = sum(entry["support"] for entry in totals.values())

    families = []

    for entry in totals.values():
        families.append({
            "family": entry["family"],
            "support": round(entry["support"], 4),
            "share": round(entry["support"] / total, 4) if total else 0.0,
            "materials": sorted(
                entry["materials"],
                key=lambda item: -item["evidence_strength"],
            ),
            "member_count": len(entry["materials"]),
        })

    families.sort(key=lambda entry: (-entry["support"], entry["family"]))

    leader = families[0] if families else None
    runner_up = families[1] if len(families) > 1 else None

    margin = (
        leader["share"] - runner_up["share"]
        if leader and runner_up else (leader["share"] if leader else 0.0)
    )

    return {
        "families": families,
        "leader": leader["family"] if leader else None,
        "leader_share": leader["share"] if leader else None,
        "runner_up": runner_up["family"] if runner_up else None,
        "margin": round(margin, 4),
        "decisive": bool(
            leader
            and leader["share"] >= FAMILY_SUPPORT_THRESHOLD
            and margin >= FAMILY_MARGIN_THRESHOLD
        ),
        "thresholds": {
            "support": FAMILY_SUPPORT_THRESHOLD,
            "margin": FAMILY_MARGIN_THRESHOLD,
            "status": "PROVISIONAL",
        },
    }


def ambiguous_set(candidates, separation, limit=4):
    """
    The candidates that cannot be told apart from the leader.

    Everything within the leader's own margin is in the set. Picking a
    winner from inside that band would be choosing on noise, which is
    what "AMBIGUOUS_SET" exists to refuse. §22.
    """
    if not candidates:
        return []

    leader_strength = candidates[0]["evidence_strength"]
    margin = separation.get("margin") or 0.0

    # The band is the leader's lead over the runner-up, floored so that a
    # zero margin does not collapse the set to one member.
    #
    # TOLERANCE, not decoration: `margin` arrives rounded while the
    # strengths do not, so the runner-up that DEFINED the margin can
    # fail a bare <= by 4e-17 and vanish from the set it created. That
    # turned a two-candidate ambiguity into a lone leader, and then into
    # UNKNOWN, on floating-point noise alone.
    band = max(margin, 0.05) + 1e-9

    members = [
        candidate for candidate in candidates
        if leader_strength - candidate["evidence_strength"] <= band
    ]

    return members[:limit]


def contextual_neighbours(candidates, taxonomy, limit=3):
    """
    Nearest known things, for an UNKNOWN result.

    "I do not know, and the nearest things I know are these" is more
    useful than "I do not know", and it is still not a claim about what
    the sample is. §22.
    """
    neighbours = []

    for candidate in candidates[:limit]:
        neighbours.append({
            "material": candidate["material"],
            "family": (
                taxonomy.family_of(candidate["material"])
                if taxonomy else None
            ),
            "evidence_strength": candidate["evidence_strength"],
        })

    return neighbours
