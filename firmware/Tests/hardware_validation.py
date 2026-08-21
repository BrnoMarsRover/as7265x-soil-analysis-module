"""
Hardware-In-the-Loop validation for the Freya science module.

THIS IS NOT A UNIT TEST. Everything in `run_all.py` runs against fake
AS7265x and fake ST3215 hardware on CPython; it proves the firmware's
logic and proves nothing about the wiring, the servo, the sensor or the
mechanism. This file is the other half: it drives the REAL board over
the REAL serial link and reports what the hardware actually did.

    py firmware\\Tests\\hardware_validation.py --port COM4
    py firmware\\Tests\\hardware_validation.py --port COM4 --stage sensor
    py firmware\\Tests\\hardware_validation.py --port COM4 --move --stage servo-move

DELIBERATELY SEPARATE FROM run_all.py. `run_all.py` is run casually and
often, including by people who are not standing next to the mechanism.
A test suite that can turn a carousel must never be something you run by
reflex, so this lives in its own entry point, needs an explicit --port,
and every stage that moves anything additionally needs --move.

NO SECOND DRIVER. Every transaction below goes through
`firmware/PC/serial_link.py` and the production command surface. The
point of a hardware test is to exercise the code that ships; a
diagnostic that talks to the servo its own way validates the diagnostic.

WHAT IT ADDS over sending commands one at a time from the shell: one
port open for the whole campaign (so a failure cannot be an artefact of
reopening), timing on every operation, and the repeatability statistics
that turn "it moved" into "it moved 40 times and the worst closing error
was 3 counts".
"""

import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
FIRMWARE_DIR = TESTS_DIR.parent
REPO_ROOT = FIRMWARE_DIR.parent

sys.path.insert(0, str(FIRMWARE_DIR / "PC"))
sys.path.insert(0, str(FIRMWARE_DIR / "ESP32"))

from serial_link import SerialLink, LinkError, DeviceError   # noqa: E402


# ======================================================================
# result recording
# ======================================================================

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
SKIP = "SKIP"

_COLOURS = {PASS: "", WARN: "", FAIL: "", SKIP: ""}


class Report:
    """
    Every check this campaign made, with its evidence.

    A check carries the numbers that decided it, not just its verdict:
    a PASS whose evidence cannot be inspected afterwards is an opinion.
    """

    def __init__(self):
        self.checks = []
        self.started_at = time.time()

    def add(self, subsystem, test, verdict, detail="", evidence=None,
            attempts=None, passed=None, failed=None):
        entry = {
            "subsystem": subsystem,
            "test": test,
            "verdict": verdict,
            "detail": detail,
            "attempts": attempts,
            "pass": passed,
            "fail": failed,
            "evidence": evidence,
            "at": round(time.time() - self.started_at, 3),
        }

        self.checks.append(entry)

        counts = ""

        if attempts is not None:
            counts = "  [{}/{}]".format(
                passed if passed is not None else "?", attempts
            )

        print("{:<6} {:<13} {:<34} {}{}".format(
            verdict, subsystem, test, detail, counts
        ))

        sys.stdout.flush()

        return entry

    def verdict_for(self, subsystem):
        marks = [c["verdict"] for c in self.checks
                 if c["subsystem"] == subsystem]

        if not marks:
            return SKIP

        if FAIL in marks:
            return FAIL

        if WARN in marks:
            return WARN

        if PASS in marks:
            return PASS

        return SKIP

    def summary(self):
        order = []

        for check in self.checks:
            if check["subsystem"] not in order:
                order.append(check["subsystem"])

        return [(name, self.verdict_for(name)) for name in order]

    def table(self):
        """The final validation table, from executed checks only."""
        lines = [
            "| Subsystem   | Test                          | Attempts | "
            "Pass | Fail | Result |",
            "| ----------- | ----------------------------- | -------: | "
            "---: | ---: | ------ |",
        ]

        for check in self.checks:
            lines.append("| {:<11} | {:<29} | {:>8} | {:>4} | {:>4} | {} |".format(
                check["subsystem"],
                check["test"][:29],
                "-" if check["attempts"] is None else check["attempts"],
                "-" if check["pass"] is None else check["pass"],
                "-" if check["fail"] is None else check["fail"],
                check["verdict"],
            ))

        return "\n".join(lines)


def stats(values):
    """mean / sd / min / max / range for a list of numbers."""
    numbers = [float(v) for v in values if v is not None]

    if not numbers:
        return None

    ordered = sorted(numbers)

    def percentile(fraction):
        """
        Nearest-rank percentile, so the value REALLY OCCURRED.

        Interpolating invents a latency nobody measured, and a p99 that
        no request ever took is a poor thing to quote in a
        qualification report.
        """
        rank = max(1, int(math.ceil(fraction * len(ordered))))

        return round(ordered[rank - 1], 4)

    result = {
        "n": len(numbers),
        "mean": round(statistics.fmean(numbers), 4),
        "min": round(ordered[0], 4),
        "max": round(ordered[-1], 4),
        "range": round(ordered[-1] - ordered[0], 4),
        "sd": round(statistics.stdev(numbers), 4) if len(numbers) > 1 else 0.0,
        "median": round(statistics.median(ordered), 4),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
    }

    if result["mean"]:
        result["cv_pct"] = round(
            100.0 * result["sd"] / abs(result["mean"]), 3
        )

    return result


def describe_damage(lines):
    """
    How many leading bytes of each damaged frame were unreadable.

    The number is the diagnosis. A corrupted prefix of exactly 64 bytes
    is one CP210x USB packet and points at the host bridge; a frame
    damaged anywhere else, or by any other amount, points somewhere the
    firmware can be blamed for.
    """
    lengths = []

    for line in lines:
        bad = 0

        for char in line:
            if char == "�":
                bad += 1

            elif bad:
                break

        lengths.append(bad)

    return lengths


def banner(text):
    print()
    print("=" * 72)
    print(text)
    print("=" * 72)
    sys.stdout.flush()


# ======================================================================
# stages
# ======================================================================

