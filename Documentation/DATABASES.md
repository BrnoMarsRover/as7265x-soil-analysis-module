# Databases

**One canonical persistent store per subsystem**, in `firmware/BD/`.

```text
BD/
├── calibration/  calibration.json          every calibration ever made,
│                                           the protected LEGACY
│                                           White/Dark, which one is
│                                           ACTIVE, and the acquisition
│                                           profiles they were taken under
├── DB1/          DB1.json                  measured here, 18 bands,
│                 DB1_source.txt            23 materials      PROTECTED
│                 operator_aliases.json     bench names only
├── DB2/          DB2.json                  measured here, 54 features,
│                 DB2_source.txt            22 materials      PROTECTED
├── DB3/          DB3.json                  external spectra, projected
├── models/       registry.json             model artifacts, and which
│                                           one is ACTIVE
├── samples/      samples.json              the PC session and the PC
│                                           archive
└── training/     decision_learning.sqlite3 observations, ground truth,
                                            predictions. OFFLINE only
```

Each store carries its own metadata, provenance and audit **inside the
same file**. There are no side-car manifests to fall out of step with the
data they describe, and no second copy of any store anywhere.

## Why one file per subsystem

The layout used to hold several truths twice. `calibration/` had four
files - the library, a pointer saying which entry was active, the legacy
White/Dark, and the acquisition profiles - and three of them were
persisted VIEWS of the fourth. `training/` had the SQLite database and a
JSON seed containing every row already in it. `samples/` had the archive
and a `*.backup.json` written by an earlier migration and never removed.

Files that must agree can disagree, and the program had a read path for
each of them. They were consolidated, and the redundant files were
**deleted** rather than kept as a fallback: a fallback reader that
survives migration is a second source of truth wearing a different name.

`Tests/software/regression/test_bd_migration.py` runs each migration on a
copy of the repository's own data and compares the result field by field.

---

## DB1 — measured, 18 bands

The historical session: 23 materials measured on this instrument under a
single illumination, with one shared Dark and one shared White.

Per material, per channel, DB1 stores the raw measurements **and** the
derived value, so the derivation stays checkable:

```json
"raw_sample": 4.8513, "dark_reference": 0.0, "white_reference": 29.9166,
"reflectance_as_supplied": 0.1622, "reflectance_recomputed": 0.162161,
"reflectance_residual": 3.9e-05, "dark_provenance": "STANDALONE_DARK_TABLE"
```

Rebuild: `py firmware/research/build_db1.py`. Deterministic — the same
source produces the same database, and the source SHA256 is stored inside
DB1.json.

### The equation, established not assumed

All 414 supplied reflectances are reproduced by

```text
R = (Sample − Dark) / (White − Dark)
```

to within **4.4×10⁻⁵**, the rounding of the source's 4 decimal places.

The discriminating channel is **D485** — the only one with a non-zero Dark
(3.4855). Omitting Dark from the *denominator* gives 0.0306 instead of the
supplied 0.0310, so the data itself proves Dark is subtracted from both
terms. A test asserts this, because a plausible-but-wrong implementation
differs only there.

### Copper(II) Sulfate — resolved

The previously committed database held 22 materials and reported
Copper(II) Sulfate as unauditable. The gap is not two missing and one
extra:

- `Bentonit` and `Bentonite` are **the same material** — maximum
  difference across all 18 channels is 4.8×10⁻⁵, pure rounding. Recorded
  as an alias.
- `Copper(II) Sulfate` is **genuinely absent** — the nearest committed
  material differs by 0.4575. Not a rename.

Exactly one material was lost. It is recovered as Chalcanthite, CuSO₄·5H₂O,
and its spectrum is distinctive enough to matter: strong in the blue
(C460 = 0.7888), collapsing across red and NIR (S760 = 0.0850).

Corroboration: the 21 unambiguously common materials agree to 5.0×10⁻⁵, so
the old database demonstrably derived from this same session.

### Anomalies — recorded, not corrected

**W940 Dark is inferred.** The standalone Dark table lists A…V — 17
channels. W940 appears only inside the material tables, where all 23 agree
on 0.0000. The value is used and labelled per channel:

```text
dark_provenance: INFERRED_FROM_MATERIAL_TABLES   (W only)
dark_provenance: STANDALONE_DARK_TABLE           (everything else)
```

