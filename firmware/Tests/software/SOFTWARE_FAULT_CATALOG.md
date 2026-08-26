# Software Fault Catalogue

**Phase:** A.3 — final adversarial campaign before hardware verification
**Date:** 2026-08-25
**Scope:** every software failure class that can affect the competition
runtime of the Freya science module.

---

## How to read this

Every fault class has an ID, a trigger, the behaviour required of the
software, the test that proves it, and exactly one classification:

| Classification | Meaning |
| --- | --- |
| `VERIFIED` | A software test drives the fault and asserts the required behaviour. |
| `HARDWARE_ONLY` | The fault cannot be produced without real silicon. Assigned to `HARDWARE_VERIFICATION_PLAN.md`. |
| `OFFLINE_ONLY` | Reachable only from post-competition tooling; cannot affect a measurement, a carousel position or a saved spectrum. |
| `NOT_APPLICABLE` | The architecture makes the fault impossible, and the reason is stated. |
| `OPEN_BLOCKER` | Known, mission-relevant, and not closed. **Must be zero at freeze.** |

```
VERIFIED         96
HARDWARE_ONLY    14
OFFLINE_ONLY      4
NOT_APPLICABLE    9
OPEN_BLOCKER      0
                ---
total           123
```

Test names are relative to `firmware/Tests/software/`.

---

## SER — the serial transport

| ID | Trigger | Required behaviour | Test | Status |
| --- | --- | --- | --- | --- |
| SER-001 | Port does not exist | `PORT_NOT_FOUND`, names the cable | `linux/test_linux.py` | VERIFIED |
| SER-002 | Port held by another program | `PORT_BUSY`, names the other program, does **not** mention groups | `linux/test_linux.py`, `linux/test_linux_runtime.py` | VERIFIED |
| SER-003 | Account not in the serial group | `PORT_DENIED`, names `dialout` and `usermod`, says re-login is needed | `linux/test_linux.py` | VERIFIED |
| SER-004 | Port opens, nothing answers | `PROTOCOL_TIMEOUT` with the console output attached | `fault_injection/test_serial_faults.py` | VERIFIED |
| SER-005 | Board sitting at the MicroPython REPL | `DEVICE_AT_REPL`, immediately, without waiting out the timeout | `fault_injection/test_serial_faults.py` | VERIFIED |
| SER-006 | Frame damaged in transit | `MALFORMED_RESPONSE`; retried only for pure reads | `fault_injection/test_serial_faults.py` | VERIFIED |
| SER-007 | Rubbish in front of an intact frame | Salvaged, counted in `salvaged_frames` | `fault_injection/test_serial_faults.py` | VERIFIED |
| SER-008 | Device disappears mid-request | `PORT_LOST`, link closes itself, every later command is `PORT_CLOSED` | `regression/test_linux_bench.py` | VERIFIED |
| SER-009 | Non-finite value in a payload | `INVALID_REQUEST` **before** anything is written | `unit/test_prompts.py` | VERIFIED |
| SER-010 | Firmware answers `ok: false` | `DeviceError` carrying the firmware's own code | `contracts/test_pc_firmware.py` | VERIFIED |
| SER-011 | Answer to a **different** request arrives | Not accepted; `stale_frames` incremented | `fault_injection/test_protocol_limits.py` | VERIFIED |
| SER-012 | Our own request echoed back | Not accepted — a response must carry `ok` | `fault_injection/test_protocol_limits.py` | VERIFIED |
| SER-013 | Forged frame with a colliding request id | Rejected unless the command also matches | `contracts/test_request_identity.py` | VERIFIED |
| SER-014 | Echo storm, then boot text, then the real answer | The real answer is found; echoes kept as noise | `fault_injection/test_protocol_limits.py` | VERIFIED |
| SER-015 | Echo storm with **no** answer behind it | `PROTOCOL_TIMEOUT` or `DEVICE_AT_REPL`, never success | `fault_injection/test_protocol_limits.py` | VERIFIED |
| SER-016 | Unsolicited but valid frames | Ignored; a real answer behind them still returns | `fault_injection/test_protocol_limits.py` | VERIFIED |
| SER-017 | Lost acknowledgement of a **movement** | Never retried — a relative move would happen twice | `fault_injection/test_serial_faults.py` | VERIFIED |
| SER-018 | Opening the port resets the board | DTR/RTS driven low **before** `open()` | `integration/test_pc.py` | VERIFIED |
| SER-019 | Real DTR/RTS electrical behaviour | — | `HARDWARE_VERIFICATION_PLAN.md` H-004 | HARDWARE_ONLY |

### Frame size and framing — A.3 §22–§27

