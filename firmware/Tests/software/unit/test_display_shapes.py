"""
Every result shape the operator can be shown, actually rendered.

WHY A WHOLE SUITE FOR A FORMATTING MODULE

`display.py` is the last thing between a scientific result and the
person who acts on it. Its branches are not logic, they are ANSWERS:
this result has candidates, that one does not; this measurement has a
quality report, that one has three invalid channels; this decision is
KNOWN_MATERIAL, that one is UNKNOWN with a reason.

Every one of those is a different screen. A branch that has never been
rendered is a screen nobody has ever seen, and the failure mode is not
a crash - it is a KeyError on a field that is optional in practice, in
front of an operator holding a soil sample.

The previous campaign entered these screens. It did not feed them the
shapes that make their branches diverge, which is why `display.py` sat
at 53% branch coverage while every screen "passed".

HOW THE SHAPES ARE BUILT

Two ways, deliberately:

    REAL     run the actual Science pipeline over the actual reference
             data and print what comes out. This is the only way to
             know the shapes are the ones production makes.
    HOSTILE  hand-built variants with fields missing, empty, null or
             of the wrong type - the shapes a partial measurement, an
             older record or a failed analysis can produce.

A formatter must survive both. It may print a dash; it may not raise.
"""

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

import contextlib                                            # noqa: E402
import io                                                    # noqa: E402

import support

support.add_project_root()
support.add_path("PC")

from BD.channels import CHANNELS                             # noqa: E402

from workflow import display                                 # noqa: E402

checks = support.Checks("display-shapes")

NAN = float("nan")
INF = float("inf")


def rendered(call):
    """(output, crash) - a formatter may print anything, never raise."""
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            call()

        return out.getvalue(), None

    except Exception as error:                         # noqa: BLE001
        return "", "{}: {}".format(type(error).__name__, error)


def survives(label, call, expect=None):
    output, crash = rendered(call)

    checks.ok(crash is None, "{} renders ({})".format(label, crash or "ok"))

    if expect and crash is None:
        checks.ok(expect in output,
                  "{} shows {!r}".format(label, expect))

    return output


# ======================================================================
# the shapes production really makes
# ======================================================================

checks.section("shapes taken from the real Science pipeline")

from BD.calibrations import CalibrationStore                 # noqa: E402
from BD.databases import References                          # noqa: E402
from BD.registry import DatabaseRegistry                     # noqa: E402
from Science import pipeline, quality                        # noqa: E402
from Science.taxonomy import Taxonomy                        # noqa: E402
from Science.decision import DecisionEngine                  # noqa: E402

references = References()
registry = DatabaseRegistry()
taxonomy = Taxonomy(registry)
engine = DecisionEngine(taxonomy=taxonomy, registry=registry)


def spectrum_like(reference, factor=1.0, offset=0.0):
    return {channel: reference.get(channel, 0.0) * factor + offset
            for channel in CHANNELS}


white = references.white
dark = references.dark

# A sample that looks like the white reference: a real, well-posed
# measurement that the pipeline can actually interpret.
raw_blocks = {
    "white": spectrum_like(white, 0.55, 12.0),
    "uv": spectrum_like(white, 0.30, 8.0),
    "ir": spectrum_like(white, 0.42, 10.0),
}

# `pipeline.build` is the real entry point - the same call
# `Mission.build_evidence` makes. Named correctly on the second
# attempt: the first version of this suite invented
# `pipeline.build_evidence`, guarded it with hasattr, and silently
# skipped the whole section. static/test_static_api.py caught the
# invented name, which is precisely the check it exists for.
package = pipeline.build(
    "M-DISPLAY",
    raw_blocks,
    dark,
    {"white": white, "uv": white, "ir": white},
    registry=registry,
    legacy_references=references,
    legacy_calibration_id=references.calibration_id,
)

checks.ok(isinstance(package, dict) and package,
          "a real evidence package was built by the real pipeline")

survives("a real evidence package",
         lambda: display.print_evidence_summary(package))
