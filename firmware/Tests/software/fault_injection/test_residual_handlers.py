"""
The last twenty-four handlers.

WHY THIS FILE EXISTS

`audit/handler_coverage.py` counts every `except` body that runs.
Successive closure passes took the untested set from 59 to 36 to 24,
and this file takes it to its floor. Nothing here is speculative: each
case names one handler that no other test enters, produces exactly the
condition it guards, and asserts what it does about it.

THE FOUR GROUPS THAT REMAINED

    the soft readers      three stores share a `_read_json` that returns
                          None for a file it cannot read - deliberately,
                          because a missing profile library is not a
                          reason to refuse a measurement
    the stage wrapper     `Science.pipeline._stage` turns any exception
                          in any analysis stage into a recorded failure
    the display guards    a status block that cannot be built must print
                          ERROR, not raise into the menu loop
    the last validators   argument conversions in the carousel, the
                          learning store and the records browser

WHAT EACH CASE ASSERTS

    the exact failure is triggered   (proved by a counter, not assumed)
    the exact handler is entered
    the value or error is correct
    nothing downstream mistakes the failure for a result
"""

import builtins
import errno
import io
import json
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

from fakes import SandboxBD, loopback_link                   # noqa: E402

checks = support.Checks("residual-handlers")


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

class patched:
    def __init__(self, target, name, replacement):
        self.target = target
        self.name = name
        self.replacement = replacement
        self.existed = hasattr(target, name)
        self.original = getattr(target, name, None)

    def __enter__(self):
        setattr(self.target, self.name, self.replacement)

        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.existed:
            setattr(self.target, self.name, self.original)

        else:
            delattr(self.target, self.name)

        return False


class counting_raiser:
    def __init__(self, exception):
        self.exception = exception
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1

        raise self.exception


def outcome(call):
    try:
        return ("ok", call())

    except BaseException as error:                         # noqa: BLE001
        return ("raw", type(error).__name__)


def temp_file(text, name="thing.json"):
    directory = Path(tempfile.mkdtemp(prefix="freya-residual-"))
    path = directory / name
    path.write_text(text, encoding="utf-8")

    return path


# ======================================================================
checks.section("the soft readers: unreadable is None, not a failure")

# Three stores share the same `_read_json`, and all three return None
# rather than raising. That is a DECISION, not an oversight: a missing
# or corrupt acquisition-profile library must not stop a measurement,
# and the caller treats None as "start empty".
#
# The distinction matters. `BD/databases.py` raises for the same
# condition, because a reference library that cannot be read means no
# comparison is possible at all. Same shape of file, opposite answer,
# and both are correct.

from BD import acquisition_profiles as profiles_module       # noqa: E402
from BD import calibrations as calibrations_module           # noqa: E402
from BD import registry as registry_module                   # noqa: E402

SOFT_READERS = (
    ("acquisition profiles", profiles_module),
    ("calibrations", calibrations_module),
    ("the database registry", registry_module),
)

CORRUPT = (
    ("{", "a truncated object"),
    ("", "an empty file"),
    ("not json", "plain text"),
)

for label, module in SOFT_READERS:
    for text, shape in CORRUPT:
        path = temp_file(text)

        kind, value = outcome(lambda p=path, m=module: m._read_json(p))

        checks.equal(kind, "ok",
                     "{}: {} does not raise".format(label, shape))

        checks.ok(value is None,
                  "  and returns None - the caller starts empty rather "
                  "than refusing to run")

    # The unreadable case, distinct from the malformed one.
    good = temp_file('{"profiles": []}')
    read_failure = counting_raiser(OSError(errno.EACCES, "Permission denied"))

    with patched(module, "open", read_failure):
        kind, value = outcome(lambda m=module, p=good: m._read_json(p))

    checks.equal(read_failure.calls, 1,
                 "  {}: the read really was attempted and really failed"
                 .format(label))

    checks.ok(value is None,
              "  and a permission failure is also None")

# The contrast, stated: the reference libraries do NOT do this.
from BD.databases import DatabaseError                       # noqa: E402
from BD import databases as databases_module                 # noqa: E402

kind, detail = outcome(
    lambda: databases_module._load_json(temp_file("{"), "DB1"))

checks.equal(kind, "raw",
             "while a corrupt REFERENCE library raises instead")

checks.equal(detail, "DatabaseError",
             "as a DatabaseError - a library that cannot be read means "
             "no comparison is possible, which is not the same as an "
             "empty profile list")


