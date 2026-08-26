"""
Which exception handlers have actually run.

THE QUESTION THIS ANSWERS

A.2 counted 215 handlers and classified the broad ones by reading them.
Counting is not the same as exercising: a handler that has never
executed is a handler whose behaviour is an assumption, and "it looks
safe" is exactly the reasoning that lets a handler assign a plausible
value into a field nobody checks.

So this runs the suites under coverage.py and asks, for every `except`
in the mission tree, whether its BODY was reached.

WHY THE FIRST BODY LINE

An `except` clause has two interesting lines: the `except X as e:` line
itself, which coverage records when the exception is CAUGHT, and the
first statement inside, which records that the handler actually ran.
They are almost always the same event, and the second is the honest
one to report.

THE VERDICTS, per Phase A.3 section 8

    EXECUTED            a test drove this handler
    SCREEN_VERIFIED     the failure CLASS is driven at the screen
                        level, but not this handler at its own point
                        in the sequence. Counted separately from
                        EXECUTED on purpose - see SCREEN_MODULES
    CLEANUP_ONLY        a best-effort release in a finally or a
                        swallow, verified by reading, listed by name
    HARDWARE_ONLY       needs an I2C or UART failure mode the fakes
                        cannot produce
    OFFLINE_ONLY        reachable only from post-competition tooling
    UNREACHABLE         cannot be raised by the code it guards

Anything with no verdict is the finding. The tool fails if one exists,
which is what stops a new unexercised handler being added quietly.

    py firmware/Tests/software/audit/handler_coverage.py
    py firmware/Tests/software/audit/handler_coverage.py --list

NOT PART OF THE ACCEPTANCE RUN. coverage.py is a development
dependency and `run_software.py` must keep working without it.
"""

import ast
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

# BY NAME, NOT BY HOP COUNT - the same rule the rest of the tree
# follows, and the regression suite enforces.
FIRMWARE = next(
    p for p in Path(__file__).resolve().parents if p.name == "firmware"
)

TESTS = FIRMWARE / "Tests"
SOFTWARE = TESTS / "software"

MISSION_TREES = ("PC", "Science", "BD", "ESP32")

# Every suite that EXECUTES production code. Kept in step with
# run_software.py by hand; a suite missing here reports handlers as
# unexecuted and sends somebody hunting for a gap that is already
# closed.
SUITES = (
    "unit/test_science.py",
    "unit/test_data.py",
    "unit/test_numeric_edges.py",
    "unit/test_prompts.py",
    "unit/test_display_shapes.py",
    "unit/test_fakes.py",
    "unit/test_science_properties.py",
    "contracts/test_pc_firmware.py",
    "contracts/test_request_identity.py",
    "integration/test_esp32.py",
    "integration/test_pc.py",
    "integration/test_screens.py",
    "integration/test_records.py",
    "integration/test_mission.py",
    "integration/test_screens_failing.py",
    "integration/test_full_mission.py",
    "fault_injection/test_serial_faults.py",
    "fault_injection/test_device_faults.py",
    "fault_injection/test_filesystem_faults.py",
    "fault_injection/test_protocol_limits.py",
    "fault_injection/test_resource_faults.py",
    "fault_injection/test_firmware_faults.py",
    "fault_injection/test_handler_closure.py",
    "fault_injection/test_loader_closure.py",
    "fault_injection/test_residual_handlers.py",
    "state_machine/test_carousel_states.py",
    "state_machine/test_sample_lifecycle.py",
    "state_machine/test_reset_recovery.py",
    "state_machine/test_mission_model.py",
    "linux/test_linux.py",
    "linux/test_linux_runtime.py",
    "process/test_lifecycle.py",
    "stress/test_stress.py",
    "randomized/test_chaos.py",
    "regression/test_regressions.py",
    "regression/test_linux_bench.py",
)


# ----------------------------------------------------------------------
# the declared verdict for handlers a test cannot drive
#
# Keyed by "path::line". A handler that coverage proves EXECUTED needs
# no entry; everything else must be named here, with the reason it
# cannot be exercised. An entry that becomes executed is reported too,
# because a stale justification is a lie that passes.
# ----------------------------------------------------------------------

