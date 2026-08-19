"""
Science planning: the map model, the requirement registry, the plan
validators and the four-site planner.

Nothing here writes to Science/data/. The shipped plan is loaded and
mutated in memory; the on-disk artifacts stay exactly as the operator
left them.
"""

import copy
import json
import sys

import support
from support import Checks

support.add_project_root()

from Science import config as science_config          # noqa: E402
from Science import mars_yard                         # noqa: E402
from Science import plan as plan_module               # noqa: E402
from Science import requirements as requirements_mod  # noqa: E402
from Science import sites                             # noqa: E402


def load_plan():
    """A fresh in-memory copy of the shipped plan."""
    with open(science_config.SCIENCE_PLAN_FILE, "r", encoding="utf-8") as h:
        return plan_module.SciencePlan(copy.deepcopy(json.load(h)))


def main_tests():
    checks = Checks("Science planning")

    # ==================================================================
    checks.section("1. the Mars Yard spatial model is source-grounded")

    yard = mars_yard.load()
    counts = yard.counts()

    checks.equal(counts["STARTING_LOCATION"], 9, "9 starting locations")
    checks.equal(counts["LANDMARK"], 15, "15 landmarks")
    checks.equal(counts["NAVIGATION_WAYPOINT"], 9, "9 navigation waypoints")
    checks.equal(counts["DEEP_SAMPLING_LOCATION"], 1, "1 deep sampling point")
    checks.equal(sum(counts.values()), 34, "34 surveyed objects in total")
    checks.equal(yard.validate(), [], "the model validates cleanly")

    # The four table values that anchor everything else.
    checks.equal(yard["S1"].x_m, 0.0, "S1 is the frame origin in X")
    checks.equal(yard["S1"].y_m, 0.0, "S1 is the frame origin in Y")
    checks.equal(yard["L4"].h_m, 0.437, "L4 carries the highest surveyed H")
    checks.equal(yard["S6"].h_m, -0.302, "S6 carries the lowest surveyed H")

    checks.ok(
        all(p.coordinate_status == mars_yard.SOURCE_GROUNDED
            for p in yard.points.values()),
        "every point declares a source-grounded coordinate",
    )
    checks.equal(
        yard.frame.get("geodetic_reference"), None,
        "no geodetic reference is invented; the source supplies none",
    )
    checks.equal(
        yard.frame.get("geodetic_status"), "NOT_SUPPLIED_BY_SOURCE",
        "and the absence is stated rather than left blank",
    )

    checks.close(
        yard.distance("S1", "S2"), 26.427,
        "S1 to S2 is the printed 26.427 m", tolerance=1e-6,
    )

    # ==================================================================
    checks.section("2. the seven spatial concepts stay distinct")

    candidates = {p.point_id for p in yard.site_candidates()}

    checks.equal(len(candidates), 24, "24 objects may carry a science site")
    checks.ok(
        not any(p.startswith("S") for p in candidates),
        "no starting location is eligible as a scientific target",
    )
    checks.ok(
        "P1" not in candidates,
        "the deep sampling point is excluded from Scientific Exploration",
    )
    checks.ok(
        yard["P1"].excluded_from_scientific_exploration,
        "and P1 says why it is excluded",
    )
    checks.equal(
        yard["S7"].zone_association, "Sampling Zone",
        "S7 is associated with the Sampling zone, not with science",
    )

    # ==================================================================
    checks.section("3. the ERC requirement registry is complete")

    registry = requirements_mod.load()

    checks.equal(len(registry), 19, "19 official requirements")
    checks.equal(registry.validate(), [], "the registry validates cleanly")

    expected = [
        "O/SCI-010", "O/SCI-020", "O/SCI-030", "O/SCI-040", "O/SCI-050",
        "O/SCI-060", "O/SCI-070", "O/SCI-080", "O/SCI-090", "O/SCI-100",
        "O/SCI-110", "O/SCI-120", "O/SCI-130", "O/SCI-140", "O/SCI-150",
        "O/SCI-160", "O/SCI-900", "O/SCI-910", "O/SCI-920",
    ]

    for requirement_id in expected:
        checks.equal(
            registry.order.count(requirement_id), 1,
            "{} appears exactly once".format(requirement_id),
        )

    checks.equal(
        sum(r.max_partial_score for r in registry.scored()), 300,
        "the scored requirements sum to the official 300 points",
    )
    checks.equal(
        registry["O/SCI-150"].max_partial_score, 50,
        "O/SCI-150 is worth 50 points, the largest single parameter",
    )
    checks.ok(
        "instrument other than simple camera"
        in registry["O/SCI-150"].official_wording,
        "and its official wording names a non-camera instrument",
    )

    checks.ok(
        all(r.judge_score == requirements_mod.JUDGE_UNKNOWN
            for r in registry),
        "no requirement claims a judge score",
    )

    # A check name that the registry does not declare must be refused,
    # so the registry stays an accurate description of what is verified.
    checks.raises(
        requirements_mod.RequirementError,
        lambda: registry["O/SCI-010"].record("invented_check", "PASS"),
        "an undeclared check name is refused",
    )

    # ==================================================================
    checks.section("4. config mirrors the registry's official limits")

    limits = registry.report_limits
    planning = limits["science_planning"]
    exploration = limits["scientific_exploration"]
    uso = limits["uso"]

    checks.equal(
        planning["subject_max_chars"],
        science_config.PLANNING_SUBJECT_MAX_CHARS, "subject limit 500")
    checks.equal(
        planning["importance_max_chars"],
        science_config.PLANNING_IMPORTANCE_MAX_CHARS,
        "importance limit 1000")
    checks.equal(
        planning["hypothesis_max_chars"],
        science_config.PLANNING_HYPOTHESIS_MAX_CHARS,
        "hypothesis limit 500")
    checks.equal(
        planning["predictions_max_chars"],
        science_config.PLANNING_PREDICTIONS_MAX_CHARS,
        "predictions limit 1500")
    checks.equal(
        exploration["report_max_chars"],
        science_config.EXPLORATION_REPORT_MAX_CHARS,
        "report limit 3000")
    checks.equal(
        exploration["figures_max"],
        science_config.EXPLORATION_FIGURES_MAX, "figure limit 3")
    checks.equal(
        exploration["required_rover_photographs"],
        science_config.EXPLORATION_REQUIRED_ROVER_PHOTOGRAPHS,
        "three annotated rover photographs are required")
    checks.equal(
        uso["description_max_chars"],
        science_config.USO_DESCRIPTION_MAX_CHARS, "USO limit 350")
    checks.ok(
        exploration["captions_count_toward_limit"] is True,
        "figure captions count toward the character limit",
    )
    checks.equal(
        science_config.TEAM_NAME, None,
        "no team name is invented; the naming rule needs a real one",
    )

    # ==================================================================
    checks.section("5. character limits are counted with spaces")

    plan = load_plan()
    report = plan.character_report()

    for field in ("subject", "importance", "hypothesis",
                  "predictions_narrative"):
        checks.ok(
            report[field]["within_limit"],
            "{} is within its limit ({} of {})".format(
                field, report[field]["characters"], report[field]["limit"]
            ),
        )

    checks.equal(
        science_config.count_characters("a b  c"), 6,
        "counting includes every space",
    )

    # An overrun is reported exactly, and nothing is truncated.
    plan.subject = "x" * 620
    report = plan.character_report()

    checks.equal(report["subject"]["characters"], 620, "620 characters seen")
    checks.equal(report["subject"]["over_by"], 120, "reported as 120 over")
    checks.ok(
        not report["subject"]["within_limit"], "and marked over the limit")
    checks.equal(
        len(plan.subject), 620, "the text itself was NOT truncated")

    # ==================================================================
    checks.section("6. the hypothesis freezes and cannot drift")

    plan = load_plan()
    hypothesis = plan.hypothesis

    checks.ok(not hypothesis.frozen, "the shipped plan is not yet frozen")

    digest = hypothesis.freeze("2026-08-18T08:00:00+00:00")

    checks.ok(hypothesis.frozen, "freezing records a time and a hash")
    checks.equal(len(digest), 64, "the hash is a full SHA256")

    unchanged, _detail = hypothesis.verify_unchanged()
    checks.ok(unchanged, "an untouched hypothesis verifies")

    checks.raises(
        plan_module.PlanError,
        lambda: hypothesis.freeze("2026-08-18T09:00:00+00:00"),
        "freezing twice is refused",
    )

    hypothesis.statement = hypothesis.statement + " and also something else"
    unchanged, detail = hypothesis.verify_unchanged()

    checks.ok(not unchanged, "editing a frozen hypothesis is detected")
    checks.ok("EDITED" in detail, "and the detail says it was edited")

    # Editing a condition counts too, not just the statement.
    plan = load_plan()
    plan.hypothesis.freeze("2026-08-18T08:00:00+00:00")
    plan.hypothesis.reject_condition = "something more convenient"

    checks.ok(
        not plan.hypothesis.verify_unchanged()[0],
        "changing the reject condition is detected as well",
    )

    # ==================================================================
    checks.section("7. relative ages are consistent and acyclic")

    plan = load_plan()
    sequence, problems = plan.age_sequence()

    checks.equal(problems, [], "the shipped chronology has no problem")
    checks.equal(
        len(sequence), len(plan.features),
        "every unit appears in the oldest-to-youngest sequence",
    )
    checks.equal(sequence[0], "U2", "the gravel plain is oldest")

    # Introduce a cycle and confirm it is caught rather than ordered.
    plan.features["U2"].younger_than = ["U4"]
    sequence, problems = plan.age_sequence()

    checks.equal(sequence, [], "a cyclic chronology yields no sequence")
    checks.ok(
        any("cycle" in p for p in problems),
        "and the cycle is named as the reason",
    )

    # A rank that contradicts a stated relation is a contradiction.
    plan = load_plan()
    plan.features["U1"].relative_age_rank = 9
    _sequence, problems = plan.age_sequence()

    checks.ok(
        any("age rank" in p for p in problems),
        "a rank contradicting a relation is reported",
    )

    # ==================================================================
    checks.section("8. references: the two kinds the rules refuse")

    plan = load_plan()

    checks.ok(
        any("cites no references" in p for p in plan.reference_problems()),
        "an empty reference list is a problem, not a pass",
    )

    plan.references = {
        "R1": plan_module.Reference({
            "reference_id": "R1",
            "citation": "Hypothesis",
            "url": "https://en.wikipedia.org/wiki/Hypothesis",
        }),
    }
    plan.citations = ["R1"]

    checks.ok(
        any("Wikipedia" in p for p in plan.reference_problems()),
        "a Wikipedia citation is refused",
    )

    plan.references = {
        "R1": plan_module.Reference({
            "reference_id": "R1",
            "citation": "Mars overview",
            "url": "https://science.nasa.gov/mars/",
        }),
    }

    checks.ok(
        any("NASA" in p for p in plan.reference_problems()),
        "a bare NASA web page is refused",
    )

    plan.references = {
        "R1": plan_module.Reference({
            "reference_id": "R1",
            "citation": "Kokaly and others, USGS Spectral Library 7",
            "doi": "10.3133/ds1035",
            "url": "https://pubs.usgs.gov/ds/1035/",
        }),
    }

    checks.equal(
        plan.reference_problems(), [],
        "a paper with a DOI is accepted",
    )

    plan.citations = ["R1", "R2"]
    checks.ok(
        any("R2" in p for p in plan.reference_problems()),
        "a citation with no matching reference is caught",
    )

    # ==================================================================
    checks.section("9. the four-site planner is driven by the predictions")

    plan = load_plan()
    plan.hypothesis.freeze("2026-08-18T08:00:00+00:00")

    targets, controls, contacts = sites.required_coverage(plan)

    checks.equal(targets, ["U1", "U4"], "the target units come from P1")
    checks.equal(controls, ["U2"], "the control unit comes from P2")
    checks.equal(contacts, [("U1", "U4")], "the contact pair comes from P3")

    site_plan = sites.select(
        yard, plan, generated_at="2026-08-18T08:00:00+00:00"
    )

    checks.equal(len(site_plan), 4, "exactly four sites are planned")
    checks.equal(
        site_plan.validate(yard, plan), [],
        "the site plan validates against the map and the plan",
    )

    roles = {site.role for site in site_plan}
    checks.ok(
        sites.ROLE_CONTROL in roles,
        "one site is a control, so a difference can be attributed",
    )
    checks.ok(
        {sites.ROLE_TARGET_A, sites.ROLE_TARGET_B} <= roles,
        "and both hypothesis targets are represented",
    )

    units = {site.geological_feature_id for site in site_plan}
    checks.ok(len(units) >= 2, "the sites span more than one mapped unit")
    checks.ok("U2" in units, "the control unit is actually visited")

    for site in site_plan:
        point = yard[site.source_point_id]
        checks.equal(
            (site.x_m, site.y_m, site.h_m),
            (point.x_m, point.y_m, point.h_m),
            "{} carries the surveyed coordinates of {}".format(
                site.site_id, point.point_id
            ),
        )

    checks.ok(
        all(s.source_point_type in science_config.SITE_ELIGIBLE_TYPES
            for s in site_plan),
        "no site sits on a start location or the deep sampling point",
    )

    ids = [s.source_point_id for s in site_plan]
    checks.equal(len(ids), len(set(ids)), "no surveyed object is reused")

    # Separation is a hard constraint, not a preference.
    located = list(site_plan)
    worst = min(
        ((a.x_m - b.x_m) ** 2 + (a.y_m - b.y_m) ** 2) ** 0.5
        for i, a in enumerate(located) for b in located[i + 1:]
    )
    checks.ok(
        worst >= sites.MIN_SITE_SEPARATION_M,
        "every pair clears the {} m minimum separation ({:.2f} m)".format(
            sites.MIN_SITE_SEPARATION_M, worst
        ),
    )

    unbound = sites.bind_predictions(plan, site_plan)
    checks.equal(unbound, [], "every prediction is bound to a site")
    checks.ok(
        bool(plan.hypothesis.linked_site_ids),
        "and the hypothesis knows where it is tested",
    )

    # ==================================================================
    checks.section("10. the planner refuses what it cannot do honestly")

    plan = load_plan()
    plan.hypothesis.freeze("2026-08-18T08:00:00+00:00")

    for prediction in plan.predictions.values():
        prediction.comparison = {"kind": "CONTACT_GEOMETRY",
                                 "feature_ids": ["U1", "U4"]}

    checks.raises(
        sites.SiteError,
        lambda: sites.select(yard, plan, generated_at="t"),
        "a plan with no target comparison is refused, not guessed",
    )

    plan = load_plan()
    plan.hypothesis = None

    checks.raises(
        sites.SiteError,
        lambda: sites.select(yard, plan, generated_at="t"),
        "sites cannot be planned before a hypothesis exists",
    )

    # ==================================================================
    checks.section("11. operator override is allowed but never silent")

    plan = load_plan()
    plan.hypothesis.freeze("2026-08-18T08:00:00+00:00")
    site_plan = sites.select(yard, plan, generated_at="t")
    sites.bind_predictions(plan, site_plan)

    first = site_plan.sites[0]
    original = first.source_point_id

    checks.raises(
        sites.SiteError,
        lambda: site_plan.override(
            first.site_id, "L13", yard, plan, "", "t"
        ),
        "an override with no reason is refused",
    )

    checks.raises(
        sites.SiteError,
        lambda: site_plan.override(
            first.site_id, "S3", yard, plan, "closer to the start", "t"
        ),
        "an override onto a starting location is refused",
    )

    checks.raises(
        sites.SiteError,
        lambda: site_plan.override(
            first.site_id, "P1", yard, plan, "already going there", "t"
        ),
        "an override onto the deep sampling point is refused",
    )

    site_plan.override(
        first.site_id, "L13", yard, plan,
        "the planned outcrop was inaccessible on the day", "t",
    )

    checks.equal(
        first.source_point_id, "L13", "a valid override moves the site")
    checks.equal(
        first.selected_by, sites.SELECTED_BY_OPERATOR,
        "and records that a human chose it",
    )
    checks.equal(len(site_plan.overrides), 1, "the override is logged")
    checks.equal(
        site_plan.overrides[0]["from_point_id"], original,
        "with the position it moved from",
    )

    return checks.report()


if __name__ == "__main__":
    sys.exit(main_tests())
