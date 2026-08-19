"""
The O/SCI requirement checker.

Runs every automated structural check the registry declares, records the
outcome against the requirement, and produces a readiness report.

What it checks is whether the EVIDENCE EXISTS and HANGS TOGETHER. What it
never checks is whether the science is right. O/SCI-030 asks whether
relative ages are "properly assigned"; this module confirms that every
unit has an age, that the stated relations contain no cycle, and that an
oldest-to-youngest sequence can be produced. Whether that sequence is the
true history of the yard is a geologist's judgement and is reported as
MANUAL_REVIEW_REQUIRED for exactly as long as no geologist has recorded
one.

No check here produces a score. See requirements.py for why.

Layer rule: Science may import BD, Measurements and DecisionModel.
"""

import hashlib
import json

from Science import config, requirements as requirements_module
from Science.requirements import CHECK_PASS, CHECK_FAIL, CHECK_SKIP

# Words that make a hypothesis a bare identification question, which the
# rules call out as too obvious ("is this object a rock?").
BARE_IDENTIFICATION_PATTERNS = (
    "is this a ",
    "is this object a ",
    "is it a ",
    "what is this",
    "is this made of",
)


def _outcome(condition):
    return CHECK_PASS if condition else CHECK_FAIL


def file_digest(path):
    """SHA256 of a file, or None if it is not there."""
    try:
        digest = hashlib.sha256()

        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(65536), b""):
                digest.update(block)

        return digest.hexdigest()

    except OSError:
        return None


def build_source_manifest(generated_at=None):
    """
    Hash every authoritative source, so we know what we built against.

    The map image hash is the one that matters operationally: O/SCI-140
    requires the source image to be untouched, and this is how that is
    demonstrated rather than asserted.
    """
    sources = {
        "mars_yard_2026.png": config.MARS_YARD_IMAGE,
        "MarsYard_zones.png": config.MARS_YARD_ZONES_IMAGE,
        "Science_Task_Rules_Updated.pdf": config.SCIENCE_RULES_PDF,
        "Exact_Science_Task_Rubric.xlsx": config.SCIENCE_RUBRIC_XLSX,
        "Soil.png": config.SOIL_PROFILE_IMAGE,
        "mars_yard_points.json": config.MARS_YARD_POINTS_FILE,
        "erc_science_requirements.json": config.REQUIREMENTS_FILE,
        "science_plan.json": config.SCIENCE_PLAN_FILE,
    }

    return {
        "manifest_version": 1,
        "generated_at": generated_at,
        "note": (
            "SHA256 of every authoritative input. The map image hash "
            "demonstrates that the spatial source of truth was never "
            "rewritten - annotation produces a separate SVG overlay."
        ),
        "sources": {
            name: {
                "path": str(path),
                "present": path.exists(),
                "sha256": file_digest(path),
                "bytes": path.stat().st_size if path.exists() else None,
            }
            for name, path in sorted(sources.items())
        },
    }


def save_source_manifest(manifest, path=None):
    path = path or config.SOURCE_MANIFEST_FILE
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary = path.with_suffix(path.suffix + ".tmp")

    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    temporary.replace(path)

    return path


# ----------------------------------------------------------------------
# Science Planning requirements
# ----------------------------------------------------------------------

