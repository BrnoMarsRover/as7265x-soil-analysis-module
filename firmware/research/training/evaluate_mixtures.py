"""
Score unmixing against mixtures that were actually weighed.

    py firmware/research/training/evaluate_mixtures.py
    py firmware/research/training/evaluate_mixtures.py --material "Iron(III) Oxide Red"

THE QUESTION THIS ANSWERS

Every mixture report the system prints says the same careful thing:

    Spectral contribution is NOT mass fraction. Converting one to the
    other needs prepared mixtures of known mass, which do not exist for
    this instrument.

This is the program that consumes those mixtures once they exist. It
takes every PREPARED_MIXTURE in the learning history, runs the unmixing
over the raw counts that were stored with it, and compares what the
algorithm estimated against what the operator weighed.

    detection   was the spiked material in the estimate at all, and at
                what fraction does it stop being detected
    ordering    when two materials were mixed, did the estimate rank
                them in the order they were weighed
    quantity    how does the estimated contribution track the prepared
                mass fraction - and is that relationship steady enough
                to invert

TWO THINGS IT WILL NOT DO
-------------------------
It will not fit a conversion on two points, and it will not report a
conversion at all unless the relationship survives leaving each mixture
out in turn. A slope fitted to three points that were all prepared on
the same afternoon, with the same soil, at the same distance, describes
that afternoon.

It will also not touch DB1, DB2, DB3 or the ground truth. It reads the
history and writes a report; promoting anything is a separate,
deliberate act through the model registry.

WHY THE MATRIX IS PART OF THE SCORE
-----------------------------------
A component mixed into ordinary soil is being looked for AGAINST that
soil, which has no reference spectrum. The unmixing can only build the
sample out of library materials, so the matrix has to come out as some
combination of whatever library material happens to resemble it - and
that combination competes with the thing being looked for. Every result
here is therefore reported per matrix, and a detection limit measured
against one soil says nothing about another.
"""

import argparse
import json
import sys
from pathlib import Path

FIRMWARE_ROOT = Path(__file__).resolve().parents[2]

if str(FIRMWARE_ROOT) not in sys.path:
    sys.path.insert(0, str(FIRMWARE_ROOT))

from BD.calibrations import read_legacy_calibration                  # noqa: E402
from BD.channels import CHANNELS                              # noqa: E402
from BD.decision_learning import (                            # noqa: E402
    DecisionLearningStore,
    ROLE_COMPONENT,
    ROLE_MATRIX,
    TRAINABLE_LEVELS,
)
from BD.registry import DatabaseRegistry                      # noqa: E402
from Science import preprocessing                             # noqa: E402
from research import mixture as unmixing                      # noqa: E402

# How many mixtures of one material, at DIFFERENT prepared fractions, it
# takes before a contribution-to-mass relationship is worth fitting.
# Three points give a slope and something to check it against; two give
# a line through two points, which fits perfectly and predicts nothing.
MIN_POINTS_FOR_QUANTITY = 3

# A component is "detected" if the unmixing gave it at least this much
# contribution. Deliberately low: the question here is whether the
# material appears at all, and where it stops appearing.
DETECTION_THRESHOLD = 0.02

# How many library materials the unmixing may fit at once. Kept small
# for the reason research/mixture.py explains at length: 18 numbers can
# be reconstructed almost perfectly by enough free spectra.
CANDIDATE_DEPTH = 6

REPORT_VERSION = "mixture-eval-v1"


def _reflectance(observation, dark, white):
    """The 18 white-illumination reflectances of one stored observation."""
    raw = (observation.get("raw") or {}).get("white")

    if not raw:
        return None

    normalized = preprocessing.normalize(raw, dark, white)

    # An undefined channel is dropped, not zeroed. The unmixing fits
    # what was measured; a zero there would be a measurement nobody made
    # and would pull the fit towards whatever library spectrum is
    # darkest on that channel.
    return {
        channel: value
        for channel, value in normalized.items()
        if value is not None
    }


