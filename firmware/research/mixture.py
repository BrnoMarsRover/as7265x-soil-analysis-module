"""
Mixture analysis — can this spectrum be explained by several materials?

Classification asks "which single reference is this closest to". That is
a different question from "could this be a mixture", and answering the
first does not answer the second. This module handles the second, and it
is deliberately conservative about what the answer means.

WHAT THE COEFFICIENTS ARE NOT
-----------------------------
They are NOT mass fractions, weight percent, or chemical concentration.

For a diffuse-reflectance instrument with 18 sparse bands, a linear
combination that reconstructs the spectrum is a SPECTRAL CONTRIBUTION
ESTIMATE. Real particulate mixing is not linear — grain size, packing
and multiple scattering all break the assumption — so a coefficient of
0.3 does not license the claim "30% of this sample is hematite".

Turning these numbers into concentration requires a validated calibration
against physically prepared mixtures of known mass. No such calibration
exists, so the field is named `spectral_contribution` and the report says
so every time.

OVERFITTING
-----------
18 measurements can be reconstructed almost perfectly by a free
combination of 20 library spectra, and the result would be meaningless.
So the candidate set is capped (MAX_ENDMEMBERS), candidates are screened
for near-collinearity, and the result reports how much the reconstruction
actually improved over the best single material. If adding a second
component barely helps, the honest answer is that the sample looks like
one material.
"""

import math

from BD.channels import CHANNELS
from Science import config


class MixtureError(Exception):
    """The mixture problem could not be posed or solved."""

    def __init__(self, code, message, details=None):
        super().__init__(message)

        self.code = code
        self.message = message
        self.details = details or {}


# ----------------------------------------------------------------------
# linear algebra
#
# Small dense problems only: at most 18 equations and MAX_ENDMEMBERS
# unknowns. Pure Python is fast enough and avoids pulling a numerical
# stack onto a project that does not otherwise need one.
# ----------------------------------------------------------------------

def _solve_symmetric(matrix, vector):
    """
    Solve a small symmetric positive-definite system by Gaussian
    elimination with partial pivoting.

    Returns None when the system is singular — which is the signal that
    the chosen endmembers are collinear and the mixture is not
    identifiable, not something to paper over with a pseudo-inverse.
    """
    size = len(vector)
    augmented = [list(row) + [vector[index]]
                 for index, row in enumerate(matrix)]

    for column in range(size):
        pivot_row = max(
            range(column, size),
            key=lambda row: abs(augmented[row][column]),
        )

        if abs(augmented[pivot_row][column]) < 1e-12:
            return None

        augmented[column], augmented[pivot_row] = (
            augmented[pivot_row], augmented[column]
        )

        pivot = augmented[column][column]

        for row in range(column + 1, size):
            factor = augmented[row][column] / pivot

            if factor == 0.0:
                continue

            for col in range(column, size + 1):
                augmented[row][col] -= factor * augmented[column][col]

    solution = [0.0] * size

    for row in range(size - 1, -1, -1):
        total = augmented[row][size]

        for col in range(row + 1, size):
            total -= augmented[row][col] * solution[col]

        solution[row] = total / augmented[row][row]

    return solution


def _least_squares(columns, target):
    """Unconstrained least squares via the normal equations."""
    size = len(columns)

    gram = [
        [sum(a * b for a, b in zip(columns[i], columns[j]))
         for j in range(size)]
        for i in range(size)
    ]
    projection = [
        sum(a * b for a, b in zip(columns[i], target))
        for i in range(size)
    ]

    return _solve_symmetric(gram, projection)


