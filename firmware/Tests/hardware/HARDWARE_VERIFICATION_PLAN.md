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
| H-002 **RESOLVED** | `HW-B2-013`, `HW-B2-014`, `HW-B3-006`, `HW-B3-007`, `HW-B3-008`, `HW-B8-002` |
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

### H-002 — RESOLVED ON HARDWARE, 2026-08-28

**Hypothesis 3 was right: the present-position register does not mean
what the driver assumed in STEP mode.**

In step servo mode register 56 is the **following error** - the signed
distance from where the shaft is to the target of the current step
command - not an absolute position. A completed 180 degree transfer
therefore reads about 2 counts before it and about 2 counts after it,
because the servo was within its dead band at both ends. The driver was
comparing an error against a position, and its arithmetic, which was
correct throughout, produced the only conclusion those readings allow.

The absolute position IS available in step mode, from register 67, the
commanded multi-turn trajectory. That register is OPEN LOOP: with the
torque limit written to 0 it advanced the full commanded 512 counts
while the shaft could not have moved and register 56 sat at 514. So the
measurement is the pair:

    measured absolute position = register 67 - register 56

Confirmed against ground truth taken by switching to position servo
mode with the torque off, where register 56 IS the absolute angle: step
mode gave 3527 - 2 = 3525, and mode 0 read 3525, 3525, 3525.

**The other four hypotheses are eliminated by the same measurements.**
The mode register read 3 throughout; eight consecutive +512 steps moved
exactly one revolution, so `counts_per_rev` is right and there is no
reduction (**H-005 resolved with it**); the absolute position tracked
every command, so nothing is mechanically decoupled; and the reading is
stable indefinitely, so nothing is racing the movement.

**Full evidence:** `artifacts/H-002-evidence-20260828.md`.

**Found while proving it, and more dangerous than H-002 was:** register
67 clamps at 32766, roughly eight revolutions of NET one-directional
travel. Past the clamp the servo stops moving in BOTH directions and
still reports a following error of about 2 - a movement that would
report VERIFIED and would not happen. A carousel that always takes the
shortest path to the next slot accumulates a full revolution every four
samples, so this is reached in ordinary service, not at the edge of it.
The driver folds the register back before it can happen, by passing
through position servo mode, which reseeds it from the absolute encoder
and moves nothing; the logical carousel angle is carried across
unchanged. `HW-B3-008` drives the register there and watches it fold.

**Now covered by:** `HW-B2-013` (the position changes when the carousel
moves), `HW-B2-014` (diagnostics does not claim more than it proves),
`HW-B3-006` (a re-sync reads exactly 0.0 deg at any raw count),
`HW-B3-007` (a held carousel is reported as a failure),
`HW-B3-008` (the trajectory register is folded before it clamps),
`HW-B8-002` (the RF-001 regression itself).

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

### H-005 — RESOLVED ON HARDWARE, 2026-08-28

`CAROUSEL_HALF_TURN_DEG` and the 4096-count assumption give 2048, and
that is right. Eight consecutive +512 steps moved the absolute position
from 3015 to 7111 - exactly +4096, one revolution - and four -1024
steps brought it back to 3015 exactly. There is no reduction between
the encoder and the counted position, so no angle in the firmware is
scaled by an unknown ratio.

What a plate-to-shaft reduction would still hide is not electrically
observable; `HW-B3-005` remains the protractor measurement that would
settle that, and it needs an operator.

### H-006 — backlash does not accumulate

**Software has verified:** 2000 simulated movements with no logical
drift.

**Hardware must establish:** whether physical position drifts after
repeated alternating movements, and whether a re-sync is needed after N
cycles.

---

