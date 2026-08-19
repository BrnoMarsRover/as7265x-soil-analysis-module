"""
The four-site planner.

The mission visits exactly four scientific sites. This module chooses
them, and — more importantly — explains why each one was chosen and what
it would take to overturn that choice.

The rule that shapes the selection: **a site is chosen for what is there,
not for its number.** Starting locations S1-S9 are excluded outright.
They mark where a run begins; choosing them as measurement targets would
be choosing a convenient label. The deep sampling point P1 is excluded
too, because its location is organiser-defined and the rules forbid using
that sub-task's material as Scientific Exploration evidence.

What remains is 24 surveyed landmarks and navigation waypoints, all with
coordinates printed on the source map.

Selection scores five things, all of them either measured from the source
coordinates or asserted by a human in the science plan:

    geological contrast    is this a different mapped unit?
    hypothesis relevance   does the hypothesis name this unit?
    spatial separation     is it far enough from what we already picked?
    role coverage          do we still need a control, or a contrast?
    route feasibility      how far from the start, and from the others?

Nothing here scores "looks interesting on the photo". The operator may
override any choice, and the override is recorded with its reason so the
final report can say a human made that call.

Layer rule: Science may import BD, Science and Science.decision.
"""

import json

from research.erc import config
from research.erc import mars_yard as yard_module

# What a site is FOR. A study with four targets and no control cannot
# tell "these two differ" from "our instrument drifts".
ROLE_CONTROL = "CONTROL"
ROLE_TARGET_A = "TARGET_A"
ROLE_TARGET_B = "TARGET_B"
ROLE_TRANSITION = "TRANSITION"

ROLES = (ROLE_CONTROL, ROLE_TARGET_A, ROLE_TARGET_B, ROLE_TRANSITION)

SELECTED_BY_PLANNER = "PLANNER"
SELECTED_BY_OPERATOR = "OPERATOR_OVERRIDE"

# Below this the two sites are close enough that one rover position could
# almost see both, and "between-site difference" starts to mean "between
# two patches of the same spot". Chosen from the yard's own scale: the
# surveyed objects span roughly 29 m by 33 m.
MIN_SITE_SEPARATION_M = 4.0


class SiteError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _point_in_polygon(x, y, vertices):
    """
    Ray casting. Vertices are [{"x_m":…, "y_m":…}, …].

    Used only to work out which mapped unit a surveyed point falls in,
    never to create a coordinate.
    """
    if len(vertices) < 3:
        return False

    inside = False
    count = len(vertices)

    for index in range(count):
        x1 = vertices[index]["x_m"]
        y1 = vertices[index]["y_m"]
        x2 = vertices[(index + 1) % count]["x_m"]
        y2 = vertices[(index + 1) % count]["y_m"]

        if (y1 > y) != (y2 > y):
            crossing = x1 + (y - y1) * (x2 - x1) / (y2 - y1)

            if x < crossing:
                inside = not inside

    return inside


def feature_at(point, plan):
    """
    Which mapped geological unit a surveyed point falls in.

    Anchor membership wins over geometry: if a human wrote "L4 is in this
    unit", that is a stronger statement than a polygon test against an
    outline they sketched.
    """
    for feature in plan.features.values():
        if point.point_id in feature.anchor_points:
            return feature

    for feature in plan.features.values():
        if feature.outline and _point_in_polygon(
            point.x_m, point.y_m, feature.outline
        ):
            return feature

    return None


class Candidate:
    """One eligible surveyed object, with its scoring rationale."""

    def __init__(self, point, feature):
        self.point = point
        self.feature = feature
        self.reasons = []
        self.score = 0.0

    @property
    def point_id(self):
        return self.point.point_id

    @property
    def feature_id(self):
        return self.feature.feature_id if self.feature else None

    def credit(self, amount, reason):
        self.score += amount
        self.reasons.append({"points": amount, "reason": reason})

    def as_dict(self):
        return {
            "point_id": self.point_id,
            "point_type": self.point.type,
            "x_m": self.point.x_m,
            "y_m": self.point.y_m,
            "h_m": self.point.h_m,
            "geological_feature": self.feature_id,
            "score": round(self.score, 3),
            "reasons": self.reasons,
        }

    def __repr__(self):
        return "<Candidate {} score={:.2f}>".format(
            self.point_id, self.score
        )