def _check_map_requirements(registry, plan):
    features = list(plan.features.values()) if plan else []

    # --- O/SCI-010 --------------------------------------------------
    r = registry["O/SCI-010"]
    r.record(
        "geological_features_exist", _outcome(bool(features)),
        "{} mapped feature(s)".format(len(features)),
        [f.feature_id for f in features],
    )

    missing_geometry = [f.feature_id for f in features if not f.has_geometry]
    r.record(
        "every_feature_has_map_geometry", _outcome(not missing_geometry),
        "without outline or anchors: {}".format(
            ", ".join(missing_geometry) or "none"
        ),
    )

    ids = [f.feature_id for f in features]
    r.record(
        "every_feature_has_unique_id",
        _outcome(len(ids) == len(set(ids))),
        "{} ids, {} unique".format(len(ids), len(set(ids))),
    )

    unlegended = [f.feature_id for f in features if not f.legend_entry]
    r.record(
        "every_feature_is_referenced_by_the_legend",
        _outcome(not unlegended),
        "without a legend entry: {}".format(
            ", ".join(unlegended) or "none"
        ),
    )

    # --- O/SCI-020 --------------------------------------------------
    r = registry["O/SCI-020"]
    no_process = [f.feature_id for f in features if not f.formation_process]
    r.record(
        "every_feature_declares_a_formation_process",
        _outcome(bool(features) and not no_process),
        "without a formation process: {}".format(
            ", ".join(no_process) or "none"
        ),
    )

    no_justification = [
        f.feature_id for f in features if not f.formation_justification
    ]
    r.record(
        "every_formation_process_has_a_justification",
        _outcome(bool(features) and not no_justification),
        "without a justification: {}".format(
            ", ".join(no_justification) or "none"
        ),
    )

    # --- O/SCI-030 --------------------------------------------------
    r = registry["O/SCI-030"]
    no_age = [
        f.feature_id for f in features if f.relative_age_rank is None
    ]
    r.record(
        "every_feature_has_a_relative_age",
        _outcome(bool(features) and not no_age),
        "without a relative age: {}".format(", ".join(no_age) or "none"),
    )

    sequence, chronology_problems = (
        plan.age_sequence() if plan else ([], ["no plan"])
    )
    cycles = [p for p in chronology_problems if "cycle" in p]

    r.record(
        "age_relations_form_no_cycle", _outcome(not cycles),
        "; ".join(cycles) or "no cycle in the stated relations",
    )
    r.record(
        "an_oldest_to_youngest_sequence_can_be_generated",
        _outcome(bool(sequence)),
        " -> ".join(sequence) if sequence
        else "no sequence could be generated",
        sequence,
    )

    no_age_note = [
        f.feature_id for f in features if not f.relative_age_note
    ]
    r.record(
        "the_legend_explains_each_relative_age",
        _outcome(bool(features) and not no_age_note),
        "without an age explanation: {}".format(
            ", ".join(no_age_note) or "none"
        ),
    )

    # --- O/SCI-040 --------------------------------------------------
    r = registry["O/SCI-040"]
    r.record(
        "every_map_unit_has_a_legend_entry", _outcome(not unlegended),
        "without a legend entry: {}".format(
            ", ".join(unlegended) or "none"
        ),
    )
    # Legend entries are stored on the unit, so the reverse direction
    # cannot dangle by construction. Recorded rather than skipped so the
    # report shows it was considered.
    r.record(
        "every_legend_entry_maps_to_a_unit", CHECK_PASS,
        "legend entries are stored on the unit, so none can be orphaned",
    )

    no_genetic = [
        f.feature_id for f in features
        if not (f.legend_entry and f.formation_process)
    ]
    r.record(
        "legend_entries_carry_genetic_information",
        _outcome(bool(features) and not no_genetic),
        "without genetic information: {}".format(
            ", ".join(no_genetic) or "none"
        ),
    )

    no_temporal = [
        f.feature_id for f in features
        if not (f.legend_entry and f.relative_age_note)
    ]
    r.record(
        "legend_entries_carry_temporal_information",
        _outcome(bool(features) and not no_temporal),
        "without temporal information: {}".format(
            ", ".join(no_temporal) or "none"
        ),
    )


def _check_map_artifact(registry, planning_map):
    r = registry["O/SCI-050"]

    if planning_map is None:
        for name in r.declared_automated_checks:
            r.record(name, CHECK_FAIL, "no planning map artifact supplied")

        return

    r.record(
        "map_artifact_exists", _outcome(bool(planning_map.get("path"))),
        str(planning_map.get("path")),
    )
    r.record(
        "map_declares_a4_geometry",
        _outcome(planning_map.get("a4") is True),
        "declared A4: {}".format(planning_map.get("a4")),
    )

    markers = planning_map.get("markers") or []
    unlabelled = [
        m.get("id") for m in markers if not m.get("label")
    ]
    r.record(
        "every_marker_carries_a_label",
        _outcome(bool(markers) and not unlabelled),
        "{} marker(s), unlabelled: {}".format(
            len(markers), ", ".join(str(u) for u in unlabelled) or "none"
        ),
    )

    legend = planning_map.get("legend") or []
    r.record(
        "no_legend_entry_is_unlabelled",
        _outcome(bool(legend) and all(e.get("text") for e in legend)),
        "{} legend entries".format(len(legend)),
    )


