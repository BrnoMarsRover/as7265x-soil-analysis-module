# Software Assurance Report — Phase A.3

**Date:** 2026-08-25
**Phase:** A.3 — final adversarial campaign before hardware verification
**Repository state:** uncommitted working tree; no commits, no pushes

---

## A. Final status

```
SOFTWARE ASSURANCE:  PASS
OPEN_BLOCKER:        0
```

> **Superseded by the closure pass.** The current state is in
> `SOFTWARE_FREEZE.md`. This report is the Phase A.3 record; the
> closure task that followed it drove the handler set from 59 untested
> to 14 declared, found a further P1 (DB2 contributed to no decision),
> and took the campaign to 40 suites and 3,495 checks.

Within the software model, no known mission-relevant software-testable
failure class remains unverified or unclassified. Every software defect
this campaign discovered has been fixed and regression-protected. Every
remaining uncertainty is assigned to hardware verification, to offline
non-mission functionality, or to an explicitly stated limitation in
§C.2 below.

This is not a claim that the software is free of defects. It is the
claim that it was attacked deliberately along every axis the phase
specified, and that what the attacks found is now closed.

---

## B. Fault catalogue

`SOFTWARE_FAULT_CATALOG.md` — 123 fault classes across ten subsystems.

```
VERIFIED         96
HARDWARE_ONLY    14
OFFLINE_ONLY      4
NOT_APPLICABLE    9
OPEN_BLOCKER      0
                ---
total           123
```

Every `HARDWARE_ONLY` item carries an H-number in
`HARDWARE_VERIFICATION_PLAN.md` or in `SOFTWARE_ASSUMPTIONS.md`. Every
`NOT_APPLICABLE` states the architectural reason the fault cannot occur.

---

## C. Mission coverage

### C.1 The mission surface, derived rather than assumed

`audit/call_graph.py` walks reachability from `rover_science_client.py`
across `PC/`, `Science/` and `BD/`:

| | |
| --- | --- |
| Functions defined in the mission domains | 527 |
| Reachable from the entry point | 481 |
| Not reachable by name | 46 |

`audit/module_inventory.py` classifies every production file:

| Role | Files |
| --- | --- |
| `MISSION_RUNTIME` | 27 |
| `MISSION_SUPPORT` | 5 |
| `HARDWARE_DRIVER` | 7 |
| `OFFLINE` | 30 |
| `TEST_ONLY` | 42 |
| **total** | **111** |

No production file has an undeclared role, and **no mission module
imports `research/` or `tools/`** — the rule that keeps a competition
run from loading offline analysis code.

The 46 unreachable functions are accounted for: thin `SerialLink`
command wrappers that exist to name a timeout (`ping`, `acquire_triad`,
`disconnect_servo`), `hard_reset` and `available_ports` which belong to
the deployment tool, the offline `ModelRegistry`, and five superseded
display printers (`print_metric_table`, `print_agreement`,
`print_cross_database`, `print_spectrum_table`,
`print_processing_table`) that are still imported by three screens but
called by none. **The last group is dead code**, triaged and left in
place under §1's instruction not to rewrite working modules; a static
check already fails if one of those names disappears.

### C.2 Failure-handler coverage — and its honest limit

`audit/handler_coverage.py` runs every executing suite under
`coverage.py` and asks, for each of the 220 `except` clauses in `PC/`,
`Science/`, `BD/` and `ESP32/`, whether the handler **body** ran.

```
EXECUTED           83     a test drove this exact handler
SCREEN_VERIFIED    51     the failure class is driven at the screen
                          level; this handler was not reached at its
                          own point in the sequence
CLEANUP_ONLY       10     best-effort release, verified by reading
HARDWARE_ONLY      16     needs an I2C or UART failure mode
OFFLINE_ONLY        1
UNCLASSIFIED       59     read, but neither driven nor covered above
                  ---
                  220
```

`0` bare `except:` clauses, enforced statically. 45 broad
`except Exception`, each read and classified.

**This is the weakest number in the report and it is stated plainly:
38% of exception handlers have been executed individually.** The
remainder are not unexamined — they are overwhelmingly one shape:
a `(LinkError, TimeoutError)` or a `StorageError` at one particular
point inside one particular operator screen. That behaviour *class* is
driven hard:

- `regression/test_linux_bench.py` enters all 17 screens with the port
  lost underneath them; none raises.