class PlannedSite:
    """One chosen scientific site, with everything the report will need."""

    def __init__(self, entry):
        self.site_id = entry["site_id"]

        self.source_point_id = entry["source_point_id"]
        self.source_point_type = entry["source_point_type"]
        self.coordinate_status = entry.get(
            "coordinate_status", yard_module.SOURCE_GROUNDED
        )
        self.coordinate_frame = entry.get("coordinate_frame")

        self.x_m = entry.get("x_m")
        self.y_m = entry.get("y_m")
        self.h_m = entry.get("h_m")

        self.geological_feature_id = entry.get("geological_feature_id")
        self.geological_context = entry.get("geological_context")

        self.role = entry.get("role")
        self.scientific_purpose = entry.get("scientific_purpose")

        self.hypothesis_id = entry.get("hypothesis_id")
        self.prediction_ids = list(entry.get("prediction_ids") or [])

        self.expected_observation = entry.get("expected_observation")
        self.support_condition = entry.get("support_condition")
        self.reject_condition = entry.get("reject_condition")

        self.measurement_plan = entry.get("measurement_plan") or {}
        self.photograph_required = entry.get("photograph_required", True)

        self.selection_reason = entry.get("selection_reason")
        self.selected_by = entry.get("selected_by", SELECTED_BY_PLANNER)
        self.override_note = entry.get("override_note")
        self.score_detail = entry.get("score_detail") or []

    def as_dict(self):
        return {
            "site_id": self.site_id,
            "source_point_id": self.source_point_id,
            "source_point_type": self.source_point_type,
            "coordinate_status": self.coordinate_status,
            "coordinate_frame": self.coordinate_frame,
            "x_m": self.x_m,
            "y_m": self.y_m,
            "h_m": self.h_m,
            "geological_feature_id": self.geological_feature_id,
            "geological_context": self.geological_context,
            "role": self.role,
            "scientific_purpose": self.scientific_purpose,
            "hypothesis_id": self.hypothesis_id,
            "prediction_ids": self.prediction_ids,
            "expected_observation": self.expected_observation,
            "support_condition": self.support_condition,
            "reject_condition": self.reject_condition,
            "measurement_plan": self.measurement_plan,
            "photograph_required": self.photograph_required,
            "selection_reason": self.selection_reason,
            "selected_by": self.selected_by,
            "override_note": self.override_note,
            "score_detail": self.score_detail,
        }

    def __repr__(self):
        return "<Site {} at {} ({})>".format(
            self.site_id, self.source_point_id, self.role
        )


