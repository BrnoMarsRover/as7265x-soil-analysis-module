# Software Assumption Register

**Phase:** A.3
**Date:** 2026-08-25

Everything the software takes on faith, stated once, with an ID and an
owner. Nothing here can be settled by running a test on this machine —
that is what makes it an assumption rather than a fact.

Two families:

- **H-nnn** — hardware assumptions. Each maps to a section of
  `firmware/Tests/hardware/HARDWARE_VERIFICATION_PLAN.md`.
- **A-nnn** — environment and platform assumptions. Each is checked as
  far as software can check it, and the residue is stated.

The rule for the whole register: if the assumption is wrong, the entry
says **what breaks**. An assumption whose consequences are not written
down cannot be prioritised on the bench.

---

## H — hardware assumptions

### H-001 — the ST3215 settles within 15 counts

**Assumed:** a completed movement leaves the shaft within
`ST3215_POSITION_TOLERANCE` (15 counts, ≈ 1.3°) of its goal.

**Why software cannot settle it:** the number is a property of the
servo's control loop under this mechanism's inertia and friction.

**If wrong:** too tight and every good movement is reported as a
`SERVO_POSITION_MISMATCH`; too loose and a slot that stopped short is
accepted, so the scanner reads the edge of a well.

**Verified by:** fifty identical slot movements, recording every
`position_error` — `HARDWARE_VERIFICATION_PLAN.md` §servo.

---

### H-002 — the encoder tracks the mechanism

**Assumed:** the position the ST3215 reports corresponds to where the
shaft physically is.

**Why software cannot settle it:** the software arithmetic is verified
correct at all 4096 positions. Whether the number describes reality is
electrical and mechanical.

**Status:** **known to have been violated once.** On the bench a
carousel visibly rotated 180° while the encoder reported 2 counts.

**If wrong:** every position decision the firmware makes is built on a
number that does not describe the mechanism. This is the single most
important open item in the project.

**Verified by:** free-shaft relative move with a protractor, then the
same with the carousel attached — `HARDWARE_VERIFICATION_PLAN.md`
§servo (2,3,4,5).

---

### H-003 — the AS7265x is ready within the configured time

**Assumed:** the sensor raises data-ready within the integration time
plus `ACQUISITION_RESPONSE_SETTLE_MS`.

**If wrong:** either every acquisition times out, or — worse — a read
returns the *previous* conversion and the spectrum belongs to the last
illumination.

**Verified by:** data-ready latency across all three illuminations,
fifty cold initializations — `HARDWARE_VERIFICATION_PLAN.md` §sensor.

---

### H-004 — the CP2102 link is electrically stable

**Assumed:** the USB bridge stays enumerated for the length of a
mission, and `/dev/ttyUSB0` does not disappear.

**Status:** **known to have been violated once.** The node vanished
mid-session on the Linux bench.

**Software response, already built:** every errno that a vanished tty
can raise is classified as `PORT_LOST` at all four pySerial entry
points, the link closes itself, and every later command is refused as
`PORT_CLOSED` rather than crashing — `LNX-001`, `LNX-002`.

**If wrong again:** the mission is interrupted but not corrupted. The
software now survives it; what it cannot do is prevent it.

**Verified by:** repeated open/close ×1000 checking for a boot banner
each time — `HARDWARE_VERIFICATION_PLAN.md` §link.

---

### H-005 — a half turn is 2048 counts on this mechanism

**Assumed:** no reduction between the servo output and the carousel, so
4096 counts is one carousel revolution.

**If wrong:** every slot angle is wrong by the reduction ratio — and
H-002 explains itself.

**Verified by:** the same protractor test with the carousel attached —
`HARDWARE_VERIFICATION_PLAN.md` §servo (4).

---

### H-006 — backlash does not accumulate

**Assumed:** repeated load ↔ scan transfers do not drift.

**If wrong:** the carousel walks away from its origin over a
competition day, and the drift is invisible until a sample misses the
scanner.

