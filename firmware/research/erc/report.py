"""
Report preparation and the ERC limit validators.

Two documents, two very different sets of rules, one principle: a limit is
enforced, never applied. Nothing here truncates text to fit. The rules say
material over the limit "will not be read and assessed", so silently
cutting a report to 3000 characters would delete science and hide the
deletion. The validator reports the exact overrun and refuses to export.

THE FIGURE CONFLICT

The rules require, in one paragraph:

    "you should include three annotated photographs from the rover
     showing geologic features that you discuss in the report"

and in another:

    "no more than 3 figures/annotated photographs with proper
     descriptions (figure caption included in the character limit)
     + an updated geological map"

Read together, the three required photographs consume the whole figure
budget. There is no fourth slot for a spectral plot, and captions eat into
the 3000 characters. This module does not resolve that quietly in either
direction — it raises `figure_budget_warning` and makes a human decide
whether a spectral panel replaces a photograph or rides inside one as an
inset. Both are defensible; picking one silently is not.

Layer rule: Science may import BD, Science and Science.decision.
"""

import json

from research.erc import config
from research.erc.plan import SUPPORTED, REJECTED, INCONCLUSIVE

# What kind of thing occupies a figure slot.
FIGURE_ANNOTATED_PHOTO = "ANNOTATED_ROVER_PHOTOGRAPH"
FIGURE_PLOT = "PLOT"
FIGURE_COMPOSITE = "COMPOSITE"

# The updated geological map is required and is explicitly listed as being
# in addition to the figure allowance.
FIGURE_UPDATED_MAP = "UPDATED_GEOLOGICAL_MAP"

COUNTED_FIGURE_KINDS = (
    FIGURE_ANNOTATED_PHOTO, FIGURE_PLOT, FIGURE_COMPOSITE
)

VALID = "VALID"
INVALID = "INVALID"


class ReportError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class Figure:
    """One figure or annotated photograph destined for a report."""

    def __init__(self, entry):
        self.figure_id = entry["figure_id"]
        self.kind = entry.get("kind", FIGURE_PLOT)
        self.caption = entry.get("caption")
        self.reference = entry.get("reference")
        self.photo_id = entry.get("photo_id")
        self.evidence = list(entry.get("evidence") or [])
        self.prediction_ids = list(entry.get("prediction_ids") or [])
        self.selected = entry.get("selected", False)

    @property
    def counts_toward_limit(self):
        return self.kind in COUNTED_FIGURE_KINDS

    @property
    def caption_characters(self):
        return config.count_characters(self.caption)

    def as_dict(self):
        return {
            "figure_id": self.figure_id,
            "kind": self.kind,
            "caption": self.caption,
            "caption_characters": self.caption_characters,
            "reference": self.reference,
            "photo_id": self.photo_id,
            "evidence": self.evidence,
            "prediction_ids": self.prediction_ids,
            "selected": self.selected,
            "counts_toward_limit": self.counts_toward_limit,
        }

    def __repr__(self):
        return "<Figure {} {}>".format(self.figure_id, self.kind)


class Claim:
    """
    One sentence proposed for the report, and what backs it.

    Every claim carries evidence ids. This is what makes the later AI
    polish safe: the polisher is handed sentences that are already tied to
    data, and its instructions forbid changing any of it.
    """

    def __init__(self, entry):
        self.claim_id = entry["claim_id"]
        self.text = entry["text"]
        self.evidence = list(entry.get("evidence") or [])
        self.analysis_id = entry.get("analysis_id")
        self.kind = entry.get("kind", "INTERPRETATION")

    @property
    def characters(self):
        return config.count_characters(self.text)

    def as_dict(self):
        return {
            "claim_id": self.claim_id,
            "text": self.text,
            "characters": self.characters,
            "evidence": self.evidence,
            "analysis_id": self.analysis_id,
            "kind": self.kind,
        }

    def __repr__(self):
        return "<Claim {}>".format(self.claim_id)


# ----------------------------------------------------------------------
# validators
# ----------------------------------------------------------------------