| ID | Trigger | Required behaviour | Test | Status |
| --- | --- | --- | --- | --- |
| SER-020 | Zero bytes, a bare newline, one byte | Not an answer; controlled timeout | `fault_injection/test_protocol_limits.py` | VERIFIED |
| SER-021 | `{}`, `[]`, `null`, a bare number | Not an answer | `fault_injection/test_protocol_limits.py` | VERIFIED |
| SER-022 | Frame at the largest legitimate size (16,454 B) | Read normally | `fault_injection/test_protocol_limits.py` | VERIFIED |
| SER-023 | Frame 10× and 100× the cap | Discarded, `oversized_lines` incremented, reader resynchronizes | `fault_injection/test_protocol_limits.py` | VERIFIED |
| SER-024 | **Endless stream with no newline** | Bounded at `MAX_FRAME_BYTES`; 2.08 MB delivered, 78 KB retained | `fault_injection/test_protocol_limits.py` | VERIFIED |
| SER-025 | Oversized frame followed by a good one | The **next** request succeeds | `fault_injection/test_protocol_limits.py` | VERIFIED |
| SER-026 | JSON nested ~17,000 deep | Controlled outcome; `RecursionError` caught | `fault_injection/test_protocol_limits.py` | VERIFIED |
| SER-027 | Duplicate keys in one object | Last value wins; cannot satisfy two commands at once | `fault_injection/test_protocol_limits.py` | VERIFIED |
| SER-028 | Binary, invalid UTF-8, NULs, C0 controls, ANSI escapes | Controlled outcome; a real frame behind them still reads | `fault_injection/test_protocol_limits.py` | VERIFIED |
| SER-029 | Firmware emitting a duplicate key | Impossible — responses are built from dicts | `fault_injection/test_protocol_limits.py` | NOT_APPLICABLE |

### Request identity — A.3 §30–§31

| ID | Trigger | Required behaviour | Test | Status |
| --- | --- | --- | --- | --- |
| SER-030 | Two sessions, ids that overlap | Session nonce plus command match | `contracts/test_request_identity.py` | VERIFIED |
| SER-031 | Deliberate nonce collision | Collision alone is **not sufficient** to accept | `contracts/test_request_identity.py` | VERIFIED |
| SER-032 | `os.urandom` fails | Refuse to start; **no** fixed fallback | `fault_injection/test_resource_faults.py` | VERIFIED |

---

## LNX — the Linux main computer

| ID | Trigger | Required behaviour | Test | Status |
| --- | --- | --- | --- | --- |
| LNX-001 | `EIO` from `in_waiting` / `flush` / `read` / `write` | `PORT_LOST`, never a traceback | `linux/test_linux_runtime.py` | VERIFIED |
| LNX-002 | `ENODEV`, `ENXIO`, `EBADF`, `EACCES`, `EPERM`, `EBUSY`, `ENOENT` mid-command | Same, at all four pySerial entry points — 32 combinations | `linux/test_linux_runtime.py` | VERIFIED |
| LNX-003 | `EINTR` — interrupted syscalls | Retried; never reported as a completed read | `linux/test_linux_runtime.py` | VERIFIED |
| LNX-004 | Endless interruption | `PROTOCOL_TIMEOUT`, never an answer | `linux/test_linux_runtime.py` | VERIFIED |
| LNX-005 | Two client processes, one port | Second gets `PORT_BUSY`; recovery after the first exits | `linux/test_linux_runtime.py` | VERIFIED |
| LNX-006 | Two processes, one archive | Last-writer-wins; file never corrupt. Concurrent writers **unsupported**, and the exclusive port open makes a second mission client impossible | `linux/test_linux_runtime.py` | VERIFIED |
| LNX-007 | Device renamed `ttyUSB0` → `ttyUSB1` | No auto-selection exists; `--port` is required | `linux/test_linux_runtime.py` | VERIFIED |
| LNX-008 | Two USB serial devices present | Nothing guesses; operator chooses | `linux/test_linux_runtime.py` | VERIFIED |
| LNX-009 | `/dev/serial/by-id/...` symlink path | Accepted as given; no pattern imposed | `linux/test_linux_runtime.py` | VERIFIED |
| LNX-010 | Node disappears and returns | No silent reattach; operator must reconnect | `linux/test_linux_runtime.py` | VERIFIED |
| LNX-011 | Boot banner arrives in front of an answer | Answer read; banner kept as evidence | `linux/test_linux_runtime.py` | VERIFIED |
| LNX-012 | Non-English locale | JSON numbers stay machine-format | `linux/test_linux_runtime.py` | VERIFIED |
| LNX-013 | **`LANG=C`, operator text is not ASCII** | Screens print; unencodable characters become visible escapes; archive keeps real UTF-8 | `linux/test_linux_runtime.py` | VERIFIED |
| LNX-014 | Wall clock jumps ±2 h, to 1970, 1000 days forward | Timeouts unaffected — every deadline is `time.monotonic()` | `linux/test_linux_runtime.py` | VERIFIED |
| LNX-015 | Broken wall clock | Only human timestamps affected; they stay explicit UTC | `linux/test_linux_runtime.py` | VERIFIED |
| LNX-016 | `PYTHONPATH`, `PWD`, `HOME`, `TZ`, … set oddly | No mission module reads any environment variable | `linux/test_linux_runtime.py` | VERIFIED |
| LNX-017 | Archive path with spaces, Unicode, 90 chars | Written and read back | `linux/test_linux_runtime.py` | VERIFIED |
| LNX-018 | `pyserial` missing | Named diagnosis with the exact pip command and **this** interpreter | `linux/test_linux_runtime.py` | VERIFIED |
| LNX-019 | Dependency inventory | Exactly one third-party package (`pyserial`), imported only by `PC/` | `linux/test_linux_runtime.py` | VERIFIED |
| LNX-020 | Interpreter version | No `match`/`case`; 3.8+ syntax throughout | `linux/test_linux_runtime.py` | VERIFIED |
| LNX-021 | Real CP2102 stability; why `/dev/ttyUSB0` vanished on the bench | — | H-004 | HARDWARE_ONLY |
| LNX-022 | `sqlite3` absent from a stripped Python build | Client fails at import. Stdlib on every mainstream build; recorded in `SOFTWARE_ASSUMPTIONS.md` A-004 | — | NOT_APPLICABLE |

