"""
The Science Plan — what we said we would do, before we did it.

This is the pre-competition artifact: geological map units, their
formation processes and relative ages, the subject, why it matters, one
falsifiable hypothesis, and the predictions that will test it.

Two properties matter more than anything else here.

**The hypothesis is frozen.** Once a science run starts, the hypothesis,
its support condition, its reject condition and its inconclusive
condition become immutable. `freeze()` records a content hash; anything
that edits them afterwards is refused. Rewriting a hypothesis after
seeing the data is the single easiest way to turn an experiment into a
story, so the software makes it impossible rather than discouraged.

**Character limits are enforced, never applied.** The rules say longer
answers WILL NOT BE READ. Silently truncating to fit would delete
scientific content and hide the loss, so a validator reports the exact
overrun and refuses to export.

Geology is operator data. This module stores, links and validates
geological features; it never generates one. A formation process or a
relative age that no human asserted does not appear.

Layer rule: Science may import BD, Measurements and DecisionModel.
"""

import hashlib
import json

from Science import config

# Where a claim came from. Photo-interpretation of the supplied drone
# image is legitimate evidence for a planning map, but it is not the same
# as having stood on the ground, and the map must say which it is.
PROVENANCE_SOURCE_MAP = "SOURCE_MAP_ORTHOPHOTO"
PROVENANCE_SOURCE_TABLE = "SOURCE_COORDINATE_TABLE"
PROVENANCE_FIELD = "FIELD_OBSERVATION"
PROVENANCE_LITERATURE = "LITERATURE"
PROVENANCE_INFERRED = "INFERRED_FROM_MAPPED_RELATIONS"

PROVENANCES = (
    PROVENANCE_SOURCE_MAP,
    PROVENANCE_SOURCE_TABLE,
    PROVENANCE_FIELD,
    PROVENANCE_LITERATURE,
    PROVENANCE_INFERRED,
)

# Review state of a geological assertion.
DRAFT = "DRAFT_REQUIRES_GEOLOGIST_REVIEW"
REVIEWED = "REVIEWED_BY_GEOLOGIST"

# Hypothesis outcomes. INCONCLUSIVE is a first-class result, not a
# failure to produce one.
SUPPORTED = "SUPPORTED"
REJECTED = "REJECTED"
INCONCLUSIVE = "INCONCLUSIVE"
NOT_EVALUATED = "NOT_EVALUATED"

OUTCOMES = (SUPPORTED, REJECTED, INCONCLUSIVE, NOT_EVALUATED)

# References the rules explicitly refuse.
FORBIDDEN_REFERENCE_HOSTS = (
    "wikipedia.org",
    "en.wikipedia.org",
)

# NASA *web pages* are refused; a NASA-hosted journal article or data
# release is not a web page in that sense, so the check looks for the
# bare-domain case and leaves anything with a DOI alone.
NASA_HOSTS = ("nasa.gov", "www.nasa.gov", "mars.nasa.gov", "science.nasa.gov")


