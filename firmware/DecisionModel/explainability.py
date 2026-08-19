"""
Explanations built ONLY from structured evidence.

Every sentence this module emits is generated from a number that is in
the decision object. There is no free text, no template that asserts
anything the evidence did not say, and nothing that describes chemistry.

Allowed, because each clause names its source:

    "DB1 shape evidence places Bentonite clearest of the field (spectral
     angle, 8.6 robust deviations clear), while DB1 magnitude evidence
     favours Pink Clay."

Forbidden, because the instrument cannot support it:

    "The sample contains magnesium carbonate."
    "97% probability."

The rule is mechanical: if a sentence cannot be traced to a field in the
decision object, it does not get written. §44, §36.
"""

from DecisionModel import reliability as reliability_module

FAMILY_WORDS = {
    "magnitude": "magnitude",
    "angular": "shape",
    "centered_shape": "correlation",
}

LEVEL_SENTENCE = {
    "KNOWN_MATERIAL": "The evidence supports naming a single material.",
    "MATERIAL_FAMILY": "The evidence supports a family but not an "
                       "individual material.",
    "AMBIGUOUS_SET": "Several candidates are statistically "
                     "indistinguishable, so none is chosen.",
    "UNKNOWN": "The evidence does not support naming a material.",
}


def _percent(value):
    return "{:.0%}".format(value) if isinstance(value, float) else "-"


def database_sentences(fusion, decision_material=None):
    """One sentence per database, naming what it actually said."""
    sentences = []

    for key, support in sorted((fusion.get("per_database") or {}).items()):
        families = support.get("families") or {}

        if not families:
            continue

        clauses = []

        for family, detail in sorted(families.items()):
            winner = detail.get("winner")

            if not winner:
                continue

            separation = detail.get("z_separation")

            if separation:
                clauses.append(
                    "{} evidence favours {} ({:.1f} robust deviations "
                    "clear of the field)".format(
                        FAMILY_WORDS.get(family, family), winner, separation
                    )
                )

            else:
                clauses.append(
                    "{} evidence favours {}".format(
                        FAMILY_WORDS.get(family, family), winner
                    )
                )

        if not clauses:
            continue

        sentence = "{}: {}.".format(key, "; ".join(clauses))

        if decision_material:
            candidate = (support.get("candidates") or {}).get(
                decision_material
            )

            if candidate and candidate["class_reliability"] in (
                reliability_module.WEAK,
            ):
                sentence += (
                    " Its answer for that class is discounted: measured "
                    "reliability is {}.".format(candidate["class_reliability"])
                )

        sentences.append(sentence)

    return sentences


def quality_sentences(evidence):
    """What the measurement itself supports, in plain terms."""
    sentences = []

    quality = evidence.get("quality") or {}
    hardware = (quality.get("hardware") or {}).get("status")
    normalization = (quality.get("normalization") or {}).get("status")
    reliability = evidence.get("channel_reliability") or {}

    total = reliability.get("features_total")
    raw_valid = reliability.get("raw_valid_total")
    normalized_valid = reliability.get("normalized_valid_total")

    if total:
        sentences.append(
            "{}/{} raw features are valid and {}/{} of them support a "
            "usable reflectance.".format(
                raw_valid, total, normalized_valid, total
            )
        )

    if hardware == "HARDWARE_QC_FAIL":
        sentences.append(
            "Hardware quality control failed, so no representation of "
            "this measurement is trustworthy."
        )

    elif normalization and normalization != "OK":
        sentences.append(
            "The raw counts are valid; it is the reference division that "
            "is poorly conditioned, so reflectance-based evidence carries "
            "less weight here than raw evidence."
        )

    return sentences


def class_sentences(evidence):
    """What the class distributions say, or why they say nothing yet."""
    class_analysis = evidence.get("class_analysis") or {}

    if not class_analysis.get("available"):
        return [
            "No class distributions exist yet, so how typical this sample "
            "is for any material could not be checked."
        ]

    nearest = class_analysis.get("nearest")
    entry = (class_analysis.get("per_material") or {}).get(nearest) or {}

    sentences = []

    if nearest:
        ratio = entry.get("within_class_ratio")

        if ratio is not None:
            sentences.append(
                "The nearest class distribution is {} and the sample sits "
                "{:.2f}x the furthest distance any verified member of that "
                "class has reached from its centroid.".format(nearest, ratio)
            )

        else:
            sentences.append(
                "The nearest class centroid is {}, which has too few "
                "independent measurements for a scatter to compare "
                "against.".format(nearest)
            )

    knn = class_analysis.get("knn")

    if knn:
        sentences.append(
            "Its {} nearest verified observations are {}.".format(
                knn["k"],
                ", ".join(
                    "{}".format(name) for name in sorted(knn["composition"])
                ),
            )
        )

    return sentences


def explain(decision, evidence, fusion, unknown_report):
    """
    The full explanation, assembled from the parts above.

    Deterministic: the same decision object always produces the same
    text, which is what makes it quotable in a report.
    """
    level = decision.get("level")

    parts = [LEVEL_SENTENCE.get(level, "")]

    if level == "KNOWN_MATERIAL" and decision.get("material"):
        separation = decision.get("separation") or {}

        parts.append(
            "{} leads the field with evidence strength {:.2f} and is "
            "{:.0%} clear of {}.".format(
                decision["material"],
                separation.get("leader_strength") or 0.0,
                separation.get("relative_margin") or 0.0,
                separation.get("runner_up") or "the runner-up",
            )
        )

    if level == "MATERIAL_FAMILY" and decision.get("family"):
        family = decision.get("family_evidence") or {}

        parts.append(
            "The {} family carries {} of the candidate evidence across "
            "{} member(s); no single member is far enough ahead of the "
            "others to be named.".format(
                decision["family"],
                _percent(family.get("leader_share")),
                len((decision.get("candidates") or [])),
            )
        )

    if level == "AMBIGUOUS_SET":
        parts.append(
            "The candidates listed are within one another's margin: {}."
            .format(
                ", ".join(
                    candidate["material"]
                    for candidate in decision.get("candidates") or []
                )
            )
        )

    if level == "UNKNOWN":
        for reason in (unknown_report.get("reasons") or [])[:3]:
            parts.append("{}: {}.".format(reason["code"], reason["detail"]))

    parts.extend(database_sentences(fusion, decision.get("material")))
    parts.extend(quality_sentences(evidence))
    parts.extend(class_sentences(evidence))

    mixture = decision.get("secondary_interpretations") or []

    if "MIXTURE_PLAUSIBLE" in mixture:
        parts.append(
            "A non-negative combination of library spectra describes the "
            "measurement better than any single one; the coefficients are "
            "spectral contributions and are not mass fractions."
        )

    parts.append(
        "This is a comparative spectral result, not a chemical "
        "identification, and the confidence level is a judgement about "
        "the structure of the evidence rather than a probability."
    )

    return " ".join(part for part in parts if part)