class SitePlan:
    """Exactly four planned sites, and the record of how they were chosen."""

    def __init__(self, sites, candidates=None, generated_at=None,
                 planner_version="SITE_PLANNER_V001"):
        self.sites = list(sites)
        self.candidates = list(candidates or [])
        self.generated_at = generated_at
        self.planner_version = planner_version
        self.overrides = []

    def __iter__(self):
        return iter(self.sites)

    def __len__(self):
        return len(self.sites)

    def by_id(self, site_id):
        for site in self.sites:
            if site.site_id == site_id:
                return site

        raise SiteError(
            "NO_SUCH_SITE", "{} is not a planned site".format(site_id)
        )

    def point_ids(self):
        return [site.source_point_id for site in self.sites]

    # ------------------------------------------------------------------
    # operator override
    # ------------------------------------------------------------------

    def override(self, site_id, point_id, yard, plan, reason, timestamp):
        """
        Move one site to a different surveyed object.

        The operator is allowed to disagree with the planner. What they
        are not allowed to do is disagree invisibly, so the previous
        choice, the new one and the stated reason are all kept.
        """
        if not reason or not reason.strip():
            raise SiteError(
                "NO_REASON",
                "an override must state why. A site that changed for no "
                "recorded reason cannot be defended in the report.",
            )

        site = self.by_id(site_id)
        point = yard.get(point_id)

        if point is None:
            raise SiteError(
                "NO_SUCH_POINT",
                "{} is not a surveyed object on the map".format(point_id),
            )

        if point.type not in config.SITE_ELIGIBLE_TYPES:
            raise SiteError(
                "INELIGIBLE_POINT",
                "{} is a {}, which cannot carry a scientific site. "
                "Eligible types are {}.".format(
                    point_id, point.type,
                    ", ".join(config.SITE_ELIGIBLE_TYPES),
                ),
            )

        if point.excluded_from_scientific_exploration:
            raise SiteError(
                "EXCLUDED_POINT",
                "{} is excluded from Scientific Exploration: {}".format(
                    point_id, point.exclusion_reason
                ),
            )

        taken = {
            other.source_point_id for other in self.sites
            if other.site_id != site_id
        }

        if point_id in taken:
            raise SiteError(
                "DUPLICATE_POINT",
                "{} is already used by another site".format(point_id),
            )

        previous = site.source_point_id
        feature = feature_at(point, plan)

        site.source_point_id = point.point_id
        site.source_point_type = point.type
        site.x_m = point.x_m
        site.y_m = point.y_m
        site.h_m = point.h_m
        site.geological_feature_id = feature.feature_id if feature else None
        site.geological_context = feature.name if feature else None
        site.selected_by = SELECTED_BY_OPERATOR
        site.override_note = reason
        site.selection_reason = "operator override: {}".format(reason)

        self.overrides.append({
            "site_id": site_id,
            "from_point_id": previous,
            "to_point_id": point_id,
            "reason": reason,
            "at": timestamp,
        })

        return site

    # ------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------

    def validate(self, yard, plan=None):
        problems = []

        if len(self.sites) != config.PLANNED_SITE_COUNT:
            problems.append(
                "the plan has {} sites; exactly {} are required".format(
                    len(self.sites), config.PLANNED_SITE_COUNT
                )
            )

        seen_sites = set()
        seen_points = set()

        for site in self.sites:
            if site.site_id in seen_sites:
                problems.append(
                    "site id {} is used more than once".format(site.site_id)
                )

            seen_sites.add(site.site_id)

            if site.source_point_id in seen_points:
                problems.append(
                    "{} is used by more than one site".format(
                        site.source_point_id
                    )
                )

            seen_points.add(site.source_point_id)

            point = yard.get(site.source_point_id)

            if point is None:
                problems.append(
                    "{} sits on {}, which is not on the source map".format(
                        site.site_id, site.source_point_id
                    )
                )
                continue

            if point.type not in config.SITE_ELIGIBLE_TYPES:
                problems.append(
                    "{} sits on {}, a {} - not an eligible science "
                    "target".format(
                        site.site_id, point.point_id, point.type
                    )
                )

            if not point.has_coordinate:
                problems.append(
                    "{} sits on {}, which has no source-grounded "
                    "coordinate".format(site.site_id, point.point_id)
                )

            # The site must carry the SAME numbers the map printed.
            for axis in ("x_m", "y_m", "h_m"):
                if getattr(site, axis) != getattr(point, axis):
                    problems.append(
                        "{} records {}={} but the source map gives {} for "
                        "{}".format(
                            site.site_id, axis, getattr(site, axis),
                            getattr(point, axis), point.point_id,
                        )
                    )

            if site.coordinate_status != yard_module.SOURCE_GROUNDED:
                problems.append(
                    "{} does not declare a source-grounded "
                    "coordinate".format(site.site_id)
                )

            if site.role not in ROLES:
                problems.append(
                    "{} declares role {!r}, which is not one of {}".format(
                        site.site_id, site.role, ", ".join(ROLES)
                    )
                )

            if not site.selection_reason:
                problems.append(
                    "{} states no reason for being selected".format(
                        site.site_id
                    )
                )

            if not site.prediction_ids:
                problems.append(
                    "{} is linked to no prediction, so nothing it "
                    "measures can test the hypothesis".format(site.site_id)
                )

            repeats = (site.measurement_plan or {}).get("repeats")

            if not repeats or repeats < config.MIN_REPEATS_FOR_SPREAD:
                problems.append(
                    "{} plans {} repeat(s); at least {} are needed before "
                    "within-site spread exists at all".format(
                        site.site_id, repeats,
                        config.MIN_REPEATS_FOR_SPREAD,
                    )
                )

        # Separation is checked between every pair, not just neighbours.
        located = [s for s in self.sites if s.x_m is not None]

        for index, first in enumerate(located):
            for second in located[index + 1:]:
                gap = (
                    (first.x_m - second.x_m) ** 2
                    + (first.y_m - second.y_m) ** 2
                ) ** 0.5

                if gap < MIN_SITE_SEPARATION_M:
                    problems.append(
                        "{} and {} are {:.2f} m apart, below the {:.1f} m "
                        "minimum separation".format(
                            first.site_id, second.site_id, gap,
                            MIN_SITE_SEPARATION_M,
                        )
                    )

        if not any(site.role == ROLE_CONTROL for site in self.sites):
            problems.append(
                "no site has the CONTROL role; without one, a difference "
                "between two targets cannot be separated from instrument "
                "or day-to-day variation"
            )

        if plan is not None:
            for site in self.sites:
                for prediction_id in site.prediction_ids:
                    if prediction_id not in plan.predictions:
                        problems.append(
                            "{} links to unknown prediction {}".format(
                                site.site_id, prediction_id
                            )
                        )

                if (
                    site.geological_feature_id
                    and site.geological_feature_id not in plan.features
                ):
                    problems.append(
                        "{} names unknown geological feature {}".format(
                            site.site_id, site.geological_feature_id
                        )
                    )

        return problems

    def as_dict(self):
        return {
            "schema_version": config.SCIENCE_PLAN_SCHEMA_VERSION,
            "planner_version": self.planner_version,
            "generated_at": self.generated_at,
            "site_count": len(self.sites),
            "required_site_count": config.PLANNED_SITE_COUNT,
            "sites": [site.as_dict() for site in self.sites],
            "overrides": self.overrides,
            "candidates_considered": [c for c in self.candidates],
        }

    def save(self, path=None):
        path = path or config.PLANNED_SITES_FILE
        path.parent.mkdir(parents=True, exist_ok=True)

        temporary = path.with_suffix(path.suffix + ".tmp")

        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(self.as_dict(), handle, indent=2, ensure_ascii=False)
            handle.write("\n")

        temporary.replace(path)

        return path

    @classmethod
    def load(cls, path=None):
        path = path or config.PLANNED_SITES_FILE

        try:
            with open(path, "r", encoding="utf-8") as handle:
                document = json.load(handle)

        except OSError as error:
            raise SiteError(
                "MISSING",
                "planned sites could not be read from {}: {}".format(
                    path, error
                ),
            )

        except ValueError as error:
            raise SiteError(
                "MALFORMED",
                "planned sites at {} are not valid JSON: {}".format(
                    path, error
                ),
            )

        plan = cls(
            [PlannedSite(entry) for entry in document.get("sites") or []],
            candidates=document.get("candidates_considered") or [],
            generated_at=document.get("generated_at"),
            planner_version=document.get("planner_version"),
        )
        plan.overrides = document.get("overrides") or []

        return plan


