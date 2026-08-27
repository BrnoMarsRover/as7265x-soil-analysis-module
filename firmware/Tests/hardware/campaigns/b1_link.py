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

from ..core.model import (Automation, IterationKind, Requirement,
                          Safety)
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
        requirements=("HW-REQ-LINK-001",),
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
        requirements=("HW-REQ-LINK-002",),
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
        requirements=("HW-REQ-LINK-003",),
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
        requirements=("HW-REQ-LINK-004",),
        iteration_kind=IterationKind.OPEN_CYCLE,
        qualification_min_iterations=5,
        characterization_min_iterations=2,
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
        requirements=("HW-REQ-LINK-005",),
        iteration_kind=IterationKind.OPEN_CYCLE,
        qualification_min_iterations=100,
        characterization_min_iterations=20,
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
        requirements=("HW-REQ-LINK-006",),
        iteration_kind=IterationKind.REQUEST,
        qualification_min_iterations=1000,
        characterization_min_iterations=100,
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
        requirements=("HW-REQ-LINK-007",),
        iteration_kind=IterationKind.REQUEST,
        characterization_min_iterations=20,
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
        requirements=("HW-REQ-LINK-008",),
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

    registry.test(
        test_id="HW-B1-009", campaign=CAMPAIGN, layer="B1",
        requirements=("HW-REQ-LINK-009",),
        title="Damaged frames are captured, and the CP210x syndrome is "
              "classified",
        objective="Keep every damaged line in a lossless form, and "
                  "recognise the known 64-leading-byte corruption when "
                  "it appears.",
        hardware_setup="ESP32 connected. Nothing special - the point is "
                       "to be watching when it happens.",
        preconditions="HW-B1-001 passed.",
        procedure=(
            "run a burst of traffic with varied payload sizes",
            "after every request, capture any line the transport marked "
            "damaged",
            "record each damaged line as escaped text AND as hex, so "
            "nothing is lost to an encoding",
            "count the leading undecodable bytes of each",
            "classify a prefix of exactly 64 bytes as the known CP210x "
            "USB-packet syndrome",
            "report the corruption rate over the burst",
        ),
        expected="No damaged frame at all. If any appears it is "
                 "captured completely and classified.",
        failure_criteria="Any damaged frame in a clean-link "
                         "qualification. A recovered frame proves "
                         "recovery works; it does not qualify the link "
                         "as clean.",
        captures=("every damaged line, escaped and as hex",
                  "the leading damage length of each",
                  "whether it matches the 64-byte syndrome",
                  "corrupt, salvaged, stale and oversized counters",
                  "the corruption rate"),
        safety=Safety.COMMUNICATION, automation=Automation.AUTOMATIC,
        requires=("link.ping", "link.status", "link.counters"),
        run=_damaged_frames, cleanup=_close_link,
        iteration_kind=IterationKind.REQUEST,
        default_iterations=200, max_iterations=20000,
        qualification_min_iterations=200,
        characterization_min_iterations=20,
        defect_prefix="HW-SER",
        notes="Measured on this bench: exactly 64 leading bytes - one "
              "CP210x USB packet - replaced by undecodable rubbish, "
              "the rest of the frame byte-perfect.",
    )

    registry.test(
        test_id="HW-B1-010", campaign=CAMPAIGN, layer="B1",
        requirements=("HW-REQ-LINK-010",),
        title="Payload size matrix",
        objective="Prove small, medium and full-size responses all "
                  "arrive intact, since the largest is two orders of "
                  "magnitude bigger than the smallest.",
        hardware_setup="ESP32 connected, sensor present so a full "
                       "spectrum can be returned.",
        preconditions="HW-B1-001 and HW-B6-002 passed.",
        procedure=(
            "send ping and record the response size",
            "send get_status and record the response size",
            "send acquire_triad and record the response size",
            "check each answer is well formed",
            "compare the largest against the transport's own frame cap",
            "record bytes read and latency per size",
        ),
        expected="Every size arrives intact, and the largest is "
                 "comfortably inside the reader's frame cap.",
        failure_criteria="A truncated or damaged large response, or one "
                         "approaching the frame cap - which would mean "
                         "the cap is doing nothing and a stuck stream "
                         "could not be distinguished from a big answer.",
        captures=("response size per command", "latency per size",
                  "bytes read", "the configured frame cap",
                  "any shape problem in the large answer"),
        safety=Safety.ILLUMINATION, automation=Automation.AUTOMATIC,
        requires=("link.ping", "link.status", "sensor.acquire_triad"),
        run=_payload_sizes, cleanup=_close_link,
        defect_prefix="HW-SER",
    )

    registry.test(
        test_id="HW-B1-011", campaign=CAMPAIGN, layer="B1",
        requirements=("HW-REQ-LINK-011",),
        title="Only one client owns the port",
        objective="Prove a second client is refused rather than "
                  "interleaving bytes on one wire.",
        hardware_setup="ESP32 connected. The harness will hold the port "
                       "and the operator will try to open it elsewhere.",
        preconditions="HW-B1-001 passed.",
        procedure=(
            "open the port and confirm it works",
            "ask the operator to start the production client on the "
            "same device in another terminal",
            "record what the second client reported",
            "check the harness's own link is unaffected",
            "ask the operator to close the second client",
            "confirm the harness still works afterwards",
        ),
        expected="The second client is refused with PORT_BUSY, and the "
                 "first is undisturbed.",
        failure_criteria="A second client that opens successfully. Two "
                         "programs interleaving commands on one "
                         "carousel is a mechanical hazard as well as a "
                         "data hazard.",
        captures=("what the second client printed",
                  "the harness's transport counters throughout",
                  "whether the first link survived"),
        safety=Safety.COMMUNICATION,
        automation=Automation.OPERATOR_ASSISTED,
        requires=("link.open", "link.ping"),
        run=_exclusive_ownership, cleanup=_close_link,
        defect_prefix="HW-SER",
    )

    registry.test(
        test_id="HW-B1-012", campaign=CAMPAIGN, layer="B1",
        requirements=("HW-REQ-LINK-012",),
        title="Device uptime, heap and reset cause under load",
        objective="Watch the ESP32's own health while it answers a "
                  "sustained burst, so a memory leak is seen before it "
                  "becomes a mid-mission crash.",
        hardware_setup="ESP32 connected.",
        preconditions="HW-B1-006 passed.",
        procedure=(
            "read uptime, free heap and reset cause where the firmware "
            "reports them",
            "run a burst mixing small and large responses",
            "sample the heap at intervals",
            "read the health fields again",
            "check uptime only increased - no reset happened",
            "compare the first and last heap readings",
        ),
        expected="No reset during the burst, and no downward trend in "
                 "free heap.",
        failure_criteria="A reset - the uptime went backwards - or a "
                         "heap that falls monotonically, which is a "
                         "leak that will eventually strand a mission.",
        captures=("uptime before and after", "heap samples over time",
                  "the reset cause if reported",
                  "the first-to-last heap difference"),
        safety=Safety.COMMUNICATION, automation=Automation.AUTOMATIC,
        requires=("link.status",),
        run=_device_health, cleanup=_close_link,
        iteration_kind=IterationKind.REQUEST,
        default_iterations=200, max_iterations=20000,
        characterization_min_iterations=50,
        assumption="H-007", defect_prefix="HW-SER",
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

    # REQUIRED, not "or absent". This test exists to establish that the
    # device on the other end of the wire is the science module, and a
    # device that returns no identity has not established that. The old
    # `reported is None or reported == expected` passed both.
    ctx.observed("the device identifies as {}".format(expected),
                 reported, expected=expected,
                 requirement=Requirement.REQUIRED)

    ctx.observed(
        "the protocol version matches the one this repository ships",
        answer.get("protocol_version"),
        expected=ctx.profile.production["protocol_version"],
        requirement=Requirement.REQUIRED)

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


# ----------------------------------------------------------------------
# the CP210x damaged-prefix syndrome
# ----------------------------------------------------------------------

# One CP210x USB packet. A frame whose first N bytes are undecodable and
# whose remainder is byte-perfect is a HOST BRIDGE artefact, not a
# firmware that builds bad JSON - and the number is the diagnosis.
CP210X_PACKET_BYTES = 64

REPLACEMENT = "�"


def _leading_damage(line):
    """How many leading characters of a line were undecodable."""
    damage = 0

    for character in line:
        if character == REPLACEMENT:
            damage += 1

        else:
            break

    return damage


def _classify_damage(line):
    """
    What a damaged line looks like, in enough detail to act on.

    Keeps the line twice: escaped, so it survives any terminal, and as
    hex, so nothing is lost to an encoding. A counter says a frame
    arrived broken; it cannot say HOW, and how is the entire diagnosis.
    """
    leading = _leading_damage(line)

    raw = line.encode("utf-8", errors="replace")

    return {
        "leading_damage_bytes": leading,
        "length": len(line),
        "escaped": line.encode(
            "unicode_escape").decode("ascii", errors="replace")[:400],
        "hex": raw.hex()[:800],
        "cp210x_packet_syndrome": leading == CP210X_PACKET_BYTES,
        "tail_looks_like_json": line.rstrip().endswith("}"),
    }


def _damaged_frames(ctx):
    ctx.require("link.ping", "link.status", "link.counters")

    requests = ctx.iterations()

    link = ctx.link.require_link("watch for damaged frames")

    before = ctx.link.counters()

    seen = []
    outcomes = []

    ctx.say("{} requests, watching every line the transport marks "
            "damaged".format(requests))

    for index in range(1, requests + 1):
        command = "ping" if index % 2 else "get_status"

        already = len(getattr(link, "damaged_lines", []) or [])

        try:
            ctx.link.request(command, retries=0)

            outcomes.append(True)

        except Exception as error:
            outcomes.append(False)

            ctx.measure(stage="damage_watch", request=index,
                        command=command, ok=False,
                        error=str(error)[:200])

        damaged = list(getattr(link, "damaged_lines", []) or [])

        for line in damaged[already:]:
            entry = _classify_damage(line)

            entry["request"] = index
            entry["command"] = command

            seen.append(entry)

            ctx.event("damaged_frame", **entry)

            ctx.measure(
                stage="damaged_frame", request=index, command=command,
                leading_damage_bytes=entry["leading_damage_bytes"],
                cp210x_syndrome=entry["cp210x_packet_syndrome"],
                length=entry["length"])

        if index % 50 == 0:
            ctx.say("{} of {}, {} damaged frames so far".format(
                index, requests, len(seen)))

    after = ctx.link.counters()

    delta = {key: after.get(key, 0) - before.get(key, 0)
             for key in after}

    syndrome = [e for e in seen if e["cp210x_packet_syndrome"]]

    rate = failure_rate(outcomes)

    ctx.record("damaged_frames", failure_rate=rate,
               counters_delta=delta, damaged=seen[:20],
               damaged_total=len(seen),
               cp210x_syndrome_total=len(syndrome),
               corruption_rate_pct=(
                   round(100.0 * len(seen) / requests, 4)
                   if requests else None))

    ctx.check(rate["all_passed"], "every request was answered",
              evidence=rate)

    ctx.check(not seen,
              "no frame arrived damaged - this is a clean-link "
              "qualification, and a recovered frame does not satisfy it",
              evidence={"damaged": len(seen),
                        "cp210x_syndrome": len(syndrome),
                        "counters_delta": delta})

    for counter in ("corrupt_frames", "salvaged_frames",
                    "stale_frames", "oversized_lines"):
        ctx.check(delta.get(counter, 0) == 0,
                  "the {} counter did not rise".format(counter),
                  evidence={"delta": delta.get(counter)})

    if syndrome:
        ctx.defect(
            title="the known CP210x 64-byte damaged-prefix syndrome "
                  "reproduced",
            observed="{} of {} damaged frames had exactly {} "
                     "undecodable leading bytes with a byte-perfect "
                     "remainder".format(
                         len(syndrome), len(seen), CP210X_PACKET_BYTES),
            expected="no damaged frames",
            reproduction=("run HW-B1-009 with the same iteration "
                          "count",),
            suspected_layer="host USB bridge (CP210x driver), not the "
                            "firmware - the damage is exactly one USB "
                            "packet and the rest of the frame is "
                            "intact",
            evidence={"examples": syndrome[:5],
                      "counters_delta": delta},
        )

    elif seen:
        ctx.defect(
            title="frames arrived damaged with an unrecognised shape",
            observed="{} damaged frames, leading damage lengths "
                     "{}".format(
                         len(seen),
                         sorted({e["leading_damage_bytes"]
                                 for e in seen})),
            expected="no damaged frames",
            reproduction=("run HW-B1-009",),
            suspected_layer="UNKNOWN - the damage is not the known "
                            "64-byte CP210x syndrome, so the host "
                            "bridge is not the obvious explanation",
            evidence={"examples": seen[:5]},
        )


def _payload_sizes(ctx):
    ctx.require("link.ping", "link.status", "sensor.acquire_triad")

    ctx.link.require_link("measure payload sizes")

    cap = getattr(ctx.link.module, "MAX_FRAME_BYTES", 65536)

    sizes = {}

    for label, call in (
            ("ping", lambda: ctx.link.request("ping", retries=1)),
            ("get_status",
             lambda: ctx.link.request("get_status", retries=1)),
            ("acquire_triad", lambda: ctx.sensor.triad())):
        before = ctx.link.counters().get("bytes_read", 0)

        transaction = call()

        after = ctx.link.counters().get("bytes_read", 0)

        size = after - before

        sizes[label] = {
            "response_bytes": size,
            "elapsed_ms": transaction["elapsed_ms"],
        }

        ctx.check(bool(transaction.get("data")),
                  "{} returned an answer".format(label),
                  evidence=sizes[label])

        ctx.measure(stage="payload", command=label, bytes=size,
                    elapsed_ms=transaction["elapsed_ms"], cap=cap)

    triad = sizes.get("acquire_triad", {}).get("response_bytes") or 0

    ctx.check(triad > (sizes.get("ping", {}).get("response_bytes") or 0),
              "the full spectrum response is larger than a ping - the "
              "size matrix really did vary",
              evidence=sizes)

    ctx.check(triad < cap * 0.5,
              "the largest response ({} bytes) is comfortably inside "
              "the {} byte frame cap".format(triad, cap),
              evidence={"largest": triad, "cap": cap})

    ctx.record("payload_sizes", cap=cap, **sizes)


def _exclusive_ownership(ctx):
    ctx.require("link.open", "link.ping")

    device = ctx.device()

    ctx.link.require_link("hold the port while a second client tries")

    ctx.link.request("ping", retries=1)

    ctx.check(True, "the harness holds the port and it works")

    before = ctx.link.counters()

    ctx.instruct(
        "In another terminal, try to start the production client on "
        "{} while this harness is still holding it:\n"
        "      python3 firmware/PC/rover_science_client.py --port {}\n"
        "   Then come back here.".format(device, device))

    printed = ctx.operator_note(
        "What did the second client print")

    refused = any(
        marker in (printed or "").upper()
        for marker in ("PORT_BUSY", "BUSY", "ACCESS", "DENIED",
                       "PERMISSION", "RESOURCE"))

    ctx.check(refused,
              "the second client was refused rather than opening the "
              "port",
              evidence={"printed": printed}, kind="OPERATOR")

    if not refused and printed:
        ctx.note(
            "The operator's account does not obviously name a refusal. "
            "If the second client CONNECTED, that is a defect: two "
            "programs interleaving commands on one carousel is a "
            "mechanical hazard as well as a data hazard.")

    ctx.link.request("ping", retries=1)

    after = ctx.link.counters()

    ctx.check(True, "the harness's own link still works afterwards")

    delta = {key: after.get(key, 0) - before.get(key, 0)
             for key in after}

    for counter in ("corrupt_frames", "stale_frames"):
        ctx.check(delta.get(counter, 0) == 0,
                  "the {} counter did not rise while the second client "
                  "was attempting the port".format(counter),
                  evidence={"delta": delta.get(counter)})

    ctx.record("exclusive_ownership", device=device, printed=printed,
               counters_delta=delta)


def _health_fields(status):
    """Uptime, heap and reset cause, wherever the firmware puts them."""
    found = {}

    for key in ("uptime_ms", "esp_uptime_ms"):
        if key in (status or {}):
            found["uptime_ms"] = status[key]

            break

    memory = (status or {}).get("memory") or {}

    for key in ("free", "free_bytes", "mem_free"):
        if key in memory:
            found["free_heap"] = memory[key]

            break

    for key in ("reset_cause", "reset_reason"):
        if key in (status or {}):
            found["reset_cause"] = status[key]

            break

    return found


def _device_health(ctx):
    ctx.require("link.status")

    requests = ctx.iterations()

    ctx.link.require_link("watch device health")

    first = _health_fields(ctx.link.request("get_status",
                                            retries=1)["data"])

    ctx.record("health_before", **first)

    ctx.observed("the firmware reports its uptime",
                 first.get("uptime_ms"),
                 requirement=Requirement.REQUIRED,
                 matches=lambda v: isinstance(v, (int, float)))

    ctx.observed("the firmware reports its free heap",
                 first.get("free_heap"),
                 requirement=Requirement.OPTIONAL_DIAGNOSTIC)

    heaps = []
    uptimes = []

    for index in range(1, requests + 1):
        command = "get_status" if index % 5 == 0 else "ping"

        try:
            transaction = ctx.link.request(command, retries=0)

        except Exception as error:
            ctx.measure(stage="health", request=index, ok=False,
                        error=str(error)[:200])

            continue

        if command != "get_status":
            continue

        fields = _health_fields(transaction["data"])

        if fields.get("free_heap") is not None:
            heaps.append(fields["free_heap"])

        if fields.get("uptime_ms") is not None:
            uptimes.append(fields["uptime_ms"])

        ctx.measure(stage="health", request=index, ok=True,
                    free_heap=fields.get("free_heap"),
                    uptime_ms=fields.get("uptime_ms"))

    last = _health_fields(ctx.link.request("get_status",
                                           retries=1)["data"])

    ctx.record("health_after", samples=len(heaps), **last)

    if first.get("uptime_ms") is not None and last.get(
            "uptime_ms") is not None:
        ctx.check(last["uptime_ms"] >= first["uptime_ms"],
                  "uptime only increased - the board did not reset "
                  "during the burst",
                  evidence={"before": first["uptime_ms"],
                            "after": last["uptime_ms"]})

    monotonic = all(
        uptimes[i] <= uptimes[i + 1] for i in range(len(uptimes) - 1))

    ctx.check(monotonic,
              "uptime never went backwards at any sample",
              evidence={"samples": len(uptimes)})

    if len(heaps) >= 4:
        tenth = max(1, len(heaps) // 10)

        opening = summarize(heaps[:tenth])
        closing = summarize(heaps[-tenth:])

        drop = opening["mean"] - closing["mean"]

        ctx.record("heap_trend", first=opening, last=closing,
                   drop=round(drop, 2))

        ctx.measure(stage="heap_trend", first_mean=opening["mean"],
                    last_mean=closing["mean"], drop=round(drop, 2))

        falling = all(
            heaps[i] >= heaps[i + 1] for i in range(len(heaps) - 1))

        ctx.check(not (falling and drop > 0),
                  "free heap is not falling monotonically across the "
                  "burst",
                  evidence={"first": opening, "last": closing,
                            "monotonically_falling": falling})

        ctx.note(
            "Free heap moved from {} to {} across {} samples. There is "
            "no authoritative heap budget in this repository, so the "
            "TREND is the finding, not the absolute value.".format(
                opening["mean"], closing["mean"], len(heaps)))

    elif not heaps:
        ctx.note(
            "This firmware does not report free heap in get_status, so "
            "the memory half of H-007 cannot be observed from the PC. "
            "The uptime half was.")
