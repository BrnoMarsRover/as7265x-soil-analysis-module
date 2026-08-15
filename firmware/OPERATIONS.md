# OPERATIONS

Practical runbook for the Freya AS7265x science module: install, run,
update, debug, recover.

This is the **only** operational document. `README.md` explains what the
project is and what the science means; the source code explains how the
implementation works; this file explains how to drive it.

> Environment: Windows, **PowerShell**. `&&` is a parser error in
> PowerShell — every command below is written to be run on its own line.
> `COM4` is used throughout; replace it if the board enumerates on
> another port.

---

## System architecture

```text
                    MAIN COMPUTER
                         │
              ┌──────────┴──────────┐
              │                     │
              ▼                     ▼
             PC                     BD
     Mission Controller      Science / Database
              │                     ▲
              │ raw spectrum        │
              └─────────────────────┘
              │
              │ USB / CP2102, newline JSON, 115200 baud
              ▼
            ESP32
      Hardware Controller
              │
        ┌─────┴─────┐
        ▼           ▼
      MG995       AS7265x
     Carousel      Sensor
```

The ESP32 moves hardware, reads hardware and reports hardware. It never
learns what a sample resembles.

**Carousel:** 4 slots, 90° apart. The scanner sits 180° - two slots -
from the loading hole:

```text
Loader Slot 1  ->  Scanner Slot 3
Loader Slot 2  ->  Scanner Slot 4
Loader Slot 3  ->  Scanner Slot 1
Loader Slot 4  ->  Scanner Slot 2
```

**Measure Sample** swings the slot out to the scanner, acquires, and
swings it back, so a successful measurement ends with the sample exactly
where it started.

---

## Directory responsibilities

```text
firmware/
├── ESP32/          uploaded to the board - and nothing else ever is
│   ├── boot.py
│   ├── main.py
│   ├── config.py
│   ├── as7265x.py
│   ├── mg995.py
│   └── carousel.py
│
├── BD/             science and science data, runs on the PC
│   ├── database.json      PROTECTED - 22 reference materials, READ ONLY
│   ├── references.json    PROTECTED - legacy White + Dark, READ ONLY
│   ├── samples.json       measured Sample archive, READ/WRITE
│   ├── calibrations/      full spectral calibrations, immutable
│   ├── config.py          calibration IDs, thresholds
│   ├── channels.py        the 18 channels and their wavelengths
│   ├── database.py        reference loading
│   ├── calibration.py     calibration build, validate, store, activate
│   ├── aggregation.py     repeat statistics and outlier handling
│   ├── quality.py         measurement quality control
│   ├── metrics.py         cosine, RMSE, Pearson, rank aggregation
│   └── sample_analysis.py validation, formulas, interpretation
│
├── PC/             mission control, runs on the PC
│   ├── rover_science_client.py   the application you start
│   ├── esp32_link.py             serial transport
│   ├── sample_store.py           reads and writes BD/samples.json
│   └── requirements.txt
│
└── OPERATIONS.md   this file
```

`ESP32_GENERIC-*.bin` in `firmware/` is the MicroPython runtime image.
It is flashed with `esptool`, never uploaded with `mpremote`.

---

## Prerequisites

```powershell
py -m pip install --upgrade mpremote pyserial
```

MicroPython must already be flashed on the ESP32. If it is not, flash the
image in `firmware/` with `esptool` first; everything below assumes a
board that boots to a MicroPython prompt.

---

## Project root

```powershell
cd C:\Users\Public\Documents\Altium\as7265x-soil-analysis-module
```

## ESP32 source directory

All `mpremote fs cp` commands below are run from here, so the paths have
no directory prefix and cannot accidentally pick up a BD or PC file.

```powershell
cd C:\Users\Public\Documents\Altium\as7265x-soil-analysis-module\firmware\ESP32
```

---

## Find the COM port

```powershell
py -m mpremote connect list
```

Look for the CP2102 / Silicon Labs entry. If several boards are attached,
unplug the others or match the serial number in the listing.

---

## Inspect the ESP32 filesystem

```powershell
py -m mpremote connect COM4 fs ls
```

```powershell
py -m mpremote connect COM4 fs ls :samples
```

The second command fails with `ENOENT` if the directory does not exist,
which on a clean board is the expected answer.

