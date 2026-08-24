"""
Asking a program what it does must not make it do it.

THE DEFECT THIS SUITE WAS BUILT AROUND

`analyse_discriminability.py` had no argument parser. It read
`sys.argv` never, and its `main()` went straight to work. So:

    py firmware/research/analyse_discriminability.py --help

ignored the flag, recomputed the whole leave-one-out analysis, and
REWROTE `firmware/BD/DB3/DB3.json` - a protected reference library.
The file happened to come out identical apart from its timestamp, so
the damage was invisible; with a different DB3 it would not have been.

That is not a documentation problem. `--help` is what an engineer types
when they do not know what a program does, which makes it exactly the
command that must be safe.

WHAT IS CHECKED

    1. every executable file has an `if __name__ == "__main__"` guard,
       so importing it does nothing
    2. every entry point that WRITES anything parses its arguments, so
       an unknown flag is refused rather than ignored
    3. `--help` on every entry point exits cleanly and changes no file
       anywhere in firmware/
    4. importing every host module changes no file either
"""

import ast
import hashlib
import subprocess
import sys
from pathlib import Path

_TESTS_DIR = next(
    p for p in Path(__file__).resolve().parents if p.name == "Tests"
)

if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

import support

checks = support.Checks("entrypoints")

FIRMWARE = support.FIRMWARE
REPO = FIRMWARE.parent


def relative(path):
    return path.relative_to(FIRMWARE).as_posix()


def every_source():
    for path in sorted(FIRMWARE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue

        yield path


# ======================================================================
# what the whole tree looks like, byte for byte
# ======================================================================

# Everything except the caches. Wider than "the protected databases" on
# purpose: an entry point that writes a stray file into research/data/
# is also a finding, just a smaller one.
def snapshot():
    digest = {}

    for path in sorted(FIRMWARE.rglob("*")):
        if not path.is_file():
            continue

        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue

        digest[relative(path)] = hashlib.sha256(
            path.read_bytes()).hexdigest()

    return digest


def compare(before, after):
    changed = sorted(
        name for name in set(before) | set(after)
        if before.get(name) != after.get(name)
    )

    return changed


BEFORE = snapshot()


# ======================================================================
checks.section("importing a file never runs it")

# A module whose top level does work cannot be imported by a test, a
# tool or another module without that work happening.

unguarded = []
entry_points = []

for path in every_source():
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))

    has_main_guard = False
    top_level_calls = []

    for node in tree.body:
        if isinstance(node, ast.If):
            test = node.test

            if (isinstance(test, ast.Compare)
                    and isinstance(test.left, ast.Name)
                    and test.left.id == "__name__"):
                has_main_guard = True

        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value.func
            name = getattr(call, "id", None) or getattr(call, "attr", None)

            # Path and sys.path setup at import time is the intended
            # pattern in this project and is not "doing work".
            if name not in ("insert", "append", "add_project_root",
                            "add_path", "patch_time"):
                top_level_calls.append("{}:{}: {}()".format(
                    relative(path), node.lineno, name))

    if has_main_guard:
        entry_points.append(path)

    # Tests/ suites ARE top-level programs by design - that is how
    # run_software.py runs them - so they are exempt from this rule.
    if path.relative_to(FIRMWARE).parts[0] == "Tests":
        continue

    unguarded.extend(top_level_calls)

checks.equal(sorted(unguarded), [],
             "no production module does work at import time")
checks.ok(len(entry_points) > 8,
          "and the tree really has entry points to check ({})".format(
              len(entry_points)))


# ======================================================================
checks.section("an entry point that writes, parses its arguments")

# The precise shape of the DB3 defect: work performed, arguments never
# read. A program that only prints is allowed to ignore argv; a program
# that WRITES must refuse a flag it does not understand.

WRITE_CALLS = {"write_text", "write_bytes", "dump", "mkdir", "replace",
               "unlink", "rmtree", "copy2", "copyfile"}

writes_without_parser = []

for path in entry_points:
    if path.relative_to(FIRMWARE).parts[0] == "Tests":
        continue

    text = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(text, filename=str(path))

    writes = False

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None)

            if name in WRITE_CALLS:
                writes = True

            elif name == "open":
                writes = True

            elif (isinstance(node.func, ast.Name)
                  and node.func.id == "open"
                  and len(node.args) > 1):
                writes = True

    parses = "argparse" in text or "parse_args" in text

    if writes and not parses:
        writes_without_parser.append(relative(path))

