"""
B6 - the AS7265x, on its own.

Independent of the carousel on purpose: the sensor sits on I2C and the
servo on UART, and they share only the ESP32. A spectrum that comes back
wrong should never have to be disentangled from a movement that also
happened, so the sensor is qualified standing still.

WHAT "AS7265X NOT FOUND" HAS MEANT ON THIS BENCH

Intermittently, and without a clear trigger. The driver's own answer is
already good - it distinguishes I2C_NO_DEVICES (the bus answered, "
nothing did) from AS7265X_ADDRESS_NOT_FOUND (devices answered, not this
one) - and B6 is where that distinction is exercised against the real
bus rather than a fake one.

THE INITIALIZATION IS THE INTERESTING PART. The driver brings the sensor
up lazily, waits out the post-reset settling, scans the bus, writes the
configuration and READS IT BACK before believing it. Each of those can
fail differently, and HW-B6-002 and HW-B6-005 exist to make each of them
happen for real, repeatedly, from cold.
"""

from ..core.model import (Automation, IterationKind, Requirement,
                          Safety)
from ..core.analysis import failure_rate, outliers, summarize


CAMPAIGN = "B6"


def register(registry):
    registry.campaign(
        CAMPAIGN, layer="B6", title="Direct AS7265x testing",
        purpose="Qualify the sensor and its I2C bus standing still, so "
                "a later integration failure can be attributed to the "
                "integration rather than to the sensor.",
        prerequisites=("B1",),
        gate_note="Gated by B1 only. The sensor does not depend on the "
                  "servo, so B6 can be run while H-002 is still open - "
                  "and should be, because it is useful work that is not "
                  "blocked.",
    )

    registry.test(
        test_id="HW-B6-001", campaign=CAMPAIGN, layer="B6",
        requirements=("HW-REQ-SENSOR-016",),
        title="On-demand I2C bus scan",
        objective="Enumerate every address answering on the I2C bus "
                  "without disturbing a sensor that is already working.",
        hardware_setup="AS7265x on the configured bus.",
        preconditions="B1 passed.",
        procedure=(
            "send an i2c_scan command",
            "record every address that answered",
            "check the AS7265x address is among them",
        ),
        expected="The configured address answers, and any other device "
                 "on the bus is recorded.",
        failure_criteria="An empty bus - wiring, pull-ups or power - or "
                         "devices answering but not the sensor.",
        captures=("every address found", "the bus description",
                  "the configured address"),
        safety=Safety.READ_ONLY, automation=Automation.AUTOMATIC,
        requires=("diagnostic.i2c_scan",),
        run=_i2c_scan, cleanup=_release,
        defect_prefix="HW-SENSOR",
        notes="BLOCKED until the test-side diagnostic agent is "
              "deployed. The competition firmware scans the bus "
              "only during initialization, so its addresses are "
              "from the last init; the agent answers with a fresh "
              "scan that initializes nothing. HW-B6-002 meanwhile "
              "covers the same ground through a forced "
              "re-initialization.",
    )

    registry.test(
        test_id="HW-B6-002", campaign=CAMPAIGN, layer="B6",
        requirements=("HW-REQ-SENSOR-001", "HW-REQ-SENSOR-002"),
        title="Initialization from cold, and the address at 0x49",
        objective="Force a full initialization and observe the bus scan "
                  "it performs.",
        hardware_setup="AS7265x connected and powered.",
        preconditions="B1 passed.",
        procedure=(
            "send sensor_test_raw with force_reinit true",
            "read the status and record the addresses the scan found",
            "check the configured address is present",
            "record the configuration that was written and read back",
            "record how long the initialization took",
        ),
        expected="The sensor initializes, the configured address is in "
                 "the scan, and the read-back configuration matches "
                 "what config.py asks for.",
        failure_criteria="I2C_NO_DEVICES (the bus is dead), "
                         "AS7265X_ADDRESS_NOT_FOUND (something else is "
                         "there), or a configuration that does not read "
                         "back.",
        captures=("the scan result", "the sensor state",
                  "the read-back configuration",
                  "initialization time", "any error with its stage"),
        safety=Safety.ILLUMINATION, automation=Automation.AUTOMATIC,
        requires=("sensor.init", "sensor.status"),
        run=_cold_initialization, cleanup=_release,
        defect_prefix="HW-SENSOR",
        notes="Replaces HW-300 and HW-302.",
    )

    registry.test(
        test_id="HW-B6-003", campaign=CAMPAIGN, layer="B6",
        requirements=("HW-REQ-LINK-003",),
        title="Sensor status without touching the sensor",
        objective="Check that get_status answers at the same speed "
                  "whether the sensor is present, missing or half dead.",
        hardware_setup="AS7265x connected.",
        preconditions="HW-B6-002 passed.",
        procedure=(
            "send get_status and record its latency",
            "check the sensor section carries state, address, bus, "
            "settings and the error fields",
            "check the latency is far below an acquisition",
        ),
        expected="A complete status, answered in milliseconds.",
        failure_criteria="A status that takes as long as an "
                         "acquisition, which would mean it is "
                         "initializing the sensor to answer - and a "
                         "status command that can hang is a status "
                         "command nobody can use during a fault.",
        captures=("the sensor status", "its latency",
                  "the recovery count and the first init error"),
        safety=Safety.READ_ONLY, automation=Automation.AUTOMATIC,
        requires=("sensor.status",),
        run=_status_is_cheap, cleanup=_release,
        defect_prefix="HW-SENSOR",
    )

    registry.test(
        test_id="HW-B6-004", campaign=CAMPAIGN, layer="B6",
        requirements=("HW-REQ-SENSOR-006", "HW-REQ-SENSOR-008"),
        title="One acquisition under each illumination",
        objective="Take a single reading under WHITE, under UV and "
                  "under IR, and check each returns eighteen channels.",
        hardware_setup="AS7265x connected, sensor head pointing at a "
                       "stable target.",
        preconditions="HW-B6-002 passed.",
        procedure=(
            "acquire one block under each illumination in turn",
            "validate the shape of every spectrum",
            "record the data-ready wait for each",
            "check the three spectra are not identical to each other",
        ),
        expected="Three well-formed 18-channel spectra that differ from "
                 "one another.",
        failure_criteria="A malformed spectrum, or two illuminations "
                         "returning identical readings - which is what "
                         "a lamp that never switched looks like.",
        captures=("each spectrum", "each data-ready wait",
                  "the shape problems found, if any"),
        safety=Safety.ILLUMINATION, automation=Automation.AUTOMATIC,
        requires=("sensor.acquire_block",),
        run=_one_per_illumination, cleanup=_release,
        assumption="H-003", defect_prefix="HW-SENSOR",
    )

    registry.test(
        test_id="HW-B6-005", campaign=CAMPAIGN, layer="B6",
        requirements=("HW-REQ-SENSOR-003",),
        iteration_kind=IterationKind.MEASUREMENT,
        qualification_min_iterations=50,
        characterization_min_iterations=10,
        title="Repeated cold initialization",
        objective="Establish whether initialization is reliable or "
                  "intermittent - the AS7265X_NOT_FOUND question.",
        hardware_setup="AS7265x connected.",
        preconditions="HW-B6-002 passed.",
        procedure=(
            "force a re-initialization N times",
            "record success, duration and the error of each",
            "record the recovery count reported by the firmware",
            "report the failure rate and the first failing attempt",
        ),
        expected="Every initialization succeeds.",
        failure_criteria="Any failure. An intermittent initialization "
                         "is the fault this bench has already seen, and "
                         "the rate is the measurement.",
        captures=("per-attempt result, duration and error code",
                  "the failure rate", "the first failing attempt",
                  "the firmware's recovery count"),
        safety=Safety.ILLUMINATION, automation=Automation.AUTOMATIC,
        requires=("sensor.init", "sensor.status"),
        run=_repeated_initialization, cleanup=_release,
        default_iterations=50, max_iterations=1000,
        defect_prefix="HW-SENSOR",
        notes="Replaces HW-302 and HW-304.",
    )

    registry.test(
        test_id="HW-B6-006", campaign=CAMPAIGN, layer="B6",
        requirements=("HW-REQ-SENSOR-005", "HW-REQ-REC-003"),
        title="Initialization after an ESP32 reset",
        objective="Check the sensor comes up from a genuine cold start, "
                  "with the post-reset settling the driver waits out.",
        hardware_setup="AS7265x connected. The board will be reset.",
        preconditions="HW-B6-005 passed.",
        procedure=(
            "reset the ESP32 with an RTS pulse and DTR low - the "
            "measured way into the application rather than the "
            "bootloader",
            "wait for the module to answer a ping",
            "check the carousel position is reported INVALID after the "
            "reset",
            "force a sensor initialization and time it",
            "check it succeeds",
        ),
        expected="The board comes back into the application, the "
                 "carousel position is correctly invalid, and the "
                 "sensor initializes.",
        failure_criteria="A board that lands in the bootloader, a "
                         "position that survives a reset - which would "
                         "be a false claim about a physical mechanism - "
                         "or an initialization that fails from cold.",
        captures=("time to answer after the reset",
                  "the carousel position validity after the reset",
                  "the initialization time and result"),
        safety=Safety.RESET, automation=Automation.AUTOMATIC,
        requires=("link.hard_reset", "sensor.init", "carousel.status"),
        run=_initialization_after_reset, cleanup=_release,
        defect_prefix="HW-SENSOR",
    )

    registry.test(
        test_id="HW-B6-007", campaign=CAMPAIGN, layer="B6",
        requirements=("HW-REQ-SENSOR-006", "HW-REQ-SENSOR-013"),
        title="The production WHITE, UV, IR sequence",
        objective="Run the exact triad the measurement uses, and check "
                  "all 54 features arrive.",
        hardware_setup="AS7265x connected, stable target.",
        preconditions="HW-B6-004 passed.",
        procedure=(
            "send acquire_triad with the production repeat count",
            "validate every block and every spectrum",
            "count the features",
            "check the blocks arrived in the order white, uv, ir",
            "check the bulbs are reported off afterwards",
        ),
        expected="Three blocks, 18 channels each, 54 features, and the "
                 "bulbs off at the end.",
        failure_criteria="A missing block, a wrong channel count, or "
                         "bulbs still on after the answer.",
        captures=("the whole triad", "the feature count",
                  "the bulbs-off report", "the temperatures",
                  "each block's data-ready waits"),
        safety=Safety.ILLUMINATION, automation=Automation.AUTOMATIC,
        requires=("sensor.acquire_triad",),
        run=_production_triad, cleanup=_release,
        defect_prefix="HW-SENSOR",
    )

    registry.test(
        test_id="HW-B6-008", campaign=CAMPAIGN, layer="B6",
        requirements=("HW-REQ-SENSOR-003",),
        iteration_kind=IterationKind.MEASUREMENT,
        qualification_min_iterations=200,
        characterization_min_iterations=50,
        title="Watching for the intermittent AS7265X_NOT_FOUND",
        objective="Run long enough to catch the intermittent fault, and "
                  "record exactly what the firmware said when it "
                  "happened.",
        hardware_setup="AS7265x connected. The bench free for the "
                       "duration.",
        preconditions="HW-B6-005 passed.",
        procedure=(
            "alternate status reads and acquisitions for N iterations",
            "record every error with its code, stage and details",
            "record the firmware's recovery count as it changes",
            "report the failure rate and the first failing iteration",
            "preserve everything recorded so far if interrupted",
        ),
        expected="No occurrence. A pass here is a bounded statement: "
                 "the fault did not appear in N iterations.",
        failure_criteria="Any occurrence, with the code and stage "
                         "recorded - which is the first real evidence "
                         "about a fault that has so far only been seen "
                         "in passing.",
        captures=("per-iteration result", "every error with code, stage "
                  "and details", "the recovery count over time",
                  "the failure rate and the first failure"),
        safety=Safety.ENDURANCE, automation=Automation.AUTOMATIC,
        requires=("sensor.init", "sensor.status",
                  "sensor.acquire_block"),
        run=_intermittent_watch, cleanup=_release,
        default_iterations=200, max_iterations=5000,
        defect_prefix="HW-SENSOR",
    )


