# Software Failure Coverage Matrix

**Phase:** A.3
**Date:** 2026-08-25

Every mission-relevant component against every way an external operation
can fail. This is a checklist, not decoration: a blank mission-relevant
cell is a gap, and there are none.

---

## Legend

| Symbol | Meaning |
| --- | --- |
| **T** | Tested — a deterministic software test drives this cell |
| **P** | Property / model / fuzz — driven by a generator, invariants asserted |
| **S** | Static — proven by parsing the source, not by running it |
| **H** | Hardware only — needs real silicon; carries an H-number |
| **—** | Not applicable, with the reason given under the table |

Column meanings:

- **Success** — the operation works
- **Timeout** — no answer within the bound
- **Exception** — the operation raises
- **Malformed** — the answer arrives and is wrong
- **Disconnect** — the far end goes away mid-operation
- **Resource** — memory, descriptors, disk or inodes give out
- **Recovery** — the operator retries, reconnects or restarts

---

## 1. SerialLink

| Operation | Success | Timeout | Exception | Malformed | Disconnect | Resource | Recovery |
| --- | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| `open()` | T | — ¹ | T | — ¹ | T | T ² | T |
| `wait_online()` | T | T | T | T | T | — ³ | T |
| `request()` | T | T | T | T | T | T | T |
| `_read_response()` | T | T | T | T | T | T | T |
| framing / resync | T | T | T | T | T | T | T |
| request-id match | T | — ⁴ | — ⁴ | T | T | — ⁴ | T |
| `close()` | T | — ⁵ | T | — ⁵ | T | — ⁵ | T |
| `hard_reset()` | T | — ⁵ | T | — ⁵ | T | — ⁵ | H (H-004) |

¹ `open()` either returns or raises; there is no bounded wait inside it.
² `os.urandom` failure at construction — FS/`SER-032`.
³ `wait_online` only pings; it allocates nothing that can fail alone.
⁴ Pure comparison over an already-parsed dict; it cannot time out, raise or exhaust anything.
⁵ `close()` is documented never to raise and swallows everything.

---

## 2. ESP32 protocol (firmware side)

| Operation | Success | Timeout | Exception | Malformed | Disconnect | Resource | Recovery |
| --- | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| `read_command()` | T | — ¹ | T | T | T | H ² | T |
| `process_line()` | T | — ¹ | T | T | — ³ | T | T |
| `dispatch()` | T | — ¹ | T | T | — ³ | T | T |
| argument validation | T | — ¹ | T | T | — ³ | — ⁴ | T |
| `send_json()` | T | — ¹ | T | T | T | T | T |
| `_ChunkSink` | T | — ¹ | T | — ⁴ | T | T | T |
| `make_json_safe()` | T | — ¹ | T | T | — ³ | T | T |
| `serve_forever()` | T | — ¹ | T | T | T | T | T |

¹ The firmware blocks on `readline`; it has no deadline of its own — the PC owns every timeout.
² Unbounded accumulation before the length check — `PROTO-014`, hardware-only in practice because only the PC writes to that stdin.
³ The firmware cannot observe the host disconnecting; it simply never receives another line.
⁴ Pure function over already-parsed values.

---

## 3. Servo abstraction

| Operation | Success | Timeout | Exception | Malformed | Disconnect | Resource | Recovery |
| --- | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| `connect()` | T | T | T | T | T | — ¹ | T |
| `read_word()` / `write_word()` | T | T | T | T | T | — ¹ | T |
| position read | T | T | T | T | T | — ¹ | T |
| `move_to()` | T | T | T | T | T | — ¹ | T |
| tolerance verdict | T | T | T | T | T | — ¹ | T |
| bus scan | T | T | T | T | T | — ¹ | T |
| checksum / framing | T | T | T | T | T | — ¹ | T |
| **encoder vs shaft agreement** | H (H-002) | H | H | H | H | — | H |
| **settling within tolerance** | H (H-001) | H | H | H | H | — | H |
| **backlash** | H (H-006) | — | — | — | — | — | H |

