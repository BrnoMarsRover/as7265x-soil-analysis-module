# Architecture

The software of a scientific instrument. Four runtime layers, one
responsibility each, with the boundaries enforced by tests rather than by
convention.

```text
firmware/
├── ESP32/          acquire      MicroPython, uploaded to the board
├── PC/             orchestrate  operator UI, workflow, serial transport
├── BD/             remember     channels, databases, calibrations, samples
├── Measurements/   understand   calibration maths, metrics, inference
├── Tests/          verify       runs without a board
└── research/       investigate  dataset building, spectral projection
```

Everything else in the repository is not software: `Hardware/` is the
Altium design, `Documentation/` is this, `Photos/` is images.

---

## 1. Why these layers

The split is by *what a machine has to be* to run the code, not by
convenience.

**ESP32 acquires.** It moves the carousel, drives the AS7265x, and returns
raw counts. It performs no science: it does not know what a material is,
loads no database, and imports nothing from the host. It must also stay
MicroPython-compatible, which rules out the scientific Python stack.

### Inside the ESP32 layer

The device firmware is itself layered, and the direction is one-way:

```text
main.py          build the subsystems, register the commands, serve
   |
protocol/        the wire format and the command surface
   |             transport, router, servo/carousel/sensor/sample commands
control/         hardware subsystem logic
   |             servo_manager (actuator lifecycle), carousel (geometry)
drivers/         one module per physical device, and nothing else
                 as7265x, st3215, st3215_registers, servo_base
```

A driver reaching back into the carousel would stop being reusable and
would make swapping actuators a non-local change. Nothing at runtime would
complain, so `test_architecture.py` reads the import statements and fails
if any module imports a layer above itself.

**Drivers know about devices, not about instruments.** `st3215.py`
understands positions, counts, speeds, feedback and the serial protocol.
It does not know what a sample, a loader or a scanner is. A method like
`servo.go_to_slot_1()` would put mechanism-specific knowledge inside a
device driver.

**The carousel knows about geometry, not about actuators.** It asks for
"two slots clockwise" or "half a turn" and the driver decides how many
encoder counts that is.
`carousel.py` contains no pulse width, no encoder count, no packet and no
register — asserted by test, because that is exactly the kind of boundary
that erodes one convenient reference at a time.

**Two actuators, honestly different.** The shared contract in
`servo_base.py` covers only what a carousel needs — move slots, move
degrees, half turn, stop, capture an origin, report status. It deliberately
does *not* define a common `set_pulse_width()` or a mandatory
`read_encoder()`, because either would be a lie about one backend. What the
backends do share is a `capabilities()` dictionary, so high-level code asks
what the actuator can do instead of testing its name.

### Two serial channels, never conflated

```text
PC  <--- USB / CP2102, sys.stdin + sys.stdout ---> ESP32   host protocol
ESP32 <--- UART2, GPIO17 / GPIO16 / GND ---> servo driver  servo bus
```

`main.py` never creates a UART; only `st3215.py` does. Nothing in
`st3215.py` touches the console streams, and nothing in
`protocol/transport.py` opens a UART. All three facts are enforced by
`test_architecture.py`.

**PC orchestrates.** Operator interface, sample workflow, serial
transport. It owns no scientific formula and writes no scientific file
itself — it calls BD to persist and Measurements to compute.

**BD remembers.** The channel vocabulary, the three databases,
calibrations and the measured-sample archive. It loads, validates
structure and reports; it computes no similarity.

**Measurements understands.** Dark correction, normalization, quality
control, metrics, cross-database inference, mixture estimation. Pure
computation over plain dictionaries — no serial port, no I²C, no UI.

### The one forbidden edge

```text
BD → Measurements    FORBIDDEN, tested
Measurements → BD    allowed and used
```

Persistence must not depend on mathematics. That is the edge which, left
open, re-entangles data with the algorithms that interpret it — the
original problem this architecture was built to fix. `test_architecture`
reads the import statements and fails if it appears.

The consequence shows up in two places, both deliberate:

- The 18-channel vocabulary lives in `BD/channels.py`, not in
  Measurements, because BD must validate record shape without importing
  the science layer.
- `BD/calibrations.py::activate()` takes a **validator callable**. It must
  refuse to activate a calibration that failed its checks, but the check
  itself is science, so PC injects it.

## 2. Feature spaces

