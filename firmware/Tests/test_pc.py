"""
PC layer tests: Sample persistence and the JSON protocol client.

The store is exercised in a temporary directory - never against
firmware/PC/data - and the serial link is driven through a loopback
that can answer, misbehave, or emit MicroPython console noise.
"""

import json
import sys
import tempfile
import time
import types
from pathlib import Path

import support
from support import Checks

support.add_project_root()
support.add_path("PC")

import esp32_link                              # noqa: E402
from Measurements import analysis as sample_analysis                         # noqa: E402
from BD import samples as sample_store                            # noqa: E402
from BD.samples import (                     # noqa: E402
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
        store = SampleStore(root / "samples.json")

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
        checks.equal(store.count(), 1, "the archive has one record")
        checks.ok(
            (root / "samples.json").exists(),
            "written to the single archive file",
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

        checks.ok(store.has_sample("S001B"), "renamed sample is archived")
        checks.ok(not store.has_sample("S001"), "the old ID is gone")
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

        checks.equal(store.count(), 1, "delete removes the record")
        checks.ok(
            not store.has_sample("S001B"), "and it can no longer be found"
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

        store = SampleStore(index)

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
        store = SampleStore(root / "samples.json")

        store.create("S001", 1, "now")

        leftovers = list(root.rglob("*.tmp"))

        checks.equal(leftovers, [], "no temporary files are left behind")

        payload = json.loads((root / "samples.json").read_text("utf-8"))

        checks.equal(payload["version"], 2, "the archive records its version")
        checks.equal(len(payload["samples"]), 1, "and one sample")

        # A second store sees what the first wrote.
        checks.equal(
            SampleStore(root / "samples.json").count(), 1,
            "the archive survives a restart of the program",
        )

    # ==================================================================
    checks.section("5. the archive lives in BD, beside the science data")

    checks.equal(
        sample_store.ARCHIVE_PATH.name, "samples.json",
        "the Sample archive is one named file",
    )
    checks.equal(
        sample_store.ARCHIVE_PATH.parent.name, "data",
        "inside BD/data, where all persistent science data lives",
    )
    checks.equal(
        sample_store.ARCHIVE_PATH.name, "samples.json", "archive filename"
    )
    checks.ok(
        sample_store.ARCHIVE_PATH.is_absolute(),
        "the path is absolute, so the working directory does not matter",
    )
    # ARCHITECTURE.md gave each data class its own labelled home instead of three
    # loose JSON files in one directory. DB1 and the legacy calibration are
    # protected; only the sample archive is ever written.
    # One file per thing, flat, self-describing. Each database carries
    # its own metadata and provenance inside it - no side-car manifests.
    data = sample_store.BD_DIR / "data"

    for name in ("DB1.json", "DB2.json", "DB3.json",
                 "calibration_legacy.json", "samples.json"):
        checks.ok((data / name).exists(), "BD/data/{} exists".format(name))

    # A flat set of named files, with ONE deliberate exception. Every
    # reference database is a single self-describing JSON file, and that
    # stays true. `decision_learning/` is not a reference database: it
    # holds a binary SQLite file plus the human-readable seed that file
    # is rebuilt from, and those two belong together rather than loose
    # among the databases they must never be confused with.
    #
    # Dot-directories are excluded: .cache holds downloaded third-party
    # archives, is gitignored, and is not part of the data layout.
    checks.equal(
        sorted(
            p.name for p in data.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        ),
        ["decision_learning"],
        "BD/data is flat apart from the decision learning directory",
    )
    checks.ok(
        (data / "decision_learning" / "seed_observations.json").exists(),
        "which keeps its observations in a readable, rebuildable seed "
        "beside the binary",
    )
    checks.equal(
        sorted(path.name for path in sample_store.BD_DIR.glob("*.json")),
        [],
        "no loose scientific JSON is left at the top of BD",
    ) if sample_store.ARCHIVE_PATH.exists() else checks.ok(
        (sample_store.BD_DIR / "database.json").exists(),
        "the reference data it sits beside is there",
    )

    # ==================================================================
    checks.section("5b. full scientific records survive a round trip")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        store = SampleStore(root / "samples.json")

        channels = sample_analysis.CHANNELS

        full = {
            "sample_id": "S010",
            "slot_id": 2,
            "state": STATE_MEASURED,
            "measured": True,
            "timestamps": {
                "created_at": "2026-08-14T10:00:00+00:00",
                "loaded_at": "2026-08-14T10:02:00+00:00",
                "measured_at": "2026-08-14T10:05:00+00:00",
            },
            "metadata": {"task": "crater floor", "note": "dry"},
            "measurement": {
                "wavelengths": sample_analysis.channel_wavelengths(),
                "raw": {c: 100.0 + i for i, c in enumerate(channels)},
                "dark_corrected": {
                    c: 99.0 + i for i, c in enumerate(channels)
                },
                "normalized": {
                    c: 0.4 + i / 100.0 for i, c in enumerate(channels)
                },
                "sensor_settings": {
                    "integration_cycles": 100, "gain": 2, "gain_x": "16x",
                    "measurement_mode": 2, "led_current": 1,
                },
            },
            "calibration": {
                "calibration_id": "FREYA_COMPETITION_2026_CAL_V1",
                "dark_reference": {c: 1.0 for c in channels},
                "white_reference": {c: 250.0 for c in channels},
            },
            "reference_matches": [
                {"rank": n + 1, "material": "M{}".format(n),
                 "similarity_percent": 90.0 - n}
                for n in range(22)
            ],
            "analysis": {
                "best_match": "M0", "best_similarity": 90.0,
                "second_match": "M1", "second_similarity": 89.0,
                "score_difference": 1.0, "status": "AMBIGUOUS",
                "automatic_conclusion": "spectral similarity only",
            },
            "analysis_status": "OK",
        }

        store.save(full)

        # Reload from disk, exactly as a restart would.
        reloaded = SampleStore(root / "samples.json").get_sample("S010")

        measurement = reloaded["measurement"]
        calibration = reloaded["calibration"]

        for name, block in (
            ("raw", measurement["raw"]),
            ("dark_corrected", measurement["dark_corrected"]),
            ("normalized", measurement["normalized"]),
            ("wavelengths", measurement["wavelengths"]),
            ("dark_reference", calibration["dark_reference"]),
            ("white_reference", calibration["white_reference"]),
        ):
            checks.equal(
                sorted(block.keys()), sorted(channels),
                "{} survives with all 18 channels".format(name),
            )

        checks.equal(
            len(reloaded["reference_matches"]), 22,
            "every material comparison survives",
        )
        checks.equal(
            reloaded["reference_matches"][0]["material"], "M0",
            "ranked best first",
        )
        checks.equal(
            reloaded["analysis"]["second_match"], "M1",
            "the second match survives",
        )
        checks.close(
            reloaded["analysis"]["score_difference"], 1.0,
            "and the margin",
        )
        checks.equal(
            reloaded["analysis"]["automatic_conclusion"],
            "spectral similarity only", "and the conclusion",
        )
        checks.equal(
            reloaded["measurement"]["sensor_settings"]["gain_x"], "16x",
            "sensor settings survive",
        )
        checks.equal(
            reloaded["metadata"]["task"], "crater floor", "metadata survives"
        )
        checks.equal(
            reloaded["timestamps"]["loaded_at"], "2026-08-14T10:02:00+00:00",
            "timestamps survive",
        )
        checks.equal(reloaded, full, "nothing at all was dropped")

        # The compact table still gets its columns.
        summary = store.find_summary("S010")

        checks.equal(summary["best_match"], "M0", "the table row is derived")
        checks.close(summary["best_similarity"], 90.0, "with its score")

    # ==================================================================
    checks.section("5c. old short records stay readable")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        archive = root / "samples.json"

        archive.write_text(json.dumps({
            "version": 1,
            "samples": [{
                "sample_id": "s1",
                "slot_id": 1,
                "state": "MEASURED",
                "measured": True,
                "created_at": "2026-08-14T13:18:24+00:00",
                "measured_at": "2026-08-14T13:18:49+00:00",
                "best_match": "Magnesium Carbonate",
                "best_similarity": 94.95,
                "status": "AMBIGUOUS",
                "analysis_status": "OK",
            }],
        }), encoding="utf-8")

        store = SampleStore(archive)

        checks.ok(store.ready, "an old short archive loads")
        checks.equal(store.count(), 1, "with its record")

        summary = store.summaries()[0]

        checks.equal(
            summary["best_match"], "Magnesium Carbonate",
            "the flat best_match is still found",
        )
        checks.close(
            summary["best_similarity"], 94.95, "and its score"
        )
        checks.equal(summary["state"], "MEASURED", "and the state")
        checks.equal(
            summary["created_at"], "2026-08-14T13:18:24+00:00",
            "and the flat timestamp",
        )
        checks.ok(summary["measured"], "and the measured flag")

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
    link.list_saved_samples()
    link.get_saved_sample("S001")
    link.delete_saved_samples()
    link.clear_all_slots()
    link.get_servo_options()
    link.select_servo("st3215")
    link.servo_diagnostics()
    link.get_servo_calibration()
    link.set_servo_calibration({"neutral_us": 1500})
    link.servo_configure(confirm=True)
    link.servo_torque(True)
    link.servo_test_move("slot_out_and_back", confirm=True)
    link.servo_stop()

    checks.equal(
        sorted(set(seen)),
        sorted([
            "clear_all_slots", "clear_slot", "delete_saved_samples",
            "fine_adjust", "get_saved_sample", "get_servo_calibration",
            "get_servo_options", "get_status", "list_saved_samples",
            "measure_raw", "move_slots", "ping", "select_servo",
            "select_slot", "sensor_test_raw", "servo_configure",
            "servo_diagnostics", "servo_stop", "servo_test_move",
            "servo_torque", "set_servo_calibration", "sync_position",
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
    checks.section("8b. a damaged answer is not a missing answer")

    # Observed on hardware during a calibration: about sixty corrupted
    # bytes arrived in front of an otherwise perfect frame, the line
    # would not parse, and the PC then waited out its full 180 s timeout
    # for an answer it had already received. Both halves of that are
    # fixed here - the frame is recovered when it can be, and when it
    # cannot the wait ends immediately instead of running to the timeout.

    checks.equal(
        esp32_link.salvage_json('��{"ok": true, "n": 1}')["n"], 1,
        "a frame with rubbish in front of it is recovered",
    )
    checks.ok(
        esp32_link.salvage_json("no json here at all") is None,
        "and a line with no object in it is not",
    )
    checks.ok(
        esp32_link.looks_like_a_frame('��"data": {"J": 4.2'),
        "a fragment carrying response fields is recognised as our frame",
    )
    checks.ok(
        not esp32_link.looks_like_a_frame("MicroPython v1.28.0 on ESP32"),
        "while console noise is not",
    )

    # The recoverable case: the JSON survived, something landed in front.
    def dirty_but_whole(request):
        answer = json.dumps(answer_ok(request, {"recovered": True}))

        return ["�" * 40 + answer]

    install_fake_serial(dirty_but_whole)
    link = esp32_link.ESP32Link("COM_TEST", timeout=1.0)
    link.open()

    checks.ok(
        link.get_status()["recovered"],
        "a response behind line noise is still delivered",
    )
    checks.equal(link.salvaged_frames, 1, "and counted as salvaged")

    link.close()

    # The unrecoverable case: the damage ate the head of the frame. The
    # command is a pure read, so asking again is safe and is what the
    # link does.
    attempts = []

    def damaged_once(request):
        attempts.append(request["cmd"])

        if len(attempts) == 1:
            return ['�' * 60 + '"data": {"bulbs_off": true, "acq']

        return [answer_ok(request, {"illumination": "ir"})]

    install_fake_serial(damaged_once)
    link = esp32_link.ESP32Link("COM_TEST", timeout=1.0)
    link.open()

    started = time.monotonic()
    data = link.acquire_block("ir", 2)
    elapsed = time.monotonic() - started

    checks.equal(
        data["illumination"], "ir",
        "an acquisition whose answer was destroyed is simply asked again",
    )
    checks.equal(len(attempts), 2, "exactly once more")
    checks.equal(link.corrupt_frames, 1, "and the damage is counted")
    checks.ok(
        elapsed < 5.0,
        "without waiting out the acquisition timeout first",
    )

    link.close()

    # A movement is NOT retried. A relative step whose acknowledgement
    # was lost has still happened, and sending it again would move the
    # carousel twice.
    movements = []

    def always_damaged(request):
        movements.append(request["cmd"])

        return ['�' * 60 + '"ok": true, "data": {']

    install_fake_serial(always_damaged)
    link = esp32_link.ESP32Link("COM_TEST", timeout=1.0)
    link.open()

    try:
        link.move_slots("cw", 1)
        raised = None

    except TimeoutError as error:
        raised = error

    checks.ok(raised is not None, "a damaged answer to a move fails")
    checks.equal(
        len(movements), 1,
        "and the movement is sent exactly once - never repeated",
    )
    checks.ok(
        "damaged" in str(raised),
        "the message says the answer arrived damaged, not that it was "
        "missing",
    )

    link.close()

    # ==================================================================
    checks.section("9. the application wires PC, BD and ESP32 together")

    import rover_science_client as app

    checks.ok(
        (app.PROJECT_ROOT / "BD").exists()
        and (app.PROJECT_ROOT / "Measurements").exists(),
        "BD and Measurements are located relative to the application, "
        "not the shell",
    )
    checks.ok(
        hasattr(app, "Mission") and hasattr(app.Mission, "analyse_raw"),
        "the mission controller owns the analysis call",
    )
    checks.equal(app.SLOT_COUNT, 4, "the UI knows about four slots")
    checks.ok(
        hasattr(app, "sync_esp32_samples"),
        "the ESP32 -> PC sync exists",
    )
    checks.ok(
        "7" in [key for key, _l, _d, _h in app.TOOLS_MENU],
        "and has its own Tools entry",
    )
    checks.equal(
        [key for key, _l, _d, _h in app.TOOLS_MENU][-1], "8",
        "with the decision learning history after it",
    )
    checks.ok(
        hasattr(app, "menu_learning_history")
        and hasattr(app, "capture_ground_truth"),
        "the learning history and ground-truth capture screens exist",
    )
    checks.ok(
        hasattr(app, "print_decision")
        and hasattr(app, "print_evidence_summary"),
        "and the client can show a decision and the evidence behind it",
    )
    checks.ok(
        "Sync ESP32 Samples to PC" in [
            label for _k, label, _d, _h in app.TOOLS_MENU
        ],
        "under its documented name",
    )
    checks.ok(
        hasattr(app, "clear_all_slots"), "Clear ALL physical slots exists"
    )
    checks.ok(
        hasattr(app, "delete_esp32_samples"),
        "Delete ALL ESP32 Samples exists",
    )
    checks.ok(
        hasattr(app, "menu_select_servo"),
        "the servo selection screen exists - option [0] asks first",
    )
    checks.ok(
        hasattr(app, "menu_servo_diagnostics"),
        "the servo diagnostics screen exists",
    )
    checks.ok(
        hasattr(app, "menu_servo_calibration"),
        "and the servo settings screen exists",
    )
    checks.ok(
        hasattr(app, "menu_servo_movement_test"),
        "the operator-confirmed movement test screen exists",
    )
    checks.ok(
        hasattr(app, "menu_servo_configure"),
        "the servo mode configuration screen exists",
    )
    # The client must ask the firmware what the servo can do rather than
    # hardcoding it. One actuator is fitted, but capability-driven
    # formatting is what keeps the screens honest when the firmware
    # reports something the client did not expect.
    source = (support.PC_DIR / "rover_science_client.py").read_text(
        encoding="utf-8"
    )

    checks.ok(
        "select_servo" in source,
        "the client can state which servo is installed",
    )
    checks.ok(
        "verified_movement" in source,
        "and adapts its output to what the active backend can do",
    )

    # Spectra that differ must not be treated as the same measurement.
    same = {c: 1.0 for c in sample_analysis.CHANNELS}
    other = dict(same)
    other["K"] = 2.0

    checks.ok(app._same_spectrum(same, dict(same)), "identical spectra match")
    checks.ok(
        not app._same_spectrum(same, other), "a differing channel does not"
    )
    checks.ok(
        not app._same_spectrum(same, {}), "an absent spectrum never matches"
    )

    offenders = []

    for path in (support.PC_DIR).glob("*.py"):
        text = path.read_text(encoding="utf-8")

        for token in ("import machine", "from machine", "I2C(", "PWM("):
            if token in text:
                offenders.append("{}: {}".format(path.name, token))

    checks.equal(
        offenders, [], "no PC module reaches for hardware directly"
    )

    # The client formats on what the firmware SAYS the servo can do.
    for token in ("capabilities", "position_feedback",
                  "print_st3215_settings"):
        checks.ok(
            token in source,
            "the client branches on capability, not on servo name ('{}')"
            .format(token),
        )

    # And the timed open-loop vocabulary is gone with the backend that
    # needed it. A pulse width on the PC side would mean the removal was
    # only half done.
    for token in ("print_mg995_calibration", "MG995_STOP_US",
                  "adjust_pulse", "edit_timings"):
        checks.ok(
            token not in source,
            "no MG995 timing vocabulary remains on the PC ('{}')".format(
                token
            ),
        )

    # And the ESP32 register level must never appear on the PC side.
    # Displaying the firmware's checksum-error COUNTER is telemetry and is
    # fine; computing a checksum would mean the PC had grown a protocol
    # implementation of its own.
    for token in ("0xFF", "checksum(", "REG_", "duty_ns", "build_packet",
                  "encode_signed", "INST_"):
        checks.ok(
            token not in source,
            "the client contains no ESP32-level detail ('{}')".format(token),
        )

    return checks.report()


if __name__ == "__main__":
    sys.exit(main_tests())
