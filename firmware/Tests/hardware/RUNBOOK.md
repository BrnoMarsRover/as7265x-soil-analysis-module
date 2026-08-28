# Real-hardware runbook

What to do, in order, on the day the module is on the bench.

> **Mostly not executed.** 24 of the 107 tests have real hardware
> evidence from 2026-08-27 (17 PASS, the rest BLOCKED on a human
> observation). Everything from B3 onward is `NOT_RUN`. This is the
> procedure, not a record.
>
> Two tests carry `ever_failed` in the ledger — **HW-B1-009, HW-B2-004** — meaning they
> failed on hardware before later passing. That history is deliberate
> and must not be cleared: HW-B1-009 is a real transport anomaly
> (one damaged frame in ~800 requests) and HW-B2-004 failed for an
> adapter defect that is now fixed.

---

## 0. Day one, in order

The short sheet. Every phase below has a full section later; this is the
sequence, the pass condition and the stop condition, in the order you
will actually work.

**`<PORT>` is resolved once, in step B, and reused everywhere.**

| # | Do this | PASS when | STOP if |
| --- | --- | --- | --- |
| A | `git status` in the repo; confirm you are on the frozen tree | 29 modified, 9 untracked, HEAD `e2848f2` | anything else — restore from the checkpoint first |
| B | identify the device (below) | exactly one CP2102 `10C4:EA60` | zero or two matches — do not guess a `ttyUSB` number |
| C | `device.py status --port <PORT>` | filesystem matches the manifest | stale files present → `deploy --clean` |
| D | `device.py deploy --port <PORT> --clean` then `verify` | every SHA256 matches, and `RECEIPT PASS` | any mismatch — do not proceed to tests |
| E | **preflight** (below) | board answers, protocol 2, sensor + servo states as expected | no answer, or PROTOCOL MISMATCH → re-deploy |
| F | `--run-campaign B0` | environment and identity recorded | B0 FAIL — the bench is not what you think |
| G | `--run-campaign B1` | link qualified | transport FAIL → stop; everything above rides on it |
| H | `--run-campaign B2` | ID 1, STEP mode, pins as configured | mode ≠ 3, or no answer → servo/power, not software |
| I | `--run HW-B2-013` | the reported position changes by the commanded counts | it does not — H-002 has recurred; **stop, capture, do not change tolerance** |
| J | B3 → B4 → B5 | `HW-B3-006`, `HW-B3-008` pass first | either fails |
| K | B6, B7 | sensor qualified | AS7265X_NOT_FOUND recurs → sensor campaign, not carousel |
| L | B8: 180 out → WHITE → UV → IR → 180 back | one full transaction, sample home | any stage FAIL → recovery flow, capture evidence |
| M | repeat L ×10, then ×25 | closing error stays bounded | drift grows → stop, characterize |
| N | all four slots, 1→2→3→4→1 | every transition verified | wrap-around fails → geometry, not transport |
| O | B9 recovery | faults handled honestly | — |
| P | B10, B12 mission rehearsal | operator workflow end to end | — |

### B. Identify the device

```bash
ls -l /dev/serial/by-id/
```

Use the `by-id` path as `<PORT>`. It survives replugging; `ttyUSB2` does
not. If more than one CP2102 is present, `--list` the candidates and
decide deliberately — **the framework will refuse to guess, and so
should you.**

### E. Read-only preflight

```bash
python3 firmware/PC/rover_science_client.py --port <PORT> --preflight --save-evidence firmware/Tests/hardware/artifacts/
```

Moves nothing, writes no EPROM, changes no torque, runs no acquisition.
Reports board identity and protocol, sensor state, servo link/id/mode,
encoder, supply voltage and temperature, carousel validity, and the
transport counters. **Run this first, and again any time something goes
strange.**

### D. What the deployment receipt is for

A successful deploy writes
`firmware/Tests/hardware/artifacts/deployment-receipt.json` with the
SHA256 of each of the seven files it verified onto the device.

`verify` compares the device against **both** a fresh rebuild and that
receipt. The rebuild is only reproducible from the checkout that
deployed — mpy-cross embeds the source path — so if you ever see every
file mismatch at once, read the `RECEIPT` line: if it PASSES, the
firmware is the firmware you deployed and the rebuild is the thing
that is wrong.

### I. H-002 — resolved, and what to check if it comes back

**Resolved on hardware 2026-08-28.** In step servo mode register 56 is
the FOLLOWING ERROR, not a position, and the absolute position is
register 67 minus it. See `artifacts/H-002-evidence-20260828.md`.

Every movement record now carries both halves, and reading them in
order is the whole diagnosis:

| field | what it says |
| --- | --- |
| `trajectory_travelled` | how far the servo was TOLD to go. Open loop — it reaches the target under a stall too |
| `measured_travel` | how far the shaft actually went |
| `following_error` | how far the shaft is from the target now |
| `verification` | `VERIFIED`, `UNVERIFIED` or `FAILED` |

`trajectory_travelled` equal to the request while `measured_travel` is
near zero means the command ran and the MECHANISM did not follow —
a stall, a slipping coupling, an exhausted torque budget. Neither of
them advancing means the command never reached the servo — bus, power
or protocol.

**Do not** raise `ST3215_POSITION_TOLERANCE`, disable encoder
verification, or substitute `trajectory_travelled` for
`measured_travel`. The last of those would pass a carousel that never
turned, which is exactly what the trajectory register does under a
stall.

### I.1 The trajectory register runs out, and it does so in service

Register 67 accumulates NET one-directional travel and clamps at
**32766**. Past the clamp the servo will not move in either direction
and still reports a following error of about 2 — a movement that looks
verified and did not happen.

A carousel that takes the shortest path to each slot advances one slot
every time, so a run working through slots 1-2-3-4-1 turns a full
revolution every four samples and reaches the clamp after about thirty
slot advances. **This is routine.**

The driver folds the register back before it gets there: it passes
through position servo mode, which reseeds register 67 from the
absolute encoder, and it carries the logical carousel angle across
unchanged. Nothing moves. It writes EPROM, so it is counted —
`trajectory_reseeds` in the servo telemetry, beside
`trajectory_headroom`, which is how far there is left to go.

If you ever see `SERVO_TRAJECTORY_LIMIT`, the fold could not be made:
run the servo configuration command, then re-synchronize.

---

## 0.1 Degraded modes — what is still true

| Situation | Still trusted | Next safe action | Never |
| --- | --- | --- | --- |
| **Link lost** | nothing about the mechanism | re-open the port, `--preflight` | assume the carousel stayed put |
| **Servo OFFLINE** | sensor, link, records | sensor diagnostics, B6/B7 can still run | any carousel campaign |
| **Servo ONLINE, position UNKNOWN** | servo link, sensor, all records | look at the plate, then **re-sync** | re-send the failed movement |
| **Sensor UNAVAILABLE** | servo, carousel position | sensor diagnostics; the measure path re-probes and may recover it | moving a sample to chase a sensor fault |
| **Position UNVERIFIED** | the servo link, the records, and the position itself — it is believed, not proven | read the status again; the position is measurable, it just was not measured at the one moment it mattered | treating it as a mechanical failure, or re-syncing working hardware |
| **Return move failed** | the spectra — they are acquired and saved | re-sync, then continue | treating the record as a normal success |
| **PC client restarted** | everything in `BD/` — samples, measurements, learning | reconnect; the client re-reads all state from the firmware | trusting any remembered position |
| **ESP32 reset** | records on the PC | re-connect servo, re-sync | assuming position survived the reboot |

A campaign that fails now prints what failed, whether a test that could
move the mechanism was among them, and the exact `--resume` command.
`--resume` only **skips** tests that already passed on hardware; it
never replays one.

---

## 1. Bench equipment

| | Needed for | Without it |
| --- | --- | --- |
| The module: ESP32, ST3215, AS7265x, carousel | everything | nothing runs |
| USB cable to the Linux main computer | everything | nothing runs |
| External servo supply | any movement | B2 onward blocked |
| **Protractor or angle gauge** | **H-002 (B3)** | the most important campaign cannot run |
| Reference mark on the plate | B3, B5, B11 | rotation drift unmeasurable |
| Multimeter | B0-008, B4-008, B7-012 | those tests report BLOCKED |
| Oscilloscope or logic analyzer | B0-010 | signal integrity BLOCKED |
| Thermal probe | B11-007 | thermal baseline BLOCKED |
| Bounded representative load | B4-007, B5-007 | loaded characterization BLOCKED |
| Stable reflectance target | B7 spectral stability | reduced |
| Camera or photodiode that sees IR | B7-006 | IR observation is UNKNOWN |
| Ability to detach the plate from the shaft | B3 shaft-vs-plate | coupling slip not isolable |

A BLOCKED test is not a failure. It is the framework saying which
instrument it needs, by name, before you power anything.

---

## 2. Pre-power checklist

Do all of this **before** applying power.