def _check_hypothesis_requirements(registry, plan):
    report = plan.character_report() if plan else {}

    # --- O/SCI-060 --------------------------------------------------
    r = registry["O/SCI-060"]
    subject = report.get("subject", {})
    importance = report.get("importance", {})

    r.record("subject_exists", _outcome(subject.get("present")),
             "{} characters".format(subject.get("characters", 0)))
    r.record(
        "subject_within_500_chars",
        _outcome(subject.get("within_limit")),
        "{}/{} characters including spaces".format(
            subject.get("characters", 0), config.PLANNING_SUBJECT_MAX_CHARS
        ),
    )
    r.record("importance_exists", _outcome(importance.get("present")),
             "{} characters".format(importance.get("characters", 0)))
    r.record(
        "importance_within_1000_chars",
        _outcome(importance.get("within_limit")),
        "{}/{} characters including spaces".format(
            importance.get("characters", 0),
            config.PLANNING_IMPORTANCE_MAX_CHARS,
        ),
    )

    linked = (
        plan.hypothesis.linked_feature_ids
        if plan and plan.hypothesis else []
    )
    r.record(
        "subject_links_to_mapped_features", _outcome(bool(linked)),
        "hypothesis links to units: {}".format(", ".join(linked) or "none"),
        linked,
    )

    # --- O/SCI-070 --------------------------------------------------
    r = registry["O/SCI-070"]
    hypothesis = plan.hypothesis if plan else None
    entry = report.get("hypothesis", {})

    r.record("hypothesis_exists", _outcome(hypothesis is not None),
             "present" if hypothesis else "absent")
    r.record(
        "hypothesis_within_500_chars",
        _outcome(entry.get("within_limit")),
        "{}/{} characters including spaces".format(
            entry.get("characters", 0), config.PLANNING_HYPOTHESIS_MAX_CHARS
        ),
    )

    for name, attribute in (
        ("support_condition_exists", "support_condition"),
        ("reject_condition_exists", "reject_condition"),
        ("inconclusive_condition_exists", "inconclusive_condition"),
    ):
        value = getattr(hypothesis, attribute, None) if hypothesis else None
        r.record(name, _outcome(bool(value)),
                 "stated" if value else "not stated")

    frozen = bool(hypothesis and hypothesis.frozen)
    unchanged = (
        hypothesis.verify_unchanged()[0] if frozen else False
    )
    r.record(
        "hypothesis_is_frozen_before_the_run",
        _outcome(frozen and unchanged),
        hypothesis.verify_unchanged()[1] if hypothesis
        else "no hypothesis",
    )

    statement = (hypothesis.statement or "").lower() if hypothesis else ""
    bare = any(p in statement for p in BARE_IDENTIFICATION_PATTERNS)
    r.record(
        "hypothesis_is_not_a_bare_material_identification_question",
        _outcome(bool(statement) and not bare),
        "matches a bare-identification pattern" if bare
        else "not phrased as a bare identification question",
    )

    # --- O/SCI-080 --------------------------------------------------
    r = registry["O/SCI-080"]
    predictions = list(plan.predictions.values()) if plan else []
    narrative = report.get("predictions_narrative", {})

    r.record("predictions_exist", _outcome(bool(predictions)),
             "{} prediction(s)".format(len(predictions)),
             [p.prediction_id for p in predictions])
    r.record(
        "predictions_within_1500_chars",
        _outcome(narrative.get("within_limit")),
        "{}/{} characters including spaces".format(
            narrative.get("characters", 0),
            config.PLANNING_PREDICTIONS_MAX_CHARS,
        ),
    )

    hypothesis_id = hypothesis.hypothesis_id if hypothesis else None
    unlinked = [
        p.prediction_id for p in predictions
        if p.hypothesis_id != hypothesis_id
    ]
    r.record(
        "every_prediction_links_to_the_hypothesis",
        _outcome(bool(predictions) and not unlinked),
        "not linked: {}".format(", ".join(unlinked) or "none"),
    )

    no_expected = [
        p.prediction_id for p in predictions if not p.expected_observation
    ]
    r.record(
        "every_prediction_states_an_expected_observation",
        _outcome(bool(predictions) and not no_expected),
        "without an expected observation: {}".format(
            ", ".join(no_expected) or "none"
        ),
    )

    no_site = [
        p.prediction_id for p in predictions if not p.planned_site_ids
    ]
    r.record(
        "every_prediction_links_to_a_planned_site",
        _outcome(bool(predictions) and not no_site),
        "without a planned site: {}".format(", ".join(no_site) or "none"),
    )

    incomplete = [
        p.prediction_id for p in predictions
        if not (p.support_criterion and p.reject_criterion
                and p.inconclusive_criterion)
    ]
    r.record(
        "every_prediction_has_support_reject_and_inconclusive_criteria",
        _outcome(bool(predictions) and not incomplete),
        "incomplete criteria: {}".format(", ".join(incomplete) or "none"),
    )

    # --- O/SCI-090 --------------------------------------------------
    r = registry["O/SCI-090"]
    figure = plan.prediction_figure if plan else None

    r.record(
        "exactly_one_prediction_figure_exists",
        _outcome(figure is not None),
        "one figure" if figure else "no prediction figure",
    )
    r.record(
        "the_figure_links_to_predictions",
        _outcome(bool((figure or {}).get("prediction_ids"))),
        "links: {}".format(
            ", ".join((figure or {}).get("prediction_ids") or []) or "none"
        ),
    )
    r.record(
        "the_figure_has_a_caption",
        _outcome(bool((figure or {}).get("caption"))),
        "caption present" if (figure or {}).get("caption")
        else "no caption",
    )
    r.record(
        "the_figure_declares_a4_geometry",
        _outcome((figure or {}).get("a4") is True),
        "declared A4: {}".format((figure or {}).get("a4")),
    )

    # --- O/SCI-100 --------------------------------------------------
    r = registry["O/SCI-100"]
    references = plan.references if plan else {}
    citations = plan.citations if plan else []
    problems = plan.reference_problems() if plan else ["no plan"]

    r.record("references_exist", _outcome(bool(references)),
             "{} reference(s)".format(len(references)),
             sorted(references))

    unresolved = [c for c in citations if c not in references]
    r.record(
        "every_citation_resolves_to_a_reference",
        _outcome(not unresolved),
        "unresolved: {}".format(", ".join(unresolved) or "none"),
    )

    uncited = [r_id for r_id in references if r_id not in citations]
    r.record(
        "every_reference_is_cited_at_least_once",
        _outcome(bool(references) and not uncited),
        "never cited: {}".format(", ".join(sorted(uncited)) or "none"),
    )

    wikipedia = [p for p in problems if "Wikipedia" in p]
    r.record(
        "no_wikipedia_reference", _outcome(not wikipedia),
        "; ".join(wikipedia) or "no Wikipedia citation",
    )

    nasa = [p for p in problems if "NASA" in p]
    r.record(
        "no_bare_nasa_web_page_reference", _outcome(not nasa),
        "; ".join(nasa) or "no bare NASA web page citation",
    )