---

## Clean ESP32 installation

The current on-device project data is disposable — nothing scientific
lives on the board any more. A clean install is the preferred way to move
to this software version.

**1. Look first.**

```powershell
py -m mpremote connect COM4 fs ls
```

**2. Remove the obsolete project files.** These are from the old flat
layout and have no place in the new one. Run them one at a time; a file
that is already gone reports an error, which is harmless.

```powershell
py -m mpremote connect COM4 fs rm :sensor_diag.py
py -m mpremote connect COM4 fs rm :database.py
py -m mpremote connect COM4 fs rm :database.json
py -m mpremote connect COM4 fs rm :references.json
py -m mpremote connect COM4 fs rm :sample_analysis.py
py -m mpremote connect COM4 fs rm :sample_store.py
py -m mpremote connect COM4 fs rm :samples.json
py -m mpremote connect COM4 fs rm :samples.json.bak
py -m mpremote connect COM4 fs rm :wipe.py
```

**3. Remove the old sample directory**, including whatever records it
holds. One command, because the filenames are not known in advance:

```powershell
py -m mpremote connect COM4 exec "import os; [os.remove('samples/' + name) for name in os.listdir('samples')]; os.rmdir('samples')"
```

If `samples` does not exist this raises `OSError: [Errno 2] ENOENT`,
which again is the expected answer on a clean board.

> [!CAUTION]
> Remove **project** files only. Do not delete MicroPython system files,
> and do not run a blanket "erase everything" script — it would take the
> runtime with it.

**4. Upload the six runtime files** (see the next section).

**5. Reset and verify.**

```powershell
py -m mpremote connect COM4 fs ls
```

Exactly these six should remain:

```text
as7265x.py   boot.py   carousel.py   config.py   main.py   mg995.py
```

---

## Upload the ESP32 runtime

From `firmware\ESP32`:

```powershell
py -m mpremote connect COM4 fs cp boot.py :boot.py
py -m mpremote connect COM4 fs cp main.py :main.py
py -m mpremote connect COM4 fs cp config.py :config.py
py -m mpremote connect COM4 fs cp as7265x.py :as7265x.py
py -m mpremote connect COM4 fs cp mg995.py :mg995.py
py -m mpremote connect COM4 fs cp carousel.py :carousel.py
```

Then reset:

```powershell
py -m mpremote connect COM4 reset
```

That is the complete runtime. The list comes from the actual imports:
`main.py` imports `as7265x`, `carousel`, `config` and `mg995`;
`as7265x.py` and `mg995.py` import `config` and `machine`; `carousel.py`
imports `config` and `mg995`. Nothing else is reachable.

---

## Upload only what changed

During development, re-upload the changed file and reset — not the whole
set.

One file:

```powershell
py -m mpremote connect COM4 fs cp as7265x.py :as7265x.py
```

```powershell
py -m mpremote connect COM4 reset
```

Several files:

```powershell
py -m mpremote connect COM4 fs cp main.py :main.py
```

```powershell
py -m mpremote connect COM4 fs cp as7265x.py :as7265x.py
```

```powershell
py -m mpremote connect COM4 fs cp config.py :config.py
```

```powershell
py -m mpremote connect COM4 reset
```

---

## Reset the ESP32

```powershell
py -m mpremote connect COM4 reset
```

A reset clears all runtime state: the carousel position becomes unknown,
physical slot occupancy is forgotten, and the retained raw acquisitions
are gone. Re-run the Initial Carousel Calibration afterwards. Saved
Samples are on the PC and are unaffected.

> [!TIP]
> If a measurement was taken but this application lost the result, run
> **Tools → Sync ESP32 Samples to PC** *before* resetting the board. The
> acquisition buffer is RAM only and does not survive a reset.

---

## Open the MicroPython REPL

```powershell
py -m mpremote connect COM4 repl
```

Leave with `Ctrl+]`.

Useful checks inside the REPL:

```python
import os
os.listdir()
```

```python
import main
```

`import main` loads the module **without** starting the command server
(the server only runs when MicroPython executes `main.py` at boot), so it
is a safe way to surface an import error.

To exercise the sensor by hand:

```python
import as7265x
runtime = as7265x.SensorRuntime()
runtime.ensure_ready()
runtime.settings()
```

