"""
The ERC requirement registry.

Nineteen official requirements, their verbatim wording, and what this
software can and cannot say about each of them.

The one rule that shapes this whole module: **software readiness is not a
judge score.** A field being present does not earn a point. O/SCI-020 asks
whether formation processes are *properly* identified; a checker can
confirm that every mapped feature carries a process, and that is all. The
word "properly" belongs to a geologist.

So a requirement never reports a number out of a maximum. It reports:

    automated structural checks   PASS / FAIL, with named checks
    scientific substance          MANUAL_REVIEW_REQUIRED
    judge score                   UNKNOWN

`max_partial_score` is carried because the team needs to know where the
points are - O/SCI-150 is worth 50 and O/SCI-100 is worth 5, and that
should inform effort. It is never used as a numerator.

Layer rule: Science may import BD, Measurements and DecisionModel.
"""

import json

from Science import config

# Verification status, in increasing order of confidence. See the same
# vocabulary in the data file, which is authoritative.
NOT_EVALUATED = "NOT_EVALUATED"
NOT_READY = "NOT_READY"
READY_AUTOMATED = "READY_AUTOMATED"
MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
VERIFIED_MANUALLY = "VERIFIED_MANUALLY"
OPERATIONAL_MANUAL = "OPERATIONAL_MANUAL"
JUDGE_ONLY = "JUDGE_ONLY"
FAILED = "FAILED"
NOT_APPLICABLE = "N/A"

STATUSES = (
    NOT_EVALUATED,
    NOT_READY,
    READY_AUTOMATED,
    MANUAL_REVIEW_REQUIRED,
    VERIFIED_MANUALLY,
    OPERATIONAL_MANUAL,
    JUDGE_ONLY,
    FAILED,
    NOT_APPLICABLE,
)

# What a judge decided. Only a human may ever move this off UNKNOWN.
JUDGE_UNKNOWN = "UNKNOWN"

# One automated check's outcome.
CHECK_PASS = "PASS"
CHECK_FAIL = "FAIL"
CHECK_SKIP = "SKIP"

PHASES = ("science_planning", "scientific_exploration", "both")


class RequirementError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class CheckResult:
    """One named automated check against one requirement."""

    def __init__(self, name, outcome, detail=None, evidence=None):
        if outcome not in (CHECK_PASS, CHECK_FAIL, CHECK_SKIP):
            raise RequirementError(
                "BAD_OUTCOME",
                "{} is not a check outcome".format(outcome),
            )

        self.name = name
        self.outcome = outcome
        self.detail = detail
        self.evidence = list(evidence or [])

    @property
    def passed(self):
        return self.outcome == CHECK_PASS

    def as_dict(self):
        return {
            "check": self.name,
            "outcome": self.outcome,
            "detail": self.detail,
            "evidence": self.evidence,
        }

    def __repr__(self):
        return "<{} {}>".format(self.outcome, self.name)


class Requirement:
    """One O/SCI requirement and everything known about its readiness."""

    def __init__(self, entry):
        self.id = entry["id"]
        self.scored_parameter = entry.get("scored_parameter")
        self.official_wording = entry["official_wording"]
        self.max_partial_score = entry.get("max_partial_score", 0)
        self.max_penalty_score = entry.get("max_penalty_score")
        self.phase = entry.get("phase")
        self.applicability = entry.get("applicability")
        self.verification_type = entry.get("verification_type")
        self.linked_subsystem = entry.get("linked_subsystem")

        self.declared_automated_checks = list(
            entry.get("automated_checks") or []
        )
        self.manual_checks = list(entry.get("manual_checks") or [])
        self.linked_tests = list(entry.get("linked_tests") or [])
        self.reportable_outcome = entry.get("reportable_outcome")
        self.notes = list(entry.get("notes") or [])

        # Mutable readiness state, reset on every checker run.
        self.status = entry.get("status", NOT_EVALUATED)
        self.judge_score = JUDGE_UNKNOWN
        self.results = []
        self.evidence = []

    # ------------------------------------------------------------------
    # recording outcomes
    # ------------------------------------------------------------------

    def record(self, name, outcome, detail=None, evidence=None):
        """Record one automated check. Unknown check names are refused."""
        if name not in self.declared_automated_checks:
            raise RequirementError(
                "UNDECLARED_CHECK",
                "{} recorded check {!r}, which the registry does not "
                "declare for it. Add it to the registry first, so the "
                "requirement document stays the description of what is "
                "actually verified.".format(self.id, name),
            )

        result = CheckResult(name, outcome, detail, evidence)
        self.results.append(result)

        for item in result.evidence:
            if item not in self.evidence:
                self.evidence.append(item)

        return result

    def reset(self):
        self.results = []
        self.evidence = []
        self.status = NOT_EVALUATED

    # ------------------------------------------------------------------
    # readiness
    # ------------------------------------------------------------------

    @property
    def failures(self):
        return [r for r in self.results if r.outcome == CHECK_FAIL]

    @property
    def unchecked(self):
        """Declared checks that this run did not record an outcome for."""
        recorded = {r.name for r in self.results}

        return [
            name for name in self.declared_automated_checks
            if name not in recorded
        ]

    def conclude(self):
        """
        Turn recorded checks into a readiness status.

        Deliberately conservative, and it never reaches VERIFIED_MANUALLY
        - only a human writing into the registry can do that.
        """
        if self.status in (OPERATIONAL_MANUAL, NOT_APPLICABLE,
                           VERIFIED_MANUALLY):
            return self.status

        if not self.declared_automated_checks:
            self.status = OPERATIONAL_MANUAL

            return self.status

        if self.failures:
            self.status = NOT_READY

            return self.status

        if self.unchecked or not self.results:
            self.status = NOT_EVALUATED

            return self.status

        # Every structural check passed. If the requirement also has
        # scientific substance to judge - and almost all of them do - the
        # honest status says so rather than claiming readiness.
        self.status = (
            MANUAL_REVIEW_REQUIRED if self.manual_checks
            else READY_AUTOMATED
        )

        return self.status

    def as_dict(self):
        return {
            "id": self.id,
            "scored_parameter": self.scored_parameter,
            "official_wording": self.official_wording,
            "max_partial_score": self.max_partial_score,
            "max_penalty_score": self.max_penalty_score,
            "phase": self.phase,
            "applicability": self.applicability,
            "verification_type": self.verification_type,
            "linked_subsystem": self.linked_subsystem,
            "status": self.status,
            "judge_score": self.judge_score,
            "automated_checks": [r.as_dict() for r in self.results],
            "unchecked": self.unchecked,
            "manual_checks": self.manual_checks,
            "linked_tests": self.linked_tests,
            "reportable_outcome": self.reportable_outcome,
            "evidence": self.evidence,
            "notes": self.notes,
        }

    def __repr__(self):
        return "<{} {}>".format(self.id, self.status)


