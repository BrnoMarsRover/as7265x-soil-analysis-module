#!/usr/bin/env python3
"""
Freya science module - main computer application.

Mission controller and operator interface. This program owns the
workflow, the Sample lifecycle and the persistent Sample archive. It
talks to two things:

    PC  ->  serial  ->  ESP32      hardware: carousel + RAW spectrum
    PC  ->  import  ->  BD         science:  White/Dark, normalization,
                                             database comparison

The ESP32 never learns what a sample resembles, and BD never learns
that a serial port exists.

Usage:

    py rover_science_client.py --port COM4
    py rover_science_client.py --port COM4 --command get_status
"""

import argparse
import json
import sys
import textwrap
from pathlib import Path

# BD and Measurements are siblings of this directory. Putting the project
# root on the path means the application runs from anywhere without any
# PYTHONPATH setup, and every import below says which layer it came from.
PC_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PC_DIR.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# --- BD: persistence --------------------------------------------------
from BD import config as bd_config                      # noqa: E402
from BD.calibrations import (              # noqa: E402
    CalibrationError,
    CalibrationStore,
)
from BD.samples import (                   # noqa: E402
    METADATA_FIELDS,
    STATE_EMPTY,
    STATE_LOADED,
    STATE_MEASURED,
    STATE_READY_TO_LOAD,
    SampleStore,
    StorageError,
    validate_sample_id,
)
from BD.databases import (        # noqa: E402
    DatabaseError,
    MaterialDatabase,
    References,
)
from BD.channels import (                     # noqa: E402
    AS7265X_18,
    AS7265X_54_MULTIILLUM,
    FeatureSpaceError,
    ILLUMINATIONS,
    combine_illuminations,
)
from BD.registry import DatabaseRegistry      # noqa: E402
from BD.acquisition_profiles import (          # noqa: E402
    AcquisitionProfileStore,
    from_measurement,
)
from BD.decision_learning import (             # noqa: E402
    DecisionLearningStore,
    LearningError,
)
from BD.taxonomy import Taxonomy               # noqa: E402

# --- Measurements: science -------------------------------------------
from Measurements import aggregation                    # noqa: E402
from Measurements import analysis as sample_analysis    # noqa: E402
from Measurements import config as science_config       # noqa: E402
from Measurements import channel_reliability            # noqa: E402
from Measurements import evidence as evidence_module    # noqa: E402
from Measurements import inference                      # noqa: E402

# --- DecisionModel: interpretation ------------------------------------
from DecisionModel import class_models                  # noqa: E402
from DecisionModel.engine import DecisionEngine         # noqa: E402
from Measurements.calibration import (                  # noqa: E402
    build_calibration,
    validate_calibration,
)

# --- PC: transport ----------------------------------------------------
from esp32_link import (                                # noqa: E402
    CONNECT_TIMEOUT,
    DEFAULT_BAUDRATE,
    DEFAULT_TIMEOUT,
    ESP32Link,
    LinkError,
    MEASUREMENT_TIMEOUT,
    utc_timestamp,
)

# Four physical slots, 90 degrees apart; the scanner sits two slots
# (180 degrees) from the loader.
SLOT_COUNT = 4
RULE = "=" * 60


# ======================================================================
# small console helpers
# ======================================================================

def ask(prompt, default=""):
    try:
        answer = input("{}: ".format(prompt)).strip()

    except EOFError:
        return default

    return answer or default


def choose(prompt="Select"):
    """Menu input, normalized. A bare Enter is not an unknown option."""
    return ask(prompt).strip().lower()


def ask_int(prompt, minimum=None, maximum=None, default=None):
    """
    Ask for a whole number; blank cancels unless there is a default.

    Returns None only when the operator deliberately cancels, and every
    caller must say something when that happens - silently dropping back
    to the menu is what made "Measure Sample" look like it did nothing.
    """
    while True:
        if default is not None:
            raw = ask("{} [{}]".format(prompt, default))

            if not raw:
                return default

        else:
            raw = ask("{} (blank = cancel)".format(prompt))

            if not raw:
                return None

        try:
            value = int(raw)

        except ValueError:
            print("Enter a whole number.")

            continue

        if minimum is not None and value < minimum:
            print("Minimum is {}.".format(minimum))

            continue

        if maximum is not None and value > maximum:
            print("Maximum is {}.".format(maximum))

            continue

        return value


def ask_float(prompt, minimum=None, maximum=None):
    while True:
        raw = ask("{} (blank = cancel)".format(prompt))

        if not raw:
            return None

        try:
            value = float(raw.replace(",", "."))

        except ValueError:
            print("Enter a number, for example 2.5 or -1.")

            continue

        if minimum is not None and value < minimum:
            print("Minimum is {}.".format(minimum))

            continue

        if maximum is not None and value > maximum:
            print("Maximum is {}.".format(maximum))

            continue

        return value


def confirm(prompt):
    """Explicit yes only. Anything else, including a bare Enter, is no."""
    return ask("{} [y/N]".format(prompt)).strip().lower().startswith("y")


def pause():
    ask("Press Enter to continue")


def banner(title):
    print()
    print(RULE)
    print(" {}".format(title))
    print(RULE)
    print()


def score(value):
    return "{:.2f} %".format(value) if isinstance(value, (int, float)) else "-"


def number(value, digits=4):
    return "{:.{}f}".format(value, digits) if isinstance(
        value, (int, float)
    ) else "-"


def report_link_error(error):
    print()
    print("Module refused the command:")
    print("  code   : {}".format(error.code))
    print("  message: {}".format(error.message))

    data = error.data or {}

    if data.get("recovery"):
        print("  carousel: {}".format(data["recovery"].get("message")))

    elif data.get("moved") is False:
        print("  carousel: nothing was moved.")


def report_failure(error):
    """One place that knows how to print either kind of link failure."""
    if isinstance(error, LinkError):
        report_link_error(error)

    else:
        print()
        print("Timeout: {}".format(error))


def report_return_move(return_move):
    """
    Report the 180 deg return as its own outcome.

    Acquisition and mechanical recovery are separate results: a servo
    that failed to come home must never be reported as a failed
    measurement, and a successful measurement must never imply the
    carousel is where the software thinks it is.
    """
    if not return_move:
        return

    if return_move.get("returned"):
        print("Returning Slot home............ PASS")

        return

    print()
    print("!! RETURN MOVEMENT FAILED")
    print("   {}".format(return_move.get("message", "")))

    if return_move.get("exception_message"):
        print("   {}".format(return_move["exception_message"]))

    print("   Carousel position is now UNKNOWN - re-sync before moving.")