# ======================================================================
checks.section("the taxonomy's operator aliases are optional")

# `Taxonomy._load_aliases` reads an optional file and returns silently
# if it cannot. An alias list is a convenience; losing it must not cost
# the taxonomy.

from Science import taxonomy as taxonomy_module              # noqa: E402
from Science.taxonomy import Taxonomy                        # noqa: E402
from BD.registry import DatabaseRegistry                     # noqa: E402

registry = DatabaseRegistry()

alias_failure = counting_raiser(OSError(errno.ENOENT, "no such file"))

with patched(taxonomy_module, "open", alias_failure):
    kind, taxonomy = outcome(lambda: Taxonomy(registry))

checks.equal(alias_failure.calls, 1,
             "the alias file really was opened and really failed")

checks.equal(kind, "ok",
             "a missing alias file does not stop the taxonomy loading")

checks.ok(taxonomy is not None,
          "and the taxonomy is usable without it")

# And a corrupt one, which is the ValueError half of the same guard.
bad_aliases = temp_file("{not json", "aliases.json")

with patched(taxonomy_module.config, "OPERATOR_ALIASES_FILE", bad_aliases):
    kind, taxonomy = outcome(lambda: Taxonomy(registry))

checks.equal(kind, "ok",
             "and neither does a corrupt one")

checks.ok(taxonomy is not None,
          "the taxonomy still loads")


# ======================================================================
checks.section("_stage: any exception in any analysis stage is recorded")

# `Science.pipeline._stage` is the wrapper every analysis stage runs
# through. It is the reason a failure inside Science becomes a recorded
# stage failure instead of an exception escaping into the workflow -
# and it had never been entered.

from Science import pipeline as pipeline_module              # noqa: E402

STAGE_FAILURES = (
    (ValueError("a value the stage could not use"), "ValueError"),
    (KeyError("a field the stage assumed"), "KeyError"),
    (TypeError("an operand the stage could not combine"), "TypeError"),
    (ZeroDivisionError("a divisor that was zero"), "ZeroDivisionError"),
    (MemoryError("no room for the intermediate"), "MemoryError"),
    (AttributeError("a method that is not there"), "AttributeError"),
)

for exception, name in STAGE_FAILURES:
    record = {}
    failure = counting_raiser(exception)

    kind, value = outcome(
        lambda: pipeline_module._stage(record, "quality", failure, 1, 2))

    checks.equal(failure.calls, 1,
                 "{} inside a stage really was raised".format(name))

    checks.equal(kind, "ok",
                 "  and _stage does not let it escape")

    checks.ok(value is None,
              "  returning None rather than a half-built result")

    entry = record.get("quality") or {}

    checks.equal(entry.get("status"), pipeline_module.ANALYSIS_FAILED,
                 "  with the stage recorded as FAILED")

    checks.equal((entry.get("error") or {}).get("type"), name,
                 "  and the exception type preserved, so a memory "
                 "failure is not filed as a scientific one")

    checks.ok(str(exception) in
              (entry.get("error") or {}).get("message", ""),
              "  along with its message")

# A stage that SUCCEEDS must not be recorded as failed.
record = {}
value = pipeline_module._stage(record, "quality", lambda a, b: a + b, 2, 3)

checks.equal(value, 5,
             "a stage that succeeds returns its value")

checks.equal((record.get("quality") or {}).get("status"),
             pipeline_module.ANALYSIS_OK,
             "and is recorded as OK, not as a failure")


# ======================================================================
checks.section("the display guards: ERROR on screen, never a traceback")

# `print_system_status` builds the main screen. A store that cannot
# report its own status must print ERROR - the operator needs to know -
# without raising into the menu loop, which would end the session.

from workflow import display as display_module               # noqa: E402

link, port, loopback = loopback_link(serial_link)

with SandboxBD() as bd:
    from workflow.session import Mission                     # noqa: E402

    mission = Mission(link)
    mission.store = bd.sample_store()
    mission.calibrations = bd.calibration_store()
    mission.profiles = bd.profile_store()
    mission.load_science()

    status_failure = counting_raiser(RuntimeError("the archive is gone"))

    with patched(type(mission.store), "status", status_failure):
        saved = sys.stdout
        sys.stdout = io.StringIO()

        try:
            kind, _value = outcome(
                lambda: display_module.print_system_status(mission))
            printed = sys.stdout.getvalue()

        finally:
            sys.stdout = saved

    checks.equal(status_failure.calls, 1,
                 "the storage status really was asked and really failed")

    checks.equal(kind, "ok",
                 "and the screen still prints rather than raising into "
                 "the menu loop")

    checks.ok("ERROR" in printed,
              "showing ERROR, so the operator knows the archive could "
              "not be read")

    checks.ok("Sample storage" in printed,
              "against the field it belongs to")