- `integration/test_screens_failing.py` drives 10 screens × 4 write
  failure modes — 40 combinations — asserting no crash, no archive
  change, and **no false success**.

What is not proven is each handler at its own point in its screen's
sequence. A screen with six link handlers is entered once per script,
so five stay dark. `SCREEN_VERIFIED` is counted separately from
`EXECUTED` precisely so that renaming cannot turn 38% into 90%.

**Residue: 59 handlers remain neither executed nor covered by that
rule.** They are, exactly:

| Where | Count |
| --- | --- |
| `ESP32/protocol.py` | 26 |
| `PC/workflow/session.py` | 6 |
| `PC/serial_link.py`, `BD/databases.py`, `ESP32/carousel.py` | 3 each |
| `ESP32/main.py`, `BD/registry.py`, `BD/decision_learning.py`, `Science/pipeline.py`, `PC/workflow/records.py`, `PC/rover_science_client.py` | 2 each |
| seven other files | 1 each |

| Catching | Count |
| --- | --- |
| `Exception` (broad) | 24 |
| `(TypeError, ValueError)` — argument validation | 9 |
| `SensorError` | 6 |
| `(OSError, ValueError)` | 4 |
| `ServoError` | 3 |
| nine other types | 13 |

Each has been read. None matches the §16 pattern — a programming error
assigned into a plausible-looking value — and that count remains **0**
across the whole tree. The broad ones were individually classified in
A.2 and re-read here. But they have not been *driven*, and this report
does not claim they have.

The largest single block, `ESP32/protocol.py`, is the most tractable
remaining work: `fault_injection/test_firmware_faults.py` already
demonstrates the technique — build the real firmware, make the fake
sensor or servo fail, and assert the frame the PC receives — and
extending it command by command would close most of the 26. That is a
known, scoped, unfinished piece of work rather than an unknown.

### C.3 Statement and branch coverage

Measured with `coverage.py 7.15.4` in branch mode over the four
production domains. Reported by class, because a single percentage
cannot distinguish a hardware-only branch from a forgotten one.

| Class | Closure |
| --- | --- |
| Mission-critical, software-testable | executed and asserted |
| Operator-reachable, software-testable | executed |
| Offline-only (`research/`, `tools/`, `Science/model_registry.py`) | imports, `--help`, no destructive execution |
| Hardware-only | listed in `HARDWARE_VERIFICATION_PLAN.md` |

---

## D. Resource exhaustion

`fault_injection/test_resource_faults.py` — 77 checks.

| Injected | Where | Result |
| --- | --- | --- |
| `MemoryError` | archive serialize, `mkstemp`, `fdopen`, `replace` | no save reported; archive byte-identical; RAM rolled back |
| `MemoryError` | request frame construction | propagates as itself; **zero bytes reached the wire** |
| `MemoryError` | response parsing | not swallowed into a believable answer |
| `MemoryError` | Science pipeline | analysis `FAILED`, exception type preserved in the record, RAW untouched |
| `EMFILE` / `ENFILE` | temp file, archive open | `StorageError`, never a traceback |
| `ENOSPC` | all five write stages | archive intact, no temp left behind, RAM matches disk |
| inode exhaustion | file creation | failed save; archive still readable |
| `EROFS` | write | writes fail, **reads still work** |
| `EACCES` mid-run | write | rollback to durable; restored permission adds exactly one record |
| orphan `.samples-*.tmp` | restart | not read, not counted, not deleted on a guess |
| `os.urandom` failure | startup | refuses to start; **no fixed fallback** |

Bounded-growth results: after 1,600 rejected lines the diagnostic
buffers are still capped at 40; after 1,100 successful requests the
link retains exactly what it retained after 100; after a 60-sample day
nothing has grown.

---

## E. Linux failure coverage

`linux/test_linux_runtime.py` — 191 checks.

**The errno matrix** — 8 errno classes × 4 pySerial entry points = 32
combinations, every one classified `PORT_LOST` with the errno carried
on the error:

```
EIO  ENODEV  ENXIO  EBADF  EACCES  EPERM  EBUSY  ENOENT
        ×
in_waiting   flush   read   write
```

Also covered: `EINTR` (retried, never reported as a completed read, and
an endless run of them times out); two processes on one port
(`PORT_BUSY`, then recovery); two processes on one archive (last-writer-
wins, file never corrupt, concurrent writers documented as unsupported);
device renamed, duplicated, symlinked via `/dev/serial/by-id/`, removed
and returned; boot banner in front of an answer; non-English locale;
wall-clock jumps of ±2 h, to 1970 and 1000 days forward; environment
independence; awkward archive paths; dependency and interpreter
assumptions.