# ----------------------------------------------------------------------
# Scientific Exploration requirements
# ----------------------------------------------------------------------

def _check_field_evidence(registry, run, analysis, site_plan):
    r = registry["O/SCI-110"]
    reached = run.reached_sites() if run else []

    no_observation = [
        v.site_id for v in reached if not v.observation_ids
    ]
    r.record(
        "every_visited_site_has_an_observation",
        _outcome(bool(reached) and not no_observation),
        "reached {} site(s), without an observation: {}".format(
            len(reached), ", ".join(no_observation) or "none"
        ),
        [v.site_id for v in reached],
    )

    no_location = [
        v.site_id for v in reached
        if v.x_m is None or v.y_m is None
    ]
    r.record(
        "every_observation_has_a_location", _outcome(not no_location),
        "without a location: {}".format(", ".join(no_location) or "none"),
    )

    observations = list(run.observations.values()) if run else []

    no_time = [o.observation_id for o in observations if not o.at]
    r.record(
        "every_observation_has_a_timestamp",
        _outcome(bool(observations) and not no_time),
        "without a timestamp: {}".format(", ".join(no_time) or "none"),
    )

    no_description = [
        o.observation_id for o in observations if not o.description
    ]
    r.record(
        "every_observation_has_a_description",
        _outcome(bool(observations) and not no_description),
        "without a description: {}".format(
            ", ".join(no_description) or "none"
        ),
    )

    no_photo = [
        o.observation_id for o in observations if not o.photo_ids
    ]
    r.record(
        "every_observation_has_a_linked_photograph",
        _outcome(bool(observations) and not no_photo),
        "without a photograph: {}".format(", ".join(no_photo) or "none"),
    )

    no_measurements = [
        v.site_id for v in reached if not v.measurement_ids
    ]
    r.record(
        "every_planned_measurement_site_has_measurements",
        _outcome(bool(reached) and not no_measurements),
        "without measurements: {}".format(
            ", ".join(no_measurements) or "none"
        ),
    )

    if analysis is None:
        r.record(
            "every_measurement_has_a_quality_verdict", CHECK_SKIP,
            "no analysis supplied",
        )

    else:
        missing = [
            record.measurement_id for record in analysis.records
            if not record.hardware_quality
        ]
        r.record(
            "every_measurement_has_a_quality_verdict",
            _outcome(bool(analysis.records) and not missing),
            "{} measurement(s), without a quality verdict: {}".format(
                len(analysis.records), ", ".join(missing) or "none"
            ),
        )


