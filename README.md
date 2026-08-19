# AS7265x Soil Analysis Module

> Multispectral soil-analysis subsystem developed for the **Brno Mars Rover / Freya** science payload.

![Status](https://img.shields.io/badge/status-hardware%20complete%20%7C%20calibration%20%26%20verification-orange)
![Hardware](https://img.shields.io/badge/PCB-V1.0%20final-blue)
![Firmware](https://img.shields.io/badge/firmware-3.0.0-black)
![Sensor](https://img.shields.io/badge/sensor-AS7265x-4c8bf5)

<p align="center">
  <img src="Photos/VER1.0/3D_PCB_ver1,0.png"
       alt="AS7265x Soil Analysis Module PCB V1.0"
       width="760">
</p>

---

# 1. System Architecture & Sensor Task

## Overview

The **AS7265x Soil Analysis Module** is a rover-mounted subsystem for comparative multispectral analysis of soil and geological samples.

The system uses the **AS7265x 18-channel VIS-NIR spectral sensor** to measure reflected light between approximately **410 nm and 940 nm**. An ESP32 controls the sensor, the eight-slot sample carousel, illumination, and the calibration pipeline.

The ESP32 is a **passive command-controlled instrument**. It performs no automatic movement and no automatic measurement: the main computer is the mission controller and issues one JSON command at a time over the USB serial link. The ESP32 moves the carousel, reads the sensor and returns a raw spectrum; all scientific processing and all persistent sample data live on the main computer.

The project combines:

- custom PCB design;
- protected power distribution;
- embedded control;
- multispectral data acquisition;
- calibration and measurement methodology;
- spectral database development;
- mechanical and rover-system integration.

The current PCB V1.0 provides the core functionality required for the competition while remaining open to future sensor and hardware improvements.

---

## Engineering Scope

The project was developed from system concept to an assembled and operational hardware platform.

The engineering work includes:

- defining the overall science-module architecture;
- selecting the sensor, controller, actuator, connectors, and protection components;
- designing the complete schematic in Altium Designer;
- creating a custom two-layer carrier PCB;
- designing protected 5 V and 3.3 V power branches;
- integrating I²C, UART, PWM, and illumination interfaces;
- preparing the component BOM and purchasing documentation;
- assembling and debugging the first PCB revision;
- developing ESP32 firmware for sensor and servo control;
- creating a structured spectral material database;
- defining calibration and sample-preparation procedures;
- preparing the system for mechanical integration with the Freya rover.

---

## System Operation

The module is command-driven. Nothing moves and nothing is measured until the rover main computer asks for it.

1. The operator aligns physical Slot 1 with the loading hole and confirms it (`sync_position`).
2. `select_slot` brings the next slot to the loading hole, normally one 45 deg clockwise step.
3. The PC creates a persistent Sample record with an ID and science metadata.
4. The external rover arm deposits soil; the operator confirms that it happened.
5. `measure_raw` rotates the same physical slot to the scanner, settles, and acquires one **raw** 18-channel spectrum.
6. The PC corrects that spectrum with the **fixed** stored dark and white references.
7. The normalized spectrum is compared against **every** material in the reference database.
8. An automatic, deliberately conservative interpretation is generated.
9. The complete scientific record is written to the PC archive — the same record opened in step 3.

```mermaid
flowchart LR
    SAMPLE[Soil sample in carousel slot]
    LIGHT[Controlled illumination]
    SENSOR[AS7265x spectral sensor]
    ESP[ESP32 hardware controller]
    SERVO[MG995 360 deg carousel servo]
    PC[PC mission controller]
    BD[BD science layer]
    DATABASE[(database.json<br/>reference materials)]
    REFS[(references.json<br/>fixed white + dark)]
    STORE[(PC data/samples<br/>measured samples)]

    ESP -->|GPIO| LIGHT
    LIGHT --> SAMPLE
    SAMPLE -->|Reflected VIS-NIR light| SENSOR
    ESP <-->|I²C| SENSOR
    ESP -->|timed PWM| SERVO
    ESP -->|USB serial JSON: RAW spectrum| PC
    PC -->|raw| BD
    DATABASE --> BD
    REFS --> BD
    BD -->|normalized + matches| PC
    PC --> STORE
```

---

## Sample Carousel

The sample mechanism is a full 360° carousel driven by an **MG995 modified for continuous rotation**. It has no encoder, no Hall sensor and no position feedback of any kind; position is tracked entirely in software.

| Property | Value |
|---|---|
| Physical sample slots | 8 |
| Angle between neighbouring slot centres | 360° / 8 = **45°** |
| Scanning position ↔ loading position | **180°**, i.e. **4 slots** apart |

Because the two fixed positions are exactly opposite, the slot at the loader follows directly from the slot at the scanner:

```text
load_slot = ((scan_slot - 1 + 4) % 8) + 1
```

The relationship is its own inverse and holds in both directions: putting Slot 3 at the loader necessarily puts Slot 7 at the scanner, and Slot 1 at the loader means Slot 5 at the scanner.

Slots are numbered **1–8 clockwise**, and the origin is established by the operator: physical Slot 1 is aligned with the **soil loading hole** and declared as Slot 1.

```text
Slot 1 = loading position (the calibrated origin)
Slot 2 = 45°  clockwise from Slot 1
...
Slot 8 = 315° clockwise from Slot 1
```

### Software position tracking

There is no sensor to ask, so after every reset:

```python
position_valid = False
current_scan_slot = None
selected_slot = None
```

Synchronization is the **first** operation after boot. The operator moves the carousel with whole-slot and degree commands until Slot 1 sits under the loading hole, then confirms it:

```json
{"cmd": "sync_position", "load_slot": 1}
```

`sync_position` never moves anything — it records where the operator has already put the mechanism. Because the scanner is exactly 180° opposite, declaring Slot 1 at the loader also declares **Slot 5 at the scanner**.

From then on the firmware maintains position by counting calibrated slot transitions. If a movement fails, the tracked position is **discarded** rather than guessed, and the operator must re-synchronize.

### Three separate position concepts

These are deliberately kept apart:

| Concept | Meaning |
|---|---|
| `current_load_slot` | Which slot is physically under the loading hole |
| `current_scan_slot` | Which slot is physically under the scanner |
| `selected_slot` | Which slot the operator is working with |

While soil is deposited the selected slot sits at the loader. After a measurement the carousel has swung 180°, so the **same** selected slot now sits at the scanner instead.

### Choosing a slot

`select_slot` brings the chosen slot to the loading hole. Sequential progression is always one clockwise step, because any move of four slots or fewer resolves in the forward (clockwise) direction:

```text
Slot 1 -> Slot 2   = one clockwise slot transition
Slot 2 -> Slot 3   = one clockwise slot transition
Slot 8 -> Slot 1   = one clockwise slot transition
```

Each transition spans 45° of geometry but is *commanded* with the calibrated ~40° equivalent — see below.

Selecting a distant slot still takes the shortest path.

### Movement calibration: geometry vs servo command

The single most important distinction in the carousel code:

| | |
|---|---|
| **Geometry** | what the carousel *is* — 45° slot spacing, 180° loader↔scanner |
| **Calibration** | what the servo must be *commanded* to do |

These are not the same, and one is never derived from the other. A continuous-rotation servo has acceleration, backlash and load-dependent speed, so the command that actually lands a slot correctly is not "45° worth of runtime". Hardware testing showed a full 45° command **overshoots** the next slot; the effective command is closer to **40°**.

Each movement type therefore has its own independent calibration:

| Movement | Constants | Notes |
|---|---|---|
| Adjacent slot CW (N → N+1) | `NEXT_SLOT_CW_MS` | Effective ≈ **40°** |
| Adjacent slot CCW (N → N−1) | `NEXT_SLOT_CCW_MS` | Effective ≈ **45°** |
| Half turn (loader ↔ scanner) | `LOAD_TO_SCAN_CW_MS` / `SCAN_TO_LOAD_CCW_MS` | **Independent** — never 4 × adjacent |
| Fine alignment | `CW_MS_PER_DEGREE` / `CCW_MS_PER_DEGREE` | Small corrections only |

> [!IMPORTANT]
> **The two directions are not symmetric.** Measured on the real mechanism: a ~40° command lands the next slot correctly going clockwise, but the *same* command stops about 5° short coming back counter-clockwise, which needs ~45°. Backlash and servo asymmetry both contribute. Never share one calibration between directions — including for fine alignment.

> [!IMPORTANT]
> The half turn is **not** four adjacent-slot moves. Four adjacent commands total roughly 4 × 40° = **160°**, which falls well short of the scanner. It is one continuous sweep with its own calibrated runtime, which also pays the servo's acceleration ramp only once.

### Fine alignment

Timed positioning drifts, so alignment is corrected in **degrees**, never milliseconds:

```json
{"cmd": "fine_adjust", "degrees": -2.5}
```

Positive is clockwise, and `duration_ms = abs(degrees) x CW_MS_PER_DEGREE`. Fine adjustment is **alignment only** — it deliberately does not change the logical slot numbering, the slot state or the sample ID. A carousel nudged by +1.5° is still on the same logical slot in the same phase. Corrections are capped by `MAX_FINE_ADJUST_DEG` (default ±15°); anything larger must use whole-slot movement.

Milliseconds are an internal implementation detail. They appear only in `config.py`, the MG995 driver, and `--verbose` debug output.

### Measurement movement

Measurement is strict about what it will run on. The PC refuses unless the sample is `LOADED`, and the ESP32 independently refuses unless the slot is **the currently selected one** and physically **at the loading hole**. A slot that is not selected is refused with `SLOT_NOT_SELECTED`; a selected slot that is not at the loader is refused with `SLOT_NOT_AT_LOADER`. This is what stops the software's idea of the carousel from drifting away from the mechanism.

It then swings out to the scanner and **stops there**:

```text
Slot X at LOAD
  -> 180 deg calibrated half turn (LOAD_TO_SCAN_CW_MS)
Slot X at SCAN  ->  acquire RAW, return it to the PC
                    carousel_phase = SCAN
```

No hidden return: after a measurement the sample really is at the scanner, and the status says so. **Choose Slot** restores the loading orientation automatically when the operator moves on to the next sample:

```text
Choose Slot 3  ->  180 deg calibrated return (SCAN_TO_LOAD_CCW_MS)
               ->  one clockwise slot transition
               ->  Slot 3 at the loading hole, phase = LOAD
```

If a measurement *fails* after the outward swing, the carousel is returned to the loading position automatically so the retry is not blocked, and the slot stays `LOADED`. If that recovery move itself fails, `position_valid` is set to `False` so the operator re-synchronizes rather than moving blind.

### One record per sample

A sample is a single scientific object from preparation to analysis. *Prepare Sample* opens the persistent record on the PC, *Confirm Loaded* updates it, and *Measure Sample* **completes that same record** — same Sample ID, preparation metadata preserved, no derived `S002_measurement` entity:

```text
Prepare Sample   ->  state READY_TO_LOAD, created_at, metadata
Confirm Loaded   ->  state LOADED, loaded_at
Measure Sample   ->  state MEASURED, measured_at, three spectra,
                     all database matches, analysis
```

---

## Servo Calibration

A continuous-rotation servo has no angle command, only a neutral pulse, direction pulses, and time. Every value that depends on the real mechanism lives in [`firmware/ESP32/config.py`](firmware/ESP32/config.py).

> [!IMPORTANT]
> The shipped values are safe starting points, **not** known-good values. `SERVO_STOP_US`, the direction pulses and the 45° step timing **must be calibrated on the real mechanism**. Load, gearing, battery voltage and the individual servo all change them.

Recommended order, using **Tools → Servo / Carousel Test**:

1. **Neutral.** Trim `SERVO_STOP_US` until the carousel does not creep in either direction.
2. **Direction.** Move one slot and note which way it turns. If the labels are inverted, swap `SERVO_CW_US` and `SERVO_CCW_US`.
3. **Slot order.** Move one slot clockwise and check whether the *next higher* or *next lower* slot number arrives at the scanner. Set `CAROUSEL_FORWARD_DIRECTION` accordingly.
4. **Step time.** Adjust `NEXT_SLOT_CW_MS` and `NEXT_SLOT_CCW_MS` until one step lands cleanly on the next slot. The two directions are separate because backlash and servo asymmetry are rarely symmetric.
5. **Half turn.** Adjust `LOAD_TO_SCAN_CW_MS` and `SCAN_TO_LOAD_CCW_MS` until the 180° sweep lands on the scanner and returns. These are **independently calibrated** and must never be derived from four adjacent-slot moves.
6. **Fine alignment.** Trim `CW_MS_PER_DEGREE` and `CCW_MS_PER_DEGREE` by requesting ±5° and measuring the actual travel.

Multi-slot moves run as N discrete single steps rather than one long rotation, so the servo's start-up acceleration is paid once per step and the calibration stays linear in the step count.

---

## Main Features

- 18-channel spectral acquisition from approximately 410 nm to 940 nm;
- ESP32-based sensor and actuator control;
- eight-slot 360° sample carousel with software position tracking;
- deterministic USB-serial JSON command protocol for the main computer;
- persistent scientific sample storage on the main computer;
- fixed white and dark calibration references, never re-measured mid-run;
- comparison against every material in the spectral reference database;
- automatic, conservative interpretation of each measurement;
- USB programming and debugging through the ESP32 development board;
- protected 5 V rover power input;
- filtered 3.3 V sensor supply;
- separate protected servo power branch;
- MOSFET-controlled external illumination;
- JST-XH connectors for external modules;
- 1206 passive components for easier manual assembly.

---

# 2. Hardware

## Hardware V1.0

The final hardware is built around a custom two-layer carrier PCB connecting the ESP32 controller, AS7265x spectral sensor and carousel servo.

## Main Components

| Function | Component | Purpose |
|---|---|---|
| Controller | ESP-WROOM-32 development board with CP2102 | Sensor control, carousel control, USB serial communication and data processing |
| Spectral sensor | AS7265x multispectral triad | 18-channel VIS-NIR measurement |
| Servo | MG995 modified for 360° continuous rotation | Timed rotation of the eight-slot sample carousel |
| Sensor regulator | TLV75533PDBVR | Protected 3.3 V sensor supply |
| Reverse-polarity protection | AO4407A | Protects the board from incorrect input polarity |
| Main and servo fuses | Littelfuse 2920L300/15DR | Resettable overcurrent protection |
| Input and servo TVS | SMBJ5.0A-13-F | Suppression of voltage transients |
| Sensor filtering | BLM31PG121SN1L | High-frequency noise filtering |
| Illumination switch | AO3400A | Low-side control of external illumination |
| Main power connector | XT30PB | Rover 5 V input |
| Signal connectors | JST-XH | Sensor, UART, servo and auxiliary connections |

---

## Power Architecture

The carrier PCB is powered from the rover's regulated **5 V rail**.

```text
XT30 5 V
   │
   ▼
Main resettable fuse
   │
   ▼
AO4407A reverse-polarity protection
   │
   ▼
+5V_PROTECTED
   │
   ├──► ESP32 development board
   │
   ├──► Servo branch
   │      ├─ resettable fuse
   │      ├─ TVS protection
   │      └─ local bulk / ceramic capacitance
   │
   └──► TLV75533 3.3 V LDO
          │
          ▼
       ferrite bead
          │
          ▼
      +3V3_SENSOR
          │
          ▼
       AS7265x
```

The input stage includes:

- resettable overcurrent protection;
- reverse-polarity protection;
- transient-voltage suppression;
- 1000 µF bulk capacitance;
- ceramic decoupling capacitors.

The servo branch is separated from the sensor supply to reduce the effect of servo current transients on spectral measurements.

> [!CAUTION]
> PCB V1.0 is designed for a regulated **5 V input**, not a higher-voltage battery rail.

---

## Electrical Interfaces

### AS7265x Sensor

The external AS7265x module is connected through a 6-pin JST-XH interface.

| Signal | ESP32 connection | Description |
|---|---|---|
| `+3V3_SENSOR` | — | Filtered 3.3 V sensor supply |
| `GND` | GND | Common ground |
| `ESP32_SDA` | GPIO21 | I²C data |
| `ESP32_SCL` | GPIO22 | I²C clock |
| `AS7265X_INT` | Configurable GPIO | Sensor interrupt |
| `AS7265X_RST` | Configurable GPIO | Sensor reset |

The I²C lines include series resistors and optional pull-up and ESD-protection positions.

### Rover UART connector (reserved)

The PCB carries a dedicated 3.3 V UART interface on UART2.

| Signal | ESP32 pin | Description |
|---|---|---|
| `UART2_TX` | GPIO17 | ESP32 transmit |
| `UART2_RX` | GPIO16 | ESP32 receive |
| `3V3_REF` | 3.3 V | Logic-level reference |
| `GND` | GND | Common ground |

> [!NOTE]
> **This connector is currently unused and reserved.** The competition system communicates over USB instead (see below), and the firmware never initializes a `machine.UART` peripheral. The hardware interface remains on the board for a future revision.

### Main-computer link (USB)

The competition connection is the ESP32 development board's USB port:

```text
Slot 1 at LOAD  →  Slot 5 at SCAN
Slot 2 at LOAD  →  Slot 6 at SCAN
Slot 3 at LOAD  →  Slot 7 at SCAN
...
```

The same cable both powers the ESP32 development board and carries the command protocol. Sensor and servo hardware are powered from the science board's protected rails as before.

Commands arrive on `sys.stdin` and responses leave on `sys.stdout`. The PC selects the baud rate (115200) when it opens the port; no pin or peripheral configuration is involved on either side.

### Servo

| Signal | Description |
|---|---|
| `+5V_SERVO` | Protected servo power |
| `GND` | Servo ground |
| `SERVO_PWM` | ESP32 PWM control (GPIO25) |

The servo connector has dedicated power protection and local bulk capacitance.

The MG995 is a continuous-rotation servo: the PWM pulse selects direction and speed, not an angle. The firmware creates the PWM peripheral lazily, on the first movement command, so powering up the ESP32 never drives the servo pin and can never nudge the carousel out of position.

---

# Hardware Gallery

## Schematics

| Power | ESP32 |
|:---:|:---:|
| <img src="Photos/VER1.0/01_POWER_ver1,0.png" alt="Power schematic" width="100%"> | <img src="Photos/VER1.0/02_ESP32_ver1,0.png" alt="ESP32 schematic" width="100%"> |

## Connections & PCB Layout

| Connections | 2D PCB Layout |
|:---:|:---:|
| <img src="Photos/VER1.0/03_CONNECTION_ver1,0.png" alt="Connections schematic" width="100%"> | <img src="Photos/VER1.0/2D_PCB_ver1,0.png" alt="2D PCB layout" width="100%"> |

## Final Hardware

| Assembled PCB | Complete Rover Module |
|:---:|:---:|
| <img src="Photos/VER1.0/PCB_photo_ver1,0.jpg" alt="Assembled PCB V1.0" width="100%"> | <img src="Photos/VER1.0/Fullprojec_photo_ver1,0.jpg" alt="Complete Freya AS7265x Soil Analysis Module" width="100%"> |

---

## Engineering Development Process

```text
Reflectance[i] =
    (Sample[i] - Dark[i])
    /
    (White[i] - Dark[i])
```

There is exactly **one** white reference and **one** dark reference for the whole competition run. Both live in [`firmware/BD/references.json`](firmware/BD/references.json), which is opened read-only and never written by any part of the system.

```json
{
  "white": { "A": 29.9166, "...": "...", "W": 61.7612 },
  "dark":  { "A": 0.0,     "...": "...", "W": 0.0 }
}
```

At startup the PC application loads the file, validates that both entries exist and that all 18 AS7265x channels are present and numeric, and reuses them for every sample. The ESP32 never sees this file; it does not need it to run the hardware.

Nothing measures white or dark: not at startup, not before a sample, not on any command. There is deliberately **no** "measure dark", "measure white" or "calibrate" command anywhere in the firmware or the application — the operator never performs calibration during normal operation.

The calibration set is identified as `FREYA_COMPETITION_2026_CAL_V1`, and every stored sample records that ID plus its provenance, so any record can be traced back to the references it was normalized against.

If the references are missing or invalid, the application still starts and **System Status** reports exactly what is at fault, while Measure Sample refuses to run rather than acquiring a spectrum that cannot be normalized.

Each measured sample stores three spectra, so a measurement can be re-derived later:

| Stored spectrum | Content |
|---|---|
| `raw` | Exactly what the AS7265x returned |
| `dark_corrected` | `Sample - Dark`, per channel |
| `normalized` | `(Sample - Dark) / (White - Dark)` |

Negative dark-corrected values are kept as they are. A channel reading below the stored dark reference is real information about noise or drift, and clamping it to zero would hide that from the operator.

PTFE is planned as the primary stable white-reference material.

---

## Software Architecture

```text
firmware/
├─ ESP32/    MicroPython hardware controller
├─ BD/       PC-side spectral processing / database
├─ PC/       mission controller / operator application
└─ OPERATIONS.md
```

The software is divided into three functional layers:

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
              │ USB / CP2102
              │ newline JSON
              ▼
            ESP32
      Hardware Controller
              │
        ┌─────┴─────┐
        ▼           ▼
      MG995       AS7265x
     Carousel      Sensor
```

| Layer | Runs on | Owns |
|---|---|---|
| `firmware/ESP32/` | MicroPython on the ESP32 | carousel, servo, AS7265x, **raw** 18-channel acquisition, hardware status |
| `firmware/BD/` | CPython on the PC | fixed White/Dark, material database, dark correction, normalization, comparison, interpretation |
| `firmware/PC/` | CPython on the PC | operator interface, Sample lifecycle, persistent Sample archive, orchestration |

The boundaries are strict and enforced by the test suite:

- ESP32 imports nothing from `BD/` or `PC/`, and needs no JSON data file to move hardware or read the sensor.
- BD depends on no serial port, no I2C, no carousel and no long-lived measurement object — it is pure data processing.
- PC reaches the ESP32 **only** over the serial protocol, and BD **only** by import.

**The ESP32 does not know what material a sample resembles.** It moves hardware, reads hardware, and reports hardware.

---

## ESP32 Firmware

MicroPython executes `main.py` automatically after boot, but that startup does **not** move the servo, measure anything, or cycle through anything. It prepares the hardware and waits.

```text
0x49
```

Current acquisition configuration:

```text
firmware/ESP32/
├─ boot.py       MicroPython boot hook (unused)
├─ main.py       transport, command dispatch, hardware handlers
├─ config.py     hardware settings only: pins, gain, servo, carousel timing
├─ as7265x.py    AS7265x driver + the one sensor runtime lifecycle
├─ mg995.py      timed continuous-rotation servo driver
└─ carousel.py   8-slot geometry and software position tracking
```

Six files, and every one of them is uploaded to the device. Nothing else is.

### One sensor lifecycle

There is exactly one path to a working sensor:

```text
create I²C
   ↓
scan bus
   ↓
verify 0x49
   ↓
verify internal AS7265x devices
   ↓
apply configuration
   ↓
read configuration back
   ↓
READY
```

A failed sensor initialization at boot is not permanently latched. Later sensor commands can retry the complete bring-up sequence.

---

## Acquisition Sequence

```text
ensure sensor ready
        ↓
AS7265x onboard white LED ON
        ↓
300 ms illumination settle
        ↓
start new integration
        ↓
wait DATA_READY
        ↓
read 18 calibrated channels
        ↓
validate spectrum
        ↓
AS7265x onboard white LED OFF
        ↓
return RAW spectrum
```

The LED shutdown is executed through a protected cleanup path so a failed acquisition does not intentionally leave the illumination enabled.

---

## Command Protocol

Communication uses newline-delimited JSON over USB / CP2102.

| Command | Function |
|---|---|
| `ping` | Liveness check |
| `get_status` | Sensor, carousel, servo and physical slot state |
| `sync_position` | Declare the origin: Slot 1 at the loading hole (moves nothing) |
| `select_slot` | Choose the physical slot and bring it to the loader |
| `move_slots` | Whole-slot movement, exactly 45° per slot |
| `fine_adjust` | Small alignment correction in degrees; logical slot unchanged |
| `clear_slot` | Free a physical slot; the PC's scientific record is untouched |
| `measure_raw` | Swing to the scanner and acquire one RAW 18-channel spectrum |
| `sensor_test_raw` | Exercise the whole sensor path and return RAW; moves nothing |
| `servo_stop` | Brake the servo and release the pin |

There is deliberately no `measure_sample`, no `list_samples`, no `get_references` and no `get_database_status`. Those are science and persistence, and they live on the PC.

A successful `measure_raw` response:

```json
{
  "request_id": "42",
  "ok": true,
  "data": {
    "slot_id": 1,
    "sample_id": "S001",
    "raw": {"A": 1.23, "B": 2.34, "...": "...", "W": 4.56},
    "sensor_settings": {
      "integration_cycles": 100,
      "gain": 2,
      "gain_x": "16x",
      "measurement_mode": 2,
      "led_current": 1
    },
    "carousel": {"selected_slot": 1, "carousel_phase": "SCAN"}
  }
}
```

Every payload is JSON-safe primitives only. Exceptions are never serialized; they become `code`, `message`, `stage`, `exception_type` and `exception_message` fields.

### Failure semantics

- A sensor fault is detected **before** the carousel moves, so a broken sensor never strands a sample at the scanner.
- If acquisition fails after the swing, the firmware returns the mechanism to the loader and says so — or, if it cannot, invalidates position tracking rather than reporting a position it no longer knows.
- If the ESP32 fails before RAW is obtained, the PC leaves the sample `LOADED`. No false `MEASURED` state is ever written.
- If RAW arrives but the PC-side analysis fails, the spectrum is still saved with `analysis_status: "FAILED"` and the exact error. Acquired science survives downstream software failure.

---

## BD — Science Layer

`firmware/BD/` runs on the main computer and is never uploaded to the ESP32.

```text
firmware/BD/
├─ references.json     one fixed White + one fixed Dark   PROTECTED, READ ONLY
├─ database.json       22 reference material spectra       PROTECTED, READ ONLY
├─ config.py           calibration ID, thresholds, precision
├─ database.py         reference loading, cosine similarity, comparison
└─ sample_analysis.py  validation, the two formulas, interpretation
```

> [!CAUTION]
> `database.json` and `references.json` are authoritative scientific assets. Normal operation never writes to them, and neither `References` nor `MaterialDatabase` has a save path at all. Regenerating either would silently invalidate every measurement taken against it.

### Calibration model

The competition uses **one** accepted White and **one** accepted Dark for the whole run, stored in `references.json` under a single calibration ID. The operator is never asked to measure White or Dark, and nothing at runtime can re-measure them. That is what makes two samples taken hours apart comparable.

```text
S = raw sample spectrum from the ESP32
D = fixed Dark
W = fixed White

C = S - D                 dark corrected
R = (S - D) / (W - D)     normalized reflectance
```

Applied per channel, across all 18. Negative results are **kept**: a channel reading below the stored dark reference is real information about noise or drift, and clamping it to zero would hide that. Where `W == D` the reflectance is undefined and reported as `0.0`, with the affected channels named.

### Pure functions, no shared state

```python
validate_spectrum(raw)
dark_correct(sample, dark)
normalize(sample, dark, white)
database.compare(normalized)
interpret(matches)
```

Every one takes plain dictionaries and returns plain dictionaries. No `SoilMeasurementSystem`, no object whose `sample_data` can be `None` while a spectrum exists elsewhere. The `'NoneType' object has no attribute 'sample_data'` class of bug is structurally impossible here, because there is no shared object to disagree with itself.

### Comparison

The normalized spectrum is compared against **every** material in the database by cosine similarity, sorted best first. The complete ranking is stored with the sample; the interface decides how many rows to show.

---

## Automatic Interpretation

A compact written conclusion is generated from the best match, the second-best match, and the gap between them.

```text
STRONG_REFERENCE_MATCH   clear leader
AMBIGUOUS                top two candidates too close to separate
WEAK_REFERENCE_MATCH     nothing in the database is a good fit
NO_DATABASE              no reference materials available
```

Both thresholds live in `firmware/BD/config.py` and are recorded inside each sample record, so old results stay interpretable after retuning:

```python
MIN_SIMILARITY_PERCENT   = 85.0
AMBIGUITY_MARGIN_PERCENT = 1.5
```

> [!NOTE]
> These are **heuristic competition-support thresholds, not scientifically validated criteria**. Because cosine similarity between non-negative reflectance vectors is high for almost any pair, the informative quantity is the *gap* between the top candidates rather than the absolute score. Tune both against measurements of known materials.

The wording is deliberately constrained. This module performs **comparative spectral classification** against a small reference library. It reports spectral similarity, match scores and reference matches — never chemical probability, composition percentages or identification certainty.

Example output:

```text
The measured spectrum shows the highest cosine similarity to Red Clay
(91.4%). Bentonit is the second closest reference (87.2%). Because the
score difference is only 4.2 percentage points, the classification is
considered ambiguous.
```

---

## PC — Mission Controller

`firmware/PC/` is standard CPython.

```text
firmware/PC/
├─ rover_science_client.py   operator interface and mission workflow
├─ esp32_link.py             serial transport and the hardware command API
├─ sample_store.py           persistent Sample archive
├─ requirements.txt          pyserial
└─ data/
    ├─ samples.json          index of every Sample
    └─ samples/S001.json     one complete scientific record per Sample
```

BD is imported by path relative to the application, so it runs from any working directory with no `PYTHONPATH` setup.

### Sample lifecycle

```text
EMPTY
  ↓
READY_TO_LOAD
  ↓
LOADED
  ↓
MEASURED
```

`MEASURED` is **not** `EMPTY`. The soil physically stays in the slot until the operator clears it. Clearing a physical slot frees the mechanism and keeps the scientific record; deleting a Sample is a separate, explicitly confirmed action.

The record opened by *Prepare* is the record completed by *Measure*. One coherent scientific object per sample — no second Sample ID is ever created during measurement.

### Sample record

Each record holds the Sample ID and slot, the lifecycle state, PC-supplied timestamps, mission metadata, all three spectra across all 18 channels, the sensor settings *read back from the sensor*, the calibration provenance, the ranked match against every database material, and the automatic interpretation.

Raw is primary data and is never overwritten by anything derived from it. `raw`, `dark_corrected` and `normalized` are stored separately, which is what makes offline re-analysis possible: a stored spectrum can be re-run later against different thresholds, an updated material database or a different accepted calibration, without repeating the physical acquisition.

Writes go through a temporary file and an atomic rename. A present-but-unparseable index is reported and every write refused, rather than overwritten — data that looks damaged is usually still recoverable by hand.

The PC supplies every timestamp, because the ESP32 has no reliable wall clock and never invents one:

```python
datetime.now(timezone.utc).isoformat()
```

Mission metadata is task, hypothesis, location, map point, note and photo reference. Every field is optional; an unanswered field stays `null` rather than being invented to satisfy a prompt. Photos are never stored — only a filename, ID or path.

### Operator interface

```text
============================================================
 FREYA SCIENCE MODULE
============================================================

Selected: Slot 1 / S001
State:    LOADED
Position: LOAD

Loader: Slot 1    Scanner: Slot 5

1  S001     LOADED
2  ----     EMPTY
...

[1] Choose Sample / Slot
[2] Prepare Sample [DONE]
[3] Confirm Sample Loaded [DONE]
[4] Measure Sample [AVAILABLE]
[5] Fine Carousel Alignment

[t] Tools / Records
[h] Help
[q] Exit
```

Everything secondary lives behind `[t]`: Sample Database, System Status, Re-sync Carousel, Servo / Carousel Test, Sensor Test, Clear Physical Slot.

**Sensor Test is one command.** The operator never has to decide which internal layer to test: it runs the whole chain — ESP32 sensor recovery, I2C, internal devices, configuration, illumination, a new 18-channel acquisition, then the PC's dark correction, normalization and database comparison — through the production code path, prints every stage as PASS or FAIL, and saves nothing.

For installation, upload, startup and recovery procedures, see **[firmware/OPERATIONS.md](firmware/OPERATIONS.md)**.

---

## Competition Workflow

```text
INITIAL CAROUSEL CALIBRATION   Slot 1 under the loading hole, declared once
    ↓
CHOOSE SLOT                    ESP32 brings the slot to the loading hole
    ↓
PREPARE SAMPLE                 PC creates the persistent record
    ↓
external rover arm deposits soil
    ↓
CONFIRM LOADED                 state = LOADED
    ↓
MEASURE SAMPLE
    ↓        ESP32   180° swing to the scanner
    ↓        ESP32   RAW 18-channel acquisition
    ↓        PC      receives RAW
    ↓        BD      validation, C = S - D, R = (S-D)/(W-D)
    ↓        BD      comparison against ALL materials, interpretation
    ↓        PC      completes and saves the SAME record
    ↓
state = MEASURED, soil still physically in the slot
    ↓
operator adds location, map point, photo, notes
    ↓
REPORT
```

A typical mid-run state:

```text
Slot 1   S001   MEASURED
Slot 2   S002   LOADED
Slot 3   ----   EMPTY
Slot 4   ----   EMPTY
Slot 5   S003   MEASURED
Slot 6   ----   EMPTY
Slot 7   ----   EMPTY
Slot 8   ----   EMPTY
```

---
## Spectral Material Database

A dedicated database is being developed for calibration, comparison, and future classification algorithms.

The database contains:

### Martian and Geological Analogs

- iron oxides;
- iron sulfate;
- magnesium sulfate;
- sulfur;
- gypsum;
- calcium and magnesium carbonates;
- kaolin;
- bentonite and smectite clays;
- quartz sand;
- dolomite;
- terrestrial soils and mineral mixtures.

### Optical References

- PTFE white reference;
- magnesium oxide;
- calcium carbonate;
- activated carbon;
- black iron oxide.

### Organic and Contamination References

- citric acid;
- ascorbic acid;
- tartaric acid;
- cornstarch;
- peat;
- PET plastic;
- wood ash;
- rover-related contamination materials.

A recommended database record contains:

```csv
timestamp,
sample_id,
material,
preparation,
distance_mm,
mass_g,
integration_time,
gain,
ch_410,
ch_435,
ch_460,
ch_485,
ch_510,
ch_535,
ch_560,
ch_585,
ch_610,
ch_645,
ch_680,
ch_705,
ch_730,
ch_760,
ch_810,
ch_860,
ch_900,
ch_940
```

The database is intended for training and validating comparative classification methods. It does not provide definitive chemical identification from a single spectrum.

`firmware/BD/database.json` holds **known reference materials only**. Measured competition samples are stored separately under `firmware/PC/data/`, and nothing in the system writes to the reference database.

Each measurement is compared against every entry in the database, and all results are stored ranked by descending similarity — not only the top few.

> [!IMPORTANT]
> The match percentage is **cosine similarity**, not composition and not probability.
>
> Correct: `Highest spectral similarity: Red Clay — 92.1%`
> Incorrect: `The sample is 92.1% Red Clay.`

---

## Development Process

The module was developed through the following stages:

1. **System definition**  
   Sensor, controller, actuator, communication, power, and mechanical requirements were defined.

2. **Component selection**  
   Components were selected according to electrical requirements, availability, package size, protection level, and manual assembly constraints.

3. **Schematic design**  
   Separate power, controller, and external-interface schematic sections were created in Altium Designer.

4. **PCB layout**  
   The board was routed as a two-layer PCB with a ground plane, protected power branches, mounting holes, and accessible external connectors.

5. **Manufacturing and assembly**  
   Gerber files were generated, the PCB was manufactured, and components were assembled manually.

6. **Firmware integration**  
   ESP32 firmware was developed to operate the spectral sensor and sample-handling servo.

7. **Scientific validation**  
   White-reference measurements, spectral acquisition, material selection, and database preparation were performed.

---

## Current Project Status

### Implemented

- assembled PCB V1.0;
- protected power-input architecture;
- ESP32 carrier-board integration;
- AS7265x communication;
- multispectral channel acquisition;
- MG995 continuous-rotation carousel driver;
- eight-slot carousel state model with software position tracking;
- USB-serial JSON command protocol (CP2102 console, no UART peripheral);
- three-layer software split: ESP32 hardware / BD science / PC mission control;
- single authoritative sensor lifecycle with automatic recovery;
- verified sensor configuration, read back from the registers;
- persistent sample storage on the PC;
- fixed white/dark reference calibration;
- comparison against the full reference-material database;
- automatic conservative interpretation;
- main-PC application with a reusable serial API;
- external illumination control architecture;
- rover UART hardware interface (reserved for a future revision);
- structured spectral-material database;
- mechanical mounting provisions.

### Ongoing Work

- servo calibration on the assembled mechanism (neutral pulse, direction, 45° step timing);
- final rover mechanical integration;
- measurement repeatability testing;
- distance-dependent spectral correction;
- tuning the interpretation thresholds against known materials;
- integration of the client API into the rover main software.

---

## Planned V1.1 Improvements

Future hardware revisions are planned to include:

- VL53L4CD time-of-flight distance sensor;
- automatic recording of sample height and sensor distance;
- optional load-cell support;
- external HX711 validation;
- possible NAU7802 integration;
- improved servo and logic power separation;
- additional test points;
- improved connector labelling;
- revised power protection;
- activation of the reserved UART2 rover connector as an alternative to USB;
- expanded automated calibration and data logging.

The distance sensor is considered the highest-priority measurement extension because spectral intensity depends strongly on sensor-to-sample geometry.

The load cell is planned as optional experimental metadata rather than a requirement for the current competition task.

---

# Repository Structure

```text
as7265x-soil-analysis-module/
├─ README.md
├─ Photos/
│  └─ VER1.0/
│     ├─ 01_POWER_ver1,0.png
│     ├─ 02_ESP32_ver1,0.png
│     ├─ 03_CONNECTION_ver1,0.png
│     ├─ 2D_PCB_ver1,0.png
│     ├─ 3D_PCB_ver1,0.png
│     └─ PCB_photo_ver1,0.jpg
├─ Hardware/            Altium project and manufacturing outputs
│  ├─ Altium/
│  └─ Manufacturing/
├─ firmware/
│  ├─ ESP32/            MicroPython hardware controller (the only uploaded code)
│  ├─ BD/               science layer + protected White/Dark and material database
│  ├─ PC/               mission controller, operator UI, Sample archive
│  └─ OPERATIONS.md     install, run, update, debug and recover
├─ tests/               regression suite, runs the real firmware on CPython
└─ Documentation/
```

Manufacturing outputs should be stored separately for each PCB revision:

```text
Hardware/Manufacturing/
├─ V1.0/
└─ V1.1/
```

---

# Documentation

Project documentation includes:

- PCB architecture and component models;
- consolidated BOM;
- component purchasing list;
- spectral material database;
- PCB schematic and layout images;
- manufacturing files;
- firmware and measurement data.

Recommended repository links:

- [Operations runbook](firmware/OPERATIONS.md) — install, upload, run, debug, recover
- [PCB BOM and component models](Documentation/Mars%20Rover%20AS7265x%20Soil%20Analyzer%20PCB%20-%20BOM%20_%20Component%20Models%20v0.1.pdf)
- [PCB purchase list](Documentation/Purchase%20List%20-%20Mars%20Rover%20AS7265x%20PCB.docx)
- [Spectral material database](Documentation/Spectral%20Material%20Database.docx)

Document roles are kept separate on purpose:

| Document | Answers |
|---|---|
| `README.md` | what the project is, how it is built, what the science means |
| `firmware/OPERATIONS.md` | how to install, run, update, debug and recover it |
| source code | how the implementation works |

---

## Long-Term Development

After the current competition system is completed, possible development directions include:

- acquire repeatable 18-channel VIS-NIR spectra;
- normalize measurements against White/Dark references;
- compare spectral shape with a controlled material library;
- preserve both RAW and processed data.

It does **not** claim exact chemical composition, concentration or laboratory-grade mineral identification.

---

## License

No open-source license has currently been selected.

Until a `LICENSE` file is added, the project should be treated as **all rights reserved**.

---

## Acknowledgements

Developed as part of the **Brno Mars Rover / Freya** student rover project.

---

## Author

**Maksym Pleshyvtsev**  
Electronics and Communication Technologies Student  
Brno University of Technology — FEKT
