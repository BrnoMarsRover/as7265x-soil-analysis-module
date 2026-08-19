"""
Scientific Exploration Analysis — Phase 3.

The mission-level question. Not "what is this rock?" but "taken together,
do these observations support the claim we made before we set out?"

THE STATISTIC, AND WHY IT IS THIS ONE

The instrument has no reference library measured from this yard. DB1 is
23 laboratory materials; DB3 is 84 USGS spectra projected through a model
of our sensor. Neither can tell us that a yard surface *is* basalt, and
saying so would be the confident wrong answer this whole system exists to
avoid.

What the instrument can establish, honestly, is whether two places differ
by more than the same place differs from itself:

    between-site distance  ||  centroid(A) - centroid(B) ||
    within-site spread     mean distance of a site's own repeats
                           from their own centroid  (call it sigma)
    standard error         sigma_pooled * sqrt(1/nA + 1/nB)
    separation ratio       between distance / standard error

The denominator is the standard error of the centroid difference, NOT the
spread of individual repeats. That distinction is the whole correctness of
this module. A centroid of n repeats is roughly sqrt(n) times more precise
than one repeat, so dividing by the individual spread flatters every
comparison: two samples drawn from the *same* surface will sit about
sigma*sqrt(2/n) apart, which against a denominator of sigma looks like a
ratio near 1 and gets called "separated". An earlier version of this
module made exactly that error and reported two identical synthetic sites
as reproducibly different.

A ratio above the threshold means the two centroids are further apart than
sampling noise alone would place them. That is a claim about reproducible
difference, which is exactly what the hypothesis is about, and it needs no
reference library and no material identification.

WHAT IS DELIBERATELY NOT COMPUTED

No p-value. No confidence interval. No claim of statistical significance.
With three repeats per site, every test worth the name has assumptions
that cannot be checked, and a p-value computed anyway would be a decorated
guess. The ratio is reported with its inputs — n, the raw distances, the
spread — so a reader can see precisely how thin the evidence is.

The separation threshold is labelled PROVISIONAL_UNVALIDATED, in the same
sense and for the same reason as every other threshold in this codebase.

Layer rule: Science may import BD, Measurements and DecisionModel.
"""

import math

from Science import config
from Science.plan import SUPPORTED, REJECTED, INCONCLUSIVE, NOT_EVALUATED

from Measurements import distances as distance_module

# One distance per independent metric family. All three are non-negative
# distances where zero means identical, so "between over within" means
# the same *kind* of thing in each. They are never pooled across
# families - the units are incomparable and averaging them would
# reintroduce exactly the shape-over-magnitude bias the family split
# exists to prevent.
FAMILY_MAGNITUDE = "magnitude"
FAMILY_ANGULAR = "angular"
FAMILY_CENTERED = "centered_shape"

FAMILIES = (FAMILY_MAGNITUDE, FAMILY_ANGULAR, FAMILY_CENTERED)

# How many standard errors apart two centroids must be before the
# separation counts as more than sampling noise.
#
# Two is the conventional descriptive heuristic, and that is all it is
# claimed to be. It is NOT a significance level: no distributional
# assumption is made, no p-value follows from it, and with three repeats
# per site none could be checked. It has never been validated against
# repeated measurements of surfaces known to be identical, so it carries
# the same label as every other unvalidated threshold in this codebase.
SEPARATION_RATIO_THRESHOLD = 2.0
THRESHOLD_STATUS = "PROVISIONAL_UNVALIDATED"

# How many of the three families must agree before a separation counts as
# reproduced. Two of three is the plan's own criterion.
MIN_FAMILIES_AGREEING = 2

ANALYSIS_VERSION = config.ANALYSIS_VERSION


class AnalysisError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def family_distance(family, first, second, channels=None):
    """
    Distance between two spectra, in one metric family's terms.

    Returns None when the metric is undefined for these vectors - a
    constant spectrum has no Pearson correlation, a zero vector has no
    angle - rather than substituting a number.
    """
    metrics = distance_module.all_metrics(first, second, channels)

    if family == FAMILY_MAGNITUDE:
        return metrics.get("rmse")

    if family == FAMILY_ANGULAR:
        return metrics.get("spectral_angle_deg")

    if family == FAMILY_CENTERED:
        r = metrics.get("pearson_r")

        return None if r is None else 1.0 - r

    raise AnalysisError(
        "UNKNOWN_FAMILY", "{} is not a metric family".format(family)
    )


def centroid(spectra, channels=None):
    """Channel-wise mean of several spectra."""
    if not spectra:
        return {}

    channels = channels or sorted(
        set().union(*[set(s) for s in spectra])
    )

    mean = {}

    for channel in channels:
        values = [
            s[channel] for s in spectra
            if channel in s and isinstance(s[channel], (int, float))
            and not isinstance(s[channel], bool)
        ]

        if values:
            mean[channel] = sum(values) / len(values)

    return mean


