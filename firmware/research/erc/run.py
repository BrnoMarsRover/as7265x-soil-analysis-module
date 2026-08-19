"""
The science run — the mission-level record of one traverse.

`samples.json` records physical samples and what the instrument read from
them. It does not know there was a hypothesis. This file is the other
half: which sites were visited, what was seen there, which samples and
measurements came from which site, what was photographed, what unexpected
objects turned up, and what the configuration was when it all started.

Two separations are structural here, not stylistic.

**Observation is not interpretation.** An observation is a thing that was
seen: "the boundary between the pale and light-toned surfaces is visible
in frame at roughly two metres". An interpretation is a claim about what
it means: "the sharpness of that boundary favours emplacement over
alteration". They are different classes with different fields, because
once they share a field the report loses the ability to say which is
which — and a judge reading "the deposit was emplaced" as an observation
is reading a fabrication.

**Planned is not actual.** A planned site says where we intended to go. A
site visit says where we went. They are linked but never merged, so
"planned four sites, reached three" stays expressible.

Layer rule: Science may import BD, Science and Science.decision.
"""

import hashlib
import json

from research.erc import config

# How a site visit turned out. A site that was planned and not reached is
# a fact about the mission, not a gap to be tidied away.
VISIT_PLANNED = "PLANNED"
VISIT_REACHED = "REACHED"
VISIT_ABANDONED = "ABANDONED"
VISIT_UNPLANNED = "UNPLANNED"

VISIT_STATES = (VISIT_PLANNED, VISIT_REACHED, VISIT_ABANDONED,
                VISIT_UNPLANNED)

# Whether an observation was something we set out to look for.
OBSERVATION_PLANNED = "PLANNED"
OBSERVATION_OPPORTUNISTIC = "OPPORTUNISTIC"

# Run lifecycle. The lock matters: after LOCKED the configuration snapshot
# is fixed and any later change is a flag, not an edit.
RUN_DRAFT = "DRAFT"
RUN_LOCKED = "LOCKED"
RUN_ACTIVE = "ACTIVE"
RUN_TRAVERSE_COMPLETE = "TRAVERSE_COMPLETE"
RUN_FINALISED = "FINALISED"

RUN_STATES = (RUN_DRAFT, RUN_LOCKED, RUN_ACTIVE, RUN_TRAVERSE_COMPLETE,
              RUN_FINALISED)


class RunError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _hash(payload):
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class Photograph:
    """One rover image, and what it is of."""

    def __init__(self, entry):
        self.photo_id = entry["photo_id"]
        self.reference = entry.get("reference")
        self.caption = entry.get("caption")
        self.taken_at = entry.get("taken_at")
        self.site_id = entry.get("site_id")
        self.uso_id = entry.get("uso_id")
        self.annotated = entry.get("annotated", False)
        self.subject = entry.get("subject")
        self.annotations = list(entry.get("annotations") or [])

    def as_dict(self):
        return {
            "photo_id": self.photo_id,
            "reference": self.reference,
            "caption": self.caption,
            "taken_at": self.taken_at,
            "site_id": self.site_id,
            "uso_id": self.uso_id,
            "annotated": self.annotated,
            "subject": self.subject,
            "annotations": self.annotations,
        }

    def __repr__(self):
        return "<Photo {}>".format(self.photo_id)


