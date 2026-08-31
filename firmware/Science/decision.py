
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

from BD.decision_learning import LABEL_EXACT_MATERIAL, VERIFIED

MODEL_VERSION = "FREYA_DECISION_V001"
MODEL_KIND = "COLD_START_DETERMINISTIC"

KNOWN_MATERIAL = "KNOWN_MATERIAL"
MATERIAL_FAMILY = "MATERIAL_FAMILY"
AMBIGUOUS_SET = "AMBIGUOUS_SET"
UNKNOWN = "UNKNOWN"

LEVELS = (KNOWN_MATERIAL, MATERIAL_FAMILY, AMBIGUOUS_SET, UNKNOWN)

# ----------------------------------------------------------------------
# OUTCOME STATUS - a different question from LEVEL
#
# `level` says how SPECIFIC the answer is: a material, a family, a set,
# or nothing. `status` says what KIND of outcome produced it, and the
# two are not the same question.
#
# Three very different situations used to arrive at level = UNKNOWN and
# become indistinguishable in the stored record:
#
#   the measurement was not usable at all
#   the measurement was fine and the libraries had nothing to say
#   the measurement was fine and the evidence pointed nowhere in
#       particular
#
# An external reader deciding whether to re-measure, to extend the
# library, or to accept "we do not know" needs to tell those apart, and
# a reason string is not something a reader can branch on.
CLASSIFIED = "CLASSIFIED"
AMBIGUOUS = "AMBIGUOUS"
INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
INVALID_MEASUREMENT = "INVALID_MEASUREMENT"

