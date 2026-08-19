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
        ┌─────┴──────────────────────┐
        │                            │ I2C
        │                            ▼
        │                         AS7265x
        │                          Sensor
        ▼
   ServoManager  - the operator connects the servo before anything moves
        │
        ▼
   UART2: GPIO17 TX
          GPIO16 RX
          GND
        │
        ▼
   Waveshare Serial Bus
    Servo Driver Board   <-- EXTERNAL SERVO PSU
        │ servo bus
        ▼
    ST3215 Servo
   encoder, closed loop
        │
        ▼
     Carousel
```

**One actuator, and it is connected explicitly.** After every boot the
servo is disconnected and carousel movement is blocked. The operator
connects it at `[0] Carousel Setup`.

| ST3215 | |
|---|---|
| Interface | UART2 serial bus, 1 Mbps |
| Positioning | encoder counts |
| Feedback | 4096-count absolute encoder |
| Movement | commanded and **verified** |
| Telemetry | voltage, temperature, load, current |
| Servo power | **external supply** |
| ID range | 0-253 (factory default 1) |

> [!IMPORTANT]
> The ST3215 servo subsystem is **externally powered**. The Freya ESP32
> science PCB provides only UART TX, UART RX and a common ground reference
> to the Waveshare serial bus servo driver. **No servo supply current is
> drawn from the ESP32 PCB.**

> [!NOTE]
> Connecting is deliberately explicit rather than automatic. Until the
> UART is open and the servo has answered, the firmware refuses to move
> the carousel at all: a carousel that turns without feedback is a
> carousel with no idea where it is.

The ESP32 moves hardware, reads hardware and reports hardware. It never
learns what a sample resembles.

**Carousel:** 4 slots, 90° apart. One slot is 90°, the loader↔scanner
sweep is 180° — 1024 and 2048 encoder counts respectively, each movement
commanded in counts and then verified by reading the encoder back.

The scanner sits 180° - two slots - from the loading hole:

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

Six runtime layers, one responsibility each (ARCHITECTURE.md). Only the
first is uploaded to the board.

```text
firmware/
├── ESP32/          acquire - THE ONLY CODE UPLOADED TO THE BOARD
│   ├── boot.py  main.py  config.py
│   ├── drivers/    as7265x, st3215, st3215_registers, servo_base
│   ├── control/    carousel, servo_manager
│   └── protocol/   transport, router, servo/carousel/sensor/sample
│                   commands
│
├── PC/             orchestrate - operator UI and serial transport
│   ├── rover_science_client.py    the application you start
│   ├── esp32_link.py              serial transport
│   └── requirements.txt
│
├── BD/             remember - data and access to it
│   ├── config.py                where every data file lives
│   ├── channels.py              18 channels + the two feature spaces
│   ├── databases.py             loading a material database
│   ├── registry.py              DB1/DB2/DB3 as independent sources
│   ├── calibrations.py          calibration library and active pointer
│   ├── samples.py               the measured-sample archive
│   ├── taxonomy.py              material identity, read from DB1/DB3
│   ├── acquisition_profiles.py  HOW a measurement was made
│   ├── decision_learning.py     SQLite history of what was concluded
│   └── data/
│       ├── DB1.json                 measured here, 18 bands, 23 materials
│       ├── DB1_source.txt           verbatim record DB1 is built from
│       ├── DB2.json                 measured here, 54 features    EMPTY
│       ├── DB3.json                 USGS, projected, 84 materials READY
│       ├── calibration_legacy.json  the White/Dark DB1 was measured against
│       ├── calibrations.json        every calibration + the active one
│       ├── acquisition_profiles.json
│       ├── operator_aliases.json    bench names for known materials
│       ├── samples.json             samples measured during a run
│       └── decision_learning/       seed + decision_learning.sqlite3
│
├── Measurements/   understand - deterministic maths, no conclusions
│   ├── config.py              thresholds and metric settings
│   ├── preprocessing.py       dark, normalize, unit vector, SNV
│   ├── analysis.py            dark correction and normalization
│   ├── calibration.py         build and validate a full calibration
│   ├── aggregation.py         repeat statistics and outlier handling
│   ├── quality.py             hardware QC vs normalization QC
│   ├── channel_reliability.py per illumination x channel verdicts
│   ├── spectral_features.py   derivatives, block energies, ratios
│   ├── metrics.py             evidence families
│   ├── distances.py           six metrics, winners and margins
│   ├── class_distance.py      centroid, standardized, Mahalanobis, kNN
│   ├── mixture.py             NNLS unmixing, spectral contributions
│   ├── inference.py           per-database analysis and consensus
│   └── evidence.py            the versioned EvidencePackage
│
├── DecisionModel/  interpret - one measurement -> one Decision
│   ├── engine.py              the decision hierarchy
│   ├── evidence_fusion.py     magnitude-aware fusion
│   ├── hierarchy.py           family before material
│   ├── unknown_detection.py   out-of-distribution
│   ├── reliability.py         per-database, per-class reliability
│   ├── class_models.py        distributions from verified observations
│   ├── explainability.py      sentences built only from evidence
│   ├── calibration_transfer.py staged, off by default
│   ├── model_registry.py      one ACTIVE model
│   └── training/              import, datasets, group-aware CV
│
├── Science/        the mission - ERC Scientific Exploration
│   ├── config.py       paths and the official ERC limits
│   ├── mars_yard.py    34 surveyed objects, source-grounded
│   ├── requirements.py the 19 O/SCI requirements
│   ├── plan.py         map units, frozen hypothesis, predictions
│   ├── sites.py        the four-site planner
│   ├── run.py          the mission record
│   ├── analysis.py     prediction and hypothesis verdicts
│   ├── checker.py      automated O/SCI checks, source manifest
│   ├── report.py       ERC limit validators, science package
│   ├── mapping.py      annotated SVG maps
│   ├── prerun.py       pre-run check and configuration lock
│   └── data/           map points, requirements, plan, AI polish prompt
│
├── Tests/          verify - runs without a board
└── research/       investigate
    ├── build_db1.py               rebuild DB1 from its source
    ├── import_usgs.py             import external spectra
    ├── spectral_projection.py     project them to our bands
    └── analyse_discriminability.py  leave-one-out class separability