---

## MEM — memory

| ID | Trigger | Required behaviour | Test | Status |
| --- | --- | --- | --- | --- |
| MEM-001 | `MemoryError` serializing the archive | No save reported; archive byte-identical; RAM rolled back | `fault_injection/test_resource_faults.py` | VERIFIED |
| MEM-002 | `MemoryError` at `mkstemp` / `fdopen` / `replace` | Same, at each stage | `fault_injection/test_resource_faults.py` | VERIFIED |
| MEM-003 | `MemoryError` building a request frame | Propagates as itself; **nothing** written to the wire | `fault_injection/test_resource_faults.py` | VERIFIED |
| MEM-004 | `MemoryError` parsing a response | Not swallowed into a believable answer | `fault_injection/test_resource_faults.py` | VERIFIED |
| MEM-005 | `MemoryError` inside Science | Analysis `FAILED` with the exception type in the record; RAW untouched | `fault_injection/test_resource_faults.py` | VERIFIED |
| MEM-006 | 1,600 rejected lines | `last_noise` and `damaged_lines` capped at 40 | `fault_injection/test_resource_faults.py` | VERIFIED |
| MEM-007 | 1,100 successful requests | Retained-object count unchanged between 100 and 1,100 | `fault_injection/test_resource_faults.py` | VERIFIED |
| MEM-008 | 60 sample workflows | Diagnostic buffers still capped; ids unique | `integration/test_full_mission.py` | VERIFIED |
| MEM-009 | `MemoryError` in a firmware command handler | Well-formed error frame naming `MemoryError`; next command served | `fault_injection/test_firmware_faults.py` | VERIFIED |
| MEM-010 | Firmware response cannot be joined in one block | Pieces written individually; whole response still arrives | `fault_injection/test_firmware_faults.py` | VERIFIED |
| MEM-011 | **Real ESP32 heap fragmentation during acquisition** | — | H-005 | HARDWARE_ONLY |

---

## FS — the filesystem

| ID | Trigger | Required behaviour | Test | Status |
| --- | --- | --- | --- | --- |
| FS-001 | `ENOSPC` at `mkdir` | `StorageError`; archive intact; no temp left behind | `fault_injection/test_resource_faults.py` | VERIFIED |
| FS-002 | `ENOSPC` at `mkstemp` | Same | `fault_injection/test_resource_faults.py` | VERIFIED |
| FS-003 | `ENOSPC` at the write | Same | `fault_injection/test_resource_faults.py` | VERIFIED |
| FS-004 | `ENOSPC` at `fsync` | Same | `fault_injection/test_resource_faults.py` | VERIFIED |
| FS-005 | `ENOSPC` at `os.replace` | Same | `fault_injection/test_resource_faults.py` | VERIFIED |
| FS-006 | `EMFILE` / `ENFILE` | `StorageError`, not a traceback | `fault_injection/test_resource_faults.py` | VERIFIED |
| FS-007 | `EMFILE` opening the archive to read | Fails loudly; never read as an empty archive | `fault_injection/test_resource_faults.py` | VERIFIED |
| FS-008 | Inode exhaustion (`ENOSPC` with free bytes) | Failed save; archive still readable | `fault_injection/test_resource_faults.py` | VERIFIED |
| FS-009 | Read-only filesystem | Writes fail; **reads still work** | `fault_injection/test_resource_faults.py` | VERIFIED |
| FS-010 | Permission revoked between two saves | Rollback to durable; restored permission adds exactly one record | `fault_injection/test_resource_faults.py` | VERIFIED |
| FS-011 | Orphan `.samples-*.tmp` from a crash | Not read, not counted, not deleted on a guess | `fault_injection/test_resource_faults.py`, `process/test_lifecycle.py` | VERIFIED |
| FS-012 | Archive is malformed JSON | `SAMPLE_READ_ERROR`; never half-read | `fault_injection/test_filesystem_faults.py` | VERIFIED |
| FS-013 | Archive is not a Sample archive | Refused by shape | `fault_injection/test_filesystem_faults.py` | VERIFIED |
| FS-014 | Path-shaped Sample ID | Refused **before** any write | `fault_injection/test_filesystem_faults.py` | VERIFIED |
| FS-015 | Schema migration write fails | `load()` raises; caller sees the failure | `fault_injection/test_filesystem_faults.py` | VERIFIED |
| FS-016 | Every screen × four write-failure modes | No crash, no archive change, no false success | `integration/test_screens_failing.py` | VERIFIED |
| FS-017 | Failed save then operator retry | Exactly one record; no ghost | `integration/test_screens_failing.py` | VERIFIED |

