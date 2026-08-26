# Architecture

The system in one picture, and the rules that keep it that shape.

```
                         OPERATOR
                            │
                            ▼
                            PC
                 workflow / orchestration
                      /              \
                     /                \
                    ▼                  ▼
                 ESP32              Science
             hardware + RAW       scientific math
                    │                  │
                    │                  ▼
                    │                  BD
                    │        calibration / DB1 / DB2 /
                    │        DB3 / training / samples
                    │                  ▲
                    └── Measurement ───┘
```

Five questions, five answers:

| Question | Answer |
|---|---|
| Who controls physical hardware? | **ESP32** |
| Who controls operator workflow? | **PC** |
| Who performs scientific processing? | **Science** |
| Who produces the final classification? | **Decision Model, inside Science** |
| Where are calibration, references and completed results stored? | **BD** |
| Who generates the final report? | **Not this project.** |

---

## 1. The directory layout

```
firmware/
├── ESP32/      the hardware controller, MicroPython, flat
├── PC/         operator workflow and orchestration
├── Science/    every scientific formula and the Decision Model
├── BD/         calibration, DB1/DB2/DB3, training data, records
├── Tests/      the software campaign, and the hardware backlog
├── tools/      device.py — deploy and verify the ESP32
└── research/   non-production: experiments, ERC planning, training
```

`ESP32/`, `PC/`, `Science/` and `BD/` are the production domains.
`Tests/`, `tools/` and `research/` support them. There is nothing else.

---

## 2. Dependency direction

Allowed:

```
PC ──────► ESP32       JSON over USB, nothing else
PC ──────► Science
PC ──────► BD
Science ─► BD          calibration and reference databases
Tests ───► everything
tools ───► PC/serial_link.py
```

Forbidden, and each enforced by a check in `Tests/software/static/test_architecture.py`:

```
ESP32 ──► Science, BD, PC        the device cannot see a database
Science ► a serial port, a servo, a carousel, the operator
BD ─────► Science                storage must be readable without
                                 the layer that interprets it
BD ─────► hardware
production ► research            an unvalidated experiment must never
                                 reach a reported conclusion
```

`BD/channels.py` is the one thing every layer imports. It holds the
18 channel letters, their wavelengths and the feature-space
identifiers — the *schema* of the stored data, not its meaning. BD has
to be able to check the shape of a record without depending on Science.

---

## 3. ESP32 — hardware, and only hardware

Seven files, flat, no packages:

| File | Responsibility |
|---|---|
| `boot.py` | Deliberately empty. Explains why. |
| `main.py` | Build the runtime state, build the protocol, serve. |
| `config.py` | Every hardware constant, once. |
| `sensor.py` | AS7265x driver and the one sensor lifecycle. |
| `servo.py` | ST3215 wire protocol, driver and connection gate. |
| `carousel.py` | Slot geometry and logical position. |
| `protocol.py` | Newline-JSON framing, dispatch, and all 25 commands. |

The device never learns what a material is. No cosine, no
normalization, no database, no decision.

### Protocol first

Nothing between reset and the serving loop touches a peripheral — no
I²C transaction, no UART, no settling delay, no retry loop. A board
that cannot be reached cannot be diagnosed.

```
RESET → boot.py → main.py → JSON protocol ONLINE
                              │
                              ├── sensor:   NOT_INITIALIZED → READY | UNAVAILABLE
                              ├── servo:    not connected until asked
                              └── carousel: position invalid until declared
```

Measured on hardware: the board answers `ping` **3 ms** after boot. The
3-second sensor settling delay is still enforced, but it is paid by the
first command that actually needs the sensor, not by the protocol.

Every one of those degraded states leaves `ping`, `get_status` and the
diagnostics answering — because those are how an operator finds out
which of them is the problem.

### Carousel geometry

```
4 slots, 90° apart
loader and scanner exactly 180° apart = 2 slots

    load_slot = ((scan_slot - 1 + 2) % 4) + 1
```

Its own inverse: Slot 1 at the loader necessarily means Slot 3 at the
scanner. The mapping, the slot count and the offset all come from
`config.py`, and the carousel asserts their consistency at construction.

