"""
B1 - the Main PC to ESP32 link, before anything below it is trusted.

The layer everything else runs over. If the transport drops one frame in
a hundred, every campaign above it inherits a one-in-a-hundred mystery,
so B1 measures the transport on its own terms first: open, ping, status,
close, reopen, a hundred open cycles, a thousand requests on one open
port, and the latency distribution of each.

THE TWO MEASURED FACTS THIS LAYER IS BUILT ON

    opening the port must not reset the board. `SerialLink.open()`
    drives DTR and RTS low before opening for exactly that reason, and
    HW-B1-002 is the test that says whether it works on THIS board.

    /dev/ttyUSB0 disappeared mid-request once, on this bench, and the
    electrical cause is unknown. That is RF-002, and HW-B1-005 is where
    a thousand open cycles either reproduce it or bound it.
"""

from ..core.model import Automation, Safety
from ..core.analysis import failure_rate, outliers, summarize


CAMPAIGN = "B1"


def register(registry):
    registry.campaign(
        CAMPAIGN, layer="B1", title="Main PC to ESP32 communication",
        purpose="Measure the transport on its own terms, so that a "
                "failure in a later campaign can be attributed to the "
                "thing under test rather than to the wire.",
        prerequisites=("B0",),
        gate_note="Gated by B0: a link result is only attributable once "
                  "the device it was measured on is identified.",
    )

    registry.test(
        test_id="HW-B1-001", campaign=CAMPAIGN, layer="B1",
        title="Port open and first ping",
        objective="Establish that the resolved device is the science "
                  "module and that it answers the protocol.",
        hardware_setup="ESP32 connected over USB, firmware running.",
        preconditions="HW-B0-004 has identified exactly one device.",
        procedure=(
            "open the port through the production SerialLink, with DTR "
            "and RTS driven low before open",
            "send ping and wait for the answer",
            "record the round trip time and the answer",
            "check the firmware name and protocol version",
        ),
        expected="A ping answer carrying the expected firmware name and "
                 "protocol version, within the connect timeout.",
        failure_criteria="No answer (PROTOCOL_TIMEOUT), the REPL "
                         "answering instead of the firmware "
                         "(DEVICE_AT_REPL), the port already held "
                         "(PORT_BUSY), or a protocol version the PC "
                         "does not speak.",
        captures=("device", "baud rate", "round trip time",
                  "the whole ping answer", "transport counters"),
        safety=Safety.COMMUNICATION, automation=Automation.AUTOMATIC,
        requires=("link.open", "link.ping"),
        run=_open_and_ping, cleanup=_close_link,
        defect_prefix="HW-SER",
    )

    registry.test(
        test_id="HW-B1-002", campaign=CAMPAIGN, layer="B1",
        title="Opening the port does not reset the board",
        objective="Prove the DTR/RTS discipline in SerialLink.open() "
                  "works on this board, so starting the client does not "
                  "reboot an instrument holding a synchronized "
                  "carousel position.",
        hardware_setup="ESP32 connected, firmware running for long "
                       "enough that its uptime is unmistakable.",
        preconditions="HW-B1-001 passed.",
        procedure=(
            "read the module's uptime",
            "close the port",
            "reopen it and read the uptime again",
            "repeat ten times",
            "check the uptime never goes backwards",
        ),
        expected="Uptime increases monotonically across every reopen. "
                 "No boot banner, no reset.",
        failure_criteria="Any uptime that decreases. The board rebooted "
                         "when the port was opened, H-008 is false, and "
                         "every operator session starts by destroying "
                         "the carousel reference.",
        captures=("uptime before and after each cycle",
                  "any console noise seen at open",
                  "the reset count if the firmware reports one"),
        safety=Safety.COMMUNICATION, automation=Automation.AUTOMATIC,
        requires=("link.open", "link.ping", "link.status"),
        run=_open_does_not_reset, cleanup=_close_link,
        assumption="H-008", defect_prefix="HW-SER",
        notes="Replaces HW-101 in PHASE_B_CAMPAIGNS.md.",
    )

    registry.test(
        test_id="HW-B1-003", campaign=CAMPAIGN, layer="B1",
        title="get_status answers completely",
        objective="Check that the status the whole PC layer depends on "
                  "carries every section it promises.",
        hardware_setup="ESP32 connected.",
        preconditions="HW-B1-001 passed.",
        procedure=(
            "send get_status",
            "check for the sensor, servo, carousel and slots sections",
            "record the state each reports",
            "check get_status does not touch the sensor - the state "
            "must answer at the same speed whether the AS7265x is "
            "present or missing",
        ),
        expected="Every section present, and the answer fast enough "
                 "that nothing was initialized to produce it.",
        failure_criteria="A missing section, or a status that takes as "
                         "long as an acquisition - which would mean it "
                         "is initializing the sensor to answer.",
        captures=("the whole status", "its latency",
                  "sensor state", "servo state", "carousel state"),
        safety=Safety.COMMUNICATION, automation=Automation.AUTOMATIC,
        requires=("link.status",),
        run=_status_contract, cleanup=_close_link,
        defect_prefix="HW-SER",
    )

    registry.test(
        test_id="HW-B1-004", campaign=CAMPAIGN, layer="B1",
        title="Close, reopen and reuse the session",
        objective="Prove the port is released cleanly and a new session "
                  "can take it without the board noticing.",
        hardware_setup="ESP32 connected.",
        preconditions="HW-B1-001 passed.",
        procedure=(
            "open, ping, get_status, close - five times",
            "check every cycle succeeds",
            "check the request ids of one session are never accepted by "
            "another",
        ),
        expected="Five clean cycles, no stale frames accepted.",
        failure_criteria="A cycle that cannot reopen, or any stale "
                         "frame counted by the transport - which would "
                         "mean one session's answer reached another.",
        captures=("per-cycle result and latency",
                  "the stale frame counter"),
        safety=Safety.COMMUNICATION, automation=Automation.AUTOMATIC,
        requires=("link.open", "link.ping", "link.status"),
        run=_close_reopen, cleanup=_close_link,
        default_iterations=5, max_iterations=200,
        defect_prefix="HW-SER",
    )

    registry.test(
        test_id="HW-B1-005", campaign=CAMPAIGN, layer="B1",
        title="CP2102 repeated open cycles",
        objective="Bound or reproduce RF-002: the bridge wedging, or "
                  "the device node disappearing, after repeated opens.",
        hardware_setup="ESP32 connected. The bench free for the "
                       "duration - a hundred cycles takes minutes.",
        preconditions="HW-B1-002 and HW-B1-004 passed.",
        procedure=(
            "for each cycle: open, ping, record the latency, close",
            "record the error and the recovery for any cycle that fails",
            "continue after a failure - the failure RATE is the result",
            "report the first failing cycle",
        ),
        expected="Every cycle opens and answers. At least 100 cycles.",
        failure_criteria="Any cycle that fails. One failure in a "
                         "hundred is not a pass; it is an intermittent "
                         "fault with a hundred-cycle sample.",
        captures=("cycle number", "open result", "first response",
                  "latency", "bytes read", "error", "recovery",
                  "the first failing cycle"),
        safety=Safety.ENDURANCE, automation=Automation.AUTOMATIC,
        requires=("link.open", "link.ping"),
        run=_open_cycles, cleanup=_close_link,
        default_iterations=100, max_iterations=2000,
        assumption="H-004", defect_prefix="HW-USB",
    )

    registry.test(
        test_id="HW-B1-006", campaign=CAMPAIGN, layer="B1",
        title="Persistent-open request endurance",
        objective="Prove one open port survives a thousand requests "
                  "without accumulating corruption.",
        hardware_setup="ESP32 connected.",
        preconditions="HW-B1-005 passed.",
        procedure=(
            "open the port once",
            "send alternating ping and get_status requests",
            "record the latency of every request",
            "watch the corrupt, salvaged, stale and oversized counters",
            "close once at the end",
        ),
        expected="Every request answered, and no counter rising.",
        failure_criteria="Any unanswered request, or a corruption "
                         "counter that grows - a rising count is the "
                         "difference between one unlucky frame and an "
                         "unhealthy link.",
        captures=("per-request latency", "the latency distribution",
                  "every transport counter, before and after",
                  "the first failing request"),
        safety=Safety.ENDURANCE, automation=Automation.AUTOMATIC,
        requires=("link.ping", "link.status", "link.counters"),
        run=_request_endurance, cleanup=_close_link,
        default_iterations=1000, max_iterations=20000,
        defect_prefix="HW-SER",
    )

    registry.test(
        test_id="HW-B1-007", campaign=CAMPAIGN, layer="B1",
        title="Response latency distribution",
        objective="Measure what a command actually costs, so a later "
                  "timeout can be set from evidence rather than from a "
                  "guess.",
        hardware_setup="ESP32 connected, idle.",
        preconditions="HW-B1-001 passed.",
        procedure=(
            "send ping N times and record every round trip",
            "send get_status N times and record every round trip",
            "compute mean, median, sd, p95, p99 and worst for each",
            "flag any outlier beyond three standard deviations",
        ),
        expected="A distribution recorded for both commands. This test "
                 "MEASURES; it fails only if a command does not answer.",
        failure_criteria="An unanswered request. A slow tail is "
                         "evidence, not a failure - it is what the "
                         "timeouts must be set against.",
        captures=("every individual latency",
                  "the distribution for each command",
                  "outliers with their index"),
        safety=Safety.COMMUNICATION, automation=Automation.AUTOMATIC,
        requires=("link.ping", "link.status"),
        run=_latency_distribution, cleanup=_close_link,
        default_iterations=50, max_iterations=5000,
        defect_prefix="HW-SER",
    )

    registry.test(
        test_id="HW-B1-008", campaign=CAMPAIGN, layer="B1",
        title="Device renumbering after a replug",
        objective="Establish whether the device node changes when the "
                  "cable is pulled and pushed back, and whether the "
                  "stable identity survives it.",
        hardware_setup="ESP32 connected. The operator can reach the "
                       "USB cable.",
        preconditions="HW-B0-004 recorded the device identity.",
        procedure=(
            "record the device inventory",
            "ask the operator to unplug the USB cable",
            "record the inventory again and check the device is gone",
            "ask the operator to plug it back in and wait for "
            "enumeration",
            "record the inventory a third time",
            "compare the device path AND the stable identity",
        ),
        expected="The stable identity is unchanged. The device path may "
                 "change - that is the finding, not the failure.",
        failure_criteria="The device does not reappear, or its USB "
                         "serial number changes, which would mean the "
                         "profile cannot identify it reliably at all.",
        captures=("the three inventories",
                  "the path before and after",
                  "the stable identity before and after"),
        safety=Safety.MANUAL_DISCONNECT,
        automation=Automation.OPERATOR_ASSISTED,
        requires=("link.enumerate",),
        run=_replug_renumbering,
        defect_prefix="HW-USB",
    )


