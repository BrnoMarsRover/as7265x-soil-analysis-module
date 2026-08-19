"""
Out-of-distribution detection — making UNKNOWN reachable.

A cosine of 0.99 against the nearest library entry is not evidence that
the sample is that entry. Reflectance is non-negative and smooth, so
almost anything scores 0.99 against almost anything: on DB1, 45% of all
material pairs already do. A similarity metric therefore CANNOT say "this
is not in the library" - it has no vocabulary for absence.

What can say it:

    distance to the nearest CLASS, in units of that class's own scatter
    nearest-neighbour distance to any real observation
    how flat the candidate field is (nobody stands out)
    disagreement between metric families
    disagreement between databases
    a measurement that failed hardware QC

Each is an independent reason to refuse. They are counted, not averaged,
because they are different kinds of doubt: two mild reasons and one
severe reason are not the same thing, and averaging would let a strong
signal on one axis paper over a fatal one on another. §37.

UNKNOWN IS A RESULT. Returning it is not a failure of the system, it is
the system working. §61.
"""

# Beyond this many times the class's own worst within-class distance, the
# sample is outside anything that class has ever been seen to do.
# PROVISIONAL: it needs several independent repeats per class before it
# can be calibrated, and with one observation per class it cannot fire.
OUTSIDE_CLASS_RATIO = 2.0

# Evidence strength below which nothing in the library really fits.
WEAK_SUPPORT = 0.25

# Relative margin below which the leader is not separable from the field.
NOT_SEPARABLE_MARGIN = 0.15

# Reasons of this severity or worse force UNKNOWN on their own.
SEVERE = "SEVERE"
MODERATE = "MODERATE"
MILD = "MILD"

# Two moderate reasons are enough to refuse.
MODERATE_REASONS_FOR_UNKNOWN = 2


def assess(evidence, fusion, separation):
    """
    Every independent reason to answer UNKNOWN, with its severity.

    Returns the reasons and whether they compel a refusal. The engine
    applies it; this module only measures.
    """
    reasons = []

    quality = (evidence.get("quality") or {})
    hardware = (quality.get("hardware") or {})
    normalization = (quality.get("normalization") or {})

    if hardware.get("status") == "HARDWARE_QC_FAIL":
        reasons.append({
            "code": "HARDWARE_QC_FAIL",
            "severity": SEVERE,
            "detail": "the instrument did not produce a usable "
                      "measurement, so nothing derived from it can be "
                      "trusted",
        })

    reliability = evidence.get("channel_reliability") or {}
    total = reliability.get("features_total") or 0
    normalized_valid = reliability.get("normalized_valid_total") or 0

    if total and normalized_valid < total * 0.25:
        reasons.append({
            "code": "TOO_FEW_RELIABLE_FEATURES",
            "severity": SEVERE,
            "detail": "only {}/{} features support a usable reflectance"
                      .format(normalized_valid, total),
        })

    candidates = fusion.get("candidates") or []

    if not candidates:
        reasons.append({
            "code": "NO_CANDIDATES",
            "severity": SEVERE,
            "detail": "no database produced a comparable candidate",
        })

    else:
        leader = candidates[0]

        if leader["evidence_strength"] < WEAK_SUPPORT:
            reasons.append({
                "code": "NO_GOOD_MATCH",
                "severity": MODERATE,
                "detail": "the best candidate stands out from the field by "
                          "only {:.2f} on a 0-1 scale; in a library where "
                          "everything scores alike, that is not a match"
                          .format(leader["evidence_strength"]),
            })

    relative_margin = separation.get("relative_margin")

    if relative_margin is not None and relative_margin < NOT_SEPARABLE_MARGIN:
        reasons.append({
            "code": "NOT_SEPARABLE",
            "severity": MODERATE,
            "detail": "the leader is only {:.0%} clear of the runner-up"
                      .format(relative_margin),
        })

    class_analysis = evidence.get("class_analysis") or {}

    if class_analysis.get("available"):
        nearest = class_analysis.get("nearest")
        entry = (class_analysis.get("per_material") or {}).get(nearest) or {}
        ratio = entry.get("within_class_ratio")

        if ratio is not None and ratio > OUTSIDE_CLASS_RATIO:
            reasons.append({
                "code": "OUTSIDE_KNOWN_CLASSES",
                "severity": SEVERE,
                "detail": "the sample sits {:.1f}x further from the nearest "
                          "class centroid than any member of that class "
                          "ever has".format(ratio),
            })

    else:
        reasons.append({
            "code": "NO_CLASS_STATISTICS",
            "severity": MILD,
            "detail": class_analysis.get("reason")
            or "no class distributions exist yet, so 'inside a known "
               "class' cannot be checked at all",
        })

    if normalization.get("status") == "NORMALIZATION_UNUSABLE":
        reasons.append({
            "code": "NORMALIZATION_UNUSABLE",
            "severity": MODERATE,
            "detail": "the reference division is too ill-conditioned for "
                      "reflectance-based comparison; the raw measurement "
                      "is unaffected",
        })

    severe = [entry for entry in reasons if entry["severity"] == SEVERE]
    moderate = [entry for entry in reasons if entry["severity"] == MODERATE]

    return {
        "reasons": reasons,
        "severe": len(severe),
        "moderate": len(moderate),
        "unknown_required": bool(
            severe or len(moderate) >= MODERATE_REASONS_FOR_UNKNOWN
        ),
        "thresholds": {
            "outside_class_ratio": OUTSIDE_CLASS_RATIO,
            "weak_support": WEAK_SUPPORT,
            "not_separable_margin": NOT_SEPARABLE_MARGIN,
            "status": "PROVISIONAL - not yet validated against a set of "
                      "samples known to be outside the library",
        },
    }