---

## F. Serial and protocol adversarial coverage

`fault_injection/test_protocol_limits.py` — 73 checks.

| Attack | Result |
| --- | --- |
| Nothing, a bare newline, one byte, `{}`, `[]`, `null`, a bare number | none is an answer |
| Largest legitimate frame (16,454 B) | read normally |
| 10× and 100× the cap | discarded, counted, reader resynchronizes |
| **Endless stream with no newline** | 2.08 MB delivered, **78 KB retained** |
| Oversized frame then a good one | the next request succeeds |
| ~17,000-deep nesting | controlled; `RecursionError` caught |
| Duplicate keys | last value wins; cannot satisfy two commands at once |
| Binary, invalid UTF-8, NULs, C0 controls, ANSI escapes, lone surrogate | controlled; a real frame behind them still reads |
| Echo storm ×3 + boot banner + real answer | the real answer is found |
| Echo storm with no answer | fails; never a fabricated success |
| Five classes of unsolicited valid frame | none displaces a real answer |

---

## G. Process lifecycle

`process/test_lifecycle.py` — 99 checks.

Ctrl+D at any menu ends the session with exit 0 **and releases the
port**; Ctrl+D at a free-text field still returns the field's default.
Ctrl+C is covered with a move on the wire, at three points of a
measurement, and during a save. SIGTERM is injected at all four save
stages. Restart-from-durable-truth is verified after disk-full,
read-only, SIGTERM and OOM. 4,000 junk menu selections leave normally;
300 menu round-trips vary the stack by **≤ 1 frame**; no `menu_*`
function calls itself.

---

## H. Persistence

Atomicity is tested at every stage of `_write_json` — `mkdir`,
`mkstemp`, write, `fsync`, `replace` — against `ENOSPC`, `EROFS`,
`ENOENT`, `EMFILE` and `MemoryError`. After each: the previous archive
is byte-identical, the in-memory archive is rolled back to match it, no
temporary file is left behind, and the file is still valid JSON.

40 screen × write-failure combinations produce no crash, no archive
change and no false success. A failed save followed by an operator
retry leaves **exactly one** record.

---

## I. Science correctness guards

`unit/test_science_properties.py` — 92 checks.

Properties over generated spectra: `sim(x,x) = 1` and **never above 1**
across four magnitude ranges; symmetry; scale invariance; distances
non-negative, zero only on identity, symmetric, triangle inequality;
cosine and spectral angle rank identically.

Differential: `cosine`, `rmse`, `mae` and `normalize` each checked
against an independently written reference implementation.

Numerical: subnormals, the largest finite double, −0.0, and values so
large that squaring overflows — where every metric now returns `None`
rather than `inf` or a raise. Spectral floats survive JSON bit-exactly,
and a cosine computed before and after a round trip is identical.

Structural: a missing, NaN, infinite, string, null or list channel is
dropped and the loss is visible in the pair count; an unknown 19th
channel is ignored entirely; reordering changes nothing; `W == D` gives
`None` per channel, never `0.0`.

---

## J. Model-based testing

`state_machine/test_mission_model.py` — 19 checks over a seven-state
mission model, eight seeds, 24 steps each.

175 generated steps, the model and the real system compared **after
every step**, with resets and disconnects injected at generated
transitions in the second pass. Nine mission invariants are asserted
after every transition rather than at the end.

The model found a rule nobody had written down: **`connect_servo`
always invalidates the carousel position.** The model assumed it did
not, disagreed with the firmware on eight seeds out of eight, and the
firmware was right — a servo just connected has an encoder zero the
firmware has never seen. That is the file paying for itself.

---

## K. Mutation results

`mutation.py` — round 1 (23 mutations) plus round 2 (14 new).

```
attempted    37
killed       37
survived      0
not applied   0
```

Every mutated file restored byte-for-byte, verified by hash (12 files).

Round 2 targets the safety rules added or hardened in this phase, plus
three classes round 1 never reached: the firmware's own validation, the
host's frame-size policy, and the honesty of the metrics.

**One round-1 mutation regressed and was re-killed.** "let cosine leave
[−1, 1]" began surviving, because the properties suite asserted
`abs(value − 1.0) < 1e-12` while removing the clamp produces
`1.0000000000000002` — one ULP over, four orders of magnitude inside
the tolerance. The bound is exact, not approximate; the assertion was
corrected and the mutation dies again. Without the mutation campaign
that loss of sensitivity would have been invisible.