# ----------------------------------------------------------------------
# selection
# ----------------------------------------------------------------------

def score_candidates(yard, plan, start_point_id="S1"):
    """
    Score every eligible surveyed object.

    Scores are additive and each one records its reason, so the planner's
    output can be read as an argument rather than a ranking.
    """
    start = yard.get(start_point_id)
    hypothesis_features = set(
        plan.hypothesis.linked_feature_ids if plan.hypothesis else []
    )

    # Which units the predictions actually ask us to compare.
    predicted_features = set()

    for prediction in plan.predictions.values():
        for feature_id in (prediction.comparison or {}).get(
            "feature_ids", []
        ):
            predicted_features.add(feature_id)

    candidates = []

    for point in yard.site_candidates():
        feature = feature_at(point, plan)
        candidate = Candidate(point, feature)

        if feature is None:
            candidate.credit(
                0.0,
                "falls in no mapped geological unit, so nothing about it "
                "can be linked to the hypothesis",
            )
            candidates.append(candidate)
            continue

        candidate.credit(
            2.0,
            "lies in mapped unit {} ({})".format(
                feature.feature_id, feature.name
            ),
        )

        if feature.feature_id in hypothesis_features:
            candidate.credit(
                4.0,
                "the hypothesis names unit {} directly".format(
                    feature.feature_id
                ),
            )

        if feature.feature_id in predicted_features:
            candidate.credit(
                3.0,
                "a prediction asks for a measurement in unit {}".format(
                    feature.feature_id
                ),
            )

        # A landmark is a physical feature someone surveyed because it is
        # there; a navigation waypoint is a position. Both are eligible,
        # but the landmark is more likely to BE something.
        if point.type == yard_module.LANDMARK:
            candidate.credit(
                1.0, "a surveyed landmark rather than a bare position"
            )

        if start is not None and start.has_coordinate:
            reach = point.distance_to(start)

            # Reachability, not a preference for being close: anything
            # inside the yard is drivable, so this only separates the far
            # corner from the rest.
            candidate.credit(
                max(0.0, 1.5 - reach / 30.0),
                "{:.1f} m from the {} start position".format(
                    reach, start_point_id
                ),
            )

        candidates.append(candidate)

    candidates.sort(key=lambda c: (-c.score, c.point_id))

    return candidates


