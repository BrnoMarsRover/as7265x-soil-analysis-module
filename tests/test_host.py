"""
Loopback test: the real host client talking to the real firmware
dispatcher through a fake serial port. Validates the wire format,
request_id matching, timestamp injection and error mapping.
"""

import os
import sys

import re as _re

import test_firmware as fw

sys.path.insert(0, os.path.join(fw.REPO, "host"))


class LoopbackSerial:
    """Feeds each written command line straight into ScienceModule."""

    def __init__(self, module, noise=()):
        self.module = module
        self.rx = b""
        self.timeout = 1.0
        self.seen = []

        # Console output the firmware does not control: MicroPython's
        # boot banner, REPL prompts, a stray partial line.
        self.noise = list(noise)

    def reset_input_buffer(self):
        self.rx = b""

    def reset_output_buffer(self):
        pass

    def flush(self):
        pass

    def close(self):
        pass

    def write(self, data):
        import json

        # Anything the console emits arrives before the reply does.
        while self.noise:
            self.rx += self.noise.pop(0)

        for line in data.split(b"\n"):
            if not line.strip():
                continue

            request = json.loads(line.decode())
            self.seen.append(request)
            response = self.module.dispatch_command(request)
            self.rx += json.dumps(response).encode() + b"\n"

        return len(data)

    def readline(self):
        index = self.rx.find(b"\n")

        if index < 0:
            return b""

        line = self.rx[: index + 1]
        self.rx = self.rx[index + 1:]

        return line