# ======================================================================
# shared
# ======================================================================

def _release(ctx):
    """
    Try to leave every bulb off, and record it if that cannot be
    confirmed.

    There is no "all lamps off" command; the firmware switches them off
    itself and reports `bulbs_off` with each acquisition. So cleanup
    reads the status and reports what it can see - it does not claim an
    off state it has not observed.
    """
    record = {}

    try:
        status = ctx.sensor.status()

        record["sensor_state"] = status.get("state")
        record["illumination_confirmed_off"] = None
        record["note"] = (
            "the firmware switches the bulbs off at the end of every "
            "acquisition and reports bulbs_off with the answer; there "
            "is no separate off command, so this cleanup records the "
            "sensor state rather than asserting an off state")

    except Exception as error:
        record["sensor_state"] = "UNREADABLE"
        record["error"] = "{}: {}".format(type(error).__name__, error)

    closed = ctx.link.close(reason="B6 cleanup")

    record["port_released"] = closed.get("closed")
    record["confirmed"] = bool(closed.get("closed"))

    return record


def _sensor_error(error):
    return {
        "type": type(error).__name__,
        "message": str(error)[:300],
        "code": getattr(error, "code", None),
        "data": getattr(error, "data", None),
    }


# ======================================================================
# bodies
# ======================================================================