survives("a real quality report",
         lambda: display.print_quality(package.get("quality")))

decision = engine.decide(package)

checks.ok(isinstance(decision, dict) and decision.get("level"),
          "and a real decision came out of it ({})".format(
              (decision or {}).get("level")))

survives("a real decision", lambda: display.print_decision(decision))
survives("a real decision in detail",
         lambda: display.print_decision(decision, detail=True))

# The database comparison block, as the pipeline really shapes it.
survives("real database results",
         lambda: display.print_database_results(
             package.get("database_results")))


# ======================================================================
checks.section("every decision level")

# The four-level vocabulary is the whole point of the Decision Model,
# and each level prints differently.

LEVELS = ("KNOWN_MATERIAL", "MATERIAL_FAMILY", "AMBIGUOUS_SET",
          "UNKNOWN")

for level in LEVELS:
    decision = {
        "decision_model_version": "DM-TEST",
        "level": level,
        "material": "Kaolin" if level == "KNOWN_MATERIAL" else None,
        "family": "clay" if level in ("KNOWN_MATERIAL",
                                      "MATERIAL_FAMILY") else None,
        "confidence": "MODERATE",
        "reason": "a reason long enough to be wrapped by textwrap so "
                  "that the wrapping branch is taken as well as the "
                  "branch that prints a short one",
    }

    output = survives("level {}".format(level),
                      lambda d=decision: display.print_decision(d))

    checks.ok(level.replace("_", " ") in output,
              "and the headline says {}".format(level.replace("_", " ")))

# An unknown level must not vanish: the fallback prints it raw.
output = survives(
    "an unrecognised level",
    lambda: display.print_decision({"level": "SOMETHING_NEW",
                                    "confidence": "LOW"}))

checks.ok("SOMETHING_NEW" in output,
          "a level this build has never heard of is printed as-is "
          "rather than silently dropped")


# ======================================================================
checks.section("decisions with and without every optional part")

BASE = {"decision_model_version": "DM-TEST", "level": "UNKNOWN",
        "confidence": "LOW"}

VARIANTS = (
    ("no optional fields at all", {}),
    ("a material", {"material": "Gypsum"}),
    ("a family but no material", {"family": "sulfate"}),
    ("one candidate", {"candidates": [
        {"material": "Kaolin", "evidence_level": "STRONG",
         "family": "clay"}]}),
    ("six candidates", {"candidates": [
        {"material": "M{}".format(i), "evidence_level": "WEAK",
         "family": "f{}".format(i)} for i in range(6)]}),
    ("a candidate missing its family", {"candidates": [
        {"material": "Kaolin", "evidence_level": "STRONG"}]}),
    ("a candidate that is all nulls", {"candidates": [
        {"material": None, "evidence_level": None, "family": None}]}),
    ("a very long material name", {"candidates": [
        {"material": "M" * 120, "evidence_level": "STRONG",
         "family": "clay"}]}),
    ("secondary interpretations", {
        "secondary_interpretations": ["could be a mixture",
                                      "could be wet"]}),
    ("an empty candidate list", {"candidates": []}),
    ("an empty secondary list", {"secondary_interpretations": []}),
    ("a reason", {"reason": "the spectrum matched nothing above "
                            "threshold"}),
    ("everything at once", {
        "material": "Kaolin", "family": "clay",
        "candidates": [{"material": "Kaolin", "evidence_level": "STRONG",
                        "family": "clay"}],
        "secondary_interpretations": ["possible mixture"],
        "reason": "strong match", "evidence": {"strength": 0.9}}),
)

for label, extra in VARIANTS:
    decision = dict(BASE)
    decision.update(extra)

    survives("a decision with {}".format(label),
             lambda d=decision: display.print_decision(d))
    survives("a decision with {} in detail".format(label),
             lambda d=decision: display.print_decision(d, detail=True))