```

`ESP32_GENERIC-*.bin` in `ESP32/` is the MicroPython runtime image.
It is flashed with `esptool`, never uploaded with `mpremote`.

> [!IMPORTANT]
> **The dependency direction is one-way and enforced.**
> `Science -> DecisionModel -> Measurements -> BD`, never the reverse.
> Persistence may not depend on mathematics, and mathematics may not
> depend on the mission - otherwise a competition deadline could reach
> into a scientific result. `Tests/test_architecture.py` enforces every
> edge by reading the import statements.

---

## Prerequisites

```powershell
py -m pip install --upgrade mpremote pyserial
```

MicroPython must already be flashed on the ESP32. If it is not, flash the
image in `ESP32/` with `esptool` first; everything below assumes a
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

## The complete device manifest

**Exactly these 18 files go on the board. Nothing else.** This list is the
authority; every upload command below is derived from it.

| On the device | What it is |
|---|---|
| `boot.py` | runs before `main.py` |
| `config.py` | pins, geometry, ST3215 settings |
| `main.py` | builds the subsystems, serves the protocol |
| `drivers/__init__.py` | package marker |
| `drivers/as7265x.py` | I2C spectral sensor |
| `drivers/servo_base.py` | rotation vocabulary, `ServoError` |
| `drivers/st3215.py` | ST3215 UART driver |
| `drivers/st3215_registers.py` | ST3215 wire protocol as data |
| `control/__init__.py` | package marker |
| `control/carousel.py` | slot geometry and planning |
| `control/servo_manager.py` | servo lifecycle, the movement gate |
| `protocol/__init__.py` | package marker |
| `protocol/transport.py` | framing over USB serial |
| `protocol/router.py` | command dispatch |
| `protocol/servo_commands.py` | servo command surface |
| `protocol/carousel_commands.py` | carousel command surface |
| `protocol/sensor_commands.py` | sensor command surface |
| `protocol/sample_commands.py` | on-device sample records |

3 root files + 5 drivers + 3 control + 7 protocol = **18**.

Every path on the device mirrors its path under `firmware/ESP32/`
exactly, so `drivers/st3215.py` on the board comes from
`firmware/ESP32/drivers/st3215.py` and nowhere else.

The list is not a preference - it is what the imports actually reach.
`main.py` imports `control` and `protocol`; `protocol` imports `control`;
`control` imports `drivers`; `drivers` imports only `config` and
`machine`. Nothing else is reachable, and the direction is enforced by
`Tests/test_architecture.py`.

> [!NOTE]
> There is no `drivers/mg995.py`. The MG995 backend was removed - see
> **Clean ESP32 installation** for how to clear it off a board that still
> carries it.

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

**4. Remove the old FLAT layout files.** The firmware is a package tree
now, so any driver still sitting at the device root is a stale duplicate,
and MicroPython will import it in preference to the packaged one:

```powershell
py -m mpremote connect COM4 fs rm :as7265x.py
py -m mpremote connect COM4 fs rm :carousel.py
py -m mpremote connect COM4 fs rm :mg995.py
```

Those three are the entire old flat layout. `boot.py`, `config.py` and
`main.py` also lived at the root, but they still do - the upload simply
overwrites them.

**5. Remove the withdrawn MG995 backend from the package tree**, if this
board was ever flashed with a development build that carried it:

```powershell
py -m mpremote connect COM4 fs rm :drivers/mg995.py
```

Nothing imports it any more, so a stale copy does no harm on its own. It
is still 20 KB of a 4 MB flash, and it will mislead the next person who
lists the directory.

**6. Upload the runtime tree** (see the next section).

**7. Reset and verify.**

```powershell
py -m mpremote connect COM4 fs ls
```

```powershell
py -m mpremote connect COM4 fs ls :drivers
```

The root should hold exactly this:

```text
boot.py   config.py   main.py   control/   drivers/   protocol/
```

And the three package directories exactly this - 5, 3 and 7 files:

```text
drivers/    __init__.py  as7265x.py  servo_base.py  st3215.py
            st3215_registers.py