def run():
    import shutil
    import tempfile

    workdir = tempfile.mkdtemp(prefix="freya_host_")
    for name in ("database.json", "references.json"):
        shutil.copy(os.path.join(fw.FIRMWARE, name),
                    os.path.join(workdir, name))
    os.chdir(workdir)

    main = fw.load_main_module()
    import config

    config.STARTUP_DELAY_SECONDS = 0
    config.SERVO_SETTLE_TIME = 0
    config.SCAN_SETTLE_TIME = 0
    config.NEXT_SLOT_CW_MS = 1
    config.NEXT_SLOT_CCW_MS = 2
    config.SERVO_INTER_STEP_PAUSE_MS = 0

    sci = main.ScienceModule()
    sci.boot()

    from as7265x import SoilMeasurementSystem

    driver = fw.FakeDriver({
        "A": 12.0, "B": 40.0, "C": 130.0, "D": 70.0, "E": 110.0, "F": 150.0,
        "G": 120.0, "H": 190.0, "I": 190.0, "J": 45.0, "K": 5.0, "L": 7.0,
        "R": 600.0, "S": 180.0, "T": 40.0, "U": 22.0, "V": 33.0, "W": 20.0,
    })
    sci.driver = driver
    sci.sensor = SoilMeasurementSystem(driver)
    sci.sensor_error = None
    sci.install_references()

    import rover_science_client as host

    def make_client(noise=()):
        c = host.RoverScienceClient.__new__(host.RoverScienceClient)
        c.port = "LOOPBACK"
        c.baudrate = 115200
        c.timeout = 5.0
        c.connect_timeout = 5.0
        c.verbose = False
        c.online = False
        c._request_id = 0
        c.serial = LoopbackSerial(sci, noise=noise)

        return c

    print("\n[host] connecting through console noise")

    # Exactly what a real ESP32 emits after the port open resets it.
    boot_noise = [
        b"ets Jun  8 2016 00:22:57\r\n",
        b"rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\r\n",
        b"configsip: 0, SPIWP:0xee\r\n",
        b"MicroPython v1.28.0 on 2026-04-06; ESP32 module with ESP32\r\n",
        b'Type "help()" for more information.\r\n',
        b">>> \r\n",
        b'{"request_id": "stale", "ok": true, "data": {}}\n',
        b"\n",
    ]

    noisy = make_client(noise=boot_noise)
    noisy.wait_online()
    fw.check("client comes online despite the boot banner",
             noisy.online is True)
    fw.check("banner did not corrupt the session",
             noisy.get_status()["database_material_count"] == 22)

    client = make_client()

    print("\n[host] round trip")
    client.wait_online()
    fw.check("wait_online marks the link online", client.online is True)

    data = client.ping()
    fw.check("ping through the client", data["pong"] is True)

    status = client.get_status()
    fw.check("get_status through the client",
             status["database_material_count"] == 22)

    fw.check("client injects an ISO timestamp",
             "T" in client.serial.seen[-1]["timestamp"],
             client.serial.seen[-1])
    fw.check("client sends an incrementing request_id",
             client.serial.seen[0]["request_id"] == "1"
             and client.serial.seen[1]["request_id"] == "2")
    client_src = open(os.path.join(fw.REPO, "host",
                                   "rover_science_client.py")).read()
    pin_refs = [t for t in ("GPIO16", "GPIO17", "UART2", "TX=", "RX=")
                if t in client_src]
    fw.check("client references no specific pins or UART peripheral",
             not pin_refs, pin_refs)
    import re as _re
    toggles = _re.findall(
        r"\.(?:dtr|rts)\s*=|(?:dsrdtr|rtscts)\s*=", client_src
    )
    fw.check("client never toggles the reset lines", not toggles, toggles)

    print("\n[host] full competition workflow")
    client.sync_position(1)
    carousel = client.get_carousel_status()
    fw.check("sync through the client",
             carousel["current_scan_slot"] == 1
             and carousel["current_load_slot"] == 5)

    client.prepare_load(1, "S001", metadata={"task": "survey",
                                             "hypothesis": "iron rich"})
    slots = client.get_slots()["slots"]
    fw.check("prepare_load through the client",
             slots[0]["state"] == "READY_TO_LOAD"
             and slots[0]["sample_id"] == "S001")

    client.confirm_loaded(1)
    fw.check("confirm_loaded through the client",
             client.get_slots()["slots"][0]["state"] == "LOADED")

    result = client.measure_sample(1, metadata={"location": "site A"})
    fw.check("measure through the client", result["saved"] is True)
    fw.check("all matches came back over the wire",
             len(result["sample"]["reference_matches"]) == 22)
    fw.check("metadata from both stages present",
             result["sample"]["metadata"]["task"] == "survey"
             and result["sample"]["metadata"]["location"] == "site A")

    listing = client.list_samples()
    fw.check("list_samples through the client", listing["count"] == 1)

    record = client.get_sample("S001")["sample"]
    fw.check("get_sample through the client", record["sample_id"] == "S001")

    updated = client.update_sample_metadata("S001", {"note": "dry"})
    fw.check("update_sample_metadata through the client",
             updated["metadata"]["note"] == "dry")

    slot = client.get_slots()["slots"][0]
    fw.check("slot still occupied after measuring",
             slot["state"] == "MEASURED" and slot["occupied"] is True)

    client.clear_slot(1)
    fw.check("clear_slot through the client",
             client.get_slots()["slots"][0]["state"] == "EMPTY")
    fw.check("science record survives clear_slot",
             client.list_samples()["count"] == 1)

    print("\n[host] error mapping")
    try:
        client.measure_sample(3)
        fw.check("error raised for empty slot", False, "no exception")
    except host.RoverScienceError as error:
        fw.check("error raised for empty slot", error.code == "SLOT_EMPTY",
                 error.code)

    try:
        client.request("get_sample", sample_id="MISSING")
        fw.check("error raised for missing sample", False, "no exception")
    except host.RoverScienceError as error:
        fw.check("error raised for missing sample",
                 error.code == "SAMPLE_NOT_FOUND", error.code)

    print("\n[host] display helpers")
    lines = host.format_slot_table(client.get_slots()["slots"])
    fw.check("slot table renders 8 rows", len(lines) == 8, lines)
    fw.check("empty slots render as ----", "----" in lines[0], lines[0])

    # ------------------------------------------------------------------
    print("\n[host] the reported bug: Measure must never do nothing")
    # ------------------------------------------------------------------
    import builtins
    import io as _io

    def run_menu(handler, answers, target=None):
        queue = list(answers)

        def fake_input(prompt=""):
            value = queue.pop(0) if queue else ""
            sys.stdout.write("{}{}\n".format(prompt, value))

            return value

        real_input = builtins.input
        real_stdout = sys.stdout
        builtins.input = fake_input
        screen = _io.StringIO()
        sys.stdout = screen

        err = None
        try:
            handler(target if target is not None else client)
        except BaseException as exc:                      # noqa: BLE001
            err = exc
        finally:
            sys.stdout = real_stdout
            builtins.input = real_input

        return screen.getvalue(), err

    # A slot in LOADED, operator just presses Enter at the prompt.
    client.select_slot(4)
    client.prepare_load(4, "S200")
    client.confirm_loaded(4)

    screen, err = run_menu(host.menu_measure, ["m"])

    fw.check("measure runs on the selected slot",
             "MEASUREMENT COMPLETE" in screen, screen[:300])
    fw.check("no exception escaped the menu", err is None, repr(err))
    fw.check("stage log shown to the operator",
             "[4/9] SENSOR_READ" in screen, screen[:300])
    fw.check("all nine stages displayed",
             all("[{}/9]".format(i) in screen for i in range(1, 10)))
    fw.check("progress message shown before waiting",
             "Measurement in progress" in screen)
    fw.check("result reports the physical slot",
             "= MEASURED / OCCUPIED" in screen, screen[-600:])
    fw.check("result confirms persistence",
             "samples.json: SAVED" in screen)
    fw.check("firmware really transitioned the slot",
             sci.carousel.slots[4]["state"] == "MEASURED")

    # Explicit slot number still works.
    client.select_slot(6)
    client.prepare_load(6, "S201")
    client.confirm_loaded(6)
    screen, err = run_menu(host.menu_measure, ["m"])
    fw.check("prepared slot becomes the measured slot",
             "MEASUREMENT COMPLETE" in screen and err is None, screen[:300])
    fw.check("slot 6 measured", sci.carousel.slots[6]["state"] == "MEASURED")

    # Nothing LOADED: must explain, not fall through in silence.
    screen, err = run_menu(host.menu_measure, ["m"])
    fw.check("non-LOADED slot is explained clearly",
             "MEASUREMENT NOT AVAILABLE" in screen
             or "No slot is currently LOADED" in screen, screen[:400])
    fw.check("operator told what state is required",
             "Required state:" in screen
             or "confirm it loaded" in screen, screen[:400])

    # Failure path must name the stage.
    client.select_slot(7)
    client.prepare_load(7, "S202")
    client.confirm_loaded(7)
    driver.fail = True
    screen, err = run_menu(host.menu_measure, ["m"])
    driver.fail = False

    fw.check("failure is announced, not swallowed",
             "MEASUREMENT FAILED" in screen, screen[:300])
    fw.check("failure names the stage",
             "Stage:     SENSOR_READ" in screen, screen)
    fw.check("failure states the slot is unchanged",
             "Slot remains: LOADED" in screen)
    fw.check("failure promises no false state",
             "No false MEASURED state was written." in screen)
    fw.check("firmware kept the slot LOADED",
             sci.carousel.slots[7]["state"] == "LOADED")

    # Calibration must be visible and never requested.
    screen, err = run_menu(host.print_status, [])
    fw.check("status shows fixed calibration",
             "FIXED STORED REFERENCES" in screen, screen[-600:])
    fw.check("status shows 18/18 channels",
             screen.count("18/18") >= 2, screen[-600:])
    fw.check("status shows recalibration disabled",
             "DISABLED" in screen)

    screen, err = run_menu(host.menu_help, [])
    fw.check("help explains calibration", "CALIBRATION" in screen)
    flat = " ".join(screen.split())
    fw.check("help says no white/dark measurement is needed",
             "do NOT need to perform dark or white measurements" in flat,
             flat[:600])
    fw.check("help lists the five workflow commands",
             all("[{}]".format(i) in screen for i in range(1, 6)), screen)
    fw.check("help explains the Tools submenu",
             "[t] Tools / Records" in screen)
    fw.check("help shows the normal workflow",
             "NORMAL WORKFLOW" in screen)

    screen, err = run_menu(host.print_status, [])
    fw.check("status reassures about calibration",
             "FIXED STORED REFERENCES" in screen, screen[-800:])

    # No menu may offer a white/dark calibration action.
    labels = " ".join(
        e[1] for e in host.TOOLS_MENU
    ).lower() + " " + " ".join(host.MAIN_ACTIONS.keys()).lower()
    fw.check("menu offers no white/dark calibration",
             not any(w in labels for w in
                     ("white", "dark", "calibrate")), labels)

    # ------------------------------------------------------------------
    print("\n[host] new carousel workflow UI")
    # ------------------------------------------------------------------
    config.NEXT_SLOT_CW_MS = 600
    config.NEXT_SLOT_CCW_MS = 600

    fresh = main.ScienceModule()
    fresh.boot()
    fresh.driver = driver
    fresh.sensor = SoilMeasurementSystem(driver)
    fresh.sensor_error = None
    fresh.install_references()

    c2 = make_client()
    c2.serial = LoopbackSerial(fresh)
    c2.online = True

    # Startup: unsynchronized, short menu, sync is the required action.
    screen, err = run_menu(
        lambda c: host.print_startup_screen(c, {}), [], target=c2)
    fw.check("startup shows NOT CALIBRATED",
             "NOT CALIBRATED" in screen, screen[:400])
    fw.check("startup tells the operator to calibrate Slot 1",
             "physical Slot 1 must be aligned" in screen,
             screen[:600])
    fw.check("startup offers exactly one workflow action",
             "[0] Initial Carousel Calibration" in screen
             and "[1] Choose" not in screen, screen)

    # No menu anywhere may ask for milliseconds.
    client_src = open(os.path.join(fw.REPO, "host",
                                   "rover_science_client.py")).read()
    prompts = _re.findall(r'ask\w*\(\s*"([^"]+)"', client_src)
    ms_prompts = [p for p in prompts
                  if "ms" in p.lower() or "millisec" in p.lower()]
    fw.check("no operator prompt asks for milliseconds",
             not ms_prompts, ms_prompts)
    fw.check("no sensor distance prompt remains",
             "Sensor distance in mm" not in client_src)
    fw.check("metadata fields exclude sensor distance",
             all(k != "sensor_distance_mm" for k, _ in host.METADATA_FIELDS),
             host.METADATA_FIELDS)

    # Sync: whole slot CW, fine +2, fine -1, then set as Slot 1 (Test A).
    screen, err = run_menu(
        host.menu_initial_calibration,
        ["1",                # one whole slot clockwise
         "3", "2",           # fine +2 deg
         "3", "-1",          # fine -1 deg
         "5"],               # SET CURRENT POSITION AS SLOT 1
        target=c2,
    )
    fw.check("calibration menu ran without error", err is None,
             repr(err))
    fw.check("calibration menu shows the goal",
             "Align physical Slot 1 exactly under the soil loading hole"
             in screen, screen[:600])
    fw.check("whole slot move reported",
             "Moved one slot clockwise." in screen, screen)
    fw.check("fine adjust reported in degrees",
             "Adjusted +2.00 deg clockwise." in screen, screen)
    fw.check("negative fine adjust reported counter-clockwise",
             "Adjusted -1.00 deg counter-clockwise." in screen, screen)
    fw.check("calibration completion announced",
             "Calibration complete." in screen, screen[-800:])
    fw.check("Slot 1 declared the loading position",
             "Slot 1 = LOADING position" in screen, screen[-800:])
    fw.check("Slot 5 declared the scanner position",
             "Slot 5 = SCANNER position" in screen, screen[-800:])
    fw.check("clockwise ordering shown",
             "1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7 -> 8" in screen)
    fw.check("workflow entry announced",
             "Entering normal competition workflow" in screen)

    fw.check("firmware loader = 1 after sync",
             fresh.carousel.get_load_slot() == 1)
    fw.check("firmware scanner = 5 after sync",
             fresh.carousel.current_scan_slot == 5)
    fw.check("position valid after sync",
             fresh.carousel.position_valid is True)
    fw.check("selected slot = 1 after sync",
             fresh.carousel.selected_slot == 1)

    # Now the full menu appears.
    state = host.read_state(c2)
    screen, err = run_menu(
        lambda c: host.print_main_screen(c, *state), [], target=c2)
    fw.check("main screen shows the selected slot",
             "Selected:" in screen, screen[:400])
    fw.check("main screen shows only five workflow commands",
             all("[{}]".format(i) in screen for i in range(1, 6))
             and "[6]" not in screen, screen)
    fw.check("secondary commands live under Tools",
             "[t] Tools / Records" in screen)
    fw.check("main screen shows action availability",
             "[AVAILABLE]" in screen or "[LOCKED" in screen, screen)

    # Test B - choose Slot 2, expect one clockwise slot.
    screen, err = run_menu(host.menu_choose_slot, ["2"], target=c2)
    fw.check("choose slot ran cleanly", err is None, repr(err))
    fw.check("choose slot reports one clockwise transition",
             "1 slot transition(s) clockwise" in screen, screen[-600:])
    fw.check("choose slot reports the resulting phase",
             "Phase:" in screen, screen[-600:])
    fw.check("selected slot is 2", fresh.carousel.selected_slot == 2)
    fw.check("loader is Slot 2", fresh.carousel.get_load_slot() == 2)

    # Fine adjust must not change the logical slot.
    screen, err = run_menu(host.menu_fine_adjust, ["1"], target=c2)
    fw.check("fine adjust after sync reported",
             "Adjusted +1.00 deg clockwise." in screen, screen)
    fw.check("selected slot unchanged by fine adjust",
             fresh.carousel.selected_slot == 2)
    fw.check("loader unchanged by fine adjust",
             fresh.carousel.get_load_slot() == 2)

    screen, err = run_menu(host.menu_fine_adjust, ["45"], target=c2)
    fw.check("oversized fine adjust refused in the UI",
             "limited to" in screen
             and "whole-slot movement" in screen.lower(), screen)

    # Debug mode must show the degree -> millisecond conversion.
    c2.verbose = True
    screen, err = run_menu(host.menu_fine_adjust, ["2.5"], target=c2)
    c2.verbose = False
    fw.check("debug shows the conversion",
             "[DEBUG] Fine adjustment" in screen
             and "ms/degree" in screen
             and "actual duration command" in screen, screen)

    # Test C - prepare defaults to the selected slot, no distance asked.
    screen, err = run_menu(
        host.menu_prepare,
        ["S777", "survey", "", "", "", "", ""],
        target=c2,
    )
    fw.check("prepare uses the selected slot without asking",
             "Selected physical slot:" in screen
             and "Slot 2" in screen, screen[:400])
    fw.check("prepare never asks for an operator name",
             "Operator" not in screen, screen)
    fw.check("prepare never asks for sensor distance",
             "distance" not in screen.lower(), screen)
    fw.check("optional fields marked optional",
             "[optional]" in screen)
    fw.check("Slot 2 READY_TO_LOAD",
             fresh.carousel.slots[2]["state"] == "READY_TO_LOAD")

    # Test D
    screen, err = run_menu(host.menu_confirm, ["y"], target=c2)
    fw.check("confirm asks for explicit yes",
             "[y] Confirm Loaded" in screen, screen)
    fw.check("confirm marks the slot LOADED",
             fresh.carousel.slots[2]["state"] == "LOADED", screen)

    # Test E - measurement swings 180 deg
    screen, err = run_menu(host.menu_measure, ["m"], target=c2)
    fw.check("measure screen names the sample",
             "[m] Measure S777" in screen, screen[:800])
    fw.check("measurement completed", "MEASUREMENT COMPLETE" in screen,
             screen[-2000:])
    fw.check("Slot 2 measured",
             fresh.carousel.slots[2]["state"] == "MEASURED")
    fw.check("Slot 2 left at the scanner, phase reported SCAN",
             fresh.carousel.current_scan_slot == 2
             and fresh.carousel.phase() == "SCAN",
             fresh.carousel.status())
    fw.check("selected slot still 2 after the swing",
             fresh.carousel.selected_slot == 2)

    record = fresh.store.get_sample("S777")
    fw.check("sensor distance saved as null",
             record["metadata"]["sensor_distance_mm"] is None)
    fw.check("skipped metadata saved as null",
             record["metadata"]["location"] is None)
    fw.check("entered metadata saved",
             record["metadata"]["task"] == "survey")

    shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    run()

    print("\n" + "=" * 60)
    if fw.FAILURES:
        print("{} of {} checks FAILED:".format(len(fw.FAILURES),
                                               fw.CHECKS[0]))
        for name in fw.FAILURES:
            print("  - {}".format(name))
        sys.exit(1)

    print("all {} host checks passed".format(fw.CHECKS[0]))
