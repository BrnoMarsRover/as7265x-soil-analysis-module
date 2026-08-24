"""
The main computer is a Linux machine. This suite assumes nothing else.

WHY IT IS ITS OWN GROUP

Development happens on Windows and the module is operated from Linux.
Everything platform-shaped therefore works on the machine where it is
written and is untested on the machine where it matters, which is the
worst possible arrangement. Two defects of exactly that shape have
already reached the operator:

    pySerial's open() failures were classified by their WINDOWS
    exception text only, so on Linux a missing /dev/ttyUSB0 and an
    account outside the `dialout` group both arrived as the useless
    PORT_OPEN_FAILED

    every operator-facing "run this command" message named `py`, a
    launcher that does not exist on Linux

WHAT IS CHECKED

    error classification for every POSIX errno pySerial can produce
    device-node discovery, with none, one and several attached
    case-exact paths, because Linux cares and Windows does not
    working-directory independence for every entry point
    that nothing printed to an operator names a Windows-only tool

WHAT CANNOT BE CHECKED HERE

Whether the account is in the serial group, and whether the port really
opens. Those need the hardware and belong to Tests/hardware.
"""

import os
import subprocess
import sys
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
support.add_path("tools")

import serial_link                                          # noqa: E402
from serial_link import LinkError, SerialLink                # noqa: E402

checks = support.Checks("linux")

FIRMWARE = support.FIRMWARE
REPO = FIRMWARE.parent


# ======================================================================
checks.section("every POSIX open() failure is classified")

# The exact strings `serialposix.py` builds. It formats the underlying
# OSError into the message, so the errno text is what has to be
# recognized - there is no errno attribute to read.

POSIX_FAILURES = (
    ("[Errno 2] could not open port /dev/ttyUSB0: [Errno 2] No such file "
     "or directory: '/dev/ttyUSB0'",
     "PORT_NOT_FOUND", "the board is not plugged in"),

    ("[Errno 13] could not open port /dev/ttyUSB0: [Errno 13] Permission "
     "denied: '/dev/ttyUSB0'",
     "PORT_DENIED", "the account is not in the serial group"),

    ("Could not exclusively lock port /dev/ttyUSB0: [Errno 11] Resource "
     "temporarily unavailable",
     "PORT_BUSY", "another process holds the flock"),

    ("[Errno 16] could not open port /dev/ttyUSB0: [Errno 16] Device or "
     "resource busy: '/dev/ttyUSB0'",
     "PORT_BUSY", "the device is busy"),

    ("[Errno 2] could not open port /dev/ttyACM0: [Errno 2] No such file "
     "or directory: '/dev/ttyACM0'",
     "PORT_NOT_FOUND", "a native-USB board that is not there"),
)

for text, expected, why in POSIX_FAILURES:
    error = serial_link._classify_open_failure("/dev/ttyUSB0", Exception(text))

    checks.equal(error.code, expected,
                 "{} -> {}".format(why, expected))
    checks.ok(error.data.get("detail") == text,
              "and the raw text is kept for the ones we did not "
              "anticipate ({})".format(expected))

# The three codes must stay distinct, because they need three different
# actions: plug it in, close the other program, join the group.
codes = {
    serial_link._classify_open_failure("/dev/ttyUSB0", Exception(text)).code
    for text, _expected, _why in POSIX_FAILURES
}

checks.equal(sorted(codes), ["PORT_BUSY", "PORT_DENIED", "PORT_NOT_FOUND"],
             "and they remain three distinct diagnoses, not one")

denied = serial_link._classify_open_failure(
    "/dev/ttyUSB0",
    Exception("[Errno 13] could not open port /dev/ttyUSB0: [Errno 13] "
              "Permission denied: '/dev/ttyUSB0'"))

checks.ok("dialout" in denied.message,
          "PORT_DENIED names the group to join")
checks.ok("usermod" in denied.message,
          "and gives the command that joins it")
checks.ok("log out" in denied.message.lower(),
          "and says the change needs a new login, which is the part "
          "everybody forgets")
checks.ok("another program" not in denied.message,
          "and does NOT send the operator hunting for a process - "
          "nothing is holding the port")


# ======================================================================
checks.section("Windows failures keep their own meaning")

# The same classifier serves both machines, so the Windows cases have
# to keep working. On Windows a denial IS another program holding the
# handle, which is the opposite conclusion from the same word.

WINDOWS_FAILURES = (
    ("could not open port 'COM4': FileNotFoundError(2, 'The system "
     "cannot find the file specified.', None, 2)", "PORT_NOT_FOUND"),
    ("could not open port 'COM4': PermissionError(13, 'Access is "
     "denied.', None, 5)", "PORT_BUSY"),
    ("could not open port 'COM4': PermissionError(13, 'Zugriff "
     "verweigert', None, 5)", "PORT_BUSY"),
)

for text, expected in WINDOWS_FAILURES:
    error = serial_link._classify_open_failure("COM4", Exception(text))
    checks.equal(error.code, expected,
                 "a Windows failure is still {}".format(expected))

