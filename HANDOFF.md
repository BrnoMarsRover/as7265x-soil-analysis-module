# HANDOFF — Freya AS7265x science module

State as of **2026-08-19**, after the full software rewrite. Read this
first, then `Documentation/ARCHITECTURE.md`, then
`Documentation/OPERATIONS.md`.

This is the "where we are right now" note, written for whoever picks
the project up next — human or model — with no memory of how it got
here.

---

## 0. Read this before touching anything

**Nothing is committed.** The rewrite lives entirely in the working
tree. The author commits deliberately: **do not commit or push unless
asked.**

**`firmware/BD/samples/samples.json` is untracked AND gitignored.** It
holds the only real measurement ever taken (`s456`). Git cannot restore
it. Beside it, `samples.schema2.backup.json` is the pre-migration copy
of that same record — also untracked, also irreplaceable. Copy both
outside the repository before any bulk file operation.

`firmware/BD/calibration/calibration_active.json` and
`firmware/research/data/` are gitignored too. Everything else under
`BD/` is tracked.

---

## 1. What changed in this session

The whole software system was redesigned from the requirements and the
useful parts of the old implementation migrated into it. This was not a
refactor; two top-level domains were dissolved and the record model was
replaced.

### Structure

```
before                              after
──────────────────────────────      ──────────────────────────────
firmware/ESP32/  18 files,          firmware/ESP32/   7 files, flat
  drivers/ control/ protocol/
firmware/PC/     2 files            firmware/PC/      client + link
  rover_science_client.py 6128 ln      + workflow/ (8 modules)
firmware/Measurements/  15 files    firmware/Science/ 11 modules
firmware/DecisionModel/ 16 files      (both dissolved into it)
firmware/Science/  ERC planning     firmware/research/erc/
firmware/BD/data/  11 files in      firmware/BD/{calibration,DB1,DB2,
  one directory                       DB3,training,models,samples}/
                                    firmware/tools/device.py  (new)
```

`Measurements/` and `DecisionModel/` no longer exist. Neither does the
old `Science/`, which was ERC mission planning and report generation —
it is `research/erc/` now, and production Science never imports it.

### The record model

`sample.measurement` (singular) became:

```
Sample → N Measurements → N AnalysisRuns
```

The one existing record was migrated losslessly: RAW is byte-identical,
and the CALCULATED representations that used to sit beside it moved
into `A001` where they belong. `test_data.py` checks that field by
field against the backup.

### Ordering

The measurement flow was `acquire → analyse → save`. It is now
`acquire → PERSIST RAW → analyse → persist the AnalysisRun`, so a
Science failure costs an analysis rather than an experiment.

### Things deliberately removed

- **MG995** — every reference, including comments. `ST3215` is the only
  actuator. Zero matches for `mg995` outside the test that asserts it.
- **The servo capability table** — seven booleans that were all `True`
  with one actuator, gating menu options that always existed.
- **The second per-database comparison** (`comparison.infer`,
  `build_consensus`, `assess_confidence`) — it re-ran the comparison
  `pipeline.build()` already does and reached its own consensus, so one
  record held two answers with nothing to say which was right.
- **`Measurements/analysis.py`** — a second preprocessing + metrics +
  interpretation stack beside the current one.
- **NNLS mixture estimation** — moved to `research/mixture.py`. It
  produced numbers that look exactly like composition with no validated
  quantitative basis. Similarity is not abundance.

---

## 2. Two bugs found by measuring, not reading

Both had been producing the symptom "COM4 does not answer".

**Opening the port reset the board.** pySerial asserts DTR and RTS on
open, which drives the auto-reset circuit. The old `main.py` then spent
its first seconds in `STARTUP_DELAY_SECONDS` plus sensor-init retries
before it served anything, so `ping` timed out against a board that was
alive and busy booting. Fixed on both sides: the lines are driven low
*before* `open()`, and `main.py` serves before touching a peripheral.
The board now answers `ping` 3 ms after boot.

**`mpremote` leaves the board at the REPL.** It interrupts whatever is
running to take control and does not restart `main.py`. Any deployment
step using it must run *before* the reset — the first version of
`device.py` checked port release with `mpremote` *after* the reset and
so stopped the board serving one line after reporting that it served.

A related trap: at the REPL the board echoes the request and evaluates
it, so the echo carries **our own `request_id`**. A reader matching on
`request_id` alone accepts it and reports a healthy link to a board
that is not running the firmware. A response frame must be required to
contain `"ok"`.

---

## 3. Where the hardware stands

Both peripherals were **physically absent** from the bench during this
session, and that is a bench condition, not a software fault:

```
AS7265x   the I2C bus responds; nothing answers at 0x49
          -> I2C_NO_DEVICES, stage I2C_SCAN
ST3215    no answer from servo ID 1 on UART2 (TX 17, RX 16, 1 Mbps)
          -> SERVO_NOT_FOUND
```

Everything that does not need them was verified on the real board:
clean deploy, content hashes, imports, reset, automatic boot, `ping`,
`get_status`, structured degradation, `PORT_BUSY` for a second client,
and immediate port release.

**Next hardware session:** reconnect both, then

1. `py firmware\tools\device.py deploy --port COM4 --clean`
2. `py firmware\PC\rover_science_client.py --port COM4`, option `[0]`
3. Confirm the ST3215 answers, and check `ST3215_POSITION_TOLERANCE`
   (15 counts ≈ 1.3°) against the real repeatability — the shipped
   value is conservative and **not measured**.
4. Take one full WHITE/UV/IR calibration, then one sample, and confirm
   the record comes out as `Sample → M001 → A001`.

See also the standing note on the ST3215 ECHO_ONLY fault: the scan
previously answered only with TX/RX exchanged, and only ID 1 was ever
probed.

---

## 4. What is not finished

**DB2 is empty.** It is the current 54-feature measured library and has
zero materials. It cannot be derived from DB1 — DB1 holds 18 raw values
per material under a single illumination, and the 36 missing UV and IR
features were never measured. Populating DB2 means physically
remeasuring the materials under WHITE, UV and IR. The registry reports
it as `EMPTY` and the other two databases carry on without it.

**The Decision Model is a cold start.** `FREYA_DECISION_V001` is
deterministic — fused evidence, measured margins, class distance where
it exists, declared database priors — and every threshold it uses is
labelled `PROVISIONAL_UNVALIDATED`. There are twelve verified
observations, one per material, which is not enough to train anything.
The model registry already knows how to refuse to activate a
replacement that is not better.

**The legacy sample reads INVALID_MEASUREMENT.** `s456` was measured
under the old conditions, and normalizing it against today's active
calibration puts 9 channels far outside 0–1 reflectance. That verdict
is correct, and all the DB1 and DB3 evidence is preserved beside it —
DB1 still says Pink Clay under its own frozen calibration, which is
what the original record concluded.

**`ST3215_POSITION_TOLERANCE` is unmeasured.** Stated again because it
is the one shipped constant that is a guess.

---

## 5. How to check the state yourself

```powershell
py firmware\Tests\run_all.py
```

Five suites, 707 checks, no hardware needed. `test_architecture.py`
alone is 312 checks and will tell you immediately if a boundary has
moved: it greps for MG995, for 8-slot assumptions, for a second driver,
for BD importing Science, for production importing research, and for a
second module importing pyserial.

```powershell
py firmware\tools\device.py status --port COM4
```

What is actually on the device, and whether it matches the manifest.