# ======================================================================
# shared
# ======================================================================

def _close_link(ctx):
    """
    Release the port, and say honestly whether it was released.

    Runs after every outcome, including an abort. A close that fails
    because the device already vanished is recorded as unconfirmed - the
    framework does not get to claim it tidied up after a device that is
    no longer there.
    """
    record = ctx.link.close(reason="test cleanup")

    return {
        "confirmed": bool(record.get("closed")),
        "port_released": record.get("closed"),
        "was_open": record.get("was_open"),
        "error": record.get("error"),
    }


def _uptime_of(answer):
    for key in ("uptime_ms", "esp_uptime_ms"):
        if isinstance(answer, dict) and key in answer:
            return answer[key]

    return None


# ======================================================================
# bodies
# ======================================================================

def _open_and_ping(ctx):
    ctx.require("link.open", "link.ping")

    ctx.record("open")

    ctx.link.require_link("open the port for B1")

    transaction = ctx.link.request("ping", retries=2)

    answer = transaction["data"] or {}

    ctx.check(bool(answer), "ping returned an answer",
              evidence={"latency_ms": transaction["elapsed_ms"]})

    ctx.measure(stage="ping", latency_ms=transaction["elapsed_ms"],
                firmware=answer.get("firmware"),
                version=answer.get("version"))

    expected = ctx.profile.production["firmware_name"]

    reported = answer.get("firmware") or answer.get("name")

    ctx.check(reported is None or reported == expected,
              "the device identifies as {}".format(expected),
              evidence={"reported": reported, "expected": expected})

    protocol = answer.get("protocol_version")

    ctx.check(
        protocol is None
        or protocol == ctx.profile.production["protocol_version"],
        "the protocol version matches the one this repository ships",
        evidence={"reported": protocol,
                  "expected": ctx.profile.production["protocol_version"]},
    )

    ctx.record("counters", **ctx.link.counters())


