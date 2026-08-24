"""
A whole session, from the main screen to a saved scientific record.

WHAT IS REAL HERE AND WHAT IS NOT

    real    every screen in workflow/, the menu loop, the prompts, the
            Mission controller, SerialLink's framing and timeouts, the
            ESP32 command parser, the carousel geometry, the servo and
            sensor drivers, Science, and the BD record model
    fake    serial.Serial, machine.I2C, machine.UART, the clock, the
            operator's keyboard, and the directory the records go in

That list is the point of the suite. Every defect this campaign has
found so far lived in a branch that only runs when several layers are
connected: a screen that calls a method the link does not have, a
response field the screen forgets to read, a save whose failure the
workflow does not notice. Testing the layers apart cannot find those.

TWO SESSIONS

    the good one     everything works; a Sample goes in and a record
                     with RAW in it comes out
    the bad one      a hostile session in which the connection is
                     refused, a response is destroyed, the sensor dies,
                     a save fails and the board disconnects - and the
                     operator still finishes with an archive that is
                     true
"""

import sys
from pathlib import Path

_TESTS_DIR = next(
    p for p in Path(__file__).resolve().parents if p.name == "Tests"
)

if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

_SOFTWARE_DIR = _TESTS_DIR / "software"

if str(_SOFTWARE_DIR) not in sys.path:
    sys.path.insert(0, str(_SOFTWARE_DIR))

import support

support.add_project_root()
support.add_path("PC")

import serial_link                                          # noqa: E402
from serial_link import DeviceError, LinkError               # noqa: E402

from BD.samples import (                                     # noqa: E402
    ACQUISITION_FAILED,
    ACQUISITION_SUCCESS,
    STATE_LOADED,
    STATE_MEASURED,
    STATE_READY_TO_LOAD,
)

from fakes import FakeClock, SandboxBD, run_screen           # noqa: E402
from fakes.clock import install_clock                        # noqa: E402
from fakes.esp32 import (                                    # noqa: E402
    LoopbackDevice,
    MALFORMED,
    TIMEOUT,
)
from fakes.serial_port import install_fake_serial, open_link  # noqa: E402

checks = support.Checks("mission")

restore_serial = install_fake_serial(serial_link)


def session(sensor=None, servo=None, lie=None, **faults):
    """A client, a wire, the real firmware and a throwaway BD."""
    clock = FakeClock()
    installed = install_clock(serial_link, clock)

    loopback = LoopbackDevice(device=sensor, servo=servo, lie=lie)
    loopback.build()

    link, port = open_link(serial_link, loopback, clock=clock, **faults)
    link.online = True

    bd = SandboxBD()

    return link, loopback, bd, installed


# The prompts a full PREPARE asks, in order: the Sample ID and then the
# six optional metadata fields.
def prepare_answers(sample_id, note="mission test"):
    return [sample_id, "", "", "", "", note, ""]


# ======================================================================
checks.section("the good session, driven through the real menu loop")

link, loopback, bd, installed = session()

