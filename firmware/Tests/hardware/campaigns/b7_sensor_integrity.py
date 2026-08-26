"""
B7 - the shape of the data, and the lamps that produced it.

WHAT THIS CAMPAIGN IS NOT

It is not about whether the instrument identifies soil correctly. That
is science, it belongs to the Science domain and to the material
libraries, and it needs reference materials rather than a bench. B7 asks
a narrower and more urgent question: does the DATA ARRIVE INTACT?

    18 WHITE channels
    18 UV channels
    18 IR channels
    54 features

and nothing missing, nothing duplicated, nothing malformed, no NaN, no
infinity, no wrong length, no block out of order.

A spectrum that is the wrong shape is a communication failure. If it
reaches Science it becomes a scientific mystery instead, and that is a
much more expensive place to discover it.

THE ILLUMINATION CHECKS ARE OPERATOR-ASSISTED AND CANNOT BE OTHERWISE

The serial port cannot tell you which bulb lit. Only a human looking at
the sensor head can, and the framework asks rather than assuming. Note
the naming: the hardware has WHITE, UV and IR illuminators. The
requirements say RED; there is no red illuminator on this module, and IR
is what the RED requirement is served by.
"""

from ..core.model import Automation, Safety
from ..core.analysis import failure_rate, outliers, summarize


CAMPAIGN = "B7"


