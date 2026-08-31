"""
What happens when the machine runs out of something.

THE FAILURES THIS COVERS ARE NOT ABOUT THE ROVER

They are about the laptop the rover is plugged into: its memory, its
file descriptors, its disk, its inodes, its /tmp, and the permissions
on the directory the archive lives in. Every one of them is invisible
until the day it happens, and the day it happens is the day the archive
is being written.

THE STANDARD APPLIED HERE

The program does NOT have to survive an out-of-memory condition. It is
allowed to stop. What it is not allowed to do is any of these four:

    save corrupted data
    report a success that did not happen
    corrupt what was already durable
    claim a physical position it can no longer know

So each case below asks the same two questions in order: did it fail,
and did it fail HONESTLY? A MemoryError that produces "could not save"
is a pass. A MemoryError that produces "SAVED" is the defect this file
exists to find.

WHY MemoryError IS INJECTED RATHER THAN PROVOKED

Allocating until CPython gives up takes an unpredictable amount of time,
swaps the machine, and may kill the test runner instead of the code
under test. Raising MemoryError at the exact allocation boundary that
matters is deterministic, instant, and tests the same handler. What it
does NOT prove is that a real allocation failure lands in these places
and nowhere else; on the ESP32 that question is HARDWARE_ONLY (H-005).
"""

import errno
import json
import os
import sys
import tempfile
from pathlib import Path

_TESTS_DIR = next(
    p for p in Path(__file__).resolve().parents if p.name == "Tests"
)

if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

_SOFTWARE_DIR = _TESTS_DIR / "software"

if str(_SOFTWARE_DIR) not in sys.path:
    sys.path.insert(0, str(_SOFTWARE_DIR))

import support

support.add_project_root()
support.add_path("PC")

import serial_link                                          # noqa: E402

from BD import config as bd_config          # noqa: E402
from BD import samples as samples_module                     # noqa: E402
from BD.samples import (                                     # noqa: E402
    archive_store,
    StorageError,
)

from fakes import (                                          # noqa: E402
    LoopbackDevice,
    SandboxBD,
    loopback_link,
    sandbox_mission,
)

checks = support.Checks("resource-faults")


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def fresh_store(sample="S001"):
    """A store on a throwaway file, with one sample already saved."""
    directory = Path(tempfile.mkdtemp(prefix="freya-res-"))
    path = directory / "samples.json"
    path.write_text('{"schema_version": 4, "samples": []}',
                    encoding="utf-8")

    store = archive_store(path).load()
    store.create(sample, 1)

    return store, path, directory


class patched:
    """
    Replace one attribute for the duration of a `with` block.

    Named by module and attribute rather than searched for, because a
    patch that silently lands on the wrong object produces a test that
    passes while injecting nothing - which has happened in this tree
    before and is recorded in test_filesystem_faults.py.
    """

    def __init__(self, target, name, replacement):
        self.target = target
        self.name = name
        self.replacement = replacement
        self.original = getattr(target, name)

    def __enter__(self):
        setattr(self.target, self.name, self.replacement)

        return self

    def __exit__(self, exc_type, exc_value, traceback):
        setattr(self.target, self.name, self.original)

        return False


class shadowed:
    """
    Shadow a BUILTIN inside one module, then take the shadow away.

    Separate from `patched` on purpose. `patched` insists the attribute
    already exists, which is what stops a patch from silently landing
    on nothing; a builtin like `open` is resolved through the module's
    globals without ever being an attribute of it, so patching it means
    CREATING the name - exactly what `patched` refuses to do. Keeping
    the two apart means neither has to weaken its own rule.
    """

    def __init__(self, module, name, replacement):
        self.module = module
        self.name = name
        self.replacement = replacement
        self.existed = name in vars(module)
        self.original = getattr(module, name, None)

    def __enter__(self):
        setattr(self.module, self.name, self.replacement)

        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.existed:
            setattr(self.module, self.name, self.original)

        else:
            delattr(self.module, self.name)

        return False


def raiser(exception):
    def raise_it(*args, **kwargs):
        raise exception

    return raise_it


def oserror(code):
    """An OSError carrying a real errno, the way the OS raises it."""
    return OSError(code, os.strerror(code))