try:
    from workflow import screen as screen_module             # noqa: E402
    from workflow.session import Mission                     # noqa: E402

    # Mission is built by `interactive`, so the sandbox has to be
    # installed into it as it is created. Patching the class the screen
    # module resolves is the least invasive way to do that and leaves
    # every line of `interactive` running for real.
    original_mission = screen_module.Mission

    def sandboxed_mission(link_):
        mission = original_mission(link_)
        mission.store = bd.sample_store()
        mission.calibrations = bd.calibration_store()
        mission.profiles = bd.profile_store()
        mission.load_science()

        return mission

    screen_module.Mission = sandboxed_mission

    script = (
        # startup screen: the position is not valid yet
        ["0"]
        # CAROUSEL SERVO: [1] Connect, then the pause
        + ["1", ""]
        # CAROUSEL SETUP: [7] set this position as Slot 1 / LOAD
        + ["7"]
        # main screen: [1] choose slot -> slot 1
        + ["1", "1"]
        # [2] prepare
        + ["2"] + prepare_answers("S-MISSION-1")
        # [3] confirm the soil is in
        + ["3", "y"]
        # [4] measure, then whatever pauses the result screen asks for
        + ["4", "", "", "", ""]
        # quit
        + ["q"]
    )

    value, output, console = run_screen(
        script, lambda: screen_module.interactive(link), exhausted="q")

    checks.equal(value, 0,
                 "the session ends by quitting cleanly, not by raising")
    checks.ok("FREYA SCIENCE MODULE" in output,
              "and the main screen was actually reached")

    store = bd.sample_store()
    record = store.get_sample("S-MISSION-1")

    checks.ok(record is not None,
              "a Sample record exists afterwards")

    if record:
        checks.equal(record["state"], STATE_MEASURED,
                     "and its state is MEASURED")

        measurements = record.get("measurements") or []
        checks.ok(len(measurements) >= 1,
                  "with at least one Measurement on it")

        if measurements:
            measurement = measurements[0]

            checks.equal(measurement["acquisition_status"],
                         ACQUISITION_SUCCESS,
                         "whose acquisition succeeded")

            raw = measurement.get("raw") or {}

            checks.equal(sorted(raw), ["ir", "uv", "white"],
                         "and which carries RAW for all three "
                         "illuminations, grouped by lamp")

            for name, block in raw.items():
                checks.equal(len(block), 18,
                             "{} has all 18 channels".format(name))

            checks.ok(measurement.get("calibration_id") is not None,
                      "and names the calibration it must be read under")

            runs = measurement.get("analysis_runs") or []
            checks.ok(len(runs) >= 1,
                      "and Science produced an AnalysisRun from it")

        checks.ok(record.get("metadata", {}).get("note") == "mission test",
                  "the metadata the operator typed is on the record")

finally:
    screen_module.Mission = original_mission
    installed.restore()
    link.close()
    bd.close()


# ======================================================================
checks.section("RAW is persisted before Science is allowed to fail")

# The ordering the whole workflow is built around. It is asserted by
# BREAKING Science and checking that the spectrum survived anyway.

link, loopback, bd, installed = session()

try:
    from workflow import measure as measure_module           # noqa: E402
    from workflow.session import Mission                     # noqa: E402

    mission = Mission(link)
    mission.store = bd.sample_store()
    mission.calibrations = bd.calibration_store()
    mission.profiles = bd.profile_store()
    mission.load_science()

    link.connect_servo()
    link.sync_position(load_slot=1)
    link.select_slot(1, sample_id="S-ORDER")

    mission.store.create("S-ORDER", 1)
    mission.store.set_state("S-ORDER", STATE_LOADED)

    status = mission.hardware_status()
    view = mission.slot_view(status)

    # Science raises. Everything before it must already be on disk.
    def exploding(*args, **kwargs):
        raise RuntimeError("Science is broken on purpose")

    original_analyse = mission.analyse_measurement
    mission.analyse_measurement = exploding

    try:
        run_screen(["", "", "", ""],
                   lambda: measure_module.menu_measure(mission, status, view),
                   exhausted="")

    except Exception:                                  # noqa: BLE001
        # A raising analysis is allowed to reach the caller; what is
        # not allowed is for it to cost the spectrum.
        pass

    finally:
        mission.analyse_measurement = original_analyse

    reread = bd.sample_store().get_sample("S-ORDER")
    measurements = (reread or {}).get("measurements") or []

    checks.ok(len(measurements) >= 1,
              "the Measurement is on disk even though the analysis blew "
              "up afterwards")

    if measurements:
        raw = measurements[0].get("raw") or {}
        checks.equal(sorted(raw), ["ir", "uv", "white"],
                     "and it has the full RAW spectrum - the counts the "
                     "detector reported cannot be obtained again from a "
                     "slot that has been emptied, and every derived "
                     "number can")

finally:
    installed.restore()
    link.close()
    bd.close()


# ======================================================================
checks.section("a measurement that fails is recorded as a failure")

# Not as a success full of zeros, and not as nothing at all.

link, loopback, bd, installed = session(
    lie={"measure_raw": TIMEOUT})

