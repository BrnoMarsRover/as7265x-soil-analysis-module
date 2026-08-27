# Hardware verification

Real hardware. A real CP2102, a real ESP32, a real ST3215, a real
AS7265x, and a carousel that turns.

> ## Status: NO HARDWARE HAS BEEN EXECUTED
>
> Every one of the **107 registered tests** is `NOT_RUN`, across **13
> campaigns** and **113 requirements**. 94 are `READY_FOR_HARDWARE` —
> their definitions, prerequisites, capabilities and configuration have
> all been checked offline and they will execute the moment a board is
> connected. 13 are `BLOCKED`, and every one of them is blocked on
> something you can supply: a field in the bench profile, an instrument,
> a fixture, or the test-side diagnostic agent.
>
> **Nothing in this directory is evidence about the hardware.** The
> framework's own test suite passes — 1,678 checks against a fake
> transport — and that says the harness is ready. It says nothing about
> a carousel.
>
> Start at [RUNBOOK.md](RUNBOOK.md) when the module is on the bench.

---

## What a result says now

A single status could not tell these apart, so there are three
independent axes and `status` is a projection of them:

| Axis | Values | Question it answers |
| --- | --- | --- |
| readiness | `READY` `BLOCKED` | could the procedure start? |
| execution | `NOT_RUN` `RUNNING` `COMPLETED` `ABORTED` `ERROR` | what happened once it did? |
| verdict | `NOT_EVALUATED` `PASS` `FAIL` `INCONCLUSIVE` `CHARACTERIZATION` | what does the evidence say about the hardware? |

The two the old model could not express are the ones that matter most:

- **`INCONCLUSIVE`** — the procedure ran and a required observation was
  missing or ambiguous. An operator who answered UNKNOWN; a field the
  firmware did not return; a fault injected outside its target window.
  Not a failure, and emphatically not a pass.
- **`CHARACTERIZATION`** — measurements were collected and no
  authoritative requirement exists to judge them. Most of B4 is this
  until somebody writes down what the closing error is allowed to be.
  Of 113 requirements, **33 have no authoritative source** and say so;
  those can only ever characterize.

**A `PASS` needs five things**, and each was a way to fake one before:
the body completed, at least one check was made, every check passed,
every `REQUIRED` observation was actually obtained, and the run met its
own declared qualification sample size.

---

## Why this is separate from `Tests/software`

`Tests/software` is 40 suites and 3495 checks that run against fake
hardware. It proves the firmware's logic and proves nothing about the
wiring, the servo, the sensor or the mechanism. This campaign is the
other half.

The separation is not tidiness. `run_all.py` is the command run by
reflex — in a hook, from an editor, by someone who is not standing next
to the mechanism — and the carousel may be holding samples or be
mechanically constrained. **A test suite that can turn an actuator must
never be reachable by reflex.** So:

- `Tests/run_all.py` cannot reach this tree.
- The default invocation of `run_hardware_tests.py` prints a list and
  exits.
- There is no default port and there will not be one.
- A real run needs `--confirm-hardware`, and then a second, specific
  confirmation for whatever class of test was selected.
- The only files under `Tests/hardware/` named `test_*.py` are in
  `offline_tests/`, and every one of them runs on a fake transport.

Tripwires around `serial.Serial`, `serial.serial_for_url`,
`list_ports.comports`, `SerialLink.open`, `SerialLink.hard_reset`,
`SerialLink.available_ports`, `os.open` on any device path, and
`subprocess` invocation of mpremote, esptool, ampy, rshell or the
diagnostic agent confirm that import, `--help`, `--list`,
`--describe`, `--capabilities`, every campaign dry run and the entire
offline suite perform **zero** hardware operations. Each tripwire
raises; one that merely counted could be called and ignored.

---

## Installation

Python 3.8 or newer, and the same `pyserial` the operator client uses:

```bash
python3 -m pip install -r firmware/PC/requirements.txt
```

On a fresh Linux account, before the first real run:

```bash
sudo usermod -aG dialout $USER
```

then log out and back in — group membership is read at login. See
`PORT_DENIED` in `Documentation/OPERATIONS.md`.

---

## Configuration

Copy the example and edit the copy. Your copy is git-ignored; the
example is tracked.

```bash
cp firmware/Tests/hardware/configuration/profile.example.json firmware/Tests/hardware/configuration/my-bench.json
```

The profile describes the bench: which device, at which baud, with which
limits. It does **not** duplicate the firmware's constants —
`firmware/ESP32/config.py` is read directly, so the campaign always
qualifies the numbers the firmware actually uses.