**Logical geometry is not actuator calibration.** One slot is 90°,
always, even where the real mechanism needs a small correction. The
correction is a mechanical calibration; the 90° is a fact about the
carousel.

---

## 4. PC — workflow and orchestration

```
PC/
├── rover_science_client.py   entry point: parse, open one port, hand over
├── serial_link.py            the ONE serial owner
└── workflow/
    ├── prompts.py            the only input() in the project
    ├── display.py            results → tables
    ├── session.py            link, stores, loaded science layer
    ├── calibration.py        making and choosing a calibration
    ├── carousel.py           servo setup, alignment, diagnostics
    ├── measure.py            the measurement sequence
    ├── records.py            the archive, and ground truth
    └── screen.py             the main screen and the menu loop
```

The PC implements no scientific formula. Every number on screen was
computed by Science.

### Exactly one serial owner

`serial_link.py` is the only module in the project that imports
pyserial. Two facts in it were measured on the bench, not assumed:

- **Opening the port resets the board.** pySerial asserts DTR and RTS
  on open, which drives the auto-reset circuit. Setting both low
  *before* `open()` leaves the firmware running.
- **A hardware reset is an RTS pulse with DTR low**, producing
  `boot:0x13 (SPI_FAST_FLASH_BOOT)`. Driving both lines instead lands
  in `boot:0x3 (DOWNLOAD_BOOT)`, where the firmware never runs.

Failures carry a code, because "the module did not answer" has eight
causes needing eight different actions:

```
PORT_NOT_FOUND      no such port
PORT_BUSY           it exists and something else holds it
PORT_DENIED         it exists, nothing holds it, and this account may
                    not open it - Linux serial group membership
PORT_OPEN_FAILED    it exists, is free, and would not open
PORT_LOST           it disappeared mid-request
PORT_CLOSED         the link is closed, usually because it was lost.
                    Every hardware method answers this rather than
                    raising - see below
PROTOCOL_TIMEOUT    the port opened; nothing answered
DEVICE_AT_REPL      MicroPython is at >>>, not serving
MALFORMED_RESPONSE  something answered and it was not a frame
INVALID_REQUEST     we were asked to send something that is not legal
                    JSON, and refused before sending it
DEVICE_ERROR        the firmware answered "ok": false
```

**A closed link is an operational condition, not a programming error.**
Every method on `SerialLink` answers a closed link with a `PORT_CLOSED`
LinkError. It used to raise a bare `RuntimeError`, and on the Linux
bench that ended the application: `/dev/ttyUSB0` disappeared, the link
correctly closed itself, and the next status refresh from the menu loop
was an uncaught traceback. 39 places in the PC layer catch `LinkError`;
exactly one caught `RuntimeError`.

**Matching an answer to its question** needs three things to agree: the
request id, the presence of `ok`, and the command name. Request ids are
`<24 random bits>-<counter>`, because two earlier designs collided
across sessions — a counter from 1, and a clock-seeded counter that
wrapped every 1000 seconds.

**A movement failure says what the mechanism did.** `NOT_STARTED`,
`MOVED` or `UNKNOWN` — never a bare "nothing moved", which an operator
standing at the rover acts on physically.

---

## 5. Science — the mathematics, and nothing else

```
Science/
├── channels via BD          the 18 bands and the feature spaces
├── preprocessing.py         dark, white, representations, repeats
├── quality.py               measurement quality and channel reliability
├── features.py              derivatives, ratios, cross-illumination
├── metrics.py               cosine, SAM, correlation, distances, ranking
├── comparison.py            distance to a material CLASS
├── calibration.py           building and validating a calibration
├── taxonomy.py              material identity and family
├── class_models.py          class statistics from verified history
├── model_registry.py        which model is ACTIVE, and why
├── decision.py              the Decision Model
└── pipeline.py              evidence, and the one entry point
```

Deterministic and hardware-independent:

```python
run = pipeline.analyze(measurement, calibration, registry, ...)
```

Given the same Measurement, calibration and databases it returns the
same answer. It opens no port, moves nothing, asks nothing, writes
nothing.

### The order

