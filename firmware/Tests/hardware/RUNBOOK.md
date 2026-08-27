# Real-hardware runbook

What to do, in order, on the day the module is on the bench.

> **Nothing in this runbook has been executed.** All 107 tests are
> `NOT_RUN`. This is the procedure, not a record.

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