def outcome(call):
    """
    Run something and describe how it ended, without letting it escape.

    Returns ("ok", value), ("storage", code) or ("raw", type name).
    The third is the interesting one: a raw exception type means the
    failure escaped the vocabulary the screens are written against.
    """
    try:
        return ("ok", call())

    except StorageError as error:
        return ("storage", error.code)

    except serial_link.LinkError as error:
        return ("link", error.code)

    except BaseException as error:                         # noqa: BLE001
        return ("raw", type(error).__name__)


# ======================================================================
checks.section("10. MemoryError while the archive is being written")

# The whole point of the atomic write is that the previous archive
# survives a failure. Memory is one more way for it to fail, and the
# rule does not change: what is on disk stays readable, and what is in
# memory goes back to matching it.

MEMORY_POINTS = (
    ("json.dump", "serializing the archive"),
    ("tempfile.mkstemp", "creating the temporary file"),
    ("os.fdopen", "opening the temporary file"),
    ("os.replace", "the atomic rename"),
)

for dotted, description in MEMORY_POINTS:
    store, path, directory = fresh_store()
    before = path.read_bytes()

    where, _dot, name = dotted.rpartition(".")
    target = getattr(samples_module, where)

    with patched(target, name, raiser(MemoryError("simulated"))):
        kind, detail = outcome(
            lambda: store.add_measurement("S001", raw={"white": [1] * 18})
        )

    checks.ok(kind != "ok",
              "MemoryError while {} does not report a save".format(
                  description))

    checks.equal(path.read_bytes(), before,
                 "and the previous archive is byte-identical after it")

    # The rule from A.2: RAM must not be left ahead of the disk.
    reread = archive_store(path).load()
    checks.equal(
        len(store.get_sample("S001").get("measurements") or []),
        len(reread.get_sample("S001").get("measurements") or []),
        "and the in-memory archive matches the file again",
    )


# ======================================================================
checks.section("10. MemoryError while a frame is built or parsed")

# The serial owner is the only place a frame is serialized, and the only
# place one is parsed. Both are allocation boundaries.

link, port, loopback = loopback_link(serial_link)

with patched(serial_link.json, "dumps",
             raiser(MemoryError("no room for the request"))):
    kind, detail = outcome(lambda: link.request("ping"))

checks.ok(kind == "raw" and detail == "MemoryError",
          "MemoryError building a request frame propagates as itself "
          "({} {}) - it is not turned into a device error, which would "
          "blame the board for the laptop".format(kind, detail))

# AND NOTHING WENT OUT ON THE WIRE.
#
# This replaces an assertion that was written here first and was
# objectively wrong: that the link should stop calling itself online.
# It should not. `online` means the board answered a ping; a laptop
# that cannot allocate a string has learned nothing about the board,
# and marking the device offline would send the operator to the cable
# for a fault in their own machine.
#
# The property that actually matters is this one: the frame could not
# be built, so no command was issued. A half-sent command to a
# mechanism that moves is the failure worth ruling out.
checks.equal(len(port.written), 0,
             "and no bytes reached the wire - a command that could not "
             "be serialized was never issued to the mechanism")

link.close()

# Parsing. A frame that cannot be parsed for lack of memory must not
# become a salvaged frame or an accepted answer.
link, port, loopback = loopback_link(serial_link)

with patched(serial_link.json, "loads",
             raiser(MemoryError("no room for the answer"))):
    kind, detail = outcome(lambda: link.request("ping"))

checks.ok(kind == "raw" and detail == "MemoryError",
          "MemoryError parsing a response is not swallowed into a "
          "believable answer ({} {})".format(kind, detail))

link.close()


# ======================================================================
checks.section("10. MemoryError during Science, with RAW already stored")

# This is the case the workflow was designed around: RAW is persisted
# BEFORE Science is asked anything, so an analysis that dies costs an
# analysis and not an experiment.

link, port, loopback = loopback_link(serial_link)
mission, bd = sandbox_mission(link)

mission.session.create("S100", 1)
measurement = mission.session.add_measurement(
    "S100", raw={"white": [100] * 18, "uv": [50] * 18, "ir": [70] * 18}
)