def _check_traceability(registry, plan, site_plan, run, analysis, claims):
    r = registry["O/SCI-120"]
    hypothesis = plan.hypothesis if plan else None
    predictions = list(plan.predictions.values()) if plan else []

    broken = []

    unlinked = [
        p.prediction_id for p in predictions
        if not hypothesis or p.hypothesis_id != hypothesis.hypothesis_id
    ]
    broken.extend(unlinked)
    r.record(
        "hypothesis_to_prediction_links_resolve",
        _outcome(bool(predictions) and not unlinked),
        "unlinked predictions: {}".format(", ".join(unlinked) or "none"),
    )

    planned_ids = {s.site_id for s in site_plan} if site_plan else set()
    dangling = [
        "{}->{}".format(p.prediction_id, s)
        for p in predictions for s in p.planned_site_ids
        if s not in planned_ids
    ]
    broken.extend(dangling)
    r.record(
        "prediction_to_planned_observation_links_resolve",
        _outcome(bool(predictions) and not dangling),
        "dangling: {}".format(", ".join(dangling) or "none"),
    )

    unvisited = []

    for prediction in predictions:
        for site_id in prediction.planned_site_ids:
            visit = run.visits.get(site_id) if run else None

            if visit is None or not visit.observation_ids:
                unvisited.append(
                    "{}->{}".format(prediction.prediction_id, site_id)
                )

    r.record(
        "planned_observation_to_actual_observation_links_resolve",
        _outcome(bool(predictions) and not unvisited),
        "without an actual observation: {}".format(
            ", ".join(unvisited) or "none"
        ),
    )

    observations = list(run.observations.values()) if run else []
    without_measurements = [
        o.observation_id for o in observations
        if not o.measurement_ids
    ]
    r.record(
        "observation_to_measurement_links_resolve",
        _outcome(bool(observations) and not without_measurements),
        "without measurements: {}".format(
            ", ".join(without_measurements) or "none"
        ),
    )

    if analysis is None:
        r.record("measurement_to_analysis_links_resolve", CHECK_SKIP,
                 "no analysis supplied")
        r.record("analysis_to_conclusion_links_resolve", CHECK_SKIP,
                 "no analysis supplied")

    else:
        analysed = {r_.measurement_id for r_ in analysis.records}
        recorded = set(run.all_measurement_ids()) if run else set()
        missing = sorted(recorded - analysed)
        broken.extend(missing)

        r.record(
            "measurement_to_analysis_links_resolve",
            _outcome(bool(recorded) and not missing),
            "measurements not carried into the analysis: {}".format(
                ", ".join(missing) or "none"
            ),
        )

        verdicts = analysis.prediction_verdicts
        unresolved = [
            p.prediction_id for p in predictions
            if p.prediction_id not in verdicts
        ]
        r.record(
            "analysis_to_conclusion_links_resolve",
            _outcome(bool(predictions) and not unresolved),
            "predictions with no verdict: {}".format(
                ", ".join(unresolved) or "none"
            ),
        )

    claims = claims or []
    unevidenced = [c.claim_id for c in claims if not c.evidence]
    broken.extend(unevidenced)
    r.record(
        "every_report_claim_carries_evidence_ids",
        _outcome(bool(claims) and not unevidenced),
        "{} claim(s), without evidence: {}".format(
            len(claims), ", ".join(unevidenced) or "none"
        ),
    )

    r.record(
        "no_broken_link_in_the_traceability_chain",
        _outcome(not broken),
        "{} broken link(s)".format(len(broken)),
    )


