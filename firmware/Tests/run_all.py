"""
Run every suite and print one summary.

Each suite is a standalone program that must run in its OWN process:
ESP32/ and BD/ both contain a module called `config`, and the ESP32
firmware is reloaded from scratch several times within one suite.
Sharing an interpreter would let one suite's imports decide another
suite's results.

Five suites, one per question:

    architecture  are the boundaries still where they are supposed to be
    esp32         does the hardware controller behave
    science       is the mathematics right
    data          is the scientific record sound
    pc            does the orchestration hold the port and the order

    py run_all.py            every suite
    py run_all.py esp32      only suites whose name contains "esp32"

Exit status is non-zero if any suite fails, so this is usable in a hook.
"""

import subprocess
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent

# Architecture first: if a boundary has moved, every later failure is
# likely to be a symptom of that rather than a fault of its own.
SUITES = (
    ("test_architecture.py", "domain boundaries and obsolete architecture"),
    ("test_esp32.py", "protocol, drivers, carousel, on fake hardware"),
    ("test_science.py", "formulas, comparison, Decision Model"),
    ("test_data.py", "record model, RAW immutability, provenance"),
    ("test_pc.py", "serial lifecycle, error kinds, measurement order"),
)


def run(name):
    """Run one suite, returning (passed, checks, output)."""
    result = subprocess.run(
        [sys.executable, str(HERE / name)],
        capture_output=True, text=True, cwd=str(HERE),
    )

    checks = 0

    for line in result.stdout.splitlines():
        if "all " in line and " checks passed" in line:
            try:
                checks = int(line.split("all ")[1].split(" checks")[0])

            except (IndexError, ValueError):
                checks = 0

    return result.returncode == 0, checks, result


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    pattern = argv[0] if argv else None

    suites = [
        (name, description) for name, description in SUITES
        if pattern is None or pattern in name
    ]

    if not suites:
        print("No suite matches {!r}.".format(pattern))

        return 2

    print("=" * 68)
    print("Freya science module - test suites")
    print("=" * 68)
    print()

    total = 0
    failures = []

    for name, description in suites:
        print("{:<26} {:<38}".format(name, description), end="", flush=True)

        passed, checks, result = run(name)

        total += checks

        if passed:
            print("{:>4} ok".format(checks))

        else:
            print("  FAILED")
            failures.append((name, result))

    print()
    print("=" * 68)

    if failures:
        print("{} of {} suites FAILED, {} checks passed elsewhere".format(
            len(failures), len(suites), total
        ))

        for name, result in failures:
            print()
            print("-" * 68)
            print("{}:".format(name))
            print("-" * 68)

            # Only the failing lines and the tail, so one broken suite does
            # not bury the summary.
            for line in result.stdout.splitlines():
                if line.strip().startswith("FAIL") or "FAILED" in line:
                    print(line)

            if result.stderr.strip():
                print(result.stderr.strip()[-2000:])

        return 1

    print("all {} suites passed, {} checks total".format(len(suites), total))

    return 0


if __name__ == "__main__":
    sys.exit(main())
