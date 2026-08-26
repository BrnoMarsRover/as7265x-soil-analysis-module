# Phase B — the plan

The order the hardware campaign is climbed, and what every test in it is
for.

**Nothing here has been run.** 78 tests, all `NOT_RUN`. See `README.md`
for status and for how to run anything safely.

---

## The pyramid

```text
B12  competition mission rehearsal          gated by B10
B11  endurance                              gated by B8
B10  the production operator workflow       gated by B8
B9   Linux / USB / reset / recovery         gated by B1
B8   carousel + measurement integration     gated by B5 and B7
B7   sensor integrity and illumination      gated by B6
B6   direct AS7265x                         gated by B1
B5   the physical carousel                  gated by B4
B4   servo characterization                 gated by B3
B3   H-002: encoder versus mechanism        gated by B2
B2   direct ST3215 communication            gated by B1
B1   Main PC to ESP32 communication         gated by B0
B0   bench and environment inventory        the floor
```

**Layer *N+1* is not evidence while layer *N* is unresolved.** The gates
are enforced by the runner against a ledger that only real hardware
results can write to, so a dry run and a self-test cannot open one.

Two branches run in parallel above B1: the servo side (B2 → B3 → B4 →
B5) and the sensor side (B6 → B7). They meet at B8. **The sensor branch
is not blocked by H-002**, so B6 and B7 are useful work that can be done
while the encoder question is still open.

### The order to actually run them in

1. **B0** — inventory. Nothing is connected; nothing is opened.
2. **B1** — the link, on its own terms.
3. **B2** — the servo answers, and is the servo we think it is.
4. **B3** — **H-002.** The most important test in the campaign.
5. **B6, B7** — the sensor, in parallel with everything above. Not
   blocked by H-002.
6. **B4** — repeatability, and the measurement that sets the tolerance.
7. **B5** — the carousel, once the encoder is trustworthy.
8. **B8** — the whole transaction, including the RF-001 regression.
9. **B9** — take things away and see what is admitted.
10. **B10** — a human, the real client, the real archive.
11. **B11** — the long runs.
12. **B12** — the rehearsal. Never first.

---

## H-002 is the gate on everything with a slot number in it

```text
physical carousel movement observed   about 180 degrees
reported encoder/software movement    about 2 counts
expected target movement              about 2048 counts
```

Software has already settled the arithmetic: `centred_error` is correct
at all 4096 positions, `expected = wrap_counts(start + requested)` is
right, and the tolerance comparison is right and inclusive. Given the
readings the driver received, its refusal was the only correct outcome.
**The arithmetic is not a hypothesis.**

What is open is the physical relationship between the commanded goal,
the reported position, the encoder count and the output-shaft angle. B3
measures all four together and separates the hypotheses without choosing
one in advance:

| # | Hypothesis | Settled by |
| --- | --- | --- |
| 1 | position mode instead of STEP — a relative goal read as absolute | `HW-B2-002` |
| 2 | `counts_per_rev` is not the real resolution | `HW-B3-002` |
| 3 | present-position does not update in STEP mode | `HW-B3-001`, `HW-B3-003` |
| 4 | the servo moves and the encoder does not follow | `HW-B3-001` |
| 5 | the read races the movement / insufficient settling | `HW-B2-004`, `HW-B3-003` |
| 6 | a stale response, or a reply to another command | `HW-B3-003` |
| 7 | a reduction between servo and carousel | `HW-B3-005` |
| 8 | byte order in the position register | `HW-B3-004` — **BLOCKED** |
| 9 | the servo id, baud or pin order is not what we think | `HW-B2-003` |
| 10 | mechanical slip | `HW-B3-001` repeats |

Cheapest first: hypotheses 1, 5 and 9 are eliminated by B2 without
moving anything, which is why B3 is gated behind it.

Two legs of the B3 angle series are **circularly ambiguous to the
encoder** and are judged accordingly: a 180 degree movement gives the
same reading in both directions, and a 360 degree movement returns a
delta of zero. The operator's direction and angle observations carry the
sign for those — which is the whole reason the test is
operator-assisted, and why a leg with no human observation is not
evidence about the mechanism.

**`ST3215_POSITION_TOLERANCE = 15` is not touched by B3.** The later
value comes from `HW-B4-003`'s measured distribution and from nothing
else.

---

## This bench has four slots, not eight

Read from `firmware/ESP32/config.py`, never copied:

| Constant | Value | Provenance |
| --- | --- | --- |
| `CAROUSEL_SLOT_COUNT` | 4 | CONFIGURED |
| `CAROUSEL_SLOT_GEOMETRY_DEG` | 90.0 | CONFIGURED |
| `CAROUSEL_SCAN_LOAD_OFFSET` | 2 slots = 180° | CONFIGURED |
| `ST3215_COUNTS_PER_REV` | 4096 | CONFIGURED, and **ASSUMED** physically |
| `ST3215_COUNTS_PER_SLOT` | 1024 | CONFIGURED |
| `ST3215_HALF_TURN_COUNTS` | 2048 | CONFIGURED |
| `ST3215_POSITION_TOLERANCE` | 15 | CONFIGURED, and a **guess** (H-001) |
| `AS7265X_ADDRESS` | 0x49 | CONFIGURED, verified on the real board |
| `ST3215_TX_PIN` / `RX_PIN` | GPIO17 / GPIO16 | CONFIGURED, and **ASSUMED** wiring |
| servo-to-carousel gear ratio | 1.0 | **ASSUMED** — H-005, measured by `HW-B3-005` |

Every transition matrix in B5 is **generated** from the slot count, so
the campaign is correct for this plate and would still be correct for an
eight-slot one.

---

## Assumptions and the tests that settle them

From `HARDWARE_VERIFICATION_PLAN.md`:

| Assumption | What it claims | Tests |
| --- | --- | --- |
| **H-001** | the position tolerance is 15 counts | `HW-B4-001`, `HW-B4-002`, `HW-B4-003` |
| **H-002** | the encoder tracks the mechanism in STEP mode | `HW-B2-002`, `HW-B2-004`, `HW-B2-006`, `HW-B3-001`, `HW-B3-003`, `HW-B3-004`, `HW-B8-002` |
| **H-003** | the AS7265x is ready within the configured integration time | `HW-B6-004`, `HW-B7-002`, `HW-B7-005` |
| **H-004** | the CP2102 survives repeated open cycles | `HW-B0-004`, `HW-B1-005` |
| **H-005** | a half turn is exactly 2048 counts on this mechanism | `HW-B3-002`, `HW-B3-005`, `HW-B5-003` |
| **H-006** | backlash does not accumulate | `HW-B5-004`, `HW-B5-005`, `HW-B11-003`, `HW-B11-004` |
| **H-007** | heap and timing are stable over long runs | `HW-B7-003`, `HW-B11-002` |
| **H-008** | opening the port does not reset the board | `HW-B1-002` |

---

## Defect families

| Prefix | Layer it belongs to |
| --- | --- |
| `HW-SER-nnn` | the serial transport |
| `HW-USB-nnn` | the USB bridge and device enumeration |
| `HW-SERVO-nnn` | the ST3215 and its bus |
| `HW-CAR-nnn` | the carousel mechanism |
| `HW-SENSOR-nnn` | the AS7265x and the I2C bus |
| `HW-INT-nnn` | the integrated measurement transaction |
| `HW-MISSION-nnn` | the operator workflow and the mission |

---

## Blocked, and what would unblock each

Three tests are registered, documented and executable, and cannot run
today because the shipped system has no interface for them. **None of
them has been worked around by adding an unverified diagnostic command
to production firmware.**

### `HW-B2-006` and `HW-B3-004` — no raw ST3215 packet access

The PC sees the driver's *interpretation* of the position register and
never the two bytes it was built from. That blocks capturing a raw
reply, and it blocks checking whether an alternative byte order explains
the H-002 contradiction.

**Recommendation.** Add a bounded, read-only diagnostic command to
`firmware/ESP32/protocol.py` — suggested name `servo_raw_read` — taking
`{id, register, length}` with length limited to 1..4, calling the
existing `ST3215.read_byte` / `read_word` path, and returning both the
parsed value **and** the raw reply bytes. Read-only: no register *write*
passthrough, because a wrong write to the ST3215 memory table can change
the servo id or baud rate and take the bus away entirely.

**Mitigation in place.** `HW-B3-001` records the same byte-order
interpretations against the *parsed* value as a diagnostic hint. That is
weaker — it cannot see the wire — but it needs no firmware change and it
would make a byte-swap visible if one is there.

### `HW-B6-001` — no on-demand I2C scan