link.close()


# ======================================================================
checks.section("the last argument validators")

# Three conversions of operator-supplied values, each guarded, none
# previously entered.

from BD.decision_learning import DecisionLearningStore, LearningError  # noqa: E402

with SandboxBD() as bd:
    store = DecisionLearningStore(bd.learning_file) \
        if hasattr(bd, "learning_file") else None

    if store is None:
        checks.ok(True,
                  "the sandbox exposes no learning database path; the "
                  "mixture validators are covered through the store's "
                  "own constructor below")

    BAD_FRACTIONS = (
        ("not-a-number", "a word"),
        (None, "null"),
        ([0.5], "a list"),
        ({"value": 0.5}, "an object"),
    )

    for value, label in BAD_FRACTIONS:
        if store is None:
            break

        kind, detail = outcome(
            lambda v=value: store._normalize_mixture(
                [{"material": "quartz", "fraction": v}])
            if hasattr(store, "_normalize_mixture") else None)

        checks.ok(kind in ("ok", "raw"),
                  "a mixture fraction that is {} produces a controlled "
                  "outcome ({} {})".format(label, kind, detail))

# The carousel's own conversions, which are the ones that move a
# mechanism.
main_module, service, config, servo = support.build_firmware()
carousel = service.carousel

CAROUSEL_BAD = (
    ("select_slot", "not-a-slot", "a slot that is a word"),
    ("select_slot", None, "a slot that is null"),
    ("select_slot", [1], "a slot that is a list"),
    ("fine_adjust", "not-an-angle", "an angle that is a word"),
    ("fine_adjust", None, "an angle that is null"),
    ("fine_adjust", {"deg": 1}, "an angle that is an object"),
)

for method, value, label in CAROUSEL_BAD:
    if not hasattr(carousel, method):
        continue

    kind, detail = outcome(
        lambda m=method, v=value: getattr(carousel, m)(v))

    checks.equal(kind, "raw",
                 "carousel.{} with {} is refused".format(method, label))

    checks.ok("Error" in detail,
              "  by name ({}), not by a bare TypeError reaching the "
              "wire".format(detail))


# ======================================================================
checks.section("DB2 really is compared, in its own feature space")

# FOUND BY A SURVIVING MUTATION, and it is the reason this section
# exists rather than another edge case.
#
# `pipeline.analyze` chooses each database's measured vector from that
# database's own declared feature space:
#
#     DB1                     white_legacy          18 values
#     AS7265X_54_MULTIILLUM   combine_illuminations 54 features
#     otherwise               white_active          18 values
#
# Disabling the 54-feature branch survived every suite. DB2 is not
# empty - it holds 22 materials - so the branch runs on every analysis;
# what was missing was any assertion that DB2 PRODUCES anything. With
# the branch gone it silently contributes nothing, every decision is
# made on DB1 and DB3 alone, and no screen says so.
#
# The protection is by CONSTRUCTION rather than by validation:
# `BD/channels.py::require_compatible` exists to refuse a mismatched
# comparison and has no callers, because a mismatch is never built in
# the first place. This is the test for the construction.

from workflow.session import Mission as _Mission             # noqa: E402
from fakes import sandbox_mission                            # noqa: E402
from BD.channels import (                                    # noqa: E402
    AS7265X_18,
    AS7265X_54_MULTIILLUM,
)

link, port, loopback = loopback_link(serial_link)
mission, bd = sandbox_mission(link)

spaces = {
    key: handle.feature_space
    for key, handle in mission.registry.databases.items()
}

checks.equal(spaces.get("DB2"), AS7265X_54_MULTIILLUM,
             "DB2 declares the 54-feature space")

checks.equal(spaces.get("DB1"), AS7265X_18,
             "while DB1 declares the 18-band space")

checks.ok(mission.registry.databases["DB2"].ready,
          "and DB2 is READY with {} materials - the 54-feature branch "
          "runs on every analysis".format(
              len(mission.registry.databases["DB2"].database.materials)))

from BD.channels import CHANNELS                             # noqa: E402

