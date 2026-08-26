"""
A deterministic fake transport. FRAMEWORK-ONLY. Never evidence.

WHAT THIS PROVES, AND WHAT IT CANNOT

It proves the harness: that a success path produces the checks it should,
that a timeout becomes the status it should, that cleanup runs after a
failure, that an abort keeps its evidence, that a missing capability
produces BLOCKED rather than a false pass.

It proves NOTHING about the hardware. A fake ST3215 that answers
"position 2048" says the framework can read a position; it says nothing
about whether a real encoder tracks a real mechanism, which is the
entire open question this campaign exists to settle.

That is why every result produced against this transport carries
`Evidence.SELFTEST`, why `TestResult` refuses to hold PASS with a
non-hardware evidence class, and why the runner turns a would-be pass
into SKIPPED with the reason attached when the transport is fake.

HOW IT SUBSTITUTES

At the LINK level, which is the same level `Tests/software` fakes -
`serial.Serial` there, `LinkAdapter.request` here. Everything above the
substitution is the real framework: the real adapters, the real registry,
the real runner, the real gates, the real evidence writer.
"""

import time

from ..adapters.base import AdapterError


class FakeError(Exception):
    """A scripted device failure, carrying a code like the real one."""

    def __init__(self, code, message="scripted failure", data=None):
        super().__init__(message)

        self.code = code
        self.message = message
        self.data = data or {}


class FakeLink:
    """
    Stands in for `LinkAdapter`, with a script per command.

    A script entry is either a callable (given the payload, returns the
    answer) or a plain value returned as the answer. Raising from a
    callable is how a failure is scripted, and the exception is
    normalized exactly as the real adapter normalizes a LinkError.
    """

    def __init__(self, context, script=None, latency_ms=1.0):
        self.context = context
        self.script = dict(script or {})
        self.latency_ms = float(latency_ms)

        self.calls = []
        self.opened = 0
        self.closed = 0
        self.close_raises = False

        self._counters = {
            "corrupt_frames": 0,
            "salvaged_frames": 0,
            "stale_frames": 0,
            "oversized_lines": 0,
            "bytes_read": 0,
        }

        self.transactions = []

        self.module = _FakeSerialLinkModule()

    # ------------------------------------------------------------------
    # the LinkAdapter surface the adapters and tests use
    # ------------------------------------------------------------------

    def capabilities(self):
        """The real detection, so capability gating is exercised for real."""
        from ..adapters.link import LinkAdapter

        return LinkAdapter(self.context).capabilities()

    def capability(self, name):
        return self.capabilities().get(name)

    def has(self, name):
        found = self.capability(name)

        return bool(found and found.available)

    def enumerate_ports(self):
        self.context.require_hardware_mode("enumerate serial ports")

        return list(self.script.get(
            "__ports__",
            [{"port": "/dev/fake0",
              "description": "fake CP2102",
              "hwid": "USB VID:PID=10C4:EA60 SER=FAKE0001"}]))

    def require_link(self, reason="talk to the fake"):
        self.context.require_hardware_mode(reason)

        self.opened += 1

        return self

    def is_open(self):
        return self.opened > self.closed

    def close(self, reason=None):
        self.closed += 1

        if self.close_raises:
            return {"closed": False, "was_open": True,
                    "error_type": "OSError",
                    "error": "scripted cleanup failure: the device is "
                             "already gone"}

        return {"closed": True, "was_open": True, "reason": reason}

    def counters(self):
        return dict(self._counters)

    def request(self, command, timeout=None, retries=0, **payload):
        self.context.require_hardware_mode("send {}".format(command))

        started = time.perf_counter()

        self.calls.append({"command": command, "payload": dict(payload)})

        entry = self.script.get(command)

        record = {
            "command": command,
            "payload": dict(payload),
            "timeout": timeout,
            "retries": retries,
            "counters_before": self.counters(),
        }

        try:
            if entry is None:
                raise FakeError(
                    "NO_SCRIPT",
                    "the fake transport has no script for {!r}. A "
                    "framework test that reaches an unscripted command "
                    "is testing something it did not mean "
                    "to.".format(command))

            answer = entry(payload) if callable(entry) else entry

        except FakeError as error:
            record["ok"] = False
            record["error"] = {"type": "FakeError", "code": error.code,
                               "message": error.message}

            record["elapsed_ms"] = self.latency_ms

            self.transactions.append(record)

            self.context.event("transaction", **record)

            raise AdapterError(
                "{} failed: {}".format(command, error.message),
                code=error.code, original=error,
                data={"kind": "DEVICE", "what": command,
                      "device_data": error.data,
                      "transaction": record})

        record["ok"] = True
        record["data"] = answer
        record["elapsed_ms"] = round(
            max(self.latency_ms,
                (time.perf_counter() - started) * 1000.0), 3)

        record["counters_after"] = self.counters()
        record["counters_delta"] = {
            key: 0 for key in self._counters}

        self.transactions.append(record)

        self.context.event("transaction", **record)

        return record

    def data(self, command, **kwargs):
        return self.request(command, **kwargs)["data"]

    # ------------------------------------------------------------------

    def bump(self, counter, amount=1):
        """Make a transport counter rise, for the tests that watch them."""
        self._counters[counter] = self._counters.get(counter, 0) + amount