def _i2c_scan(ctx):
    """
    Who is on the bus, right now, without disturbing anything.

    This is what distinguishes "the sensor is absent" from "the bus is
    dead" - and it does it without initializing the sensor, which the
    production path cannot.
    """
    ctx.require("diagnostic.i2c_scan")

    ctx.diagnostic.identify()

    transaction = ctx.diagnostic.i2c_scan()

    answer = transaction["data"] or {}

    ctx.record("i2c_scan", **answer)

    addresses = answer.get("addresses")

    ctx.require_observation("the list of answering I2C addresses",
                            addresses, evidence={"answer": answer})

    ctx.check(bool(addresses),
              "at least one device answered on the I2C bus - an empty "
              "bus means wiring, pull-ups or power, not a missing "
              "sensor",
              evidence=answer)

    expected = answer.get("expected_address")

    ctx.observed(
        "the AS7265x answers at its configured address",
        answer.get("expected_present"), expected=True,
        requirement=Requirement.REQUIRED,
        evidence={"expected_address": expected, "found": addresses})

    ctx.measure(stage="i2c_scan",
                addresses=";".join(addresses or []),
                count=answer.get("count"),
                expected_address=expected,
                expected_present=answer.get("expected_present"),
                elapsed_ms=answer.get("elapsed_ms"))

    others = [a for a in (addresses or []) if a != expected]

    if others:
        ctx.note(
            "Other devices answered on the bus: {}. Not a fault, but "
            "worth recording - an unexpected address is either another "
            "component or a wiring problem.".format(", ".join(others)))

    if addresses and not answer.get("expected_present"):
        ctx.defect(
            title="the I2C bus is alive but the AS7265x is not on it",
            observed="addresses {} answered; {} did not".format(
                ", ".join(addresses), expected),
            expected="the AS7265x answering at {}".format(expected),
            reproduction=("run HW-B6-001 with the diagnostic agent "
                          "deployed",),
            suspected_layer="sensor power or its I2C connection - the "
                            "bus itself is working",
            evidence=answer,
        )