control/    __init__.py  carousel.py  servo_manager.py

protocol/   __init__.py  transport.py  router.py  servo_commands.py
            carousel_commands.py  sensor_commands.py  sample_commands.py
```

Anything else in those directories is stale. `__pycache__` in particular
means a recursive copy was used where it should not have been.

---

## Upload the ESP32 runtime

The firmware is a package tree, so the three directories must exist on the
device before anything is copied into them. `mkdir` on an existing
directory reports `EEXIST`, which is harmless.

From `ESP32`:

```powershell
py -m mpremote connect COM4 fs mkdir :drivers
```

```powershell
py -m mpremote connect COM4 fs mkdir :control
```

```powershell
py -m mpremote connect COM4 fs mkdir :protocol
```

Then the application entry points:

```powershell
py -m mpremote connect COM4 fs cp boot.py main.py config.py :
```

The drivers:

```powershell
py -m mpremote connect COM4 fs cp drivers/__init__.py drivers/as7265x.py drivers/servo_base.py drivers/st3215.py drivers/st3215_registers.py :drivers/
```

The control layer:

```powershell
py -m mpremote connect COM4 fs cp control/__init__.py control/carousel.py control/servo_manager.py :control/
```

The protocol layer:

```powershell
py -m mpremote connect COM4 fs cp protocol/__init__.py protocol/transport.py protocol/router.py protocol/servo_commands.py protocol/carousel_commands.py protocol/sensor_commands.py protocol/sample_commands.py :protocol/
```

Then reset:

```powershell
py -m mpremote connect COM4 reset
```

> [!IMPORTANT]
> The `__init__.py` files are **not optional**. MicroPython needs them to
> treat `drivers/`, `control/` and `protocol/` as packages; without them
> `main.py` fails to import and the board sits at the REPL.

**Do not** use a recursive copy of the whole directory. Running the tests
creates `__pycache__/` folders full of CPython bytecode, and a recursive
copy would upload those too — useless bytes in a 4 MB flash.

That is the complete runtime. The list comes from the actual imports:
`main.py` imports `control` and `protocol`; `protocol` imports `control`;
`control` imports `drivers`; `drivers` imports only `config` and
`machine`. Nothing else is reachable, and the dependency direction is
enforced by `Tests/test_architecture.py`.

---

## Upload only what changed

During development, re-upload the changed file and reset — not the whole
set.

One file, with its directory:

```powershell
py -m mpremote connect COM4 fs cp drivers/as7265x.py :drivers/as7265x.py
```

```powershell
py -m mpremote connect COM4 reset
```

Several files, each to its own directory on the device:

```powershell
py -m mpremote connect COM4 fs cp main.py :main.py
```

```powershell
py -m mpremote connect COM4 fs cp config.py :config.py
```

```powershell
py -m mpremote connect COM4 fs cp drivers/st3215.py :drivers/st3215.py
```

```powershell
py -m mpremote connect COM4 reset
```

> [!WARNING]
> The destination path must match the source path. Copying
> `drivers/as7265x.py` to `:as7265x.py` puts a second copy at the device
> root, and MicroPython imports **that** one instead - so the file you
> just edited appears to have no effect. If a change seems to be ignored,
> list the root and look for a driver that should not be there.

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

The usual cause is a missing module. With a package tree there are two
distinct ways for that to happen:

- a file was not uploaded — check each directory with
  `fs ls :drivers`, `fs ls :control`, `fs ls :protocol`;
- an `__init__.py` is missing, so MicroPython does not treat the directory
  as a package at all and the import fails with `ImportError: no module
  named 'drivers'` even though the files are visibly there.

Narrow it down one layer at a time:

```python
import drivers.st3215
import control.carousel
import protocol.router
import main
```

The first one that raises names the problem. Upload what is missing and
reset.

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
| Fixed White and Dark | `BD/calibrations/legacy/references.json` | **read only** |
| Reference material database (22 materials) | `BD/databases/legacy_18ch/database.json` | **read only** |
| Measured Sample archive | `BD/samples/samples.json` | read/write |
| Full spectral calibrations | `BD/calibrations/` | append only |
| Active calibration pointer | `BD/calibrations/active.json` | rewritten on activation |
| Analysis thresholds and calibration ID | `BD/config.py` | |
| Hardware settings (pins, gain, servo bus, carousel geometry) | `ESP32/config.py` | |

All three data files sit together in `BD`, but only
`samples.json` is ever written. The other two are opened read-only and
have no save path in the code at all.

`samples.json` holds **complete** records - all three 18-channel spectra,
the White and Dark actually used for that sample, the comparison against
every material, the sensor settings, timestamps and metadata - so a
measurement can be re-derived from the archive alone. Writes go to a
temporary file and are moved into place with an atomic rename, so an
interrupted write cannot corrupt the archive.

An archive from the retired `PC/data/` layout is picked up and
migrated automatically on first start; the old files are left where they
are. Old short records stay readable and are listed correctly.

---

## Back up the measured Samples

Measured Samples are the run's scientific output and the only data here
that cannot be regenerated. Copy the archive:

```powershell
cd C:\Users\Public\Documents\Altium\as7265x-soil-analysis-module\firmware\BD\data
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

