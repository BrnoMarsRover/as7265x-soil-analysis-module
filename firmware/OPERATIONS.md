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
├── BD/             science, runs on the PC
│   ├── database.json      PROTECTED - 22 reference materials, READ ONLY
│   ├── references.json    PROTECTED - fixed White + Dark, READ ONLY
│   ├── config.py          calibration ID, thresholds
│   ├── database.py        reference loading, cosine comparison
│   └── sample_analysis.py validation, formulas, interpretation
│
├── PC/             mission control, runs on the PC
│   ├── rover_science_client.py   the application you start
│   ├── esp32_link.py             serial transport
│   ├── sample_store.py           persistent Sample archive
│   ├── requirements.txt
│   └── data/                     measured Samples live here
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

A reset clears all runtime state: the carousel position becomes unknown
and physical slot occupancy is forgotten. Re-run the Initial Carousel
Calibration afterwards. Saved Samples are on the PC and are unaffected.

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

| What | Where |
|---|---|
| Fixed White and Dark | `firmware/BD/references.json` |
| Reference material database (22 materials) | `firmware/BD/database.json` |
| Analysis thresholds and calibration ID | `firmware/BD/config.py` |
| Hardware settings (pins, gain, servo timing) | `firmware/ESP32/config.py` |
| Measured Sample index | `firmware/PC/data/samples.json` |
| One record per Sample | `firmware/PC/data/samples/S001.json` |

---

## Back up the measured Samples

Measured Samples are the run's scientific output and the only data here
that cannot be regenerated. Copy the whole directory:

```powershell
cd C:\Users\Public\Documents\Altium\as7265x-soil-analysis-module\firmware\PC
```

```powershell
Copy-Item -Recurse data data_backup
```

Dated copy, so repeated backups do not overwrite each other:

```powershell
Copy-Item -Recurse data ("data_backup_" + (Get-Date -Format yyyyMMdd_HHmmss))
```

Do this after every competition run.

---

## Sensor Test

One command that exercises the whole system through the production code
path. It requires no carousel synchronization, no Sample ID, moves
nothing, and saves nothing.

```text
Run the application
  → [t] Tools / Records
  → [5] Sensor Test
```

It reports, in order:

```text
ESP32 HARDWARE
  Serial communication / Sensor recovery / I2C 0x49 /
  Internal devices / Configuration / Illumination /
  18-channel acquisition

PC SCIENCE PIPELINE
  White/Dark references / Dark correction / Normalization /
  Material database / Database comparison

SETTINGS      integration cycles, gain, LED current, mode
SPECTRUM      all 18 channels: RAW, dark-corrected, normalized
DATABASE      every material, ranked by similarity
RESULT        best match, second match, difference, conclusion
```

If a stage fails it prints `FAILED STAGE`, the error code, the exact
message, and whatever partial result it had. It never returns silently to
the menu.

---

## Competition startup checklist

```text
1.  Connect the ESP32 USB cable.
2.  Confirm the COM port:   py -m mpremote connect list
3.  Start the PC application.
4.  Confirm "Connection: ONLINE".
5.  Tools -> System Status: sensor READY, 100 cycles, 16x,
    BD references READY, material DB READY.
6.  Initial Carousel Calibration.
7.  Align physical Slot 1 under the loading hole.
8.  Confirm the current position as Slot 1.
9.  Choose Slot.
10. Prepare Sample (Sample ID + metadata).
11. Rover arm deposits soil.
12. Confirm Sample Loaded.
13. Measure Sample.
14. Verify the Sample was saved (Tools -> Sample Database).
15. Continue with the next slot.
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

```powershell
git diff -- as7265x-soil-analysis-module.PrjPcb
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
integration cycles = 100
gain               = 16x
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
firmware/PC/data/*
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
as7265x-soil-analysis-module.PrjPcb
```

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
Saved Samples:  firmware\PC\data

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

Back up Samples (from firmware\PC):
Copy-Item -Recurse data data_backup

Run the tests (from tests):
py test_esp32.py
py test_science.py
py test_pc.py
py test_integration.py
```