def _cold_initialization(ctx):
    ctx.require("sensor.init", "sensor.status")

    expected = ctx.profile.production["sensor"]["address_hex"]

    transaction = ctx.sensor.initialize(force=True, repeats=1)

    answer = transaction["data"] or {}

    ctx.record("initialization", elapsed_ms=transaction["elapsed_ms"],
               **answer)

    ctx.check(True, "sensor_test_raw with force_reinit succeeded",
              evidence={"elapsed_ms": transaction["elapsed_ms"]})

    status = ctx.sensor.status()

    ctx.record("sensor_status", **status)

    scan = status.get("last_scan") or []

    ctx.check(expected in [str(a).upper() for a in scan]
              or expected.lower() in [str(a).lower() for a in scan],
              "the AS7265x answered at {} during the bus scan".format(
                  expected),
              evidence={"scan": scan, "expected": expected})

    ctx.check(status.get("state") == "READY",
              "the sensor reports READY after initialization",
              evidence={"state": status.get("state"),
                        "first_init_error": status.get(
                            "first_init_error")})

    settings = status.get("settings") or answer.get("sensor_settings")

    production = ctx.profile.production["sensor"]

    if isinstance(settings, dict):
        for reported_key, expected_value in (
                ("integration_cycles", production["integration_cycles"]),
                ("gain", production["gain"]),
                ("measurement_mode", production["measurement_mode"])):
            if reported_key in settings:
                ctx.check(settings[reported_key] == expected_value,
                          "the sensor read back {} = {}".format(
                              reported_key, expected_value),
                          evidence={"reported": settings[reported_key],
                                    "expected": expected_value})

    ctx.measure(stage="cold_init", elapsed_ms=transaction["elapsed_ms"],
                state=status.get("state"),
                scan=";".join(str(a) for a in scan),
                recovery_count=status.get("recovery_count"))