class MeasurementRecord:
    """
    One measurement as this layer needs it.

    A deliberately small contract so the analysis can run on real
    EvidencePackages and on synthetic fixtures without either having to
    imitate the other.
    """

    def __init__(self, entry):
        self.measurement_id = entry["measurement_id"]
        self.site_id = entry.get("site_id")
        self.sample_id = entry.get("sample_id")
        self.spectrum = dict(entry.get("spectrum") or {})

        # PASS / WARNING / HARDWARE_QC_FAIL, as Measurements/quality.py
        # splits them. Normalization problems are tracked separately
        # because a normalization warning does not invalidate raw counts.
        self.hardware_quality = entry.get("hardware_quality", "PASS")
        self.normalization_quality = entry.get("normalization_quality", "OK")

        self.decision = entry.get("decision")
        self.calibration_id = entry.get("calibration_id")
        self.at = entry.get("at")
        self.independent = entry.get("independent", True)
        self.synthetic = entry.get("synthetic", False)

    @property
    def usable(self):
        """
        Whether this measurement may contribute to a site aggregate.

        Hardware failure disqualifies; a normalization warning does not.
        That split is load-bearing: in the operator's own session six of
        twelve measurements were suppressed entirely because reflectance
        exceeded 1.0, when the raw counts were fine and only the
        reference division was ill-conditioned.
        """
        return self.hardware_quality != "HARDWARE_QC_FAIL"

    def as_dict(self):
        return {
            "measurement_id": self.measurement_id,
            "site_id": self.site_id,
            "sample_id": self.sample_id,
            "hardware_quality": self.hardware_quality,
            "normalization_quality": self.normalization_quality,
            "calibration_id": self.calibration_id,
            "at": self.at,
            "independent": self.independent,
            "usable": self.usable,
            "synthetic": self.synthetic,
        }


class SiteAggregate:
    """The measurements at one site, and how much they disagree."""

    def __init__(self, site_id, records, channels=None):
        self.site_id = site_id
        self.records = list(records)
        self.channels = channels

        self.usable = [r for r in self.records if r.usable]
        self.excluded = [r for r in self.records if not r.usable]

        self.n = len(self.usable)
        self.n_independent = len(
            [r for r in self.usable if r.independent]
        )

        self.centroid = centroid(
            [r.spectrum for r in self.usable], channels
        )

        # Within-site spread, per family: the mean distance from each
        # repeat to the site's own centroid.
        self.within = {}
        self.within_detail = {}

        for family in FAMILIES:
            gaps = []

            for record in self.usable:
                gap = family_distance(
                    family, record.spectrum, self.centroid, channels
                )

                if gap is not None:
                    gaps.append((record.measurement_id, gap))

            self.within_detail[family] = gaps

            # One measurement has no spread. Reporting zero would claim
            # perfect repeatability from a single reading.
            self.within[family] = (
                sum(g for _mid, g in gaps) / len(gaps)
                if len(gaps) >= config.MIN_REPEATS_FOR_SPREAD else None
            )

    @property
    def has_spread(self):
        return any(value is not None for value in self.within.values())

    def limitations(self):
        notes = []

        if self.n == 0:
            notes.append(
                "{}: no usable measurement".format(self.site_id)
            )

        elif self.n < config.MIN_REPEATS_FOR_SPREAD:
            notes.append(
                "{}: {} usable measurement, so within-site spread cannot "
                "be measured at all".format(self.site_id, self.n)
            )

        elif self.n < config.DEFAULT_REPEATS_PER_SAMPLE:
            notes.append(
                "{}: {} usable measurements, fewer than the {} planned; "
                "the spread estimate rests on very few "
                "repeats".format(
                    self.site_id, self.n,
                    config.DEFAULT_REPEATS_PER_SAMPLE,
                )
            )

        if self.n_independent < self.n:
            notes.append(
                "{}: {} of {} repeats are not independent "
                "repositionings, so the spread understates real "
                "variability".format(
                    self.site_id, self.n - self.n_independent, self.n
                )
            )

        if self.excluded:
            notes.append(
                "{}: {} measurement(s) excluded for hardware quality "
                "failure ({})".format(
                    self.site_id, len(self.excluded),
                    ", ".join(r.measurement_id for r in self.excluded),
                )
            )

        degraded = [
            r.measurement_id for r in self.usable
            if r.normalization_quality != "OK"
        ]

        if degraded:
            notes.append(
                "{}: {} measurement(s) carry a normalization warning; raw "
                "counts are valid but the reference division is poorly "
                "conditioned ({})".format(
                    self.site_id, len(degraded), ", ".join(degraded)
                )
            )

        return notes

    def as_dict(self):
        return {
            "site_id": self.site_id,
            "n_usable": self.n,
            "n_independent": self.n_independent,
            "n_excluded": len(self.excluded),
            "measurement_ids": [r.measurement_id for r in self.usable],
            "excluded_measurement_ids": [
                r.measurement_id for r in self.excluded
            ],
            "centroid": self.centroid,
            "within_site_spread": self.within,
            "within_site_detail": {
                family: [
                    {"measurement_id": mid, "distance": gap}
                    for mid, gap in gaps
                ]
                for family, gaps in self.within_detail.items()
            },
            "limitations": self.limitations(),
        }

    def __repr__(self):
        return "<SiteAggregate {} n={}>".format(self.site_id, self.n)