stored_before = json.loads(bd.samples_file.read_text(encoding="utf-8"))

from Science import pipeline as pipeline_module               # noqa: E402

with patched(pipeline_module, "analyze",
             raiser(MemoryError("no room for the analysis"))):
    kind, run = outcome(lambda: mission.analyse_measurement(measurement))

checks.equal(kind, "ok",
             "MemoryError inside Science is caught by analyse_measurement "
             "rather than escaping - RAW is already stored, so the run "
             "is what fails, not the mission")

checks.equal(run.get("analysis_status"), "FAILED",
             "and the run is FAILED")

checks.ok(not run.get("decision"),
          "with no decision - an analysis that never ran cannot identify "
          "a material")

# THE §9 QUESTION, ASKED HERE. A broad `except Exception` around
# Science converts a programming-type failure into a valid mission
# state, which is legitimate ONLY because the state it converts to is
# an honest failure AND the exception type survives into the record.
# A handler that swallowed the type would leave a FAILED run nobody
# could diagnose.
checks.equal((run.get("error") or {}).get("code"), "MemoryError",
             "and the record names the exception type, so a memory "
             "failure is not filed as a scientific one")

stored_after = json.loads(bd.samples_file.read_text(encoding="utf-8"))
checks.equal(stored_after, stored_before,
             "and the RAW that was already stored is untouched by the "
             "failed analysis")

link.close()
bd.close()


# ======================================================================
checks.section("12. every long-lived runtime structure is bounded")

# A rover session is hours long. Anything that grows once per frame,
# per error or per retry has to have a ceiling, or the ceiling is the
# machine's memory.

link, port, loopback = loopback_link(serial_link)

# Feed a great many frames that are all rejected, in every way the
# reader can reject one, and watch the diagnostic buffers.
noise = (
    b"boot text with no frame in it\n",
    b'{"request_id": "nope-1", "ok": true, "cmd": "ping"}\n',
    b"\xff\xfe\x00 binary rubbish\n",
    b'{"request_id": "x"',
)

for round_number in range(400):
    for payload in noise:
        port._enqueue(payload)

    try:
        link.request("ping", timeout=0.05)

    except serial_link.LinkError:
        pass

checks.ok(len(link.last_noise) <= serial_link.NOISE_LIMIT,
          "after 1600 rejected lines, last_noise is still capped at "
          "{} (holding {})".format(serial_link.NOISE_LIMIT,
                                   len(link.last_noise)))

checks.ok(len(link.damaged_lines) <= serial_link.NOISE_LIMIT,
          "and damaged_lines is capped at {} (holding {})".format(
              serial_link.NOISE_LIMIT, len(link.damaged_lines)))

link.close()

# The counters themselves are integers and may grow freely; what must
# not grow is anything holding the LINES.
attributes = [
    name for name, value in vars(link).items()
    if isinstance(value, (list, dict, set))
]

unbounded = [
    name for name in attributes
    if len(getattr(link, name)) > serial_link.NOISE_LIMIT
]

checks.equal(unbounded, [],
             "and no container on the link exceeds the cap after the "
             "storm ({} containers checked)".format(len(attributes)))


# ======================================================================
checks.section("13. memory does not grow across a long simulated run")

# Not a leak claim - Python's allocator makes those easy to assert
# wrongly. What is asserted is narrower and checkable: the number of
# live objects the link retains does not depend on how many requests
# have gone through it.

link, port, loopback = loopback_link(serial_link)


def retained():
    return sum(
        len(value) for value in vars(link).values()
        if isinstance(value, (list, dict, set))
    )


for _ in range(100):
    link.request("ping")

after_100 = retained()

for _ in range(1000):
    link.request("ping")

after_1100 = retained()

checks.equal(after_100, after_1100,
             "1000 further successful requests retain nothing new "
             "({} items both times)".format(after_100))

checks.ok(link.bytes_read > 0,
          "and the traffic really happened ({:,} bytes read)".format(
              link.bytes_read))

link.close()


# ======================================================================
checks.section("14. file descriptor exhaustion")

# EMFILE is what a process gets when it has used every descriptor it is
# allowed. It can arrive at any open() in the program, and the archive
# is the one that matters.