---

## Diagnose "main.py did not start"

Symptom: the PC application prints that the module did not answer a ping,
and mentions non-JSON output. If what came back looks like

```text
>>>
Traceback (most recent call last):
{'request_id': '1', 'cmd': 'ping'}
```

then the board is sitting at the REPL and `main.py` is **not** running.
The REPL echoes the command back and prints it as a Python dict — that is
the fingerprint.

Do not blame USB or I2C yet. Check what is actually on the board:

```powershell
py -m mpremote connect COM4 fs ls
```

Then read the real error:

```powershell
py -m mpremote connect COM4 repl
```

```python
import main
```

The usual cause is a missing module: all six runtime files must be
present. Upload the missing one and reset.

---

## Install the PC dependencies

```powershell
cd C:\Users\Public\Documents\Altium\as7265x-soil-analysis-module\firmware\PC
```

```powershell
py -m pip install -r requirements.txt
```

## Run the PC application

```powershell
cd C:\Users\Public\Documents\Altium\as7265x-soil-analysis-module\firmware\PC
```

```powershell
py rover_science_client.py --port COM4
```

The application locates and imports BD by itself, relative to its own
file. No `PYTHONPATH` setup is required, and it does not matter which
directory you start it from.

Useful flags:

```powershell
py rover_science_client.py --port COM4 --verbose
```

```powershell
py rover_science_client.py --port COM4 --command get_status
```

`--command` runs one **hardware** command and prints the raw JSON. It
does not run analysis and does not save anything.

---

## Where the data lives

| What | Where | Access |
|---|---|---|
| Fixed White and Dark | `firmware/BD/references.json` | **read only** |
| Reference material database (22 materials) | `firmware/BD/database.json` | **read only** |
| Measured Sample archive | `firmware/BD/samples.json` | read/write |
| Full spectral calibrations | `firmware/BD/calibrations/` | append only |
| Active calibration pointer | `firmware/BD/calibrations/active.json` | rewritten on activation |
| Analysis thresholds and calibration ID | `firmware/BD/config.py` | |
| Hardware settings (pins, gain, servo timing) | `firmware/ESP32/config.py` | |

All three data files sit together in `firmware/BD`, but only
`samples.json` is ever written. The other two are opened read-only and
have no save path in the code at all.

`samples.json` holds **complete** records - all three 18-channel spectra,
the White and Dark actually used for that sample, the comparison against
every material, the sensor settings, timestamps and metadata - so a
measurement can be re-derived from the archive alone. Writes go to a
temporary file and are moved into place with an atomic rename, so an
interrupted write cannot corrupt the archive.

An archive from the retired `firmware/PC/data/` layout is picked up and
migrated automatically on first start; the old files are left where they
are. Old short records stay readable and are listed correctly.

---

## Back up the measured Samples

Measured Samples are the run's scientific output and the only data here
that cannot be regenerated. Copy the archive:

```powershell
cd C:\Users\Public\Documents\Altium\as7265x-soil-analysis-module\firmware\BD
```

```powershell
Copy-Item samples.json ("samples_backup_" + (Get-Date -Format yyyyMMdd_HHmmss) + ".json")
```

Do this after every competition run. Never copy over `database.json` or
`references.json` while doing it.

---

## The two calibrations

This is the single most important thing to understand about the science
data.

```text
LEGACY   BD/references.json, id FREYA_COMPETITION_2026_CAL_V1
         The White/Dark that database.json was normalized against.
         IMMUTABLE. The ONLY calibration ever used to compare a
         measurement against the material library.

ACTIVE   BD/calibrations/FREYA_FULL_SPECTRAL_CAL_<stamp>.json
         A full Dark + WHITE/UV/IR calibration you create. Used for the
         scientific record, the 54-feature dataset and quality control.
```

Every new measurement is normalized **both** ways:

```text
RAW WHITE spectrum
        ├── ACTIVE calibration  -> the scientific record, QC, UV/IR
        └── LEGACY calibration  -> comparison with database.json
```

That is what lets you recalibrate the instrument **without remeasuring
the material library**. A new calibration never touches
`references.json`, `database.json` or the legacy ID.

The material library has 18 WHITE-illumination features per material, so
only the WHITE spectrum is compared against it. UV and IR are recorded
as auxiliary data and quality information until matching reference data
exists — they are never artificially compared with the legacy library.

