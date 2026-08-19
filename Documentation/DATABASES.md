# Databases

Three databases, one file each, in `firmware/BD/data/`.

```text
DB1.json                  measured here, 18 bands, 23 materials   READY
DB1_source.txt            the verbatim record DB1 is built from
DB2.json                  measured here, 54 features               EMPTY
DB3.json                  external spectra, projected, 84 spectra   READY
calibration_legacy.json   the White/Dark DB1 was measured against
calibrations.json         every calibration ever made, and which is active
samples.json              samples measured during a run
```

Each database carries its own metadata, provenance and audit **inside the
same file**. There are no side-car manifests to fall out of step with the
data they describe, and no second copy of any database anywhere.

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

## DB2 — measured, 54 features · EMPTY

18 spectral bands × 3 illumination conditions = 54 features per material.
Feature ids are illumination-qualified: `white:A` … `ir:W`.

**Why it is empty, and why that cannot be fixed in software.** DB1 holds
18 raw Sample values per material under a single unrecorded illumination.
The 36 missing UV and IR features were never measured. Changing the White
reference cannot create them, and no code path exists that would try.

Complete and tested: schema, feature-space definition, loader, validator,
status reporting, compatibility rules, and the guard that refuses to
compare an 18-band measurement against this 54-feature space.

Blocked on: a full spectral calibration on the instrument, then physical
remeasurement of each material under WHITE, UV and IR.

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

**Legacy** (`calibration_legacy.json`) — the White/Dark DB1 was measured
against. Immutable, and the only calibration ever used to compare against
DB1. Re-normalising the library against a newer White would silently
change what every stored number means.

**Active** — a full Dark + WHITE/UV/IR calibration made by the operator,
used for the scientific record and quality control. It is one entry in
`calibrations.json`, which holds **every** calibration ever made, in full,
and records which of them is in force.

One file, because "which calibrations do I have, and which am I using?" is
one question. The earlier layout answered it with a directory listing plus
a side-car pointer file, which is how an operator ends up making a fresh
calibration on every restart instead of reusing the one already on disk.

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

## samples.json

The run's scientific output, and the only file this system writes.

It is **untracked and gitignored** — measured competition data belongs to
a run, not to the source tree. That also makes it the most fragile file in
the repository: git cannot restore it. `test_science` records its size and
SHA256 before the suite and verifies them after, so a run that damages it
fails loudly instead of silently.

Back it up with the procedure in `OPERATIONS.md` after every session.