def evaluate(store, registry=None, material=None, levels=None):
    """Score every prepared mixture the history holds."""
    registry = registry or DatabaseRegistry()
    library = registry.get("DB1")

    if library is None or not library.ready:
        return {
            "status": "NO_LIBRARY",
            "reason": "DB1 is not loaded, so there is nothing to unmix "
                      "against.",
        }

    references = read_legacy_calibration()
    dark, white = references.dark, references.white

    mixtures = store.mixture_training_set(
        levels=levels or TRAINABLE_LEVELS, require_fractions=True
    )

    if material:
        mixtures = [
            record for record in mixtures
            if any(component["material_key"] == material
                   for component in record["components"])
        ]

    if not mixtures:
        return {
            "status": "NO_MIXTURES",
            "reason": "No prepared mixture with recorded proportions is "
                      "on file yet. Mix a known material into a known "
                      "matrix, measure it, and save it from the sensor "
                      "test as a KNOWN PREPARED MIXTURE.",
            "how_many_are_needed": MIN_POINTS_FOR_QUANTITY,
        }

    channels = list(CHANNELS)
    results = []

    for record in mixtures:
        measured = _reflectance(record, dark, white)

        if not measured:
            results.append({
                "measurement_id": record["measurement_id"],
                "status": "NO_WHITE_SPECTRUM",
            })

            continue

        prepared = {
            component["material_key"]: component["prepared_mass_fraction"]
            for component in record["components"]
            if component["role"] == ROLE_COMPONENT
        }

        matrix = next(
            (
                component.get("matrix_label")
                for component in record["components"]
                if component["role"] == ROLE_MATRIX
            ),
            None,
        )

        # The candidate set is the whole library truncated, not the
        # prepared components. Handing the unmixing the answer and then
        # scoring how well it found it would measure nothing at all.
        candidates = _rank_candidates(measured, library, channels)

        estimate = unmixing.estimate(
            measured, library.materials, candidates, channels=channels
        )

        estimated = {
            component["material"]: component["spectral_contribution"]
            for component in estimate.get("components") or []
        }

        per_component = []

        for name, fraction in sorted(prepared.items()):
            contribution = estimated.get(name, 0.0)

            per_component.append({
                "material_key": name,
                "prepared_mass_fraction": fraction,
                "spectral_contribution": contribution,
                "detected": contribution >= DETECTION_THRESHOLD,
                "rank": _rank_of(name, estimate.get("components") or []),
            })

        results.append({
            "measurement_id": record["measurement_id"],
            "status": estimate.get("status"),
            "matrix": matrix,
            "sample_context": record.get("sample_context"),
            "components": per_component,
            "reconstruction_rmse": estimate.get("reconstruction_rmse"),
            "endmembers": estimate.get("endmembers"),
            "ordering_correct": _ordering_correct(per_component),
        })

    return {
        "status": "EVALUATED",
        "report_version": REPORT_VERSION,
        "mixtures_scored": len(results),
        "detection_threshold": DETECTION_THRESHOLD,
        "results": results,
        "by_material": _summarise(results),
    }


def _rank_candidates(measured, library, channels):
    """The nearest library materials by cosine, as a candidate set."""
    from Science import metrics

    scored = []

    for name, spectrum in library.materials.items():
        # metrics.paired() and metrics.cosine() are the Science layer's
        # own pairing and angular metric. An earlier revision called a
        # `metrics.cosine_similarity(dict, dict, keys=...)` that has
        # never existed on this module, caught the AttributeError and
        # silently scored every candidate with a private copy of the
        # formula instead - a shortlist computed outside Science.
        pairs = metrics.paired(measured, spectrum, channels)

        if len(pairs) < 3:
            continue

        score = metrics.cosine(pairs)

        scored.append((score if score is not None else 0.0, name))

    scored.sort(reverse=True)

    return [name for _, name in scored[:CANDIDATE_DEPTH]]


def _rank_of(name, components):
    for index, component in enumerate(components, start=1):
        if component["material"] == name:
            return index

    return None


