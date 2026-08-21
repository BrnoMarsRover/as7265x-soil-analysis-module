# Scientific Exploration — the mission layer

ERC 2026, Science Task, Scientific Exploration sub-task.

`Science/` answers "what did the instrument measure?". `Science/`
answers "what does this one measurement most defensibly mean?". Neither
knows there is a competition. **`Science/` is the layer that knows**: it
holds the pre-declared hypothesis, the four planned sites, the mission
record, the analysis that evaluates the hypothesis, and the ERC
requirement registry.

---

## 1. The pipeline

```text
                    ERC MISSION RULES
                           +
                     SCIENCE PLAN
                           +
                       HYPOTHESIS
                           +
                       PREDICTIONS
                           │
                           ▼
┌─────────────────────────────────────────────┐
│          PHASE 1 — MEASUREMENT              │   Science/
│                                             │   (already existed)
│ Sensor acquisition                          │
│ Raw 18/54 channels                          │
│ Calibration                                 │
│ Normalization                               │
│ Quality control                             │
│ Feature extraction                          │
│ Mathematical methods                        │
│ DB comparison methods                       │
└──────────────────────┬──────────────────────┘
                       │  EvidencePackage
                       ▼
┌─────────────────────────────────────────────┐
│        PHASE 2 — DECISION MODEL             │   Science/
│                                             │   (already existed)
│ DB1 measured legacy   [READ ONLY]           │
│ DB2 full measured     [READ ONLY]           │
│ DB3 theoretical                             │
│ Previous confirmed samples                  │
│                                             │
│ distance-to-reference / distance-to-class   │
│ method consensus / uncertainty              │
│ OOD / unknown detection                     │
│ material + scientific interpretation        │
└──────────────────────┬──────────────────────┘
                       │  Decision
                       ▼
              samples.json + science_run.json
                       │  FULL TRACEABLE RECORD
                       ▼
┌─────────────────────────────────────────────┐
│ PHASE 3 — SCIENTIFIC EXPLORATION ANALYSIS   │   Science/  (new)
│                                             │
│ Load hypothesis / predictions / samples     │
│ Aggregate within site, compare between      │
│ Evaluate predictions                        │
│ Evaluate hypothesis                         │
│ Determine limitations                       │
│ Geological interpretation change            │
│ Select strongest evidence + figures         │
└──────────────────────┬──────────────────────┘
                       ▼
              FINAL SCIENCE PACKAGE
         ┌─────────────┼──────────────┐
         ▼             ▼              ▼
     conclusion      figures       evidence
                       │
                       ▼
                   AI POLISH          language only
                       │
                       ▼
                    ERC PDF
```

---

## 2. Layers

```text
firmware/
├── ESP32/          acquire      MicroPython, flat on the device
├── PC/             orchestrate  operator UI, workflow, transport
├── BD/             remember     channels, databases, calibrations, samples
├── Science/   understand   deterministic maths -> EvidencePackage
├── Science/  interpret    one measurement -> Decision
├── Science/        the mission  plan, sites, run, analysis, requirements
├── Tests/          verify       runs without a board
└── research/       investigate  dataset building, projection
```

Layer rule, enforced by `Tests/test_architecture.py` §2bb:

```text
Science -> BD              allowed
Science -> Science    allowed
Science -> Science.decision   allowed

BD -> Science              FORBIDDEN
Science -> Science    FORBIDDEN
Science.decision -> Science   FORBIDDEN
ESP32 -> Science           FORBIDDEN
Science -> PC / ESP32      FORBIDDEN
```

The direction matters for the same reason it did one layer down. If a
mission concept leaked downward, the mathematics would stop being
testable without a mission — and a competition deadline could reach into
a scientific result.

`Science/` is also forbidden from naming `DB1_FILE`, `DB2_FILE`, `DB1.json`
or `DB2.json` anywhere in its source, asserted per-file by test.

### Modules

| Module | Responsibility |
|---|---|
| `mars_yard.py` | 34 surveyed objects, seven distinct spatial concepts |
| `requirements.py` | the 19 O/SCI requirements and readiness vocabulary |
| `plan.py` | map units, frozen hypothesis, predictions, references |
| `sites.py` | the four-site planner and operator override |
| `run.py` | the mission record: visits, observations, USOs, config lock |
| `analysis.py` | Phase 3 — the mission-level verdict |
| `checker.py` | automated O/SCI structural checks + source manifest |
| `report.py` | ERC limit validators and the science package |
| `mapping.py` | annotated SVG maps that never touch the source image |
| `prerun.py` | pre-run readiness check and configuration snapshot |

---

## 3. Mars Yard: seven things that are not each other

`mars_yard_2026.png` is the spatial source of truth. Every coordinate in
`data/mars_yard_points.json` was transcribed from the tables printed on
that map. Nothing is computed from pixels.