---

## Acquisition

Every spectrum, without exception, follows this sequence:

```text
ALL BULBS OFF
  -> select illumination
  -> set the configured bulb current
  -> enable the bulb
  -> wait for the illumination to settle
  -> trigger the MODE 3 one-shot conversion
  -> wait DATA_READY
  -> read all 18 calibrated channels
  -> ALL BULBS OFF        (from a finally block)
```

**Mode 3, one-shot.** Continuous mode free-runs, so a read can return a
conversion that started before the illumination changed — the spectrum
and the lamp that produced it were not guaranteed to belong together.
One-shot arms the conversion *after* the lamp is on and settled.

**Three lamps**, each owned by one internal device:

```text
WHITE -> device 0x00      UV -> device 0x02      IR -> device 0x01
```

A normal measurement takes all three, repeated:

```text
18 channels x 3 illuminations = 54 spectral features
```

Repeats are configurable and independent — `SAMPLE_REPEATS` (3) for a
rover measurement, `CALIBRATION_REPEATS` (10) for a calibration. The
ESP32 returns every individual reading; the PC does the statistics.

---

## Sensor Test / Calibration

```text
Run the application
  -> [t] Tools / Records
  -> [5] Sensor Test / Calibration
```

Every screen shows the state of both calibrations first, so you always
know which one a result was produced under.

```text
============================================================
 SENSOR TEST / CALIBRATION
============================================================

Calibration:
  Active full calibration: PASS / MISSING / INVALID
  Legacy DB calibration:   PASS / MISSING / INVALID

[1] Full Sensor + Analysis Test
    Run the complete production measurement pipeline
    without saving a Sample.

[2] LED / Illumination Test
    Test WHITE, UV and IR illumination independently.

[3] Full Spectral Calibration
    Create a new complete Dark + WHITE/UV/IR calibration.

[4] Show Active Calibration
[5] Validate Active Calibration
[6] Calibration History

[0] Back
```

If no full calibration is active it says so and points at `[3]` rather
than quietly falling back.

### [1] Full Sensor + Analysis Test

The **same production path** a real measurement uses - `acquire_triad`
on the device, dual normalization, quality control, then the ranked
comparison. There is no separate test pipeline. It moves nothing and
saves nothing.

```text
HARDWARE           I2C 0x49, slave devices, mode, gain, integration,
                   LED currents, all lamps off
ACQUISITION        WHITE / UV / IR repeats
MEASUREMENT QUALITY  PASS / WARNING / FAIL with reasons
FULL SPECTRAL DATA   54 channels, raw and reflectance
LEGACY DB COMPARISON every material: combined / cosine / RMSE / Pearson
RESULT             best match, agreement, conclusion

TEST ONLY - NOTHING SAVED
```

### [2] LED / Illumination Test

Each lamp on its own, with the enable bit **read back** at every step:

```text
WHITE LED:  PASS   25mA
UV LED:     PASS   25mA
IR LED:     PASS   25mA

ALL LEDs OFF: PASS
```

A lamp is never left on: shutdown happens in a `finally` block.

### [3] Full Spectral Calibration

Creates a new calibration file. It does **not** modify
`references.json`, `database.json` or the legacy calibration, and it
does not become active until you confirm.

**Step 1/2 - DARK.** Remove the sample, close the optical path, make
sure no light reaches the target. All three lamps stay **off** - dark is
the detector's own response with no illumination at all. There is no
such thing as a "UV dark".

```text
Dark acquisition 1/10 ... 10/10
```

The Dark is validated before anything else. **If it fails, calibration
stops there** - a bad Dark corrupts every reflectance computed against
it.

**Step 2/2 - WHITE TARGET.** Install the diffuse white reference in the
exact sample position and do not move the sensor. Three datasets are
then taken automatically against the same target:

```text
WHITE illumination 1/10 ... 10/10     -> 18 channels
UV    illumination 1/10 ... 10/10     -> 18 channels
IR    illumination 1/10 ... 10/10     -> 18 channels
                                       = 54 white-reference values
```

Then validation, and only then:

```text
Activate this calibration? [y/N]
```

Only `y` activates. A calibration that fails validation cannot be
activated at all; you may keep it on disk as engineering data, clearly
marked invalid.