**Verified by:** alternating movements ×200, measuring accumulated
offset — `HARDWARE_VERIFICATION_PLAN.md` §servo.

---

### H-007 — the ESP32 has enough heap during a real acquisition

**Assumed:** the device can build and send a full triad response
without fragmenting its heap past recovery.

**Why software cannot settle it:** CPython's allocator is nothing like
MicroPython's on a 520 KB device, and fragmentation depends on the real
allocation history.

**Software response, already built:** `MemoryError` is injected at the
firmware's real allocation boundaries and the guards are proven to work
— a response that cannot be joined is written in pieces, and any
handler that raises still produces a well-formed error frame naming
`MemoryError` (`MEM-009`, `MEM-010`).

**What is NOT claimed:** that the device has sufficient RAM. Only that
the code does the right thing when it does not.

**Verified by:** heap and endurance measurement on the device —
**to be added to the hardware plan.**

---

### H-008 — opening the port does not reset the board

**Assumed:** with DTR and RTS driven low before `open()`, the auto-reset
circuit does not fire.

**Status:** measured once on this development board, and the software is
built on that measurement.

**If wrong:** starting the client reboots an instrument that may be
holding a hand-synchronized carousel position. The software would
notice — the position comes back invalid — so this costs a
resynchronization, not data.

**Verified by:** open the port fifty times, checking for a boot banner —
`HARDWARE_VERIFICATION_PLAN.md` §link.

---

### H-009 — the illumination named RED is the IR lamp

**Assumed:** the requirement document's "RED" illuminator is the
hardware's IR channel. The board has white, UV and IR, and no red
illuminator.

**Why software cannot settle it:** it is a naming question about a
physical part.

**If wrong:** the spectra are correct and the *label* on one third of
them is wrong, which would misdescribe every stored measurement.

**Verified by:** confirming the fitted part against the schematic —
**to be added to the hardware plan.**

---

## A — environment and platform assumptions

### A-001 — the competition interpreter is CPython 3.8 or newer

**Checked in software:** no `match`/`case` anywhere in the mission tree;
every module parses and imports under the development interpreter
(3.14). `linux/test_linux_runtime.py`.

**Residue:** the tests here run on 3.14. The Linux main computer's
interpreter has **not** been pinned in this repository, and a syntax or
stdlib difference between 3.8 and 3.14 would not be caught by this
campaign.

**Action before the competition:** run
`python3 firmware/Tests/run_all.py` **on the rover's own machine**, with
its own interpreter. That single run converts this assumption into a
fact and needs no hardware attached.

---

### A-002 — `pyserial` is the only third-party runtime dependency

**Checked in software:** every import in `PC/`, `Science/` and `BD/` is
resolved against the standard library; exactly one third-party name
appears, and only under `PC/`. `linux/test_linux_runtime.py`.

**Residue:** none. This one is mechanically proven.

---

### A-003 — a missing `pyserial` is diagnosable

**Checked in software:** the message names the package, the exact `pip`
command, and `sys.executable` rather than a launcher that only exists on
Windows.

**Residue:** none.

---

### A-004 — `sqlite3` is present in the deployed Python

**Assumed:** the standard-library `sqlite3` module is importable.

**Why it matters:** `BD/decision_learning.py` imports it at module
level, and `workflow/session.py` imports that module at *its* top level.
The learning layer is designed to be optional at **runtime** — a
missing or corrupt database file is survivable and reported — but a
missing `sqlite3` **module** would fail at import, before any guard.

**Checked in software:** the runtime-optionality claim is tested (a
missing, empty and corrupt learning database are all survivable). The
module's presence is not, because it is present on every mainstream
build.

**If wrong:** the client does not start at all, with an `ImportError`
naming `sqlite3`.

**Action before the competition:** covered by the A-001 action — a
single run on the rover's own machine proves it.

---

### A-005 — the archive directory is writable and on one filesystem