try:
    from workflow import measure as measure_module           # noqa: E402
    from workflow.session import Mission                     # noqa: E402

    mission = Mission(link)
    mission.store = bd.sample_store()
    mission.calibrations = bd.calibration_store()
    mission.profiles = bd.profile_store()
    mission.load_science()

    link.connect_servo()
    link.sync_position(load_slot=1)
    link.select_slot(1, sample_id="S-FAILED")

    mission.store.create("S-FAILED", 1)
    mission.store.set_state("S-FAILED", STATE_LOADED)

    status = mission.hardware_status()
    view = mission.slot_view(status)

    link.timeout = 2.0

    _value, output, _console = run_screen(
        ["", "", ""],
        lambda: measure_module.menu_measure(mission, status, view),
        exhausted="")

    checks.ok("failed" in output.lower(),
              "the operator is told the measurement failed")

    record = bd.sample_store().get_sample("S-FAILED")
    measurements = (record or {}).get("measurements") or []

    checks.ok(len(measurements) == 1,
              "and a Measurement was recorded for it")

    if measurements:
        measurement = measurements[0]

        checks.equal(measurement["acquisition_status"], ACQUISITION_FAILED,
                     "marked FAILED")
        checks.ok("raw" not in measurement,
                  "with NO raw key at all - not null, not zeros. A "
                  "spectrum of zeros cannot be told apart from a "
                  "genuinely dark one")
        checks.ok((measurement.get("error") or {}).get("code"),
                  "and it carries the code that explains why")

    checks.equal(record["state"], STATE_LOADED,
                 "and the Sample is still LOADED - the soil is still in "
                 "the slot, so it can be measured again")

finally:
    installed.restore()
    link.close()
    bd.close()


# ======================================================================
checks.section("a save that fails is not reported as a save")

link, loopback, bd, installed = session()

try:
    from BD import samples as samples_module                 # noqa: E402
    from workflow import measure as measure_module           # noqa: E402
    from workflow.session import Mission                     # noqa: E402

    mission = Mission(link)
    mission.store = bd.sample_store()
    mission.calibrations = bd.calibration_store()
    mission.profiles = bd.profile_store()
    mission.load_science()

    link.connect_servo()
    link.sync_position(load_slot=1)
    link.select_slot(1, sample_id="S-NOSAVE")

    mission.store.create("S-NOSAVE", 1)
    mission.store.set_state("S-NOSAVE", STATE_LOADED)

    status = mission.hardware_status()
    view = mission.slot_view(status)

    before = bd.samples_file.read_bytes()

    original_replace = samples_module.os.replace

    def refuse(*args, **kwargs):
        raise OSError(28, "No space left on device")

    samples_module.os.replace = refuse

    try:
        _value, output, _console = run_screen(
            ["", "", "", ""],
            lambda: measure_module.menu_measure(mission, status, view),
            exhausted="")

    except Exception as error:                         # noqa: BLE001
        output = "RAISED:" + type(error).__name__

    finally:
        samples_module.os.replace = original_replace

    checks.ok(not output.startswith("RAISED:"),
              "a full disk during a save does not crash the operator "
              "client ({})".format(output[:60]))
    checks.equal(bd.samples_file.read_bytes(), before,
                 "and the archive on disk is unchanged")

    reread = bd.sample_store().get_sample("S-NOSAVE")
    checks.equal((reread or {}).get("measurements") or [], [],
                 "and no Measurement was recorded, because none was "
                 "saved")

finally:
    installed.restore()
    link.close()
    bd.close()


# ======================================================================
checks.section("the hostile session: everything goes wrong, in order")

# One long session in which each failure happens once and the operator
# recovers from it. What is being checked is not that any single one is
# handled - the fault suites do that - but that the SEQUENCE leaves a
# true archive behind.

servo = support.FakeST3215()
sensor = support.FakeAS7265X()

link, loopback, bd, installed = session(sensor=sensor, servo=servo)

