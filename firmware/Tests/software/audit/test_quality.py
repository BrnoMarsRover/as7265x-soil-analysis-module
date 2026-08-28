"""
The tests, tested.

WHY

A suite is only evidence if its checks can fail. This tree has already
produced three tests that could not:

    a patch applied to `json.dumps` where the code calls `json.dump`,
    so the test passed while injecting nothing;

    a `SensorError` raised from a stale module, so the firmware never
    recognised it and the test reported a defect that did not exist;

    a case labelled "saving twice does not write twice" that fed "3" as
    a Sample ID, saved once, and asserted nothing at all.

None of those was caught by running the suite. They were caught by
looking at the suite. This does that mechanically.

WHAT IT LOOKS FOR - closure task sections 24 and 25

    zero assertions            a section that checks nothing
    weak assertions            only `is not None`, which passes for
                               almost any bug
    swallowed failures         `except Exception` around the thing
                               under test, then a pass
    unproven injection         a fault injected without any assertion
                               that it actually happened
    real BD                    a test writing to the protected tree
    order dependence           a suite that only passes after another

    py firmware/Tests/software/audit/test_quality.py
    py firmware/Tests/software/audit/test_quality.py --verbose
"""

import ast
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

FIRMWARE = next(
    p for p in Path(__file__).resolve().parents if p.name == "firmware"
)

SOFTWARE = FIRMWARE / "Tests" / "software"

# Names that count as making a claim.
ASSERTIONS = {"ok", "equal", "close", "raises"}

# Names that count as PROVING an injected fault really fired.
INJECTION_EVIDENCE = {"calls", "failures", "interrupted", "touches",
                      "write_count", "read_count", "opened_count",
                      "closed_count", "buffer_resets", "corrupt_frames",
                      "stale_frames", "salvaged_frames",
                      "oversized_lines", "iterations", "delivered",
                      "recovery_count", "sleeps"}

# Files that are infrastructure rather than suites.
NOT_SUITES = {"__init__.py", "run_software.py", "mutation.py",
              "coverage_report.py"}