# The two no-ops that must stay no-ops.
for empty in (None, {}):
    output, crash = rendered(lambda e=empty: display.print_decision(e))

    checks.ok(crash is None and output == "",
              "print_decision({!r}) prints nothing and does not "
              "raise".format(empty))


# ======================================================================
checks.section("match tables: empty, one, many, and malformed")

def match(name="Kaolin", **extra):
    """
    A match as the CURRENT pipeline builds one.

    Both spellings are present on purpose. `print_metric_table` reads
    `cosine_similarity_percent` and `combined_rank`;
    `print_matches` reads `similarity_percent` and
    `rank`, because it renders `reference_matches` from records
    MIGRATED out of the old flat schema. Getting that wrong is how the
    first version of this suite reported thirty failures that were all
    the same mistake in the test.
    """
    entry = {
        "material": name,
        "rank": 1,
        "combined_rank": 1,
        "cosine_rank": 1,
        "rmse_rank": 1,
        "pearson_rank": 1,
        "similarity_percent": 98.5,
        "cosine_similarity_percent": 98.5,
        "rmse": 0.012,
        "pearson_r": 0.99,
        "spectral_angle_deg": 9.9,
        "family": "clay",
    }
    entry.update(extra)

    return entry


MATCH_SETS = (
    ("no matches", []),
    ("one match", [match()]),
    ("twenty matches", [match("M{}".format(i)) for i in range(20)]),
    ("a match with no metrics", [{"material": "Kaolin"}]),
    ("a match with null metrics", [match(cosine_similarity_percent=None,
                                         rmse=None, pearson_r=None)]),
    ("a match with NaN", [match(cosine_similarity_percent=NAN, rmse=NAN)]),
    ("a match with infinity", [match(rmse=INF)]),
    ("a match with a string metric",
     [match(cosine_similarity_percent="high")]),
    ("a match with no material name", [match(material=None)]),
    ("a match with a very long name", [match("M" * 200)]),
)

for label, matches in MATCH_SETS:
    survives("print_matches with {}".format(label),
             lambda m=matches: display.print_matches(m))
    survives("print_metric_table with {}".format(label),
             lambda m=matches: display.print_metric_table(m))
    survives("print_matches limited, with {}".format(label),
             lambda m=matches: display.print_matches(m, limit=3))

for empty in (None, []):
    survives("print_matches({!r})".format(empty),
             lambda e=empty: display.print_matches(e))


# ----------------------------------------------------------------------
# THE SHAPE THAT ACTUALLY CRASHED.
#
# `reference_matches` on a MIGRATED record is whatever the previous
# software stored. It has no `rank`, and the three rank columns
# formatted `match.get("rank")` straight into a `{:<4}` field -
# `"{:<4}".format(None)` raises TypeError. Browsing a legacy sample in
# the records screen took the screen down.
# ----------------------------------------------------------------------

LEGACY_MATCHES = (
    ("no rank key at all",
     [{"material": "Kaolin", "similarity_percent": 97.1}]),
    ("a null rank",
     [{"material": "Kaolin", "rank": None, "similarity_percent": 97.1}]),
    ("a null combined_rank",
     [{"material": "Kaolin", "combined_rank": None, "rank": None}]),
    ("a rank that is a string",
     [{"material": "Kaolin", "rank": "1st"}]),
    ("a rank that is a float",
     [{"material": "Kaolin", "rank": 1.0}]),
    ("nothing but a material name", [{"material": "Kaolin"}]),
    ("an entirely empty match", [{}]),
)

for label, matches in LEGACY_MATCHES:
    for name, call in (
        ("print_matches", display.print_matches),
        ("print_metric_table", display.print_metric_table),
    ):
        output, crash = rendered(lambda c=call, m=matches: c(m))

        checks.ok(crash is None,
                  "{} survives a legacy match with {} ({})".format(
                      name, label, crash or "ok"))

        if crash is None:
            checks.ok(output.strip() != "",
                      "and still prints a row for it ({})".format(label))