class RequirementRegistry:
    """Every O/SCI requirement, loaded once."""

    def __init__(self, path=None):
        self.path = path or config.REQUIREMENTS_FILE
        self.document = self._read()

        self.report_limits = self.document.get("report_limits") or {}
        self.groups = self.document.get("requirement_groups") or []
        self.sources = self.document.get("sources") or []
        self.total_max_score = self.document.get("total_max_score")

        self.requirements = {}
        self.order = []

        for entry in self.document.get("requirements") or []:
            requirement = Requirement(entry)

            if requirement.id in self.requirements:
                raise RequirementError(
                    "DUPLICATE",
                    "{} appears more than once".format(requirement.id),
                )

            self.requirements[requirement.id] = requirement
            self.order.append(requirement.id)

    def _read(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return json.load(handle)

        except OSError as error:
            raise RequirementError(
                "MISSING",
                "the requirement registry could not be read from {}: "
                "{}".format(self.path, error),
            )

        except ValueError as error:
            raise RequirementError(
                "MALFORMED",
                "the requirement registry at {} is not valid JSON: "
                "{}".format(self.path, error),
            )

    def __getitem__(self, requirement_id):
        try:
            return self.requirements[requirement_id]

        except KeyError:
            raise RequirementError(
                "NO_SUCH_REQUIREMENT",
                "{} is not an ERC science requirement".format(
                    requirement_id
                ),
            )

    def get(self, requirement_id):
        return self.requirements.get(requirement_id)

    def __iter__(self):
        for requirement_id in self.order:
            yield self.requirements[requirement_id]

    def __len__(self):
        return len(self.requirements)

    def of_phase(self, phase):
        return [
            r for r in self
            if r.phase == phase or r.phase == "both"
        ]

    def penalties(self):
        return [r for r in self if r.applicability == "penalty"]

    def scored(self):
        return [r for r in self if r.applicability == "direct"]

    def reset(self):
        for requirement in self:
            requirement.reset()

    def conclude(self):
        for requirement in self:
            requirement.conclude()

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------

    def summary(self):
        counts = {}

        for requirement in self:
            counts[requirement.status] = counts.get(requirement.status, 0) + 1

        return {
            "requirement_count": len(self),
            "by_status": counts,
            "not_ready": [
                r.id for r in self if r.status == NOT_READY
            ],
            "judge_scores_claimed": 0,
            "judge_score_note": (
                "This software never claims a judge score. Every "
                "requirement reports judge_score UNKNOWN until a human "
                "records a real result."
            ),
        }

    def validate(self):
        """Structural problems with the registry document itself."""
        problems = []

        expected = [
            "O/SCI-010", "O/SCI-020", "O/SCI-030", "O/SCI-040", "O/SCI-050",
            "O/SCI-060", "O/SCI-070", "O/SCI-080", "O/SCI-090", "O/SCI-100",
            "O/SCI-110", "O/SCI-120", "O/SCI-130", "O/SCI-140", "O/SCI-150",
            "O/SCI-160",
            "O/SCI-900", "O/SCI-910", "O/SCI-920",
        ]

        for requirement_id in expected:
            if requirement_id not in self.requirements:
                problems.append("{} is missing".format(requirement_id))

        for requirement_id in self.order:
            if requirement_id not in expected:
                problems.append(
                    "{} is not an official requirement id".format(
                        requirement_id
                    )
                )

        for requirement in self:
            if not requirement.official_wording:
                problems.append(
                    "{} carries no official wording".format(requirement.id)
                )

            if requirement.status not in STATUSES:
                problems.append(
                    "{} declares unknown status {}".format(
                        requirement.id, requirement.status
                    )
                )

            if requirement.judge_score != JUDGE_UNKNOWN:
                problems.append(
                    "{} claims a judge score, which software may never "
                    "do".format(requirement.id)
                )

        # The group totals are the organiser's arithmetic. If our
        # transcription disagrees with it, the transcription is wrong.
        for group in self.groups:
            member_total = sum(
                self[requirement_id].max_partial_score
                for requirement_id in group["requirements"]
                if requirement_id in self.requirements
            )

            declared = group.get("max_task_score", 0)

            if declared and member_total != declared:
                problems.append(
                    "group {} declares {} points but its requirements sum "
                    "to {}".format(group["group_id"], declared, member_total)
                )

        return problems

    def status(self):
        return {
            "document_id": self.document.get("document_id"),
            "schema_version": self.document.get("schema_version"),
            "requirement_count": len(self),
            "total_max_score": self.total_max_score,
            "groups": [g["group_id"] for g in self.groups],
            "problems": self.validate(),
        }


def load(path=None):
    return RequirementRegistry(path)