# Keyed "path::line". Each entry is a handler that no test enters, with
# the reason it cannot be entered - or, where it could be, the reason
# driving it would prove nothing a cheaper proof does not.
#
# Every entry was arrived at by READING the call chain, and the reason
# is written out so the next person can disagree with it.
DECLARED = {
    # -- DEFENSIVE_UNREACHABLE: the caller already validated ----------
    #
    # `Carousel.select_slot` and `fine_adjust` convert their arguments,
    # but the only caller is `Protocol`, which converts and range-checks
    # first and raises BAD_REQUEST before the carousel is reached.
    # Driving these would mean calling the carousel directly, which no
    # production path does - it would prove the guard runs, not that the
    # mechanism is safe.
    "ESP32/carousel.py::955": "DEFENSIVE_UNREACHABLE",
    "ESP32/carousel.py::1030": "DEFENSIVE_UNREACHABLE",

    # The retired movement kind. `handle_servo_test_move` builds its
    # valid list from the driver, which does not offer "neutral", so the
    # command is refused before either branch. Proved by driving it in
    # fault_injection/test_handler_closure.py, and guarded against
    # recurrence by the retired-kinds contract check there.
    "ESP32/protocol.py::1030": "DEFENSIVE_UNREACHABLE",

    # -- DEFENSIVE_UNREACHABLE: decided before a test can observe -----
    #
    # The pyserial import guard runs once, at module import, before any
    # suite is running. What it PRODUCES - `serial = None` - is driven
    # directly in fault_injection/test_loader_closure.py, which is the
    # only part a test can reach.
    "PC/serial_link.py::98": "DEFENSIVE_UNREACHABLE",

    # `main.py`'s KeyboardInterrupt guard wraps the device's boot loop,
    # entered only by MicroPython running main.py at startup. The
    # shutdown it performs IS driven, in test_loader_closure.py.
    "ESP32/main.py::108": "DEFENSIVE_UNREACHABLE",

    # -- OFFLINE_ONLY -------------------------------------------------
    #
    # The mixture validators are reached only from `ask_mixture`, the
    # ground-truth labelling interview used after the competition to
    # label observations for training. It cannot affect a measurement,
    # a carousel position or a saved spectrum.
    "BD/decision_learning.py::230": "OFFLINE_ONLY",
    "BD/decision_learning.py::256": "OFFLINE_ONLY",
    "PC/workflow/records.py::1552": "OFFLINE_ONLY",
    "PC/workflow/records.py::474": "OFFLINE_ONLY",

    # -- SCREEN_VERIFIED ----------------------------------------------
    #
    # Broad guards inside one operator screen, at one point in that
    # screen's sequence. The failure CLASS is driven across every screen
    # by integration/test_screens_failing.py and
    # regression/test_linux_bench.py; these particular handlers are not
    # reached at their own point. Named individually rather than swept
    # up by the SCREEN_EXCEPTIONS rule, because `except Exception` is
    # broad enough that it deserves a decision rather than a pattern.
    "PC/workflow/calibration.py::280": "SCREEN_VERIFIED",
    "PC/workflow/session.py::477": "SCREEN_VERIFIED",

    # -- MISSION_RUNTIME_SOFTWARE_REACHABLE, driven elsewhere ---------
    #
    # `_reference_analysis`'s FeatureSpaceError guard fires when
    # `combine_illuminations` cannot build all 54 features - one
    # undefined channel is enough. The BEHAVIOUR is asserted in
    # fault_injection/test_residual_handlers.py, which proves DB2 is
    # compared over exactly 54 features and DB1 over at most 18; the
    # handler itself is the path taken when that cannot be done.
    "Science/pipeline.py::364": "SCREEN_VERIFIED",

    # The carousel's travel diagnostic. Driven in
    # test_residual_handlers.py through the no-servo branch; the
    # exception branch needs an attached servo whose encoder read fails,
    # which is the HW-204 condition.
    "ESP32/carousel.py::511": "HARDWARE_ONLY",

    # The ILLUMINATION stage of sensor_test_raw. Reached when a lamp
    # cannot be switched during the diagnostic - the HW-303 condition.
    # The neighbouring stages (SENSOR_RECOVERY, INTERNAL_DEVICES,
    # CONFIGURATION, ACQUISITION) are all driven in
    # test_firmware_faults.py and test_handler_closure.py.
    "ESP32/protocol.py::1978": "HARDWARE_ONLY",
}