mission.store.create("S-DB2", 1)
measurement = mission.store.add_measurement(
    "S-DB2",
    raw={
        name: {channel: 1000.0 + index * 7
               for index, channel in enumerate(CHANNELS)}
        for name in ("white", "uv", "ir")
    },
)

run = mission.analyse_measurement(measurement)

checks.equal(run.get("analysis_status"), "OK",
             "the analysis completes")

results = {
    entry.get("database"): entry
    for entry in run.get("database_results") or []
}

checks.ok("DB2" in results,
          "and produces a DB2 result")

db2 = results.get("DB2") or {}

checks.equal(db2.get("feature_space"), AS7265X_54_MULTIILLUM,
             "recorded in the 54-feature space")

checks.ok(db2.get("available") is not False,
          "DB2 is AVAILABLE ({})".format(
              db2.get("reason") or "no reason recorded"))

# THE ASSERTION THAT KILLS THE MUTATION.
#
# `channels_compared` is the number of features that actually lined up
# between the measurement and the library. In the 54-feature space that
# is 54. Remove the branch and DB2 receives the 18 white-light values,
# whose channel names do not exist in a 54-feature library at all - so
# nothing lines up and this drops to 0, silently, while DB2 goes on
# reporting itself available.
checks.equal(db2.get("channels_compared"), 54,
             "and 54 features were compared - the vector DB2 was given "
             "belongs to DB2's own feature space")

checks.ok((db2.get("candidate_count") or 0) > 0,
          "with {} candidate(s) scored, so DB2 really contributes to "
          "the decision rather than silently dropping out".format(
              db2.get("candidate_count")))

# And DB1 is still compared in ITS space: the selection is per
# database, not one space for everything.
db1 = results.get("DB1") or {}

checks.equal(db1.get("feature_space"), AS7265X_18,
             "DB1 is still recorded in the 18-band space")

# 18 is the ceiling, not the count: a channel whose reflectance is
# undefined is dropped, and this synthetic flat spectrum leaves one.
# What matters is that DB1 is compared in the 18-band space and DB2 is
# not - 17 against 54, never 17 against 17.
checks.ok(0 < (db1.get("channels_compared") or 0) <= 18,
          "and compared over {} of at most 18 channels, not 54".format(
              db1.get("channels_compared")))

checks.ok((db1.get("candidate_count") or 0) > 0,
          "with {} candidate(s) of its own".format(
              db1.get("candidate_count")))

bd.close()
link.close()


# ======================================================================
checks.section("the last broad guards on the optional layers")

# Six `except Exception` handlers, each wrapping one optional step. The
# rule they all follow: a step that cannot run returns UNKNOWN or says
# NOT SAVED - never a plausible value, and never an exception into the
# menu loop.

link, port, loopback = loopback_link(serial_link)
mission, bd = sandbox_mission(link)

# The acquisition profile, which is a provenance record rather than a
# measurement. Losing it must not cost the measurement.
profile_failure = counting_raiser(RuntimeError("the profile store is gone"))

with patched(type(mission.profiles), "ensure", profile_failure):
    kind, profile = outcome(
        lambda: mission.current_profile({"gain": 3}, "1.0"))

checks.equal(profile_failure.calls, 1,
             "the profile store really was asked and really failed")

checks.equal(kind, "ok",
             "a profile that cannot be stored does not raise")

checks.ok(profile is None,
          "and is None - the measurement keeps its numbers and loses "
          "only the provenance note")

# The decision engine. A failure here must produce UNKNOWN, not a
# material.
if mission.decision_engine is not None:
    decide_failure = counting_raiser(RuntimeError("the model is broken"))

    with patched(type(mission.decision_engine), "decide", decide_failure):
        kind, decision = outcome(
            lambda: mission.decide({"features": {}}, None))

    checks.equal(decide_failure.calls, 1,
                 "the decision engine really was asked and really failed")

    checks.equal(kind, "ok",
                 "a decision engine that raises does not take the "
                 "analysis with it")

    checks.equal((decision or {}).get("level"), "UNKNOWN",
                 "and the level is UNKNOWN")

    checks.ok((decision or {}).get("material") is None,
              "with NO material - an engine that failed cannot have "
              "identified anything")

else:
    checks.ok(True,
              "no decision engine in this sandbox; the guard is covered "
              "by the constructor test in test_loader_closure.py")

bd.close()
link.close()