### [4] [5] [6] Inspection

`[4]` shows the active calibration and, separately and unmistakably, the
protected legacy one. `[5]` re-runs the checks against the active
calibration and writes nothing. `[6]` lists every calibration with its
status; the legacy entry is `PROTECTED` and cannot be deleted from the
interface.

Calibration files are immutable - one file per calibration, never
overwritten, never deleted here. Only the active pointer changes.

---

## Measurement quality control

Quality runs **before** any classification, and a FAIL means no
identification is reported at all - only the stored spectra.

| Check | Catches |
|---|---|
| illumination | White-Dark too small to divide by |
| validity | missing or non-finite channels |
| reflectance | R far above 1 or below 0 |
| triad boundary | a step at F535->G560 or L705->R730 |
| repeatability | acquisitions that did not repeat |
| distance | optional VL53L4CD gate, if a reading exists |

Nothing is clamped or repaired. Channels that cannot be trusted are
named and excluded from the comparison; everything else is reported.

Every threshold lives in `firmware/BD/config.py` with a comment
explaining what it is for.

---

## Reference comparison

Three metrics, for every material:

| Metric | Measures | Better |
|---|---|---|
| Cosine | shape, ignores brightness | higher |
| RMSE | absolute reflectance error | lower |
| Pearson r | correlation about each mean | higher |

Cosine alone was the problem: reflectance is non-negative, so nearly
every material scores 95-100%, and a dim spectrum and a bright one of
the same shape are a *perfect* 100% match to it. RMSE keeps the
magnitude cosine throws away.

The three **ranks** are combined - ranks, not scores, because the
metrics are on incomparable scales and a weighted blend of them would be
an invented confidence. When they point at different materials the
result is `METRICS_DISAGREE`, shown rather than hidden.

None of these numbers is a probability, a certainty or a composition.
The library holds pure materials and cannot decompose a mixture; every
conclusion says so.

---

## Clear physical slots

```text
[t] Tools / Records  →  [6] Clear Physical Slot
```

Enter a slot number to free one slot, or **`a`** to free all four at
once. Clearing ALL asks for a typed `YES`; anything else cancels, and it
says so immediately if every slot is already empty.

This is **carousel occupancy only**. It does not delete a single Sample
record - not on the PC, not on the ESP32 - and it does not move the
carousel or invalidate the tracked position.

```text
Clear Physical Slot     frees the mechanism      records KEPT
Delete Sample           removes a PC record      one Sample
Delete ALL ESP32        removes device records   PC archive KEPT
```

---

## ESP32 Sample database

The ESP32 keeps the last raw acquisition per slot in RAM. Two explicit
operations manage it, both under **Tools → Sample Database**:

```text
[6] Import ALL Samples from ESP32
[7] Delete ALL Samples from ESP32
```

They are deliberately **not** chained. The safe workflow is:

```text
Import ALL   →   verify in the Sample Database   →   Delete ALL
```

**Import ALL** is a copy, never a move:

| Situation | Result |
|---|---|
| ID not on the PC | **IMPORT** |
| ID on the PC, same spectrum | **SKIP** |
| ID on the PC, different data | **CONFLICT** - nothing overwritten |

Running it twice imports nothing the second time, and the device keeps
its records either way.

**Delete ALL** asks a plain `[y/N]` - `y` or `yes` deletes, anything
else (including a bare Enter) cancels. It lists which records are not yet
on the PC before asking, so nothing unexported is destroyed by accident.

Afterwards it **reads the device back** rather than trusting the reply.
Only a genuinely empty device gives:

```text
ESP32 Sample storage is now empty.
```

If records survive the delete it prints `DELETE VERIFICATION FAILED` and
names what is still there.

It removes device records **only** - PC Samples, physical slot occupancy,
`BD/database.json` and `BD/references.json` are all untouched.

> [!CAUTION]
> The device buffer is RAM only. A reset empties it, so import before
> you reset the board.

---

## Sync ESP32 Samples to PC

The ESP32 keeps the last raw acquisition per slot in RAM. If this
application crashed, was restarted, or was replaced by a different
laptop after a measurement, that acquisition can still be recovered:

```text
Run the application
  → [t] Tools / Records
  → [7] Sync ESP32 Samples to PC
```

