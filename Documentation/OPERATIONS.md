# Operations

How to run, deploy and diagnose the Freya science module.

Everything here assumes you start from the repository root:

```powershell
cd C:\Users\Public\Documents\Altium\as7265x-soil-analysis-module
```

That matters for the *script path only*. Every path the programs
themselves use is resolved from `__file__`, so what they read and write
does not depend on the working directory — but `py rover_science_client.py`
from somewhere else is a missing file, not a missing COM port.

---

## 1. Requirements

```powershell
py -m pip install -r firmware\PC\requirements.txt
```

pyserial for the operator client; mpremote and mpy-cross for
deployment. Nothing else.

`mpy-cross` MUST match the MicroPython on the board. The board runs
v1.28.0 and loads `.mpy` version 6.3, which `mpy-cross==1.28.0.post2`
emits. A mismatch does not warn - the module fails to import.

---

## 2. Run the operator client

```powershell
py firmware\PC\rover_science_client.py --port COM4
```

Opening the port does **not** reset the board, so the client can be
restarted without losing a carousel position the operator synchronized
by hand.

### The main menu

```
0. Initial Carousel Calibration / Re-sync
1. Choose Sample / Slot
2. Prepare Sample
3. Confirm Sample Loaded
4. Measure Sample
5. Fine Carousel Alignment
t. Tools / Records
h. Help
q. Exit
```

Slots are **1, 2, 3 and 4**. Nothing else is valid.

Option `[0]` is not optional after a reboot. The carousel has no index
mark, so software cannot discover which physical slot is at the loading
hole — the operator connects the servo and declares it once.

---

## 3. Deploy the ESP32

One command:

```powershell
py firmware\tools\device.py deploy --port COM4 --clean
```

It refuses to report success until the board has been reset, has booted
on its own, and has answered a ping:

```
SOURCES       PASS   7 files
BUILD         PASS   5 modules compiled, 268453 bytes of source -> 66983 bytes on device
PORT          PASS   COM4
CLEAN         PASS   removed boot.py, carousel.mpy, config.mpy, ...
UPLOAD        PASS   7 files
MANIFEST      PASS   7 files, nothing else
CONTENT       PASS   sha256 matches for all 7 files
IMPORTS       PASS   config, sensor, servo, carousel, protocol
RESET         PASS   RTS pulse, DTR low
PING          PASS   freya-science-module 6.0.0 (protocol 2)
GET_STATUS    PASS   25 commands
PORT RELEASE  PASS   COM4
```

Other commands:

```powershell
py firmware\tools\device.py status --port COM4    # what is on the device now
py firmware\tools\device.py verify --port COM4    # check without changing
py firmware\tools\device.py clean  --port COM4    # remove every user file
py firmware\tools\device.py reset  --port COM4    # reset and confirm it serves
```

### What `--clean` does, and why

MicroPython's filesystem persists across deployments. A file the
firmware no longer imports stays on the device forever, and the day
somebody adds an import with the same name it comes back from the dead.
This project has already lived through exactly that: the device carried
`drivers/`, `control/` and `protocol/` packages from an architecture
that no longer exists.

`--clean` removes every user file before uploading, so what is on the
device is the manifest and nothing else. It removes **user files only** —
the MicroPython runtime lives in flash, not in the filesystem, and is
never touched.

### The firmware is compiled before it is uploaded

The device receives **bytecode**, not source. `config`, `sensor`,
`servo`, `carousel` and `protocol` are cross-compiled to `.mpy` by
`mpy-cross`; only `main.py` and `boot.py` go up as source, because the
MicroPython runtime opens those two by name at startup.

This is not an optimization. MicroPython compiles a `.py` on the device
at import, and the parse tree is the largest transient allocation the
board makes. At roughly 7500 lines it no longer fits:

```
IMPORTS       FAIL   MemoryError: memory allocation failed,
                     allocating 1196 bytes
```

Compiling on the device also leaves the heap in pieces, and a response
of a few kilobytes needs its bytes contiguous. Measured on the board:

```
deployed as .py     94784 B free, largest single block   8 kB
deployed as .mpy    70544 B free, largest single block  32 kB
```

With the sources deployed, every WHITE/UV/IR triad after the first came
back `RESPONSE_TOO_LARGE` - after spending its full 24 seconds reading
the sensor.

**Nothing is generated into the repository.** The build directory is
temporary and per-run, and the `.py` files stay the only source of
truth. `mpy-cross` output is deterministic, so `verify` rebuilds and
compares hashes without keeping an artefact.

If the board boots with

```
MemoryError: memory allocation failed, allocating 4294966961 bytes
```

a `.mpy` arrived truncated - that is a length field read off the end of
a short file. `mpremote fs cp` has been observed to do this silently,
with exit code 0, so `upload` now hashes every file on the device as it
lands and sends it again if it does not match.

### The manifest is authoritative

`tools/device.py` uploads exactly the seven files named in
`ESP32_FILES`, built from the seven sources in `ESP32_SOURCES`. It does
not scan the directory: a scratch file left in `ESP32/` would otherwise
become part of the firmware, and what the device runs would depend on
the state of somebody's working copy. A `.py` file in `ESP32/` that is
not in the manifest is reported and not uploaded.

