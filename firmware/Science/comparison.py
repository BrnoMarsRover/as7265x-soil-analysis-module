"""
Distance to a CLASS, not to a point.

A single reference spectrum says where a material sat once. It says
nothing about how much a second jar of the same material, packed
differently, would differ - and that spread is what decides whether two
candidates are actually separable.

So as verified observations accumulate, a material stops being

    x_material

and becomes

    material: observations[], centroid, spread, covariance

with distances that know about the spread:

    nearest centroid          ||x - mu||               needs 1 observation
    standardized Euclidean    scaled by per-feature sd needs ~3
    Mahalanobis (shrunk)      full covariance          needs many
    k-nearest-neighbour       distance to real members needs 1

COVARIANCE WITH TWELVE OBSERVATIONS IS A TRAP

Mahalanobis needs the inverse of the covariance matrix. With 18 features
and three observations of a class, that matrix is singular: inverting it
produces enormous, meaningless distances that look authoritative. The
usual fix is shrinkage - pull the sample covariance towards a diagonal
target,

    S* = (1 - a) S + a * mu_trace * I

with the shrinkage `a` rising as the sample count falls. This module does
that, and it also REFUSES outright below a floor of independent
observations, falling back to standardized Euclidean and saying so. A
distance that cannot be estimated is reported as unavailable, never
guessed.

INDEPENDENT MEASUREMENTS, NOT SENSOR REPEATS

Three acquisitions of an undisturbed sample measure the sensor's noise.
Three acquisitions with the sample removed and repacked between them
measure the material's variability, which is perhaps ten times larger and
is the thing these statistics are supposed to describe. Only independent
measurements are counted towards `n_independent`, and every statistic
carries that number so nobody can mistake one for the other. §17.
"""

import math

from BD.channels import (
    AS7265X_18,
    AS7265X_54_MULTIILLUM,
    CHANNELS,
    project_to_18,
)
from Science import config, metrics

# Below this many INDEPENDENT observations a full covariance cannot be
# estimated at all, and Mahalanobis is refused rather than approximated.
MIN_OBSERVATIONS_FOR_COVARIANCE = 6

# Below this, per-feature variance is still too noisy to standardize by,
# so plain Euclidean is used.
MIN_OBSERVATIONS_FOR_VARIANCE = 3

# Floor on a per-feature standard deviation, in the feature's own units.
# Without it, a feature that happened to repeat exactly twice divides by
# zero and dominates every distance.
MIN_STDEV = 1e-6

# Shrinkage floor: even with plenty of observations, some pull towards
# the diagonal keeps the inverse stable.
MIN_SHRINKAGE = 0.05


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False

    return not (math.isnan(value) or math.isinf(value))


def _vector(spectrum, features):
    return [
        float(spectrum.get(feature))
        if _finite(spectrum.get(feature)) else None
        for feature in features
    ]


def _median(values):
    if not values:
        return None

    ordered = sorted(values)
    middle = len(ordered) // 2

    if len(ordered) % 2:
        return ordered[middle]

    return (ordered[middle - 1] + ordered[middle]) / 2.0


