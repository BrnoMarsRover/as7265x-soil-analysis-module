"""
DecisionModel — interpretation, kept apart from measurement.

    Measurements/   deterministic mathematics  -> EvidencePackage
    DecisionModel/  learned interpretation     -> Decision

The split exists so that the mathematics stays reproducible and testable
while the interpretation is free to be versioned, retrained and compared
against its own history. A change here can never alter what was measured.

WHAT THIS LAYER MAY AND MAY NOT DO

    may     weigh evidence, learn which databases and metrics have been
            right before, decide it does not know
    may not modify DB1, DB2 or DB3; treat its own output as ground truth;
            rewrite a historical prediction

The four decision levels are the entire vocabulary of a conclusion:

    KNOWN_MATERIAL    MATERIAL_FAMILY    AMBIGUOUS_SET    UNKNOWN

Secondary interpretations (MIXTURE_PLAUSIBLE, LOW_SIGNAL,
NORMALIZATION_WARNING, OUT_OF_DISTRIBUTION, INSUFFICIENT_REFERENCE_DATA)
travel alongside and never replace the level.
"""

from DecisionModel.engine import (  # noqa: F401
    AMBIGUOUS_SET,
    DecisionEngine,
    KNOWN_MATERIAL,
    MATERIAL_FAMILY,
    UNKNOWN,
    decide,
)
from DecisionModel.model_registry import ModelRegistry  # noqa: F401
