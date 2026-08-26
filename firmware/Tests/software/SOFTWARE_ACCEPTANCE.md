# Software Acceptance Record

> **SUPERSEDED BY PHASE A.3.** This file is the A.2 record and is kept
> as history. The current state of the software is in
> `SOFTWARE_ASSURANCE_REPORT.md`, with `SOFTWARE_FAULT_CATALOG.md`,
> `SOFTWARE_FAILURE_MATRIX.md` and `SOFTWARE_ASSUMPTIONS.md` beside it.
>
> A.3 found seven further defects — three of which would have been felt
> on the field — and the numbers below have all moved: 28 suites became
> 37, 2,366 checks became 3,188, and 23 mutations became 37.

**Date:** 2026-08-24
**Phase:** A.2 — final software closure before hardware verification
**Repository state:** uncommitted working tree; no commits, no pushes

---

## 1. Status

```
SOFTWARE ACCEPTANCE:  PASS
```

No known software-testable defect remains in the competition-relevant
system. Every defect found across the two campaigns is fixed and
regression-protected. Every remaining uncertainty is assigned to
hardware (`HARDWARE_VERIFICATION_PLAN.md`) or to offline tooling.

This is **not** a claim that the software is free of bugs. It is the
claim that we have tried hard to break it and can no longer do so
without hardware.

---

## 2. The numbers

| | |
| --- | --- |
| Suites | 28 |
| Checks | 2366 |
| Runs compared | 2, byte-identical |
| `BD/` after every run | 24 files, unchanged |
| Mutations attempted / killed | 23 / 23 |
| Statement + branch coverage | 71% |
| Third-party dependencies on the competition path | 1 (`pyserial`) |

Phase A ended at 22 suites and 1787 checks; A.2 added 6 suites and 579
checks, and closed the two coverage gaps the previous report named.

---

## 3. How to reproduce this result

```bash
python3 firmware/Tests/run_all.py                     # the campaign
python3 firmware/Tests/software/mutation.py           # does it have teeth
python3 firmware/Tests/software/coverage_report.py    # branch coverage
```

The first is the acceptance gate. The second and third are diagnostic
and are **not** part of it: `mutation.py` edits production files (and
restores them byte-for-byte, verified by hash), and
`coverage_report.py` needs `coverage.py`, which is a development
dependency and must never be needed to run the campaign.

---

## 4. Defects found and fixed

### Phase A.2 — the real Linux bench failures

These were observed with a rover on a desk, not imagined.

| ID | Severity | Defect | Fix |
| --- | --- | --- | --- |
| RF-001 | **P0** | A failed half turn reported `moved: False`, so the operator was told "carousel: nothing was moved" after a rotation they had just watched | The servo attaches motion evidence to every failure; the carousel derives a three-way verdict (`NOT_STARTED` / `MOVED` / `UNKNOWN`); the screen states which, and tells the operator to look at the mechanism |
| RF-002 | **P0** | `PORT_LOST` closed the link correctly, and the next `hardware_status()` killed the application with `RuntimeError("Link is not open")` | A closed link raises `PORT_CLOSED`, a `LinkError` — the language the 39 existing handlers already catch |
| RF-003 | **P1** | The same crash from the sensor test, on the second attempt | Same root cause; fixed by the same change |
| RF-004 | **P1** | Connection loss was handled as one failed call rather than an application state | Centralised in the one serial owner; every screen verified against a lost port |
| RF-005 | **P1** | A measurement failure could not be located in the sequence | The firmware names the stage (`PRECHECK` / `MOVE_TO_SCANNER` / `ACQUISITION`); the screen prints a different sentence for each |

### Phase A.2 — found by this campaign

| Severity | Defect | Fix |
| --- | --- | --- |
| **P1** | Request ids were seeded from `time.time()*1000 % 1000000`, which **wraps every 1000 seconds** — two clients started 17 minutes apart produced identical ids, and `wait_online` makes `ping` the first command of every session | A random 24-bit session nonce from `os.urandom` plus a counter: `<nonce>-<n>`. The firmware echoes it verbatim, so no protocol change |
| **P1** | Browsing a **migrated legacy record** crashed the records screen: three tables formatted `match.get("rank")` into `{:<4}`, and migrated `reference_matches` have no rank | `rank_of()`, matching the module's existing `score()`/`number()` discipline |
| **P1** | The measurement pre-flight "proved" the sensor from a cached flag, so a sensor that died since the last measurement passed and the carousel made the whole 180° journey before anyone found out | `ensure_ready(probe=True)` — one register read before anything moves |
| **P2** | `print_decision` formatted `candidate.get("evidence_level", "-")`, which returns `None` when the key is present and null — the same blind spot as `rank`, reachable from stored AnalysisRuns | `or "-"` |

