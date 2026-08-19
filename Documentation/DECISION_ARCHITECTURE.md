# Decision architecture — audit and implementation plan

The system is being split into six layers so that FREYA can learn from
verified measurements **without ever rewriting a measured reference
spectrum**.

```text
1. physical acquisition            ESP32/
2. deterministic processing        Measurements/     -> EvidencePackage
3. immutable measured references   BD/ DB1, DB2
4. learning / experience           BD/ decision_learning.sqlite3
5. decision model                  DecisionModel/    -> Decision
6. explainable conclusion          PC/
```

---

## Part 1 — Audit of what already exists

Done before any code was written. The conclusion is that a large part of
layers 2 and 5 already exist but are **fused together** in two modules,
and that almost nothing of layer 4 exists.

### What exists and is reused as-is

| Concern | Where | Verdict |
|---|---|---|
| 18 channels, 2 feature spaces, projection | `BD/channels.py` | reuse, extended with `combine_illuminations` |
| DB1/DB2/DB3 loading, validation, status | `BD/registry.py` | reuse unchanged |
| Material taxonomy: `material_id`, `canonical_name`, `chemical_formula`, `material_class`, `aliases` | inside `DB1.json` / `DB3.json` | **already exists** — no new taxonomy invented, see §51 |
| Calibration library and activation | `BD/calibrations.py` | reuse, schema extended |
| Sample archive | `BD/samples.py` | reuse; learning DB references it by id + hash |
| Repeat aggregation, CV, outlier rejection | `Measurements/aggregation.py` | reuse |
| cosine / RMSE / MAE / SAM / Pearson | `Measurements/metrics.py` | reuse the primitives; ranking replaced |
| QC checks | `Measurements/quality.py` | reuse the checks; **verdict semantics split** |
| NNLS mixture | `Measurements/mixture.py` | reuse, demoted to secondary evidence |
| Cross-database inference, consensus, confidence | `Measurements/inference.py` | **moves to `DecisionModel/`** |

### What is wrong today, evidenced by the operator's own session

Twelve known reference materials were measured on 2026-08-17. Under the
current pipeline:

- **6 of 12 produced no identification at all.** `quality.check_reflectance`
  returned FAIL because reflectance exceeded 1.0, and a FAIL suppresses
  classification entirely. But the RAW spectra are perfectly good: the
  samples are simply brighter than the white target that was used, so the
  *normalization* is ill-conditioned, not the *measurement*. This is
  exactly §11 — `HARDWARE_QC_FAIL` and `NORMALIZATION_WARNING` are
  different things and were being conflated.
- **UV normalization is meaningless above ~610 nm and nothing says so.**
  The UV white reference reads 1.6–4.3 counts on K–W against a dark of 0,
  so R_uv lands on values like 0.333 or 2.727 — quantization of a
  denominator of two counts. RAW is valid; normalized is not. §12.
- **Rank aggregation destroys the evidence.** Activated Carbon scored
  RMSE 0.0088 against a runner-up at 0.17 — a twentyfold separation — and
  that arrived at the decision layer as "rank 1", indistinguishable from
  a winner that led by 0.001. §25.
- **One prediction was simply wrong** (Iron(II) Sulfate measured, Kaolin
  reported) and the system had nowhere to record that fact. §27.
- `Measurements/analysis.py` reaches a semantic conclusion
  (`best_match`, `automatic_conclusion`), which §10 forbids.

### Layer rule, unchanged and extended

```text
BD -> Measurements       FORBIDDEN
BD -> DecisionModel      FORBIDDEN
Measurements -> BD       allowed
DecisionModel -> BD      allowed
DecisionModel -> Measurements  allowed
Measurements -> DecisionModel  FORBIDDEN
```

`Tests/test_architecture.py` enforces all six.

---

## Part 2 — Implementation stages

Each stage lands with its own tests and leaves the tree green.

### Stage A1 — BD: identity, profiles, learning store

- `BD/taxonomy.py` — reads the material identity that **already exists**
  in DB1/DB3. Resolves display names, ids, aliases (including the Czech
  names already stored) and families. Invents nothing.
- `BD/acquisition_profiles.py` + `data/acquisition_profiles.json` — HOW a
  measurement was made, separate from the calibration (what the reference
  read) and from the measurement (what the sample read). §40.
- `BD/decision_learning.py` — SQLite. Observations, ground truth with
  provenance, per-model predictions, class statistics, training runs.
  Historical predictions are immutable; a new model version writes a new
  row. §5, §6, §46.

### Stage A2 — Measurements: evidence, not conclusions

- `preprocessing.py` — dark correction, reference normalization,
  unit-vector, SNV.
- `spectral_features.py` — wavelength-aware first and second derivative,
  block energies, cross-illumination ratios and log-ratios.
- `channel_reliability.py` — per illumination x channel: `raw_valid`,
  `normalized_reliability`, `weight`, reasons. Raw validity and
  normalized validity are separate verdicts. §12.
- `distances.py` — six metrics, each with winner, runner-up, absolute and
  relative margin, and the score distribution. §14, §25.
- `class_distance.py` — nearest centroid, standardized Euclidean,
  shrinkage Mahalanobis, kNN. §15.
- `evidence.py` — assembles the versioned `EvidencePackage`. §42.

### Stage A3 — DecisionModel: cold start

- `engine.py` — the hierarchical decision. §21, §22.
- `evidence_fusion.py` — magnitude-aware fusion, never equal rank sums.
- `hierarchy.py` — family before material.
- `unknown_detection.py` — out-of-distribution. §37.
- `reliability.py` — per-database, per-class reliability learned from
  verified history, falling back to the shipped priors. §26, §45.