# ======================================================================
# mission controller
# ======================================================================

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
            aggregated = aggregation.aggregate_block(block)
            blocks[name] = aggregated["spectrum"]

        if not blocks:
            return None

        statistics = {
            name: aggregation.aggregate_block(block)["statistics"]
            for name, block in (
                acquisition.get("illuminations") or {}
            ).items()
        }

        profile = self.current_profile(sensor_settings)

        return evidence_module.build(
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

        package = (result or {}).get("evidence")
        decision = (result or {}).get("decision")
        measurement = (result or {}).get("measurement") or {}

        raw = measurement.get("raw") or {}

        if not raw:
            raise LearningError(
                "RAW_REQUIRED",
                "the measurement carries no raw spectra to store",
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
            sensor_settings=measurement.get("sensor_settings"),
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
            # Validation is science, so it comes from Measurements rather
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
        return self.link.get_status()

    def slot_view(self, status):
        """
        Merge the PC's Sample lifecycle with the ESP32's physical state.

        The PC is authoritative for what a slot MEANS; the ESP32 is
        authoritative for what it physically holds.
        """
        by_slot = self.store.active_samples()
        physical = {
            slot.get("slot_id"): slot
            for slot in (status.get("slots") or [])
        }

        view = []

        for slot_id in range(1, SLOT_COUNT + 1):
            entry = by_slot.get(slot_id)

            view.append({
                "slot_id": slot_id,
                "sample_id": entry.get("sample_id") if entry else None,
                "state": entry.get("state") if entry else STATE_EMPTY,
                "occupied": bool(
                    (physical.get(slot_id) or {}).get("occupied")
                ),
            })

        return view

    def entry_for(self, view, slot_id):
        for entry in view:
            if entry["slot_id"] == slot_id:
                return entry

        return {"slot_id": slot_id, "sample_id": None, "state": STATE_EMPTY}

    # ------------------------------------------------------------------
    # the measurement pipeline
    # ------------------------------------------------------------------

    def analyse_raw(self, acquisition, sensor_settings=None,
                    distance_mm=None):
        """
        Acquisition in, complete BD result out.

        Accepts the WHITE/UV/IR protocol and, for an archived record
        from before this release, a bare 18-channel white spectrum.

        Raises AnalysisError or DatabaseError; the caller is responsible
        for preserving the acquired spectra either way.
        """
        if self.references is None:
            raise DatabaseError(
                "REFERENCES_NOT_LOADED",
                self.science_error or "White/Dark references are not loaded.",
            )

        result = sample_analysis.analyze(
            acquisition,
            self.references,
            self.database,
            sensor_settings,
            self.active_calibration,
            distance_mm,
        )

        result["cross_database"] = self.infer_cross_database(result)

        # The new pipeline, beside the old one rather than instead of it.
        # `analysis` remains the DB1 conclusion under the legacy
        # calibration; `evidence` is the deterministic package and
        # `decision` is what the Decision Model made of it.
        measurement_id = (
            acquisition.get("measurement_id")
            or "TEST_{}".format(utc_timestamp())
        )

        try:
            result["evidence"] = self.build_evidence(
                acquisition, sensor_settings, distance_mm, measurement_id
            )

        except Exception as error:
            result["evidence"] = None
            result["evidence_error"] = "{}: {}".format(
                type(error).__name__, error
            )

        result["decision"] = self.decide(
            result.get("evidence"), result.get("mixture")
        )

        return result

    def feature_sources(self, result):
        """
        Which normalization of this measurement each database may see.

        The databases are not normalized alike, and no single vector
        serves all three:

            DB1  LEGACY  the frozen White/Dark the library was built
                         against, and the only one it may be compared
                         with. Using today's calibration would silently
                         change what every stored number means.
            DB2  ACTIVE  measured on this instrument under the current
                         calibration - the same 54 features it stores.
            DB3  ACTIVE  never measured here at all, so the honest
                         comparison is against the instrument as it is
                         now. Falls back to LEGACY when no calibration is
                         active, and says which it used.

        Returns (feature_sources, top_level_features, feature_space).
        """
        measurement = result["measurement"]

        legacy = (
            measurement.get("legacy_database_normalized") or {}
        ).get("white") or {}

        legacy_id = (result.get("calibration") or {}).get(
            "legacy_database_calibration_id"
        )
        active_id = (result.get("calibration") or {}).get(
            "active_calibration_id"
        )

        legacy_label = "legacy:{}".format(legacy_id)

        # The 54-feature vector exists only when a full calibration
        # produced all three illuminations. A partial one is not
        # assembled: see BD/channels.py::combine_illuminations.
        active_54 = None
        active_white = None

        if active_id:
            try:
                active_54 = combine_illuminations(
                    measurement.get("active_normalized") or {}
                )
                active_white = {
                    channel: active_54["white:{}".format(channel)]
                    for channel in sample_analysis.CHANNELS
                }

            except FeatureSpaceError:
                active_54 = None
                active_white = None

        if active_54 is not None:
            features = active_54
            feature_space = AS7265X_54_MULTIILLUM
            current = active_white
            current_label = "active:{}".format(active_id)

        else:
            features = legacy
            feature_space = AS7265X_18
            current = legacy
            current_label = legacy_label

        sources = {
            "DB1": {
                "features": legacy,
                "feature_space": AS7265X_18,
                "normalization": legacy_label,
            },
            "DB2": {
                "features": features,
                "feature_space": feature_space,
                "normalization": current_label,
            },
            "DB3": {
                "features": current,
                "feature_space": AS7265X_18,
                "normalization": current_label,
            },
        }

        return sources, features, feature_space

    def infer_cross_database(self, result):
        """
        Score the measurement against DB1, DB2 and DB3 separately.

        Never pooled with the legacy comparison and never a replacement
        for it: `analysis` stays the DB1 conclusion under the calibration
        DB1 was built with, and this is the wider view - what each
        independent library says, whether they agree, and how much that
        agreement is worth.

        A failure here must never lose a measurement, so it is reported
        as a block with a reason rather than raised.
        """
        if self.registry is None:
            return None

        try:
            sources, features, feature_space = self.feature_sources(result)

            if not features:
                return {
                    "status": "SKIPPED",
                    "reason": "the measurement produced no normalized "
                              "spectrum to compare",
                }

            report = inference.infer(
                features,
                feature_space,
                self.registry,
                result.get("quality"),
                (result.get("quality") or {}).get("usable_channels"),
                feature_sources=sources,
            )

            report["status"] = "OK"

            return report

        except Exception as error:
            return {
                "status": "FAILED",
                "reason": "{}: {}".format(type(error).__name__, error),
            }


# ======================================================================
# screens: status and hardware
# ======================================================================

def print_system_status(mission):
    """PC, BD and ESP32 in one place. A failure in one still shows the rest."""
    banner("SYSTEM STATUS")

    store = mission.store

    print("PC")
    print("Connection:       {}".format(
        "ONLINE" if mission.link.online else "UNKNOWN"
    ))
    print("Sample storage:   {}".format("READY" if store.ready else "ERROR"))
    print("Samples saved:    {}".format(store.count()))
    print("Sample archive:   {}".format(store.archive_path))

    if store.migrated_from:
        print("  migrated from:  {}".format(store.migrated_from))

    if store.error:
        print("Storage error:    {}".format(store.error))

    print()
    print("BD")

    health = mission.calibration_health()

    if mission.references is not None:
        refs = mission.references.status()

        print("Legacy cal:       {} ({}/{} white + {}/{} dark)".format(
            health["legacy"],
            refs["white_channels"], refs["channels_required"],
            refs["dark_channels"], refs["channels_required"],
        ))
        print("  id:             {}".format(refs["calibration_id"]))
        print("  protected:      YES - DB1.json depends on it")

        if refs["zero_denominator_channels"]:
            print("  warning: White == Dark on {}".format(
                ",".join(refs["zero_denominator_channels"])
            ))

    else:
        print("Legacy cal:       ERROR - {}".format(mission.science_error))

    print("Active cal:       {}".format(health["active"]))

    if mission.active_calibration is not None:
        print("  id:             {}".format(health["active_id"]))
        print("  created:        {}".format(
            mission.active_calibration.created_at
        ))
        print("  illuminations:  WHITE + UV + IR")

    else:
        print("  {}".format(
            mission.calibration_error or "not created yet"
        ))

    print("  library:        {} calibration(s) in {}".format(
        health["stored"], mission.calibrations.library_path.name
    ))

    if mission.database is not None:
        print("Material DB:      READY ({} materials)".format(
            mission.database.count()
        ))

        incomplete = mission.database.incomplete_materials()

        if incomplete:
            print("  warning: {} material(s) have incomplete spectra".format(
                len(incomplete)
            ))

    else:
        print("Material DB:      ERROR - {}".format(mission.science_error))

    print()
    print("DECISION MODEL")

    if mission.decision_engine is not None:
        print("Model:            {} ({})".format(
            mission.decision_engine.version, mission.decision_engine.kind
        ))

    else:
        print("Model:            UNAVAILABLE - {}".format(
            mission.learning_error
        ))

    if mission.learning is not None:
        learning = mission.learning.status()

        print("Learning history: {} observation(s), {} verified".format(
            learning["observations"], learning["by_verification"]["VERIFIED"]
        ))
        print("  file:           {}".format(learning["file"]))
        print("  materials with verified measurements: {}".format(
            len(learning["verified_materials"])
        ))

    if mission.class_snapshot is not None:
        coverage = class_models.coverage(mission.class_snapshot)

        print("Class models:     {} material(s), {} with measurable "
              "scatter".format(
                  mission.class_snapshot["materials"],
                  coverage["with_scatter"],
              ))
        print("  snapshot:       {}".format(
            mission.class_snapshot["snapshot_id"]
        ))

    if mission.taxonomy is not None:
        taxonomy = mission.taxonomy.status()

        print("Vocabulary:       {} material(s) in {} families".format(
            taxonomy["materials"], taxonomy["families"]
        ))

    if mission.registry is not None:
        print()
        print("DATABASES  (scored separately, never pooled)")
        print()

        for line in mission.registry.summary().splitlines():
            print("  {}".format(line))

    print()
    print("ESP32")

    try:
        status = mission.hardware_status()

    except (LinkError, TimeoutError) as error:
        print("Controller:       UNREACHABLE ({})".format(error))
        print()

        return

    sensor = status.get("sensor") or {}
    settings = sensor.get("settings") or {}
    carousel = status.get("carousel") or {}

    print("Controller:       READY ({} {})".format(
        status.get("firmware"), status.get("version")
    ))
    print("Sensor:           {}".format(
        "READY" if sensor.get("ready") else "UNAVAILABLE"
    ))
    print("I2C:              {} on bus {}".format(
        sensor.get("address"), (sensor.get("bus") or {}).get("bus")
    ))

    if settings:
        print("Integration:      {} cycles".format(
            settings.get("integration_cycles")
        ))
        print("Gain:             {}".format(settings.get("gain_x")))
        print("LED current:      {}".format(
            settings.get("led_current_ma", settings.get("led_current"))
        ))
        print("Mode:             {}".format(settings.get("measurement_mode")))

    # A boot failure that was later recovered is a warning, never a
    # reason to report the sensor as unavailable now.
    if sensor.get("boot_error") and sensor.get("ready"):
        print("Boot warning:     {} (recovered {}x)".format(
            sensor["boot_error"].get("code"), sensor.get("recovery_count")
        ))

    elif sensor.get("boot_error"):
        print("Boot error:       {} - {}".format(
            sensor["boot_error"].get("code"),
            sensor["boot_error"].get("message"),
        ))

    if sensor.get("current_error"):
        print("Current error:    {} - {}".format(
            sensor["current_error"].get("code"),
            sensor["current_error"].get("message"),
        ))

    # Acquisitions the ESP32 is still holding, so an operator can see at
    # a glance whether a sync would transfer anything.
    retained = [
        slot for slot in (status.get("slots") or [])
        if slot.get("has_measurement")
    ]

    print("Held acquisitions: {}".format(len(retained)))

    print()
    print("CAROUSEL")

    geometry = carousel.get("geometry") or {}
    encoder = carousel.get("encoder") or {}

    print("Slots:            {} ({:.0f} deg apart, {} counts)".format(
        carousel.get("slot_count", SLOT_COUNT),
        geometry.get("slot_spacing_deg", 360.0 / SLOT_COUNT),
        geometry.get("counts_per_slot"),
    ))
    print("Synchronized:     {}".format(
        "YES" if carousel.get("position_valid") else "NO"
    ))
    print("Selected slot:    {}".format(carousel.get("selected_slot")))
    print("Loader:           {}".format(carousel.get("current_load_slot")))
    print("Scanner:          {}".format(carousel.get("current_scan_slot")))
    print("Origin:           {} counts".format(
        encoder.get("origin_counts")
    ))
    print("Alignment offset: {} counts ({} deg)".format(
        encoder.get("alignment_offset_counts"),
        encoder.get("alignment_offset_deg"),
    ))

    if encoder.get("drift_counts") is not None:
        print("Drift vs nominal: {} counts ({} deg)".format(
            encoder.get("drift_counts"), encoder.get("drift_deg")
        ))

    if geometry.get("error"):
        print("GEOMETRY ERROR:   {}".format(geometry["error"]))

    print()
    print("SERVO")

    # Which actuator is in charge is the single most important thing an
    # operator can be told about the carousel, so it is never hidden
    # behind an error.
    servo = status.get("servo") or {}
    backend = servo.get("backend") or {}

    print_servo_block(servo)

    if servo.get("selected") and not backend.get("connected"):
        error = backend.get("error") or {}

        if error:
            print("  ** NOT ANSWERING: {} - {}".format(
                error.get("code"), error.get("message")
            ))
            print("  The ST3215 subsystem is powered externally; check that")
            print("  supply, the common ground and the TX/RX pair first.")

    print()


# ======================================================================
# screens: spectrum and analysis
# ======================================================================

def print_spectrum_table(measurement):
    wavelengths = measurement.get("wavelengths") or {}
    raw = measurement.get("raw") or {}
    corrected = measurement.get("dark_corrected") or {}
    normalized = measurement.get("normalized") or {}

    print("CH   nm    {:>12} {:>12} {:>12}".format(
        "RAW", "DARK-CORR", "NORMALIZED"
    ))

    for channel in sample_analysis.CHANNELS:
        print("{:<4} {:<5} {:>12} {:>12} {:>12}".format(
            channel,
            wavelengths.get(channel, "-"),
            number(raw.get(channel)),
            number(corrected.get(channel)),
            number(normalized.get(channel), 6),
        ))


def print_processing_table(measurement, dark, white):
    """Every step of the calculation, per channel, side by side."""
    raw = measurement.get("raw") or {}
    corrected = measurement.get("dark_corrected") or {}
    normalized = measurement.get("normalized") or {}

    print("CH   {:>10} {:>10} {:>10} {:>12} {:>12}".format(
        "RAW", "DARK", "WHITE", "DARK-CORR", "NORMALIZED"
    ))

    for channel in sample_analysis.CHANNELS:
        print("{:<4} {:>10} {:>10} {:>10} {:>12} {:>12}".format(
            channel,
            number(raw.get(channel)),
            number(dark.get(channel)),
            number(white.get(channel)),
            number(corrected.get(channel)),
            number(normalized.get(channel), 6),
        ))


def print_triad_table(measurement):
    """All three illuminations side by side - the 54 features."""
    raw = measurement.get("raw") or {}
    active = measurement.get("active_normalized") or {}
    wavelengths = measurement.get("wavelengths") or {}

    if not isinstance(raw, dict) or "white" not in raw:
        print_spectrum_table(measurement)

        return

    have_active = bool(active)

    header = "CH   nm    {:>11} {:>11} {:>11}".format(
        "WHITE raw", "UV raw", "IR raw"
    )

    if have_active:
        header += "   {:>9} {:>9} {:>9}".format("R white", "R uv", "R ir")

    print(header)

    for channel in sample_analysis.CHANNELS:
        row = "{:<4} {:<5} {:>11} {:>11} {:>11}".format(
            channel,
            wavelengths.get(channel, "-"),
            number((raw.get("white") or {}).get(channel)),
            number((raw.get("uv") or {}).get(channel)),
            number((raw.get("ir") or {}).get(channel)),
        )

        if have_active:
            row += "   {:>9} {:>9} {:>9}".format(
                number((active.get("white") or {}).get(channel), 4),
                number((active.get("uv") or {}).get(channel), 4),
                number((active.get("ir") or {}).get(channel), 4),
            )

        print(row)


def print_quality(report):
    """Measurement quality, with the reasons spelled out."""
    if not report:
        return

    print("Overall: {}".format(report.get("status")))

    for check in report.get("checks") or []:
        if check["status"] == "PASS":
            continue

        print("  [{}] {}: {}".format(
            check["status"], check["check"], check["message"]
        ))

    invalid = report.get("invalid_channels") or []

    if invalid:
        print("  Channels excluded from comparison: {}".format(
            ",".join(invalid)
        ))


def print_metric_table(matches, limit=None):
    """
    The ranked comparison, showing every metric.

    All three are printed because they disagree in informative ways -
    collapsing them into one number is exactly what made a 97% cosine
    look like a confident identification.
    """
    if not matches:
        print("No reference materials were compared.")

        return

    shown = matches if limit is None else matches[:limit]

    print("{:<3} {:<26} {:>8} {:>9} {:>8} {:>8}".format(
        "#", "Material", "Combined", "Cosine", "RMSE", "Pearson"
    ))

    for match in shown:
        pearson = match.get("pearson_r")
        rmse_value = match.get("rmse")

        print("{:<3} {:<26} {:>8} {:>9} {:>8} {:>8}".format(
            match.get("combined_rank", match.get("rank")),
            str(match.get("material"))[:26],
            "{}/{}/{}".format(
                match.get("cosine_rank"),
                match.get("rmse_rank"),
                match.get("pearson_rank"),
            ),
            score(match.get("cosine_similarity_percent")),
            "{:.4f}".format(rmse_value) if rmse_value is not None else "-",
            "{:+.3f}".format(pearson) if pearson is not None else "-",
        ))

    if limit is not None and len(matches) > limit:
        print("... {} more (all stored with the sample)".format(
            len(matches) - limit
        ))

    print()
    print("Combined column is the cosine/RMSE/Pearson rank triple.")
    print("Cosine is shape only; RMSE keeps magnitude; Pearson is")
    print("correlation. None of them is a probability.")


def print_agreement(agreement):
    if not agreement or agreement.get("agree") is None:
        return

    if agreement.get("agree"):
        print("Metrics agree: all three rank {} at or near the top.".format(
            agreement.get("combined_best")
        ))

        return

    print("METRICS DISAGREE:")
    print("  best by cosine : {}".format(agreement.get("cosine_best")))
    print("  best by RMSE   : {}".format(agreement.get("rmse_best")))
    print("  best by Pearson: {}".format(agreement.get("pearson_best")))


def print_matches(matches, limit=None):
    if not matches:
        print("No reference materials were compared.")

        return

    shown = matches if limit is None else matches[:limit]

    print("{:<4} {:<32} {:>12}".format("#", "Material", "Similarity"))

    for match in shown:
        print("{:<4} {:<32} {:>12}".format(
            match.get("rank"),
            str(match.get("material"))[:32],
            score(match.get("similarity_percent")),
        ))

    if limit is not None and len(matches) > limit:
        print("... {} more (all stored with the sample)".format(
            len(matches) - limit
        ))


DATABASE_LABELS = {
    "DB1": "measured here, 18 bands",
    "DB2": "measured here, 54 features",
    "DB3": "external spectra, projected",
}


def print_cross_database(cross, limit=5):
    """
    What each of the three databases says, separately, and what that adds
    up to.

    Never a single ranked list. DB1 says "this looks like something we
    measured on this instrument"; DB3 says "this looks like a laboratory
    spectrum of that mineral, after modelling our sensor". Those are
    different claims, so they are printed as different answers and their
    scores are never averaged.
    """
    if not cross:
        return

    if cross.get("status") != "OK":
        print("Cross-database inference: {}".format(cross.get("status")))
        print("  {}".format(cross.get("reason")))

        return

    print("{:<5} {:<28} {:<26} {:>8}  {}".format(
        "DB", "Library", "Best match", "Cosine", "Class reliability"
    ))

    unavailable = []

    for result in cross.get("database_results") or []:
        key = result.get("database")
        status = result.get("status")

        if status != "OK":
            print("{:<5} {:<28} {}".format(
                key, DATABASE_LABELS.get(key, ""), status
            ))
            unavailable.append((key, status, result.get("reason")))

            continue

        print("{:<5} {:<28} {:<26} {:>8}  {}".format(
            key,
            DATABASE_LABELS.get(key, ""),
            str(result.get("best_material"))[:26],
            score(result.get("best_similarity")),
            result.get("class_reliability"),
        ))

    # A database that could not be used says why, in full, below the
    # table rather than inside a column it would destroy.
    for key, status, reason in unavailable:
        if not reason:
            continue

        print()
        print("  {} {}:".format(key, status))

        for line in textwrap.wrap(str(reason), 66):
            print("    {}".format(line))

    # Which calibration each database saw. It differs by design, and a
    # reader who does not know that would mistrust the numbers.
    print()

    for result in cross.get("database_results") or []:
        if result.get("normalization"):
            print("  {:<5} normalized with {}{}".format(
                result.get("database"),
                result.get("normalization"),
                "  (narrowed to the 18 WHITE bands)"
                if result.get("comparison_mode") == "PROJECTED_TO_18" else "",
            ))

    for result in cross.get("database_results") or []:
        if result.get("status") != "OK":
            continue

        matches = result.get("matches") or []

        if not matches:
            continue

        print()
        print("  {} - top {}".format(result.get("database"), limit))

        for match in matches[:limit]:
            print("    {:<3} {:<30} {:>8}   rmse {}".format(
                match.get("combined_rank", match.get("rank")),
                str(match.get("material"))[:30],
                score(match.get("cosine_similarity_percent")),
                "{:.4f}".format(match["rmse"])
                if match.get("rmse") is not None else "-",
            ))

    consensus = cross.get("consensus") or {}
    confidence = cross.get("confidence") or {}

    print()
    print("CONSENSUS")
    print()
    print("  Level:      {}".format(consensus.get("level")))
    print("  Material:   {}".format(consensus.get("material") or "-"))

    if consensus.get("nearest_material"):
        print("  Nearest:    {} (not named as the answer)".format(
            consensus["nearest_material"]
        ))

    print("  Family:     {}".format(consensus.get("family") or "-"))
    print("  Supported by: {}".format(
        ", ".join(consensus.get("supporting_databases") or []) or "-"
    ))

    if consensus.get("reason"):
        print("  Reason:     {}".format(consensus["reason"]))

    for entry in consensus.get("discounted_for_low_reliability") or []:
        print("  DISCOUNTED: {} said {} - {}".format(
            entry.get("database"),
            entry.get("best_material"),
            entry.get("reason"),
        ))

    for entry in consensus.get("disagreements") or []:
        print("  DISAGREES:  {} says {} ({})".format(
            entry.get("database"),
            entry.get("best_material"),
            score(entry.get("best_similarity")),
        ))

    print()
    print("  Confidence: {}".format(confidence.get("level")))

    for factor in confidence.get("factors") or []:
        if factor.get("verdict") == "PASS":
            continue

        print("    [{}] {}: {}".format(
            factor.get("verdict"), factor.get("factor"), factor.get("detail")
        ))

    print("    Confidence is an engineering judgement from the structure")
    print("    of the evidence, NOT a probability.")

    mixture = cross.get("mixture")

    if mixture:
        print()
        print("MIXTURE ANALYSIS  ({})".format(mixture.get("database")))
        print()
        print("  Status: {}".format(mixture.get("status")))

        for component in mixture.get("components") or []:
            print("    {:<30} spectral contribution {:.3f}".format(
                str(component.get("material"))[:30],
                component.get("spectral_contribution", 0.0),
            ))

        if mixture.get("components"):
            print()
            print("  Spectral contribution is NOT mass fraction. Converting")
            print("  one to the other needs prepared mixtures of known")
            print("  mass, which do not exist for this instrument.")


DECISION_HEADLINE = {
    "KNOWN_MATERIAL": "KNOWN MATERIAL",
    "MATERIAL_FAMILY": "MATERIAL FAMILY",
    "AMBIGUOUS_SET": "AMBIGUOUS SET",
    "UNKNOWN": "UNKNOWN",
}


def print_evidence_summary(package):
    """The measurement half: what the instrument actually supports."""
    if not package:
        return

    quality = package.get("quality") or {}
    reliability = package.get("channel_reliability") or {}

    hardware = (quality.get("hardware") or {}).get("status")
    normalization = (quality.get("normalization") or {}).get("status")

    print("MEASUREMENT")
    print()
    print("  Hardware QC:    {}".format(hardware))
    print("  Normalization:  {}".format(normalization))
    print("  Reliable features: {}/{} raw, {}/{} usable as reflectance".format(
        reliability.get("raw_valid_total"),
        reliability.get("features_total"),
        reliability.get("normalized_valid_total"),
        reliability.get("features_total"),
    ))

    for illumination in ILLUMINATIONS:
        entry = (reliability.get("by_illumination") or {}).get(illumination)

        if not entry:
            continue

        print("    {:<6} raw {}/18, reflectance {}/18".format(
            illumination.upper(),
            entry["raw_valid_channels"],
            entry["normalized_valid_channels"],
        ))

    if hardware != "PASS" or normalization != "OK":
        print()

        for line in textwrap.wrap(
            channel_reliability.summarize(reliability), 66
        ):
            print("  {}".format(line))


def print_decision(decision, detail=False):
    """
    The conclusion, in the four-level vocabulary and nothing else.

    Deliberately compact by default: the operator needs the level, the
    answer and the confidence at a glance, and everything behind it on
    request.
    """
    if not decision:
        return

    level = decision.get("level")

    print("DECISION MODEL   {}".format(
        decision.get("decision_model_version")
    ))
    print()
    print("  Level:       {}".format(DECISION_HEADLINE.get(level, level)))

    if decision.get("material"):
        print("  Material:    {}".format(decision["material"]))

    if decision.get("family"):
        print("  Family:      {}".format(decision["family"]))

    print("  Confidence:  {}".format(decision.get("confidence")))

    candidates = decision.get("candidates") or []

    if candidates:
        print()
        print("  Candidates:")

        for index, candidate in enumerate(candidates, start=1):
            print("    {}. {:<30} {:<7} {}".format(
                index,
                str(candidate.get("material"))[:30],
                candidate.get("evidence_level", "-"),
                candidate.get("family") or "",
            ))

    secondary = decision.get("secondary_interpretations") or []

    if secondary:
        print()
        print("  Also: {}".format(", ".join(secondary)))

    if decision.get("reason"):
        print()

        for line in textwrap.wrap("Why: {}".format(decision["reason"]), 66):
            print("  {}".format(line))

    if not detail:
        return

    print()
    print("  EVIDENCE")

    evidence = decision.get("evidence") or {}

    for key, entry in sorted((evidence.get("databases") or {}).items()):
        print()
        print("    {} (weight {})".format(key, entry.get("database_weight")))

        for family, summary in sorted((entry.get("families") or {}).items()):
            print("      {:<15} {:<28} margin {} z {}".format(
                family,
                str(summary.get("winner"))[:28],
                (
                    "{:.4f}".format(summary["absolute_margin"])
                    if summary.get("absolute_margin") is not None else "-"
                ),
                (
                    "{:.1f}".format(summary["z_separation"])
                    if summary.get("z_separation") is not None else "-"
                ),
            ))

    for entry in evidence.get("discounted") or []:
        print()
        print("    DISCOUNTED {} said {} - {}".format(
            entry["database"], entry["material"], entry["reason"]
        ))

    unknown = evidence.get("unknown_detection") or {}

    if unknown.get("reasons"):
        print()
        print("    DOUBTS")

        for reason in unknown["reasons"]:
            print("      [{}] {}".format(reason["severity"], reason["code"]))

    print()
    print("  EXPLANATION")
    print()

    for line in textwrap.wrap(decision.get("explanation") or "", 66):
        print("    {}".format(line))

    print()
    print("  PROVENANCE")
    print()

    provenance = decision.get("provenance") or {}

    for key in ("decision_model_version", "evidence_schema_version",
                "calibration_id", "legacy_calibration_id",
                "acquisition_profile_id", "class_statistics_snapshot"):
        print("    {:<28} {}".format(key, provenance.get(key)))

    for key, entry in sorted(
        (provenance.get("database_versions") or {}).items()
    ):
        print("    {:<28} {} ({}, {} materials)".format(
            key, entry.get("version"), entry.get("status"),
            entry.get("materials"),
        ))


def offer_decision_detail(result):
    """'Why?' - the full evidence, on request."""
    decision = (result or {}).get("decision")

    if not decision:
        return

    print()

    if choose("[w] Why?  [Enter] continue").strip().lower() != "w":
        return

    print()
    print_decision(decision, detail=True)
    print()
    pause()


# ----------------------------------------------------------------------
# ground truth capture
# ----------------------------------------------------------------------

def capture_ground_truth(mission, measurement_id, result, save_prompt=True):
    """
    Ask what the sample actually was, and record it if the operator knows.

    NEVER REQUIRED. A rover measurement of unknown soil is the normal
    case, and refusing to store it without a label would throw away the
    observations the system most needs. The question is offered, and "I
    do not know" is a first-class answer that is stored as such. §59.

    The operator's answer is the ONLY source of ground truth. The model's
    own conclusion is never offered as a default, never pre-selected, and
    the learning store refuses it outright if anything tries. §46.
    """
    if mission.learning is None:
        return None

    if save_prompt:
        print()

        if not confirm("Save this measurement to the learning history?"):
            return None

    print()
    print("Ground truth available?")
    print()
    print("  [1] Yes - exact material known")
    print("  [2] Material family known")
    print("  [3] Known prepared mixture")
    print("  [4] Unknown sample")
    print("  [5] Save the measurement without a label")
    print()

    answer = choose("Select")

    if answer not in ("1", "2", "3", "4", "5"):
        print("Not saved.")

        return None

    label_type = {
        "1": "EXACT_MATERIAL",
        "2": "MATERIAL_FAMILY",
        "3": "PREPARED_MIXTURE",
        "4": "UNKNOWN_SAMPLE",
        "5": "NO_LABEL",
    }[answer]

    material = None
    family = None
    mixture = None

    if answer == "1":
        material = ask_material(mission)

        if material is None:
            print("Cancelled; nothing was saved.")

            return None

        family = material.family_id

    elif answer == "2":
        family = ask_family(mission)

        if family is None:
            print("Cancelled; nothing was saved.")

            return None

    elif answer == "3":
        mixture = ask_mixture(mission)

        if not mixture:
            print("Cancelled; nothing was saved.")

            return None

    status, source, certainty = ask_verification(answer)

    try:
        record = mission.record_observation(
            measurement_id, result,
            label_type=label_type,
            material=material,
            family_id=family,
            mixture=mixture,
            verification_status=status,
            verification_source=source,
            certainty=certainty,
        )

    except Exception as error:
        print()
        print("NOT SAVED: {}".format(error))

        return None

    print()
    print("Saved to the learning history as {}.".format(measurement_id))
    print("The reference databases were not modified.")

    return record


def ask_material(mission):
    """Resolve a material name through the controlled vocabulary."""
    taxonomy = mission.taxonomy

    if taxonomy is None:
        print("The material vocabulary is not loaded.")

        return None

    while True:
        text = ask("Material (name, alias or blank to cancel)")

        if not text:
            return None

        identity = taxonomy.get(text)

        if identity is not None:
            print("  -> {} ({})".format(
                identity.display_name, identity.family_id or "no family"
            ))

            if confirm("Is that right?"):
                return identity

            continue

        suggestions = taxonomy.suggest(text)

        print()
        print("'{}' is not a known material.".format(text))

        if suggestions:
            print("Did you mean:")

            for index, name in enumerate(suggestions, start=1):
                print("  [{}] {}".format(index, name))

            choice = ask("Number, or another name")

            if choice.isdigit() and 1 <= int(choice) <= len(suggestions):
                return taxonomy.get(suggestions[int(choice) - 1])

        print()
        print("A ground-truth label is never guessed: an unknown name is")
        print("refused rather than attached to the nearest match.")


def ask_family(mission):
    families = sorted(mission.taxonomy.families()) if mission.taxonomy else []

    if not families:
        return None

    print()

    for index, name in enumerate(families, start=1):
        print("  [{}] {}".format(index, name))

    choice = ask("Family number (blank = cancel)")

    if choice.isdigit() and 1 <= int(choice) <= len(families):
        return families[int(choice) - 1]

    return None


def ask_mixture(mission):
    """
    Components of a PREPARED mixture, with their prepared fractions.

    Only for a mixture the operator actually made and knows the
    composition of. A mixture guessed from the spectrum is not ground
    truth, and there is deliberately no way to enter one.
    """
    components = []

    print()
    print("Enter each component of the PREPARED mixture. Blank to finish.")

    while True:
        identity = ask_material(mission)

        if identity is None:
            break

        fraction = ask_float(
            "Prepared mass fraction of {} (0-1)".format(
                identity.display_name
            ),
            0.0, 1.0,
        )

        if fraction is None:
            break

        components.append({
            "material_key": identity.key,
            "material_id": identity.material_id,
            "prepared_mass_fraction": fraction,
        })

    return components or None


def ask_verification(answer):
    """How much the label is worth, asked rather than assumed."""
    if answer in ("4", "5"):
        return "UNKNOWN", "operator", None

    print()
    print("How is that known?")
    print()
    print("  [1] VERIFIED - a labelled reference material, measured")
    print("      deliberately")
    print("  [2] OPERATOR_ASSERTED - believed, but not from a labelled")
    print("      container")
    print("  [3] UNVERIFIED - a guess. Recorded, never trained on.")
    print()

    choice = choose("Select") or "2"

    return {
        "1": ("VERIFIED", "operator_known_reference_material", 1.0),
        "2": ("OPERATOR_ASSERTED", "operator_assertion", None),
        "3": ("UNVERIFIED", "operator_guess", None),
    }.get(choice, ("OPERATOR_ASSERTED", "operator_assertion", None))


def menu_learning_history(mission):
    """What the system has seen, and how often it has been right."""
    banner("DECISION LEARNING HISTORY")

    if mission.learning is None:
        print("The learning database is not available:")
        print("  {}".format(mission.learning_error))
        print()
        pause()

        return

    status = mission.learning.status()

    print("Database: {}".format(status["file"]))
    print()
    print("Observations:      {}".format(status["observations"]))
    print("Labelled:          {}".format(status["labelled"]))

    for level, count in status["by_verification"].items():
        print("  {:<18} {}".format(level, count))

    print("Predictions:       {}".format(status["predictions"]))
    print("Model versions:    {}".format(
        ", ".join(status["model_versions"]) or "-"
    ))

    print()
    print("VERIFIED MATERIALS")
    print()

    if status["verified_materials"]:
        for material, count in sorted(status["verified_materials"].items()):
            print("  {:<34} {} independent measurement(s)".format(
                material[:34], count
            ))

    else:
        print("  none yet")

    print()
    print("CONFUSION HISTORY  (verified truth vs what a model said)")
    print()

    rows = mission.learning.confusion()

    if rows:
        for row in rows:
            outcome = (
                "correct" if row["predicted"] == row["actual"]
                else "no answer" if not row["predicted"] else "WRONG"
            )

            print("  {:<12} {:<26} -> {:<26} {}".format(
                row["model_version"][:12],
                str(row["actual"])[:26],
                str(row["predicted"])[:26],
                outcome,
            ))

    else:
        print("  no model has been scored against verified truth yet")

    print()
    print("A prediction is never ground truth. Retraining is an explicit")
    print("command, never a consequence of measuring.")
    print()
    pause()


def print_result_block(analysis):
    print("Best match:   {}".format(analysis.get("best_match")))
    print("Similarity:   {}".format(score(analysis.get("best_similarity"))))
    print("Second match: {}".format(analysis.get("second_match")))
    print("Difference:   {}".format(score(analysis.get("score_difference"))))
    print("Status:       {}".format(analysis.get("status")))
    print()
    print("Conclusion:")
    print("  {}".format(analysis.get("automatic_conclusion")))


def print_settings_block(settings):
    settings = settings or {}

    mode = settings.get("measurement_mode")
    mode_name = settings.get("measurement_mode_name")

    print("Mode:               {}{}".format(
        mode, " {}".format(mode_name) if mode_name else ""
    ))
    print("Integration cycles: {}".format(settings.get("integration_cycles")))
    print("Gain:               {}".format(settings.get("gain_x")))

    currents = settings.get("led_currents_ma")

    if currents:
        print("WHITE current:      {}".format(currents.get("white")))
        print("UV current:         {}".format(currents.get("uv")))
        print("IR current:         {}".format(currents.get("ir")))

    else:
        print("LED current:        {}".format(
            settings.get("led_current_ma", settings.get("led_current"))
        ))


# ======================================================================
# screens: sensor test
# ======================================================================

ESP32_STAGE_LABELS = (
    ("SENSOR_RECOVERY", "Sensor recovery"),
    ("I2C_ADDRESS", "I2C 0x49"),
    ("INTERNAL_DEVICES", "Internal devices"),
    ("CONFIGURATION", "Configuration"),
    ("ILLUMINATION", "Illumination"),
    ("ACQUISITION", "18-channel acquisition"),
)


def print_check(label, ok):
    print("{:<27}{}".format(label, "PASS" if ok else "FAIL"))


def print_calibration_health(mission):
    """
    The two calibrations, at the top of every sensor test.

    The operator has to be able to see at a glance which calibration a
    result was produced under - and be told plainly when the active one
    is missing rather than quietly falling back.
    """
    health = mission.calibration_health()

    print("Calibration:")
    print("  Active full calibration: {}{}".format(
        health["active"],
        "  {}".format(health["active_id"]) if health["active_id"] else "",
    ))
    print("  Legacy DB calibration:   {}{}".format(
        health["legacy"],
        "  {}".format(health["legacy_id"]) if health["legacy_id"] else "",
    ))

    if health["active"] != "PASS":
        print()
        print("  No usable full spectral calibration.")
        print("  UV and IR reflectance will not be computed.")

        if health["stored"]:
            print("  {} calibration(s) are stored: [7] Select Which".format(
                health["stored"]
            ))
            print("  Calibration To Use, or [3] to make a new one.")
        else:
            print("  Run [5] Sensor Test -> [3] Full Spectral Calibration.")

    return health


def menu_sensor_test(mission):
    """The engineering submenu: test, calibrate, inspect."""
    while True:
        banner("SENSOR TEST / CALIBRATION")

        print_calibration_health(mission)

        print()
        print("[1] Full Sensor + Analysis Test")
        print("    Run the complete production measurement pipeline")
        print("    without saving a Sample.")
        print()
        print("[2] LED / Illumination Test")
        print("    Test WHITE, UV and IR illumination independently.")
        print()
        print("[3] Full Spectral Calibration")
        print("    Create a new complete Dark + WHITE/UV/IR calibration.")
        print()
        print("[4] Show Active Calibration")
        print("[5] Validate Active Calibration")
        print("[6] Calibration History")
        print()
        print("[7] Select Which Calibration To Use")
        print("    Choose from every calibration ever made. Nothing new")
        print("    is measured.")
        print()
        print("[0] Back")

        selection = choose()

        try:
            if selection == "0":
                return

            if selection == "1":
                menu_full_sensor_test(mission)

            elif selection == "2":
                menu_led_test(mission)

            elif selection == "3":
                menu_full_calibration(mission)

            elif selection == "4":
                show_active_calibration(mission)

            elif selection == "5":
                validate_active_calibration(mission)

            elif selection == "6":
                show_calibration_history(mission)

            elif selection == "7":
                menu_select_calibration(mission)

            elif selection:
                print("Unknown option.")

        except (LinkError, TimeoutError) as error:
            report_failure(error)
            pause()

        except CalibrationError as error:
            print()
            print("Calibration error: {} - {}".format(
                error.code, error.message
            ))
            pause()


# ----------------------------------------------------------------------
# [1] full sensor + analysis test
# ----------------------------------------------------------------------

def menu_full_sensor_test(mission):
    """
    The complete production pipeline, saving nothing.

    Deliberately the SAME code path a real measurement uses: the ESP32
    acquires WHITE/UV/IR through acquire_triad, the PC normalizes it
    both ways, quality control runs, and the legacy comparison ranks
    every material. A pass here means a measurement will work.
    """
    banner("SENSOR + ANALYSIS TEST")

    health = print_calibration_health(mission)

    print()
    print("ESP32 HARDWARE")
    print()

    try:
        data = mission.link.sensor_test_raw()

    except (LinkError, TimeoutError) as error:
        print_check("Serial communication", False)
        print()
        print("FAILED STAGE  SERIAL_LINK")

        if isinstance(error, LinkError):
            print("error code    {}".format(error.code))
            print("message       {}".format(error.message))
        else:
            print("message       {}".format(error))

        print()
        pause()

        return

    print_check("Serial communication", True)

    checks = {entry["stage"]: entry for entry in data.get("checks") or []}

    for stage, label in ESP32_STAGE_LABELS:
        entry = checks.get(stage)

        if entry is None:
            print("{:<27}{}".format(label, "SKIPPED"))
        else:
            print_check(label, entry.get("ok"))

    settings = data.get("sensor_settings")
    blocks = data.get("illuminations")

    if not blocks:
        print()
        print("FAILED STAGE  {}".format(data.get("failed_stage")))

        for entry in data.get("checks") or []:
            if entry.get("ok"):
                continue

            error = entry.get("error") or {}
            print("error code    {}".format(error.get("code")))
            print("message       {}".format(error.get("message")))

            if error.get("details"):
                print("details       {}".format(
                    json.dumps(error["details"])[:300]
                ))

        if settings:
            print()
            print("SETTINGS")
            print()
            print_settings_block(settings)

        print()
        print("TEST ONLY - NOTHING SAVED")
        print()
        pause()

        return

    print()
    print("ACQUISITION")
    print()

    for name in ILLUMINATIONS:
        block = blocks.get(name) or {}
        print("{:<20}{} repeat(s), {}/18 channels".format(
            "{} illumination".format(name.upper()),
            block.get("repeats"),
            len((block.get("acquisitions") or [{}])[0]),
        ))

    print("{:<20}{}".format(
        "All lamps off", "YES" if data.get("bulbs_off") else "NO"
    ))

    if mission.references is None:
        print()
        print("FAILED STAGE  BD_REFERENCES")
        print("message       {}".format(mission.science_error))
        print()
        print("TEST ONLY - NOTHING SAVED")
        print()
        pause()

        return

    try:
        result = mission.analyse_raw(data, settings)

    except Exception as error:
        print()
        print("FAILED STAGE  BD_ANALYSIS")
        print("error code    {}".format(type(error).__name__))
        print("message       {}".format(error))
        print()
        print("TEST ONLY - NOTHING SAVED")
        print()
        pause()

        return

    print()
    print("MEASUREMENT QUALITY")
    print()
    print_quality(result["quality"])

    print()
    print("SETTINGS")
    print()
    print_settings_block(result["measurement"].get("sensor_settings"))

    print()
    print("FULL SPECTRAL DATA")
    print()
    print_triad_table(result["measurement"])

    if health["active"] != "PASS":
        print()
        print("Active (UV/IR) reflectance columns are absent because no")
        print("full spectral calibration is active.")

    print()
    print("LEGACY DATABASE COMPARISON")
    print("  normalized with {}".format(
        result["calibration"]["legacy_database_calibration_id"]
    ))
    print()
    print_metric_table(result["reference_matches"], limit=8)

    print()
    print_agreement(result.get("metric_agreement"))

    print()
    print("=" * 60)
    print()
    print("THREE-DATABASE COMPARISON")
    print()
    print_cross_database(result.get("cross_database"))

    print()
    print("=" * 60)
    print()

    if result.get("evidence"):
        print_evidence_summary(result["evidence"])
        print()
        print_decision(result.get("decision"))

    else:
        print("DECISION MODEL")
        print()
        print("  Not run: {}".format(
            result.get("evidence_error")
            or "no active calibration, so no evidence package could be "
               "built"
        ))

    print()
    print("=" * 60)
    print()
    print("RESULT  (DB1, legacy calibration - the previous pipeline)")
    print()
    print_result_block(result["analysis"])

    print()
    print("TEST ONLY - NOTHING SAVED")

    offer_decision_detail(result)

    if mission.learning is not None and result.get("evidence"):
        capture_ground_truth(
            mission,
            "TEST_{}".format(utc_timestamp().replace(":", "").replace(
                "-", "")[:15]),
            result,
        )

    print()
    pause()


# ----------------------------------------------------------------------
# [2] LED / illumination test
# ----------------------------------------------------------------------

def menu_led_test(mission):
    """Each lamp on its own, with the state read back from the register."""
    banner("LED / ILLUMINATION TEST")

    print("Each lamp is switched on alone, held briefly, then switched")
    print("off. The enable bit is read back at every step.")
    print()

    try:
        data = mission.link.led_test()

    except (LinkError, TimeoutError) as error:
        report_failure(error)
        print()
        pause()

        return

    for entry in data.get("lamps") or []:
        name = str(entry.get("illumination", "?")).upper()

        print("{:<12}{}{}".format(
            "{} LED:".format(name),
            "PASS" if entry.get("ok") else "FAIL",
            "   {}".format(entry.get("current_ma"))
            if entry.get("current_ma") else "",
        ))

        if not entry.get("ok"):
            error = entry.get("error") or {}
            print("             stage   {}".format(error.get("stage")))
            print("             code    {}".format(error.get("code")))
            print("             message {}".format(error.get("message")))

    print()
    print("{:<12}{}".format(
        "ALL LEDs OFF:", "PASS" if data.get("all_off") else "FAIL"
    ))

    states = data.get("final_states") or {}

    if states and not data.get("all_off"):
        print("  still on: {}".format(
            ", ".join(name for name, on in states.items() if on) or "unknown"
        ))

    print()
    print("Overall: {}".format("PASS" if data.get("ok") else "FAIL"))
    print()
    pause()


# ----------------------------------------------------------------------
# [3] full spectral calibration
# ----------------------------------------------------------------------

def _acquire_calibration_block(mission, illumination, repeats, label):
    """One calibration block, with per-acquisition progress."""
    print()
    print("{} acquisition, {} repeats...".format(label, repeats))
    sys.stdout.flush()

    block = mission.link.acquire_block(illumination, repeats)

    taken = len(block.get("acquisitions") or [])

    for index in range(1, taken + 1):
        print("  {} {}/{}".format(label, index, taken))

    if not block.get("bulbs_off", True):
        print("  WARNING: a lamp was still on after the block.")

    return aggregation.aggregate_block(block)


def _acquire_with_retry(mission, illumination, repeats, label):
    """
    One calibration block, retried as often as the operator wants.

    A calibration is four acquisitions taken against two physical
    setups, and it used to be abandoned in full the moment any one of
    them failed - after which the operator had to reinstall the White
    target and start again from the Dark. A failed block is almost
    always a transport fault, so the block itself is what gets repeated.

    Returns the aggregated block, or None if the operator gives up.
    """
    while True:
        try:
            return _acquire_calibration_block(
                mission, illumination, repeats, label
            )

        except (LinkError, TimeoutError) as error:
            print()
            print("{}_CALIBRATION_FAILED".format(illumination.upper()))
            report_failure(error)
            print()
            print("Nothing else is lost: this block alone is repeated, and")
            print("the reference target must NOT be moved.")
            print()

            if not confirm("Try this block again?"):
                return None


def _print_block_summary(title, aggregated):
    statistics = aggregated["statistics"]
    spectrum = aggregated["spectrum"]

    print()
    print(title)
    print()
    print("CH   {:>10} {:>10} {:>10} {:>9}".format(
        "Median", "Mean", "StdDev", "CV"
    ))

    for channel in sample_analysis.CHANNELS:
        summary = statistics["channels"].get(channel) or {}
        cv = summary.get("cv")

        print("{:<4} {:>10} {:>10} {:>10} {:>9}".format(
            channel,
            number(spectrum.get(channel)),
            number(summary.get("mean")),
            number(summary.get("stdev")),
            "{:.3%}".format(cv) if cv is not None else "-",
        ))

    print()
    print("  repeats {}, rejected {}, unstable channels {}".format(
        statistics.get("repeats"),
        statistics.get("rejected_total"),
        len(statistics.get("unstable_channels") or []),
    ))


def menu_full_calibration(mission):
    """
    Guided Dark + WHITE/UV/IR calibration.

    Creates a NEW calibration file. It never touches calibration_legacy.json or
    DB1.json, and it does not become active until the operator
    confirms after seeing the validation.
    """
    banner("FULL SPECTRAL CALIBRATION")

    health = mission.calibration_health()

    print("Current active calibration:")
    print("  ID:      {}".format(health["active_id"] or "none"))
    print("  Status:  {}".format(health["active"]))

    if mission.active_calibration is not None:
        print("  Created: {}".format(mission.active_calibration.created_at))

    print()
    print("Legacy DB calibration:")
    print("  ID:        {}".format(health["legacy_id"]))
    print("  Protected: YES")
    print()
    print("A new calibration will NOT modify the legacy database")
    print("calibration, calibration_legacy.json or DB1.json. The existing")
    print("material library stays valid and does not need remeasuring.")

    stored = health["stored"]

    if stored:
        print()
        print("{} calibration(s) are already stored. A new one is only".format(
            stored
        ))
        print("needed if the optics, the lamps or the sensor settings have")
        print("changed - otherwise use [7] Select Which Calibration To Use.")

    print()

    if not confirm("Continue?"):
        print("Cancelled.")

        return

    repeats = ask_int(
        "Repeats per block", 2, 25, default=bd_config_repeats()
    )

    if repeats is None:
        print("Cancelled.")

        return

    # -- step 1: dark --------------------------------------------------
    banner("STEP 1/2 - DARK REFERENCE")

    print("Remove the sample, close the optical path, and make sure no")
    print("light reaches the measurement target.")
    print()
    print("All WHITE, UV and IR LEDs will remain OFF. Dark is the")
    print("detector's own response with no illumination at all.")
    print()

    ask("Press Enter when ready")

    dark = _acquire_with_retry(mission, "dark", repeats, "Dark")

    if dark is None:
        print()
        print("Calibration abandoned at the Dark step. Nothing was saved")
        print("and the active calibration is unchanged.")
        print()
        pause()

        return

    _print_block_summary("DARK REFERENCE", dark)

    dark_unstable = len(
        dark["statistics"].get("unstable_channels") or []
    )
    dark_missing = dark["statistics"].get("missing_channels") or []

    dark_ok = not dark_missing and dark_unstable <= 8

    print()
    print("Dark quality: {}".format("PASS" if dark_ok else "FAIL"))

    if not dark_ok:
        print()

        if dark_missing:
            print("Missing channels: {}".format(",".join(dark_missing)))

        print("The Dark reference is not usable. Calibration stopped")
        print("before the White step - a bad Dark would corrupt every")
        print("reflectance computed against it.")
        print()
        pause()

        return

    # -- step 2: white target ------------------------------------------
    banner("STEP 2/2 - WHITE REFERENCE")

    print("Install the diffuse White reference target in the exact")
    print("sample measurement position.")
    print()
    print("Do not move the sensor. The three illuminations are measured")
    print("one after another against the same target.")
    print()

    ask("Press Enter when ready")

    white_blocks = {}

    for name in ILLUMINATIONS:
        block = _acquire_with_retry(
            mission, name, repeats, "{} illumination".format(name.upper())
        )

        if block is None:
            print()
            print("Calibration abandoned at the {} step. The {} block(s)".format(
                name.upper(), len(white_blocks)
            ))
            print("already acquired are discarded: a calibration is only")
            print("meaningful if Dark and all three illuminations were")
            print("measured against the same target in the same session.")
            print()
            pause()

            return

        white_blocks[name] = block

    for name in ILLUMINATIONS:
        _print_block_summary(
            "{} WHITE REFERENCE".format(name.upper()), white_blocks[name]
        )

    # -- build and validate --------------------------------------------
    try:
        status = mission.hardware_status()
        settings = (status.get("sensor") or {}).get("settings") or {}

    except (LinkError, TimeoutError):
        settings = {}

    document = build_calibration(
        dark, white_blocks, settings, repeats
    )

    result = validate_calibration(document, settings)
    document["validation"] = result

    banner("CALIBRATION VALIDATION")

    _print_validation(document, result)

    print()
    print("New calibration ID:")
    print("  {}".format(document["calibration_id"]))
    print()

    if result["status"] == "FAIL":
        print("This calibration did NOT pass validation and cannot be")
        print("activated.")
        print()

        if confirm("Keep it on disk as engineering data?"):
            path = mission.calibrations.save(document)
            print("Saved (inactive, marked invalid): {}".format(path))

        else:
            print("Discarded.")

        print()
        pause()

        return

    path = mission.calibrations.save(document)
    print("Saved: {}".format(path))
    print()

    if not confirm("Activate this calibration?"):
        print("Saved but NOT activated. The previous calibration is still")
        print("in force.")
        print()
        pause()

        return

    # The validator is injected: BD stores calibrations but must not
    # import the science layer, so PC supplies the scientific judgement
    # that decides whether this one may become active. See ARCHITECTURE.md.
    try:
        mission.calibrations.activate(
            document["calibration_id"], validate_calibration
        )

    except CalibrationError as error:
        print()
        print("!! ACTIVATION REFUSED: {}".format(error.message))
        print("   The calibration is saved but the previous one is still")
        print("   in force.")
        print()
        pause()

        return

    mission.load_science()

    print()
    print("Active calibration is now {}.".format(
        document["calibration_id"]
    ))
    print("The legacy database calibration is unchanged.")
    print()
    pause()


def bd_config_repeats():
    """
    Default repeats per calibration block.

    This used to read CALIBRATION_REPEATS off the BD config with a
    fallback, but that constant only ever existed in ESP32/config.py, so
    the lookup always missed and the fallback was the real value. It is
    now a named host-side default.
    """
    return science_config.DEFAULT_CALIBRATION_REPEATS


def _print_validation(document, result):
    dark_statistics = (document.get("dark") or {}).get("statistics") or {}
    references = document.get("white_reference") or {}

    print("Dark:")
    print("  {}/18 channels valid".format(
        18 - len(dark_statistics.get("missing_channels") or [])
    ))
    print("  repeatability: {}".format(
        "PASS" if len(dark_statistics.get("unstable_channels") or []) <= 3
        else "WARNING"
    ))

    for name in ILLUMINATIONS:
        statistics = (references.get(name) or {}).get("statistics") or {}

        print()
        print("{} illumination:".format(name.upper()))
        print("  {}/18 channels".format(
            18 - len(statistics.get("missing_channels") or [])
        ))
        print("  repeatability: {}".format(
            "PASS" if len(statistics.get("unstable_channels") or []) <= 3
            else "WARNING"
        ))

    settings = document.get("sensor_settings") or {}

    print()
    print("Sensor settings:")
    print("  Mode:        {} [{}]".format(
        settings.get("measurement_mode"),
        "PASS" if settings.get("measurement_mode") == 3 else "FAIL",
    ))
    print("  Gain:        {} [{}]".format(
        settings.get("gain_x"),
        "PASS" if settings.get("gain") == 2 else "FAIL",
    ))
    print("  Integration: {} [{}]".format(
        settings.get("integration_cycles"),
        "PASS" if settings.get("integration_cycles") == 100 else "FAIL",
    ))

    print()
    print("Overall calibration: {}".format(result["status"]))

    for failure in result["failures"]:
        print("  [FAIL]    {}".format(failure["message"]))

    for warning in result["warnings"]:
        print("  [WARNING] {}".format(warning["message"]))


# ----------------------------------------------------------------------
# [4] [5] [6] calibration inspection
# ----------------------------------------------------------------------

def show_active_calibration(mission):
    banner("ACTIVE FULL CALIBRATION")

    calibration = mission.active_calibration

    if calibration is None:
        print("No full spectral calibration is active.")
        print()
        print("Reason: {}".format(
            mission.calibration_error or "none has been created"
        ))
        print()
        print("Run [3] Full Spectral Calibration.")

    else:
        status = calibration.status()

        print("Calibration ID:  {}".format(status["calibration_id"]))
        print("Created:         {}".format(status["created_at"]))
        print("Schema version:  {}".format(status["schema_version"]))
        print("File:            {}".format(status["file"]))
        print("Repeats:         {}".format(status["repeats"]))
        print("Validation:      {}".format(status["validation"]))
        print()
        print("Dark channels:            {}/18".format(
            status["dark_channels"]
        ))

        for name in ILLUMINATIONS:
            print("{:<26}{}/18".format(
                "{}-reference channels:".format(name.upper()),
                status["white_channels"][name],
            ))

        print()
        print_settings_block(status["sensor_settings"])

    print()
    print("=" * 60)
    print()
    print("LEGACY DATABASE CALIBRATION")
    print()

    if mission.references is None:
        print("NOT LOADED: {}".format(mission.science_error))

    else:
        status = mission.references.status()

        print("Calibration ID:  {}".format(status["calibration_id"]))
        print("File:            {}".format(status["file"]))
        print("Protected:       YES - never modified, never regenerated")
        print("Database:        {}".format(status["database"]))
        print("Illumination:    WHITE only (18 reference features)")
        print("Dark channels:   {}/18".format(status["dark_channels"]))
        print("White channels:  {}/18".format(status["white_channels"]))
        print()
        print("This is the ONLY calibration used to compare a measurement")
        print("against DB1.json. It is why the material library does")
        print("not need remeasuring after a new calibration.")

    print()
    pause()


def validate_active_calibration(mission):
    """Software checks against the active calibration. Writes nothing."""
    banner("ACTIVE CALIBRATION VALIDATION")

    if mission.active_calibration is None:
        print("File integrity:       MISSING")
        print()
        print("No full spectral calibration is active. Run")
        print("[3] Full Spectral Calibration.")
        print()
        pause()

        return

    settings = None

    try:
        status = mission.hardware_status()
        settings = (status.get("sensor") or {}).get("settings")

    except (LinkError, TimeoutError):
        print("(ESP32 not reachable - checking the file only.)")
        print()

    document = mission.active_calibration.document
    result = validate_calibration(document, settings)

    def verdict(condition):
        return "PASS" if condition else "FAIL"

    dark_missing = (
        (document.get("dark") or {}).get("statistics") or {}
    ).get("missing_channels") or []

    references = document.get("white_reference") or {}

    print("{:<22}{}".format(
        "File integrity:",
        verdict(document.get("schema_version")
                == bd_config.CALIBRATION_SCHEMA_VERSION),
    ))
    print("{:<22}{}".format(
        "Dark reference:", verdict(not dark_missing)
    ))

    for name in ILLUMINATIONS:
        missing = (
            (references.get(name) or {}).get("statistics") or {}
        ).get("missing_channels") or []

        print("{:<22}{}".format(
            "{} reference:".format(name.upper()), verdict(not missing)
        ))

    settings_ok = not any(
        failure["code"] == "CALIBRATION_INCOMPATIBLE"
        for failure in result["failures"]
    )

    print("{:<22}{}".format("Sensor configuration:", verdict(settings_ok)))
    print("{:<22}{}".format("Overall:", result["status"]))

    for failure in result["failures"]:
        print()
        print("  [FAIL]    {}".format(failure["message"]))

    for warning in result["warnings"]:
        print()
        print("  [WARNING] {}".format(warning["message"]))

    print()
    pause()


def calibration_state(entry):
    """ACTIVE / INVALID / INACTIVE for one stored calibration."""
    if entry.get("active"):
        return "ACTIVE"

    if entry.get("validation") == "FAIL":
        return "INVALID"

    return "INACTIVE"


def print_calibration_list(entries):
    """
    The stored calibrations, with the settings each was made under.

    An ID and a date are not enough to choose between two calibrations -
    what separates them is gain, integration time and how many repeats
    they averaged, so those are on the same line.
    """
    print("{:<4} {:<40} {:<20} {:<7} {:<7} {:<4} {}".format(
        "#", "Calibration ID", "Created", "Gain", "Cycles", "Rep", "Status"
    ))

    for index, entry in enumerate(entries, start=1):
        created = str(entry.get("created_at") or "-")

        print("{:<4} {:<40} {:<20} {:<7} {:<7} {:<4} {}".format(
            index,
            str(entry.get("calibration_id"))[:40],
            created[:19].replace("T", " "),
            str(entry.get("gain_x") or "-"),
            str(entry.get("integration_cycles") or "-"),
            str(entry.get("repeats") or "-"),
            calibration_state(entry),
        ))


def show_calibration_history(mission):
    banner("CALIBRATION HISTORY")

    entries = mission.calibrations.history()

    print("Library: {}".format(mission.calibrations.library_path))
    print("Stored:  {} calibration(s)".format(len(entries)))
    print()

    if entries:
        print_calibration_list(entries)
    else:
        print("No full spectral calibration has been created yet.")

    print()
    print("LEGACY DATABASE CALIBRATION (separate, protected)")
    print()
    print("  {}".format(
        mission.references.calibration_id if mission.references else "-"
    ))
    print("  Never listed above and never selectable: it is the White and")
    print("  Dark DB1 was measured against, and the only calibration DB1")
    print("  may be compared with.")
    print()
    print("Stored calibrations are immutable. [7] Select Which Calibration")
    print("To Use changes which one is in force; it rewrites none of them.")
    print()
    pause()


def menu_select_calibration(mission):
    """
    Choose which stored calibration is in force. Measures nothing.

    The reason this screen exists: a calibration survives a restart, so
    an operator coming back to the instrument should be able to pick
    yesterday's rather than being pushed into making a new one every
    session. Making a new calibration is a deliberate act, not a
    consequence of starting the program.
    """
    while True:
        banner("SELECT WHICH CALIBRATION TO USE")

        entries = mission.calibrations.history()

        if not entries:
            print("No full spectral calibration has been made yet.")
            print()
            print("Run [3] Full Spectral Calibration to create the first")
            print("one. Until then UV and IR reflectance are not computed")
            print("and the legacy DB1 comparison runs on its own.")
            print()
            pause()

            return

        print("Library: {}".format(mission.calibrations.library_path))
        print()
        print_calibration_list(entries)
        print()
        print("A calibration marked INVALID failed its scientific checks")
        print("and cannot be activated. It is kept as engineering data.")
        print()

        choice = ask_int(
            "Calibration to use", 1, len(entries), default=None
        )

        if choice is None:
            print("Cancelled - the active calibration is unchanged.")
            print()
            pause()

            return

        entry = entries[choice - 1]

        if entry.get("active"):
            print()
            print("{} is already active.".format(entry["calibration_id"]))
            print()
            pause()

            return

        # The validator is injected: BD stores calibrations but must not
        # import the science layer, so PC supplies the scientific
        # judgement that decides whether this one may become active.
        try:
            mission.calibrations.activate(
                entry["calibration_id"], validate_calibration
            )

        except CalibrationError as error:
            print()
            print("!! NOT ACTIVATED: {}".format(error.message))
            print("   The previous calibration is still in force.")
            print()
            pause()

            continue

        mission.load_science()

        print()
        print("Active calibration is now {}.".format(
            entry["calibration_id"]
        ))
        print("Created {}.".format(entry.get("created_at")))
        print("The legacy database calibration is unchanged.")
        print()
        pause()

        return


# ======================================================================
# screens: carousel
# ======================================================================

def menu_select_servo(mission, allow_cancel=True):
    """
    Connect the ST3215 and confirm it answers.

    THE FIRST THING option [0] does. It is a deliberate, explicit step
    rather than something that happens on boot: until the UART is open
    and the servo has replied, the firmware refuses to move the carousel
    at all, because a carousel that turns without feedback is a carousel
    with no idea where it is.

    Connecting always invalidates the carousel position on the ESP32.
    Position state cannot survive a reconnect - the servo may have been
    unplugged, moved by hand, or replaced.
    """
    try:
        options = mission.link.get_servo_options()

    except (LinkError, TimeoutError) as error:
        report_failure(error)

        return None

    supported = options.get("supported") or []
    current = (options.get("servo") or {}).get("label")

    banner("CAROUSEL SERVO SELECTION")

    print("Which servo is currently installed?")
    print()

    for index, entry in enumerate(supported, start=1):
        print("[{}] {}".format(index, entry.get("label")))
        print("    {}".format(entry.get("description")))
        print()

    print("Currently selected: {}".format(current or "NOT SELECTED"))
    print()

    if allow_cancel:
        print("[c] Cancel")

    selection = choose()

    if selection == "c" and allow_cancel:
        print("Cancelled; the servo selection is unchanged.")

        return None

    chosen = None

    for index, entry in enumerate(supported, start=1):
        if selection == str(index):
            chosen = entry

    if chosen is None:
        if selection:
            print("Unknown option.")

        return None

    print()
    print("Selecting {}...".format(chosen.get("label")))
    sys.stdout.flush()

    try:
        data = mission.link.select_servo(chosen["type"])

    except (LinkError, TimeoutError) as error:
        report_failure(error)
        print()
        print("Nothing is selected, so carousel movement stays blocked.")
        print()

        # SERVO_NOT_FOUND names four assumptions at once - ID, baud rate,
        # which wire is TX, and whether the servo has power - and tests
        # none of them. The scan tests all four, so it is offered right
        # here rather than left for the operator to find in a submenu.
        if (
            isinstance(error, LinkError)
            and error.code in ("SERVO_NOT_FOUND", "SERVO_UART_TIMEOUT",
                               "SERVO_CHECKSUM_ERROR",
                               "SERVO_PROTOCOL_ERROR")
            and chosen["type"] == "st3215"
        ):
            print("The bus scan can tell you WHICH of those assumptions is")
            print("wrong: it pings every baud rate the ST3215 supports, in")
            print("both pin orders, and moves nothing.")
            print()

            if confirm("Run the servo bus scan now?"):
                menu_servo_bus_scan(mission)

                return None

        pause()

        return None

    servo = data.get("servo") or {}
    capabilities = servo.get("capabilities") or {}

    print()
    print("{} selected.".format(servo.get("label")))
    print()
    print("  Position feedback: {}".format(
        "YES" if capabilities.get("position_feedback") else "NO"
    ))
    print("  Verified movement: {}".format(
        "YES" if capabilities.get("verified_movement") else "NO"
    ))
    print("  Timed positioning: {}".format(
        "YES" if capabilities.get("timed_positioning") else "NO"
    ))
    print("  Telemetry:         {}".format(
        "YES" if capabilities.get("telemetry") else "NO"
    ))
    print()
    print("The carousel position was invalidated by the change - align")
    print("Slot 1 with the loading hole and confirm it.")
    print()
    pause()

    return chosen["type"]


def servo_capabilities(mission, status=None):
    """Capabilities of the active backend, or an empty dict."""
    try:
        status = status or mission.hardware_status()

    except (LinkError, TimeoutError):
        return {}

    return ((status.get("servo") or {}).get("capabilities")) or {}


def servo_selected(mission, status=None):
    try:
        status = status or mission.hardware_status()

    except (LinkError, TimeoutError):
        return False

    return bool((status.get("servo") or {}).get("selected"))


def menu_initial_calibration(mission):
    """
    Establish the carousel origin.

    Two steps, in this order:

        1. state which servo is installed
        2. align physical Slot 1 with the loading hole and confirm it

    The second step is the operator asserting the one fact no actuator can
    measure: which physical slot is called Slot 1. There is no limit
    switch, no Hall sensor and no index mark, so this is required after
    every power-up.

    The confirmation also captures the ST3215's encoder reading, so the
    logical model is tied to a real measurement rather than to a count
    that drifts.

    Neither path writes anything persistent to the servo. Changing the
    ST3215's stored configuration is a separate SERVICE operation under
    Tools.
    """
    try:
        status = mission.hardware_status()

    except (LinkError, TimeoutError) as error:
        report_failure(error)

        return False

    if not servo_selected(mission, status):
        if menu_select_servo(mission) is None:
            return False

    while True:
        try:
            status = mission.hardware_status()

        except (LinkError, TimeoutError) as error:
            report_failure(error)

            return False

        servo = status.get("servo") or {}
        capabilities = servo.get("capabilities") or {}
        backend = servo.get("backend") or {}

        banner("CAROUSEL SETUP")

        print("Servo: {}".format(servo.get("label")))

        if capabilities.get("position_feedback"):
            print("Communication: {}".format(
                "ONLINE" if backend.get("connected") else "NOT ANSWERING"
            ))
            print("Servo ID:      {}".format(backend.get("id")))
            print("Encoder:       {} counts ({} deg)".format(
                backend.get("position_counts"), backend.get("position_deg")
            ))
            print("Mode:          {}".format(backend.get("mode_name")))
            print("Voltage:       {} V".format(backend.get("voltage_v")))
            print("Temperature:   {} C".format(backend.get("temperature_c")))

        else:
            print("Positioning:   timed, open loop (no encoder)")
            print("Signal pin:    GPIO{}".format(backend.get("pin")))

        print()
        print("Goal:")
        print("Align physical Slot 1 exactly under the soil loading hole.")
        print()
        print("[1] Move one whole slot clockwise")
        print("[2] Move one whole slot counter-clockwise")
        print("[3] Fine alignment by degrees (+ = clockwise)")
        print("[4] STOP servo")

        if capabilities.get("torque_control"):
            print("[5] RELEASE torque, to turn the carousel by hand")
            print("[6] HOLD torque again")

        print()
        print("[7] SET CURRENT POSITION AS SLOT 1 / LOAD")
        print()
        print("[s] Change servo")

        if capabilities.get("position_feedback"):
            print("[d] Diagnostics")

        print()
        print("[c] Cancel")

        selection = choose()

        try:
            if selection == "1":
                report_slot_move(mission.link.move_slots("cw", 1))

            elif selection == "2":
                report_slot_move(mission.link.move_slots("ccw", 1))

            elif selection == "3":
                menu_fine_adjust(mission)

            elif selection == "4":
                mission.link.servo_stop()
                print("Servo stopped.")

            elif selection == "5" and capabilities.get("torque_control"):
                if confirm("Release torque? The carousel will turn freely"):
                    mission.link.servo_torque(False)
                    print("Torque released. Turn the carousel by hand, then")
                    print("hold torque again before setting the origin.")

            elif selection == "6" and capabilities.get("torque_control"):
                mission.link.servo_torque(True)
                print("Torque enabled; the carousel is held again.")

            elif selection == "7":
                data = mission.link.sync_load_slot(1)
                carousel = data.get("carousel") or {}
                reference = carousel.get("reference") or {}
                origin = reference.get("origin") or {}

                print()
                print("Carousel setup complete.")
                print("Slot {} = LOADING position".format(
                    carousel.get("current_load_slot")
                ))
                print("Slot {} = SCANNER position".format(
                    carousel.get("current_scan_slot")
                ))

                if origin.get("feedback"):
                    print("Encoder origin: {} counts".format(
                        origin.get("origin_counts")
                    ))

                else:
                    print("Origin: your assertion - this servo has no "
                          "position sensor.")

                print()

                return True

            elif selection == "s":
                menu_select_servo(mission)

            elif selection == "d" and capabilities.get("position_feedback"):
                menu_servo_diagnostics(mission)

            elif selection == "c":
                print("Cancelled; carousel position unchanged.")

                return False

            elif selection:
                print("Unknown option.")

        except (LinkError, TimeoutError) as error:
            report_failure(error)


def report_slot_move(data):
    """What a whole-slot movement did, in whatever terms the backend has."""
    move = (data or {}).get("move") or {}
    servo = move.get("servo") or {}

    if not move.get("moved"):
        print("Nothing moved.")

        return

    print("Moved {} slot(s) {} ({:.0f} deg).".format(
        move.get("steps"), move.get("direction"), move.get("degrees") or 0
    ))

    if servo.get("verified"):
        print("  encoder:   {} -> {} counts".format(
            servo.get("start_position"), servo.get("actual_position")
        ))
        print("  error:     {} counts ({} deg), tolerance {}".format(
            servo.get("position_error"),
            servo.get("position_error_deg"),
            servo.get("tolerance_counts"),
        ))
        print("  elapsed:   {} ms".format(servo.get("elapsed_ms")))

    else:
        print("  commanded: {} ms per slot (timed, not verified)".format(
            servo.get("step_ms")
        ))


def menu_fine_adjust(mission):
    """Small mechanical correction. Does not change slot numbering."""
    banner("FINE CAROUSEL ALIGNMENT")

    capabilities = servo_capabilities(mission)

    print("Positive = clockwise, negative = counter-clockwise.")
    print()
    print("The correction is REMEMBERED: the next slot movement keeps it")
    print("instead of returning the carousel to the old theoretical")
    print("centre.")
    print()

    degrees = ask_float("Degrees", -15.0, 15.0)

    if degrees is None:
        print("Cancelled; carousel not moved.")

        return

    try:
        data = mission.link.fine_adjust(degrees)

    except (LinkError, TimeoutError) as error:
        report_failure(error)

        return

    adjustment = data.get("adjustment") or {}
    reference = (data.get("carousel") or {}).get("reference") or {}

    print()
    print("Requested: {:+.2f} deg".format(degrees))

    if adjustment.get("moved") is False:
        print(adjustment.get("message") or "Nothing moved.")

        return

    if capabilities.get("position_feedback"):
        # Encoder terms. A PWM runtime would be meaningless here.
        print("Commanded: {} counts ({} deg)".format(
            adjustment.get("requested_counts"),
            adjustment.get("commanded_degrees"),
        ))
        print("Encoder:   {} -> {} counts".format(
            adjustment.get("start_position"),
            adjustment.get("actual_position"),
        ))
        print("Error:     {} counts ({} deg), tolerance {} counts".format(
            adjustment.get("position_error"),
            adjustment.get("position_error_deg"),
            adjustment.get("tolerance_counts"),
        ))

    else:
        # Timed terms. There is no encoder to report.
        print("Direction: {}".format(adjustment.get("direction")))
        print("Runtime:   {} ms at {} ms/deg".format(
            adjustment.get("duration_ms"), adjustment.get("ms_per_degree")
        ))

        if adjustment.get("reliable") is False:
            print("Warning:   the runtime is shorter than one PWM frame, so")
            print("           the servo may not have responded.")

    print("Alignment offset now {} deg.".format(
        reference.get("alignment_offset_deg")
    ))

    if reference.get("drift_deg") is not None:
        print("Drift vs nominal: {} deg".format(reference.get("drift_deg")))


def menu_resync(mission):
    """Re-declare the origin after a reboot or a lost position."""
    banner("RE-SYNC CAROUSEL")

    if not servo_selected(mission):
        print("No servo is selected, so there is nothing to synchronize.")
        print("Run [0] Carousel Setup first.")
        print()
        pause()

        return

    print("Align physical Slot 1 with the soil loading hole, then confirm.")
    print()
    print("Nothing moves. On a servo with an encoder this also captures the")
    print("encoder reading as the carousel origin.")
    print()

    if not confirm("Is Slot 1 now under the loading hole?"):
        print("Cancelled; position tracking unchanged.")

        return

    try:
        data = mission.link.sync_load_slot(1)

    except (LinkError, TimeoutError) as error:
        report_failure(error)

        return

    carousel = data.get("carousel") or {}
    origin = ((carousel.get("reference") or {}).get("origin")) or {}

    print()
    print("Synchronized. Loader = Slot {}, scanner = Slot {}.".format(
        carousel.get("current_load_slot"),
        carousel.get("current_scan_slot"),
    ))

    if origin.get("feedback"):
        print("Encoder origin: {} counts.".format(origin.get("origin_counts")))


# ----------------------------------------------------------------------
# servo tools
# ----------------------------------------------------------------------

def menu_servo_test(mission):
    """Servo and carousel tools, adapted to whichever backend is active."""
    while True:
        try:
            status = mission.hardware_status()

        except (LinkError, TimeoutError) as error:
            report_failure(error)

            return

        servo = status.get("servo") or {}
        capabilities = servo.get("capabilities") or {}

        banner("SERVO / CAROUSEL TOOLS")

        print("Servo: {}".format(servo.get("label")))
        print()

        if not servo.get("selected"):
            print("No servo is selected. Carousel movement is blocked.")
            print()
            print("[s] Select the installed servo")
            print("[b] ST3215 bus scan - find out why one does not answer")
            print("[0] Back")

            selection = choose()

            if selection == "s":
                menu_select_servo(mission)

            elif selection == "b":
                try:
                    menu_servo_bus_scan(mission)

                except (LinkError, TimeoutError) as error:
                    report_failure(error)

            elif selection == "0":
                return

            continue

        print("[1] One slot clockwise")
        print("[2] One slot counter-clockwise")
        print("[3] Fine adjustment by degrees")
        print("[4] STOP servo")
        print()
        print("[5] Diagnostics (moves nothing)")
        print("[6] Movement tests (turns the carousel)")
        print("[7] Calibration / settings")
        print()
        print("[s] Change servo")
        print("[b] Bus scan (moves nothing)")

        if capabilities.get("persistent_config"):
            print("[e] SERVICE: write servo configuration to its EPROM")

        if capabilities.get("torque_control"):
            print("[t] Torque hold / release")

        print()
        print("[0] Back")

        selection = choose()

        try:
            if selection == "1":
                report_slot_move(mission.link.move_slots("cw", 1))

            elif selection == "2":
                report_slot_move(mission.link.move_slots("ccw", 1))

            elif selection == "3":
                menu_fine_adjust(mission)

            elif selection == "4":
                mission.link.servo_stop()
                print("Servo stopped.")
                print("The tracked position was dropped - re-sync before")
                print("the next measurement.")

            elif selection == "5":
                menu_servo_diagnostics(mission)

            elif selection == "6":
                menu_servo_movement_test(mission)

            elif selection == "7":
                menu_servo_calibration(mission)

            elif selection == "s":
                menu_select_servo(mission)

            elif selection == "b":
                menu_servo_bus_scan(mission)

            elif selection == "e" and capabilities.get("persistent_config"):
                menu_servo_configure(mission)

            elif selection == "t" and capabilities.get("torque_control"):
                menu_servo_torque(mission)

            elif selection == "0":
                return

            elif selection:
                print("Unknown option.")

        except (LinkError, TimeoutError) as error:
            report_failure(error)


def print_servo_block(servo):
    """The servo half of a status report, in the active backend's terms."""
    capabilities = servo.get("capabilities") or {}
    backend = servo.get("backend") or {}

    print("  Servo:         {}".format(servo.get("label")))

    if not servo.get("selected"):
        print("  {}".format(servo.get("message")))

        return

    print("  Feedback:      {}".format(
        "encoder, movement verified"
        if capabilities.get("verified_movement")
        else "none - timed, open loop"
    ))

    if capabilities.get("position_feedback"):
        print("  Link:          id {} on UART{}, {} baud".format(
            backend.get("id"), backend.get("uart_id"), backend.get("baud")
        ))
        print("  Wiring:        TX GPIO{} -> driver TX, RX GPIO{} -> driver "
              "RX, GND".format(backend.get("tx_pin"), backend.get("rx_pin")))
        print("  Power:         external supply at the driver board")
        print("  Mode:          {} ({})".format(
            backend.get("mode_name"), backend.get("mode")
        ))

        if backend.get("mode_correct") is False:
            print("  ** WRONG MODE - run SERVICE: write servo "
                  "configuration **")

        print("  Position:      {} counts ({} deg)".format(
            backend.get("position_counts"), backend.get("position_deg")
        ))
        print("  Moving:        {}".format(backend.get("moving")))
        print("  Torque:        {}".format(backend.get("torque_enabled")))
        print("  Voltage:       {} V".format(backend.get("voltage_v")))
        print("  Temperature:   {} C".format(backend.get("temperature_c")))
        print("  Load:          {} (0.1%)".format(backend.get("load_permille")))
        print("  Current:       {} mA".format(backend.get("current_ma")))

        flags = backend.get("status_flags")

        if flags:
            print("  ** SERVO ALARM: {} **".format(", ".join(flags)))

        bus = backend.get("bus") or {}

        if bus.get("retry") or bus.get("timeout") or bus.get("checksum"):
            print("  Bus:           {} tx, {} rx, {} retries, {} timeouts, "
                  "{} checksum".format(
                      bus.get("tx"), bus.get("rx"), bus.get("retry"),
                      bus.get("timeout"), bus.get("checksum"),
                  ))

    else:
        print("  Signal pin:    GPIO{} at {} Hz".format(
            backend.get("pin"), backend.get("pwm_freq")
        ))
        print("  Power:         PCB servo branch, not switched by firmware")
        print("  Neutral:       {} us".format(backend.get("stop_us")))
        print("  Direction:     CW {} us / CCW {} us".format(
            backend.get("cw_us"), backend.get("ccw_us")
        ))
        print("  Slot timing:   CW {} ms / CCW {} ms".format(
            backend.get("next_slot_cw_ms"), backend.get("next_slot_ccw_ms")
        ))
        print("  Half turn:     {} ms / {} ms".format(
            backend.get("load_to_scan_cw_ms"),
            backend.get("scan_to_load_ccw_ms"),
        ))

        if backend.get("calibration_modified"):
            print("  ** RUNTIME CALIBRATION OVERRIDE ACTIVE **")


SCAN_ADVICE = {
    "SERVO_FOUND": (
        "A servo answered. If the settings above differ from the ones in",
        "ESP32/config.py, change config.py to match and upload it again -",
        "the servo is telling you what it actually uses.",
    ),
    "WRONG_ID": (
        "The bus works. Only the ID is wrong, and that is a one-line",
        "change in ESP32/config.py (ST3215_SERVO_ID).",
    ),
    "ECHO_ONLY": (
        "The ESP32 side is proven good: it transmits and hears itself.",
        "What has NOT been proven is that the servo has power. A driver",
        "board with its LED lit only means the board is powered; measure",
        "at the servo's own connector, with the servo plugged in, while",
        "the scan runs. Then sweep the full ID range - a servo whose ID",
        "was changed answers to nothing else.",
    ),
    "CORRUPT_TRAFFIC": (
        "Frames arrive but decode wrong. That is nearly always the common",
        "ground: the ESP32 GND and the servo supply GND must be joined,",
        "even though the servo takes its power from elsewhere.",
    ),
    "NOISE_ONLY": (
        "Something transmits, nothing decodes. Check the ground first,",
        "then whether anything else is driving the same pins.",
    ),
    "SILENT_BUS": (
        "The bus is completely silent, in both pin orders, at all eight",
        "baud rates. Nothing is transmitting, so this is power or",
        "connection - not configuration, and not the firmware.",
        "",
        "Measure 6-12.6 V at the SERVO connector while the scan runs, not",
        "at the supply terminals: a supply reading 9.3 V with the servo",
        "unplugged proves only that the supply works. Then confirm the",
        "servo lead is seated in the driver board's bus port, and that",
        "the driver board's own logic supply is present.",
    ),
}


def print_bus_scan(report):
    """The scan, as a table of what was heard at every combination."""
    scanned = report.get("scanned") or {}

    print("Scanned:")
    print("  UART{}   TX GPIO{}   RX GPIO{}".format(
        scanned.get("uart_id"),
        scanned.get("configured_tx_pin"),
        scanned.get("configured_rx_pin"),
    ))
    print("  {} baud rate(s), {} servo ID(s), {} probe(s)".format(
        len(scanned.get("bauds") or []),
        scanned.get("id_count"),
        scanned.get("probes"),
    ))
    print("  Configured: ID {} at {} baud".format(
        scanned.get("configured_id"), scanned.get("configured_baud")
    ))
    print()

    print("{:<20} {:>9} {:>7} {:>6} {:>6}  {}".format(
        "Pin order", "Baud", "Bytes", "Echo", "Bad", "Answered"
    ))

    for probe in report.get("probes") or []:
        answered = probe.get("ids_answered") or []
        others = probe.get("other_ids") or []

        if answered:
            verdict = "ID {}".format(
                ", ".join(str(value) for value in answered)
            )
        elif others:
            verdict = "other ID {}".format(
                ", ".join(str(value) for value in others)
            )
        else:
            verdict = "-"

        print("{:<20} {:>9} {:>7} {:>6} {:>6}  {}".format(
            str(probe.get("pin_order"))[:20],
            probe.get("baud"),
            probe.get("bytes"),
            "YES" if probe.get("echo") else "-",
            probe.get("checksum_errors"),
            verdict,
        ))

    print()
    print("  Bytes = bytes received, Echo = our own transmission came")
    print("  back, Bad = frames that failed their checksum.")

    print()
    print("RESULT: {}".format(report.get("result")))
    print()

    for line in textwrap.wrap(str(report.get("diagnosis") or ""), 66):
        print("  {}".format(line))

    for difference in report.get("differences") or []:
        print()

        for line in textwrap.wrap("- {}".format(difference), 66):
            print("  {}".format(line))

    advice = SCAN_ADVICE.get(report.get("result"))

    if advice:
        print()
        print("WHAT TO DO")
        print()

        for line in advice:
            print("  {}".format(line))

    if report.get("released_servo"):
        print()
        print("The ST3215 backend was released so the scan could reopen")
        print("UART2. Select the servo again once it answers.")


def menu_servo_bus_scan(mission):
    """
    Find out WHY the servo does not answer, without guessing.

    Deliberately reachable with nothing selected: this is the tool for
    the moment select_servo has just failed, and requiring a working
    selection first would be circular.
    """
    while True:
        banner("SERVO BUS SCAN")

        print("Pings the servo bus and reports what answers. MOVES")
        print("NOTHING - a ping asks a servo to identify itself.")
        print()
        print("[1] Quick scan")
        print("    The configured ID, at all 8 baud rates, in both pin")
        print("    orders. About 1 second.")
        print()
        print("[2] Full ID sweep")
        print("    Every ID from 0 to 253 at one baud rate, in both pin")
        print("    orders. Use when the quick scan hears an echo but no")
        print("    servo. About 20 seconds.")
        print()
        print("[0] Back")

        selection = choose()

        if selection == "0":
            return

        if selection == "1":
            ids, bauds = None, None

        elif selection == "2":
            print()
            print("Baud rate to sweep [1] 1000000  [2] 500000  [3] 115200")

            answer = choose("Baud")
            bauds = {
                "1": [1000000], "2": [500000], "3": [115200],
            }.get(answer, [1000000])

            ids = "all"

        else:
            if selection:
                print("Unknown option.")

            continue

        print()
        print("Scanning...")
        sys.stdout.flush()

        try:
            report = mission.link.servo_bus_scan(ids=ids, bauds=bauds)

        except (LinkError, TimeoutError) as error:
            report_failure(error)
            print()
            pause()

            continue

        print()
        print_bus_scan(report)
        print()
        pause()


def menu_servo_diagnostics(mission):
    """Read-only backend check, stage by stage."""
    banner("SERVO DIAGNOSTICS")

    print("Checking the active servo. Nothing will move.")
    print()
    sys.stdout.flush()

    try:
        report = mission.link.servo_diagnostics()

    except (LinkError, TimeoutError) as error:
        report_failure(error)
        print()
        pause()

        return

    for entry in report.get("steps") or []:
        print_check(entry.get("step"), entry.get("ok"))

        value = entry.get("value")

        if entry.get("ok") and isinstance(value, str):
            print("      {}".format(value))

        if not entry.get("ok"):
            error = entry.get("error") or {}

            if error:
                print("      {}: {}".format(
                    error.get("code"), error.get("message")
                ))

            elif isinstance(value, str):
                print("      {}".format(value))

    print()

    if report.get("ok"):
        print("{} OK.".format(report.get("label")))

    else:
        error = report.get("error") or {}

        print("{} FAILED: {}".format(
            report.get("label"), error.get("code")
        ))
        print("  {}".format(error.get("message")))

        if report.get("uart_id") is not None:
            print()
            print("Check, in this order:")
            print("  1. external servo power supply at the driver board")
            print("  2. ESP32 GND to driver board GND (common reference)")
            print("  3. GPIO17 -> driver TX, GPIO16 -> driver RX")
            print("  4. servo ID and baud rate")

    if report.get("baud_matches") is False:
        print()
        print("The servo reports {} baud but the firmware opens {}.".format(
            report.get("baud_reported"), report.get("baud")
        ))

    print()
    pause()


def menu_servo_configure(mission):
    """SERVICE: write the operating mode into the servo's own memory."""
    banner("SERVICE: SERVO CONFIGURATION")

    print("This writes the servo's EPROM: the operating mode and both")
    print("angle limits. It is needed ONCE per servo, and it survives a")
    print("power cycle.")
    print()
    print("It is NOT part of carousel setup. Ordinary calibration")
    print("establishes a runtime origin and touches no persistent servo")
    print("state; this command is the only thing that does.")
    print()
    print("Step servo mode is what the carousel needs: every movement is a")
    print("relative number of encoder counts, so the carousel can turn")
    print("indefinitely and never takes the long way round the 4095/0")
    print("encoder boundary.")
    print()
    print("EPROM has a finite write life - do not run this routinely.")
    print()

    if not confirm("Write the servo EPROM now?"):
        print("Cancelled; the servo was not changed.")

        return

    try:
        result = mission.link.servo_configure(confirm=True)

    except (LinkError, TimeoutError) as error:
        report_failure(error)

        return

    print()
    print("Mode is now {} ({}).".format(
        result.get("mode_name"), result.get("mode")
    ))
    print("Angle limits: {} .. {}".format(
        result.get("min_angle_limit"), result.get("max_angle_limit")
    ))
    print()
    pause()


def menu_servo_torque(mission):
    """Hold or release the servo, explicitly."""
    banner("SERVO TORQUE")

    print("Holding torque keeps the carousel exactly where the last")
    print("movement left it, which is what a measurement depends on.")
    print()
    print("[1] HOLD  (enable torque)")
    print("[2] RELEASE (turn the carousel by hand; drops the tracked")
    print("    position)")
    print("[0] Back")

    selection = choose()

    try:
        if selection == "1":
            mission.link.servo_torque(True)
            print("Torque enabled.")

        elif selection == "2":
            if confirm("Release torque? The carousel will turn freely"):
                mission.link.servo_torque(False)
                print("Torque released. Re-sync the carousel afterwards.")

    except (LinkError, TimeoutError) as error:
        report_failure(error)


# ST3215 settings, shown read-only. See menu_servo_calibration.
def print_st3215_settings(calibration):
    values = calibration["current"]
    units = calibration.get("units") or {}

    print("ST3215 settings (read only):")
    print()
    print("  Speed:              {} steps/s".format(
        values["speed_steps_per_s"]
    ))
    print("  Acceleration:       {}".format(values["acceleration"]))
    print("  Position tolerance: {} counts".format(
        values["position_tolerance_counts"]
    ))
    print("  Settle:             {} ms".format(values["settle_ms"]))
    print("  Poll interval:      {} ms".format(values["poll_interval_ms"]))
    print("  Move timeout:       {} ms".format(values["move_timeout_ms"]))
    print()

    for key in ("speed_steps_per_s", "acceleration",
                "position_tolerance_counts"):
        if units.get(key):
            print("  {:<26} {}".format(key + ":", units[key]))

    print()
    print(calibration.get("note") or "")


def menu_servo_calibration(mission):
    """
    Servo settings. Read only.

    There is nothing to calibrate. The ST3215 commands every movement in
    encoder counts and verifies it by reading the encoder back, so its
    limits are engineering values that belong in config.py under version
    control rather than numbers trimmed at runtime.

    This screen used to be editable, because the MG995 it also drove was
    open-loop and its timings had to be measured on the real mechanism.
    That backend is gone, and with it every pulse width, settle time and
    correction factor this screen existed to tune.
    """
    banner("SERVO SETTINGS")

    try:
        calibration = mission.link.get_servo_calibration()

    except (LinkError, TimeoutError) as error:
        report_failure(error)
        print()
        pause()

        return

    if calibration.get("editable"):
        # The firmware says its settings are runtime-editable. No servo
        # this client drives has that any more, so rather than silently
        # showing a read-only screen, say what happened.
        print("The firmware reports editable calibration, which no servo")
        print("this client supports should have. Check that the firmware")
        print("and this client are the same version.")
        print()

    print_st3215_settings(calibration)
    print()
    pause()



def report_move_test(result):
    """Per-leg results, in whatever terms the backend can report."""
    print()

    if result.get("verified_movement"):
        print("  Legs:          {} x {} = {}".format(
            result.get("repeat"), result.get("legs"), result.get("leg_count")
        ))
        print("  Net travel:    {} counts ({} deg)".format(
            result.get("net_counts"), result.get("net_degrees")
        ))
        print("  Encoder:       {} -> {}".format(
            result.get("start_position"), result.get("end_position")
        ))
        print("  Worst error:   {} counts (tolerance {})".format(
            result.get("worst_position_error"), result.get("tolerance_counts")
        ))

        if result.get("closed_loop_error_counts") is not None:
            print("  Closing error: {} counts ({} deg)".format(
                result.get("closed_loop_error_counts"),
                result.get("closed_loop_error_deg"),
            ))

        print()

        for index, movement in enumerate(result.get("movements") or []):
            print("  leg {}: {:+5} counts, {} -> {}, error {:+} counts, "
                  "{} ms".format(
                      index + 1,
                      movement.get("requested_counts"),
                      movement.get("start_position"),
                      movement.get("actual_position"),
                      movement.get("position_error"),
                      movement.get("elapsed_ms"),
                  ))

    else:
        print("  Duration:      {} ms x {} = {} ms".format(
            result.get("duration_ms"), result.get("repeat"),
            result.get("total_duration_ms"),
        ))
        print("  Nominal:       {:.0f} deg".format(
            result.get("nominal_degrees", 0)
        ))
        print("  Pulse:         {} us".format(result.get("pulse_us")))
        print()
        print("  This servo has no encoder, so the result has to be judged")
        print("  by eye against a physical reference.")

    print()
    print("Movement complete.")

    if result.get("position_invalidated"):
        print()
        print("Carousel position tracking was invalidated - re-sync before")
        print("normal operation.")


def menu_servo_movement_test(mission):
    """
    Operator-confirmed movement tests, offered per backend.

    Run the diagnostics first: there is no point turning a mechanism whose
    servo is not answering.
    """
    while True:
        try:
            status = mission.hardware_status()

        except (LinkError, TimeoutError) as error:
            report_failure(error)

            return

        servo = status.get("servo") or {}
        capabilities = servo.get("capabilities") or {}
        kinds = [
            (entry["kind"], entry["label"])
            for entry in (servo.get("test_move_kinds") or [])
        ]

        banner("SERVO MOVEMENT TEST")

        print("Servo: {}".format(servo.get("label")))
        print()
        print("THE CAROUSEL WILL TURN. Check the mechanism is clear.")
        print()

        if capabilities.get("verified_movement"):
            print("Every movement is commanded in encoder counts and")
            print("verified from the encoder afterwards, so what you get")
            print("back is what the servo measured.")

        else:
            print("This servo has no encoder, so every result has to be")
            print("judged by eye. Position tracking is invalidated by any")
            print("movement here.")

        print()

        for index, (kind, label) in enumerate(kinds, start=1):
            print("[{}] {}".format(index, label))

        print()
        print("[0] Back")

        selection = choose()

        if selection == "0":
            return

        chosen = None

        for index, entry in enumerate(kinds, start=1):
            if selection == str(index):
                chosen = entry

        if chosen is None:
            if selection:
                print("Unknown option.")

            continue

        kind, label = chosen
        degrees = None
        hold_ms = None

        if kind == "degrees":
            degrees = ask_float("Degrees (+ = clockwise)", -180.0, 180.0)

            if degrees is None:
                continue

        if kind == "neutral":
            seconds = ask_int("Hold for how many seconds", 1, 30, default=5)

            if seconds is None:
                continue

            hold_ms = seconds * 1000

        repeat = ask_int("Repeat how many times", 1, 8, default=1)

        if repeat is None:
            continue

        print()
        print("{} x {}".format(label, repeat))

        if not confirm("Move the carousel now?"):
            print("Cancelled; nothing moved.")

            continue

        sys.stdout.flush()

        try:
            result = mission.link.servo_test_move(
                kind, repeat=repeat, degrees=degrees, hold_ms=hold_ms,
                confirm=True,
            )

        except (LinkError, TimeoutError) as error:
            report_failure(error)

            continue

        report_move_test(result)

        if kind in ("slot_out_and_back", "out_and_back"):
            print()
            print("A symmetrical test should close on itself. This is the")
            print("repeatability figure that matters for Measure Sample.")

        print()
        pause()


# ======================================================================
# screens: the sample workflow
# ======================================================================

def menu_choose_slot(mission, status, view):
    """Bring a physical slot to the soil loading position."""
    carousel = status.get("carousel") or {}
    current = carousel.get("selected_slot")

    banner("CHOOSE SAMPLE / SLOT")

    for entry in view:
        marker = " <- current" if entry["slot_id"] == current else ""

        print("  {}  {:<10} {}{}".format(
            entry["slot_id"],
            entry["sample_id"] or "----",
            entry["state"],
            marker,
        ))

    print()

    suggested = (current % SLOT_COUNT) + 1 if current else 1
    target = ask_int(
        "Choose slot [1-{}]".format(SLOT_COUNT), 1, SLOT_COUNT,
        default=suggested,
    )

    if target is None:
        print("Cancelled; carousel not moved.")

        return

    entry = mission.entry_for(view, target)

    print()
    print("Moving Slot {} to the loading position...".format(target))
    sys.stdout.flush()

    data = mission.link.select_slot(target, entry.get("sample_id"))
    move = data.get("move") or {}

    if move.get("restored_load_orientation"):
        print("Restored the loading orientation with the calibrated "
              "half turn first.")

    print("Slot {} is now at the {} position.".format(
        target, (data.get("carousel") or {}).get("carousel_phase")
    ))


def ask_metadata():
    """Every field optional. A skipped field stays null, never invented."""
    print()
    print("Metadata - press Enter to skip any field.")
    print()

    metadata = {}

    for key, label in METADATA_FIELDS:
        value = ask("  {} [optional]".format(label))

        if value:
            metadata[key] = value

    return metadata or None


def menu_prepare(mission, status, view):
    """Create the persistent Sample record and bring its slot to the loader."""
    carousel = status.get("carousel") or {}
    slot_id = carousel.get("selected_slot")

    if slot_id is None:
        print("No slot is selected. Choose a slot first.")

        return

    entry = mission.entry_for(view, slot_id)

    if entry["state"] != STATE_EMPTY:
        print("Slot {} already holds sample {} ({}). Clear the physical "
              "slot before preparing a new one.".format(
                  slot_id, entry["sample_id"], entry["state"]
              ))

        return

    banner("PREPARE SAMPLE")

    print("Physical slot: {}".format(slot_id))
    print()

    raw_id = ask("Sample ID (blank = cancel)")

    if not raw_id:
        print("Cancelled; no sample was created.")

        return

    try:
        sample_id = validate_sample_id(raw_id)

    except StorageError as error:
        print(error.message)

        return

    if mission.store.get_state(sample_id) == STATE_MEASURED:
        print("Sample ID {} already exists as a MEASURED record. Choose "
              "another ID so the earlier result is not overwritten.".format(
                  sample_id
              ))

        return

    metadata = ask_metadata()

    if carousel.get("carousel_phase") != "LOAD":
        print()
        print("Bringing Slot {} to the loading position...".format(slot_id))

    # Also attaches the Sample ID to the slot so the ESP32 can echo it
    # back for correlation.
    mission.link.select_slot(slot_id, sample_id)

    record = mission.store.create(
        sample_id, slot_id, utc_timestamp(), metadata
    )

    print()
    print("Sample {} created in slot {}.".format(sample_id, slot_id))
    print("State: {}".format(record["state"]))
    print()
    print("The rover arm can now deposit soil. Confirm it afterwards with "
          "[3].")


def menu_confirm(mission, status, view):
    """Record that the arm has physically deposited soil."""
    carousel = status.get("carousel") or {}
    slot_id = carousel.get("selected_slot")
    entry = mission.entry_for(view, slot_id) if slot_id else None

    if not entry or entry["state"] == STATE_EMPTY:
        print("No sample is prepared in the selected slot.")

        return

    if entry["state"] != STATE_READY_TO_LOAD:
        print("Slot {} is {}, not {}.".format(
            slot_id, entry["state"], STATE_READY_TO_LOAD
        ))

        return

    banner("CONFIRM SAMPLE LOADED")

    print("Slot {} / sample {}".format(slot_id, entry["sample_id"]))
    print()

    if not confirm("Has the soil been deposited in this slot?"):
        print("Not confirmed; the sample stays {}.".format(
            STATE_READY_TO_LOAD
        ))

        return

    record = mission.store.set_state(
        entry["sample_id"], STATE_LOADED, "loaded_at", utc_timestamp()
    )

    print()
    print("Sample {} is now {}.".format(record["sample_id"], record["state"]))


def apply_measurement(record, result):
    """
    Write a complete BD result into a Sample record.

    Everything the pipeline produced is kept: all three 18-channel
    spectra, the White and Dark actually used, the comparison against
    every material, and the whole analysis block. The handful of flat
    fields at the end are mirrors for the compact table and for reading
    the archive by eye - the authoritative values stay in `analysis`.
    """
    analysis = result.get("analysis") or {}

    record.update({
        "measured": bool(result.get("measurement")),
        "schema_version": bd_config.SAMPLE_SCHEMA_VERSION,
        "measurement": result["measurement"],
        "calibration": result["calibration"],
        "database": result["database"],
        "quality": result.get("quality"),
        "reference_matches": result["reference_matches"],
        "metric_agreement": result.get("metric_agreement"),

        # DB1, DB2 and DB3 scored separately, plus the consensus across
        # them. Kept beside `analysis` rather than replacing it: that
        # one is the DB1 conclusion under the calibration DB1 was built
        # with, and the two answer different questions.
        "cross_database": result.get("cross_database"),

        "analysis": result["analysis"],
        "analysis_status": result["analysis_status"],
        "analysis_error": result["analysis_error"],

        "best_match": analysis.get("best_match"),
        "best_similarity": analysis.get("best_similarity"),
        "best_rmse": analysis.get("best_rmse"),
        "best_pearson_r": analysis.get("best_pearson_r"),
        "second_match": analysis.get("second_match"),
        "second_similarity": analysis.get("second_similarity"),
        "score_difference": analysis.get("score_difference"),
        "status": analysis.get("status"),
        "quality_status": (result.get("quality") or {}).get("status"),
        "conclusion": analysis.get("automatic_conclusion"),
    })

    return record


def menu_measure(mission, status, view):
    """
    The full measurement: ESP32 acquires RAW, BD analyses it, PC saves it.

    The Sample record opened by Prepare is COMPLETED here. No second
    Sample ID is ever created.
    """
    carousel = status.get("carousel") or {}
    slot_id = carousel.get("selected_slot")
    entry = mission.entry_for(view, slot_id) if slot_id else None

    # ---- everything that can be refused before the mechanism moves --
    if not entry or entry["state"] == STATE_EMPTY:
        print("No sample is prepared in the selected slot.")

        return

    if entry["state"] == STATE_READY_TO_LOAD:
        print("Slot {} does not contain confirmed soil yet. Use [3] "
              "Confirm Sample Loaded first.".format(slot_id))

        return

    if entry["state"] == STATE_MEASURED:
        print("Sample {} has already been measured. Clear the physical "
              "slot to reuse it.".format(entry["sample_id"]))

        return

    if not carousel.get("position_valid"):
        print("The carousel is not synchronized. Use Tools -> Re-sync "
              "Carousel first.")

        return

    if carousel.get("carousel_phase") != "LOAD":
        print("Slot {} is not at the loading position (phase {}). Choose "
              "the slot again to bring it back.".format(
                  slot_id, carousel.get("carousel_phase")
              ))

        return

    if mission.references is None:
        print("BD references are not loaded: {}".format(mission.science_error))
        print("Measuring now would acquire a spectrum that cannot be "
              "normalized. Fix firmware/BD first.")

        return

    sample_id = entry["sample_id"]

    banner("MEASURE SAMPLE")

    print("Slot {} / sample {}".format(slot_id, sample_id))
    print()
    print("Checking Sample................. PASS")
    print("Checking carousel.............. PASS")
    print()
    print("The carousel will swing 180 deg to the scanner, acquire one "
          "18-channel spectrum, then swing 180 deg back so the sample "
          "ends where it started. This takes a few seconds.")
    print()
    sys.stdout.flush()

    # ---- ESP32: out, acquire, back ----------------------------------
    try:
        data = mission.link.measure_raw(slot_id, sample_id)

    except (LinkError, TimeoutError) as error:
        print()
        print("Measurement failed before any spectrum was obtained.")

        if isinstance(error, LinkError):
            report_link_error(error)
            report_return_move((error.data or {}).get("return_move"))
        else:
            print("Timeout: {}".format(error))

        print()
        print("Sample {} remains {}. Nothing was saved.".format(
            sample_id, STATE_LOADED
        ))
        print()
        pause()

        return

    settings = data.get("sensor_settings")
    blocks = data.get("illuminations") or {}
    measured_at = utc_timestamp()

    print("Measuring {}.................. PASS".format(sample_id))

    for name in ILLUMINATIONS:
        block = blocks.get(name) or {}

        print("  {:<6} {} repeat(s), {}/18 channels".format(
            name.upper(),
            block.get("repeats", "-"),
            len((block.get("acquisitions") or [{}])[0]),
        ))

    # The acquisition succeeded. Whether the carousel made it home is a
    # separate outcome and must not affect what gets saved.
    report_return_move(data.get("return_move"))

    # ---- BD: analyse -------------------------------------------------
    analysis_error = None

    try:
        result = mission.analyse_raw(data, settings)

    except Exception as error:
        # Acquired science must survive downstream software failure.
        analysis_error = "{}: {}".format(type(error).__name__, error)
        result = {
            "measurement": {
                "wavelengths": sample_analysis.channel_wavelengths(),
                "raw": {
                    name: (blocks.get(name) or {}).get("acquisitions", [{}])[0]
                    for name in blocks
                },
                "active_normalized": {},
                "legacy_database_normalized": {},
                "dark_corrected": None,
                "normalized": None,
                "sensor_settings": settings,
            },
            "calibration": None,
            "database": None,
            "quality": None,
            "reference_matches": [],
            "metric_agreement": {},
            "analysis": None,
            "analysis_status": "FAILED",
            "analysis_error": analysis_error,
        }

        print()
        print("!! ANALYSIS FAILED: {}".format(analysis_error))
        print("   The acquired spectra are intact and will still be saved.")

    # ---- PC: complete the SAME record --------------------------------
    record = mission.store.get_sample(sample_id) or {"sample_id": sample_id}

    timestamps = record.get("timestamps") or {}
    timestamps["measured_at"] = measured_at

    record.update({
        "sample_id": sample_id,
        "slot_id": slot_id,
        "state": STATE_MEASURED,
        "timestamps": timestamps,
        "hardware": {
            "carousel": data.get("carousel"),
            "data_ready_wait_ms": data.get("data_ready_wait_ms"),
            "zero_channels": data.get("zero_channels"),
            "home_restored": data.get("home_restored"),
        },
    })

    apply_measurement(record, result)

    try:
        mission.store.save(record)
        saved = True

    except StorageError as error:
        saved = False

        print()
        print("!! COULD NOT SAVE: {}".format(error.message))
        print("   The measurement below was NOT written to disk.")

    # ---- report ------------------------------------------------------
    banner("MEASUREMENT COMPLETE")

    print("Sample ID:      {}".format(sample_id))
    print("Physical slot:  {}".format(slot_id))
    print("State:          {} -> {}".format(STATE_LOADED, STATE_MEASURED))
    print("Saved:          {}".format("YES" if saved else "NO"))
    print("Home position:  {}".format(
        "RESTORED" if data.get("home_restored") else "NOT RESTORED"
    ))

    print()
    print("SETTINGS")
    print()
    print_settings_block(result["measurement"].get("sensor_settings"))

    quality_report = result.get("quality")

    print()
    print("MEASUREMENT QUALITY")
    print()

    if quality_report:
        print_quality(quality_report)
    else:
        print("Not assessed: the analysis did not run.")

    if result["analysis_status"] == "OK":
        print()
        print("REFERENCE COMPARISON")
        print("  legacy calibration {}".format(
            result["calibration"]["legacy_database_calibration_id"]
        ))
        print()
        print_metric_table(result["reference_matches"], limit=5)

        print()
        print_agreement(result.get("metric_agreement"))

        print()
        print("RESULT  (DB1, legacy calibration)")
        print()
        print_result_block(result["analysis"])

        print()
        print("THREE-DATABASE COMPARISON")
        print()
        print_cross_database(result.get("cross_database"), limit=3)

        if result.get("evidence"):
            print()
            print("=" * 60)
            print()
            print_evidence_summary(result["evidence"])
            print()
            print_decision(result.get("decision"))

    else:
        print()
        print("Analysis status: FAILED - {}".format(result["analysis_error"]))
        print("The acquired spectra are stored and can be re-analysed "
              "offline.")

    offer_decision_detail(result)

    # The measurement is already saved as a Sample. This asks whether it
    # should ALSO become a learning observation, which is a different
    # question with a different answer: a rover measurement of unknown
    # soil is worth keeping and has no label. §59.
    if mission.learning is not None and result.get("evidence"):
        capture_ground_truth(mission, sample_id, result)

    print()
    print("Full 54-channel data is in the saved record; open the Sample")
    print("from Tools -> Sample Database to see it.")

    print()

    if data.get("home_restored"):
        print("Slot {} is back at the loading position, exactly where it "
              "started. The soil is still physically in the slot.".format(
                  slot_id
              ))

    else:
        print("The measurement is saved, but the carousel did NOT return "
              "home. Re-sync the carousel (Tools -> Re-sync Carousel) "
              "before moving anything else.")

    print()
    pause()


def menu_clear_slot(mission, status, view):
    """
    Free a physical slot.

    This frees the mechanism only. The saved scientific record stays in
    the PC archive; Delete Sample is what removes that.
    """
    banner("CLEAR PHYSICAL SLOT")

    for entry in view:
        print("  {}  {:<10} {}".format(
            entry["slot_id"], entry["sample_id"] or "----", entry["state"]
        ))

    print()
    print("Enter slot number to clear.")
    print("[a] Clear ALL physical slots")
    print("[0] Back")
    print()

    answer = ask("Select (blank = cancel)").strip().lower()

    if not answer or answer == "0":
        print("Cancelled.")

        return

    if answer == "a":
        clear_all_slots(mission, view)

        return

    try:
        slot_id = int(answer)

    except ValueError:
        print("Enter a slot number, 'a' for all, or 0 to go back.")

        return

    if slot_id < 1 or slot_id > SLOT_COUNT:
        print("Slots are 1 to {}.".format(SLOT_COUNT))

        return

    entry = mission.entry_for(view, slot_id)

    if entry["state"] == STATE_EMPTY:
        print("Slot {} is already empty.".format(slot_id))

        return

    print()
    print("Slot {} holds sample {} ({}).".format(
        slot_id, entry["sample_id"], entry["state"]
    ))
    print("The saved scientific record will be KEPT.")
    print()

    if not confirm("Has the soil been physically removed?"):
        print("Cancelled; nothing changed.")

        return

    mission.link.clear_slot(slot_id)

    try:
        mission.store.set_state(entry["sample_id"], STATE_EMPTY)

    except StorageError as error:
        print("Slot freed, but the record could not be updated: {}".format(
            error.message
        ))

        return

    print()
    print("Slot {} is free. Sample {} remains in the archive.".format(
        slot_id, entry["sample_id"]
    ))


def clear_all_slots(mission, view):
    """
    Free every physical slot at once.

    Carousel occupancy only. Saved Sample records - on the PC and on the
    ESP32 - are left completely alone; deleting those is a separate,
    separately confirmed operation.
    """
    occupied = [entry for entry in view if entry["state"] != STATE_EMPTY]

    if not occupied:
        print()
        print("All physical slots are already empty.")

        return

    print()
    print("Clear ALL {} physical slots?".format(SLOT_COUNT))
    print()
    print("This only clears carousel occupancy.")
    print("Saved Sample records will NOT be deleted.")
    print()
    print("Currently occupied:")

    for entry in occupied:
        print("  Slot {}  {}  {}".format(
            entry["slot_id"], entry["sample_id"] or "----", entry["state"]
        ))

    print()

    if ask("Type YES to continue") != "YES":
        print("Cancelled; nothing changed.")

        return

    data = mission.link.clear_all_slots()

    # The PC lifecycle state is authoritative, so free the slots there
    # too - without deleting anything.
    failures = []

    for entry in occupied:
        if not entry["sample_id"]:
            continue

        try:
            mission.store.set_state(entry["sample_id"], STATE_EMPTY)

        except StorageError as error:
            failures.append((entry["sample_id"], error.message))

    print()
    print("Physical slots cleared: {}".format(data.get("cleared_count", 0)))
    print("All {} slots are now EMPTY.".format(SLOT_COUNT))

    if failures:
        print()
        print("Some PC records could not be updated:")

        for sample_id, message in failures:
            print("  {}: {}".format(sample_id, message))

    print()
    print("Saved Sample records were NOT deleted.")


# ======================================================================
# screens: sample database
# ======================================================================

def print_sample_table(store):
    summaries = store.summaries()

    if not summaries:
        print("No samples have been saved yet.")

        return summaries

    print("{:<4} {:<12} {:<6} {:<14} {:<22} {:>10}".format(
        "#", "Sample", "Slot", "State", "Best match", "Similarity"
    ))

    for index, entry in enumerate(summaries, start=1):
        print("{:<4} {:<12} {:<6} {:<14} {:<22} {:>10}".format(
            index,
            str(entry.get("sample_id"))[:12],
            str(entry.get("slot_id") or "-"),
            str(entry.get("state"))[:14],
            str(entry.get("best_match") or "-")[:22],
            score(entry.get("best_similarity")),
        ))

    return summaries


def print_full_sample(record):
    banner("SAMPLE {}".format(record.get("sample_id")))

    timestamps = record.get("timestamps") or {}
    metadata = record.get("metadata") or {}

    print("Slot:        {}".format(record.get("slot_id")))
    print("State:       {}".format(record.get("state")))
    print("Created:     {}".format(timestamps.get("created_at")))
    print("Loaded:      {}".format(timestamps.get("loaded_at")))
    print("Measured:    {}".format(timestamps.get("measured_at")))

    print()
    print("METADATA")

    for key, label in METADATA_FIELDS:
        print("  {:<18}{}".format(label + ":", metadata.get(key) or "-"))

    measurement = record.get("measurement")

    if not measurement:
        print()
        print("No spectrum has been acquired for this sample yet.")
        print()
        pause()

        return

    calibration = record.get("calibration") or {}

    print()
    print("CALIBRATION")

    # Newer records carry both; older ones only the single legacy id.
    legacy_id = calibration.get("legacy_database_calibration_id") \
        or calibration.get("calibration_id", "-")

    print("  legacy (database):  {}".format(legacy_id))
    print("  active (science):   {}".format(
        calibration.get("active_calibration_id") or "-"
    ))
    print("  {}".format(calibration.get("equation", "-")))

    print()
    print("SENSOR SETTINGS")
    print()
    print_settings_block(measurement.get("sensor_settings"))

    quality_report = record.get("quality")

    if quality_report:
        print()
        print("MEASUREMENT QUALITY")
        print()
        print_quality(quality_report)

    print()
    print("RAW SPECTRUM")
    print()
    print_triad_table(measurement)

    # The White and Dark this sample was actually processed against,
    # snapshotted into the record so the numbers above can be checked
    # by hand without opening calibration_legacy.json.
    dark = calibration.get("dark_reference")
    white = calibration.get("white_reference")

    if dark and white:
        print()
        print("WHITE / DARK PROCESSING (legacy calibration)")
        print()
        print_processing_table(measurement, dark, white)

    matches = record.get("reference_matches") or []

    print()
    print("DATABASE COMPARISON ({} materials)".format(len(matches)))
    print()

    # A record written before the multi-metric release has only cosine.
    if matches and matches[0].get("rmse") is not None:
        print_metric_table(matches)
        print()
        print_agreement(record.get("metric_agreement"))

    else:
        print_matches(matches)

    analysis = record.get("analysis")

    print()
    print("RESULT")
    print()

    if analysis:
        print_result_block(analysis)
    else:
        print("Analysis status: {} - {}".format(
            record.get("analysis_status"), record.get("analysis_error")
        ))

    print()
    pause()


def pick_sample(summaries, prompt="Sample number"):
    index = ask_int(prompt, 1, len(summaries))

    if index is None:
        return None

    return summaries[index - 1].get("sample_id")


def menu_sample_database(mission):
    """
    View and manage saved samples.

    Measurement fields are read-only here: raw, dark_corrected,
    normalized, sensor settings, matches, analysis and calibration are
    scientific results, not editable text. Metadata may be corrected.
    """
    store = mission.store

    while True:
        banner("SAMPLE DATABASE")

        summaries = print_sample_table(store)

        print()
        print("[1] Open sample")
        print("[2] Edit metadata")
        print("[3] Rename sample")
        print("[4] Delete sample")
        print("[5] Refresh")
        print()
        print("[6] Import ALL Samples from ESP32")
        print("[7] Delete ALL Samples from ESP32")
        print()
        print("[0] Back")

        selection = choose()

        try:
            if selection == "0":
                return

            if selection == "5":
                store.load()

                continue

            if selection == "6":
                sync_esp32_samples(mission)
                store.load()

                continue

            if selection == "7":
                delete_esp32_samples(mission)

                continue

            if not summaries:
                if selection:
                    print("There are no samples yet.")

                continue

            if selection == "1":
                sample_id = pick_sample(summaries)

                if sample_id:
                    record = store.get_sample(sample_id)

                    if record is None:
                        print("Record file for {} is missing.".format(
                            sample_id
                        ))
                    else:
                        print_full_sample(record)

            elif selection == "2":
                sample_id = pick_sample(summaries)

                if sample_id:
                    metadata = ask_metadata()

                    if metadata:
                        store.update_metadata(sample_id, metadata)
                        print("Metadata updated.")
                    else:
                        print("Nothing entered; metadata unchanged.")

            elif selection == "3":
                sample_id = pick_sample(summaries)

                if sample_id:
                    new_id = ask("New Sample ID (blank = cancel)")

                    if new_id:
                        store.rename(sample_id, new_id)
                        print("Renamed {} -> {}.".format(sample_id, new_id))

            elif selection == "4":
                sample_id = pick_sample(summaries)

                if sample_id:
                    print()
                    print("This permanently deletes the scientific record "
                          "for {}.".format(sample_id))
                    print("Clearing a physical slot is a different thing "
                          "and keeps the record.")
                    print()

                    typed = ask(
                        "Type the Sample ID to confirm deletion"
                    )

                    if typed == sample_id:
                        store.delete(sample_id)
                        print("Sample {} deleted.".format(sample_id))
                    else:
                        print("Not deleted.")

            elif selection:
                print("Unknown option.")

        except StorageError as error:
            print()
            print("Storage error: {} ({})".format(error.message, error.code))


# ======================================================================
# ESP32 -> PC sample synchronization
# ======================================================================

def sync_esp32_samples(mission):
    """
    Copy every acquisition the ESP32 holds into the PC archive.

    This is a COPY, never a move: the ESP32 keeps its records, and an ID
    that already exists on the PC is never overwritten. Running it twice
    therefore transfers nothing the second time.

        ID not on the PC              -> IMPORT
        ID on the PC, same spectrum   -> SKIP
        ID on the PC, different data  -> CONFLICT, left untouched

    It writes only to the measured-Sample archive. The material database
    and the White/Dark references are never opened for writing anywhere
    in this program.
    """
    banner("IMPORT ESP32 SAMPLES TO PC")

    try:
        index = mission.link.list_saved_samples()

    except (LinkError, TimeoutError) as error:
        print("Reading Sample index............ FAIL")
        report_failure(error)
        print()
        pause()

        return

    print("Connecting to ESP32............. PASS")
    print("Reading Sample index............ PASS")
    print()

    entries = index.get("samples") or []

    print("ESP32 Samples: {}".format(len(entries)))
    print()

    if not entries:
        print("The ESP32 is holding no acquisitions. Its buffer is RAM "
              "only and is empty after a reset.")
        print()
        pause()

        return

    imported = []
    skipped = []
    conflicts = []
    failed = []

    for entry in entries:
        sample_id = entry.get("sample_id")

        if not sample_id:
            continue

        try:
            payload = mission.link.get_saved_sample(sample_id)

        except (LinkError, TimeoutError) as error:
            failed.append(sample_id)
            print("{:<12}FAILED              {}".format(sample_id, error))

            continue

        device_raw = _white_spectrum(payload.get("measurement"))
        existing = mission.store.get_sample(sample_id)

        if existing is not None:
            stored_raw = _white_spectrum(existing.get("measurement"))

            if _same_spectrum(stored_raw, device_raw):
                skipped.append(sample_id)
                print("{:<12}already exists      SKIP".format(sample_id))

            else:
                conflicts.append(sample_id)
                print("{:<12}conflict            CONFLICT".format(sample_id))

            continue

        try:
            mission.store.save(
                _build_record(mission, sample_id, entry, payload)
            )

            imported.append(sample_id)
            print("{:<12}imported            PASS".format(sample_id))

        except StorageError as error:
            failed.append(sample_id)
            print("{:<12}FAILED              {}".format(
                sample_id, error.message
            ))

    print()
    print("Imported:   {}".format(len(imported)))
    print("Skipped:    {}".format(len(skipped)))
    print("Conflicts:  {}".format(len(conflicts)))
    print("Failed:     {}".format(len(failed)))

    if imported:
        print()
        print("Imported:")

        for sample_id in imported:
            print("  {}".format(sample_id))

    if conflicts:
        print()
        print("CONFLICTS - the PC already has these IDs with DIFFERENT")
        print("measurement data. Nothing was overwritten. Rename or delete")
        print("the PC record first if the device copy is the one you want:")

        for sample_id in conflicts:
            print("  {}".format(sample_id))

    print()
    print("ESP32 database was NOT modified.")
    print("PC Sample archive updated: {}".format(mission.store.archive_path))
    print()
    pause()


def _white_spectrum(measurement):
    """
    The WHITE 18-channel spectrum out of any measurement shape.

    Handles the device's live acquisition block, an archived record from
    this release, and a bare 18-channel dict from before it - which is
    what makes an identical/conflicting comparison possible across the
    schema change.
    """
    measurement = measurement or {}

    blocks = measurement.get("illuminations")

    if isinstance(blocks, dict):
        acquisitions = (blocks.get("white") or {}).get("acquisitions") or []

        return acquisitions[0] if acquisitions else {}

    raw = measurement.get("raw") or {}

    if isinstance(raw.get("white"), dict):
        return raw["white"]

    return raw


def _same_spectrum(first, second):
    """
    Whether two raw spectra are the same measurement.

    Compared channel by channel with a tolerance, because a value that
    has been through JSON and a round() is not always bit-identical to
    the one that came off the sensor.
    """
    if not first or not second:
        return False

    for channel in sample_analysis.CHANNELS:
        if channel not in first or channel not in second:
            return False

        try:
            if abs(float(first[channel]) - float(second[channel])) > 1e-4:
                return False

        except (TypeError, ValueError):
            return False

    return True


def _build_record(mission, sample_id, entry, payload):
    """
    Turn one retained acquisition into a PC Sample record.

    Every field the device actually stores is copied faithfully; nothing
    is invented. Analysis is run through the normal BD path so the
    imported Sample is a complete record rather than a stub - and if BD
    is unavailable the raw spectrum is stored anyway with
    analysis_status FAILED.
    """
    measurement = payload.get("measurement") or {}

    # The device buffer holds whatever the acquisition returned - the
    # WHITE/UV/IR protocol on current firmware, a bare white spectrum on
    # older. analyse_raw accepts both.
    settings = measurement.get("sensor_settings")
    raw = measurement.get("raw") or {}

    record = {
        "sample_id": sample_id,
        "slot_id": payload.get("slot_id") or entry.get("slot_id"),
        "state": STATE_MEASURED,
        "timestamps": {
            "created_at": None,
            "loaded_at": None,
            "measured_at": utc_timestamp(),
        },
        "metadata": None,
        "source": {
            "origin": "esp32_sync",
            "esp_uptime_ms": measurement.get("esp_uptime_ms"),
            "note": "Copied from the ESP32 acquisition buffer. Timestamps "
                    "and metadata were not recorded on the device.",
        },
        "missing_information": [
            "created_at", "loaded_at", "metadata",
        ],
    }

    try:
        result = mission.analyse_raw(measurement, settings)

    except Exception as error:
        record.update({
            "measured": bool(raw),
            "measurement": {
                "wavelengths": sample_analysis.channel_wavelengths(),
                "raw": raw,
                "dark_corrected": None,
                "normalized": None,
                "sensor_settings": settings,
            },
            "calibration": None,
            "database": None,
            "reference_matches": [],
            "analysis": None,
            "analysis_status": "FAILED",
            "analysis_error": "{}: {}".format(type(error).__name__, error),
        })

        return record

    return apply_measurement(record, result)


def delete_esp32_samples(mission):
    """
    Delete every Sample record held on the ESP32.

    Destructive and deliberately narrow. It removes the device's own
    records and nothing else: the PC archive, the physical slot states,
    the material database and the White/Dark references are all
    untouched.

    Import first if the data matters - this is not chained to the import
    on purpose, so the operator can verify the copy before destroying
    the original.
    """
    banner("DELETE ALL ESP32 SAMPLES")

    try:
        index = mission.link.list_saved_samples()

    except (LinkError, TimeoutError) as error:
        report_failure(error)
        print()
        pause()

        return

    entries = index.get("samples") or []

    if not entries:
        print("ESP32 Sample storage is already empty.")
        print()
        pause()

        return

    print("ESP32 Samples: {}".format(len(entries)))
    print()

    for entry in entries:
        on_pc = mission.store.has_sample(entry.get("sample_id"))

        print("  {:<12}{}".format(
            entry.get("sample_id"),
            "already imported to PC" if on_pc else "NOT ON THE PC YET",
        ))

    missing = [
        entry.get("sample_id") for entry in entries
        if not mission.store.has_sample(entry.get("sample_id"))
    ]

    if missing:
        print()
        print("!! {} of these are NOT in the PC archive. Import them "
              "first, or they are gone for good.".format(len(missing)))

    print()
    print("PC Samples, the material database, the White/Dark references")
    print("and the physical slot states will NOT be changed.")
    print()

    if ask("Delete ALL saved Samples from ESP32? [y/N]").strip().lower() \
            not in ("y", "yes"):
        print("Cancelled.")
        print()
        pause()

        return

    print()
    print("Deleting...")

    try:
        data = mission.link.delete_saved_samples()

    except (LinkError, TimeoutError) as error:
        report_failure(error)
        print()
        pause()

        return

    print("Deleted: {}".format(data.get("deleted_count", 0)))

    # Do not trust the return code. Ask the device what it still holds.
    try:
        after = mission.link.list_saved_samples()

    except (LinkError, TimeoutError) as error:
        print()
        print("DELETE VERIFICATION FAILED - could not read the device back:")
        report_failure(error)
        print()
        pause()

        return

    remaining = after.get("samples") or []

    if remaining:
        print()
        print("DELETE VERIFICATION FAILED")
        print("The device reported success but still holds {} "
              "record(s):".format(len(remaining)))

        for entry in remaining:
            print("  {}".format(entry.get("sample_id")))

    else:
        print()
        print("ESP32 Sample storage is now empty.")
        print("PC Sample archive was not modified.")
        print("Physical slot occupancy was not changed.")

    print()
    pause()


# ======================================================================
# tools menu
# ======================================================================

TOOLS_MENU = (
    ("1", "Sample Database", "View and manage saved Samples.",
     lambda mission, status, view: menu_sample_database(mission)),
    ("2", "System Status", "Show PC, database and hardware state.",
     lambda mission, status, view: (print_system_status(mission), pause())),
    ("3", "Re-sync Carousel", "Restore physical position tracking.",
     lambda mission, status, view: menu_resync(mission)),
    ("4", "Servo / Carousel Tools",
     "ST3215 diagnostics, movement tests and manual movement.",
     lambda mission, status, view: menu_servo_test(mission)),
    ("5", "Sensor Test / Calibration",
     "Test the sensor and analysis pipeline; create a full calibration.",
     lambda mission, status, view: menu_sensor_test(mission)),
    ("6", "Clear Physical Slot", "Free a physical carousel slot.",
     menu_clear_slot),
    ("7", "Sync ESP32 Samples to PC",
     "Copy acquisitions held on the ESP32 into the PC archive.",
     lambda mission, status, view: sync_esp32_samples(mission)),
    ("8", "Decision Learning History",
     "What the system has measured, what it concluded, and what the "
     "samples actually were.",
     lambda mission, status, view: menu_learning_history(mission)),
)


def menu_tools(mission, status, view):
    while True:
        banner("TOOLS / RECORDS")

        for key, label, description, _handler in TOOLS_MENU:
            print("[{}] {}".format(key, label))
            print("    {}".format(description))
            print()

        print("[0] Back")

        selection = choose()

        if selection == "0":
            return

        handler = None

        for key, _label, _description, action in TOOLS_MENU:
            if selection == key:
                handler = action

                break

        if handler is None:
            if selection:
                print("Unknown option.")

            continue

        try:
            handler(mission, status, view)

            # Slot and carousel state may have changed underneath us.
            status = mission.hardware_status()
            view = mission.slot_view(status)

        except LinkError as error:
            report_link_error(error)

        except TimeoutError as error:
            print()
            print("Timeout: {}".format(error))

        except KeyboardInterrupt:
            print()
            print("Cancelled.")


# ======================================================================
# help
# ======================================================================

HELP_TEXT = """NORMAL COMPETITION WORKFLOW

  0. Initial Carousel Calibration - once, Slot 1 under the loading hole
  1. Choose Sample / Slot
  2. Prepare Sample          (creates the persistent record)
  3. Rover arm deposits soil
  4. Confirm Sample Loaded
  5. Measure Sample          (180 deg out, RAW, 180 deg back, saved)

CAROUSEL

  4 slots, 90 degrees apart. The scanner sits 180 degrees - two slots -
  from the loading hole:

      Loader Slot 1  ->  Scanner Slot 3
      Loader Slot 2  ->  Scanner Slot 4
      Loader Slot 3  ->  Scanner Slot 1
      Loader Slot 4  ->  Scanner Slot 2

  Measure Sample swings the slot out to the scanner and back again, so a
  successful measurement ends with the sample exactly where it started.

  The carousel is driven by an ST3215 serial bus servo with a 4096-count
  absolute encoder, so every movement is commanded in counts and then
  checked against the encoder before the software believes it. One slot
  is 1024 counts; the loader/scanner sweep is 2048. A movement that
  cannot be verified is reported as a failure and the tracked position is
  dropped - the software never assumes a movement worked.

  Initial Carousel Calibration is still needed after every power-up. The
  encoder knows exactly where the servo is, but nothing can tell it which
  physical slot you call Slot 1.

CALIBRATION

  The module uses one fixed Dark and one fixed White reference stored
  in firmware/BD/data/calibration_legacy.json. They were accepted before the
  competition and are used for every sample. You are never asked to
  measure White or Dark.

      C = Sample - Dark
      R = (Sample - Dark) / (White - Dark)

WHERE THINGS LIVE

  ESP32   carousel + AS7265x, RAW spectra only
  BD      White/Dark, material database, all analysis
  PC      workflow, Sample records, this interface

  Saved samples: firmware/PC/data/

MEASURED IS NOT EMPTY

  The soil physically stays in the slot after a measurement. Use
  Tools -> Clear Physical Slot when it has been removed. That keeps the
  scientific record; Delete Sample is what removes it.

SYNC ESP32 SAMPLES TO PC

  The ESP32 holds the last raw acquisition per slot in RAM. If this
  program lost a result - a crash, a restart, a different laptop -
  Tools -> Sync ESP32 Samples to PC copies it into the archive. It never
  overwrites an existing Sample and never deletes the ESP32 copy."""


def menu_help(mission, status, view):
    banner("HELP")

    print(HELP_TEXT)
    print()
    pause()


# ======================================================================
# main screen
# ======================================================================

def action_labels(entry, carousel):
    """What the operator can actually do right now."""
    state = entry.get("state", STATE_EMPTY)
    phase = carousel.get("carousel_phase")

    labels = {"2": "", "3": "", "4": ""}

    if state == STATE_EMPTY:
        labels["2"] = "[AVAILABLE]"
        labels["3"] = "[LOCKED - no sample prepared]"
        labels["4"] = "[LOCKED - no sample prepared]"

    elif state == STATE_READY_TO_LOAD:
        labels["2"] = "[DONE]"
        labels["3"] = "[AVAILABLE]"
        labels["4"] = "[LOCKED - sample not confirmed]"

    elif state == STATE_LOADED:
        labels["2"] = "[DONE]"
        labels["3"] = "[DONE]"
        labels["4"] = (
            "[AVAILABLE]" if phase == "LOAD"
            else "[LOCKED - sample not at loading hole]"
        )

    elif state == STATE_MEASURED:
        labels["2"] = "[DONE]"
        labels["3"] = "[DONE]"
        labels["4"] = "[DONE - MEASURED]"

    return labels


def print_main_screen(mission, status, view):
    carousel = status.get("carousel") or {}
    selected = carousel.get("selected_slot")
    entry = mission.entry_for(view, selected) if selected else {}

    banner("FREYA SCIENCE MODULE")

    print("Selected: Slot {} / {}".format(
        selected, entry.get("sample_id") or "----"
    ))
    print("State:    {}".format(entry.get("state", "-")))
    print("Position: {}".format(carousel.get("carousel_phase", "?")))

    # Which actuator is driving the carousel is always on screen, together
    # with whether it can verify its own movements. Those two facts change
    # what a measurement is worth.
    servo = status.get("servo") or {}
    capabilities = servo.get("capabilities") or {}

    print("Servo:    {}{}".format(
        servo.get("label", "NOT SELECTED"),
        " (encoder verified)" if capabilities.get("verified_movement")
        else " (timed, open loop)" if servo.get("selected") else "",
    ))

    print()
    print("Loader: Slot {}    Scanner: Slot {}".format(
        carousel.get("current_load_slot"),
        carousel.get("current_scan_slot"),
    ))
    print()

    for item in view:
        print("{}  {:<8} {}".format(
            item["slot_id"], item["sample_id"] or "----", item["state"]
        ))

    sensor = status.get("sensor") or {}

    if not sensor.get("ready"):
        print()
        print("Sensor: UNAVAILABLE - it will be retried automatically on "
              "the next measurement or sensor test.")

    if mission.science_error:
        print()
        print("BD: {}".format(mission.science_error))

    if mission.active_calibration is None:
        print()
        print("NO ACTIVE CALIBRATION - UV/IR reflectance is not available.")
        print("Tools -> Sensor Test -> [7] to select a stored calibration,")
        print("or [3] to make one. The material library is unaffected.")

    labels = action_labels(entry, carousel)

    print()
    print("[1] Choose Sample / Slot")
    print("[2] Prepare Sample {}".format(labels["2"]))
    print("[3] Confirm Sample Loaded {}".format(labels["3"]))
    print("[4] Measure Sample {}".format(labels["4"]))
    print("[5] Fine Carousel Alignment")
    print()
    print("[t] Tools / Records")
    print("[h] Help")
    print("[q] Exit")


def print_startup_screen(servo_label=None):
    banner("FREYA SCIENCE MODULE")

    print("Carousel servo: {}".format(servo_label or "NOT SELECTED"))
    print("Carousel:       NOT CALIBRATED")
    print()
    print("Before working with samples:")
    print("  1. connect the ST3215 carousel servo")
    print("  2. align physical Slot 1 with the soil loading hole")
    print()
    print("[0] Carousel Setup")
    print("[t] Tools / Records")
    print("[h] Help")
    print("[q] Exit")


MAIN_ACTIONS = {
    "1": menu_choose_slot,
    "2": menu_prepare,
    "3": menu_confirm,
    "4": menu_measure,
    "5": lambda mission, status, view: menu_fine_adjust(mission),
    "t": menu_tools,
    "h": menu_help,
}


def interactive(link):
    """Calibrate the carousel once, then run the sample loop."""
    print("Connecting to the science module on {}...".format(link.port))

    try:
        link.wait_online()

    except (LinkError, TimeoutError) as error:
        print("No answer from the science module: {}".format(error))
        print()
        print("Check that the USB cable is connected, that the port is "
              "correct, and that no other program (REPL, mpremote, serial "
              "monitor) is holding the port open.")

        return 1

    print("Connection: ONLINE")

    mission = Mission(link)

    if mission.science_error:
        print("BD warning: {}".format(mission.science_error))

    while True:
        try:
            status = mission.hardware_status()

        except (LinkError, TimeoutError) as error:
            print()
            print("Could not read the hardware state: {}".format(error))

            if choose("Retry? [Y/n]").startswith("n"):
                return 1

            continue

        view = mission.slot_view(status)
        carousel = status.get("carousel") or {}

        if not carousel.get("position_valid"):
            print_startup_screen((status.get("servo") or {}).get("label"))

            selection = choose()

            if selection == "q":
                return 0

            try:
                if selection == "0":
                    menu_initial_calibration(mission)

                elif selection == "t":
                    menu_tools(mission, status, view)

                elif selection == "h":
                    menu_help(mission, status, view)

                elif selection:
                    print("Unknown option.")

            except LinkError as error:
                report_link_error(error)

            except TimeoutError as error:
                print("Timeout: {}".format(error))

            except KeyboardInterrupt:
                print()
                print("Cancelled.")

            continue

        print_main_screen(mission, status, view)

        selection = choose()

        if selection == "q":
            return 0

        handler = MAIN_ACTIONS.get(selection)

        if handler is None:
            if selection:
                print("Unknown option.")

            continue

        try:
            handler(mission, status, view)

        except LinkError as error:
            report_link_error(error)

        except TimeoutError as error:
            print()
            print("Timeout: {}".format(error))

        except StorageError as error:
            print()
            print("Storage error: {} ({})".format(error.message, error.code))

        except KeyboardInterrupt:
            print()
            print("Cancelled.")


# ======================================================================
# entry point
# ======================================================================

def one_shot(link, command, payload_text):
    payload = json.loads(payload_text) if payload_text else {}

    if not isinstance(payload, dict):
        print("--payload must be a JSON object.", file=sys.stderr)

        return 2

    link.wait_online()

    print(json.dumps(
        link.request(command, timeout=MEASUREMENT_TIMEOUT, **payload),
        indent=2,
    ))

    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="Main-PC application for the Freya AS7265x science "
                    "module."
    )

    parser.add_argument(
        "--port", required=True,
        help="Serial port of the ESP32, e.g. COM4 or /dev/ttyUSB0.",
    )
    parser.add_argument(
        "--baud", type=int, default=DEFAULT_BAUDRATE,
        help="Baud rate (default: {}).".format(DEFAULT_BAUDRATE),
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT,
        help="Response timeout for ordinary commands (default: "
             "{:.0f} s).".format(DEFAULT_TIMEOUT),
    )
    parser.add_argument(
        "--connect-timeout", type=float, default=CONNECT_TIMEOUT,
        help="Seconds to wait for the module to answer a ping after the "
             "port is opened (default: {:.0f}).".format(CONNECT_TIMEOUT),
    )
    parser.add_argument(
        "--command",
        help="Run a single hardware command and print the JSON result "
             "instead of opening the menu.",
    )
    parser.add_argument(
        "--payload",
        help="JSON object with extra fields for --command, e.g. "
             "'{\"slot\": 1}'.",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Echo the raw protocol traffic.",
    )

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    try:
        link = ESP32Link(
            port=args.port,
            baudrate=args.baud,
            timeout=args.timeout,
            connect_timeout=args.connect_timeout,
            verbose=args.verbose,
        )

    except RuntimeError as error:
        print(error, file=sys.stderr)

        return 2

    try:
        link.open()

    except Exception as error:  # serial.SerialException and friends
        print("Could not open {}: {}".format(args.port, error),
              file=sys.stderr)
        print("If the port is busy, close any REPL, mpremote session or "
              "serial monitor holding it.", file=sys.stderr)

        return 2

    try:
        if args.command:
            return one_shot(link, args.command, args.payload)

        return interactive(link)

    except LinkError as error:
        print("Module refused the command: {}".format(error), file=sys.stderr)

        return 1

    except TimeoutError as error:
        print("Timeout: {}".format(error), file=sys.stderr)

        return 1

    except KeyboardInterrupt:
        print()

        return 0

    finally:
        link.close()


if __name__ == "__main__":
    sys.exit(main())