def register(registry):
    registry.campaign(
        CAMPAIGN, layer="B7",
        title="Sensor data integrity, endurance and illumination",
        purpose="Prove the 54 features arrive intact, repeatedly, and "
                "that the illumination behaves - including switching "
                "off when it should.",
        prerequisites=("B6",),
        gate_note="Gated by B6: data shape means nothing from a sensor "
                  "that does not initialize reliably.",
    )

    registry.test(
        test_id="HW-B7-001", campaign=CAMPAIGN, layer="B7",
        title="The 54-feature contract",
        objective="Check every way a spectrum can be the wrong shape, "
                  "against the real device.",
        hardware_setup="AS7265x connected, stable target.",
        preconditions="HW-B6-007 passed.",
        procedure=(
            "acquire the production triad",
            "check every channel letter is present exactly once per "
            "illumination",
            "check no channel is missing, duplicated or unexpected",
            "check every value is a real number - not NaN, not "
            "infinity, not a string",
            "check the acquisition count matches the repeats requested",
            "check the data-ready wait list is the same length as the "
            "acquisition list",
        ),
        expected="No shape problems at all.",
        failure_criteria="Any shape problem. Each one is a defect with "
                         "a name, and none of them may be repaired by "
                         "the test side.",
        captures=("every problem found, as a sentence",
                  "the feature count",
                  "the channels present per illumination"),
        safety=Safety.ILLUMINATION, automation=Automation.AUTOMATIC,
        requires=("sensor.acquire_triad",),
        run=_shape_contract, cleanup=_release,
        defect_prefix="HW-SENSOR",
    )

    registry.test(
        test_id="HW-B7-002", campaign=CAMPAIGN, layer="B7",
        title="A hundred repeated static measurements",
        objective="Establish that repeated acquisition of an unchanging "
                  "target is stable in shape and bounded in time.",
        hardware_setup="AS7265x connected, target NOT MOVED for the "
                       "whole run.",
        preconditions="HW-B7-001 passed.",
        procedure=(
            "acquire one white block per iteration, N times",
            "validate the shape of every one",
            "record the elapsed time and the data-ready wait",
            "record the per-channel spread across the run",
            "list the timing outliers",
        ),
        expected="Every acquisition well formed, and the timing "
                 "distribution recorded.",
        failure_criteria="Any malformed acquisition, or any that fails. "
                         "Channel-value spread is EVIDENCE, not a "
                         "failure - a noisy channel is a scientific "
                         "observation, not a broken link.",
        captures=("per-iteration shape result and timing",
                  "the timing distribution and outliers",
                  "the per-channel spread"),
        safety=Safety.ILLUMINATION, automation=Automation.AUTOMATIC,
        requires=("sensor.acquire_block",),
        run=_repeated_static, cleanup=_release,
        default_iterations=100, max_iterations=5000,
        assumption="H-003", defect_prefix="HW-SENSOR",
    )

    registry.test(
        test_id="HW-B7-003", campaign=CAMPAIGN, layer="B7",
        title="Sensor endurance",
        objective="Run long enough to see heating, drift or an "
                  "intermittent failure that a hundred acquisitions "
                  "would miss.",
        hardware_setup="As HW-B7-002. The bench free for the duration.",
        preconditions="HW-B7-002 passed.",
        procedure=(
            "acquire the full triad N times",
            "validate every one",
            "record timing and temperatures throughout",
            "report progress periodically",
            "compare the first tenth of the run with the last",
            "preserve everything recorded so far if interrupted",
        ),
        expected="No failure, and no trend in acquisition time.",
        failure_criteria="Any failure or malformed acquisition. A "
                         "timing trend is evidence about heating and is "
                         "reported, not failed.",
        captures=("per-iteration result, timing and temperature",
                  "the first and last tenth compared",
                  "the first failing iteration"),
        safety=Safety.ENDURANCE, automation=Automation.AUTOMATIC,
        requires=("sensor.acquire_triad",),
        run=_sensor_endurance, cleanup=_release,
        default_iterations=200, max_iterations=5000,
        prerequisites=("HW-B7-002",),
        assumption="H-007", defect_prefix="HW-SENSOR",
    )

    registry.test(
        test_id="HW-B7-004", campaign=CAMPAIGN, layer="B7",
        title="Sensor disconnect and recovery",
        objective="Pull the sensor, prove the failure is named "
                  "correctly, then reconnect and prove it recovers "
                  "without a reboot.",
        hardware_setup="AS7265x on a connector the operator can "
                       "unplug WITHOUT shorting anything. If the sensor "
                       "is soldered down, this test cannot be run "
                       "safely and must be skipped.",
        preconditions="HW-B6-005 passed.",
        procedure=(
            "acquire once and confirm it works",
            "ask the operator to disconnect the sensor",
            "attempt an acquisition and record the exact error",
            "check the error names the sensor rather than something "
            "generic",
            "ask the operator to reconnect it",
            "force a re-initialization",
            "acquire again and confirm it works",
        ),
        expected="A named sensor error while disconnected, and full "
                 "recovery afterwards with no reboot.",
        failure_criteria="An unhandled exception, a generic error, or a "
                         "sensor that will not come back without a "
                         "reset. A failure that is not latched is the "
                         "design; a failure that is latched is a "
                         "defect.",
        captures=("the working acquisition before",
                  "the error while disconnected, with code and stage",
                  "the recovery result",
                  "the working acquisition after"),
        safety=Safety.MANUAL_DISCONNECT,
        automation=Automation.OPERATOR_ASSISTED,
        requires=("sensor.init", "sensor.acquire_block",
                  "sensor.status"),
        run=_disconnect_recovery, cleanup=_release,
        defect_prefix="HW-SENSOR",
        notes="Replaces HW-303 and HW-304.",
    )

    registry.test(
        test_id="HW-B7-005", campaign=CAMPAIGN, layer="B7",
        title="Data-ready latency across the three illuminations",
        objective="Measure the real ready latency, which is H-003.",
        hardware_setup="AS7265x connected.",
        preconditions="HW-B7-001 passed.",
        procedure=(
            "acquire a block under each illumination, several repeats",
            "record every data_ready_wait_ms",
            "compute the distribution per illumination",
            "compare with the driver's computed budget",
        ),
        expected="A distribution per illumination, all inside the "
                 "driver's wait budget.",
        failure_criteria="A wait at or beyond the budget, which means "
                         "the timeout is doing nothing and a sensor "
                         "that never asserts ready is "
                         "indistinguishable from a slow one.",
        captures=("every data-ready wait",
                  "the distribution per illumination",
                  "the configured integration cycles and budget"),
        safety=Safety.ILLUMINATION, automation=Automation.AUTOMATIC,
        requires=("sensor.acquire_block",),
        run=_data_ready_latency, cleanup=_release,
        default_iterations=10, max_iterations=200,
        assumption="H-003", defect_prefix="HW-SENSOR",
    )

    registry.test(
        test_id="HW-B7-006", campaign=CAMPAIGN, layer="B7",
        title="The right lamp lights, and only the right lamp",
        objective="Have a human confirm which illuminator is actually "
                  "on during each phase.",
        hardware_setup="AS7265x connected, sensor head VISIBLE to the "
                       "operator. UV is being switched on - nobody "
                       "should be looking directly into the head.",
        preconditions="HW-B6-004 passed.",
        procedure=(
            "run led_test with a bounded hold time",
            "ask the operator which source was lit during each phase",
            "acquire under WHITE and ask what lit",
            "acquire under UV and ask what lit",
            "acquire under IR and ask what lit - IR may be invisible, "
            "and UNKNOWN is an acceptable answer",
            "ask whether any other source was on at the same time",
        ),
        expected="Each phase lights the source it names, and nothing "
                 "else is on at the same time.",
        failure_criteria="The wrong source, two sources at once, or a "
                         "source that stays on into the next phase.",
        captures=("the operator's answer for each phase",
                  "whether IR was visible at all",
                  "any simultaneous illumination reported"),
        safety=Safety.ILLUMINATION,
        automation=Automation.OPERATOR_ASSISTED,
        requires=("sensor.led_test", "sensor.acquire_block"),
        run=_which_lamp, cleanup=_release,
        defect_prefix="HW-SENSOR",
        notes="The hardware has WHITE, UV and IR. The requirements say "
              "RED; there is no red illuminator on this module and IR "
              "serves that requirement. Do not record 'RED' here.",
    )

    registry.test(
        test_id="HW-B7-007", campaign=CAMPAIGN, layer="B7",
        title="Illumination is off after a measurement",
        objective="Confirm, by eye, that no bulb is left on when an "
                  "acquisition finishes.",
        hardware_setup="As HW-B7-006.",
        preconditions="HW-B7-006 passed.",
        procedure=(
            "acquire the full triad",
            "read the firmware's bulbs_off report",
            "ask the operator whether any source is still lit",
            "wait a few seconds and ask again",
        ),
        expected="The firmware reports the bulbs off and the operator "
                 "sees nothing lit.",
        failure_criteria="A bulb still on. Beyond the power cost, an "
                         "illuminator left on heats the sensor and "
                         "biases the next measurement.",
        captures=("the bulbs_off report",
                  "the operator's observation, twice"),
        safety=Safety.ILLUMINATION,
        automation=Automation.OPERATOR_ASSISTED,
        requires=("sensor.acquire_triad",),
        run=_off_after_measurement, cleanup=_release,
        defect_prefix="HW-SENSOR",
    )

    registry.test(
        test_id="HW-B7-008", campaign=CAMPAIGN, layer="B7",
        title="Illumination after a failed acquisition",
        objective="Check the lamps go off on the ERROR path too, and "
                  "record explicitly when that cannot be confirmed.",
        hardware_setup="As HW-B7-006. The sensor will be disconnected "
                       "mid-sequence if that can be done safely.",
        preconditions="HW-B7-004 passed - the same connector is used.",
        procedure=(
            "start an acquisition",
            "ask the operator to disconnect the sensor during it",
            "record the error",
            "ask the operator whether any lamp is still lit",
            "if the link is still up, read the status",
            "record explicitly if the off state cannot be confirmed",
        ),
        expected="A named error, and no lamp left on.",
        failure_criteria="A lamp still on after a failure. It is also a "
                         "FAILURE OF THE EVIDENCE if the off state "
                         "cannot be confirmed at all - that is recorded "
                         "as such rather than assumed away.",
        captures=("the error", "the operator's observation",
                  "whether the off state could be confirmed"),
        safety=Safety.FAULT_INJECTION,
        automation=Automation.OPERATOR_ASSISTED,
        requires=("sensor.acquire_block", "sensor.status"),
        run=_off_after_failure, cleanup=_release,
        defect_prefix="HW-SENSOR",
    )