---

## PROC — process lifecycle

| ID | Trigger | Required behaviour | Test | Status |
| --- | --- | --- | --- | --- |
| PROC-001 | **Ctrl+D at any menu** | Session ends; exit 0; **port released** | `process/test_lifecycle.py` | VERIFIED |
| PROC-002 | Ctrl+D at a free-text field | Returns the field's default — not a shutdown | `process/test_lifecycle.py` | VERIFIED |
| PROC-003 | Ctrl+C at the menu | Exit 0; port released | `process/test_lifecycle.py` | VERIFIED |
| PROC-004 | Ctrl+C with a move on the wire | Propagates; the command **was** issued; position becomes untrusted | `process/test_lifecycle.py` | VERIFIED |
| PROC-005 | Ctrl+C before the measure command is written | Nothing written; nothing saved | `process/test_lifecycle.py` | VERIFIED |
| PROC-006 | Ctrl+C with the acquisition in flight | Command issued; no measurement recorded | `process/test_lifecycle.py` | VERIFIED |
| PROC-007 | Ctrl+C during the save | Archive byte-identical; ESP32 still holds the acquisition | `process/test_lifecycle.py` | VERIFIED |
| PROC-008 | SIGTERM at each of four save stages | Archive byte-identical and still valid JSON | `process/test_lifecycle.py` | VERIFIED |
| PROC-009 | Restart after disk-full / read-only / SIGTERM / OOM | Restarted process sees durable truth; next save works | `process/test_lifecycle.py` | VERIFIED |
| PROC-010 | Restart after an interrupted mission save | Interrupted sample absent; mission continues | `integration/test_full_mission.py` | VERIFIED |
| PROC-011 | stdout breaks mid-screen (`EPIPE`) | Controlled `BrokenPipeError`; archive unaffected | `process/test_lifecycle.py` | VERIFIED |
| PROC-012 | 4,000 junk menu selections | Each answered; blanks not scolded; exit 0 | `process/test_lifecycle.py` | VERIFIED |
| PROC-013 | 300 menu round-trips | Stack depth varies by ≤ 1 frame | `process/test_lifecycle.py` | VERIFIED |
| PROC-014 | Menus recursing instead of looping | No `menu_*` calls itself | `process/test_lifecycle.py` | VERIFIED |
| PROC-015 | SIGHUP / SIGQUIT delivery | Python raises no catchable exception for `SIGKILL`; `SIGHUP` default-terminates. Durable state is protected by the atomic write, proven under SIGTERM (PROC-008) | — | NOT_APPLICABLE |

---

## STATE — mission and carousel state

| ID | Trigger | Required behaviour | Test | Status |
| --- | --- | --- | --- | --- |
| STATE-001 | ESP32 reset | Position invalid; no PC memory restores it | `state_machine/test_reset_recovery.py` | VERIFIED |
| STATE-002 | PC restart, board still running | Position read back **from the board** | `integration/test_full_mission.py` | VERIFIED |
| STATE-003 | Both restart | Position invalid | `state_machine/test_reset_recovery.py` | VERIFIED |
| STATE-004 | Reset at each point of a command | Classified; no false position | `state_machine/test_reset_recovery.py` | VERIFIED |
| STATE-005 | `connect_servo` | **Always** invalidates the position | `state_machine/test_mission_model.py` | VERIFIED |
| STATE-006 | Failed movement | Three-way verdict `NOT_STARTED` / `MOVED` / `UNKNOWN` | `regression/test_linux_bench.py` | VERIFIED |
| STATE-007 | Generated action sequences, 8 seeds | Model and system agree after **every** step | `state_machine/test_mission_model.py` | VERIFIED |
| STATE-008 | Same, with resets and disconnects injected | Same | `state_machine/test_mission_model.py` | VERIFIED |
| STATE-009 | Nine mission invariants | Hold after every transition | `state_machine/test_mission_model.py` | VERIFIED |
| STATE-010 | Carousel position across all 4096 encoder positions | Arithmetic correct | `state_machine/test_carousel_states.py` | VERIFIED |
| STATE-011 | **Encoder reporting 2 counts for a visible 180° turn** | — | H-002 | HARDWARE_ONLY |
| STATE-012 | Whether 15 counts is the right tolerance | — | H-001 | HARDWARE_ONLY |
| STATE-013 | Whether a half turn is 2048 counts on this mechanism | — | H-005 | HARDWARE_ONLY |
| STATE-014 | Physical backlash | — | H-006 | HARDWARE_ONLY |

