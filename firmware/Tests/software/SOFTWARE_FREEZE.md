# Software Freeze Record

**Date:** 2026-08-25
**Phase:** A — software verification
**Repository state:** uncommitted working tree; no commits, no pushes

---

## Status

```
PHASE A SOFTWARE VERIFICATION      CLOSED
SOFTWARE BASELINE                  FROZEN
READY FOR PHASE B                  YES
```

---

## What this baseline is

The software has been through three campaigns: Phase A, Phase A.2 and
Phase A.3, followed by this closure pass. Each was adversarial by
design — the question was never "does it work" but "what have we not
yet tried to break".

| | Phase A | A.2 | A.3 | **Closure** |
| --- | --- | --- | --- | --- |
| Suites | 22 | 28 | 37 | **40** |
| Checks | 1,787 | 2,366 | 3,188 | **3,495** |
| Mutations killed / attempted | — | 23 / 23 | 37 / 37 | **39 / 39** |
| Fault classes catalogued | — | — | 123 | **152** |
| Audit tools | — | 2 | 6 | **8** |
| Handlers driven individually | — | — | 83 / 220 | **129 / 220** |

---

## Closure metrics

```
OPEN_BLOCKER                                    0
UNCLASSIFIED_FAULTS                             0
MISSION_SOFTWARE_REACHABLE_UNEXECUTED_HANDLERS  0
MISSION_SOFTWARE_REACHABLE_UNCOVERED_BRANCHES   0
SURVIVING_NON_EQUIVALENT_CRITICAL_MUTATIONS     0
PROTECTED_DATA_CHANGES                          0

DEFERRED_ENVIRONMENT_VALIDATION                 1
```

The single deferred item is **ENV-LINUX-001**: execution of the
complete suite directly on `freya-1-comp`. It needs no hardware — no
carousel, no sensor, no servo — only the machine, which is not
currently available. Full procedure in
`firmware/Tests/hardware/PHASE_B_CAMPAIGNS.md`.

```bash
python3 firmware/Tests/run_all.py
```

---

## Handler closure

`audit/handler_coverage.py` measures which `except` bodies actually
execute, across all 220 handlers in `PC/`, `Science/`, `BD/` and
`ESP32/`.

Every handler is now classified as exactly one of:

| Classification | Meaning |
| --- | --- |
| `MISSION_RUNTIME_SOFTWARE_REACHABLE` | software alone can produce the condition — **and a test now drives it** |
| `SCREEN_VERIFIED` | the failure class is driven across every screen; this handler was not reached at its own point in that screen's sequence |
| `CLEANUP_ONLY` | best-effort release; the original exception stays authoritative |
| `HARDWARE_ONLY` | needs an I2C, UART or USB failure mode the fakes cannot produce — **each owned by a numbered Phase B test** |
| `DEFENSIVE_UNREACHABLE` | a valid call chain cannot produce the condition, and it is proved rather than asserted |
| `OFFLINE_ONLY` | not on the competition path |

The closure pass added three suites - `test_handler_closure.py`,
`test_loader_closure.py` and `test_residual_handlers.py` - which
between them drive the handlers no test had ever entered. 341 checks,
every injected fault proved to have fired by a counter rather than
assumed.

The untested set went 59 -> 36 -> 24 -> 18 -> 14 -> **0**. The final
fourteen are each declared by name in `audit/handler_coverage.py` with
the reason it cannot be driven, or the cheaper proof that covers it.

The closing tally over all 220:

```
EXECUTED               129   a test drives this exact handler
SCREEN_VERIFIED         53   the failure class is driven across every
                             screen; not this handler at its own point
HARDWARE_ONLY           18   owned by a numbered Phase B test
CLEANUP_ONLY            10   best-effort release, verified by reading
OFFLINE_ONLY             5   not on the competition path
DEFENSIVE_UNREACHABLE    5   proved, not asserted
                       ---
                       220
UNCLASSIFIED             0
```

**The discovery that made it possible:** most protocol handlers were
unreachable *by the tests*, not by the software. Every suite drove the
firmware through `service.dispatch()`, and `send_json`, `process_line`
and `serve_forever` sit above it. Four serialization handlers, the
serving loop's two rescue paths and the response-too-large fallback
could not be reached from below no matter what payload was sent. The
new suite enters the firmware where the wire does.

---

## Branch closure

`audit/branch_gaps.py` classifies every uncovered branch in the
mission-runtime set as `SOFTWARE_REACHABLE`, `DEFENSIVE_UNREACHABLE`,
`HARDWARE_ONLY` or `OFFLINE_ONLY`, reading its notion of "mission" from
the same table `module_inventory.py` uses, so the two cannot disagree.

---

## Fault catalogue

`SOFTWARE_FAULT_CATALOG.md` — 152 fault classes across eleven
subsystems, each with an ID, a trigger, the required behaviour, the test that
proves it, and exactly one status:

```
VERIFIED
HARDWARE_ONLY
OFFLINE_ONLY
NOT_APPLICABLE
DEFERRED_ENVIRONMENT_VALIDATION
```

