# Freya AS7265x Science Module — Handoff

State of the project as of 2026-08-11. Paste the "Prompt for a new
chat" section below into a fresh conversation to continue.

---

## THE ONE OPEN PROBLEM

**The AS7265x does not answer on I2C.** `AS7265X_Driver.__init__` raises
`OSError("AS7265X not found on I2C")` at boot, so `self.sensor is None`
and both Measure Sample and Sensor Testing correctly refuse.

Every software fault that was masking this has been fixed. The next step
is on real hardware, not in code:

```text
Run the client → [t] Tools → [6] Sensor Testing → [2] I2C Scan
```

That prints the actual bus scan. Three outcomes, three conclusions:

| Result | Meaning |
|---|---|
| `none` detected | nothing on the bus: SDA/SCL swapped, missing pull-ups, or sensor 3V3 not reaching the module |
| other addresses, no `0x49` | bus wiring fine; wrong device or wrong address |
| `0x49` found, `AS7265X_SLAVES_NOT_DETECTED` | master answers, internal VIS/NIR slaves do not — fault on the sensor module itself |

Report that one line and the next step follows from it.

---

## Architecture (settled — do not redesign)

**ESP32 = passive instrument. PC = mission controller.** MicroPython runs
`main.py` at boot but performs no automatic movement or measurement.

**Transport:** newline-delimited JSON over the USB/CP2102 serial console
(`sys.stdin` / `sys.stdout`). The PCB UART2 connector on GPIO16/17 is
**unused and reserved** — no `machine.UART` is ever created.

**Three JSON files, three roles:**

```text
references.json   one fixed white + one fixed dark      READ ONLY
database.json     reference material spectra (22)       READ ONLY
samples.json      carousel samples we measure           the only writable one
```

`samples.json` is an index; each full record lives in `samples/<ID>.json`
(splitting keeps peak RAM low). Enforced by test section 34.

**Carousel:** 8 slots, 45° spacing, loader and scanner 180° (4 slots)
apart. Slot 1 at the loader is the operator-declared origin. No encoder —
position is software-tracked and forgotten on reboot.

**Three separate position concepts:** `current_load_slot`,
`current_scan_slot`, `selected_slot`. `carousel_phase` (LOAD/SCAN) is
*derived*, never stored, so it cannot drift.

**Sample lifecycle:** `prepare_load` opens the persistent record →
`confirm_loaded` updates it → `measure_sample` completes the **same**
record. One coherent scientific object per sample.

**One shared acquisition core:** `acquire_science_measurement()`. Both
Measure Sample and Sensor Testing call it. Only two `take_sample()` call
sites exist in the whole firmware (the core, plus the deliberately
isolated `raw_measurement`).

---

## MicroPython gotchas found the hard way

These cost real debugging time. Do not reintroduce them.

1. **`Exception.__init__(self, msg)` does not work.** Raises
   `type object 'Exception' has no attribute '__init__'`. Use
   `super().__init__(msg)`. This masked every sensor error as
   `INTERNAL_ERROR` with `stage: unknown`.
2. **`sys.stdout.write()` is non-blocking and returns a short count.**
   A single 7 kB write drops its tail silently → the PC waits forever.
   Fixed with `_write_all()`, which loops on the return value in 256-byte
   chunks. Non-ASCII is escaped to `\uXXXX` first so byte and character
   counts match.
3. **stdout IS the protocol stream.** No `print()` outside JSON. All
   diagnostics go through `_debug()`, gated by `config.DEBUG = False`.
4. **A missing module drops the board to the REPL**, where it echoes
   commands back as Python dict reprs. `import sensor_diag` is therefore
   wrapped in try/except.
5. No `os.makedirs`, no `traceback` in firmware.

---

## Calibration constants still needing real-hardware tuning

All in `firmware/config.py`. Shipped values are consistent starting
points, **not** measured.