The bus is scanned only during sensor initialization, so `get_status`
reports the *last* scan rather than a fresh one. That means the bus
cannot be inspected without disturbing a sensor that is working.

**Recommendation.** Add a read-only `i2c_scan` command that calls the
existing `sensor.scan_bus(i2c)` and returns the addresses found, without
initializing or configuring anything. A few lines; it moves nothing; it
would let a bench test tell "the sensor is absent" from "the bus is
dead".

**Mitigation in place.** `HW-B6-002` covers the same ground through a
forced re-initialization, which is heavier but needs no firmware change.

### Three further capabilities are missing by design, and block nothing

| Capability | Why it is absent |
| --- | --- |
| `link.raw_stream` | a second reader on the same port would steal bytes from the production reader. Use a hardware line tap if full wire capture is ever needed. |
| `carousel.interrupt_move` | the wire is strictly one request at a time, so no command can be delivered mid-move. `HW-B9-004` interrupts the movement *physically*, which is a more faithful test of what the rover will actually see. |
| `workflow.drive_menus` | a simulated operator would produce a workflow result no operator observed, and it would write into `firmware/BD/samples/` — the run's only irreplaceable output. |

---

## Traceability

Generated from the live registry; `offline_tests/test_registry.py` fails
the build if any row here would be incomplete.

