"""
PC layer tests: Sample persistence and the JSON protocol client.

The store is exercised in a temporary directory - never against
firmware/PC/data - and the serial link is driven through a loopback
that can answer, misbehave, or emit MicroPython console noise.
"""

import json
import sys
import tempfile
import types
from pathlib import Path

import support
from support import Checks

support.add_path("PC")

import esp32_link                              # noqa: E402
import sample_store                            # noqa: E402
from sample_store import (                     # noqa: E402
    STATE_EMPTY,
    STATE_LOADED,
    STATE_MEASURED,
    STATE_READY_TO_LOAD,
    SampleStore,
    StorageError,
    validate_sample_id,
)


# ----------------------------------------------------------------------
# loopback serial
# ----------------------------------------------------------------------

class FakeSerial:
    """A serial port whose other end is a Python function."""

    def __init__(self, responder, noise=()):
        self.responder = responder
        self.pending = [line.encode("utf-8") + b"\n" for line in noise]
        self.written = []
        self.timeout = 0.5
        self.closed = False

    def reset_input_buffer(self):
        pass

    def reset_output_buffer(self):
        pass

    def write(self, data):
        self.written.append(data)

        request = json.loads(data.decode("utf-8"))

        for line in self.responder(request):
            self.pending.append(
                (line if isinstance(line, str) else json.dumps(line))
                .encode("utf-8") + b"\n"
            )

        return len(data)

    def flush(self):
        pass

    def readline(self):
        return self.pending.pop(0) if self.pending else b""

    def close(self):
        self.closed = True


def install_fake_serial(responder, noise=()):
    """Point esp32_link at a loopback instead of pyserial."""
    port = FakeSerial(responder, noise)

    module = types.ModuleType("serial")
    module.EIGHTBITS = 8
    module.PARITY_NONE = "N"
    module.STOPBITS_ONE = 1
    module.SerialException = OSError
    module.Serial = lambda **kwargs: port

    esp32_link.serial = module

    return port


def answer_ok(request, data=None):
    return {
        "request_id": request["request_id"],
        "ok": True,
        "cmd": request["cmd"],
        "data": data if data is not None else {},
    }