### Phase A — carried forward

Stale-frame acceptance, NaN as a carousel angle, NaN on the wire, a full
disk crashing the client, the phantom in-memory Sample, RAW stored by
reference, `--help` rewriting DB3, the Linux port classification, three
names that did not exist, the bus scan losing the origin silently, and
non-finite values through `normalize()`. All fixed in Phase A; all still
regression-protected here.

---

## 5. Coverage

Measured with `coverage.py 7.15.4`, **branch mode**, over the suites
that execute production code.

Reported by class, because a single number cannot distinguish a
hardware-only branch from a forgotten one.

| Class | Closure |
| --- | --- |
| Mission-critical, software-testable | executed and asserted |
| Operator-reachable, software-testable | executed |
| Offline-only (`research/`, `tools/`, `Science/model_registry.py`) | imports, `--help`, no destructive execution |
| Hardware-only | listed in `HARDWARE_VERIFICATION_PLAN.md` |

Combined statement + branch coverage of the four production domains:
**71%** (9481 statements, 2850 branches). The figures that moved in
this campaign:

| Module | Before A.2 | After A.2 |
| --- | --- | --- |
| `PC/workflow/display.py` | 52% | **76%** |
| `PC/workflow/records.py` | 34% | **49%** |
| `PC/serial_link.py` | 85% | 85% |
| **Total** | 67% | **71%** |

`records.py` remains the lowest at 49% and that is the honest number.
What is now covered is every flow an operator reaches during and after
a mission: listing, opening, editing, renaming, deleting, importing,
the learning history, and a record migrated from the old schema. What
remains uncovered is the ground-truth labelling machinery -
`ask_material`, `ask_family`, `ask_mixture`, `ask_sample_context` -
which is a long branching interview used offline, after the
competition, to label observations for model training. It is
classified **OFFLINE_ONLY**: it cannot affect a measurement, a
carousel position or a saved spectrum.

### Classification of what remains uncovered

- **`Science/model_registry.py` — 0%, OFFLINE_ONLY.** It has **no
  callers anywhere**: only a mention in `Science/__init__.py`'s
  docstring. It is intentional API for offline model promotion (§50),
  not dead code from a removed architecture, and it is not on the
  mission path. Verified: it imports, it does not write on import, and
  its status vocabulary is coherent. Not removed — it is a documented
  future workflow, not an accident.
- **`ESP32/boot.py` — one line, deliberately does nothing.**
- **Defensive branches in the ESP32 drivers** that require an I²C or
  UART failure mode the fakes cannot produce (a half-written register,
  a frame corrupted in a specific byte). Listed as hardware items.
- **`research/`** — offline analysis and ERC planning. Verified for
  safe import and safe `--help`; branch coverage is not pursued.

---

## 6. Mutation verification

`mutation.py` applies 23 mutations to decisions that can cause an
operational failure, runs the suites that should notice, and restores
every file byte-for-byte.

The one survivor was a real finding: `Carousel.require_position` could
be turned into a no-op and the carousel state suite still passed,
because its assertion was `code != "OK"` rather than the exact code.
The assertion was strengthened to `POSITION_NOT_SYNCHRONIZED` and the
mutation now dies.

Two bugs in the harness itself were found and fixed the same way: a
text-mode restore that rewrote line endings (caught by its own hash
check), and multi-line patterns that stopped matching once the restore
was made byte-exact.

---

## 7. Exception audit

215 exception handlers in mission code (`PC`, `Science`, `BD`,
`ESP32`), of which 45 are broad (`except Exception`).

- **0** bare `except:` — enforced statically.
- **7** silent `except Exception: pass`, every one best-effort cleanup:
  releasing a port, clearing a buffer after a reset, switching lamps off
  in a `finally` beside a bare `raise`, and the last-resort attempt to
  tell the host a response could not be sent. Each is listed by name in
  `static/test_static_api.py`; a new one fails the suite and has to be
  justified in the same change that introduces it.