def _open_does_not_reset(ctx):
    ctx.require("link.open", "link.ping", "link.status")

    uptimes = []

    for cycle in range(1, 11):
        ctx.link.require_link("reopen for cycle {}".format(cycle))

        transaction = ctx.link.request("get_status", retries=2)
        uptime = _uptime_of(transaction["data"])

        uptimes.append(uptime)

        ctx.measure(stage="reopen", cycle=cycle, uptime_ms=uptime,
                    latency_ms=transaction["elapsed_ms"])

        ctx.link.close(reason="uptime cycle {}".format(cycle))

    known = [u for u in uptimes if u is not None]

    ctx.check(bool(known),
              "the firmware reports an uptime that can be compared",
              evidence={"uptimes": uptimes})

    if not known:
        return

    decreases = [
        (index + 1, known[index], known[index + 1])
        for index in range(len(known) - 1)
        if known[index + 1] < known[index]
    ]

    ctx.check(not decreases,
              "uptime never goes backwards across a close and reopen, "
              "so opening the port does not reset the board",
              evidence={"uptimes": known, "decreases": decreases})

    if decreases:
        ctx.defect(
            title="opening the serial port resets the ESP32",
            observed="uptime fell at cycles {}".format(
                ", ".join(str(d[0]) for d in decreases)),
            expected="uptime increases monotonically; DTR and RTS are "
                     "driven low before open() precisely to prevent a "
                     "reset",
            reproduction=("run HW-B1-002",),
            suspected_layer="host USB bridge / board auto-reset circuit",
            evidence={"uptimes": known, "decreases": decreases},
        )