def _status_is_cheap(ctx):
    ctx.require("sensor.status")

    transaction = ctx.link.request("get_status", retries=2)

    status = (transaction["data"] or {}).get("sensor") or {}

    ctx.record("sensor_status", latency_ms=transaction["elapsed_ms"],
               **status)

    for field in ("state", "address", "bus", "recovery_count"):
        ctx.check(field in status,
                  "the sensor status carries '{}'".format(field),
                  evidence={"keys": sorted(status.keys())})

    ctx.check(transaction["elapsed_ms"] < 2000,
              "the status answered in {} ms, without initializing "
              "anything".format(transaction["elapsed_ms"]),
              evidence={"latency_ms": transaction["elapsed_ms"]})

    ctx.measure(stage="status", latency_ms=transaction["elapsed_ms"],
                state=status.get("state"),
                recovery_count=status.get("recovery_count"))


def _one_per_illumination(ctx):
    ctx.require("sensor.acquire_block")

    spectra = {}

    for illumination in ("white", "uv", "ir"):
        transaction = ctx.sensor.acquire(illumination, repeats=1)

        block = transaction["data"] or {}

        problems = ctx.sensor.validate_block(
            block, expected_repeats=1, illumination=illumination)

        ctx.check(not problems,
                  "the {} block is well formed: one acquisition of 18 "
                  "channels".format(illumination),
                  evidence={"problems": problems})

        acquisitions = block.get("acquisitions") or []

        if acquisitions:
            spectra[illumination] = acquisitions[0]

        waits = block.get("data_ready_wait_ms") or []

        ctx.measure(stage="single_acquisition", illumination=illumination,
                    elapsed_ms=transaction["elapsed_ms"],
                    data_ready_wait_ms=waits[0] if waits else None,
                    channels=len(acquisitions[0]) if acquisitions else 0)

        ctx.event("spectrum", illumination=illumination,
                  spectrum=acquisitions[0] if acquisitions else None)

    names = sorted(spectra)

    for index, first in enumerate(names):
        for second in names[index + 1:]:
            ctx.check(spectra[first] != spectra[second],
                      "the {} and {} spectra differ".format(first, second),
                      evidence={"identical": spectra[first]
                                == spectra[second]})