The profile keeps three kinds of number apart, and the distinction
matters more than it looks:

| Provenance | Meaning | Example |
| --- | --- | --- |
| `CONFIGURED` | what the firmware does, true by definition | `ST3215_POSITION_TOLERANCE = 15` |
| `ASSUMED` | what we believe the mechanism is, unmeasured | 4096 counts per revolution; a 1.0 gear ratio |
| `MEASURED` | what a hardware test actually observed | filled in by B3 and B4 |

H-002 exists because at least one `ASSUMED` value may be wrong.

**There is no default port.** `/dev/ttyUSB0` is an enumeration order,
not a name: plug in a second USB serial device and the science module
moves to `ttyUSB1` with nothing visibly changing. The profile names a
`/dev/serial/by-id` path, a USB serial number, or a VID/PID — and an
ambiguous selector is an error, never a choice.

---

## Safe commands — these touch nothing

```bash
python3 firmware/Tests/hardware/run_hardware_tests.py --list
```

```bash
python3 firmware/Tests/hardware/run_hardware_tests.py --describe HW-B3-001
```

```bash
python3 firmware/Tests/hardware/run_hardware_tests.py --capabilities
```

```bash
python3 firmware/Tests/hardware/run_hardware_tests.py --dry-run --all
```

`--dry-run` checks every gate that can be checked without a device:
the definition, the prerequisites, the capabilities and the profile. It
does not execute a single test body and opens no transport, so a dry
run **cannot** produce hardware evidence. Its artifacts are stamped
`DRY_RUN / NO_HARDWARE_EVIDENCE` in the directory name, the manifest and
every event line.

The framework's own tests:

```bash
python3 firmware/Tests/hardware/offline_tests/run_offline.py
```

---

## Running one real test, later

### The first run bootstraps the profile

A profile identifies the board by its stable USB identity — and the way
to *learn* that identity is `HW-B0-004`, which needs a profile. So the
very first run is bootstrapped with an explicit device, for the
inventory only:

```bash
python3 firmware/Tests/hardware/run_hardware_tests.py --run HW-B0-004 --confirm-hardware --port /dev/ttyUSB0
```

That test opens nothing — it enumerates what the OS already knows. Read
`usb_serial_number` out of the run's `measurements.csv`, put it in your
profile, and use `--profile` from then on. After that the campaign never
has to be told a device name again, and cannot open the wrong board.

### Every run after that

```bash
python3 firmware/Tests/hardware/run_hardware_tests.py --profile firmware/Tests/hardware/configuration/my-bench.json --run HW-B0-001 --confirm-hardware
```

A whole campaign:

```bash
python3 firmware/Tests/hardware/run_hardware_tests.py --profile firmware/Tests/hardware/configuration/my-bench.json --run-campaign B1 --confirm-hardware
```

### Every option

| Option | Effect |
| --- | --- |
| `--list` | the catalogue. Touches nothing. |
| `--describe TEST-ID` | one complete definition. Touches nothing. |
| `--capabilities` | what the production system can and cannot be asked, and why |
| `--dry-run` | check every offline gate for the selection |
| `--run TEST-ID` | run one test. Repeatable. |
| `--run-campaign B3` | run a whole campaign. Repeatable. |
| `--all` | select everything |
| `--confirm-hardware` | **required** for any real run |
| `--port DEVICE` | an exact device, overriding the profile's selector |
| `--profile PATH` | the bench profile |
| `--iterations N` | override the repeat count, bounded by each test and by the profile |
| `--output PATH` | where the evidence directory goes |
| `--non-interactive` | never prompt; tests needing a human become `BLOCKED` |
| `--resume RUN-ID` | skip tests that already passed on hardware in that run |
| `--stop-on-failure` | stop at the first `FAIL`, `ERROR` or `ABORT` |
| `--expert-override` | bypass a layer gate — needs `--override-reason` and a confirmation |
| `--json` | machine-readable `--list`, `--describe`, `--capabilities` |
| `--verbose` | print every check, not only the failures |

Exit codes: `0` clean, `1` a failure, `2` a usage or configuration
error, `3` a real run without `--confirm-hardware`, `4` aborted,
`5` something was blocked.

---

## What a result means