class Observation:
    """
    Something that was seen. Not what it means.

    `description` must stay descriptive. There is no validator that can
    stop an author writing an interpretation here, but keeping the two
    classes apart makes the mistake visible in review rather than
    invisible in the schema.
    """

    def __init__(self, entry):
        self.observation_id = entry["observation_id"]
        self.site_id = entry.get("site_id")
        self.at = entry.get("at")
        self.kind = entry.get("kind", OBSERVATION_PLANNED)

        self.description = entry.get("description")
        self.photo_ids = list(entry.get("photo_ids") or [])
        self.measurement_ids = list(entry.get("measurement_ids") or [])
        self.sample_ids = list(entry.get("sample_ids") or [])

        self.prediction_ids = list(entry.get("prediction_ids") or [])
        self.surface_texture = entry.get("surface_texture")
        self.grain_scale = entry.get("grain_scale")
        self.context_note = entry.get("context_note")

    def as_dict(self):
        return {
            "observation_id": self.observation_id,
            "site_id": self.site_id,
            "at": self.at,
            "kind": self.kind,
            "description": self.description,
            "photo_ids": self.photo_ids,
            "measurement_ids": self.measurement_ids,
            "sample_ids": self.sample_ids,
            "prediction_ids": self.prediction_ids,
            "surface_texture": self.surface_texture,
            "grain_scale": self.grain_scale,
            "context_note": self.context_note,
        }

    def __repr__(self):
        return "<Observation {} at {}>".format(
            self.observation_id, self.site_id
        )


class Interpretation:
    """
    A claim about what observations mean.

    Always carries the observations it rests on. An interpretation with
    an empty `observation_ids` is an opinion, and the validator says so.
    """

    def __init__(self, entry):
        self.interpretation_id = entry["interpretation_id"]
        self.statement = entry["statement"]
        self.observation_ids = list(entry.get("observation_ids") or [])
        self.measurement_ids = list(entry.get("measurement_ids") or [])
        self.analysis_ids = list(entry.get("analysis_ids") or [])
        self.confidence_language = entry.get("confidence_language")
        self.alternatives = list(entry.get("alternatives") or [])
        self.at = entry.get("at")
        self.post_hoc = entry.get("post_hoc", False)

    def as_dict(self):
        return {
            "interpretation_id": self.interpretation_id,
            "statement": self.statement,
            "observation_ids": self.observation_ids,
            "measurement_ids": self.measurement_ids,
            "analysis_ids": self.analysis_ids,
            "confidence_language": self.confidence_language,
            "alternatives": self.alternatives,
            "at": self.at,
            "post_hoc": self.post_hoc,
        }

    def __repr__(self):
        return "<Interpretation {}>".format(self.interpretation_id)


class UnexpectedObject:
    """
    One USO. Up to three are graded, 350 characters of description each.

    Spectral measurement is optional on purpose: the rules ask for a
    photograph, a description and an ad-hoc hypothesis. Forcing every odd
    rock through the material Decision Model would produce confident
    nonsense about objects the reference libraries have never seen.
    """

    def __init__(self, entry):
        self.uso_id = entry["uso_id"]
        self.label = entry.get("label")
        self.found_at = entry.get("found_at")

        self.x_m = entry.get("x_m")
        self.y_m = entry.get("y_m")
        self.h_m = entry.get("h_m")
        self.coordinate_status = entry.get("coordinate_status", "UNKNOWN")
        self.near_point_id = entry.get("near_point_id")

        self.description = entry.get("description")
        self.adhoc_hypothesis = entry.get("adhoc_hypothesis")
        self.photo_id = entry.get("photo_id")
        self.map_marker = entry.get("map_marker", False)
        self.measurement_ids = list(entry.get("measurement_ids") or [])

    @property
    def description_characters(self):
        return config.count_characters(self.description)

    def validate(self):
        problems = []

        if not self.description or not self.description.strip():
            problems.append("{} has no description".format(self.uso_id))

        elif self.description_characters > config.USO_DESCRIPTION_MAX_CHARS:
            problems.append(
                "{} description is {} characters, {} over the {} limit "
                "(characters including spaces)".format(
                    self.uso_id, self.description_characters,
                    self.description_characters
                    - config.USO_DESCRIPTION_MAX_CHARS,
                    config.USO_DESCRIPTION_MAX_CHARS,
                )
            )

        if not self.adhoc_hypothesis:
            problems.append(
                "{} states no ad-hoc hypothesis about what it is".format(
                    self.uso_id
                )
            )

        if not self.photo_id:
            problems.append(
                "{} has no photograph; the rubric awards 3 of its 10 "
                "points for one".format(self.uso_id)
            )

        if not self.map_marker:
            problems.append(
                "{} is not marked on the updated map; the rubric awards 2 "
                "of its 10 points for that".format(self.uso_id)
            )

        return problems

    def as_dict(self):
        return {
            "uso_id": self.uso_id,
            "label": self.label,
            "found_at": self.found_at,
            "x_m": self.x_m,
            "y_m": self.y_m,
            "h_m": self.h_m,
            "coordinate_status": self.coordinate_status,
            "near_point_id": self.near_point_id,
            "description": self.description,
            "description_characters": self.description_characters,
            "adhoc_hypothesis": self.adhoc_hypothesis,
            "photo_id": self.photo_id,
            "map_marker": self.map_marker,
            "measurement_ids": self.measurement_ids,
        }

    def __repr__(self):
        return "<USO {}>".format(self.uso_id)