- `explainability.py` — sentences built only from structured evidence. §44.
- `model_registry.py` + `models/registry.json` — one ACTIVE model. §48.
- `calibration_transfer.py` — staged, EXPERIMENTAL, off by default. §28.

### Stage A4 — seed the learning database

The twelve measurements of 2026-08-17 are imported as the first
`VERIFIED` observations, with their original raw spectra, the calibration
and profile in force, and the prediction the old pipeline made. Nothing
in DB1/DB2/DB3 is touched. §52.

### Stage A5 — PC integration and ground-truth capture. §58, §59.

### Stage A6 — old vs new comparison report. §65.

### Later phases

Phase C (nearest centroid, kNN, regularized LDA, PLS-DA, SVM) and Phase E
(affine / DS / PDS transfer) are scaffolded with their interfaces and
their validation harness, and are **not** trained on twelve observations.
The registry records them as `INSUFFICIENT_DATA` until the dataset can
support group-aware validation. §33, §62.

---

## Part 3 — What was built, and what it does

### The layers, as they now exist

```text
firmware/
├── Measurements/          deterministic mathematics -> EvidencePackage
│   ├── preprocessing.py       dark, normalize, unit, SNV, conditioning
│   ├── spectral_features.py   wavelength-aware derivatives, energies,
│   │                          cross-illumination ratios
│   ├── channel_reliability.py 54 verdicts: raw_valid vs normalized_valid
│   ├── distances.py           6 metrics + margins + absolute goodness
│   ├── class_distance.py      centroid, standardized, shrunk Mahalanobis, kNN
│   └── evidence.py            the versioned EvidencePackage
│
├── DecisionModel/         interpretation -> Decision
│   ├── engine.py              the hierarchy, four levels
│   ├── evidence_fusion.py     magnitude-aware, one vote per family
│   ├── hierarchy.py           family before material
│   ├── unknown_detection.py   OOD, counted not averaged
│   ├── reliability.py         measured where possible, declared where not
│   ├── class_models.py        distributions from verified observations
│   ├── explainability.py      sentences built only from evidence fields
│   ├── calibration_transfer.py staged, UNAVAILABLE until pairs exist
│   ├── model_registry.py      one ACTIVE model, activation is a decision
│   └── training/              import, datasets, group-aware CV, baselines
│
└── BD/
    ├── taxonomy.py            identity, read from DB1/DB3, never invented
    ├── acquisition_profiles.py HOW a measurement was made
    ├── decision_learning.py   the SQLite history
    └── data/
        ├── operator_aliases.json
        └── decision_learning/
            ├── seed_observations.json     readable, rebuildable source
            └── decision_learning.sqlite3  the working database
```

### The four rules, enforced by tests

| Rule | Where it is enforced | Test |
|---|---|---|
| A prediction is never ground truth | `add_ground_truth` refuses any source naming a model | `test_decision` §1 |
| A historical prediction is never rewritten | `add_prediction` refuses a duplicate (measurement, version) | `test_decision` §1 |
| A raw measurement is never edited | observations are insert-only and hashed | `test_decision` §1 |
| A decision never modifies a reference | no DecisionModel module names a database file | `test_decision` §8, `test_architecture` §2c |

### Old pipeline versus new, on the twelve verified measurements

Every one of the twelve is a labelled laboratory material, so the truth
is known for all of them.

| | old pipeline | Decision Model V001 |
|---|---|---|
| named the right material | 2 | 2 |
| **named the wrong material** | **3** | **0** |
| suppressed entirely by QC | 7 | 0 |
| family correct | — | 1 |
| family wrong | — | 3 |
| ambiguous set containing the truth | — | 1 |
| ambiguous set missing it | — | 1 |
| UNKNOWN | — | 4 |

The headline is the second row. The old pipeline named three materials
confidently and wrongly; the new one names none. It also stopped throwing
away seven good measurements: their raw counts were always valid, and
what had failed was the reference division.

It is not uniformly better. Three family-level answers are wrong, which
is a real failure even though it is a less dangerous one than a confident
wrong name. Both remaining problems have the same cause and the same fix:

1. **One independent measurement per material.** Every class has a
   centroid and no scatter, so "is this sample typical of Talc?" cannot
   be asked. A second independent measurement of each material - removed,
   repacked, remeasured - unlocks class distance, standardized distance
   and, at six per class, Mahalanobis.
2. **The white reference does not describe the samples.** Six of the
   twelve exceed it, one by 53%. The reference target is measured at a
   different effective geometry from the samples. Re-taking the
   calibration with the target in the exact sample position would repair
   the normalization for half the dataset.

### What was NOT done, deliberately

- **No threshold was tuned against these twelve.** Two changes were made
  after seeing results, and both were structural faults found by unit
  tests, not adjustments to make the samples score better: a
  floating-point comparison that dropped the runner-up from its own
  ambiguity band, and a corroboration rule that no chemical could ever
  satisfy because DB3 contains no chemicals. Every threshold remains
  labelled `PROVISIONAL_UNVALIDATED`.
- **No classifier was trained.** `cross_validation.feasibility` reports
  that twelve classes with one group each cannot support leave-one-out
  validation - holding a class out removes it from training entirely, so
  the score would measure the dataset rather than the model. LDA, PLS-DA
  and SVM are named, their prerequisites stated, and left unimplemented.
- **No calibration transfer was fitted.** Zero paired materials exist:
  DB1's session and this one share names but no measurement pairs.
  `calibration_transfer.build` returns UNAVAILABLE with the count.

### Running it

```bash
py -m DecisionModel.training.import_seed
```

```bash
py -m DecisionModel.training.import_seed --apply
```

```bash
py -m DecisionModel.training.validate --record --detail
```
