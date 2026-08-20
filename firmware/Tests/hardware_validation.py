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

    result = {
        "n": len(numbers),
        "mean": round(statistics.fmean(numbers), 4),
        "min": round(min(numbers), 4),
        "max": round(max(numbers), 4),
        "range": round(max(numbers) - min(numbers), 4),
        "sd": round(statistics.stdev(numbers), 4) if len(numbers) > 1 else 0.0,
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
        triad_times = []
        triad_failures = 0

        for _ in range(self.args.triads):
            data, error, elapsed = self.timed(
                self.link.acquire_triad, repeats=self.args.spectra
            )

            if error is not None:
                triad_failures += 1

                continue

            triad_times.append(elapsed)

        summary = stats(triad_times)

        self.report.add(
            "AS7265x", "triad x{}".format(self.args.triads),
            PASS if not triad_failures else FAIL,
            "mean {:.0f} ms, max {:.0f} ms".format(
                summary["mean"], summary["max"]
            ) if summary else "no completed triad",
            evidence=summary, attempts=self.args.triads,
            passed=self.args.triads - triad_failures, failed=triad_failures,
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

STAGE_ORDER = ("link", "servo-comms", "servo-move", "servo-slots",
               "servo-repeat", "sensor", "sensor-repeat", "integration",
               "negative", "memory", "e2e")


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
            "servo-comms": campaign.stage_servo_comms,
            "servo-move": campaign.stage_servo_move,
            "servo-slots": campaign.stage_servo_slots,
            "servo-repeat": campaign.stage_servo_repeat,
            "sensor": campaign.stage_sensor,
            "sensor-repeat": campaign.stage_sensor_repeat,
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