def build_class_statistics(observations, features=None,
                           n_independent=None):
    """
    Centroid, spread and covariance for one material.

    `observations` is a list of feature dicts, one per INDEPENDENT
    measurement. Every statistic records how many it was built from,
    because a centroid of one and a centroid of thirty are both called a
    centroid and are not the same claim.
    """
    features = tuple(features or CHANNELS)
    vectors = [_vector(entry, features) for entry in observations]

    count = len(vectors)
    n_independent = count if n_independent is None else int(n_independent)

    if not count:
        return {
            "features": list(features),
            "n_observations": 0,
            "n_independent": 0,
            "centroid": None,
            "usable": False,
            "reason": "no observations",
        }

    centroid = {}
    per_feature = {}

    for index, feature in enumerate(features):
        values = [
            vector[index] for vector in vectors if vector[index] is not None
        ]

        if not values:
            centroid[feature] = None
            per_feature[feature] = {
                "n": 0, "mean": None, "median": None, "stdev": None,
                "mad": None, "min": None, "max": None,
            }

            continue

        mean = sum(values) / float(len(values))
        median = _median(values)

        if len(values) > 1:
            variance = sum((value - mean) ** 2 for value in values) / float(
                len(values) - 1
            )
            stdev = math.sqrt(variance)
        else:
            stdev = None

        centroid[feature] = round(mean, 8)
        per_feature[feature] = {
            "n": len(values),
            "mean": round(mean, 8),
            "median": round(median, 8),
            "stdev": round(stdev, 8) if stdev is not None else None,
            "mad": round(_median([abs(v - median) for v in values]), 8),
            "min": round(min(values), 8),
            "max": round(max(values), 8),
        }

    statistics = {
        "features": list(features),
        "n_observations": count,
        "n_independent": n_independent,
        "centroid": centroid,
        "per_feature": per_feature,
        "usable": True,
        "supports_variance": n_independent >= MIN_OBSERVATIONS_FOR_VARIANCE,
        "supports_covariance": n_independent
        >= MIN_OBSERVATIONS_FOR_COVARIANCE,
    }

    statistics["within_class_distances"] = _within_class(
        vectors, centroid, features
    )

    if statistics["supports_covariance"]:
        statistics["covariance"] = _shrunk_covariance(
            vectors, centroid, features, n_independent
        )

    else:
        statistics["covariance"] = None
        statistics["covariance_refused"] = (
            "{} independent observation(s); a covariance over {} features "
            "needs at least {}. Estimating it anyway would produce a "
            "singular matrix and confident nonsense.".format(
                n_independent, len(features), MIN_OBSERVATIONS_FOR_COVARIANCE
            )
        )

    return statistics


def _within_class(vectors, centroid, features):
    """How far the class's own members sit from their centroid."""
    distances = []

    for vector in vectors:
        total = 0.0
        used = 0

        for index, feature in enumerate(features):
            value = vector[index]
            mean = centroid.get(feature)

            if value is None or mean is None:
                continue

            total += (value - mean) ** 2
            used += 1

        if used:
            distances.append(math.sqrt(total))

    if not distances:
        return None

    ordered = sorted(distances)

    return {
        "n": len(ordered),
        "min": round(ordered[0], 8),
        "median": round(_median(ordered), 8),
        "max": round(ordered[-1], 8),
        "distances": [round(value, 8) for value in ordered],
    }


def _shrunk_covariance(vectors, centroid, features, n_independent):
    """
    Sample covariance pulled towards a scaled identity.

    The shrinkage weight falls as observations accumulate, so the
    estimate becomes the sample covariance only when there is enough data
    to justify it. It never reaches zero: a little regularization keeps
    the inverse from amplifying the least-observed direction.
    """
    size = len(features)
    rows = []

    for vector in vectors:
        row = []

        for index, feature in enumerate(features):
            value = vector[index]
            mean = centroid.get(feature)

            row.append(
                0.0 if value is None or mean is None else value - mean
            )

        rows.append(row)

    if len(rows) < 2:
        return None

    divisor = float(len(rows) - 1)
    matrix = [[0.0] * size for _ in range(size)]

    for row in rows:
        for i in range(size):
            for j in range(size):
                matrix[i][j] += row[i] * row[j] / divisor

    trace = sum(matrix[i][i] for i in range(size))
    target = trace / size if size else 0.0

    # More observations, less shrinkage. The form is deliberately simple
    # and is labelled PROVISIONAL: a data-driven estimator (Ledoit-Wolf)
    # needs more observations than exist to estimate its own parameter.
    weight = max(MIN_SHRINKAGE, min(1.0, size / float(n_independent + size)))

    shrunk = [
        [
            (1.0 - weight) * matrix[i][j] + (weight * target if i == j else 0.0)
            for j in range(size)
        ]
        for i in range(size)
    ]

    return {
        "matrix": [[round(value, 10) for value in row] for row in shrunk],
        "shrinkage": round(weight, 6),
        "shrinkage_target": round(target, 10),
        "estimator": "diagonal-target shrinkage, PROVISIONAL",
        "n_independent": n_independent,
    }


