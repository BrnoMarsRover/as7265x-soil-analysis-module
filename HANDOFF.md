# HANDOFF — Freya AS7265x science module

State as of **2026-08-27**, after the code-only master audit. Read
this first, then `Documentation/ARCHITECTURE.md`, then
`Documentation/OPERATIONS.md`. Sections 1-4 below are the 2026-08-19
rewrite record and still describe the architecture; sections 0.1 to
0.4 are what changed most recently.

This is the "where we are right now" note, written for whoever picks
the project up next — human or model — with no memory of how it got
here.

---

## 0.1 Master audit, 2026-08-27 (code only, hardware disconnected)

A full file-by-file audit ran with no hardware attached. What it found,
beyond confirming the day's UI, state-model, sensor-key and transport
work:

**Four regression suites existed but never ran.** `run_software.py`
carries a hand-written `SUITES` manifest and nothing checked it against
the directory, so `test_operator_ui`, `test_learning_ui`,
`test_damage_capture` and `test_damaged_motion` — 148 checks written
that same day — sat in `regression/` unregistered. The campaign
reported "40 suites passed" and was telling the truth about the forty
it knew about. All four are registered now, and `unregistered()` fails
the run if a suite file is ever added without being listed.

**A ledger entry forgot that a test had ever failed.** `Ledger.record`
overwrote unconditionally, so re-running until green erased the
evidence: `HW-B1-009` (one damaged frame in 200 requests) and
`HW-B2-004` both read as clean PASS. Failures now accumulate in
`failed_runs` / `ever_failed` beside the current status. `status_of` is
unchanged, so no gate opens differently — this is evidence, not control
flow. Both real failures have been backfilled from the run artifacts.

**A database comparing zero features called itself available.** That
was the mechanism behind the DB2 defect, and it was still unguarded
after the keys were fixed. `_reference_analysis` now reports a library
that shares no feature with the measurement as UNAVAILABLE with a
reason, instead of as a library with no opinion.

**The ST3215 pin orientation is confirmed, and `config.py` said
otherwise.** See section 3.

Smaller: `test_firmware_contract.py` resolved `firmware/` by counting
parent hops (the repo forbids this, and its own guard caught it);
Czech matrix words were unreachable as actually typed — `pisek`,
`puda`, `hlina` are now matched with their diacritics.

**Verified this pass, with no hardware:** every command the firmware
serves is classified mutating or read-only and no mutating command
retries; the sensor is proved live *before* the carousel moves, so a
dead sensor cannot strand a sample at the scanner; illumination is
killed from a `finally` on every acquisition path; prepared mixtures
DO have a consumer (`research/training/evaluate_mixtures.py`, which
runs and honestly reports `NO_MIXTURES`); `BD/` is byte-identical
before and after the campaign.

---

## 0.2 Operator-flow pass, 2026-08-27 (code only, hardware disconnected)

The master audit verified the operator screens by calling render
functions with status dictionaries the tests wrote themselves. Real use
then exposed two problems it had reported as VERIFIED. Both lived
BETWEEN the functions that were tested.

**`servo["selected"]` is a key no firmware has ever sent.**
`ServoLink.status()` sends `connected`. Four PC sites tested `selected`,
so every one of them took its false branch permanently:

* every screen showed `Servo: NOT SELECTED` over an answering ST3215;
* `print_servo_block` printed the label and then the literal word
  `None` — all servo telemetry (link, mode, encoder, voltage,
  temperature) was unreachable;
* Carousel Setup told every operator that no servo was connected — and
  that is the screen the operator is sent to in order to re-sync;
* the whole "servo ONLINE + carousel UNKNOWN means RE-SYNC, not
  reconnect" distinction was dead code.

It survived review because every fixture in the tree contained
`selected`, so reader and fixture agreed and neither had ever been
compared to a board.

**A failed measurement dumped the operator at the root screen.**
`menu_measure` printed a correct compact failure block and RETURNED;
the main loop's next iteration saw `position_valid == False` and drew
the context-free startup screen. The sample, the slot, the LOADED
state, the stage and the missing spectrum were gone one keypress after
they appeared. Nothing rendered anything wrong.

There is now a **MEASUREMENT RECOVERY** context that holds the operator
in the failed sample until they choose to leave, offers only safe
actions (status refresh, diagnostics, re-sync — never a movement
resend, because carousel moves are relative and may already have
happened), and returns them to the same sample once the position is
trustworthy again.