- [ ] Supply polarity checked at the connector.
- [ ] Connector identity confirmed — nothing is one pin out.
- [ ] ESP32 and servo supply share a common ground.
- [ ] Servo current does **not** pass through the sensor PCB.
- [ ] Power can be removed without reaching past anything that moves.
- [ ] The mechanism can be stopped by hand or by cutting power.
- [ ] Carousel is empty and free to turn a full revolution.
- [ ] Nobody is looking into the sensor head (UV will be switched on).
- [ ] A reference mark exists on the plate.

`HW-B0-005` asks each of these again, one at a time, and records the
answers. Answer them honestly; a confirmation you did not actually check
is worse than a BLOCKED test.

**Never** short a rail, reverse polarity, overvolt anything, force a
brownout, stall the servo, hot-plug what is not designed for it, or
defeat a fuse. No test in this campaign asks for any of that, and if one
appears to, stop and read it again.

---

## 3. Create the bench profile

```bash
cp firmware/Tests/hardware/configuration/profile.example.json firmware/Tests/hardware/configuration/my-bench.json
```

The example is a template and **thirteen tests are BLOCKED until it is
filled in**. Each names the field it needs:

| Field | Unblocks |
| --- | --- |
| `unit.module_id` | `HW-B0-006` |
| `instruments.multimeter` | `HW-B0-008`, `HW-B4-008`, `HW-B7-012` |
| `instruments.oscilloscope` | `HW-B0-010` |
| `instruments.thermal_probe` | `HW-B11-007` |
| `fixtures.representative_load` | `HW-B4-007`, `HW-B5-007` |
| `diagnostic_firmware.deployed` | `HW-B2-006`, `HW-B2-008`, `HW-B2-011`, `HW-B3-004`, `HW-B6-001` |

An instrument with no calibration date is refused by the profile
validator.

---

## 4. Identify the hardware unit

A prerequisite PASS earned on one module says **nothing** about another,
and the layer gates check that by fingerprint. So:

```bash
python3 firmware/Tests/hardware/run_hardware_tests.py --run HW-B0-004 --confirm-hardware --port /dev/ttyUSB0
```

That enumerates and opens nothing. Read `usb_serial_number` out of the
run's `measurements.csv`, put it in `my-bench.json` along with
`unit.module_id`, and use `--profile` from then on. After this the
campaign never needs to be told a device name again and **cannot open
the wrong board**.

---

## 5. Production versus diagnostic firmware

Run everything you can on the **competition firmware**. Only five tests
need the diagnostic agent, and it is deployed for a diagnostic session
and removed afterwards.

Before deploying it, record the production firmware hash. Then follow
`test_side_firmware/DEPLOYMENT.md` exactly, verify with `HW-B2-011`, and
restore with `HW-B2-012`.

> **Every prerequisite PASS earned while the agent was deployed is void**
> for a production run. The run fingerprint includes the diagnostic
> firmware, so the gate will refuse them — by design. Re-run what
> matters on the restored firmware.

---

## 6. Execution order

```bash
python3 firmware/Tests/hardware/run_hardware_tests.py --profile firmware/Tests/hardware/configuration/my-bench.json --run-campaign B0 --confirm-hardware
```

Then, in this order:

| Step | Campaign | Why here |
| --- | --- | --- |
| 1 | **B0** | inventory. Nothing connected, nothing opened. |
| 2 | **B1** | the link, measured on its own terms. |
| 3 | **B2** | the servo answers, and is the servo we think it is. |
| 4 | **B3** | **H-002.** The most important campaign. |
| 5 | **B6, B7** | the sensor — *in parallel, not blocked by H-002*. |
| 6 | **B4** | repeatability; the measurement that sets the tolerance. |
| 7 | **B5** | the carousel, once the encoder is trustworthy. |
| 8 | **B8** | the whole transaction, including the RF-001 regression. |
| 9 | **B9** | take things away and see what is admitted. |
| 10 | **B10** | a human, the real client, the real archive. |
| 11 | **B11** | the long runs. |
| 12 | **B12** | the rehearsal. Never first. |

Replace `B0` with each campaign in turn. The gates enforce the order
anyway; this is what to type.

---

## 7. Stop conditions

**Stop the campaign and diagnose** if any of these happen:

- `HW-B2-002` reports the servo is not in STEP mode. That is H-002
  hypothesis 1 and everything above B2 is meaningless until it is
  settled.
- `HW-B2-004` shows a position that changes with nothing commanded. A
  read that is not stable at rest cannot measure a movement.
- `HW-B3-001` shows the encoder and the protractor disagreeing. **Do not
  proceed to B5.** The result names which hypotheses remain open.
- `HW-B8-002` reproduces RF-001. Every carousel result above B3 is
  invalid until it is explained.
- Any ESP32 reset during movement (`HW-B4-008`). That is a brownout and
  it will masquerade as a firmware fault in every later campaign.
