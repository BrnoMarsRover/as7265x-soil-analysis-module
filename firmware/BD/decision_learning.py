"""
The decision learning database — what was measured, what was concluded,
and what the sample actually was.

PC-side only. It never goes near the ESP32.

WHAT THIS IS NOT
----------------
It is not a fourth reference database. DB1 and DB2 say what a material
LOOKS LIKE and are measured evidence; this says what the system has SEEN
and how often it was right. Nothing in this module can modify a measured
reference spectrum, and `test_architecture` asserts that it does not
import anything that could.

THE FOUR RULES THIS SCHEMA EXISTS TO ENFORCE
--------------------------------------------
1. **A prediction is never ground truth.** They are separate tables with
   separate provenance. `add_ground_truth` refuses a label whose source is
   a model. Without that rule a system trained on its own output learns
   its own mistakes and grows more confident with every round.

2. **A historical prediction is never rewritten.** When model v7
   re-analyses a measurement that v3 got wrong, v7's answer is a NEW row.
   Both survive, which is what makes regression tracking possible at all.

3. **A raw measurement is never edited.** The observation carries the raw
   spectra it was taken with and a hash of them. Re-deriving reflectance
   under a new calibration produces a new derived record, never a change
   to the stored counts.

4. **Only trusted labels train.** `training_set()` returns VERIFIED
   labels by default. OPERATOR_ASSERTED must be asked for explicitly, and
   UNVERIFIED / UNKNOWN can never be requested at all - they are not
   labels, they are the absence of one.

WHY SQLITE
----------
This table grows without bound and is read by selection, join and
aggregate: "every VERIFIED carbonate observation under profile P,
excluding session S, that model v4 got wrong". A JSON file answers that
by loading everything into memory and looping.

Layer rule: BD must never import Science or Science.decision.
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from BD import config

SCHEMA_VERSION = config.DECISION_LEARNING_SCHEMA_VERSION

# What kind of statement the ground truth is.
LABEL_EXACT_MATERIAL = "EXACT_MATERIAL"
LABEL_FAMILY = "MATERIAL_FAMILY"
LABEL_PREPARED_MIXTURE = "PREPARED_MIXTURE"
LABEL_UNKNOWN_SAMPLE = "UNKNOWN_SAMPLE"
LABEL_NONE = "NO_LABEL"

LABEL_TYPES = (
    LABEL_EXACT_MATERIAL, LABEL_FAMILY, LABEL_PREPARED_MIXTURE,
    LABEL_UNKNOWN_SAMPLE, LABEL_NONE,
)

# ----------------------------------------------------------------------
# WHAT A MIXTURE COMPONENT IS
#
# A prepared mixture has two kinds of ingredient and they are not
# interchangeable:
#
#   COMPONENT  a library material, weighed in deliberately.
#              "0.10 of Iron(III) Oxide Red"
#   MATRIX     what it was mixed INTO. Ordinary soil, sand, the local
#              regolith simulant - a real substance with a real mass that
#              is NOT in any library and has no reference spectrum.
#
# The matrix has to be first-class rather than "the leftover fraction",
# because it is most of the sample and most of the signal. A model asked
# to find 10% hematite in garden soil is being asked to find it against
# that soil, and an evaluation that does not know what the other 90% was
# cannot say whether a miss was the model's fault or the matrix's.
ROLE_COMPONENT = "COMPONENT"
ROLE_MATRIX = "MATRIX"

COMPONENT_ROLES = (ROLE_COMPONENT, ROLE_MATRIX)

# Prepared fractions are weighed, not estimated, so they should add up.
# The tolerance is for scale resolution and arithmetic, not for a guess:
# 5 parts in 1000 is about what a 0.01 g kitchen scale gives on a 20 g
# sample.
FRACTION_SUM_TOLERANCE = 0.005

# How the sample was physically presented. These are the variables the
# operator can change between measurements of the same material, and
# they are recorded so a difference in the spectrum can be attributed to
# one of them instead of to the material.
PACKING_STATES = ("LOOSE", "TAMPED", "PRESSED", "UNKNOWN")
MOISTURE_STATES = ("OVEN_DRY", "AIR_DRY", "DAMP", "WET", "UNKNOWN")

# How much the label is worth. The order matters: it is the trust ladder.
VERIFIED = "VERIFIED"
OPERATOR_ASSERTED = "OPERATOR_ASSERTED"
UNVERIFIED = "UNVERIFIED"
UNKNOWN = "UNKNOWN"

VERIFICATION_LEVELS = (VERIFIED, OPERATOR_ASSERTED, UNVERIFIED, UNKNOWN)

# The only levels that may ever become supervised training labels, and
# VERIFIED is the only one used unless a caller explicitly asks for more.
TRUSTED_LEVELS = (VERIFIED,)
TRAINABLE_LEVELS = (VERIFIED, OPERATOR_ASSERTED)

# A ground-truth source that names a model is refused outright: see rule 1.
FORBIDDEN_LABEL_SOURCES = (
    "decision_model", "model", "prediction", "inference", "classifier",
    "self", "auto",
)


class LearningError(Exception):
    """The learning database refused an operation."""

    def __init__(self, code, message, details=None):
        super().__init__(message)

        self.code = code
        self.message = message
        self.details = details or {}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def normalize_mixture(components):
    """
    Validate and canonicalise the components of a PREPARED mixture.

    Returns the cleaned list. Raises LearningError naming the specific
    problem, because every refusal here is preventing a training example
    that would teach the wrong thing:

        a fraction outside 0-1        is not a fraction
        fractions that do not add up  means something was not weighed,
                                      and the model would learn the
                                      missing mass as belonging to
                                      whatever WAS listed
        the same material twice       is two answers to one question
        a component with no identity  cannot be scored against anything

    ONLY MASS FRACTIONS ARE ACCEPTED, and they are stored under a name
    that says so. Spectral contribution is what an unmixing algorithm
    estimates; prepared mass fraction is what the operator weighed. The
    entire value of a prepared mixture is that those two are independent,
    so the estimate can be scored against the fact. Storing them in one
    field would destroy exactly the thing the record exists for.
    """
    if not isinstance(components, (list, tuple)) or not components:
        raise LearningError(
            "MIXTURE_EMPTY",
            "A PREPARED_MIXTURE label needs at least one component. A "
            "mixture nobody can name the parts of is an unknown sample, "
            "and there is a label for that.",
        )

    cleaned = []
    seen = set()
    total = 0.0
    weighed = 0

    for index, component in enumerate(components):
        if not isinstance(component, dict):
            raise LearningError(
                "MIXTURE_COMPONENT_MALFORMED",
                "Component {} is not a record.".format(index + 1),
            )

        role = component.get("role") or ROLE_COMPONENT

        if role not in COMPONENT_ROLES:
            raise LearningError(
                "MIXTURE_ROLE_UNKNOWN",
                "Component {} has role {!r}; expected one of {}.".format(
                    index + 1, role, ", ".join(COMPONENT_ROLES)
                ),
            )

        material_key = component.get("material_key")
        matrix_label = component.get("matrix_label")

        if role == ROLE_COMPONENT and not material_key:
            raise LearningError(
                "MIXTURE_COMPONENT_UNIDENTIFIED",
                "Component {} names no material. A component that cannot "
                "be resolved to a library material cannot be scored "
                "against anything - record it as the MATRIX instead, "
                "which is an honest statement that it has no reference "
                "spectrum.".format(index + 1),
            )

        if role == ROLE_MATRIX and not (matrix_label or material_key):
            raise LearningError(
                "MIXTURE_MATRIX_UNNAMED",
                "The matrix of component {} has no name. 'Ordinary soil' "
                "is a perfectly good answer; nothing at all is not, "
                "because the matrix is most of the sample and most of "
                "the signal.".format(index + 1),
            )

        identifier = material_key or "matrix::{}".format(matrix_label)

        if identifier in seen:
            raise LearningError(
                "MIXTURE_COMPONENT_REPEATED",
                "{} appears twice in the mixture. One material, one "
                "fraction.".format(material_key or matrix_label),
            )

        seen.add(identifier)

        fraction = component.get("prepared_mass_fraction")

        if fraction is not None:
            try:
                fraction = float(fraction)

            except (TypeError, ValueError):
                raise LearningError(
                    "MIXTURE_FRACTION_NOT_A_NUMBER",
                    "The fraction of {} is {!r}, which is not a "
                    "number.".format(
                        material_key or matrix_label, fraction
                    ),
                )

            if not 0.0 <= fraction <= 1.0:
                raise LearningError(
                    "MIXTURE_FRACTION_OUT_OF_RANGE",
                    "The fraction of {} is {}. A mass fraction lies "
                    "between 0 and 1; percentages go in as 0.10, not "
                    "10.".format(material_key or matrix_label, fraction),
                )

            total += fraction
            weighed += 1

        mass = component.get("mass_g")

        if mass is not None:
            try:
                mass = float(mass)

            except (TypeError, ValueError):
                raise LearningError(
                    "MIXTURE_MASS_NOT_A_NUMBER",
                    "The mass of {} is {!r}, which is not a number."
                    .format(material_key or matrix_label, mass),
                )

            if mass < 0.0:
                raise LearningError(
                    "MIXTURE_MASS_NEGATIVE",
                    "The mass of {} is negative.".format(
                        material_key or matrix_label
                    ),
                )

        cleaned.append({
            "role": role,
            "material_key": material_key,
            "material_id": component.get("material_id"),
            "family_id": component.get("family_id"),
            "matrix_label": matrix_label,
            "prepared_mass_fraction": fraction,
            "mass_g": mass,
            "note": component.get("note"),
        })

    # All weighed or none weighed. A mixture where two components have
    # fractions and the third does not is not a partially-known mixture;
    # it is a mixture whose composition is unknown, because the two
    # numbers only mean anything relative to a total nobody recorded.
    if weighed and weighed != len(cleaned):
        raise LearningError(
            "MIXTURE_FRACTIONS_INCOMPLETE",
            "{} of {} components carry a prepared fraction. Either every "
            "component is weighed or none is: a fraction is a share of a "
            "total, and a total with a hole in it is not a total."
            .format(weighed, len(cleaned)),
            {"weighed": weighed, "components": len(cleaned)},
        )

    if weighed and abs(total - 1.0) > FRACTION_SUM_TOLERANCE:
        raise LearningError(
            "MIXTURE_FRACTIONS_DO_NOT_SUM",
            "The prepared fractions add up to {:.4f}, not 1.0. The "
            "missing {:+.4f} is real mass that was in the cup and in the "
            "measurement; attributing it to the components that WERE "
            "listed would make every one of them wrong."
            .format(total, 1.0 - total),
            {"total": total, "tolerance": FRACTION_SUM_TOLERANCE},
        )

    return cleaned


def hash_payload(payload):
    """
    Stable SHA256 of a JSON-serialisable payload.

    Sorted keys and no whitespace, so the same content always hashes the
    same regardless of how it was assembled.
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         default=str)

    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One physical measurement. Immutable once written.