The inference is well supported but remains an inference, and no other
original file exists to confirm it.

**Three materials exceed reflectance 1.0** — Magnesium Carbonate peaks at
1.2003, plus Talc and Citric Acid. **Not clipped.** A sample returning
more light than the white reference is real information, most plausibly
that these bright powders scatter more strongly than the reference target.
Clipping would destroy the evidence and hide a calibration-geometry
problem. Each is flagged `REFLECTANCE_ABOVE_ONE`.

**Iron(II,III) Oxide Black reads exactly 0.0000** on K680 and L705 — the
two channels where the White reference is weakest (14.93 and 21.15,
against 1873.60 at R730). A black iron oxide returning nothing where the
illumination is weakest is physically coherent. Flagged `ZERO_RAW_SAMPLE`
and preserved: "the detector saw nothing" and "we have no reading" are
different facts.

### What DB1 does not know

The session recorded no acquisition settings. Every material carries
`gain`, `integration_cycles`, `measurement_mode`, `geometry`,
`distance_mm` and `repeats` as **UNKNOWN** — not defaults.

There are also **no replicates**, so no repeatability statistics exist.
That removes the preferred basis for judging whether a future calibration
change exceeds measurement noise.

---

## DB2 — measured, 54 features

18 spectral bands × 3 illumination conditions = 54 features per material.
Feature ids are illumination-qualified: `white:A` … `ir:W`.

**22 of DB1's 23 materials**, measured on 2026-08-17 under one full
spectral calibration (`FREYA_FULL_SPECTRAL_CAL_20260817_173914`) without
moving the sensor between materials. Sodium Bicarbonate was not measured
and is **absent** from DB2 rather than present and zero.

Per material, per feature, DB2 stores the raw counts, the dark, the
reference measured **under that same lamp**, the reflectance the
instrument printed, and the reflectance recomputed from the raw:

```json
"ir:W": {
  "illumination": "ir", "channel": "W", "wavelength_nm": 940,
  "raw_sample": 29.7777, "dark_reference": 0.0,
  "white_reference": 888.9196, "reference_span": 888.9196,
  "reflectance_as_supplied": 0.0335,
  "reflectance_recomputed": 0.033499, "reflectance_residual": 1.0e-06,
  "flags": []
}
```

Rebuild: `py firmware/research/build_db2.py`
Audit without writing: `py firmware/research/build_db2.py --audit-only`

**The audit is the point.** All 1188 reflectances (22 × 54) are
recomputed from the raw counts and compared against what the instrument
printed. The largest disagreement is 5.0e-05 — exactly the rounding of a
4-decimal display, on every single value.

### What the audit flags, and why none of it is repaired

**Reflectance is not clipped.** `R > 1` means the material returned more
light than the white target under that lamp; `R < 0` means the sample
read below the dark offset on a channel where that offset is large
(C460 reads 77.9 counts dark, D485 reads 35.7). Both are real
consequences of a reference that does not describe that material's
geometry, and both are preserved and flagged per feature.

**UV is weak on 8 channels.** The calibration itself was accepted with
that warning, and the audit reproduces it independently: under UV the
reference rises less than 5 counts above the dark on 8 of 18 channels.
Reflectance there is quantised and should not be read as a precise
number. `REFERENCE_RANGE_MARGINAL` marks each one.

### Why it could not have been derived from DB1

DB1 holds 18 raw Sample values per material under a single *unrecorded*
illumination. The 36 UV and IR features per material were never measured.
Changing the White reference cannot create them, and no code path exists
that would try. DB2 required physically remeasuring every container.

The guard that refuses to compare an 18-band measurement against this
54-feature space is unchanged and still enforced by
`BD/channels.py::require_compatible`. A 54-feature measurement may be
*narrowed* to its 18 WHITE bands — that is real, not invented — and the
registry reports it as `PROJECTED_TO_18` so a result always says so.

---

## DB3 — external reference, projected · 84 spectra

Real laboratory reflectance spectra from the **USGS Spectral Library
Version 7** (public domain, DS-1035), projected into the 18 AS7265x bands.
84 spectra across 73 material groups, chosen for a rover science task:
iron oxides, sulfates, phyllosilicates, carbonates, mafic minerals,
feldspars, silica, serpentines, micas, evaporites, whole rocks, sands,
playa crusts and characterised mixtures.