| Constant | Ships as | Tune by |
|---|---|---|
| `SERVO_STOP_US` | 1500 | trim until no creep |
| `SERVO_CW_US` / `SERVO_CCW_US` | 1600 / 1400 | direction check |
| `NEXT_SLOT_CW_MS` | 533 | one slot forward lands centred |
| `NEXT_SLOT_CCW_MS` | 600 | one slot back — **tune separately** |
| `LOAD_TO_SCAN_CW_MS` | 2400 | 180° must reach the scanner |
| `SCAN_TO_LOAD_CCW_MS` | 2400 | 180° must return |
| `CW_MS_PER_DEGREE` / `CCW_` | 13.3 | fine adjust ±5°, measure travel |
| `CAROUSEL_FORWARD_DIRECTION` | `"cw"` | if choosing the next slot walks numbering backwards, flip it |

One slot = **45° in both directions** (`SLOT_STEP_DEG`). Only the timing
differs per direction. The half turn is **independently calibrated** —
never four adjacent moves.

Interpretation thresholds (`MIN_SIMILARITY_PERCENT = 85.0`,
`AMBIGUITY_MARGIN_PERCENT = 1.5`) are heuristic competition-support
values, not validated science.

---

## Verification

564 automated checks, all passing. They stub `machine`, run the real
firmware modules on CPython with a deterministic fake sensor, and drive
the real client menus through a loopback serial.

```bash
cd tests
py test_firmware.py      # 420 checks
py test_host.py          # 101 checks
py test_db_manager.py    #  43 checks
py audit_json.py         # response JSON-safety + sizes
py audit_attrs.py        # attribute resolution audit
```

Run all of these after any change. They caught several real bugs,
including the corrupted-database data-loss path.

---

## Environment

- Windows, **PowerShell** — `&&` is a parser error. Use `;` or separate
  lines.
- Commands run from `firmware\`, so paths have no `firmware/` prefix.
- `sensor_diag.py` is a **new file** — it must be uploaded or `main.py`
  falls back to REPL (now guarded, but diagnostics are then unavailable).

```powershell
py -m mpremote connect COM4 fs cp main.py config.py as7265x.py mg995.py carousel.py database.py database.json references.json sample_store.py sample_analysis.py sensor_diag.py boot.py :
```

```powershell
py -m mpremote connect COM4 reset
```

```powershell
py ..\host\rover_science_client.py --port COM4
```

Never upload `samples.json` or `samples/` — that is live competition data
created on the device.

---

## Operator workflow

```text
0. Initial Carousel Calibration   (once, Slot 1 under the loading hole)
   ↓
1. Choose Sample / Slot     2. Prepare Sample
3. Confirm Sample Loaded    4. Measure Sample
5. Fine Carousel Alignment  (only if alignment is off)
```

`[t] Tools / Records` holds everything secondary: Sample Database,
System Status, Re-sync, Servo Diagnostics, Sensor Testing, Clear Slot.

Measure leaves the sample at the SCANNER on purpose; Choose Slot restores
the loading orientation automatically on the next sample.

---

# Prompt for a new chat

> I am continuing work on the Freya AS7265x soil-analysis module at
> `C:\Users\Public\Documents\Altium\as7265x-soil-analysis-module`.
>
> **Read `HANDOFF.md` in the repo root first** — it has the full state,
> the settled architecture, the MicroPython gotchas and the calibration
> constants. Then read the current local files; the GitHub copy may be
> older.
>
> The project works end to end in software: 564 automated checks in
> `tests/` all pass. Do not redesign the UI, carousel model, sample state
> machine, database architecture, fixed references, or the USB protocol.
>
> **The one open problem is hardware:** the AS7265x does not answer on
> I2C at `0x49`, so the sensor object is None at boot and both Measure
> Sample and Sensor Testing correctly refuse. Every software fault that
> was masking this has already been fixed.
>
> My next action is to run on the real board:
> `[t] Tools → [6] Sensor Testing → [2] I2C Scan`
> and report the detected addresses.
>
> Constraints: Windows PowerShell (no `&&`), never write to
> `references.json` or `database.json`, never commit or push to GitHub,
> and run the `tests/` harness after any change.
>
> Here is what the I2C Scan printed:
>
> ```
> <paste the output here>
> ```