- Any protected reference file hash changes (`HW-B10-005`). Stop
  immediately; that is the one kind of test failure that costs more than
  the bug it was looking for.
- An illuminator left on after a failure (`HW-B7-008`, `HW-B9-005`).

**Do not** widen a tolerance, a timeout or a retry count to get past any
of these.

---

## 8. H-002 first

B3 is the campaign the whole servo side waits on. Before running it:

1. `HW-B2-002` must show `mode_correct` true.
2. `HW-B2-004` must show a stable position at rest.
3. Have a protractor and a reference mark.
4. **Detach the plate from the output shaft if you can.** The test asks
   whether the *encoder* tracks the *shaft*; a coupling that slips is a
   separate hypothesis and the assembled mechanism cannot tell them
   apart. If you cannot detach it, say so when asked and measure the
   plate.

```bash
python3 firmware/Tests/hardware/run_hardware_tests.py --profile firmware/Tests/hardware/configuration/my-bench.json --describe HW-B3-001
```

Read that before starting. Then:

```bash
python3 firmware/Tests/hardware/run_hardware_tests.py --profile firmware/Tests/hardware/configuration/my-bench.json --run HW-B3-001 --confirm-hardware
```

Answer every angle and direction question honestly. **UNKNOWN is a
correct answer** and produces INCONCLUSIVE, which is worth far more than
a guess that produces a PASS.

---

## 9. Evidence

Every run writes its own directory under `artifacts/`. Nothing there is
committed.

**Back it up as soon as a campaign finishes**, before the next one:

```bash
cp -r firmware/Tests/hardware/artifacts/HW-<run-id> ~/freya-evidence/
```

A number quoted in a report with no run id behind it is an opinion; a
run id with no archived directory behind it is a broken reference.

---

## 10. When something fails

1. The result already carries a defect record under `defects/` with a
   persistent `HW-xxx-nnn` id, the observed and expected behaviour, and
   reproduction steps.
2. Fill in root cause, fix, verification and regression test as the work
   happens. An empty "root cause" is a true statement about a fresh
   defect.
3. If the cause turns out to be reachable in software, add a regression
   test to `Tests/software` — that is where it will be caught next time
   for free.
4. Fix it. Then re-run:

```bash
python3 firmware/Tests/hardware/run_hardware_tests.py --profile firmware/Tests/hardware/configuration/my-bench.json --resume HW-<run-id> --run-campaign B4 --confirm-hardware
```

`--resume` skips only tests that actually **passed on hardware** in that
run, and refuses a run whose fingerprint no longer matches.

---

## 11. What invalidates a previous PASS

The run fingerprint covers all of this. When any of it changes, the
affected prerequisites stop opening gates — automatically, not by
anyone remembering:

- the repository commit, or a dirty working tree;
- the production ESP32 firmware or its configuration;
- the PC client or the protocol;
- the bench profile;
- **the physical module, servo, sensor or carousel assembly**;
- the mechanical assembly revision — a re-coupled plate is a different
  mechanism as far as H-002 is concerned;
- the test-side diagnostic firmware, deployed or removed;
- the hardware framework itself.

If you rebuild the mechanism, bump `unit.carousel_assembly_revision`.
Everything that depended on the old one will correctly stop counting.

---

## 12. Restore production firmware

Before the module goes anywhere near a competition:

```bash
python3 -m mpremote connect <device> fs rm :diagnostic_agent.py
python3 firmware/tools/device.py --port <device> --deploy
python3 firmware/Tests/hardware/run_hardware_tests.py --profile firmware/Tests/hardware/configuration/my-bench.json --run HW-B2-012 --confirm-hardware
```

`HW-B2-012` verifies the production firmware answers, the diagnostic
protocol does not, and the profile no longer claims a deployment.

---

## 13. Command reference

Safe — these touch nothing:

```bash
python3 firmware/Tests/hardware/run_hardware_tests.py --list
```

```bash
python3 firmware/Tests/hardware/run_hardware_tests.py --capabilities
```

```bash
python3 firmware/Tests/hardware/run_hardware_tests.py --dry-run --all
```

```bash
python3 firmware/Tests/hardware/offline_tests/run_offline.py
```

Per campaign, with a profile and an explicit confirmation:

```bash
python3 firmware/Tests/hardware/run_hardware_tests.py --profile firmware/Tests/hardware/configuration/my-bench.json --run-campaign B1 --confirm-hardware
```

Endurance runs are off by default, need their own confirmation, and
refuse a sample size below their own qualification minimum — a smaller
run produces CHARACTERIZATION, never a qualification PASS.