```
RAW Measurement
  → schema / acquisition validation
  → calibration selection
  → dark correction
  → white normalization
  → quality evaluation
  → feature extraction
  → DB1, DB2, DB3, each INDEPENDENTLY
  → individual metric evidence
  → distance to reference, distance to class
  → cross-method and cross-database agreement
  → Decision Model
  → AnalysisRun
```

Each stage records its own status. A database that will not load, an
absent class snapshot or a model that raises leaves everything the
earlier stages produced intact:

```
analysis_status = PARTIAL
DB1  OK      evidence kept
DB2  OK      evidence kept
DB3  FAILED  reason recorded
```

### One comparison, not two

`pipeline.build()` compares the measurement against each database once
and keeps the three results apart. The Decision Model is the only thing
allowed to combine them. There is deliberately no second per-database
comparison reaching its own consensus — that arrangement produced two
answers in one record with nothing to say which was right.

### Provenance classes

Never blurred:

| Class | Example |
|---|---|
| `MEASURED` | an AS7265x raw channel; DB1 and DB2 |
| `CALCULATED` | dark-corrected, normalized, a cosine, a 54-vector |
| `REFERENCE` | a published external spectrum, as published |
| `DERIVED_REFERENCE` | that spectrum projected onto our bands; DB3 |
| `MODEL_INFERENCE` | a Decision Model conclusion |

---

## 6. BD — the authoritative record store

```
BD/
├── calibration/   dark and white references, and the conditions
├── DB1/           MEASURED here, 18 channels, 23 materials, legacy
├── DB2/           MEASURED here, 54 features (WHITE/UV/IR)
├── DB3/           DERIVED_REFERENCE, 84 external spectra projected
├── training/      labelled records and the decision history, OFFLINE only
├── models/        validated model artifacts and the registry
└── samples/       completed Sample records — the run's only output
```

One directory per thing it answers a question about. Two of these are
read-only scientific evidence and one is the only thing the system
writes.

### The three databases are never pooled

A cosine of 0.97 against DB1 means *"this looks like something we
measured here"*. The same number against DB3 means *"this looks like a
laboratory spectrum, after modelling our sensor"*. They are compared
separately, reported separately, and only then checked for agreement.

DB1 is compared **only** against the frozen legacy calibration it was
built with. Normalizing it against today's calibration would silently
change what every stored number means.

---

## 7. The record model

```
Sample                      the physical material
│
├── metadata
│
└── Measurements[]          one completed physical acquisition each
      │
      ├── RAW, grouped by illumination     ← written once, never again
      ├── acquisition metadata
      ├── acquisition_status
      ├── calibration_id
      │
      └── AnalysisRuns[]    one interpretation each
            ├── preprocessing
            ├── quality
            ├── metrics
            ├── DB1 / DB2 / DB3, separately
            ├── reference distances
            ├── class distances
            ├── method and database agreement
            ├── Decision Model result
            └── versions and provenance
```

Three layers, never collapsed. A store where `sample.measurement` is
singular cannot be taught repeatability afterwards without rewriting
every record ever saved.

**RAW is immutable.** Once a successful Measurement is persisted its
`raw` block is never written again — not by normalization, a new
calibration, a new Science version or an edit. There is no API to
modify a Measurement; the only way to add data is to add a record.

**RAW is saved before Science runs.** The measurement flow is

```
acquire → PERSIST RAW → analyse → persist the AnalysisRun
```

so a database that will not load, an exception in the pipeline or a
model that raises costs an *analysis*, not an *experiment*.

**A failed acquisition is not poor quality.** It is stored with
`acquisition_status = FAILED` and no `raw` key at all — deliberately
not a successful measurement full of zeros, which cannot be told apart
from a genuinely dark reading.

---

## 8. Where the project stops

```
FREYA
  ↓
complete structured Sample Result
  ↓
BD/samples/
  ↓
──────── PROJECT BOUNDARY ────────
  ↓
external AI or human workflow
  ↓
report / document / prose
```

The system produces a structured, versioned, traceable scientific
record. It does not produce prose, PDFs, mission reports or competition
documents, and production Science contains nothing that could.

The record has to carry enough evidence for someone outside to write
that report without guessing: which pipeline, which model, which
library versions, which calibration, what each database said on its
own, where the methods disagreed, and what was refused.