CREATE TABLE IF NOT EXISTS observations (
    measurement_id          TEXT PRIMARY KEY,
    created_at              TEXT NOT NULL,
    session_id              TEXT,
    sample_group            TEXT,
    independent_measurement INTEGER NOT NULL DEFAULT 1,
    repeat_index            INTEGER NOT NULL DEFAULT 0,

    acquisition_profile_id  TEXT,
    calibration_id          TEXT,
    legacy_calibration_id   TEXT,

    raw_json                TEXT NOT NULL,
    raw_hash                TEXT NOT NULL,
    sensor_settings_json    TEXT,
    quality_json            TEXT,
    channel_reliability_json TEXT,
    evidence_json           TEXT,
    evidence_schema_version INTEGER,

    archive_reference       TEXT,
    archive_hash            TEXT,

    origin                  TEXT NOT NULL DEFAULT 'MEASURED',
    notes                   TEXT
);

CREATE INDEX IF NOT EXISTS observations_session
    ON observations (session_id);
CREATE INDEX IF NOT EXISTS observations_group
    ON observations (sample_group);

-- What the sample actually was. Never written by a model.
CREATE TABLE IF NOT EXISTS ground_truth (
    measurement_id      TEXT PRIMARY KEY REFERENCES observations
                            (measurement_id),
    recorded_at         TEXT NOT NULL,
    label_type          TEXT NOT NULL,
    material_key        TEXT,
    material_id         TEXT,
    family_id           TEXT,
    mixture_json        TEXT,
    verification_status TEXT NOT NULL,
    verification_source TEXT,
    certainty           REAL,
    note                TEXT
);