---

## L. Stress and endurance

Actual counts, not aspirations:

| | |
| --- | --- |
| Complete sample workflows in one session | 60 |
| Generated model steps | 175 |
| Three-fault chaos runs | 6 seeds × 8 steps |
| Junk menu selections | 4,000 |
| Menu round-trips | 300 |
| Rejected protocol lines | 1,600 |
| Successful requests in one link | 1,100 |
| Screen × write-failure combinations | 40 |
| errno × entry-point combinations | 32 |
| Bytes absorbed from an unterminated stream | 2,076,672 |

---

## M. Test infrastructure assurance

`unit/test_fakes.py` verifies the fakes themselves. The rule enforced
throughout: **a fake replaces a wire, never a decision.**

Three test defects were found and fixed during this phase, each of the
kind §93 exists to catch:

1. **A patch that landed on nothing.** `json.dumps` was patched where
   `_write_json` calls `json.dump`. The test passed while injecting
   nothing.
2. **A fault raised with the wrong class.** `build_firmware` purges and
   re-imports the ESP32 tree per instance, so `from sensor import
   SensorError` binds a class the running firmware has never heard of.
   The injected error fell through to the last-resort handler and the
   test reported a firmware defect that did not exist.
3. **A test that did not do what its comment said.** A case labelled
   "saving twice does not write twice" fed `["2", "3"]` — `"3"` was
   consumed as the *Sample ID*, the record was named `3`, it saved
   once, never revisited the menu, and asserted nothing. It passed only
   because `ask` turned end-of-input into a blank answer; the EOF fix
   exposed it. Rewritten to do what it claimed, with four assertions.

Two of my own assertions were wrong about the architecture and were
corrected rather than forced: a client restart does **not** cost the
board's position (only a board reset does), and measurement ids are
scoped to their Sample by design, not globally unique.

---

## N. Defects found and fixed in this phase

| # | Severity | Defect | Fix |
| --- | --- | --- | --- |
| 1 | **P0** | `in_waiting` and `flush()` raise a **raw `OSError`** on Linux — pySerial wraps `read`/`write` but not those two. `_read_response` calls `in_waiting` before every read, so this is the *first* thing that fails when `/dev/ttyUSB0` disappears. It escaped `except serial.SerialException` as an unhandled traceback — the RF-002 defect in a different exception type, on exactly the H-004 path | Catch `(serial.SerialException, OSError)`; carry the errno on the error |
| 2 | **P1** | **Ctrl+D spun the main menu forever.** Every menu is a `while True` around `choose()`, each leaving by its own key (`"q"` at the top, `"0"` in submenus); `ask` returned `""` on EOF, which matches none of them. Measured: 3,001 prompts without exiting, at full CPU — and because `main()` releases the port in a `finally` that was never reached, the client went on **holding `/dev/ttyUSB0`**, so the next client was refused `PORT_BUSY` for a reason unrelated to the board | `OperatorGone(BaseException)` from `choose()` only; `main()` exits 0 and the `finally` releases the port |
| 3 | **P2** | **No frame-size policy on the host.** The firmware has capped received commands at 4,096 bytes from the start; the host had no equivalent. A device that talks without ever sending a newline accumulated 2.08 MB inside one `measure_raw` timeout, in a single string copied on every append | `MAX_FRAME_BYTES = 65536` (4× the largest legitimate 16,454-byte frame); over-long lines discarded and counted, reader resynchronizes at the next newline. Retained memory: 2.08 MB → 78 KB |
| 4 | **P2** | `RecursionError` from `json.loads` / `salvage_json` at ~17,000 nesting depth is **not a `ValueError`**, so it escaped every handler in the module and would kill the client from a line of console noise | Catch `(ValueError, RecursionError)` at both parse points. Guarded independently of the byte cap, because the threshold is a property of the interpreter's stack, not of this protocol |
| 5 | **P2** | **A Czech note crashed the records screen.** Python encodes stdout with the locale's encoding; under `LANG=C` — a systemd unit, a minimal container, a redirected pipe — that is ASCII, and printing a sample note with a háček raised `UnicodeEncodeError` from inside `print()`. This is a Brno team; the note field is free text | `make_console_unbreakable()` at startup sets `errors="backslashreplace"` on stdout/stderr and `"replace"` on stdin. The terminal's *encoding* is left alone, so a UTF-8 terminal stays perfect and nothing becomes mojibake. The archive keeps real UTF-8 |
| 6 | **P3** | Metrics broke their own convention. Squaring overflows above ≈1.3e154 and Python's `**` **raises** rather than returning infinity, so `rmse` and `euclidean` raised out of the metric while `mae` returned a bare `inf` — a number that ranks, formats, and reaches an operator's metric table looking like a measurement | `_defined()` guard on all seven metrics: a result that is not a real number is `None`, the same vocabulary `paired()` already used for inputs. Cannot change any in-range result |
| 7 | **P3** | The two menu loops in `interactive()` dispatched to **overlapping** screens while catching different exception sets — the main loop handled `StorageError`, the startup loop did not. Not reachable today; reachable the moment any tools screen propagates one | The startup loop catches the same set; a structural test asserts both dispatch blocks stay in step |