# What each kind of prediction comparison needs a site FOR.
#
# Derived from the plan rather than configured, so that changing the
# experiment changes the site roles with it. An earlier version of this
# module scored candidates and then handed out roles by list position;
# that put three sites in one unit, left the control unit unvisited, and
# still called the first site CONTROL. Roles now come from what the
# predictions actually ask for.
COMPARISON_TARGET = "BETWEEN_SITE_VS_WITHIN_SITE"
COMPARISON_CONTROL = "WITHIN_SITE_SPREAD_COMPARISON"
COMPARISON_CONTACT = "CONTACT_GEOMETRY"

TARGET_ROLES = (ROLE_TARGET_A, ROLE_TARGET_B)


def _unit_centroid(feature, yard):
    """Mean position of a unit's anchor points, in metres."""
    located = [
        yard[point_id] for point_id in feature.anchor_points
        if yard.get(point_id) is not None
        and yard[point_id].has_coordinate
    ]

    if not located:
        return None

    return (
        sum(p.x_m for p in located) / len(located),
        sum(p.y_m for p in located) / len(located),
    )


def required_coverage(plan):
    """
    Which mapped units the experiment must visit, and in what role.

    Read from the predictions' own `comparison` declarations, so the
    answer is the experiment's, not the planner's:

        BETWEEN_SITE_VS_WITHIN_SITE   its units are the targets
        WITHIN_SITE_SPREAD_COMPARISON its unit is the control
        CONTACT_GEOMETRY              its unit pair needs a contact site

    Returns (targets, controls, contacts).
    """
    targets = []
    controls = []
    contacts = []

    for prediction_id in plan.prediction_order:
        comparison = plan.predictions[prediction_id].comparison or {}
        kind = comparison.get("kind")
        features = list(comparison.get("feature_ids") or [])

        if kind == COMPARISON_TARGET:
            for feature_id in features:
                if feature_id not in targets:
                    targets.append(feature_id)

        elif kind == COMPARISON_CONTROL:
            for feature_id in features:
                if feature_id not in controls:
                    controls.append(feature_id)

        elif kind == COMPARISON_CONTACT and len(features) >= 2:
            pair = (features[0], features[1])

            if pair not in contacts:
                contacts.append(pair)

    return targets, controls, contacts