def nnls(columns, target, max_iterations=64):
    """
    Non-negative least squares: minimise ||Ax - b|| subject to x >= 0.

    Lawson-Hanson active set. `columns` is the endmember matrix given as
    a list of columns (one per endmember), `target` is the measured
    spectrum. Returns coefficients, or None if the problem is singular.

    Non-negativity matters physically: a negative contribution would mean
    a material removed light from the mixture, which is not a thing a
    reflectance mixture can do.
    """
    count = len(columns)

    if count == 0:
        return None

    coefficients = [0.0] * count
    passive = []

    for _iteration in range(max_iterations):
        residual = [
            target[row] - sum(
                coefficients[index] * columns[index][row]
                for index in range(count)
            )
            for row in range(len(target))
        ]

        gradient = [
            sum(a * b for a, b in zip(columns[index], residual))
            for index in range(count)
        ]

        candidates = [
            index for index in range(count)
            if index not in passive and gradient[index] > 1e-10
        ]

        if not candidates:
            break

        entering = max(candidates, key=lambda index: gradient[index])
        passive.append(entering)

        # Inner loop: solve on the passive set, drop anything negative.
        for _inner in range(max_iterations):
            solution = _least_squares(
                [columns[index] for index in passive], target
            )

            if solution is None:
                passive.remove(entering)

                if not passive:
                    return None

                break

            if all(value > 0 for value in solution):
                for slot, index in enumerate(passive):
                    coefficients[index] = solution[slot]

                break

            # Move as far toward the new solution as non-negativity allows.
            ratios = [
                coefficients[index] / (coefficients[index] - solution[slot])
                for slot, index in enumerate(passive)
                if solution[slot] <= 0
                and coefficients[index] != solution[slot]
            ]

            step = min(ratios) if ratios else 0.0

            for slot, index in enumerate(passive):
                coefficients[index] += step * (
                    solution[slot] - coefficients[index]
                )

            passive = [
                index for index in passive
                if coefficients[index] > 1e-12
            ]

            if not passive:
                break

    return coefficients


# ----------------------------------------------------------------------
# the mixture problem
# ----------------------------------------------------------------------

def _vector(spectrum, channels):
    return [float(spectrum.get(channel, 0.0) or 0.0) for channel in channels]


def _rmse(a, b):
    return math.sqrt(
        sum((x - y) ** 2 for x, y in zip(a, b)) / float(len(a))
    )


def _collinear(first, second):
    """Cosine between two endmembers, for redundancy screening."""
    dot = sum(a * b for a, b in zip(first, second))
    norm_a = math.sqrt(sum(a * a for a in first))
    norm_b = math.sqrt(sum(b * b for b in second))

    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0

    return dot / (norm_a * norm_b)


def select_endmembers(candidates, library, channels):
    """
    Trim a candidate list to a usable, non-redundant endmember set.

    Two library spectra that are 0.999 similar carry the same information;
    fitting both makes the solution unstable and the split between them
    arbitrary. The first (better-ranked) one is kept.
    """
    selected = []
    vectors = []

    for name in candidates:
        spectrum = library.get(name)

        if spectrum is None:
            continue

        vector = _vector(spectrum, channels)

        if all(value == 0.0 for value in vector):
            continue

        if any(
            _collinear(vector, existing) > config.MIXTURE_COLLINEARITY_LIMIT
            for existing in vectors
        ):
            continue

        selected.append(name)
        vectors.append(vector)

        if len(selected) >= config.MIXTURE_MAX_ENDMEMBERS:
            break

    return selected, vectors