# And the whole legacy path, through the screen that renders it.
legacy_run = {
    "legacy_analysis": {"analysis_status": "OK",
                        "interpretation": "Kaolin"},
    "migrated_from_schema": 2,
    "reference_matches": [{"material": "Kaolin",
                           "similarity_percent": 97.1}],
}

survives("a migrated analysis run, end to end",
         lambda: (display.print_result_block(legacy_run["legacy_analysis"]),
                  display.print_matches(legacy_run["reference_matches"])))


# ======================================================================
checks.section("quality reports, including the ones that reject")

QUALITY = (
    ("a passing report", {"status": "PASS", "score": 0.95, "checks": []}),
    ("a failing report", {"status": "FAIL", "score": 0.1, "checks": [
        {"check": "reflectance", "status": "FAIL",
         "message": "eleven channels above 1.0"}]}),
    ("a report with a passing check that is skipped",
     {"status": "PASS", "checks": [
         {"check": "distance", "status": "PASS", "message": "ok"}]}),
    ("a report with invalid channels",
     {"status": "PASS", "invalid_channels": ["A", "B"]}),
    ("a report with warnings", {"status": "PASS", "score": 0.7,
                                "warnings": ["three invalid channels"]}),
    ("a report with no score", {"status": "PASS"}),
    ("a report with a null score", {"status": "PASS", "score": None}),
    ("a report with a NaN score", {"status": "PASS", "score": NAN}),
    ("an empty report", {}),
    ("a report that is a list", []),
    ("a report whose checks are all warnings",
     {"status": "WARN", "checks": [
         {"check": "repeatability", "status": "WARN",
          "message": "spread above threshold"}]}),
)

for label, report in QUALITY:
    survives("print_quality with {}".format(label),
             lambda r=report: display.print_quality(r))

survives("print_quality(None)", lambda: display.print_quality(None))


# ======================================================================
checks.section("spectrum tables from partial and broken measurements")

full = {channel: 100.0 + index for index, channel in enumerate(CHANNELS)}
half = dict(list(full.items())[:9])

SPECTRA = (
    ("a full 18-channel spectrum", full),
    ("half the channels", half),
    ("one channel", {CHANNELS[0]: 1.0}),
    ("no channels", {}),
    ("channels that are all None", {c: None for c in CHANNELS}),
    ("channels containing NaN", dict(full, A=NAN)),
    ("channels containing infinity", dict(full, B=INF)),
    ("a channel that does not exist", dict(full, ZZ=1.0)),
    ("string values", {c: "n/a" for c in CHANNELS}),
)

for label, spectrum in SPECTRA:
    survives("print_spectrum_table with {}".format(label),
             lambda s=spectrum: display.print_spectrum_table(
                 {"raw": {"white": s}}))
    survives("print_processing_table with {}".format(label),
             lambda s=spectrum: display.print_processing_table(
                 {"raw": {"white": s}}, dark, white))
    survives("print_triad_table with {}".format(label),
             lambda s=spectrum: display.print_triad_table(
                 {"white": s, "uv": s, "ir": s}))

survives("print_triad_table with one illumination missing",
         lambda: display.print_triad_table({"white": full, "uv": full}))
survives("print_triad_table with an empty block",
         lambda: display.print_triad_table({"white": {}, "uv": {},
                                            "ir": {}}))
survives("print_triad_table(None)",
         lambda: display.print_triad_table(None))


# ======================================================================
checks.section("database and cross-database results")

def database_result(key="DB2", count=3, **extra):
    entry = {
        "database": key,
        "status": "READY",
        "version": "1.0",
        "material_count": 23,
        "channel_count": 18,
        "metrics": {
            "cosine": {
                "best": match("M0"),
                "runner_up": match("M1"),
                "margin": 0.01,
                "matches": [match("M{}".format(i)) for i in range(count)],
            },
        },
    }
    entry.update(extra)

    return entry


