"""
How much each source of evidence has actually been worth.

Three kinds of trust, all of which start as a declared prior and are
meant to be replaced by measurement as verified observations accumulate:

    database reliability      how often DB1 / DB2 / DB3 have been right
    class reliability         how often a NAMED CLASS is right when a
                              database names it
    metric reliability        which metric families separate which kinds
                              of material on this instrument

THE PRIORS ARE PRIORS, AND THEY SAY SO

    DB2  1.00   measured on this instrument, under the current
                calibration, in the same feature space it stores
    DB1  0.80   measured on this instrument, but under a calibration
                from another session and with 18 of the 54 features
    DB3  0.40   never measured on this instrument at all: a laboratory
                spectrum passed through a Gaussian model of a sensor
                whose real response curves nobody has

Every one is labelled PROVISIONAL_UNVALIDATED and every one is
overridden by measurement the moment there is enough of it. The
threshold for "enough" is deliberately not one observation: a single
lucky answer from DB3 must not promote it above DB1.

WHAT MEASUREMENT MEANS HERE

Precision, from the learning database: of the times this source named
class X, how often was the verified truth X? That is the number that
matters for a decision - not recall, and certainly not similarity.
"""

from BD.decision_learning import LABEL_EXACT_MATERIAL, VERIFIED

# Declared priors. PROVISIONAL_UNVALIDATED, and replaced per class as
# soon as there is enough verified history to measure the real number.
DATABASE_PRIORS = {
    "DB1": 0.80,
    "DB2": 1.00,
    "DB3": 0.40,
}

PRIOR_STATUS = "PROVISIONAL_UNVALIDATED"

# Verified answers a source must have given for a class before its
# measured precision replaces the prior. Below this the estimate is
# noise: one right answer out of one is not 100% reliability.
MIN_ANSWERS_FOR_MEASURED_RELIABILITY = 4

# How a measured precision maps onto the vocabulary already used by the
# DB3 discriminability analysis, so the two speak the same language.
STRONG = "STRONG"
MODERATE = "MODERATE"
WEAK = "WEAK"
INSUFFICIENT = "INSUFFICIENT_DATA"
UNRATED = "UNRATED"

STRONG_THRESHOLD = 0.70
MODERATE_THRESHOLD = 0.40


def rate(precision):
    if precision is None:
        return UNRATED

    if precision >= STRONG_THRESHOLD:
        return STRONG

    if precision >= MODERATE_THRESHOLD:
        return MODERATE

    return WEAK


class ReliabilityModel:
    """
    Trust, measured where possible and declared where not.

    Built from the learning database and the databases' own stored
    discriminability blocks. Holds no state of its own beyond what it
    was built from, so two engines built from the same history make the
    same judgements.
    """

    def __init__(self, learning_store=None, registry=None,
                 model_version=None):
        self.registry = registry
        self.model_version = model_version

        self.database_priors = dict(DATABASE_PRIORS)
        self.measured = {}
        self.confusion = {}
        self.observations_used = 0

        if learning_store is not None:
            self._learn(learning_store, model_version)

    def _learn(self, store, model_version):
        """
        Count, per (source, named class), how often it was right.

        Uses only VERIFIED exact-material labels. An OPERATOR_ASSERTED
        label may be good enough to train a classifier on if the operator
        says so explicitly, but it is not good enough to silently rewrite
        how much a database is trusted.
        """
        rows = store.confusion(model_version=model_version, levels=(VERIFIED,))

        self.observations_used = len({row["measurement_id"] for row in rows})

        for row in rows:
            predicted = row.get("predicted")
            actual = row.get("actual")
            source = row.get("model_version") or "unknown"

            if not predicted:
                continue

            key = (source, predicted)
            entry = self.measured.setdefault(
                key, {"named": 0, "correct": 0}
            )

            entry["named"] += 1
            entry["correct"] += 1 if predicted == actual else 0

            pair = self.confusion.setdefault(actual, {})
            pair[predicted] = pair.get(predicted, 0) + 1

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------

    def database_weight(self, database_key):
        """How much this database's opinion is worth, before class."""
        return self.database_priors.get(database_key, 0.5)

    def class_reliability(self, database_key, material):
        """
        Reliability of a database's answer for one class.

        Order of preference, most trustworthy first:
          1. measured precision from verified history, if there is enough
          2. the discriminability block the database itself carries
          3. UNRATED
        """
        key = (database_key, material)
        entry = self.measured.get(key)

        if entry and entry["named"] >= MIN_ANSWERS_FOR_MEASURED_RELIABILITY:
            precision = entry["correct"] / float(entry["named"])

            return {
                "rating": rate(precision),
                "precision": round(precision, 4),
                "basis": "MEASURED_FROM_VERIFIED_HISTORY",
                "named": entry["named"],
                "correct": entry["correct"],
            }

        stored = self._stored_rating(database_key, material)

        if stored is not None:
            return stored

        return {
            "rating": UNRATED,
            "precision": None,
            "basis": "NO_MEASUREMENT",
            "named": entry["named"] if entry else 0,
            "note": "Not enough verified answers to measure this. "
                    "UNRATED is not the same as poor.",
        }

    def _stored_rating(self, database_key, material):
        if self.registry is None:
            return None

        handle = self.registry.get(database_key)

        if handle is None or not handle.ready:
            return None

        try:
            rating, detail = handle.class_reliability(material)

        except Exception:
            return None

        if not rating:
            return None

        return {
            "rating": rating,
            "precision": (detail or {}).get("precision"),
            "basis": "DATABASE_DISCRIMINABILITY_ANALYSIS",
            "material_class": (detail or {}).get("material_class"),
            "detail": detail,
        }

    def confusions_for(self, material, limit=4):
        """
        What has historically been confused with this material.

        Reported as evidence, never as a correction: it says "when the
        truth was X, the system said Y this often", and the decision
        layer may use that to widen a candidate set. It can never edit a
        reference spectrum. §27.
        """
        counts = self.confusion.get(material) or {}

        ordered = sorted(
            counts.items(), key=lambda item: (-item[1], item[0])
        )

        return [
            {"predicted": name, "times": times}
            for name, times in ordered[:limit]
        ]

    def status(self):
        return {
            "database_priors": dict(self.database_priors),
            "prior_status": PRIOR_STATUS,
            "measured_pairs": len(self.measured),
            "observations_used": self.observations_used,
            "min_answers_for_measurement":
                MIN_ANSWERS_FOR_MEASURED_RELIABILITY,
            "note": "Database weights are declared priors until enough "
                    "verified history exists to measure them per class. "
                    "They are not tuned by hand against results.",
        }