checks.equal(sorted(writes_without_parser), [],
             "every entry point that can write a file also parses its "
             "arguments, so --help cannot be mistaken for 'go'")


# ======================================================================
checks.section("--help acts on nothing")

# DERIVED, not listed. A hand-written list is a list that stops
# including new programs the day somebody adds one, which is the same
# failure mode as not having the test. The entry points are whatever
# has a `__main__` guard.
#
# ESP32/main.py is excluded and is the only exclusion: it is the
# firmware's serving loop, it imports `machine`, and it is exercised
# properly by integration/test_esp32.py.
SKIP_HELP = {"ESP32/main.py"}

HELP_TARGETS = [
    relative(path) for path in entry_points
    if relative(path) not in SKIP_HELP
    and not relative(path).startswith("Tests/software/")
]

helped = 0

for name in HELP_TARGETS:
    path = FIRMWARE / name

    result = subprocess.run(
        [sys.executable, str(path), "--help"],
        capture_output=True, text=True, cwd=str(REPO), timeout=120,
    )

    output = (result.stdout or "") + (result.stderr or "")

    checks.ok(
        result.returncode in (0, 2) and (
            "usage" in output.lower() or "software tests only" in output),
        "{} --help prints usage and exits (rc={})".format(
            name, result.returncode),
    )

    helped += 1

checks.ok(helped == len(HELP_TARGETS),
          "every entry point was asked ({})".format(helped))

after_help = snapshot()
changed_by_help = compare(BEFORE, after_help)

checks.equal(changed_by_help, [],
             "and asking every one of them for help changed no file "
             "anywhere under firmware/ - this is the DB3 regression")


# ======================================================================
checks.section("importing the host tree acts on nothing")

support.add_project_root()
support.add_path("PC")
support.add_path("tools")

import importlib                                            # noqa: E402

imported = 0

for path in every_source():
    parts = list(path.relative_to(FIRMWARE).with_suffix("").parts)

    if parts[0] in ("ESP32", "Tests"):
        continue

    if parts[-1] == "__init__":
        parts = parts[:-1]

    dotted = ".".join(parts[1:] if parts[0] in ("PC", "tools") else parts)

    if not dotted:
        continue

    try:
        importlib.import_module(dotted)
        imported += 1

    except BaseException:                              # noqa: BLE001
        # Reported by static/test_static_api.py; not this suite's job.
        pass

after_import = snapshot()
changed_by_import = compare(BEFORE, after_import)

checks.equal(changed_by_import, [],
             "importing all {} host modules changed no file".format(
                 imported))


# ======================================================================
checks.section("run_all.py cannot reach the hardware campaign")

# The one test that keeps a reflex-run suite from turning a carousel
# that may be holding samples.

runner = FIRMWARE / "Tests" / "run_all.py"
runner_text = runner.read_text(encoding="utf-8")

checks.ok("hardware" in runner_text and "run_hardware" in runner_text,
          "run_all.py knows the hardware campaign exists")

for flag in ("--hardware", "--with-hardware", "hardware"):
    result = subprocess.run(
        [sys.executable, str(runner), flag],
        capture_output=True, text=True, cwd=str(REPO), timeout=120,
    )

    checks.ok(result.returncode == 2 and "SOFTWARE tests only" in result.stdout,
              "run_all.py {} refuses instead of quietly running the "
              "software suite".format(flag))

hardware_runner = FIRMWARE / "Tests" / "hardware" / "run_hardware.py"
result = subprocess.run(
    [sys.executable, str(hardware_runner)],
    capture_output=True, text=True, cwd=str(REPO), timeout=120,
)

checks.ok(result.returncode != 0
          and "--port" in (result.stderr + result.stdout),
          "run_hardware.py refuses to start without an explicit --port, "
          "so there is no port to open by accident")

software_runner = FIRMWARE / "Tests" / "software" / "run_software.py"
software_text = software_runner.read_text(encoding="utf-8")

checks.ok("hardware" not in [
    line.split("/")[0].strip(' ("')
    for line in software_text.splitlines() if line.strip().startswith('("')
], "the software runner's suite list contains no hardware suite")


sys.exit(checks.report())