def validate_science_planning(plan):
    """
    The Science Planning Report against its four character limits.

    Returns a dict with an overall verdict and the exact overrun of every
    field, so an author fixing four problems gets four numbers rather
    than the first one.
    """
    report = plan.character_report()
    problems = []

    for name, entry in sorted(report.items()):
        if not entry["present"]:
            problems.append({
                "field": name,
                "problem": "EMPTY",
                "detail": "{} is required and is empty".format(name),
            })

        elif not entry["within_limit"]:
            problems.append({
                "field": name,
                "problem": "OVER_LIMIT",
                "characters": entry["characters"],
                "limit": entry["limit"],
                "over_by": entry["over_by"],
                "detail": (
                    "{} is {} characters including spaces, {} over the "
                    "limit of {}. The rules state longer descriptions "
                    "WILL NOT BE READ.".format(
                        name, entry["characters"], entry["over_by"],
                        entry["limit"],
                    )
                ),
            })

    figures = 1 if plan.prediction_figure else 0

    if figures != config.PLANNING_FIGURES_MAX:
        problems.append({
            "field": "prediction_figure",
            "problem": "FIGURE_COUNT",
            "detail": (
                "{} prediction figure(s); the rules allow exactly {} "
                "(possibly composite) A4 figure. More than one will not "
                "be taken into account.".format(
                    figures, config.PLANNING_FIGURES_MAX
                )
            ),
        })

    for problem in plan.reference_problems():
        problems.append({
            "field": "references",
            "problem": "REFERENCES",
            "detail": problem,
        })

    return {
        "document": "SCIENCE_PLANNING_REPORT",
        "verdict": VALID if not problems else INVALID,
        "characters": report,
        "figures": figures,
        "figure_limit": config.PLANNING_FIGURES_MAX,
        "problems": problems,
        "counting": "characters including spaces",
        "truncation": (
            "Nothing was truncated. A limit is reported, never applied."
        ),
    }


def report_text(claims):
    """The report body as it will be submitted."""
    return "\n\n".join(claim.text for claim in claims if claim.text)


def figure_budget_warning(figures, required_photographs=None):
    """
    The three-photographs-versus-three-figures conflict, made explicit.

    Returns None when there is nothing to decide, otherwise a warning
    describing the conflict and the options. It never picks one.
    """
    required = (
        config.EXPLORATION_REQUIRED_ROVER_PHOTOGRAPHS
        if required_photographs is None else required_photographs
    )

    selected = [f for f in figures if f.selected and f.counts_toward_limit]
    photos = [f for f in selected if f.kind == FIGURE_ANNOTATED_PHOTO]
    others = [f for f in selected if f.kind != FIGURE_ANNOTATED_PHOTO]

    if len(photos) >= required and not others:
        return None

    if len(selected) <= config.EXPLORATION_FIGURES_MAX and (
        len(photos) >= required
    ):
        return None

    return {
        "outcome": "FIGURE_BUDGET_CONFLICT",
        "required_annotated_photographs": required,
        "figure_limit": config.EXPLORATION_FIGURES_MAX,
        "selected_total": len(selected),
        "selected_photographs": len(photos),
        "selected_other": len(others),
        "detail": (
            "The rules require {} annotated rover photographs and "
            "independently cap the report at {} figures/annotated "
            "photographs. {} photograph(s) and {} other figure(s) are "
            "currently selected.".format(
                required, config.EXPLORATION_FIGURES_MAX,
                len(photos), len(others),
            )
        ),
        "options": [
            "Carry the spectral evidence as an inset inside one of the "
            "three annotated photographs, keeping three figure slots.",
            "Replace one annotated photograph with the spectral figure, "
            "accepting that the rules asked for three photographs.",
            "Build one composite figure that is both a photograph and a "
            "plot, and count it once.",
        ],
        "decision": "REQUIRES_HUMAN_DECISION",
        "note": (
            "This software will not choose. Both readings are defensible "
            "and the choice changes what a judge sees."
        ),
    }


