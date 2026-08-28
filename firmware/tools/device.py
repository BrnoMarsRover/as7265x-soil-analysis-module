"""
The ESP32 device tool: status, clean, deploy, verify, reset.

ONE command deploys the firmware:

    python3 firmware/tools/device.py deploy --port /dev/ttyUSB0 --clean
    py      firmware/tools/device.py deploy --port COM4         --clean

The first line is the Linux main computer, the second the Windows
bench; forward slashes work on both. On Linux --port may be left out
when exactly one USB-serial device is attached - see default_port().

It is not allowed to report success until the board has been reset,
has booted on its own, and has answered a ping. Everything else here
exists to make that one command trustworthy.

WHY A MANIFEST RATHER THAN A DIRECTORY SCAN

ESP32_FILES below is the authoritative list of what runs on the device.
Uploading "every .py file under ESP32/" instead would mean that a
scratch file, a half-finished module or an editor backup left in the
source directory silently becomes part of the firmware - and that
whatever the device is running depends on the state of somebody's
working copy. The manifest is explicit, short, and reviewed.

WHY CLEAN MATTERS

MicroPython's filesystem persists across deployments. A file that the
firmware no longer imports stays on the device forever, and the day
somebody adds an import with the same name it comes back from the dead.
This project has already lived through exactly that: the device carried
`drivers/`, `control/` and `protocol/` packages from an architecture
that no longer exists. `--clean` removes every user file before
uploading, so what is on the device is the manifest and nothing else.

Cleaning removes USER FILES ONLY. The MicroPython runtime lives in
flash, not in the filesystem, and is never touched.

WORKING DIRECTORY INDEPENDENCE

Every path is resolved from this file's own location. Running the tool
from C:\\Users\\Maksym uploads exactly the same files as running it from
the repository root; it never uploads whatever happens to be in the
current directory.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path


# ======================================================================
# paths - resolved from THIS FILE, never from the working directory
# ======================================================================

TOOLS_DIR = Path(__file__).resolve().parent
FIRMWARE_DIR = TOOLS_DIR.parent
REPO_ROOT = FIRMWARE_DIR.parent
ESP32_DIR = FIRMWARE_DIR / "ESP32"
PC_DIR = FIRMWARE_DIR / "PC"


# ======================================================================
# the manifest
# ======================================================================
# The complete contents of the device filesystem. Nothing else is
# uploaded, and after a clean deployment nothing else is present.
#
# Order is load order for the import check, so a module is only
# imported after everything it imports.

ESP32_SOURCES = (
    "config.py",
    "sensor.py",
    "servo.py",
    "carousel.py",
    "protocol.py",
    "main.py",
    "boot.py",
)

# ======================================================================
# WHY THE FIRMWARE IS COMPILED BEFORE IT IS UPLOADED
# ======================================================================
# MicroPython compiles a .py file to bytecode ON THE DEVICE, at import,
# and the parse tree it builds to do that is the largest transient
# allocation the board ever makes. These modules are ~7500 lines, and
# the board could no longer afford it:
#
#     deploying the sources        IMPORTS FAIL   MemoryError,
#                                  allocating 1196 bytes
#
# That is not a warning about the future. The firmware would not start.
#
# It also explains a fault that looked nothing like a build problem.
# Compiling on the device leaves the heap in pieces, and a response of a
# few kilobytes needs its bytes CONTIGUOUS: measured on the board, 86576
# bytes free and not one hole of 1457. Every WHITE/UV/IR triad after the
# first came back RESPONSE_TOO_LARGE, having spent its full 24 seconds
# reading the sensor first.
#
#     deployed as .py     largest free block    8 kB
#     deployed as .mpy    largest free block   32 kB
#
# So the five large modules are compiled here, by mpy-cross, and the
# device receives bytecode it can load without a compiler. The SOURCE
# stays the source of truth - nothing is generated into the repository
# and nothing is edited in bytecode form.
#
# main.py and boot.py stay as source. MicroPython looks for those two by
# name at startup, they are 116 and 16 lines, and keeping them readable
# on the device is worth more than the few hundred bytes.
PRECOMPILED = ("config", "sensor", "servo", "carousel", "protocol")

SOURCE_ON_DEVICE = ("main.py", "boot.py")

# What the device filesystem holds after a clean deployment.
ESP32_FILES = tuple(
    name + ".mpy" for name in PRECOMPILED
) + SOURCE_ON_DEVICE

# Modules the import check exercises, in dependency order. boot.py is
# excluded because importing it does nothing by design, and main.py
# because importing it would start the serving loop - which is exactly
# what the reset-and-ping step tests properly.
IMPORT_CHECK = ("config", "sensor", "servo", "carousel", "protocol")


# ======================================================================
# mpremote
# ======================================================================

MPREMOTE = [sys.executable, "-m", "mpremote"]

# Temporary build directory, created on first use by build_firmware().
BUILD_DIR = None


class DeviceError(Exception):
    """A step of the pipeline failed."""


def run_mpremote(port, *args, timeout=120, check=True):
    """
    Run one mpremote command against the given port.

    THE CHILD IS TAKEN DOWN WITH US.

    `subprocess.run` does not reliably kill its child on Ctrl+C: the
    KeyboardInterrupt unwinds this process while mpremote keeps
    running, keeps the serial handle, and outlives the tool that
    started it. The next deployment then fails with
    "failed to access COM4 (it may be in use by another program)" and
    the program in question is an orphan of the run that was
    interrupted - which is a genuinely confusing thing to debug,
    because the obvious suspect is the operator's own client.

    Popen plus an explicit kill in `finally` closes that window.
    """
    command = MPREMOTE + ["connect", port] + [str(a) for a in args]

    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )

    try:
        stdout, stderr = process.communicate(timeout=timeout)

    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()

        raise DeviceError(
            "mpremote timed out after {} s: {}".format(
                timeout, " ".join(str(a) for a in args)
            )
        )

    except BaseException:
        # KeyboardInterrupt included, deliberately: it is the case
        # this exists for.
        process.kill()

        try:
            process.communicate(timeout=5)

        except Exception:
            pass

        raise

    result = subprocess.CompletedProcess(
        command, process.returncode, stdout, stderr
    )

    if check and result.returncode != 0:
        raise DeviceError(
            "mpremote {} failed (exit {}):\n{}\n{}".format(
                " ".join(str(a) for a in args), result.returncode,
                result.stdout.strip(), result.stderr.strip(),
            )
        )

    return result


def device_listing(port):
    """
    Every entry in the device's root, as (name, is_directory).

    `fs ls` prints a size and a name per line; a directory is printed
    with a trailing slash.
    """
    result = run_mpremote(port, "fs", "ls", ":")

    entries = []

    for line in result.stdout.splitlines():
        line = line.strip()

        if not line or line.startswith("ls "):
            continue

        parts = line.split(None, 1)

        if len(parts) != 2:
            continue

        name = parts[1].strip()

        entries.append((name.rstrip("/"), name.endswith("/")))

    return entries


def device_sha256(port, name):
    """The device's own SHA256 of one file, or None if it has none."""
    result = run_mpremote(port, "fs", "sha256sum", ":" + name, check=False)

    if result.returncode != 0:
        return None

    for line in result.stdout.split():
        token = line.strip()

        if len(token) == 64:
            try:
                int(token, 16)

            except ValueError:
                continue

            return token

    return None