**Also found by driving the real loop:** `carousel["encoder"]` is
another dead key (the data is under `reference`), so System Status
printed `Origin: None counts` and never showed drift;
`geometry["counts_per_slot"]` does not exist, so the slot line ended
`None counts`; the multi-leg move record omitted three of the five
fields the move report prints, so a good one-slot move reported
`encoder: None -> 3072`; `step_ms` and the branch that read it were
unreachable leftovers of the removed timed servo backend.

**Learning history:** searching the material list was implemented as an
exact lookup, so typing `carbon` — the documented way to use the screen
— was answered with *"'carbon' is not a known material name, id or
alias"* and the bench material ranked BELOW a catalogue entry. Search
is now a first-class action, bench materials rank first, and a numbered
pick no longer asks for a second confirmation. Separately, ground truth
was keyed on the per-sample `measurement_id`, so the second sample ever
labelled was refused with an immutability error and the mixture the
operator had just weighed was silently not saved; observations are now
keyed `sample_id/measurement_id`.

**Test infrastructure:** `sandbox_mission` did not redirect the
learning store, so any test saving ground truth wrote into the real
`BD/training/decision_learning.sqlite3`. It does now.

`Tests/software/regression/test_operator_flow.py` drives
`screen.interactive()` itself with scripted keystrokes and asserts on
what the operator ends up looking at, including a guard that compares
every status key the screens read against a live board's own responses.

---

## 0.3 Pre-hardware freeze audit, 2026-08-27

A final code-only coherence pass before the bench session. Method:
drive the REAL firmware through all 25 commands plus error paths,
harvest **every key it can emit** (299), harvest the real Science
output, and diff both against every key the PC actually reads. That
found four more dead consumer keys of the same family as `selected`.

**The servo movement test printed nothing.** `report_move_test` gated
its whole output on `result["verified_movement"]`; the record says
`verified`. A real movement test printed `Movement complete.` and not
one leg, encoder reading, net travel, worst error or closing error.
This is the screen that characterises the actuator and investigates
H-002 — it was blank, and the only place the key existed was a fixture
that invented it.

**A failed acquisition blamed the carousel.** `carousel_outcome` read
`data["recovery"]`; the block is called `return_move`. The branch was
dead, so every acquisition failure fell through to the outbound
`motion` verdict — always MOVED — and the screen contradicted itself
two lines apart: `POSITION UNKNOWN … re-sync` above
`Returning Slot home............ PASS`, over a carousel the firmware
had brought home and still trusted. A sensor fault sent operators to
re-sync working hardware.

**Measurement quality was invisible.** `print_quality` read
`report["status"]` and `report["checks"]`; `split_quality` separates
the report into `hardware` and `normalization` and leaves no top-level
status. Every quality display printed `Overall: None` and listed
nothing — the reasons a measurement was degraded were computed on every
analysis and shown on none.

**Removed as proven-dead:** `print_cross_database` (161 lines) read the
*previous* pipeline's shape — no caller, and `calibration.py` already
documents that shape as gone. Its tests passed it `None`. Also the
unreachable timed-servo branch in the move report, and `CW`/`CCW`
imported into ESP32 `carousel.py` and never used (RAM on a deployed
`.mpy`). `print_result_block` was **kept**: it renders `legacy_analysis`
from migrated flat-schema records, which is a real compatibility job.

**Test isolation made structural.** The meta-audit's `real-BD-write`
rule only caught `SampleStore(` when the line also named a config path.
It now catches every writable store *including the no-argument form* —
`DecisionLearningStore()` is exactly how test code reached the real
`BD/training/decision_learning.sqlite3`. Proven to fire.

**Verified unchanged:** all 25 commands classified, no mutating command
retries; 190 files compile; no live 8-slot or 45-degree assumption in
production; eight scripted operator rehearsals show no `None`, no
traceback and no unhandled exception on any screen.

---

## 0.4 Final judgement pass, 2026-08-27 (evening)

Two findings, both at boundaries rather than inside a module.

**The raw encoder word was discarded, and H-002 needs it.**
`read_position` returns `decode_signed(word)`, and `decode_signed`
maps `0x8002` to `-2`. On a screen that is indistinguishable from a
genuine reading of `-2`, so no evidence could separate *"the encoder
really is near zero"* from *"the sign rule is reading a large value as
a small one"* — two hypotheses needing opposite investigations, in the
exact measurement H-002 turns on. The undecoded word is now kept
(`last_position_raw`, and `position_raw` in the feedback block), shown
in hex on the servo diagnostics screen, and carried in the preflight
evidence. It is **reported only** — asserted by a test that every
occurrence is an assignment or a report, never a decision.