Every threshold lives in `BD/config.py` with a comment
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

## Carousel setup: connecting the servo

One actuator is fitted, and connecting to it is an explicit step. After
every boot:

```text
active servo    = NONE
carousel        = position invalid
movement        = blocked
```

Every movement command answers `SERVO_NOT_CONNECTED` until the operator
connects it. That is a safety property, not an inconvenience: until the
servo has answered, the firmware has no idea where the carousel is.

```text
[0] Carousel Setup

============================================================
 CAROUSEL SERVO
============================================================

Connect the carousel servo?

[1] Waveshare ST3215
    Serial bus servo, encoder-based positioning

[c] Cancel
```

Connecting **always** invalidates the carousel position, even on a
reconnect. Position state cannot survive it: between one connection and
the next the servo may have been unplugged, turned by hand, or replaced
by another with a different encoder zero.

Changing servo mid-session is done from the same place, or from
**Tools → Servo / Carousel Tools → Change servo**. The previous backend is
released first — UART2 closed — before the new one comes up.

---

## ST3215 bring-up

The ST3215 is closed loop. It carries a 4096-count absolute magnetic
encoder and runs its own position controller, so there is **no timing to
calibrate**: a movement is commanded in encoder counts and the firmware
reads the encoder back to confirm it. What follows is a sequence of
**checks**, and only the last three move anything.

Every constant is namespaced `ST3215_*`.

### 0. Wiring and power — before anything else

```text
ESP32 PCB                        Waveshare servo driver board
---------                        ----------------------------
GPIO17 / TX2  ---------------->  TX
GPIO16 / RX2  ---------------->  RX
GND           <-------------->   GND

                NO POWER CONNECTION FROM THE ESP32 PCB
```

```text
External servo power supply (6–12.6 V, 12 V recommended)
          │
          ▼
Waveshare serial bus servo driver board
          │  servo bus
          ▼
       ST3215
          │
          ▼
       Carousel
```

> [!IMPORTANT]
> **The ST3215 servo subsystem is externally powered.** The Freya ESP32
> science PCB provides only UART TX, UART RX and a common ground reference
> to the Waveshare serial bus servo driver. **No servo supply current is
> drawn from the ESP32 PCB.**