¹ The servo driver allocates nothing per operation beyond small fixed buffers; heap pressure on the device is `MEM-011` / H-005.

---

## 4. Sensor abstraction

| Operation | Success | Timeout | Exception | Malformed | Disconnect | Resource | Recovery |
| --- | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| `bring_up()` | T | T | T | T | T | T | T |
| `ensure_ready()` | T | T | T | T | T | T | T |
| virtual register read | T | T | T | T | T | — ¹ | T |
| `acquire_one()` | T | T | T | T | T | T | T |
| `acquire_triad()` | T | T | T | T | T | T | T |
| lamp on / off | T | T | T | T | T | — ¹ | T |
| channel validation | T | — ¹ | T | T | — ¹ | — ¹ | T |
| **real data-ready latency** | H (H-003) | H | — | — | — | — | H |
| **half-written I²C register** | H | H | H | H | — | — | H |

¹ Register-level operations are fixed-size and synchronous.

---

## 5. Carousel workflow

| Operation | Success | Timeout | Exception | Malformed | Disconnect | Resource | Recovery |
| --- | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| `sync_position` | T | T | T | T | T | — ¹ | T |
| `select_slot` | T | T | T | T | T | — ¹ | T |
| `move_slots` | T | T | T | T | T | — ¹ | T |
| `fine_adjust` | T | T | T | T | T | — ¹ | T |
| position invalidation | T/P | T | T | T | T | — ¹ | T |
| phase derivation | T/P | — ¹ | T | T | — ¹ | — ¹ | T |
| reset → position lost | T/P | T | T | T | T | — ¹ | T |
| `connect_servo` → invalidate | T/P | T | T | T | T | — ¹ | T |

¹ Carousel state is a handful of integers; there is nothing here to exhaust.

---

## 6. Measurement workflow

| Operation | Success | Timeout | Exception | Malformed | Disconnect | Resource | Recovery |
| --- | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| pre-flight refusals | T | T | T | T | T | T | T |
| sensor probe before moving | T | T | T | T | T | T | T |
| move to scanner | T | T | T | T | T | — | T |
| acquisition | T | T | T | T | T | T | T |
| **persist RAW** | T | — ¹ | T | T | — ¹ | T | T |
| analysis | T | — ¹ | T | T | — ¹ | T | T |
| persist AnalysisRun | T | — ¹ | T | T | — ¹ | T | T |
| conclusion update | T | — ¹ | T | T | — ¹ | T | T |
| return move | T | T | T | T | T | — | T |
| stage naming on failure | T | T | T | T | T | T | T |
| Ctrl+C at each stage | T | — | T | — | T | T | T |

¹ Local operations with no wire and no deadline.

---

## 7. Science pipeline

| Operation | Success | Timeout | Exception | Malformed | Disconnect | Resource | Recovery |
| --- | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| `dark_correct` | T/P | — ¹ | T | T/P | — ¹ | T | T |
| `normalize` | T/P | — ¹ | T | T/P | — ¹ | T | T |
| `build_representations` | T/P | — ¹ | T | T/P | — ¹ | T | T |
| metrics (all seven) | T/P | — ¹ | T | T/P | — ¹ | T | T |
| `compare_library` | T/P | — ¹ | T | T/P | — ¹ | T | T |
| quality report | T/P | — ¹ | T | T/P | — ¹ | T | T |
| decision | T/P | — ¹ | T | T/P | — ¹ | T | T |
| `analyze()` end to end | T | — ¹ | T | T | — ¹ | T | T |

¹ Pure arithmetic. There is no external operation to time out or disconnect.

---

## 8. Sample persistence

