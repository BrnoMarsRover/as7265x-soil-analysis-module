
"""
The decision engine — evidence in, an honest conclusion out.

    EvidencePackage
        -> reliability (measured where possible, declared where not)
        -> fusion      (magnitude-aware, one vote per metric family)
        -> hierarchy   (family before material)
        -> unknown     (is any of this trustworthy at all)
        -> Decision

FOUR LEVELS, AND ONLY FOUR

    KNOWN_MATERIAL    one material, with the evidence to name it
    MATERIAL_FAMILY   the family holds, the member does not
    AMBIGUOUS_SET     several candidates that cannot be separated
    UNKNOWN           nothing in the libraries explains this

Secondary interpretations - MIXTURE_PLAUSIBLE, LOW_SIGNAL,
NORMALIZATION_WARNING, OUT_OF_DISTRIBUTION, INSUFFICIENT_REFERENCE_DATA -
travel with the decision and never replace the level.

V001 IS A COLD START, AND SAYS SO

There is not enough verified history to train a classifier: twelve
observations, one per material. So V001 is deterministic - fused
evidence, measured margins, class distance where it exists, declared
database priors - and every threshold it uses is labelled PROVISIONAL.
The architecture around it is what a trained model will slot into; the
model registry already knows how to refuse to activate one that is not
better. §24, §62.
"""

from datetime import datetime, timezone

from DecisionModel import (
    evidence_fusion,
    explainability,
    hierarchy,
    reliability as reliability_module,
    unknown_detection,
)

MODEL_VERSION = "FREYA_DECISION_V001"
MODEL_KIND = "COLD_START_DETERMINISTIC"

KNOWN_MATERIAL = "KNOWN_MATERIAL"
MATERIAL_FAMILY = "MATERIAL_FAMILY"
AMBIGUOUS_SET = "AMBIGUOUS_SET"
UNKNOWN = "UNKNOWN"

LEVELS = (KNOWN_MATERIAL, MATERIAL_FAMILY, AMBIGUOUS_SET, UNKNOWN)

# Secondary interpretations. Never a level.
MIXTURE_PLAUSIBLE = "MIXTURE_PLAUSIBLE"
LOW_SIGNAL = "LOW_SIGNAL"
NORMALIZATION_WARNING = "NORMALIZATION_WARNING"
OUT_OF_DISTRIBUTION = "OUT_OF_DISTRIBUTION"
INSUFFICIENT_REFERENCE_DATA = "INSUFFICIENT_REFERENCE_DATA"

CONFIDENCE_HIGH = "HIGH"
CONFIDENCE_MEDIUM = "MEDIUM"
CONFIDENCE_LOW = "LOW"
CONFIDENCE_NONE = "NONE"

# --- thresholds, every one PROVISIONAL ---------------------------------
# Naming a single material needs the leader to stand clear of the field
# AND to be supported by something, not merely to have come first.
KNOWN_MATERIAL_MIN_STRENGTH = 0.45
KNOWN_MATERIAL_MIN_RELATIVE_MARGIN = 0.35

# Two independent sources agreeing is worth more than one source being
# emphatic, so a single-source answer needs a larger margin.
SINGLE_SOURCE_MIN_RELATIVE_MARGIN = 0.55