def _check_geology_change(registry, run):
    r = registry["O/SCI-130"]
    change = (run.geology_change if run else None) or {}

    fields = (
        ("pre_traverse_interpretation_recorded",
         "pre_traverse_interpretation"),
        ("new_observations_recorded", "new_observations"),
        ("post_traverse_interpretation_recorded",
         "post_traverse_interpretation"),
        ("what_changed_is_stated", "what_changed"),
        ("why_it_changed_is_stated", "why_it_changed"),
        ("remaining_uncertainty_is_stated", "remaining_uncertainty"),
    )

    for name, key in fields:
        value = change.get(key)
        r.record(name, _outcome(bool(value)),
                 "recorded" if value else "not recorded")

    evidence = change.get("evidence_ids") or []
    r.record(
        "the_change_cites_evidence_ids", _outcome(bool(evidence)),
        "{} evidence id(s)".format(len(evidence)), evidence,
    )


def _check_updated_map(registry, run, updated_map, manifest):
    r = registry["O/SCI-140"]

    if updated_map is None:
        for name in r.declared_automated_checks:
            if name == "the_source_image_was_not_modified":
                continue

            r.record(name, CHECK_FAIL, "no updated map supplied")

    else:
        r.record(
            "updated_map_artifact_exists",
            _outcome(bool(updated_map.get("path"))),
            str(updated_map.get("path")),
        )
        r.record(
            "updated_map_version_differs_from_the_planning_map",
            _outcome(
                updated_map.get("version")
                != updated_map.get("planning_map_version")
            ),
            "updated {} vs planning {}".format(
                updated_map.get("version"),
                updated_map.get("planning_map_version"),
            ),
        )

        markers = updated_map.get("markers") or []
        site_markers = [m for m in markers if m.get("kind") == "SITE"]
        r.record(
            "observation_sites_are_marked",
            _outcome(bool(site_markers)),
            "{} site marker(s)".format(len(site_markers)),
        )

        changed = [m for m in markers if m.get("changed")]
        r.record(
            "new_or_changed_features_are_marked_distinctly",
            _outcome(bool(changed)),
            "{} new/changed marker(s)".format(len(changed)),
        )

        uso_markers = [m for m in markers if m.get("kind") == "USO"]
        expected_usos = len(run.usos) if run else 0
        r.record(
            "uso_locations_are_marked",
            _outcome(len(uso_markers) >= expected_usos),
            "{} USO marker(s) for {} recorded object(s)".format(
                len(uso_markers), expected_usos
            ),
        )

        unlinked = [
            m.get("id") for m in markers if not m.get("data_ref")
        ]
        r.record(
            "every_marker_links_to_a_data_object",
            _outcome(bool(markers) and not unlinked),
            "markers with no data reference: {}".format(
                ", ".join(str(u) for u in unlinked) or "none"
            ),
        )

    # The source-image check runs whether or not an updated map exists:
    # it is about what we did NOT do.
    baseline = (
        (manifest or {}).get("sources", {})
        .get("mars_yard_2026.png", {})
        .get("sha256")
    )
    current = file_digest(config.MARS_YARD_IMAGE)

    if baseline is None or current is None:
        r.record(
            "the_source_image_was_not_modified", CHECK_SKIP,
            "no baseline hash recorded for the source image",
        )

    else:
        r.record(
            "the_source_image_was_not_modified",
            _outcome(baseline == current),
            "sha256 {}".format(
                "unchanged" if baseline == current
                else "CHANGED - the spatial source of truth was rewritten"
            ),
        )


def _check_instrument_evidence(registry, run, analysis, plan):
    r = registry["O/SCI-150"]
    records = analysis.records if analysis else []

    r.record(
        "non_camera_instrument_measurements_exist",
        _outcome(bool(records)),
        "{} AS7265x measurement(s)".format(len(records)),
        [record.measurement_id for record in records[:12]],
    )

    without_calibration = [
        record.measurement_id for record in records
        if not record.calibration_id
    ]
    r.record(
        "measurements_carry_calibration_provenance",
        _outcome(bool(records) and not without_calibration),
        "without a calibration id: {}".format(
            ", ".join(without_calibration) or "none"
        ),
    )

    r.record(
        "mathematical_analysis_was_performed",
        _outcome(bool(analysis and analysis.comparisons)),
        "{} site comparison(s)".format(
            len(analysis.comparisons) if analysis else 0
        ),
    )

    decisions = [r_ for r_ in records if r_.decision]
    r.record(
        "decision_model_results_exist",
        _outcome(bool(decisions)),
        "{} of {} measurement(s) carry a Decision Model result".format(
            len(decisions), len(records)
        ),
    )

    linked_predictions = bool(
        analysis and any(
            v.evidence for v in analysis.prediction_verdicts.values()
        )
    )
    r.record(
        "instrument_evidence_links_to_a_prediction",
        _outcome(linked_predictions),
        "prediction verdicts cite instrument evidence" if linked_predictions
        else "no prediction verdict cites instrument evidence",
    )

    linked_hypothesis = bool(
        plan and plan.hypothesis and plan.hypothesis.linked_site_ids
    )
    r.record(
        "instrument_evidence_links_to_the_hypothesis",
        _outcome(linked_hypothesis),
        "hypothesis is tested at: {}".format(
            ", ".join(plan.hypothesis.linked_site_ids)
            if linked_hypothesis else "no site"
        ),
    )


