# Phase B — executable campaigns

The companion to `HARDWARE_VERIFICATION_PLAN.md`. That file states the
assumptions; this one states what to do about them, in enough detail
that a result survives the session it was produced in.

**Nothing here has been run.** The hardware was disconnected throughout
Phase A.

> ## These procedures are now registered and executable
>
> `run_hardware_tests.py` carries all of them as 78 registered tests
> across B0–B12, with the layer gates enforced, the evidence written to
> a per-run directory, and the operator prompted rather than assumed.
> `PLAN.md` has the order and the traceability table; `README.md` has
> the status and the safe commands.
>
> The HW-numbers below map onto the registered ids:
>
> | Here | Registered as |
> | --- | --- |
> | HW-100 | `HW-B0-004` |
> | HW-101 | `HW-B1-002` |
> | HW-102 | `HW-B9-001` … `HW-B9-005` |
> | HW-200 | `HW-B2-001` |
> | HW-201, HW-202 | `HW-B2-003` |
> | HW-203 | `HW-B2-002` |
> | HW-204 | `HW-B3-001` |
> | HW-205 | `HW-B4-003` |
> | HW-206 | `HW-B5-004` |
> | HW-300 | `HW-B6-002` |
> | HW-301 | `HW-B7-004` |
> | HW-302 | `HW-B6-005` |
> | HW-303 | `HW-B7-004` |
> | HW-304 | `HW-B6-005`, `HW-B7-004` |
> | HW-305 | `HW-B7-005` |
> | HW-306 | `HW-B11-002` |
> | HW-400 | `HW-B5-003` |
> | HW-401 | `HW-B9-004` |
> | HW-402 | `HW-B5-006` |
>
> The `HARDWARE_ONLY` handler owners in the table below are unchanged;
> read the owner column against the mapping above.

---

## Every HARDWARE_ONLY handler has an owner

`audit/handler_coverage.py` classifies sixteen exception handlers as
`HARDWARE_ONLY`: the fakes speak the real register and packet
protocols, which is what makes them useful, and exactly why they cannot
produce a half-written register or a frame corrupted in one specific
byte.

Each is owned by a numbered test below. A handler with no owner is a
handler nobody will ever see run.

| Handler | Condition it guards | Owner |
| --- | --- | --- |
| `sensor.py:240` `except OSError` | an I2C write the bus NAKs | HW-301 |
| `sensor.py:657` `except SensorError` | a channel that will not read back | HW-303 |
| `sensor.py:672` `except Exception` | a float that will not unpack from four raw bytes | HW-303 |
| `sensor.py:732` `except Exception` | `I2C()` construction failing on real pins | HW-300 |
| `sensor.py:751` `except Exception` | bus description unavailable | HW-300 |
| `sensor.py:973` `except Exception` | a recovery attempt failing | HW-304 |
| `sensor.py:1013` `except SensorError` | first initialization failing from cold | HW-302 |
| `servo.py:476` `except ImportError` | no `machine.Pin` — CPython only | see note |
| `servo.py:753` `except AttributeError` | a UART with no `deinit` | HW-201 |
| `servo.py:1779` `except Exception` | a movement leg failing mid-sequence | HW-204 |
| `servo.py:1997` `except Exception` | a diagnostics read failing | HW-203 |
| `servo.py:2029` `except ServoError` | a register read failing during diagnostics | HW-203 |
| `servo.py:2087` `except ServoError` | a scan probe failing | HW-202 |
| `servo.py:2497` `except AttributeError` | a backend without the optional method | HW-201 |
| `servo.py:2651` `except Exception` | driver construction failing | HW-200 |
| `servo.py:2725` `except Exception` | a torque write failing | HW-205 |

**Note on `servo.py:476`.** It is the one entry that is not hardware at
all: it guards `from machine import Pin`, and it executes on *every*
CPython run of the suite, because there is no `machine` module there.
It is already covered, every time. It is listed only so the table has
no gaps.

---

## How every test is written

```
setup       what must be true before starting
procedure   exactly what to do
expected    what a pass looks like
failure     what a fail looks like, and what it means
capture     the data to keep, so the result survives the session
```

A test that does not say what to capture produces an opinion rather
than a measurement.

---

## 1. `mainpc_esp32` — before anything moves

### HW-100 — USB enumeration and stability

```
setup       ESP32 unplugged; a terminal on freya-1-comp
procedure   plug in; ls -l /dev/serial/by-id/; dmesg | tail -20
expected    one CP2102 node within 2 s, with a stable by-id name
failure     no node, or a name that changes between plugs. H-004 is
            then false, and --port must use the by-id path rather than
            ttyUSBn
capture     the by-id string, the ttyUSBn it points at, the dmesg lines
```

### HW-101 — opening the port does not reset the board

```
setup       firmware running; ping answering
procedure   open and close the port 1000 times, reading the console
            after each open, looking for rst: or boot:
expected    zero boot banners. H-008 holds
failure     any banner. Opening the client reboots the instrument: the
            DTR/RTS discipline in serial_link.open() does not work on
            this board, and the operator must re-sync after every start
capture     banner count, and the text of the first one if any
```