def estimate(measured, library, candidates, channels=None,
             sum_to_one=None):
    """
    Estimate the spectral contributions of a small candidate set.

    `candidates` should come from the classification ranking — the whole
    library is never fitted at once. Returns a result dictionary that is
    explicit about what it does and does not claim.
    """
    channels = list(channels or CHANNELS)

    if sum_to_one is None:
        sum_to_one = config.MIXTURE_SUM_TO_ONE

    selected, vectors = select_endmembers(candidates, library, channels)

    if len(selected) < 2:
        return {
            "status": "NOT_APPLICABLE",
            "reason": "fewer than two independent candidate endmembers",
            "endmembers": selected,
            "components": [],
            "interpretation": "Nothing to unmix: the candidate set does "
                              "not contain two spectrally distinct "
                              "materials.",
        }

    target = _vector(measured, channels)

    if all(value == 0.0 for value in target):
        return {
            "status": "NOT_APPLICABLE",
            "reason": "measured spectrum is all zero",
            "endmembers": selected,
            "components": [],
        }

    coefficients = nnls(vectors, target)

    if coefficients is None:
        return {
            "status": "UNSTABLE",
            "reason": "the endmember matrix is singular; the mixture is "
                      "not identifiable from these candidates",
            "endmembers": selected,
            "components": [],
            "interpretation": "The candidate spectra are too similar to "
                              "separate. Reported as unstable rather than "
                              "as a fitted answer.",
        }

    reconstruction = [
        sum(
            coefficients[index] * vectors[index][row]
            for index in range(len(selected))
        )
        for row in range(len(channels))
    ]

    residual = [t - r for t, r in zip(target, reconstruction)]
    mixture_rmse = _rmse(target, reconstruction)

    # Single-material baseline: the best one-component fit, scaled
    # freely. If the mixture barely beats it, the sample is better
    # described as one material and the report must say so.
    best_single = None

    for index, name in enumerate(selected):
        vector = vectors[index]
        denominator = sum(value * value for value in vector)

        if denominator <= 0:
            continue

        scale = sum(
            t * v for t, v in zip(target, vector)
        ) / denominator
        scaled = [scale * value for value in vector]
        single_rmse = _rmse(target, scaled)

        if best_single is None or single_rmse < best_single[1]:
            best_single = (name, single_rmse)

    total = sum(coefficients)

    components = []

    for index, name in enumerate(selected):
        if coefficients[index] <= config.MIXTURE_MINIMUM_CONTRIBUTION:
            continue

        components.append({
            "material": name,
            "spectral_contribution": round(coefficients[index], 4),
            "normalized_contribution": (
                round(coefficients[index] / total, 4) if total > 0 else None
            ),
        })

    components.sort(
        key=lambda item: -item["spectral_contribution"]
    )

    improvement = None

    if best_single and best_single[1] > 0:
        improvement = round(
            (best_single[1] - mixture_rmse) / best_single[1], 4
        )

    if len(components) < 2:
        status = "SINGLE_COMPONENT"
        interpretation = (
            "The fit is dominated by one material; there is no evidence "
            "of a mixture in this spectrum."
        )

    elif (
        improvement is not None
        and improvement < config.MIXTURE_MINIMUM_IMPROVEMENT
    ):
        status = "NOT_BETTER_THAN_SINGLE"
        interpretation = (
            "A mixture reconstructs the spectrum only {:.1%} better than "
            "the best single material. That is not enough to claim a "
            "mixture.".format(improvement)
        )

    else:
        status = "MIXTURE_PLAUSIBLE"
        interpretation = (
            "The spectrum is reconstructed better by a combination than "
            "by any single reference. These are SPECTRAL CONTRIBUTIONS, "
            "not mass fractions - no validated concentration model exists."
        )

    return {
        "status": status,
        "method": "NNLS" + (" + sum-to-one" if sum_to_one else ""),
        "feature_count": len(channels),
        "endmembers": selected,
        "components": components,
        "reconstruction_rmse": round(mixture_rmse, 6),
        "best_single_material": best_single[0] if best_single else None,
        "best_single_rmse": (
            round(best_single[1], 6) if best_single else None
        ),
        "improvement_over_single": improvement,
        "residual": {
            channel: round(value, 6)
            for channel, value in zip(channels, residual)
        },
        "max_abs_residual": round(max(abs(v) for v in residual), 6),
        "coefficient_sum": round(total, 4),
        "interpretation": interpretation,
        "caveat": "Spectral contribution is not mass fraction. Converting "
                  "one to the other requires calibration against "
                  "physically prepared mixtures of known composition, "
                  "which does not exist for this instrument.",
    }