```text
Connecting to ESP32............. PASS
Reading Sample index............ PASS

ESP32 Samples: 4

S001        already exists      SKIP
S002        already exists      SKIP
S003        transferred         PASS
S004        transferred         PASS

Transferred: 2
Skipped:     2
Failed:      0
```

Behaviour, deliberately conservative:

- a Sample ID the PC already has is **skipped**, never overwritten;
- the ESP32 copy is **kept** - this is a copy, not a move;
- running it twice transfers nothing the second time;
- it writes only to the PC Sample archive. `BD/database.json` and
  `BD/references.json` are never opened for writing anywhere in this
  program.

The buffer is RAM only and is empty after a reset, so sync before you
reset the board.

---

## Servo calibration

The carousel is open-loop: no encoder, no Hall sensor, no feedback. No
calculation can guarantee an exact angle — only calibration can make the
movement **repeatable**. Precision is prioritised over speed throughout,
and the shipped pulses sit close to neutral on purpose.

```text
[t] Tools / Records  →  [4] Servo / Carousel Test  →  [5] Calibration
```

Values are edited **in RAM on the ESP32**, so you can converge without an
upload-and-reset cycle per attempt. They are **not** persistent: press
`c` to print the block, paste it into `firmware/ESP32/config.py`, and
upload that file.

### Calibration order

Do these in order. Nothing downstream means anything until neutral is
right.

**1. True neutral.** `[1] Test STOP / neutral`, hold for 5–10 s and
watch. Any creep at all means the pulse is wrong. Trim with
`[6] Fine-adjust neutral pulse` in 1–2 µs steps: if it crept clockwise,
lower it; counter-clockwise, raise it. It is often *not* exactly 1500.

**2. Minimum reliable CW pulse.** With `[7]`, walk the offset up from
neutral — `+10`, `+15`, `+20`, … — testing with `[2] Test 90 deg CW`
each time. Take the smallest offset that starts every single time and
never stalls, then add a small margin.

**3. Minimum reliable CCW pulse.** Repeat with `[8]` and
`[3] Test 90 deg CCW`. **Do not mirror the CW value** — a continuous
servo is not symmetric, and CW may need +43 µs where CCW needs −51 µs.

**4. Measure the angular speed.** Run `[2] Test 90 deg CW` once and note
how far it actually went. The calibration screen shows the implied speed
(`deg/s`) for every profile, so you can sanity-check the four timings
against each other — the two 180° figures should land near the matching
90° one.

**5. 90° CW timing — let the arithmetic do the work.** Mark a physical
reference, run `[2]`, then type the **observed angular error** when it
asks. The screen re-times the move for you:

```text
Error [deg]: +4

  Target:        90.00 deg
  Actual:        94.00 deg
  old duration:  1200 ms
  new duration:  1149 ms

Apply new duration? [y/N]:
```

The formula is `t_new = t_old × target / actual`, which is exact while
the speed is roughly constant, so this converges in two or three
iterations instead of drifting around by guesswork. Repeat until the
error stops shrinking.

**6. 90° CCW timing.** Same with `[3]`, independently. Do **not** copy
the CW value.

**7. Full-turn drift check.** `[2]` with **repeat 4**: 4 × 90° should
come back to the starting orientation. Then `[3]` with repeat 4. The
error here is four times the per-step error, which makes small biases
visible that a single move hides. Enter the accumulated error and let
the screen re-time it.

**8. 180° LOAD→SCAN.** `[4]`, adjusted independently with the same
error-entry loop. **Never** set it to 2 × the 90° time — a continuous
servo pays its acceleration ramp once per move, so one long sweep and
two short ones do not cover the same angle.

**9. 180° SCAN→LOAD.** `[5]`, independently again.

**10. Out-and-back — the one that matters most.** `[o]` runs
LOAD→SCAN then SCAN→LOAD, several cycles, and each cycle should end
exactly where it started. This is the movement Measure Sample performs,
so optimise for the smallest error **after the pair**, not for either
half on its own. If the carousel drifts clockwise, shorten LOAD→SCAN or
lengthen SCAN→LOAD; counter-clockwise, the reverse.

**11. Repeatability, not one lucky move.** Run each of the four
profiles about five times and note the errors. What matters is the
largest error you see, not the average of a good run.