**A closed link offered a retry that could never work.** `PORT_LOST`
closes the link and clears the serial handle; nothing in the menu loop
re-opens it. Every `Retry? [Y/n]` afterwards raised `PORT_CLOSED`
again — forever, with the prompt defaulting to Yes. An operator whose
USB cable fell out would press Enter, see the same line, and conclude
the client had hung. It now says the link cannot be re-opened, gives
the restart command, warns that re-opening resets the board (so the
carousel will need re-syncing), and exits 1. It deliberately does
**not** re-open automatically: opening the port resets the ESP32, and
that physical consequence must not follow from a keypress meaning
"try again".

Stopped there. Everything else examined — sandbox isolation, resume
semantics, evidence uniqueness, deployment identity, recovery context —
was already correct, and further churn the night before hardware costs
more than it returns.

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

**`firmware/Tests/hardware/artifacts/` is gitignored as well**, and it
is the ONLY record of what the hardware has ever done: every run's
`summary.json` plus `ledger.json`, which is what the layer gates read.
Git cannot restore any of it. Treat it exactly like `samples.json` —
copy it out before any bulk file operation. A `--dry-run` writes a new
run directory there but never touches the ledger.

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

The ST3215 ECHO_ONLY note is **closed**. On 2026-08-27 the bus scan
(`HW-B2-003`) probed eight baud rates in both pin orders and servo ID 1
answered at 1 Mbps with the pins **as configured** — TX GPIO17, RX
GPIO16, status byte `0x00`. The earlier "answers only with TX/RX
exchanged" reading was the ESP32 hearing its own transmission, and
`servo.release_uart_pins()` now prevents that loopback. Do not swap the
pin assignment.

---

## 4. What is not finished

**DB2 is populated but one material short.** 22 of DB1's 23 materials
were measured under WHITE, UV and IR on 2026-08-17 and are in DB2 with
54 features each. Sodium Bicarbonate was not measured and is absent
rather than zero. Rebuild with `py firmware/research/build_db2.py`.

**UV is weak on 8 of 18 channels.** The calibration was accepted with
that warning and the DB2 audit reproduces it independently: under UV the
white reference rises less than 5 counts above the dark there, so those
reflectances are quantised. This is why several materials come back
`NORMALIZATION_UNUSABLE` with 27 of 54 features unusable as reflectance
while all 54 are valid as raw counts. Fixing it means more UV current or
a longer integration, not more software.

**The Decision Model is a cold start.** `FREYA_DECISION_V001` is
deterministic — fused evidence, measured margins, class distance where
it exists, declared database priors — and every threshold it uses is
labelled `PROVISIONAL_UNVALIDATED`. There are 22 verified observations,
one per material, which is not enough to train anything: with a single
measurement per class no class has a measurable scatter. Scored against
those 22 it named 3 correctly and refused to name the rest. The model
registry already knows how to refuse to activate a replacement that is
not better.

**No prepared mixture has been measured yet.** The record for one now
exists end to end — multiple weighed components, a named matrix such as
ordinary soil, and the sample's distance, mass, packing and moisture —
and `research/training/evaluate_mixtures.py` scores unmixing against it.
Until mixtures are actually measured, no statement about "how much of
X is in this sample" is available from this instrument, and the report
says so rather than estimating one.

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

Twenty-one suites, no hardware needed and none possible: `run_all.py`
reaches `Tests/software/` only. `Tests/hardware/` is a separate
campaign with its own entry point, no default port, and a `--move`
flag on anything that turns the carousel.

`static/test_architecture.py` alone is 316 checks and will tell you
immediately if a boundary has moved: it greps for MG995, for 8-slot
assumptions, for a second driver, for BD importing Science, for
production importing research, and for a second module importing
pyserial. `static/test_static_api.py` beside it reads every name,
import and call site in the tree, which is what catches a method that
does not exist in a branch nothing has run yet.

The run finishes by re-hashing `firmware/BD/` and fails if a byte
moved.

```powershell
py firmware\tools\device.py status --port COM4
```

What is actually on the device, and whether it matches the manifest.