def _ordering_correct(per_component):
    """
    Did the estimate rank the components the way they were weighed?

    Undefined for a single component - there is no order to get right -
    and reported as None rather than as a pass, because counting it as a
    success would inflate the score with cases that tested nothing.
    """
    weighed = [
        component for component in per_component
        if component["rank"] is not None
    ]

    if len(weighed) < 2:
        return None

    by_mass = sorted(
        per_component, key=lambda c: -c["prepared_mass_fraction"]
    )
    by_estimate = sorted(
        weighed, key=lambda c: c["rank"]
    )

    return [c["material_key"] for c in by_mass][:len(by_estimate)] == [
        c["material_key"] for c in by_estimate
    ]


def _summarise(results):
    """Per material: was it found, and does contribution track mass."""
    points = {}

    for result in results:
        for component in result.get("components") or []:
            entry = points.setdefault(component["material_key"], {
                "observations": 0,
                "detected": 0,
                "points": [],
                "matrices": set(),
                "lowest_detected_fraction": None,
                "highest_missed_fraction": None,
            })

            entry["observations"] += 1
            entry["points"].append((
                component["prepared_mass_fraction"],
                component["spectral_contribution"],
            ))

            if result.get("matrix"):
                entry["matrices"].add(result["matrix"])

            fraction = component["prepared_mass_fraction"]

            if component["detected"]:
                entry["detected"] += 1

                if (entry["lowest_detected_fraction"] is None
                        or fraction < entry["lowest_detected_fraction"]):
                    entry["lowest_detected_fraction"] = fraction

            elif (entry["highest_missed_fraction"] is None
                    or fraction > entry["highest_missed_fraction"]):
                entry["highest_missed_fraction"] = fraction

    summary = {}

    for name, entry in points.items():
        distinct = {fraction for fraction, _ in entry["points"]}

        summary[name] = {
            "observations": entry["observations"],
            "detected": entry["detected"],
            "distinct_fractions": len(distinct),
            "matrices": sorted(entry["matrices"]),
            "lowest_detected_fraction": entry["lowest_detected_fraction"],
            "highest_missed_fraction": entry["highest_missed_fraction"],
            "quantity": _quantity_fit(entry["points"]),
        }

    return summary


def _quantity_fit(points):
    """
    Does spectral contribution track prepared mass, well enough to invert?

    Reports a least-squares line and a leave-one-out check. The check is
    the part that matters: a slope that changes when any single mixture
    is removed was describing that mixture, not the material.
    """
    if len(points) < MIN_POINTS_FOR_QUANTITY:
        return {
            "status": "INSUFFICIENT_DATA",
            "points": len(points),
            "needed": MIN_POINTS_FOR_QUANTITY,
            "reason": "A contribution-to-mass relationship cannot be "
                      "estimated from {} point(s). Prepare the same "
                      "material at more than one fraction."
                      .format(len(points)),
        }

    distinct = {fraction for fraction, _ in points}

    if len(distinct) < MIN_POINTS_FOR_QUANTITY:
        return {
            "status": "INSUFFICIENT_SPREAD",
            "points": len(points),
            "distinct_fractions": len(distinct),
            "reason": "{} mixtures but only {} different fraction(s). "
                      "Repeats measure the instrument's scatter; they "
                      "cannot show how the estimate responds to "
                      "concentration.".format(len(points), len(distinct)),
        }

    fit = _line(points)

    if fit is None:
        return {
            "status": "DEGENERATE",
            "reason": "the prepared fractions are all equal",
        }

    # Leave one out. Every point removed in turn, the line refitted, and
    # the spread of the resulting slopes reported. A relationship that
    # survives this is at least not the artefact of a single mixture.
    slopes = []

    for index in range(len(points)):
        reduced = points[:index] + points[index + 1:]

        if len({fraction for fraction, _ in reduced}) < 2:
            continue

        left_out = _line(reduced)

        if left_out is not None:
            slopes.append(left_out["slope"])

    stability = None

    if len(slopes) >= 2:
        mean = sum(slopes) / len(slopes)
        spread = max(slopes) - min(slopes)
        stability = {
            "slopes": [round(value, 4) for value in slopes],
            "spread": round(spread, 4),
            "relative_spread": (
                round(abs(spread / mean), 4) if mean else None
            ),
        }

    return {
        "status": "FITTED",
        "points": len(points),
        "distinct_fractions": len(distinct),
        "slope": round(fit["slope"], 4),
        "intercept": round(fit["intercept"], 4),
        "r_squared": round(fit["r_squared"], 4),
        "leave_one_out": stability,
        "caveat": "A slope is not a concentration model. It says how "
                  "this estimate responded to this material in this "
                  "matrix at these distances, and it is only worth "
                  "inverting if leave_one_out shows it barely moves.",
    }