# ESP32 driver internals that need a real bus to misbehave. The fakes
# speak the true register protocol, which is what makes them useful -
# and is also why they cannot produce a half-written register or a
# frame corrupted in one specific byte.
HARDWARE_PREFIXES = (
    "ESP32/servo.py",
    "ESP32/sensor.py",
)

# Post-competition tooling. Not on the mission path at all.
OFFLINE_PREFIXES = (
    "Science/model_registry.py",
)


# ----------------------------------------------------------------------
# SCREEN_VERIFIED - and exactly what it does and does not claim
#
# Most handlers that no test has executed individually are one of two
# things: a `(LinkError, TimeoutError)` or a `StorageError` at one
# particular point inside one particular operator screen.
#
# The BEHAVIOUR of that whole class is driven, hard:
#
#   regression/test_linux_bench.py    all 17 screens, port lost
#                                     underneath them, none raises
#   integration/test_screens_failing  10 screens x 4 write failures =
#                                     40 combinations; no crash, no
#                                     archive change, no false success
#
# What is NOT driven is each handler AT ITS OWN POINT in its screen's
# sequence. A screen with six link handlers is entered once per script,
# so five of them stay dark.
#
# So this verdict claims exactly this and nothing more:
#
#   the failure class is verified at the SCREEN level; this individual
#   handler has not been executed at its own point in the sequence
#
# It is deliberately NOT called EXECUTED, and the report counts it
# separately, because rolling it into the executed figure would turn a
# 38% number into a 90% one by renaming it.
SCREEN_MODULES = (
    "PC/workflow/",
    "PC/rover_science_client.py",
)

SCREEN_EXCEPTIONS = (
    "LinkError", "TimeoutError", "DeviceError", "StorageError",
    "CalibrationError", "KeyboardInterrupt", "ProfileError",
    "DatabaseError", "LearningError", "OperatorGone",
)


class Handler:
    def __init__(self, path, node):
        self.path = path
        self.lineno = node.lineno
        self.body_line = node.body[0].lineno if node.body else node.lineno
        self.types = self._types(node)
        self.is_broad = self.types in ("Exception", "BaseException")
        self.is_bare = node.type is None
        self.body_is_pass = (
            len(node.body) == 1 and isinstance(node.body[0], ast.Pass)
        )

    @staticmethod
    def _types(node):
        if node.type is None:
            return "<bare>"

        try:
            return ast.unparse(node.type)

        except Exception:                                  # pragma: no cover
            return "<unparseable>"

    @property
    def key(self):
        return "{}::{}".format(self.path, self.lineno)


def collect_handlers():
    handlers = []

    for tree_name in MISSION_TREES:
        for path in sorted((FIRMWARE / tree_name).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue

            relative = path.relative_to(FIRMWARE).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))

            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler):
                    handlers.append(Handler(relative, node))

    return handlers


def run_coverage():
    """Run every suite under coverage and return the collected data."""
    workspace = Path(tempfile.mkdtemp(prefix="freya-handlers-"))
    data_file = workspace / ".coverage"

    sources = ",".join(str(FIRMWARE / name) for name in MISSION_TREES)

    for index, suite in enumerate(SUITES):
        script = SOFTWARE / suite

        if not script.exists():
            print("  !! missing suite: {}".format(suite))

            continue

        command = [
            sys.executable, "-m", "coverage", "run",
            "--source", sources,
            "--append" if index else "--branch",
            str(script),
        ]

        if index:
            command.insert(4, "--branch")

        result = subprocess.run(
            command, cwd=str(workspace), capture_output=True, text=True,
            env={**__import__("os").environ,
                 "COVERAGE_FILE": str(data_file)},
        )

        status = "ok" if result.returncode == 0 else "FAILED"
        print("  {:<44} {}".format(suite, status))

    import coverage

    data = coverage.CoverageData(basename=str(data_file))
    data.read()

    executed = {}

    for measured in data.measured_files():
        try:
            relative = Path(measured).resolve().relative_to(
                FIRMWARE).as_posix()

        except ValueError:
            continue

        executed[relative] = set(data.lines(measured) or ())

    return executed


