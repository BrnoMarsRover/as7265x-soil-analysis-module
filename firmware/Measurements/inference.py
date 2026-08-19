"""
Cross-database inference — three databases, three answers, one honest
conclusion.

DB1, DB2 and DB3 are scored SEPARATELY and never pooled. They are
different kinds of evidence:

    DB1  measured here, 18 bands      "this looks like something we
                                       measured on this instrument"
    DB2  measured here, 54 features   the same, with UV and IR
    DB3  external, projected          "this looks like a laboratory
                                       spectrum of that mineral, after
                                       modelling our sensor"

A cosine of 0.97 does not mean the same thing in each case, so the scores
are never averaged across databases. What IS combined is the ranking:
when independent sources agree on a material or a family, that agreement
is evidence; when they disagree, the disagreement is reported rather than
resolved by arithmetic.

CONFIDENCE IS NOT SIMILARITY
----------------------------
A 99% cosine against a library where every pair scores 99% carries no
information. Confidence therefore comes from evidence structure, not from
the top score:

    how far ahead the winner is of the runner-up
    whether the metric families agree
    whether the databases agree
    how good the measurement itself was
    whether the sample resembles the library at all

Any of those failing lowers confidence, and the system is allowed to
answer UNKNOWN.
"""

from BD.channels import (
    AS7265X_18,
    AS7265X_54_MULTIILLUM,
    project_to_18,
)
from Measurements import config, metrics, mixture

# Conclusion levels, most specific first. The system falls back to a
# broader claim rather than guessing a narrower one.
LEVEL_MATERIAL = "MATERIAL"
LEVEL_FAMILY = "MATERIAL_FAMILY"
LEVEL_UNKNOWN = "UNKNOWN"

# How much a database's class-level answer is worth, as measured by
# research/analyse_discriminability.py and stored in the database itself.
RELIABILITY_STRONG = "STRONG"
RELIABILITY_MODERATE = "MODERATE"
RELIABILITY_WEAK = "WEAK"
RELIABILITY_INSUFFICIENT = "INSUFFICIENT_DATA"
RELIABILITY_UNRATED = "UNRATED"

# Only a class MEASURED AND FOUND POOR loses its vote. "We measured it
# and it is unreliable" and "we have no measurement" are different
# statements, and treating them alike would silence a library that has
# simply not been assessed yet - including DB1, which carries no
# discriminability block at all.
NOT_CORROBORATING = (RELIABILITY_WEAK,)

CONFIDENCE_HIGH = "HIGH"
CONFIDENCE_MODERATE = "MODERATE"
CONFIDENCE_LOW = "LOW"
CONFIDENCE_NONE = "NONE"