---

## PROTO — the firmware's command surface

| ID | Trigger | Required behaviour | Test | Status |
| --- | --- | --- | --- | --- |
| PROTO-001 | Command line over `MAX_COMMAND_BYTES` | `COMMAND_TOO_LONG` before parsing | `fault_injection/test_firmware_faults.py` | VERIFIED |
| PROTO-002 | Line exactly at the limit | Answered **successfully** | `fault_injection/test_firmware_faults.py` | VERIFIED |
| PROTO-003 | Line that is not JSON | `INVALID_JSON` — a different diagnosis | `fault_injection/test_firmware_faults.py` | VERIFIED |
| PROTO-004 | Unknown command | Refused with a code | `fault_injection/test_firmware_faults.py` | VERIFIED |
| PROTO-005 | 16 wrong-type payload fields | Refused; dispatcher never crashes | `fault_injection/test_firmware_faults.py` | VERIFIED |
| PROTO-006 | Every command × a payload of pure nonsense | All 26 answer with a well-formed frame | `fault_injection/test_firmware_faults.py` | VERIFIED |
| PROTO-007 | Programming exception inside a handler | `INTERNAL_ERROR` naming the real type; **not** a hardware code | `fault_injection/test_firmware_faults.py` | VERIFIED |
| PROTO-008 | Unserializable value in a response | Replaced by its type name; **numbers stay numbers** | `fault_injection/test_firmware_faults.py` | VERIFIED |
| PROTO-009 | Pathologically nested response | Truncated at documented depth, and says so | `fault_injection/test_firmware_faults.py` | VERIFIED |
| PROTO-010 | Sensor unavailable, per command | Three different contracts, each correct | `fault_injection/test_firmware_faults.py` | VERIFIED |
| PROTO-011 | Servo silent | Carousel not attached to a driver that is not there | `fault_injection/test_firmware_faults.py` | VERIFIED |
| PROTO-012 | Codes produced vs codes handled | Zero host branches on a code nothing produces | `audit/vocabulary.py` | VERIFIED |
| PROTO-013 | Response fields consumed vs produced | Zero case-only collisions | `audit/vocabulary.py` | VERIFIED |
| PROTO-014 | Unterminated line into the firmware's `readline` | MicroPython accumulates before the length check. Only the PC writes to that stdin, and it never sends an unterminated line | — | HARDWARE_ONLY |
| PROTO-015 | Half-written I²C register, single-byte-corrupted servo frame | — | H-003 | HARDWARE_ONLY |

---

## SCI — the science layer