class PlanError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _hash(payload):
    """Stable content hash, used to prove a hypothesis did not change."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class GeologicalFeature:
    """
    One mapped unit or feature.

    Carries what O/SCI-010 through O/SCI-040 ask for: an identity, an
    outline, a formation process, a relative age and a legend entry -
    each with the provenance of the claim.
    """

    def __init__(self, entry):
        self.feature_id = entry["feature_id"]
        self.name = entry["name"]
        self.description = entry.get("description")

        # Geometry is a list of {x_m, y_m} vertices, or the ids of
        # surveyed points the unit is anchored to. A unit with neither
        # cannot be drawn and fails O/SCI-010.
        self.outline = list(entry.get("outline") or [])
        self.anchor_points = list(entry.get("anchor_points") or [])

        self.formation_process = entry.get("formation_process")
        self.formation_justification = entry.get("formation_justification")

        # Relative age as an integer rank: 1 is oldest. Ranks may tie.
        self.relative_age_rank = entry.get("relative_age_rank")
        self.relative_age_note = entry.get("relative_age_note")
        self.older_than = list(entry.get("older_than") or [])
        self.younger_than = list(entry.get("younger_than") or [])

        self.legend_entry = entry.get("legend_entry")
        self.legend_colour = entry.get("legend_colour")

        self.provenance = entry.get("provenance")
        self.review_status = entry.get("review_status", DRAFT)

    @property
    def has_geometry(self):
        return bool(self.outline or self.anchor_points)

    def as_dict(self):
        return {
            "feature_id": self.feature_id,
            "name": self.name,
            "description": self.description,
            "outline": self.outline,
            "anchor_points": self.anchor_points,
            "formation_process": self.formation_process,
            "formation_justification": self.formation_justification,
            "relative_age_rank": self.relative_age_rank,
            "relative_age_note": self.relative_age_note,
            "older_than": self.older_than,
            "younger_than": self.younger_than,
            "legend_entry": self.legend_entry,
            "legend_colour": self.legend_colour,
            "provenance": self.provenance,
            "review_status": self.review_status,
        }

    def __repr__(self):
        return "<Feature {} {}>".format(self.feature_id, self.name)


class Prediction:
    """
    One testable consequence of the hypothesis.

    A prediction that does not name what would refute it is not a
    prediction, so reject_criterion is required rather than optional.
    """

    def __init__(self, entry):
        self.prediction_id = entry["prediction_id"]
        self.hypothesis_id = entry["hypothesis_id"]
        self.statement = entry["statement"]

        self.expected_observation = entry.get("expected_observation")
        self.planned_site_ids = list(entry.get("planned_site_ids") or [])
        self.required_measurements = entry.get("required_measurements")
        self.comparison = entry.get("comparison")

        self.support_criterion = entry.get("support_criterion")
        self.reject_criterion = entry.get("reject_criterion")
        self.inconclusive_criterion = entry.get("inconclusive_criterion")

        self.required_context_observation = entry.get(
            "required_context_observation"
        )
        self.required_photograph = entry.get("required_photograph", True)

    def as_dict(self):
        return {
            "prediction_id": self.prediction_id,
            "hypothesis_id": self.hypothesis_id,
            "statement": self.statement,
            "expected_observation": self.expected_observation,
            "planned_site_ids": self.planned_site_ids,
            "required_measurements": self.required_measurements,
            "comparison": self.comparison,
            "support_criterion": self.support_criterion,
            "reject_criterion": self.reject_criterion,
            "inconclusive_criterion": self.inconclusive_criterion,
            "required_context_observation":
                self.required_context_observation,
            "required_photograph": self.required_photograph,
        }

    def __repr__(self):
        return "<Prediction {}>".format(self.prediction_id)


class Hypothesis:
    """
    The falsifiable claim, and the three conditions that resolve it.

    Immutable once frozen. `content_hash` covers exactly the fields whose
    change would alter what was being tested.
    """

    HASHED_FIELDS = (
        "hypothesis_id",
        "statement",
        "support_condition",
        "reject_condition",
        "inconclusive_condition",
    )

    def __init__(self, entry):
        self.hypothesis_id = entry["hypothesis_id"]
        self.statement = entry["statement"]

        self.support_condition = entry.get("support_condition")
        self.reject_condition = entry.get("reject_condition")
        self.inconclusive_condition = entry.get("inconclusive_condition")

        self.linked_feature_ids = list(entry.get("linked_feature_ids") or [])
        self.linked_site_ids = list(entry.get("linked_site_ids") or [])

        self.created_at = entry.get("created_at")
        self.frozen_at = entry.get("frozen_at")
        self.content_hash = entry.get("content_hash")

        self.outcome = entry.get("outcome", NOT_EVALUATED)

    def payload(self):
        return {name: getattr(self, name) for name in self.HASHED_FIELDS}

    def compute_hash(self):
        return _hash(self.payload())

    @property
    def frozen(self):
        return bool(self.frozen_at and self.content_hash)

    def freeze(self, timestamp):
        """
        Fix the hypothesis before the run.

        Freezing twice is refused rather than ignored: a second freeze
        would quietly re-bless whatever the text says now.
        """
        if self.frozen:
            raise PlanError(
                "ALREADY_FROZEN",
                "hypothesis {} was frozen at {} and cannot be frozen "
                "again".format(self.hypothesis_id, self.frozen_at),
            )

        self.frozen_at = timestamp
        self.content_hash = self.compute_hash()

        return self.content_hash

    def verify_unchanged(self):
        """
        Confirm the frozen text is still the text being tested.

        Returns (ok, detail). Called by the analysis layer before it is
        allowed to publish a verdict.
        """
        if not self.frozen:
            return False, "hypothesis has never been frozen"

        current = self.compute_hash()

        if current != self.content_hash:
            return False, (
                "hypothesis {} has been EDITED since it was frozen at {}. "
                "The frozen hash was {}, the current text hashes to {}. "
                "A verdict against an edited hypothesis is not a test of "
                "the original claim.".format(
                    self.hypothesis_id, self.frozen_at,
                    self.content_hash[:12], current[:12],
                )
            )

        return True, "hypothesis unchanged since {}".format(self.frozen_at)

    def as_dict(self):
        record = self.payload()
        record.update({
            "linked_feature_ids": self.linked_feature_ids,
            "linked_site_ids": self.linked_site_ids,
            "created_at": self.created_at,
            "frozen_at": self.frozen_at,
            "content_hash": self.content_hash,
            "outcome": self.outcome,
        })

        return record

    def __repr__(self):
        return "<Hypothesis {} {}>".format(
            self.hypothesis_id, "FROZEN" if self.frozen else "draft"
        )


class Reference:
    """One cited work."""

    def __init__(self, entry):
        self.reference_id = entry["reference_id"]
        self.citation = entry["citation"]
        self.kind = entry.get("kind")
        self.doi = entry.get("doi")
        self.url = entry.get("url")

    def as_dict(self):
        return {
            "reference_id": self.reference_id,
            "citation": self.citation,
            "kind": self.kind,
            "doi": self.doi,
            "url": self.url,
        }

    def __repr__(self):
        return "<Reference {}>".format(self.reference_id)


class SciencePlan:
    """The whole pre-competition plan, loaded from one document."""

    def __init__(self, document):
        self.document = document

        self.plan_id = document.get("plan_id")
        self.schema_version = document.get("schema_version")
        self.created_at = document.get("created_at")
        self.review_status = document.get("review_status", DRAFT)

        self.subject = document.get("subject")
        self.importance = document.get("importance")
        self.predictions_narrative = document.get("predictions_narrative")

        self.features = {}
        self.feature_order = []

        for entry in document.get("geological_features") or []:
            feature = GeologicalFeature(entry)
            self.features[feature.feature_id] = feature
            self.feature_order.append(feature.feature_id)

        hypothesis_entry = document.get("hypothesis")
        self.hypothesis = (
            Hypothesis(hypothesis_entry) if hypothesis_entry else None
        )

        self.predictions = {}
        self.prediction_order = []

        for entry in document.get("predictions") or []:
            prediction = Prediction(entry)
            self.predictions[prediction.prediction_id] = prediction
            self.prediction_order.append(prediction.prediction_id)

        self.references = {}

        for entry in document.get("references") or []:
            reference = Reference(entry)
            self.references[reference.reference_id] = reference

        self.citations = list(document.get("citations") or [])
        self.prediction_figure = document.get("prediction_figure")

    # ------------------------------------------------------------------
    # loading and saving
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path=None):
        path = path or config.SCIENCE_PLAN_FILE

        try:
            with open(path, "r", encoding="utf-8") as handle:
                return cls(json.load(handle))

        except OSError as error:
            raise PlanError(
                "MISSING",
                "the science plan could not be read from {}: {}".format(
                    path, error
                ),
            )

        except ValueError as error:
            raise PlanError(
                "MALFORMED",
                "the science plan at {} is not valid JSON: {}".format(
                    path, error
                ),
            )

    def as_dict(self):
        record = {
            "plan_id": self.plan_id,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "review_status": self.review_status,
            "subject": self.subject,
            "importance": self.importance,
            "predictions_narrative": self.predictions_narrative,
            "geological_features": [
                self.features[fid].as_dict() for fid in self.feature_order
            ],
            "hypothesis": (
                self.hypothesis.as_dict() if self.hypothesis else None
            ),
            "predictions": [
                self.predictions[pid].as_dict()
                for pid in self.prediction_order
            ],
            "references": [r.as_dict() for r in self.references.values()],
            "citations": self.citations,
            "prediction_figure": self.prediction_figure,
        }

        # Underscore-prefixed keys are human annotations - authorship
        # notes, caveats, lists of what is deliberately still missing.
        # They are not part of the schema, and a round-trip through save()
        # must not quietly delete the note explaining that the geology is
        # unreviewed.
        for key, value in self.document.items():
            if key.startswith("_") and key not in record:
                record[key] = value

        return record

    def save(self, path=None):
        path = path or config.SCIENCE_PLAN_FILE
        path.parent.mkdir(parents=True, exist_ok=True)

        temporary = path.with_suffix(path.suffix + ".tmp")

        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(self.as_dict(), handle, indent=2, ensure_ascii=False)
            handle.write("\n")

        temporary.replace(path)

        return path

    # ------------------------------------------------------------------
    # chronology
    # ------------------------------------------------------------------

    def age_sequence(self):
        """
        Oldest to youngest, or a description of why one cannot be built.

        Returns (sequence, problems). O/SCI-030 asks for relative ages
        "properly assigned"; the machine part of that is that the stated
        relations are consistent and acyclic.
        """
        problems = []

        # Explicit pairwise relations first, since they are stronger than
        # a rank and can contradict one.
        edges = set()

        for feature in self.features.values():
            for other in feature.older_than:
                if other not in self.features:
                    problems.append(
                        "{} is declared older than unknown feature "
                        "{}".format(feature.feature_id, other)
                    )
                    continue

                edges.add((feature.feature_id, other))

            for other in feature.younger_than:
                if other not in self.features:
                    problems.append(
                        "{} is declared younger than unknown feature "
                        "{}".format(feature.feature_id, other)
                    )
                    continue

                edges.add((other, feature.feature_id))

        # Kahn's algorithm; a remainder means a cycle.
        incoming = {fid: 0 for fid in self.features}

        for older, younger in edges:
            incoming[younger] += 1

        ready = sorted(fid for fid, n in incoming.items() if n == 0)
        ordered = []

        while ready:
            current = ready.pop(0)
            ordered.append(current)

            for older, younger in sorted(edges):
                if older != current:
                    continue

                incoming[younger] -= 1

                if incoming[younger] == 0:
                    ready.append(younger)

            ready.sort()

        if len(ordered) != len(self.features):
            stuck = sorted(set(self.features) - set(ordered))
            problems.append(
                "the relative-age relations contain a cycle involving: "
                "{}".format(", ".join(stuck))
            )

            return [], problems

        # A declared rank that disagrees with the relations is a real
        # contradiction, not a rounding difference.
        for older, younger in sorted(edges):
            rank_older = self.features[older].relative_age_rank
            rank_younger = self.features[younger].relative_age_rank

            if rank_older is None or rank_younger is None:
                continue

            if rank_older > rank_younger:
                problems.append(
                    "{} is declared older than {}, but its age rank {} is "
                    "higher than {}".format(
                        older, younger, rank_older, rank_younger
                    )
                )

        return ordered, problems

    # ------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------

    def character_report(self):
        """
        Every limited field, its count, its limit and its overrun.

        Reported for all fields at once rather than failing on the first,
        because an author fixing four overruns wants all four numbers.
        """
        fields = (
            ("subject", self.subject, config.PLANNING_SUBJECT_MAX_CHARS),
            ("importance", self.importance,
             config.PLANNING_IMPORTANCE_MAX_CHARS),
            ("hypothesis",
             self.hypothesis.statement if self.hypothesis else None,
             config.PLANNING_HYPOTHESIS_MAX_CHARS),
            ("predictions_narrative", self.predictions_narrative,
             config.PLANNING_PREDICTIONS_MAX_CHARS),
        )

        report = {}

        for name, text, limit in fields:
            count = config.count_characters(text)
            report[name] = {
                "characters": count,
                "limit": limit,
                "over_by": max(0, count - limit),
                "within_limit": count <= limit,
                "present": bool(text and text.strip()),
                "counting": "characters including spaces",
            }

        return report

    def validate(self):
        """Every structural problem, as a list of strings."""
        problems = []

        # --- character limits ---------------------------------------
        for name, entry in sorted(self.character_report().items()):
            if not entry["present"]:
                problems.append("{} is empty".format(name))

            elif not entry["within_limit"]:
                problems.append(
                    "{} is {} characters, {} over the limit of {} "
                    "(characters including spaces)".format(
                        name, entry["characters"], entry["over_by"],
                        entry["limit"],
                    )
                )

        # --- hypothesis ---------------------------------------------
        if self.hypothesis is None:
            problems.append("the plan states no hypothesis")

        else:
            for name in ("support_condition", "reject_condition",
                         "inconclusive_condition"):
                if not getattr(self.hypothesis, name):
                    problems.append(
                        "the hypothesis states no {}".format(name)
                    )

            for feature_id in self.hypothesis.linked_feature_ids:
                if feature_id not in self.features:
                    problems.append(
                        "the hypothesis links to unknown geological "
                        "feature {}".format(feature_id)
                    )

        # --- predictions --------------------------------------------
        if not self.predictions:
            problems.append("the plan states no predictions")

        for prediction in self.predictions.values():
            if self.hypothesis is None:
                break

            if prediction.hypothesis_id != self.hypothesis.hypothesis_id:
                problems.append(
                    "{} links to hypothesis {}, but the plan's hypothesis "
                    "is {}".format(
                        prediction.prediction_id, prediction.hypothesis_id,
                        self.hypothesis.hypothesis_id,
                    )
                )

            for name in ("expected_observation", "support_criterion",
                         "reject_criterion", "inconclusive_criterion"):
                if not getattr(prediction, name):
                    problems.append(
                        "{} states no {}".format(
                            prediction.prediction_id, name
                        )
                    )

            if not prediction.planned_site_ids:
                problems.append(
                    "{} names no planned site".format(
                        prediction.prediction_id
                    )
                )

        # --- geological features ------------------------------------
        if not self.features:
            problems.append("the plan maps no geological features")

        for feature in self.features.values():
            if not feature.has_geometry:
                problems.append(
                    "{} has neither an outline nor anchor points, so it "
                    "cannot be drawn on the map".format(feature.feature_id)
                )

            if not feature.formation_process:
                problems.append(
                    "{} states no formation process".format(
                        feature.feature_id
                    )
                )

            if not feature.formation_justification:
                problems.append(
                    "{} states no justification for its formation "
                    "process".format(feature.feature_id)
                )

            if feature.relative_age_rank is None:
                problems.append(
                    "{} has no relative age".format(feature.feature_id)
                )

            if not feature.legend_entry:
                problems.append(
                    "{} has no legend entry".format(feature.feature_id)
                )

            if feature.provenance not in PROVENANCES:
                problems.append(
                    "{} declares provenance {!r}, which is not one of "
                    "{}".format(
                        feature.feature_id, feature.provenance,
                        ", ".join(PROVENANCES),
                    )
                )

        _, chronology_problems = self.age_sequence()
        problems.extend(chronology_problems)

        # --- references ---------------------------------------------
        problems.extend(self.reference_problems())

        # --- prediction figure --------------------------------------
        if not self.prediction_figure:
            problems.append(
                "the plan has no prediction figure; the rules allow "
                "exactly one (possibly composite) A4 figure"
            )

        return problems

    def reference_problems(self):
        """Citation integrity, and the two reference kinds the rules refuse."""
        problems = []

        if not self.references:
            problems.append("the plan cites no references")

        for citation in self.citations:
            if citation not in self.references:
                problems.append(
                    "the text cites {}, which is not in the reference "
                    "list".format(citation)
                )

        for reference_id in sorted(self.references):
            if reference_id not in self.citations:
                problems.append(
                    "{} is listed but never cited in the text".format(
                        reference_id
                    )
                )

        for reference in self.references.values():
            haystack = " ".join(
                part for part in
                (reference.url or "", reference.citation or "")
            ).lower()

            for host in FORBIDDEN_REFERENCE_HOSTS:
                if host in haystack:
                    problems.append(
                        "{} cites Wikipedia, which the rules explicitly "
                        "refuse as a scientific reference".format(
                            reference.reference_id
                        )
                    )
                    break

            if reference.doi:
                # A DOI means a paper, wherever it happens to be hosted.
                continue

            for host in NASA_HOSTS:
                if host in haystack:
                    problems.append(
                        "{} cites a NASA web page with no DOI; the rules "
                        "ask for papers or book chapters".format(
                            reference.reference_id
                        )
                    )
                    break

        return problems

    def review_problems(self):
        """
        Geological claims that no human has confirmed.

        Not a structural failure - a plan may legitimately be a draft -
        but it must be visible, because a DRAFT map scored by a judge is
        scored as a real one.
        """
        pending = [
            feature.feature_id for feature in self.features.values()
            if feature.review_status != REVIEWED
        ]

        if not pending:
            return []

        return [
            "{} geological feature(s) are still {}: {}".format(
                len(pending), DRAFT, ", ".join(sorted(pending))
            )
        ]

    def status(self):
        problems = self.validate()

        return {
            "plan_id": self.plan_id,
            "review_status": self.review_status,
            "feature_count": len(self.features),
            "prediction_count": len(self.predictions),
            "reference_count": len(self.references),
            "hypothesis_frozen": (
                self.hypothesis.frozen if self.hypothesis else False
            ),
            "characters": self.character_report(),
            "valid": not problems,
            "problems": problems,
            "review_warnings": self.review_problems(),
        }


def load(path=None):
    return SciencePlan.load(path)
