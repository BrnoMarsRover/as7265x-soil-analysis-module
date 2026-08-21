"""
The operator session: everything the screens need, in one object.

Holds the link to the ESP32, the persistent stores and the loaded
science layer, and answers "what is the state of the world right now".

WHAT IT OWNS AND WHAT IT DOES NOT

It owns orchestration: which calibration is active, which databases
loaded, when to ask Science a question, and when to write to BD. It
owns no science - every number comes from Science - and no hardware -
every hardware fact comes from get_status.

EVERY DEPENDENCY IS OPTIONAL EXCEPT THE ARCHIVE

A missing learning history, an empty DB2 or an absent class snapshot
each disable one thing and are recorded rather than raised. An
instrument that refuses to measure because an auxiliary database is
unavailable is a worse instrument. The archive is the exception: with
nowhere to persist RAW there is no safe way to acquire it.
"""

from BD import config as bd_config
from BD.acquisition_profiles import AcquisitionProfileStore, from_measurement
from BD.calibrations import CalibrationError, CalibrationStore
from BD.channels import (
    AS7265X_18,
    AS7265X_54_MULTIILLUM,
    FeatureSpaceError,
    ILLUMINATIONS,
    combine_illuminations,
)
from BD.databases import DatabaseError, MaterialDatabase, References
from BD.decision_learning import DecisionLearningStore, LearningError
from BD.registry import DatabaseRegistry
from BD.samples import (
    ACQUISITION_FAILED,
    ACQUISITION_SUCCESS,
    STATE_EMPTY,
    STATE_LOADED,
    STATE_MEASURED,
    STATE_READY_TO_LOAD,
    SampleStore,
    StorageError,
)

from Science import class_models
from Science import config as science_config
from Science import pipeline, quality
from Science.decision import DecisionEngine
from Science.taxonomy import Taxonomy

from serial_link import LinkError

SLOT_COUNT = 4

# How many DIFFERENT prepared fractions of one material it takes before
# "how much of it is there" is a question worth asking of the data.
#
# Three points define a slope and a curvature; two define a line through
# two points, which fits perfectly and predicts nothing. This is a floor
# on what to attempt, not a promise that three is enough - it is
# PROVISIONAL, like every other threshold in the decision layer, and the
# evaluation in research/training/evaluate_mixtures.py is what actually
# decides whether an estimate holds up.
MIXTURE_FRACTIONS_FOR_QUANTITY = 3

import json
import sys
import textwrap

from serial_link import utc_timestamp

from Science.calibration import validate_calibration

from Science import comparison, preprocessing


