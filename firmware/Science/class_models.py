"""
Class models: turning verified observations into distributions.

A reference database entry is one point. A class model is what that
material has actually been seen to do across independent measurements -
and the difference is the whole basis for saying "this sample is
atypical" or "these two candidates overlap".

BUILT ONLY FROM TRUSTED LABELS

`build` reads the learning database and uses VERIFIED exact-material
observations. Nothing else. An OPERATOR_ASSERTED label may be included
only when the caller says so explicitly, and UNVERIFIED and UNKNOWN can
never be included at all - they are not labels. §4.

INDEPENDENT MEASUREMENTS ONLY

Three acquisitions of an undisturbed sample tell you about the sensor.
Three acquisitions with the sample repacked between them tell you about
the material. Only the second kind counts towards `n_independent`, and
every statistic carries that number. §17.

A SNAPSHOT IS IMMUTABLE

Statistics are saved under a snapshot id and a model records which
snapshot it was built against. Rebuilding after new observations arrive
produces a NEW snapshot: an old conclusion stays reproducible. §20, §47.
"""

import hashlib
from datetime import datetime, timezone

from BD.channels import AS7265X_18, CHANNELS
from BD.decision_learning import (
    LABEL_EXACT_MATERIAL,
    TRUSTED_LEVELS,
    VERIFIED,
)
from Science import comparison, preprocessing


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def snapshot_id(measurement_ids, feature_space=AS7265X_18):
    """
    A snapshot's identity is the set of observations that built it.

    Two rebuilds from the same observations produce the same id, so a
    model that says which snapshot it used can be checked.
    """
    payload = "|".join(sorted(measurement_ids)) + "|" + feature_space
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]

    return "CLASSES_{}".format(digest.upper())


def observation_features(observation, calibration_lookup=None,
                         feature_space=AS7265X_18):
    """
    Re-derive the comparison features from an observation's RAW.

    Derived from raw every time rather than stored: the calibration in
    force may have changed, and a stored reflectance would silently mean
    something different from a freshly computed one. The raw counts are
    the source of truth. §8.
    """
    raw = observation.get("raw") or {}
    white_raw = raw.get("white")

    if not white_raw:
        return None

    calibration = None

    if calibration_lookup is not None:
        calibration = calibration_lookup(observation.get("calibration_id"))

    if calibration is None:
        return None

    return preprocessing.normalize(
        white_raw, calibration.dark, calibration.white_for("white")
    )


def build(store, calibration_lookup, levels=None, feature_space=AS7265X_18,
          include_repeats=False):
    """
    Class statistics for every material with trusted observations.

    Returns (snapshot_id, statistics, observations) where `observations`
    keeps the per-material feature vectors, so k-nearest-neighbour
    evidence can use the real members rather than only the centroid.
    """
    levels = tuple(levels or TRUSTED_LEVELS)

    labelled = store.labelled(
        levels=levels, label_types=(LABEL_EXACT_MATERIAL,)
    )

    grouped = {}
    used_ids = []
    skipped = []

    for observation in labelled:
        if not include_repeats and not observation.get(
            "independent_measurement"
        ):
            skipped.append({
                "measurement_id": observation["measurement_id"],
                "reason": "sensor repeat, not an independent measurement",
            })

            continue

        features = observation_features(
            observation, calibration_lookup, feature_space
        )

        if not features:
            skipped.append({
                "measurement_id": observation["measurement_id"],
                "reason": "its calibration is not available, so the "
                          "reflectance cannot be re-derived",
            })

            continue

        material = observation.get("material_key")

        grouped.setdefault(material, []).append({
            "measurement_id": observation["measurement_id"],
            "features": features,
            "session_id": observation.get("session_id"),
            "sample_group": observation.get("sample_group"),
        })

        used_ids.append(observation["measurement_id"])

    statistics = {}
    observations = {}

    for material, entries in sorted(grouped.items()):
        vectors = [entry["features"] for entry in entries]

        # Independence is counted by sample group where one exists: two
        # measurements of the same physical jar are one sample seen
        # twice, however far apart they were taken.
        groups = {
            entry.get("sample_group") or entry["measurement_id"]
            for entry in entries
        }

        statistics[material] = comparison.build_class_statistics(
            vectors, CHANNELS, n_independent=len(groups)
        )
        statistics[material]["source_measurement_ids"] = [
            entry["measurement_id"] for entry in entries
        ]
        statistics[material]["sample_groups"] = sorted(groups)

        observations[material] = vectors

    identity = snapshot_id(used_ids, feature_space)

    return {
        "snapshot_id": identity,
        "built_at": utc_now(),
        "feature_space": feature_space,
        "levels_used": list(levels),
        "materials": len(statistics),
        "observations_used": len(used_ids),
        "measurement_ids": sorted(used_ids),
        "skipped": skipped,
        "statistics": statistics,
        "observations": observations,
        "note": "Built from verified observations only. Nothing here "
                "modifies DB1, DB2 or DB3.",
    }


def save(store, snapshot):
    """Persist a snapshot so a model can name the statistics it used."""
    for material, statistics in snapshot["statistics"].items():
        store.save_class_statistics(
            snapshot["snapshot_id"],
            material,
            snapshot["feature_space"],
            statistics,
            statistics.get("source_measurement_ids") or [],
            statistics.get("n_independent") or 0,
            snapshot["built_at"],
        )

    return snapshot["snapshot_id"]


def coverage(snapshot):
    """
    What the class models can and cannot support yet, per material.

    The honest headline for the current dataset: one independent
    measurement per material supports a centroid and nothing else. Saying
    so plainly is more useful than producing a Mahalanobis distance from
    a singular matrix.
    """
    rows = []

    for material, statistics in sorted(snapshot["statistics"].items()):
        rows.append({
            "material": material,
            "n_independent": statistics.get("n_independent"),
            "centroid": bool(statistics.get("centroid")),
            "standardized": bool(statistics.get("supports_variance")),
            "mahalanobis": bool(statistics.get("supports_covariance")),
            "within_class_scatter": bool(
                (statistics.get("within_class_distances") or {})
            ),
        })

    return {
        "materials": rows,
        "with_scatter": sum(1 for row in rows if row["within_class_scatter"]),
        "supporting_covariance": sum(
            1 for row in rows if row["mahalanobis"]
        ),
        "note": "A material with one independent measurement has a "
                "centroid and no scatter: 'how typical is this sample' "
                "cannot be answered for it, and the decision layer is "
                "told so rather than being given a fabricated spread.",
    }