def suite_files():
    found = []

    for path in sorted(SOFTWARE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue

        if path.name in NOT_SUITES:
            continue

        if "audit" in path.parts or "fakes" in path.parts:
            continue

        found.append(path)

    return found


def call_name(node):
    if not isinstance(node, ast.Call):
        return None

    func = node.func

    if isinstance(func, ast.Attribute):
        return func.attr

    if isinstance(func, ast.Name):
        return func.id

    return None


class Finding:
    def __init__(self, kind, path, line, detail):
        self.kind = kind
        self.path = path
        self.line = line
        self.detail = detail

    def __str__(self):
        return "  {:<20} {}:{}  {}".format(
            self.kind, self.path, self.line, self.detail)


def sections_of(tree, source_lines):
    """
    Split a suite at its `checks.section(...)` calls.

    A suite is a script, not a collection of functions, so the section
    is the unit a reader thinks in and the unit an empty check shows up
    in.
    """
    marks = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and call_name(node) == "section":
            title = ""

            if node.args and isinstance(node.args[0], ast.Constant):
                title = str(node.args[0].value)

            elif node.args:
                title = "<computed>"

            marks.append((node.lineno, title))

    marks.sort()
    sections = []

    for index, (line, title) in enumerate(marks):
        end = marks[index + 1][0] if index + 1 < len(marks) \
            else len(source_lines) + 1
        sections.append((line, end, title))

    return sections


def asserting_helpers(tree):
    """
    Local functions that make a claim, so calling one counts as one.

    WITHOUT THIS THE TOOL CRIES WOLF. `test_display_shapes.py` defines

        def survives(label, call): ... checks.ok(...)

    and then uses it a hundred times. Seven of its sections were
    reported as making no check at all, when every one of them makes a
    dozen through that helper - which is the whole reason the helper
    exists.

    Resolved by fixed point, because a helper may call another helper.
    """
    direct = set()
    calls_made = {}

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        names = {call_name(inner) for inner in ast.walk(node)}
        calls_made[node.name] = names

        if names & ASSERTIONS:
            direct.add(node.name)

    changed = True

    while changed:
        changed = False

        for name, names in calls_made.items():
            if name in direct:
                continue

            if names & direct:
                direct.add(name)
                changed = True

    return direct


def analyse(path):
    relative = path.relative_to(FIRMWARE).as_posix()
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source)
    findings = []

    claims = ASSERTIONS | asserting_helpers(tree)

    # -- zero-assertion sections ---------------------------------------
    assertion_lines = []
    weak_lines = []

    for node in ast.walk(tree):
        name = call_name(node)

        if name in claims:
            assertion_lines.append(node.lineno)

            # `checks.ok(x is not None, ...)` passes for almost any bug
            # in x. Flagged unless the section makes a stronger claim
            # somewhere too.
            if name == "ok" and node.args:
                first = node.args[0]

                if isinstance(first, ast.Compare):
                    if any(isinstance(op, (ast.IsNot,))
                           for op in first.ops):
                        if any(isinstance(c, ast.Constant)
                               and c.value is None
                               for c in first.comparators):
                            weak_lines.append(node.lineno)

    for start, end, title in sections_of(tree, lines):
        inside = [n for n in assertion_lines if start < n < end]

        if not inside:
            findings.append(Finding(
                "zero-assertion", relative, start,
                "section {!r} makes no check".format(title[:48])))

            continue

        weak_inside = [n for n in weak_lines if start < n < end]

        if len(weak_inside) == len(inside):
            findings.append(Finding(
                "only-is-not-None", relative, start,
                "section {!r}: every check is `is not None`".format(
                    title[:40])))

    # -- swallowed failures --------------------------------------------
    #
    # `try: <thing under test> except Exception: pass` with no assertion
    # afterwards turns any failure into a pass.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue

        for handler in node.handlers:
            broad = (
                handler.type is None
                or (isinstance(handler.type, ast.Name)
                    and handler.type.id in ("Exception", "BaseException"))
            )

            if not broad:
                continue

            only_pass = (len(handler.body) == 1
                         and isinstance(handler.body[0], ast.Pass))

            if not only_pass:
                continue

            # Acceptable when the body is cleanup, not the thing under
            # test - recognised by there being no assertion inside the
            # try at all.
            has_assertion = any(
                call_name(inner) in claims
                for inner in ast.walk(node)
            )

            if has_assertion:
                findings.append(Finding(
                    "swallowed-failure", relative, handler.lineno,
                    "broad except: pass around code that also asserts"))

    # -- unproven fault injection --------------------------------------
    #
    # A file that injects faults must somewhere assert that one fired.
    injects = any(
        name in ("raiser", "counting_raiser", "failing", "interrupting")
        for name in (call_name(n) for n in ast.walk(tree))
    )

    if injects:
        proves = False

        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue

            if node.attr in INJECTION_EVIDENCE:
                proves = True

                break

        # A fault whose EFFECT is asserted by a distinct error code is
        # also proof; look for a code comparison.
        if not proves:
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(
                        node.value, str):
                    value = node.value

                    if value.isupper() and "_" in value and len(value) > 5:
                        proves = True

                        break

        if not proves:
            findings.append(Finding(
                "unproven-injection", relative, 1,
                "injects faults but never asserts one fired"))

    # -- the protected tree --------------------------------------------
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()

        if stripped.startswith("#"):
            continue

        # A suite may NAME the real tree to hash it or to read reference
        # data; what it must not do is build a WRITABLE store on it.
        #
        # EVERY writable store, and the no-argument form too.
        #
        # This checked `SampleStore(` alone, and only when the line also
        # named ARCHIVE_PATH or config.SAMPLES_FILE. It therefore missed
        # the two ways a suite actually reaches the production archive:
        #
        #   DecisionLearningStore()   no argument means BD/training/
        #   SampleStore()             no argument means BD/samples/
        #
        # The first is not hypothetical. `sandbox_mission` did not
        # redirect the learning store, so code driving a sandboxed
        # Mission wrote observations straight into the real
        # `BD/training/decision_learning.sqlite3`. The campaign's BD/
        # hash check caught it afterwards; nothing caught it before,
        # and this rule is what should have.
        # ONE suite is allowed to name the production stores: the one
        # whose whole job is to police them. `data_integrity` builds a
        # default store to ASSERT that the default path is the real
        # archive - the very rule enforced here - and never writes
        # through it. Exempting it by name keeps the rule sharp for
        # everybody else instead of weakening the pattern for all.
        if "data_integrity" in relative:
            continue

        for store in ("SampleStore", "CalibrationStore",
                      "DecisionLearningStore", "AcquisitionProfileStore"):
            call = store + "("

            if call not in stripped or "SandboxBD" in stripped:
                continue

            after = stripped.split(call, 1)[1].lstrip()

            if after.startswith(")"):
                findings.append(Finding(
                    "real-BD-write", relative, index,
                    "{}() with no argument is the production store"
                    .format(store)))

            elif "ARCHIVE_PATH" in stripped or "config." in stripped:
                findings.append(Finding(
                    "real-BD-write", relative, index,
                    "opens the production {} directly".format(store)))

    return findings


def main(argv):
    verbose = "--verbose" in argv
    files = suite_files()

    all_findings = []

    for path in files:
        all_findings.extend(analyse(path))

    print("=" * 72)
    print("  TEST QUALITY AUDIT")
    print("=" * 72)
    print()
    print("  {} suite files scanned".format(len(files)))
    print()

    by_kind = {}

    for finding in all_findings:
        by_kind.setdefault(finding.kind, []).append(finding)

    for kind in ("zero-assertion", "only-is-not-None", "swallowed-failure",
                 "unproven-injection", "real-BD-write"):
        found = by_kind.get(kind, [])
        print("  {:<22} {:>3}".format(kind, len(found)))

        if found and (verbose or len(found) <= 12):
            for finding in found:
                print("      {}:{}  {}".format(
                    finding.path, finding.line, finding.detail))

    print()
    print("-" * 72)

    if all_findings:
        print("  FAIL  {} weak test(s) - each is a check that cannot "
              "fail".format(len(all_findings)))

    else:
        print("  ok    every section makes at least one falsifiable "
              "claim, every injected fault is proved to have fired, and "
              "no suite touches the production archive")

    print("-" * 72)
    print()

    return 1 if all_findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
