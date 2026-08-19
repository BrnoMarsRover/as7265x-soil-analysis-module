"""
Reproducible dataset snapshots.

A training run that cannot say exactly what it was trained on cannot be
reproduced, compared or trusted. So every dataset is:

    a fixed list of measurement ids
    a fixed feature pipeline version
    a fixed set of database and calibration versions
    a hash over all of the above plus the raw payloads

The hash is what makes reproducibility real rather than aspirational: if
a measurement is edited afterwards, the hash changes and the training
run no longer matches its own manifest. §47, §55.

FEATURES ARE DERIVED, NEVER STORED

Every feature is recomputed from the raw counts and the calibration that
was in force. A stored reflectance would be a second copy of the truth,
free to drift from the first.
"""

from datetime import datetime, timezone

from BD.channels import AS7265X_18, CHANNELS
from BD.decision_learning import (
    LABEL_EXACT_MATERIAL,
    TRUSTED_LEVELS,
    hash_payload,
)
from Science import preprocessing, features

FEATURE_PIPELINE_VERSION = "evidence-v1"

# The representations a classifier may be built on. Each is a complete
# view of the measurement; they are offered separately rather than
# concatenated, because which of them carries the signal is exactly what
# a benchmark is supposed to find out.
REPRESENTATIONS = (
    "normalized_white",
    "unit_white",
    "snv_white",
    "derivative_white",
)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def features_for(observation, calibration, representation):
    """One named representation of one observation, from its raw counts."""
    raw = (observation.get("raw") or {}).get("white")

    if not raw or calibration is None:
        return None

    normalized = preprocessing.normalize(
        raw, calibration.dark, calibration.white_for("white")
    )

    if representation == "normalized_white":
        return normalized

    if representation == "unit_white":
        return preprocessing.unit_vector(normalized)

    if representation == "snv_white":
        return preprocessing.snv(normalized)

    if representation == "derivative_white":
        return spectral_features.first_derivative(normalized)

    raise ValueError("unknown representation {!r}".format(representation))


def build(store, calibration_lookup, representation="normalized_white",
          levels=None, feature_space=AS7265X_18):
    """
    A hashed, reproducible dataset from the trusted learning history.

    Refuses anything that is not a VERIFIED exact-material label unless
    the caller explicitly widens `levels`, and never accepts UNVERIFIED
    or UNKNOWN at all - the learning store enforces that.
    """
    levels = tuple(levels or TRUSTED_LEVELS)

    labelled = store.labelled(
        levels=levels, label_types=(LABEL_EXACT_MATERIAL,)
    )

    samples = []
    skipped = []

    for observation in labelled:
        calibration = calibration_lookup(observation.get("calibration_id"))

        vector = features_for(observation, calibration, representation)

        if not vector:
            skipped.append({
                "measurement_id": observation["measurement_id"],
                "reason": "calibration {} is not available".format(
                    observation.get("calibration_id")
                ),
            })

            continue

        samples.append({
            "measurement_id": observation["measurement_id"],
            "label": observation.get("material_key"),
            "family": observation.get("family_id"),
            "session_id": observation.get("session_id"),
            "sample_group": observation.get("sample_group"),
            "acquisition_profile_id": observation.get(
                "acquisition_profile_id"
            ),
            "calibration_id": observation.get("calibration_id"),
            "verification_status": observation.get("verification_status"),
            "features": vector,
            "raw_hash": observation.get("raw_hash"),
        })

    samples.sort(key=lambda entry: entry["measurement_id"])

    manifest = {
        "built_at": utc_now(),
        "feature_pipeline": FEATURE_PIPELINE_VERSION,
        "representation": representation,
        "feature_space": feature_space,
        "features": list(CHANNELS),
        "levels_used": list(levels),
        "measurement_ids": [entry["measurement_id"] for entry in samples],
        "labels": sorted({entry["label"] for entry in samples}),
        "skipped": skipped,
        "size": len(samples),
    }

    manifest["dataset_hash"] = hash_payload({
        "pipeline": FEATURE_PIPELINE_VERSION,
        "representation": representation,
        "ids": manifest["measurement_ids"],
        "raw_hashes": [entry["raw_hash"] for entry in samples],
        "labels": [entry["label"] for entry in samples],
    })

    return {"manifest": manifest, "samples": samples}