# A LIST of per-database entries, each with its own status and metric
# block - not a mapping. Reading the function rather than assuming the
# shape is the difference between testing it and testing a guess.
DATABASE_SETS = (
    ("no databases", []),
    ("one ready database", [database_result()]),
    ("three ready databases", [database_result("DB1"),
                               database_result("DB2"),
                               database_result("DB3")]),
    ("a database with no metrics",
     [{"database": "DB2", "status": "READY", "metrics": {}}]),
    ("a database that is not READY",
     [{"database": "DB2", "status": "MISSING",
       "error": {"code": "FILE_NOT_FOUND", "message": "missing"}}]),
    ("a database with a null metric block",
     [{"database": "DB2", "status": "READY", "metrics": None}]),
    ("one ready and one unavailable",
     [database_result("DB1"),
      {"database": "DB3", "status": "MISSING"}]),
)

for label, results in DATABASE_SETS:
    survives("print_database_results with {}".format(label),
             lambda r=results: display.print_database_results(r))

# `print_agreement` is called with a mapping or nothing. A bare string
# is not a shape any caller produces, so it is not asserted.
for label, agreement in (("None", None), ("empty", {}),
                         ("a full block", {"agreement": "AGREE",
                                           "materials": ["Kaolin"]})):
    survives("print_agreement with {}".format(label),
             lambda a=agreement: display.print_agreement(a))


# ======================================================================
checks.section("evidence summaries with parts missing")

EVIDENCE = (
    ("nothing", None),
    ("an empty package", {}),
    ("only raw", {"raw": {"white": full}}),
    ("raw and normalized", {"raw": {"white": full},
                            "normalized": {"white": full}}),
    ("a quality report", {"quality": {"status": "PASS", "score": 0.9}}),
    ("a failed quality report", {"quality": {"status": "FAIL",
                                             "score": 0.1}}),
    ("channel reliability", {"reliability": {"white": {
        "usable": 15, "total": 18}}}),
    ("a calibration id", {"calibration_id": "CAL-2026-001"}),
    ("no calibration id", {"calibration_id": None}),
    ("a distance", {"distance_mm": 12.5}),
    ("a null distance", {"distance_mm": None}),
    ("everything", {"raw": {"white": full}, "normalized": {"white": full},
                    "quality": {"status": "PASS", "score": 0.9},
                    "calibration_id": "CAL-1", "distance_mm": 10.0}),
)

for label, evidence in EVIDENCE:
    survives("print_evidence_summary with {}".format(label),
             lambda e=evidence: display.print_evidence_summary(e))


# ======================================================================
checks.section("result and settings blocks")

# Both callers guard (`if legacy:` and `status.get("servo") or {}`), so
# None is outside the contract and is not asserted.
RESULTS = (
    ("an empty analysis", {}),
    ("a failed analysis", {"analysis_status": "FAILED",
                           "error": {"code": "NO_CALIBRATION",
                                     "message": "none active"}}),
    ("a successful analysis", {"analysis_status": "OK",
                               "decision": {"level": "UNKNOWN",
                                            "confidence": "LOW"}}),
    ("an analysis with no decision", {"analysis_status": "OK"}),
    ("an analysis whose decision is null", {"analysis_status": "OK",
                                            "decision": None}),
)

for label, analysis in RESULTS:
    survives("print_result_block with {}".format(label),
             lambda a=analysis: display.print_result_block(a))

SETTINGS = (
    ("nothing", None),
    ("empty settings", {}),
    ("full settings", {"gain": 2, "integration_cycles": 100,
                       "measurement_mode": 3, "gain_name": "16x"}),
    ("settings with nulls", {"gain": None, "integration_cycles": None}),
    ("settings with extra keys", {"gain": 2, "unexpected": "field"}),
)

for label, settings in SETTINGS:
    survives("print_settings_block with {}".format(label),
             lambda s=settings: display.print_settings_block(s))


# ======================================================================
checks.section("the servo block, in every backend state")