def select(yard, plan, generated_at, start_point_id="S1", repeats=None):
    """
    Choose exactly four sites, driven by what the predictions require.

    Allocation order is deliberate. The units the hypothesis is about are
    placed first, then the control, then a site at the contact — because
    running out of slots should cost the least important observation, not
    the control that makes the others interpretable.

    Within each required unit the highest-scoring candidate wins, with
    the point id breaking ties, so the result is deterministic.
    """
    repeats = repeats or config.DEFAULT_REPEATS_PER_SAMPLE

    if plan.hypothesis is None:
        raise SiteError(
            "NO_HYPOTHESIS",
            "sites cannot be planned before a hypothesis exists: there "
            "would be nothing for them to test",
        )

    if not plan.predictions:
        raise SiteError(
            "NO_PREDICTIONS",
            "sites cannot be planned before predictions exist: the "
            "predictions are what say which units must be visited",
        )

    targets, controls, contacts = required_coverage(plan)

    if not targets:
        raise SiteError(
            "NO_TARGET_UNITS",
            "no prediction declares a {} comparison, so no unit is "
            "identified as a target of the hypothesis".format(
                COMPARISON_TARGET
            ),
        )

    if len(targets) > len(TARGET_ROLES):
        raise SiteError(
            "TOO_MANY_TARGETS",
            "the predictions declare {} target units ({}), but the site "
            "role vocabulary defines only {}. Either reduce the "
            "comparison or extend TARGET_ROLES deliberately.".format(
                len(targets), ", ".join(targets), len(TARGET_ROLES)
            ),
        )

    required_count = len(targets) + len(controls) + len(contacts)

    if required_count > config.PLANNED_SITE_COUNT:
        raise SiteError(
            "OVERSUBSCRIBED",
            "the predictions require {} sites ({} target, {} control, {} "
            "contact) but the mission plans {}. Nothing is dropped "
            "silently - reduce the predictions or raise "
            "PLANNED_SITE_COUNT.".format(
                required_count, len(targets), len(controls), len(contacts),
                config.PLANNED_SITE_COUNT,
            ),
        )

    candidates = score_candidates(yard, plan, start_point_id)

    by_unit = {}

    for candidate in candidates:
        if candidate.feature is not None:
            by_unit.setdefault(candidate.feature_id, []).append(candidate)

    chosen = []          # (candidate, role, reason)
    taken = set()

    def far_enough(candidate):
        for other, _role, _reason in chosen:
            gap = (
                (candidate.point.x_m - other.point.x_m) ** 2
                + (candidate.point.y_m - other.point.y_m) ** 2
            ) ** 0.5

            if gap < MIN_SITE_SEPARATION_M:
                return False

        return True

    def take(unit_id, role, reason, order=None):
        pool = order if order is not None else by_unit.get(unit_id, [])

        for candidate in pool:
            if candidate.point_id in taken:
                continue

            if not far_enough(candidate):
                continue

            chosen.append((candidate, role, reason))
            taken.add(candidate.point_id)

            return candidate

        return None

    unplaced = []

    # --- the units the hypothesis is about --------------------------
    for index, unit_id in enumerate(targets):
        placed = take(
            unit_id,
            TARGET_ROLES[index],
            "unit {} is a target of hypothesis {}".format(
                unit_id, plan.hypothesis.hypothesis_id
            ),
        )

        if placed is None:
            unplaced.append((unit_id, TARGET_ROLES[index]))

    # --- the control ------------------------------------------------
    for unit_id in controls:
        placed = take(
            unit_id,
            ROLE_CONTROL,
            "unit {} is the control: it is measured with the same "
            "instrument and calibration so that a difference between the "
            "target units can be told apart from session variation"
            .format(unit_id),
        )

        if placed is None:
            unplaced.append((unit_id, ROLE_CONTROL))

    # --- the contact ------------------------------------------------
    # The best site for a contact question is the candidate that sits
    # closest to the OTHER unit, since that is where the two meet. This
    # uses only surveyed coordinates.
    for first_id, second_id in contacts:
        first = plan.features.get(first_id)
        second = plan.features.get(second_id)

        if first is None or second is None:
            unplaced.append(("{}/{}".format(first_id, second_id),
                             ROLE_TRANSITION))
            continue

        pool = []

        for unit_id, other in ((first_id, second), (second_id, first)):
            centroid = _unit_centroid(other, yard)

            if centroid is None:
                continue

            for candidate in by_unit.get(unit_id, []):
                reach = (
                    (candidate.point.x_m - centroid[0]) ** 2
                    + (candidate.point.y_m - centroid[1]) ** 2
                ) ** 0.5
                pool.append((reach, candidate.point_id, candidate))

        pool.sort()

        placed = take(
            None,
            ROLE_TRANSITION,
            "closest available candidate to the {}/{} contact, measured "
            "from the surveyed anchor centroids of both units".format(
                first_id, second_id
            ),
            order=[candidate for _reach, _pid, candidate in pool],
        )

        if placed is None:
            unplaced.append(("{}/{}".format(first_id, second_id),
                             ROLE_TRANSITION))

    if unplaced:
        raise SiteError(
            "COVERAGE_NOT_MET",
            "the predictions require sites that could not be placed: {}. "
            "Either the unit has no eligible surveyed object, or every "
            "candidate in it is within {:.1f} m of a site already "
            "chosen.".format(
                "; ".join(
                    "{} as {}".format(unit, role) for unit, role in unplaced
                ),
                MIN_SITE_SEPARATION_M,
            ),
        )

    # --- remaining slots --------------------------------------------
    # Spare capacity goes to a second observation of a target unit,
    # because replication within a target is what turns "these two spots
    # differ" into "these two units differ".
    for candidate in candidates:
        if len(chosen) >= config.PLANNED_SITE_COUNT:
            break

        if candidate.feature is None or candidate.point_id in taken:
            continue

        if candidate.feature_id not in targets:
            continue

        if not far_enough(candidate):
            continue

        chosen.append((
            candidate,
            ROLE_TARGET_A if candidate.feature_id == targets[0]
            else ROLE_TARGET_B,
            "second observation of target unit {}, giving the "
            "between-unit comparison a replicate rather than a single "
            "spot".format(candidate.feature_id),
        ))
        taken.add(candidate.point_id)

    if len(chosen) != config.PLANNED_SITE_COUNT:
        raise SiteError(
            "TOO_FEW_SITES",
            "only {} of {} sites could be placed under the {:.1f} m "
            "minimum separation. Map more units, add eligible objects, "
            "or lower the separation deliberately.".format(
                len(chosen), config.PLANNED_SITE_COUNT,
                MIN_SITE_SEPARATION_M,
            ),
        )

    units = {candidate.feature_id for candidate, _r, _why in chosen}

    if len(units) < 2:
        raise SiteError(
            "SINGLE_UNIT",
            "all four sites fall in one mapped unit ({}). Four "
            "measurements of the same unit cannot test a claim about the "
            "difference between units.".format(units.pop()),
        )

    predictions_by_feature = {}

    for prediction_id in plan.prediction_order:
        comparison = plan.predictions[prediction_id].comparison or {}

        for feature_id in comparison.get("feature_ids") or []:
            predictions_by_feature.setdefault(feature_id, []).append(
                prediction_id
            )

    sites = []

    for index, (candidate, role, reason) in enumerate(chosen):
        point = candidate.point
        feature = candidate.feature

        linked = sorted(set(
            predictions_by_feature.get(feature.feature_id, [])
        ))

        if not linked:
            linked = sorted(plan.prediction_order)

        sites.append(PlannedSite({
            "site_id": "SITE-{:02d}".format(index + 1),
            "source_point_id": point.point_id,
            "source_point_type": point.type,
            "coordinate_status": point.coordinate_status,
            "coordinate_frame": yard.frame.get("frame_id"),
            "x_m": point.x_m,
            "y_m": point.y_m,
            "h_m": point.h_m,
            "geological_feature_id": feature.feature_id,
            "geological_context": feature.name,
            "role": role,
            "scientific_purpose": reason,
            "hypothesis_id": plan.hypothesis.hypothesis_id,
            "prediction_ids": linked,
            "measurement_plan": {
                "repeats": repeats,
                "repeat_kind": "INDEPENDENT_REPOSITIONED",
                "note": (
                    "Repeats must be independent - lift and re-place the "
                    "sample between them. Re-reading one undisturbed "
                    "placement measures the sensor, not the material, and "
                    "would understate the within-site spread that every "
                    "between-site comparison is judged against."
                ),
            },
            "photograph_required": True,
            "selection_reason": "{}; {}".format(
                reason,
                "; ".join(
                    item["reason"] for item in candidate.reasons
                    if item["points"] > 0
                ),
            ),
            "selected_by": SELECTED_BY_PLANNER,
            "score_detail": candidate.reasons,
        }))

    return SitePlan(
        sites,
        candidates=[c.as_dict() for c in candidates],
        generated_at=generated_at,
    )