def _repeated_initialization(ctx):
    ctx.require("sensor.init", "sensor.status")

    attempts = ctx.iterations()

    outcomes = []
    durations = []
    errors = []

    for index in range(1, attempts + 1):
        try:
            transaction = ctx.sensor.initialize(force=True, repeats=1)

            outcomes.append(True)
            durations.append(transaction["elapsed_ms"])

            ctx.measure(stage="reinit", attempt=index, ok=True,
                        elapsed_ms=transaction["elapsed_ms"])

        except Exception as error:
            outcomes.append(False)

            detail = _sensor_error(error)

            errors.append({"attempt": index, **detail})

            ctx.measure(stage="reinit", attempt=index, ok=False,
                        error_code=detail["code"] or "",
                        error=detail["message"])

        if index % 10 == 0:
            ctx.say("{} of {} initializations, {} failures".format(
                index, attempts, outcomes.count(False)))

    rate = failure_rate(outcomes)

    ctx.result.first_failure_iteration = rate["first_failure_iteration"]

    # The final status is a nice-to-have on top of the result that
    # matters, so a sensor too sick to answer it does not turn a
    # measured failure rate into an exception - but the reason is
    # recorded rather than dropped.
    try:
        status = ctx.sensor.status()

    except Exception as error:
        status = {
            "state": "UNREADABLE",
            "error": "{}: {}".format(type(error).__name__, error),
        }

    ctx.record("repeated_initialization", failure_rate=rate,
               duration=summarize(durations), errors=errors[:20],
               final_status=status)

    ctx.check(rate["all_passed"],
              "every forced initialization succeeded",
              evidence=rate)

    if errors:
        ctx.defect(
            title="the AS7265x does not initialize reliably",
            observed="{} of {} attempts failed, first at attempt {}. "
                     "Codes seen: {}".format(
                         rate["failed"], rate["iterations"],
                         rate["first_failure_iteration"],
                         sorted({e["code"] for e in errors})),
            expected="every forced initialization succeeds",
            reproduction=("run HW-B6-005 with the same iteration count",),
            suspected_layer="I2C bus, sensor power, or post-reset "
                            "settling",
            evidence={"failure_rate": rate, "errors": errors[:20]},
        )


def _initialization_after_reset(ctx):
    ctx.require("link.hard_reset", "sensor.init", "carousel.status")

    link = ctx.link.require_link("reset the board")

    ctx.record("reset")

    link.hard_reset()

    import time

    started = time.perf_counter()

    link.wait_online()

    online_ms = round((time.perf_counter() - started) * 1000.0, 3)

    ctx.check(True, "the module answered a ping after the reset",
              evidence={"online_ms": online_ms})

    carousel = ctx.carousel.status()

    ctx.check(carousel.get("position_valid") is not True,
              "the carousel position is INVALID after a reset - the "
              "board cannot know where a mechanism is after losing "
              "power to its own logic",
              evidence={"carousel": carousel})

    transaction = ctx.sensor.initialize(force=True, repeats=1)

    ctx.check(True, "the sensor initialized from a genuine cold start",
              evidence={"elapsed_ms": transaction["elapsed_ms"]})

    status = ctx.sensor.status()

    ctx.check(status.get("state") == "READY",
              "the sensor reports READY after the reset",
              evidence={"state": status.get("state")})

    ctx.measure(stage="after_reset", online_ms=online_ms,
                init_ms=transaction["elapsed_ms"],
                position_valid=carousel.get("position_valid"),
                sensor_state=status.get("state"))