class SiteVisit:
    """What actually happened at one site."""

    def __init__(self, entry):
        self.site_id = entry["site_id"]
        self.planned = entry.get("planned", True)
        self.state = entry.get("state", VISIT_PLANNED)

        self.source_point_id = entry.get("source_point_id")
        self.x_m = entry.get("x_m")
        self.y_m = entry.get("y_m")
        self.h_m = entry.get("h_m")
        self.coordinate_status = entry.get("coordinate_status")

        self.arrived_at = entry.get("arrived_at")
        self.departed_at = entry.get("departed_at")

        self.observation_ids = list(entry.get("observation_ids") or [])
        self.sample_ids = list(entry.get("sample_ids") or [])
        self.measurement_ids = list(entry.get("measurement_ids") or [])
        self.photo_ids = list(entry.get("photo_ids") or [])

        self.abandon_reason = entry.get("abandon_reason")

    def as_dict(self):
        return {
            "site_id": self.site_id,
            "planned": self.planned,
            "state": self.state,
            "source_point_id": self.source_point_id,
            "x_m": self.x_m,
            "y_m": self.y_m,
            "h_m": self.h_m,
            "coordinate_status": self.coordinate_status,
            "arrived_at": self.arrived_at,
            "departed_at": self.departed_at,
            "observation_ids": self.observation_ids,
            "sample_ids": self.sample_ids,
            "measurement_ids": self.measurement_ids,
            "photo_ids": self.photo_ids,
            "abandon_reason": self.abandon_reason,
        }

    def __repr__(self):
        return "<Visit {} {}>".format(self.site_id, self.state)


class ConfigurationSnapshot:
    """
    What the system looked like when the run was locked.

    Exists for O/SCI-900. It cannot see the physical rover, so it never
    claims a hardware modification happened - it records what software
    could see and reports when that changed.
    """

    def __init__(self, entry):
        self.taken_at = entry.get("taken_at")
        self.payload = entry.get("payload") or {}
        self.content_hash = entry.get("content_hash")

    @classmethod
    def capture(cls, payload, taken_at):
        snapshot = cls({"taken_at": taken_at, "payload": payload})
        snapshot.content_hash = _hash(payload)

        return snapshot

    def compare(self, payload):
        """
        Differences between the locked configuration and a later one.

        Returns (unchanged, differences). The caller decides what to do;
        this module states the fact.
        """
        current = _hash(payload)

        if current == self.content_hash:
            return True, []

        differences = []
        keys = set(self.payload) | set(payload)

        for key in sorted(keys):
            was = self.payload.get(key)
            now = payload.get(key)

            if was != now:
                differences.append({
                    "field": key,
                    "at_lock": was,
                    "now": now,
                })

        return False, differences

    def as_dict(self):
        return {
            "taken_at": self.taken_at,
            "payload": self.payload,
            "content_hash": self.content_hash,
        }