### HW-102 — the port disappears mid-command

```
setup       a measurement in progress
procedure   pull the USB at each point: before the request, during the
            write, while waiting for the answer, mid-response.
            Repeat with the cable pulled at the hub rather than at the
            board
expected    PORT_LOST every time; the client survives; every later
            command is PORT_CLOSED; connect + sync recovers
failure     any traceback, or a command that appears to succeed
capture     the errno in error.data["errno"] for each point
```

`LNX-001` and `LNX-002` simulate all 32 errno x entry-point
combinations. This is the test that says which of them the real
hardware actually produces.

---

## 2. `esp32_servo` — the RF-001 investigation

**Run this before any carousel campaign.** Until H-002 is settled, no
carousel result means anything.

### HW-200 — the driver comes up

```
setup       servo powered, bus wired as the config describes
procedure   connect_servo; read mode, ID, baud and limits from the
            servo registers themselves
expected    the values match ESP32/config.py
failure     a mismatch. The config describes a servo that is not
            fitted. Owns servo.py:2651
capture     every register value read
```

### HW-201 — the ECHO_ONLY fault

```
setup       servo powered
procedure   servo_bus_scan at every baud, both pin orders, IDs 1-10
expected    exactly one ID answers, at one baud, in one pin order
failure     ECHO_ONLY again. TX and RX are crossed and the scan is
            seeing its own transmission. Owns servo.py:753 and
            servo.py:2497
capture     the full scan report: every baud, every ID, both orders
```

A previous scan answered only with TX and RX exchanged, and only ID 1
was probed. Probe the whole range this time.

### HW-202 — a probe that fails mid-scan

```
setup       servo powered
procedure   start a bus scan, then disconnect the servo data line while
            it is running
expected    the scan completes and reports the failure per ID
failure     the scan raises, or reports a clean result for a bus that
            was cut. Owns servo.py:2087
capture     the report, and the moment the line was cut
```

### HW-203 — diagnostics against a servo that stops answering

```
setup       servo connected and answering
procedure   servo_diagnostics; then power the servo down and repeat
expected    the first succeeds; the second reports SERVO_UART_TIMEOUT
            and the client survives
failure     a traceback, or diagnostics reporting values from a servo
            with no power. Owns servo.py:1997 and servo.py:2029
capture     both reports, side by side
```

### HW-204 — H-002: does the encoder track the mechanism?

**The most important test in this document.**

```
setup       servo FREE - carousel detached - with a protractor on the
            output shaft
procedure   1. read the encoder
            2. command a 2048-count relative move
            3. read the encoder again
            4. measure the shaft angle with the protractor
            5. repeat 10 times, both directions
            6. reattach the carousel and repeat every step
expected    2048 counts = 180 degrees, and the encoder delta matches
            the commanded delta within the H-001 tolerance
failure     the bench event repeating: a visible 180 degrees with an
            encoder delta of about 2 counts. That would mean the
            encoder is not reporting shaft position, and every carousel
            decision the firmware makes is built on a number that does
            not describe the mechanism. Owns servo.py:1779
capture     a table of commanded counts, encoder before, encoder after
            and protractor degrees, for all 20 free runs and all 20
            attached runs. Photograph the protractor at least once
```

If the free-shaft runs are correct and the attached runs are not, the
difference is the reduction ratio, and H-005 is false.

### HW-205 — torque, and the position tolerance

```
setup       carousel attached, HW-204 settled
procedure   servo_torque off and on, reading the register back each
            time; then fifty identical slot movements, recording every
            position_error
expected    torque reads back as commanded; the errors cluster well
            inside 15 counts
failure     a torque write that does not read back owns servo.py:2725.
            Errors near or beyond 15 mean the H-001 constant is wrong
            and must be re-derived from this data rather than guessed
capture     all fifty position_error values, as a list
```

### HW-206 — backlash

```
setup       carousel attached
procedure   alternate load and scan 200 times; measure the accumulated
            offset at 0, 50, 100, 150 and 200
expected    no monotonic drift. H-006 holds
failure     drift. The carousel walks away from its origin over a
            competition day, invisibly, until a sample misses the
            scanner
capture     the five offsets and the protractor reading at each
```

---

## 3. `esp32_sensor`

### HW-300 — the bus comes up on real pins

```
setup       sensor wired per config
procedure   power on; sensor_test_raw
expected    I2C initializes, 0x49 answers, all three devices present
failure     I2C_INIT_FAILED. The pins in config are not the pins
            fitted. Owns sensor.py:732 and sensor.py:751
capture     the full stage list from the diagnostic
```

### HW-301 — an I2C write the bus refuses

```
setup       sensor running
procedure   short SDA to ground briefly during a configuration write
expected    I2C_WRITE_FAILED, named as such, and the client survives
failure     a silent success, or a traceback. Owns sensor.py:240
capture     the error frame, and what was shorted when
```

### HW-302 — cold initialization, fifty times

```
setup       sensor powered down
procedure   power on, sensor_test_raw, power off; 50 cycles
expected    50 successes; record how long each took
failure     any failure. Owns sensor.py:1013, and the first_init_error
            it records is the diagnosis
capture     50 durations and 50 outcomes
```