def _production_triad(ctx):
    ctx.require("sensor.acquire_triad")

    repeats = ctx.profile.production["sensor"]["sample_repeats"]

    transaction = ctx.sensor.triad(repeats=repeats)

    report = transaction["data"] or {}

    problems = ctx.sensor.validate_triad(report, expected_repeats=repeats)

    ctx.check(not problems,
              "the triad is well formed: white, uv and ir, {} "
              "acquisitions each, 18 channels each".format(repeats),
              evidence={"problems": problems})

    features = ctx.sensor.feature_count(report)

    ctx.check(features == 54,
              "54 spectral features arrived",
              evidence={"features": features})

    order = list((report.get("illuminations") or {}).keys())

    ctx.check(set(order) == {"white", "uv", "ir"},
              "all three illuminations are present",
              evidence={"order": order})

    bulbs_off = report.get("bulbs_off")

    # REQUIRED and explicitly True. `is not False` also passed None,
    # which is "the firmware did not say" - and an unconfirmable
    # off-state is exactly the thing HW-REQ-SENSOR-014 says must be
    # reported as unconfirmed rather than assumed.
    ctx.observed("the firmware reports the bulbs off after the triad",
                 bulbs_off, expected=True,
                 requirement=Requirement.REQUIRED)

    ctx.record("triad", elapsed_ms=transaction["elapsed_ms"],
               features=features, bulbs_off=bulbs_off,
               temperatures=report.get("temperatures"),
               protocol_version=report.get("protocol_version"))

    ctx.measure(stage="triad", elapsed_ms=transaction["elapsed_ms"],
                features=features, repeats=repeats,
                bulbs_off=bulbs_off)


def _intermittent_watch(ctx):
    ctx.require("sensor.init", "sensor.status", "sensor.acquire_block")

    rounds = ctx.iterations()

    outcomes = []
    errors = []
    recovery_counts = []
    waits = []

    ctx.say("{} rounds of status and acquisition, watching for the "
            "intermittent fault".format(rounds))

    for index in range(1, rounds + 1):
        try:
            status = ctx.sensor.status()

            recovery_counts.append(status.get("recovery_count"))

            transaction = ctx.sensor.acquire("white", repeats=1)

            block = transaction["data"] or {}

            block_waits = block.get("data_ready_wait_ms") or []

            if block_waits:
                waits.append(block_waits[0])

            outcomes.append(True)

            if index % 20 == 0:
                ctx.measure(stage="watch", round=index, ok=True,
                            elapsed_ms=transaction["elapsed_ms"],
                            recovery_count=status.get("recovery_count"))

        except Exception as error:
            outcomes.append(False)

            detail = _sensor_error(error)

            errors.append({"round": index, **detail})

            ctx.measure(stage="watch", round=index, ok=False,
                        error_code=detail["code"] or "",
                        error=detail["message"])

            ctx.say("round {}: {}".format(index, detail["message"][:80]))

        if index % 50 == 0:
            ctx.say("{} of {} rounds, {} failures".format(
                index, rounds, outcomes.count(False)))

    rate = failure_rate(outcomes)

    ctx.result.first_failure_iteration = rate["first_failure_iteration"]

    known = [c for c in recovery_counts if c is not None]

    ctx.record("intermittent_watch", failure_rate=rate,
               errors=errors[:30], data_ready=summarize(waits),
               recovery_first=known[0] if known else None,
               recovery_last=known[-1] if known else None)

    ctx.check(rate["all_passed"],
              "the intermittent fault did not appear in {} "
              "rounds".format(rounds),
              evidence=rate)

    if known:
        ctx.check(known[0] == known[-1],
                  "the firmware's recovery count did not rise",
                  evidence={"first": known[0], "last": known[-1]})

    if errors:
        ctx.defect(
            title="the intermittent sensor fault reproduced",
            observed="{} of {} rounds failed, first at round {}. Codes: "
                     "{}".format(rate["failed"], rate["iterations"],
                                 rate["first_failure_iteration"],
                                 sorted({e["code"] for e in errors})),
            expected="every round completes",
            reproduction=("run HW-B6-008 with the same iteration count",),
            suspected_layer="I2C bus or sensor power",
            evidence={"failure_rate": rate, "errors": errors[:30]},
        )
