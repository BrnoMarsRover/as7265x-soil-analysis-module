"""
The model registry — exactly one ACTIVE model, and no model deleted.

    EXPERIMENTAL   trained, not validated. May be run by hand, never
                   used for a reported conclusion.
    VALIDATED      passed group-aware held-out validation.
    ACTIVE         the one production model. Exactly one at a time.
    RETIRED        superseded. Kept: reproducing a conclusion from six
                   months ago means being able to load the model that
                   produced it.

ACTIVATION IS A DECISION, NOT A CONSEQUENCE OF TRAINING. A model that
finished training is not thereby better than the one in production, and
`compare_for_activation` exists to force that question to be asked with
numbers. Overall accuracy is not enough: a model that gains two points
overall while losing a class entirely, or while becoming unable to say
UNKNOWN, is a worse instrument. §50.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from BD import config as bd_config

EXPERIMENTAL = "EXPERIMENTAL"
VALIDATED = "VALIDATED"
ACTIVE = "ACTIVE"
RETIRED = "RETIRED"

STATUSES = (EXPERIMENTAL, VALIDATED, ACTIVE, RETIRED)

# Model artifacts are DATA, so they live in BD with everything else
# that has to survive a code change and be referenced by a stored
# conclusion. This module owns the RULES for promoting one; BD owns the
# bytes.
MODELS_DIR = bd_config.MODELS_DIR
REGISTRY_FILE = bd_config.MODEL_REGISTRY_FILE

# A class may not lose more than this fraction of its recall for the sake
# of an overall gain. PROVISIONAL: derived from nothing but the judgement
# that silently losing a material is worse than a small average gain.
MAX_CLASS_RECALL_LOSS = 0.10

# Nor may the ability to answer UNKNOWN degrade: a model that names a
# material for every out-of-distribution sample scores well on a closed
# test set and is dangerous in the field.
MAX_UNKNOWN_DETECTION_LOSS = 0.05


class ModelRegistryError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)

        self.code = code
        self.message = message
        self.details = details or {}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def _write_json(path, payload):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")

    path.parent.mkdir(parents=True, exist_ok=True)

    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())

    os.replace(tmp, path)


class ModelRegistry:
    """Every decision model that has ever existed, and which one is live."""

    def __init__(self, path=None):
        self.path = Path(path or REGISTRY_FILE)

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                document = json.load(handle)

        except (OSError, ValueError):
            document = {
                "schema_version": 1,
                "updated_at": utc_now(),
                "active": None,
                "models": [],
            }

        document.setdefault("models", [])

        return document

    def _save(self, document):
        document["updated_at"] = utc_now()
        _write_json(self.path, document)

    def models(self):
        return list(self._load()["models"])

    def get(self, version):
        for model in self._load()["models"]:
            if model.get("version") == version:
                return model

        return None

    def active(self):
        document = self._load()
        version = document.get("active")

        if not version:
            return None

        return self.get(version)

    def register(self, version, kind, status=EXPERIMENTAL, description=None,
                 training_run_id=None, metrics=None, feature_pipeline=None,
                 provenance=None):
        """Add a model. Registering never activates it."""
        if status not in STATUSES:
            raise ModelRegistryError(
                "BAD_STATUS",
                "status must be one of {}.".format(", ".join(STATUSES)),
            )

        if self.get(version) is not None:
            raise ModelRegistryError(
                "MODEL_EXISTS",
                "{} is already registered. Model versions are immutable."
                .format(version),
            )

        document = self._load()

        document["models"].append({
            "version": version,
            "kind": kind,
            "status": ACTIVE if status == ACTIVE else status,
            "registered_at": utc_now(),
            "description": description,
            "training_run_id": training_run_id,
            "feature_pipeline": feature_pipeline,
            "metrics": metrics or {},
            "provenance": provenance or {},
        })

        self._save(document)

        if status == ACTIVE:
            self.activate(version, force=True)

        return self.get(version)

    def set_status(self, version, status):
        if status not in STATUSES:
            raise ModelRegistryError(
                "BAD_STATUS",
                "status must be one of {}.".format(", ".join(STATUSES)),
            )

        document = self._load()

        for model in document["models"]:
            if model["version"] == version:
                model["status"] = status
                self._save(document)

                return model

        raise ModelRegistryError(
            "MODEL_NOT_FOUND", "No model {}.".format(version)
        )

    def activate(self, version, force=False, regression_report=None):
        """
        Make one model the production model.

        Refuses an EXPERIMENTAL model unless forced: a model that has not
        been validated has, by definition, no evidence that it is better
        than the one it would replace.
        """
        model = self.get(version)

        if model is None:
            raise ModelRegistryError(
                "MODEL_NOT_FOUND", "No model {}.".format(version)
            )

        if model["status"] == EXPERIMENTAL and not force:
            raise ModelRegistryError(
                "MODEL_NOT_VALIDATED",
                "{} is EXPERIMENTAL. Validate it against held-out data "
                "before it decides anything that gets reported."
                .format(version),
            )

        document = self._load()
        previous = document.get("active")

        for entry in document["models"]:
            if entry["version"] == version:
                entry["status"] = ACTIVE
                entry["activated_at"] = utc_now()
                entry["activation_regression_report"] = regression_report

            elif entry["version"] == previous:
                entry["status"] = RETIRED
                entry["retired_at"] = utc_now()

        document["active"] = version
        document["previous_active"] = previous

        self._save(document)

        return self.get(version)

    # ------------------------------------------------------------------
    # activation safety
    # ------------------------------------------------------------------

    def compare_for_activation(self, candidate_metrics, active_metrics):
        """
        Should this model replace the active one? Answered with numbers.

        Returns a verdict and the reasons. It never activates anything -
        the operator does that, having read the reasons.
        """
        blocking = []
        notes = []

        candidate_metrics = candidate_metrics or {}
        active_metrics = active_metrics or {}

        def delta(name):
            new = candidate_metrics.get(name)
            old = active_metrics.get(name)

            if new is None or old is None:
                return None

            return new - old

        overall = delta("balanced_accuracy")

        if overall is not None:
            notes.append({
                "metric": "balanced_accuracy",
                "change": round(overall, 4),
            })

        # Per-class recall collapse. This is the check that stops an
        # average from hiding a disaster.
        candidate_recall = candidate_metrics.get("per_class_recall") or {}
        active_recall = active_metrics.get("per_class_recall") or {}

        for material, old_value in active_recall.items():
            new_value = candidate_recall.get(material)

            if new_value is None:
                blocking.append({
                    "code": "CLASS_DROPPED",
                    "message": "{} is not classified at all by the "
                               "candidate.".format(material),
                })

                continue

            if old_value - new_value > MAX_CLASS_RECALL_LOSS:
                blocking.append({
                    "code": "CLASS_RECALL_COLLAPSE",
                    "message": "{} recall falls {:.0%} -> {:.0%}.".format(
                        material, old_value, new_value
                    ),
                })

        unknown_change = delta("unknown_detection_rate")

        if unknown_change is not None and \
                unknown_change < -MAX_UNKNOWN_DETECTION_LOSS:
            blocking.append({
                "code": "UNKNOWN_DETECTION_DEGRADED",
                "message": "The candidate is worse at recognising samples "
                           "it should refuse: {:.0%} -> {:.0%}.".format(
                               active_metrics["unknown_detection_rate"],
                               candidate_metrics["unknown_detection_rate"],
                           ),
            })

        if overall is not None and overall <= 0 and not blocking:
            blocking.append({
                "code": "NO_IMPROVEMENT",
                "message": "Balanced accuracy did not improve "
                           "({:+.4f}).".format(overall),
            })

        return {
            "recommendation": "REJECT" if blocking else "ACCEPT",
            "blocking": blocking,
            "notes": notes,
            "thresholds": {
                "max_class_recall_loss": MAX_CLASS_RECALL_LOSS,
                "max_unknown_detection_loss": MAX_UNKNOWN_DETECTION_LOSS,
                "status": "PROVISIONAL - engineering judgement, not "
                          "derived from a validation study",
            },
        }

    def status(self):
        models = self.models()

        return {
            "file": str(self.path),
            "count": len(models),
            "active": (self.active() or {}).get("version"),
            "by_status": {
                status: [
                    model["version"] for model in models
                    if model.get("status") == status
                ]
                for status in STATUSES
            },
        }