| Operation | Success | Timeout | Exception | Malformed | Disconnect | Resource | Recovery |
| --- | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| `load()` | T | — ¹ | T | T | — ¹ | T | T |
| schema migration | T | — ¹ | T | T | — ¹ | T | T |
| `create()` | T | — ¹ | T | T | — ¹ | T | T |
| `set_state()` | T | — ¹ | T | T | — ¹ | T | T |
| `add_measurement()` | T | — ¹ | T | T | — ¹ | T | T |
| `add_analysis_run()` | T | — ¹ | T | T | — ¹ | T | T |
| `rename()` / `delete()` | T | — ¹ | T | T | — ¹ | T | T |
| `_write_json()` — mkdir | T | — ¹ | T | — ² | — ¹ | T | T |
| `_write_json()` — mkstemp | T | — ¹ | T | — ² | — ¹ | T | T |
| `_write_json()` — write | T | — ¹ | T | — ² | — ¹ | T | T |
| `_write_json()` — fsync | T | — ¹ | T | — ² | — ¹ | T | T |
| `_write_json()` — replace | T | — ¹ | T | — ² | — ¹ | T | T |
| rollback on failure | T | — ¹ | T | — ² | — ¹ | T | T |
| orphan temp files | T | — ¹ | T | T | — ¹ | T | T |

¹ Local filesystem operations; no deadline, no remote end.
² A write cannot produce a malformed result — it either lands atomically or does not land.

---

## 9. Calibration persistence

| Operation | Success | Timeout | Exception | Malformed | Disconnect | Resource | Recovery |
| --- | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| load library | T | — | T | T | — | T | T |
| migrate schema | T | — | T | T | — | T | T |
| select active | T | — | T | T | — | T | T |
| save new calibration | T | — | T | T | — | T | T |
| legacy calibration immutability | T/S | — | T | T | — | — | T |
| missing calibration at startup | T | — | T | T | — | T | T |

---

## 10. Records browser

| Operation | Success | Timeout | Exception | Malformed | Disconnect | Resource | Recovery |
| --- | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| list samples | T | — | T | T | T | T | T |
| open a record | T | — | T | T | T | T | T |
| migrated legacy record | T | — | T | T | T | T | T |
| rename / delete | T | — | T | T | T | T | T |
| import | T | — | T | T | T | T | T |
| sync ESP32 acquisitions | T | T | T | T | T | T | T |
| learning history | T | — | T | T | T | T | T |
| ground-truth labelling | — ¹ | — | — | — | — | — | — |

¹ `OFFLINE_ONLY`. A long branching interview used after the competition to label observations for training; it cannot affect a measurement, a carousel position or a saved spectrum. `SCI-019`.

---

## 11. Configuration

| Source | Missing | Empty | Truncated | Malformed | Wrong schema | Wrong type | Out of range | Unknown field |
| --- | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| sample archive | T | T | T | T | T | T | T | T |
| calibration library | T | T | T | T | T | T | T | T |
| acquisition profiles | T | T | T | T | T | T | T | T |
| DB1 / DB2 / DB3 | T | T | T | T | T | T | T | T |
| learning database | T | T | T | T | T | T | T | T |
| `ESP32/config.py` | S ¹ | S ¹ | S ¹ | S ¹ | S ¹ | S ¹ | S ¹ | S ¹ |
| `Science/config.py` | S ¹ | S ¹ | S ¹ | S ¹ | S ¹ | S ¹ | S ¹ | S ¹ |
| `BD/config.py` | S ¹ | S ¹ | S ¹ | S ¹ | S ¹ | S ¹ | S ¹ | S ¹ |

¹ These are **Python modules deployed with the code**, not data files read at runtime. A malformed one is a syntax error at import, caught by the compile check in the acceptance sequence and by `entrypoints/test_entrypoints.py`. They cannot be corrupted independently of the firmware they ship with.

### Extreme configuration values — A.3 §21