- **0** instances of the §16 pattern — a programming error assigned into
  a plausible-looking value. Every assigning handler records the failure
  into a field the operator sees.

The most important of the silent handlers is `sensor.acquire_one`: if
its lamp-off raised, it would **mask** the `SensorError` being
re-raised, and the operator would be told the lamp failed rather than
why the acquisition did.

---

## 8. Request identity

The question: can an answer to another question be accepted as the
answer to ours?

Two designs have already failed here — a counter from 1, and a
clock-seeded counter that wrapped every 1000 seconds. The current
design is `<24 random bits>-<counter>`, and a frame is accepted only
when the id matches, the frame carries `ok`, **and** the command name
matches.

`contracts/test_request_identity.py` proves the point that matters: not
that a collision is unlikely, but that a collision is **not sufficient**.
Every check there forges a matching id and asks whether the frame is
accepted anyway.

---

## 9. Reset and restart

`state_machine/test_reset_recovery.py` covers all four combinations of
PC and ESP32 restart, plus a reset at each point of a command.

The property enforced: **a position never survives a reset.** If the
firmware has forgotten where the carousel is, no PC-side memory may put
a slot number back on screen — the servo could have been turned by hand
while the board was down, and a remembered number is indistinguishable
from a measured one once displayed.

Also verified: boot banners and startup tracebacks are preserved for
diagnosis and are never mistaken for an answer; the device's retained
acquisition buffer is volatile, which is why RAW is persisted the
instant it arrives.

---

## 10. Data integrity

Every suite writes through `SandboxBD`, a temporary tree seeded from the
real reference data. `run_software.py` hashes every file under
`firmware/BD/` before and after the whole campaign and fails the run if
one byte moved — a check that does not depend on anybody remembering to
use the sandbox.

Verified across every campaign run in this phase: **BD/ unchanged.**

---

## 11. Test architecture

```
firmware/Tests/
├── run_all.py                 software only; refuses --hardware
├── software/
│   ├── run_software.py        the acceptance gate + BD/ hash guard
│   ├── mutation.py            does the suite have teeth (diagnostic)
│   ├── coverage_report.py     branch coverage (diagnostic)
│   ├── fakes/                 clock, serial port, ESP32, console, BD
│   ├── static/ unit/ contracts/ integration/ fault_injection/
│   ├── state_machine/ linux/ entrypoints/ data_integrity/
│   └── stress/ randomized/ regression/
└── hardware/
    ├── run_hardware.py        --port required, --move to turn anything
    ├── hardware_validation.py
    └── HARDWARE_VERIFICATION_PLAN.md
```

Mocking is at the lowest practical boundary: `serial.Serial`,
`machine.I2C`, `machine.UART`, the clock, the keyboard, and the
directory records live in. Everything above that line is production
code, including the ESP32 firmware — `LoopbackDevice` runs the real
`Protocol` dispatcher in-process.

---

## 12. What is still uncertain

No P0 or P1 software-testable risk is open.

Remaining uncertainty is hardware, and every item has an ID in
`HARDWARE_VERIFICATION_PLAN.md`:

- **H-002** is the one that matters. On the bench a carousel visibly
  rotated 180° while the encoder reported 2 counts. The software
  arithmetic is verified correct at all 4096 positions; **why the
  encoder and the operator disagreed is unknown and cannot be
  determined from software.**
- **H-001** — whether 15 counts is the right tolerance.
- **H-003** — real AS7265x ready latency.
- **H-004** — CP2102 reliability, and why `/dev/ttyUSB0` disappeared.
- **H-005** — whether a half turn is really 2048 counts on this
  mechanism. If there is a reduction, H-002 explains itself.
- **H-006** — physical backlash.

---

## 13. The acceptance question

> If the ESP32, ST3215, AS7265x and USB link behaved exactly according
> to the assumptions our software interfaces represent, is there any
> known software reason the competition workflow would fail, corrupt
> data, lose logical state, accept false success or become
> unrecoverable?

**No.** The evidence is the campaign: every command executed against the
real firmware; every screen entered and abused; every external operation
failed on purpose; both state machines walked exhaustively; all four
restart combinations; a forged-collision attack on request identity;
23 mutations of safety-critical decisions, all killed; and the archive
provably untouched.

The honest qualifier: this holds **given the assumptions**. H-002 is
evidence that at least one of them was wrong on real hardware, and the
software could not have known.