| Test ID | Campaign | Layer | Objective | Automation | Safety | Prerequisites | Required capability | Readiness |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `HW-B0-001` | B0 | B0 | Record the machine the campaign ran on, and prove the Python and pyserial available are ones the production client can use. | AUTOMATIC | READ_ONLY | - | - | READY_FOR_HARDWARE |
| `HW-B0-002` | B0 | B0 | Tie the campaign to an exact commit and to the exact production constants it is qualifying. | AUTOMATIC | READ_ONLY | - | - | READY_FOR_HARDWARE |
| `HW-B0-003` | B0 | B0 | Prove the profile that will select the device is complete and internally consistent BEFORE a campaign depends on it. | AUTOMATIC | READ_ONLY | - | - | READY_FOR_HARDWARE |
| `HW-B0-004` | B0 | B0 | Record every serial device the machine can see, and establish a STABLE identity for the science module that survives a replug. | AUTOMATIC | READ_ONLY | - | `link.enumerate` | READY_FOR_HARDWARE |
| `HW-B0-005` | B0 | B0 | Have a human confirm the physical assumptions every later campaign is built on, one at a time. | OPERATOR_ASSISTED | READ_ONLY | - | - | READY_FOR_HARDWARE |
| `HW-B1-001` | B1 | B1 | Establish that the resolved device is the science module and that it answers the protocol. | AUTOMATIC | COMMUNICATION | - | `link.open`, `link.ping` | READY_FOR_HARDWARE |
| `HW-B1-002` | B1 | B1 | Prove the DTR/RTS discipline in SerialLink.open() works on this board, so starting the client does not reboot an instrument holding a synchronized ... | AUTOMATIC | COMMUNICATION | - | `link.open`, `link.ping`, `link.status` | READY_FOR_HARDWARE |
| `HW-B1-003` | B1 | B1 | Check that the status the whole PC layer depends on carries every section it promises. | AUTOMATIC | COMMUNICATION | - | `link.status` | READY_FOR_HARDWARE |
| `HW-B1-004` | B1 | B1 | Prove the port is released cleanly and a new session can take it without the board noticing. | AUTOMATIC | COMMUNICATION | - | `link.open`, `link.ping`, `link.status` | READY_FOR_HARDWARE |
| `HW-B1-005` | B1 | B1 | Bound or reproduce RF-002: the bridge wedging, or the device node disappearing, after repeated opens. | AUTOMATIC | ENDURANCE | - | `link.open`, `link.ping` | READY_FOR_HARDWARE |
| `HW-B1-006` | B1 | B1 | Prove one open port survives a thousand requests without accumulating corruption. | AUTOMATIC | ENDURANCE | - | `link.ping`, `link.status`, `link.counters` | READY_FOR_HARDWARE |
| `HW-B1-007` | B1 | B1 | Measure what a command actually costs, so a later timeout can be set from evidence rather than from a guess. | AUTOMATIC | COMMUNICATION | - | `link.ping`, `link.status` | READY_FOR_HARDWARE |
| `HW-B1-008` | B1 | B1 | Establish whether the device node changes when the cable is pulled and pushed back, and whether the stable identity survives it. | OPERATOR_ASSISTED | MANUAL_DISCONNECT | - | `link.enumerate` | READY_FOR_HARDWARE |
| `HW-B2-001` | B2 | B2 | Bring the ST3215 driver up over the real UART and confirm the servo answers a ping. | AUTOMATIC | COMMUNICATION | - | `servo.connect` | READY_FOR_HARDWARE |
| `HW-B2-002` | B2 | B2 | Read every register the driver's assumptions rest on, and compare each with what config.py expects. | AUTOMATIC | COMMUNICATION | - | `servo.diagnostics` | READY_FOR_HARDWARE |
| `HW-B2-003` | B2 | B2 | Turn 'no answer from servo 1' into a table that says which of the four assumptions is wrong. | AUTOMATIC | COMMUNICATION | - | `servo.bus_scan` | READY_FOR_HARDWARE |
| `HW-B2-004` | B2 | B2 | Establish whether the reported position is stable when the mechanism is standing still. | AUTOMATIC | COMMUNICATION | - | `servo.read_position` | READY_FOR_HARDWARE |
| `HW-B2-005` | B2 | B2 | Prove the numbers the servo is being driven with are the numbers in config.py. | AUTOMATIC | COMMUNICATION | - | `servo.calibration` | READY_FOR_HARDWARE |
| `HW-B2-006` | B2 | B2 | Capture the bytes of a position read exactly as they came off the servo bus, so the driver's parsing can be checked against the wire. | AUTOMATIC | COMMUNICATION | - | `servo.raw_packet` | **BLOCKED** |
| `HW-B2-007` | B2 | B2 | Check the error path is a structured refusal rather than a movement or a crash. | OPERATOR_ASSISTED | COMMUNICATION | - | `servo.test_move` | READY_FOR_HARDWARE |
| `HW-B3-001` | B3 | B3 | Measure, for each of +10, +45, +90, +180 and +360 degrees and each reverse, what was commanded, what the encoder reported, and what a human measure... | OPERATOR_ASSISTED | MOTION | - | `servo.test_move`, `servo.read_position`, `servo.connect` | READY_FOR_HARDWARE |
| `HW-B3-002` | B3 | B3 | Establish how many counts one physical revolution of the output shaft actually is, rather than assuming 4096. | OPERATOR_ASSISTED | MOTION | - | `servo.test_move`, `servo.read_position`, `servo.connect` | READY_FOR_HARDWARE |
| `HW-B3-003` | B3 | B3 | Separate 'the encoder did not move' from 'we were given an old answer'. | AUTOMATIC | MOTION | - | `servo.test_move`, `servo.read_position`, `servo.connect` | READY_FOR_HARDWARE |
| `HW-B3-004` | B3 | B3 | Read the position register as bytes and check whether an alternative byte order would explain the observed contradiction. | AUTOMATIC | COMMUNICATION | - | `servo.raw_packet` | **BLOCKED** |
| `HW-B3-005` | B3 | B3 | Derive the ratio between the servo's reported movement and the carousel's physical movement, so hypothesis 7 is a number rather than a suspicion. | OPERATOR_ASSISTED | MOTION | - | `servo.test_move`, `servo.read_position`, `servo.connect` | READY_FOR_HARDWARE |
| `HW-B4-001` | B4 | B4 | Compare the two directions at the smallest movement the carousel ever makes. | AUTOMATIC | MOTION | - | `servo.test_move`, `servo.connect` | READY_FOR_HARDWARE |
| `HW-B4-002` | B4 | B4 | Establish whether the error depends on the size of the movement. | AUTOMATIC | MOTION | - | `servo.test_move`, `servo.connect` | READY_FOR_HARDWARE |
| `HW-B4-003` | B4 | B4 | MEASURE the number ST3215_POSITION_TOLERANCE should be. This is H-001. | AUTOMATIC | MOTION | - | `servo.test_move`, `servo.connect` | READY_FOR_HARDWARE |
| `HW-B4-004` | B4 | B4 | Measure how long a real movement takes, so ST3215_MOVE_TIMEOUT_MS can be argued about with numbers. | AUTOMATIC | MOTION | - | `servo.test_move`, `servo.connect` | READY_FOR_HARDWARE |
| `HW-B4-005` | B4 | B4 | Count the transport faults that occur while the servo bus is busy, which is when they are most likely. | AUTOMATIC | MOTION | - | `servo.test_move`, `servo.diagnostics`, `link.counters` | READY_FOR_HARDWARE |
| `HW-B4-006` | B4 | B4 | Look for drift, heating and intermittent failure over a longer run than any single-figure test. | AUTOMATIC | ENDURANCE | HW-B4-003 | `servo.test_move`, `servo.connect` | READY_FOR_HARDWARE |
| `HW-B5-001` | B5 | B5 | Prove each neighbouring transition lands where the firmware says it did. | OPERATOR_ASSISTED | MOTION | - | `carousel.select_slot`, `carousel.sync`, `carousel.status`, `servo.connect` | READY_FOR_HARDWARE |
| `HW-B5-002` | B5 | B5 | Check the movements that cross more than one slot, including the one that must take the shorter way round. | AUTOMATIC | MOTION | - | `carousel.select_slot`, `carousel.sync`, `carousel.status` | READY_FOR_HARDWARE |
| `HW-B5-003` | B5 | B5 | Verify the physical relationship the whole measurement depends on: the slot at the loading hole arrives under the sensor after the transfer. | OPERATOR_ASSISTED | MOTION | - | `carousel.sync`, `carousel.status`, `carousel.select_slot` | READY_FOR_HARDWARE |
| `HW-B5-004` | B5 | B5 | Measure whether the resting position of a slot depends on which way it was approached. | AUTOMATIC | MOTION | - | `carousel.select_slot`, `carousel.status`, `servo.read_position` | READY_FOR_HARDWARE |
| `HW-B5-005` | B5 | B5 | Establish whether position drifts after many consecutive slot movements in one direction. | AUTOMATIC | MOTION | - | `carousel.select_slot`, `carousel.status`, `servo.read_position` | READY_FOR_HARDWARE |
| `HW-B5-006` | B5 | B5 | Prove the operator can recover a known position after the plate has been moved by hand. | OPERATOR_ASSISTED | MOTION | - | `carousel.sync`, `carousel.status`, `carousel.select_slot` | READY_FOR_HARDWARE |
| `HW-B6-001` | B6 | B6 | Enumerate every address answering on the I2C bus without disturbing a sensor that is already working. | AUTOMATIC | READ_ONLY | - | `sensor.i2c_scan_on_demand` | **BLOCKED** |
| `HW-B6-002` | B6 | B6 | Force a full initialization and observe the bus scan it performs. | AUTOMATIC | ILLUMINATION | - | `sensor.init`, `sensor.status` | READY_FOR_HARDWARE |
| `HW-B6-003` | B6 | B6 | Check that get_status answers at the same speed whether the sensor is present, missing or half dead. | AUTOMATIC | READ_ONLY | - | `sensor.status` | READY_FOR_HARDWARE |
| `HW-B6-004` | B6 | B6 | Take a single reading under WHITE, under UV and under IR, and check each returns eighteen channels. | AUTOMATIC | ILLUMINATION | - | `sensor.acquire_block` | READY_FOR_HARDWARE |
| `HW-B6-005` | B6 | B6 | Establish whether initialization is reliable or intermittent - the AS7265X_NOT_FOUND question. | AUTOMATIC | ILLUMINATION | - | `sensor.init`, `sensor.status` | READY_FOR_HARDWARE |
| `HW-B6-006` | B6 | B6 | Check the sensor comes up from a genuine cold start, with the post-reset settling the driver waits out. | AUTOMATIC | RESET | - | `link.hard_reset`, `sensor.init`, `carousel.status` | READY_FOR_HARDWARE |
| `HW-B6-007` | B6 | B6 | Run the exact triad the measurement uses, and check all 54 features arrive. | AUTOMATIC | ILLUMINATION | - | `sensor.acquire_triad` | READY_FOR_HARDWARE |
| `HW-B6-008` | B6 | B6 | Run long enough to catch the intermittent fault, and record exactly what the firmware said when it happened. | AUTOMATIC | ENDURANCE | - | `sensor.init`, `sensor.status`, `sensor.acquire_block` | READY_FOR_HARDWARE |
| `HW-B7-001` | B7 | B7 | Check every way a spectrum can be the wrong shape, against the real device. | AUTOMATIC | ILLUMINATION | - | `sensor.acquire_triad` | READY_FOR_HARDWARE |
| `HW-B7-002` | B7 | B7 | Establish that repeated acquisition of an unchanging target is stable in shape and bounded in time. | AUTOMATIC | ILLUMINATION | - | `sensor.acquire_block` | READY_FOR_HARDWARE |
| `HW-B7-003` | B7 | B7 | Run long enough to see heating, drift or an intermittent failure that a hundred acquisitions would miss. | AUTOMATIC | ENDURANCE | HW-B7-002 | `sensor.acquire_triad` | READY_FOR_HARDWARE |
| `HW-B7-004` | B7 | B7 | Pull the sensor, prove the failure is named correctly, then reconnect and prove it recovers without a reboot. | OPERATOR_ASSISTED | MANUAL_DISCONNECT | - | `sensor.init`, `sensor.acquire_block`, `sensor.status` | READY_FOR_HARDWARE |
| `HW-B7-005` | B7 | B7 | Measure the real ready latency, which is H-003. | AUTOMATIC | ILLUMINATION | - | `sensor.acquire_block` | READY_FOR_HARDWARE |
| `HW-B7-006` | B7 | B7 | Have a human confirm which illuminator is actually on during each phase. | OPERATOR_ASSISTED | ILLUMINATION | - | `sensor.led_test`, `sensor.acquire_block` | READY_FOR_HARDWARE |
| `HW-B7-007` | B7 | B7 | Confirm, by eye, that no bulb is left on when an acquisition finishes. | OPERATOR_ASSISTED | ILLUMINATION | - | `sensor.acquire_triad` | READY_FOR_HARDWARE |
| `HW-B7-008` | B7 | B7 | Check the lamps go off on the ERROR path too, and record explicitly when that cannot be confirmed. | OPERATOR_ASSISTED | FAULT_INJECTION | - | `sensor.acquire_block`, `sensor.status` | READY_FOR_HARDWARE |
| `HW-B8-001` | B8 | B8 | Run measure_raw end to end and check every stage. | AUTOMATIC | FULL_SYSTEM | - | `carousel.measure`, `carousel.sync`, `carousel.status`, `servo.connect` | READY_FOR_HARDWARE |
| `HW-B8-002` | B8 | B8 | Reproduce the exact bench failure that opened H-002, deliberately, and record what happens now. | AUTOMATIC | FULL_SYSTEM | HW-B3-001 | `carousel.measure`, `carousel.sync`, `carousel.status` | READY_FOR_HARDWARE |
| `HW-B8-003` | B8 | B8 | Check that measuring each slot in turn leaves no state behind and no accumulated position error. | AUTOMATIC | FULL_SYSTEM | - | `carousel.measure`, `carousel.sync`, `carousel.status` | READY_FOR_HARDWARE |
| `HW-B8-004` | B8 | B8 | Check the 54 features that come back through the integrated path are the same shape as the ones the sensor produces on its own. | AUTOMATIC | FULL_SYSTEM | - | `carousel.measure`, `sensor.acquire_triad` | READY_FOR_HARDWARE |
| `HW-B9-001` | B9 | B9 | Check the link fails as PORT_LOST, the client survives, and every later command is a clean PORT_CLOSED rather than a traceback. | OPERATOR_ASSISTED | MANUAL_DISCONNECT | B1 | `link.open`, `link.ping`, `link.status` | READY_FOR_HARDWARE |
| `HW-B9-002` | B9 | B9 | Check the link fails as PORT_LOST, the client survives, and every later command is a clean PORT_CLOSED rather than a traceback. | OPERATOR_ASSISTED | MANUAL_DISCONNECT | B1 | `link.open`, `link.ping`, `link.status` | READY_FOR_HARDWARE |
| `HW-B9-003` | B9 | B9 | Check the link fails as PORT_LOST, the client survives, and every later command is a clean PORT_CLOSED rather than a traceback. | OPERATOR_ASSISTED | MANUAL_DISCONNECT | B1 | `link.open`, `link.ping`, `link.status` | READY_FOR_HARDWARE |
| `HW-B9-004` | B9 | B9 | Check that a movement interrupted by a lost link leaves the position UNKNOWN rather than assumed. | OPERATOR_ASSISTED | MANUAL_DISCONNECT | B5 | `carousel.select_slot`, `carousel.status`, `carousel.sync` | READY_FOR_HARDWARE |
| `HW-B9-005` | B9 | B9 | Check a measurement interrupted by a lost link fails cleanly and leaves nothing illuminated. | OPERATOR_ASSISTED | FAULT_INJECTION | B8 | `carousel.measure`, `carousel.status` | READY_FOR_HARDWARE |
| `HW-B9-006` | B9 | B9 | Reset the board deliberately and check the position reference does not survive it. | AUTOMATIC | RESET | - | `link.hard_reset`, `carousel.status`, `carousel.sync` | READY_FOR_HARDWARE |
| `HW-B9-007` | B9 | B9 | Remove power entirely and check the same thing a reset checks, plus that the port comes back. | OPERATOR_ASSISTED | POWER_CYCLE | - | `link.enumerate`, `carousel.status`, `carousel.sync` | READY_FOR_HARDWARE |
| `HW-B9-008` | B9 | B9 | Remove the servo from the bus and check the failure is named and the position invalidated. | OPERATOR_ASSISTED | FAULT_INJECTION | B2 | `servo.diagnostics`, `servo.connect`, `carousel.status` | READY_FOR_HARDWARE |
| `HW-B9-009` | B9 | B9 | Fail the acquisition in the middle of the triad and check the answer names WHICH illumination failed. | OPERATOR_ASSISTED | FAULT_INJECTION | HW-B7-004 | `sensor.acquire_triad`, `sensor.status` | READY_FOR_HARDWARE |
| `HW-B9-010` | B9 | B9 | Prove the science data survives a failed return, and that the position is admitted to be uncertain. | OPERATOR_ASSISTED | FAULT_INJECTION | B8 | `carousel.measure`, `carousel.status`, `carousel.saved_samples` | READY_FOR_HARDWARE |
| `HW-B10-001` | B10 | B10 | Check the entry point and every screen the B10 procedure names still exist, before an operator is asked to follow instructions that mention them. | AUTOMATIC | READ_ONLY | - | - | READY_FOR_HARDWARE |
| `HW-B10-002` | B10 | B10 | Have an operator take the real client from launch to a synchronized carousel. | OPERATOR_ASSISTED | FULL_SYSTEM | - | `workflow.client`, `workflow.screens`, `carousel.status` | READY_FOR_HARDWARE |
| `HW-B10-003` | B10 | B10 | Check sample-to-slot association, measurement identity and persistence across more than one sample. | OPERATOR_ASSISTED | FULL_SYSTEM | - | `workflow.client`, `workflow.records` | READY_FOR_HARDWARE |
| `HW-B10-004` | B10 | B10 | Check that a fault during a session leaves an application the operator can carry on with. | OPERATOR_ASSISTED | FAULT_INJECTION | B9 | `workflow.client` | READY_FOR_HARDWARE |
| `HW-B11-001` | B11 | B11 | Very many requests on one open port, looking for accumulated corruption. | AUTOMATIC | ENDURANCE | HW-B1-006 | `link.ping`, `link.status`, `link.counters` | READY_FOR_HARDWARE |
| `HW-B11-002` | B11 | B11 | Very many acquisitions, looking for heating, drift and the intermittent initialization fault. | AUTOMATIC | ENDURANCE | HW-B7-003 | `sensor.acquire_triad` | READY_FOR_HARDWARE |
| `HW-B11-003` | B11 | B11 | Very many movements, looking for wear, a loosening coupling and thermal drift. | AUTOMATIC | ENDURANCE | HW-B4-006 | `servo.test_move`, `servo.connect` | READY_FOR_HARDWARE |
| `HW-B11-004` | B11 | B11 | Many full rotations, looking for accumulated position drift. | AUTOMATIC | ENDURANCE | HW-B5-005 | `carousel.select_slot`, `carousel.status`, `servo.read_position` | READY_FOR_HARDWARE |
| `HW-B11-005` | B11 | B11 | The complete measurement transaction, repeated, which is the only test that stresses everything at once. | AUTOMATIC | ENDURANCE | HW-B8-001 | `carousel.measure`, `carousel.status`, `carousel.sync` | READY_FOR_HARDWARE |
| `HW-B12-001` | B12 | B12 | Take the instrument through a full competition sequence - setup, every slot, analysis, records - and time it. | OPERATOR_ASSISTED | FULL_SYSTEM | - | `workflow.client`, `workflow.records`, `carousel.status` | READY_FOR_HARDWARE |
| `HW-B12-002` | B12 | B12 | Establish that the mission is repeatable and that its duration is predictable enough to plan around. | OPERATOR_ASSISTED | FULL_SYSTEM | HW-B12-001 | `workflow.client`, `carousel.status` | READY_FOR_HARDWARE |