def bind_predictions(plan, site_plan):
    """
    Fill in each prediction's planned sites, and the hypothesis's.

    A prediction is written before the sites exist - it names the mapped
    UNITS it wants compared. Once the planner has placed sites on those
    units, this resolves unit references into site ids so the
    traceability chain H -> P -> Site is complete in both directions.

    Returns the list of predictions that ended up with no site, which is
    a real planning failure rather than a detail: a prediction nothing
    will visit cannot be tested.
    """
    sites_by_feature = {}

    for site in site_plan:
        sites_by_feature.setdefault(
            site.geological_feature_id, []
        ).append(site.site_id)

    unbound = []

    for prediction_id in plan.prediction_order:
        prediction = plan.predictions[prediction_id]
        wanted = (prediction.comparison or {}).get("feature_ids", [])

        bound = []

        for feature_id in wanted:
            bound.extend(sites_by_feature.get(feature_id, []))

        prediction.planned_site_ids = sorted(set(bound))

        if not prediction.planned_site_ids:
            unbound.append(prediction_id)

    # The reverse link: which sites the hypothesis is tested at.
    if plan.hypothesis is not None:
        linked = []

        for feature_id in plan.hypothesis.linked_feature_ids:
            linked.extend(sites_by_feature.get(feature_id, []))

        plan.hypothesis.linked_site_ids = sorted(set(linked))

    # And each site's own prediction list, now that binding is known.
    for site in site_plan:
        serving = sorted(
            prediction_id for prediction_id in plan.prediction_order
            if site.site_id
            in plan.predictions[prediction_id].planned_site_ids
        )

        if serving:
            site.prediction_ids = serving

    return unbound