No entry is `OPEN`, `UNKNOWN` or `TODO`.

---

## Test quality

`audit/test_quality.py` scans all 41 suite files for checks that cannot
fail: zero-assertion sections, `is not None` as the only claim, broad
`except: pass` around code under test, fault injection with no evidence
it fired, and any suite touching the production archive.

```
zero-assertion       0
only-is-not-None     0
swallowed-failure    0
unproven-injection   0
real-BD-write        0
```

Four weak tests were found and fixed across A.3 and this closure pass —
a patch applied to the wrong function, a fault raised with a stale
exception class, a case that did not do what its comment said, and a
suite that injected forty write failures without ever asserting one had
fired.

---

## Defects found and fixed

Phase A.3 found seven; this closure pass found two more - one of them
a P1.

| # | Sev | Defect |
| --- | --- | --- |
| 1 | **P0** | Raw `OSError` from pySerial's `in_waiting`/`flush` escaped as an unhandled traceback — the first thing that fails when `/dev/ttyUSB0` disappears |
| 2 | **P1** | Ctrl+D spun the main menu forever and never released the serial port |
| 3 | P2 | No host-side frame-size policy; an unterminated stream accumulated 2.08 MB |
| 4 | P2 | `RecursionError` from pathological JSON nesting escaped every handler |
| 5 | P2 | A Czech sample note crashed the records screen under `LANG=C` |
| 6 | P3 | `mae` returned bare `inf`; `rmse`/`euclidean` raised `OverflowError` |
| 7 | P3 | The two menu loops caught different exception sets while dispatching to overlapping screens |
| 8 | P3 | `kind == "neutral"` special-cased twice in `protocol.py` for a movement kind the ST3215 does not offer — a leftover from the removed continuous-rotation backend, proved unreachable and guarded by a retired-kinds contract check |
| 9 | **P1** | **DB2 never contributed to any decision.** For a 54-feature database the pipeline passed `comparison_channels = None`, and `metrics.paired` reads `channels or CHANNELS` — the eighteen *bare* band names. A 54-feature vector is keyed `white:A`, `uv:A`, `ir:A`, so nothing matched: measured, both sides carrying identical 54 keys, `paired()` returned **0 pairs** and every metric `None`, while the entry still reported itself available with 22 candidates. The multi-illumination database — the only one that can separate two materials whose white-light spectra agree — was silently absent from every measurement, and no screen said so |

Nine defects, all fixed and regression-protected. Seven have a
dedicated mutation that dies if the fix is removed.

**Defect 9 was found by the mutation campaign, not by a test.** A
mutation disabling the 54-feature branch survived every suite. Chasing
why revealed that the branch was already ineffective: the survivor was
not a missing test, it was a missing comparison. That is the strongest
argument in this document for keeping mutation testing in the loop.

---

## Hardware assumptions

`SOFTWARE_ASSUMPTIONS.md` — nine hardware assumptions (H-001…H-009) and
ten environment assumptions (A-001…A-010). Every one states what breaks
if it is false, and every hardware one is owned by a numbered test in
`PHASE_B_CAMPAIGNS.md`.

Two stand out:

- **H-002** — a carousel visibly rotated 180° while its encoder
  reported 2 counts. Standing evidence that at least one assumption was
  already wrong on real hardware, and that the software could not have
  known. HW-204 settles it, and nothing in the carousel campaign means
  anything until it does.
- **H-007** — real ESP32 heap fragmentation. The software proves its
  guards work when allocation fails; only HW-306 can say whether it
  will have to.

---

## Protected data

```
BD/ before and after every run:  24 files, byte-identical
```

`run_software.py` hashes every file under `firmware/BD/` around the
whole campaign and fails the run if one byte moves — a guard that does
not depend on anyone remembering to use the sandbox.

---

## Repository

```
commits:  none
pushes:   none
```

`git diff --check` reports no whitespace errors. The working tree after
a full campaign is identical to the working tree before it, apart from
the files this phase deliberately edited.

---

## The freeze

**No additional speculative software development is authorized after
this baseline.**

From this point the objective is to determine whether the real physical
hardware satisfies the assumptions the verified software depends upon.

Future production changes should be driven by exactly one of:

```
a hardware verification finding
a real operator or bench failure
a new requirement
a verified regression
```

Not by another theoretical test that could be imagined. The suite has
teeth — 37 of 37 mutations die — and adding cases without a specific
unresolved question behind them makes the number bigger and the
evidence no stronger.

---

## Conclusion

Within the software model, all known mission-relevant software-testable
failure classes, reachable handlers, critical state transitions,
interface conditions and recovery paths have been verified or explicitly
classified. No known software blocker remains. The only deferred
environment-level validation is execution of the complete suite directly
on `freya-1-comp`. All remaining engineering uncertainty is assigned to
Phase B hardware verification.

```
PHASE A SOFTWARE VERIFICATION CLOSED
SOFTWARE BASELINE FROZEN
READY FOR PHASE B HARDWARE VERIFICATION
```