unknown = serial_link._classify_open_failure(
    "/dev/ttyUSB0", Exception("something nobody has seen before"))

checks.equal(unknown.code, "PORT_OPEN_FAILED",
             "and anything unrecognized is still named as unrecognized "
             "rather than guessed at")


# ======================================================================
checks.section("device-node discovery")

import device                                                # noqa: E402

real_platform = sys.platform


class FakeDev:
    """A /dev directory with a chosen set of nodes in it."""

    def __init__(self, names):
        self.names = list(names)

    def is_dir(self):
        return True

    def glob(self, pattern):
        import fnmatch

        return [Path("/dev") / name for name in self.names
                if fnmatch.fnmatch(name, pattern)]


def with_dev(names, platform="linux"):
    """Run attached_ports()/default_port() against a fake /dev."""
    original_path = device.Path
    original_platform = sys.platform

    class PatchedPath(type(Path())):
        pass

    def fake_path(value):
        if str(value) == "/dev":
            return FakeDev(names)

        return original_path(value)

    device.Path = fake_path
    sys.platform = platform

    try:
        return device.attached_ports(), device.default_port()

    finally:
        device.Path = original_path
        sys.platform = original_platform


nodes, chosen = with_dev([])
checks.equal(nodes, [], "with nothing plugged in, no device is found")
checks.equal(chosen, None,
             "and there is no default port - a deployment tool that "
             "guessed here would reflash something unknown")

nodes, chosen = with_dev(["ttyUSB0"])
checks.equal(nodes, ["/dev/ttyUSB0"], "one CP210x bridge is found")
checks.equal(chosen, "/dev/ttyUSB0",
             "and it becomes the default, so --port can be omitted")

nodes, chosen = with_dev(["ttyACM0"])
checks.equal(chosen, "/dev/ttyACM0",
             "a native-USB board is found the same way")

nodes, chosen = with_dev(["ttyUSB0", "ttyUSB1"])
checks.equal(sorted(nodes), ["/dev/ttyUSB0", "/dev/ttyUSB1"],
             "two devices are both reported")
checks.equal(chosen, None,
             "and NEITHER is chosen - picking one of two boards to "
             "reflash is not a decision a tool should make silently")

nodes, chosen = with_dev(["ttyS0", "random", "null"])
checks.equal(nodes, [],
             "the built-in serial ports and ordinary device nodes are "
             "not mistaken for the board")

nodes, chosen = with_dev(["cu.usbserial-0001"], platform="darwin")
checks.equal(chosen, "/dev/cu.usbserial-0001",
             "macOS names its bridges differently and is handled too")

nodes, chosen = with_dev(["ttyUSB0"], platform="win32")
checks.equal(nodes, [],
             "on Windows nothing is globbed - /dev does not exist")
checks.equal(chosen, "COM4",
             "and the bench default is COM4, unchanged")


# ======================================================================
checks.section("the port is never guessed into a command")

result = subprocess.run(
    [sys.executable, str(FIRMWARE / "Tests" / "hardware" /
                         "run_hardware.py")],
    capture_output=True, text=True, cwd=str(REPO), timeout=120,
)

checks.ok(result.returncode != 0,
          "the hardware runner refuses to start without --port")
checks.ok("--port" in result.stderr + result.stdout,
          "and says which argument is missing")


# ======================================================================
checks.section("paths are case-exact, because Linux cares")

# Windows will open BD/db1/DB1.JSON when the directory is BD/DB1. Linux
# will not. Every path the software builds is checked against the
# spelling on disk.

def spelling_of(path):
    parts = []
    current = Path(path)

    while current != current.parent:
        parts.append(current.name)
        current = current.parent

    walk = current

    for name in reversed(parts):
        if not walk.is_dir():
            return None

        entries = {entry.name for entry in walk.iterdir()}

        if name not in entries:
            lowered = {entry.lower(): entry for entry in entries}

            return lowered.get(name.lower())

        walk = walk / name

    return True


import importlib                                             # noqa: E402

faults = []
checked = 0

for dotted in ("BD.config", "Science.config", "research.erc.config"):
    module = importlib.import_module(dotted)

    for name in dir(module):
        value = getattr(module, name)

        if name.startswith("_") or not isinstance(value, Path):
            continue

        checked += 1
        spelling = spelling_of(value)

        if isinstance(spelling, str):
            faults.append("{}.{}: {} vs {!r}".format(
                dotted, name, value, spelling))

checks.equal(faults, [],
             "{} path constants, every one spelled the way the disk "
             "spells it".format(checked))

# The four domain directories, by exact name.
for name in ("BD", "ESP32", "PC", "Science", "Tests", "research", "tools"):
    entries = {entry.name for entry in FIRMWARE.iterdir()}

    checks.ok(name in entries,
              "firmware/{} is spelled exactly that way on disk".format(
                  name))


# ======================================================================
checks.section("no import differs from its file only by case")

# `from Science import config` and a directory called `science` work on
# Windows and are an ImportError on Linux.

import ast                                                   # noqa: E402

on_disk = set()