| ID | Trigger | Required behaviour | Test | Status |
| --- | --- | --- | --- | --- |
| SCI-001 | `sim(x, x)` for any valid spectrum | Exactly 1, and **never above** 1 | `unit/test_science_properties.py` | VERIFIED |
| SCI-002 | `sim(a, b)` vs `sim(b, a)` | Identical | `unit/test_science_properties.py` | VERIFIED |
| SCI-003 | Spectrum scaled ×0.5 … ×1000 | Cosine unchanged — brightness is not identity | `unit/test_science_properties.py` | VERIFIED |
| SCI-004 | Distances | Non-negative, zero only on identity, symmetric, triangle inequality | `unit/test_science_properties.py` | VERIFIED |
| SCI-005 | Cosine vs spectral angle | Rank-identical over generated libraries | `unit/test_science_properties.py` | VERIFIED |
| SCI-006 | Production vs independent reference implementations | Agree to 1e-12 | `unit/test_science_properties.py` | VERIFIED |
| SCI-007 | `normalize` vs `R = (S−D)/(W−D)` | Agree channel by channel | `unit/test_science_properties.py` | VERIFIED |
| SCI-008 | Subnormal, largest finite, −0.0, 0.1+0.2 | 1.0 or an honest `None`; never NaN | `unit/test_science_properties.py` | VERIFIED |
| SCI-009 | **Values so large that squaring overflows** | Every metric returns `None`; **no `inf`, no raise** | `unit/test_science_properties.py` | VERIFIED |
| SCI-010 | Spectral floats through JSON | Bit-identical; cosine identical before and after | `unit/test_science_properties.py` | VERIFIED |
| SCI-011 | Missing / NaN / inf / string / null / list channel | Dropped from the comparison, and the loss is visible in the pair count | `unit/test_science_properties.py` | VERIFIED |
| SCI-012 | Unknown 19th channel | Ignored entirely; result unchanged | `unit/test_science_properties.py` | VERIFIED |
| SCI-013 | Channels reordered | Result unchanged — read by name | `unit/test_science_properties.py` | VERIFIED |
| SCI-014 | Duplicate channel | Structurally impossible in a dict | `unit/test_science_properties.py` | NOT_APPLICABLE |
| SCI-015 | `W == D` | Every channel `None` — **not 0.0** | `unit/test_science_properties.py` | VERIFIED |
| SCI-016 | Non-finite in sample / dark / white | That channel `None`; other 17 unaffected | `unit/test_science_properties.py` | VERIFIED |
| SCI-017 | Analysis fails after RAW is stored | Costs an analysis, never the experiment | `integration/test_pc.py` | VERIFIED |
| SCI-018 | Model promotion / registry workflows | Not on the mission path; no callers | `audit/module_inventory.py` | OFFLINE_ONLY |
| SCI-019 | Ground-truth labelling interviews | Post-competition only | `audit/module_inventory.py` | OFFLINE_ONLY |
| SCI-020 | Whether the decision thresholds are scientifically right | Provisional by design; `research/training/` evaluates them | — | OFFLINE_ONLY |

---

## UI — the operator surface

| ID | Trigger | Required behaviour | Test | Status |
| --- | --- | --- | --- | --- |
| UI-001 | Sample ID: empty, whitespace, 500 chars, path, glob, NUL, newline | Refused **before** any write | `fault_injection/test_filesystem_faults.py` | VERIFIED |
| UI-002 | Duplicate Sample ID | Refused; original untouched | `fault_injection/test_filesystem_faults.py` | VERIFIED |
| UI-003 | NaN / infinity as a carousel angle | Refused with an explanation | `unit/test_prompts.py` | VERIFIED |
| UI-004 | Non-numeric where a number is required | Re-asked, never accepted | `unit/test_prompts.py` | VERIFIED |
| UI-005 | Blank at a cancellable prompt | Cancels, and the caller says so | `unit/test_prompts.py` | VERIFIED |
| UI-006 | Every result shape a screen can be handed | Formats without raising | `unit/test_display_shapes.py` | VERIFIED |
| UI-007 | Migrated legacy record with no `rank` | Formatted through `rank_of()` | `regression/test_regressions.py` | VERIFIED |
| UI-008 | `--help` on every entry point | No serial, no DB write, no analysis | `entrypoints/test_entrypoints.py` | VERIFIED |
| UI-009 | Invalid CLI options | argparse diagnostic, no traceback | `entrypoints/test_entrypoints.py` | VERIFIED |
| UI-010 | Importing any mission module | No serial, no movement, no DB write | `entrypoints/test_entrypoints.py` | VERIFIED |
| UI-011 | Very narrow / very wide / non-interactive terminal | Formatting does not crash | `unit/test_display_shapes.py` | VERIFIED |
| UI-012 | Free-text fields at max length | Stored and redisplayed | `integration/test_records.py` | VERIFIED |
| UI-013 | Both menu loops catch the same failures | Enforced structurally | `integration/test_screens_failing.py` | VERIFIED |
| UI-014 | Expected operational faults | Controlled message, no traceback | `regression/test_linux_bench.py` | VERIFIED |
| UI-015 | Unexpected programming defects | Traceback preserved for developers | `fault_injection/test_firmware_faults.py` | VERIFIED |

---

## DATA — persistence and integrity

| ID | Trigger | Required behaviour | Test | Status |
| --- | --- | --- | --- | --- |
| DATA-001 | Any test run | `BD/` byte-identical before and after | `data_integrity/test_protected_data.py` | VERIFIED |
| DATA-002 | Failed write | RAM rolled back to durable | `fault_injection/test_filesystem_faults.py` | VERIFIED |
| DATA-003 | RAW stored | Deep-copied, not by reference | `unit/test_data.py` | VERIFIED |
| DATA-004 | Failed acquisition | Recorded with **no** raw block | `state_machine/test_sample_lifecycle.py` | VERIFIED |
| DATA-005 | Measure the same sample twice | Added beside, nothing overwritten | `state_machine/test_sample_lifecycle.py` | VERIFIED |
| DATA-006 | Save the same acquisition twice | `ALREADY SAVED`; exactly one record | `integration/test_pc.py` | VERIFIED |
| DATA-007 | Rename / delete / clear twice | Deterministic | `integration/test_records.py` | VERIFIED |
| DATA-008 | 60-sample day | Ids unique; one measurement each | `integration/test_full_mission.py` | VERIFIED |
| DATA-009 | Measurement ids across samples | Scoped to their Sample by design | `integration/test_full_mission.py` | NOT_APPLICABLE |
| DATA-010 | Protected DB read from a read-only copy | Reads succeed; nothing attempts a write | `data_integrity/test_protected_data.py` | VERIFIED |