| Status | Meaning |
| --- | --- |
| `NOT_RUN` | nobody has looked at it |
| `READY_FOR_HARDWARE` | every offline gate passes; it runs when a board is connected |
| `SKIPPED` | a precondition that is nobody's fault, or a declined confirmation |
| `BLOCKED` | it cannot run, and the reason is named — a missing production interface, a missing operator, or a lower layer that has not passed |
| `PASS` | the real procedure ran on real hardware and satisfied its acceptance criteria |
| `FAIL` | the procedure ran and the hardware did not satisfy it |
| `ERROR` | the framework itself broke, or the test made no observation at all |
| `ABORTED` | Ctrl+C, or the operator answered ABORT |

**`PASS` requires hardware evidence, mechanically.** `TestResult`
refuses to be constructed with `PASS` and a non-hardware evidence class,
so a dry run and a framework self-test cannot produce one — it is a type
error rather than a convention somebody has to remember. A self-test run
that exercises a success path reports `SKIPPED`, with the reason "these
checks passed against a fake transport; this says nothing about the
hardware".

A campaign verdict is pessimistic: any `FAIL` makes it `FAIL`, and any
unrun test makes it `INCOMPLETE`. **99 passes and one unexplained
failure is not a `PASS`** — it is an intermittent fault with a
hundred-iteration sample, and every repeated campaign reports the first
failing iteration, because "failed at 3" and "failed at 4,987" are
different faults with the same failure count.

---

## Evidence

Every real run creates its own directory:

```text
artifacts/HW-20260826-141233-EXECUTE/
├── run_manifest.json     mode, evidence class, git commit, host, profile
├── events.jsonl          one JSON object per event, flushed as it happens
├── measurements.csv      the numbers, for a spreadsheet or a plot
├── summary.json          every result, machine-readable
├── summary.md            the same for a human
├── operator_notes.md     what the operator observed, in their words
├── raw_serial.log        raw wire text, when any was captured
└── defects/HW-SERVO-001.md
```

The mode is in the **directory name**, not only in a field inside it,
because evidence gets copied and pasted into reports and the first thing
anybody sees is the folder.

`events.jsonl` is flushed after every line. A run killed by a power cut,
a Ctrl+C or a kernel oops leaves behind everything that happened up to
that moment — partial evidence is the only kind an interrupted endurance
run can produce, and it is worth having.

Nothing under `artifacts/` is committed.

---

## Operator-assisted tests

Some things cannot be read off a serial port. Whether the carousel
physically turned 180 degrees is not knowable from the wire — that is
the entire content of H-002 — so the framework asks a human and records
the answer as an answer:

- every operator input is timestamped and written to the evidence;
- every input is validated (`about 180ish` is not an angle);
- `UNKNOWN` is always an acceptable answer and becomes `None`, never a
  fabricated zero;
- checks made from a human observation are marked `OPERATOR`, so a
  reader can always tell them from a measurement;
- `ABORT` is accepted at every prompt;
- in `--non-interactive` mode nothing is asked and **nothing is
  assumed** — the test becomes `BLOCKED`.

---

## Safety

The default invocation performs no hardware action. Beyond that, each
class asks its own question, once per run:

`READ_ONLY` · `COMMUNICATION` · `MOTION` · `ILLUMINATION` ·
`MANUAL_DISCONNECT` · `RESET` · `POWER_CYCLE` · `FAULT_INJECTION` ·
`ENDURANCE` · `FULL_SYSTEM`

Everything from `MOTION` upward needs a second confirmation on top of
`--confirm-hardware`, because "yes, the board is connected" and "yes,
the mechanism is clear and nothing is loaded" are different statements.

Endurance tests are off by default, need explicit bounded iteration
counts, and reject zero, negative, non-integer and over-ceiling values.

Fault injection introduces one controlled fault at a time — a cable, a
connector, a power switch. **Nothing in this campaign asks for an
electrical short, a reversed polarity, an overvoltage or a deliberate
brownout**; those damage hardware and prove nothing that pulling a
connector does not. An offline test scans every registered test's text
to keep it that way.

After uncertain movement or a lost link, **the carousel position is
UNKNOWN and a re-sync is required**. A reset or a power cycle does not
preserve confidence in a physical position, and B9 exists partly to
prove the firmware agrees. Cleanup tries to leave the illumination off
and to release the port; when it cannot confirm either, it records
`cleanup: {"confirmed": false}` with the reason rather than claiming
success.

**Never hide a failure by widening a tolerance, a timeout or a retry
count.** `ST3215_POSITION_TOLERANCE` is not touched by any test here.
HW-B4-003 measures the real closing-error distribution; any change to
that constant belongs in a commit, argued from those numbers, with the
run id attached.

---

## Interruption and cleanup