def _line(points):
    """Least-squares fit of contribution against prepared fraction."""
    n = len(points)
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n

    covariance = sum((x - mean_x) * (y - mean_y) for x, y in points)
    variance = sum((x - mean_x) ** 2 for x, _ in points)

    if variance == 0:
        return None

    slope = covariance / variance
    intercept = mean_y - slope * mean_x

    total = sum((y - mean_y) ** 2 for _, y in points)
    residual = sum(
        (y - (slope * x + intercept)) ** 2 for x, y in points
    )

    return {
        "slope": slope,
        "intercept": intercept,
        "r_squared": 1.0 - residual / total if total else 1.0,
    }


# ----------------------------------------------------------------------
# reporting
# ----------------------------------------------------------------------

def print_report(report):
    print("MIXTURE EVALUATION")
    print()

    if report["status"] != "EVALUATED":
        print("  {}".format(report["status"]))
        print()
        print("  {}".format(report["reason"]))

        return

    print("Mixtures scored: {}".format(report["mixtures_scored"]))
    print("Detected at:     contribution >= {}".format(
        report["detection_threshold"]))
    print()

    print("PER MIXTURE")
    print()
    print("  {:<22} {:<28} {:>9} {:>9} {:>6}".format(
        "measurement", "component", "prepared", "estimate", "found"))

    for result in report["results"]:
        for component in result.get("components") or []:
            print("  {:<22} {:<28} {:>8.1f}% {:>9.4f} {:>6}".format(
                result["measurement_id"][:22],
                component["material_key"][:28],
                component["prepared_mass_fraction"] * 100.0,
                component["spectral_contribution"],
                "yes" if component["detected"] else "NO",
            ))

        if result.get("matrix"):
            print("  {:<22} {:<28} {:>9} {:>9} {:>6}".format(
                "", "in: " + result["matrix"][:24], "", "", ""))

    print()
    print("PER MATERIAL")
    print()

    for name, entry in sorted(report["by_material"].items()):
        print("  {}".format(name))
        print("    found in {} of {} mixture(s)".format(
            entry["detected"], entry["observations"]))

        if entry["lowest_detected_fraction"] is not None:
            print("    lowest fraction still found: {:.1f}%".format(
                entry["lowest_detected_fraction"] * 100.0))

        if entry["highest_missed_fraction"] is not None:
            print("    highest fraction MISSED:     {:.1f}%".format(
                entry["highest_missed_fraction"] * 100.0))

        if entry["matrices"]:
            print("    matrices: {}".format(", ".join(entry["matrices"])))

        quantity = entry["quantity"]

        if quantity["status"] == "FITTED":
            print("    contribution = {:.3f} x mass + {:.3f}  "
                  "(r2 {:.3f})".format(
                      quantity["slope"], quantity["intercept"],
                      quantity["r_squared"]))

            stability = quantity["leave_one_out"]

            if stability and stability["relative_spread"] is not None:
                print("    leave-one-out slope spread: {:.1%}".format(
                    stability["relative_spread"]))

        else:
            print("    quantity: {} - {}".format(
                quantity["status"], quantity.get("reason", "")))

        print()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Score unmixing against prepared mixtures."
    )
    parser.add_argument("--material", default=None,
                        help="only mixtures containing this material")
    parser.add_argument("--database", default=None,
                        help="learning database")
    parser.add_argument("--json", action="store_true",
                        help="emit the full report as JSON")

    arguments = parser.parse_args(argv)

    store = DecisionLearningStore(arguments.database)
    report = evaluate(store, material=arguments.material)

    if arguments.json:
        print(json.dumps(report, indent=2, default=str))

        return 0

    print_report(report)

    return 0


if __name__ == "__main__":
    sys.exit(main())
