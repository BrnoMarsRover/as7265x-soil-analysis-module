"""
End-to-end test of the three layers together.

The real ESP32 firmware sits behind a loopback serial port, the real PC
client drives it, and the real BD layer analyses whatever comes back:

    fake AS7265x -> ESP32 main.py -> loopback -> PC -> BD -> PC archive

Nothing about the pipeline is simulated except the silicon and the
cable. This is what proves the architecture boundary actually holds -
that a RAW spectrum leaves the ESP32 and becomes a complete scientific
record without the firmware ever touching the science.
"""

import hashlib
import json
import sys
import tempfile
import types
from pathlib import Path

import support
from support import Checks, FakeAS7265X


def build_firmware(device):
    """Load the real ESP32 firmware against a fake sensor."""
    support.load_esp32(device)

    import config as esp32_config

    esp32_config.STARTUP_DELAY_SECONDS = 0
    esp32_config.SENSOR_INIT_RETRY_MS = 0
    esp32_config.I2C_SCAN_RETRY_MS = 0
    esp32_config.ILLUMINATION_SETTLE_MS = 0
    esp32_config.SERVO_SETTLE_MS = 0
    esp32_config.SERVO_INTER_STEP_PAUSE_MS = 0
    esp32_config.SCAN_SETTLE_TIME = 0.0
    esp32_config.NEXT_SLOT_CW_MS = 1
    esp32_config.NEXT_SLOT_CCW_MS = 1
    esp32_config.LOAD_TO_SCAN_CW_MS = 1
    esp32_config.SCAN_TO_LOAD_CCW_MS = 1

    import main as firmware

    module = firmware.HardwareModule()
    module.boot()

    # ESP32 and BD both have a module called `config`. Drop the ESP32
    # one from the cache now that the firmware holds its own reference,
    # so the PC side imports BD's instead.
    esp32_path = str(support.FIRMWARE / "ESP32")

    for name in ("config",):
        sys.modules.pop(name, None)

    while esp32_path in sys.path:
        sys.path.remove(esp32_path)

    return module


class FirmwareSerial:
    """Loopback that runs the real firmware dispatcher."""

    def __init__(self, module):
        self.module = module
        self.pending = []
        self.timeout = 0.5
        self.frames = []

    def reset_input_buffer(self):
        pass

    def reset_output_buffer(self):
        pass

    def write(self, data):
        request = json.loads(data.decode("utf-8"))
        response = self.module.dispatch_command(request)

        # Prove the firmware's own serializer handles it, exactly as it
        # would on the wire.
        encoded = json.dumps(response)
        self.frames.append(encoded)
        self.pending.append(encoded.encode("utf-8") + b"\n")

        return len(data)

    def flush(self):
        pass

    def readline(self):
        return self.pending.pop(0) if self.pending else b""

    def close(self):
        pass


def install_link(esp32_link, module):
    port = FirmwareSerial(module)

    fake = types.ModuleType("serial")
    fake.EIGHTBITS = 8
    fake.PARITY_NONE = "N"
    fake.STOPBITS_ONE = 1
    fake.SerialException = OSError
    fake.Serial = lambda **kwargs: port

    esp32_link.serial = fake

    return port


def soil_like(index, device):
    """A plausible, non-flat spectrum with real structure."""
    return 40.0 + index * 18.0 + device * 11.0


def plausible_sample(references, fraction=0.35):
    """
    A raw spectrum that could actually have come off this instrument.

    Derived from the real white reference, so the reflectance it
    produces lands inside the physical 0-1 range instead of being
    correctly rejected by quality control as impossible.
    """
    from channels import CHANNELS

    return {
        channel: references.dark[channel]
        + (references.white[channel] - references.dark[channel])
        * (fraction + 0.12 * ((index % 5) - 2) / 5.0)
        for index, channel in enumerate(CHANNELS)
    }


def run_quietly(app, function, *arguments, answer=""):
    """
    Call an interactive screen without its prompts or output.

    `input` is shadowed at module level, which Python resolves before the
    builtin, so the screen's own pause() returns immediately. `answer` is
    what every prompt receives - enough to drive a confirmation, which is
    all these screens ask for.
    """
    import io
    import contextlib

    original_input = getattr(app, "input", None)
    app.input = lambda prompt="": answer

    try:
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            function(*arguments)

        return captured.getvalue()

    finally:
        if original_input is None:
            del app.input
        else:
            app.input = original_input