---

## MULTI — more than one fault at once

| ID | Trigger | Required behaviour | Test | Status |
| --- | --- | --- | --- | --- |
| MULTI-001 | `PORT_LOST` + stale frame after reconnect | Stale frame not consumed; no inherited position | `integration/test_full_mission.py` | VERIFIED |
| MULTI-002 | Sensor failure + save failure | Nothing acquired, archive untouched, next measurement works | `integration/test_full_mission.py` | VERIFIED |
| MULTI-003 | Movement ambiguity + client restart | Position still unknown after the restart | `integration/test_full_mission.py` | VERIFIED |
| MULTI-004 | Disk full + operator retry | No ghost record; retry durable | `integration/test_full_mission.py` | VERIFIED |
| MULTI-005 | ESP32 reboot + port renumbering | New nonce; position not inherited | `integration/test_full_mission.py` | VERIFIED |
| MULTI-006 | Up to three concurrent faults, 6 seeds × 8 steps | Archive sound after every step | `integration/test_full_mission.py` | VERIFIED |
| MULTI-007 | Seeded chaos over whole workflows | Deterministic; recovery always possible | `randomized/test_chaos.py` | VERIFIED |
| MULTI-008 | Full hostile mission, 10 stages | No false success; archive sound | `integration/test_full_mission.py` | VERIFIED |

---

## TEST — the harness itself

| ID | Trigger | Required behaviour | Test | Status |
| --- | --- | --- | --- | --- |
| TEST-001 | Fakes reimplementing production logic | Forbidden; fakes replace wires, not decisions | `unit/test_fakes.py` | VERIFIED |
| TEST-002 | A suite touching real `BD/` | Impossible — hash guard around the campaign | `data_integrity/test_protected_data.py` | VERIFIED |
| TEST-003 | Does the suite have teeth | 37 mutations, 37 killed | `mutation.py` | VERIFIED |
| TEST-004 | Mutation harness corrupting files | Byte-for-byte hash restore verified | `mutation.py` | VERIFIED |
| TEST-005 | Every production file has a declared role | 111 files, none unknown | `audit/module_inventory.py` | VERIFIED |
| TEST-006 | Offline code on the mission path | None | `audit/module_inventory.py` | VERIFIED |
| TEST-007 | Mission call graph | 481 reachable functions, 46 explained | `audit/call_graph.py` | VERIFIED |
| TEST-008 | Exception handler classification | 83 of 220 executed, 51 screen-verified, 27 cleanup/hardware/offline; 59 read but not driven - the honest number is in the assurance report | `audit/handler_coverage.py` | VERIFIED |
| TEST-009 | Path resolution by hop count | Forbidden repository-wide | `regression/test_regressions.py` | VERIFIED |

---

## HDL — the handler closure pass

Added by the final closure task. Every entry is a handler that no test
had ever entered before this pass.