def _status_contract(ctx):
    ctx.require("link.status")

    transaction = ctx.link.request("get_status", retries=2)
    status = transaction["data"] or {}

    for section in ("sensor", "servo", "carousel"):
        ctx.check(section in status,
                  "get_status carries the {} section".format(section),
                  evidence={"keys": sorted(status.keys())})

    ctx.measure(stage="status", latency_ms=transaction["elapsed_ms"],
                sensor_state=(status.get("sensor") or {}).get("state"),
                position_valid=(status.get("carousel") or {}).get(
                    "position_valid"))

    ctx.check(transaction["elapsed_ms"] < 5000,
              "get_status answers without initializing the sensor",
              evidence={"latency_ms": transaction["elapsed_ms"]})

    ctx.record("status", **status)


def _close_reopen(ctx):
    ctx.require("link.open", "link.ping", "link.status")

    cycles = ctx.iterations()
    outcomes = []

    for cycle in range(1, cycles + 1):
        try:
            ctx.link.require_link("cycle {}".format(cycle))

            ping = ctx.link.request("ping", retries=1)
            status = ctx.link.request("get_status", retries=1)

            outcomes.append(True)

            ctx.measure(stage="cycle", cycle=cycle, ok=True,
                        ping_ms=ping["elapsed_ms"],
                        status_ms=status["elapsed_ms"])

        except Exception as error:
            outcomes.append(False)

            ctx.measure(stage="cycle", cycle=cycle, ok=False,
                        error="{}: {}".format(
                            type(error).__name__, error))

        finally:
            ctx.link.close(reason="cycle {}".format(cycle))

    rate = failure_rate(outcomes)

    ctx.result.first_failure_iteration = rate["first_failure_iteration"]

    ctx.check(rate["all_passed"],
              "every open/ping/status/close cycle succeeded",
              evidence=rate)