def validate_scientific_exploration(claims, figures, run, analysis=None,
                                    updated_map=None):
    """
    The Scientific Exploration Report against every hard limit.

    Captions count toward the 3000 characters, because the rules say so
    explicitly. That is easy to forget and expensive to discover late.
    """
    problems = []

    body = report_text(claims)
    body_characters = config.count_characters(body)

    counted = [f for f in figures if f.selected and f.counts_toward_limit]
    caption_characters = sum(f.caption_characters for f in counted)
    total_characters = body_characters + caption_characters

    if total_characters > config.EXPLORATION_REPORT_MAX_CHARS:
        problems.append({
            "field": "report_text",
            "problem": "OVER_LIMIT",
            "characters": total_characters,
            "body_characters": body_characters,
            "caption_characters": caption_characters,
            "limit": config.EXPLORATION_REPORT_MAX_CHARS,
            "over_by": total_characters - config.EXPLORATION_REPORT_MAX_CHARS,
            "detail": (
                "the report is {} characters including spaces ({} body + "
                "{} in figure captions), {} over the limit of {}. Figure "
                "captions count toward the limit.".format(
                    total_characters, body_characters, caption_characters,
                    total_characters - config.EXPLORATION_REPORT_MAX_CHARS,
                    config.EXPLORATION_REPORT_MAX_CHARS,
                )
            ),
        })

    if len(counted) > config.EXPLORATION_FIGURES_MAX:
        problems.append({
            "field": "figures",
            "problem": "TOO_MANY_FIGURES",
            "detail": (
                "{} figures/annotated photographs are selected; the limit "
                "is {}".format(len(counted), config.EXPLORATION_FIGURES_MAX)
            ),
        })

    for figure in counted:
        if not figure.caption:
            problems.append({
                "field": "figures",
                "problem": "NO_CAPTION",
                "detail": "{} has no caption; the rules require proper "
                          "descriptions".format(figure.figure_id),
            })

    photos = [f for f in counted if f.kind == FIGURE_ANNOTATED_PHOTO]

    if len(photos) < config.EXPLORATION_REQUIRED_ROVER_PHOTOGRAPHS:
        problems.append({
            "field": "figures",
            "problem": "TOO_FEW_PHOTOGRAPHS",
            "detail": (
                "{} annotated rover photograph(s) selected; the rules ask "
                "for {}".format(
                    len(photos),
                    config.EXPLORATION_REQUIRED_ROVER_PHOTOGRAPHS,
                )
            ),
        })

    if updated_map is None:
        problems.append({
            "field": "updated_geological_map",
            "problem": "MISSING",
            "detail": (
                "no updated geological map is attached; it is required "
                "and is in addition to the figure allowance"
            ),
        })

    # --- USO --------------------------------------------------------
    if len(run.usos) > config.USO_MAX_OBJECTS:
        problems.append({
            "field": "uso",
            "problem": "TOO_MANY",
            "detail": "{} unexpected objects; only {} are graded".format(
                len(run.usos), config.USO_MAX_OBJECTS
            ),
        })

    for uso_id in run.uso_order:
        for detail in run.usos[uso_id].validate():
            problems.append({
                "field": "uso",
                "problem": "USO",
                "detail": detail,
            })

    # --- traceability -----------------------------------------------
    for claim in claims:
        if not claim.evidence:
            problems.append({
                "field": "claims",
                "problem": "NO_EVIDENCE",
                "detail": (
                    "{} carries no evidence ids, so nothing ties it to a "
                    "measurement or observation".format(claim.claim_id)
                ),
            })

    # --- hypothesis verdict -----------------------------------------
    if analysis is not None:
        if analysis.hypothesis_outcome not in (
            SUPPORTED, REJECTED, INCONCLUSIVE
        ):
            problems.append({
                "field": "hypothesis",
                "problem": "NO_VERDICT",
                "detail": (
                    "the analysis records no hypothesis verdict; the "
                    "report must state SUPPORTED, REJECTED or "
                    "INCONCLUSIVE"
                ),
            })

        if analysis.hypothesis_outcome == INCONCLUSIVE:
            if not analysis.what_would_resolve_it():
                problems.append({
                    "field": "hypothesis",
                    "problem": "NO_REMEDY",
                    "detail": (
                        "the verdict is INCONCLUSIVE but the report does "
                        "not say what else would be needed to test the "
                        "hypothesis, which the rules ask for explicitly"
                    ),
                })

        if analysis.integrity_problems:
            for detail in analysis.integrity_problems:
                problems.append({
                    "field": "hypothesis",
                    "problem": "INTEGRITY",
                    "detail": detail,
                })

    return {
        "document": "SCIENTIFIC_EXPLORATION_REPORT",
        "verdict": VALID if not problems else INVALID,
        "characters": {
            "body": body_characters,
            "captions": caption_characters,
            "total": total_characters,
            "limit": config.EXPLORATION_REPORT_MAX_CHARS,
            "remaining": (
                config.EXPLORATION_REPORT_MAX_CHARS - total_characters
            ),
            "counting": "characters including spaces, captions included",
        },
        "figures": {
            "counted": len(counted),
            "limit": config.EXPLORATION_FIGURES_MAX,
            "annotated_photographs": len(photos),
            "required_photographs":
                config.EXPLORATION_REQUIRED_ROVER_PHOTOGRAPHS,
            "updated_map_attached": updated_map is not None,
        },
        "figure_budget_warning": figure_budget_warning(figures),
        "usos": len(run.usos),
        "problems": problems,
        "truncation": (
            "Nothing was truncated. A limit is reported, never applied."
        ),
    }


