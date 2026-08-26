"""
Every production module, classified, with no file left unknown.

WHY THIS EXISTS

Phase A.3 asks a question the coverage report cannot answer: is there a
production file whose ROLE nobody has decided? A file at 0% coverage is
either offline tooling that must never run during a mission, or a
forgotten mission path. Those two need opposite responses, and a
percentage cannot tell them apart.

So the role is declared here, by hand, once per file, and the tool
fails if a file exists that is not in the table - which is what makes
a new module impossible to add silently.

THE FIVE ROLES

    MISSION_RUNTIME    reachable from rover_science_client.py during a
                       competition run
    MISSION_SUPPORT    imported by mission runtime, but only on paths
                       an operator reaches outside a measurement
    HARDWARE_DRIVER    runs on the ESP32, against real silicon
    OFFLINE            analysis, research and deployment tooling; must
                       never be imported by the mission path
    TEST_ONLY          the harness itself

Run it:

    py firmware/Tests/software/audit/module_inventory.py
"""

import ast
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# BY NAME, NOT BY HOP COUNT. Every suite in this tree resolves
# firmware/ by walking up to the directory called it, because a file
# that moves one level changes a hop count silently and changes nothing
# about a name. The regression suite enforces it.
FIRMWARE = next(
    p for p in Path(__file__).resolve().parents if p.name == "firmware"
)

MISSION_RUNTIME = "MISSION_RUNTIME"
MISSION_SUPPORT = "MISSION_SUPPORT"
HARDWARE_DRIVER = "HARDWARE_DRIVER"
OFFLINE = "OFFLINE"
TEST_ONLY = "TEST_ONLY"

# ----------------------------------------------------------------------
# the declared role of every production file
#
# Paths are relative to firmware/. A file missing here is a failure, and
# so is an entry naming a file that no longer exists - both mean the
# inventory and the tree have drifted apart.
# ----------------------------------------------------------------------

ROLES = {
    # -- the operator application -------------------------------------
    "PC/rover_science_client.py": MISSION_RUNTIME,
    "PC/serial_link.py": MISSION_RUNTIME,
    "PC/workflow/__init__.py": MISSION_RUNTIME,
    "PC/workflow/screen.py": MISSION_RUNTIME,
    "PC/workflow/session.py": MISSION_RUNTIME,
    "PC/workflow/measure.py": MISSION_RUNTIME,
    "PC/workflow/carousel.py": MISSION_RUNTIME,
    "PC/workflow/display.py": MISSION_RUNTIME,
    "PC/workflow/prompts.py": MISSION_RUNTIME,
    "PC/workflow/calibration.py": MISSION_SUPPORT,
    "PC/workflow/records.py": MISSION_SUPPORT,

    # -- the science layer --------------------------------------------
    "Science/__init__.py": MISSION_RUNTIME,
    "Science/config.py": MISSION_RUNTIME,
    "Science/preprocessing.py": MISSION_RUNTIME,
    "Science/features.py": MISSION_RUNTIME,
    "Science/metrics.py": MISSION_RUNTIME,
    "Science/comparison.py": MISSION_RUNTIME,
    "Science/decision.py": MISSION_RUNTIME,
    "Science/pipeline.py": MISSION_RUNTIME,
    "Science/quality.py": MISSION_RUNTIME,
    "Science/calibration.py": MISSION_RUNTIME,
    "Science/taxonomy.py": MISSION_RUNTIME,
    "Science/class_models.py": MISSION_SUPPORT,
    "Science/model_registry.py": OFFLINE,

    # -- the data layer -----------------------------------------------
    "BD/__init__.py": MISSION_RUNTIME,
    "BD/config.py": MISSION_RUNTIME,
    "BD/databases.py": MISSION_RUNTIME,
    "BD/registry.py": MISSION_RUNTIME,
    "BD/samples.py": MISSION_RUNTIME,
    "BD/calibrations.py": MISSION_RUNTIME,
    "BD/channels.py": MISSION_RUNTIME,
    "BD/acquisition_profiles.py": MISSION_SUPPORT,
    "BD/decision_learning.py": MISSION_SUPPORT,

    # -- the firmware --------------------------------------------------
    "ESP32/boot.py": HARDWARE_DRIVER,
    "ESP32/main.py": HARDWARE_DRIVER,
    "ESP32/config.py": HARDWARE_DRIVER,
    "ESP32/protocol.py": HARDWARE_DRIVER,
    "ESP32/carousel.py": HARDWARE_DRIVER,
    "ESP32/servo.py": HARDWARE_DRIVER,
    "ESP32/sensor.py": HARDWARE_DRIVER,
}

# Whole trees whose every file carries one role. Checked for existence,
# not enumerated by hand: research/ changes with the science, and a
# campaign that fails because somebody added an analysis script would
# teach people to delete the check.
ROLE_TREES = (
    ("research", OFFLINE),
    ("tools", OFFLINE),
    ("Tests", TEST_ONLY),
)

# OFFLINE code must never be reachable from the mission path. This is
# the rule that keeps a competition run from importing matplotlib.
FORBIDDEN_ON_MISSION_PATH = ("research", "tools")


def production_files():
    """Every .py file under firmware/, excluding caches."""
    found = []

    for path in sorted(FIRMWARE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue

        found.append(path.relative_to(FIRMWARE).as_posix())

    return found


def role_of(relative):
    if relative in ROLES:
        return ROLES[relative]

    head = relative.split("/", 1)[0]

    for tree, role in ROLE_TREES:
        if head == tree:
            return role

    return None


def imports_of(path):
    """Top-level module names imported by one file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))

    except (OSError, SyntaxError):
        return set()

    names = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])

        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                names.add(node.module.split(".")[0])

    return names


def main():
    files = production_files()
    unknown = [name for name in files if role_of(name) is None]
    stale = [name for name in ROLES if not (FIRMWARE / name).exists()]

    counts = {}

    for name in files:
        role = role_of(name) or "UNKNOWN"
        counts[role] = counts.get(role, 0) + 1

    print("=" * 68)
    print("  MODULE INVENTORY")
    print("=" * 68)
    print()

    for role in (MISSION_RUNTIME, MISSION_SUPPORT, HARDWARE_DRIVER,
                 OFFLINE, TEST_ONLY):
        print("  {:<18} {:>4} files".format(role, counts.get(role, 0)))

    print()
    print("  {:<18} {:>4} files".format("total", len(files)))
    print()

    # -- the rule that matters ----------------------------------------
    leaks = []

    for name in files:
        if role_of(name) not in (MISSION_RUNTIME, MISSION_SUPPORT):
            continue

        for imported in imports_of(FIRMWARE / name):
            if imported in FORBIDDEN_ON_MISSION_PATH:
                leaks.append((name, imported))

    print("-" * 68)
    failures = 0

    if unknown:
        failures += len(unknown)
        print("  FAIL  {} production file(s) with no declared role:"
              .format(len(unknown)))

        for name in unknown:
            print("          {}".format(name))

    else:
        print("  ok    every production file has a declared role")

    if stale:
        failures += len(stale)
        print("  FAIL  {} inventory entr(ies) naming a missing file:"
              .format(len(stale)))

        for name in stale:
            print("          {}".format(name))

    else:
        print("  ok    every inventory entry names a file that exists")

    if leaks:
        failures += len(leaks)
        print("  FAIL  offline code imported from the mission path:")

        for name, imported in leaks:
            print("          {} imports {}".format(name, imported))

    else:
        print("  ok    no mission module imports research/ or tools/")

    print("-" * 68)
    print()

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