> [!CAUTION]
> The **common ground is mandatory**. Separate supplies with no shared
> reference leave the UART with no defined logic level; the symptom is a
> servo that never answers, or answers only sometimes.

> [!NOTE]
> The PCB also carries a `+5V_SERVO` branch and a `SERVO_PWM` line on
> GPIO25, left from an earlier PWM servo. **That hardware is unused —
> leave it unconnected.** It remains on the schematic because it is
> populated on existing boards; no firmware drives it.

### 1. Select the servo

`[0] Carousel Setup` → `[2] Waveshare ST3215`.

If the servo does not answer, the selection **fails** and nothing is
connected — the firmware has no fallback. The error names
the reason:

| Error | Look at |
|---|---|
| `SERVO_NOT_FOUND` | external servo supply, then the common ground |
| `SERVO_NOT_FOUND` with power confirmed | GPIO17 → TX, GPIO16 → RX (not swapped) |
| `SERVO_PROTOCOL_ERROR` naming another ID | two servos sharing an ID |
| `SERVO_CHECKSUM_ERROR`, intermittent | wiring, grounding, cable length |

### 2. Communication — `Diagnostics`

Moves nothing. Every stage is reported on its own line:

```text
  ok   uart
  ok   ping
  ok   id
  ok   baud_code
  ok   mode
  ok   torque
  ok   angle_limits
  ok   feedback
```

Then the telemetry the servo itself reports: position in counts and
degrees, speed, load, input voltage, temperature, current, and any alarm
flags. `baud_matches: false` means the servo has been reconfigured off
1 Mbps.

### 3. Operating mode — `SERVICE: write servo configuration`

Run **once per servo**. It writes the ST3215 **EPROM**: step servo mode
(memory table address `0x21` = 3) and both angle limits cleared, which is
what lets the servo step indefinitely.

Step servo mode is the reason the carousel never takes the long way round.
In that mode the goal register means *"move this many counts from where you
are"*, so `Slot 4 → Slot 1` is `+1024` counts whatever the absolute encoder
happens to read — crossing 4095 → 0 is a non-event.

The firmware **refuses to move** a servo reporting any other mode, and says
so with `SERVO_MODE_ERROR`. In position servo mode the same register means
"go to this absolute count", and a request for one slot would send the
carousel somewhere else entirely.

> [!IMPORTANT]
> This is a **SERVICE** operation, deliberately separate from carousel
> setup. Ordinary setup establishes a runtime origin and writes nothing
> persistent to the servo. EPROM has a finite write life and survives a
> power cycle, so this is needed once, not once per session.

### 4. Forward direction — `Movement tests → One slot forward`

Move one slot and watch which slot number arrives at the scanner. If the
numbers run backwards, flip `CAROUSEL_FORWARD_DIRECTION` in
`ESP32/config.py` from `"cw"` to `"ccw"` and upload it.

This is the one thing software cannot work out for itself: it depends on
how the carousel is mounted, not on the servo, so it is shared by both
backends.

### 5. Repeatability — `One slot forward and back`

Symmetrical, so it must close on itself. The report gives the number that
matters:

```text
  Net travel:    0 counts (0.0 deg)
  Worst error:   3 counts (tolerance 15)
  Closing error: 1 counts (0.088 deg)
```

Run it with **repeat 5** and note the **largest** closing error you see,
not the average of a good run. Then do the same with `Half turn out and
back`, which is exactly what Measure Sample performs.

### 6. The encoder boundary — `Encoder boundary crossing`

Parks the servo a few counts short of the 4095 → 0 seam, steps across it,
and steps back. Confirm that each leg reports a small error and that the
carousel visibly moves one slot, not almost a full revolution.

### 7. Set the position tolerance

`ST3215_POSITION_TOLERANCE` ships at **15 counts (≈1.3°)** and is
deliberately conservative — it has **not** been measured on this mechanism.
Set it from what you measured in steps 5 and 6: comfortably above the worst
closing error you observed, and no tighter.

```text
1 count   = 0.0879°
15 counts = 1.32°
```

Too tight turns ordinary backlash into a failed measurement. Too loose lets
a genuinely misaligned slot be measured.

Then upload and reset:

```powershell
py -m mpremote connect COM4 fs cp config.py :config.py
```

```powershell
py -m mpremote connect COM4 reset
```