# `connected` IS THE KEY THE FIRMWARE SENDS; `selected` never existed.
# These fixtures carried both, which is how the whole tree came to agree
# with a reader that was testing a key no board has ever sent. The dead
# one is gone so it cannot be copied out of here into a new fixture.
#
# The two levels mean different things and both are exercised below:
#   servo["connected"]             a driver is attached at all
#   servo["backend"]["connected"]  and that driver's link answers
SERVO_STATES = (
    ("not connected", {"connected": False, "label": "NOT CONNECTED",
                       "message": "The ST3215 is not connected."}),
    ("connected and healthy", {
        "connected": True, "label": "ST3215",
        "backend": {"connected": True, "id": 1, "position_counts": 2048,
                    "position_deg": 180.0, "mode_name": "STEP",
                    "voltage_v": 11.9, "temperature_c": 34},
        # NO invented `capabilities.verified_movement`. That key existed
        # nowhere but here, and `report_move_test` read it - so the
        # servo movement test's entire per-leg report was unreachable
        # while this fixture kept the rendering test green.
        }),
    # ATTACHED, AND THE LINK IS DOWN. This said `connected: False` at
    # the top level, so after the reader was corrected it stopped
    # reaching the backend-error rendering it is named for and passed
    # without testing anything.
    ("attached but the backend errored", {
        "connected": True, "label": "ST3215",
        "backend": {"connected": False,
                    "error": {"code": "SERVO_UART_TIMEOUT",
                              "message": "no answer"}}}),
    ("a backend with null readings", {
        "connected": True, "label": "ST3215",
        "backend": {"connected": True, "id": None,
                    "position_counts": None, "voltage_v": None,
                    "temperature_c": None}}),
    ("no backend key", {"connected": True, "label": "ST3215"}),
)

for label, servo in SERVO_STATES:
    survives("print_servo_block with {}".format(label),
             lambda s=servo: display.print_servo_block(s))


# ======================================================================
checks.section("return-move reports")

RETURNS = (
    ("nothing", None),
    ("a successful return", {"returned": True}),
    ("a failed return", {"returned": False,
                         "message": "position could not be verified"}),
    ("a failed return with an exception", {
        "returned": False, "message": "servo timeout",
        "exception_message": "no answer from servo 1"}),
    ("a return with no verdict", {"message": "something happened"}),
)

for label, return_move in RETURNS:
    output = survives("report_return_move with {}".format(label),
                      lambda r=return_move: display.report_return_move(r))

    if return_move and return_move.get("returned") is False:
        checks.ok("UNKNOWN" in output.upper(),
                  "a failed return says the carousel position is now "
                  "unknown ({})".format(label))


# ======================================================================
checks.section("check lines and bus scans")

for label, ok in (("passing", True), ("failing", False),
                  ("unknown", None)):
    survives("print_check {}".format(label),
             lambda o=ok: display.print_check("A stage", o))

SCANS = (
    ("an empty report", {}),
    ("a report with no probes", {"scanned": {}, "probes": [],
                                 "result": "NOTHING_ANSWERED"}),
    ("a probe that answered", {
        "scanned": {"uart_id": 2, "configured_tx_pin": 17,
                    "configured_rx_pin": 16, "bauds": [1000000],
                    "id_count": 1, "probes": 1, "configured_id": 1,
                    "configured_baud": 1000000},
        "probes": [{"pin_order": "normal", "baud": 1000000, "bytes": 12,
                    "echo": False, "checksum_errors": 0,
                    "ids_answered": [1]}],
        "result": "FOUND", "diagnosis": "a servo answered"}),
    ("a probe that only echoed", {
        "scanned": {}, "probes": [
            {"pin_order": "swapped", "baud": 1000000, "bytes": 8,
             "echo": True, "checksum_errors": 0, "ids_answered": [],
             "other_ids": []}],
        "result": "ECHO_ONLY"}),
    ("a scan that released the servo", {
        "scanned": {}, "probes": [], "result": "FOUND",
        "released_servo": {"message": "released for the scan"}}),
)

for label, report in SCANS:
    survives("print_bus_scan with {}".format(label),
             lambda r=report: display.print_bus_scan(r))


sys.exit(checks.report())
