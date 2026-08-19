"""
Scientific Exploration: the mission record, the mission-level analysis,
the report limits, the requirement checker and the annotated map.

Every measurement here is synthetic and marked `synthetic: true`. Nothing
in this suite reads or writes DB1, DB2 or the production sample archive,
and every artifact it produces goes to a temporary directory.

The end-to-end scenarios are the point of the suite: the system must be
able to produce an honest INCONCLUSIVE, not only a confident answer.
"""

import copy
import json
import random
import shutil
import sys
import tempfile
from pathlib import Path

import support
from support import Checks

support.add_project_root()

from BD.channels import CHANNELS                       # noqa: E402
from Science import analysis as analysis_module        # noqa: E402
from Science import checker                            # noqa: E402
from Science import config as science_config           # noqa: E402
from Science import mapping                            # noqa: E402
from Science import mars_yard                          # noqa: E402
from Science import plan as plan_module                # noqa: E402
from Science import report as report_module            # noqa: E402
from Science import run as run_module                  # noqa: E402
from Science import sites                              # noqa: E402
from Science.plan import SUPPORTED, REJECTED, INCONCLUSIVE   # noqa: E402

KEYS = [c["id"] if isinstance(c, dict) else c for c in CHANNELS]

# Three distinguishable synthetic surfaces. Slopes differ as well as
# levels, so the angular and centered families have something to see.
BASE_U1 = {k: 0.20 + 0.020 * i for i, k in enumerate(KEYS)}
BASE_U4 = {k: 0.55 - 0.015 * i for i, k in enumerate(KEYS)}
BASE_U2 = {k: 0.35 + 0.005 * i for i, k in enumerate(KEYS)}


def jitter(base, seed, scale=0.04):
    """Independent per-channel noise, seeded so the suite is repeatable."""
    rng = random.Random(seed)

    return {k: base[k] * (1.0 + rng.gauss(0.0, scale)) for k in base}


def load_plan():
    with open(science_config.SCIENCE_PLAN_FILE, "r", encoding="utf-8") as h:
        return plan_module.SciencePlan(copy.deepcopy(json.load(h)))


def build_scenario(scenario, repeats=3, failing_sites=()):
    """
    A whole synthetic mission.

        A  the target units genuinely differ
        B  the target units are the SAME surface
        C  one repeat per site, so no spread exists
    """
    yard = mars_yard.load()
    plan = load_plan()
    plan.hypothesis.freeze("2026-08-18T08:00:00+00:00")

    site_plan = sites.select(yard, plan, generated_at="2026-08-18T08:00:00+00:00")
    sites.bind_predictions(plan, site_plan)

    run = run_module.ScienceRun.start(
        "RUN-SYNTHETIC-{}".format(scenario), plan, site_plan, yard,
        "2026-08-18T08:00:00+00:00",
    )
    run.lock({"synthetic": True, "scenario": scenario},
             "2026-08-18T08:05:00+00:00")
    run.begin_traverse("2026-08-18T09:00:00+00:00")

    bases = {"U1": BASE_U1, "U4": BASE_U4, "U2": BASE_U2}

    if scenario == "B":
        # The two target units are the same surface. A correct analysis
        # must NOT call them separated.
        bases = {"U1": BASE_U1, "U4": BASE_U1, "U2": BASE_U2}

    if scenario == "C":
        repeats = 1

    measurements = []
    seed = 1000

    for site in site_plan:
        run.reach_site(site.site_id, "2026-08-18T09:10:00+00:00")
        base = bases[site.geological_feature_id]

        ids = []

        for index in range(repeats):
            seed += 977
            measurement_id = "{}-M{:02d}".format(site.site_id, index + 1)
            ids.append(measurement_id)

            measurements.append({
                "measurement_id": measurement_id,
                "site_id": site.site_id,
                "sample_id": "{}-SAMPLE".format(site.site_id),
                "spectrum": jitter(base, seed),
                "hardware_quality": (
                    "HARDWARE_QC_FAIL" if site.site_id in failing_sites
                    else "PASS"
                ),
                "normalization_quality": "OK",
                "calibration_id": "SYNTHETIC_CAL_V1",
                "decision": {"level": "UNKNOWN", "synthetic": True},
                "independent": True,
                "synthetic": True,
                "at": "2026-08-18T09:12:00+00:00",
            })
            run.bind_measurement(
                site.site_id, measurement_id, "{}-SAMPLE".format(site.site_id)
            )

        run.add_photograph({
            "photo_id": "PH-{}".format(site.site_id),
            "reference": "synthetic://{}".format(site.site_id),
            "site_id": site.site_id,
            "annotated": True,
            "caption": "Synthetic view of {}".format(site.site_id),
        })
        run.add_observation({
            "observation_id": "OBS-{}".format(site.site_id),
            "site_id": site.site_id,
            "at": "2026-08-18T09:15:00+00:00",
            "description": "Synthetic surface observation.",
            "photo_ids": ["PH-{}".format(site.site_id)],
            "measurement_ids": ids,
            "sample_ids": ["{}-SAMPLE".format(site.site_id)],
        })

    run.end_traverse("2026-08-18T11:00:00+00:00")

    return yard, plan, site_plan, run, measurements