def analyse_database(handle, features, feature_space, usable_channels=None,
                     normalization=None):
    """
    Compare a measurement against ONE database.

    Returns a self-contained result naming the database, its version, the
    feature space actually used and WHICH normalization of the
    measurement it saw, so a stored conclusion can always be traced to
    the library and the calibration that produced it.
    """
    mode = "DIRECT"
    compared = features

    identity = {
        "database": handle.key,
        "database_id": handle.database_id,
        "version": handle.version,
        "evidence": handle.evidence,
        "feature_space": handle.feature_space,
        "normalization": normalization,
        "matches": [],
    }

    # Readiness first. A database with no data at all is EMPTY - saying
    # INCOMPATIBLE instead would blame the measurement for the library's
    # absence and hide the real reason from the operator.
    if not handle.ready:
        result = dict(identity)
        result["status"] = handle.status
        result["reason"] = (handle.problems or ["not available"])[0]

        return result

    if feature_space != handle.feature_space:
        # The only narrowing that is real: a 54-feature measurement
        # contains its 18 WHITE bands. Anything else is refused - an
        # 18-band measurement genuinely lacks the UV and IR features a
        # 54-feature library expects, and they cannot be derived.
        if (
            feature_space == AS7265X_54_MULTIILLUM
            and handle.feature_space == AS7265X_18
        ):
            compared = project_to_18(features, feature_space, "white")
            mode = "PROJECTED_TO_18"

        else:
            result = dict(identity)
            result["status"] = "INCOMPATIBLE"
            result["reason"] = (
                "measurement is {} but this database is {}; the missing "
                "features cannot be derived".format(
                    feature_space, handle.feature_space
                )
            )

            return result

    matches = metrics.compare_all(
        compared, handle.materials, usable_channels
    )
    agreement = metrics.family_agreement(matches)

    best = matches[0] if matches else None
    second = matches[1] if len(matches) > 1 else None

    # How much this database's answer is worth, measured rather than
    # assumed. A library can name a class confidently and still be wrong
    # most of the time in this feature space; the database records that
    # per class and the answer carries it.
    reliability, reliability_detail = (
        handle.class_reliability(best["material"])
        if best else (RELIABILITY_UNRATED, {})
    )

    return {
        "class_reliability": reliability,
        "class_reliability_detail": reliability_detail,
        "database": handle.key,
        "database_id": handle.database_id,
        "version": handle.version,
        "evidence": handle.evidence,
        "feature_space": handle.feature_space,
        "normalization": normalization,
        "comparison_mode": mode,
        "status": "OK",
        "material_count": handle.count(),
        "matches": matches,
        "family_agreement": agreement,
        "best_material": best["material"] if best else None,
        "best_similarity": (
            best["cosine_similarity_percent"] if best else None
        ),
        "best_rmse": best["rmse"] if best else None,
        "margin_percent": (
            round(
                best["cosine_similarity_percent"]
                - second["cosine_similarity_percent"], 3
            )
            if best and second
            and best["cosine_similarity_percent"] is not None
            and second["cosine_similarity_percent"] is not None
            else None
        ),
        "rank_separation": (
            round(second["rank_score"] - best["rank_score"], 3)
            if best and second else None
        ),
    }


def _family_of(handle, material):
    """Canonical family of a material, if the database knows one."""
    metadata = (handle.metadata or {}).get(material) or {}

    return metadata.get("material_class") or metadata.get("canonical_name")


def build_consensus(database_results, registry):
    """
    Combine per-database conclusions without pooling their scores.

    Agreement between INDEPENDENT sources is the useful signal. DB1 and
    DB2 both measure this instrument, so their agreement is weaker
    evidence than agreement between DB1 and DB3, which share no
    measurement, no instrument and no operator.
    """
    usable = [
        result for result in database_results
        if result.get("status") == "OK" and result.get("best_material")
    ]

    # A database whose class reliability was MEASURED and found poor does
    # not get a vote on the material. It is still reported in full - the
    # operator sees what the library said and why it was discounted - but
    # counting it would be counting a coin flip as a second opinion.
    voting = [
        result for result in usable
        if result.get("class_reliability", RELIABILITY_UNRATED)
        not in NOT_CORROBORATING
    ]
    discounted = [
        {
            "database": result["database"],
            "best_material": result["best_material"],
            "class_reliability": result.get("class_reliability"),
            "reason": "measured class reliability is {} for {}".format(
                result.get("class_reliability"),
                (result.get("class_reliability_detail") or {}).get(
                    "material_class", "this class"),
            ),
        }
        for result in usable if result not in voting
    ]

    if not usable:
        return {
            "level": LEVEL_UNKNOWN,
            "material": None,
            "family": None,
            "supporting_databases": [],
            "disagreements": [],
            "reason": "no database produced a comparable result",
        }

    # Fall back to the full set only if discounting left nothing: a
    # discounted answer is better than no answer, provided it is labelled.
    considered = voting or usable

    by_material = {}
    by_family = {}

    for result in considered:
        material = result["best_material"]
        by_material.setdefault(material, []).append(result["database"])

        handle = registry.get(result["database"])
        family = _family_of(handle, material) if handle else None

        if family:
            by_family.setdefault(family, []).append(result["database"])

    material, supporters = max(
        by_material.items(), key=lambda item: (len(item[1]), item[0])
    )

    family, family_supporters = (
        max(by_family.items(), key=lambda item: (len(item[1]), item[0]))
        if by_family else (None, [])
    )

    disagreements = [
        {
            "database": result["database"],
            "evidence": result["evidence"],
            "best_material": result["best_material"],
            "best_similarity": result["best_similarity"],
            "class_reliability": result.get("class_reliability"),
        }
        for result in considered
        if result["best_material"] != material
    ]

    # Independent means "does not share measurements". DB1 and DB2 are
    # the same instrument; DB3 is a different world entirely.
    independent = len({
        registry.get(key).evidence
        for key in supporters
        if registry.get(key)
    })

    if len(supporters) >= 2 and independent >= 2:
        level = LEVEL_MATERIAL
    elif len(family_supporters) >= 2:
        level = LEVEL_FAMILY
    elif len(usable) == 1:
        level = LEVEL_MATERIAL
    else:
        level = LEVEL_FAMILY if family else LEVEL_MATERIAL

    return {
        "level": level,
        "material": material,
        "family": family,
        "supporting_databases": sorted(supporters),
        "family_supporting_databases": sorted(family_supporters),
        "independent_evidence_kinds": independent,
        "databases_compared": [result["database"] for result in usable],
        "databases_voting": [result["database"] for result in considered],
        "discounted_for_low_reliability": discounted,
        "disagreements": disagreements,
        "agreement": not disagreements,
    }