class Mission:
    """
    Everything the operator screens need, in one place.

    Holds the link to the ESP32, the persistent Sample archive and the
    loaded BD science layer. Sample state is read from the archive, so
    it survives a restart of this program; carousel position is read
    from the ESP32, which forgets it on reset.
    """

    def __init__(self, link):
        self.link = link
        self.store = SampleStore()
        self.calibrations = CalibrationStore()

        self.references = None
        self.database = None
        self.registry = None
        self.taxonomy = None
        self.active_calibration = None
        self.science_error = None
        self.calibration_error = None

        # The learning layer is optional at runtime: a rover measurement
        # must still work if the history is unavailable, so a failure
        # here is recorded and everything else carries on.
        self.learning = None
        self.learning_error = None
        self.decision_engine = None
        self.class_snapshot = None
        self.profiles = AcquisitionProfileStore()

        # Recorded on every Measurement, so a stored record says
        # which firmware produced it. Filled by hardware_status().
        self.firmware_version = None

        self.load_science()
        self.load_decision_model()

    # ------------------------------------------------------------------
    # BD
    # ------------------------------------------------------------------

    def load_science(self):
        """
        Load the protected reference data and the active calibration.

        Two calibrations, always kept apart:

          LEGACY  calibration_legacy.json - what DB1 was normalized
                  against, and the only thing it may be compared with.
                  Immutable.

          ACTIVE  the full Dark + WHITE/UV/IR calibration the operator
                  made. Used for the scientific record and for quality
                  control. May legitimately be absent on a fresh
                  install; the legacy comparison still works without it.
        """
        self.references = None
        self.database = None
        self.registry = None
        self.active_calibration = None
        self.science_error = None
        self.calibration_error = None

        try:
            self.references = References()
            self.database = MaterialDatabase()

        except DatabaseError as error:
            self.science_error = "{}: {}".format(error.code, error.message)

        # DB1, DB2 and DB3 as three independent sources. A database that
        # is empty or unreadable is reported by the registry rather than
        # raising here: DB2 is legitimately empty, and losing DB3 must
        # not take the legacy DB1 comparison down with it.
        self.registry = DatabaseRegistry()

        try:
            self.taxonomy = Taxonomy(self.registry)

        except Exception as error:
            self.taxonomy = None
            self.science_error = self.science_error or "{}: {}".format(
                type(error).__name__, error
            )

        try:
            self.active_calibration = self.calibrations.active()

            if self.active_calibration is None:
                stored = len(self.calibrations.history())

                self.calibration_error = (
                    "{} calibration(s) are stored but none is selected. "
                    "Sensor Test -> [7] Select Which Calibration To "
                    "Use.".format(stored)
                    if stored
                    else "No full spectral calibration has been made yet."
                )

        except CalibrationError as error:
            self.calibration_error = "{}: {}".format(
                error.code, error.message
            )

        return self.science_error is None

    def load_decision_model(self):
        """
        Open the learning history and build the decision engine.

        Every failure here is survivable and is recorded rather than
        raised: an instrument that refuses to measure because its
        learning database is missing would be a worse instrument.
        """
        self.learning = None
        self.learning_error = None
        self.decision_engine = None
        self.class_snapshot = None

        try:
            self.learning = DecisionLearningStore()

        except Exception as error:
            self.learning_error = "{}: {}".format(
                type(error).__name__, error
            )

        try:
            if self.learning is not None:
                snapshot = class_models.build(
                    self.learning, self.calibration_for
                )
                self.class_snapshot = snapshot

            self.decision_engine = DecisionEngine(
                taxonomy=self.taxonomy,
                registry=self.registry,
                learning_store=self.learning,
                class_snapshot=(
                    self.class_snapshot["snapshot_id"]
                    if self.class_snapshot else None
                ),
            )

        except Exception as error:
            self.learning_error = self.learning_error or "{}: {}".format(
                type(error).__name__, error
            )

        return self.decision_engine is not None

    def calibration_for(self, calibration_id):
        """A stored calibration by id, or None. Never raises."""
        if not calibration_id:
            return None

        if (
            self.active_calibration is not None
            and self.active_calibration.calibration_id == calibration_id
        ):
            return self.active_calibration

        try:
            return self.calibrations.load(calibration_id)

        except CalibrationError:
            return None

    def current_profile(self, sensor_settings, firmware_version=None):
        """
        The acquisition profile these conditions correspond to.

        Created once per distinct set of conditions; the same settings
        always return the same profile id.
        """
        try:
            return self.profiles.ensure(from_measurement(
                sensor_settings, firmware_version=firmware_version
            ))

        except Exception:
            return None

    def build_evidence(self, acquisition, sensor_settings=None,
                       distance_mm=None, measurement_id="MEASUREMENT"):
        """
        The deterministic evidence package for one acquisition.

        Returns None when there is no active calibration: without a
        reference there is no reflectance, and the honest answer is that
        the evidence cannot be built rather than a package full of zeros.
        """
        if self.active_calibration is None:
            return None

        blocks = {}

        for name, block in (acquisition.get("illuminations") or {}).items():
            aggregated = preprocessing.aggregate_block(block)
            blocks[name] = aggregated["spectrum"]

        if not blocks:
            return None

        statistics = {
            name: preprocessing.aggregate_block(block)["statistics"]
            for name, block in (
                acquisition.get("illuminations") or {}
            ).items()
        }

        profile = self.current_profile(sensor_settings)

        return pipeline.build(
            measurement_id,
            blocks,
            self.active_calibration.dark,
            self.active_calibration.white,
            registry=self.registry,
            sensor_settings=sensor_settings,
            acquisition_profile=profile,
            calibration_id=self.active_calibration.calibration_id,
            legacy_calibration_id=(
                self.references.calibration_id if self.references else None
            ),
            legacy_references=self.references,
            sample_statistics=statistics,
            class_statistics=(
                self.class_snapshot["statistics"]
                if self.class_snapshot else None
            ),
            class_observations=(
                self.class_snapshot["observations"]
                if self.class_snapshot else None
            ),
            distance_mm=distance_mm,
        )

    def decide(self, package, mixture=None):
        """The decision for one evidence package, or None."""
        if package is None or self.decision_engine is None:
            return None

        try:
            return self.decision_engine.decide(package, mixture=mixture)

        except Exception as error:
            return {
                "level": "UNKNOWN",
                "material": None,
                "family": None,
                "confidence": "NONE",
                "reason": "the decision model failed: {}: {}".format(
                    type(error).__name__, error
                ),
                "candidates": [],
                "secondary_interpretations": [],
                "explanation": "",
            }

    def record_observation(self, measurement_id, result, label_type,
                           material=None, family_id=None, mixture=None,
                           sample_context=None,
                           verification_status="UNKNOWN",
                           verification_source=None, certainty=None,
                           session_id=None, sample_group=None,
                           independent_measurement=True, notes=None):
        """
        Store one measurement, its label and the model's prediction.

        The prediction is written under the model's own version, beside
        the ground truth and never as it: `BD.decision_learning` refuses
        a label whose source names a model, so this cannot become
        self-training even by mistake. §46.
        """
        if self.learning is None:
            raise LearningError(
                "LEARNING_UNAVAILABLE",
                self.learning_error or "the learning database is not open",
            )

        package = (result or {}).get("evidence") or {}
        decision = (result or {}).get("decision")

        # THE RAW IS IN THE EVIDENCE PACKAGE, and always was.
        #
        # This read `result["measurement"]["raw"]`. An AnalysisRun has
        # no "measurement" key - the block of that name lives inside the
        # evidence package and holds the wavelengths and the
        # illumination list, not the spectra. So `raw` was empty every
        # single time and this method refused every observation it was
        # ever offered with "the measurement carries no raw spectra to
        # store", which reads like a sensor fault and is a typo.
        #
        # Nothing could be recorded in the learning history at all.
        raw = package.get("raw") or {}

        if not raw:
            raise LearningError(
                "RAW_REQUIRED",
                "the analysis produced no evidence package, so there "
                "are no raw spectra to store - a calibration must be "
                "active before an observation can be recorded",
            )

        self.learning.add_observation(
            measurement_id,
            raw,
            session_id=session_id,
            sample_group=sample_group or (
                material.key if material is not None else None
            ),
            independent_measurement=independent_measurement,
            acquisition_profile_id=(
                (package or {}).get("acquisition", {}).get(
                    "acquisition_profile_id"
                )
            ),
            calibration_id=(
                self.active_calibration.calibration_id
                if self.active_calibration else None
            ),
            legacy_calibration_id=(
                self.references.calibration_id if self.references else None
            ),
            sensor_settings=(
                (package.get("acquisition") or {}).get("sensor_settings")
            ),
            quality=(package or {}).get("quality"),
            channel_reliability=(package or {}).get("channel_reliability"),
            evidence_schema_version=(package or {}).get("schema_version"),
            notes=notes,
        )

        self.learning.add_ground_truth(
            measurement_id,
            label_type,
            material_key=material.key if material is not None else None,
            material_id=(
                material.material_id if material is not None else None
            ),
            family_id=family_id,
            mixture=mixture,
            verification_status=verification_status,
            verification_source=verification_source,
            certainty=certainty,
        )

        # How the sample was presented. Written after the label because
        # a rejected mixture should not leave a context row behind
        # describing a measurement whose ground truth was refused.
        if sample_context:
            self.learning.add_sample_context(
                measurement_id, **sample_context
            )

        if decision:
            try:
                self.learning.add_prediction(
                    measurement_id,
                    decision.get(
                        "decision_model_version", "UNKNOWN_MODEL"
                    ),
                    decision.get("level", "UNKNOWN"),
                    material_key=decision.get("material"),
                    family_id=decision.get("family"),
                    candidates=decision.get("candidates"),
                    confidence=decision.get("confidence"),
                    decision={
                        "reason": decision.get("reason"),
                        "explanation": decision.get("explanation"),
                        "provenance": decision.get("provenance"),
                    },
                )

            except LearningError:
                pass

        return self.learning.get_observation(measurement_id)

    def mixture_readiness(self):
        """
        One line on how far the prepared-mixture library has to go.

        Said after every mixture is saved, because the useful thing to
        know at the bench is not "saved" but "how many more of these do
        I need". A quantity model needs the same material at several
        different fractions; a detection model needs several materials
        against the same matrix. Neither is close with three.
        """
        if self.learning is None:
            return None

        try:
            summary = self.learning.mixture_summary()

        except Exception:
            return None

        if not summary["mixtures"]:
            return None

        spread = [
            key for key, entry in summary["by_material"].items()
            if len(set(entry["fractions"])) >= MIXTURE_FRACTIONS_FOR_QUANTITY
        ]

        return (
            "{} prepared mixture(s) on record over {} material(s); {} "
            "have the {}+ different fractions a quantity estimate would "
            "need.".format(
                summary["mixtures"], summary["materials_spiked"],
                len(spread), MIXTURE_FRACTIONS_FOR_QUANTITY,
            )
        )

    def calibration_health(self):
        """PASS / MISSING / INVALID for each of the two calibrations."""
        if self.references is None:
            legacy = "MISSING"
        elif self.references.white_missing or self.references.dark_missing:
            legacy = "INVALID"
        else:
            legacy = "PASS"

        if self.active_calibration is None:
            active = "MISSING"
        else:
            # Validation is science, so it comes from Science rather
            # than from the stored record itself (see Documentation/ARCHITECTURE.md).
            result = validate_calibration(self.active_calibration.document)
            active = "PASS" if result["status"] != "FAIL" else "INVALID"

        try:
            stored = len(self.calibrations.history())

        except CalibrationError:
            stored = 0

        return {
            "legacy": legacy,
            "active": active,
            "stored": stored,
            "legacy_id": (
                self.references.calibration_id if self.references else None
            ),
            "active_id": (
                self.active_calibration.calibration_id
                if self.active_calibration else None
            ),
        }

    # ------------------------------------------------------------------
    # state
    # ------------------------------------------------------------------

    def hardware_status(self):
        """
        The live hardware state, and the firmware version behind it.

        Read fresh every time round the main screen. Nothing about the
        hardware is cached: a carousel that reports a valid position is
        reporting it now, not when the program started.
        """
        status = self.link.get_status()

        self.firmware_version = status.get("version")

        return status

    def slot_view(self, status):
        """
        Merge the PC's Sample lifecycle with the ESP32's physical state.

        The PC is authoritative for what a slot MEANS; the ESP32 is
        authoritative for what it physically holds.
        """
        # active_samples() answers "which Sample ID is in which slot".
        # The record itself is looked up for the rest, because a slot
        # row needs the state and the measurement count too - and the
        # store is the one place that knows them.
        by_slot = self.store.active_samples()
        physical = {
            slot.get("slot_id"): slot
            for slot in (status.get("slots") or [])
        }

        view = []

        for slot_id in range(1, SLOT_COUNT + 1):
            sample_id = by_slot.get(slot_id)
            record = (
                self.store.get_sample(sample_id) if sample_id else None
            )

            view.append({
                "slot_id": slot_id,
                "sample_id": sample_id,
                "state": (record or {}).get("state", STATE_EMPTY),
                "measurement_count": len(
                    (record or {}).get("measurements") or []
                ),
                "occupied": bool(
                    (physical.get(slot_id) or {}).get("occupied")
                ),
            })

        return view

    def entry_for(self, view, slot_id):
        for entry in view:
            if entry["slot_id"] == slot_id:
                return entry

        return {
            "slot_id": slot_id,
            "sample_id": None,
            "state": STATE_EMPTY,
            "measurement_count": 0,
            "occupied": False,
        }

    # ------------------------------------------------------------------
    # the measurement pipeline
    # ------------------------------------------------------------------

    def analyse_measurement(self, measurement, distance_mm=None):
        """
        A STORED Measurement in, one complete AnalysisRun out.

        The Measurement is already in BD by the time this is called -
        that ordering is the whole reason this method takes a stored
        record rather than a fresh acquisition. Everything below can
        fail without costing the experiment.

        ONE pipeline. An earlier revision ran two side by side and
        stored both: a legacy DB1 comparison and the current
        cross-database one, which meant two normalizations, two idea of
        what "the conclusion" was, and a record where the two could
        disagree with nothing to say which was right. DB1 is now one of
        three databases inside the single pipeline, compared under the
        frozen legacy calibration it was built with, and its result sits
        beside DB2's and DB3's rather than above them.

        Never raises for a scientific outcome. A failure inside Science
        comes back as an AnalysisRun with a status and whatever earlier
        stages produced, because a partial run is evidence and an
        exception is not.
        """
        if self.active_calibration is None:
            return {
                "analysis_status": pipeline.ANALYSIS_FAILED,
                "measurement_id": measurement.get("measurement_id"),
                "error": {
                    "code": "NO_ACTIVE_CALIBRATION",
                    "message": self.calibration_error
                    or "No calibration is active, so no reflectance can "
                       "be derived. RAW is saved and can be analysed "
                       "once a calibration is selected.",
                },
            }

        acquisition = dict(measurement.get("acquisition") or {})

        if distance_mm is not None:
            acquisition["distance_mm"] = distance_mm

        prepared = dict(measurement)
        prepared["acquisition"] = acquisition
        prepared["statistics"] = measurement.get("statistics")

        try:
            return pipeline.analyze(
                prepared,
                self.active_calibration.as_dict()
                if hasattr(self.active_calibration, "as_dict")
                else {
                    "calibration_id":
                        self.active_calibration.calibration_id,
                    "dark": self.active_calibration.dark,
                    "white": self.active_calibration.white,
                    "statistics": getattr(
                        self.active_calibration, "statistics", None
                    ),
                },
                self.registry,
                taxonomy=self.taxonomy,
                learning_store=self.learning,
                class_statistics=(
                    self.class_snapshot["statistics"]
                    if self.class_snapshot else None
                ),
                class_observations=(
                    self.class_snapshot["observations"]
                    if self.class_snapshot else None
                ),
                class_snapshot=(
                    self.class_snapshot["snapshot_id"]
                    if self.class_snapshot else None
                ),
                legacy_references=self.references,
                feature_sources=None,
            )

        except Exception as error:
            # Only a caller error reaches here - a Measurement with no
            # RAW, or no calibration at all. Scientific failures are
            # reported inside the run, not raised.
            return {
                "analysis_status": pipeline.ANALYSIS_FAILED,
                "measurement_id": measurement.get("measurement_id"),
                "error": {
                    "code": getattr(error, "code", type(error).__name__),
                    "message": str(error),
                },
            }

    def analyse_acquisition(self, data, measurement_id="SENSOR_TEST"):
        """
        Analyse an acquisition WITHOUT storing anything.

        For the sensor test only, which says "TEST ONLY - NOTHING
        SAVED" on screen and means it. The measurement record is built
        in memory and thrown away; nothing reaches BD.

        This is the ONE path that analyses something unstored, and it
        exists because a sensor test needs to prove the whole chain
        works without putting a throwaway acquisition into the
        scientific record. Every acquisition of an actual SAMPLE goes
        through add_measurement first.
        """
        measurement = self.measurement_from_acquisition(data)
        measurement["measurement_id"] = measurement_id
        measurement["sample_id"] = None

        return self.analyse_measurement(measurement)

    def measurement_from_acquisition(self, data, sample_id=None):
        """
        Turn what the ESP32 returned into the fields BD stores.

        RAW STAYS GROUPED BY ILLUMINATION. The three blocks are what
        was physically measured under three different lamps, and once
        they are concatenated into one 54-element vector nothing can
        tell which lamp produced which number - which makes every
        per-illumination quality judgement impossible and every
        per-illumination fault invisible. The combined vector is
        CALCULATED and belongs to an AnalysisRun.

        Repeats are aggregated into one spectrum per illumination, and
        the spread across them is kept beside it as statistics: that
        spread is the only evidence there is that a channel is stable
        rather than merely present.
        """
        blocks = data.get("illuminations") or {}

        raw = {}
        statistics = {}

        for name, block in blocks.items():
            aggregated = preprocessing.aggregate_block(block)
            raw[name] = aggregated["spectrum"]
            statistics[name] = aggregated["statistics"]

        settings = data.get("sensor_settings")

        return {
            "raw": raw or None,
            "statistics": statistics or None,
            "acquisition": {
                "sensor_settings": settings,
                "profile_id": self.current_profile(settings),
                "illuminations": sorted(raw),
                "repeats": {
                    name: (block or {}).get("repeats")
                    for name, block in blocks.items()
                },
                "protocol_version": data.get("protocol_version"),
                "firmware_version": self.firmware_version,
                "temperatures": data.get("temperatures"),
                "wavelengths": pipeline.channel_wavelengths()
                if hasattr(pipeline, "channel_wavelengths") else None,
            },
            "hardware": {
                "carousel": data.get("carousel"),
                "move": data.get("move"),
                "return_move": data.get("return_move"),
                "home_restored": data.get("home_restored"),
                "bulbs_off": data.get("bulbs_off"),
            },
            "calibration_id": (
                self.active_calibration.calibration_id
                if self.active_calibration else None
            ),
        }