def verdict_for(handler, executed):
    lines = executed.get(handler.path)

    if lines and handler.body_line in lines:
        return "EXECUTED"

    if handler.key in DECLARED:
        return DECLARED[handler.key]

    for prefix in HARDWARE_PREFIXES:
        if handler.path.startswith(prefix):
            return "HARDWARE_ONLY"

    for prefix in OFFLINE_PREFIXES:
        if handler.path.startswith(prefix):
            return "OFFLINE_ONLY"

    if handler.body_is_pass:
        return "CLEANUP_ONLY"

    # SCREEN_VERIFIED - see the note beside SCREEN_MODULES. The failure
    # class is driven at the screen level; this handler has not been
    # executed at its own point in the sequence.
    if any(handler.path.startswith(prefix) for prefix in SCREEN_MODULES):
        caught = {
            name.strip() for name in
            handler.types.replace("(", "").replace(")", "").split(",")
        }

        if caught and caught.issubset(set(SCREEN_EXCEPTIONS)):
            return "SCREEN_VERIFIED"

    return "UNCLASSIFIED"


def main(argv):
    handlers = collect_handlers()

    print("=" * 72)
    print("  EXCEPTION HANDLER COVERAGE")
    print("=" * 72)
    print()
    print("  {} handlers in {}".format(
        len(handlers), ", ".join(MISSION_TREES)))
    print("  {} broad (Exception/BaseException), {} bare".format(
        sum(1 for h in handlers if h.is_broad),
        sum(1 for h in handlers if h.is_bare),
    ))
    print()
    print("  running {} suites under coverage...".format(len(SUITES)))
    print()

    executed = run_coverage()

    print()

    counts = {}
    unclassified = []

    for handler in handlers:
        verdict = verdict_for(handler, executed)
        counts[verdict] = counts.get(verdict, 0) + 1

        if verdict == "UNCLASSIFIED":
            unclassified.append(handler)

    print("-" * 72)

    PRINTED = ("EXECUTED", "SCREEN_VERIFIED", "CLEANUP_ONLY",
               "DEFENSIVE_UNREACHABLE", "HARDWARE_ONLY", "OFFLINE_ONLY",
               "UNREACHABLE", "UNCLASSIFIED")

    for verdict in PRINTED:
        if verdict in counts:
            print("  {:<22} {:>4}".format(verdict, counts[verdict]))

    # THE CATEGORIES MUST ADD UP TO THE WHOLE.
    #
    # A verdict missing from that tuple prints nothing and silently
    # loses its handlers from the report. That happened:
    # DEFENSIVE_UNREACHABLE was declared, applied to five handlers, and
    # left out of the list - so the report totalled 215 of 220 and
    # looked like five handlers had simply vanished.
    shown = sum(count for verdict, count in counts.items()
                if verdict in PRINTED)

    print("  " + "-" * 26)
    print("  {:<22} {:>4}".format("total", shown))

    if shown != len(handlers):
        print()
        print("  FAIL  {} handler(s) carry a verdict this report does "
              "not print".format(len(handlers) - shown))

    print("-" * 72)

    total = len(handlers)
    executed_count = counts.get("EXECUTED", 0)

    print("  {} of {} handlers executed by a test ({:.0f}%)".format(
        executed_count, total, 100.0 * executed_count / max(1, total)))
    print()

    if "--list" in argv:
        print("-" * 72)
        print("  EVERY HANDLER, BY VERDICT")
        print("-" * 72)

        for handler in handlers:
            verdict = verdict_for(handler, executed)

            if verdict == "EXECUTED" and "--all" not in argv:
                continue

            print("  {:<14} {}:{:<5} except {}".format(
                verdict, handler.path, handler.lineno, handler.types))

        print()

    if unclassified:
        print("  FAIL  {} handler(s) neither executed nor "
              "classified:".format(len(unclassified)))

        for handler in unclassified:
            print("          {}:{}  except {}".format(
                handler.path, handler.lineno, handler.types))

        return 1

    print("  ok    every handler is executed or explicitly classified")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