class Campaign:
    """One open port, every stage, one report."""

    def __init__(self, link, report, args):
        self.link = link
        self.report = report
        self.args = args

        self.identity = None
        self.servo_connected = False

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def timed(self, call, *positional, **named):
        """Run one production call and return (data, error, elapsed_ms)."""
        started = time.perf_counter()

        try:
            data = call(*positional, **named)
            elapsed = (time.perf_counter() - started) * 1000.0

            return data, None, elapsed

        except (LinkError, DeviceError) as error:
            elapsed = (time.perf_counter() - started) * 1000.0

            return None, error, elapsed

    # ------------------------------------------------------------------
    # STAGE: link
    # ------------------------------------------------------------------

    def stage_link(self):
        banner("STAGE link - ESP32 host communication")

        data, error, elapsed = self.timed(self.link.ping)

        if error is not None:
            self.report.add("ESP32", "ping", FAIL, str(error))

            return False

        self.identity = data

        self.report.add(
            "ESP32", "identity", PASS,
            "{} {} (protocol {})".format(
                data.get("firmware"), data.get("version"),
                data.get("protocol_version"),
            ),
            evidence=data,
        )

        # Repeated pings on ONE open port: latency and frame integrity.
        #
        # Failures are recorded with their CODE and the console text the
        # link collected. A counter that says "1 of 40 failed" and not
        # which of seven transport faults it was cannot be diagnosed
        # afterwards, and this campaign only gets one pass at each fault.
        latencies = []
        failures = []
        rounds = self.args.link_pings

        for index in range(rounds):
            _, error, elapsed = self.timed(self.link.ping)

            if error is None:
                latencies.append(elapsed)

            else:
                failures.append({
                    "index": index,
                    "code": getattr(error, "code", type(error).__name__),
                    "message": str(error)[:300],
                    "data": getattr(error, "data", None),
                    "elapsed_ms": round(elapsed, 1),
                })

        summary = stats(latencies)

        self.report.add(
            "ESP32", "ping latency", PASS if not failures else FAIL,
            "mean {:.1f} ms, max {:.1f} ms{}".format(
                summary["mean"], summary["max"],
                "" if not failures
                else "; failed: " + ", ".join(
                    f["code"] for f in failures),
            ) if summary else "no successful ping",
            evidence={"latency_ms": summary, "failures": failures},
            attempts=rounds,
            passed=rounds - len(failures), failed=len(failures),
        )

        data, error, elapsed = self.timed(self.link.get_status)

        if error is not None:
            self.report.add("ESP32", "get_status", FAIL, str(error))

        else:
            self.report.add(
                "ESP32", "get_status", PASS,
                "{} commands, sensor={} servo={}".format(
                    len(data.get("commands", [])),
                    (data.get("sensor") or {}).get("state"),
                    "connected" if (data.get("servo") or {}).get("connected")
                    else "not connected",
                ),
                evidence={"commands": data.get("commands"),
                          "sensor": data.get("sensor"),
                          "servo": data.get("servo"),
                          "carousel": data.get("carousel")},
            )

        self.report.add(
            "ESP32", "frame integrity", PASS if not self.link.corrupt_frames
            else WARN,
            "{} corrupt, {} salvaged{}".format(
                self.link.corrupt_frames, self.link.salvaged_frames,
                "" if not self.link.corrupt_frames
                else "; corrupted prefixes: {}".format(
                    describe_damage(self.link.damaged_lines)),
            ),
            evidence={"corrupt": self.link.corrupt_frames,
                      "salvaged": self.link.salvaged_frames,
                      "prefix_lengths": describe_damage(
                          self.link.damaged_lines),
                      "damaged": [line[:400]
                                  for line in self.link.damaged_lines]},
        )

        return True

    # ------------------------------------------------------------------
    # STAGE: transport  (moves nothing)
    # ------------------------------------------------------------------

    def _payload_bytes(self, cmd, **payload):
        """
        The size of a response ON THE WIRE, not of the parsed object.

        At 115200 baud with 8N1 framing a byte costs 10 bits, so
        86.8 us. That is the floor under every latency in this stage and
        the number to compare software overhead against: a 4-kilobyte
        acquisition cannot be delivered in less than 355 ms however good
        the firmware is.
        """
        before = self.link.bytes_read
        data, error, elapsed = self.timed(
            self.link.request, cmd, timeout=200.0, **payload
        )

        return data, error, elapsed, self.link.bytes_read - before

    def stage_transport(self):
        banner("STAGE transport - PC <-> ESP32 characterization")

        # 1. LATENCY PER COMMAND SHAPE.
        #
        # One number for "the link" is not useful: a ping and a full
        # triad exercise completely different parts of it. Each command
        # shape is profiled on its own so the fixed overhead and the
        # per-byte cost can be told apart.
        profiles = (
            ("ping", {}, self.args.transport_samples),
            ("get_status", {}, max(5, self.args.transport_samples // 4)),
        )

        for cmd, payload, rounds in profiles:
            latencies = []
            sizes = []
            failures = []

            for index in range(rounds):
                _, error, elapsed, nbytes = self._payload_bytes(cmd, **payload)

                if error is None:
                    latencies.append(elapsed)
                    sizes.append(nbytes)

                else:
                    failures.append({
                        "index": index,
                        "code": getattr(error, "code", type(error).__name__),
                        "message": str(error)[:200],
                    })

            summary = stats(latencies)
            size = stats(sizes)
            wire = (size["mean"] * 10.0 / 115200.0 * 1000.0) if size else 0.0

            self.report.add(
                "Transport", "{} latency".format(cmd),
                PASS if not failures else FAIL,
                "p50 {:.1f}  p95 {:.1f}  p99 {:.1f}  max {:.1f} ms; "
                "{:.0f} B = {:.1f} ms on the wire".format(
                    summary["median"], summary["p95"], summary["p99"],
                    summary["max"], size["mean"], wire,
                ) if summary else "no successful {}".format(cmd),
                evidence={
                    "latency_ms": summary,
                    "response_bytes": size,
                    "wire_ms_at_115200": round(wire, 2),
                    "overhead_ms": (round(summary["median"] - wire, 2)
                                    if summary else None),
                    "failures": failures,
                },
                attempts=rounds, passed=len(latencies), failed=len(failures),
            )

        # 2. LARGE PAYLOADS.
        #
        # What an acquisition actually costs to deliver, measured rather
        # than assumed. Both the block and the triad are production
        # commands; nothing here is a special benchmark path.
        for label, cmd, payload in (
            ("acquire_block white x1", "acquire_block",
             {"illumination": "white", "repeats": 1}),
            ("acquire_block white x{}".format(self.args.spectra),
             "acquire_block",
             {"illumination": "white", "repeats": self.args.spectra}),
            ("acquire_triad x{}".format(self.args.spectra), "acquire_triad",
             {"repeats": self.args.spectra}),
        ):
            data, error, elapsed, nbytes = self._payload_bytes(cmd, **payload)

            if error is not None:
                self.report.add(
                    "Transport", label, FAIL,
                    "{}: {}".format(
                        getattr(error, "code", type(error).__name__),
                        str(error)[:200]),
                )

                continue

            wire = nbytes * 10.0 / 115200.0 * 1000.0

            self.report.add(
                "Transport", label, PASS,
                "{} B, {:.0f} ms total, {:.0f} ms wire, {:.0f} ms board".format(
                    nbytes, elapsed, wire, elapsed - wire),
                evidence={
                    "response_bytes": nbytes,
                    "total_ms": round(elapsed, 1),
                    "wire_ms_at_115200": round(wire, 1),
                    "board_ms": round(elapsed - wire, 1),
                    "wire_fraction_pct": round(100.0 * wire / elapsed, 1),
                },
            )

        # 3. ENDURANCE.
        #
        # Lightweight production commands, back to back, on ONE open
        # port and without resetting the board. Counted the way section
        # 10 of the campaign asks for it: first-attempt success is a
        # different number from eventual success, and a retry is
        # evidence even when it works.
        rounds = self.args.transport_requests

        # LIGHTWEIGHT PRODUCTION COMMANDS ONLY.
        #
        # sensor_test_raw belongs to the sensor stages, not here: it
        # runs a six-stage bring-up and takes 16 s, so a thousand of
        # them is four and a half hours of the wrong measurement. This
        # stage is about the TRANSPORT, and the transport is exercised
        # by commands whose answers come back immediately.
        #
        # servo_diagnostics is included only when the servo is actually
        # connected. Asking for it without one produces a perfectly
        # correct SERVO_NOT_CONNECTED refusal, and counting a correct
        # refusal as a transport failure is how a campaign reports 6
        # faults it does not have.
        mix = ["ping", "get_status"]

        if not self.servo_connected:
            try:
                self.link.connect_servo()
                self.servo_connected = True

            except (LinkError, DeviceError):
                self.servo_connected = False

        if self.servo_connected:
            mix.append("servo_diagnostics")

        mix = tuple(mix)

        latencies = []
        failures = []
        by_command = {}
        first_attempt_ok = 0

        corrupt_before = self.link.corrupt_frames
        salvaged_before = self.link.salvaged_frames

        for index in range(rounds):
            cmd = mix[index % len(mix)]

            # Read-only commands, so a retry is safe - but the retry is
            # COUNTED, never hidden.
            started = time.perf_counter()

            try:
                self.link.request(cmd, timeout=30.0)
                elapsed = (time.perf_counter() - started) * 1000.0

                first_attempt_ok += 1
                latencies.append(elapsed)
                by_command.setdefault(cmd, []).append(elapsed)

            except (LinkError, DeviceError) as error:
                elapsed = (time.perf_counter() - started) * 1000.0
                code = getattr(error, "code", type(error).__name__)

                failures.append({
                    "index": index,
                    "cmd": cmd,
                    "code": code,
                    "message": str(error)[:200],
                    "console": (getattr(error, "data", None) or {}).get(
                        "console"),
                    "elapsed_ms": round(elapsed, 1),
                })

            if (index + 1) % max(1, rounds // 10) == 0:
                print("      {}/{} requests, {} failed".format(
                    index + 1, rounds, len(failures)))
                sys.stdout.flush()

        summary = stats(latencies)

        self.report.add(
            "Transport", "{} request endurance".format(rounds),
            PASS if not failures else FAIL,
            "{}/{} first attempt; p50 {:.1f} p95 {:.1f} p99 {:.1f} "
            "max {:.1f} ms".format(
                first_attempt_ok, rounds,
                summary["median"], summary["p95"], summary["p99"],
                summary["max"]) if summary else "no successful request",
            evidence={
                "requests": rounds,
                "first_attempt_ok": first_attempt_ok,
                "latency_ms": summary,
                "per_command_ms": {
                    name: stats(values) for name, values in by_command.items()
                },
                "failures": failures,
                "corrupt_frames": self.link.corrupt_frames - corrupt_before,
                "salvaged_frames": self.link.salvaged_frames - salvaged_before,
            },
            attempts=rounds, passed=first_attempt_ok,
            failed=rounds - first_attempt_ok,
        )

        self.report.add(
            "Transport", "framing over endurance",
            PASS if self.link.corrupt_frames == corrupt_before else WARN,
            "{} corrupt, {} salvaged in {} requests".format(
                self.link.corrupt_frames - corrupt_before,
                self.link.salvaged_frames - salvaged_before, rounds),
            evidence={
                "corrupt": self.link.corrupt_frames - corrupt_before,
                "salvaged": self.link.salvaged_frames - salvaged_before,
                "damaged": [line[:400]
                            for line in self.link.damaged_lines],
                "prefix_lengths": describe_damage(self.link.damaged_lines),
            },
        )

        return True

    # ------------------------------------------------------------------
    # STAGE: reconnect  (moves nothing)
    # ------------------------------------------------------------------

    def stage_reconnect(self):
        """
        Open, ask, close - many times, and then longer sessions.

        THE PORT IS RELEASED FOR THIS STAGE. Every other stage runs on
        the campaign's single open link, deliberately, so that a failure
        cannot be blamed on reopening. This one is the opposite test: it
        exists to prove that reopening is safe, that the board is not
        reset by it, and that nothing accumulates over a hundred
        connections.
        """
        banner("STAGE reconnect - port open/close and session reuse")

        port = self.link.port

        # Hand the port back before opening it again from scratch.
        self.link.close()

        try:
            cycles = self.args.reconnect_cycles
            opens = []
            firsts = []
            failures = []
            uptimes = []

            for index in range(cycles):
                started = time.perf_counter()
                probe = SerialLink(port, timeout=20.0)

                try:
                    probe.open()
                    opened = (time.perf_counter() - started) * 1000.0

                    asked = time.perf_counter()
                    data = probe.request("ping", timeout=20.0)
                    first = (time.perf_counter() - asked) * 1000.0

                    opens.append(opened)
                    firsts.append(first)
                    uptimes.append(data.get("uptime_ms"))

                except (LinkError, DeviceError) as error:
                    failures.append({
                        "cycle": index,
                        "code": getattr(error, "code", type(error).__name__),
                        "message": str(error)[:200],
                    })

                finally:
                    probe.close()

            # THE BOARD MUST NOT HAVE REBOOTED. Uptime that goes
            # backwards across the campaign means opening the port
            # reset it, which is the fault serial_link.open() exists to
            # avoid - and it would silently destroy a synchronized
            # carousel position mid-mission.
            went_backwards = [
                (uptimes[i - 1], uptimes[i])
                for i in range(1, len(uptimes))
                if uptimes[i] is not None and uptimes[i - 1] is not None
                and uptimes[i] < uptimes[i - 1]
            ]

            open_summary = stats(opens)
            first_summary = stats(firsts)

            self.report.add(
                "Transport", "{} open/request/close".format(cycles),
                PASS if not failures else FAIL,
                "open p95 {:.0f} ms, first request p95 {:.0f} ms".format(
                    open_summary["p95"], first_summary["p95"]
                ) if open_summary else "no cycle completed",
                evidence={"open_ms": open_summary,
                          "first_request_ms": first_summary,
                          "failures": failures},
                attempts=cycles, passed=len(opens), failed=len(failures),
            )

            self.report.add(
                "Transport", "board survives reconnection",
                PASS if not went_backwards else FAIL,
                "uptime rose across all {} connections".format(len(uptimes))
                if not went_backwards
                else "uptime went BACKWARDS {} times - opening the port is "
                     "resetting the board".format(len(went_backwards)),
                evidence={"uptime_ms": uptimes,
                          "regressions": went_backwards},
            )

            # Longer sessions: several commands per connection, so a
            # per-session resource leak has somewhere to show up.
            sessions = self.args.reconnect_sessions
            session_failures = []
            per_session = []

            for index in range(sessions):
                probe = SerialLink(port, timeout=20.0)
                started = time.perf_counter()

                try:
                    probe.open()
                    probe.wait_online()

                    for _ in range(5):
                        probe.request("ping", timeout=20.0)

                    probe.request("get_status", timeout=20.0)

                    per_session.append(
                        (time.perf_counter() - started) * 1000.0)

                except (LinkError, DeviceError) as error:
                    session_failures.append({
                        "session": index,
                        "code": getattr(error, "code", type(error).__name__),
                        "message": str(error)[:200],
                    })

                finally:
                    probe.close()

            summary = stats(per_session)

            self.report.add(
                "Transport", "{} multi-command sessions".format(sessions),
                PASS if not session_failures else FAIL,
                "6 commands each; p95 {:.0f} ms per session".format(
                    summary["p95"]) if summary else "no session completed",
                evidence={"session_ms": summary,
                          "failures": session_failures},
                attempts=sessions, passed=len(per_session),
                failed=len(session_failures),
            )

        finally:
            # Give the campaign its link back, whatever happened.
            self.link.open()
            self.link.wait_online()

        return True

    # ------------------------------------------------------------------
    # STAGE: servo-comms  (moves nothing)
    # ------------------------------------------------------------------

    def stage_servo_comms(self):
        banner("STAGE servo-comms - ST3215 without movement")

        # 1. The CONFIGURED combination only. A 254-ID sweep is a last
        #    resort, not an opening move.
        data, error, elapsed = self.timed(
            self.link.servo_bus_scan, ids=[1], bauds=[1000000], swap=False,
            timeout=60.0,
        )

        if error is not None:
            self.report.add("ST3215", "bus scan (configured)", FAIL,
                            str(error))

        else:
            found = data.get("found") or []

            self.report.add(
                "ST3215", "bus scan (configured)",
                PASS if found else FAIL,
                "{} responder(s); {:.0f} ms".format(len(found), elapsed),
                evidence=data,
            )

        # 2. connect_servo through the production lifecycle.
        data, error, elapsed = self.timed(self.link.connect_servo)

        if error is not None:
            self.report.add("ST3215", "connect_servo", FAIL, str(error),
                            evidence=getattr(error, "data", None))

            return False

        self.servo_connected = True

        self.report.add(
            "ST3215", "connect_servo", PASS,
            "{:.0f} ms".format(elapsed), evidence=data,
        )

        # 3. Diagnostics: every register the driver knows how to read.
        data, error, elapsed = self.timed(self.link.servo_diagnostics)

        if error is not None:
            self.report.add("ST3215", "diagnostics", FAIL, str(error))

            return False

        steps = data.get("steps") or []
        bad = [s["step"] for s in steps if not s.get("ok")]

        self.report.add(
            "ST3215", "diagnostics", PASS if data.get("ok") else FAIL,
            "id={} mode={} torque={} baud={} steps ok {}/{}".format(
                data.get("servo_id", "?"), data.get("mode_name"),
                data.get("torque_enabled"), data.get("baud_reported"),
                len(steps) - len(bad), len(steps),
            ),
            evidence=data, attempts=len(steps),
            passed=len(steps) - len(bad), failed=len(bad),
        )

        if not data.get("mode_correct"):
            self.report.add(
                "ST3215", "operating mode", FAIL,
                "servo reports mode {} ({}), firmware needs {}".format(
                    data.get("mode"), data.get("mode_name"),
                    data.get("expected_mode"),
                ),
                evidence={"mode": data.get("mode"),
                          "expected": data.get("expected_mode")},
            )

        # 4. Transport soak: many consecutive register reads.
        rounds = self.args.servo_reads
        latencies = []
        failures = []
        errors_seen = []

        for index in range(rounds):
            data, error, elapsed = self.timed(self.link.get_servo_calibration)

            if error is None:
                latencies.append(elapsed)

            else:
                failures.append(index)
                errors_seen.append(str(error))

        summary = stats(latencies)

        self.report.add(
            "ST3215", "register read soak",
            PASS if not failures else FAIL,
            "{} reads, mean {:.1f} ms, max {:.1f} ms".format(
                rounds, summary["mean"], summary["max"]
            ) if summary else "no successful read",
            evidence={"latency_ms": summary, "errors": errors_seen[:5]},
            attempts=rounds, passed=rounds - len(failures),
            failed=len(failures),
        )

        # 5. Bus statistics kept by the driver itself: retries and
        #    checksum failures the transport absorbed silently.
        data, error, _ = self.timed(self.link.servo_diagnostics)

        if error is None:
            bus = data.get("bus") or {}

            trouble = sum(
                int(bus.get(key) or 0)
                for key in ("timeouts", "checksum_errors", "retries",
                            "bad_id", "malformed")
                if key in bus
            )

            self.report.add(
                "ST3215", "bus error counters",
                PASS if trouble == 0 else WARN,
                json.dumps(bus, sort_keys=True), evidence=bus,
            )

        return True

    # ------------------------------------------------------------------
    # STAGE: servo-move
    # ------------------------------------------------------------------

    def _move(self, subsystem, label, kind, repeat=1, degrees=None):
        """One production servo_test_move, recorded with its encoder."""
        data, error, elapsed = self.timed(
            self.link.servo_test_move, kind, repeat=repeat, degrees=degrees,
            confirm=True, timeout=120.0,
        )

        if error is not None:
            self.report.add(
                subsystem, label, FAIL, str(error),
                evidence=getattr(error, "data", None),
                attempts=1, passed=0, failed=1,
            )

            return None

        closing = data.get("closed_loop_error_counts")
        legs = data.get("legs") or []

        detail = "net {} counts, closing error {} counts ({} deg), {:.0f} ms".format(
            data.get("net_counts"), closing,
            data.get("closed_loop_error_deg"), elapsed,
        )

        verdict = PASS

        if closing is None:
            verdict = FAIL

        self.report.add(
            subsystem, label, verdict, detail,
            evidence={
                "kind": kind, "repeat": repeat, "degrees": degrees,
                "legs": legs, "net_counts": data.get("net_counts"),
                "start_position": data.get("start_position"),
                "end_position": data.get("end_position"),
                "closing_counts": closing,
                "closing_deg": data.get("closed_loop_error_deg"),
                "elapsed_ms": round(elapsed, 1),
            },
            attempts=1, passed=1 if verdict == PASS else 0,
            failed=0 if verdict == PASS else 1,
        )

        return data

    def stage_servo_move(self):
        banner("STAGE servo-move - progressive real movement")

        if not self.servo_connected and not self.stage_servo_comms():
            return False

        # Stage 1/2/3: 5, 10 then 15 degrees, out and back each time.
        for angle in (5.0, 10.0, 15.0):
            out = self._move("ST3215", "micro {:.0f} deg out".format(angle),
                             "degrees", degrees=angle)

            if out is None:
                return False

            back = self._move("ST3215", "micro {:.0f} deg back".format(angle),
                              "degrees", degrees=-angle)

            if back is None:
                return False

        return True

    def stage_servo_slots(self):
        banner("STAGE servo-slots - carousel-scale movement")

        if not self.servo_connected and not self.stage_servo_comms():
            return False

        for kind, label in (
            ("slot_forward", "one slot forward"),
            ("slot_reverse", "one slot reverse"),
            ("slot_out_and_back", "slot out and back"),
            ("half_turn_forward", "180 deg forward"),
            ("half_turn_reverse", "180 deg reverse"),
            ("out_and_back", "180 deg out and back"),
            ("wrap", "encoder seam 4095->0"),
        ):
            if self._move("ST3215", label, kind) is None:
                return False

        return True

    # ------------------------------------------------------------------
    # STAGE: servo-repeat
    # ------------------------------------------------------------------

    def _campaign(self, label, kind, cycles, degrees=None):
        """Repeat one symmetrical movement and characterise it."""
        closings = []
        durations = []
        failures = 0

        for index in range(cycles):
            data, error, elapsed = self.timed(
                self.link.servo_test_move, kind, repeat=1, degrees=degrees,
                confirm=True, timeout=120.0,
            )

            if error is not None:
                failures += 1

                continue

            closings.append(data.get("closed_loop_error_counts"))
            durations.append(elapsed)

        closing_stats = stats([abs(c) for c in closings if c is not None])
        duration_stats = stats(durations)

        verdict = PASS if failures == 0 else FAIL

        self.report.add(
            "ST3215", label, verdict,
            "|error| mean {:.2f} max {:.0f} counts; {:.0f} ms/cycle".format(
                closing_stats["mean"], closing_stats["max"],
                duration_stats["mean"],
            ) if closing_stats and duration_stats else "no completed cycle",
            evidence={"closing_counts": closings,
                      "abs_closing_stats": closing_stats,
                      "duration_ms": duration_stats},
            attempts=cycles, passed=cycles - failures, failed=failures,
        )

        return closings

    def stage_servo_repeat(self):
        banner("STAGE servo-repeat - repeatability campaign")

        if not self.servo_connected and not self.stage_servo_comms():
            return False

        self._campaign("micro repeatability x{}".format(self.args.micro_cycles),
                       "degrees", self.args.micro_cycles, degrees=5.0)

        # The 5 deg legs above are one-way; put it back where it started.
        self._move("ST3215", "micro return", "degrees",
                   degrees=-5.0 * self.args.micro_cycles)

        self._campaign("slot out/back x{}".format(self.args.slot_cycles),
                       "slot_out_and_back", self.args.slot_cycles)

        self._campaign("180 out/back x{}".format(self.args.half_cycles),
                       "out_and_back", self.args.half_cycles)

        return True

    # ------------------------------------------------------------------
    # STAGE: sensor
    # ------------------------------------------------------------------

    def stage_sensor(self):
        banner("STAGE sensor - AS7265x")

        data, error, elapsed = self.timed(
            self.link.sensor_test_raw, force_reinit=False, repeats=1
        )

        if error is not None:
            self.report.add("AS7265x", "sensor_test_raw", FAIL, str(error),
                            evidence=getattr(error, "data", None))

            return False

        checks = data.get("checks") or []
        bad = [c["stage"] for c in checks if not c.get("ok")]

        self.report.add(
            "AS7265x", "sensor_test_raw",
            PASS if data.get("ok") and not bad else FAIL,
            "{}/{} stages ok, {:.0f} ms".format(
                len(checks) - len(bad), len(checks), elapsed
            ),
            evidence={"checks": checks, "bus": data.get("bus"),
                      "settings": data.get("sensor_settings")},
            attempts=len(checks), passed=len(checks) - len(bad),
            failed=len(bad),
        )

        settings = data.get("sensor_settings") or {}

        self.report.add(
            "AS7265x", "config readback", PASS, json.dumps(
                settings, sort_keys=True)[:160],
            evidence=settings,
        )

        # Lamps, each verified by register readback.
        data, error, elapsed = self.timed(self.link.led_test, hold_ms=300)

        if error is not None:
            self.report.add("AS7265x", "led_test", FAIL, str(error))

        else:
            # The firmware already decides this, per lamp and overall:
            # `ok` is on_readback AND off_readback, where off_readback
            # is "the lamp read back OFF" - true is good. Re-deriving
            # that here got the polarity backwards once and reported
            # three working lamps as failed, so take the device's
            # verdict and check the fields it was computed from.
            lamps = data.get("lamps") or []
            problems = [lamp for lamp in lamps if not lamp.get("ok")]
            all_off = data.get("all_off")

            self.report.add(
                "AS7265x", "led_test",
                PASS if lamps and not problems and all_off else FAIL,
                "{} lamps on+off verified; all_off={}".format(
                    len(lamps), all_off
                ),
                evidence=data, attempts=len(lamps),
                passed=len(lamps) - len(problems), failed=len(problems),
            )

        return True

    def _block(self, illumination, repeats):
        """One acquire_block, returned as per-channel column lists."""
        data, error, elapsed = self.timed(
            self.link.acquire_block, illumination, repeats
        )

        if error is not None:
            return None, error, elapsed

        return data, None, elapsed

    def stage_sensor_repeat(self):
        banner("STAGE sensor-repeat - acquisition and noise")

        repeats = self.args.spectra

        for illumination in ("dark", "white", "uv", "ir"):
            data, error, elapsed = self._block(illumination, repeats)

            if error is not None:
                self.report.add(
                    "AS7265x", "{} block".format(illumination), FAIL,
                    str(error),
                )

                continue

            acquisitions = data.get("acquisitions") or []
            channels = sorted(acquisitions[0].keys()) if acquisitions else []

            per_channel = {}
            zero_channels = []
            bad_values = []

            for channel in channels:
                column = [a.get(channel) for a in acquisitions]

                for value in column:
                    if value is None or (
                        isinstance(value, float)
                        and (math.isnan(value) or math.isinf(value))
                    ):
                        bad_values.append((channel, value))

                clean = [v for v in column
                         if isinstance(v, (int, float))
                         and not math.isnan(v) and not math.isinf(v)]

                per_channel[channel] = stats(clean)

                if clean and all(v == 0 for v in clean):
                    zero_channels.append(channel)

            identical = len({json.dumps(a, sort_keys=True)
                             for a in acquisitions}) == 1 and repeats > 1

            cvs = [s["cv_pct"] for s in per_channel.values()
                   if s and "cv_pct" in s]

            verdict = PASS

            if bad_values or len(channels) != 18:
                verdict = FAIL

            elif identical:
                verdict = FAIL

            elif zero_channels:
                verdict = WARN

            self.report.add(
                "AS7265x", "{} x{}".format(illumination, repeats), verdict,
                "{} channels, median CV {:.2f}%, {} zero, {:.0f} ms".format(
                    len(channels),
                    statistics.median(cvs) if cvs else float("nan"),
                    len(zero_channels), elapsed,
                ),
                evidence={
                    "channels": len(channels),
                    "zero_channels": zero_channels,
                    "identical_repeats": identical,
                    "bad_values": bad_values,
                    "data_ready_wait_ms": data.get("data_ready_wait_ms"),
                    "per_channel": per_channel,
                    "elapsed_ms": round(elapsed, 1),
                },
                attempts=repeats, passed=repeats if verdict != FAIL else 0,
                failed=0 if verdict != FAIL else repeats,
            )

        # Complete triads, the shape a real measurement uses.
        #
        # THE FAILURES ARE THE EVIDENCE. This loop used to `continue`
        # past an error without recording anything but a count, so a
        # campaign that failed 2 of 3 triads reported "mean 24677 ms"
        # and nothing whatever about why - and the two failures had
        # cost 24 seconds of sensor time each. The code was
        # RESPONSE_TOO_LARGE every time, which named the fault
        # precisely; it was simply thrown away.
        triad_times = []
        triad_failures = []

        # A RETRY THAT SUCCEEDS IS STILL EVIDENCE. acquire_triad is sent
        # with retries=1, so a frame damaged in transit is re-requested
        # and the caller sees a success that took twice as long. Without
        # these counters the only trace is an outlier in the latency -
        # measured here as one triad of 29961 ms against a p99 of 15100,
        # which is exactly one extra acquisition. §10, §15.
        corrupt_before = self.link.corrupt_frames
        salvaged_before = self.link.salvaged_frames
        damaged_before = len(self.link.damaged_lines)

        for index in range(self.args.triads):
            data, error, elapsed = self.timed(
                self.link.acquire_triad, repeats=self.args.spectra
            )

            if error is not None:
                triad_failures.append({
                    "index": index,
                    "code": getattr(error, "code", type(error).__name__),
                    "message": str(error)[:300],
                    "data": getattr(error, "data", None),
                    "elapsed_ms": round(elapsed, 1),
                })

                continue

            blocks = data.get("illuminations") or {}
            shape = {
                name: len(block.get("acquisitions") or [])
                for name, block in blocks.items()
            }
            widths = sorted({
                len(spectrum)
                for block in blocks.values()
                for spectrum in (block.get("acquisitions") or [])
            })

            if sorted(blocks) != ["ir", "uv", "white"] or widths != [18]:
                triad_failures.append({
                    "index": index,
                    "code": "TRIAD_SHAPE",
                    "message": "blocks={} repeats={} channel widths={}".format(
                        sorted(blocks), shape, widths),
                    "elapsed_ms": round(elapsed, 1),
                })

                continue

            triad_times.append(elapsed)

        summary = stats(triad_times)

        self.report.add(
            "AS7265x", "triad x{}".format(self.args.triads),
            PASS if not triad_failures else FAIL,
            "{}{}".format(
                "mean {:.0f} ms, max {:.0f} ms".format(
                    summary["mean"], summary["max"]
                ) if summary else "no completed triad",
                "" if not triad_failures else "; failed: " + ", ".join(
                    sorted({f["code"] for f in triad_failures})),
            ),
            evidence={"latency_ms": summary, "failures": triad_failures},
            attempts=self.args.triads,
            passed=len(triad_times), failed=len(triad_failures),
        )

        corrupt = self.link.corrupt_frames - corrupt_before
        salvaged = self.link.salvaged_frames - salvaged_before
        damaged = self.link.damaged_lines[damaged_before:]

        self.report.add(
            "AS7265x", "triad framing", PASS if not corrupt else WARN,
            "{}/{} triads delivered on the FIRST attempt; {} frame(s) "
            "damaged in transit, {} salvaged".format(
                len(triad_times) - corrupt, self.args.triads,
                corrupt, salvaged),
            evidence={
                "corrupt_frames": corrupt,
                "salvaged_frames": salvaged,
                "first_attempt_rate_pct": (
                    round(100.0 * (len(triad_times) - corrupt)
                          / self.args.triads, 2)
                    if self.args.triads else None
                ),
                "prefix_lengths": describe_damage(damaged),
                "damaged": [line[:400] for line in damaged],
            },
            attempts=self.args.triads,
            passed=len(triad_times) - corrupt, failed=corrupt,
        )

        return True

    # ------------------------------------------------------------------
    # STAGE: integration
    # ------------------------------------------------------------------

    def stage_integration(self):
        banner("STAGE integration - servo and sensor together")

        if not self.servo_connected and not self.stage_servo_comms():
            return False

        # A stationary baseline first, so "after movement" has something
        # to be compared against.
        baseline, error, _ = self._block("white", self.args.spectra)

        if error is not None:
            self.report.add("Integration", "baseline spectrum", FAIL,
                            str(error))

            return False

        def mean_of(block):
            values = []

            for acquisition in block.get("acquisitions") or []:
                values.extend(
                    v for v in acquisition.values()
                    if isinstance(v, (int, float))
                )

            return statistics.fmean(values) if values else None

        base_mean = mean_of(baseline)

        self.report.add("Integration", "baseline spectrum", PASS,
                        "mean channel value {:.1f}".format(base_mean),
                        evidence={"mean": base_mean})

        failures = 0
        deltas = []

        for index in range(self.args.interleave):
            moved = self._move("Integration",
                               "interleave move {}".format(index + 1),
                               "slot_out_and_back")

            if moved is None:
                failures += 1

                continue

            after, error, _ = self._block("white", self.args.spectra)

            if error is not None:
                self.report.add(
                    "Integration",
                    "spectrum after move {}".format(index + 1), FAIL,
                    str(error),
                )

                failures += 1

                continue

            after_mean = mean_of(after)
            delta_pct = (
                100.0 * (after_mean - base_mean) / base_mean
                if base_mean else None
            )

            deltas.append(delta_pct)

            self.report.add(
                "Integration", "spectrum after move {}".format(index + 1),
                PASS, "mean {:.1f} ({:+.2f}% vs baseline)".format(
                    after_mean, delta_pct
                ),
                evidence={"mean": after_mean, "delta_pct": delta_pct},
            )

        # A healthy board answers ping right after all of that.
        _, error, _ = self.timed(self.link.ping)

        self.report.add(
            "Integration", "link after movement",
            PASS if error is None else FAIL,
            "corrupt frames {}, salvaged {}".format(
                self.link.corrupt_frames, self.link.salvaged_frames
            ),
            evidence={"corrupt": self.link.corrupt_frames,
                      "salvaged": self.link.salvaged_frames},
            attempts=self.args.interleave,
            passed=self.args.interleave - failures, failed=failures,
        )

        return True

    # ------------------------------------------------------------------
    # STAGE: negative
    # ------------------------------------------------------------------

    def stage_negative(self):
        banner("STAGE negative - error injection")

        cases = [
            ("unknown command", "definitely_not_a_command", {}),
            ("bad slot", "select_slot", {"slot": 99}),
            ("bad illumination", "acquire_block",
             {"illumination": "gamma", "repeats": 1}),
            ("bad repeats", "acquire_block",
             {"illumination": "white", "repeats": -4}),
            ("bad move kind", "servo_test_move",
             {"kind": "somersault", "confirm": True}),
            ("move without confirm", "servo_test_move",
             {"kind": "slot_forward"}),
            ("bad fine adjust", "fine_adjust", {"degrees": 400}),
        ]

        rejected = 0
        accepted = []

        for label, cmd, payload in cases:
            data, error, elapsed = self.timed(
                self.link.request, cmd, timeout=20.0, **payload
            )

            if isinstance(error, DeviceError):
                rejected += 1

                self.report.add(
                    "Negative", label, PASS,
                    "{}: {}".format(error.code, error.message[:60]),
                    evidence={"code": error.code},
                )

            elif error is not None:
                accepted.append(label)

                self.report.add("Negative", label, FAIL,
                                "transport failure: {}".format(error))

            else:
                accepted.append(label)

                self.report.add("Negative", label, FAIL,
                                "the firmware ACCEPTED it",
                                evidence=data)

            # The board must still be serving after every one of these.
            _, ping_error, _ = self.timed(self.link.ping)

            if ping_error is not None:
                self.report.add("Negative", "{} - alive after".format(label),
                                FAIL, str(ping_error))

        self.report.add(
            "Negative", "all rejected cleanly",
            PASS if not accepted else FAIL,
            "{}/{} rejected".format(rejected, len(cases)),
            evidence={"accepted": accepted}, attempts=len(cases),
            passed=rejected, failed=len(cases) - rejected,
        )

        return True

    # ------------------------------------------------------------------
    # STAGE: e2e
    # ------------------------------------------------------------------

    def stage_e2e(self):
        banner("STAGE e2e - complete measure_raw cycles")

        status, error, _ = self.timed(self.link.get_status)

        if error is not None:
            self.report.add("Measurement", "status", FAIL, str(error))

            return False

        carousel = status.get("carousel") or {}

        if not carousel.get("position_valid"):
            self.report.add(
                "Measurement", "carousel origin", SKIP,
                "position is not synchronized; sync_position needs a "
                "physically known slot and must never be faked",
                evidence=carousel,
            )

            return False

        cycles = self.args.e2e_cycles
        durations = []
        failures = 0

        for index in range(cycles):
            data, error, elapsed = self.timed(
                self.link.measure_raw, self.args.e2e_slot,
                sample_id="hil{:03d}".format(index + 1),
                repeats=self.args.spectra,
            )

            if error is not None:
                failures += 1

                self.report.add("Measurement", "cycle {}".format(index + 1),
                                FAIL, str(error))

                continue

            durations.append(elapsed)

            self.report.add(
                "Measurement", "cycle {}".format(index + 1),
                PASS if data.get("home_restored") else FAIL,
                "home_restored={} bulbs_off={} {:.0f} ms".format(
                    data.get("home_restored"), data.get("bulbs_off"), elapsed
                ),
                evidence={"home_restored": data.get("home_restored"),
                          "bulbs_off": data.get("bulbs_off"),
                          "carousel": data.get("carousel"),
                          "elapsed_ms": round(elapsed, 1)},
            )

        summary = stats(durations)

        self.report.add(
            "Measurement", "cycles x{}".format(cycles),
            PASS if not failures else FAIL,
            "mean {:.0f} ms".format(summary["mean"]) if summary
            else "no completed cycle",
            evidence=summary, attempts=cycles, passed=cycles - failures,
            failed=failures,
        )

        return True

    # ------------------------------------------------------------------
    # STAGE: memory
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # STAGE: freshness  (moves nothing)
    # ------------------------------------------------------------------

    def _heap(self):
        """Free heap and largest contiguous block, or an empty dict."""
        data, error, _ = self.timed(self.link.get_status)

        if error is not None:
            return {}

        return data.get("memory") or {}

    def stage_freshness(self):
        """
        Prove every acquisition is a NEW conversion, not a cached one.

        SPECTRUM SHAPE ALONE CANNOT SHOW THIS. Repeated readings of a
        static scene are SUPPOSED to be similar, and identical values
        are expected wherever the signal lands on the same ADC count -
        this bench sees exactly that on the dim channels. So identical
        numbers are not evidence of staleness and different numbers are
        not evidence of freshness. §29, §30.

        The evidence used here is the sensor's own state and timing:

            every acquisition waits a real DATA_READY interval, which a
            returned cache would not

            the interval matches the configured integration time, so
            the conversion that produced the frame was started AFTER
            the lamp was switched

            switching the lamp changes the spectrum, which a frame
            carried over from the previous illumination could not do
        """
        banner("STAGE freshness - every frame is a new conversion")

        # A deliberately non-repeating order. A cache that returned the
        # previous frame would be caught by the repeats of a lamp that
        # is NOT adjacent to itself.
        order = ("white", "uv", "ir", "uv", "white", "ir", "white")

        frames = []
        failures = []

        for index, illumination in enumerate(order):
            data, error, elapsed = self._block(illumination, 1)

            if error is not None:
                failures.append({
                    "index": index,
                    "illumination": illumination,
                    "code": getattr(error, "code", type(error).__name__),
                    "message": str(error)[:200],
                })

                continue

            acquisitions = data.get("acquisitions") or []
            waits = data.get("data_ready_wait_ms") or []

            frames.append({
                "index": index,
                "requested": illumination,
                "reported": data.get("illumination"),
                "spectrum": acquisitions[0] if acquisitions else {},
                "data_ready_ms": waits[0] if waits else None,
                "elapsed_ms": round(elapsed, 1),
            })

        if failures or len(frames) < len(order):
            self.report.add(
                "AS7265x", "freshness sequence", FAIL,
                "{} of {} acquisitions failed".format(
                    len(failures), len(order)),
                evidence={"failures": failures},
                attempts=len(order), passed=len(frames),
                failed=len(failures),
            )

            return True

        # 1. THE LABEL MATCHES WHAT WAS ASKED FOR.
        mislabelled = [
            frame for frame in frames
            if frame["reported"] != frame["requested"]
        ]

        self.report.add(
            "AS7265x", "illumination label", PASS if not mislabelled else FAIL,
            "every frame came back under the lamp it was asked for"
            if not mislabelled
            else "{} frame(s) reported a different lamp".format(
                len(mislabelled)),
            evidence={"mislabelled": mislabelled},
            attempts=len(frames), passed=len(frames) - len(mislabelled),
            failed=len(mislabelled),
        )

        # 2. EVERY FRAME WAITED FOR A CONVERSION.
        #
        # A cached frame is returned immediately. A real one-shot cannot
        # complete faster than its integration time, so a wait far below
        # that would mean the firmware read a conversion it did not
        # start.
        waits = [frame["data_ready_ms"] for frame in frames
                 if frame["data_ready_ms"] is not None]
        summary = stats(waits)
        instant = [frame for frame in frames
                   if not frame["data_ready_ms"]]

        self.report.add(
            "AS7265x", "DATA_READY per acquisition",
            PASS if waits and not instant else FAIL,
            "min {:.0f} ms, median {:.0f} ms, max {:.0f} ms over {} "
            "acquisitions".format(
                summary["min"], summary["median"], summary["max"],
                summary["n"]) if summary else "no DATA_READY reported",
            evidence={"data_ready_ms": summary,
                      "instant_frames": instant},
            attempts=len(frames), passed=len(waits) - len(instant),
            failed=len(instant),
        )

        # 3. CHANGING THE LAMP CHANGES THE SPECTRUM.
        #
        # Consecutive frames under DIFFERENT lamps that are identical
        # channel for channel would mean the second frame was the
        # first one handed back again. Frames under the SAME lamp are
        # not compared: on a static scene they are legitimately allowed
        # to be identical, and calling that a fault is exactly the
        # mistake §30 warns against.
        carried_over = []

        for previous, current in zip(frames, frames[1:]):
            if previous["requested"] == current["requested"]:
                continue

            if previous["spectrum"] and (
                previous["spectrum"] == current["spectrum"]
            ):
                carried_over.append({
                    "from": previous["requested"],
                    "to": current["requested"],
                    "index": current["index"],
                })

        transitions = sum(
            1 for previous, current in zip(frames, frames[1:])
            if previous["requested"] != current["requested"]
        )

        self.report.add(
            "AS7265x", "frame is not carried over",
            PASS if not carried_over else FAIL,
            "{} lamp changes, none returned the previous frame".format(
                transitions)
            if not carried_over
            else "{} lamp change(s) returned an IDENTICAL frame".format(
                len(carried_over)),
            evidence={"carried_over": carried_over,
                      "transitions": transitions},
            attempts=transitions, passed=transitions - len(carried_over),
            failed=len(carried_over),
        )

        # 4. HOW MUCH THE SAME LAMP REPEATS ITSELF, reported and not
        #    judged. This is the number that would look alarming without
        #    the three checks above, and it is exactly the number a
        #    static ceiling scene is expected to produce.
        same_lamp = {}

        for name in ("white", "uv", "ir"):
            spectra = [frame["spectrum"] for frame in frames
                       if frame["requested"] == name]

            if len(spectra) < 2:
                continue

            identical = sum(
                1 for a, b in zip(spectra, spectra[1:]) if a == b
            )
            channels_equal = []

            for a, b in zip(spectra, spectra[1:]):
                channels_equal.append(sum(
                    1 for channel in a if a.get(channel) == b.get(channel)
                ))

            same_lamp[name] = {
                "frames": len(spectra),
                "identical_pairs": identical,
                "channels_equal_per_pair": channels_equal,
            }

        self.report.add(
            "AS7265x", "repeat similarity (reported only)", PASS,
            "; ".join(
                "{} {}/18 channels equal".format(
                    name, entry["channels_equal_per_pair"][0])
                for name, entry in same_lamp.items()
                if entry["channels_equal_per_pair"]
            ) or "not enough repeats to compare",
            evidence={"same_lamp": same_lamp,
                      "note": "identical repeats of ONE lamp on a static "
                              "scene are expected and are not a fault"},
        )

        return True

    # ------------------------------------------------------------------
    # STAGE: sensor-endurance  (moves nothing)
    # ------------------------------------------------------------------

    CHECKPOINTS = (1, 10, 25, 50, 100, 200, 500)

    def stage_sensor_endurance(self):
        """
        Many acquisitions in a row, without resetting the board.

        THE HISTORICAL FAULT APPEARED AFTER MANY MEASUREMENTS, so the
        thing being tested is cumulative runtime rather than any single
        acquisition. Everything that could drift is sampled at
        checkpoints: heap, largest contiguous block, acquisition time,
        DATA_READY, and the number of channels that came back. §33-§38.
        """
        banner("STAGE sensor-endurance - {} acquisitions per illumination"
               .format(self.args.endurance))

        rounds = self.args.endurance

        for illumination in self.args.endurance_lamps.split(","):
            illumination = illumination.strip()

            if not illumination:
                continue

            self._endurance_run(illumination, rounds)

        return True

    def _endurance_run(self, illumination, rounds):
        durations = []
        waits = []
        failures = []
        checkpoints = []
        widths = {}
        zero_frames = 0
        previous = None
        repeated_frames = 0

        start_heap = self._heap()

        for index in range(1, rounds + 1):
            data, error, elapsed = self._block(illumination, 1)

            if error is not None:
                failures.append({
                    "index": index,
                    "code": getattr(error, "code", type(error).__name__),
                    "message": str(error)[:200],
                    "elapsed_ms": round(elapsed, 1),
                })

            else:
                acquisitions = data.get("acquisitions") or []
                spectrum = acquisitions[0] if acquisitions else {}
                block_waits = data.get("data_ready_wait_ms") or []

                durations.append(elapsed)
                widths[len(spectrum)] = widths.get(len(spectrum), 0) + 1

                if block_waits and block_waits[0] is not None:
                    waits.append(block_waits[0])

                values = list(spectrum.values())

                if values and all(
                    value == 0 for value in values
                    if isinstance(value, (int, float))
                ):
                    zero_frames += 1

                if previous is not None and spectrum == previous:
                    repeated_frames += 1

                previous = spectrum

            if index in self.CHECKPOINTS or index == rounds:
                heap = self._heap()
                recent = durations[-10:]

                checkpoints.append({
                    "acquisition": index,
                    "free": heap.get("free"),
                    "largest_block": heap.get("largest_block"),
                    "allocated": heap.get("allocated"),
                    "last_ms": round(durations[-1], 1) if durations else None,
                    "recent_mean_ms": (
                        round(statistics.fmean(recent), 1) if recent else None
                    ),
                    "failures_so_far": len(failures),
                })

                print("      {:<5} {:>4}/{:<4} free={} largest={} "
                      "recent_mean={} ms".format(
                          illumination, index, rounds,
                          heap.get("free"), heap.get("largest_block"),
                          checkpoints[-1]["recent_mean_ms"]))
                sys.stdout.flush()

        end_heap = self._heap()
        summary = stats(durations)
        wait_summary = stats(waits)

        # THE CHANNEL COUNT IS NOT NEGOTIABLE. 18 every time or the
        # acquisition is not a measurement.
        wrong_width = {
            width: count for width, count in widths.items() if width != 18
        }

        verdict = PASS

        if failures or wrong_width or zero_frames:
            verdict = FAIL

        self.report.add(
            "AS7265x", "{} x{}".format(illumination, rounds), verdict,
            "{}/{} ok; {:.0f} ms median, p95 {:.0f}, max {:.0f}".format(
                len(durations), rounds,
                summary["median"], summary["p95"], summary["max"]
            ) if summary else "no acquisition completed",
            evidence={
                "duration_ms": summary,
                "data_ready_ms": wait_summary,
                "channel_widths": widths,
                "zero_frames": zero_frames,
                "identical_consecutive_frames": repeated_frames,
                "failures": failures,
                "checkpoints": checkpoints,
                "heap_start": start_heap,
                "heap_end": end_heap,
            },
            attempts=rounds, passed=len(durations), failed=len(failures),
        )

        # DRIFT. A campaign that succeeds while getting steadily slower
        # or steadily poorer is still a finding: acquisition 1 and
        # acquisition N must look alike. §37.
        if len(durations) >= 20:
            first = durations[:10]
            last = durations[-10:]
            change = statistics.fmean(last) - statistics.fmean(first)

            self.report.add(
                "AS7265x", "{} latency drift".format(illumination),
                PASS if abs(change) < 250 else WARN,
                "first 10 mean {:.0f} ms, last 10 mean {:.0f} ms, "
                "{:+.0f} ms".format(
                    statistics.fmean(first), statistics.fmean(last), change),
                evidence={"first_10_ms": round(statistics.fmean(first), 1),
                          "last_10_ms": round(statistics.fmean(last), 1),
                          "change_ms": round(change, 1)},
            )

        if start_heap.get("free") and end_heap.get("free"):
            drift = end_heap["free"] - start_heap["free"]

            self.report.add(
                "AS7265x", "{} heap over {} acquisitions".format(
                    illumination, rounds),
                PASS if drift > -8000 else FAIL,
                "start {} B, end {} B, {:+d} B; largest block {} -> {}".format(
                    start_heap["free"], end_heap["free"], drift,
                    start_heap.get("largest_block"),
                    end_heap.get("largest_block")),
                evidence={"start": start_heap, "end": end_heap,
                          "drift": drift},
            )

        return True

    def stage_memory(self):
        banner("STAGE memory - heap across repeated work")

        heaps = []

        for index in range(self.args.memory_rounds):
            data, error, _ = self.timed(self.link.get_status)

            if error is None:
                memory = data.get("memory") or {}
                free = memory.get("free") or memory.get("mem_free")

                if free is not None:
                    heaps.append(int(free))

            self.timed(self.link.acquire_block, "white", 1)

        if not heaps:
            self.report.add("ESP32", "heap tracking", SKIP,
                            "get_status reports no memory figure")

            return True

        drift = heaps[-1] - heaps[0]

        self.report.add(
            "ESP32", "heap across {} rounds".format(len(heaps)),
            PASS if drift > -20000 else FAIL,
            "start {} B, end {} B, drift {:+d} B".format(
                heaps[0], heaps[-1], drift
            ),
            evidence={"heaps": heaps, "drift": drift},
        )

        return True


# ======================================================================
# entry point
# ======================================================================

MOVING_STAGES = {"servo-move", "servo-slots", "servo-repeat",
                 "integration", "e2e"}

STAGE_ORDER = ("link", "transport", "reconnect", "servo-comms",
               "servo-move", "servo-slots", "servo-repeat", "sensor",
               "sensor-repeat", "freshness", "sensor-endurance",
               "integration", "negative", "memory", "e2e")


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Real-hardware validation for the Freya science module."
    )

    parser.add_argument("--port", required=True,
                        help="serial port of the ESP32, e.g. COM4")
    parser.add_argument("--stage", action="append", default=None,
                        choices=STAGE_ORDER + ("all",),
                        help="stage to run; repeatable. Default: every "
                             "stage that does not move anything.")
    parser.add_argument("--move", action="store_true",
                        help="permit stages that physically turn the "
                             "carousel")
    parser.add_argument("--json", default=None,
                        help="write the full evidence record here")

    parser.add_argument("--link-pings", type=int, default=20)
    parser.add_argument("--transport-samples", type=int, default=40,
                        help="latency samples per command shape")
    parser.add_argument("--transport-requests", type=int, default=200,
                        help="lightweight requests in the endurance run")
    parser.add_argument("--reconnect-cycles", type=int, default=20,
                        help="open -> request -> close cycles")
    parser.add_argument("--reconnect-sessions", type=int, default=10,
                        help="longer sessions with several commands each")
    parser.add_argument("--endurance", type=int, default=100,
                        help="acquisitions per illumination in the "
                             "sensor-endurance stage")
    parser.add_argument("--endurance-lamps", default="white,uv,ir",
                        help="which lamps the endurance stage runs")
    parser.add_argument("--servo-reads", type=int, default=40)
    parser.add_argument("--micro-cycles", type=int, default=10)
    parser.add_argument("--slot-cycles", type=int, default=4)
    parser.add_argument("--half-cycles", type=int, default=5)
    parser.add_argument("--spectra", type=int, default=5)
    parser.add_argument("--triads", type=int, default=3)
    parser.add_argument("--interleave", type=int, default=3)
    parser.add_argument("--memory-rounds", type=int, default=8)
    parser.add_argument("--e2e-cycles", type=int, default=5)
    parser.add_argument("--e2e-slot", type=int, default=1)

    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])

    if args.stage is None:
        stages = [s for s in STAGE_ORDER if s not in MOVING_STAGES]

    elif "all" in args.stage:
        stages = list(STAGE_ORDER)

    else:
        stages = [s for s in STAGE_ORDER if s in args.stage]

    blocked = [s for s in stages if s in MOVING_STAGES and not args.move]

    if blocked:
        print("Skipping {} - they turn the carousel; pass --move.".format(
            ", ".join(blocked)
        ))

        stages = [s for s in stages if s not in blocked]

    report = Report()

    banner("Freya HARDWARE-IN-THE-LOOP validation   port={}  stages={}".format(
        args.port, ",".join(stages)
    ))

    link = SerialLink(args.port, timeout=20.0)

    try:
        link.open()
        link.wait_online()

        campaign = Campaign(link, report, args)

        methods = {
            "link": campaign.stage_link,
            "transport": campaign.stage_transport,
            "reconnect": campaign.stage_reconnect,
            "servo-comms": campaign.stage_servo_comms,
            "servo-move": campaign.stage_servo_move,
            "servo-slots": campaign.stage_servo_slots,
            "servo-repeat": campaign.stage_servo_repeat,
            "sensor": campaign.stage_sensor,
            "sensor-repeat": campaign.stage_sensor_repeat,
            "freshness": campaign.stage_freshness,
            "sensor-endurance": campaign.stage_sensor_endurance,
            "integration": campaign.stage_integration,
            "negative": campaign.stage_negative,
            "memory": campaign.stage_memory,
            "e2e": campaign.stage_e2e,
        }

        for name in stages:
            try:
                methods[name]()

            except (LinkError, DeviceError) as error:
                report.add(name, "stage aborted", FAIL, str(error))

            except KeyboardInterrupt:
                report.add(name, "stage interrupted", FAIL,
                           "stopped by the operator")

                break

    except LinkError as error:
        print("LINK FAILURE [{}] {}".format(error.code, error.message))

        return 2

    finally:
        # ALWAYS. A held COM4 is the next session's PORT_BUSY.
        link.close()

    banner("RESULTS")
    print(report.table())

    print()

    for subsystem, verdict in report.summary():
        print("   {:<14} {}".format(subsystem, verdict))

    if args.json:
        Path(args.json).write_text(
            json.dumps({"port": args.port, "stages": stages,
                        "checks": report.checks}, indent=2),
            encoding="utf-8",
        )

        print("\nevidence written to {}".format(args.json))

    failed = [c for c in report.checks if c["verdict"] == FAIL]

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