for path in FIRMWARE.rglob("*.py"):
    if "__pycache__" in path.parts:
        continue

    parts = list(path.relative_to(FIRMWARE).with_suffix("").parts)

    if parts[-1] == "__init__":
        parts = parts[:-1]

    if parts:
        on_disk.add(".".join(parts))
        on_disk.add(parts[0])

lowered = {name.lower(): name for name in on_disk}

case_mismatches = []

for path in FIRMWARE.rglob("*.py"):
    if "__pycache__" in path.parts:
        continue

    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))

    for node in ast.walk(tree):
        names = []

        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]

        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]

        for name in names:
            root = name.split(".")[0]

            if root in on_disk:
                continue

            if root.lower() in lowered:
                case_mismatches.append("{}:{}: imports {!r}, on disk it "
                                       "is {!r}".format(
                                           path.relative_to(FIRMWARE)
                                           .as_posix(), node.lineno, root,
                                           lowered[root.lower()]))

checks.equal(sorted(set(case_mismatches)), [],
             "no import names a module whose file differs only in case")


# ======================================================================
checks.section("entry points do not depend on the working directory")

# Every path the programs use is resolved from __file__, so running
# them from anywhere must give the same answer. Checked by running them
# from three genuinely different places.

import tempfile                                              # noqa: E402

ELSEWHERE = Path(tempfile.mkdtemp(prefix="freya-cwd-"))

WORKING_DIRECTORIES = (
    ("the repository root", REPO),
    ("firmware/", FIRMWARE),
    ("a directory outside the repository", ELSEWHERE),
)

TARGETS = (
    "tools/device.py",
    "PC/rover_science_client.py",
    "research/build_db1.py",
)

for label, cwd in WORKING_DIRECTORIES:
    for target in TARGETS:
        result = subprocess.run(
            [sys.executable, str(FIRMWARE / target), "--help"],
            capture_output=True, text=True, cwd=str(cwd), timeout=120,
        )

        checks.ok(result.returncode == 0 and "usage" in
                  (result.stdout + result.stderr).lower(),
                  "{} runs from {}".format(target, label))

# The heavier claim: a program that READS data finds it from anywhere.
for label, cwd in WORKING_DIRECTORIES:
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, r'{}'); "
         "from BD.databases import References; "
         "print(len(References().dark))".format(FIRMWARE)],
        capture_output=True, text=True, cwd=str(cwd), timeout=120,
    )

    checks.ok(result.returncode == 0 and result.stdout.strip().isdigit(),
              "BD finds its own data from {} ({})".format(
                  label, (result.stdout + result.stderr).strip()[:60]))


# ======================================================================
checks.section("nothing an operator is told to type is Windows-only")

# `py` is the Windows launcher. A message that tells a Linux operator
# to run `py something.py` is advice that cannot be followed.

OPERATOR_FACING = (
    FIRMWARE / "PC" / "serial_link.py",
    FIRMWARE / "PC" / "rover_science_client.py",
    FIRMWARE / "tools" / "device.py",
)

offenders = []

for path in OPERATOR_FACING:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue

        if not isinstance(node.value, str):
            continue

        text = node.value

        # Only the launcher invocation, not the word "py" anywhere.
        for marker in ("py -m pip", "py firmware", "py -c"):
            if marker in text:
                offenders.append("{}:{}: {!r}".format(
                    path.relative_to(FIRMWARE).as_posix(), node.lineno,
                    text[:60]))

checks.equal(sorted(set(offenders)), [],
             "no operator-facing string tells a Linux machine to run "
             "`py`")

# And the messages that DO name an interpreter name the running one.
source = (FIRMWARE / "PC" / "serial_link.py").read_text(encoding="utf-8")

checks.ok("sys.executable" in source,
          "the pyserial install hint names the interpreter that is "
          "actually running, so it is correct on both machines")

device_source = (FIRMWARE / "tools" / "device.py").read_text(
    encoding="utf-8")

checks.ok("sys.executable" in device_source,
          "and so does the deployment tool")


# ======================================================================
checks.section("no path is built by pasting separators together")

# `"BD" + "\\" + name` works on Windows and produces a filename with a
# backslash IN IT on Linux.

pasted = []

for path in FIRMWARE.rglob("*.py"):
    if "__pycache__" in path.parts:
        continue

    if path.relative_to(FIRMWARE).parts[0] == "Tests":
        continue

    for number, line in enumerate(
            path.read_text(encoding="utf-8-sig").split("\n"), 1):
        stripped = line.strip()

        if stripped.startswith("#"):
            continue

        # A literal backslash inside a string that also looks like a
        # path fragment.
        if '"\\\\"' in stripped or "'\\\\'" in stripped:
            if "replace" not in stripped:
                pasted.append("{}:{}".format(
                    path.relative_to(FIRMWARE).as_posix(), number))

checks.equal(pasted, [],
             "no module joins path segments with a literal backslash - "
             "the ones that appear are all `.replace(chr(92), '/')` "
             "normalizing output for display")


sys.exit(checks.report())