Ctrl+C anywhere — inside a body, inside a prompt, between tests —
produces an `ABORTED` result for the running test, runs its cleanup,
writes the summary and stops. Every test with a cleanup handler gets it
run after a pass, a failure, an exception and an abort. A cleanup that
throws is recorded as unconfirmed and never overwrites the body's
verdict: a test that failed and then cleaned up badly failed for the
reason it failed.

---

## Reporting a hardware defect

Failures get a persistent identifier from the test's own family, so a
defect can be referred to across runs:

`HW-SER-nnn` · `HW-USB-nnn` · `HW-SERVO-nnn` · `HW-CAR-nnn` ·
`HW-SENSOR-nnn` · `HW-INT-nnn` · `HW-MISSION-nnn`

Each is written to `defects/` with observed behaviour, expected
behaviour, reproduction steps, the evidence, the suspected layer, and
empty sections for root cause, fix, verification and regression test.
An empty "root cause" is a true statement about a fresh defect; fill it
in as the work happens, and add a regression test to `Tests/software`
when the cause turns out to be reachable there.

---

## Resuming after a failure

```bash
python3 firmware/Tests/hardware/run_hardware_tests.py --resume HW-20260826-141233-EXECUTE --run-campaign B4 --confirm-hardware --profile firmware/Tests/hardware/configuration/my-bench.json
```

`--resume` skips only tests that actually **passed on hardware** in the
named run. Fix the fault first: a resumed run that steps over a failure
is a run that has not been done.

---

## Layer gates

Layer *N+1* is not valid evidence while layer *N* is unresolved, so the
gates are enforced rather than documented. `B5` is gated by `B3` because
slot-addressed results mean nothing while the encoder and the mechanism
disagree; `B12` is gated all the way down to `B0`.

Only a **hardware** `PASS` opens a gate. Dry runs and self-tests write
nothing to the ledger.

The override exists for deliberate out-of-order diagnosis and costs
three things: `--expert-override`, a written `--override-reason`, and an
interactive confirmation. It is recorded permanently in the result and
in the event log, because an override that is not visible in the
evidence turns a diagnostic detour into a claim about the hardware.

---

## When production code changes

The framework reads the production system rather than describing it: the
firmware command table is parsed out of `protocol.py`, the PC command
surface is introspected from `SerialLink`, the constants come from
`config.py`, and the workflow screens are parsed out of the workflow
package. So a rename shows up as a **capability that has gone missing**
and the affected tests turn `BLOCKED` with the name in the reason —
rather than as a mysterious failure at the bench with a rover on the
field.

After any production change, run:

```bash
python3 firmware/Tests/hardware/offline_tests/run_offline.py
```

and re-read `--capabilities`. If a capability that used to be available
is now missing, the campaign that depended on it needs re-reading before
it is re-run.

---

## The thirteen tests that are BLOCKED today

None is blocked on a gap nobody can close. Every one names a thing you
can supply.

| Missing | Tests | How to unblock |
| --- | --- | --- |
| the test-side diagnostic agent | `HW-B2-006` `HW-B2-008` `HW-B2-011` `HW-B3-004` `HW-B6-001` | deploy it per [test_side_firmware/DEPLOYMENT.md](test_side_firmware/DEPLOYMENT.md) |
| `unit.module_id` | `HW-B0-006` | name the physical module in the bench profile |
| `instruments.multimeter` | `HW-B0-008` `HW-B4-008` `HW-B7-012` | declare it with model, serial and calibration date |
| `instruments.oscilloscope` | `HW-B0-010` | the same |
| `instruments.thermal_probe` | `HW-B11-007` | the same |
| `fixtures.representative_load` | `HW-B4-007` `HW-B5-007` | a bounded, documented mass — never a jam fixture |

**The competition firmware was not changed to unblock anything.** The
three original blockers needed raw servo bytes and an on-demand I2C
scan; adding either to a build that can reach a competition is a worse
defect than the gap it fills. They are provided instead by a read-only,
whitelisted, manually deployed agent under `test_side_firmware/`, which
never runs at boot and has no movement command at all.

`--capabilities` prints the full reason and recommendation for each.

---

## Related documents

- `PLAN.md` — the B0 to B12 order and the full traceability table
- `HARDWARE_VERIFICATION_PLAN.md` — the H-001 to H-008 assumptions and
  what would falsify each
- `PHASE_B_CAMPAIGNS.md` — the earlier prose procedures, and the owner
  of every `HARDWARE_ONLY` exception handler
- `Documentation/OPERATIONS.md` — running the instrument
- `../software/SOFTWARE_FREEZE.md` — the frozen software baseline this
  campaign sits on top of
