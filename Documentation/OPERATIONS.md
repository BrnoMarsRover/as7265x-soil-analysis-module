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

### 0. On Linux, read every command below through this table

The main computer is Linux; the bench machine is Windows. The programs
are the same on both — every path they use is built with `pathlib`, and
nothing in the project shells out to a platform command. Only the way
you *type* the command changes:

| Windows (as written below) | Linux |
| --- | --- |
| `py` | `python3` |
| `firmware\PC\...` | `firmware/PC/...` (forward slashes work on Windows too) |
| `COM4` | `/dev/ttyUSB0` — or `/dev/ttyACM0`, and the number moves between plug-ins |
| `Get-CimInstance ...` (find the holder of a port) | `sudo fuser -v /dev/ttyUSB0`, or `lsof /dev/ttyUSB0` |

```bash
cd ~/development/maksym/as7265x-soil-analysis-module
python3 -m pip install -r firmware/PC/requirements.txt
python3 firmware/PC/rover_science_client.py --port /dev/ttyUSB0
```

**One Linux-only step, once per account.** Serial devices belong to a
group — `dialout` on Debian and Ubuntu, `uucp` on Arch — and an account
outside it gets a permission error on open that has nothing to do with
the board:

```bash
sudo usermod -aG dialout $USER
```

Then log out and back in: group membership is read at login, so a
freshly opened terminal in the same session still fails. `id -nG`
confirms it. See `PORT_DENIED` in §4.

`device.py` needs no `--port` on Linux when exactly one USB-serial
device is attached — it finds it. With none or several attached it says
so and lists what it saw, rather than guessing which board to reflash.

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

On Linux the same list, plus what the kernel thought when the board was
plugged in:

```bash
ls -l /dev/ttyUSB* /dev/ttyACM*
```

### PORT_CLOSED

```
PORT_CLOSED: The connection to the science module is closed
(/dev/ttyUSB0 was lost during a get_status command).
Reconnect the module before running get_status.
```

The link is not open — almost always because it was **lost**, not
because anyone closed it. The message names the command that lost it.

Every hardware action is refused with this until you reconnect;
software-only screens (Records, Help) keep working. Restart the client
with the module plugged in:

```bash
python3 firmware/PC/rover_science_client.py --port /dev/ttyUSB0
```

**After reconnecting, the carousel origin is gone.** That is deliberate:
the servo may have been turned by hand while the link was down, and a
remembered slot number is indistinguishable from a measured one once it
is on screen. Connect the servo and re-declare Slot 1.

### PORT_DENIED

```
PORT_DENIED: /dev/ttyUSB0 exists but this account may not open it.
```

**Linux only, and it is not a fault in the board.** The device node is
there; the account is not in the group that owns it. This is a
different failure from `PORT_BUSY` and needs a different action —
nothing is holding the port, so hunting for a process finds nothing.

```bash
ls -l /dev/ttyUSB0          # the group in column 4 is the one you need
sudo usermod -aG dialout $USER
```

Log out and back in afterwards. Group membership is established at
login, so the change does not reach a terminal that is already open.

`sudo` on the client is not the fix. It works once and then writes
`firmware/BD/samples/` as root, which is the one directory in this
project that git cannot restore.

---

### SERVO_POSITION_MISMATCH — the carousel moved and the measurement stopped

```
MEASUREMENT FAILED - MOVE_TO_SCANNER

Stage:      failed moving the sample to the scanner
Move:       half turn cw
Encoder:    2048 -> 2050 cnt (+0.18 deg)
Expected:   4096 cnt
Error:      2046 cnt (tolerance 15)
Carousel:   POSITION UNKNOWN - the encoder measured travel
Spectrum:   NOT ACQUIRED - none was saved
Sample:     s0007 remains LOADED
Recorded:   M001 (FAILED)
Action:     inspect the mechanism and re-sync
```

**Read the two numbers.** "Commanded N counts, the encoder moved M" is
the whole diagnosis:

| | |
| --- | --- |
| M close to N | the servo went where it was told and stopped slightly out — suspect the tolerance or mechanical binding |
| M near zero, and the carousel visibly turned | **the encoder is not tracking the mechanism.** This happened on the bench on 2026-08-24 and is open hardware item **H-002** in `firmware/Tests/hardware/HARDWARE_VERIFICATION_PLAN.md`. Check the servo's operating mode, and whether there is a reduction between servo and carousel |
| M near zero, and nothing moved | the servo is not acting on the goal — check power at the driver board |

No spectrum was acquired and none was saved. The carousel position is
invalidated, and the sample is somewhere between the loader and the
scanner: **look at the mechanism first**, then re-sync.

**The servo has not disconnected.** A position mismatch is a mechanism
or feedback fault with the link answering normally, so the recovery is
`Re-sync carousel`, not `Connect servo`. Telling an operator to
reconnect working hardware was itself a defect, fixed on 2026-08-27.