-- The components of a PREPARED mixture, one row each.
--
-- The same list is also stored as mixture_json on ground_truth, which is
-- the verbatim label. This table is the QUERYABLE form of it, and it
-- exists because the questions worth asking are per-component:
--
--     every observation containing Iron(III) Oxide Red above 5%
--     what has ever been mixed into 'garden soil'
--     how the error on a component varies with its own fraction
--
-- None of those can be asked of a JSON blob without loading every row.
-- Written only by add_ground_truth, from the same validated list, so the
-- two forms cannot disagree.
CREATE TABLE IF NOT EXISTS ground_truth_components (
    measurement_id         TEXT NOT NULL REFERENCES observations
                               (measurement_id),
    component_index        INTEGER NOT NULL,
    role                   TEXT NOT NULL,
    material_key           TEXT,
    material_id            TEXT,
    family_id              TEXT,
    matrix_label           TEXT,
    prepared_mass_fraction REAL,
    mass_g                 REAL,
    note                   TEXT,
    PRIMARY KEY (measurement_id, component_index)
);

CREATE INDEX IF NOT EXISTS gt_components_material
    ON ground_truth_components (material_key);
CREATE INDEX IF NOT EXISTS gt_components_fraction
    ON ground_truth_components (material_key, prepared_mass_fraction);

-- How the sample was physically presented to the sensor.
--
-- Sensor settings live in the acquisition profile; this is about the
-- SAMPLE, and it changes between two measurements that share a profile
-- exactly. Distance, how much powder was in the cup, how hard it was
-- tamped and how wet it was all move the spectrum, and without them a
-- model trying to learn a material learns the operator's habits
-- instead.
--
-- Every field is optional and NULL means NOT RECORDED, never a default.
-- "Distance unknown" and "distance 30 mm" must stay distinguishable:
-- two measurements that both say unknown are not thereby known to
-- match.
CREATE TABLE IF NOT EXISTS sample_context (
    measurement_id      TEXT PRIMARY KEY REFERENCES observations
                            (measurement_id),
    recorded_at         TEXT NOT NULL,
    sensor_to_sample_mm REAL,
    sample_mass_g       REAL,
    sample_depth_mm     REAL,
    packing             TEXT,
    moisture            TEXT,
    grain_size          TEXT,
    container           TEXT,
    ambient_light       TEXT,
    substrate           TEXT,
    note                TEXT
);

-- What a model concluded. One row per (measurement, model version).
-- Never updated: a newer model writes a newer row.
CREATE TABLE IF NOT EXISTS predictions (
    prediction_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    measurement_id       TEXT NOT NULL REFERENCES observations
                             (measurement_id),
    model_version        TEXT NOT NULL,
    predicted_at         TEXT NOT NULL,
    level                TEXT NOT NULL,
    material_key         TEXT,
    family_id            TEXT,
    candidates_json      TEXT,
    confidence           TEXT,
    decision_json        TEXT,
    superseded           INTEGER NOT NULL DEFAULT 0,
    UNIQUE (measurement_id, model_version)
);

CREATE INDEX IF NOT EXISTS predictions_model
    ON predictions (model_version);

-- Rebuildable statistics over trusted observations. Snapshotted, so a
-- model can say which statistics it was trained against.
CREATE TABLE IF NOT EXISTS class_statistics (
    snapshot_id     TEXT NOT NULL,
    material_key    TEXT NOT NULL,
    feature_space   TEXT NOT NULL,
    built_at        TEXT NOT NULL,
    n_independent   INTEGER NOT NULL,
    statistics_json TEXT NOT NULL,
    source_ids_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, material_key, feature_space)
);