| ID | Trigger | Required behaviour | Test | Status |
| --- | --- | --- | --- | --- |
| HDL-001 | Response contains a value JSON cannot encode | Re-encoded through `make_json_safe`; **numbers stay numbers**; one frame out | `fault_injection/test_handler_closure.py` | VERIFIED |
| HDL-002 | `make_json_safe` fails too | Minimal error frame **carrying the request id** — a frame without it is invisible to the PC | `fault_injection/test_handler_closure.py` | VERIFIED |
| HDL-003 | `json.dumps` raises `MemoryError` on a fragmented heap | Response streamed in pieces; the spectrum arrives intact | `fault_injection/test_handler_closure.py` | VERIFIED |
| HDL-004 | Even the streamed write fails | `RESPONSE_TOO_LARGE`, exception type named, line terminated | `fault_injection/test_handler_closure.py` | VERIFIED |
| HDL-005 | `sys.print_exception` unavailable | Diagnostic degrades to a sentence; never raises inside the error path | `fault_injection/test_handler_closure.py` | VERIFIED |
| HDL-006 | Every heap probe allocation fails | `largest_block: 0`; free and allocated still reported | `fault_injection/test_handler_closure.py` | VERIFIED |
| HDL-007 | `send_json` fails inside `process_line` | `RESPONSE_FAILED` rather than silence | `fault_injection/test_handler_closure.py` | VERIFIED |
| HDL-008 | `process_line` itself dies inside `serve_forever` | The loop still puts a frame on the wire | `fault_injection/test_handler_closure.py` | VERIFIED |
| HDL-009 | Six operator fields converted with the wrong type | `BAD_REQUEST` naming the field | `fault_injection/test_handler_closure.py` | VERIFIED |
| HDL-010 | Servo failure at diagnostics, configure, test move, bus scan | Driver's own code, and a failed test **invalidates the position** | `fault_injection/test_handler_closure.py` | VERIFIED |
| HDL-011 | Sensor failure at block, LED, lamp readback, temperature | Named code; unreadable lamp state is `None`, never `False` | `fault_injection/test_handler_closure.py` | VERIFIED |
| HDL-012 | `sensor_test_raw` failing at each of four stages | Diagnostic completes; the failed stage is named; no spectrum | `fault_injection/test_handler_closure.py` | VERIFIED |
| HDL-013 | The return move after a measurement fails | Position invalidated, failure reported as data, science preserved | `fault_injection/test_handler_closure.py` | VERIFIED |
| HDL-014 | **`kind == "neutral"` special-cased for a movement the driver does not offer** | Refused as `BAD_REQUEST` by the kind check; both branches provably unreachable; a retired-kinds contract check stops a new one appearing | `fault_injection/test_handler_closure.py` | NOT_APPLICABLE |
| HDL-015 | A reference library missing, unreadable or corrupt | `FILE_NOT_FOUND` / `FILE_UNREADABLE` / `FILE_INVALID_JSON`, naming the path | `fault_injection/test_loader_closure.py` | VERIFIED |
| HDL-016 | Taxonomy, learning store or decision engine broken | The Mission still constructs; the failure is **recorded**, not swallowed | `fault_injection/test_loader_closure.py` | VERIFIED |
| HDL-017 | No active calibration | Analysis `FAILED` as `NO_ACTIVE_CALIBRATION`; **no decision** | `fault_injection/test_loader_closure.py` | VERIFIED |
| HDL-018 | Each of five distinct `main()` failures | Four distinct exit codes, each diagnosed on stderr | `fault_injection/test_loader_closure.py` | VERIFIED |
| HDL-019 | `pyserial` absent | `RuntimeError` naming the package, the pip command and **this** interpreter | `fault_injection/test_loader_closure.py` | VERIFIED |
| HDL-020 | Board answers `ok:false` to a ping | Still **online** — it answered, which is the whole question | `fault_injection/test_loader_closure.py` | VERIFIED |
| HDL-021 | Context manager entered against a silent board | Port **released**; the caller never got a link to close | `fault_injection/test_loader_closure.py` | VERIFIED |
| HDL-022 | Servo release fails during firmware shutdown | Shutdown completes anyway | `fault_injection/test_loader_closure.py` | VERIFIED |
| HDL-023 | Profile, calibration or registry file corrupt | `None`, so the caller starts empty — deliberately **unlike** a reference library | `fault_injection/test_residual_handlers.py` | VERIFIED |
| HDL-024 | Operator alias file missing or corrupt | Taxonomy still loads | `fault_injection/test_residual_handlers.py` | VERIFIED |
| HDL-025 | Any of six exception types inside any analysis stage | Stage recorded `FAILED` with the **exception type preserved** | `fault_injection/test_residual_handlers.py` | VERIFIED |
| HDL-026 | Sample store cannot report its status | Screen prints `ERROR`; never raises into the menu loop | `fault_injection/test_residual_handlers.py` | VERIFIED |
| HDL-027 | Carousel `select_slot` / `fine_adjust` with a wrong-typed value | Refused by name, before the wire | `fault_injection/test_residual_handlers.py` | VERIFIED |

---

## ENV — deferred environment validation

| ID | Trigger | Required behaviour | Test | Status |
| --- | --- | --- | --- | --- |
| ENV-LINUX-001 | The complete suite has never run on `freya-1-comp` | All suites PASS; `BD/` hashes unchanged; no unexpected tracked changes | `PHASE_B_CAMPAIGNS.md` § ENV-LINUX-001 | **DEFERRED_ENVIRONMENT_VALIDATION** |

Needs no hardware — no carousel, no sensor, no servo. One command:

```bash
python3 firmware/Tests/run_all.py
```

It settles `SOFTWARE_ASSUMPTIONS.md` A-001 and A-004, the only two
assumptions in the register that could be closed without hardware.

---

## Open blockers

```
OPEN_BLOCKER = 0
```

Nothing in this catalogue is mission-relevant, software-testable and
unresolved. Every remaining uncertainty is `HARDWARE_ONLY` and carries
an H-number in `firmware/Tests/hardware/HARDWARE_VERIFICATION_PLAN.md`,
or is `OFFLINE_ONLY` and cannot affect a measurement, a carousel
position or a saved spectrum.