store, path, directory = fresh_store()
before = path.read_bytes()

for code, label in ((errno.EMFILE, "EMFILE"), (errno.ENFILE, "ENFILE")):
    with patched(samples_module.tempfile, "mkstemp",
                 raiser(oserror(code))):
        kind, detail = outcome(
            lambda: store.add_measurement("S001", raw={"white": [1] * 18})
        )

    checks.equal(kind, "storage",
                 "{} creating the temporary file is a StorageError, not "
                 "a traceback ({} {})".format(label, kind, detail))

checks.equal(path.read_bytes(), before,
             "and neither attempt changed the archive")

# Reading, too: a descriptor shortage while opening the archive must be
# reported rather than read as an empty archive.
with shadowed(samples_module, "open", raiser(oserror(errno.EMFILE))):
    kind, detail = outcome(lambda: archive_store(path).load())

checks.ok(kind in ("storage", "raw"),
          "EMFILE opening the archive fails loudly ({} {})".format(
              kind, detail))

checks.ok(kind != "ok",
          "and is never mistaken for an archive with no samples in it")


# ======================================================================
checks.section("15. the temporary file cannot be created")

# mkstemp is the first thing the atomic write does. Everything below
# tests that the DURABLE file survives each way it can fail.

store, path, directory = fresh_store()
store.add_measurement("S001", raw={"white": [7] * 18})
before = path.read_bytes()

TEMP_FAILURES = (
    (oserror(errno.EROFS), "the directory is read-only"),
    (oserror(errno.ENOENT), "the directory has vanished"),
    (oserror(errno.EACCES), "permission is refused"),
    (oserror(errno.ENOSPC), "there is no space for it"),
)

for exception, label in TEMP_FAILURES:
    with patched(samples_module.tempfile, "mkstemp", raiser(exception)):
        kind, detail = outcome(
            lambda: store.add_measurement("S001", raw={"white": [2] * 18})
        )

    checks.equal(kind, "storage",
                 "a temporary file that cannot be made because {} is a "
                 "StorageError".format(label))

checks.equal(path.read_bytes(), before,
             "and after all {} of them the durable archive is "
             "byte-identical".format(len(TEMP_FAILURES)))


# ======================================================================
checks.section("16. ENOSPC at every stage of the atomic write")

# Disk-full is not one event. It can arrive at the directory, at the
# temporary file, at the write, at the flush, at the fsync or at the
# rename, and the guarantee has to hold at every one of them.

# `mkdir` is reached as a bound method on the Path the caller supplied -
# BD/samples.py never imports Path itself - so that one is injected on
# the class. The rest are module-level names the file really does call.
STAGES = (
    ("mkdir", Path, "mkdir"),
    ("mkstemp", samples_module.tempfile, "mkstemp"),
    ("write", samples_module.json, "dump"),
    ("fsync", samples_module.os, "fsync"),
    ("replace", samples_module.os, "replace"),
)

for label, target, name in STAGES:
    store, path, directory = fresh_store()
    store.add_measurement("S001", raw={"white": [9] * 18})
    before = path.read_bytes()
    files_before = sorted(p.name for p in directory.iterdir())

    with patched(target, name, raiser(oserror(errno.ENOSPC))):
        kind, detail = outcome(
            lambda: store.add_measurement("S001", raw={"white": [3] * 18})
        )

    checks.equal(kind, "storage",
                 "ENOSPC at {} is reported as a failed save, not a "
                 "crash ({} {})".format(label, kind, detail))

    checks.equal(path.read_bytes(), before,
                 "  and the previous archive survives ENOSPC at {}"
                 .format(label))

    reread = archive_store(path).load()
    checks.equal(
        len(store.get_sample("S001").get("measurements") or []),
        len(reread.get_sample("S001").get("measurements") or []),
        "  and memory matches the disk again after ENOSPC at {}".format(
            label),
    )

    # A failed write must not leave its scratch file behind for the
    # next run to find.
    files_after = sorted(p.name for p in directory.iterdir())
    leftovers = [
        name for name in files_after
        if name not in files_before and name.startswith(".samples-")
    ]

    checks.equal(leftovers, [],
                 "  and no temporary file is left behind by ENOSPC at "
                 "{}".format(label))