def validate_filename(filename, report_name, team_name=None):
    """
    The {team}_{report_name} convention. 5% of the grade rides on it.
    """
    team = team_name if team_name is not None else config.TEAM_NAME

    if not team:
        return {
            "verdict": INVALID,
            "problem": "TEAM_NAME_NOT_CONFIGURED",
            "detail": (
                "no team name is configured, so the required "
                "{{team}}_{{report_name}} filename cannot be built or "
                "checked. Set Science/config.py TEAM_NAME. Getting this "
                "wrong costs 5% of the final grade."
            ),
            "expected": None,
            "actual": filename,
        }

    expected = "{}_{}".format(team, report_name)
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename

    return {
        "verdict": VALID if stem == expected else INVALID,
        "problem": None if stem == expected else "NAME_MISMATCH",
        "detail": (
            "filename matches the required convention"
            if stem == expected else
            "filename stem is {!r} but the rules require {!r}".format(
                stem, expected
            )
        ),
        "expected": expected,
        "actual": filename,
    }


def deadline_status(run, now):
    """O/SCI-920: how long is left, and is it already late."""
    elapsed = run.elapsed_since_traverse(now)

    if elapsed is None:
        return {
            "status": "UNKNOWN",
            "detail": (
                "the traverse end time is not recorded, so the 2.5 hour "
                "deadline cannot be computed. It is NOT assumed."
            ),
            "deadline_hours": config.REPORT_DEADLINE_HOURS,
        }

    if elapsed["overdue"]:
        status = "OVERDUE"

    elif elapsed["remaining_minutes"] <= 30:
        status = "CRITICAL"

    elif elapsed["remaining_minutes"] <= 60:
        status = "WARNING"

    else:
        status = "OK"

    record = dict(elapsed)
    record["status"] = status
    record["deadline_hours"] = config.REPORT_DEADLINE_HOURS

    if elapsed["overdue"]:
        # The penalty is proportional, per started hour.
        record["penalty_note"] = (
            "{:.2f} hours late. The rules deduct 20% of the report's "
            "points per hour of delay, calculated proportionally. The "
            "actual deduction is a judge's arithmetic, not ours.".format(
                elapsed["hours_late"]
            )
        )

    return record


# ----------------------------------------------------------------------
# the science package
# ----------------------------------------------------------------------

def build_package(plan, site_plan, run, analysis, claims, figures,
                  requirement_status=None, updated_map=None,
                  generated_at=None, now=None):
    """
    Everything the team needs to write and submit the report.

    Deterministic, and it makes no scientific decision: every verdict in
    here was reached by the analysis layer before this function ran.
    """
    validation = validate_scientific_exploration(
        claims, figures, run, analysis, updated_map
    )

    return {
        "package_version": config.ANALYSIS_VERSION,
        "generated_at": generated_at,
        "science_run_id": run.science_run_id,
        "plan_id": plan.plan_id,

        "hypothesis": {
            "hypothesis_id": (
                plan.hypothesis.hypothesis_id if plan.hypothesis else None
            ),
            "statement": (
                plan.hypothesis.statement if plan.hypothesis else None
            ),
            "frozen_at": (
                plan.hypothesis.frozen_at if plan.hypothesis else None
            ),
            "content_hash": (
                plan.hypothesis.content_hash if plan.hypothesis else None
            ),
            "outcome": analysis.hypothesis_outcome,
            "rationale": analysis.hypothesis_rationale,
            "limitations": analysis.hypothesis_limitations,
            "what_would_resolve_it": analysis.what_would_resolve_it(),
        },

        "prediction_verdicts": {
            prediction_id: verdict.as_dict()
            for prediction_id, verdict in sorted(
                analysis.prediction_verdicts.items()
            )
        },

        "site_comparisons": [
            comparison.as_dict()
            for comparison in analysis.comparisons.values()
        ],

        "evidence_ranking": analysis.rank_evidence(),
        "contrary_evidence": analysis.contrary_evidence(),

        "claims": [claim.as_dict() for claim in claims],
        "figures": [figure.as_dict() for figure in figures],
        "figure_manifest": {
            "selected": [
                f.figure_id for f in figures
                if f.selected and f.counts_toward_limit
            ],
            "candidates": [
                f.figure_id for f in figures if not f.selected
            ],
            "retained_note": (
                "Unselected candidates are kept in the package. The ERC "
                "figure limit constrains the submission, not the evidence."
            ),
        },

        "geology_change": run.geology_change,
        "unexpected_objects": [
            run.usos[uso_id].as_dict() for uso_id in run.uso_order
        ],

        "updated_map": updated_map,
        "validation": validation,
        "deadline": deadline_status(run, now) if now else None,
        "requirements_status": requirement_status,

        "method_note": analysis.as_dict()["method"],
        "ai_boundary": (
            "Every verdict, number and material statement in this package "
            "was produced by the analysis layer. The AI polish step may "
            "only improve language. See AI_POLISH_PROMPT.md."
        ),
    }


def save_package(package, path=None):
    path = path or (config.OUTPUT_DIR / "science_package.json")
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary = path.with_suffix(path.suffix + ".tmp")

    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(package, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    temporary.replace(path)

    return path