### You stay in the failed measurement

The client does not drop you back at the main menu. It shows
**MEASUREMENT RECOVERY**, which keeps the sample, the slot, the stage
and what was lost on screen until you choose to leave:

```
MEASUREMENT RECOVERY
Sample:     s0007 / Slot 1 / LOADED
Stage:      MOVE_TO_SCANNER
Servo:      ONLINE
Carousel:   POSITION UNKNOWN
Sensor:     READY
Spectrum:   NOT ACQUIRED - none was saved
Recorded:   M001 (FAILED)

[1] Refresh hardware state
[2] Re-sync carousel (nothing moves)
[3] Servo diagnostics
[4] Sensor diagnostics
[0] Abort to the main menu
```

The options follow the state: `[2]` offers **Carousel Setup** instead
when the servo is genuinely not connected, and a `[5] Back to this
sample` appears once the position is trustworthy again — which returns
you to the same slot, still loaded, ready to measure.

**There is deliberately no "retry the movement".** Carousel movement is
relative, so a movement whose acknowledgement was lost may already have
happened; resending it would turn one 180 degree sweep into two. Every
action on this screen is a read, a diagnostic, or a re-declaration of
the origin that moves nothing.

The screen will **never** say "nothing was moved" in this case. That
sentence is reserved for a refusal that happened before the servo was
commanded — it used to be printed here too, which sent an operator away
from a carousel that had just turned 180 degrees.

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

That one file is the run's only irreplaceable output and it is **not in
version control** — git cannot restore it. Copy it outside the
repository before any bulk file operation.

It holds **two collections**:

```text
session   the run in progress. Prepare and Measure write here.
archive   the permanent record. Only an explicit import puts anything
          in it.
```

Both are in the one file. There is no backup file beside it: the schema
migration rewrites the canonical document in place, and a second copy in
a production data folder is a second source of truth waiting to be read
by mistake. Git carries the history of everything that is tracked.

A third store is not on this computer at all: the **ESP32** keeps the
last acquisition from each slot in RAM, and loses it when the board
resets — which happens every time the serial port is opened. If a
measurement matters, import it.

Everything else under `BD/` is tracked: `calibration/calibration.json`,
DB1, DB2, DB3, the training database and the model registry.

---

## 8. Run the tests

On the main computer:

```bash
python3 firmware/Tests/run_all.py
```

On the Windows bench:

```powershell
py firmware\Tests\run_all.py
```

This runs the **software** campaign and only the software campaign.
Nothing it does can open a serial port or turn the carousel — see §8.1.

Thirty-nine suites, grouped by the question they answer:

```
static        boundaries, and every name/import/call site in the tree
unit          formulas, record shapes, operator input, numeric edges,
              and the Science invariants as properties
contracts     PC and firmware agreeing on names, arguments, shapes
integration   layers driving each other, up to whole simulated missions
fault         serial, sensor, servo, filesystem, memory, protocol
              limits and the firmware's own error paths, injected
state         carousel, Sample and whole-mission state machines, abused
linux         ports, permissions, errno classes, locale, clock, paths
process       signals, EOF, interruption and restart
entrypoints   importing or --help must not DO anything
integrity     the protected databases, hashed before and after
stress        thousands of cycles, looking for drift and leaks
randomized    seeded fault storms; the seed is printed on failure
regression    one test per defect that ever reached the bench
```

One group, or one suite, at a time:

```bash
python3 firmware/Tests/run_all.py linux
```

The suites can also be run in a deterministic scrambled order, which is
how the acceptance runs prove they do not depend on each other:

```bash
python3 firmware/Tests/run_all.py --shuffle=777
```

The suites run the **real** firmware modules, the real client and the
real Science on CPython. Only the hardware boundary is faked —
`serial.Serial`, `machine.I2C`, `machine.UART`, the clock, the
keyboard and the directory records go in. The AS7265x and ST3215 fakes
speak the actual register and frame protocols, so a driver bug shows up
as a failing check rather than as a passing check against a stub that
shares the bug.

The run ends by re-hashing every file under `firmware/BD/` and fails if
one byte moved. A test that damages the archive is the one kind of test
failure that costs more than the bug it was looking for.

### 8.1 Hardware tests are a separate campaign

```powershell
py firmware\Tests\hardware\run_hardware.py --port COM4
```

That one **turns the carousel**. It is not reachable from `run_all.py`,
it has no default port, and every stage that moves anything needs an
additional `--move`. `run_all.py --hardware` is refused rather than
quietly running the software suite instead.

The reason is that `run_all.py` is the command people run by reflex —
from a hook, from an editor, from another room — and the carousel may
be holding samples or be mechanically constrained.

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
