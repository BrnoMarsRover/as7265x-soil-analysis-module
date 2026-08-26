# Hardware Verification Plan — Phase B

What to do when the rover is connected.

> **This file states the ASSUMPTIONS and what would falsify each.**
>
> The executable campaign is now `run_hardware_tests.py`: 78 registered
> tests across B0–B12, each with an objective, a setup, preconditions, a
> procedure, an expected result, failure criteria and a definition of
> what to capture. `PLAN.md` carries the order, the layer gates and a
> traceability table generated from the registry itself; `README.md`
> carries the status and the safe commands. Every H-number below maps to
> the tests that settle it — see the assumptions table in `PLAN.md`.
>
> `PHASE_B_CAMPAIGNS.md` remains the prose companion. It assigns an
> owner to all sixteen `HARDWARE_ONLY` exception handlers and carries
> **ENV-LINUX-001**, the one deferred software item, which needs no
> hardware at all.
>
> **Nothing has been executed against hardware.** All 78 tests are
> `NOT_RUN`.

## Where each assumption is now settled

| Assumption | Tests |
| --- | --- |
| H-001 | `HW-B4-001`, `HW-B4-002`, `HW-B4-003` |
| H-002 | `HW-B2-002`, `HW-B2-004`, `HW-B2-006`, `HW-B3-001`, `HW-B3-003`, `HW-B3-004`, `HW-B8-002` |
| H-003 | `HW-B6-004`, `HW-B7-002`, `HW-B7-005` |
| H-004 | `HW-B0-004`, `HW-B1-005` |
| H-005 | `HW-B3-002`, `HW-B3-005`, `HW-B5-003` |
| H-006 | `HW-B5-004`, `HW-B5-005`, `HW-B11-003`, `HW-B11-004` |
| H-007 | `HW-B7-003`, `HW-B11-002` |
| H-008 | `HW-B1-002` |

```bash
python3 firmware/Tests/hardware/run_hardware_tests.py --describe HW-B3-001
```

This is the backlog produced by two software verification campaigns. It
is not a wish list: every item here exists because a specific software
assumption could not be settled without the physical hardware, and each
one names the assumption, the test, and what result would falsify it.

**Nothing in this document has been run.** The hardware was disconnected
throughout Phase A. Any claim that a line here passes must come from
actually running it against the board.

---

## How to run anything here

The campaign entry point. Listing, describing and dry-running touch no
hardware:

```bash
python3 firmware/Tests/hardware/run_hardware_tests.py --list
```

```bash
python3 firmware/Tests/hardware/run_hardware_tests.py --dry-run --all
```

A real run needs an explicit confirmation, and then a second one for
whatever the selected tests physically do:

```bash
python3 firmware/Tests/hardware/run_hardware_tests.py --profile firmware/Tests/hardware/configuration/my-bench.json --run-campaign B0 --confirm-hardware
```

There is no default port and there will not be one. `run_all.py` cannot
reach any of this.

`run_hardware.py` and `hardware_validation.py` are the earlier
stage-based HIL suite. They still work and are still driven by an
explicit `--port` plus `--move`; the registry-based entry point above
supersedes them for new work, because it carries the layer gates, the
capability detection and the evidence model they do not.

Before the first run on a fresh Linux account:

```bash
sudo usermod -aG dialout $USER
```

then log out and back in. See `PORT_DENIED` in `Documentation/OPERATIONS.md`.

---

## The assumptions software cannot settle

Each has an ID so a test result can refer to it.

### H-001 — the ST3215 position tolerance is 15 counts

`ESP32/config.py` ships `ST3215_POSITION_TOLERANCE = 15`, about 1.3
degrees. It is the one shipped constant that is a guess, and it decides
whether a movement is accepted or reported as `SERVO_POSITION_MISMATCH`.

**Software has verified:** that the configured value is applied
inclusively (`abs(error) <= tolerance`), that the error is a circular
distance with no seam at 4095/0, and that a value outside it invalidates
the position rather than being rounded away.

**Hardware must establish:** the real closing error of the mechanism.
Command a slot movement fifty times, record `position_error` from each,
and take the distribution. The tolerance should be set from the measured
spread, not from the number that happens to be there.

**Falsified if:** the observed spread routinely exceeds 15 counts. Then
either the tolerance is too tight or the mechanism has a problem the
tolerance was hiding.

### H-002 — the encoder tracks the mechanism in STEP mode

**This is the open question from RF-001 and it is the most important
item in this document.**

On the Linux bench a 180 degree transfer was commanded, the carousel
visibly rotated, and the encoder reported **2 counts** of travel. The
firmware refused the measurement, which was correct. What is not known
is why the encoder and the operator disagreed.

**Software has verified:** the arithmetic. `centred_error` is correct at
every one of the 4096 positions; `expected = wrap_counts(start +
requested)` is right; the tolerance comparison is right. Given the
readings the driver received, the refusal was the only correct outcome.

**Hardware must establish** which of these is true:

1. the servo was in position mode, not STEP mode, so a relative goal was
   interpreted as an absolute one;
2. `counts_per_rev` does not match the servo's actual resolution;
3. the present-position register does not update in STEP mode as the
   driver assumes;
4. the servo moved mechanically without the encoder following - a
   coupling or gearbox fault;
5. the position read is racing the movement and settling is insufficient.

**How:** connect the servo, run `servo_diagnostics`, read the mode
register, then command a known relative movement and read the position
before and after with the mechanism free. Compare with a protractor.

**Falsified if:** the encoder tracks correctly. Then the bench failure
was a one-off electrical event and the diagnostic added in Phase A.2 -
which now prints travelled counts alongside the error - will say so next
time.

### H-003 — the AS7265x is ready within the configured integration time

`SENSOR_INTEGRATION_CYCLES = 100` and the driver waits
`cycles * 2.8 * 1.5 + 1` ms for data-ready.

**Software has verified:** that a sensor which never asserts ready is
timed out and named, that a partial read is refused, and that the lamps
are switched off afterwards in every path including the failing ones.

**Hardware must establish:** the real ready latency across all three
illuminations and a realistic temperature range.

### H-004 — the CP2102 survives repeated open cycles

`SerialLink.open()` drives DTR and RTS low before opening so the board
is not reset. This was measured once.

**Software has verified:** that the lines are set before `open()` and
stay low, and that the port is released on every exit path.

**Hardware must establish:** that a thousand open/close cycles do not
wedge the bridge, and that the board never resets during them. This is
also where **RF-002** belongs: `/dev/ttyUSB0` disappeared mid-request on
the bench and the electrical cause is unknown.

### H-005 — a half turn is exactly 2048 counts on this mechanism

`CAROUSEL_HALF_TURN_DEG` and the 4096-count assumption give 2048.

**Hardware must establish:** whether the mechanism has a reduction
between servo and carousel. If it does, every angle in the firmware is
wrong by that ratio and H-002 explains itself.

### H-006 — backlash does not accumulate

**Software has verified:** 2000 simulated movements with no logical
drift.

**Hardware must establish:** whether physical position drifts after
repeated alternating movements, and whether a re-sync is needed after N
cycles.

---