class ScienceRun:
    """One traverse, and everything traceable about it."""

    def __init__(self, document=None):
        document = document or {}
        self.document = document

        self.science_run_id = document.get("science_run_id")
        self.schema_version = document.get(
            "schema_version", config.SCIENCE_RUN_SCHEMA_VERSION
        )
        self.state = document.get("state", RUN_DRAFT)

        self.plan_id = document.get("plan_id")
        self.hypothesis_id = document.get("hypothesis_id")
        self.hypothesis_hash = document.get("hypothesis_hash")
        self.map_document_id = document.get("map_document_id")
        self.rules_version = document.get("rules_version")

        self.start_point_id = document.get("start_point_id")
        self.created_at = document.get("created_at")
        self.locked_at = document.get("locked_at")
        self.traverse_started_at = document.get("traverse_started_at")
        self.traverse_ended_at = document.get("traverse_ended_at")
        self.finalised_at = document.get("finalised_at")

        self.planned_site_ids = list(document.get("planned_site_ids") or [])

        self.visits = {}
        self.visit_order = []

        for entry in document.get("site_visits") or []:
            visit = SiteVisit(entry)
            self.visits[visit.site_id] = visit
            self.visit_order.append(visit.site_id)

        self.observations = {}
        self.observation_order = []

        for entry in document.get("observations") or []:
            observation = Observation(entry)
            self.observations[observation.observation_id] = observation
            self.observation_order.append(observation.observation_id)

        self.interpretations = {}
        self.interpretation_order = []

        for entry in document.get("interpretations") or []:
            interpretation = Interpretation(entry)
            self.interpretations[interpretation.interpretation_id] = (
                interpretation
            )
            self.interpretation_order.append(
                interpretation.interpretation_id
            )

        self.photographs = {}

        for entry in document.get("photographs") or []:
            photograph = Photograph(entry)
            self.photographs[photograph.photo_id] = photograph

        self.usos = {}
        self.uso_order = []

        for entry in document.get("unexpected_objects") or []:
            uso = UnexpectedObject(entry)
            self.usos[uso.uso_id] = uso
            self.uso_order.append(uso.uso_id)

        snapshot = document.get("configuration_snapshot")
        self.configuration_snapshot = (
            ConfigurationSnapshot(snapshot) if snapshot else None
        )
        self.configuration_alerts = list(
            document.get("configuration_alerts") or []
        )

        self.geology_change = document.get("geology_change") or {}
        self.analysis = document.get("analysis") or {}
        self.report_artifacts = document.get("report_artifacts") or {}
        self.database_versions = document.get("database_versions") or {}

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    @classmethod
    def start(cls, science_run_id, plan, site_plan, yard, created_at,
              start_point_id="S1", rules_version=None):
        """
        Open a run against a plan whose hypothesis is already frozen.

        Refusing to start on an unfrozen hypothesis is the point: a
        hypothesis that can still be edited is not yet a prediction about
        anything.
        """
        if plan.hypothesis is None:
            raise RunError(
                "NO_HYPOTHESIS", "the plan states no hypothesis"
            )

        if not plan.hypothesis.frozen:
            raise RunError(
                "HYPOTHESIS_NOT_FROZEN",
                "hypothesis {} has not been frozen. Freeze it before the "
                "traverse, so that what is tested is what was claimed "
                "beforehand.".format(plan.hypothesis.hypothesis_id),
            )

        run = cls({
            "science_run_id": science_run_id,
            "schema_version": config.SCIENCE_RUN_SCHEMA_VERSION,
            "state": RUN_DRAFT,
            "plan_id": plan.plan_id,
            "hypothesis_id": plan.hypothesis.hypothesis_id,
            "hypothesis_hash": plan.hypothesis.content_hash,
            "map_document_id": yard.document.get("document_id"),
            "rules_version": rules_version,
            "start_point_id": start_point_id,
            "created_at": created_at,
            "planned_site_ids": [site.site_id for site in site_plan],
        })

        for site in site_plan:
            run.visits[site.site_id] = SiteVisit({
                "site_id": site.site_id,
                "planned": True,
                "state": VISIT_PLANNED,
                "source_point_id": site.source_point_id,
                "x_m": site.x_m,
                "y_m": site.y_m,
                "h_m": site.h_m,
                "coordinate_status": site.coordinate_status,
            })
            run.visit_order.append(site.site_id)

        return run

    def lock(self, payload, at):
        """Freeze the configuration. O/SCI-900 traceability starts here."""
        if self.state != RUN_DRAFT:
            raise RunError(
                "ALREADY_LOCKED",
                "run {} is {}; configuration is locked once".format(
                    self.science_run_id, self.state
                ),
            )

        self.configuration_snapshot = ConfigurationSnapshot.capture(
            payload, at
        )
        self.locked_at = at
        self.state = RUN_LOCKED

        return self.configuration_snapshot

    def check_configuration(self, payload, at):
        """
        Compare the live configuration against the locked one.

        Records an alert if it moved. Deliberately does NOT assert that a
        penalty occurred - only a judge decides that.
        """
        if self.configuration_snapshot is None:
            raise RunError(
                "NOT_LOCKED",
                "no configuration snapshot exists; lock the run first",
            )

        unchanged, differences = self.configuration_snapshot.compare(payload)

        if unchanged:
            return True, []

        alert = {
            "at": at,
            "outcome": "POTENTIAL_O_SCI_900_RISK",
            "differences": differences,
            "note": (
                "Software-visible configuration changed after the run was "
                "locked. This is a flag for the operator, not a finding: "
                "whether an ERC penalty applies is a judge's decision, and "
                "this software cannot see the physical rover."
            ),
        }

        self.configuration_alerts.append(alert)

        return False, differences

    def begin_traverse(self, at):
        if self.state not in (RUN_LOCKED,):
            raise RunError(
                "NOT_LOCKED",
                "the run must be locked before the traverse begins; it is "
                "{}".format(self.state),
            )

        self.traverse_started_at = at
        self.state = RUN_ACTIVE

    def end_traverse(self, at):
        """
        Mark the traverse complete. The 2.5-hour clock starts here.
        """
        if self.state != RUN_ACTIVE:
            raise RunError(
                "NOT_ACTIVE",
                "the run is {}, not {}".format(self.state, RUN_ACTIVE),
            )

        self.traverse_ended_at = at
        self.state = RUN_TRAVERSE_COMPLETE

    # ------------------------------------------------------------------
    # recording
    # ------------------------------------------------------------------

    def visit(self, site_id):
        try:
            return self.visits[site_id]

        except KeyError:
            raise RunError(
                "NO_SUCH_VISIT",
                "{} is not a site of this run".format(site_id),
            )

    def reach_site(self, site_id, at):
        visit = self.visit(site_id)
        visit.state = VISIT_REACHED
        visit.arrived_at = at

        return visit

    def abandon_site(self, site_id, at, reason):
        if not reason:
            raise RunError(
                "NO_REASON",
                "abandoning a planned site must state why; the report has "
                "to account for the observation that was not made",
            )

        visit = self.visit(site_id)
        visit.state = VISIT_ABANDONED
        visit.departed_at = at
        visit.abandon_reason = reason

        return visit

    def add_photograph(self, entry):
        photograph = Photograph(entry)

        if photograph.photo_id in self.photographs:
            raise RunError(
                "DUPLICATE_PHOTO",
                "{} already exists".format(photograph.photo_id),
            )

        self.photographs[photograph.photo_id] = photograph

        if photograph.site_id and photograph.site_id in self.visits:
            visit = self.visits[photograph.site_id]

            if photograph.photo_id not in visit.photo_ids:
                visit.photo_ids.append(photograph.photo_id)

        return photograph

    def add_observation(self, entry):
        observation = Observation(entry)

        if observation.observation_id in self.observations:
            raise RunError(
                "DUPLICATE_OBSERVATION",
                "{} already exists".format(observation.observation_id),
            )

        self.observations[observation.observation_id] = observation
        self.observation_order.append(observation.observation_id)

        if observation.site_id and observation.site_id in self.visits:
            visit = self.visits[observation.site_id]

            if observation.observation_id not in visit.observation_ids:
                visit.observation_ids.append(observation.observation_id)

            for sample_id in observation.sample_ids:
                if sample_id not in visit.sample_ids:
                    visit.sample_ids.append(sample_id)

            for measurement_id in observation.measurement_ids:
                if measurement_id not in visit.measurement_ids:
                    visit.measurement_ids.append(measurement_id)

        return observation

    def add_interpretation(self, entry):
        interpretation = Interpretation(entry)

        if interpretation.interpretation_id in self.interpretations:
            raise RunError(
                "DUPLICATE_INTERPRETATION",
                "{} already exists".format(
                    interpretation.interpretation_id
                ),
            )

        self.interpretations[interpretation.interpretation_id] = (
            interpretation
        )
        self.interpretation_order.append(interpretation.interpretation_id)

        return interpretation

    def add_uso(self, entry):
        uso = UnexpectedObject(entry)

        if uso.uso_id in self.usos:
            raise RunError(
                "DUPLICATE_USO", "{} already exists".format(uso.uso_id)
            )

        # The rules grade up to three. A fourth is not refused outright -
        # the team may want it recorded - but it is flagged, because
        # submitting four means one is ignored and nobody chose which.
        self.usos[uso.uso_id] = uso
        self.uso_order.append(uso.uso_id)

        return uso

    def bind_measurement(self, site_id, measurement_id, sample_id=None):
        """Attach one measurement, and optionally its sample, to a site."""
        visit = self.visit(site_id)

        if measurement_id not in visit.measurement_ids:
            visit.measurement_ids.append(measurement_id)

        if sample_id and sample_id not in visit.sample_ids:
            visit.sample_ids.append(sample_id)

        return visit

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------

    def reached_sites(self):
        return [
            self.visits[site_id] for site_id in self.visit_order
            if self.visits[site_id].state == VISIT_REACHED
        ]

    def measurements_at(self, site_id):
        return list(self.visit(site_id).measurement_ids)

    def all_measurement_ids(self):
        found = []

        for site_id in self.visit_order:
            for measurement_id in self.visits[site_id].measurement_ids:
                if measurement_id not in found:
                    found.append(measurement_id)

        return found

    def observations_at(self, site_id):
        return [
            self.observations[observation_id]
            for observation_id in self.visit(site_id).observation_ids
            if observation_id in self.observations
        ]

    def elapsed_since_traverse(self, now):
        """
        Deadline arithmetic for O/SCI-920.

        Returns None when the traverse end is unknown, rather than
        guessing a start time and producing a confident wrong deadline.
        """
        if not self.traverse_ended_at:
            return None

        from datetime import datetime

        try:
            ended = datetime.fromisoformat(self.traverse_ended_at)
            current = datetime.fromisoformat(now)

        except (TypeError, ValueError):
            return None

        elapsed = (current - ended).total_seconds()
        allowed = config.REPORT_DEADLINE_HOURS * 3600.0

        return {
            "traverse_ended_at": self.traverse_ended_at,
            "now": now,
            "elapsed_seconds": elapsed,
            "elapsed_minutes": round(elapsed / 60.0, 1),
            "deadline_seconds": allowed,
            "remaining_seconds": allowed - elapsed,
            "remaining_minutes": round((allowed - elapsed) / 60.0, 1),
            "overdue": elapsed > allowed,
            "hours_late": max(0.0, (elapsed - allowed) / 3600.0),
        }

    # ------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------

    def validate(self):
        problems = []

        if self.state not in RUN_STATES:
            problems.append(
                "unknown run state {!r}".format(self.state)
            )

        if not self.hypothesis_hash:
            problems.append(
                "the run records no hypothesis hash, so it cannot prove "
                "which hypothesis it was testing"
            )

        for site_id in self.visit_order:
            visit = self.visits[site_id]

            if visit.state == VISIT_ABANDONED and not visit.abandon_reason:
                problems.append(
                    "{} was abandoned with no reason recorded".format(
                        site_id
                    )
                )

        for observation_id in self.observation_order:
            observation = self.observations[observation_id]

            for photo_id in observation.photo_ids:
                if photo_id not in self.photographs:
                    problems.append(
                        "{} references unknown photograph {}".format(
                            observation_id, photo_id
                        )
                    )

            if observation.site_id and observation.site_id not in self.visits:
                problems.append(
                    "{} references unknown site {}".format(
                        observation_id, observation.site_id
                    )
                )

        for interpretation_id in self.interpretation_order:
            interpretation = self.interpretations[interpretation_id]

            if not interpretation.observation_ids:
                problems.append(
                    "{} rests on no observation, which makes it an "
                    "opinion rather than an interpretation".format(
                        interpretation_id
                    )
                )

            for observation_id in interpretation.observation_ids:
                if observation_id not in self.observations:
                    problems.append(
                        "{} cites unknown observation {}".format(
                            interpretation_id, observation_id
                        )
                    )

        if len(self.usos) > config.USO_MAX_OBJECTS:
            problems.append(
                "{} unexpected objects are recorded; only {} are graded, "
                "and nothing has chosen which to drop".format(
                    len(self.usos), config.USO_MAX_OBJECTS
                )
            )

        for uso_id in self.uso_order:
            problems.extend(self.usos[uso_id].validate())

        return problems

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def as_dict(self):
        return {
            "science_run_id": self.science_run_id,
            "schema_version": self.schema_version,
            "state": self.state,
            "plan_id": self.plan_id,
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_hash": self.hypothesis_hash,
            "map_document_id": self.map_document_id,
            "rules_version": self.rules_version,
            "start_point_id": self.start_point_id,
            "created_at": self.created_at,
            "locked_at": self.locked_at,
            "traverse_started_at": self.traverse_started_at,
            "traverse_ended_at": self.traverse_ended_at,
            "finalised_at": self.finalised_at,
            "planned_site_ids": self.planned_site_ids,
            "site_visits": [
                self.visits[site_id].as_dict()
                for site_id in self.visit_order
            ],
            "observations": [
                self.observations[observation_id].as_dict()
                for observation_id in self.observation_order
            ],
            "interpretations": [
                self.interpretations[interpretation_id].as_dict()
                for interpretation_id in self.interpretation_order
            ],
            "photographs": [
                photograph.as_dict()
                for photograph in self.photographs.values()
            ],
            "unexpected_objects": [
                self.usos[uso_id].as_dict() for uso_id in self.uso_order
            ],
            "configuration_snapshot": (
                self.configuration_snapshot.as_dict()
                if self.configuration_snapshot else None
            ),
            "configuration_alerts": self.configuration_alerts,
            "geology_change": self.geology_change,
            "analysis": self.analysis,
            "report_artifacts": self.report_artifacts,
            "database_versions": self.database_versions,
        }

    def save(self, path=None):
        path = path or config.SCIENCE_RUN_FILE
        path.parent.mkdir(parents=True, exist_ok=True)

        temporary = path.with_suffix(path.suffix + ".tmp")

        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(self.as_dict(), handle, indent=2, ensure_ascii=False)
            handle.write("\n")

        temporary.replace(path)

        return path

    @classmethod
    def load(cls, path=None):
        path = path or config.SCIENCE_RUN_FILE

        try:
            with open(path, "r", encoding="utf-8") as handle:
                return cls(json.load(handle))

        except OSError as error:
            raise RunError(
                "MISSING",
                "the science run could not be read from {}: {}".format(
                    path, error
                ),
            )

        except ValueError as error:
            raise RunError(
                "MALFORMED",
                "the science run at {} is not valid JSON: {}".format(
                    path, error
                ),
            )

    def status(self):
        reached = self.reached_sites()

        return {
            "science_run_id": self.science_run_id,
            "state": self.state,
            "plan_id": self.plan_id,
            "hypothesis_id": self.hypothesis_id,
            "planned_sites": len(self.planned_site_ids),
            "sites_reached": len(reached),
            "sites_abandoned": len([
                v for v in self.visits.values()
                if v.state == VISIT_ABANDONED
            ]),
            "observations": len(self.observations),
            "interpretations": len(self.interpretations),
            "photographs": len(self.photographs),
            "measurements": len(self.all_measurement_ids()),
            "unexpected_objects": len(self.usos),
            "configuration_locked": self.configuration_snapshot is not None,
            "configuration_alerts": len(self.configuration_alerts),
            "problems": self.validate(),
        }