class SiteComparison:
    """Between two sites: are they further apart than they are wide?"""

    def __init__(self, first, second, channels=None):
        self.first = first
        self.second = second
        self.pair = (first.site_id, second.site_id)

        self.between = {}
        self.pooled_within = {}
        self.standard_error = {}
        self.ratio = {}
        self.separated = {}

        for family in FAMILIES:
            gap = (
                family_distance(
                    family, first.centroid, second.centroid, channels
                )
                if first.centroid and second.centroid else None
            )

            self.between[family] = gap

            a = first.within.get(family)
            b = second.within.get(family)

            if a is None or b is None or first.n < 1 or second.n < 1:
                self.pooled_within[family] = None
                self.standard_error[family] = None
                self.ratio[family] = None
                self.separated[family] = None
                continue

            pooled = (a + b) / 2.0
            self.pooled_within[family] = pooled

            # The precision of the DIFFERENCE between two centroids, not
            # the spread of one site's repeats. See the module docstring:
            # using the spread here reports identical sites as different.
            error = pooled * math.sqrt(1.0 / first.n + 1.0 / second.n)
            self.standard_error[family] = error

            if gap is None:
                self.ratio[family] = None
                self.separated[family] = None

            elif error <= 0.0:
                # Zero spread across real repeats means the repeats were
                # not independent acquisitions. A ratio of infinity is a
                # symptom, not evidence.
                self.ratio[family] = None
                self.separated[family] = None

            else:
                ratio = gap / error
                self.ratio[family] = ratio
                self.separated[family] = (
                    ratio >= SEPARATION_RATIO_THRESHOLD
                )

    @property
    def families_agreeing(self):
        return [
            family for family in FAMILIES
            if self.separated.get(family) is True
        ]

    @property
    def families_disagreeing(self):
        return [
            family for family in FAMILIES
            if self.separated.get(family) is False
        ]

    @property
    def families_undetermined(self):
        return [
            family for family in FAMILIES
            if self.separated.get(family) is None
        ]

    @property
    def reproducibly_separated(self):
        """
        True, False, or None for "cannot tell".

        None is a real answer here and is passed through rather than
        collapsed into False.
        """
        if len(self.families_undetermined) == len(FAMILIES):
            return None

        if len(self.families_agreeing) >= MIN_FAMILIES_AGREEING:
            return True

        if len(self.families_disagreeing) >= MIN_FAMILIES_AGREEING:
            return False

        return None

    def as_dict(self):
        return {
            "pair": list(self.pair),
            "between_site_distance": self.between,
            "pooled_within_site_spread": self.pooled_within,
            "standard_error_of_difference": self.standard_error,
            "separation_ratio": self.ratio,
            "separated_by_family": self.separated,
            "families_agreeing": self.families_agreeing,
            "families_disagreeing": self.families_disagreeing,
            "families_undetermined": self.families_undetermined,
            "reproducibly_separated": self.reproducibly_separated,
            "threshold": SEPARATION_RATIO_THRESHOLD,
            "threshold_status": THRESHOLD_STATUS,
            "n": {
                self.first.site_id: self.first.n,
                self.second.site_id: self.second.n,
            },
            "statistical_note": (
                "separation_ratio is the centroid separation expressed "
                "in standard errors of that separation. It is "
                "descriptive, not inferential: no p-value is computed, "
                "because with {} and {} repeats the assumptions of any "
                "such test cannot be checked.".format(
                    self.first.n, self.second.n
                )
            ),
        }

    def __repr__(self):
        return "<Comparison {} vs {}>".format(*self.pair)


class PredictionVerdict:
    """One prediction, resolved against the evidence."""

    def __init__(self, prediction_id, outcome, rationale, evidence=None,
                 supporting=None, contradicting=None, limitations=None,
                 comparisons=None):
        self.prediction_id = prediction_id
        self.outcome = outcome
        self.rationale = rationale
        self.evidence = list(evidence or [])
        self.supporting = list(supporting or [])
        self.contradicting = list(contradicting or [])
        self.limitations = list(limitations or [])
        self.comparisons = list(comparisons or [])

    def as_dict(self):
        return {
            "prediction_id": self.prediction_id,
            "outcome": self.outcome,
            "rationale": self.rationale,
            "evidence": self.evidence,
            "supporting_measurements": self.supporting,
            "contradicting_measurements": self.contradicting,
            "limitations": self.limitations,
            "comparisons": self.comparisons,
        }

    def __repr__(self):
        return "<{} {}>".format(self.prediction_id, self.outcome)