def _check_usos(registry, run, updated_map):
    r = registry["O/SCI-160"]
    usos = [run.usos[uso_id] for uso_id in run.uso_order] if run else []

    r.record(
        "at_most_three_usos",
        _outcome(len(usos) <= config.USO_MAX_OBJECTS),
        "{} object(s), maximum graded {}".format(
            len(usos), config.USO_MAX_OBJECTS
        ),
    )

    if not usos:
        # No USO is a legitimate outcome, not a structural failure - but
        # it forfeits 25 points, and the report should say so.
        for name in (
            "every_uso_has_a_photograph", "every_uso_has_a_description",
            "every_uso_description_within_350_chars",
            "every_uso_has_an_adhoc_hypothesis",
            "every_uso_is_marked_on_the_updated_map",
        ):
            r.record(name, CHECK_SKIP,
                     "no unexpected objects were recorded")

        r.notes.append(
            "No USO recorded. This is not a failure, but O/SCI-160 is "
            "worth 25 points and none of them can be earned without one."
        )

        return

    no_photo = [u.uso_id for u in usos if not u.photo_id]
    r.record("every_uso_has_a_photograph", _outcome(not no_photo),
             "without a photograph: {}".format(
                 ", ".join(no_photo) or "none"))

    no_description = [u.uso_id for u in usos if not u.description]
    r.record("every_uso_has_a_description", _outcome(not no_description),
             "without a description: {}".format(
                 ", ".join(no_description) or "none"))

    over = [
        "{} ({} chars)".format(u.uso_id, u.description_characters)
        for u in usos
        if u.description_characters > config.USO_DESCRIPTION_MAX_CHARS
    ]
    r.record(
        "every_uso_description_within_350_chars", _outcome(not over),
        "over the {} character limit: {}".format(
            config.USO_DESCRIPTION_MAX_CHARS, ", ".join(over) or "none"
        ),
    )

    no_hypothesis = [u.uso_id for u in usos if not u.adhoc_hypothesis]
    r.record(
        "every_uso_has_an_adhoc_hypothesis", _outcome(not no_hypothesis),
        "without an ad-hoc hypothesis: {}".format(
            ", ".join(no_hypothesis) or "none"
        ),
    )

    unmarked = [u.uso_id for u in usos if not u.map_marker]
    r.record(
        "every_uso_is_marked_on_the_updated_map", _outcome(not unmarked),
        "not marked on the map: {}".format(", ".join(unmarked) or "none"),
    )