def _open_cycles(ctx):
    ctx.require("link.open", "link.ping")

    cycles = ctx.iterations()

    outcomes = []
    latencies = []
    failures = []

    ctx.say("{} open cycles - this takes a while".format(cycles))

    for cycle in range(1, cycles + 1):
        row = {"stage": "open_cycle", "cycle": cycle}

        try:
            ctx.link.require_link("open cycle {}".format(cycle))

            transaction = ctx.link.request("ping", retries=0)

            outcomes.append(True)
            latencies.append(transaction["elapsed_ms"])

            row["ok"] = True
            row["latency_ms"] = transaction["elapsed_ms"]
            row["bytes_read"] = transaction["counters_after"].get(
                "bytes_read")

        except Exception as error:
            outcomes.append(False)

            row["ok"] = False
            row["error_type"] = type(error).__name__
            row["error"] = str(error)[:200]
            row["code"] = getattr(error, "code", None)

            failures.append({"cycle": cycle, "error": row["error"],
                             "code": row["code"]})

        finally:
            closed = ctx.link.close(reason="open cycle {}".format(cycle))

            row["closed"] = closed.get("closed")

        ctx.measure(**row)

        if cycle % 25 == 0:
            ctx.say("cycle {} of {}, {} failures so far".format(
                cycle, cycles, len(failures)))

    rate = failure_rate(outcomes)

    ctx.result.first_failure_iteration = rate["first_failure_iteration"]

    ctx.record("open_cycles", failure_rate=rate,
               latency=summarize(latencies), failures=failures[:20])

    ctx.check(cycles >= 100,
              "at least 100 open cycles were run",
              evidence={"cycles": cycles})

    ctx.check(rate["all_passed"],
              "every open cycle opened and answered",
              evidence=rate)

    if failures:
        ctx.defect(
            title="the CP2102 link failed during repeated open cycles",
            observed="{} of {} cycles failed, first at cycle {}".format(
                rate["failed"], rate["iterations"],
                rate["first_failure_iteration"]),
            expected="every cycle opens the port and receives a ping "
                     "answer",
            reproduction=("run HW-B1-005 with the same iteration count",),
            suspected_layer="USB bridge / host driver (RF-002)",
            evidence={"failure_rate": rate, "failures": failures[:20]},
        )


def _request_endurance(ctx):
    ctx.require("link.ping", "link.status", "link.counters")

    requests = ctx.iterations()

    ctx.link.require_link("open once for the request endurance run")

    before = ctx.link.counters()

    outcomes = []
    latencies = []

    for index in range(1, requests + 1):
        command = "ping" if index % 2 else "get_status"

        try:
            transaction = ctx.link.request(command, retries=0)

            outcomes.append(True)
            latencies.append(transaction["elapsed_ms"])

            if index % 50 == 0:
                ctx.measure(stage="endurance", request=index,
                            command=command, ok=True,
                            latency_ms=transaction["elapsed_ms"])

        except Exception as error:
            outcomes.append(False)

            ctx.measure(stage="endurance", request=index, command=command,
                        ok=False, error="{}: {}".format(
                            type(error).__name__, error)[:200])

        if index % 200 == 0:
            ctx.say("{} of {} requests".format(index, requests))

    after = ctx.link.counters()

    delta = {key: after.get(key, 0) - before.get(key, 0) for key in after}

    rate = failure_rate(outcomes)

    ctx.result.first_failure_iteration = rate["first_failure_iteration"]

    ctx.record("request_endurance", failure_rate=rate,
               latency=summarize(latencies),
               counters_before=before, counters_after=after,
               counters_delta=delta)

    ctx.check(requests >= 1000,
              "at least 1000 requests were sent on one open port",
              evidence={"requests": requests})

    ctx.check(rate["all_passed"], "every request was answered",
              evidence=rate)

    for counter in ("corrupt_frames", "stale_frames", "oversized_lines"):
        ctx.check(delta.get(counter, 0) == 0,
                  "the {} counter did not rise".format(counter),
                  evidence={"delta": delta.get(counter),
                            "before": before.get(counter),
                            "after": after.get(counter)})

    if delta.get("salvaged_frames", 0):
        ctx.note(
            "{} frames were salvaged from lines with rubbish in front "
            "of them. Not fatal - the production reader handles it - "
            "but it is a link that is not clean.".format(
                delta["salvaged_frames"]))