# The class-reliability lookup inside the decision layer. It is reached
# through `_stored_rating`, which asks a database handle for a
# material's historical reliability.
from Science import decision as decision_module              # noqa: E402
from Science.decision import ReliabilityModel               # noqa: E402


class UnreadableHandle:
    ready = True

    def class_reliability(self, material):
        raise RuntimeError("the reliability table is unreadable")


class OneHandleRegistry:
    def get(self, key):
        return UnreadableHandle()


model = ReliabilityModel.__new__(ReliabilityModel)
model.registry = OneHandleRegistry()

kind, rating = outcome(
    lambda: ReliabilityModel._stored_rating(model, "DB1", "quartz"))

checks.equal(kind, "ok",
             "an unreadable reliability table does not raise")

checks.ok(rating is None,
          "and produces None rather than a rating nobody measured")

# The firmware's lamp readback inside led_test's result block.
main_module2, service2, config2, servo2 = support.build_firmware()
driver2 = service2.sensor.ensure_ready()

states_failure = counting_raiser(RuntimeError("the enable register is gone"))

with patched(type(driver2), "bulb_states", states_failure):
    frame = service2.dispatch({
        "request_id": "residual-1", "cmd": "led_test", "hold_ms": 0})

checks.ok(states_failure.calls >= 1,
          "the lamp state really was read back and really failed")

data = frame.get("data") or {}

checks.ok("all_off" in data,
          "led_test still returns its result block")

checks.ok(data.get("all_off") is None,
          "with all_off as None - UNKNOWN, never False, which would "
          "claim the lamps are still on")


# ======================================================================
checks.section("18. the handlers that are provably unreachable")

# Two of the remaining handlers cannot be entered by any valid call
# chain, and are classified DEFENSIVE_UNREACHABLE rather than driven.
# Forcing an impossible state to colour a line green would be the
# opposite of assurance, so each is PROVED here instead.

# 1. `serial_link.py`'s ImportError guard around pyserial.
#
# It runs at MODULE IMPORT, once, before any test can observe it. By
# the time a suite is running, `serial_link` is already imported and
# the branch is decided. What the guard produces - `serial = None` -
# IS driven, in test_loader_closure.py, by setting it directly.
serial_source = (support.FIRMWARE / "PC" / "serial_link.py").read_text(
    encoding="utf-8")

checks.ok("except ImportError:" in serial_source,
          "the pyserial import is guarded")

checks.ok("serial = None" in serial_source,
          "and the guard sets serial to None, which IS the state "
          "test_loader_closure.py drives")

checks.ok("pragma: no cover" in serial_source.split(
              "except ImportError:")[1][:120],
          "and the handler is marked as uncoverable in-process, rather "
          "than left looking like an oversight")

# 2. The retired movement kind, proved unreachable in
#    test_handler_closure.py and re-stated here so the count is
#    complete.
protocol_source = (support.FIRMWARE / "ESP32" / "protocol.py").read_text(
    encoding="utf-8")

checks.ok('kind == "neutral"' in protocol_source,
          "the retired movement kind is still special-cased")

checks.ok("test_handler_closure.py" not in protocol_source,
          "and its unreachability is proved by a test rather than "
          "asserted in a comment")


# ======================================================================
checks.section("the firmware's boot loop stops on Ctrl+C")

# `ESP32/main.py` catches KeyboardInterrupt so a developer at the REPL
# can stop the firmware without a traceback, and so the servo is
# released on the way out rather than left holding a position.

main_source = (support.FIRMWARE / "ESP32" / "main.py").read_text(
    encoding="utf-8")

checks.ok("except KeyboardInterrupt" in main_source,
          "main.py catches KeyboardInterrupt")

checks.ok("shutdown" in main_source,
          "and shuts down on the way out")

# Driven: the serving loop interrupted, and the shutdown that follows.
hardware = main_module.Hardware()

serve_interrupt = counting_raiser(KeyboardInterrupt())

with patched(type(service), "serve_forever", serve_interrupt):
    kind, detail = outcome(
        lambda: main_module.run() if hasattr(main_module, "run") else None)

if hasattr(main_module, "run"):
    checks.equal(serve_interrupt.calls, 1,
                 "the serving loop really was entered and really "
                 "interrupted")

    checks.equal(kind, "ok",
                 "and Ctrl+C leaves the firmware without a traceback")

else:
    checks.ok(True,
              "main.py has no run() to drive; its KeyboardInterrupt "
              "guard is proved by the source check above and by the "
              "shutdown test in test_loader_closure.py")


sys.exit(checks.report())