```text
starting location   S1-S9    where a run may begin      NOT a target
navigation waypoint W1-W9    a surveyed position        candidate target
landmark            L1-L15   a surveyed feature         candidate target
deep sampling point P1       another sub-task           NOT a target
geological feature  U1-U5    an interpretation we drew
scientific site     SITE-nn  a target we chose
sample / measurement         what was actually measured
```

Collapsing any two of these is how a mission ends up reporting that it
measured a start line. **S1–S9 are start positions, not science points** —
the zoning figure numbers them 1–9, which is easy to misread as a list of
observation sites.

| Type | Count |
|---|---|
| Starting locations | 9 |
| Landmarks | 15 |
| Navigation waypoints | 9 |
| Deep sampling | 1 |
| **Total** | **34** |
| Eligible to carry a science site | **24** |

Frame: `MARS_YARD_2026_LOCAL`, metres, origin at S1, **no geodetic
reference** — the source supplies none, so none is invented.

The pixel registration used to draw overlays is declared
`ESTIMATED_BY_MARKER_REGISTRATION` and is never used to derive a
coordinate.

---

## 4. The statistic

The instrument has no reference library measured from this yard. DB1 is
23 laboratory materials; DB3 is 84 USGS spectra projected through a model
of our sensor. Neither can establish that a yard surface *is* basalt.

What it can establish is whether two places differ by more than the same
place differs from itself:

```text
between-site distance   || centroid(A) - centroid(B) ||
within-site spread      mean distance of a site's repeats
                        from their own centroid          (sigma)
standard error          sigma_pooled * sqrt(1/nA + 1/nB)
separation ratio        between distance / standard error
```

**The denominator is the standard error of the difference, not the spread
of individual repeats.** A centroid of *n* repeats is about √n more
precise, so dividing by the individual spread flatters every comparison:
two samples of the *same* surface sit about σ√(2/n) apart, which against a
denominator of σ looks like a ratio near 1 and gets called "separated". An
earlier revision of `analysis.py` made exactly that error and reported two
identical synthetic sites as reproducibly different.
`test_science_exploration.py` §4 is the regression test.

Three independent metric families, never pooled:

| Family | Distance used | Sees brightness? |
|---|---|---|
| `magnitude` | RMSE | yes |
| `angular` | spectral angle (deg) | no |
| `centered_shape` | 1 − Pearson r | no |

Two of three must agree before a separation counts as reproduced.
Threshold: 2.0 standard errors, labelled `PROVISIONAL_UNVALIDATED`.

### What is deliberately not computed

- **No p-value, no confidence interval, no significance claim.** With
  three repeats the assumptions of any such test cannot be checked.
- **No material identity from site spectra.** A similarity score against
  DB1 or DB3 is not evidence of identity, and is never reported as one.
- **No similarity converted into probability.**

### Known metric caveat

`1 − Pearson r` scales with the spectrum's own variance, so a control on a
near-flat surface widens in that family alone for reasons unrelated to the
session. The control test therefore requires a majority of families, not
unanimity, and records the disagreeing family as a limitation.

---

## 5. Outcomes

Predictions and the hypothesis each resolve to `SUPPORTED`, `REJECTED` or
`INCONCLUSIVE`. **INCONCLUSIVE is a first-class result**, and when it
occurs `what_would_resolve_it()` states what additional measurement would
settle it — which the rules ask for explicitly.

Guards:

- A run cannot start on an unfrozen hypothesis.
- A frozen hypothesis that was later edited **blocks any verdict**; the
  analysis records an integrity problem and refuses.
- Targets separating but the control failing yields INCONCLUSIVE, not
  SUPPORTED — the separation has not been distinguished from session
  variation.
- Hardware QC failure excludes a measurement; a *normalization* warning
  does not. Conflating the two previously suppressed six good
  measurements out of twelve.

---

## 6. Requirements: readiness is not a score

All 19 O/SCI requirements live in
`Science/data/erc_science_requirements.json` with verbatim rubric wording
and their real point values (300 total).

**The software never claims a judge score.** O/SCI-020 asks whether
formation processes are *properly* identified; a checker can confirm that
every mapped unit carries a process and a justification, and that is all.

```text
O/SCI-020
  PASS  every_feature_declares_a_formation_process
  PASS  every_formation_process_has_a_justification
  MANUAL REVIEW REQUIRED: a geologist confirms each process is correct
  judge score: UNKNOWN
```

Status vocabulary: `NOT_EVALUATED`, `NOT_READY`, `READY_AUTOMATED`,
`MANUAL_REVIEW_REQUIRED`, `VERIFIED_MANUALLY`, `OPERATIONAL_MANUAL`,
`JUDGE_ONLY`, `FAILED`, `N/A`.

O/SCI-150 reports `NON_CAMERA_INSTRUMENT_EVIDENCE_PRESENT`, never "50/50".
O/SCI-900 reports `POTENTIAL_O_SCI_900_RISK`, never a confirmed penalty.
O/SCI-910 is `OPERATIONAL_MANUAL` — no software can observe it.