def main_tests():
    checks = Checks("End to end")

    device = FakeAS7265X()
    device.fill_spectrum(soil_like)

    firmware = build_firmware(device)

    support.add_path("BD")
    support.add_path("PC")

    import esp32_link
    import rover_science_client as app
    from sample_store import (
        STATE_LOADED, STATE_MEASURED, STATE_READY_TO_LOAD, SampleStore
    )

    # Load a spectrum the instrument could really have produced, so the
    # quality checks are exercised rather than tripped.
    from database import References

    device.fill_from_channels(plausible_sample(References()))

    port = install_link(esp32_link, firmware)

    link = esp32_link.ESP32Link("COM_TEST")
    link.open()
    link.wait_online()

    checks.ok(link.online, "the PC reaches the firmware over the protocol")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)

        mission = app.Mission(link)
        mission.store = SampleStore(root / "samples.json")

        # ==============================================================
        checks.section("1. BD loads the protected data")

        checks.ok(
            mission.science_error is None,
            "references and database load cleanly",
        )
        checks.equal(
            mission.database.count(), 22, "22 reference materials available"
        )

        # ==============================================================
        checks.section("2. carousel calibration")

        status = mission.hardware_status()

        checks.ok(
            not status["carousel"]["position_valid"],
            "the carousel starts unsynchronized after a reset",
        )

        link.sync_load_slot(1)
        status = mission.hardware_status()

        checks.ok(
            status["carousel"]["position_valid"], "synchronization succeeds"
        )
        checks.equal(
            status["carousel"]["current_load_slot"], 1, "Slot 1 at the loader"
        )
        checks.equal(
            status["carousel"]["current_scan_slot"], 3,
            "Slot 3 at the scanner, 180 degrees / two slots away",
        )
        checks.equal(
            status["carousel"]["slot_count"], 4, "four slots"
        )
        checks.equal(
            len(status["slots"]), 4, "four physical slot records"
        )

        # ==============================================================
        checks.section("3. prepare and confirm")

        link.select_slot(1, "S001")
        record = mission.store.create(
            "S001", 1, "2026-08-11T12:00:00+00:00",
            {"task": "crater floor", "photo_reference": "IMG_0042"},
        )

        checks.equal(record["state"], STATE_READY_TO_LOAD, "sample prepared")

        mission.store.set_state(
            "S001", STATE_LOADED, "loaded_at", "2026-08-11T12:02:00+00:00"
        )

        view = mission.slot_view(mission.hardware_status())
        entry = mission.entry_for(view, 1)

        checks.equal(entry["state"], STATE_LOADED, "slot 1 reads as LOADED")
        checks.equal(entry["sample_id"], "S001", "with the right sample")

        # ==============================================================
        checks.section("4. measurement: 180 out, RAW, 180 back")

        home_scan_slot = mission.hardware_status()["carousel"][
            "current_scan_slot"
        ]

        data = link.measure_raw(1, "S001")

        checks.equal(
            sorted(data["illuminations"].keys()), ["ir", "uv", "white"],
            "all three illuminations arrive",
        )

        for name in ("white", "uv", "ir"):
            block = data["illuminations"][name]

            checks.equal(
                len(block["acquisitions"][0]), 18,
                "{} carries 18 channels".format(name.upper()),
            )
            checks.equal(
                len(block["acquisitions"]), block["repeats"],
                "{} returns every repeat".format(name.upper()),
            )

        checks.equal(
            data["sensor_settings"]["measurement_mode"], 3,
            "acquired in deterministic one-shot mode",
        )
        checks.ok(data["bulbs_off"], "every lamp is off afterwards")
        checks.ok(data["home_restored"], "the carousel returned home")
        checks.equal(
            data["carousel"]["carousel_phase"], "LOAD",
            "the sample ends at the loading position it started from",
        )
        checks.equal(
            data["carousel"]["current_scan_slot"], home_scan_slot,
            "the tracked position matches the starting position",
        )
        checks.ok(
            data["carousel"]["position_valid"],
            "position tracking survived the round trip",
        )
        checks.equal(
            data["sensor_settings"]["integration_cycles"], 100,
            "settings were read back from the sensor, not assumed",
        )
        checks.equal(
            data["sensor_settings"]["gain_x"], "16x", "gain is 16x"
        )

        firmware_frame = port.frames[-1]

        for token in ("normalized", "similarity", "best_match", "Kaolin"):
            checks.ok(
                token not in firmware_frame,
                "the ESP32 frame contains no '{}'".format(token),
            )

        # ==============================================================
        checks.section("5. BD turns RAW into science")

        result = mission.analyse_raw(data, data["sensor_settings"])

        measurement = result["measurement"]

        checks.equal(result["analysis_status"], "OK", "analysis succeeds")
        checks.equal(
            sorted(measurement["raw"].keys()), ["ir", "uv", "white"],
            "all three raw spectra preserved - 54 features",
        )
        checks.equal(
            len(measurement["raw"]["white"]), 18, "white raw preserved"
        )
        checks.equal(
            len(measurement["dark_corrected"]), 18, "dark-corrected computed"
        )
        checks.equal(
            len(measurement["normalized"]), 18, "normalized computed"
        )
        checks.equal(
            result["quality"]["status"], "PASS", "quality control ran"
        )
        checks.equal(
            len(result["reference_matches"]), 22,
            "every material was compared",
        )
        checks.ok(
            result["analysis"]["best_match"] is not None,
            "an interpretation was produced",
        )

        # The formula, verified against the protected references.
        references = mission.references
        channel = "A"
        raw_white = measurement["raw"]["white"][channel]

        expected = (
            (raw_white - references.dark[channel])
            / (references.white[channel] - references.dark[channel])
        )

        checks.close(
            measurement["normalized"][channel], round(expected, 6),
            "R = (S - D) / (W - D) holds against the real references",
            tolerance=1e-6,
        )
        checks.close(
            measurement["dark_corrected"][channel],
            round(raw_white - references.dark[channel], 4),
            "C = S - D holds against the real Dark",
            tolerance=1e-4,
        )
        checks.equal(
            measurement["legacy_database_normalized"]["white"],
            measurement["normalized"],
            "the database comparison used the LEGACY calibration",
        )

        # ==============================================================
        checks.section("6. the PC completes the SAME record")

        record = mission.store.get_sample("S001")
        record["state"] = STATE_MEASURED
        record["timestamps"]["measured_at"] = "2026-08-11T12:05:00+00:00"

        # The same assembly the measurement screen uses, so the archive
        # under test is the one production writes.
        app.apply_measurement(record, result)

        mission.store.save(record)

        checks.equal(
            mission.store.count(), 1, "no second Sample ID was created"
        )

        saved = mission.store.get_sample("S001")

        checks.equal(saved["state"], STATE_MEASURED, "state is MEASURED")
        checks.equal(
            saved["timestamps"]["created_at"], "2026-08-11T12:00:00+00:00",
            "the preparation timestamp survived",
        )
        checks.equal(
            saved["timestamps"]["loaded_at"], "2026-08-11T12:02:00+00:00",
            "the loading timestamp survived",
        )
        checks.equal(
            saved["metadata"]["photo_reference"], "IMG_0042",
            "the metadata survived",
        )
        checks.equal(
            len(saved["reference_matches"]), 22,
            "all matches are stored, not just the top few",
        )
        checks.equal(
            saved["calibration"]["legacy_database_calibration_id"],
            "FREYA_COMPETITION_2026_CAL_V1",
            "the record names the calibration the comparison was valid under",
        )

        # The whole point of the archive: everything needed to re-derive
        # the result is inside the record, including the White and Dark
        # actually used.
        channels = app.sample_analysis.CHANNELS

        for name, block in (
            ("raw white", saved["measurement"]["raw"]["white"]),
            ("raw uv", saved["measurement"]["raw"]["uv"]),
            ("raw ir", saved["measurement"]["raw"]["ir"]),
            ("dark_corrected", saved["measurement"]["dark_corrected"]),
            ("normalized", saved["measurement"]["normalized"]),
            ("dark_reference", saved["calibration"]["dark_reference"]),
            ("white_reference", saved["calibration"]["white_reference"]),
        ):
            checks.equal(
                sorted(block.keys()), sorted(channels),
                "{} is stored with all 18 channels".format(name),
            )

        checks.equal(
            sum(
                len(saved["measurement"]["raw"][name])
                for name in ("white", "uv", "ir")
            ),
            54,
            "the record holds the full 54 spectral features",
        )
        checks.ok(
            saved["quality"], "the quality report is archived with it"
        )

        checks.equal(
            saved["calibration"]["dark_reference"],
            {c: references.dark[c] for c in channels},
            "the Dark snapshot is the one actually used",
        )
        checks.equal(
            saved["calibration"]["white_reference"],
            {c: references.white[c] for c in channels},
            "and so is the White snapshot",
        )
        checks.equal(
            saved["best_match"], saved["analysis"]["best_match"],
            "the flat table mirror agrees with the analysis block",
        )
        checks.ok(
            saved["conclusion"], "the written conclusion is stored"
        )
        checks.ok(
            saved["measured"], "and the measured flag is set"
        )

        # Offline re-analysis must be possible from the stored raw.
        replay = app.sample_analysis.analyze(
            {"raw": saved["measurement"]["raw"]["white"]},
            references, mission.database,
        )

        checks.equal(
            replay["analysis"]["best_match"],
            saved["analysis"]["best_match"],
            "the stored raw spectrum reproduces the same result offline",
        )

        # ==============================================================
        checks.section("7. failure semantics")

        # The measurement already brought the carousel home, so choosing
        # the next slot is one plain 90 degree step - no half turn to
        # undo first.
        move = link.select_slot(2)

        checks.ok(
            not move["move"].get("restored_load_orientation"),
            "no orientation restore is needed after a completed measurement",
        )
        checks.equal(
            move["move"]["steps"], 1, "one whole-slot step to the next slot"
        )
        checks.equal(
            move["carousel"]["current_load_slot"], 2,
            "Slot 2 is at the loader",
        )
        checks.equal(
            move["carousel"]["current_scan_slot"], 4,
            "which puts Slot 4 at the scanner",
        )

        # A sensor that dies mid-run must not produce a MEASURED sample.
        mission.store.create("S002", 2, "2026-08-11T12:10:00+00:00")
        mission.store.set_state(
            "S002", STATE_LOADED, "loaded_at", "2026-08-11T12:11:00+00:00"
        )

        device.absent_scans = 999
        firmware.sensor.ready = False

        try:
            link.measure_raw(2, "S002")
            failed = None

        except esp32_link.LinkError as error:
            failed = error

        checks.ok(failed is not None, "a dead sensor fails the measurement")
        checks.equal(
            failed.data.get("moved"), False,
            "and nothing moved, so the sample is still at the loader",
        )
        checks.equal(
            mission.store.get_state("S002"), STATE_LOADED,
            "the sample stays LOADED - no false MEASURED state",
        )

        # It recovers by itself once the sensor answers again.
        device.absent_scans = 0

        recovered = link.measure_raw(2, "S002")

        checks.equal(
            len(recovered["illuminations"]["white"]["acquisitions"][0]), 18,
            "the next measurement recovers the sensor and succeeds",
        )

        status = mission.hardware_status()

        checks.ok(
            status["sensor"]["ready"], "the sensor reports READY again"
        )
        checks.ok(
            status["sensor"]["recovery_count"] >= 1,
            "the recovery is counted and visible",
        )

        # ==============================================================
        checks.section("8. ESP32 -> PC sample sync")

        database_before = hashlib.sha256(
            Path(mission.database.path).read_bytes()
        ).hexdigest()
        references_before = hashlib.sha256(
            Path(mission.references.path).read_bytes()
        ).hexdigest()

        index = link.list_saved_samples()

        checks.equal(
            index["count"], 2,
            "the ESP32 is holding both acquisitions it took",
        )

        held = sorted(entry["sample_id"] for entry in index["samples"])

        checks.equal(held, ["S001", "S002"], "and names them")

        # Both already exist on the PC, so a sync must transfer nothing.
        report = run_quietly(app, app.sync_esp32_samples, mission)

        checks.equal(
            mission.store.count(), 2,
            "syncing samples the PC already has creates nothing",
        )

        # Now simulate the PC having lost one.
        mission.store.delete("S002")

        checks.equal(mission.store.count(), 1, "S002 removed from the PC")

        report = run_quietly(app, app.sync_esp32_samples, mission)

        checks.equal(mission.store.count(), 2, "the missing sample came back")

        restored = mission.store.get_sample("S002")

        checks.ok(restored is not None, "S002 exists on the PC again")
        checks.equal(
            len(restored["measurement"]["raw"]["white"]), 18,
            "with all 18 white raw channels",
        )
        checks.equal(
            sorted(restored["measurement"]["raw"].keys()),
            ["ir", "uv", "white"],
            "and all three illuminations",
        )
        checks.equal(
            len(restored["measurement"]["normalized"]), 18,
            "and a normalized spectrum computed by BD on the PC",
        )
        checks.equal(
            len(restored["reference_matches"]), 22,
            "compared against every material",
        )
        checks.equal(
            restored["state"], STATE_MEASURED, "restored as MEASURED"
        )
        checks.equal(
            restored["source"]["origin"], "esp32_sync",
            "and marked as having come from the device",
        )

        # Idempotence: a third run must change nothing.
        before = mission.store.count()
        report = run_quietly(app, app.sync_esp32_samples, mission)

        checks.equal(
            mission.store.count(), before, "sync is idempotent"
        )

        # The copy is a copy: the device keeps its own.
        checks.equal(
            link.list_saved_samples()["count"], 2,
            "the ESP32 still holds both - sync never deletes",
        )

        checks.equal(
            hashlib.sha256(
                Path(mission.database.path).read_bytes()
            ).hexdigest(),
            database_before,
            "database.json was not touched by the sync",
        )
        checks.equal(
            hashlib.sha256(
                Path(mission.references.path).read_bytes()
            ).hexdigest(),
            references_before,
            "references.json was not touched by the sync",
        )

        # ==============================================================
        checks.section("9. import conflict handling")

        # Same Sample ID on both sides, different measurement data.
        clashing = mission.store.get_sample("S002")
        clashing["measurement"]["raw"]["white"]["A"] = 999.0
        mission.store.save(clashing)

        report = run_quietly(app, app.sync_esp32_samples, mission)

        checks.ok("CONFLICT" in report, "a differing record is a CONFLICT")
        checks.ok("Conflicts:  1" in report, "and is counted as one")
        checks.close(
            mission.store.get_sample(
                "S002")["measurement"]["raw"]["white"]["A"],
            999.0,
            "the PC record was NOT overwritten",
        )

        # ==============================================================
        checks.section("10. delete ALL ESP32 samples")

        run_quietly(app, app.delete_esp32_samples, mission, answer="n")

        checks.equal(
            link.list_saved_samples()["count"], 2,
            "'n' deletes nothing",
        )

        run_quietly(app, app.delete_esp32_samples, mission, answer="")

        checks.equal(
            link.list_saved_samples()["count"], 2,
            "and a bare Enter deletes nothing either",
        )

        pc_before = mission.store.count()
        slots_before = [
            slot["occupied"]
            for slot in mission.hardware_status()["slots"]
        ]

        report = run_quietly(
            app, app.delete_esp32_samples, mission, answer="y"
        )

        checks.equal(
            link.list_saved_samples()["count"], 0,
            "'y' empties the ESP32 storage",
        )
        checks.ok(
            "ESP32 Sample storage is now empty" in report,
            "and the emptiness is verified by reading the device back",
        )
        checks.ok(
            "VERIFICATION FAILED" not in report,
            "with no verification failure",
        )
        checks.equal(
            mission.store.count(), pc_before,
            "the PC archive is untouched",
        )
        checks.equal(
            [
                slot["occupied"]
                for slot in mission.hardware_status()["slots"]
            ],
            slots_before,
            "physical slot occupancy is untouched",
        )
        checks.equal(
            hashlib.sha256(
                Path(mission.database.path).read_bytes()
            ).hexdigest(),
            database_before,
            "database.json untouched by the delete",
        )
        checks.equal(
            hashlib.sha256(
                Path(mission.references.path).read_bytes()
            ).hexdigest(),
            references_before,
            "references.json untouched by the delete",
        )

        # ==============================================================
        checks.section("11. clear ALL physical slots")

        occupied_before = [
            slot["slot_id"]
            for slot in mission.hardware_status()["slots"]
            if slot["occupied"]
        ]

        checks.ok(occupied_before, "slots are occupied to begin with")

        view = mission.slot_view(mission.hardware_status())
        pc_before = mission.store.count()

        run_quietly(app, app.clear_all_slots, mission, view, answer="yes")

        checks.equal(
            [
                slot["slot_id"]
                for slot in mission.hardware_status()["slots"]
                if slot["occupied"]
            ],
            occupied_before,
            "a lowercase 'yes' is NOT the confirmation and clears nothing",
        )

        run_quietly(app, app.clear_all_slots, mission, view, answer="YES")

        status = mission.hardware_status()

        checks.ok(
            all(not slot["occupied"] for slot in status["slots"]),
            "YES clears every physical slot",
        )
        checks.ok(
            all(slot["sample_id"] is None for slot in status["slots"]),
            "and drops the slot-to-Sample associations",
        )
        checks.equal(
            mission.store.count(), pc_before,
            "no saved Sample record was deleted",
        )
        checks.ok(
            status["carousel"]["position_valid"],
            "and the carousel position is still valid",
        )

        view = mission.slot_view(status)

        checks.ok(
            all(entry["state"] == "EMPTY" for entry in view),
            "the main screen shows all four slots EMPTY",
        )

    link.close()

    return checks.report()


if __name__ == "__main__":
    sys.exit(main_tests())