-- One training run: what it used, what it produced, how it scored.
CREATE TABLE IF NOT EXISTS training_runs (
    run_id             TEXT PRIMARY KEY,
    started_at         TEXT NOT NULL,
    model_version      TEXT,
    feature_pipeline   TEXT,
    database_versions_json TEXT,
    training_ids_json  TEXT NOT NULL,
    validation_ids_json TEXT NOT NULL,
    hyperparameters_json TEXT,
    metrics_json       TEXT,
    dataset_hash       TEXT,
    git_commit         TEXT,
    status             TEXT NOT NULL DEFAULT 'EXPERIMENTAL',
    report_json        TEXT
);
"""


class DecisionLearningStore:
    """
    The observation history.

    Every write is one transaction. Every refusal names the rule it is
    enforcing, because a silent refusal in this layer looks exactly like
    a bug.
    """

    def __init__(self, path=None):
        self.path = Path(path or config.DECISION_LEARNING_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        self.connection = sqlite3.connect(str(self.path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")

        self._create()

    def _create(self):
        with self.connection:
            self.connection.executescript(SCHEMA)
            self.connection.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                ("created_at", utc_now()),
            )

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

        return False

    # ------------------------------------------------------------------
    # observations
    # ------------------------------------------------------------------

    def add_observation(self, measurement_id, raw, created_at=None,
                        session_id=None, sample_group=None,
                        independent_measurement=True, repeat_index=0,
                        acquisition_profile_id=None, calibration_id=None,
                        legacy_calibration_id=None, sensor_settings=None,
                        quality=None, channel_reliability=None,
                        evidence=None, evidence_schema_version=None,
                        archive_reference=None, archive_hash=None,
                        origin="MEASURED", notes=None):
        """
        Record one physical measurement. Refuses to overwrite.

        `raw` is the counts as the sensor reported them, per illumination.
        It is hashed on the way in: reproducing a training run later means
        proving the input has not changed since.
        """
        if not measurement_id:
            raise LearningError(
                "MEASUREMENT_ID_REQUIRED",
                "An observation needs an id to be referable.",
            )

        if not isinstance(raw, dict) or not raw:
            raise LearningError(
                "RAW_REQUIRED",
                "An observation without its raw spectra is not an "
                "observation: the whole point is to be able to re-derive "
                "the analysis later.",
            )

        if self.get_observation(measurement_id) is not None:
            raise LearningError(
                "OBSERVATION_EXISTS",
                "{} is already recorded. Science are immutable; a "
                "re-analysis is a new prediction, not an edit."
                .format(measurement_id),
                {"measurement_id": measurement_id},
            )

        with self.connection:
            self.connection.execute(
                """
                INSERT INTO observations (
                    measurement_id, created_at, session_id, sample_group,
                    independent_measurement, repeat_index,
                    acquisition_profile_id, calibration_id,
                    legacy_calibration_id, raw_json, raw_hash,
                    sensor_settings_json, quality_json,
                    channel_reliability_json, evidence_json,
                    evidence_schema_version, archive_reference,
                    archive_hash, origin, notes
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    measurement_id,
                    created_at or utc_now(),
                    session_id,
                    sample_group,
                    1 if independent_measurement else 0,
                    int(repeat_index),
                    acquisition_profile_id,
                    calibration_id,
                    legacy_calibration_id,
                    json.dumps(raw, sort_keys=True),
                    hash_payload(raw),
                    json.dumps(sensor_settings or {}, sort_keys=True),
                    json.dumps(quality) if quality is not None else None,
                    json.dumps(channel_reliability)
                    if channel_reliability is not None else None,
                    json.dumps(evidence) if evidence is not None else None,
                    evidence_schema_version,
                    archive_reference,
                    archive_hash,
                    origin,
                    notes,
                ),
            )

        return self.get_observation(measurement_id)

    def get_observation(self, measurement_id):
        row = self.connection.execute(
            "SELECT * FROM observations WHERE measurement_id = ?",
            (measurement_id,),
        ).fetchone()

        return self._observation_from(row)

    def _observation_from(self, row):
        if row is None:
            return None

        record = dict(row)

        for field, target in (
            ("raw_json", "raw"),
            ("sensor_settings_json", "sensor_settings"),
            ("quality_json", "quality"),
            ("channel_reliability_json", "channel_reliability"),
            ("evidence_json", "evidence"),
        ):
            value = record.pop(field, None)
            record[target] = json.loads(value) if value else None

        record["independent_measurement"] = bool(
            record.get("independent_measurement")
        )

        return record

    def observations(self, session_id=None, origin=None, limit=None):
        query = "SELECT * FROM observations"
        clauses = []
        params = []

        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)

        if origin is not None:
            clauses.append("origin = ?")
            params.append(origin)

        if clauses:
            query += " WHERE " + " AND ".join(clauses)

        query += " ORDER BY created_at, measurement_id"

        if limit:
            query += " LIMIT {}".format(int(limit))

        return [
            self._observation_from(row)
            for row in self.connection.execute(query, params)
        ]

    def count_observations(self):
        return self.connection.execute(
            "SELECT COUNT(*) FROM observations"
        ).fetchone()[0]

    def verify_raw(self, measurement_id):
        """Has the stored raw payload been tampered with since?"""
        row = self.connection.execute(
            "SELECT raw_json, raw_hash FROM observations "
            "WHERE measurement_id = ?",
            (measurement_id,),
        ).fetchone()

        if row is None:
            return None

        return hash_payload(json.loads(row["raw_json"])) == row["raw_hash"]

    # ------------------------------------------------------------------
    # ground truth
    # ------------------------------------------------------------------

    def add_ground_truth(self, measurement_id, label_type,
                         material_key=None, material_id=None,
                         family_id=None, mixture=None,
                         verification_status=UNVERIFIED,
                         verification_source=None, certainty=None,
                         note=None, recorded_at=None, replace=False):
        """
        Record what the sample actually was.

        Refuses a label whose source is a model. That is rule 1, and it
        is enforced here rather than in the caller because this is the
        only door into the table: a system that trains on its own output
        does not converge, it entrenches.
        """
        if self.get_observation(measurement_id) is None:
            raise LearningError(
                "OBSERVATION_NOT_FOUND",
                "Cannot label {}: no such observation.".format(
                    measurement_id
                ),
            )

        if label_type not in LABEL_TYPES:
            raise LearningError(
                "BAD_LABEL_TYPE",
                "label_type must be one of {}.".format(", ".join(LABEL_TYPES)),
            )

        if verification_status not in VERIFICATION_LEVELS:
            raise LearningError(
                "BAD_VERIFICATION",
                "verification_status must be one of {}.".format(
                    ", ".join(VERIFICATION_LEVELS)
                ),
            )

        folded = str(verification_source or "").strip().lower()

        for forbidden in FORBIDDEN_LABEL_SOURCES:
            if forbidden in folded:
                raise LearningError(
                    "PREDICTION_IS_NOT_GROUND_TRUTH",
                    "'{}' names a model as the source of a ground-truth "
                    "label. A prediction can never become ground truth: "
                    "training on it would teach the system its own "
                    "mistakes.".format(verification_source),
                    {"source": verification_source},
                )

        if label_type == LABEL_EXACT_MATERIAL and not material_key:
            raise LearningError(
                "MATERIAL_REQUIRED",
                "An EXACT_MATERIAL label needs the material.",
            )

        if label_type == LABEL_FAMILY and not family_id:
            raise LearningError(
                "FAMILY_REQUIRED",
                "A MATERIAL_FAMILY label needs the family.",
            )

        if label_type == LABEL_PREPARED_MIXTURE:
            # Validated BEFORE anything is written, so a malformed
            # mixture leaves no half-written row behind.
            mixture = normalize_mixture(mixture)

        elif mixture is not None:
            raise LearningError(
                "MIXTURE_ON_NON_MIXTURE_LABEL",
                "A component list was supplied with a {} label. Composition "
                "belongs to PREPARED_MIXTURE; attaching it to anything else "
                "would make a pure material look like a one-component "
                "mixture in every query that counts them."
                .format(label_type),
            )

        if label_type in (LABEL_UNKNOWN_SAMPLE, LABEL_NONE):
            # "I do not know what this was" is a legitimate and useful
            # record, but it is not a label and must never be trained on.
            verification_status = UNKNOWN

        existing = self.get_ground_truth(measurement_id)

        if existing is not None and not replace:
            raise LearningError(
                "GROUND_TRUTH_EXISTS",
                "{} already has ground truth ({}). Pass replace=True to "
                "correct it deliberately.".format(
                    measurement_id, existing.get("material_key")
                    or existing.get("label_type")
                ),
            )

        with self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO ground_truth (
                    measurement_id, recorded_at, label_type, material_key,
                    material_id, family_id, mixture_json,
                    verification_status, verification_source, certainty,
                    note
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    measurement_id,
                    recorded_at or utc_now(),
                    label_type,
                    material_key,
                    material_id,
                    family_id,
                    json.dumps(mixture) if mixture is not None else None,
                    verification_status,
                    verification_source,
                    certainty,
                    note,
                ),
            )

            # The queryable form of the same list. Replaced wholesale
            # rather than merged: a corrected mixture is a different
            # mixture, and leaving a component of the old one behind
            # would make the two forms disagree.
            self.connection.execute(
                "DELETE FROM ground_truth_components WHERE "
                "measurement_id = ?",
                (measurement_id,),
            )

            for index, component in enumerate(mixture or []):
                self.connection.execute(
                    """
                    INSERT INTO ground_truth_components (
                        measurement_id, component_index, role,
                        material_key, material_id, family_id,
                        matrix_label, prepared_mass_fraction, mass_g, note
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        measurement_id,
                        index,
                        component["role"],
                        component["material_key"],
                        component["material_id"],
                        component["family_id"],
                        component["matrix_label"],
                        component["prepared_mass_fraction"],
                        component["mass_g"],
                        component["note"],
                    ),
                )

        return self.get_ground_truth(measurement_id)

    def get_ground_truth(self, measurement_id):
        row = self.connection.execute(
            "SELECT * FROM ground_truth WHERE measurement_id = ?",
            (measurement_id,),
        ).fetchone()

        if row is None:
            return None

        record = dict(row)
        mixture = record.pop("mixture_json", None)
        record["mixture"] = json.loads(mixture) if mixture else None

        return record

    # ------------------------------------------------------------------
    # mixtures
    # ------------------------------------------------------------------

    def components(self, measurement_id):
        """The components of one prepared mixture, in preparation order."""
        return [
            dict(row) for row in self.connection.execute(
                "SELECT * FROM ground_truth_components "
                "WHERE measurement_id = ? ORDER BY component_index",
                (measurement_id,),
            )
        ]

    def observations_containing(self, material_key, min_fraction=None,
                                levels=None):
        """
        Every trusted observation whose sample CONTAINED this material.

        The query the mixture work exists for: "show me everything with
        hematite in it, and how much was in each". A pure material counts
        as containing itself at fraction 1.0, so asking for hematite
        returns the pure jar alongside the 10% spike - which is what a
        detection model needs, because the pure case is the easy end of
        the same curve.

        `min_fraction` filters on the PREPARED fraction, never on an
        estimated one.
        """
        levels = self._trusted(levels)
        placeholders = ",".join("?" for _ in levels)

        query = (
            "SELECT o.*, g.label_type, g.verification_status, "
            "       g.verification_source, g.certainty, "
            "       c.role, c.prepared_mass_fraction, c.mass_g, "
            "       c.matrix_label "
            "FROM observations o "
            "JOIN ground_truth g ON g.measurement_id = o.measurement_id "
            "JOIN ground_truth_components c "
            "     ON c.measurement_id = o.measurement_id "
            "WHERE g.verification_status IN ({}) "
            "  AND c.material_key = ? "
        ).format(placeholders)

        params = list(levels) + [material_key]

        if min_fraction is not None:
            query += "  AND c.prepared_mass_fraction >= ? "
            params.append(float(min_fraction))

        query += " UNION ALL "

        # The pure jar. Same material, fraction 1.0 by definition, and
        # it has no component rows because it is not a mixture.
        pure = (
            "SELECT o.*, g.label_type, g.verification_status, "
            "       g.verification_source, g.certainty, "
            "       ? AS role, 1.0 AS prepared_mass_fraction, "
            "       NULL AS mass_g, NULL AS matrix_label "
            "FROM observations o "
            "JOIN ground_truth g ON g.measurement_id = o.measurement_id "
            "WHERE g.verification_status IN ({}) "
            "  AND g.label_type = ? "
            "  AND g.material_key = ? "
        ).format(placeholders)

        query += pure
        params.extend(
            [ROLE_COMPONENT] + list(levels)
            + [LABEL_EXACT_MATERIAL, material_key]
        )

        query += " ORDER BY prepared_mass_fraction DESC, measurement_id"

        return [
            self._observation_from(row)
            for row in self.connection.execute(query, params)
        ]

    def mixture_training_set(self, levels=None, require_fractions=True):
        """
        Prepared mixtures with their composition, ready to score against.

        This is the validation set for any unmixing model: the spectrum
        on one side, what was actually weighed into the cup on the other.
        `require_fractions` drops mixtures recorded without proportions -
        they say what was IN the sample but not how much, so they can
        score detection and not quantity.
        """
        levels = self._trusted(levels)
        placeholders = ",".join("?" for _ in levels)

        rows = self.connection.execute(
            "SELECT o.*, g.verification_status, g.verification_source, "
            "       g.certainty, g.note AS label_note "
            "FROM observations o "
            "JOIN ground_truth g ON g.measurement_id = o.measurement_id "
            "WHERE g.verification_status IN ({}) "
            "  AND g.label_type = ? "
            "ORDER BY o.created_at, o.measurement_id".format(placeholders),
            list(levels) + [LABEL_PREPARED_MIXTURE],
        ).fetchall()

        records = []

        for row in rows:
            record = self._observation_from(row)
            record["components"] = self.components(record["measurement_id"])
            record["sample_context"] = self.get_sample_context(
                record["measurement_id"]
            )

            if require_fractions and any(
                component["prepared_mass_fraction"] is None
                for component in record["components"]
            ):
                continue

            records.append(record)

        return records

    def mixture_summary(self):
        """
        What the library of prepared mixtures currently covers.

        Reported so the operator can see the gap between what has been
        mixed and what would be needed to validate anything: how many
        mixtures, over which materials, at which fractions, against which
        matrices.
        """
        rows = self.connection.execute(
            "SELECT c.material_key, c.role, c.matrix_label, "
            "       c.prepared_mass_fraction, c.measurement_id "
            "FROM ground_truth_components c "
            "JOIN ground_truth g ON g.measurement_id = c.measurement_id "
            "WHERE g.verification_status IN (?,?)",
            TRAINABLE_LEVELS,
        ).fetchall()

        by_material = {}
        matrices = {}
        mixtures = set()

        for row in rows:
            mixtures.add(row["measurement_id"])

            if row["role"] == ROLE_MATRIX:
                label = row["matrix_label"] or row["material_key"] or "?"
                matrices[label] = matrices.get(label, 0) + 1

                continue

            entry = by_material.setdefault(
                row["material_key"], {"n": 0, "fractions": []}
            )
            entry["n"] += 1

            if row["prepared_mass_fraction"] is not None:
                entry["fractions"].append(row["prepared_mass_fraction"])

        for entry in by_material.values():
            fractions = sorted(entry["fractions"])
            entry["fractions"] = fractions
            entry["lowest_fraction"] = fractions[0] if fractions else None
            entry["highest_fraction"] = fractions[-1] if fractions else None

        return {
            "mixtures": len(mixtures),
            "materials_spiked": len(by_material),
            "by_material": by_material,
            "matrices": matrices,
        }

    @staticmethod
    def _trusted(levels):
        """The trust ladder check, shared by every training-set query."""
        levels = tuple(levels or TRUSTED_LEVELS)
        rejected = [
            level for level in levels if level not in TRAINABLE_LEVELS
        ]

        if rejected:
            raise LearningError(
                "UNTRUSTED_LEVEL_REQUESTED",
                "{} cannot be used as a training label. Only {} may be, "
                "and only VERIFIED is used unless asked for.".format(
                    ", ".join(rejected), " and ".join(TRAINABLE_LEVELS)
                ),
                {"requested": list(levels)},
            )

        return levels

    # ------------------------------------------------------------------
    # how the sample was presented
    # ------------------------------------------------------------------

    def add_sample_context(self, measurement_id, sensor_to_sample_mm=None,
                           sample_mass_g=None, sample_depth_mm=None,
                           packing=None, moisture=None, grain_size=None,
                           container=None, ambient_light=None,
                           substrate=None, note=None, recorded_at=None,
                           replace=False):
        """
        Record how the sample physically sat in front of the sensor.

        Refuses to overwrite silently, like a label does. Distance and
        packing are things the operator OBSERVED at the time; a second
        answer written later is a thing the operator REMEMBERS, and the
        difference between those matters enough to make it deliberate.

        Every field is optional, and omitting one stores NULL - which
        means "not recorded" and never a default. A model that treats an
        unrecorded distance as 30 mm learns a relationship to a number
        nobody measured.
        """
        if self.get_observation(measurement_id) is None:
            raise LearningError(
                "OBSERVATION_NOT_FOUND",
                "Cannot record context for {}: no such observation."
                .format(measurement_id),
            )

        if packing is not None and packing not in PACKING_STATES:
            raise LearningError(
                "BAD_PACKING_STATE",
                "packing must be one of {}.".format(
                    ", ".join(PACKING_STATES)
                ),
            )

        if moisture is not None and moisture not in MOISTURE_STATES:
            raise LearningError(
                "BAD_MOISTURE_STATE",
                "moisture must be one of {}.".format(
                    ", ".join(MOISTURE_STATES)
                ),
            )

        for name, value in (("sensor_to_sample_mm", sensor_to_sample_mm),
                            ("sample_mass_g", sample_mass_g),
                            ("sample_depth_mm", sample_depth_mm)):
            if value is not None and float(value) < 0.0:
                raise LearningError(
                    "NEGATIVE_MEASUREMENT",
                    "{} cannot be negative.".format(name),
                )

        existing = self.get_sample_context(measurement_id)

        if existing is not None and not replace:
            raise LearningError(
                "SAMPLE_CONTEXT_EXISTS",
                "{} already has a recorded sample context. Pass "
                "replace=True to correct it deliberately."
                .format(measurement_id),
            )

        with self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO sample_context (
                    measurement_id, recorded_at, sensor_to_sample_mm,
                    sample_mass_g, sample_depth_mm, packing, moisture,
                    grain_size, container, ambient_light, substrate, note
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    measurement_id,
                    recorded_at or utc_now(),
                    sensor_to_sample_mm,
                    sample_mass_g,
                    sample_depth_mm,
                    packing,
                    moisture,
                    grain_size,
                    container,
                    ambient_light,
                    substrate,
                    note,
                ),
            )

        return self.get_sample_context(measurement_id)

    def get_sample_context(self, measurement_id):
        row = self.connection.execute(
            "SELECT * FROM sample_context WHERE measurement_id = ?",
            (measurement_id,),
        ).fetchone()

        return dict(row) if row is not None else None

    def context_coverage(self):
        """
        How much of the history carries each context field.

        A field recorded on three observations out of forty cannot
        support learning anything about it, and saying so is more useful
        than letting a training run discover it as a null-heavy column.
        """
        total = self.count_observations()
        fields = (
            "sensor_to_sample_mm", "sample_mass_g", "sample_depth_mm",
            "packing", "moisture", "grain_size", "container",
            "ambient_light", "substrate",
        )

        coverage = {}

        for field in fields:
            coverage[field] = self.connection.execute(
                "SELECT COUNT(*) FROM sample_context "
                "WHERE {} IS NOT NULL".format(field)
            ).fetchone()[0]

        return {
            "observations": total,
            "with_any_context": self.connection.execute(
                "SELECT COUNT(*) FROM sample_context"
            ).fetchone()[0],
            "by_field": coverage,
        }

    # ------------------------------------------------------------------
    # predictions
    # ------------------------------------------------------------------

    def add_prediction(self, measurement_id, model_version, level,
                       material_key=None, family_id=None, candidates=None,
                       confidence=None, decision=None, predicted_at=None):
        """
        Record what a model concluded, under its own version.

        Never updates an earlier row. Model v7 disagreeing with v3 is the
        signal, not a correction: keeping both is what makes it possible
        to say whether the system is getting better.
        """
        if self.get_observation(measurement_id) is None:
            raise LearningError(
                "OBSERVATION_NOT_FOUND",
                "Cannot record a prediction for unknown measurement "
                "{}.".format(measurement_id),
            )

        if not model_version:
            raise LearningError(
                "MODEL_VERSION_REQUIRED",
                "A prediction without its model version cannot be "
                "compared with anything later.",
            )

        existing = self.connection.execute(
            "SELECT prediction_id FROM predictions "
            "WHERE measurement_id = ? AND model_version = ?",
            (measurement_id, model_version),
        ).fetchone()

        if existing is not None:
            raise LearningError(
                "PREDICTION_EXISTS",
                "{} already has a prediction from {}. Historical "
                "predictions are immutable - bump the model version."
                .format(measurement_id, model_version),
            )

        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO predictions (
                    measurement_id, model_version, predicted_at, level,
                    material_key, family_id, candidates_json, confidence,
                    decision_json
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    measurement_id,
                    model_version,
                    predicted_at or utc_now(),
                    level,
                    material_key,
                    family_id,
                    json.dumps(candidates) if candidates is not None else None,
                    confidence,
                    json.dumps(decision) if decision is not None else None,
                ),
            )

        return cursor.lastrowid

    def predictions(self, measurement_id=None, model_version=None):
        query = "SELECT * FROM predictions"
        clauses = []
        params = []

        if measurement_id is not None:
            clauses.append("measurement_id = ?")
            params.append(measurement_id)

        if model_version is not None:
            clauses.append("model_version = ?")
            params.append(model_version)

        if clauses:
            query += " WHERE " + " AND ".join(clauses)

        query += " ORDER BY predicted_at, prediction_id"

        records = []

        for row in self.connection.execute(query, params):
            record = dict(row)

            for field, target in (
                ("candidates_json", "candidates"),
                ("decision_json", "decision"),
            ):
                value = record.pop(field, None)
                record[target] = json.loads(value) if value else None

            records.append(record)

        return records

    def model_versions(self):
        return [
            row[0] for row in self.connection.execute(
                "SELECT DISTINCT model_version FROM predictions "
                "ORDER BY model_version"
            )
        ]

    # ------------------------------------------------------------------
    # the training view
    # ------------------------------------------------------------------

    def labelled(self, levels=None, label_types=None):
        """
        Observations joined to their ground truth.

        `levels` defaults to VERIFIED only. UNVERIFIED and UNKNOWN can
        never be requested: they are not labels, and quietly promoting
        them would be exactly the falsification this database exists to
        prevent.
        """
        levels = self._trusted(levels)
        label_types = tuple(label_types or (LABEL_EXACT_MATERIAL,))

        placeholders = ",".join("?" for _ in levels)
        type_placeholders = ",".join("?" for _ in label_types)

        query = (
            "SELECT o.*, g.label_type, g.material_key, g.material_id, "
            "       g.family_id, g.verification_status, "
            "       g.verification_source, g.certainty "
            "FROM observations o "
            "JOIN ground_truth g ON g.measurement_id = o.measurement_id "
            "WHERE g.verification_status IN ({}) "
            "  AND g.label_type IN ({}) "
            "ORDER BY o.created_at, o.measurement_id"
        ).format(placeholders, type_placeholders)

        records = []

        for row in self.connection.execute(query, levels + label_types):
            records.append(self._observation_from(row))

        return records

    def confusion(self, model_version=None, levels=None):
        """
        Verified truth against what a model said. Read-only history.

        This is the raw material for §27: it says what the system tends
        to confuse with what, measured rather than assumed.
        """
        levels = tuple(levels or TRUSTED_LEVELS)
        placeholders = ",".join("?" for _ in levels)

        query = (
            "SELECT p.model_version, g.material_key AS actual, "
            "       p.material_key AS predicted, p.level, p.confidence, "
            "       p.measurement_id "
            "FROM predictions p "
            "JOIN ground_truth g ON g.measurement_id = p.measurement_id "
            "WHERE g.verification_status IN ({}) "
            "  AND g.label_type = ? "
        ).format(placeholders)

        params = list(levels) + [LABEL_EXACT_MATERIAL]

        if model_version is not None:
            query += " AND p.model_version = ? "
            params.append(model_version)

        query += " ORDER BY g.material_key, p.material_key"

        return [dict(row) for row in self.connection.execute(query, params)]

    # ------------------------------------------------------------------
    # class statistics snapshots
    # ------------------------------------------------------------------

    def save_class_statistics(self, snapshot_id, material_key, feature_space,
                              statistics, source_ids, n_independent,
                              built_at=None):
        with self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO class_statistics (
                    snapshot_id, material_key, feature_space, built_at,
                    n_independent, statistics_json, source_ids_json
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    snapshot_id, material_key, feature_space,
                    built_at or utc_now(), int(n_independent),
                    json.dumps(statistics, sort_keys=True),
                    json.dumps(sorted(source_ids)),
                ),
            )

    def class_statistics(self, snapshot_id, feature_space=None):
        query = "SELECT * FROM class_statistics WHERE snapshot_id = ?"
        params = [snapshot_id]

        if feature_space is not None:
            query += " AND feature_space = ?"
            params.append(feature_space)

        result = {}

        for row in self.connection.execute(query, params):
            record = dict(row)
            record["statistics"] = json.loads(record.pop("statistics_json"))
            record["source_ids"] = json.loads(record.pop("source_ids_json"))
            result[record["material_key"]] = record

        return result

    def snapshots(self):
        return [
            row[0] for row in self.connection.execute(
                "SELECT DISTINCT snapshot_id FROM class_statistics "
                "ORDER BY snapshot_id"
            )
        ]

    # ------------------------------------------------------------------
    # training runs
    # ------------------------------------------------------------------

    def save_training_run(self, run_id, training_ids, validation_ids,
                          model_version=None, feature_pipeline=None,
                          database_versions=None, hyperparameters=None,
                          metrics=None, dataset_hash=None, git_commit=None,
                          status="EXPERIMENTAL", report=None,
                          started_at=None):
        with self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO training_runs (
                    run_id, started_at, model_version, feature_pipeline,
                    database_versions_json, training_ids_json,
                    validation_ids_json, hyperparameters_json,
                    metrics_json, dataset_hash, git_commit, status,
                    report_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    started_at or utc_now(),
                    model_version,
                    feature_pipeline,
                    json.dumps(database_versions or {}, sort_keys=True),
                    json.dumps(sorted(training_ids)),
                    json.dumps(sorted(validation_ids)),
                    json.dumps(hyperparameters or {}, sort_keys=True),
                    json.dumps(metrics or {}, sort_keys=True),
                    dataset_hash,
                    git_commit,
                    status,
                    json.dumps(report) if report is not None else None,
                ),
            )

        return run_id

    def training_runs(self):
        records = []

        for row in self.connection.execute(
            "SELECT * FROM training_runs ORDER BY started_at, run_id"
        ):
            record = dict(row)

            for field, target in (
                ("training_ids_json", "training_ids"),
                ("validation_ids_json", "validation_ids"),
                ("hyperparameters_json", "hyperparameters"),
                ("metrics_json", "metrics"),
                ("database_versions_json", "database_versions"),
                ("report_json", "report"),
            ):
                value = record.pop(field, None)
                record[target] = json.loads(value) if value else None

            records.append(record)

        return records

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------

    def status(self):
        counts = {}

        for level in VERIFICATION_LEVELS:
            counts[level] = self.connection.execute(
                "SELECT COUNT(*) FROM ground_truth "
                "WHERE verification_status = ?",
                (level,),
            ).fetchone()[0]

        materials = self.connection.execute(
            "SELECT g.material_key, COUNT(*) AS n "
            "FROM ground_truth g "
            "WHERE g.label_type = ? AND g.verification_status = ? "
            "GROUP BY g.material_key ORDER BY g.material_key",
            (LABEL_EXACT_MATERIAL, VERIFIED),
        ).fetchall()

        return {
            "file": str(self.path),
            "schema_version": SCHEMA_VERSION,
            "observations": self.count_observations(),
            "labelled": sum(counts.values()),
            "by_verification": counts,
            "predictions": self.connection.execute(
                "SELECT COUNT(*) FROM predictions"
            ).fetchone()[0],
            "model_versions": self.model_versions(),
            "verified_materials": {
                row["material_key"]: row["n"] for row in materials
            },
            "mixtures": self.mixture_summary(),
            "sample_context": self.context_coverage(),
            "training_runs": len(self.training_runs()),
            "snapshots": self.snapshots(),
        }
