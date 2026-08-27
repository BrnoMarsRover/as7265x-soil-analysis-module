# Phase B — the plan

The order the hardware campaign is climbed, and what every test in it is
for.

**Nothing here has been run.** 107 tests across 13 campaigns, carrying 113
requirements, all `NOT_RUN`. See `README.md` for status, and
[RUNBOOK.md](RUNBOOK.md) for what to do on the day the module is on the
bench.

---

## The pyramid

```text
B12  competition mission rehearsal          gated by B10 AND B11
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
12. **B12** — the rehearsal. Never first, and gated on B11 as well
    as B10: a mission rehearsal on a system whose endurance was
    never measured is one long run away from the failure nobody
    looked for.

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

Generated from the live registry and requirement catalogue.
`offline_tests/test_verdicts.py` fails the build if either direction
breaks: a test naming a requirement that does not exist, or a
requirement no test claims.

Six requirements are marked *offline suite* rather than *hardware test*.
Those are about the FRAMEWORK - that evidence is complete, that a stale
ledger cannot open a gate, that an offline path opens no port - and the
offline suite is the only place they can be established. Saying so is
more honest than inventing a hardware test that would not test them.

**33 of the 113 requirements have no authoritative source.** Those can
only ever produce CHARACTERIZATION: a measurement recorded and judged by
nobody, because nothing in this repository says what the right answer
is. That is an accurate statement about the project, not a defect in the
requirement.

## Requirement to tests

| Requirement | Title | Source | Verified by | Tests |
| --- | --- | --- | --- | --- |
| `HW-REQ-CAR-001` | Every slot transition lands correctly | PRODUCTION_CONFIG | hardware test | `HW-B5-001`, `HW-B5-002` |
| `HW-REQ-CAR-002` | The plate is physically where the slot says | DESIGN | hardware test | `HW-B5-001` |
| `HW-REQ-CAR-003` | The loader and scanner are 180 degrees apart | PRODUCTION_CONFIG | hardware test | `HW-B5-003` |
| `HW-REQ-CAR-004` | Backlash is characterized | NONE | hardware test | `HW-B5-004` |
| `HW-REQ-CAR-005` | Drift does not accumulate with rotations | NONE | hardware test | `HW-B5-005` |
| `HW-REQ-CAR-006` | A disturbed carousel can be recovered | PROTOCOL | hardware test | `HW-B5-006` |
| `HW-REQ-CAR-007` | Samples are retained through movement | DESIGN | hardware test | `HW-B5-007` |
| `HW-REQ-CAR-008` | The mechanism settles before acquisition | PRODUCTION_CONFIG | hardware test | `HW-B5-008` |
| `HW-REQ-CAR-009` | Sensor-to-sample geometry is recorded | NONE | hardware test | `HW-B5-009` |
| `HW-REQ-CAR-010` | Fine adjust is bounded and preserves the slot | PRODUCTION_CONFIG | hardware test | `HW-B5-010` |
| `HW-REQ-DIAG-001` | The diagnostic agent is never the flight build | DESIGN | hardware test | `HW-B2-011` |
| `HW-REQ-DIAG-002` | The agent is read-only by default | DESIGN | hardware test | `HW-B2-011` |
| `HW-REQ-DIAG-003` | Production firmware is restored and verified | DESIGN | hardware test | `HW-B2-012` |
| `HW-REQ-END-001` | Sustained operation does not degrade the link | NONE | hardware test | `HW-B11-001` |
| `HW-REQ-END-002` | Sustained acquisition does not drift | NONE | hardware test | `HW-B7-003`, `HW-B11-002` |
| `HW-REQ-END-003` | Sustained movement does not degrade | NONE | hardware test | `HW-B4-006`, `HW-B11-003` |
| `HW-REQ-END-004` | Rotational drift is bounded over many rotations | NONE | hardware test | `HW-B11-004` |
| `HW-REQ-END-005` | The whole system survives sustained operation | NONE | hardware test | `HW-B11-005` |
| `HW-REQ-END-006` | Partial evidence survives an interruption | DESIGN | hardware test | `HW-B11-006` |
| `HW-REQ-END-007` | A zero-failure run states its confidence | DESIGN | hardware test | `HW-B11-001` |
| `HW-REQ-ENV-001` | The host can run the production client | DESIGN | hardware test | `HW-B0-001` |
| `HW-REQ-ENV-002` | The campaign is tied to an exact revision | DESIGN | hardware test | `HW-B0-002` |
| `HW-REQ-ENV-003` | The shipped geometry is self-consistent | PRODUCTION_CONFIG | hardware test | `HW-B0-002` |
| `HW-REQ-ENV-004` | The bench profile identifies exactly one device | DESIGN | hardware test | `HW-B0-003`, `HW-B0-004` |
| `HW-REQ-ENV-005` | The wiring matches the configuration | PRODUCTION_CONFIG | hardware test | `HW-B0-005` |
| `HW-REQ-ENV-006` | The physical unit under test is identified | DESIGN | hardware test | `HW-B0-006` |
| `HW-REQ-ENV-007` | Measurement instruments are identified | DESIGN | hardware test | `HW-B0-007` |
| `HW-REQ-ENV-008` | The power topology is recorded | DESIGN | hardware test | `HW-B0-008` |
| `HW-REQ-ENV-009` | Power isolation and emergency stop are reachable | DESIGN | hardware test | `HW-B0-005` |
| `HW-REQ-ENV-010` | The repository does not contradict itself | PRODUCTION_CONFIG | hardware test | `HW-B0-009` |
| `HW-REQ-FLOW-001` | The shipped client is the client under test | DESIGN | hardware test | `HW-B10-001` |
| `HW-REQ-FLOW-002` | An operator can take the module from cold to ready | DESIGN | hardware test | `HW-B10-002` |
| `HW-REQ-FLOW-003` | Records are machine-verifiable | DESIGN | hardware test | `HW-B10-003` |
| `HW-REQ-FLOW-004` | Records persist across a client restart | DESIGN | hardware test | `HW-B10-005` |
| `HW-REQ-FLOW-005` | The application survives a recoverable fault | DESIGN | hardware test | `HW-B10-004` |
| `HW-REQ-FLOW-006` | Protected reference data is never modified | DESIGN | hardware test | `HW-B10-003`, `HW-B10-005` |
| `HW-REQ-FLOW-007` | Raw data survives an analysis failure | DESIGN | hardware test | `HW-B10-006` |
| `HW-REQ-FW-001` | Evidence is complete and reproducible | DESIGN | offline suite | _offline suite_ |
| `HW-REQ-FW-002` | A prerequisite PASS belongs to the same system | DESIGN | offline suite | _offline suite_ |
| `HW-REQ-FW-003` | Two harness runs cannot share one module | DESIGN | offline suite | _offline suite_ |
| `HW-REQ-FW-004` | Offline paths touch no hardware | DESIGN | offline suite | _offline suite_ |
| `HW-REQ-FW-005` | Iteration counts are bounded by what is repeated | DESIGN | offline suite | _offline suite_ |
| `HW-REQ-FW-006` | A qualification claim needs its sample size | DESIGN | offline suite | _offline suite_ |
| `HW-REQ-H002-001` | Commanded, reported and physical agree | PRODUCTION_CONFIG | hardware test | `HW-B3-001` |
| `HW-REQ-H002-002` | Encoder resolution is measured, not assumed | NONE | hardware test | `HW-B3-002` |
| `HW-REQ-H002-003` | A position read is fresh | NONE | hardware test | `HW-B3-003` |
| `HW-REQ-H002-004` | Every segment is judged, not just the endpoint | DESIGN | hardware test | `HW-B3-001` |
| `HW-REQ-H002-005` | Shaft and carousel are measured separately | NONE | hardware test | `HW-B3-005` |
| `HW-REQ-H002-006` | Failed movements keep their telemetry | DESIGN | hardware test | `HW-B3-001` |
| `HW-REQ-INT-001` | The complete measurement transaction works | PROTOCOL | hardware test | `HW-B8-001` |
| `HW-REQ-INT-002` | RF-001 does not recur | PRODUCTION_CONFIG | hardware test | `HW-B8-002` |
| `HW-REQ-INT-003` | No state leaks between measurements | DESIGN | hardware test | `HW-B8-003` |
| `HW-REQ-INT-004` | The integrated path does not damage data | PROTOCOL | hardware test | `HW-B8-004` |
| `HW-REQ-INT-005` | Acquisition happens with the slot centred | DESIGN | hardware test | `HW-B8-005` |
| `HW-REQ-LINK-001` | The device is the science module | PROTOCOL | hardware test | `HW-B1-001` |
| `HW-REQ-LINK-002` | Opening the port does not reset the board | DESIGN | hardware test | `HW-B1-002` |
| `HW-REQ-LINK-003` | get_status answers completely and cheaply | PROTOCOL | hardware test | `HW-B1-003`, `HW-B6-003` |
| `HW-REQ-LINK-004` | A session's answers cannot reach another session | PROTOCOL | hardware test | `HW-B1-004` |
| `HW-REQ-LINK-005` | The bridge survives repeated open cycles | NONE | hardware test | `HW-B1-005` |
| `HW-REQ-LINK-006` | One open port survives sustained traffic | NONE | hardware test | `HW-B1-006` |
| `HW-REQ-LINK-007` | Response latency is characterized | NONE | hardware test | `HW-B1-007` |
| `HW-REQ-LINK-008` | The device is identifiable across a replug | DESIGN | hardware test | `HW-B0-004`, `HW-B1-008` |
| `HW-REQ-LINK-009` | Damaged frames are captured, not just counted | MEASURED_BASELINE | hardware test | `HW-B1-009` |
| `HW-REQ-LINK-010` | Payload size does not break the link | PROTOCOL | hardware test | `HW-B1-010` |
| `HW-REQ-LINK-011` | Only one client owns the port | DESIGN | hardware test | `HW-B1-011` |
| `HW-REQ-LINK-012` | Device memory does not drift under load | NONE | hardware test | `HW-B1-012` |
| `HW-REQ-MISSION-001` | A mission measures every required slot | DESIGN | hardware test | `HW-B12-001` |
| `HW-REQ-MISSION-002` | A mission needs no workarounds | DESIGN | hardware test | `HW-B12-001` |
| `HW-REQ-MISSION-003` | Mission duration is measured | NONE | hardware test | `HW-B12-001` |
| `HW-REQ-MISSION-004` | The mission is repeatable | NONE | hardware test | `HW-B12-002` |
| `HW-REQ-PWR-001` | Supply rails are within their limits | NONE | hardware test | `HW-B0-008` |
| `HW-REQ-PWR-002` | Voltage droop does not reset the board | NONE | hardware test | `HW-B4-008` |
| `HW-REQ-PWR-003` | Illumination draws nothing when off | NONE | hardware test | `HW-B7-012` |
| `HW-REQ-PWR-004` | Bus signals are electrically valid | NONE | hardware test | `HW-B0-010` |
| `HW-REQ-REC-001` | A lost link is named, not crashed on | PROTOCOL | hardware test | `HW-B9-001`, `HW-B9-002`, `HW-B9-003` |
| `HW-REQ-REC-002` | A fault lands inside the phase it targets | DESIGN | hardware test | `HW-B9-001`, `HW-B9-002`, `HW-B9-003`, `HW-B9-004`, `HW-B9-005` |
| `HW-REQ-REC-003` | Position does not survive uncertainty | PROTOCOL | hardware test | `HW-B6-006`, `HW-B9-004`, `HW-B9-006`, `HW-B9-007`, `HW-B9-008` |
| `HW-REQ-REC-004` | Recovery time is measured from the right moment | DESIGN | hardware test | `HW-B9-007` |
| `HW-REQ-REC-005` | The module that returns is the module that left | DESIGN | hardware test | `HW-B9-007` |
| `HW-REQ-REC-006` | Science data and mechanical position fail separately | DESIGN | hardware test | `HW-B9-010` |
| `HW-REQ-REC-007` | A failing illumination names itself | PROTOCOL | hardware test | `HW-B9-009` |
| `HW-REQ-SENSOR-001` | The AS7265x answers at its configured address | PRODUCTION_CONFIG | hardware test | `HW-B6-002` |
| `HW-REQ-SENSOR-002` | The sensor configuration reads back | PRODUCTION_CONFIG | hardware test | `HW-B6-002` |
| `HW-REQ-SENSOR-003` | Initialization is reliable | NONE | hardware test | `HW-B6-005`, `HW-B6-008` |
| `HW-REQ-SENSOR-004` | The sensor recovers without a reboot | DESIGN | hardware test | `HW-B7-004` |
| `HW-REQ-SENSOR-005` | A cold power-up is distinguished from a reset | DESIGN | hardware test | `HW-B6-006` |
| `HW-REQ-SENSOR-006` | Every acquisition carries 54 well-formed features | PROTOCOL | hardware test | `HW-B6-004`, `HW-B6-007`, `HW-B7-001` |
| `HW-REQ-SENSOR-007` | Malformed shapes are actually detected | DESIGN | hardware test | `HW-B7-009` |
| `HW-REQ-SENSOR-008` | Data-ready latency is inside the driver budget | PRODUCTION_CONFIG | hardware test | `HW-B6-004`, `HW-B7-005` |
| `HW-REQ-SENSOR-009` | Spectral output is stable and characterized | NONE | hardware test | `HW-B7-002` |
| `HW-REQ-SENSOR-010` | Saturation and degenerate values are observable | DESIGN | hardware test | `HW-B7-010` |
| `HW-REQ-SENSOR-011` | Ambient light does not reach the sample | NONE | hardware test | `HW-B7-011` |
| `HW-REQ-SENSOR-012` | The named illumination is the one that lights | DESIGN | hardware test | `HW-B7-006` |
| `HW-REQ-SENSOR-013` | Illumination is off after success AND failure | DESIGN | hardware test | `HW-B6-007`, `HW-B7-007`, `HW-B7-008`, `HW-B9-005` |
| `HW-REQ-SENSOR-014` | An unconfirmable off-state is reported as such | DESIGN | hardware test | `HW-B7-008` |
| `HW-REQ-SENSOR-015` | The I2C bus is electrically sane | NONE | hardware test | `HW-B0-010` |
| `HW-REQ-SENSOR-016` | The bus can be enumerated on demand | NONE | hardware test | `HW-B6-001` |
| `HW-REQ-SERVO-001` | The servo driver comes up | PROTOCOL | hardware test | `HW-B2-001` |
| `HW-REQ-SERVO-002` | The servo is the configured servo | PRODUCTION_CONFIG | hardware test | `HW-B2-002`, `HW-B2-003` |
| `HW-REQ-SERVO-003` | The servo is in the mode the driver assumes | PRODUCTION_CONFIG | hardware test | `HW-B2-002` |
| `HW-REQ-SERVO-004` | A stationary servo reports a stable position | NONE | hardware test | `HW-B2-004` |
| `HW-REQ-SERVO-005` | The deployed firmware is the shipped firmware | PRODUCTION_CONFIG | hardware test | `HW-B2-005` |
| `HW-REQ-SERVO-006` | A malformed servo command is refused, not obeyed | PROTOCOL | hardware test | `HW-B2-007` |
| `HW-REQ-SERVO-007` | Raw servo bytes are observable | NONE | hardware test | `HW-B2-006`, `HW-B3-004` |
| `HW-REQ-SERVO-008` | Servo telemetry is observable | NONE | hardware test | `HW-B2-008` |
| `HW-REQ-SERVO-009` | Torque and stop behave as commanded | PROTOCOL | hardware test | `HW-B2-009` |
| `HW-REQ-SERVO-010` | The servo mode survives a reset | PRODUCTION_CONFIG | hardware test | `HW-B2-010` |
| `HW-REQ-SERVO-011` | Closing error is characterized | NONE | hardware test | `HW-B4-003`, `HW-B4-006` |
| `HW-REQ-SERVO-012` | Movement timing is characterized | PRODUCTION_CONFIG | hardware test | `HW-B4-004` |
| `HW-REQ-SERVO-013` | The servo bus is clean under load | DESIGN | hardware test | `HW-B4-005` |
| `HW-REQ-SERVO-014` | Direction and angle behave symmetrically | NONE | hardware test | `HW-B4-001`, `HW-B4-002` |
| `HW-REQ-SERVO-015` | Thermal and load effects are characterized | NONE | hardware test | `HW-B4-007` |
| `HW-REQ-THERM-001` | Component temperatures are bounded | NONE | hardware test | `HW-B11-007` |

## Test to requirements, with readiness

| Test | Campaign | Safety | Automation | Iterations | Requirements | Readiness |
| --- | --- | --- | --- | --- | --- | --- |
| `HW-B0-001` | B0 | READ_ONLY | AUTOMATIC | - | `HW-REQ-ENV-001` | READY_FOR_HARDWARE |
| `HW-B0-002` | B0 | READ_ONLY | AUTOMATIC | - | `HW-REQ-ENV-002`, `HW-REQ-ENV-003` | READY_FOR_HARDWARE |
| `HW-B0-003` | B0 | READ_ONLY | AUTOMATIC | - | `HW-REQ-ENV-004` | READY_FOR_HARDWARE |
| `HW-B0-004` | B0 | READ_ONLY | AUTOMATIC | - | `HW-REQ-ENV-004`, `HW-REQ-LINK-008` | READY_FOR_HARDWARE |
| `HW-B0-005` | B0 | READ_ONLY | OPERATOR_ASSISTED | - | `HW-REQ-ENV-005`, `HW-REQ-ENV-009` | READY_FOR_HARDWARE |
| `HW-B0-006` | B0 | READ_ONLY | OPERATOR_ASSISTED | - | `HW-REQ-ENV-006` | **BLOCKED** (`bench.unit_identified`) |
| `HW-B0-007` | B0 | READ_ONLY | OPERATOR_ASSISTED | - | `HW-REQ-ENV-007` | READY_FOR_HARDWARE |
| `HW-B0-008` | B0 | READ_ONLY | OPERATOR_ASSISTED | - | `HW-REQ-ENV-008`, `HW-REQ-PWR-001` | **BLOCKED** (`bench.multimeter`) |
| `HW-B0-009` | B0 | READ_ONLY | AUTOMATIC | - | `HW-REQ-ENV-010` | READY_FOR_HARDWARE |
| `HW-B0-010` | B0 | READ_ONLY | OPERATOR_ASSISTED | - | `HW-REQ-PWR-004`, `HW-REQ-SENSOR-015` | **BLOCKED** (`bench.oscilloscope`) |
| `HW-B1-001` | B1 | COMMUNICATION | AUTOMATIC | - | `HW-REQ-LINK-001` | READY_FOR_HARDWARE |
| `HW-B1-002` | B1 | COMMUNICATION | AUTOMATIC | - | `HW-REQ-LINK-002` | READY_FOR_HARDWARE |
| `HW-B1-003` | B1 | COMMUNICATION | AUTOMATIC | - | `HW-REQ-LINK-003` | READY_FOR_HARDWARE |
| `HW-B1-004` | B1 | COMMUNICATION | AUTOMATIC | 5 OPEN_CYCLE (max 200, qual 5) | `HW-REQ-LINK-004` | READY_FOR_HARDWARE |
| `HW-B1-005` | B1 | ENDURANCE | AUTOMATIC | 100 OPEN_CYCLE (max 2000, qual 100) | `HW-REQ-LINK-005` | READY_FOR_HARDWARE |
| `HW-B1-006` | B1 | ENDURANCE | AUTOMATIC | 1000 REQUEST (max 20000, qual 1000) | `HW-REQ-LINK-006` | READY_FOR_HARDWARE |
| `HW-B1-007` | B1 | COMMUNICATION | AUTOMATIC | 50 REQUEST (max 5000) | `HW-REQ-LINK-007` | READY_FOR_HARDWARE |
| `HW-B1-008` | B1 | MANUAL_DISCONNECT | OPERATOR_ASSISTED | - | `HW-REQ-LINK-008` | READY_FOR_HARDWARE |
| `HW-B1-009` | B1 | COMMUNICATION | AUTOMATIC | 200 REQUEST (max 20000, qual 200) | `HW-REQ-LINK-009` | READY_FOR_HARDWARE |
| `HW-B1-010` | B1 | ILLUMINATION | AUTOMATIC | - | `HW-REQ-LINK-010` | READY_FOR_HARDWARE |
| `HW-B1-011` | B1 | COMMUNICATION | OPERATOR_ASSISTED | - | `HW-REQ-LINK-011` | READY_FOR_HARDWARE |
| `HW-B1-012` | B1 | COMMUNICATION | AUTOMATIC | 200 REQUEST (max 20000) | `HW-REQ-LINK-012` | READY_FOR_HARDWARE |
| `HW-B2-001` | B2 | COMMUNICATION | AUTOMATIC | - | `HW-REQ-SERVO-001` | READY_FOR_HARDWARE |
| `HW-B2-002` | B2 | COMMUNICATION | AUTOMATIC | - | `HW-REQ-SERVO-002`, `HW-REQ-SERVO-003` | READY_FOR_HARDWARE |
| `HW-B2-003` | B2 | COMMUNICATION | AUTOMATIC | - | `HW-REQ-SERVO-002` | READY_FOR_HARDWARE |
| `HW-B2-004` | B2 | COMMUNICATION | AUTOMATIC | 20 REQUEST (max 500) | `HW-REQ-SERVO-004` | READY_FOR_HARDWARE |
| `HW-B2-005` | B2 | COMMUNICATION | AUTOMATIC | - | `HW-REQ-SERVO-005` | READY_FOR_HARDWARE |
| `HW-B2-006` | B2 | COMMUNICATION | AUTOMATIC | - | `HW-REQ-SERVO-007` | **BLOCKED** (`diagnostic.servo_raw`) |
| `HW-B2-007` | B2 | COMMUNICATION | OPERATOR_ASSISTED | - | `HW-REQ-SERVO-006` | READY_FOR_HARDWARE |
| `HW-B2-008` | B2 | COMMUNICATION | AUTOMATIC | - | `HW-REQ-SERVO-008` | **BLOCKED** (`diagnostic.servo_feedback`) |
| `HW-B2-009` | B2 | MOTION | OPERATOR_ASSISTED | - | `HW-REQ-SERVO-009` | READY_FOR_HARDWARE |
| `HW-B2-010` | B2 | POWER_CYCLE | OPERATOR_ASSISTED | - | `HW-REQ-SERVO-010` | READY_FOR_HARDWARE |
| `HW-B2-011` | B2 | COMMUNICATION | AUTOMATIC | - | `HW-REQ-DIAG-001`, `HW-REQ-DIAG-002` | **BLOCKED** (`diagnostic.agent`) |
| `HW-B2-012` | B2 | COMMUNICATION | OPERATOR_ASSISTED | - | `HW-REQ-DIAG-003` | READY_FOR_HARDWARE |
| `HW-B3-001` | B3 | MOTION | OPERATOR_ASSISTED | - | `HW-REQ-H002-001`, `HW-REQ-H002-004`, `HW-REQ-H002-006` | READY_FOR_HARDWARE |
| `HW-B3-002` | B3 | MOTION | OPERATOR_ASSISTED | - | `HW-REQ-H002-002` | READY_FOR_HARDWARE |
| `HW-B3-003` | B3 | MOTION | AUTOMATIC | 5 MOVEMENT (max 100) | `HW-REQ-H002-003` | READY_FOR_HARDWARE |
| `HW-B3-004` | B3 | COMMUNICATION | AUTOMATIC | - | `HW-REQ-SERVO-007` | **BLOCKED** (`diagnostic.servo_raw`) |
| `HW-B3-005` | B3 | MOTION | OPERATOR_ASSISTED | - | `HW-REQ-H002-005` | READY_FOR_HARDWARE |
| `HW-B4-001` | B4 | MOTION | AUTOMATIC | 5 MOVEMENT (max 200) | `HW-REQ-SERVO-014` | READY_FOR_HARDWARE |
| `HW-B4-002` | B4 | MOTION | AUTOMATIC | 5 MOVEMENT (max 100) | `HW-REQ-SERVO-014` | READY_FOR_HARDWARE |
| `HW-B4-003` | B4 | MOTION | AUTOMATIC | 50 MOVEMENT (max 500) | `HW-REQ-SERVO-011` | READY_FOR_HARDWARE |
| `HW-B4-004` | B4 | MOTION | AUTOMATIC | 20 MOVEMENT (max 500) | `HW-REQ-SERVO-012` | READY_FOR_HARDWARE |
| `HW-B4-005` | B4 | MOTION | AUTOMATIC | 20 MOVEMENT (max 500, qual 20) | `HW-REQ-SERVO-013` | READY_FOR_HARDWARE |
| `HW-B4-006` | B4 | ENDURANCE | AUTOMATIC | 200 MOVEMENT (max 5000) | `HW-REQ-SERVO-011`, `HW-REQ-END-003` | READY_FOR_HARDWARE |
| `HW-B4-007` | B4 | MOTION | OPERATOR_ASSISTED | 30 MOVEMENT (max 500) | `HW-REQ-SERVO-015` | **BLOCKED** (`bench.representative_load`) |
| `HW-B4-008` | B4 | MOTION | OPERATOR_ASSISTED | - | `HW-REQ-PWR-002` | **BLOCKED** (`bench.multimeter`) |
| `HW-B5-001` | B5 | MOTION | OPERATOR_ASSISTED | - | `HW-REQ-CAR-001`, `HW-REQ-CAR-002` | READY_FOR_HARDWARE |
| `HW-B5-002` | B5 | MOTION | AUTOMATIC | - | `HW-REQ-CAR-001` | READY_FOR_HARDWARE |
| `HW-B5-003` | B5 | MOTION | OPERATOR_ASSISTED | - | `HW-REQ-CAR-003` | READY_FOR_HARDWARE |
| `HW-B5-004` | B5 | MOTION | AUTOMATIC | 5 MOVEMENT (max 100) | `HW-REQ-CAR-004` | READY_FOR_HARDWARE |
| `HW-B5-005` | B5 | MOTION | AUTOMATIC | 3 ROTATION (max 100) | `HW-REQ-CAR-005` | READY_FOR_HARDWARE |
| `HW-B5-006` | B5 | MOTION | OPERATOR_ASSISTED | - | `HW-REQ-CAR-006` | READY_FOR_HARDWARE |
| `HW-B5-007` | B5 | MOTION | OPERATOR_ASSISTED | - | `HW-REQ-CAR-007` | **BLOCKED** (`bench.representative_load`) |
| `HW-B5-008` | B5 | MOTION | OPERATOR_ASSISTED | 5 MOVEMENT (max 100) | `HW-REQ-CAR-008` | READY_FOR_HARDWARE |
| `HW-B5-009` | B5 | MOTION | OPERATOR_ASSISTED | - | `HW-REQ-CAR-009` | READY_FOR_HARDWARE |
| `HW-B5-010` | B5 | MOTION | AUTOMATIC | - | `HW-REQ-CAR-010` | READY_FOR_HARDWARE |
| `HW-B6-001` | B6 | READ_ONLY | AUTOMATIC | - | `HW-REQ-SENSOR-016` | **BLOCKED** (`diagnostic.i2c_scan`) |
| `HW-B6-002` | B6 | ILLUMINATION | AUTOMATIC | - | `HW-REQ-SENSOR-001`, `HW-REQ-SENSOR-002` | READY_FOR_HARDWARE |
| `HW-B6-003` | B6 | READ_ONLY | AUTOMATIC | - | `HW-REQ-LINK-003` | READY_FOR_HARDWARE |
| `HW-B6-004` | B6 | ILLUMINATION | AUTOMATIC | - | `HW-REQ-SENSOR-006`, `HW-REQ-SENSOR-008` | READY_FOR_HARDWARE |
| `HW-B6-005` | B6 | ILLUMINATION | AUTOMATIC | 50 MEASUREMENT (max 1000, qual 50) | `HW-REQ-SENSOR-003` | READY_FOR_HARDWARE |
| `HW-B6-006` | B6 | RESET | AUTOMATIC | - | `HW-REQ-SENSOR-005`, `HW-REQ-REC-003` | READY_FOR_HARDWARE |
| `HW-B6-007` | B6 | ILLUMINATION | AUTOMATIC | - | `HW-REQ-SENSOR-006`, `HW-REQ-SENSOR-013` | READY_FOR_HARDWARE |
| `HW-B6-008` | B6 | ENDURANCE | AUTOMATIC | 200 MEASUREMENT (max 5000, qual 200) | `HW-REQ-SENSOR-003` | READY_FOR_HARDWARE |
| `HW-B7-001` | B7 | ILLUMINATION | AUTOMATIC | - | `HW-REQ-SENSOR-006` | READY_FOR_HARDWARE |
| `HW-B7-002` | B7 | ILLUMINATION | AUTOMATIC | 100 MEASUREMENT (max 5000, qual 100) | `HW-REQ-SENSOR-009` | READY_FOR_HARDWARE |
| `HW-B7-003` | B7 | ENDURANCE | AUTOMATIC | 200 MEASUREMENT (max 5000) | `HW-REQ-END-002` | READY_FOR_HARDWARE |
| `HW-B7-004` | B7 | MANUAL_DISCONNECT | OPERATOR_ASSISTED | - | `HW-REQ-SENSOR-004` | READY_FOR_HARDWARE |
| `HW-B7-005` | B7 | ILLUMINATION | AUTOMATIC | 10 MEASUREMENT (max 200) | `HW-REQ-SENSOR-008` | READY_FOR_HARDWARE |
| `HW-B7-006` | B7 | ILLUMINATION | OPERATOR_ASSISTED | - | `HW-REQ-SENSOR-012` | READY_FOR_HARDWARE |
| `HW-B7-007` | B7 | ILLUMINATION | OPERATOR_ASSISTED | - | `HW-REQ-SENSOR-013` | READY_FOR_HARDWARE |
| `HW-B7-008` | B7 | FAULT_INJECTION | OPERATOR_ASSISTED | - | `HW-REQ-SENSOR-013`, `HW-REQ-SENSOR-014` | READY_FOR_HARDWARE |
| `HW-B7-009` | B7 | ILLUMINATION | AUTOMATIC | - | `HW-REQ-SENSOR-007` | READY_FOR_HARDWARE |
| `HW-B7-010` | B7 | ILLUMINATION | OPERATOR_ASSISTED | - | `HW-REQ-SENSOR-010` | READY_FOR_HARDWARE |
| `HW-B7-011` | B7 | ILLUMINATION | OPERATOR_ASSISTED | - | `HW-REQ-SENSOR-011` | READY_FOR_HARDWARE |
| `HW-B7-012` | B7 | ILLUMINATION | OPERATOR_ASSISTED | - | `HW-REQ-PWR-003` | **BLOCKED** (`bench.multimeter`) |
| `HW-B8-001` | B8 | FULL_SYSTEM | AUTOMATIC | - | `HW-REQ-INT-001` | READY_FOR_HARDWARE |
| `HW-B8-002` | B8 | FULL_SYSTEM | AUTOMATIC | - | `HW-REQ-INT-002` | READY_FOR_HARDWARE |
| `HW-B8-003` | B8 | FULL_SYSTEM | AUTOMATIC | - | `HW-REQ-INT-003` | READY_FOR_HARDWARE |
| `HW-B8-004` | B8 | FULL_SYSTEM | AUTOMATIC | - | `HW-REQ-INT-004` | READY_FOR_HARDWARE |
| `HW-B8-005` | B8 | FULL_SYSTEM | OPERATOR_ASSISTED | - | `HW-REQ-INT-005` | READY_FOR_HARDWARE |
| `HW-B9-001` | B9 | MANUAL_DISCONNECT | OPERATOR_ASSISTED | - | `HW-REQ-REC-001`, `HW-REQ-REC-002` | READY_FOR_HARDWARE |
| `HW-B9-002` | B9 | MANUAL_DISCONNECT | OPERATOR_ASSISTED | - | `HW-REQ-REC-001`, `HW-REQ-REC-002` | READY_FOR_HARDWARE |
| `HW-B9-003` | B9 | MANUAL_DISCONNECT | OPERATOR_ASSISTED | - | `HW-REQ-REC-001`, `HW-REQ-REC-002` | READY_FOR_HARDWARE |
| `HW-B9-004` | B9 | MANUAL_DISCONNECT | OPERATOR_ASSISTED | - | `HW-REQ-REC-002`, `HW-REQ-REC-003` | READY_FOR_HARDWARE |
| `HW-B9-005` | B9 | FAULT_INJECTION | OPERATOR_ASSISTED | - | `HW-REQ-REC-002`, `HW-REQ-SENSOR-013` | READY_FOR_HARDWARE |
| `HW-B9-006` | B9 | RESET | AUTOMATIC | - | `HW-REQ-REC-003` | READY_FOR_HARDWARE |
| `HW-B9-007` | B9 | POWER_CYCLE | OPERATOR_ASSISTED | - | `HW-REQ-REC-003`, `HW-REQ-REC-004`, `HW-REQ-REC-005` | READY_FOR_HARDWARE |
| `HW-B9-008` | B9 | FAULT_INJECTION | OPERATOR_ASSISTED | - | `HW-REQ-REC-003` | READY_FOR_HARDWARE |
| `HW-B9-009` | B9 | FAULT_INJECTION | OPERATOR_ASSISTED | - | `HW-REQ-REC-007` | READY_FOR_HARDWARE |
| `HW-B9-010` | B9 | FAULT_INJECTION | OPERATOR_ASSISTED | - | `HW-REQ-REC-006` | READY_FOR_HARDWARE |
| `HW-B10-001` | B10 | READ_ONLY | AUTOMATIC | - | `HW-REQ-FLOW-001` | READY_FOR_HARDWARE |
| `HW-B10-002` | B10 | FULL_SYSTEM | OPERATOR_ASSISTED | - | `HW-REQ-FLOW-002` | READY_FOR_HARDWARE |
| `HW-B10-003` | B10 | FULL_SYSTEM | OPERATOR_ASSISTED | - | `HW-REQ-FLOW-003`, `HW-REQ-FLOW-006` | READY_FOR_HARDWARE |
| `HW-B10-004` | B10 | FAULT_INJECTION | OPERATOR_ASSISTED | - | `HW-REQ-FLOW-005` | READY_FOR_HARDWARE |
| `HW-B10-005` | B10 | FULL_SYSTEM | OPERATOR_ASSISTED | - | `HW-REQ-FLOW-004`, `HW-REQ-FLOW-006` | READY_FOR_HARDWARE |
| `HW-B10-006` | B10 | FULL_SYSTEM | OPERATOR_ASSISTED | - | `HW-REQ-FLOW-007` | READY_FOR_HARDWARE |
| `HW-B11-001` | B11 | ENDURANCE | AUTOMATIC | 5000 REQUEST (max 100000, qual 5000) | `HW-REQ-END-001`, `HW-REQ-END-007` | READY_FOR_HARDWARE |
| `HW-B11-002` | B11 | ENDURANCE | AUTOMATIC | 1000 MEASUREMENT (max 20000, qual 1000) | `HW-REQ-END-002` | READY_FOR_HARDWARE |
| `HW-B11-003` | B11 | ENDURANCE | AUTOMATIC | 1000 MOVEMENT (max 20000, qual 1000) | `HW-REQ-END-003` | READY_FOR_HARDWARE |
| `HW-B11-004` | B11 | ENDURANCE | AUTOMATIC | 50 ROTATION (max 2000, qual 50) | `HW-REQ-END-004` | READY_FOR_HARDWARE |
| `HW-B11-005` | B11 | ENDURANCE | AUTOMATIC | 50 MEASUREMENT (max 1000, qual 50) | `HW-REQ-END-005` | READY_FOR_HARDWARE |
| `HW-B11-006` | B11 | ENDURANCE | OPERATOR_ASSISTED | 500 REQUEST (max 20000) | `HW-REQ-END-006` | READY_FOR_HARDWARE |
| `HW-B11-007` | B11 | ENDURANCE | OPERATOR_ASSISTED | 50 MOVEMENT (max 2000) | `HW-REQ-THERM-001` | **BLOCKED** (`bench.thermal_probe`) |
| `HW-B12-001` | B12 | FULL_SYSTEM | OPERATOR_ASSISTED | - | `HW-REQ-MISSION-001`, `HW-REQ-MISSION-002`, `HW-REQ-MISSION-003` | READY_FOR_HARDWARE |
| `HW-B12-002` | B12 | FULL_SYSTEM | OPERATOR_ASSISTED | 3 MISSION (max 20, qual 3) | `HW-REQ-MISSION-004` | READY_FOR_HARDWARE |