Rebuild: `py firmware/research/import_usgs.py --download`

Every record carries its USGS record number, sample identifier,
instrument, wavelength range and the SHA256 of the archive it came from.
The 21 MB source archive is not committed; the derived projection is,
because USGS data is public domain.

### The projection

```text
band_i = ∫ R(λ)·S_i(λ) dλ  /  ∫ S_i(λ) dλ
```

Nearest-wavelength sampling is **not** used. `S_i` should be the
manufacturer's measured response curve; those are not available, so the
model uses a Gaussian on the nominal centre with nominal FWHM and every
record is stamped `"approximate": true`. Replacing `channel_response()`
and regenerating is the whole upgrade path.

Verified by known answers: a flat spectrum projects flat to 1.7×10⁻¹⁶; a
linear ramp projects to the band-centre value; halving the integration
step moves a band by 3×10⁻⁶. Bands outside a source's measured range are
omitted, never estimated.

### ⚠ What DB3 can and cannot do — measured, and fed into confidence

Running the whole of DB1 against DB3 gives a **class agreement of 1 in
12**. This is not a bug; it is what the sensor can do.

```text
DB1 material               DB3 nearest              class correct?
Iron(III) Oxide Red    ->  Hematite GDS27           YES
Calcium Carbonate      ->  Anhydrite GDS42          no
Talc                   ->  Anhydrite GDS42          no
Potassium Nitrate      ->  Anhydrite GDS42          no
Sulfur Powder          ->  Calcite+Montmorillonite  no
Green Clay (Illite)    ->  Bloedite GDS147          no
```

The collision statistics say why. Across DB3's 3486 material pairs,
**15.7% score cosine ≥ 0.999** and 82.7% score ≥ 0.95. Chemically
unrelated materials become indistinguishable after projection:

```text
0.99999  Niter (KNO3)        vs  Sodium Bicarbonate
0.99999  Gypsum              vs  Trona
0.99999  Anorthite           vs  Howlite
0.99999  Magnesite           vs  Quartz
```

**The physical reason.** Most mineral diagnostics live in the SWIR: OH and
H₂O overtones near 1400 and 1900 nm, metal-OH near 2200–2300 nm, carbonate
near 2300–2500 nm. All of that is **outside 410–940 nm**. What does fall
inside is iron electronic structure — Fe³⁺ charge transfer in the blue and
crystal-field absorption near 860–900 nm. A per-channel F-ratio over DB3's
classes confirms it: ~1.2 across the visible, rising to **1.41 at
860–900 nm**, exactly where the iron feature sits.

### Per-class discriminability — in the model, not the prose

`py firmware/research/analyse_discriminability.py` measures, per material
class, **how often DB3 is right when it names that class** — precision
under leave-one-out nearest-neighbour retrieval, using the production
ranking path. The result is written into `DB3.json` and consumed by the
confidence model.

```text
class                    n  answers  precision  level
iron_oxide               3        1       100%  INSUFFICIENT_DATA
mica                     4        3        67%  MODERATE
phyllosilicate_clay     12       13        62%  MODERATE
silicate_mafic           5        4        50%  MODERATE
feldspar                 3        5        40%  MODERATE
sulfate                 15       14        36%  WEAK
regolith_analogue        4        6        33%  WEAK
serpentine               3        6        33%  WEAK
carbonate                6       10         0%  WEAK
mixture_characterised    6        3         0%  WEAK
silica                   4        1         0%  INSUFFICIENT_DATA
borate                   3        3         0%  WEAK
halide                   3        2         0%  INSUFFICIENT_DATA
```

Precision, not recall, because the runtime question is not "would this
class be found if present" but "DB3 has just answered class X — should I
believe it". `sulfate` is named 14 times and is right 5 of them, which is
exactly why chalk, talc and saltpetre all came back as anhydrite.

**How the model uses it.** A class **measured and found WEAK** loses its
vote in the consensus: it is still reported in full, with the reason, but
it no longer counts as corroboration, and confidence drops because an
independent source was removed.

```text
Calcium Carbonate ->  DB3: Anhydrite [WEAK]  ->  DB3 discounted, LOW
Kaolin            ->  DB3: Anorthite [MODERATE] -> DB3 votes, MODERATE
```

