"""
Measure what the sensor can actually tell apart, per material class.

    py firmware/research/analyse_discriminability.py

Reads  firmware/BD/data/DB3.json
Writes the same file, adding a `discriminability` block.

WHY THIS EXISTS
---------------
DB3 confidently names a nearest material for every sample, including for
classes the AS7265x cannot distinguish at all. Left alone, that produces
a confident wrong answer: chalk matched to anhydrite, talc matched to
anhydrite, saltpetre matched to anhydrite.

The fix is not to distrust DB3 uniformly - it identifies iron oxides
perfectly well. The fix is to know, per class, whether a match means
anything, and to feed that into the confidence model.

THE MEASURE
-----------
Leave-one-out nearest-neighbour retrieval within DB3, scored two ways:

    RECALL     of the spectra that ARE class X, how many find class X?
    PRECISION  of the spectra that ANSWER class X, how many really are?

Precision is the one the confidence model needs. The question at runtime
is not "would this class be found if present" but "DB3 has just answered
class X - should I believe it". Those differ sharply here: a class can be
hard to find yet trustworthy when named, or easy to name and usually
wrong. `sulfate` is the second kind, and it is why chalk, talc and
saltpetre all came back as anhydrite.

Margin is reported alongside: the gap between the best same-class score
and the best other-class score. A class can retrieve correctly by a hair,
which is not the same as retrieving robustly.

SMALL CLASSES
-------------
With 84 spectra over 23 classes, most classes have a handful of members,
and leave-one-out is noisy at that size. A class with three members where
one is the only example of its mineral CANNOT retrieve itself - the two
nearest are necessarily something else. That is a property of the library,
not of the sensor, so classes below MIN_MEMBERS are rated
INSUFFICIENT_DATA rather than WEAK, and the confidence model treats the
two differently.

The comparison uses the production ranking path, so the numbers describe
the system as it actually behaves, not an idealised version of it.

NOT HARDCODED
-------------
These numbers are derived from DB3 and rewritten whenever DB3 is rebuilt.
If the library grows, or the projection model is replaced with the
manufacturer's real response curves, re-running this updates what the
confidence model believes. Nothing is pinned by hand.
"""

import collections
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

FIRMWARE_ROOT = Path(__file__).resolve().parent.parent

if str(FIRMWARE_ROOT) not in sys.path:
    sys.path.insert(0, str(FIRMWARE_ROOT))

from BD import config                       # noqa: E402
from Measurements import metrics            # noqa: E402

# A class needs at least this many members before retrieval accuracy
# means anything. Below it the answer is "not enough data", never a
# flattering guess.
MIN_MEMBERS = 3

# Precision bands. Provisional, but DERIVED: they say how often DB3 must
# be right when it names a class before that answer is worth acting on.
STRONG = 0.70
MODERATE = 0.40

LEVEL_STRONG = "STRONG"
LEVEL_MODERATE = "MODERATE"
LEVEL_WEAK = "WEAK"
LEVEL_INSUFFICIENT = "INSUFFICIENT_DATA"

ANALYSIS_VERSION = "loo-nn-v1"


def classify(precision, members, answered):
    """
    Rate a class on the PRECISION of answers naming it.

    A class nobody ever answers has no precision to measure, and a class
    with too few members cannot be assessed by leave-one-out at all. Both
    are INSUFFICIENT_DATA - unrated, not rated badly.
    """
    if members < MIN_MEMBERS or answered < MIN_MEMBERS:
        return LEVEL_INSUFFICIENT

    if precision >= STRONG:
        return LEVEL_STRONG

    if precision >= MODERATE:
        return LEVEL_MODERATE

    return LEVEL_WEAK


