# Freya AS7265x Science Module — Handoff

Current development state as of 2026-08-11, after the architecture
refactor. For what the project *is*, read `README.md`. For how to
install, run, upload, debug and recover it, read
`firmware/OPERATIONS.md`. This file is only the short "where we are
right now" note.

---

## Where the work stands

The software has been reorganized into three layers with strict
boundaries:

```text
firmware/ESP32/   hardware only: carousel, AS7265x, RAW spectra
firmware/BD/      science only:  fixed White/Dark, database, formulas
firmware/PC/      mission only:  workflow, Sample lifecycle, archive
```

The ESP32 no longer performs any scientific processing, loads no JSON
data files, and stores no sample history. It moves hardware, reads
hardware and reports hardware. Everything else moved to the PC.

278 automated checks pass across four suites, with no board attached.

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
| `SERVO_STOP_US` | 1500 | trim until no creep |
| `SERVO_CW_US` / `SERVO_CCW_US` | 1600 / 1400 | direction check |
| `NEXT_SLOT_CW_MS` | 533 | one slot forward lands centred |
| `NEXT_SLOT_CCW_MS` | 600 | one slot back — **tune separately** |
| `LOAD_TO_SCAN_CW_MS` | 2400 | 180° must reach the scanner |
| `SCAN_TO_LOAD_CCW_MS` | 2400 | 180° must return |
| `CW_MS_PER_DEGREE` / `CCW_MS_PER_DEGREE` | 13.3 | request ±5°, measure travel |
| `CAROUSEL_FORWARD_DIRECTION` | `"cw"` | if choosing the next slot walks the numbering backwards, flip it |

   One slot is **45° in both directions** (`SLOT_STEP_DEG`); only the
   timing differs per direction. The half turn is **independently
   calibrated** — never four adjacent moves.

4. **A real Sample end to end.** Choose → Prepare → Confirm → Measure,
   then check the saved record in `firmware/PC/data/`.

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
firmware/BD/database.json     22 reference materials    READ ONLY
firmware/BD/references.json   fixed White + Dark        READ ONLY
Hardware/                     Altium design             NEVER TOUCHED
as7265x-soil-analysis-module.PrjPcb                     NEVER TOUCHED
```

`tests/test_science.py` checks the SHA256 of both JSON files before and
after every run. A pre-refactor copy is in `BD_BACKUP_BEFORE_REFACTOR/`;
it can be deleted once the hardware run confirms everything works.

---

## Verification

```powershell
cd C:\Users\Public\Documents\Altium\as7265x-soil-analysis-module\tests
py test_esp32.py
py test_science.py
py test_pc.py
py test_integration.py
```

They stub `machine`, run the real firmware modules on CPython against a
fake AS7265x that speaks the actual virtual-register protocol, exercise
the real science layer against the real protected data, and drive the
real client through a loopback that runs the real firmware dispatcher.