**12. Settle.** Raise `Settle` under `[9]` until the mechanism has
stopped ringing before the next action. Speed is not important here.

**13. Verify.** Run a normal Measure Sample and confirm the sample ends
at the loading position it started from.

### If the carousel sometimes fails to start

A pulse chosen close to the deadband for precision may not always break
static friction. `Start kick` under `[9]` runs a brief stronger pulse at
the beginning of every move; it ships **disabled**. The kick comes out
of the commanded duration, so re-calibrate the timings after enabling
it.

### If it consistently overshoots

Two options, both **disabled** by default, both to be judged by
measurement rather than assumption:

- `Slow approach` — the last N ms at a pulse closer to neutral, so the
  carousel coasts in.
- `Reverse brake` — a brief opposite-direction burst at the end.

Active braking is **not** automatically better than simply stopping at
neutral. Keep either one only if repeated measurements show a smaller
error than without it.

### Optional slow approach

`Slow approach` runs the last N ms of every move at a pulse closer to
neutral, so the carousel coasts in rather than stopping dead. It ships
**disabled (0 ms)**. Enable it only if testing shows consistent
overshoot — a well-calibrated single slow pulse beats an untested motion
profile.

### After calibration

```powershell
py -m mpremote connect COM4 fs cp config.py :config.py
```

```powershell
py -m mpremote connect COM4 reset
```

Then re-run the Initial Carousel Calibration, because a reset forgets
the position.

> [!NOTE]
> Even perfect calibration cannot guarantee absolute positioning
> indefinitely: servo speed drifts with supply voltage, load,
> temperature and friction. **Fine Carousel Alignment** and **Re-sync
> Carousel** remain necessary recovery tools.

---

## Competition startup checklist

```text
1.  Connect the ESP32 USB cable.
2.  Confirm the COM port:   py -m mpremote connect list
3.  Start the PC application.
4.  Confirm "Connection: ONLINE".
5.  Tools -> System Status: sensor READY, mode 3, 100 cycles, 16x,
    legacy cal PASS, active cal PASS, material DB READY.
5a. If the active calibration is MISSING, run
    Tools -> Sensor Test -> Full Spectral Calibration first.
6.  Initial Carousel Calibration.
7.  Align physical Slot 1 under the loading hole.
8.  Confirm the current position as Slot 1.
9.  Choose Slot.
10. Prepare Sample (Sample ID + metadata).
11. Rover arm deposits soil.
12. Confirm Sample Loaded.
13. Measure Sample (180 deg out, acquire, 180 deg back).
14. Verify the Sample was saved (Tools -> Sample Database).
15. Continue with the next slot (4 slots total).
```

---

## Development workflow

```text
1. Edit local code.
2. git status
3. git diff
4. Run the test suite.
5. If an ESP32 file changed, upload only that file.
6. Reset the ESP32 if an ESP32 file changed.
7. Start the PC application and test the affected functionality.
8. Do NOT commit or push automatically.
```