class ExplorationAnalysis:
    """The whole mission-level analysis."""

    def __init__(self, plan, site_plan, run, measurements, channels=None,
                 generated_at=None):
        self.plan = plan
        self.site_plan = site_plan
        self.run = run
        self.channels = channels
        self.generated_at = generated_at
        self.analysis_version = ANALYSIS_VERSION

        self.records = [
            record if isinstance(record, MeasurementRecord)
            else MeasurementRecord(record)
            for record in measurements
        ]

        # --- site aggregates -----------------------------------------
        by_site = {}

        for record in self.records:
            by_site.setdefault(record.site_id, []).append(record)

        self.aggregates = {}

        for site in site_plan:
            self.aggregates[site.site_id] = SiteAggregate(
                site.site_id, by_site.get(site.site_id, []), channels
            )

        # --- comparisons ---------------------------------------------
        self.comparisons = {}
        site_ids = [site.site_id for site in site_plan]

        for index, first_id in enumerate(site_ids):
            for second_id in site_ids[index + 1:]:
                comparison = SiteComparison(
                    self.aggregates[first_id],
                    self.aggregates[second_id],
                    channels,
                )
                self.comparisons[(first_id, second_id)] = comparison

        self.prediction_verdicts = {}
        self.hypothesis_outcome = NOT_EVALUATED
        self.hypothesis_rationale = None
        self.hypothesis_limitations = []
        self.integrity_problems = []

    # ------------------------------------------------------------------
    # lookups
    # ------------------------------------------------------------------

    def sites_for_feature(self, feature_id):
        return [
            site.site_id for site in self.site_plan
            if site.geological_feature_id == feature_id
        ]

    def comparison(self, first_id, second_id):
        key = (first_id, second_id)

        if key in self.comparisons:
            return self.comparisons[key]

        return self.comparisons.get((second_id, first_id))

    # ------------------------------------------------------------------
    # prediction evaluation
    # ------------------------------------------------------------------

    def _evaluate_between_site(self, prediction):
        """A BETWEEN_SITE_VS_WITHIN_SITE prediction."""
        features = (prediction.comparison or {}).get("feature_ids") or []

        if len(features) < 2:
            return PredictionVerdict(
                prediction.prediction_id, INCONCLUSIVE,
                "the prediction names fewer than two units to compare",
                limitations=["comparison under-specified in the plan"],
            )

        first_sites = self.sites_for_feature(features[0])
        second_sites = self.sites_for_feature(features[1])

        if not first_sites or not second_sites:
            return PredictionVerdict(
                prediction.prediction_id, INCONCLUSIVE,
                "no site was placed in {}".format(
                    features[0] if not first_sites else features[1]
                ),
                limitations=[
                    "a unit named by the prediction was never visited"
                ],
            )

        results = []
        limitations = []
        evidence = []
        supporting = []
        contradicting = []

        for first_id in first_sites:
            for second_id in second_sites:
                comparison = self.comparison(first_id, second_id)

                if comparison is None:
                    continue

                results.append(comparison)
                evidence.append("{}|{}".format(first_id, second_id))

                target = (
                    supporting if comparison.reproducibly_separated
                    else contradicting
                )

                for aggregate in (comparison.first, comparison.second):
                    for record in aggregate.usable:
                        if record.measurement_id not in target:
                            target.append(record.measurement_id)

                limitations.extend(comparison.first.limitations())
                limitations.extend(comparison.second.limitations())

        if not results:
            return PredictionVerdict(
                prediction.prediction_id, INCONCLUSIVE,
                "no comparison could be formed between the named units",
                limitations=["no usable site pair"],
            )

        separated = [c for c in results if c.reproducibly_separated is True]
        overlapping = [
            c for c in results if c.reproducibly_separated is False
        ]
        undetermined = [
            c for c in results if c.reproducibly_separated is None
        ]

        comparison_records = [c.as_dict() for c in results]

        # Undetermined dominates: if the evidence cannot decide, the
        # honest answer is that it cannot decide.
        if undetermined and not separated and not overlapping:
            return PredictionVerdict(
                prediction.prediction_id, INCONCLUSIVE,
                "the separation ratio could not be computed for any site "
                "pair: within-site spread is unavailable, which usually "
                "means too few usable repeats",
                evidence=evidence,
                limitations=sorted(set(limitations)),
                comparisons=comparison_records,
            )

        if separated and not overlapping:
            # Report the pair whose WEAKEST family is strongest, so
            # the sentence quotes the least flattering number that
            # still supports the claim.
            best = max(
                separated,
                key=lambda c: min(
                    (v for v in c.ratio.values() if v is not None),
                    default=0.0,
                ),
            )

            return PredictionVerdict(
                prediction.prediction_id, SUPPORTED,
                "every usable site pair separates: {} of 3 metric "
                "families agree for {} vs {}, at {} standard errors of "
                "separation".format(
                    len(best.families_agreeing), best.pair[0], best.pair[1],
                    ", ".join(
                        "{}={:.2f}".format(family, value)
                        for family, value in sorted(best.ratio.items())
                        if value is not None
                    ),
                ),
                evidence=evidence,
                supporting=supporting,
                contradicting=contradicting,
                limitations=sorted(set(limitations)),
                comparisons=comparison_records,
            )

        if overlapping and not separated:
            return PredictionVerdict(
                prediction.prediction_id, REJECTED,
                "the site populations overlap within their own spread: "
                "the between-site distance does not exceed the "
                "within-site spread on a majority of metric families",
                evidence=evidence,
                supporting=supporting,
                contradicting=contradicting,
                limitations=sorted(set(limitations)),
                comparisons=comparison_records,
            )

        return PredictionVerdict(
            prediction.prediction_id, INCONCLUSIVE,
            "site pairs disagree: {} separate, {} overlap. A prediction "
            "cannot be called supported while some of the pairs it rests "
            "on contradict it.".format(len(separated), len(overlapping)),
            evidence=evidence,
            supporting=supporting,
            contradicting=contradicting,
            limitations=sorted(set(limitations)),
            comparisons=comparison_records,
        )

    def _evaluate_control(self, prediction):
        """A WITHIN_SITE_SPREAD_COMPARISON prediction."""
        features = (prediction.comparison or {}).get("feature_ids") or []
        control_sites = []

        for feature_id in features:
            control_sites.extend(self.sites_for_feature(feature_id))

        if not control_sites:
            return PredictionVerdict(
                prediction.prediction_id, INCONCLUSIVE,
                "no control site was placed in {}".format(
                    ", ".join(features) or "the named unit"
                ),
                limitations=["the control unit was never visited"],
            )

        controls = [self.aggregates[s] for s in control_sites]
        others = [
            aggregate for site_id, aggregate in self.aggregates.items()
            if site_id not in control_sites
        ]

        usable_controls = [c for c in controls if c.has_spread]

        if not usable_controls:
            return PredictionVerdict(
                prediction.prediction_id, INCONCLUSIVE,
                "the control site yielded no measurable within-site "
                "spread, so it cannot certify that a between-site "
                "difference is a property of the material",
                evidence=control_sites,
                limitations=[
                    note for c in controls for note in c.limitations()
                ],
            )

        limitations = [
            note for c in controls for note in c.limitations()
        ]
        comparable = []
        wider = []

        for family in FAMILIES:
            control_values = [
                c.within[family] for c in usable_controls
                if c.within.get(family) is not None
            ]
            other_values = [
                o.within[family] for o in others
                if o.within.get(family) is not None
            ]

            if not control_values or not other_values:
                continue

            control_mean = sum(control_values) / len(control_values)
            other_mean = sum(other_values) / len(other_values)

            # "Comparable" means the control is not markedly wider than
            # the targets. Twice as wide is the point at which a claimed
            # separation could plausibly be session variation.
            if control_mean <= 2.0 * other_mean:
                comparable.append(family)

            else:
                wider.append((family, control_mean, other_mean))

        if not comparable and not wider:
            return PredictionVerdict(
                prediction.prediction_id, INCONCLUSIVE,
                "no family produced a comparable spread at both the "
                "control and the target sites",
                evidence=control_sites,
                limitations=sorted(set(limitations)),
            )

        for family, control_mean, other_mean in wider:
            limitations.append(
                "the control's within-site spread is {:.1f}x the targets' "
                "in the {} family. That family's distance depends on the "
                "spectrum's own variance, so a flat-spectrum control "
                "inflates it without implying session instability."
                .format(
                    control_mean / other_mean if other_mean else float("inf"),
                    family,
                )
            )

        # A majority of families, not unanimity. One family disagreeing is
        # recorded as a limitation rather than treated as a failed
        # control: 1 - Pearson r scales with the spectrum's own variance,
        # so a control on a near-flat surface widens in that family alone
        # for reasons that have nothing to do with the session.
        if len(comparable) >= MIN_FAMILIES_AGREEING:
            return PredictionVerdict(
                prediction.prediction_id, SUPPORTED,
                "the control's within-site spread is comparable to the "
                "target sites' on {} of {} metric families, so a "
                "between-site separation is not explained by session-wide "
                "instability".format(len(comparable), len(FAMILIES)),
                evidence=control_sites,
                supporting=[
                    r.measurement_id for c in usable_controls
                    for r in c.usable
                ],
                limitations=sorted(set(limitations)),
            )

        return PredictionVerdict(
            prediction.prediction_id, REJECTED,
            "the control's within-site spread is markedly larger than the "
            "target sites' on {} of {} metric families, so a between-site "
            "separation of the observed size cannot be distinguished from "
            "session variation".format(len(wider), len(FAMILIES)),
            evidence=control_sites,
            contradicting=[
                r.measurement_id for c in usable_controls
                for r in c.usable
            ],
            limitations=sorted(set(limitations)),
        )

    def _evaluate_qualitative(self, prediction):
        """
        A prediction resolved by observation, not by arithmetic.

        CONTACT_GEOMETRY is the case in the shipped plan: whether a
        boundary cuts or grades is something a person reads off a
        photograph. The software confirms the evidence exists and refuses
        to invent the reading.
        """
        site_ids = list(prediction.planned_site_ids)
        observations = []
        photos = []

        for site_id in site_ids:
            if site_id not in self.run.visits:
                continue

            for observation in self.run.observations_at(site_id):
                observations.append(observation.observation_id)
                photos.extend(observation.photo_ids)

        if not observations:
            return PredictionVerdict(
                prediction.prediction_id, INCONCLUSIVE,
                "no observation was recorded at the sites this prediction "
                "depends on",
                limitations=[
                    "the contact was not observed, so neither the support "
                    "nor the reject criterion can be applied"
                ],
            )

        if not photos:
            return PredictionVerdict(
                prediction.prediction_id, INCONCLUSIVE,
                "observations exist but none carries a photograph, and "
                "this prediction is resolved by reading a boundary in an "
                "image",
                evidence=observations,
                limitations=["no photograph of the contact"],
            )

        # The evidence is present. A human must read it.
        return PredictionVerdict(
            prediction.prediction_id, INCONCLUSIVE,
            "evidence is complete ({} observation(s), {} photograph(s)) "
            "but the criterion is qualitative: whether the contact cuts "
            "or grades must be read from the imagery by a person. The "
            "software will not guess it.".format(
                len(observations), len(photos)
            ),
            evidence=observations + photos,
            limitations=[
                "awaiting human reading of the contact geometry; this is "
                "a MANUAL_REVIEW item, not a data gap"
            ],
        )

    def evaluate_predictions(self):
        """Resolve every prediction against the evidence."""
        from Science.sites import (
            COMPARISON_TARGET, COMPARISON_CONTROL, COMPARISON_CONTACT,
        )

        self.prediction_verdicts = {}

        for prediction_id in self.plan.prediction_order:
            prediction = self.plan.predictions[prediction_id]
            kind = (prediction.comparison or {}).get("kind")

            if kind == COMPARISON_TARGET:
                verdict = self._evaluate_between_site(prediction)

            elif kind == COMPARISON_CONTROL:
                verdict = self._evaluate_control(prediction)

            elif kind == COMPARISON_CONTACT:
                verdict = self._evaluate_qualitative(prediction)

            else:
                verdict = PredictionVerdict(
                    prediction_id, INCONCLUSIVE,
                    "the prediction declares comparison kind {!r}, which "
                    "this analysis has no rule for. It is reported "
                    "unresolved rather than guessed.".format(kind),
                    limitations=["unknown comparison kind"],
                )

            self.prediction_verdicts[prediction_id] = verdict

        return self.prediction_verdicts

    # ------------------------------------------------------------------
    # hypothesis evaluation
    # ------------------------------------------------------------------

    def evaluate_hypothesis(self):
        """
        Combine the prediction verdicts into one outcome.

        Refuses to run at all if the frozen hypothesis has been edited,
        because a verdict against edited text is not a test of the claim
        that was made.
        """
        hypothesis = self.plan.hypothesis

        if hypothesis is None:
            raise AnalysisError(
                "NO_HYPOTHESIS", "the plan states no hypothesis"
            )

        unchanged, detail = hypothesis.verify_unchanged()

        if not unchanged:
            self.integrity_problems.append(detail)
            self.hypothesis_outcome = INCONCLUSIVE
            self.hypothesis_rationale = (
                "REFUSED: {} No verdict is published against a hypothesis "
                "that changed after it was frozen.".format(detail)
            )

            return self.hypothesis_outcome

        if not self.prediction_verdicts:
            self.evaluate_predictions()

        outcomes = [
            v.outcome for v in self.prediction_verdicts.values()
        ]

        supported = outcomes.count(SUPPORTED)
        rejected = outcomes.count(REJECTED)
        inconclusive = outcomes.count(INCONCLUSIVE)

        # The prediction that carries the hypothesis is the one that
        # compares the units the hypothesis names. A control prediction
        # failing does not reject the hypothesis - it removes the ground
        # a support verdict would have stood on.
        from Science.sites import COMPARISON_TARGET, COMPARISON_CONTROL

        primary = []
        controls = []

        for prediction_id, verdict in self.prediction_verdicts.items():
            kind = (
                self.plan.predictions[prediction_id].comparison or {}
            ).get("kind")

            if kind == COMPARISON_TARGET:
                primary.append(verdict)

            elif kind == COMPARISON_CONTROL:
                controls.append(verdict)

        self.hypothesis_limitations = sorted(set(
            note
            for verdict in self.prediction_verdicts.values()
            for note in verdict.limitations
        ))

        if not primary:
            self.hypothesis_outcome = INCONCLUSIVE
            self.hypothesis_rationale = (
                "no prediction tests the units the hypothesis names"
            )

            return self.hypothesis_outcome

        primary_outcomes = [v.outcome for v in primary]

        if all(o == REJECTED for o in primary_outcomes):
            self.hypothesis_outcome = REJECTED
            self.hypothesis_rationale = (
                "every prediction that compares the units named by the "
                "hypothesis was rejected: the populations overlap within "
                "their own spread. {} supported, {} rejected, {} "
                "inconclusive across all predictions.".format(
                    supported, rejected, inconclusive
                )
            )

            return self.hypothesis_outcome

        if all(o == SUPPORTED for o in primary_outcomes):
            failed_controls = [
                v for v in controls if v.outcome == REJECTED
            ]

            if failed_controls:
                self.hypothesis_outcome = INCONCLUSIVE
                self.hypothesis_rationale = (
                    "the target units separate, but the control failed: "
                    "{} The separation therefore cannot be attributed to "
                    "the material rather than to the session.".format(
                        failed_controls[0].rationale
                    )
                )

                return self.hypothesis_outcome

            unresolved_controls = [
                v for v in controls if v.outcome == INCONCLUSIVE
            ]

            if unresolved_controls:
                self.hypothesis_outcome = INCONCLUSIVE
                self.hypothesis_rationale = (
                    "the target units separate, but the control could not "
                    "be resolved: {} Without it, session variation has not "
                    "been excluded.".format(
                        unresolved_controls[0].rationale
                    )
                )

                return self.hypothesis_outcome

            self.hypothesis_outcome = SUPPORTED
            self.hypothesis_rationale = (
                "every prediction comparing the units named by the "
                "hypothesis was supported, and the control confirmed that "
                "the separation exceeds session variation. {} supported, "
                "{} rejected, {} inconclusive across all "
                "predictions.".format(supported, rejected, inconclusive)
            )

            return self.hypothesis_outcome

        self.hypothesis_outcome = INCONCLUSIVE
        self.hypothesis_rationale = (
            "the predictions that test the hypothesis do not agree: {}. "
            "The data does not settle the claim either way.".format(
                ", ".join(
                    "{} {}".format(v.prediction_id, v.outcome)
                    for v in primary
                )
            )
        )

        return self.hypothesis_outcome

    def what_would_resolve_it(self):
        """
        For an INCONCLUSIVE outcome, what would actually settle it.

        The rules ask for this explicitly: "although in this case you
        should explain what else would be needed to test it".
        """
        if self.hypothesis_outcome != INCONCLUSIVE:
            return []

        needed = []

        for site_id, aggregate in sorted(self.aggregates.items()):
            if aggregate.n < config.MIN_REPEATS_FOR_SPREAD:
                needed.append(
                    "at least {} independent repositioned measurements at "
                    "{} (currently {}), so that within-site spread "
                    "exists".format(
                        config.MIN_REPEATS_FOR_SPREAD, site_id, aggregate.n
                    )
                )

            elif aggregate.n < config.DEFAULT_REPEATS_PER_SAMPLE:
                needed.append(
                    "more repeats at {} ({} of {} planned) to stabilise "
                    "the spread estimate".format(
                        site_id, aggregate.n,
                        config.DEFAULT_REPEATS_PER_SAMPLE,
                    )
                )

            if aggregate.excluded:
                needed.append(
                    "re-measurement at {} of the {} acquisition(s) that "
                    "failed hardware quality control".format(
                        site_id, len(aggregate.excluded)
                    )
                )

        for verdict in self.prediction_verdicts.values():
            if verdict.outcome != INCONCLUSIVE:
                continue

            if "qualitative" in verdict.rationale:
                needed.append(
                    "a human reading of the contact geometry for {}; the "
                    "imagery exists".format(verdict.prediction_id)
                )

        if not needed:
            needed.append(
                "the metric families disagree with adequate data, which "
                "points to a real but weak difference; more sites in each "
                "unit would show whether it is systematic"
            )

        return sorted(set(needed))

    # ------------------------------------------------------------------
    # evidence selection
    # ------------------------------------------------------------------

    def rank_evidence(self):
        """
        Rank comparisons by how well they are supported.

        Deliberately not "by how dramatic the result is". Ranking on
        effect size alone selects the luckiest pair, which is how a weak
        study produces a confident figure. Repeat count and quality come
        first; the size of the difference is the last term.
        """
        ranked = []

        for comparison in self.comparisons.values():
            first, second = comparison.first, comparison.second

            n_score = min(first.n, second.n)
            quality_penalty = len(first.excluded) + len(second.excluded)
            degraded = len([
                r for aggregate in (first, second)
                for r in aggregate.usable
                if r.normalization_quality != "OK"
            ])

            ratios = [
                value for value in comparison.ratio.values()
                if value is not None
            ]
            # The WEAKEST family, not the strongest. Taking the max
            # would rank by whichever metric happened to flatter this
            # pair, which is pooling across families through the back
            # door and is how a weak study picks its best-looking
            # figure.
            effect = min(ratios) if ratios else 0.0

            ranked.append({
                "pair": list(comparison.pair),
                "min_repeats": n_score,
                "excluded_measurements": quality_penalty,
                "normalization_warnings": degraded,
                "families_agreeing": len(comparison.families_agreeing),
                "weakest_separation_ratio": effect,
                "reproducibly_separated":
                    comparison.reproducibly_separated,
                "rank_key": (
                    n_score,
                    -quality_penalty,
                    -degraded,
                    len(comparison.families_agreeing),
                    effect,
                ),
            })

        ranked.sort(key=lambda item: item["rank_key"], reverse=True)

        for item in ranked:
            item.pop("rank_key")

        return ranked

    def contrary_evidence(self):
        """
        Everything that argues against the headline outcome.

        Kept as a first-class output so that dropping it from the report
        is a visible act rather than an omission.
        """
        contrary = []

        for prediction_id, verdict in sorted(
            self.prediction_verdicts.items()
        ):
            if (
                self.hypothesis_outcome == SUPPORTED
                and verdict.outcome in (REJECTED, INCONCLUSIVE)
            ):
                contrary.append({
                    "prediction_id": prediction_id,
                    "outcome": verdict.outcome,
                    "rationale": verdict.rationale,
                })

            elif (
                self.hypothesis_outcome == REJECTED
                and verdict.outcome == SUPPORTED
            ):
                contrary.append({
                    "prediction_id": prediction_id,
                    "outcome": verdict.outcome,
                    "rationale": verdict.rationale,
                })

        for comparison in self.comparisons.values():
            if comparison.families_disagreeing and (
                comparison.families_agreeing
            ):
                contrary.append({
                    "pair": list(comparison.pair),
                    "outcome": "FAMILY_DISAGREEMENT",
                    "rationale": (
                        "families {} report a separation while {} do "
                        "not".format(
                            ", ".join(comparison.families_agreeing),
                            ", ".join(comparison.families_disagreeing),
                        )
                    ),
                })

        return contrary

    # ------------------------------------------------------------------
    # output
    # ------------------------------------------------------------------

    def as_dict(self):
        return {
            "analysis_version": self.analysis_version,
            "generated_at": self.generated_at,
            "science_run_id": self.run.science_run_id,
            "plan_id": self.plan.plan_id,
            "hypothesis_id": (
                self.plan.hypothesis.hypothesis_id
                if self.plan.hypothesis else None
            ),
            "hypothesis_hash": (
                self.plan.hypothesis.content_hash
                if self.plan.hypothesis else None
            ),
            "integrity_problems": self.integrity_problems,
            "site_aggregates": {
                site_id: aggregate.as_dict()
                for site_id, aggregate in sorted(self.aggregates.items())
            },
            "site_comparisons": [
                comparison.as_dict()
                for comparison in self.comparisons.values()
            ],
            "prediction_verdicts": {
                prediction_id: verdict.as_dict()
                for prediction_id, verdict in sorted(
                    self.prediction_verdicts.items()
                )
            },
            "hypothesis": {
                "outcome": self.hypothesis_outcome,
                "rationale": self.hypothesis_rationale,
                "limitations": self.hypothesis_limitations,
                "what_would_resolve_it": self.what_would_resolve_it(),
            },
            "evidence_ranking": self.rank_evidence(),
            "contrary_evidence": self.contrary_evidence(),
            "method": {
                "statistic": "between-site distance / pooled within-site "
                             "spread",
                "families": list(FAMILIES),
                "threshold": SEPARATION_RATIO_THRESHOLD,
                "threshold_status": THRESHOLD_STATUS,
                "min_families_agreeing": MIN_FAMILIES_AGREEING,
                "significance_testing": (
                    "NOT PERFORMED. No p-value or confidence interval is "
                    "computed. Repeat counts are too small for the "
                    "assumptions of any such test to be checked, and a "
                    "number computed anyway would misrepresent the "
                    "strength of the evidence."
                ),
                "material_identification": (
                    "NOT CLAIMED from site spectra. No reference library "
                    "available to this instrument contains material "
                    "measured from this yard, so a similarity score "
                    "against DB1 or DB3 is not evidence of identity."
                ),
            },
        }


def analyse(plan, site_plan, run, measurements, channels=None,
            generated_at=None):
    """Build and run the full mission analysis."""
    analysis = ExplorationAnalysis(
        plan, site_plan, run, measurements, channels, generated_at
    )
    analysis.evaluate_predictions()
    analysis.evaluate_hypothesis()

    return analysis
