# Freya AS7265x Science Module — Handoff

Current development state as of 2026-08-11, after the architecture
refactor. For what the project *is*, read `README.md`. For how to
install, run, upload, debug and recover it, read
`firmware/OPERATIONS.md`. This file is only the short "where we are
right now" note.

---

## Where the work stands

**Acquisition is Mode 3 one-shot, under three lamps.** Every spectrum is
armed after the illumination is on and settled, so the spectrum and the
lamp that produced it are guaranteed to belong together. A measurement
takes WHITE, UV and IR — 18 × 3 = **54 spectral features** — repeated,
with every individual reading returned to the PC.

**Two calibrations, permanently apart.** The legacy White/Dark in
`references.json` is what `database.json` was normalized against and is
the ONLY thing ever compared with it. A new full Dark + WHITE/UV/IR
calibration lives in `BD/calibrations/` and is used for the scientific
record and quality control. Every measurement is normalized both ways —
which is exactly why **the material library never needs remeasuring**.

**Quality control runs before classification.** Illumination strength,
finite channels, reflectance sanity, detector-seam continuity,
repeatability and an optional distance gate. A FAIL means no
identification is reported, only the stored spectra.

**Three comparison metrics, not one.** Cosine sees shape and is blind to
brightness — a dim and a bright spectrum of the same shape are a perfect
100% match to it. RMSE keeps the magnitude; Pearson correlates the
variation. Their ranks are combined, and disagreement is reported as
`METRICS_DISAGREE` rather than hidden.

**Run `[5] Sensor Test / Calibration -> [3] Full Spectral Calibration`
after flashing.** Until then the active calibration is MISSING, the UI
says so, and UV/IR reflectance is not computed. The legacy comparison
still works.

**Carousel: 4 slots, 90° apart** (was 8 slots at 45°). The scanner is
still 180° from the loader, which is now **two** slots: Slot 1 at the
loader means Slot 3 at the scanner.

**Measure Sample now returns home.** The sequence is 180° out → acquire
→ 180° back, so a successful measurement leaves the sample at exactly
the position it started from. If the return fails, the measurement is
still kept and `position_valid` goes to `False` — acquisition and
mechanical recovery are separate outcomes.

**Tools → Sync ESP32 Samples to PC** copies raw acquisitions the ESP32
is still holding in RAM into the PC archive: duplicate-safe, idempotent,
and it never deletes the device copy.

**The Sample archive is `firmware/BD/samples.json`** — one file beside
`database.json` and `references.json`, holding COMPLETE records: all
three 18-channel spectra, the White and Dark actually used, the
comparison against every material, sensor settings, timestamps and
metadata. The retired `firmware/PC/data/` layout is migrated
automatically on first start.

The software is organized into three layers with strict boundaries:

```text
firmware/ESP32/   hardware only: carousel, AS7265x, RAW spectra
firmware/BD/      science only:  fixed White/Dark, database, formulas
firmware/PC/      mission only:  workflow, Sample lifecycle, archive
```

The ESP32 no longer performs any scientific processing and loads no JSON
data files. It moves hardware, reads hardware and reports hardware. The
one thing it now keeps is the last raw acquisition per slot, in RAM
only, so the PC can pull back a result it lost; it is an acquisition
buffer, not a science archive, and it is empty after a reset.

630 automated checks pass across five suites, with no board attached.

---

## The bug that motivated this, and what fixed it

The module reported `SENSOR_NOT_INITIALIZED / AS7265X not found on I2C`
while its own diagnostics, in the same session, successfully scanned
`0x49`, read physical and virtual registers, controlled the LED and
acquired all 18 channels.

Both statements were true, because there were **two sensor worlds**:

- `ScienceModule.init_sensor()` ran a single `AS7265X_Driver()`
  construction at boot. That constructor scanned the bus exactly once
  and raised on the first miss. The failure was latched into
  `self.driver = None` / `self.sensor = None`, and every later command
  checked that stale flag instead of retrying.
- `sensor_diag.run_full_diagnostics()` built its **own** `I2C` object
  and its own `StrictAS7265X`, succeeded, printed the result, and threw
  the working objects away.

That also explains the configuration mismatch: the diagnostic path only
*read* registers, and the only code that *wrote* the settings lived
behind the constructor that had already failed. So the sensor sat at its
power-on defaults (20 cycles, 3.7x) while `sample_analysis.sensor_settings()`
reported the intended values straight from `config.py` — it never asked
the hardware.

The fix is `as7265x.SensorRuntime`:

- one driver, one I2C object, one lifecycle: `ensure_ready()`;
- bounded retries at boot **and** on every later sensor command, so a
  boot failure is never permanent;
- configuration is written and then **read back and verified**, raising
  `SENSOR_CONFIG_NOT_APPLIED` with the actual register contents if the
  sensor did not accept it;
- every response carries the settings that were read back, not the ones
  that were requested;
- `StrictAS7265X`, `SoilMeasurementSystem` and `sensor_diag.py` are gone.

The related `'NoneType' object has no attribute 'sample_data'` is now
structurally impossible: BD is pure functions over plain dictionaries,
with no long-lived measurement object to disagree with itself.

---

## What still needs real hardware

Everything below is verified in software only. None of it has been run
on the physical module since the refactor.