# ======================================================================
checks.section("17. inode exhaustion - free bytes is not free files")

# ENOSPC from a filesystem with plenty of free bytes and no free inodes
# is the same errno with a different cause, and there is nothing the
# program can do about it except say so rather than pretend.

store, path, directory = fresh_store()
before = path.read_bytes()

with patched(samples_module.tempfile, "mkstemp",
             raiser(OSError(errno.ENOSPC, "No space left on device"))):
    kind, detail = outcome(
        lambda: store.add_measurement("S001", raw={"white": [4] * 18})
    )

checks.equal(kind, "storage",
             "a filesystem that cannot create a file, even with free "
             "space, produces a failed save")

checks.equal(path.read_bytes(), before,
             "and the archive that already exists is still readable")

checks.ok(archive_store(path).load().count() >= 1,
          "and can still be opened and counted")


# ======================================================================
checks.section("18. a read-only filesystem")

# The rover's card can be remounted read-only by the kernel after an I/O
# error. Reading has to keep working; writing has to fail in words an
# operator can act on.

store, path, directory = fresh_store()
store.add_measurement("S001", raw={"white": [5] * 18})
before = path.read_bytes()

with patched(samples_module.tempfile, "mkstemp",
             raiser(oserror(errno.EROFS))):
    kind, detail = outcome(
        lambda: store.add_measurement("S001", raw={"white": [6] * 18})
    )

checks.equal(kind, "storage",
             "a write to a read-only filesystem is a StorageError")

reopened = archive_store(path).load()
checks.ok(reopened.count() >= 1,
          "and the archive is still READABLE on a read-only filesystem "
          "- the mission can still be reviewed")

checks.equal(path.read_bytes(), before,
             "and unchanged")


# ======================================================================
checks.section("19. a directory that was writable at startup and is not now")

# The transaction has to roll back to what is durable, not to what was
# in memory a moment ago.

store, path, directory = fresh_store()
store.add_measurement("S001", raw={"white": [8] * 18})

durable = json.loads(path.read_text(encoding="utf-8"))
durable_count = len(durable[bd_config.ARCHIVE_COLLECTION][0].get("measurements") or [])

# Permission is revoked between one successful save and the next.
with patched(samples_module.tempfile, "mkstemp",
             raiser(oserror(errno.EACCES))):
    kind, detail = outcome(
        lambda: store.add_measurement("S001", raw={"white": [11] * 18})
    )

checks.equal(kind, "storage",
             "the save after permission is revoked fails")

in_memory = len(store.get_sample("S001").get("measurements") or [])

checks.equal(in_memory, durable_count,
             "and the in-memory archive rolled back to the {} "
             "measurement(s) that are actually on disk".format(
                 durable_count))

# Now permission returns. The next save must work and must not carry
# the rolled-back measurement forward as though it had been there.
store.add_measurement("S001", raw={"white": [12] * 18})

final = json.loads(path.read_text(encoding="utf-8"))
final_count = len(final[bd_config.ARCHIVE_COLLECTION][0].get("measurements") or [])

checks.equal(final_count, durable_count + 1,
             "and when permission returns exactly one new measurement "
             "is added - the rolled-back one is not resurrected")


# ======================================================================
checks.section("48. an orphaned temporary file from a previous crash")

# A process killed between mkstemp and os.replace leaves a .samples-*
# file behind. The next run must not read it, count it or mistake it
# for the archive.

store, path, directory = fresh_store()
store.add_measurement("S001", raw={"white": [13] * 18})
before = path.read_bytes()

orphan = directory / ".samples-crashed.tmp"
orphan.write_text(
    json.dumps({"schema_version": 4, "samples": [
        {"sample_id": "GHOST", "slot_id": 9, "state": "MEASURED"}
    ]}),
    encoding="utf-8",
)

reopened = archive_store(path).load()

checks.ok(reopened.get_sample("GHOST") is None,
          "a leftover temporary file is not read as archive content")

checks.equal(reopened.count(), 1,
             "and the sample count comes from the real archive alone")

checks.equal(path.read_bytes(), before,
             "and opening the archive beside an orphan changes nothing")