Run the tests from `tests\`:

```powershell
cd C:\Users\Public\Documents\Altium\as7265x-soil-analysis-module\tests
```

```powershell
py test_esp32.py
```

```powershell
py test_science.py
```

```powershell
py test_calibration.py
```

```powershell
py test_pc.py
```

```powershell
py test_integration.py
```

They run the real firmware on CPython against a fake AS7265x, the real
science layer against the real protected data (read-only), and the real
client against a loopback that runs the real firmware dispatcher. No
board is needed.

Check what changed:

```powershell
git status --short firmware/ESP32
```

```powershell
git diff -- firmware/ESP32
```

Always confirm the protected hardware design is untouched:

```powershell
git diff -- Hardware
```

```powershell
git status --short Hardware
```

The Altium project lives inside `Hardware/`, so that one command covers
it. To check it on its own:

```powershell
git diff -- Hardware/as7265x-soil-analysis-module.PrjPcb
```

---

## Common failure: COM port busy

```text
access denied
port busy
could not open COM4
```

Only one process may own the port. Close, in this order:

```text
the PC client
any mpremote REPL session
any IDE serial monitor
any other terminal holding the port
```

Then retry. If the port is still held, unplug and replug the board.

---

## Common failure: sensor unavailable

First, use the normal path:

```text
Tools / Records -> Sensor Test
```

The runtime attempts recovery by itself — a boot failure is never
permanent, and every sensor command re-initializes from scratch if
needed. A sensor that was not powered when the board booted will come
back on the next Sensor Test or measurement.

Expected hardware:

```text
AS7265x address   0x49
I2C bus           0
SDA               GPIO21
SCL               GPIO22
Frequency         100 kHz
```

Read the reported error code:

| Code | Meaning |
|---|---|
| `I2C_NO_DEVICES` | nothing answers on the bus: wiring, pull-ups, or sensor 3V3 |
| `AS7265X_ADDRESS_NOT_FOUND` | the bus works but 0x49 is absent: wrong device or wrong address |
| `AS7265X_SLAVES_NOT_DETECTED` | the master answers but the VIS/NIR slaves do not: fault on the sensor module itself |
| `SENSOR_CONFIG_NOT_APPLIED` | the sensor accepted the address but not the settings |
| `SENSOR_DATA_READY_TIMEOUT` | conversion never completed |

Do not add a second diagnostics subsystem. Sensor Test already walks the
whole path through the production code.

---

## Common failure: sensor configuration mismatch

Expected:

```text
measurement mode   = 3 (one-shot)
integration cycles = 100
gain               = 16x
LED current        = 25 mA on WHITE, UV and IR
```

If System Status or Sensor Test reports anything else, the reported
values are being read back from the sensor's registers, so the sensor is
genuinely running the wrong settings. Fix
`firmware/ESP32/config.py`, re-upload it, and reset:

```powershell
py -m mpremote connect COM4 fs cp config.py :config.py
```

```powershell
py -m mpremote connect COM4 reset
```

> [!CAUTION]
> Never regenerate or re-measure White/Dark to compensate for a
> configuration bug. That bakes the bug into the calibration and
> invalidates every previous measurement.

---

## Files that are NEVER uploaded to the ESP32

```text
firmware/BD/*
firmware/PC/*
firmware/BD/database.json
firmware/BD/references.json
firmware/BD/samples.json
tests/*
firmware/OPERATIONS.md
README.md
HANDOFF.md
Hardware/*
```

Only the six files in `firmware/ESP32/` belong on the board. The ESP32
does not need `database.json` or `references.json` for anything — it
performs no science.

---

## Files that must NEVER be modified

**Protected scientific data.** Read-only during normal operation; never
regenerated, reformatted, re-sorted or rewritten as part of any startup
or deployment step:

```text
firmware/BD/database.json     the reference material database
firmware/BD/references.json   the fixed competition White and Dark
```

**Protected hardware design.** Software work never touches these:

```text
Hardware/
Hardware/as7265x-soil-analysis-module.PrjPcb
```

The Altium project lives inside `Hardware/`. An older commit still holds
a root-level copy of it - do not restore that; it is obsolete.

Tests operate on temporary copies and verify the SHA256 of both protected
files before and after every run.

---

## Quick reference

```text
QUICK REFERENCE
===============

Project:
C:\Users\Public\Documents\Altium\as7265x-soil-analysis-module

ESP32 source:   firmware\ESP32
Science / BD:   firmware\BD
PC application: firmware\PC
Saved Samples:  firmware\BD\samples.json
Calibrations:   firmware\BD\calibrations

List devices:
py -m mpremote connect list

List ESP32 files:
py -m mpremote connect COM4 fs ls

Upload the runtime (from firmware\ESP32):
py -m mpremote connect COM4 fs cp boot.py :boot.py
py -m mpremote connect COM4 fs cp main.py :main.py
py -m mpremote connect COM4 fs cp config.py :config.py
py -m mpremote connect COM4 fs cp as7265x.py :as7265x.py
py -m mpremote connect COM4 fs cp mg995.py :mg995.py
py -m mpremote connect COM4 fs cp carousel.py :carousel.py

Reset:
py -m mpremote connect COM4 reset

REPL:
py -m mpremote connect COM4 repl

Run the application (from firmware\PC):
py rover_science_client.py --port COM4

Back up Samples (from firmware\BD):
Copy-Item samples.json samples_backup.json

Run the tests (from tests):
py test_esp32.py
py test_science.py
py test_calibration.py
py test_pc.py
py test_integration.py
```