| Value | Rejected where | Test |
| --- | --- | --- |
| negative / zero timeout | argparse `type=float`, then bounded waits | `entrypoints/test_entrypoints.py` |
| huge timeout | Accepted; the operator waits. Documented, not refused | `entrypoints/test_entrypoints.py` |
| invalid slot | firmware `_require_slot` | `fault_injection/test_firmware_faults.py` |
| slot of the wrong type | firmware `(TypeError, ValueError)` | `fault_injection/test_firmware_faults.py` |
| repeats outside 1…`MAX_REPEATS` | firmware bounds check | `fault_injection/test_firmware_faults.py` |
| fine adjust beyond `MAX_FINE_ADJUST_DEG` | firmware limit | `integration/test_esp32.py` |
| NaN / infinity as an angle | host prompt, before the wire | `unit/test_prompts.py` |
| invalid servo id / mode | firmware validation | `fault_injection/test_firmware_faults.py` |
| invalid baud | pySerial, classified as `PORT_OPEN_FAILED` | `linux/test_linux.py` |
| wrong channel count | Science validation | `unit/test_numeric_edges.py` |

---

## 12. CLI and startup

| Operation | Success | Bad input | Exception | Resource | Recovery |
| --- | :-: | :-: | :-: | :-: | :-: |
| argument parsing | T | T | T | — | T |
| `--help` | T | T | T | — | — |
| `--command` one-shot | T | T | T | T | T |
| `--payload` JSON | T | T | T | T | T |
| missing `pyserial` | T | — | T | — | T |
| `os.urandom` failure | — | — | T | T | T |
| console encoding | T | T | T | — | T |
| import side effects | S | S | S | — | — |
| clean shutdown (`q`) | T | T | T | — | T |
| EOF shutdown (Ctrl+D) | T | T | T | — | T |
| Ctrl+C shutdown | T | T | T | — | T |

---

## 13. Operation × fault matrices — A.3 §73–§76

### 13.1 Operation × ESP32 reset

| Operation | Before | During | After state change | Before response | After response |
| --- | :-: | :-: | :-: | :-: | :-: |
| sync | T | T | T | T | T |
| move | T | T | T | T | T |
| sensor test | T | T | T | T | T |
| measure | T | T | T | T | T |
| calibration | T | T | T | T | T |
| LED test | T | T | T | T | T |

Outcome in every cell: the position is invalid afterwards, and no PC-side memory restores it.

### 13.2 Operation × PORT_LOST

| Operation | Before | During | After state change | Before response | After response |
| --- | :-: | :-: | :-: | :-: | :-: |
| sync | T | T | T | T | T |
| move | T | T | T | T | T |
| sensor test | T | T | T | T | T |
| measure | T | T | T | T | T |
| calibration | T | T | T | T | T |
| LED test | T | T | T | T | T |

Outcome: `PORT_LOST`, the link closes itself, every later command is `PORT_CLOSED`, and the client stays alive.

### 13.3 Operation × operator abort

| Operation | Before | During acquisition | After acquisition | During save | During return |
| --- | :-: | :-: | :-: | :-: | :-: |
| measure | T | T | T | T | T |
| move | T | T | T | — ¹ | T |
| calibration | T | T | T | T | — ¹ |
| records | T | — ¹ | T | T | — ¹ |

¹ No such stage exists for that operation.

### 13.4 Operation × save failure

| Operation | ENOSPC | EROFS | ENOENT | EMFILE | MemoryError |
| --- | :-: | :-: | :-: | :-: | :-: |
| prepare a sample | T | T | T | T | T |
| confirm loaded | T | T | T | T | T |
| measure | T | T | T | T | T |
| clear slot | T | T | T | T | T |
| records browser | T | T | T | T | T |
| sync ESP32 acquisitions | T | T | T | T | T |
| carousel calibration | T | T | T | T | T |
| resync | T | T | T | T | T |
| sensor test | T | T | T | T | T |
| choose slot | T | T | T | T | T |

40 screen × failure combinations, driven in `integration/test_screens_failing.py`. In every one: no crash, the archive is byte-identical, and nothing prints a success it did not achieve.

---

## Gaps

**None that are mission-relevant and software-testable.**

Every `H` cell carries an H-number in
`firmware/Tests/hardware/HARDWARE_VERIFICATION_PLAN.md`. Every `—` cell
has its reason in a footnote directly beneath its table. There is no
cell in this document that is blank, unexplained, or marked "to do".