def main_tests():
    checks = Checks("Scientific exploration")
    workspace = Path(tempfile.mkdtemp(prefix="freya-science-test-"))

    try:
        # ==============================================================
        checks.section("1. the run record separates planned from actual")

        yard, plan, site_plan, run, measurements = build_scenario("A")

        checks.equal(len(run.planned_site_ids), 4, "four sites were planned")
        checks.equal(len(run.reached_sites()), 4, "four sites were reached")
        checks.equal(run.state, run_module.RUN_TRAVERSE_COMPLETE,
                     "the run is traverse-complete")
        checks.equal(run.validate(), [], "the run record validates")

        run.abandon_site("SITE-02", "t", "the slope was impassable")
        checks.equal(len(run.reached_sites()), 3,
                     "an abandoned site is no longer counted as reached")
        checks.equal(run.validate(), [],
                     "and an abandonment with a reason is valid")

        _y, _p, _sp, run2, _m = build_scenario("A")
        checks.raises(
            run_module.RunError,
            lambda: run2.abandon_site("SITE-02", "t", ""),
            "abandoning a site with no reason is refused",
        )

        # ==============================================================
        checks.section("2. a hypothesis must be frozen before a run starts")

        yard3 = mars_yard.load()
        plan3 = load_plan()
        site_plan3 = sites.SitePlan.load()

        checks.raises(
            run_module.RunError,
            lambda: run_module.ScienceRun.start(
                "RUN-X", plan3, site_plan3, yard3, "t"
            ),
            "a run cannot start on an unfrozen hypothesis",
        )

        # ==============================================================
        checks.section("3. observation and interpretation stay apart")

        _y, _p, _sp, run4, _m = build_scenario("A")

        run4.add_interpretation({
            "interpretation_id": "INT-1",
            "statement": "The two surfaces are consistent with different "
                         "source materials.",
            "observation_ids": ["OBS-SITE-01"],
        })
        checks.equal(run4.validate(), [],
                     "an interpretation citing an observation is valid")

        run4.add_interpretation({
            "interpretation_id": "INT-2",
            "statement": "This is obviously volcanic.",
            "observation_ids": [],
        })
        checks.ok(
            any("opinion" in p for p in run4.validate()),
            "an interpretation with no observation is called an opinion",
        )

        # ==============================================================
        checks.section("4. the separation statistic: identical surfaces")

        # THE REGRESSION TEST. An earlier version divided the centroid
        # separation by the spread of individual repeats rather than by
        # the standard error of the difference, and reported two samples
        # of ONE surface as reproducibly different.
        left = analysis_module.SiteAggregate(
            "LEFT",
            [analysis_module.MeasurementRecord({
                "measurement_id": "L{}".format(i),
                "site_id": "LEFT",
                "spectrum": jitter(BASE_U1, 500 + i),
            }) for i in range(3)],
            KEYS,
        )
        right = analysis_module.SiteAggregate(
            "RIGHT",
            [analysis_module.MeasurementRecord({
                "measurement_id": "R{}".format(i),
                "site_id": "RIGHT",
                "spectrum": jitter(BASE_U1, 900 + i),
            }) for i in range(3)],
            KEYS,
        )

        same = analysis_module.SiteComparison(left, right, KEYS)

        checks.ok(
            same.reproducibly_separated is not True,
            "two samples of ONE surface are not called separated",
        )

        for family in analysis_module.FAMILIES:
            error = same.standard_error.get(family)
            spread = same.pooled_within.get(family)

            if error is None or spread is None:
                continue

            checks.ok(
                error < spread,
                "the {} denominator is the standard error, below the "
                "raw spread".format(family),
            )

        # And genuinely different surfaces still separate.
        other = analysis_module.SiteAggregate(
            "OTHER",
            [analysis_module.MeasurementRecord({
                "measurement_id": "O{}".format(i),
                "site_id": "OTHER",
                "spectrum": jitter(BASE_U4, 1300 + i),
            }) for i in range(3)],
            KEYS,
        )
        different = analysis_module.SiteComparison(left, other, KEYS)

        checks.ok(
            different.reproducibly_separated is True,
            "two genuinely different surfaces do separate",
        )
        checks.ok(
            len(different.families_agreeing)
            >= analysis_module.MIN_FAMILIES_AGREEING,
            "with at least two metric families agreeing",
        )

        # ==============================================================
        checks.section("5. no spread means no answer, not a zero")

        single = analysis_module.SiteAggregate(
            "SINGLE",
            [analysis_module.MeasurementRecord({
                "measurement_id": "S1",
                "site_id": "SINGLE",
                "spectrum": BASE_U1,
            })],
            KEYS,
        )

        checks.equal(single.n, 1, "one usable measurement")
        checks.ok(
            all(v is None for v in single.within.values()),
            "a single measurement yields no within-site spread",
        )
        checks.ok(
            any("cannot be measured" in note
                for note in single.limitations()),
            "and the limitation says so explicitly",
        )

        undetermined = analysis_module.SiteComparison(single, left, KEYS)
        checks.ok(
            undetermined.reproducibly_separated is None,
            "a comparison against it is undetermined, not False",
        )

        # ==============================================================
        checks.section("6. scenario A - the hypothesis is supported")

        yard, plan, site_plan, run, measurements = build_scenario("A")
        result_a = analysis_module.analyse(
            plan, site_plan, run, measurements, KEYS,
            generated_at="2026-08-18T11:30:00+00:00",
        )

        checks.equal(result_a.hypothesis_outcome, SUPPORTED,
                     "distinct surfaces support the hypothesis")
        checks.equal(result_a.prediction_verdicts["P1"].outcome, SUPPORTED,
                     "P1: the target units separate")
        checks.equal(result_a.prediction_verdicts["P2"].outcome, SUPPORTED,
                     "P2: the control confirms it is not session drift")
        checks.equal(result_a.prediction_verdicts["P3"].outcome, INCONCLUSIVE,
                     "P3: a qualitative criterion is left to a human")
        checks.ok(
            "person" in result_a.prediction_verdicts["P3"].rationale,
            "and the rationale says a person must read the imagery",
        )
        checks.equal(result_a.integrity_problems, [],
                     "no hypothesis-integrity problem")

        # ==============================================================
        checks.section("7. scenario B - the hypothesis is rejected")

        yard, plan, site_plan, run, measurements = build_scenario("B")
        result_b = analysis_module.analyse(
            plan, site_plan, run, measurements, KEYS,
            generated_at="2026-08-18T11:30:00+00:00",
        )

        checks.equal(result_b.hypothesis_outcome, REJECTED,
                     "identical surfaces reject the hypothesis")
        checks.equal(result_b.prediction_verdicts["P1"].outcome, REJECTED,
                     "P1: the populations overlap within their own spread")
        checks.ok(
            bool(result_b.prediction_verdicts["P1"].contradicting),
            "and the contradicting measurements are listed",
        )

        # ==============================================================
        checks.section("8. scenario C - honestly inconclusive")

        yard, plan, site_plan, run, measurements = build_scenario("C")
        result_c = analysis_module.analyse(
            plan, site_plan, run, measurements, KEYS,
            generated_at="2026-08-18T11:30:00+00:00",
        )

        checks.equal(result_c.hypothesis_outcome, INCONCLUSIVE,
                     "one repeat per site cannot settle the question")
        checks.equal(result_c.prediction_verdicts["P1"].outcome, INCONCLUSIVE,
                     "P1 is undetermined rather than guessed")

        remedy = result_c.what_would_resolve_it()
        checks.ok(bool(remedy), "the analysis says what would resolve it")
        checks.ok(
            any("independent repositioned" in item for item in remedy),
            "naming independent repeats as the missing ingredient",
        )

        # ==============================================================
        checks.section("9. failed quality control is not hidden")

        yard, plan, site_plan, run, measurements = build_scenario(
            "A", failing_sites=("SITE-01",)
        )
        result_q = analysis_module.analyse(
            plan, site_plan, run, measurements, KEYS,
            generated_at="t",
        )

        aggregate = result_q.aggregates["SITE-01"]
        checks.equal(aggregate.n, 0, "hardware failures are excluded")
        checks.equal(len(aggregate.excluded), 3, "all three are recorded")
        checks.ok(
            any("excluded" in note for note in aggregate.limitations()),
            "and the exclusion appears as a stated limitation",
        )
        checks.equal(result_q.hypothesis_outcome, INCONCLUSIVE,
                     "losing a target site makes the result inconclusive")

        # ==============================================================
        checks.section("10. an edited hypothesis blocks any verdict")

        yard, plan, site_plan, run, measurements = build_scenario("A")
        plan.hypothesis.statement = "Something far more convenient."

        tampered = analysis_module.analyse(
            plan, site_plan, run, measurements, KEYS, generated_at="t"
        )

        checks.equal(tampered.hypothesis_outcome, INCONCLUSIVE,
                     "a tampered hypothesis yields no verdict")
        checks.ok(
            bool(tampered.integrity_problems),
            "the integrity problem is recorded",
        )
        checks.ok(
            "REFUSED" in (tampered.hypothesis_rationale or ""),
            "and the rationale says the verdict was refused",
        )

        # ==============================================================
        checks.section("11. evidence ranking prefers support over drama")

        yard, plan, site_plan, run, measurements = build_scenario("A")
        result = analysis_module.analyse(
            plan, site_plan, run, measurements, KEYS, generated_at="t"
        )
        ranked = result.rank_evidence()

        checks.ok(bool(ranked), "evidence is ranked")
        checks.ok(
            all("weakest_separation_ratio" in item for item in ranked),
            "ranking reports the weakest family, not the most flattering",
        )
        checks.ok(
            ranked[0]["min_repeats"] >= ranked[-1]["min_repeats"],
            "repeat count outranks effect size",
        )

        # ==============================================================
        checks.section("12. report limits are enforced, never applied")

        claims = [
            report_module.Claim({
                "claim_id": "C-1",
                "text": "x" * 2950,
                "evidence": ["SITE-01-M01"],
            }),
        ]
        figures = [
            report_module.Figure({
                "figure_id": "F{}".format(i),
                "kind": report_module.FIGURE_ANNOTATED_PHOTO,
                "caption": "y" * 40,
                "photo_id": "PH-SITE-01",
                "selected": True,
            })
            for i in range(3)
        ]

        validation = report_module.validate_scientific_exploration(
            claims, figures, run, result, {"path": "map.svg"}
        )

        checks.equal(validation["characters"]["body"], 2950,
                     "the body is counted with spaces")
        checks.equal(validation["characters"]["captions"], 120,
                     "captions are counted too")
        checks.equal(validation["characters"]["total"], 3070,
                     "and the total is body plus captions")
        checks.equal(validation["verdict"], report_module.INVALID,
                     "3070 characters exceeds the 3000 limit")
        checks.equal(len(claims[0].text), 2950,
                     "the text itself was NOT truncated")

        over = [p for p in validation["problems"]
                if p.get("problem") == "OVER_LIMIT"]
        checks.equal(over[0]["over_by"], 70, "the overrun is reported exactly")

        claims[0].text = "x" * 2800
        validation = report_module.validate_scientific_exploration(
            claims, figures, run, result, {"path": "map.svg"}
        )
        checks.equal(validation["verdict"], report_module.VALID,
                     "2920 characters in total is within the limit")

        # Figure count.
        figures.append(report_module.Figure({
            "figure_id": "F4", "kind": report_module.FIGURE_PLOT,
            "caption": "z", "selected": True,
        }))
        validation = report_module.validate_scientific_exploration(
            claims, figures, run, result, {"path": "map.svg"}
        )
        checks.ok(
            any(p.get("problem") == "TOO_MANY_FIGURES"
                for p in validation["problems"]),
            "a fourth figure is refused",
        )

        # Missing updated map.
        validation = report_module.validate_scientific_exploration(
            claims, figures[:3], run, result, None
        )
        checks.ok(
            any(p.get("field") == "updated_geological_map"
                for p in validation["problems"]),
            "the updated geological map is required",
        )

        # A claim with no evidence.
        validation = report_module.validate_scientific_exploration(
            [report_module.Claim({"claim_id": "C-2", "text": "hi",
                                  "evidence": []})],
            figures[:3], run, result, {"path": "map.svg"},
        )
        checks.ok(
            any(p.get("problem") == "NO_EVIDENCE"
                for p in validation["problems"]),
            "a claim with no evidence ids is refused",
        )

        # ==============================================================
        checks.section("13. the figure-budget conflict is surfaced")

        photos_only = [
            report_module.Figure({
                "figure_id": "P{}".format(i),
                "kind": report_module.FIGURE_ANNOTATED_PHOTO,
                "caption": "c", "selected": True,
            })
            for i in range(3)
        ]
        checks.equal(
            report_module.figure_budget_warning(photos_only), None,
            "three photographs and nothing else is not a conflict",
        )

        mixed = photos_only + [report_module.Figure({
            "figure_id": "PLOT", "kind": report_module.FIGURE_PLOT,
            "caption": "spectra", "selected": True,
        })]
        warning = report_module.figure_budget_warning(mixed)

        checks.ok(warning is not None,
                  "three photographs plus a plot IS a conflict")
        checks.equal(warning["decision"], "REQUIRES_HUMAN_DECISION",
                     "and the software refuses to resolve it")
        checks.equal(len(warning["options"]), 3,
                     "it offers the options instead")

        # ==============================================================
        checks.section("14. USO limits")

        yard, plan, site_plan, run, measurements = build_scenario("A")

        run.add_uso({
            "uso_id": "USO-1", "label": "Odd cube",
            "x_m": 1.0, "y_m": 2.0, "coordinate_status": "OPERATOR_ENTERED",
            "description": "A suspiciously rectangular rock.",
            "adhoc_hypothesis": "Probably a brick.",
            "photo_id": "PH-SITE-01", "map_marker": True,
        })
        checks.equal(run.usos["USO-1"].validate(), [],
                     "a complete USO validates")

        run.add_uso({
            "uso_id": "USO-2", "description": "y" * 400,
            "adhoc_hypothesis": "h", "photo_id": "PH-SITE-02",
            "map_marker": True,
        })
        problems = run.usos["USO-2"].validate()
        checks.ok(
            any("350" in p for p in problems),
            "a 400-character USO description is over the 350 limit",
        )

        run.add_uso({
            "uso_id": "USO-3", "description": "short",
            "adhoc_hypothesis": "h", "map_marker": False,
        })
        problems = run.usos["USO-3"].validate()
        checks.ok(any("photograph" in p for p in problems),
                  "a USO with no photograph is flagged")
        checks.ok(any("map" in p for p in problems),
                  "a USO not marked on the map is flagged")

        run.add_uso({
            "uso_id": "USO-4", "description": "fourth",
            "adhoc_hypothesis": "h", "photo_id": "PH-SITE-03",
            "map_marker": True,
        })
        checks.ok(
            any("only 3 are graded" in p or "graded" in p
                for p in run.validate()),
            "a fourth USO is flagged rather than silently dropped",
        )

        # ==============================================================
        checks.section("15. filename and deadline")

        naming = report_module.validate_filename(
            "Freya_ScientificExploration.pdf", "ScientificExploration",
            team_name=None,
        )
        checks.equal(naming["verdict"], report_module.INVALID,
                     "with no team configured the name cannot be checked")
        checks.equal(naming["problem"], "TEAM_NAME_NOT_CONFIGURED",
                     "and the reason is that it is not configured")

        naming = report_module.validate_filename(
            "Freya_ScientificExploration.pdf", "ScientificExploration",
            team_name="Freya",
        )
        checks.equal(naming["verdict"], report_module.VALID,
                     "a correctly named file passes")

        naming = report_module.validate_filename(
            "report_final.pdf", "ScientificExploration", team_name="Freya"
        )
        checks.equal(naming["verdict"], report_module.INVALID,
                     "a wrongly named file fails")

        deadline = report_module.deadline_status(
            run, "2026-08-18T12:00:00+00:00"
        )
        checks.equal(deadline["status"], "OK",
                     "one hour after the traverse is comfortable")
        checks.close(deadline["remaining_minutes"], 90.0,
                     "90 minutes remain of the 2.5 hours", tolerance=0.1)

        deadline = report_module.deadline_status(
            run, "2026-08-18T14:00:00+00:00"
        )
        checks.equal(deadline["status"], "OVERDUE",
                     "three hours after the traverse is overdue")
        checks.ok("penalty_note" in deadline,
                  "and the proportional penalty is explained")

        run.traverse_ended_at = None
        deadline = report_module.deadline_status(run, "2026-08-18T12:00:00+00:00")
        checks.equal(deadline["status"], "UNKNOWN",
                     "with no traverse end the deadline is not guessed")

        # ==============================================================
        checks.section("16. the configuration lock (O/SCI-900)")

        yard, plan, site_plan, run, measurements = build_scenario("A")

        unchanged, differences = run.check_configuration(
            {"synthetic": True, "scenario": "A"}, "t"
        )
        checks.ok(unchanged, "an unchanged configuration verifies")
        checks.equal(differences, [], "with no differences")

        unchanged, differences = run.check_configuration(
            {"synthetic": True, "scenario": "A", "gain": 64}, "t"
        )
        checks.ok(not unchanged, "a changed configuration is detected")
        checks.equal(len(run.configuration_alerts), 1, "and an alert raised")
        checks.equal(
            run.configuration_alerts[0]["outcome"],
            "POTENTIAL_O_SCI_900_RISK",
            "reported as a RISK, never as a confirmed penalty",
        )

        # ==============================================================
        checks.section("17. the annotated map never touches the source")

        source_before = checker.file_digest(science_config.MARS_YARD_IMAGE)

        yard, plan, site_plan, run, measurements = build_scenario("A")
        run.geology_change = {"changed_feature_ids": ["U4"]}
        run.add_uso({
            "uso_id": "USO-1", "label": "Odd cube",
            "x_m": 2.0, "y_m": 3.0,
            "description": "A suspiciously rectangular rock.",
            "adhoc_hypothesis": "Probably a brick.",
            "photo_id": "PH-SITE-01", "map_marker": True,
        })

        planning_map = mapping.build_map(
            yard, plan, mapping.PLANNING_MAP, site_plan,
            version="PLAN-V1", generated_at="t",
            path=workspace / "planning.svg",
        )
        updated_map = mapping.build_map(
            yard, plan, mapping.UPDATED_MAP, site_plan, run,
            version="UPDATED-V1", planning_map_version="PLAN-V1",
            generated_at="t", path=workspace / "updated.svg",
        )

        source_after = checker.file_digest(science_config.MARS_YARD_IMAGE)

        checks.equal(source_before, source_after,
                     "mars_yard_2026.png is byte-identical afterwards")
        checks.ok((workspace / "updated.svg").exists(),
                  "the updated map was written as SVG")
        checks.ok(updated_map["a4"] is True, "and declares A4 geometry")
        checks.ok(
            updated_map["version"] != updated_map["planning_map_version"],
            "the updated map version differs from the planning map",
        )
        checks.ok(
            all(m["data_ref"] for m in updated_map["markers"]),
            "every marker links to a data object",
        )
        checks.ok(
            any(m["kind"] == "USO" for m in updated_map["markers"]),
            "the USO is marked on the map",
        )
        checks.ok(
            any(m["changed"] for m in updated_map["markers"]),
            "new or changed features are marked distinctly",
        )

        svg = (workspace / "updated.svg").read_text(encoding="utf-8")
        checks.ok(svg.startswith("<?xml"), "the SVG is well-formed text")
        checks.ok("mars_yard_2026.png" in svg,
                  "and references the source image rather than embedding it")

        # A USO with no coordinate is reported, never plotted at a guess.
        run.add_uso({
            "uso_id": "USO-NOWHERE", "description": "seen from afar",
            "adhoc_hypothesis": "unclear", "photo_id": "PH-SITE-02",
            "map_marker": False,
        })
        unplaceable = mapping.unplaceable_usos(run)
        checks.equal(len(unplaceable), 1,
                     "a USO with no coordinate is listed as unplaceable")
        checks.ok(
            "inventing" in unplaceable[0]["reason"],
            "and the reason is that plotting it would invent a position",
        )

        # ==============================================================
        checks.section("18. the requirement checker claims no scores")

        manifest = checker.build_source_manifest("t")

        registry = checker.check(
            plan=plan, site_plan=site_plan, run=run, analysis=result,
            planning_map=planning_map, updated_map=updated_map,
            manifest=manifest, now="2026-08-18T12:00:00+00:00",
            report_generated_at="2026-08-18T12:00:00+00:00",
        )

        checks.equal(len(registry), 19, "all 19 requirements were evaluated")
        checks.ok(
            all(r.judge_score == "UNKNOWN" for r in registry),
            "every judge score remains UNKNOWN",
        )
        checks.equal(registry.summary()["judge_scores_claimed"], 0,
                     "the summary claims zero judge scores")
        checks.equal(
            registry["O/SCI-910"].status, "OPERATIONAL_MANUAL",
            "observer contact is operational, not machine-checkable",
        )
        checks.ok(
            registry["O/SCI-150"].reportable_outcome
            == "NON_CAMERA_INSTRUMENT_EVIDENCE_PRESENT",
            "O/SCI-150 reports evidence presence, never 50 points",
        )

        source_check = [
            r for r in registry["O/SCI-140"].results
            if r.name == "the_source_image_was_not_modified"
        ]
        checks.equal(len(source_check), 1, "the source image is checked")
        checks.ok(source_check[0].passed,
                  "and it was demonstrably not modified")

        checks.ok(
            registry["O/SCI-110"].status in (
                "MANUAL_REVIEW_REQUIRED", "READY_AUTOMATED"
            ),
            "O/SCI-110 evidence completeness passes on a full run",
        )

        # Scientific substance is never auto-approved.
        checks.ok(
            all(r.status != "VERIFIED_MANUALLY" for r in registry),
            "nothing reaches VERIFIED_MANUALLY without a human",
        )

        # ==============================================================
        checks.section("19. the science package holds contrary evidence")

        package = report_module.build_package(
            plan, site_plan, run, result, claims[:1], figures[:3],
            requirement_status=registry.summary(),
            updated_map=updated_map,
            generated_at="t", now="2026-08-18T12:00:00+00:00",
        )

        checks.equal(package["hypothesis"]["outcome"], SUPPORTED,
                     "the package carries the analysis verdict")
        checks.ok(
            package["hypothesis"]["content_hash"],
            "and the frozen hypothesis hash that produced it",
        )
        checks.ok("contrary_evidence" in package,
                  "contrary evidence is a first-class field")
        checks.ok(
            bool(package["figure_manifest"]["retained_note"]),
            "unselected figure candidates are retained, not deleted",
        )
        checks.ok(
            "NOT PERFORMED" in package["method_note"]["significance_testing"],
            "the package states that no significance test was performed",
        )
        checks.ok(
            "NOT CLAIMED" in package["method_note"]["material_identification"],
            "and that material identity is not claimed from site spectra",
        )
        checks.ok(
            "may only improve language" in package["ai_boundary"],
            "the AI boundary is stated in the package itself",
        )

        written = report_module.save_package(
            package, workspace / "science_package.json"
        )
        checks.ok(written.exists(), "the package writes to disk")

        # ==============================================================
        checks.section("20. no synthetic data reached production files")

        checks.ok(
            all(m.get("synthetic") for m in measurements),
            "every synthetic measurement is marked synthetic",
        )

        production_samples = json.loads(
            (support.REPO / "BD" / "data" / "samples.json").read_text(
                encoding="utf-8"
            )
        )
        ids = [s.get("sample_id") for s in production_samples["samples"]]
        checks.ok(
            not any(str(i).startswith("SITE-") for i in ids),
            "the production sample archive holds no synthetic sample",
        )

        for name in ("DB1.json", "DB2.json"):
            document = json.loads(
                (support.REPO / "BD" / "data" / name).read_text(
                    encoding="utf-8"
                )
            )
            checks.ok(
                not any("SITE-" in str(k) for k in document["materials"]),
                "{} holds no synthetic material".format(name),
            )

        return checks.report()

    finally:
        shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main_tests())