Recording an outcome for a check name the registry does not declare is
**refused**, so the registry stays an accurate description of what is
actually verified.

---

## 7. The figure conflict

The rules say both of these:

> you should include **three annotated photographs** from the rover
> showing geologic features that you discuss in the report

> no more than **3 figures/annotated photographs** with proper
> descriptions (**figure caption included in the character limit**) + an
> updated geological map

Read together, the three required photographs consume the entire figure
budget. There is no fourth slot for a spectral plot, and captions eat into
the 3000 characters.

`report.figure_budget_warning()` raises `FIGURE_BUDGET_CONFLICT` with
`decision: REQUIRES_HUMAN_DECISION` and three options. **The software will
not choose** — both readings are defensible and the choice changes what a
judge sees.

### Limits enforced

| Document | Limit |
|---|---|
| Planning: subject | 500 chars with spaces |
| Planning: importance | 1000 |
| Planning: hypothesis | 500 |
| Planning: predictions | 1500 + exactly one A4 figure |
| Exploration: report | 3000 incl. captions |
| Exploration: figures | 3, of which 3 should be rover photos |
| USO | ≤ 3 objects, 350 chars each |
| Deadline | 2.5 h after traverse |

**A limit is enforced, never applied.** Nothing truncates. The validator
reports the exact overrun and refuses to export, because the rules say
over-limit material "will not be read and assessed" — silently cutting
would delete science and hide the deletion.

---

## 8. Data safety

- `DB1` and `DB2` are read-only. No `Science/` module names either file;
  asserted per-file by test.
- `mars_yard_2026.png` is never opened for writing. Annotated maps are
  **SVG overlays that reference it**, and `checker.file_digest()` proves
  it unchanged by hash.
- Synthetic test data is marked `synthetic: true`, written only to a
  temporary directory, and the suite asserts none reached DB1, DB2 or the
  production sample archive.

> **Note.** `Tests/test_db1.py` §8 re-runs `research/build_db1.py` to prove
> the build is deterministic, which rewrites DB1's `generated_at`
> timestamp on every test run. This predates the Science layer. The
> measured data is byte-identical — the test asserts exactly that.

---

## 9. Operator workflow

```text
 1. Load the science plan            plan.load()
 2. Review the four planned sites    sites.SitePlan.load()
 3. Pre-run check                    prerun.run_check()      PASS/WARN/FAIL
 4. Freeze the hypothesis            plan.hypothesis.freeze()
 5. Lock configuration               run.lock()
 6. Begin traverse                   run.begin_traverse()

     per site:
 7.  reach_site / abandon_site (with a reason)
 8.  add_photograph
 9.  add_observation
10.  measure -> Science -> Science.decision
11.  bind_measurement

12. End traverse                     run.end_traverse()   <- 2.5 h starts
13. Analyse                          analysis.analyse()
14. Check readiness                  checker.check()
15. Build maps                       mapping.build_map()
16. Build package                    report.build_package()
17. Validate limits                  report.validate_*()
18. AI polish (language only)        AI_POLISH_PROMPT.md
19. Re-validate, export
```

`prerun.run_check()` emits PASS/WARN/FAIL per subsystem with the affected
O/SCI requirement. No silent failure: a check that cannot determine its
answer reports WARN or FAIL with a reason, never silence. A missing sensor
link reports "not verified", never "fine".

---

## 10. Scientific limitations

Stated here because they bound every conclusion this system can reach.

1. **No yard-derived reference library.** DB1 is lab reagents and clays;
   DB3 is projected USGS spectra. Material identity is not claimed.
2. **DB2 covers 22 materials, and UV is weak on 8 channels.** A
   54-feature comparison is possible; it is not equally trustworthy
   across all 54, and the marginal features are flagged per record.
3. **Small n.** Three repeats per site is the floor at which spread exists
   at all. No significance test is valid, and none is computed.
4. **The white reference does not describe the samples.** Six of twelve
   historical measurements exceeded it. Raw counts are valid;
   normalization is ill-conditioned. Tracked separately from hardware QC.
5. **All thresholds are `PROVISIONAL_UNVALIDATED`.**
6. **The shipped geology is DRAFT photo-interpretation** and must be
   reviewed by a geologist. `plan.review_problems()` surfaces this, and
   the pre-run check warns on it.
7. **No composition is estimated.** Unmixing returns a spectral
   contribution, which is not a mass fraction: particulate mixing is not
   linear, and converting between them needs a model validated against
   physically prepared mixtures. The learning database can now hold
   those mixtures, and `research/training/evaluate_mixtures.py` reports
   whether the relationship survives leaving each one out. Until it
   does, every component carries `is_mass_fraction: false`.
7. **No references and no prediction figure exist yet.** Fabricating
   citations would be worse than having none; the validator fails until a
   human supplies them.