The sensor has **18 detectors**. It does not have 54 wavelengths. What it
can produce is 18 bands under each of three illuminations — **54
features**, which is a different claim.

```text
AS7265X_18              "A" … "W"                 18 features
AS7265X_54_MULTIILLUM   "white:A" … "ir:W"        54 features
```

Every database, measurement and comparison declares its space.
Comparing 18 against 54 **raises** rather than aligning the first 18
numbers. Narrowing 54 → 18 is permitted because a 54-feature measurement
genuinely *contains* its WHITE bands; the reverse is impossible and is not
offered.

## 3. Metrics: evidence families

Implementing five similarity metrics and averaging them looks rigorous and
is not, because the metrics are not independent:

```text
SAM  = arccos(cosine)             → rank-identical to cosine
RMSE = Euclidean / sqrt(18)       → rank-identical to Euclidean
cosine(x, kx) = pearson(x, kx) = 1  → both blind to brightness
```

Measured on DB1: **45% of the 253 material pairs score cosine ≥ 0.99**,
median 0.9859. The worst pair — Borax vs Tartaric Acid at 0.99991 — is two
chemically unrelated materials. Cosine alone cannot discriminate here.

But the obvious fix was also wrong. The previous ensemble weighted
cosine : RMSE : Pearson equally and called them three independent views.
Pearson is the cosine of the mean-centered vectors and is **identically
scale-invariant**: on a real spectrum scaled to 0.25×, cosine = 1.000000,
Pearson = 1.000000, RMSE = 0.0235. So the ensemble was two shape metrics
and one magnitude metric weighted 1:1:1 — a hidden 2:1 bias toward shape,
the exact failure it was meant to fix.

Metrics are therefore grouped into **families**, each nominating one
ranking statistic; only families vote:

| Family | Ranks by | Also reported | Sees brightness? |
|---|---|---|---|
| `magnitude` | RMSE | MAE | **yes** |
| `angular` | cosine | spectral angle (deg) | no |
| `centered_shape` | Pearson r | — | no |

Families combine by **rank**, not by blending scores — a cosine of 0.99, an
RMSE of 0.03 and an r of 0.87 cannot be meaningfully averaged.

Family weights are equal and labelled `PROVISIONAL_UNVALIDATED`. Correcting
a 2:1 bias by asserting a different ratio would replace one invented number
with another; weights get derived from a validation dataset or not at all.

## 4. Three databases, never pooled

| | Evidence | Space | Meaning of a 0.97 match |
|---|---|---|---|
| **DB1** | MEASURED | 18 | "resembles something we measured here" |
| **DB2** | MEASURED | 54 | the same, with UV and IR |
| **DB3** | REFERENCE_PROJECTED | 18 | "resembles a lab spectrum, after modelling our sensor" |

Those are different claims, so scores are never averaged across databases.
Each is analysed separately and reported separately; what combines is the
*ranking*. Agreement between DB1 and DB3 is strong evidence — they share
no instrument, no operator and no measurement. Agreement between DB1 and
DB2 is weaker: same instrument.

See `DATABASES.md`.

## 5. Confidence is not similarity

A 99% cosine against a library where every pair scores 99% carries no
information. Confidence comes from evidence structure:

- how far the winner leads the runner-up
- whether the metric families agree
- whether the databases agree
- measurement quality
- whether the sample resembles the library at all

Any of these failing lowers it, and the system may answer **UNKNOWN**
rather than crowning whichever entry ranked first. Confidence is a
heuristic engineering judgement, explicitly not a calibrated probability.

## 6. Imports

`BD` and `Measurements` are packages imported from the `firmware/` root.
`PC` and `Tests` are scripts that put that root on `sys.path` and then use
absolute package imports. `ESP32` stays flat — MicroPython has no packages
and the six files are uploaded flat to the device.

`ESP32/as7265x.py` necessarily carries its own channel layout: it cannot
import `BD`. `test_architecture` asserts the host's expected sensor
settings match `ESP32/config.py`, so the two cannot drift apart silently.

## 7. History

Three earlier decisions are folded into the text above rather than kept as
separate records:

- The four-layer split and the forbidden BD→Measurements edge.
- Evidence families replacing the equal-weight three-metric ensemble.
- All software under `firmware/`. An earlier revision placed the layers at
  the repository root; that mixed software with the Altium design at the
  same level and was reversed. The move cost two path constants, which is
  itself evidence the layering was real rather than positional.