THRESHOLD_STATUS = "PROVISIONAL_UNVALIDATED"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class DecisionEngine:
    """
    One configured decision model.

    Holds the reliability model and the taxonomy, so repeated decisions
    in one session are consistent with each other and with the history
    they were built from.
    """

    version = MODEL_VERSION
    kind = MODEL_KIND

    def __init__(self, taxonomy=None, reliability=None, registry=None,
                 learning_store=None, class_snapshot=None):
        self.taxonomy = taxonomy
        self.registry = registry
        self.class_snapshot = class_snapshot

        self.reliability = reliability or reliability_module.ReliabilityModel(
            learning_store=learning_store, registry=registry
        )

    # ------------------------------------------------------------------

    def decide(self, evidence, mixture=None):
        """
        The full hierarchical decision for one evidence package.

        `mixture` is the optional NNLS result. It is secondary evidence:
        it can add MIXTURE_PLAUSIBLE, and it can never overrule a
        material-level conclusion on its own. §23.
        """
        fusion = evidence_fusion.fuse(
            evidence.get("reference_analysis"),
            self.reliability,
            evidence.get("class_analysis"),
        )

        separation = evidence_fusion.separability(fusion["candidates"])
        unknown = unknown_detection.assess(evidence, fusion, separation)
        families = hierarchy.family_support(
            fusion["candidates"], self.taxonomy
        )

        decision = self._resolve(
            evidence, fusion, separation, unknown, families
        )

        decision["secondary_interpretations"] = self._secondary(
            evidence, unknown, mixture, fusion
        )
        decision["confidence"] = self._confidence(
            decision, separation, unknown, fusion
        )

        decision["evidence"] = {
            "databases": {
                key: {
                    "families": support["families"],
                    "database_weight": support["database_weight"],
                }
                for key, support in fusion["per_database"].items()
            },
            "fusion_method": fusion["method"],
            "separation": separation,
            "family_support": families,
            "unknown_detection": unknown,
            "discounted": fusion["discounted"],
            "class_analysis_available": (
                evidence.get("class_analysis") or {}
            ).get("available", False),
        }

        decision["provenance"] = self._provenance(evidence)
        decision["warnings"] = [
            warning.get("code") for warning in evidence.get("warnings") or []
        ]
        decision["explanation"] = explainability.explain(
            decision, evidence, fusion, unknown
        )

        return decision

    # ------------------------------------------------------------------
    # the ladder
    # ------------------------------------------------------------------

    def _resolve(self, evidence, fusion, separation, unknown, families):
        """Steps 1 to 6 of the hierarchy, in order, stopping at the first
        honest answer."""
        candidates = fusion["candidates"]

        base = {
            "decision_model_version": self.version,
            "decision_model_kind": self.kind,
            "measurement_id": (
                (evidence.get("measurement") or {}).get("measurement_id")
            ),
            "decided_at": utc_now(),
            "level": UNKNOWN,
            "material": None,
            "family": None,
            "candidates": [],
            "separation": separation,
            "family_evidence": families,
            "thresholds": {
                "known_material_min_strength": KNOWN_MATERIAL_MIN_STRENGTH,
                "known_material_min_relative_margin":
                    KNOWN_MATERIAL_MIN_RELATIVE_MARGIN,
                "single_source_min_relative_margin":
                    SINGLE_SOURCE_MIN_RELATIVE_MARGIN,
                "status": THRESHOLD_STATUS,
            },
        }

        # 1 + 2. Usable, and inside a known domain?
        if unknown["unknown_required"]:
            base["level"] = UNKNOWN
            base["reason"] = unknown["reasons"][0]["detail"]
            base["candidates"] = hierarchy.contextual_neighbours(
                candidates, self.taxonomy
            )
            base["nearest_known"] = [
                entry["material"] for entry in base["candidates"]
            ]

            return base

        if not candidates:
            base["level"] = UNKNOWN
            base["reason"] = "no database produced a comparable candidate"

            return base

        leader = candidates[0]
        relative_margin = separation.get("relative_margin") or 0.0
        sources = leader.get("independent_sources") or 1

        # Corroboration is only evidence where it was POSSIBLE. DB3 holds
        # minerals and DB1 holds laboratory chemicals, so no amount of
        # agreement could ever confirm Copper(II) Sulfate: DB3 has never
        # heard of it. Demanding a second source there would make
        # KNOWN_MATERIAL unreachable for every chemical in the library -
        # punishing a material for the contents of a different database.
        possible = self._corroboration_possible(leader["material"])

        required_margin = (
            KNOWN_MATERIAL_MIN_RELATIVE_MARGIN
            if sources > 1 or not possible
            else SINGLE_SOURCE_MIN_RELATIVE_MARGIN
        )

        base["corroboration"] = {
            "independent_sources": sources,
            "corroboration_possible": possible,
            "required_relative_margin": required_margin,
            "observed_relative_margin": round(relative_margin, 4),
        }

        # 6. Strong enough for an exact material?
        if (
            leader["evidence_strength"] >= KNOWN_MATERIAL_MIN_STRENGTH
            and relative_margin >= required_margin
        ):
            base["level"] = KNOWN_MATERIAL
            base["material"] = leader["material"]
            base["family"] = (
                self.taxonomy.family_of(leader["material"])
                if self.taxonomy else None
            )
            base["candidates"] = self._candidate_list(candidates[:3])
            base["reason"] = (
                "leads by {:.0%} of its own strength with {} independent "
                "source(s) supporting it".format(relative_margin, sources)
            )

            return base

        # 3. Does a family hold instead?
        if families.get("decisive"):
            base["level"] = MATERIAL_FAMILY
            base["family"] = families["leader"]
            base["candidates"] = self._candidate_list(
                hierarchy.ambiguous_set(candidates, separation)
            )
            base["reason"] = (
                "the {} family carries {:.0%} of the candidate evidence, "
                "but no member is separable from the others".format(
                    families["leader"], families["leader_share"] or 0.0
                )
            )

            return base

        # 5. Several candidates that cannot be told apart.
        members = hierarchy.ambiguous_set(candidates, separation)

        if len(members) > 1:
            base["level"] = AMBIGUOUS_SET
            base["candidates"] = self._candidate_list(members)
            base["family"] = self._shared_family(members)
            base["reason"] = (
                "{} candidates lie within the leader's own margin"
                .format(len(members))
            )

            return base

        base["level"] = UNKNOWN
        base["reason"] = (
            "the leading candidate is not supported strongly enough to be "
            "named and no family is decisive"
        )
        base["candidates"] = hierarchy.contextual_neighbours(
            candidates, self.taxonomy
        )

        return base

    def _candidate_list(self, candidates):
        listed = []

        for candidate in candidates:
            strength = candidate.get("evidence_strength")

            listed.append({
                "material": candidate["material"],
                "evidence_strength": strength,
                "evidence_level": (
                    "HIGH" if strength is not None and strength >= 0.6
                    else "MEDIUM" if strength is not None and strength >= 0.3
                    else "LOW"
                ),
                "family": (
                    self.taxonomy.family_of(candidate["material"])
                    if self.taxonomy else None
                ),
                "supporting_databases": candidate.get("supporting_databases"),
                "class_evidence": candidate.get("class_evidence"),
            })

        return listed

    def _corroboration_possible(self, material):
        """
        Could a second database have named this material at all?

        Answered by asking the libraries, not by assuming. A material
        present in only one ready database can never be corroborated, and
        that absence says nothing whatever about the measurement.
        """
        if self.registry is None:
            return False

        holders = 0

        for handle in self.registry.databases.values():
            if handle.ready and material in handle.materials:
                holders += 1

        return holders >= 2

    def _shared_family(self, candidates):
        if not self.taxonomy:
            return None

        families = {
            self.taxonomy.family_of(candidate["material"])
            for candidate in candidates
        }
        families.discard(None)

        return families.pop() if len(families) == 1 else None

    # ------------------------------------------------------------------

    def _secondary(self, evidence, unknown, mixture, fusion):
        """Interpretations that accompany the level without replacing it."""
        secondary = []

        quality = evidence.get("quality") or {}
        normalization = (quality.get("normalization") or {}).get("status")

        if normalization and normalization != "OK":
            secondary.append(NORMALIZATION_WARNING)

        reliability = evidence.get("channel_reliability") or {}
        total = reliability.get("features_total") or 0
        normalized_valid = reliability.get("normalized_valid_total") or 0

        if total and normalized_valid < total * 0.5:
            secondary.append(LOW_SIGNAL)

        if any(
            reason["code"] == "OUTSIDE_KNOWN_CLASSES"
            for reason in unknown["reasons"]
        ):
            secondary.append(OUT_OF_DISTRIBUTION)

        if not (evidence.get("class_analysis") or {}).get("available"):
            secondary.append(INSUFFICIENT_REFERENCE_DATA)

        if mixture and mixture.get("status") == "MIXTURE_PLAUSIBLE":
            secondary.append(MIXTURE_PLAUSIBLE)

        return secondary

    def _confidence(self, decision, separation, unknown, fusion):
        """
        Confidence from the structure of the evidence, never from a score.

        Explicitly not a probability, and never presented as one. §36.
        """
        if decision["level"] == UNKNOWN:
            return CONFIDENCE_NONE

        penalties = unknown["severe"] * 3 + unknown["moderate"]

        leader = (fusion["candidates"] or [{}])[0]
        sources = leader.get("independent_sources") or 0

        if sources < 2:
            penalties += 1

        if not fusion.get("class_support_available"):
            penalties += 1

        if penalties == 0 and decision["level"] == KNOWN_MATERIAL:
            return CONFIDENCE_HIGH

        if penalties <= 1:
            return CONFIDENCE_MEDIUM

        if penalties <= 3:
            return CONFIDENCE_LOW

        return CONFIDENCE_NONE

    def _provenance(self, evidence):
        """Everything needed to reproduce this conclusion exactly. §20."""
        acquisition = evidence.get("acquisition") or {}

        databases = {}

        if self.registry is not None:
            for key, handle in sorted(self.registry.databases.items()):
                databases[key] = {
                    "version": handle.version,
                    "status": handle.status,
                    "materials": handle.count(),
                }

        return {
            "decision_model_version": self.version,
            "evidence_schema_version": evidence.get("schema_version"),
            "science_version": evidence.get("science_version"),
            "reliability_version": evidence.get("reliability_version"),
            "acquisition_profile_id": acquisition.get(
                "acquisition_profile_id"
            ),
            "calibration_id": acquisition.get("calibration_id"),
            "legacy_calibration_id": acquisition.get(
                "legacy_calibration_id"
            ),
            "database_versions": databases,
            "class_statistics_snapshot": self.class_snapshot,
            "reliability_basis": self.reliability.status(),
        }


def decide(evidence, taxonomy=None, registry=None, learning_store=None,
           mixture=None, class_snapshot=None):
    """Convenience wrapper for a one-off decision."""
    engine = DecisionEngine(
        taxonomy=taxonomy, registry=registry, learning_store=learning_store,
        class_snapshot=class_snapshot,
    )

    return engine.decide(evidence, mixture=mixture)