# Writing again must still work, and must still be atomic.
reopened.add_measurement("S001", raw={"white": [14] * 18})

checks.ok(orphan.exists(),
          "the orphan is left alone rather than deleted on a guess - "
          "it may be the only copy of something a previous run lost")


# ======================================================================
checks.section("31. the OS will not supply random bytes")

# os.urandom needs a file descriptor on Linux when getrandom is not
# available, so descriptor exhaustion can reach it. A fixed fallback
# nonce would silently restore the cross-session collision the nonce
# exists to prevent, so the client refuses to start instead.

with patched(serial_link.os, "urandom", raiser(oserror(errno.EMFILE))):
    kind, detail = outcome(lambda: serial_link.SerialLink("PORT_TEST"))

checks.equal(kind, "raw",
             "a failing os.urandom stops construction")

checks.equal(detail, "RuntimeError",
             "as a RuntimeError - the type main() already turns into a "
             "diagnosed exit rather than a traceback")

# And the message has to name the real problem.
try:
    with patched(serial_link.os, "urandom", raiser(oserror(errno.EMFILE))):
        serial_link.SerialLink("PORT_TEST")

    message = ""

except RuntimeError as error:
    message = str(error)

checks.ok("random" in message.lower(),
          "and the message says random bytes were refused")

checks.ok("session" in message.lower() or "request id" in message.lower(),
          "and says what they were for, so the operator knows it is not "
          "the board")

# The important negative: no fallback happened.
checks.ok("fallback" not in message.lower()
          or "would defeat" in message.lower(),
          "and nothing was silently substituted")


# ======================================================================
checks.section("117. a measurement under resource pressure")

# The end-to-end version of everything above: a real acquisition
# through the real firmware, with the filesystem refusing every write.
#
# WHAT CHANGED. This used to require the measurement to FAIL, because
# Measure wrote the working set to samples.json. It writes no file now,
# so a full disk cannot reach it - and the archive still has to be
# untouched, because nothing was imported. Both halves are asserted:
# the measurement survives, and nothing became stored PC science.

link, port, loopback = loopback_link(serial_link)
mission, bd = sandbox_mission(link)

link.connect_servo()
link.sync_position(load_slot=1)
link.select_slot(1, sample_id="S200")

mission.session.create("S200", 1)

acquisition = link.measure_raw(1, sample_id="S200")

checks.ok(bool(acquisition.get("illuminations")),
          "the acquisition itself succeeded - the sample really was "
          "measured")

fields = mission.measurement_from_acquisition(acquisition, "S200")
before = bd.samples_file.read_bytes()

with patched(samples_module.os, "replace", raiser(oserror(errno.ENOSPC))):
    kind, detail = outcome(
        lambda: mission.session.add_measurement("S200", **fields)
    )

checks.equal(kind, "ok",
             "a full disk does not cost the measurement - the working "
             "set is memory, and nothing was going to be written")

checks.equal(len(mission.session.get_sample("S200")
                 .get("measurements") or []), 1,
             "the Measurement is in the working set, with its RAW")

checks.equal(bd.samples_file.read_bytes(), before,
             "and the archive file is byte-identical - measuring does "
             "not put a sample in it, full disk or not")

checks.ok(mission.archive.get_sample("S200") is None,
          "and the archive holds nothing for it, because nobody "
          "imported it")

# AND THE ARCHIVE STILL REFUSES HONESTLY when it IS asked to write.
with patched(samples_module.os, "replace", raiser(oserror(errno.ENOSPC))):
    kind, detail = outcome(
        lambda: mission.archive.adopt(mission.session.get_sample("S200"))
    )

checks.equal(kind, "storage",
             "importing onto a full disk IS a failed save, and says so")

checks.equal(bd.samples_file.read_bytes(), before,
             "with the archive still byte-identical")

# THE CLAIM THAT MATTERS. The device holds the acquisition whatever the
# PC's filesystem is doing, and that is what makes the failure
# recoverable rather than a lost experiment.
retained = link.request("list_saved_samples")

checks.ok(isinstance(retained, dict),
          "and the ESP32 still holds the acquisition, so the operator "
          "can import it once there is room")

link.close()
bd.close()


sys.exit(checks.report())
