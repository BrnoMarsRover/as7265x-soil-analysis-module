"""
The ESP32 device tool: status, clean, deploy, verify, reset.

ONE command deploys the firmware:

    py firmware\\tools\\device.py deploy --port COM4 --clean

and it is not allowed to report success until the board has been reset,
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
import subprocess
import sys
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

ESP32_FILES = (
    "config.py",
    "sensor.py",
    "servo.py",
    "carousel.py",
    "protocol.py",
    "main.py",
    "boot.py",
)

# Modules the import check exercises, in dependency order. boot.py is
# excluded because importing it does nothing by design, and main.py
# because importing it would start the serving loop - which is exactly
# what the reset-and-ping step tests properly.
IMPORT_CHECK = ("config", "sensor", "servo", "carousel", "protocol")


# ======================================================================
# mpremote
# ======================================================================

MPREMOTE = [sys.executable, "-m", "mpremote"]


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

def check_manifest_sources(report):
    """Every manifest file exists locally, and nothing stray sits beside it."""
    missing = [n for n in ESP32_FILES if not (ESP32_DIR / n).is_file()]

    if missing:
        return report.step(
            "SOURCES", False,
            "missing from {}: {}".format(ESP32_DIR, ", ".join(missing)),
        )

    on_disk = {p.name for p in ESP32_DIR.glob("*.py")}
    extra = sorted(on_disk - set(ESP32_FILES))

    if extra:
        # Not fatal - it just will not be uploaded - but silence here
        # is how a file everybody assumes is running turns out not to
        # be on the device at all.
        report.step("SOURCES", True, "{} files".format(len(ESP32_FILES)))
        report.note(
            "not in the manifest, so NOT uploaded: " + ", ".join(extra)
        )

        return True

    return report.step("SOURCES", True, "{} files".format(len(ESP32_FILES)))


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


def upload(port, report):
    """Copy every manifest file to the device root."""
    for name in ESP32_FILES:
        source = ESP32_DIR / name

        result = run_mpremote(
            port, "fs", "cp", "-f", str(source), ":" + name,
            check=False, timeout=180,
        )

        if result.returncode != 0:
            return report.step(
                "UPLOAD", False,
                "{}: {}".format(
                    name, (result.stderr or result.stdout).strip()[-200:]
                ),
            )

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


def check_content(port, report):
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

        if remote != local_sha256(ESP32_DIR / name):
            mismatched.append(name)

    if mismatched:
        return report.step(
            "CONTENT", False, "hash mismatch: " + ", ".join(mismatched)
        )

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
    check_remote_manifest(args.port, report, strict=False)
    check_content(args.port, report)

    return 1 if report.failed else 0


def command_clean(args):
    report = Report()

    if not check_port(args.port, report):
        return 1

    clean_device(args.port, report)

    return 1 if report.failed else 0


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

        if not upload(args.port, report):
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
        print("    py firmware\\tools\\device.py deploy --port {} --clean"
              .format(args.port))

        return 130

    if not check_remote_manifest(args.port, report, strict=args.clean):
        return 1

    if not check_content(args.port, report):
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

    print("Deployed and verified. The board is serving the JSON protocol.")

    return 0


def command_verify(args):
    """Check a device that is already deployed, without changing it."""
    report = Report()

    if not check_manifest_sources(report):
        return 1

    if not check_port(args.port, report):
        return 1

    check_remote_manifest(args.port, report, strict=True)
    check_content(args.port, report)
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


def build_parser():
    parser = argparse.ArgumentParser(
        prog="device.py",
        description="Deploy and verify the Freya ESP32 firmware.",
    )
    parser.add_argument(
        "command",
        choices=("status", "clean", "deploy", "verify", "reset"),
    )
    parser.add_argument("--port", default="COM4",
                        help="serial port of the ESP32 (default COM4)")
    parser.add_argument(
        "--clean", action="store_true",
        help="remove every user file from the device before uploading",
    )

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

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