try:
    from workflow.session import Mission                     # noqa: E402

    mission = Mission(link)
    mission.store = bd.sample_store()
    mission.calibrations = bd.calibration_store()
    mission.profiles = bd.profile_store()
    mission.load_science()

    steps = []

    def step(label, call):
        try:
            call()
            steps.append((label, "OK"))

            return True

        except (DeviceError, LinkError) as error:
            steps.append((label, error.code))

            return False

        except Exception as error:                     # noqa: BLE001
            steps.append((label, "CRASH:" + type(error).__name__))

            return False

    # 1. the servo is not answering yet
    servo.silent = True
    step("connect with a silent servo", link.connect_servo)

    # 2. the operator fixes the wiring and connects
    servo.silent = False
    step("connect again", link.connect_servo)
    step("declare the origin", lambda: link.sync_position(load_slot=1))

    # 3. a measurement whose answer is destroyed in transit
    loopback.lie = {"measure_raw": MALFORMED}
    step("select the slot", lambda: link.select_slot(1, "S-HOSTILE"))
    mission.store.create("S-HOSTILE", 1)
    mission.store.set_state("S-HOSTILE", STATE_LOADED)
    step("measure with a destroyed answer",
         lambda: link.measure_raw(1, "S-HOSTILE"))

    # 4. the answer comes back intact next time
    loopback.lie = {}
    ok = step("measure again", lambda: link.measure_raw(1, "S-HOSTILE"))

    # 5. the sensor dies, and comes back
    sensor.bus_error = True
    step("measure with a dead sensor", lambda: link.measure_raw(1))
    sensor.bus_error = False
    step("measure once the bus returns",
         lambda: link.measure_raw(1, "S-HOSTILE"))

    # 6. the board disconnects mid-command and the operator reconnects
    servo.silent = True
    step("move with the servo gone", lambda: link.move_slots("cw", 1))
    servo.silent = False
    step("reconnect", link.connect_servo)
    step("re-declare the origin",
         lambda: link.sync_position(load_slot=1))

    crashes = [label for label, code in steps if code.startswith("CRASH")]

    checks.equal(crashes, [],
                 "not one step of the hostile session raised an "
                 "unhandled exception")

    outcomes = dict(steps)

    checks.ok(outcomes["connect with a silent servo"] != "OK",
              "a silent servo is refused")
    checks.ok(outcomes["connect again"] == "OK",
              "and connecting works once it answers")
    checks.ok(outcomes["measure with a destroyed answer"] != "OK",
              "a destroyed answer is not a measurement")
    checks.ok(outcomes["measure again"] == "OK",
              "and the next attempt succeeds")
    checks.ok(outcomes["measure with a dead sensor"] != "OK",
              "a dead sensor is not a measurement either")
    checks.ok(outcomes["re-declare the origin"] == "OK",
              "and the session is fully recoverable at the end of all "
              "of it")

    status = link.get_status()
    checks.equal(status["carousel"]["position_valid"], True,
                 "with a carousel that knows where it is")

    checks.ok(link.corrupt_frames >= 1,
              "the damaged frame was counted rather than silently "
              "survived ({} seen)".format(link.corrupt_frames))

finally:
    installed.restore()
    link.close()
    bd.close()


# ======================================================================
checks.section("the archive after all of that is still readable")

# The last question of a mission: can the records be read back by a
# process that was not there when they were written?

link, loopback, bd, installed = session()

try:
    from workflow.session import Mission                     # noqa: E402

    mission = Mission(link)
    mission.store = bd.sample_store()
    mission.calibrations = bd.calibration_store()
    mission.profiles = bd.profile_store()
    mission.load_science()

    link.connect_servo()
    link.sync_position(load_slot=1)

    for slot in (1, 2, 3, 4):
        sample_id = "S-ARCHIVE-{}".format(slot)

        link.select_slot(slot, sample_id=sample_id)
        mission.store.create(sample_id, slot)
        mission.store.set_state(sample_id, STATE_LOADED)

        data = link.measure_raw(slot, sample_id)

        mission.store.add_measurement(
            sample_id,
            raw=data.get("illuminations") and {
                name: block["acquisitions"][0]
                for name, block in data["illuminations"].items()
            },
            acquisition={"firmware_version": mission.firmware_version},
        )
        mission.store.set_state(sample_id, STATE_MEASURED)

    fresh = bd.sample_store()

    checks.equal(fresh.count(), 4,
                 "four Samples, written by one process and read by "
                 "another")

    for slot in (1, 2, 3, 4):
        record = fresh.get_sample("S-ARCHIVE-{}".format(slot))
        raw = (record["measurements"][0].get("raw") or {})

        checks.equal(sorted(raw), ["ir", "uv", "white"],
                     "S-ARCHIVE-{} kept its three illuminations".format(
                         slot))

finally:
    installed.restore()
    link.close()
    bd.close()


restore_serial()

sys.exit(checks.report())