### HW-303 — a channel that will not read

```
setup       sensor running, acquisition in progress
procedure   disconnect the sensor connector mid-acquisition, at each of
            the three illuminations
expected    CHANNEL_READ_FAILED or INCOMPLETE_SPECTRUM; no partial
            spectrum stored; lamps off afterwards
failure     a spectrum containing values from before the disconnect.
            Owns sensor.py:657 and sensor.py:672
capture     the error frame, and the lamp state read back afterwards
```

### HW-304 — recovery, and its budget

```
setup       sensor running
procedure   500 consecutive measurements, watching recovery_count
expected    recovery_count stays at 0, or rises while the measurements
            keep succeeding
failure     SENSOR_INIT_BUDGET_EXPIRED. Owns sensor.py:973, and the
            budget in config needs re-deriving from this data
capture     recovery_count after every 50 measurements
```

### HW-305 — H-003: data-ready latency

```
setup       sensor running
procedure   measure the time from acquisition command to data-ready for
            all three illuminations, 100 times each
expected    every latency inside the configured integration time plus
            ACQUISITION_RESPONSE_SETTLE_MS
failure     any timeout - or, far worse, a read that returns the
            PREVIOUS conversion, which would put the wrong
            illumination's spectrum into the record
capture     300 latencies. The maximum is what the timeout must clear
```

### HW-306 — H-007: heap and endurance on the device

```
setup       firmware running, nothing else on the board
procedure   get_status and read the memory block; then a full triad at
            MAX_REPEATS; read memory again; repeat 100 times
expected    largest_block stays big enough for a 16 kB response
failure     RESPONSE_TOO_LARGE, or largest_block falling monotonically.
            The fragmentation this firmware was built to survive is
            then real, and MEM-009 and MEM-010 are the code paths that
            will run
capture     free, allocated and largest_block after every cycle
```

The software already proves the guards work when allocation fails.
This is the test that says whether it will have to.

---

## 4. `carousel`

Only after H-002 and H-005 are settled.

### HW-400 — geometry

```
setup       carousel attached and synchronized
procedure   every slot pair, both directions
expected    each arrives within tolerance; phase reads LOAD or SCAN
failure     any slot that lands short. Geometry, or H-001
capture     position_error for every move
```

### HW-401 — a movement interrupted by power loss

```
setup       carousel moving between slots
procedure   cut servo power mid-move; restore it; read the status
expected    position_valid is FALSE afterwards, and the operator is
            told to re-sync
failure     the firmware still claiming a valid position
capture     the status frame before, during and after
```

### HW-402 — re-sync after a deliberate hand-turn

```
setup       carousel synchronized
procedure   disconnect the servo, turn the carousel by hand, reconnect
expected    connect_servo invalidates the position - it always does -
            and the operator re-syncs by eye
failure     a position that survives the hand-turn
capture     status before and after
```

---

## 5. `disconnect_recovery`

Every case in `regression/test_linux_bench.py`, with a real cable:

- pull USB before a request, during the write, while waiting,
  mid-response
- reset the ESP32 while the PC keeps running
- restart the PC while the ESP32 keeps running
- restart both
- brown out the servo supply only
- run the client under `LANG=C` with a Czech sample note

For each: the application must survive, refuse by name, and recover
through connect and sync. Software has verified all of this against
fakes. This says whether the fakes were right.

---

## 6. `endurance`

- 1000 measurements
- 10000 protocol exchanges
- repeated client restarts across a full working day
- `free` and the ESP32 memory block sampled every 100 operations

---

## 7. `mission_rehearsal`

The competition workflow, start to finish, four samples, repeatedly,
including a deliberate failure and recovery in the middle.

---

## ENV-LINUX-001 — the deferred software item

**This is not a hardware test.** It needs no carousel, no sensor and no
servo. Only the machine.

```
setup       freya-1-comp, this repository checked out
procedure   python3 --version
            python3 -m pip show pyserial
            git status --short
            python3 firmware/Tests/run_all.py
expected    all suites PASS; BD/ hashes unchanged; no unexpected
            tracked changes
failure     any suite failing, or a BD/ hash moving. The software
            baseline does not then hold on the machine it will run on,
            and the freeze is not valid there
capture     the interpreter version, the pyserial version, the full
            suite output, and the final BD/ hash line
status      DEFERRED_ENVIRONMENT_VALIDATION
```

It settles `SOFTWARE_ASSUMPTIONS.md` A-001 (the interpreter version)
and A-004 (`sqlite3` present) — the only two assumptions in the whole
register that could be closed without hardware and have not been.

---

## What to do when hardware contradicts software

The software is written to the assumptions in
`HARDWARE_VERIFICATION_PLAN.md`. If hardware falsifies one, the fix
belongs in production code and then in `Tests/software/regression/`, so
the next campaign cannot lose it.

Do not adjust a constant to make a hardware test pass without recording
the measurement that justified it. `ST3215_POSITION_TOLERANCE` is in
that document precisely because nobody wrote down where 15 came from.