1. **Clean ESP32 install.** The old flat layout is still on the board.
   Follow "Clean ESP32 installation" in `firmware/OPERATIONS.md`.
2. **Sensor Test on the real sensor.** Confirm the read-back reports
   100 cycles and 16x. If it does not, the sensor is genuinely running
   the wrong settings — fix `firmware/ESP32/config.py`, do **not**
   re-measure White/Dark to compensate.
3. **Servo calibration.** Every timing constant in
   `firmware/ESP32/config.py` is a consistent starting point, not a
   measured value:

| Constant | Ships as | Tune by |
|---|---|---|
| `SERVO_STOP_US` | 1500 | trim in 1–2 µs steps until no creep — often *not* 1500 |
| `SERVO_CW_US` / `SERVO_CCW_US` | 1555 / 1445 | neutral ±55 µs; find the smallest offset that still starts reliably |
| `NEXT_SLOT_CW_MS` | 2200 | one 90° slot forward lands centred |
| `NEXT_SLOT_CCW_MS` | 2200 | one 90° slot back — **tune separately** |
| `LOAD_TO_SCAN_CW_MS` | 4300 | 180° must reach the scanner |
| `SCAN_TO_LOAD_CCW_MS` | 4300 | 180° must return |
| `CW_MS_PER_DEGREE` / `CCW_MS_PER_DEGREE` | 24.4 | request ±5°, measure travel |
| `SERVO_SETTLE_MS` | 1200 | raise until the mechanism stops ringing |
| `SERVO_APPROACH_MS` | 0 | slow final approach, off unless overshoot is seen |
| `SERVO_START_KICK_MS` | 0 | on only if the servo sometimes fails to start |
| `SERVO_BRAKE_MS` | 0 | reverse braking, on only if measurably better |
| `CAROUSEL_FORWARD_DIRECTION` | `"cw"` | if choosing the next slot walks the numbering backwards, flip it |

   **Not one of these has been measured on the mechanism.** Precision is
   prioritised over speed: the direction pulses sit at neutral ±55 µs
   (was ±100), and every timing was scaled to match.

   Use **Tools → Servo / Carousel Test → [5] Calibration**. It tunes the
   values in RAM and prints the block to paste into `config.py`. After a
   test move it asks for the **observed angular error** and re-times the
   move for you:

   ```text
   t_new = t_old × target / (target + error)
   ```

   which converges in two or three iterations instead of guesswork. The
   `[o]` out-and-back test is the important one: a measurement is 180°
   out plus 180° back, so optimise for the smallest error after the
   **pair**. The full procedure is in `firmware/OPERATIONS.md`.

   One slot is **90° in both directions** (`SLOT_STEP_DEG`); only the
   timing differs per direction. The half turn is **independently
   calibrated** — never two adjacent moves.

4. **A real Sample end to end.** Choose → Prepare → Confirm → Measure,
   then check the saved record in `firmware/BD/samples.json`.

Interpretation thresholds (`MIN_SIMILARITY_PERCENT = 85.0`,
`AMBIGUITY_MARGIN_PERCENT = 1.5`, in `firmware/BD/config.py`) are
heuristic competition-support values, not validated science. Tune them
against measurements of known materials.

---

## MicroPython gotchas found the hard way

Do not reintroduce these.

1. **`Exception.__init__(self, msg)` does not work.** It raises
   `type object 'Exception' has no attribute '__init__'`, which masked
   every sensor error as `INTERNAL_ERROR`. Use `super().__init__(msg)`.
   `tests/test_esp32.py` fails the build if it comes back.
2. **`sys.stdout.write()` is non-blocking and returns a short count.**
   A single multi-kilobyte write drops its tail silently and the PC waits
   forever. `_write_all()` loops on the return value in 256-byte chunks;
   non-ASCII is escaped to `\uXXXX` first so byte and character counts
   match.
3. **stdout IS the protocol stream.** No `print()` outside JSON. All
   diagnostics go through `_debug()`, gated by `config.DEBUG = False`.
4. **A missing module drops the board to the REPL**, where it echoes
   commands back as Python dict reprs. That is the fingerprint to look
   for when the PC reports non-JSON output.
5. No `os.makedirs`, no `traceback`, no f-strings in firmware.

---

## Protected data

```text
firmware/BD/database.json                       22 reference materials  READ ONLY
firmware/BD/references.json                     fixed White + Dark      READ ONLY
Hardware/                                       Altium design           NEVER TOUCHED
Hardware/as7265x-soil-analysis-module.PrjPcb    Altium project          NEVER TOUCHED
```

The Altium project lives **inside `Hardware/`**, not at the repository
root. An older commit still has a root-level copy; do not restore it.

`tests/test_science.py` checks the SHA256 of both JSON files before and
after every run. They can also be verified straight against git:

```powershell
git rev-parse "HEAD:firmware/BD/database.json"
git hash-object firmware/BD/database.json
```

---

## Verification

```powershell
cd C:\Users\Public\Documents\Altium\as7265x-soil-analysis-module\tests
py test_esp32.py
py test_science.py
py test_calibration.py
py test_pc.py
py test_integration.py
```

They stub `machine`, run the real firmware modules on CPython against a
fake AS7265x that speaks the actual virtual-register protocol, exercise
the real science layer against the real protected data, and drive the
real client through a loopback that runs the real firmware dispatcher.
