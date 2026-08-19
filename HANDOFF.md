# HANDOFF — Freya AS7265x science module

State as of 2026-08-18, after the first session with the board
attached. Read this first, then `Documentation/`, and note that §9c
supersedes parts of §9.A.

This is the "where we are right now" note, written for whoever picks the
project up next — human or model — with no memory of how it got here.

---

## 0. Read this before touching anything

**Nothing is committed.** `HEAD` is `93b4d3f` ("Checkpoint before
architecture migration") with ~45 uncommitted entries in the working tree.
Everything below lives only in the working tree. The author commits
deliberately: **do not commit or push unless asked.**

**`firmware/BD/data/samples.json` is untracked AND gitignored.** It holds
the only real measurement ever taken (`s456`). Git cannot restore it. It
was truncated to 0 bytes once during a restructure and recovered only from
an out-of-tree backup. Copy it somewhere outside the repository before any
bulk file operation. `test_science` now hashes it before and after the
suite and fails if it changes.

**Environment:** Windows, PowerShell. `&&` is a parser error — one command
per line. Python is `py`. The board is COM4 when attached; **it has never
been attached in any session so far.**

---

## 1. What the system is

A rover-mounted multispectral soil-analysis payload for the Brno Mars
Rover / Freya science subsystem. An AS7265x 18-channel VIS-NIR sensor on a
4-slot carousel, driven by an ESP32, commanded over USB serial by a PC
application that performs all the science.

The carousel supports **two actuators**, and the firmware never guesses
which is fitted:

- an **MG995** continuous-rotation PWM servo on GPIO25 — timed, open loop,
  powered from the PCB servo branch;
- a **Waveshare ST3215** serial bus servo on UART2 through a Waveshare
  driver board — encoder feedback, verified movement, **externally
  powered** (the ESP32 PCB provides only TX, RX and a common ground).

After every boot no servo is selected and carousel movement is blocked.
The operator states what is installed at option [0]. See §9b.

The AS7265x has **18 detectors** covering 410–940 nm. It does not have 54
wavelengths. Under three illuminations (WHITE / UV / IR) it yields
18 × 3 = **54 features** — a different claim, and the codebase keeps the
two apart explicitly.

---

## 2. Architecture

```text
firmware/
├── ESP32/          acquire      MicroPython, uploaded to the board
│   ├── boot.py  main.py  config.py
│   ├── drivers/    external hardware, one module per device
│   ├── control/    subsystem logic: servo_manager, carousel
│   └── protocol/   the PC command protocol
├── PC/             orchestrate  operator UI, workflow, serial transport
├── BD/             remember     channels, databases, calibrations, samples
├── Measurements/   understand   calibration maths, metrics, inference
├── Tests/          verify       runs without a board
└── research/       investigate  dataset building, projection, analysis
Hardware/           Altium design — never touched by software work
Documentation/      4 documents, see §8
```

The ESP32 tree is one-way: `main -> protocol -> control -> drivers`. A
driver never imports upward, and `test_architecture.py` fails if one does.

~21 500 lines of Python. Detail in `Documentation/ARCHITECTURE.md`.

### The one rule that matters

```text
BD → Measurements    FORBIDDEN, enforced by test
Measurements → BD    allowed and used
```

Persistence must not depend on mathematics. Two consequences, both
deliberate and both easy to "fix" wrongly:

- The 18-channel vocabulary lives in `BD/channels.py`, not Measurements,
  because BD must validate record shape without importing science.
- `BD/calibrations.py::activate()` takes an **injected validator**. It
  must refuse a calibration that failed its checks, but the check is
  science, so PC passes it in.

`Tests/test_architecture.py` reads import statements and fails on
violations. It also asserts no stale copy of any layer sits at the
repository root — an earlier revision put them there and it was reversed.

### Feature spaces

```text
AS7265X_18              "A" … "W"            18 features
AS7265X_54_MULTIILLUM   "white:A" … "ir:W"   54 features
```

Comparing 18 against 54 **raises**. Narrowing 54 → 18 is allowed (a
54-feature measurement genuinely contains its WHITE bands); the reverse is
impossible and no code path offers it.

---

## 3. The three databases

One file each in `firmware/BD/data/`. Each carries its own metadata,
provenance and audit **inside the same file** — no side-car manifests.
Exactly one copy of each; no backups, no legacy duplicates.

| | Evidence | Space | Status | Count |
|---|---|---|---|---|
| **DB1** | MEASURED | 18 | READY | 23 materials |
| **DB2** | MEASURED | 54 | EMPTY | 0 |
| **DB3** | REFERENCE_PROJECTED | 18 | READY | 84 spectra |

Scored **separately and never pooled**. A cosine of 0.97 against DB1 means
"resembles something we measured here"; against DB3 it means "resembles a
lab spectrum after modelling our sensor". Different claims.

### DB1 — the historical session

23 materials measured on this instrument, one shared Dark and White. Per
channel it stores raw Sample, Dark, White, the supplied reflectance, the
recomputed one and the residual, so the derivation stays checkable.

Rebuild: `py firmware/research/build_db1.py` — deterministic, source
SHA256 stored inside DB1.json.

Established from the data, not assumed:
`R = (Sample − Dark) / (White − Dark)`, reproduced to 4.4×10⁻⁵ across all
414 values. The discriminating channel is **D485**, the only one with a
non-zero Dark (3.4855) — omitting Dark from the denominator differs only
there, and a test asserts it.

Anomalies recorded rather than corrected: W940 Dark is **inferred** from
the material tables (absent from the standalone Dark block); three
materials exceed reflectance 1.0 (Magnesium Carbonate peaks at 1.2003) and
are **not clipped**; Iron(II,III) Oxide Black reads exactly 0.0000 on K680
and L705 and is flagged, not treated as missing.

Acquisition settings are **UNKNOWN**, not defaulted. There are **no
replicates**, so no repeatability statistics exist anywhere in the project.

### DB2 — empty, and cannot be filled in software

DB1 holds 18 raw values per material under one unrecorded illumination.
The 36 missing UV/IR features were never measured; changing the White
cannot create them. Schema, loader, validator, feature-space guard and
status reporting are complete and tested. Blocked on hardware.

### DB3 — 84 real spectra from USGS splib07

Public domain (USGS DS-1035). Rebuild:
`py firmware/research/import_usgs.py --download` — downloads 21 MB into
`BD/data/.cache/`, gitignored.

Projection is a band integral, not nearest-wavelength sampling:
`band_i = ∫R(λ)S_i(λ)dλ / ∫S_i(λ)dλ`. `S_i` should be the manufacturer's
measured response curve; those are **not available**, so it is a Gaussian
on the nominal centre and FWHM, stamped `"approximate": true` on every
record. Replacing `channel_response()` in
`research/spectral_projection.py` and regenerating is the entire upgrade
path — no stored number is hand-edited.

Verified by known answers: flat spectrum projects flat to 1.7×10⁻¹⁶;
linear ramp projects to the band-centre value; halving the integration step
moves a band by 3×10⁻⁶. No extrapolation — bands outside a source's range
are omitted, never estimated.

---

## 4. The most important scientific finding

**The AS7265x cannot distinguish most of these minerals, and the system
now knows it.**

All of DB1 against DB3 gives **class agreement of 1 in 12**. Across DB3's
3486 material pairs, **15.7% score cosine ≥ 0.999**. Chemically unrelated
materials collapse together: Niter vs Sodium Bicarbonate at 0.99999,
Gypsum vs Trona, Magnesite vs Quartz.

**Why.** Most mineral diagnostics live in the SWIR — OH/H₂O overtones near
1400 and 1900 nm, metal-OH near 2200–2300 nm, carbonate near 2300–2500 nm.
All outside 410–940 nm. What *is* inside is iron electronic structure:
Fe³⁺ charge transfer in the blue, crystal-field absorption near 860–900 nm.
Per-channel F-ratio confirms it: ~1.2 across the visible, **1.41 at
860–900 nm**.

### Measured, versioned, and consumed by the model

`py firmware/research/analyse_discriminability.py` measures, per material
class, **how often DB3 is right when it names that class** — precision
under leave-one-out nearest-neighbour retrieval, using the production
ranking path. It writes a `discriminability` block into `DB3.json`, which
`Measurements/inference.py` consumes.

```text
MODERATE (4)           feldspar, mica, phyllosilicate_clay, silicate_mafic
WEAK (6)               sulfate, carbonate, serpentine, borate,
                       regolith_analogue, mixture_characterised
INSUFFICIENT_DATA (13) iron_oxide, halide, nitrate, native_element, …
STRONG (0)             none
```

`sulfate` is named 14 times and right 5 — exactly why chalk, talc and
saltpetre all came back as anhydrite.

**Behaviour:** a class measured **WEAK** loses its vote in the consensus.
It is still reported in full with the reason, but no longer counts as
corroboration, and confidence drops because an independent source was
removed.

```text
Calcium Carbonate -> DB3: Anhydrite [WEAK]     -> DB3 discounted, LOW
Kaolin            -> DB3: Anorthite [MODERATE] -> DB3 votes,      MODERATE
```

**Three states, kept apart.** `WEAK` = measured and poor.
`INSUFFICIENT_DATA` = too few members for LOO to say anything. `UNRATED` =
no analysis exists (DB1's situation — it carries no block). **Only WEAK
loses its vote**; conflating them would silence DB1 entirely. Tested.

Thresholds (STRONG ≥ 0.70, MODERATE ≥ 0.40) are labelled `PROVISIONAL` in
the data — derived from DB3's own retrieval, not validated against
physical measurements.

---

## 5. Metrics — evidence families, not a metric zoo

Settled analytically, no benchmark needed:

```text
SAM  = arccos(cosine)               → rank-identical to cosine
RMSE = Euclidean / sqrt(18)         → rank-identical to Euclidean
cosine(x, kx) = pearson(x, kx) = 1  → both blind to brightness
```

The earlier ensemble weighted cosine : RMSE : Pearson 1:1:1 and called them
three independent views. Pearson is the cosine of mean-centered vectors and
is **identically scale-invariant** — so it was two shape metrics and one
magnitude metric, a hidden 2:1 bias toward shape.

Now three families, one vote each, combined by **rank** (the statistics are
on incomparable scales):

| Family | Ranks by | Also reported | Sees brightness? |
|---|---|---|---|
| `magnitude` | RMSE | MAE | **yes** |
| `angular` | cosine | spectral angle | no |
| `centered_shape` | Pearson r | — | no |

Family weights are **equal and labelled `PROVISIONAL_UNVALIDATED`**.
Correcting a 2:1 bias by asserting a different ratio would swap one
invented number for another. Do not "tune" them without a validation set.

On DB1: 45% of the 253 material pairs score cosine ≥ 0.99, median 0.9859,
worst pair Borax vs Tartaric Acid at 0.99991.

---

## 6. Other implemented science

**Mixture analysis** (`Measurements/mixture.py`) — NNLS (Lawson-Hanson,
pure Python, no new dependency). Recovers a synthetic 0.7/0.3 blend
exactly. A pure material returns `SINGLE_COMPONENT`, not a mixture.
Collinear endmembers are screened out; candidates capped at 4. Coefficients
are named **`spectral_contribution`**, never mass fraction — converting
requires calibration against prepared mixtures of known mass, which does
not exist.

**Confidence** (`Measurements/inference.py`) — derived from evidence
structure, not the top similarity: margin over runner-up, metric-family
agreement, cross-database agreement, measurement quality, class
discriminability. Explicitly **not a calibrated probability**, and the
system may answer **UNKNOWN** rather than crowning whichever entry ranked
first.

**Quality control** runs before classification; a FAIL suppresses
identification entirely and drives confidence to NONE.

---

## 7. Tests — 1673 checks, 0 failures

```powershell
cd C:\Users\Public\Documents\Altium\as7265x-soil-analysis-module\firmware\Tests
```

```powershell
py run_all.py
```

`run_all.py` runs every suite in its own process and prints one summary.
Separate processes are not incidental: `ESP32/` and `BD/` both contain a
module called `config`, and several suites reload the whole firmware tree.

| Suite | Checks | Covers |
|---|---|---|
| `test_st3215.py` | 210 | ST3215 wire protocol, verified movement, bus scan, supplied-driver cross-check |
| `test_esp32.py` | 161 | sensor lifecycle and the JSON command protocol |
| `test_pc.py` | 143 | client, archive, data layout |
| `test_science.py` | 121 | formulas, protected-data hashes |
| `test_integration.py` | 131 | full loopback pipeline |
| `test_carousel.py` | 117 | geometry and planning, on BOTH backends |
| `test_servo_manager.py` | 111 | selection, switching, and the no-servo safety gates |
| `test_architecture.py` | 185 | layer boundaries, dependency direction, metric dependence |
| `test_inference.py` | 88 | feature space, registry, projection, mixture, discriminability |
| `test_mg995.py` | 82 | PWM backend, pulse widths, runtime calibration |
| `test_calibration.py` | 75 | calibration build / validate / activate |
| `test_decision.py` | 138 | decision levels, learning history, leakage prevention |
| `test_evidence.py` | 77 | representations, channel reliability, distances, class distance |
| `test_db1.py` | 34 | DB1 integrity, determinism, anomalies |

**Do not "fix" a failing test by changing the expectation** unless the new
number is genuinely correct and you can say why. Every such change so far
has a recorded reason (e.g. 22 → 23 materials because Copper(II) Sulfate
was recovered).

---

## 8. Documentation

| File | Answers |
|---|---|
| `Documentation/ARCHITECTURE.md` | layers, boundaries, metric families, why |
| `Documentation/DATABASES.md` | DB1/DB2/DB3, audit, projection, discriminability |
| `Documentation/OPERATIONS.md` | install, upload, run, calibrate, recover |
| `Documentation/DECISION_ARCHITECTURE.md` | the six layers, the learning database, old vs new |
| `Documentation/REQUIREMENTS.md` | 69 requirements with traceability |
| `README.md` | what the project is, the hardware, the science |

Requirements: 63 PASS, 1 PARTIAL, 4 BLOCKED_BY_HARDWARE,
1 BLOCKED_BY_EXTERNAL_DATA.

---

## 9. What is missing — in priority order

### A. Hardware validation — blocks the most

**Nothing has run on the physical board in any session.** Largest gap, and
it gates several others.

1. **Servo bring-up, whichever is fitted.** Nothing about either
   actuator has been exercised against real hardware.

   **MG995:** every timing constant in `ESP32/config.py` is an unmeasured
   starting point, including `MG995_LOAD_TO_SCAN_CW_MS` /
   `MG995_SCAN_TO_LOAD_CCW_MS` (4300 ms), which Measure Sample depends on.
   The out-and-back test is the one that matters — optimise for the error
   after the *pair*, not either half.

   **ST3215**, in order:
   - wiring and the **common ground** (GPIO17 → TX, GPIO16 → RX, GND ↔
     GND) with the servo powered **externally** — no servo power from the
     ESP32 PCB;
   - `Tools → Servo / Carousel Tools → Servo diagnostics`, which moves
     nothing and proves the link;
   - `Configure servo mode` once, writing step servo mode to servo EPROM;
   - the forward direction, which decides `CAROUSEL_FORWARD_DIRECTION`;
   - `ST3215_POSITION_TOLERANCE`, currently a conservative and
     **unmeasured** 15 counts (≈1.3°). Set it from the closing error of
     the out-and-back tests.

   Full procedures in `Documentation/OPERATIONS.md`: "MG995 bring-up and
   calibration" and "ST3215 bring-up".
2. **Sensor read-back.** Confirm the AS7265x actually reports 100 cycles
   and 16× gain. If not, fix `ESP32/config.py` — **never** re-measure
   White/Dark to compensate.
3. **Full Spectral Calibration.** `calibration_active.json` does not
   exist. Until it does, UV/IR reflectance is not computed and DB2 cannot
   begin.
4. **One real Sample end to end**, then inspect the saved record.

### B. The 18-vs-54 question — unanswered and consequential

Nobody knows how many of the 54 candidate features carry usable signal.
The UV source is ~405 nm and the IR ~875 nm against a 410–940 nm detector
array, so many channels may sit at the noise floor under the narrow
sources.

**The experiment is already implemented.** Full Spectral Calibration
produces exactly the per-illumination denominators needed, and
`Measurements/calibration.py` already reports dead and weak channels per
lamp. One calibration run answers it.

If WHITE18 beats Full54, that is the result — document it, do not defend
the original idea.

### C. DB3 — grow the thin classes

`iron_oxide` sits at `INSUFFICIENT_DATA` (3 members, 1 answer) even though
hematite matched correctly. `carbonate` is `WEAK` at 6 members. Expanding
each to 6–8 members would let leave-one-out produce a real rating instead
of a shrug. The spectra exist in splib07a — extend `WANTED` in
`research/import_usgs.py`, rebuild, re-run the discriminability analysis.

### D. AS7265x spectral response curves

The projection uses a Gaussian approximation. Obtaining the real
per-channel response from ams OSRAM would replace one function and
regenerate DB3. Note: `crustal.usgs.gov` and `data.usgs.gov` return 403 to
automated fetches; the **ScienceBase JSON API works** and is how the
archive URL was found.

### E. Known smaller items

- `Measurements/analysis.py` (568 lines) is the largest module and
  partially duplicates `inference.py` — the old single-database path
  versus the new three-database one. Merging is worthwhile but changes
  behaviour, so it needs its own change and its own tests.
- `quality.assess()` is fed the **fixed legacy** White/Dark, so its
  illumination verdict is a constant of the installation rather than a
  property of the sample. Once an active calibration exists,
  per-illumination QC should run against that.
- Detector-seam thresholds (`BOUNDARY_WARNING_RATIO = 3.0`,
  `BOUNDARY_FAIL_RATIO = 8.0`) are unmeasured starting points.
- The ST3215 driver's `move_to_position()` (absolute, position servo
  mode) is implemented but unused: the carousel only ever calls
  `move_relative()`. It exists because a generic servo driver should be
  able to do it, and it is exercised by the mode-error tests, but it has
  never been run against hardware.
- `read_feedback()` decodes `load` as a per-mille duty cycle with sign bit
  10. Both the official library and the supplied reference driver agree on
  that unusual width — the supplied driver documents having confirmed it
  on hardware — but this firmware has not.
- The supplied reference driver's `st3215_calibrate_offset()` writes the
  position-correction register with the sign in bit **15**; the official
  memory table says bit **11**. This firmware does not expose offset
  calibration at all, so nothing is affected, but if it is ever added, use
  bit 11.

---

## 9b. Dual-servo support and the firmware refactor (this session)

The ST3215 was added as a **second** supported actuator, and the MG995 was
kept. An earlier pass in this same session had deleted the MG995; that was
reversed on instruction, and the result is better than either extreme:
neither actuator is privileged, and the difference between them is
explicit rather than assumed.

**Both backends implement one contract** (`drivers/servo_base.py`): move
slots, move degrees, half turn, stop, capture an origin, report status and
diagnostics. The contract covers only what a carousel needs. There is
deliberately no shared `set_pulse_width()` and no mandatory
`read_encoder()` — either would be a lie about one backend. What they do
share is `capabilities()`, so high-level code asks what the actuator can
do instead of testing its name:

```text
MG995    position_feedback False  encoder False  timed_positioning True
         telemetry False  torque_control False  verified_movement False
ST3215   position_feedback True   encoder True   timed_positioning False
         telemetry True   torque_control True   verified_movement True
```

**Selection is explicit and is runtime state.** After a boot:
`servo = NONE`, position invalid, every movement command answers
`SERVO_NOT_SELECTED`. `select_servo` states what is fitted. There is no
auto-detection, on purpose: probing UART2 and falling back to the MG995
would silently select open-loop control on a correctly fitted ST3215 with
one broken wire. Switching actuator always releases the previous backend
and invalidates the carousel position — physical position state cannot
cross a change of hardware.

**The firmware tree was split** along the lines that were already
implicit: `drivers/` for devices, `control/` for subsystem logic,
`protocol/` for the wire format. `main.py` went from 1807 lines to 265 and
now only builds subsystems, registers command groups and serves.

**Configuration is namespaced.** Every constant is `MG995_*`, `ST3215_*`
or `CAROUSEL_*`; no generic `SERVO_*` name survives, which
`test_architecture.py` enforces. `MG995_JOG_MAX_MS` now exists — the old
`mg995.py` referenced a constant that did not.

**The ST3215 driver** was written from the official Waveshare memory
table, the official SCServo reference implementation and the official
ServoDriverST demo, then cross-checked against the supplied
`st3215.c` / `st3215_regs.h`. No register address or frame field was
guessed. Where the supplied driver disagreed with the official docs, the
official behaviour was used and the divergence is recorded in
`drivers/st3215_registers.py` and asserted in `test_st3215.py` — most
importantly that torque register value 128 is a **mid-point calibration**,
not the "damped release" the supplied header calls it.

**ST3215 operating mode: step servo (mode 3).** The goal register means
"move this many counts from where you are". That is why `Slot 4 → Slot 1`
is a single +1024-count step and never a 270° journey — the absolute
encoder crossing 4095 → 0 is irrelevant to a relative command. Position
servo mode would either be limited to one revolution or eventually run out
of multi-turn range; the reasoning is written out in `ESP32/config.py`.

Writing that mode is a **SERVICE** operation, kept apart from carousel
calibration: ordinary setup establishes a runtime origin and writes
nothing persistent to the servo.

**Verification, not assumption — where it is possible.** On the ST3215
every movement reads the start position, commands counts, polls until the
servo reports it has stopped, settles, reads again and compares against a
tolerance. On the MG995 none of that is possible, and the firmware says so
rather than inventing it: `drift` is reported as not measurable, and the
movement record carries `verified: false`.

Either way, a movement that cannot be completed **invalidates** the
tracked position. There is no fallback from one backend to the other's
method: an ST3215 that loses feedback does not revert to timing, and an
MG995 does not pretend to have an encoder.

**Position model.** `sync_position` asks the backend to capture whatever
reference it has — an encoder reading on the ST3215, an explicit "nothing
to capture" on the MG995. Fine alignment accumulates into
`alignment_offset_deg` (degrees, so it means the same on either backend),
which is part of every later expected position, so an operator's +2° trim
is not undone by the next slot movement. On the ST3215 the offset records
the angle actually *commanded* after rounding to whole counts, so
quantization never shows up later as apparent drift.

**Power.** The firmware has no servo-power concept at all — no pin, no
enable, no switch, and `test_architecture.py` enforces that for both
backends. The ST3215 is powered externally at its driver board. The PCB's
`+5V_SERVO` branch and `SERVO_PWM` line on GPIO25 are what an MG995 build
runs on, and are unused when an ST3215 is fitted. **No Altium file was
touched.**

**Still open:** everything in §9.A.1. The work is complete in software and
fully tested against protocol-level fakes for both actuators, but no byte
has ever reached a real ST3215 and no pulse a real MG995.

---

## 9c. First hardware session — what it broke, and what was done

**The board was attached for the first time.** Everything in §9.A that
says "has never run on hardware" is now partly out of date: the sensor,
the illumination, the acquisition path and a full spectral calibration all
ran. The carousel did not — no servo has answered yet.

Four faults came back from that session. All four are addressed below;
none of them is fixed by hope, and two of them are diagnostics rather than
cures because the remaining cause is physical.

### 1. The ST3215 never answered — and the error said nothing useful

```text
SERVO_NOT_FOUND
No answer from servo ID 1 on UART2 (TX GPIO17, RX GPIO16, 1000000 baud).
```

True, and nearly useless. It names four independent assumptions — ID, baud
rate, which wire carries TX, whether the servo has power — and tests none
of them. The operator had checked the wiring and measured 9.3 V, which
rules out nothing: 9.3 V at the supply with the servo unplugged says only
that the supply works.

**`drivers/st3215.py::bus_scan()`** now asks the bus instead. It pings at
all eight baud rates the ST3215 supports, in **both pin orders**, and
reports what came back at every combination — bytes received, whether our
own transmission echoed, checksum failures, and any ID that answered. Six
verdicts, each with a different next step:

```text
SERVO_FOUND      found it; if ID/baud/pins differ from config.py, the
                 servo is telling you what it actually uses
WRONG_ID         one-line change to ST3215_SERVO_ID
ECHO_ONLY        the ESP32 side is PROVEN GOOD - it transmits and hears
                 itself. Look at servo power and the servo ID
CORRUPT_TRAFFIC  frames arrive, checksums fail -> common ground
NOISE_ONLY       bytes but no frames -> ground, or a contended pin
SILENT_BUS       nothing, either pin order, any rate -> power or
                 connection, not configuration
```

Only `INST_PING` is ever sent, which is a read: **the scan moves nothing**.
It runs with **no servo selected**, which is the entire point — requiring
a working selection before diagnosing why selection fails would be
circular. If an ST3215 is selected it is released first (the scan reopens
UART2 at each rate) and the release is reported.

Reachable three ways: offered automatically when `select_servo` fails with
a bus-level error, `Tools → Servo / Carousel Tools → [b]`, and the same
menu's no-servo-selected branch.

`servo_bus_scan` is a new firmware command. `test_st3215.py` covers all
six verdicts against a fake bus that answers at exactly one baud rate, ID
and pin order, and asserts that the scan sends nothing but PING.

### 2. A calibration was lost to a damaged answer

```text
IR_CALIBRATION_FAILED
Timeout: No response to request 19 within 180 s. Device also sent
non-JSON output: ����...����"data": {"bulbs_off": true, "acquisitions":
```

Read that carefully: the module **did** answer. About sixty bytes at the
head of the frame were destroyed in transit, the line would not parse, and
the PC then waited out its full 180 s timeout for a response it had
already received — and threw away the whole calibration, Dark included.

Three separate fixes, because there were three separate mistakes:

- **Recover what can be recovered.** `esp32_link.salvage_json()` parses
  from the first `{`, so a frame with rubbish merely in *front* of it is
  delivered normally.
- **Never wait out the timeout for an answer that has already arrived.** A
  line that will not parse but carries response fields (`"request_id"`,
  `"data":`) is a damaged frame, and raises `CorruptFrameError`
  immediately. Pure reads — acquisitions, status, diagnostics — are then
  re-sent up to `READ_RETRIES` times. **Movements are not**, and that is
  deliberate: an ST3215 step is relative, so a movement whose
  acknowledgement was lost has still happened, and repeating it would move
  the carousel twice.
- **Stop making the damage.** The response was being written into the
  switching transient of the illumination LED being turned off
  microseconds earlier. `ACQUISITION_RESPONSE_SETTLE_MS = 5` waits for the
  supply to recover first, and `RESPONSE_GUARD_NEWLINE` puts a newline in
  front of every frame so anything already on the console stays in its own
  line.

And the calibration wizard no longer throws away three good blocks because
the fourth failed: a failed block is **retried on its own**, with the
reference target untouched.

The transient explanation is inference from the timing, not a measurement.
If damaged frames continue, `link.corrupt_frames` counts them.

### 3. Only DB1 was being consulted

The sensor test compared against DB1 and stopped there. DB3's 84 projected
spectra — the entire reason the USGS import exists — never ran.

`Measurements/inference.py::infer()` already did cross-database inference
and was fully tested; nothing in the PC called it. It is now wired in at
`Mission.analyse_raw`, and every measurement and every sensor test reports
all three databases separately, plus the consensus, the confidence
factors and the mixture analysis. The result is stored with the sample
under `cross_database`, beside `analysis` rather than replacing it.

**The part that needed real design:** the three databases are not
normalized alike, and one feature vector cannot serve them all. DB1 may
only ever be compared against the frozen legacy White/Dark it was built
with. DB3 was never measured on this instrument at all, so the honest
comparison is against the CURRENT calibration. Forcing one vector on both
would have made one of them quietly wrong.

So `infer()` gained `feature_sources`: per database, which normalization
it is entitled to see. Every database result records the normalization it
was scored under. `test_inference.py` asserts that scoring DB1 against the
wrong normalization is measurably worse, so the split cannot rot into
decoration.

On the operator's own measurement (grey milled dry soil), DB1 says
Bentonite at 99.69% while DB3 says Pyroxene Basalt at 98.04% — a
disagreement, correctly reported as `MATERIAL_FAMILY` with LOW confidence
rather than resolved by arithmetic.

### 4. A new calibration on every restart

Calibrations were one file each plus a `calibration_active.json` pointer —
one fact spread across two files, and no list to choose from. The operator
was making a fresh calibration every session because the interface offered
no alternative.

`BD/data/calibrations.json` now holds **every** calibration ever made, in
full — timestamp, sensor settings, repeats, per-channel statistics and
every individual acquisition — together with the ID of the active one.
`Sensor Test → [7] Select Which Calibration To Use` lists them with the
gain, integration time and repeat count each was taken under, because an
ID and a date are not enough to choose between two of them.

Migration is automatic and non-destructive: the old per-calibration files
are read once into the library and **left exactly where they are**.

Storage stays immutable. Saving appends, activating changes one field, and
nothing is ever deleted through the interface.

### Still open after this session

- **No servo has answered yet.** The scan tells you which of the four
  assumptions is wrong; it cannot supply power. Run it, then act on the
  verdict. Everything in §9.A.1 remains unstarted.
- The switching-transient explanation for the damaged frame is
  circumstantial. `corrupt_frames` is the number to watch.
- `quality.assess()` still runs against the fixed legacy White/Dark
  (§9.E). Now that an active calibration exists on hardware, this is worth
  doing next.
- DB2 is still empty, and one full calibration now exists — the 18-vs-54
  question in §B can finally be answered with real data.

---

## 9d. The decision architecture (this session)

The analysis pipeline was split into six layers so that FREYA can learn
from verified measurements **without ever rewriting a measured reference
spectrum**. Full detail in `Documentation/DECISION_ARCHITECTURE.md`; what
follows is what a new session needs to know.

### The split

```text
Measurements/    deterministic mathematics   -> EvidencePackage
DecisionModel/   learned interpretation      -> Decision
BD/              immutable data + history
```

`Measurements/` no longer reaches a conclusion. It answers "what did the
detector report, how good was it, how far is it from everything" and
stops. `DecisionModel/` decides what that means, in a vocabulary of
exactly four levels — `KNOWN_MATERIAL`, `MATERIAL_FAMILY`,
`AMBIGUOUS_SET`, `UNKNOWN` — with secondary interpretations that travel
alongside and never replace the level.

`test_architecture.py` enforces both directions: BD imports neither
Measurements nor DecisionModel, and Measurements never imports
DecisionModel.

### The learning database

`BD/data/decision_learning/decision_learning.sqlite3`, PC-side only,
built from the readable `seed_observations.json` beside it. It holds
observations, ground truth with provenance, and one prediction row per
(measurement, model version).

**Four rules it exists to enforce**, each with a test:

1. A prediction can never become ground truth. `add_ground_truth` refuses
   any label whose source names a model.
2. A historical prediction is never rewritten. Model v7 disagreeing with
   v3 writes a new row; both survive.
3. A raw measurement is never edited. Observations are insert-only and
   hashed.
4. Only VERIFIED labels train by default. UNVERIFIED and UNKNOWN can
   never be requested as training labels at all.

The twelve measurements of 2026-08-17 are imported as the first verified
observations, with what the old pipeline concluded about each.

### The finding that mattered most

**Six of twelve measurements were being thrown away for no reason.**
`quality.check_reflectance` failed them for exceeding reflectance 1.0,
and a FAIL suppressed identification entirely. But the raw counts were
always valid — the samples are simply brighter than the white target that
was used. Hardware quality and normalization quality are now separate
verdicts, and a `NORMALIZATION_WARNING` lowers the weight of
reflectance-derived evidence without suppressing anything.

The same distinction runs down to individual features: of 54, all 54 are
valid raw counts and only 27 support a usable reflectance, because the UV
lamp leaves 1.7 counts of headroom above dark at 680 nm and the IR lamp
8.5 counts at 460 nm. `Measurements/channel_reliability.py` computes that
per feature and says so in counts.

### Old versus new, on the twelve

| | old | V001 |
|---|---|---|
| right material named | 2 | 2 |
| **wrong material named** | **3** | **0** |
| suppressed by QC | 7 | 0 |
| family right / wrong | — | 1 / 3 |
| ambiguous with / without truth | — | 1 / 1 |
| UNKNOWN | — | 4 |

Better where it matters most - it no longer names materials confidently
and wrongly - and not uniformly better: three family answers are wrong.

### What is blocking the next step, precisely

1. **One independent measurement per material.** Every class has a
   centroid and no scatter, so "is this typical of Talc?" cannot be
   asked, and `cross_validation.feasibility` refuses to run supervised
   validation. A SECOND independent measurement of each material -
   removed, repacked, remeasured - is the single highest-value thing to
   do next. At six per class, Mahalanobis becomes available.
2. **The white reference does not describe the samples.** Six of twelve
   exceed it, one by 53%. Re-take the calibration with the target in the
   exact sample position.
3. **Zero paired materials for calibration transfer**, so
   `calibration_transfer.build` returns UNAVAILABLE with the count.

### Rules for whoever works on this next

- **Do not tune a threshold against the twelve.** They are validation
  cases. Every threshold is labelled `PROVISIONAL_UNVALIDATED`; changing
  one on the strength of the comparison report makes the report
  meaningless.
- **Do not implement LDA, PLS-DA or SVM yet.** With 12 classes and one
  group each they are singular or need a validation set that does not
  exist. `cross_validation.feasibility` is the gate.
- **Retraining is an explicit command**, never a consequence of
  measuring.

```powershell
py -m DecisionModel.training.validate --record --detail
```

---

## 10. Rules that are not negotiable

Each was learned the expensive way.

1. **Never fabricate a spectrum, wavelength or metadata field.** DB3's
   validator refuses a record without `source_dataset`,
   `source_record_id` and `license`. If data cannot be obtained, say so
   and build the pipeline around the gap.
2. **Raw measurements are never overwritten by derived values.** DB1 keeps
   Sample/Dark/White beside the reflectance for exactly this reason.
3. **Never clip reflectance to [0,1].** Values above 1.0 are real
   information about geometry or scattering.
4. **Mathematically dependent metrics do not get independent votes.**
5. **Similarity is not probability. Contribution is not concentration.**
6. **UNKNOWN is a successful result.** Do not force a material.
7. **Do not invent weights or thresholds.** Derive them, or label them
   PROVISIONAL.
8. **The ESP32 performs no science** and gains no CPython dependency.
9. **Do not commit** unless asked.
10. **A prediction is never ground truth.** A model trained on its own
    output learns its own mistakes and grows more confident with every
    round.
11. **A historical prediction is never rewritten.** A newer model writes
    a newer row; both survive, and that is what makes regression
    tracking possible.
12. **Measurements produces evidence, DecisionModel produces
    conclusions.** Neither does the other's job.

---

## 11. First moves for a new session

1. `git status` and `git log -1` — confirm still at `93b4d3f`, uncommitted.
2. Back up `firmware/BD/data/samples.json` outside the repository.
3. Run `py run_all.py`; expect 14 suites, 1673 checks.
4. Read `Documentation/ARCHITECTURE.md` and `DATABASES.md`.
5. Then pick from §9. If the board is available, **A is worth more than
   everything else combined.**

```powershell
py C:\Users\Public\Documents\Altium\as7265x-soil-analysis-module\firmware\PC\rover_science_client.py --port COM4
```

```powershell
py C:\Users\Public\Documents\Altium\as7265x-soil-analysis-module\firmware\research\analyse_discriminability.py
```