def analyse(materials, classes):
    """
    Leave-one-out retrieval per class, using the production ranking.

    Returns {class: {members, retrieval_accuracy, margin, level, ...}}.
    """
    names = sorted(materials)
    by_class = collections.defaultdict(list)

    for name in names:
        material_class = classes.get(name)

        if material_class:
            by_class[material_class].append(name)

    hits = collections.Counter()
    totals = collections.Counter()
    answered = collections.Counter()
    answered_correct = collections.Counter()
    margins = collections.defaultdict(list)
    confusions = collections.defaultdict(collections.Counter)

    for name in names:
        own_class = classes.get(name)

        if not own_class:
            continue

        library = {
            other: materials[other] for other in names if other != name
        }

        ranked = metrics.compare_all(materials[name], library)

        if not ranked:
            continue

        best = ranked[0]
        best_class = classes.get(best["material"])

        totals[own_class] += 1

        if best_class:
            answered[best_class] += 1

        if best_class == own_class:
            hits[own_class] += 1

            if best_class:
                answered_correct[best_class] += 1
        else:
            confusions[own_class][best_class or "unclassified"] += 1

        # Margin: how far the best same-class candidate sits from the best
        # different-class one, in cosine points. Negative means the wrong
        # class actually scored higher.
        same = next(
            (entry for entry in ranked
             if classes.get(entry["material"]) == own_class), None
        )
        other = next(
            (entry for entry in ranked
             if classes.get(entry["material"]) != own_class), None
        )

        if same and other:
            same_score = same.get("cosine_similarity_percent")
            other_score = other.get("cosine_similarity_percent")

            if same_score is not None and other_score is not None:
                margins[own_class].append(same_score - other_score)

    report = {}

    for material_class, members in sorted(by_class.items()):
        total = totals[material_class]
        accuracy = (hits[material_class] / total) if total else 0.0
        margin_values = margins[material_class]
        mean_margin = (
            sum(margin_values) / len(margin_values) if margin_values else None
        )

        times_answered = answered[material_class]
        precision = (
            answered_correct[material_class] / times_answered
            if times_answered else 0.0
        )

        report[material_class] = {
            "members": len(members),
            "evaluated": total,
            "times_answered": times_answered,
            "precision": round(precision, 3),
            "recall": round(accuracy, 3),
            "retrieval_accuracy": round(accuracy, 3),
            "mean_margin_percent": (
                round(mean_margin, 3) if mean_margin is not None else None
            ),
            "level": classify(precision, len(members), times_answered),
            "confused_with": dict(
                confusions[material_class].most_common(3)
            ),
        }

    return report


def main():
    path = config.DB3_FILE

    if not path.exists():
        print("DB3 does not exist; build it first")

        return 1

    document = json.loads(path.read_text(encoding="utf-8"))
    entries = document.get("materials") or {}

    if not entries:
        print("DB3 has no materials")

        return 1

    materials = {}
    classes = {}

    for name, entry in entries.items():
        spectrum = {
            channel: cell.get("reflectance_as_supplied")
            for channel, cell in (entry.get("channels") or {}).items()
            if cell.get("reflectance_as_supplied") is not None
        }

        if spectrum:
            materials[name] = spectrum
            classes[name] = entry.get("material_class")

    report = analyse(materials, classes)

    print("=" * 74)
    print("DB3 DISCRIMINABILITY - leave-one-out nearest-neighbour retrieval")
    print("=" * 74)
    print("{:<24} {:>4} {:>7} {:>9} {:>7} {:>8}  {}".format(
        "class", "n", "answers", "precision", "recall", "margin", "level"))
    print("-" * 74)

    for material_class, values in sorted(
        report.items(), key=lambda item: (-item[1]["precision"],
                                          -item[1]["members"])
    ):
        print("{:<24} {:>4} {:>7} {:>9.0%} {:>7.0%} {:>8}  {}".format(
            material_class,
            values["members"],
            values["times_answered"],
            values["precision"],
            values["recall"],
            ("{:+.2f}".format(values["mean_margin_percent"])
             if values["mean_margin_percent"] is not None else "-"),
            values["level"],
        ))

    usable = [
        name for name, values in report.items()
        if values["level"] in (LEVEL_STRONG, LEVEL_MODERATE)
    ]

    print()
    print("classes whose DB3 match is worth acting on: {} of {}".format(
        len(usable), len(report)))
    print("  {}".format(", ".join(sorted(usable)) or "none"))

    document["discriminability"] = {
        "analysis_version": ANALYSIS_VERSION,
        "method": "leave-one-out nearest-neighbour retrieval within DB3, "
                  "using the production combined-family ranking. Rated on "
                  "PRECISION: of the answers naming a class, how many were "
                  "right.",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "min_members_for_rating": MIN_MEMBERS,
        "thresholds": {"strong": STRONG, "moderate": MODERATE},
        "threshold_status": "PROVISIONAL - derived from DB3 retrieval, not "
                            "validated against physical measurements",
        "note": "Measured, not assumed. Regenerate whenever DB3 or the "
                "projection model changes: "
                "py firmware/research/analyse_discriminability.py",
        "by_class": report,
    }

    path.write_text(json.dumps(document, indent=2), encoding="utf-8")

    print()
    print("written into {}".format(path.name))

    return 0


if __name__ == "__main__":
    sys.exit(main())