**Assumed:** `firmware/BD/samples/` is writable, and the temporary file
`_write_json` creates lands on the same filesystem as the archive, so
`os.replace` is atomic.

**Checked in software:** the temporary is created with
`dir=path.parent`, which guarantees the same directory. Every way the
write can fail is tested (`FS-001`…`FS-010`).

**Residue:** atomicity of `rename(2)` is a filesystem guarantee. It
holds on ext4, xfs and btrfs; it does **not** hold across a mount
boundary, which the `dir=` argument prevents.

---

### A-006 — the terminal encoding may be anything

**Assumed:** stdout may be UTF-8, cp1252, latin-1 or ASCII.

**Checked in software:** the client reconfigures stdout, stderr and
stdin to a non-raising error handler at startup, and a record with
Czech text is printed on all four encodings without raising.
`LNX-013`.

**Residue:** none for correctness. On a non-UTF-8 terminal some
characters render as `\uXXXX` escapes; the **archive** is unaffected and
keeps real UTF-8.

---

### A-007 — the wall clock may be wrong; the monotonic clock may not

**Assumed:** `time.monotonic()` does not go backwards.

**Checked in software:** every timeout in the mission tree is built on
`time.monotonic()`; `time.time()` appears in mission code only inside a
comment explaining why it is not used. Wall-clock jumps of ±2 hours, to
1970 and 1000 days forward are all driven, and no timeout is disturbed.
`LNX-014`, `LNX-015`.

**Residue:** none. `time.monotonic()` is guaranteed monotonic by the
language.

---

### A-008 — one client, one port, one archive

**Assumed:** exactly one operator client runs against the module.

**Checked in software:** the port is opened with `exclusive = True`, so
a second client is refused with `PORT_BUSY`. Concurrent archive writers
are **not supported**, and the exclusive open is what makes a second
mission client impossible in the first place. `LNX-005`, `LNX-006`.

**Residue:** two processes could still both open the archive *without*
the port — for example the client and an offline analysis script. That
is last-writer-wins, the file is never corrupt, and it is documented
rather than defended.

---

### A-009 — the repository is deployed as checked out

**Assumed:** every path resolves from `__file__`, so the working
directory and `PYTHONPATH` do not change what the program reads or
writes.

**Checked in software:** no mission module reads any environment
variable; entry points resolve their own location; every suite resolves
the tree by directory **name** rather than by counting parents, and a
regression test enforces it. `LNX-016`.

**Residue:** the repository must be checked out with its case preserved.
`linux/test_linux.py` checks that no two files differ only by case and
that no import differs from its file only by case.

---

### A-010 — the firmware on the device matches this repository

**Assumed:** the `.mpy` files on the ESP32 were built from the `ESP32/`
tree in this checkout.

**Why software cannot settle it:** the host cannot see what is flashed.

**Partially checked:** every response records a firmware version, and
that version is stored on every Measurement, so a mismatch is
detectable *after the fact* in the archive.

**Action before the competition:** redeploy with `tools/device.py`
immediately before the run, and confirm the version reported by `ping`.

---

## Ownership summary — A.3 §121

| Category | Count | Owner |
| --- | --- | --- |
| Hardware, with a plan section | 9 (H-001…H-009) | Phase B, on the bench |
| Environment, mechanically proven | 6 (A-002, A-003, A-005, A-006, A-007, A-009) | Closed |
| Environment, needs one run on the rover's machine | 2 (A-001, A-004) | **Before the competition, no hardware needed** |
| Environment, documented design decisions | 2 (A-008, A-010) | Intentional |

Nothing in this register is unowned, and nothing is marked
`UNKNOWN`, `TODO` or `MAYBE`.

Two of these — A-001 and A-004 — are worth stating plainly, because
they are the only items in the whole campaign that could be closed
today and have not been: **the test campaign has never been run on the
Linux machine the rover actually uses.** It needs no hardware, no
carousel and no sensor. It is one command.
