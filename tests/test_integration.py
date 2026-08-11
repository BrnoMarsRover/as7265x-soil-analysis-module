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
    esp32_config.SERVO_SETTLE_TIME = 0.0
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

    port = install_link(esp32_link, firmware)

    link = esp32_link.ESP32Link("COM_TEST")
    link.open()
    link.wait_online()

    checks.ok(link.online, "the PC reaches the firmware over the protocol")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)

        mission = app.Mission(link)
        mission.store = SampleStore(root / "samples.json", root / "samples")

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
            status["carousel"]["current_scan_slot"], 5,
            "Slot 5 at the scanner, 180 degrees away",
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
        checks.section("4. measurement: ESP32 returns RAW only")

        data = link.measure_raw(1, "S001")

        checks.equal(len(data["raw"]), 18, "18 raw channels arrive")
        checks.equal(
            data["carousel"]["carousel_phase"], "SCAN",
            "the sample really is at the scanner",
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

        result = mission.analyse_raw(data["raw"], data["sensor_settings"])

        measurement = result["measurement"]

        checks.equal(result["analysis_status"], "OK", "analysis succeeds")
        checks.equal(len(measurement["raw"]), 18, "raw preserved")
        checks.equal(
            len(measurement["dark_corrected"]), 18, "dark-corrected computed"
        )
        checks.equal(
            len(measurement["normalized"]), 18, "normalized computed"
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
        expected = (
            (data["raw"][channel] - references.dark[channel])
            / (references.white[channel] - references.dark[channel])
        )

        checks.close(
            measurement["normalized"][channel], round(expected, 6),
            "R = (S - D) / (W - D) holds against the real references",
            tolerance=1e-6,
        )
        checks.close(
            measurement["dark_corrected"][channel],
            round(data["raw"][channel] - references.dark[channel], 4),
            "C = S - D holds against the real Dark",
            tolerance=1e-4,
        )

        # ==============================================================
        checks.section("6. the PC completes the SAME record")

        record = mission.store.get_sample("S001")
        record["state"] = STATE_MEASURED
        record["timestamps"]["measured_at"] = "2026-08-11T12:05:00+00:00"
        record["measurement"] = measurement
        record["calibration"] = result["calibration"]
        record["database"] = result["database"]
        record["reference_matches"] = result["reference_matches"]
        record["analysis"] = result["analysis"]
        record["analysis_status"] = result["analysis_status"]

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
            saved["calibration"]["calibration_id"],
            "FREYA_COMPETITION_2026_CAL_V1",
            "the record names the calibration it was normalized against",
        )

        # Offline re-analysis must be possible from the stored raw.
        replay = app.sample_analysis.analyze(
            saved["measurement"]["raw"], references, mission.database
        )

        checks.equal(
            replay["analysis"]["best_match"],
            saved["analysis"]["best_match"],
            "the stored raw spectrum reproduces the same result offline",
        )

        # ==============================================================
        checks.section("7. failure semantics")

        # Measuring again from the scanner must be refused, not faked.
        try:
            link.measure_raw(1, "S001")
            refused = None

        except esp32_link.LinkError as error:
            refused = error

        checks.ok(refused is not None, "a second measurement is refused")
        checks.equal(
            refused.code, "SLOT_NOT_AT_LOADER",
            "because the sample is no longer at the loading hole",
        )

        # Choose the next slot: the firmware restores the load
        # orientation with its own calibrated half turn.
        move = link.select_slot(2)

        checks.ok(
            move["move"]["restored_load_orientation"],
            "selecting the next slot restores the loading orientation",
        )
        checks.equal(
            move["carousel"]["current_load_slot"], 2,
            "and brings Slot 2 to the loader",
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
            len(recovered["raw"]), 18,
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

    link.close()

    return checks.report()


if __name__ == "__main__":
    sys.exit(main_tests())