def local_sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ======================================================================
# reporting
# ======================================================================

class Report:
    """The pass/fail table the pipeline prints as it goes."""

    def __init__(self):
        self.rows = []
        self.failed = False

    def step(self, label, ok, detail=""):
        self.rows.append((label, ok, detail))

        if not ok:
            self.failed = True

        print("{:<14}{}{}".format(
            label, "PASS" if ok else "FAIL",
            "   " + detail if detail else "",
        ))

        return ok

    def note(self, text):
        print("              {}".format(text))


# ======================================================================
# steps
# ======================================================================

def build_firmware(report):
    """
    Compile the large modules to bytecode and return {device name: path}.

    The build directory is temporary and per-run. Nothing is generated
    into the repository: a checked-in .mpy is a second copy of the
    firmware that can disagree with the source, and the whole point of
    the content check below is that there is exactly one answer to
    "what is on the device".

    mpy-cross output is REPRODUCIBLE FROM THE SAME CHECKOUT, which is
    what `verify` needs, but it is NOT path-independent: the compiler
    embeds the source path so a traceback on the device can name a
    file, and the same bytes compiled from a different directory - or
    with different line endings - produce a different .mpy.

    Measured 2026-08-27: identical source content compiled under two
    directory names gave two different SHA256 at 2152 and 2153 bytes.

    So `verify` rebuilds and compares without keeping an artefact
    around, and that is sound **as long as it runs from the checkout
    that deployed**. Verifying a device from a second copy of the
    repository reports a CONTENT mismatch that is about the path, not
    about the bytes that matter.
    """
    global BUILD_DIR

    if BUILD_DIR is None:
        BUILD_DIR = Path(tempfile.mkdtemp(prefix="freya-build-"))

    built = {}

    for name in PRECOMPILED:
        source = ESP32_DIR / (name + ".py")
        target = BUILD_DIR / (name + ".mpy")

        result = subprocess.run(
            [sys.executable, "-m", "mpy_cross", "-o", str(target),
             str(source)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

        if result.returncode != 0 or not target.is_file():
            message = (result.stderr or result.stdout).strip()[-300:]

            if "No module named" in message:
                message = (
                    "mpy-cross is not installed. It must match the "
                    "firmware on the board (MicroPython v1.28.0): "
                    "{} -m pip install mpy-cross==1.28.0.post2".format(
                        sys.executable)
                )

            report.step("BUILD", False,
                        "{}: {}".format(source.name, message))

            return None

        built[name + ".mpy"] = target

    for name in SOURCE_ON_DEVICE:
        built[name] = ESP32_DIR / name

    total = sum(path.stat().st_size for path in built.values())
    source_total = sum(
        (ESP32_DIR / (name + ".py")).stat().st_size for name in PRECOMPILED
    )

    report.step(
        "BUILD", True,
        "{} modules compiled, {} bytes of source -> {} bytes on device".format(
            len(PRECOMPILED), source_total, total,
        ),
    )

    return built


def check_manifest_sources(report):
    """Every manifest file exists locally, and nothing stray sits beside it."""
    missing = [n for n in ESP32_SOURCES if not (ESP32_DIR / n).is_file()]

    if missing:
        return report.step(
            "SOURCES", False,
            "missing from {}: {}".format(ESP32_DIR, ", ".join(missing)),
        )

    on_disk = {p.name for p in ESP32_DIR.glob("*.py")}
    extra = sorted(on_disk - set(ESP32_SOURCES))

    if extra:
        # Not fatal - it just will not be uploaded - but silence here
        # is how a file everybody assumes is running turns out not to
        # be on the device at all.
        report.step("SOURCES", True, "{} files".format(len(ESP32_SOURCES)))
        report.note(
            "not in the manifest, so NOT uploaded: " + ", ".join(extra)
        )

        return True

    return report.step("SOURCES", True,
                       "{} files".format(len(ESP32_SOURCES)))


def check_port(port, report):
    """The port exists and something answers on it."""
    try:
        sys.path.insert(0, str(PC_DIR))

        from serial_link import SerialLink

        names = [p["port"] for p in SerialLink.available_ports()]

    except ImportError:
        names = None

    if names is not None and port not in names:
        return report.step(
            "PORT", False,
            "{} not found; available: {}".format(
                port, ", ".join(names) or "none"
            ),
        )

    result = run_mpremote(port, "fs", "ls", ":", check=False, timeout=30)

    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        hint = detail[-1] if detail else "no detail"

        return report.step("PORT", False, "{}: {}".format(port, hint))

    return report.step("PORT", True, port)


def clean_device(port, report):
    """
    Remove every user file and directory from the device filesystem.

    The MicroPython runtime is in flash and is not part of the
    filesystem, so nothing here can damage it.
    """
    entries = device_listing(port)

    if not entries:
        return report.step("CLEAN", True, "already empty")

    removed = []

    for name, is_directory in entries:
        args = ["fs", "rm", "-r", ":" + name] if is_directory else \
               ["fs", "rm", ":" + name]

        result = run_mpremote(port, *args, check=False)

        if result.returncode != 0:
            return report.step(
                "CLEAN", False,
                "could not remove {}: {}".format(
                    name, (result.stderr or result.stdout).strip()[-200:]
                ),
            )

        removed.append(name + ("/" if is_directory else ""))

    left = device_listing(port)

    if left:
        return report.step(
            "CLEAN", False,
            "still present: {}".format(", ".join(n for n, _ in left)),
        )

    return report.step(
        "CLEAN", True, "removed {}".format(", ".join(removed))
    )


UPLOAD_ATTEMPTS = 3


def upload(port, report, built):
    """
    Copy every manifest file to the device root, and prove each arrived.

    A COPY THAT RETURNS ZERO IS NOT A COPY THAT ARRIVED. Observed on
    this bench: `mpremote fs cp` reported success for servo.mpy and put
    22507 of its 22532 bytes on the device - 25 bytes short, exit code
    0, no warning anywhere. Bytecode truncated that way does not fail
    politely; the board came up with

        MemoryError: memory allocation failed, allocating 4294966961 bytes

    which is a length field read out of the end of a file that stopped
    early, and the firmware would not boot at all.

    So every file is hashed on the device as soon as it lands, and a
    file that does not match is sent again. The whole-deployment
    CONTENT check still runs afterwards - this is a retry, not a
    replacement for verification.
    """
    resent = []

    for name in ESP32_FILES:
        source = built[name]
        expected = local_sha256(source)

        for attempt in range(UPLOAD_ATTEMPTS):
            result = run_mpremote(
                port, "fs", "cp", "-f", str(source), ":" + name,
                check=False, timeout=180,
            )

            if result.returncode != 0:
                if attempt + 1 < UPLOAD_ATTEMPTS:
                    resent.append(name)

                    continue

                return report.step(
                    "UPLOAD", False,
                    "{}: {}".format(
                        name, (result.stderr or result.stdout).strip()[-200:]
                    ),
                )

            landed = device_sha256(port, name)

            if landed is None or landed == expected:
                # None means this build cannot hash on the device; the
                # CONTENT step reports that separately.
                break

            if attempt + 1 >= UPLOAD_ATTEMPTS:
                return report.step(
                    "UPLOAD", False,
                    "{} arrived damaged {} times running".format(
                        name, UPLOAD_ATTEMPTS
                    ),
                )

            resent.append(name)

    if resent:
        report.step(
            "UPLOAD", True,
            "{} files, {} resent after a damaged copy".format(
                len(ESP32_FILES), len(resent)
            ),
        )
        report.note("resent: " + ", ".join(sorted(set(resent))))

        return True

    return report.step(
        "UPLOAD", True, "{} files".format(len(ESP32_FILES))
    )


def check_remote_manifest(port, report, strict=True):
    """
    The device holds the manifest, and - after a clean - nothing else.

    An unexpected file is only a failure when the deployment claimed to
    clean first; otherwise it is reported and the pipeline continues,
    because an incremental deploy onto an old filesystem is a
    legitimate thing to do knowingly.
    """
    entries = device_listing(port)
    present = {name for name, _ in entries}

    missing = [n for n in ESP32_FILES if n not in present]

    if missing:
        return report.step(
            "MANIFEST", False, "missing on device: " + ", ".join(missing)
        )

    unexpected = sorted(present - set(ESP32_FILES))

    if unexpected:
        if strict:
            return report.step(
                "MANIFEST", False,
                "stale on device: " + ", ".join(unexpected),
            )

        report.step("MANIFEST", True,
                    "{} files".format(len(ESP32_FILES)))
        report.note("also present (not in the manifest): "
                    + ", ".join(unexpected))

        return True

    return report.step("MANIFEST", True,
                       "{} files, nothing else".format(len(ESP32_FILES)))


def check_content(port, report, built):
    """
    The bytes on the device are the bytes in the repository.

    A copy that returned zero proves the command ran, not that the file
    arrived intact. The device computes its own SHA256 and it must
    match the local one.
    """
    mismatched = []
    unverifiable = []

    for name in ESP32_FILES:
        remote = device_sha256(port, name)

        if remote is None:
            unverifiable.append(name)

            continue

        if remote != local_sha256(built[name]):
            mismatched.append(name)

    if mismatched:
        report.step(
            "CONTENT", False, "hash mismatch: " + ", ".join(mismatched)
        )

        # WHICH OF THE TWO CAUSES IS IT? Both look identical here, and
        # they call for opposite responses: one is a corrupted or stale
        # upload, the other is a phantom.
        report.note(
            "if EVERY file mismatches, suspect the checkout rather than "
            "the device: mpy-cross embeds the source path, so rebuilding "
            "from a different copy of the repository - or one with "
            "different line endings - cannot reproduce the bytes that "
            "were uploaded. Re-run verify from the checkout that "
            "deployed. If only SOME files mismatch, the upload is the "
            "suspect: re-deploy with --clean."
        )

        return False

    if unverifiable:
        report.step("CONTENT", True, "{} files verified".format(
            len(ESP32_FILES) - len(unverifiable)))
        report.note("device could not hash: " + ", ".join(unverifiable))

        return True

    return report.step(
        "CONTENT", True,
        "sha256 matches for all {} files".format(len(ESP32_FILES)),
    )


def check_imports(port, report):
    """
    Every module imports cleanly on the device.

    This catches a syntax error or a missing name before the reset, so
    a broken deployment fails here rather than by leaving the board at
    a REPL prompt afterwards.

    It is NOT a boot test. Importing a module by hand proves nothing
    about whether main.py runs by itself - that is what the reset and
    ping steps are for.

    It also leaves the board at the REPL, because mpremote interrupts
    whatever is running to take control and does not restart it. That
    is precisely why this step comes BEFORE the reset and never after.
    """
    statement = "; ".join("import " + m for m in IMPORT_CHECK)

    result = run_mpremote(port, "exec", statement, check=False, timeout=90)

    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()

        return report.step(
            "IMPORTS", False, detail[-1] if detail else "failed"
        )

    return report.step(
        "IMPORTS", True, ", ".join(IMPORT_CHECK)
    )


def reset_and_ping(port, report):
    """
    Reset, let the board boot ON ITS OWN, then ping and get_status.

    No manual import, no mpremote exec. This is the only step that
    proves the deployment actually works: boot.py and main.py run
    because the board started, and the protocol answers because
    main.py reached its serving loop.
    """
    sys.path.insert(0, str(PC_DIR))

    try:
        from serial_link import LinkError, SerialLink

    except ImportError as error:
        return report.step("RESET", False,
                           "pyserial is required: {}".format(error))

    link = SerialLink(port, verbose=False)

    try:
        link.open()

    except LinkError as error:
        return report.step("RESET", False,
                           "{}: {}".format(error.code, error.message))

    try:
        link.hard_reset()
        report.step("RESET", True, "RTS pulse, DTR low")

        # The board runs its first-stage bootloader before main.py.
        time.sleep(0.6)

        try:
            data = link.wait_online(timeout=20)

        except LinkError as error:
            report.step("PING", False,
                        "{}: {}".format(error.code, error.message))

            return False

        identity = link.ping()

        report.step("PING", True, "{} {} (protocol {})".format(
            identity.get("firmware"), identity.get("version"),
            identity.get("protocol_version"),
        ))

        status = link.get_status()

        sensor = (status.get("sensor") or {}).get("state")
        servo = (status.get("servo") or {}).get("connected")
        carousel = (status.get("carousel") or {}).get("position_valid")

        report.step("GET_STATUS", True, "{} commands".format(
            len(status.get("commands") or [])))
        report.note(
            "sensor={}  servo={}  carousel position valid={}".format(
                sensor,
                "connected" if servo else "not connected",
                carousel,
            )
        )

        return True

    finally:
        link.close()

    return False


def check_port_released(port, report):
    """
    The port is free the instant the tool lets go of it.

    Run immediately after close, because "it works if you wait" is how
    a leaked handle hides.

    Deliberately NOT done with mpremote. Every mpremote command
    interrupts whatever the board is running and leaves it at the
    MicroPython REPL - so using it here would take a board that had
    just been proved to be serving the protocol and stop it serving,
    one line after reporting success. Opening the port with pySerial
    and closing it again tests exactly the thing being claimed - that
    another program can have the port - and disturbs nothing.
    """
    sys.path.insert(0, str(PC_DIR))

    try:
        from serial_link import LinkError, SerialLink

    except ImportError as error:                       # pragma: no cover
        return report.step("PORT RELEASE", False, str(error))

    probe = SerialLink(port)

    try:
        probe.open()

    except LinkError as error:
        return report.step(
            "PORT RELEASE", False,
            "{}: {}".format(error.code, error.message),
        )

    probe.close()

    return report.step("PORT RELEASE", True, port)


# ======================================================================
# commands
# ======================================================================

def command_status(args):
    """What is on the device right now, and how it compares to the manifest."""
    report = Report()

    if not check_port(args.port, report):
        return 1

    entries = device_listing(args.port)

    print()
    print("device filesystem ({} entries):".format(len(entries)))

    for name, is_directory in entries:
        marker = "/" if is_directory else " "
        known = "manifest" if name in ESP32_FILES else "STALE"

        print("   {:<20}{}  {}".format(name + marker, "", known))

    print()
    built = build_firmware(report)

    if built is None:
        return 1

    check_remote_manifest(args.port, report, strict=False)
    check_content(args.port, report, built)

    return 1 if report.failed else 0


def command_clean(args):
    report = Report()

    if not check_port(args.port, report):
        return 1

    clean_device(args.port, report)

    return 1 if report.failed else 0


RECEIPT_PATH = (
    REPO_ROOT / "firmware" / "Tests" / "hardware" / "artifacts"
    / "deployment-receipt.json"
)


def write_receipt(port, built):
    """
    Record the exact bytes this deployment put on the device.

    WHY A RECEIPT AT ALL.

    `verify` proves the device matches the source by REBUILDING and
    comparing SHA256. That is sound from the checkout that deployed,
    and only from there: mpy-cross embeds the source path, so the same
    source compiled from a second copy of the repository - or with
    different line endings - produces different bytes and `verify`
    reports a mismatch about the path rather than about the firmware.
    Measured 2026-08-27: identical content under two directory names
    gave two different SHA256.

    The receipt removes the recompilation from the question. It is
    written after the device has already been proved to match the
    build, so what it records is not a claim about what was intended -
    it is what was verified to be there.

    Best effort by design: a receipt that cannot be written must not
    fail a deployment that has otherwise succeeded and been verified.
    """
    payload = {
        "deployed_utc": _utc_now(),
        "port": str(port),
        "source_dir": str(ESP32_DIR),
        "mpy_cross": _mpy_cross_version(),
        "files": {
            name: local_sha256(built[name]) for name in ESP32_FILES
        },
    }

    try:
        RECEIPT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RECEIPT_PATH.write_text(
            json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")

        return payload

    except OSError as error:                           # pragma: no cover
        print("(receipt not written to {}: {})".format(
            RECEIPT_PATH, error))

        return payload


def read_receipt():
    """The last deployment receipt, or None. Never raises."""
    try:
        return json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))

    except (OSError, ValueError):
        return None


def check_against_receipt(port, report):
    """
    Compare the device against the RECORDED deployment, not a rebuild.

    Path-independent, so it answers the one question a rebuild cannot:
    are these the bytes that were actually put there? Reports nothing
    when there is no receipt - a device deployed before receipts
    existed is not a fault.
    """
    receipt = read_receipt()

    if not receipt or not receipt.get("files"):
        return None

    mismatched = []
    unverifiable = []

    for name, expected in sorted(receipt["files"].items()):
        remote = device_sha256(port, name)

        if remote is None:
            unverifiable.append(name)

        elif remote != expected:
            mismatched.append(name)

    if mismatched:
        report.step(
            "RECEIPT", False,
            "device differs from the {} deployment: {}".format(
                receipt.get("deployed_utc", "recorded"),
                ", ".join(mismatched)))

        return False

    report.step(
        "RECEIPT", True,
        "matches the deployment recorded at {}".format(
            receipt.get("deployed_utc", "an unknown time")))

    return True


def _utc_now():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _mpy_cross_version():
    try:
        result = subprocess.run(
            [sys.executable, "-m", "mpy_cross", "--version"],
            capture_output=True, text=True, timeout=30)

        text = (result.stdout or result.stderr or "").strip()

        return text.splitlines()[0] if text else None

    except Exception:                                  # noqa: BLE001
        return None


def command_deploy(args):
    """
    The full acceptance pipeline.

    Nothing prints "deployed" until the post-reset ping has succeeded.
    """
    report = Report()

    print("Freya ESP32 deployment")
    print("source: {}".format(ESP32_DIR))
    print("port:   {}".format(args.port))
    print()

    if not check_manifest_sources(report):
        return 1

    built = build_firmware(report)

    if built is None:
        return 1

    if not check_port(args.port, report):
        return 1

    # FROM HERE UNTIL UPLOAD FINISHES THE DEVICE IS INCOMPLETE.
    #
    # Clean has to precede upload - that is the whole point of it - so
    # there is a window in which the device has had its firmware
    # removed and has not yet been given the new one. A Ctrl+C in that
    # window leaves a board that boots into the REPL and answers
    # nothing, which looks exactly like a dead board and is not one.
    #
    # It cannot be prevented. It CAN be said out loud, which is the
    # difference between a two-minute fix and an afternoon.
    try:
        if args.clean:
            if not clean_device(args.port, report):
                return 1

        if not upload(args.port, report, built):
            return 1

    except KeyboardInterrupt:
        print()
        print("INTERRUPTED PART-WAY THROUGH THE DEPLOYMENT.")
        print()
        print("The device filesystem is INCOMPLETE - some manifest files")
        print("are missing, so it will boot into the MicroPython REPL and")
        print("answer nothing. That is not a fault; it is half a")
        print("deployment.")
        print()
        print("Run the same command again to finish:")
        print()
        print("    {} {} deploy --port {} --clean".format(
            Path(sys.executable).name,
            Path(__file__).resolve().relative_to(REPO_ROOT).as_posix(),
            args.port,
        ))

        return 130

    if not check_remote_manifest(args.port, report, strict=args.clean):
        return 1

    if not check_content(args.port, report, built):
        return 1

    if not check_imports(args.port, report):
        return 1

    if not reset_and_ping(args.port, report):
        return 1

    check_port_released(args.port, report)

    print()

    if report.failed:
        print("DEPLOYMENT FAILED")

        return 1

    receipt = write_receipt(args.port, built)

    print("Deployed and verified. The board is serving the JSON protocol.")
    print()
    print("Receipt: {}".format(RECEIPT_PATH))
    print("  {} files, recorded at {}".format(
        len(receipt["files"]), receipt["deployed_utc"]))
    print("  `verify` compares the device against these hashes, so it")
    print("  no longer depends on rebuilding from this exact path.")

    return 0


def command_verify(args):
    """Check a device that is already deployed, without changing it."""
    report = Report()

    if not check_manifest_sources(report):
        return 1

    if not check_port(args.port, report):
        return 1

    built = build_firmware(report)

    if built is None:
        return 1

    check_remote_manifest(args.port, report, strict=True)
    check_content(args.port, report, built)

    # PATH-INDEPENDENT, AND THEREFORE THE ANSWER WHEN THE REBUILD
    # DISAGREES. `check_content` compares against a fresh compile, which
    # is only reproducible from the checkout that deployed. This
    # compares against what the deployment actually verified onto the
    # device. Silent when no receipt exists.
    check_against_receipt(args.port, report)

    check_imports(args.port, report)

    return 1 if report.failed else 0


def command_reset(args):
    """Reset the board and confirm it comes back serving."""
    report = Report()

    if not check_port(args.port, report):
        return 1

    if not reset_and_ping(args.port, report):
        return 1

    check_port_released(args.port, report)

    return 1 if report.failed else 0


# USB-serial device nodes, per platform. Listed by GLOB rather than by
# pyserial's list_ports, deliberately: PC/serial_link.py is the one
# module in this project allowed to import pyserial, and a deployment
# tool that never opens a port has no business becoming the second. A
# device node is a file, and looking for a file needs no serial library.
POSIX_PORT_GLOBS = (
    "ttyUSB*",        # Linux, CP210x / FT232 style bridges - this board
    "ttyACM*",        # Linux, native-USB boards
    "cu.usbserial*",  # macOS, the same bridges
    "cu.usbmodem*",   # macOS, native USB
)


def attached_ports():
    """Every USB-serial device node present, sorted. POSIX only."""
    if sys.platform.startswith("win"):
        return []

    dev = Path("/dev")

    if not dev.is_dir():                               # pragma: no cover
        return []

    found = []

    for pattern in POSIX_PORT_GLOBS:
        # as_posix(), not str(): identical on the Linux machine this
        # runs on, and it keeps the function meaningful when it is
        # exercised from the Windows bench, where str() on a Path would
        # produce `\dev\ttyUSB0` and hide whether the logic is right.
        found.extend(path.as_posix() for path in dev.glob(pattern))

    return sorted(set(found))


def default_port():
    """
    What --port means when it is not given.

    COM4 is right on the Windows bench machine and meaningless on the
    Linux main computer, where the same board is /dev/ttyUSB0 or
    /dev/ttyACM0 and the number moves between plug-ins. So there is no
    POSIX literal to default to: the device nodes are looked up, and if
    the answer is not exactly one there is no default at all - guessing
    which of several attached boards to reflash is not a thing a
    deployment tool should do.
    """
    if sys.platform.startswith("win"):
        return "COM4"

    candidates = attached_ports()

    return candidates[0] if len(candidates) == 1 else None


def build_parser():
    parser = argparse.ArgumentParser(
        prog="device.py",
        description="Deploy and verify the Freya ESP32 firmware.",
    )
    parser.add_argument(
        "command",
        choices=("status", "clean", "deploy", "verify", "reset"),
    )
    parser.add_argument("--port", default=None,
                        help="serial port of the ESP32 (default: COM4 on "
                             "Windows, the single attached USB-serial "
                             "device on Linux and macOS)")
    parser.add_argument(
        "--clean", action="store_true",
        help="remove every user file from the device before uploading",
    )

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.port is None:
        args.port = default_port()

    if args.port is None:
        print("No serial port given and none could be chosen for you.")
        print()

        ports = attached_ports()

        if ports:
            print("USB-serial devices attached right now:")

            for line in ports:
                print("    {}".format(line))

            print()
            print("More than one, so the tool will not pick for you.")

        else:
            print("No USB-serial device node is present - the board is "
                  "not plugged in, or its USB bridge has not "
                  "enumerated.")

        print()
        print("Name one with --port, for example:")
        print("    {} {} {} --port /dev/ttyUSB0".format(
            Path(sys.executable).name,
            Path(__file__).name,
            args.command,
        ))

        return 2

    handlers = {
        "status": command_status,
        "clean": command_clean,
        "deploy": command_deploy,
        "verify": command_verify,
        "reset": command_reset,
    }

    try:
        return handlers[args.command](args)

    except DeviceError as error:
        print("FAILED: {}".format(error))

        return 1

    except KeyboardInterrupt:
        print("\ninterrupted")

        return 130


if __name__ == "__main__":
    sys.exit(main())