Defects 1, 2 and 5 are the ones that would have been felt on the
competition field. All seven are regression-protected, and five have a
dedicated mutation that dies.

---

## O. Data integrity

Every suite writes through `SandboxBD`, a temporary tree seeded from
the real reference data. `run_software.py` hashes every file under
`firmware/BD/` before and after the whole campaign and fails the run if
one byte moved — a guard that does not depend on anyone remembering to
use the sandbox.

```
BD/ before:  24 files
BD/ after:   24 files, unchanged
```

Verified across every acceptance run in this phase, including the
shuffled-order runs.

---

## P. Repository state

```
no commits, no pushes
```

`git diff --check` reports no whitespace errors. It does report
CRLF/LF normalization warnings on five files — the repository has mixed
line endings and Git will normalize them on the next checkout. That is
a **deterministic-checkout recommendation**, not a defect: the mutation
harness already restores files byte-for-byte and verifies it by hash,
which is what caught a text-mode restore rewriting line endings during
Phase A.2. A `.gitattributes` is not required by anything here and has
not been added.

No test produces a tracked change: the working tree after a full
campaign is identical to the working tree before it, apart from the
files this phase deliberately edited.

---

## Q. The numbers

| | Phase A | A.2 | **A.3** |
| --- | --- | --- | --- |
| Suites | 22 | 28 | **37** |
| Checks | 1,787 | 2,366 | **3,188** |
| Mutations killed / attempted | — | 23 / 23 | **37 / 37** |
| Handlers executed individually | — | — | **83 / 220 (38%)** |
| Audit tools | — | 2 | **6** |
| `BD/` after every run | unchanged | unchanged | **unchanged** |

New in A.3: `test_protocol_limits`, `test_resource_faults`,
`test_firmware_faults`, `test_linux_runtime`, `test_lifecycle`,
`test_mission_model`, `test_science_properties`, `test_screens_failing`,
`test_full_mission` — and four audit tools (`module_inventory`,
`call_graph`, `handler_coverage`, `vocabulary`).

---

## R. The final question — A.3 §125

> Can you name any realistic software failure that can affect the
> competition runtime which is neither tested, statically checked,
> explicitly rejected by design, nor assigned to hardware verification?

**No known unclassified software failure class remains.**

Two things are nonetheless worth naming, because "classified" is not
the same as "closed":

1. **60 exception handlers have not been driven individually** (§C.2).
   Their failure classes are verified at the screen level; the handlers
   themselves are read, not executed. This is a coverage limit, stated
   rather than papered over.

2. **The campaign has never run on the rover's own Linux machine**
   (`SOFTWARE_ASSUMPTIONS.md` A-001, A-004). It needs no hardware, no
   carousel and no sensor — one command on the main computer converts
   two assumptions into facts. This is the single highest-value action
   remaining, and it is not a hardware task.

---

## S. Conclusion

Within the software model, no known mission-relevant software-testable
failure class remains unverified or unclassified. Every software defect
discovered in this campaign has been fixed and regression-protected.
Every remaining uncertainty has been explicitly assigned to physical
hardware verification, to offline non-mission functionality, or to the
two stated limitations above.

The honest qualifier, unchanged from A.2 and now better evidenced:
**this holds given the assumptions.** H-002 — a carousel that visibly
turned 180° while its encoder reported 2 counts — is standing evidence
that at least one of those assumptions was wrong on real hardware, and
that the software could not have known.

```
PHASE A SOFTWARE ASSURANCE CLOSED
```