class _FakePySerial:
    """
    Stands in for the pyserial module the real serial_link imports.

    It exists only so that a body which reports the pyserial version -
    HW-B0-001 does - has something to report. It has no port, no open()
    and no way to reach a device; the version string says plainly that
    it is not pyserial.
    """

    __version__ = "0.0-fake (framework self-test, not pyserial)"


class _FakeSerialLinkModule:
    """The parts of the real serial_link module the adapters read."""

    MOVE_TIMEOUT = 60.0
    MEASURE_TIMEOUT = 180.0
    DEFAULT_TIMEOUT = 10.0

    serial = _FakePySerial


# ======================================================================
# ready-made scripts
# ======================================================================

def healthy_script(counts_per_rev=4096):
    """
    A device that works. Enough of the real answer shapes to drive the
    real test bodies.
    """
    state = {"position": 1024, "uptime": 100000}

    def ping(_payload):
        return {"firmware": "freya-science-module", "version": "6.0.0",
                "protocol_version": 2, "uptime_ms": state["uptime"]}

    def get_status(_payload):
        state["uptime"] += 1000

        return {
            "uptime_ms": state["uptime"],
            "sensor": {"state": "READY", "address": "0x49",
                       "bus": {"bus": 0}, "last_scan": ["0x49"],
                       "settings": {"integration_cycles": 100,
                                    "gain": 2, "measurement_mode": 3},
                       "recovery_count": 0, "first_init_error": None},
            "servo": {"connected": True, "label": "ST3215"},
            "carousel": {"position_valid": True, "current_scan_slot": 3,
                         "current_load_slot": 1, "slot_count": 4,
                         "scan_load_offset_slots": 2,
                         "carousel_phase": "LOAD"},
        }

    def diagnostics(_payload):
        return {
            "ok": True, "moved": False, "connected": True,
            "mode": 3, "mode_name": "step", "mode_correct": True,
            "expected_mode": 3, "torque_enabled": True,
            "baud_reported": 1000000, "baud_matches": True,
            "feedback": {"position": state["position"]},
            "steps": [
                {"step": "uart", "ok": True, "value": {}},
                {"step": "ping", "ok": True, "value": True},
                {"step": "id", "ok": True, "value": 1},
                {"step": "feedback", "ok": True,
                 "value": {"position": state["position"]}},
            ],
            "bus": {"errors": 0, "timeouts": 0, "retries": 0},
        }

    def test_move(payload):
        degrees = float(payload.get("degrees") or 0.0)
        repeat = int(payload.get("repeat") or 1)

        counts = int(round(degrees * counts_per_rev / 360.0)) * repeat

        start = state["position"]

        state["position"] = (start + counts) % counts_per_rev

        return {
            "kind": payload.get("kind"), "moved": True, "verified": True,
            "repeat": repeat, "legs": [counts], "net_counts": counts,
            "start_position": start, "end_position": state["position"],
            "closed_loop_error_counts": 0, "worst_position_error": 1,
            "tolerance_counts": 15, "position_invalidated": bool(counts),
        }

    def spectrum(offset=0.0):
        return {
            channel: round(100.0 + offset + index, 3)
            for index, channel in enumerate(
                "A B C D E F G H I J K L R S T U V W".split())
        }

    def acquire_block(payload):
        repeats = int(payload.get("repeats") or 1)
        illumination = payload.get("illumination", "white")

        offset = {"white": 0.0, "uv": 30.0, "ir": 60.0,
                  "dark": 90.0}.get(illumination, 0.0)

        return {
            "illumination": illumination,
            "repeats": repeats,
            "acquisitions": [spectrum(offset + index)
                             for index in range(repeats)],
            "data_ready_wait_ms": [420 for _ in range(repeats)],
            "bulbs_off": True,
        }

    def acquire_triad(payload):
        repeats = int(payload.get("repeats") or 3)

        return {
            "illuminations": {
                name: acquire_block(
                    {"repeats": repeats, "illumination": name})
                for name in ("white", "uv", "ir")
            },
            "repeats": repeats,
            "bulbs_off": True,
            "temperatures": {"master": 31},
            "protocol_version": 2,
        }

    def measure_raw(payload):
        answer = acquire_triad({"repeats": 3})

        answer["slot"] = payload.get("slot")
        answer["carousel"] = {"position_valid": True,
                              "current_load_slot": payload.get("slot"),
                              "current_scan_slot": 3}
        answer["movement"] = {"travelled_counts": 2048,
                              "position_error": 2}

        return answer

    return {
        "ping": ping,
        "get_status": get_status,
        "connect_servo": lambda p: {"servo": {"connected": True,
                                              "label": "ST3215"}},
        "disconnect_servo": lambda p: {"servo": {"connected": False}},
        "servo_diagnostics": diagnostics,
        "servo_stop": lambda p: {"stopped": True},
        "servo_torque": lambda p: {"torque": p.get("enable")},
        "servo_bus_scan": lambda p: {
            "found": [{"id": 1, "baud": 1000000, "swapped": False}],
            "scanned": 16},
        "get_servo_calibration": lambda p: {
            "editable": False,
            "current": {"speed_steps_per_s": 600, "acceleration": 20,
                        "position_tolerance_counts": 15,
                        "settle_ms": 250, "poll_interval_ms": 20,
                        "move_timeout_ms": 12000}},
        "servo_test_move": test_move,
        "sync_position": lambda p: {"carousel": {"position_valid": True,
                                                 "current_load_slot":
                                                 p.get("load_slot", 1)}},
        "select_slot": lambda p: {
            "carousel": {"position_valid": True,
                         "current_load_slot": p.get("slot"),
                         "current_scan_slot": 3},
            "movement": {"travelled_counts": 1024, "position_error": 1}},
        "move_slots": lambda p: {"carousel": {"position_valid": True}},
        "fine_adjust": lambda p: {"carousel": {"position_valid": True}},
        "sensor_test_raw": lambda p: {
            "raw": spectrum(), "data_ready_wait_ms": 420,
            "zero_channels": [], "sensor_settings": {}},
        "acquire_block": acquire_block,
        "acquire_triad": acquire_triad,
        "led_test": lambda p: {"held_ms": p.get("hold_ms")},
        "measure_raw": measure_raw,
        "list_saved_samples": lambda p: {"samples": []},
        "get_saved_sample": lambda p: {"sample": None},
    }


def failing(code, message="scripted failure", data=None):
    """A script entry that always fails with a given code."""
    def entry(_payload):
        raise FakeError(code, message, data)

    return entry


def timing_out(seconds=0.0):
    """A script entry that behaves as the transport's timeout does."""
    def entry(_payload):
        if seconds:
            time.sleep(seconds)

        raise FakeError(
            "PROTOCOL_TIMEOUT",
            "the module did not answer within the timeout")

    return entry


def aborting():
    """A script entry that raises KeyboardInterrupt, as Ctrl+C does."""
    def entry(_payload):
        raise KeyboardInterrupt()

    return entry