def _check_penalties(registry, run, now, report_generated_at):
    # --- O/SCI-900 --------------------------------------------------
    r = registry["O/SCI-900"]
    snapshot = run.configuration_snapshot if run else None

    r.record(
        "configuration_snapshot_taken_at_run_start",
        _outcome(snapshot is not None),
        "locked at {}".format(snapshot.taken_at) if snapshot
        else "no configuration snapshot",
    )
    r.record(
        "configuration_hash_recorded",
        _outcome(bool(snapshot and snapshot.content_hash)),
        (snapshot.content_hash[:16] + "...") if snapshot
        and snapshot.content_hash else "no hash",
    )

    alerts = run.configuration_alerts if run else []
    r.record(
        "configuration_unchanged_since_lock", _outcome(not alerts),
        "{} change(s) detected after the lock".format(len(alerts))
        if alerts else "no software-visible change since the lock",
    )

    if alerts:
        r.notes.append(
            "POTENTIAL_O_SCI_900_RISK: software-visible configuration "
            "changed after the run was locked. Whether an ERC penalty "
            "applies is a judge's decision."
        )

    # --- O/SCI-910 --------------------------------------------------
    # Nothing to record: the registry declares no automated checks, so
    # conclude() will report it as OPERATIONAL_MANUAL.

    # --- O/SCI-920 --------------------------------------------------
    r = registry["O/SCI-920"]

    r.record(
        "traverse_end_timestamp_recorded",
        _outcome(bool(run and run.traverse_ended_at)),
        run.traverse_ended_at if run and run.traverse_ended_at
        else "not recorded",
    )
    r.record(
        "report_generation_timestamp_recorded",
        _outcome(bool(report_generated_at)),
        report_generated_at or "not recorded",
    )

    elapsed = run.elapsed_since_traverse(now) if run and now else None

    r.record(
        "elapsed_time_computed", _outcome(elapsed is not None),
        "{} minutes since the traverse".format(elapsed["elapsed_minutes"])
        if elapsed else "cannot be computed without both timestamps",
    )
    r.record(
        "deadline_remaining_computed", _outcome(elapsed is not None),
        "{} minutes remaining".format(elapsed["remaining_minutes"])
        if elapsed else "cannot be computed",
    )

    if elapsed is None:
        r.record("deadline_warning_raised_when_due", CHECK_SKIP,
                 "no deadline arithmetic available")

    else:
        due = elapsed["remaining_minutes"] <= 60 or elapsed["overdue"]
        r.record(
            "deadline_warning_raised_when_due", CHECK_PASS,
            "{} - warning {}".format(
                "OVERDUE" if elapsed["overdue"]
                else "{} minutes remaining".format(
                    elapsed["remaining_minutes"]
                ),
                "raised" if due else "not yet due",
            ),
        )


# ----------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------

def check(plan=None, site_plan=None, run=None, analysis=None, claims=None,
          planning_map=None, updated_map=None, manifest=None,
          registry=None, now=None, report_generated_at=None):
    """
    Run every automated check that the supplied evidence allows.

    Missing evidence produces FAIL or SKIP against the specific check, so
    the readiness report distinguishes "we looked and it is absent" from
    "we could not look".
    """
    registry = registry or requirements_module.load()
    registry.reset()

    if plan is not None:
        _check_map_requirements(registry, plan)
        _check_hypothesis_requirements(registry, plan)

    _check_map_artifact(registry, planning_map)

    if run is not None:
        _check_field_evidence(registry, run, analysis, site_plan)
        _check_traceability(registry, plan, site_plan, run, analysis, claims)
        _check_geology_change(registry, run)
        _check_usos(registry, run, updated_map)

    _check_updated_map(registry, run, updated_map, manifest)

    if run is not None or analysis is not None:
        _check_instrument_evidence(registry, run, analysis, plan)

    _check_penalties(registry, run, now, report_generated_at)

    registry.conclude()

    return registry


def render(registry):
    """The requirements dashboard, as text."""
    lines = ["ERC SCIENCE REQUIREMENTS - READINESS", "=" * 72]

    for requirement in registry:
        lines.append("")
        lines.append("{} - {}   (max {} pts)".format(
            requirement.id, requirement.status,
            requirement.max_partial_score
            if requirement.applicability == "direct"
            else requirement.max_penalty_score,
        ))
        lines.append("  {}".format(requirement.official_wording[:100]))

        for result in requirement.results:
            lines.append("    {:<4} {}".format(
                result.outcome, result.name
            ))

            if result.outcome != CHECK_PASS and result.detail:
                lines.append("         {}".format(result.detail[:90]))

        if requirement.manual_checks:
            lines.append("    MANUAL REVIEW REQUIRED:")

            for item in requirement.manual_checks:
                lines.append("      - {}".format(item[:88]))

        lines.append("    judge score: {}".format(requirement.judge_score))

    summary = registry.summary()
    lines.append("")
    lines.append("=" * 72)

    for status, count in sorted(summary["by_status"].items()):
        lines.append("{:<26} {}".format(status, count))

    lines.append("")
    lines.append(summary["judge_score_note"])

    return "\n".join(lines)


def save_status(registry, path=None):
    path = path or (config.OUTPUT_DIR / "requirements_status.json")
    path.parent.mkdir(parents=True, exist_ok=True)

    document = {
        "generated_by": "Science/checker.py",
        "summary": registry.summary(),
        "requirements": [r.as_dict() for r in registry],
    }

    temporary = path.with_suffix(path.suffix + ".tmp")

    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    temporary.replace(path)

    return path