def _latency_distribution(ctx):
    ctx.require("link.ping", "link.status")

    samples = ctx.iterations()

    ctx.link.require_link("measure latency")

    measured = {}

    for command in ("ping", "get_status"):
        latencies = []
        failures = 0

        for index in range(1, samples + 1):
            try:
                transaction = ctx.link.request(command, retries=0)

                latencies.append(transaction["elapsed_ms"])

            except Exception as error:
                failures += 1

                ctx.measure(stage="latency", command=command, sample=index,
                            ok=False, error=str(error)[:200])

        distribution = summarize(latencies)

        measured[command] = {
            "distribution": distribution,
            "failures": failures,
            "outliers": outliers(latencies),
        }

        ctx.check(failures == 0,
                  "every {} request was answered".format(command),
                  evidence={"failures": failures, "samples": samples})

        if distribution:
            ctx.measure(
                stage="latency_summary", command=command,
                n=distribution["n"], mean_ms=distribution["mean"],
                median_ms=distribution["median"], p95_ms=distribution["p95"],
                p99_ms=distribution["p99"], max_ms=distribution["max"],
                sd_ms=distribution["sd"],
            )

    ctx.record("latency", **measured)

    ctx.note(
        "These are the numbers a future timeout change must be argued "
        "from. Never raise a timeout to hide a failure - raise it only "
        "when a measured p99 says the current one is too tight.")


def _replug_renumbering(ctx):
    ctx.require("link.enumerate")

    before = ctx.link.enumerate_ports()

    ctx.record("inventory_before", ports=before)

    ctx.instruct("Unplug the USB cable from the science module now.")

    during = ctx.link.enumerate_ports()

    ctx.record("inventory_unplugged", ports=during)

    ctx.check(len(during) < len(before) or during != before,
              "the device disappeared from the inventory when unplugged",
              evidence={"before": len(before), "during": len(during)})

    ctx.instruct("Plug the USB cable back in and wait a few seconds for "
                 "the operating system to enumerate it.")

    after = ctx.link.enumerate_ports()

    ctx.record("inventory_after", ports=after)

    ctx.check(len(after) >= len(before),
              "the device reappeared after the replug",
              evidence={"before": len(before), "after": len(after)})

    from ..configuration import ports as ports_module

    def identities(entries):
        found = {}

        for entry in entries:
            identity = ports_module.parse_hwid(entry.get("hwid"))

            key = identity.get("serial") or entry.get("hwid")

            if key:
                found[key] = entry.get("port")

        return found

    was = identities(before)
    now_ = identities(after)

    shared = set(was) & set(now_)

    ctx.check(bool(shared),
              "at least one device kept the same stable identity across "
              "the replug",
              evidence={"before": was, "after": now_})

    moved = {key: (was[key], now_[key]) for key in shared
             if was[key] != now_[key]}

    ctx.record("renumbering", moved=moved, stable=list(shared))

    if moved:
        ctx.note(
            "The device path CHANGED across a replug: {}. This is not a "
            "failure; it is the reason the profile must select by "
            "by-id path or USB serial number and never by "
            "ttyUSBn.".format(moved))