def _invert(matrix):
    """
    Gauss-Jordan inverse with partial pivoting. Returns None if singular.

    Pure Python on purpose: the whole project has no numpy dependency,
    and an 18x18 inverse is 5832 multiply-adds.
    """
    size = len(matrix)
    augmented = [
        list(matrix[i]) + [1.0 if i == j else 0.0 for j in range(size)]
        for i in range(size)
    ]

    for column in range(size):
        pivot_row = max(
            range(column, size), key=lambda r: abs(augmented[r][column])
        )

        if abs(augmented[pivot_row][column]) < 1e-12:
            return None

        augmented[column], augmented[pivot_row] = (
            augmented[pivot_row], augmented[column]
        )

        pivot = augmented[column][column]

        for k in range(column, 2 * size):
            augmented[column][k] /= pivot

        for row in range(size):
            if row == column:
                continue

            factor = augmented[row][column]

            if factor == 0.0:
                continue

            for k in range(column, 2 * size):
                augmented[row][k] -= factor * augmented[column][k]

    return [row[size:] for row in augmented]


def nearest_centroid_distance(spectrum, statistics):
    """Plain Euclidean distance to the class centroid."""
    centroid = (statistics or {}).get("centroid")

    if not centroid:
        return None

    total = 0.0
    used = 0

    for feature in statistics["features"]:
        value = spectrum.get(feature)
        mean = centroid.get(feature)

        if not _finite(value) or mean is None:
            continue

        total += (value - mean) ** 2
        used += 1

    if not used:
        return None

    return math.sqrt(total)


def standardized_distance(spectrum, statistics):
    """
    Euclidean distance with each feature scaled by its own spread.

    A feature that varies a lot between jars of the same material should
    not count as much as one that repeats tightly, and this is the
    cheapest correct way to say so - it needs a variance per feature but
    no covariance, so it works with a handful of observations where
    Mahalanobis cannot.
    """
    if not statistics or not statistics.get("supports_variance"):
        return None

    per_feature = statistics.get("per_feature") or {}
    total = 0.0
    used = 0

    for feature in statistics["features"]:
        value = spectrum.get(feature)
        summary = per_feature.get(feature) or {}
        mean = summary.get("mean")
        stdev = summary.get("stdev")

        if not _finite(value) or mean is None or stdev is None:
            continue

        total += ((value - mean) / max(stdev, MIN_STDEV)) ** 2
        used += 1

    if not used:
        return None

    return math.sqrt(total / used)


def mahalanobis_distance(spectrum, statistics):
    """
    Full covariance distance, or None with the reason recorded upstream.

    Refused rather than approximated when the class has too few
    independent observations - see the module docstring.
    """
    covariance = (statistics or {}).get("covariance")

    if not covariance:
        return None

    features = statistics["features"]
    centroid = statistics["centroid"]

    difference = []

    for feature in features:
        value = spectrum.get(feature)
        mean = centroid.get(feature)

        if not _finite(value) or mean is None:
            return None

        difference.append(value - mean)

    inverse = _invert(covariance["matrix"])

    if inverse is None:
        return None

    total = 0.0

    for i, left in enumerate(difference):
        row = inverse[i]
        total += left * sum(row[j] * difference[j]
                            for j in range(len(difference)))

    if total < 0:
        # Numerically possible on a barely-positive-definite matrix.
        return None

    return math.sqrt(total)


def knn_evidence(spectrum, class_observations, k=3, features=None):
    """
    Distance to the nearest actual members, and who the neighbours were.

    Useful precisely where the parametric statistics are not: with three
    observations of a class there is no covariance, but "this sample is
    closer to a real bentonite measurement than to any other real
    measurement" is still evidence.
    """
    features = tuple(features or CHANNELS)
    neighbours = []

    for material, observations in class_observations.items():
        for index, observation in enumerate(observations):
            total = 0.0
            used = 0

            for feature in features:
                value = spectrum.get(feature)
                other = observation.get(feature)

                if not _finite(value) or not _finite(other):
                    continue

                total += (value - other) ** 2
                used += 1

            if used:
                neighbours.append({
                    "material": material,
                    "index": index,
                    "distance": math.sqrt(total),
                })

    if not neighbours:
        return None

    neighbours.sort(key=lambda entry: entry["distance"])
    nearest = neighbours[:max(1, int(k))]

    # How many of the k nearest neighbours each material won. A VOTE
    # TALLY, and named as one: called "composition" it reads as a claim
    # about what the sample is made of, and "basalt: 2, sand: 1" in a
    # stored record is exactly the kind of number an external reader
    # would turn into "two-thirds basalt". It is nothing of the sort.
    votes = {}

    for entry in nearest:
        votes[entry["material"]] = votes.get(entry["material"], 0) + 1

    return {
        "k": int(k),
        "nearest_material": nearest[0]["material"],
        "nearest_distance": round(nearest[0]["distance"], 8),
        "mean_k_distance": round(
            sum(entry["distance"] for entry in nearest) / len(nearest), 8
        ),
        "neighbour_votes": votes,
        "neighbours": [
            {
                "material": entry["material"],
                "distance": round(entry["distance"], 8),
            }
            for entry in nearest
        ],
        "population": len(neighbours),
    }