Then re-run Carousel Setup, because a reset forgets both the servo
selection and which physical slot is Slot 1.

### 8. Verify end to end

Run a normal Measure Sample and confirm the sample ends at the loading
position it started from, and that `Drift vs nominal` in
**Tools → System Status** stays small over several cycles.

> [!NOTE]
> The encoder removes timing drift, but it
> cannot tell the firmware which physical slot is Slot 1 — there is no
> limit switch, no Hall sensor and no index mark. **Re-sync Carousel**
> after every power-up remains mandatory, and **Fine Carousel Alignment**
> remains the tool for mechanical trim.

### If a movement fails

| Error | Meaning | What to do |
|---|---|---|
| `SERVO_NOT_CONNECTED` | the servo is not connected | run `[0] Carousel Setup` |
| `SERVO_POSITION_MISMATCH` | the servo stopped, in the wrong place | check for a mechanical obstruction; check the tolerance |
| `SERVO_POSITION_TIMEOUT` | it never reported arrival | check load, supply voltage and the load/current telemetry |
| `SERVO_UART_TIMEOUT` | the link went quiet mid-movement | wiring and grounding |
| `SERVO_MODE_ERROR` | wrong operating mode | run SERVICE: write servo configuration |
| `SERVO_TORQUE_DISABLED` | torque was released | Torque hold / release |
| `SERVO_NOT_SUPPORTED` | the servo cannot do this | a capability answer, not a fault |

In every one of these cases the tracked position is **invalidated**, not
guessed. The firmware reads whatever the backend can still report and says
so, then requires a re-sync. There is no fallback to timed movement on the
ST3215, and no silent substitution of one movement for another: losing
feedback is a hardware fault, not a reason to start guessing.

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
6.  Carousel Setup  ->  connect the ST3215
    ST3215). Nothing will move until this is done.
6a. ST3215 only, first time on this servo: Tools -> Servo / Carousel
    Tools -> Diagnostics, then SERVICE: write servo configuration.
7.  Align physical Slot 1 under the loading hole.
8.  Confirm the current position as Slot 1 / LOAD.
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