def assess_confidence(database_results, consensus, quality=None):
    """
    Confidence from evidence structure, NOT from the top similarity.

    Every factor is reported with its verdict, so an operator can see why
    the answer is or is not trusted rather than being handed a number.
    """
    factors = []
    penalties = 0

    def factor(name, verdict, detail, penalty=0):
        nonlocal penalties
        factors.append({
            "factor": name, "verdict": verdict, "detail": detail,
        })
        penalties += penalty

    quality_status = (quality or {}).get("status")

    if quality_status == "FAIL":
        factor("measurement_quality", "FAIL",
               "the measurement did not pass quality control", 99)
    elif quality_status == "WARNING":
        factor("measurement_quality", "WARNING",
               "quality control raised warnings", 1)
    else:
        factor("measurement_quality", "PASS", "quality control passed")

    usable = [r for r in database_results if r.get("status") == "OK"]
    voting = consensus.get("databases_voting") or []

    if not usable:
        factor("database_availability", "FAIL",
               "no database was comparable", 99)
    elif len(voting) <= 1:
        factor("database_availability", "WARNING",
               "only {} carries a usable vote; no cross-database "
               "corroboration".format(
                   ", ".join(voting) or "one database"), 1)
    else:
        factor("database_availability", "PASS",
               "{} databases vote ({})".format(
                   len(voting), ", ".join(voting)))

    primary = usable[0] if usable else None

    if primary:
        similarity = primary.get("best_similarity")

        if similarity is None:
            factor("match_quality", "FAIL", "no comparable score", 2)
        elif similarity < config.MIN_SIMILARITY_PERCENT:
            factor("match_quality", "FAIL",
                   "best match {:.1f}% is below the {:.1f}% support "
                   "threshold".format(
                       similarity, config.MIN_SIMILARITY_PERCENT), 2)
        else:
            factor("match_quality", "PASS",
                   "best match {:.1f}%".format(similarity))

        separation = primary.get("rank_separation")

        if separation is None:
            factor("separation", "WARNING",
                   "only one candidate to compare", 1)
        elif separation < config.MIN_RANK_SEPARATION:
            factor("separation", "FAIL",
                   "the runner-up is not separable from the winner "
                   "(rank separation {:.2f})".format(separation), 2)
        else:
            factor("separation", "PASS",
                   "clear separation from the runner-up "
                   "({:.2f})".format(separation))

        agreement = primary.get("family_agreement") or {}

        if agreement.get("agree") is False:
            factor("metric_agreement", "FAIL",
                   "the metric families disagree: shape and magnitude "
                   "point at different materials", 2)
        elif agreement.get("agree"):
            factor("metric_agreement", "PASS",
                   "all metric families agree")
        else:
            factor("metric_agreement", "WARNING",
                   "metric agreement could not be assessed", 1)

    # Measured class reliability. A library that names a class it gets
    # right 36% of the time in this feature space is not corroboration,
    # however high its cosine. This is the number research/
    # analyse_discriminability.py measures and the database stores.
    discounted = consensus.get("discounted_for_low_reliability") or []

    if discounted:
        factor("class_discriminability", "FAIL",
               "; ".join(
                   "{} named {} but this sensor separates that class "
                   "poorly ({})".format(
                       entry["database"], entry["best_material"],
                       entry["class_reliability"])
                   for entry in discounted), 3)

    else:
        rated = [
            result for result in usable
            if result.get("class_reliability") in (
                RELIABILITY_STRONG, RELIABILITY_MODERATE)
        ]
        unrated = [
            result for result in usable
            if result.get("class_reliability") in (
                RELIABILITY_UNRATED, RELIABILITY_INSUFFICIENT)
        ]

        if rated:
            factor("class_discriminability", "PASS",
                   "the sensor separates the nominated class "
                   "({})".format(", ".join(
                       "{}: {}".format(r["database"],
                                       r["class_reliability"])
                       for r in rated)))

        elif unrated:
            factor("class_discriminability", "WARNING",
                   "the nominated class has no usable discriminability "
                   "measurement ({}) - unknown is not the same as "
                   "reliable".format(", ".join(sorted({
                       str(r.get("class_reliability")) for r in unrated
                   }))), 1)

    if consensus.get("disagreements"):
        factor("cross_database", "WARNING",
               "databases disagree: {}".format(", ".join(
                   "{} says {}".format(d["database"], d["best_material"])
                   for d in consensus["disagreements"])), 2)
    elif len(usable) > 1:
        factor("cross_database", "PASS",
               "every available database agrees")

    # The ladder counts how many independent things went wrong. Losing a
    # database to measured-poor discriminability costs 3, because it does
    # not merely warn - it removes a source of corroboration entirely.
    if penalties >= 99:
        level = CONFIDENCE_NONE
    elif penalties >= 4:
        level = CONFIDENCE_LOW
    elif penalties >= 2:
        level = CONFIDENCE_MODERATE
    elif penalties >= 1:
        level = CONFIDENCE_MODERATE
    else:
        level = CONFIDENCE_HIGH

    return {
        "level": level,
        "factors": factors,
        "penalty_score": penalties,
        "basis": "evidence structure, not similarity magnitude",
        "note": "Confidence is a heuristic engineering judgement. It is "
                "NOT a calibrated probability and must not be reported "
                "as one.",
    }