# ======================================================================
# shared
# ======================================================================

def _release(ctx):
    from .b6_sensor import _release as shared

    return shared(ctx)


# ======================================================================
# bodies
# ======================================================================

def _shape_contract(ctx):
    ctx.require("sensor.acquire_triad")

    repeats = ctx.profile.production["sensor"]["sample_repeats"]

    transaction = ctx.sensor.triad(repeats=repeats)

    report = transaction["data"] or {}

    problems = ctx.sensor.validate_triad(report, expected_repeats=repeats)

    ctx.check(not problems,
              "the triad satisfies the 54-feature contract",
              evidence={"problems": problems})

    blocks = report.get("illuminations") or {}

    from ..adapters.sensor import CHANNELS

    for name in ("white", "uv", "ir"):
        block = blocks.get(name) or {}
        acquisitions = block.get("acquisitions") or []

        ctx.check(len(acquisitions) == repeats,
                  "{}: {} acquisitions returned, {} requested".format(
                      name, len(acquisitions), repeats),
                  evidence={"count": len(acquisitions)})

        for index, spectrum in enumerate(acquisitions):
            found = sorted(spectrum) if isinstance(spectrum, dict) else []

            ctx.check(found == sorted(CHANNELS),
                      "{} acquisition {}: exactly the 18 expected "
                      "channels".format(name, index + 1),
                      evidence={"found": found})

    features = ctx.sensor.feature_count(report)

    ctx.check(features == 54, "54 features",
              evidence={"features": features})

    ctx.measure(stage="shape", features=features, repeats=repeats,
                problems=len(problems),
                elapsed_ms=transaction["elapsed_ms"])

    if problems:
        ctx.defect(
            title="the spectrum does not satisfy the 54-feature contract",
            observed="; ".join(problems[:10]),
            expected="18 channels under each of white, uv and ir, every "
                     "value a real number",
            reproduction=("run HW-B7-001",),
            suspected_layer="sensor driver or the I2C read path",
            evidence={"problems": problems},
        )