STATUSES = (CLASSIFIED, AMBIGUOUS, UNKNOWN, INSUFFICIENT_EVIDENCE,
            INVALID_MEASUREMENT)

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

        self.reliability = reliability or ReliabilityModel(
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
        fusion = fuse(
            evidence.get("reference_analysis"),
            self.reliability,
            evidence.get("class_analysis"),
        )

        separation = separability(fusion["candidates"])
        unknown = assess_unknown(evidence, fusion, separation)
        families = family_support(
            fusion["candidates"], self.taxonomy
        )

        decision = self._resolve(
            evidence, fusion, separation, unknown, families
        )

        decision["secondary_interpretations"] = self._secondary(
            evidence, unknown, mixture, fusion
        )
        decision["mixture"] = self._mixture_report(mixture)
        decision["confidence"] = self._confidence(
            decision, separation, unknown, fusion
        )
        decision["confidence_reasons"] = self._confidence_reasons(
            decision, unknown, fusion
        )
        decision["status"] = self._status(decision, evidence, fusion)

        # WHAT THE STORED DECISION HAS TO BE ABLE TO ANSWER.
        #
        # "Why iron_oxide and not Activated Carbon?" is answerable only
        # from the per-CANDIDATE support each database gave, and that
        # used to be dropped here: the stored evidence kept the per-
        # FAMILY winners and the database weights, so a reader could
        # see that DB1 preferred Iron(II) Sulfate on cosine and could
        # not see how much support DB1 gave to the material that
        # actually won.
        #
        # Nothing new is computed. `fusion` already holds all of it;
        # this stopped throwing it away.
        decision["evidence"] = {
            "databases": {
                key: {
                    "families": support["families"],
                    "database_weight": support["database_weight"],
                    "candidates": support["candidates"],
                    "families_available": support["families_available"],
                }
                for key, support in fusion["per_database"].items()
            },
            "fusion_method": fusion["method"],
            "family_weights": fusion.get("family_weights"),
            "family_weight_status": fusion.get("family_weight_status"),
            "separation": separation,
            "family_support": families,
            "unknown_detection": unknown,
            "discounted": fusion["discounted"],
            "class_support_available": fusion.get(
                "class_support_available", False
            ),
            "class_analysis_available": (
                evidence.get("class_analysis") or {}
            ).get("available", False),
            "coverage": self._coverage(evidence),
        }

        decision["gates"] = self._gates(
            decision, fusion, separation, unknown, families
        )

        decision["provenance"] = self._provenance(evidence)
        decision["warnings"] = [
            warning.get("code") for warning in evidence.get("warnings") or []
        ]
        decision["explanation"] = explain(
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

            # WHAT THE CANDIDATE LIST IS. Two different things are
            # returned under one key: the candidates that were actually
            # weighed (`WEIGHED`), and, for an UNKNOWN, the nearest
            # known things - which are context, not candidates, and
            # carry no per-database support because none was fused for
            # them. A screen that cannot tell them apart prints
            # "supported by 0 of the databases" over a list that was
            # never a claim about the sample.
            "candidates_are": "WEIGHED",
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
            base["candidates"] = contextual_neighbours(
                candidates, self.taxonomy
            )
            base["candidates_are"] = "NEAREST_KNOWN"
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
                ambiguous_set(candidates, separation)
            )
            base["reason"] = (
                "the {} family carries {:.0%} of the candidate evidence, "
                "but no member is separable from the others".format(
                    families["leader"], families["leader_share"] or 0.0
                )
            )

            return base

        # 5. Several candidates that cannot be told apart.
        members = ambiguous_set(candidates, separation)

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
        base["candidates"] = contextual_neighbours(
            candidates, self.taxonomy
        )
        base["candidates_are"] = "NEAREST_KNOWN"

        return base

    @staticmethod
    def _status(decision, evidence, fusion):
        """
        What KIND of outcome this is, beside how specific it is.

        Checked in order of how fundamental the problem is: an unusable
        measurement is not a failure to classify, and an empty library
        is not a sample that resists classification. Only when the
        measurement was good and the libraries had something to say
        does an UNKNOWN mean "this does not look like anything we
        know", which is a genuine scientific result.
        """
        quality = evidence.get("quality") or {}
        hardware = (quality.get("hardware") or {}).get("status")
        normalization = (quality.get("normalization") or {}).get("status")

        if hardware in ("HARDWARE_QC_FAIL", "FAIL"):
            return INVALID_MEASUREMENT

        if normalization == "NORMALIZATION_UNUSABLE":
            return INVALID_MEASUREMENT

        # Nothing to compare against. Not the sample's fault.
        if not (fusion.get("candidates") or []):
            return INSUFFICIENT_EVIDENCE

        level = decision.get("level")

        if level == KNOWN_MATERIAL:
            return CLASSIFIED

        if level in (MATERIAL_FAMILY, AMBIGUOUS_SET):
            return AMBIGUOUS

        return UNKNOWN

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
                "independent_sources": candidate.get("independent_sources"),

                # HOW MUCH EACH DATABASE GAVE THIS CANDIDATE, kept per
                # database and never summed into one number. This is
                # the field that lets a screen say "DB3 supported it at
                # 0.71 and DB1 at 0.12" instead of "0.41", which is the
                # difference between evidence and an average.
                "per_database": {
                    key: {
                        "support": entry.get("support"),
                        "families": entry.get("families"),
                        "family_agreement": entry.get("family_agreement"),
                        "class_reliability": entry.get("class_reliability"),
                        "class_reliability_basis": entry.get(
                            "class_reliability_basis"
                        ),
                        "votes": entry.get("votes"),
                    }
                    for key, entry in (
                        candidate.get("per_database") or {}
                    ).items()
                },
                "class_evidence": candidate.get("class_evidence"),
                "class_evidence_applied": candidate.get(
                    "class_evidence_applied"
                ),
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

    @staticmethod
    def _mixture_report(mixture):
        """
        The multi-component finding, carried beside the decision.

        A decision has ONE level and that level names at most one
        material. When a sample is a spike of something in ordinary
        soil, the honest single-material answer is "the soil" - and it
        throws away the only part the operator cared about.

        So the components travel separately, with three things attached
        to every one of them:

            spectral_contribution   what the fit used
            is_mass_fraction        False. Always, currently.
            quantity_model          which validated model converted it,
                                    or None

        `is_mass_fraction` is a field rather than a docstring because a
        reader six months from now gets it from the record instead of
        from the source, and because the day a quantity model IS
        validated, the field is where that becomes visible. Until then
        it says False on every row, which is exactly the claim the
        instrument can support.
        """
        if not mixture:
            return None

        components = mixture.get("components") or []

        return {
            "status": mixture.get("status"),
            "method": mixture.get("method"),
            "component_count": len(components),
            "components": [
                {
                    "material_key": component.get("material"),
                    "spectral_contribution": component.get(
                        "spectral_contribution"
                    ),
                    "normalized_contribution": component.get(
                        "normalized_contribution"
                    ),
                    "is_mass_fraction": False,
                    "quantity_model": None,
                }
                for component in components
            ],
            "reconstruction_rmse": mixture.get("reconstruction_rmse"),
            "improvement_over_single": mixture.get(
                "improvement_over_single"
            ),
            "caveat": "Spectral contribution is not mass fraction. "
                      "Converting one to the other needs a model "
                      "validated against physically prepared mixtures; "
                      "research/training/evaluate_mixtures.py scores the "
                      "prepared mixtures on file and reports whether one "
                      "holds up yet.",
        }

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

    @staticmethod
    def _coverage(evidence):
        """
        How much measurement the conclusion actually rests on.

        Read straight off the evidence package. It is the first thing
        an operator needs when a result looks wrong: a decision taken
        over three usable reflectance channels out of eighteen is a
        different object from one taken over seventeen, and the level
        and confidence alone do not say which happened.
        """
        reliability = evidence.get("channel_reliability") or {}
        quality_block = evidence.get("quality") or {}

        return {
            "features_total": reliability.get("features_total"),
            "raw_valid_total": reliability.get("raw_valid_total"),
            "normalized_valid_total": reliability.get(
                "normalized_valid_total"
            ),
            "by_illumination": reliability.get("by_illumination"),
            "hardware_qc": (quality_block.get("hardware") or {}).get(
                "status"
            ),
            "normalization": (quality_block.get("normalization") or {}).get(
                "status"
            ),
        }

    def _gates(self, decision, fusion, separation, unknown, families):
        """
        Each step of the ladder, with its verdict and the numbers behind it.

        THE LADDER ALREADY MAKES THESE DECISIONS. `_resolve` walks
        usability, then material, then family, then ambiguity, and
        returns at the first honest answer - so the reason a result is
        MATERIAL_FAMILY rather than KNOWN_MATERIAL is a comparison that
        has already been computed and then discarded.

        This records it. NOTHING HERE RE-DECIDES ANYTHING and nothing
        here is a post-hoc rationalisation: every value is the same
        object the corresponding branch of `_resolve` tested, and the
        verdicts are derived from the level that branch produced.

        A gate is PASS, FAIL or NOT_REACHED - the third because the
        ladder stops, and a gate that never ran must not be reported as
        one that failed.
        """
        level = decision.get("level")
        candidates = fusion["candidates"]
        leader = candidates[0] if candidates else {}

        corroboration = decision.get("corroboration") or {}
        required = corroboration.get("required_relative_margin")
        observed = corroboration.get("observed_relative_margin")

        gates = []

        # 1 + 2. Usable, and inside a known domain?
        gates.append({
            "gate": "MEASUREMENT_USABLE",
            "verdict": "FAIL" if unknown["unknown_required"] else "PASS",
            "detail": (
                unknown["reasons"][0]["detail"]
                if unknown["unknown_required"] and unknown["reasons"]
                else "no severe doubt, and fewer than {} moderate "
                     "ones".format(MODERATE_REASONS_FOR_UNKNOWN)
            ),
            "severe_doubts": unknown["severe"],
            "moderate_doubts": unknown["moderate"],
            "codes": [entry["code"] for entry in unknown["reasons"]],
        })

        # A candidate to judge at all.
        gates.append({
            "gate": "CANDIDATES_PRESENT",
            "verdict": (
                "NOT_REACHED" if unknown["unknown_required"]
                else "PASS" if candidates else "FAIL"
            ),
            "detail": (
                "the ladder stopped before this test; {} candidate(s) "
                "from {} database(s) had been found".format(
                    len(candidates), len(fusion["per_database"]))
                if unknown["unknown_required"]
                else "{} candidate(s) from {} database(s)".format(
                    len(candidates), len(fusion["per_database"]))
            ),
        })

        # 6. Strong enough for an exact material?
        if unknown["unknown_required"] or not candidates:
            material_verdict = "NOT_REACHED"
            material_detail = (
                "the ladder stopped before the material test"
            )

        else:
            strength = leader.get("evidence_strength")
            strong = (
                strength is not None
                and strength >= KNOWN_MATERIAL_MIN_STRENGTH
            )
            separated = (
                required is not None and observed is not None
                and observed >= required
            )

            material_verdict = "PASS" if level == KNOWN_MATERIAL else "FAIL"

            if strong and separated:
                material_detail = (
                    "{} carries {:.3f} evidence (needs {:.2f}) and leads by "
                    "{:.0%} of its own strength (needs {:.0%})".format(
                        leader.get("material"), strength,
                        KNOWN_MATERIAL_MIN_STRENGTH, observed, required,
                    )
                )

            elif not strong:
                material_detail = (
                    "{} carries {} evidence, below the {:.2f} needed to "
                    "name a material".format(
                        leader.get("material"),
                        "{:.3f}".format(strength)
                        if strength is not None else "no",
                        KNOWN_MATERIAL_MIN_STRENGTH,
                    )
                )

            else:
                material_detail = (
                    "{} leads by only {:.0%} of its own strength; {:.0%} is "
                    "needed with {} independent source(s)".format(
                        leader.get("material"), observed or 0.0,
                        required or 0.0,
                        corroboration.get("independent_sources"),
                    )
                )

        gates.append({
            "gate": "MATERIAL_LEVEL",
            "verdict": material_verdict,
            "detail": material_detail,
            "leader": leader.get("material"),
            "leader_strength": leader.get("evidence_strength"),
            "min_strength": KNOWN_MATERIAL_MIN_STRENGTH,
            "observed_relative_margin": observed,
            "required_relative_margin": required,
            "independent_sources": corroboration.get("independent_sources"),
            "corroboration_possible": corroboration.get(
                "corroboration_possible"
            ),
        })

        # 3. Does a family hold instead?
        if material_verdict in ("PASS", "NOT_REACHED"):
            family_verdict = "NOT_REACHED"
            family_detail = (
                "not consulted: the material test already answered"
                if material_verdict == "PASS"
                else "the ladder stopped before the family test"
            )

        else:
            family_verdict = "PASS" if families.get("decisive") else "FAIL"
            share = families.get("leader_share")
            family_detail = (
                "the {} family carries {} of the candidate evidence".format(
                    families.get("leader") or "leading",
                    "{:.0%}".format(share) if share is not None else "none",
                )
                + ("" if families.get("decisive")
                   else ", which is not decisive")
            )

        gates.append({
            "gate": "FAMILY_LEVEL",
            "verdict": family_verdict,
            "detail": family_detail,
            "leader": families.get("leader"),
            "leader_share": families.get("leader_share"),
        })

        # 5. Several candidates that cannot be told apart.
        #
        # NOT_REACHED whenever the ladder stopped earlier, which
        # includes the case that used to be reported as FAIL: an
        # unusable measurement returns UNKNOWN from step 1 and the
        # ambiguity test is never run. Saying FAIL there described a
        # comparison that had not happened.
        if unknown["unknown_required"] or not candidates:
            ambiguity_verdict = "NOT_REACHED"
            ambiguity_detail = "the ladder stopped before the "\
                               "ambiguity test"

        elif level in (KNOWN_MATERIAL, MATERIAL_FAMILY):
            ambiguity_verdict = "NOT_REACHED"
            ambiguity_detail = "not consulted: an earlier step already "\
                               "answered"

        elif level == AMBIGUOUS_SET:
            ambiguity_verdict = "PASS"
            ambiguity_detail = (
                "{} candidate(s) lie within the leader's own "
                "margin".format(len(decision.get("candidates") or []))
            )

        else:
            ambiguity_verdict = "FAIL"
            ambiguity_detail = (
                "no second candidate lies inside the leader's margin"
            )

        gates.append({
            "gate": "AMBIGUOUS_SET",
            "verdict": ambiguity_verdict,
            "detail": ambiguity_detail,
            "margin": separation.get("margin"),
            "relative_margin": separation.get("relative_margin"),
            "runner_up": separation.get("runner_up"),
        })

        return gates

    def _confidence_reasons(self, decision, unknown, fusion):
        """
        Why the confidence is what it is, as the penalties that produced it.

        `_confidence` counts penalties and maps the total onto a word.
        Counting them again here would be a second implementation that
        could drift, so this lists the SAME conditions and each says
        whether it applied - the total is the one `_confidence`
        returned.
        """
        leader = (fusion["candidates"] or [{}])[0]
        sources = leader.get("independent_sources") or 0

        reasons = []

        for entry in unknown["reasons"]:
            weight = 3 if entry["severity"] == SEVERE else (
                1 if entry["severity"] == MODERATE else 0
            )

            if weight:
                reasons.append({
                    "penalty": weight,
                    "code": entry["code"],
                    "detail": entry["detail"],
                })

        if sources < 2:
            reasons.append({
                "penalty": 1,
                "code": "SINGLE_SOURCE",
                "detail": "only {} database supports the leading "
                          "candidate".format(sources or "no"),
            })

        if not fusion.get("class_support_available"):
            reasons.append({
                "penalty": 1,
                "code": "NO_CLASS_STATISTICS",
                "detail": "no measured class distribution exists yet, so "
                          "'inside the region this material occupies' "
                          "could not be checked",
            })

        return reasons

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

# ====================================================================
# how much a database's word is worth
# ====================================================================
# Not 'how good is this database' in the abstract. When this
# database said X, how often was the verified truth X? A prior
# until enough answers have been checked to measure it.

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

# ====================================================================
# evidence fusion
# ====================================================================
# Turns per-database, per-metric evidence into per-candidate
# support. Members of one metric family never get two votes, and a
# database's vote is weighted by what its word has been worth.

# Families whose members are mathematically dependent get one vote.
FAMILY_WEIGHTS = {
    "magnitude": 1.0,
    "angular": 1.0,
    "centered_shape": 1.0,
}

FAMILY_WEIGHT_STATUS = "PROVISIONAL_UNVALIDATED"

# A class reliability of WEAK removes the vote entirely; the answer is
# still reported. Same rule the previous inference layer used, kept
# because it was right and is now measured rather than assumed.
NON_VOTING_RATINGS = (WEAK,)

# Support below this is not evidence for anything.
MIN_SUPPORT = 0.05

# How many robust deviations clear of the field a winner must stand
# before its lead counts for everything it could.
#
# WHY THIS EXISTS. Median-relative support is a RELATIVE scale, and on
# its own it is degenerate: whoever wins gets 1.0 and everyone else 0,
# whether the lead was 0.4 or 0.0005. A field of three carbonates
# separated by five thousandths of a cosine produced "support 1.0,
# margin 1.0, name the material" - which is precisely the magnitude-blind
# behaviour that replacing rank aggregation was meant to end.
#
# So relative standing is scaled by how unusual the winner is against the
# field's own scatter, and by how good its fit is in absolute terms. A
# winner that leads a tight pack by a hair now earns a small fraction of
# the support that one standing clear of the field does.
#
# PROVISIONAL: four deviations is an engineering judgement, not a
# measured threshold.
Z_FOR_FULL_CONFIDENCE = 4.0

# A winner that fits badly in absolute terms cannot earn full support
# however far ahead of a worse field it is. Absolute goodness is reported
# per metric by Science/distances.py.
USE_ABSOLUTE_GOODNESS = True


def _support_from(scores, higher_is_better):
    """
    Place every candidate on a 0..1 scale relative to the field.

    Returns {} when the field cannot discriminate at all - every score
    identical - which is itself a finding and must not be smoothed over.
    """
    usable = {
        name: value for name, value in scores.items()
        if isinstance(value, (int, float))
    }

    if len(usable) < 2:
        return {}

    values = sorted(usable.values())
    middle = len(values) // 2

    median = (
        values[middle] if len(values) % 2
        else (values[middle - 1] + values[middle]) / 2.0
    )

    best = max(values) if higher_is_better else min(values)
    span = abs(best - median)

    if span <= 0:
        return {}

    support = {}

    for name, value in usable.items():
        raw = (value - median) if higher_is_better else (median - value)
        support[name] = max(0.0, min(1.0, raw / span))

    return support


def database_support(database_result, reliability, database_key):
    """
    Candidate support from ONE database, one vote per metric family.

    `database_result` is the reference_analysis entry the measurement
    layer produced. Its per-metric `top` lists and score distributions
    are what carry magnitude, so they are what is used.
    """
    metrics = (database_result or {}).get("metrics") or {}
    families = (database_result or {}).get("families") or {}

    per_candidate = {}
    family_detail = {}

    for family, summary in families.items():
        metric = summary.get("metric")
        definition = metrics.get(metric) or {}

        scores = {
            entry["material"]: entry["score"]
            for entry in definition.get("top") or []
        }

        # `top` is truncated, which is exactly right: support is about
        # standing out from the field, and a candidate outside the top
        # of its own metric is not standing out.
        support = _support_from(
            scores, definition.get("higher_is_better", True)
        )

        weight = FAMILY_WEIGHTS.get(family, 1.0)

        # Relative standing is only half the story. Scale it by how far
        # the winner stands out from the field's own scatter, and by how
        # good the fit is in absolute terms - see Z_FOR_FULL_CONFIDENCE.
        separation = summary.get("z_separation")

        confidence = (
            min(1.0, separation / Z_FOR_FULL_CONFIDENCE)
            if separation else 0.25
        )

        goodness = summary.get("absolute_goodness")

        if USE_ABSOLUTE_GOODNESS and goodness is not None:
            confidence *= max(0.0, min(1.0, goodness))

        family_detail[family] = {
            "metric": metric,
            "winner": summary.get("winner"),
            "absolute_margin": summary.get("absolute_margin"),
            "relative_margin": summary.get("relative_margin"),
            "z_separation": separation,
            "absolute_goodness": goodness,
            "separation_confidence": round(confidence, 4),
            "weight": weight,
            "support": {
                name: round(value * confidence, 4)
                for name, value in support.items()
            },
        }

        for name, value in support.items():
            scaled = value * confidence

            entry = per_candidate.setdefault(
                name, {"weighted": 0.0, "families": {}}
            )
            entry["weighted"] += weight * scaled
            entry["families"][family] = round(scaled, 4)

    total_weight = sum(FAMILY_WEIGHTS.get(f, 1.0) for f in family_detail)

    candidates = {}

    for name, entry in per_candidate.items():
        strength = entry["weighted"] / total_weight if total_weight else 0.0

        class_rating = reliability.class_reliability(database_key, name)

        candidates[name] = {
            "support": round(strength, 4),
            "families": entry["families"],
            "family_agreement": len(entry["families"]),
            "class_reliability": class_rating["rating"],
            "class_reliability_basis": class_rating["basis"],
            "votes": class_rating["rating"] not in NON_VOTING_RATINGS,
        }

    return {
        "database": database_key,
        "candidates": candidates,
        "families": family_detail,
        "families_available": sorted(family_detail),
        "database_weight": reliability.database_weight(database_key),
    }


def fuse(reference_analysis, reliability, class_analysis=None):
    """
    Combine every database's support into one candidate table.

    Databases are weighted, never pooled: each contribution is kept, so
    the answer to "why?" is always "DB1 said this much, DB3 said that
    much, and DB3's answer was discounted because its class reliability
    for that class was measured WEAK".
    """
    databases = (reference_analysis or {}).get("databases") or {}

    per_database = {}
    fused = {}
    discounted = []

    for key, result in sorted(databases.items()):
        if not result.get("available"):
            continue

        support = database_support(result, reliability, key)
        per_database[key] = support

        weight = support["database_weight"]

        for name, entry in support["candidates"].items():
            if not entry["votes"]:
                discounted.append({
                    "database": key,
                    "material": name,
                    "reason": "class reliability measured {}".format(
                        entry["class_reliability"]
                    ),
                })

                continue

            record = fused.setdefault(name, {
                "material": name,
                "strength": 0.0,
                "weight": 0.0,
                "databases": {},
            })

            record["strength"] += weight * entry["support"]
            record["weight"] += weight
            record["databases"][key] = entry

    candidates = []

    for record in fused.values():
        strength = (
            record["strength"] / record["weight"] if record["weight"] else 0.0
        )

        candidates.append({
            "material": record["material"],
            "evidence_strength": round(strength, 4),
            "supporting_databases": sorted(record["databases"]),
            "independent_sources": len(record["databases"]),
            "per_database": record["databases"],
        })

    # Class distance is separate evidence and is added, never averaged
    # in: it answers a different question - "does the sample lie inside
    # the region this material has been seen to occupy" - and a library
    # cosine cannot substitute for it.
    class_support = _class_support(class_analysis)

    for candidate in candidates:
        entry = class_support.get(candidate["material"])

        candidate["class_evidence"] = entry

        if entry and entry.get("support") is not None:
            candidate["evidence_strength"] = round(
                0.7 * candidate["evidence_strength"]
                + 0.3 * entry["support"], 4
            )
            candidate["class_evidence_applied"] = True

        else:
            candidate["class_evidence_applied"] = False

    candidates.sort(
        key=lambda item: (-item["evidence_strength"], item["material"])
    )

    return {
        "candidates": candidates,
        "per_database": per_database,
        "discounted": discounted,
        "class_support_available": bool(class_support),
        "family_weights": dict(FAMILY_WEIGHTS),
        "family_weight_status": FAMILY_WEIGHT_STATUS,
        "method": "median-relative support per metric family, one vote "
                  "per family, databases weighted by measured or declared "
                  "reliability. Ranks are never summed.",
    }


def _class_support(class_analysis):
    """
    Turn class distances into 0..1 support, using the class's own scatter.

    The scale is the class's observed within-class spread, not an
    arbitrary constant: "inside the range this material has actually been
    seen to occupy" is a statement the data can support, while "within
    0.1 reflectance units" is one it cannot.
    """
    if not class_analysis or not class_analysis.get("available"):
        return {}

    support = {}

    for material, entry in (class_analysis.get("per_material") or {}).items():
        ratio = entry.get("within_class_ratio")

        if ratio is None:
            support[material] = {
                "support": None,
                "reason": "no within-class scatter yet - needs independent "
                          "repeats of this material",
                "n_independent": entry.get("n_independent"),
            }

            continue

        # ratio 0 -> dead centre, 1 -> as far out as the furthest member
        # ever was, >2 -> outside anything ever seen.
        value = max(0.0, min(1.0, 1.0 - (ratio / 2.0)))

        support[material] = {
            "support": round(value, 4),
            "within_class_ratio": ratio,
            "centroid_distance": entry.get("centroid_distance"),
            "mahalanobis_distance": entry.get("mahalanobis_distance"),
            "n_independent": entry.get("n_independent"),
        }

    return support


def separability(candidates):
    """
    Is the leader actually distinguishable from the field?

    The margin is reported in absolute and relative terms; the decision
    thresholds live in engine.py, because this module measures and does
    not decide.
    """
    usable = [
        candidate for candidate in candidates
        if candidate["evidence_strength"] >= MIN_SUPPORT
    ]

    if not usable:
        return {
            "leader": None, "runner_up": None, "margin": None,
            "relative_margin": None, "candidates_above_floor": 0,
        }

    leader = usable[0]
    runner_up = usable[1] if len(usable) > 1 else None

    margin = (
        leader["evidence_strength"] - runner_up["evidence_strength"]
        if runner_up else leader["evidence_strength"]
    )

    return {
        "leader": leader["material"],
        "leader_strength": leader["evidence_strength"],
        "runner_up": runner_up["material"] if runner_up else None,
        "runner_up_strength": (
            runner_up["evidence_strength"] if runner_up else None
        ),
        "margin": round(margin, 4),
        "relative_margin": (
            round(margin / leader["evidence_strength"], 4)
            if leader["evidence_strength"] > 0 else None
        ),
        "candidates_above_floor": len(usable),
    }

# ====================================================================
# family and ambiguity
# ====================================================================
# When the material cannot be named, the family often can. This is
# what lets the answer degrade to 'a carbonate' instead of jumping
# to a specific carbonate it cannot justify.

# A family needs this share of the candidate evidence before it is
# reported as the answer. PROVISIONAL.
FAMILY_SUPPORT_THRESHOLD = 0.45

# And this much more than the next family, or the families themselves are
# not separable and the honest answer is the candidate set.
FAMILY_MARGIN_THRESHOLD = 0.15

# How many candidates are considered when looking for a family. Beyond
# this the tail is noise: every library entry has SOME support.
FAMILY_CANDIDATE_DEPTH = 6


def family_support(candidates, taxonomy, depth=FAMILY_CANDIDATE_DEPTH):
    """
    Aggregate candidate evidence by family.

    Support is summed within a family, not averaged: three carbonates
    each with moderate support IS stronger evidence for "carbonate" than
    one carbonate with the same moderate support, and averaging would
    erase exactly that.
    """
    totals = {}

    for candidate in candidates[:depth]:
        material = candidate["material"]
        family = taxonomy.family_of(material) if taxonomy else None

        if not family:
            continue

        entry = totals.setdefault(family, {
            "family": family,
            "support": 0.0,
            "materials": [],
        })

        entry["support"] += candidate["evidence_strength"]
        entry["materials"].append({
            "material": material,
            "evidence_strength": candidate["evidence_strength"],
            "supporting_databases": candidate.get("supporting_databases"),
        })

    total = sum(entry["support"] for entry in totals.values())

    families = []

    for entry in totals.values():
        families.append({
            "family": entry["family"],
            "support": round(entry["support"], 4),
            "share": round(entry["support"] / total, 4) if total else 0.0,
            "materials": sorted(
                entry["materials"],
                key=lambda item: -item["evidence_strength"],
            ),
            "member_count": len(entry["materials"]),
        })

    families.sort(key=lambda entry: (-entry["support"], entry["family"]))

    leader = families[0] if families else None
    runner_up = families[1] if len(families) > 1 else None

    margin = (
        leader["share"] - runner_up["share"]
        if leader and runner_up else (leader["share"] if leader else 0.0)
    )

    return {
        "families": families,
        "leader": leader["family"] if leader else None,
        "leader_share": leader["share"] if leader else None,
        "runner_up": runner_up["family"] if runner_up else None,
        "margin": round(margin, 4),
        "decisive": bool(
            leader
            and leader["share"] >= FAMILY_SUPPORT_THRESHOLD
            and margin >= FAMILY_MARGIN_THRESHOLD
        ),
        "thresholds": {
            "support": FAMILY_SUPPORT_THRESHOLD,
            "margin": FAMILY_MARGIN_THRESHOLD,
            "status": "PROVISIONAL",
        },
    }


def ambiguous_set(candidates, separation, limit=4):
    """
    The candidates that cannot be told apart from the leader.

    Everything within the leader's own margin is in the set. Picking a
    winner from inside that band would be choosing on noise, which is
    what "AMBIGUOUS_SET" exists to refuse. §22.
    """
    if not candidates:
        return []

    leader_strength = candidates[0]["evidence_strength"]
    margin = separation.get("margin") or 0.0

    # The band is the leader's lead over the runner-up, floored so that a
    # zero margin does not collapse the set to one member.
    #
    # TOLERANCE, not decoration: `margin` arrives rounded while the
    # strengths do not, so the runner-up that DEFINED the margin can
    # fail a bare <= by 4e-17 and vanish from the set it created. That
    # turned a two-candidate ambiguity into a lone leader, and then into
    # UNKNOWN, on floating-point noise alone.
    band = max(margin, 0.05) + 1e-9

    members = [
        candidate for candidate in candidates
        if leader_strength - candidate["evidence_strength"] <= band
    ]

    return members[:limit]


def contextual_neighbours(candidates, taxonomy, limit=3):
    """
    Nearest known things, for an UNKNOWN result.

    "I do not know, and the nearest things I know are these" is more
    useful than "I do not know", and it is still not a claim about what
    the sample is. §22.
    """
    neighbours = []

    for candidate in candidates[:limit]:
        neighbours.append({
            "material": candidate["material"],
            "family": (
                taxonomy.family_of(candidate["material"])
                if taxonomy else None
            ),
            "evidence_strength": candidate["evidence_strength"],
        })

    return neighbours

# ====================================================================
# unknown detection
# ====================================================================
# The check that stops every sample being forced into a known
# class. A sample the library has never seen must come out UNKNOWN,
# not as the nearest thing in a library that does not contain it.

# Beyond this many times the class's own worst within-class distance, the
# sample is outside anything that class has ever been seen to do.
# PROVISIONAL: it needs several independent repeats per class before it
# can be calibrated, and with one observation per class it cannot fire.
OUTSIDE_CLASS_RATIO = 2.0

# Evidence strength below which nothing in the library really fits.
WEAK_SUPPORT = 0.25

# Relative margin below which the leader is not separable from the field.
NOT_SEPARABLE_MARGIN = 0.15

# Reasons of this severity or worse force UNKNOWN on their own.
SEVERE = "SEVERE"
MODERATE = "MODERATE"
MILD = "MILD"

# Two moderate reasons are enough to refuse.
MODERATE_REASONS_FOR_UNKNOWN = 2


def assess_unknown(evidence, fusion, separation):
    """
    Every independent reason to answer UNKNOWN, with its severity.

    Returns the reasons and whether they compel a refusal. The engine
    applies it; this module only measures.
    """
    reasons = []

    quality = (evidence.get("quality") or {})
    hardware = (quality.get("hardware") or {})
    normalization = (quality.get("normalization") or {})

    if hardware.get("status") == "HARDWARE_QC_FAIL":
        reasons.append({
            "code": "HARDWARE_QC_FAIL",
            "severity": SEVERE,
            "detail": "the instrument did not produce a usable "
                      "measurement, so nothing derived from it can be "
                      "trusted",
        })

    reliability = evidence.get("channel_reliability") or {}
    total = reliability.get("features_total") or 0
    normalized_valid = reliability.get("normalized_valid_total") or 0

    if total and normalized_valid < total * 0.25:
        reasons.append({
            "code": "TOO_FEW_RELIABLE_FEATURES",
            "severity": SEVERE,
            "detail": "only {}/{} features support a usable reflectance"
                      .format(normalized_valid, total),
        })

    candidates = fusion.get("candidates") or []

    if not candidates:
        reasons.append({
            "code": "NO_CANDIDATES",
            "severity": SEVERE,
            "detail": "no database produced a comparable candidate",
        })

    else:
        leader = candidates[0]

        if leader["evidence_strength"] < WEAK_SUPPORT:
            reasons.append({
                "code": "NO_GOOD_MATCH",
                "severity": MODERATE,
                "detail": "the best candidate stands out from the field by "
                          "only {:.2f} on a 0-1 scale; in a library where "
                          "everything scores alike, that is not a match"
                          .format(leader["evidence_strength"]),
            })

    relative_margin = separation.get("relative_margin")

    if relative_margin is not None and relative_margin < NOT_SEPARABLE_MARGIN:
        reasons.append({
            "code": "NOT_SEPARABLE",
            "severity": MODERATE,
            "detail": "the leader is only {:.0%} clear of the runner-up"
                      .format(relative_margin),
        })

    class_analysis = evidence.get("class_analysis") or {}

    if class_analysis.get("available"):
        nearest = class_analysis.get("nearest")
        entry = (class_analysis.get("per_material") or {}).get(nearest) or {}
        ratio = entry.get("within_class_ratio")

        if ratio is not None and ratio > OUTSIDE_CLASS_RATIO:
            reasons.append({
                "code": "OUTSIDE_KNOWN_CLASSES",
                "severity": SEVERE,
                "detail": "the sample sits {:.1f}x further from the nearest "
                          "class centroid than any member of that class "
                          "ever has".format(ratio),
            })

    else:
        reasons.append({
            "code": "NO_CLASS_STATISTICS",
            "severity": MILD,
            "detail": class_analysis.get("reason")
            or "no class distributions exist yet, so 'inside a known "
               "class' cannot be checked at all",
        })

    if normalization.get("status") == "NORMALIZATION_UNUSABLE":
        reasons.append({
            "code": "NORMALIZATION_UNUSABLE",
            "severity": MODERATE,
            "detail": "the reference division is too ill-conditioned for "
                      "reflectance-based comparison; the raw measurement "
                      "is unaffected",
        })

    severe = [entry for entry in reasons if entry["severity"] == SEVERE]
    moderate = [entry for entry in reasons if entry["severity"] == MODERATE]

    return {
        "reasons": reasons,
        "severe": len(severe),
        "moderate": len(moderate),
        "unknown_required": bool(
            severe or len(moderate) >= MODERATE_REASONS_FOR_UNKNOWN
        ),
        "thresholds": {
            "outside_class_ratio": OUTSIDE_CLASS_RATIO,
            "weak_support": WEAK_SUPPORT,
            "not_separable_margin": NOT_SEPARABLE_MARGIN,
            "status": "PROVISIONAL - not yet validated against a set of "
                      "samples known to be outside the library",
        },
    }

# ====================================================================
# explanation
# ====================================================================
# Structured reasons, both supporting and conflicting, so a
# conclusion can be argued with. Not prose: the words here are
# labels on evidence, and the report that a person reads is built
# outside this project entirely.

FAMILY_WORDS = {
    "magnitude": "magnitude",
    "angular": "shape",
    "centered_shape": "correlation",
}

LEVEL_SENTENCE = {
    "KNOWN_MATERIAL": "The evidence supports naming a single material.",
    "MATERIAL_FAMILY": "The evidence supports a family but not an "
                       "individual material.",
    "AMBIGUOUS_SET": "Several candidates are statistically "
                     "indistinguishable, so none is chosen.",
    "UNKNOWN": "The evidence does not support naming a material.",
}


def _percent(value):
    return "{:.0%}".format(value) if isinstance(value, float) else "-"


def database_sentences(fusion, decision_material=None):
    """One sentence per database, naming what it actually said."""
    sentences = []

    for key, support in sorted((fusion.get("per_database") or {}).items()):
        families = support.get("families") or {}

        if not families:
            continue

        clauses = []

        for family, detail in sorted(families.items()):
            winner = detail.get("winner")

            if not winner:
                continue

            separation = detail.get("z_separation")

            if separation:
                clauses.append(
                    "{} evidence favours {} ({:.1f} robust deviations "
                    "clear of the field)".format(
                        FAMILY_WORDS.get(family, family), winner, separation
                    )
                )

            else:
                clauses.append(
                    "{} evidence favours {}".format(
                        FAMILY_WORDS.get(family, family), winner
                    )
                )

        if not clauses:
            continue

        sentence = "{}: {}.".format(key, "; ".join(clauses))

        if decision_material:
            candidate = (support.get("candidates") or {}).get(
                decision_material
            )

            if candidate and candidate["class_reliability"] in (
                WEAK,
            ):
                sentence += (
                    " Its answer for that class is discounted: measured "
                    "reliability is {}.".format(candidate["class_reliability"])
                )

        sentences.append(sentence)

    return sentences


def quality_sentences(evidence):
    """What the measurement itself supports, in plain terms."""
    sentences = []

    quality = evidence.get("quality") or {}
    hardware = (quality.get("hardware") or {}).get("status")
    normalization = (quality.get("normalization") or {}).get("status")
    reliability = evidence.get("channel_reliability") or {}

    total = reliability.get("features_total")
    raw_valid = reliability.get("raw_valid_total")
    normalized_valid = reliability.get("normalized_valid_total")

    if total:
        sentences.append(
            "{}/{} raw features are valid and {}/{} of them support a "
            "usable reflectance.".format(
                raw_valid, total, normalized_valid, total
            )
        )

    if hardware == "HARDWARE_QC_FAIL":
        sentences.append(
            "Hardware quality control failed, so no representation of "
            "this measurement is trustworthy."
        )

    elif normalization and normalization != "OK":
        sentences.append(
            "The raw counts are valid; it is the reference division that "
            "is poorly conditioned, so reflectance-based evidence carries "
            "less weight here than raw evidence."
        )

    return sentences


def class_sentences(evidence):
    """What the class distributions say, or why they say nothing yet."""
    class_analysis = evidence.get("class_analysis") or {}

    if not class_analysis.get("available"):
        return [
            "No class distributions exist yet, so how typical this sample "
            "is for any material could not be checked."
        ]

    nearest = class_analysis.get("nearest")
    entry = (class_analysis.get("per_material") or {}).get(nearest) or {}

    sentences = []

    if nearest:
        ratio = entry.get("within_class_ratio")

        if ratio is not None:
            sentences.append(
                "The nearest class distribution is {} and the sample sits "
                "{:.2f}x the furthest distance any verified member of that "
                "class has reached from its centroid.".format(nearest, ratio)
            )

        else:
            sentences.append(
                "The nearest class centroid is {}, which has too few "
                "independent measurements for a scatter to compare "
                "against.".format(nearest)
            )

    knn = class_analysis.get("knn")

    if knn:
        sentences.append(
            "Its {} nearest verified observations are {}.".format(
                knn["k"],
                ", ".join(
                    "{}".format(name) for name in sorted(knn["neighbour_votes"])
                ),
            )
        )

    return sentences


def explain(decision, evidence, fusion, unknown_report):
    """
    The full explanation, assembled from the parts above.

    Deterministic: the same decision object always produces the same
    text, which is what makes it quotable in a report.
    """
    level = decision.get("level")

    parts = [LEVEL_SENTENCE.get(level, "")]

    if level == "KNOWN_MATERIAL" and decision.get("material"):
        separation = decision.get("separation") or {}

        parts.append(
            "{} leads the field with evidence strength {:.2f} and is "
            "{:.0%} clear of {}.".format(
                decision["material"],
                separation.get("leader_strength") or 0.0,
                separation.get("relative_margin") or 0.0,
                separation.get("runner_up") or "the runner-up",
            )
        )

    if level == "MATERIAL_FAMILY" and decision.get("family"):
        family = decision.get("family_evidence") or {}

        parts.append(
            "The {} family carries {} of the candidate evidence across "
            "{} member(s); no single member is far enough ahead of the "
            "others to be named.".format(
                decision["family"],
                _percent(family.get("leader_share")),
                len((decision.get("candidates") or [])),
            )
        )

    if level == "AMBIGUOUS_SET":
        parts.append(
            "The candidates listed are within one another's margin: {}."
            .format(
                ", ".join(
                    candidate["material"]
                    for candidate in decision.get("candidates") or []
                )
            )
        )

    if level == "UNKNOWN":
        for reason in (unknown_report.get("reasons") or [])[:3]:
            parts.append("{}: {}.".format(reason["code"], reason["detail"]))

    parts.extend(database_sentences(fusion, decision.get("material")))
    parts.extend(quality_sentences(evidence))
    parts.extend(class_sentences(evidence))

    mixture = decision.get("secondary_interpretations") or []

    if "MIXTURE_PLAUSIBLE" in mixture:
        parts.append(
            "A non-negative combination of library spectra describes the "
            "measurement better than any single one; the coefficients are "
            "spectral contributions and are not mass fractions."
        )

    parts.append(
        "This is a comparative spectral result, not a chemical "
        "identification, and the confidence level is a judgement about "
        "the structure of the evidence rather than a probability."
    )

    return " ".join(part for part in parts if part)
