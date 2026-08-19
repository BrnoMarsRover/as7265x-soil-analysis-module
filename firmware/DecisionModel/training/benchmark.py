"""
Supervised baselines, benchmarked honestly.

WHAT IS IMPLEMENTED, AND WHY THESE

    nearest centroid   the simplest thing that could work, and the right
                       first baseline: any model that cannot beat it is
                       not earning its complexity
    k-nearest-neighbour  the strongest simple method on a small dataset,
                       because it makes no distributional assumption at
                       all

WHAT IS NOT, AND WHY NOT YET

Regularized LDA, PLS-DA and SVM are the right next three (§30). None of
them is implemented here, and the reason is not effort:

    LDA and PLS-DA need a covariance or a latent structure estimated
    from more independent samples than there are classes. With 12
    observations over 12 classes, both are singular by construction.

    An SVM needs a validation set to choose C and the kernel width.
    Choosing them on the training data - the only data there is - is
    exactly the leakage §34 forbids.

Adding them before the data can support them would produce numbers that
look like performance and are noise. The harness they will plug into is
here; the gate that lets them in is `cross_validation.feasibility`.

NO NEURAL NETWORK. Twelve examples. §30.
"""

import math

from DecisionModel.training import cross_validation


def _distance(left, right, features):
    total = 0.0
    used = 0

    for feature in features:
        a = left.get(feature)
        b = right.get(feature)

        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            continue

        total += (a - b) ** 2
        used += 1

    if not used:
        return None

    return math.sqrt(total)


def fit_nearest_centroid(samples, features):
    """Centroids from the TRAINING fold only. Nothing else is touched."""
    sums = {}
    counts = {}

    for sample in samples:
        label = sample["label"]
        vector = sample["features"]

        target = sums.setdefault(label, {feature: 0.0 for feature in features})
        counts[label] = counts.get(label, 0) + 1

        for feature in features:
            value = vector.get(feature)

            if isinstance(value, (int, float)):
                target[feature] += value

    return {
        label: {
            feature: total / counts[label]
            for feature, total in vector.items()
        }
        for label, vector in sums.items()
    }


def predict_nearest_centroid(model, vector, features):
    best = None
    best_distance = None

    for label, centroid in model.items():
        distance = _distance(vector, centroid, features)

        if distance is None:
            continue

        if best_distance is None or distance < best_distance:
            best = label
            best_distance = distance

    return best, best_distance


def predict_knn(training, vector, features, k=1):
    scored = []

    for sample in training:
        distance = _distance(vector, sample["features"], features)

        if distance is not None:
            scored.append((distance, sample["label"]))

    if not scored:
        return None, None

    scored.sort()
    nearest = scored[:max(1, int(k))]

    votes = {}

    for distance, label in nearest:
        votes[label] = votes.get(label, 0) + 1

    winner = max(sorted(votes), key=lambda label: votes[label])

    return winner, nearest[0][0]


def evaluate(dataset, method="nearest_centroid", k=1):
    """
    Leave-one-group-out evaluation of one baseline.

    Every model is fitted inside the fold. The feasibility check runs
    first and its verdict travels with the result, so an accuracy of zero
    can never be quoted without the reason beside it.
    """
    samples = dataset["samples"]
    features = dataset["manifest"]["features"]

    observations = [
        {
            "measurement_id": sample["measurement_id"],
            "sample_group": sample["sample_group"],
            "session_id": sample["session_id"],
            "material_key": sample["label"],
        }
        for sample in samples
    ]

    feasible = cross_validation.feasibility(observations)
    folds = cross_validation.leave_one_group_out(observations)

    by_id = {sample["measurement_id"]: sample for sample in samples}

    correct = 0
    total = 0
    predictions = []
    per_class = {}

    for fold in folds:
        training = [by_id[identifier] for identifier in fold.train_ids]

        if not training:
            continue

        model = (
            fit_nearest_centroid(training, features)
            if method == "nearest_centroid" else None
        )

        for identifier in fold.test_ids:
            sample = by_id[identifier]

            if method == "nearest_centroid":
                predicted, distance = predict_nearest_centroid(
                    model, sample["features"], features
                )

            else:
                predicted, distance = predict_knn(
                    training, sample["features"], features, k
                )

            hit = predicted == sample["label"]

            correct += 1 if hit else 0
            total += 1

            entry = per_class.setdefault(
                sample["label"], {"n": 0, "correct": 0}
            )
            entry["n"] += 1
            entry["correct"] += 1 if hit else 0

            predictions.append({
                "measurement_id": identifier,
                "actual": sample["label"],
                "predicted": predicted,
                "distance": distance,
                "correct": hit,
                "held_out": fold.held_out,
                "training_size": len(training),
            })

    return {
        "method": method,
        "k": k if method == "knn" else None,
        "representation": dataset["manifest"]["representation"],
        "dataset_hash": dataset["manifest"]["dataset_hash"],
        "folds": len(folds),
        "n": total,
        "accuracy": round(correct / total, 4) if total else None,
        "per_class_recall": {
            label: round(entry["correct"] / entry["n"], 4)
            for label, entry in sorted(per_class.items())
        },
        "balanced_accuracy": (
            round(
                sum(
                    entry["correct"] / entry["n"]
                    for entry in per_class.values()
                ) / len(per_class), 4
            ) if per_class else None
        ),
        "predictions": predictions,
        "feasibility": feasible,
        "interpretation": (
            "Meaningful." if feasible["supervised_validation_possible"]
            else "NOT MEANINGFUL AS A MODEL SCORE. " + feasible["reason"]
        ),
    }


def compare(dataset, methods=None):
    """Run several baselines over the same dataset and tabulate them."""
    methods = methods or [
        ("nearest_centroid", 1), ("knn", 1), ("knn", 3),
    ]

    results = []

    for method, k in methods:
        results.append(evaluate(dataset, method=method, k=k))

    return {
        "dataset_hash": dataset["manifest"]["dataset_hash"],
        "representation": dataset["manifest"]["representation"],
        "results": results,
        "feasibility": results[0]["feasibility"] if results else None,
    }