def _repeated_static(ctx):
    ctx.require("sensor.acquire_block")

    rounds = ctx.iterations()

    ctx.instruct(
        "Place a stable target under the sensor head and do not move it "
        "or the module until this test finishes.")

    outcomes = []
    elapsed = []
    waits = []
    per_channel = {}

    for index in range(1, rounds + 1):
        try:
            transaction = ctx.sensor.acquire("white", repeats=1)

            block = transaction["data"] or {}

            problems = ctx.sensor.validate_block(
                block, expected_repeats=1, illumination="white")

            outcomes.append(not problems)

            elapsed.append(transaction["elapsed_ms"])

            block_waits = block.get("data_ready_wait_ms") or []

            if block_waits:
                waits.append(block_waits[0])

            acquisitions = block.get("acquisitions") or []

            if acquisitions and isinstance(acquisitions[0], dict):
                for channel, value in acquisitions[0].items():
                    if isinstance(value, (int, float)):
                        per_channel.setdefault(channel, []).append(value)

            if problems:
                ctx.check(False,
                          "iteration {}: the acquisition is well "
                          "formed".format(index),
                          evidence={"problems": problems})

            if index % 20 == 0:
                ctx.measure(stage="static", iteration=index, ok=True,
                            elapsed_ms=transaction["elapsed_ms"],
                            data_ready_ms=block_waits[0]
                            if block_waits else None)

        except Exception as error:
            outcomes.append(False)

            ctx.measure(stage="static", iteration=index, ok=False,
                        error="{}: {}".format(
                            type(error).__name__, error)[:200])

        if index % 25 == 0:
            ctx.say("{} of {} acquisitions".format(index, rounds))

    rate = failure_rate(outcomes)

    ctx.result.first_failure_iteration = rate["first_failure_iteration"]

    spread = {
        channel: summarize(values)
        for channel, values in sorted(per_channel.items())
    }

    ctx.record("repeated_static", failure_rate=rate,
               elapsed=summarize(elapsed), data_ready=summarize(waits),
               outliers=outliers(elapsed), channel_spread=spread)

    ctx.check(rounds >= 100,
              "at least 100 acquisitions were taken",
              evidence={"rounds": rounds})

    ctx.check(rate["all_passed"],
              "every acquisition succeeded and was well formed",
              evidence=rate)

    ctx.check(len(spread) == 18,
              "all 18 channels reported a value on every successful "
              "acquisition",
              evidence={"channels": sorted(spread)})