def compare_classes(spectrum, class_statistics, class_observations=None,
                    k=3):
    """
    Every available class distance, per material, with what was refused.

    A material whose statistics cannot support a metric reports that
    metric as None and says why. The decision layer must be able to tell
    "far away" from "not measurable", because they mean opposite things.
    """
    results = {}

    for material, statistics in class_statistics.items():
        entry = {
            "material": material,
            "n_independent": statistics.get("n_independent"),
            "centroid_distance": nearest_centroid_distance(
                spectrum, statistics
            ),
            "standardized_distance": standardized_distance(
                spectrum, statistics
            ),
            "mahalanobis_distance": mahalanobis_distance(
                spectrum, statistics
            ),
            "within_class": statistics.get("within_class_distances"),
            "unavailable": [],
        }

        if entry["standardized_distance"] is None:
            entry["unavailable"].append({
                "metric": "standardized_distance",
                "reason": "needs {} independent observations".format(
                    MIN_OBSERVATIONS_FOR_VARIANCE
                ),
            })

        if entry["mahalanobis_distance"] is None:
            entry["unavailable"].append({
                "metric": "mahalanobis_distance",
                "reason": statistics.get("covariance_refused")
                or "covariance not invertible",
            })

        # Where the sample falls relative to the class's own scatter.
        # This is the number that makes UNKNOWN possible: a sample twice
        # as far from the centroid as any member of the class has ever
        # been is not a member of the class, whatever its cosine says.
        within = statistics.get("within_class_distances")

        if within and entry["centroid_distance"] is not None:
            worst = within.get("max") or 0.0
            entry["within_class_ratio"] = (
                round(entry["centroid_distance"] / worst, 6)
                if worst > 0 else None
            )

        else:
            entry["within_class_ratio"] = None

        results[material] = entry

    ranked = sorted(
        (
            entry for entry in results.values()
            if entry["centroid_distance"] is not None
        ),
        key=lambda entry: entry["centroid_distance"],
    )

    report = {
        "per_material": results,
        "ranked": [entry["material"] for entry in ranked],
        "nearest": ranked[0]["material"] if ranked else None,
        "nearest_distance": (
            ranked[0]["centroid_distance"] if ranked else None
        ),
        "runner_up": ranked[1]["material"] if len(ranked) > 1 else None,
        "runner_up_distance": (
            ranked[1]["centroid_distance"] if len(ranked) > 1 else None
        ),
        "classes": len(results),
    }

    if report["nearest_distance"] is not None and report["runner_up_distance"]:
        report["margin"] = round(
            report["runner_up_distance"] - report["nearest_distance"], 8
        )
        report["margin_ratio"] = round(
            report["runner_up_distance"]
            / max(report["nearest_distance"], 1e-9), 6
        )

    else:
        report["margin"] = None
        report["margin_ratio"] = None

    if class_observations:
        report["knn"] = knn_evidence(spectrum, class_observations, k)

    else:
        report["knn"] = None

    return report


# ======================================================================
# WHERE THE PER-DATABASE COMPARISON LIVES
# ======================================================================
#
# Not here. `pipeline.build()` compares a measurement against DB1, DB2
# and DB3 independently, under the calibration each one is entitled to
# see, and keeps the three results apart for the Decision Model to
# fuse.
#
# A second implementation used to live in this file: it re-ran the same
# per-database comparison, then reached its OWN consensus and its OWN
# confidence. Two answers, produced by two code paths, stored in one
# record - and nothing to say which was right when they disagreed. The
# evidence path kept its evidence separate and let the Decision Model
# combine it, which is the architecture; this one combined first.
#
# What this module owns now is the CLASS side: how far a measurement is
# from a material's distribution, which is a different question from
# how far it is from any individual reference and needs the class's own
# scatter to answer.