Run the tests from `Tests\`:

```powershell
cd C:\Users\Public\Documents\Altium\as7265x-soil-analysis-module\firmware\Tests
```

Everything, with one summary line per suite:

```powershell
py run_all.py
```

```text
test_architecture.py       layer boundaries and metric dependence 229 ok
test_st3215.py             ST3215 protocol and verified movement  210 ok
test_servo_manager.py      servo lifecycle and the movement gate   91 ok
test_carousel.py           carousel geometry and planning         103 ok
test_esp32.py              sensor lifecycle and command protocol  161 ok
test_pc.py                 client, archive and data layout        146 ok
test_integration.py        ESP32 -> PC -> BD end to end           131 ok
test_science.py            formulas and protected data            121 ok
test_calibration.py        calibration build, validate, activate   75 ok
test_inference.py          feature space, registry, mixture        88 ok
test_evidence.py           representations, reliability, distances  77 ok
test_decision.py           decision levels, learning history, leakage 138 ok
test_db1.py                DB1 integrity and determinism           34 ok
test_science_plan.py       Mars Yard, requirements, plan, four sites 110 ok
test_science_exploration.py mission analysis, limits, readiness    108 ok
all 15 suites passed, 1822 checks total
```

One suite, by substring:

```powershell
py run_all.py servo
```

Or a single file directly, which is what to do while working on it:

```powershell
py test_carousel.py
```

Each suite runs in its own process, and that is not incidental:
`firmware/ESP32` and `firmware/BD` both contain a module called `config`,
and several suites reload the whole firmware tree. Sharing one interpreter
would let one suite's imports decide another suite's results.

They run the real firmware on CPython against a fake AS7265x AND a fake
ST3215 that speaks the actual serial-bus frame protocol, the real science
layer against the real protected data (read-only), and the real client
against a loopback that runs the real firmware dispatcher. No board is
needed, and both servo backends are covered.

Check what changed:

```powershell
git status --short firmware/ESP32
```

```powershell
git diff -- ESP32
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
`ESP32/config.py`, re-upload it, and reset:

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
BD/*
PC/*
Measurements/*
DecisionModel/*
Science/*
Tests/*
research/*
BD/data/*
Documentation/*
README.md
HANDOFF.md
Hardware/*
**/__pycache__/*
```

Only the 18 files under `ESP32/` belong on the board: `boot.py`,
`config.py`, `main.py` and the three package directories. The ESP32 does
not need `DB1.json`, `calibration_legacy.json` or anything else
scientific — it performs no science.

`__pycache__/` deserves its own mention. Running the tests fills
`ESP32/drivers/`, `ESP32/control/` and `ESP32/protocol/` with CPython
bytecode, which is why the upload commands name files explicitly instead
of copying directories recursively.

---

## Files that must NEVER be modified

**Protected scientific data.** Read-only during normal operation; never
regenerated, reformatted, re-sorted or rewritten as part of any startup
or deployment step:

```text
BD/databases/legacy_18ch/database.json     the reference material database
BD/calibrations/legacy/references.json   the fixed competition White and Dark
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

ESP32 source:   firmware\ESP32       (boot, config, main + drivers/ control/ protocol/)
Science / BD:   firmware\BD
Measurements:   firmware\Measurements
PC application: firmware\PC
Tests:          firmware\Tests
Saved Samples:  firmware\BD\data\samples.json
Calibrations:   firmware\BD\calibrations


--- SERVO ------------------------------------------------------------

ONE actuator: Waveshare ST3215. Nothing moves until it is connected.
After every boot:  servo = NOT CONNECTED, position invalid,
                   every movement answers SERVO_NOT_CONNECTED.

  ST3215   UART2 serial bus, 1 Mbps
           TX GPIO17, RX GPIO16, GND   (see config.py ST3215_*)
           4096-count encoder, closed loop, verified movement
           POWERED EXTERNALLY at the driver board - no servo current
           is drawn from this PCB
           ID range 0-253, factory default 1

Connect it:  PC option [0] Carousel Setup.
Connecting always invalidates the carousel position.


--- FIND THE BOARD ---------------------------------------------------

List devices:
py -m mpremote connect list

List ESP32 files:
py -m mpremote connect COM4 fs ls :
py -m mpremote connect COM4 fs ls :drivers
py -m mpremote connect COM4 fs ls :control
py -m mpremote connect COM4 fs ls :protocol


# CLEAR THE STALE FILES (safe to run on an already-clean board;
#                        "ENOENT" just means it was not there)

old flat layout, now inside packages:
py -m mpremote connect COM4 fs rm :as7265x.py
py -m mpremote connect COM4 fs rm :carousel.py
py -m mpremote connect COM4 fs rm :mg995.py

withdrawn backend, if a dev build ever put it there:
py -m mpremote connect COM4 fs rm :drivers/mg995.py

old flat project data:
py -m mpremote connect COM4 fs rm :database.py
py -m mpremote connect COM4 fs rm :database.json
py -m mpremote connect COM4 fs rm :references.json
py -m mpremote connect COM4 fs rm :sample_analysis.py
py -m mpremote connect COM4 fs rm :sample_store.py
py -m mpremote connect COM4 fs rm :samples.json
py -m mpremote connect COM4 fs rm :samples.json.bak
py -m mpremote connect COM4 fs rm :sensor_diag.py
py -m mpremote connect COM4 fs rm :wipe.py

Do NOT use "fs rm -r :". It removes every user file on the board,
including anything you have installed under lib/, and it is never
needed - boot.py, config.py and main.py are overwritten by the upload
below rather than deleted first.


--- UPLOAD THE RUNTIME (from firmware\ESP32) -------------------------
create three folders:

py -m mpremote connect COM4 fs mkdir :drivers
py -m mpremote connect COM4 fs mkdir :control
py -m mpremote connect COM4 fs mkdir :protocol

The three directories must exist first. "EEXIST" is harmless.


py -m mpremote connect COM4 fs cp boot.py :boot.py
py -m mpremote connect COM4 fs cp main.py :main.py
py -m mpremote connect COM4 fs cp config.py :config.py

py -m mpremote connect COM4 fs cp drivers/__init__.py :drivers/__init__.py
py -m mpremote connect COM4 fs cp drivers/as7265x.py :drivers/as7265x.py
py -m mpremote connect COM4 fs cp drivers/servo_base.py :drivers/servo_base.py
py -m mpremote connect COM4 fs cp drivers/st3215.py :drivers/st3215.py
py -m mpremote connect COM4 fs cp drivers/st3215_registers.py :drivers/st3215_registers.py

py -m mpremote connect COM4 fs cp control/__init__.py :control/__init__.py
py -m mpremote connect COM4 fs cp control/servo_manager.py :control/servo_manager.py
py -m mpremote connect COM4 fs cp control/carousel.py :control/carousel.py

py -m mpremote connect COM4 fs cp protocol/__init__.py :protocol/__init__.py
py -m mpremote connect COM4 fs cp protocol/transport.py :protocol/transport.py
py -m mpremote connect COM4 fs cp protocol/router.py :protocol/router.py
py -m mpremote connect COM4 fs cp protocol/servo_commands.py :protocol/servo_commands.py
py -m mpremote connect COM4 fs cp protocol/carousel_commands.py :protocol/carousel_commands.py
py -m mpremote connect COM4 fs cp protocol/sensor_commands.py :protocol/sensor_commands.py
py -m mpremote connect COM4 fs cp protocol/sample_commands.py :protocol/sample_commands.py

The __init__.py files are NOT optional - MicroPython needs them to
import the packages. Do NOT use a recursive copy: it would upload the
__pycache__ folders the tests leave behind.

18 files total: 3 at the root, 6 drivers, 3 control, 6 protocol.


--- VERIFY THE UPLOAD ------------------------------------------------

py -m mpremote connect COM4 exec "import drivers.st3215; print('ST3215 OK')"
py -m mpremote connect COM4 exec "import drivers.st3215; print('ST3215 OK')"
py -m mpremote connect COM4 exec "import control.servo_manager; print('MANAGER OK')"
py -m mpremote connect COM4 exec "import control.carousel; print('CAROUSEL OK')"
py -m mpremote connect COM4 exec "import protocol.router; print('ROUTER OK')"
py -m mpremote connect COM4 exec "import main; print('MAIN OK')"

An ImportError here is the whole reason main.py would sit at the REPL
instead of answering the PC.


--- RUN --------------------------------------------------------------

Reset:
py -m mpremote connect COM4 reset

REPL:
py -m mpremote connect COM4 repl

Inspect a running instance from the REPL (after Ctrl+C):
>>> import main
>>> main.module.servos.status()
>>> main.module.carousel.status()

Run the application (from firmware\PC):
py rover_science_client.py --port COM4

One command without the UI (from firmware\PC):
py rover_science_client.py --port COM4 --command get_status
py rover_science_client.py --port COM4 --command select_servo --payload "{\"servo\": \"st3215\"}"


--- FIRST RUN ORDER --------------------------------------------------

1. py rover_science_client.py --port COM4
2. [0] Carousel Setup      -> connect the ST3215
3. ST3215, first time only -> Tools -> Servo / Carousel Tools
                              -> Diagnostics
                              -> SERVICE: write servo configuration
4. Align physical Slot 1 with the loading hole
5. [7] SET CURRENT POSITION AS SLOT 1 / LOAD
6. Normal workflow: [1] choose slot .. [4] measure


--- BACK UP THE SAMPLES (from firmware\BD\data) ----------------------

Copy-Item samples.json samples_backup.json


--- TESTS (from firmware\Tests) --------------------------------------

Everything, one summary:
py run_all.py

One suite:
py run_all.py st3215

Individually:
py test_architecture.py     layer boundaries, dependency direction
py test_st3215.py           ST3215 protocol and verified movement
py test_servo_manager.py    servo selection and switching
py test_carousel.py         geometry and planning, on both backends
py test_esp32.py            sensor lifecycle and command protocol
py test_pc.py               client, archive, data layout
py test_integration.py      ESP32 -> PC -> BD end to end
py test_science.py          formulas, protected-data hashes
py test_calibration.py      calibration build / validate / activate
py test_inference.py        feature space, registry, mixture
py test_db1.py              DB1 integrity and determinism

Expect: all 15 suites passed, 1822 checks total.


--- ERROR CODES YOU WILL ACTUALLY SEE -------------------------------

SERVO_NOT_CONNECTED       run [0] Carousel Setup first
SERVO_NOT_FOUND           ST3215 silent: external PSU, then common GND
SERVO_MODE_ERROR          run SERVICE: write servo configuration
SERVO_POSITION_MISMATCH   stopped in the wrong place: obstruction?
SERVO_POSITION_TIMEOUT    never arrived: load, supply voltage
SERVO_CHECKSUM_ERROR      wiring, grounding, cable length
SERVO_NOT_SUPPORTED       the servo cannot do this; a capability answer
POSITION_NOT_SYNCHRONIZED re-sync the carousel
SLOT_NOT_AT_LOADER        select the slot first, then measure
```