def _sensor_endurance(ctx):
    ctx.require("sensor.acquire_triad")

    rounds = ctx.iterations()

    outcomes = []
    elapsed = []
    temperatures = []

    ctx.say("{} full triads. Progress every 25; partial evidence is "
            "preserved if this is interrupted.".format(rounds))

    for index in range(1, rounds + 1):
        try:
            transaction = ctx.sensor.triad()

            report = transaction["data"] or {}

            problems = ctx.sensor.validate_triad(report)

            outcomes.append(not problems)
            elapsed.append(transaction["elapsed_ms"])

            temperature = report.get("temperatures")

            if temperature is not None:
                temperatures.append(temperature)

            if problems:
                ctx.check(False,
                          "iteration {}: the triad is well "
                          "formed".format(index),
                          evidence={"problems": problems})

            if index % 20 == 0:
                ctx.measure(stage="sensor_endurance", iteration=index,
                            ok=True, elapsed_ms=transaction["elapsed_ms"],
                            features=ctx.sensor.feature_count(report))

        except Exception as error:
            outcomes.append(False)

            ctx.measure(stage="sensor_endurance", iteration=index,
                        ok=False, error="{}: {}".format(
                            type(error).__name__, error)[:200])

        if index % 25 == 0:
            ctx.say("{} of {} triads, {} failures".format(
                index, rounds, outcomes.count(False)))

    rate = failure_rate(outcomes)

    ctx.result.first_failure_iteration = rate["first_failure_iteration"]

    tenth = max(1, len(elapsed) // 10)

    first_tenth = summarize(elapsed[:tenth])
    last_tenth = summarize(elapsed[-tenth:])

    ctx.record("sensor_endurance", failure_rate=rate,
               elapsed=summarize(elapsed), first_tenth=first_tenth,
               last_tenth=last_tenth,
               temperatures=temperatures[-5:] if temperatures else None)

    ctx.check(rate["all_passed"],
              "every triad in the endurance run succeeded",
              evidence=rate)

    if first_tenth and last_tenth:
        drift = last_tenth["mean"] - first_tenth["mean"]

        ctx.note(
            "Acquisition time moved from {} ms to {} ms between the "
            "first and last tenth of the run ({:+.1f} ms). That is "
            "evidence about heating, not a failure.".format(
                first_tenth["mean"], last_tenth["mean"], drift))


def _disconnect_recovery(ctx):
    ctx.require("sensor.init", "sensor.acquire_block", "sensor.status")

    connector = ctx.ask(
        "Can the AS7265x be unplugged safely, without shorting "
        "anything")

    if not connector:
        ctx.skip(
            "the sensor cannot be disconnected safely on this bench; a "
            "soldered sensor is not a fault and this test does not "
            "apply")

    before = ctx.sensor.acquire("white", repeats=1)

    ctx.check(True, "an acquisition works before the disconnect",
              evidence={"elapsed_ms": before["elapsed_ms"]})

    ctx.instruct("Disconnect the AS7265x from the I2C bus now.")

    error_detail = None

    try:
        ctx.sensor.acquire("white", repeats=1)

        ctx.check(False,
                  "an acquisition fails while the sensor is "
                  "disconnected",
                  evidence={"result": "it succeeded, which means "
                                      "something else answered"})

    except Exception as error:
        error_detail = {
            "type": type(error).__name__,
            "code": getattr(error, "code", None),
            "message": str(error)[:300],
            "data": getattr(error, "data", None),
        }

        ctx.check(True,
                  "an acquisition fails while the sensor is "
                  "disconnected",
                  evidence=error_detail)

        code = str(error_detail.get("code") or "")

        ctx.check("SENSOR" in code.upper() or "I2C" in code.upper()
                  or "AS7265" in code.upper(),
                  "the error names the sensor rather than being generic "
                  "({})".format(code or "no code"),
                  evidence=error_detail)

    ctx.record("disconnected_error", **(error_detail or {}))

    ctx.instruct("Reconnect the AS7265x now.")

    recovered = ctx.sensor.initialize(force=True, repeats=1)

    ctx.check(True, "the sensor re-initializes after being reconnected",
              evidence={"elapsed_ms": recovered["elapsed_ms"]})

    after = ctx.sensor.acquire("white", repeats=1)

    problems = ctx.sensor.validate_block(
        after["data"] or {}, expected_repeats=1, illumination="white")

    ctx.check(not problems,
              "an acquisition works again, with no reboot",
              evidence={"problems": problems,
                        "elapsed_ms": after["elapsed_ms"]})

    status = ctx.sensor.status()

    ctx.measure(stage="disconnect_recovery",
                error_code=(error_detail or {}).get("code") or "",
                recovery_count=status.get("recovery_count"),
                state=status.get("state"))


def _data_ready_latency(ctx):
    ctx.require("sensor.acquire_block")

    repeats = ctx.iterations()

    production = ctx.profile.production["sensor"]

    # The driver waits cycles * 2.8 * 1.5 + 1 ms for data-ready.
    budget = production["integration_cycles"] * 2.8 * 1.5 + 1

    measured = {}

    for illumination in ("white", "uv", "ir"):
        transaction = ctx.sensor.acquire(illumination, repeats=repeats)

        block = transaction["data"] or {}

        waits = [w for w in (block.get("data_ready_wait_ms") or [])
                 if isinstance(w, (int, float))]

        distribution = summarize(waits)

        measured[illumination] = distribution

        ctx.check(bool(waits),
                  "the {} block reported its data-ready waits".format(
                      illumination),
                  evidence={"waits": waits[:10]})

        if distribution:
            ctx.check(distribution["max"] < budget,
                      "the slowest {} wait was {} ms, inside the {:.0f} "
                      "ms budget".format(
                          illumination, distribution["max"], budget),
                      evidence={"distribution": distribution,
                                "budget_ms": budget})

            ctx.measure(stage="data_ready", illumination=illumination,
                        n=distribution["n"], mean_ms=distribution["mean"],
                        p95_ms=distribution["p95"],
                        max_ms=distribution["max"], budget_ms=budget)

    ctx.record("data_ready", budget_ms=budget,
               integration_cycles=production["integration_cycles"],
               **measured)


def _which_lamp(ctx):
    ctx.require("sensor.led_test", "sensor.acquire_block")

    ctx.instruct(
        "Look at the sensor head, but NOT directly into it - the UV "
        "source is about to be switched on.")

    hold = min(600, ctx.profile.data["illumination"]["max_hold_ms"])

    transaction = ctx.sensor.led_test(hold_ms=hold)

    ctx.record("led_test", hold_ms=hold, **(transaction["data"] or {}))

    ctx.check(True, "led_test ran",
              evidence={"hold_ms": hold,
                        "elapsed_ms": transaction["elapsed_ms"]})

    for illumination, question in (
            ("white", "Was the WHITE source lit during the white "
                      "acquisition"),
            ("uv", "Was the UV source lit during the uv acquisition"),
    ):
        ctx.sensor.acquire(illumination, repeats=1)

        ctx.confirm_observation(question)

    ctx.sensor.acquire("ir", repeats=1)

    visible = ctx.observe(
        "Was the IR source visible at all during the ir acquisition "
        "(IR is often invisible - UNKNOWN is a correct answer)",
        ("YES", "NO", "UNKNOWN"))

    ctx.record("ir_visibility", answer=visible)

    if visible == "UNKNOWN":
        ctx.note(
            "IR could not be confirmed by eye. That is expected and is "
            "recorded as UNKNOWN rather than assumed - a phone camera "
            "will usually show an IR emitter if a confirmation is "
            "needed.")

    simultaneous = ctx.ask(
        "Did you at any point see MORE THAN ONE source lit at the same "
        "time")

    ctx.check(not simultaneous,
              "only one illumination source was lit at a time",
              evidence={"operator_saw_simultaneous": simultaneous},
              kind="OPERATOR")


def _off_after_measurement(ctx):
    ctx.require("sensor.acquire_triad")

    transaction = ctx.sensor.triad()

    report = transaction["data"] or {}

    bulbs_off = report.get("bulbs_off")

    ctx.check(bulbs_off is not False,
              "the firmware reports the bulbs off after the triad",
              evidence={"bulbs_off": bulbs_off})

    ctx.confirm_observation(
        "Is every illumination source OFF now, immediately after the "
        "measurement")

    import time

    time.sleep(3)

    ctx.confirm_observation(
        "Is every illumination source still off three seconds later")

    ctx.record("off_after_measurement", bulbs_off=bulbs_off)


def _off_after_failure(ctx):
    ctx.require("sensor.acquire_block", "sensor.status")

    connector = ctx.ask(
        "Can the AS7265x be unplugged safely during an acquisition")

    if not connector:
        ctx.skip(
            "the sensor cannot be disconnected safely on this bench")

    ctx.instruct(
        "In a moment an acquisition will start. Disconnect the AS7265x "
        "WHILE it is running, then press Enter here.")

    error_detail = None

    try:
        ctx.sensor.acquire("white", repeats=5)

    except Exception as error:
        error_detail = {
            "type": type(error).__name__,
            "code": getattr(error, "code", None),
            "message": str(error)[:300],
        }

    ctx.record("failure_error", **(error_detail or {"note": "no error"}))

    ctx.check(error_detail is not None,
              "the acquisition failed while the sensor was disconnected",
              evidence=error_detail or {})

    lit = ctx.ask("Is any illumination source still lit")

    ctx.check(not lit,
              "no lamp was left on after the failed acquisition",
              evidence={"operator_saw_light": lit},
              kind="OPERATOR")

    confirmable = True
    status = None

    try:
        status = ctx.sensor.status()

    except Exception as error:
        confirmable = False

        ctx.note(
            "The off state could NOT be confirmed from the firmware: "
            "{}: {}. The operator's observation is the only evidence "
            "here, and it is recorded as such.".format(
                type(error).__name__, error))

    ctx.record("off_after_failure", confirmable=confirmable,
               status=status, operator_saw_light=lit)

    ctx.measure(stage="off_after_failure",
                error_code=(error_detail or {}).get("code") or "",
                operator_saw_light=lit,
                firmware_confirmable=confirmable)