def infer(features, feature_space, registry, quality=None,
          usable_channels=None, run_mixture=True, feature_sources=None):
    """
    The full cross-database inference.

        each database, independently
            -> consensus across them
            -> confidence from evidence structure
            -> optional mixture analysis
            -> a conclusion no stronger than the evidence

    `feature_sources` names, PER DATABASE, which normalization of the
    same measurement that database is entitled to see:

        {"DB1": {"features": ..., "feature_space": ...,
                 "normalization": "legacy"}, ...}

    It exists because the databases are not normalized alike and no
    single vector can serve them all. DB1 may only ever be compared
    against the frozen legacy White/Dark it was built with - normalize it
    against today's calibration and every stored number quietly means
    something else. DB3 was never measured on this instrument at all, so
    the honest comparison is against the CURRENT calibration. Forcing one
    vector on both would make one of them wrong, silently.

    Anything not named falls back to `features` / `feature_space`.

    Returns a machine-readable result; formatting for humans is the PC
    layer's job.
    """
    sources = dict(feature_sources or {})

    def source_for(key):
        entry = sources.get(key) or {}

        return (
            entry.get("features", features),
            entry.get("feature_space", feature_space),
            entry.get("normalization"),
        )

    # Every database is analysed, compatible or not: an absent DB2 and an
    # incompatible one must both be visible, not silently omitted.
    # analyse_database reports the reason itself.
    database_results = []
    compared_features = {}

    for key, handle in sorted(registry.databases.items()):
        db_features, db_space, db_normalization = source_for(key)

        database_results.append(analyse_database(
            handle, db_features, db_space, usable_channels,
            db_normalization,
        ))

        compared_features[key] = (db_features, db_space)

    database_results.sort(key=lambda result: result["database"])

    consensus = build_consensus(database_results, registry)
    confidence = assess_confidence(database_results, consensus, quality)

    # The system must be able to answer UNKNOWN rather than always
    # crowning whichever library entry happened to rank first. A sample
    # that resembles nothing is a real and useful result.
    verdicts = {
        factor["factor"]: factor["verdict"]
        for factor in confidence["factors"]
    }

    no_good_match = verdicts.get("match_quality") == "FAIL"
    not_separable = verdicts.get("separation") == "FAIL"
    families_disagree = verdicts.get("metric_agreement") == "FAIL"

    if confidence["level"] == CONFIDENCE_NONE:
        consensus = dict(consensus)
        consensus["level"] = LEVEL_UNKNOWN
        consensus["reason"] = "evidence is insufficient to name a material"

    elif no_good_match and (not_separable or families_disagree):
        # Nothing in the library is a good fit AND the ranking cannot be
        # trusted. Naming the top entry here would be guessing.
        consensus = dict(consensus)
        consensus["level"] = LEVEL_UNKNOWN
        consensus["nearest_material"] = consensus.get("material")
        consensus["material"] = None
        consensus["reason"] = (
            "no reference is a good match and the ranking is not "
            "separable; the sample is probably not represented by the "
            "available libraries"
        )

    elif no_good_match and consensus.get("family"):
        # Poor material-level fit, but the family may still hold.
        consensus = dict(consensus)
        consensus["level"] = LEVEL_FAMILY
        consensus["nearest_material"] = consensus.get("material")
        consensus["reason"] = (
            "no reference matches well enough to name a material; "
            "reporting at family level instead"
        )

    mixture_result = None

    if run_mixture and confidence["level"] != CONFIDENCE_NONE:
        primary = next(
            (r for r in database_results if r.get("status") == "OK"), None
        )

        if primary and primary["matches"]:
            handle = registry.get(primary["database"])
            candidates = [
                match["material"]
                for match in primary["matches"][
                    :config.MIXTURE_MAX_ENDMEMBERS + 2
                ]
            ]
            compared, compared_space = compared_features[primary["database"]]

            if primary.get("comparison_mode") == "PROJECTED_TO_18":
                compared = project_to_18(compared, compared_space, "white")

            mixture_result = mixture.estimate(
                compared, handle.materials, candidates,
                usable_channels,
            )
            mixture_result["database"] = primary["database"]

    return {
        "feature_space": feature_space,
        "normalizations": {
            key: result.get("normalization")
            for key, result in (
                (entry["database"], entry) for entry in database_results
            )
        },
        "database_results": database_results,
        "consensus": consensus,
        "confidence": confidence,
        "mixture": mixture_result,
        "analysis_version": config.ANALYSIS_VERSION,
        "science_version": config.SCIENCE_VERSION,
        "database_versions": {
            key: handle.version
            for key, handle in sorted(registry.databases.items())
        },
        "limitations": [
            "The AS7265x provides 18 sparse bands over 410-940 nm. "
            "Materials whose diagnostic features are narrower than the "
            "band spacing, or lie outside that range, cannot be "
            "distinguished by this instrument however good the library.",
            "Reflectance depends on grain size, packing, surface "
            "roughness and geometry, none of which are measured here.",
            "Similarity is not probability and contribution is not "
            "concentration.",
        ],
    }