### Verifying a deployment

`deploy` and `verify` both check that the device's own SHA256 of every
file matches the local one. A copy that returned zero proves the
command ran, not that the file arrived.

---

## 4. Diagnose the link

### Verbose transport

```powershell
py firmware\PC\rover_science_client.py --port COM4 --command ping --verbose
```

```
PORT OPEN        COM4 at 115200 baud, dtr=False rts=False
TX JSON          {"request_id": "1", "cmd": "ping", ...}
RX JSON          {"data": {...}, "ok": true, "request_id": "1", ...}
PORT CLOSED      COM4
```

Also emitted where they apply: `RX NON-JSON`, `RX ECHO`, `RX SALVAGED`,
`RX DAMAGED`, `RESET`.

### PORT_BUSY

```
PORT_BUSY: COM4 exists but is already open in another program.
```

Something else holds the handle — another client, a serial monitor, an
`mpremote` session, an editor's terminal. Find it rather than killing
Python at large:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'rover_science_client|mpremote|COM4' } |
  Select-Object ProcessId, ParentProcessId, Name, CreationDate, CommandLine
```

**A `py.exe` with a `python.exe` child is ONE application**, not two
clients. `py.exe` is the launcher; check `ParentProcessId` before
concluding anything.

Never use `taskkill /F /IM python.exe`. It ends unrelated work.

### PROTOCOL_TIMEOUT

```
PROTOCOL_TIMEOUT: COM4 opened, but the science module did not answer
a ping within 15 s.
```

The port is fine. The firmware is not answering. In order:

1. `py firmware\tools\device.py status --port COM4` — is the manifest there?
2. `py firmware\tools\device.py reset --port COM4` — does it come back?
3. Run with `--verbose` and read what arrives instead of a frame. A
   traceback, a boot banner and REPL text are each reported verbatim.

### DEVICE_AT_REPL

```
DEVICE_AT_REPL: the board is at the MicroPython REPL prompt, not
running the firmware.
```

**Any `mpremote` command leaves the board here.** It interrupts
whatever is running to take control and does not restart `main.py`.
That is normal and is not damage:

```powershell
py firmware\tools\device.py reset --port COM4
```

### PORT_NOT_FOUND

The board is not plugged in, or its USB bridge has not enumerated.
Check the cable first, then:

```powershell
py -c "from serial.tools import list_ports; print([p.device for p in list_ports.comports()])"
```

---

## 5. The manual REPL

Only for hand debugging, and never with the client attached — `stdout`
IS the protocol stream.

```powershell
py -m mpremote connect COM4 repl
```

Leave it with **Ctrl-]**. Then put the board back to work:

```powershell
py firmware\tools\device.py reset --port COM4
```

Inside the REPL the running instance is reachable:

```python
import main
main.hardware.carousel.status()
main.hardware.sensor.state()
```

---

## 6. Confirm the port was released

Immediately after the client exits:

```powershell
py -m mpremote connect COM4 fs ls :
```

Success means the port is free. If it is not, find the holder with the
`Get-CimInstance` query above — and note that this command itself
leaves the board at the REPL, so reset afterwards.

---

## 7. Where the scientific records are

```
firmware/BD/samples/samples.json
```

That is the run's only irreplaceable output and it is **not in version
control** — git cannot restore it. Copy it outside the repository
before any bulk file operation.

Beside it, `samples.schemaN.backup.json` is the pre-migration archive,
written once by the schema migration. It is also the only copy of what
it holds.

Everything else under `BD/` is tracked: calibration, DB1, DB2, DB3, the
training data and the model registry.

---

## 8. Run the tests

```powershell
py firmware\Tests\run_all.py
```

Five suites, ~707 checks, no hardware required:

```
test_architecture.py   domain boundaries and obsolete architecture
test_esp32.py          protocol, drivers, carousel, on fake hardware
test_science.py        formulas, comparison, Decision Model
test_data.py           record model, RAW immutability, provenance
test_pc.py             serial lifecycle, error kinds, measurement order
```

One suite at a time:

```powershell
py firmware\Tests\run_all.py esp32
```

The suites run the **real** firmware modules on CPython against fakes
that speak the actual I²C and ST3215 protocols, so a driver bug shows
up as a failing check rather than as a passing check against a stub
that shares the bug.

---

## 9. Hardware smoke test

After a deployment, with no sample loaded:

```powershell
py firmware\tools\device.py deploy --port COM4 --clean
py firmware\PC\rover_science_client.py --port COM4 --command get_status
```

`get_status` reports the sensor state, whether the servo is connected
and whether the carousel position is trusted. All three may legitimately
be negative on a bench with nothing attached — and `ping` still works,
which is the point.

**Movement is a separate, explicit test.** Nothing in a deployment
turns the carousel: it may hold samples or be mechanically constrained.
Use `[0] Carousel Setup` or Tools → Servo / Carousel Tools when you
intend it.