**Measured-poor and unmeasured are kept apart.** `WEAK` means we checked
and it is unreliable. `INSUFFICIENT_DATA` means the class has too few
members for leave-one-out to say anything, and `UNRATED` means no analysis
exists at all — which is DB1's situation, since it carries no
discriminability block. Only `WEAK` loses its vote; conflating the three
would silence DB1 entirely.

**Correction to an earlier claim.** A previous version of this document
said DB3 "identifies iron oxides perfectly well". That was a conclusion
drawn from a single successful case. The measurement says `iron_oxide` is
`INSUFFICIENT_DATA`: precision is 1.0, but on **one** answer across three
members, which supports nothing. Hematite really was matched correctly;
that is not evidence the class is reliable.

The thresholds (STRONG ≥ 0.70, MODERATE ≥ 0.40) are labelled
`PROVISIONAL` in the data. They are derived from DB3's own retrieval, not
validated against physical measurements, and re-running the analysis after
DB3 grows or after the projection model is replaced updates what the
confidence model believes. Nothing is pinned by hand.

## Calibrations

Everything below lives in **one** file, `calibration/calibration.json`.

**Legacy** (`kind: "LEGACY"`, `protected: true`) — the White/Dark DB1 was
measured against. Immutable, and the only calibration ever used to compare
against DB1. Re-normalising the library against a newer White would
silently change what every stored number means. It is a RECORD in the
calibration list rather than a separate file: being protected is a
property of the record, and every write path refuses it by name. It is
never offered in the selection list and can never become active, because
it was measured under white light only and normalising a UV acquisition
against it would be a fault the numbers would not reveal.

**Active** — a full Dark + WHITE/UV/IR calibration made by the operator,
used for the scientific record and quality control. One entry among the
calibrations, with `active_calibration_id` naming it.

**Acquisition profiles** — the conditions each calibration was taken
under, and what decides whether one may be applied to a measurement at
all. In the same document, because a profile and the calibration taken
under it are one subsystem.

One file, because "which calibrations do I have, which am I using, and
under what conditions were they taken?" is one question. The earlier
layout answered it with four files that had to be kept in step.

Every measurement is normalized **both** ways. That is precisely what lets
the instrument be recalibrated without remeasuring the material library.

Which normalization each database sees is not a detail — it is the rule
the whole scheme rests on:

| Database | Normalized with | Why |
|---|---|---|
| DB1 | **LEGACY**, always | it was built against that White/Dark; any other makes every stored number mean something else |
| DB2 | ACTIVE | it stores the same 54 features under the current calibration |
| DB3 | ACTIVE, falling back to LEGACY | it was never measured on this instrument, so the honest comparison is against the instrument as it is now |

Each database result records the normalization it was scored under, so a
stored conclusion can always be traced to the calibration that produced
it.

---

## samples.json — two collections, three owners

The run's scientific output, and the only file this system writes. It
holds **two collections with different lifetimes**:

```text
session   the working set of the run in progress. Prepare Sample creates
          a record here; Measure Sample completes it. Durable, because
          RAW that exists only in ESP32 RAM is one board reset from being
          gone - but it is scaffolding, not an archive.

archive   the permanent PC record. Nothing arrives here except an
          explicit operator import: from the ESP32, or from the session.
```

Beside them, on a different computer, is a third store the PC does not
own:

```text
ESP32     the device's own RAM buffer, one acquisition per slot, cleared
          by a board reset - and opening the serial port resets the board.
```

**No implicit synchronization.** A measurement lands on the ESP32 and in
the PC session. It reaches the archive only when the operator imports it.
Import is a COPY: it never moves, and it never overwrites a stored
scientific record - a same-ID sample whose data differs is reported as a
CONFLICT and left alone.

**Every delete names one store.** Delete from the ESP32, from the PC
session, or from the PC archive; there is deliberately no "delete
everywhere". None of them touches the Decision Learning database, DB1,
DB2, DB3 or the calibrations.

`samples.json` is **untracked and gitignored** — measured competition data
belongs to a run, not to the source tree. That also makes it the most
fragile file in the repository: git cannot restore it. `test_science`
records its size and SHA256 before the suite and verifies them after, so a
run that damages it fails loudly instead of silently.

Back it up with the procedure in `OPERATIONS.md` after every session.