def main_tests():
    checks = Checks("PC controller")

    # ==================================================================
    checks.section("1. Sample ID rules")

    checks.equal(validate_sample_id("  S001 "), "S001", "IDs are trimmed")
    checks.equal(validate_sample_id("ROCK-07"), "ROCK-07", "hyphens allowed")
    checks.equal(validate_sample_id("a_1"), "a_1", "underscores allowed")

    for bad in ("", "   ", "S 001", "S/001", "S:1", "..", "x" * 25):
        checks.raises(
            StorageError,
            lambda value=bad: validate_sample_id(value),
            "{!r} is refused".format(bad),
        )

    checks.raises(
        StorageError,
        lambda: validate_sample_id(7),
        "a non-string ID is refused",
    )

    # ==================================================================
    checks.section("2. Sample lifecycle")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        store = SampleStore(root / "samples.json", root / "samples")

        checks.ok(store.ready, "a fresh store is ready")
        checks.equal(store.count(), 0, "and empty")
        checks.ok(
            not (root / "samples.json").exists(),
            "nothing is written to disk until the first sample",
        )

        record = store.create("S001", 1, "2026-08-11T10:00:00+00:00",
                              {"task": "regolith survey"})

        checks.equal(record["state"], STATE_READY_TO_LOAD, "created as READY")
        checks.equal(record["slot_id"], 1, "slot recorded")
        checks.equal(
            record["metadata"]["task"], "regolith survey", "metadata kept"
        )
        checks.ok(
            record["metadata"]["hypothesis"] is None,
            "an unanswered field stays null, never invented",
        )
        checks.equal(store.count(), 1, "the index has one entry")
        checks.ok(
            (root / "samples" / "S001.json").exists(),
            "the record lives in its own file",
        )

        store.set_state("S001", STATE_LOADED, "loaded_at", "2026-08-11T10:05")

        checks.equal(
            store.get_state("S001"), STATE_LOADED, "state advances to LOADED"
        )
        checks.equal(
            store.get_sample("S001")["timestamps"]["loaded_at"],
            "2026-08-11T10:05",
            "the transition is stamped",
        )
        checks.equal(
            store.get_sample("S001")["timestamps"]["created_at"],
            "2026-08-11T10:00:00+00:00",
            "the earlier timestamp survives",
        )

        # Completing the SAME record, as measurement does.
        record = store.get_sample("S001")
        record["state"] = STATE_MEASURED
        record["measurement"] = {"raw": {"A": 1.0}}
        record["analysis"] = {
            "best_match": "Kaolin", "best_similarity": 97.5,
            "status": "STRONG_REFERENCE_MATCH",
        }
        record["analysis_status"] = "OK"
        record["timestamps"]["measured_at"] = "2026-08-11T10:09"

        store.save(record)

        checks.equal(store.count(), 1, "no second sample was created")

        summary = store.find_summary("S001")

        checks.equal(summary["state"], STATE_MEASURED, "summary state updated")
        checks.equal(summary["best_match"], "Kaolin", "summary carries the match")
        checks.close(
            summary["best_similarity"], 97.5, "and the score"
        )
        checks.ok(summary["measured"], "summary marks it measured")

        # Metadata may be corrected; science may not be touched by it.
        store.update_metadata("S001", {"note": "dry, fine grained"})
        reloaded = store.get_sample("S001")

        checks.equal(
            reloaded["metadata"]["note"], "dry, fine grained", "note added"
        )
        checks.equal(
            reloaded["metadata"]["task"], "regolith survey",
            "existing metadata survives the merge",
        )
        checks.equal(
            reloaded["measurement"], {"raw": {"A": 1.0}},
            "the measurement is untouched by a metadata edit",
        )

        # MEASURED is not EMPTY: clearing the slot keeps the record.
        active = store.active_samples()

        checks.equal(
            active[1]["sample_id"], "S001",
            "a measured sample still occupies its slot",
        )

        store.set_state("S001", STATE_EMPTY)

        checks.equal(
            store.active_samples(), {},
            "clearing the slot frees it",
        )
        checks.ok(
            store.get_sample("S001") is not None,
            "but the scientific record is still there",
        )

        # Rename and delete.
        store.rename("S001", "S001B")

        checks.ok(store.has_sample("S001B"), "renamed sample is indexed")
        checks.ok(not store.has_sample("S001"), "the old ID is gone")
        checks.ok(
            not (root / "samples" / "S001.json").exists(),
            "the old record file is removed",
        )
        checks.equal(
            store.get_sample("S001B")["analysis"]["best_match"], "Kaolin",
            "every scientific value survived the rename",
        )

        store.create("S002", 2, "2026-08-11T11:00:00+00:00")

        checks.raises(
            StorageError,
            lambda: store.rename("S002", "S001B"),
            "renaming onto an existing ID is refused",
        )

        store.delete("S001B")

        checks.equal(store.count(), 1, "delete removes the index entry")
        checks.ok(
            not (root / "samples" / "S001B.json").exists(),
            "and the record file",
        )
        checks.raises(
            StorageError,
            lambda: store.delete("NOPE"),
            "deleting an unknown sample is refused",
        )

    # ==================================================================
    checks.section("3. a damaged index is never overwritten")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        index = root / "samples.json"
        index.write_text("{ this is not json", encoding="utf-8")

        store = SampleStore(index, root / "samples")

        checks.ok(not store.ready, "a corrupt index is not ready")
        checks.ok(store.error is not None, "and says so")

        checks.raises(
            StorageError,
            lambda: store.create("S001", 1, "now"),
            "writes are refused while the index looks damaged",
        )
        checks.equal(
            index.read_text(encoding="utf-8"), "{ this is not json",
            "the damaged file was NOT modified",
        )

    # ==================================================================
    checks.section("4. atomic writes")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        store = SampleStore(root / "samples.json", root / "samples")

        store.create("S001", 1, "now")

        leftovers = list(root.rglob("*.tmp"))

        checks.equal(leftovers, [], "no temporary files are left behind")

        payload = json.loads((root / "samples.json").read_text("utf-8"))

        checks.equal(payload["version"], 1, "the index records its version")
        checks.equal(len(payload["samples"]), 1, "and one sample")

        # A second store sees what the first wrote.
        checks.equal(
            SampleStore(root / "samples.json", root / "samples").count(), 1,
            "the archive survives a restart of the program",
        )

    # ==================================================================
    checks.section("5. the default archive path is inside PC/")

    checks.equal(
        sample_store.DATA_DIR.parent.name, "PC",
        "sample data lives under firmware/PC",
    )
    checks.equal(
        sample_store.INDEX_PATH.name, "samples.json", "index filename"
    )
    checks.ok(
        sample_store.DATA_DIR.is_absolute(),
        "the path is absolute, so the working directory does not matter",
    )

    # ==================================================================
    checks.section("6. protocol: a good round trip")

    port = install_fake_serial(
        lambda request: [answer_ok(request, {"pong": True})]
    )

    link = esp32_link.ESP32Link("COM_TEST")
    link.open()

    data = link.ping()

    checks.ok(data["pong"], "ping returns its data block")

    sent = json.loads(port.written[-1].decode("utf-8"))

    checks.equal(sent["cmd"], "ping", "the command is sent")
    checks.ok("request_id" in sent, "with a request id")
    checks.ok("timestamp" in sent, "and a PC timestamp")

    link.close()

    # ==================================================================
    checks.section("7. protocol: errors, noise and stray frames")

    def refuse(request):
        return [{
            "request_id": request["request_id"],
            "ok": False,
            "cmd": request["cmd"],
            "error": {
                "code": "SLOT_NOT_AT_LOADER",
                "message": "Slot 1 is not at the loading position.",
            },
            "data": {"moved": False, "carousel_phase": "SCAN"},
        }]

    install_fake_serial(refuse)
    link = esp32_link.ESP32Link("COM_TEST")
    link.open()

    try:
        link.measure_raw(1)
        raised = None

    except esp32_link.LinkError as error:
        raised = error

    checks.ok(raised is not None, "an ok:false response raises LinkError")
    checks.equal(raised.code, "SLOT_NOT_AT_LOADER", "the code is preserved")
    checks.equal(
        raised.data["moved"], False,
        "the partial data block reaches the caller",
    )

    link.close()

    # Console noise, a stray frame for another request, then the answer.
    def noisy(request):
        return [
            "MicroPython v1.28.0 on 2026-04-06; ESP32 module",
            ">>> ",
            {"request_id": "999", "ok": True, "cmd": "other", "data": {}},
            "not json at all",
            answer_ok(request, {"firmware": "freya-science-module"}),
        ]

    install_fake_serial(noisy)
    link = esp32_link.ESP32Link("COM_TEST")
    link.open()

    data = link.get_status()

    checks.equal(
        data["firmware"], "freya-science-module",
        "console noise and stray frames are skipped",
    )

    link.close()

    # Nothing but REPL noise: the timeout must explain why.
    install_fake_serial(
        lambda request: [
            ">>> {'request_id': '1', 'cmd': 'ping'}",
            ">>> ",
        ]
    )

    link = esp32_link.ESP32Link("COM_TEST", timeout=0.4, connect_timeout=0.6)
    link.open()

    try:
        link.wait_online()
        message = ""

    except TimeoutError as error:
        message = str(error)

    checks.ok(
        "REPL" in message and "main.py" in message,
        "a REPL prompt is diagnosed as main.py not running",
    )

    link.close()

    # A JSON object addressed to us but without ok is malformed.
    install_fake_serial(
        lambda request: [{"request_id": request["request_id"], "data": {}}]
    )

    link = esp32_link.ESP32Link("COM_TEST", timeout=0.5)
    link.open()

    checks.raises(
        esp32_link.LinkError, link.ping,
        "a response without 'ok' is a protocol error",
    )

    link.close()

    # ==================================================================
    checks.section("8. protocol: the PC sends only hardware commands")

    seen = []

    def record_command(request):
        seen.append(request["cmd"])

        return [answer_ok(request, {})]

    install_fake_serial(record_command)
    link = esp32_link.ESP32Link("COM_TEST")
    link.open()

    link.ping()
    link.get_status()
    link.sync_load_slot(1)
    link.select_slot(2, "S001")
    link.move_slots("cw", 1)
    link.fine_adjust(1.5)
    link.clear_slot(2)
    link.measure_raw(2, "S001")
    link.sensor_test_raw()
    link.servo_stop()

    checks.equal(
        sorted(set(seen)),
        sorted([
            "clear_slot", "fine_adjust", "get_status", "measure_raw",
            "move_slots", "ping", "select_slot", "sensor_test_raw",
            "servo_stop", "sync_position",
        ]),
        "the client speaks exactly the hardware protocol",
    )

    scientific = {
        "measure_sample", "test_measurement", "sensor_diagnostics",
        "list_samples", "get_sample", "prepare_load", "confirm_loaded",
        "get_references", "get_database_status", "raw_measurement",
    }

    checks.equal(
        sorted(scientific.intersection(seen)), [],
        "no scientific or persistence command is ever sent to the ESP32",
    )

    link.close()

    # ==================================================================
    checks.section("9. the application wires PC, BD and ESP32 together")

    import rover_science_client as app

    checks.ok(
        app.BD_DIR.name == "BD" and app.BD_DIR.exists(),
        "BD is located relative to the application, not the shell",
    )
    checks.ok(
        hasattr(app, "Mission") and hasattr(app.Mission, "analyse_raw"),
        "the mission controller owns the analysis call",
    )

    source = (support.FIRMWARE / "PC").glob("*.py")
    offenders = []

    for path in source:
        text = path.read_text(encoding="utf-8")

        for token in ("import machine", "from machine", "I2C(", "PWM("):
            if token in text:
                offenders.append("{}: {}".format(path.name, token))

    checks.equal(
        offenders, [], "no PC module reaches for hardware directly"
    )

    return checks.report()


if __name__ == "__main__":
    sys.exit(main_tests())
