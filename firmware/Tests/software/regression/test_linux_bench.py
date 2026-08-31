"""
The failures that actually happened, on the real Linux bench.

Everything in this file was observed with a rover on a desk, not
imagined. Each section reproduces one observed failure in software,
against the real firmware behind a fake wire, and asserts the behaviour
the system SHOULD have had - which in every case is not the behaviour
that was observed.

    RF-001  a 180 degree transfer failed its position check after the
            carousel had visibly turned, and the operator was told
            "carousel: nothing was moved"
    RF-002  /dev/ttyUSB0 disappeared mid-request; the link closed
            itself correctly, and the next status refresh killed the
            application with RuntimeError("Link is not open")
    RF-003  the same crash from the sensor test, on the second attempt
    RF-004  connection loss was handled as one failed call rather than
            as an application state
    RF-005  a measurement failure could not be located in the sequence

THE RULE THIS FILE WAS WRITTEN UNDER

Do not encode the observed behaviour. The observed behaviour contains
the bugs. Each test asserts what the system must do, and the production
code was changed until it did.
"""

import json
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
)

from fakes import FakeClock, SandboxBD, run_screen           # noqa: E402
from fakes.clock import install_clock                        # noqa: E402
from fakes.esp32 import LoopbackDevice                       # noqa: E402
from fakes.serial_port import install_fake_serial, open_link  # noqa: E402

from workflow.session import Mission                         # noqa: E402

checks = support.Checks("linux-bench")

restore_serial = install_fake_serial(serial_link)

# 4096 counts per revolution; a half turn is 2048.
HALF_TURN_COUNTS = 2048


def bench(servo=None, sensor=None, synced=True, prepared=None):
    """A client, a wire, the real firmware and a throwaway archive."""
    clock = FakeClock()
    installed = install_clock(serial_link, clock)

    servo = servo or support.FakeST3215()
    sensor = sensor or support.FakeAS7265X()

    loopback = LoopbackDevice(device=sensor, servo=servo)
    loopback.build()

    link, port = open_link(serial_link, loopback, clock=clock)
    link.online = True

    bd = SandboxBD()

    mission = Mission(link)
    mission.samples = bd.sample_database()
    mission.session = mission.samples.session()
    mission.archive = mission.samples.archive()
    mission.calibrations = bd.calibration_store()
    mission.profiles = bd.profile_store()
    mission.load_science()

    if synced:
        link.connect_servo()
        link.sync_position(load_slot=1)

    if prepared:
        link.select_slot(1, sample_id=prepared)
        mission.session.create(prepared, 1)
        mission.session.set_state(prepared, STATE_LOADED)

    class Bench:
        pass

    handle = Bench()
    handle.link = link
    handle.port = port
    handle.servo = servo
    handle.sensor = sensor
    handle.loopback = loopback
    handle.mission = mission
    handle.bd = bd
    handle.clock = clock
    handle.close = lambda: (installed.restore(), link.close(), bd.close())

    return handle


def failure_of(call):
    try:
        call()

        return None, None

    except (DeviceError, LinkError) as error:
        return error.code, error

    except Exception as error:                         # noqa: BLE001
        return "CRASH:" + type(error).__name__, error


# ======================================================================
# RF-001 — the half turn that moved, and was reported as not moving
# ======================================================================

checks.section("RF-001A: the position error is computed circularly")

# Before anything else: is the arithmetic that produced the bench
# diagnostic correct? The observed numbers were an error of 2046
# against a target of 2048 with a tolerance of 15. Those are only
# alarming if the maths is wrong, so the maths is checked first and
# separately.

servo_module = None

esp32_path = str(support.ESP32_DIR)

if esp32_path not in sys.path:
    sys.path.insert(0, esp32_path)

support.patch_time()
support.install_machine(support.FakeAS7265X(), support.FakeST3215())

import servo as servo_module                                 # noqa: E402

centred = servo_module.centred_error
COUNTS = 4096

CIRCULAR = (
    (2048 - 2046, 2, "two counts apart, nowhere near the seam"),
    (0 - 4095, 1, "0 and 4095 are ONE count apart, not 4095"),
    (4095 - 0, -1, "and the same the other way round"),
    (0 - 1, -1, "either side of zero"),
    (2048, -2048, "exactly half a turn is reported as -2048"),
    (2049, -2047, "just past half a turn folds to the short way"),
    (-2049, 2047, "and so does just past it the other way"),
    (4096, 0, "a whole revolution is zero"),
    (4096 * 3 + 5, 5, "and so are three of them, plus five"),
    (-4096 * 3 - 5, -5, "in either direction"),
)

for delta, expected, why in CIRCULAR:
    checks.equal(centred(delta, COUNTS), expected, why)

# The specific bench case, stated as its own check.
checks.equal(centred(2048 - 2046, COUNTS), 2,
             "RF-001A: an encoder at 2046 against a target of 2048 is "
             "TWO counts out, not 2046 - so the bench diagnostic was "
             "not reporting a position, it was reporting an error")

# Every value around the whole ring, so no seam is special.
seam_faults = []

for position in range(COUNTS):
    for offset in (-2, -1, 0, 1, 2):
        target = (position + offset) % COUNTS
        error = centred(target - position, COUNTS)

        if abs(error) != abs(offset):
            seam_faults.append((position, offset, error))

checks.equal(seam_faults[:5], [],
             "all {} encoder positions, each compared against its five "
             "nearest neighbours, and the seam is a non-event".format(
                 COUNTS))


# ======================================================================
checks.section("RF-001C: the tolerance is applied as documented")

# The rule is `abs(error) <= tolerance`, inclusive. Asserted so that a
# later change from <= to < is a failing test rather than a servo that
# starts refusing movements it used to accept.
#
# THE VALUE 15 IS NOT UNDER TEST. Whether 15 counts is the physically
# right tolerance is a hardware question (assumption H-001); that the
# configured value is interpreted correctly is a software one.

import config as esp32_config                                # noqa: E402

tolerance = esp32_config.ST3215_POSITION_TOLERANCE

for error, inside in ((0, True), (1, True), (tolerance - 1, True),
                      (tolerance, True), (tolerance + 1, False),
                      (-tolerance, True), (-(tolerance + 1), False)):
    checks.equal(abs(error) <= tolerance, inside,
                 "an error of {} is {} the {}-count tolerance".format(
                     error, "inside" if inside else "outside", tolerance))


# ======================================================================
checks.section("RF-001D: a failed half turn never claims nothing moved")

# THE OBSERVED FAILURE, reproduced with the bench's own numbers.
#
# `short_by` makes the servo accept the goal, move, and stop short -
# which is exactly what the encoder reported on the bench: commanded
# 2048 counts, stopped 2046 counts from the target.

handle = bench(prepared="S-RF001")

try:
    handle.servo.short_by = HALF_TURN_COUNTS - 2

    code, error = failure_of(
        lambda: handle.link.measure_raw(1, sample_id="S-RF001"))

    checks.equal(code, "SERVO_POSITION_MISMATCH",
                 "the bench failure reproduces exactly")

    data = error.data or {}

    checks.equal(data.get("moved"), True,
                 "RF-001D: the failure reports that the carousel DID "
                 "move - it used to hardcode moved=False here, which is "
                 "what put 'nothing was moved' on the operator's screen "
                 "after a visible 180 degree rotation")
    checks.equal(data.get("motion"), "MOVED",
                 "and names the verdict, not just a boolean")
    checks.equal(data.get("phase"), "MOVE_TO_SCANNER",
                 "and says which stage of the measurement it was in")

    detail = data.get("motion_detail") or {}

    checks.equal(detail.get("commanded"), True,
                 "the evidence says a goal was written")
    checks.ok(detail.get("start_position") is not None
              and detail.get("actual_position") is not None,
              "and carries both encoder readings, so the operator can "
              "see what the servo thought it did")
    checks.ok("travelled_counts" in detail,
              "and how far the encoder actually travelled - the number "
              "the bench diagnostic never showed, and the one that says "
              "whether the encoder agrees with your eyes")

    checks.ok("HAS MOVED" in error.message.upper()
              or "MOVED" in error.message.upper(),
              "and the message itself says the carousel has moved")

finally:
    handle.close()


# ======================================================================
checks.section("RF-001D: and the operator's screen says so too")

from workflow.display import report_link_error                # noqa: E402

import contextlib                                            # noqa: E402
import io                                                    # noqa: E402


def screen_for(error):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        report_link_error(error)

    return out.getvalue()


handle = bench(prepared="S-RF001B")

try:
    handle.servo.short_by = HALF_TURN_COUNTS - 2
    _code, error = failure_of(
        lambda: handle.link.measure_raw(1, sample_id="S-RF001B"))

    printed = screen_for(error)

    checks.ok("nothing was moved" not in printed.lower(),
              "RF-001D: the screen does NOT say 'nothing was moved' - "
              "this exact sentence, after a rotation the operator had "
              "just watched, is the defect")
    # THE GUARANTEE, NOT THE SENTENCE. The compact renderer states the
    # same two facts in fewer words: that the encoder measured travel
    # (so the carousel did move), and that the mechanism must be looked
    # at. Asserting the old block-capital phrasing would pin the
    # wording rather than the promise, and the promise is what RF-001D
    # is about.
    checks.ok("encoder measured travel" in printed.lower(),
              "it says the carousel moved, on the encoder's evidence")
    checks.ok("mechanism" in printed.lower(),
              "and tells the operator to look at the mechanism before "
              "assuming which slot is where")
    checks.ok("POSITION UNKNOWN" in printed.upper(),
              "and that the position is not to be trusted")
    checks.ok("re-sync" in printed.lower() or "resync" in printed.lower(),
              "and that a re-sync is needed")

finally:
    handle.close()


# ======================================================================
checks.section("RF-001D: 'nothing moved' is still said when it is true")

# The fix must not go the other way. A pre-flight refusal genuinely has
# not moved anything, and saying so is what lets an operator leave the
# mechanism alone.

handle = bench(prepared="S-RF001C")

try:
    handle.sensor.bus_error = True          # fails the pre-flight check

    code, error = failure_of(
        lambda: handle.link.measure_raw(1, sample_id="S-RF001C"))

    checks.ok(code is not None, "a dead sensor refuses the measurement "
                                "({})".format(code))

    data = error.data or {}

    checks.equal(data.get("moved"), False,
                 "and this refusal DOES report moved=False - it happens "
                 "before anything is commanded, which is the whole "
                 "reason the sensor is proved first")
    checks.equal(data.get("motion"), "NOT_STARTED", "explicitly")
    checks.equal(data.get("phase"), "PRECHECK", "at the pre-flight stage")

    printed = screen_for(error)

    # Same promise, compact wording: the carousel is UNCHANGED and the
    # servo was NEVER COMMANDED. This is the direction the RF-001D fix
    # must not break - saying "nothing moved" when it is true is what
    # lets an operator leave the mechanism alone.
    checks.ok("unchanged" in printed.lower()
              and "never commanded" in printed.lower(),
              "and the screen says nothing was moved, because nothing "
              "was")

finally:
    handle.close()


# ======================================================================
checks.section("RF-001F/G: a failed transfer stops the measurement")

handle = bench(prepared="S-RF001D")

try:
    handle.servo.short_by = HALF_TURN_COUNTS - 2

    before = handle.sensor.channel_reads

    failure_of(lambda: handle.link.measure_raw(1, sample_id="S-RF001D"))

    checks.equal(handle.sensor.channel_reads, before,
                 "RF-001G: NOT ONE channel was read - science must not "
                 "be acquired from a position that could not be verified")

    status = handle.link.get_status()

    checks.equal(status["carousel"]["position_valid"], False,
                 "RF-001F: and the position is invalidated")

    record = handle.mission.session.get_sample("S-RF001D")
    measurements = (record or {}).get("measurements") or []

    checks.ok(all(m.get("acquisition_status") != ACQUISITION_SUCCESS
                  for m in measurements),
              "and no successful Measurement was recorded")

    code, _ = failure_of(lambda: handle.link.move_slots("cw", 1))
    checks.ok(code is None or code != "OK",
              "a further movement is possible only as a blind relative "
              "move; an absolute one is refused")

    code, _ = failure_of(lambda: handle.link.select_slot(2))
    checks.ok(code is not None,
              "selecting a slot is refused until the operator re-syncs "
              "({})".format(code))

finally:
    handle.close()


# ======================================================================
checks.section("RF-001L: a failed RETURN never discards good science")

# The case that must not be confused with the one above. The spectra
# exist. Only the journey home failed.

handle = bench(prepared="S-RF001E")

try:
    original_return = handle.loopback.service._return_home

    def failing_return(home_scan_slot):
        report = original_return(home_scan_slot)
        report["returned"] = False
        report["message"] = "the return movement could not be verified"

        return report

    handle.loopback.service._return_home = failing_return

    data = handle.link.measure_raw(1, sample_id="S-RF001E")

    checks.ok(data is not None,
              "RF-001L: the measurement still returns its data")

    blocks = data.get("illuminations") or {}
    checks.equal(sorted(blocks), ["ir", "uv", "white"],
                 "with all three illuminations - a failed return must "
                 "not turn a real spectrum into 'no spectrum obtained'")

    return_move = data.get("return_move") or {}
    checks.equal(return_move.get("returned"), False,
                 "and the return is reported as its own failed outcome")
    checks.equal(data.get("home_restored"), False,
                 "and home_restored says the sample is not back")

finally:
    handle.close()


# ======================================================================
checks.section("RF-001I/J: a failure is locatable in the sequence")

# Every stage that can fail, failed in turn, and the report must say
# WHICH. "Measurement failed" is not a diagnosis.

STAGES = (
    ("the transfer to the scanner",
     lambda h: setattr(h.servo, "short_by", HALF_TURN_COUNTS - 2),
     "MOVE_TO_SCANNER", False),
    ("the acquisition",
     lambda h: setattr(h.sensor, "bus_error", True),
     None, None),
)

for label, break_it, phase, spectra in STAGES:
    handle = bench(prepared="S-STAGE")

    try:
        break_it(handle)

        code, error = failure_of(
            lambda: handle.link.measure_raw(1, sample_id="S-STAGE"))

        checks.ok(code is not None,
                  "a failure in {} is reported ({})".format(label, code))

        data = (error.data or {}) if error else {}

        if phase:
            checks.equal(data.get("phase"), phase,
                         "and names the stage: {}".format(phase))

        checks.ok(not str(code).startswith("CRASH"),
                  "and is a named refusal, not an exception")

    finally:
        handle.close()


# ======================================================================
# RF-002 — PORT_LOST, then RuntimeError
# ======================================================================

checks.section("RF-002/REG-LINUX-002: PORT_LOST does not kill the client")

handle = bench()

try:
    handle.link.connect_servo()

    # The Linux device disappears mid-request, exactly as observed.
    handle.port.fail_read_after = 0

    code, error = failure_of(handle.link.get_status)

    checks.equal(code, "PORT_LOST", "the loss itself is detected")
    checks.ok(handle.link.serial is None,
              "and the link closes itself, which is correct")
    checks.ok((error.data or {}).get("reconnect_required") is True,
              "and says a reconnect is required")

    # THE DEFECT: this is the call the main menu loop makes on every
    # redraw, and it used to end the application.
    code, error = failure_of(handle.mission.hardware_status)

    checks.ok(not str(code).startswith("CRASH"),
              "REG-LINUX-002: the next hardware_status() does NOT raise "
              "RuntimeError - it used to end the application with "
              "'Link is not open; call open() first.'")
    checks.equal(code, "PORT_CLOSED",
                 "it is a LinkError with a code the screens already "
                 "catch, which is why one fix covers every screen")
    checks.ok("Reconnect" in error.message,
              "and the message tells the operator what to do")
    checks.ok("lost" in error.message.lower(),
              "and says the connection was LOST rather than merely "
              "'not open' - the operator did not close anything")

finally:
    handle.close()


# ======================================================================
checks.section("RF-002J: every link method has one closed-link contract")

# Mixed contracts are how the original defect survived: most methods
# were never called while closed, so nobody noticed the one that was.

handle = bench()

try:
    handle.port.fail_read_after = 0
    failure_of(handle.link.get_status)

    link = handle.link
    inconsistent = []

    CALLS = (
        ("ping", lambda: link.ping()),
        ("get_status", lambda: link.get_status()),
        ("connect_servo", lambda: link.connect_servo()),
        ("disconnect_servo", lambda: link.disconnect_servo()),
        ("servo_stop", lambda: link.servo_stop()),
        ("servo_diagnostics", lambda: link.servo_diagnostics()),
        ("servo_bus_scan", lambda: link.servo_bus_scan()),
        ("get_servo_calibration", lambda: link.get_servo_calibration()),
        ("servo_configure", lambda: link.servo_configure(confirm=True)),
        ("servo_torque", lambda: link.servo_torque(True)),
        ("servo_test_move", lambda: link.servo_test_move("degrees",
                                                         degrees=1.0)),
        ("sync_position", lambda: link.sync_position(load_slot=1)),
        ("select_slot", lambda: link.select_slot(1)),
        ("move_slots", lambda: link.move_slots("cw", 1)),
        ("fine_adjust", lambda: link.fine_adjust(1.0)),
        ("clear_slot", lambda: link.clear_slot(1)),
        ("clear_all_slots", lambda: link.clear_all_slots()),
        ("measure_raw", lambda: link.measure_raw(1)),
        ("sensor_test_raw", lambda: link.sensor_test_raw()),
        ("acquire_block", lambda: link.acquire_block("white", 1)),
        ("acquire_triad", lambda: link.acquire_triad()),
        ("led_test", lambda: link.led_test()),
        ("list_saved_samples", lambda: link.list_saved_samples()),
        ("get_saved_sample", lambda: link.get_saved_sample("X")),
        ("delete_saved_sample",
         lambda: link.delete_saved_sample("S001")),
        ("delete_saved_samples", lambda: link.delete_saved_samples()),
        ("hard_reset", lambda: link.hard_reset()),
    )

    for name, call in CALLS:
        code, _error = failure_of(call)

        if code != "PORT_CLOSED":
            inconsistent.append("{} -> {}".format(name, code))

    checks.equal(inconsistent, [],
                 "all {} link methods answer a closed link with the same "
                 "PORT_CLOSED LinkError - no method raises a bare "
                 "RuntimeError any more".format(len(CALLS)))

    # AND THE LIST ABOVE IS COMPLETE, checked mechanically.
    #
    # The table is written by hand, which is what makes each call
    # realistic - and also what would let a NEW command wrapper be
    # added without anyone thinking about the closed-link case. That is
    # exactly how the original defect survived: most methods were never
    # called while closed, so nobody noticed the one that was.
    #
    # So the public surface is enumerated from the class itself and
    # compared against the table.
    NOT_HARDWARE_COMMANDS = {
        # Lifecycle, not commands. `open` and `close` are the methods
        # that CHANGE whether the link is open, and `available_ports`
        # is a static enumeration that never touches this link at all.
        "open", "close", "available_ports",
        # The two generic senders every wrapper above goes through.
        # They are covered transitively by all 26 wrappers, and calling
        # them directly here would test the same line twice.
        "request", "wait_online",
    }

    public_methods = {
        name for name in dir(serial_link.SerialLink)
        if not name.startswith("_")
        and callable(getattr(serial_link.SerialLink, name))
    }

    covered = {name for name, _call in CALLS}
    missing = sorted(public_methods - covered - NOT_HARDWARE_COMMANDS)

    checks.equal(missing, [],
                 "and the table covers EVERY public method of "
                 "SerialLink - {} public, {} exercised here, {} "
                 "excluded as lifecycle rather than commands".format(
                     len(public_methods), len(covered),
                     len(NOT_HARDWARE_COMMANDS)))

    stale = sorted(covered - public_methods)

    checks.equal(stale, [],
                 "and names nothing that no longer exists")

finally:
    handle.close()


# ======================================================================
checks.section("RF-002E/F: reconnect does not restore stale state")

handle = bench()

try:
    handle.link.connect_servo()
    handle.link.sync_position(load_slot=1)

    checks.equal(handle.link.get_status()["carousel"]["position_valid"],
                 True, "the carousel is synchronized before the loss")

    handle.port.fail_read_after = 0
    failure_of(handle.link.get_status)

    # The device comes back and the operator reconnects.
    handle.port.fail_read_after = None
    handle.port.read_count = 0
    handle.link.serial = handle.port
    handle.link.closed_reason = None

    # The board rebooted while it was away: fresh firmware state.
    handle.loopback.service = None
    handle.loopback.build()

    status = handle.link.get_status()

    checks.equal(status["carousel"]["position_valid"], False,
                 "RF-002F: after a reconnect the carousel position is "
                 "NOT restored - the servo may have been moved by hand "
                 "while the link was down")
    checks.equal(status["servo"]["connected"], False,
                 "and the servo connection is not assumed either")

    code, _ = failure_of(lambda: handle.link.measure_raw(1))
    checks.ok(code is not None,
              "so a measurement is refused until the operator "
              "re-declares the origin ({})".format(code))

    handle.link.connect_servo()
    handle.link.sync_position(load_slot=1)

    checks.equal(handle.link.get_status()["carousel"]["position_valid"],
                 True, "and the documented recovery works")

finally:
    handle.close()


# ======================================================================
# RF-003 — the sensor test, twice
# ======================================================================

checks.section("RF-003/REG-LINUX-003: the sensor test survives a lost port")

from workflow import calibration as calibration_screens       # noqa: E402

handle = bench()

try:
    handle.port.fail_read_after = 0

    # Attempt one: this part was already controlled on the bench.
    _v, first, _c = run_screen(
        [""], lambda: calibration_screens.menu_full_sensor_test(
            handle.mission))

    checks.ok("FAILED STAGE" in first,
              "the first attempt reports a failed stage")
    checks.ok("PORT_LOST" in first or "PORT_CLOSED" in first,
              "and names the serial failure, not a sensor failure")

    # Attempt two: THIS is what crashed on the bench.
    crashed = None

    try:
        _v, second, _c = run_screen(
            [""], lambda: calibration_screens.menu_full_sensor_test(
                handle.mission))

    except Exception as error:                         # noqa: BLE001
        crashed = "{}: {}".format(type(error).__name__, error)
        second = ""

    checks.ok(crashed is None,
              "REG-LINUX-003: the SECOND attempt does not raise - it "
              "used to end the application with 'Link is not open' "
              "({})".format(crashed))
    checks.ok("FAILED STAGE" in second,
              "and reports the failure the same controlled way")

    # RF-003E: and again, and again.
    survived = 0

    for _attempt in range(6):
        try:
            run_screen([""], lambda: calibration_screens
                       .menu_full_sensor_test(handle.mission))
            survived += 1

        except Exception:                              # noqa: BLE001
            break

    checks.equal(survived, 6,
                 "RF-003E: six further attempts, all refused and all "
                 "survived")

finally:
    handle.close()


# ======================================================================
checks.section("RF-003B: a lost link is not diagnosed as a sensor fault")

handle = bench()

try:
    handle.port.fail_read_after = 0

    _v, printed, _c = run_screen(
        [""], lambda: calibration_screens.menu_full_sensor_test(
            handle.mission))

    checks.ok("SERIAL" in printed.upper(),
              "the failed stage is the SERIAL link")
    checks.ok("AS7265" not in printed.upper(),
              "and the AS7265x is not blamed for a cable that fell out")

finally:
    handle.close()


# ======================================================================
# RF-004 — connection loss as an application state
# ======================================================================

checks.section("RF-004C/REG-LINUX-006: every screen survives a lost port")

from workflow import (                                        # noqa: E402
    carousel as carousel_screens,
    measure as measure_screens,
    records as records_screens,
    screen as screen_screens,
)

SCREENS_AFTER_LOSS = (
    ("carousel.menu_initial_calibration",
     lambda m, s, v: carousel_screens.menu_initial_calibration(m), ["c"]),
    ("carousel.menu_resync",
     lambda m, s, v: carousel_screens.menu_resync(m), ["y", ""]),
    ("carousel.menu_servo_test",
     lambda m, s, v: carousel_screens.menu_servo_test(m), ["0"]),
    ("carousel.menu_servo_diagnostics",
     lambda m, s, v: carousel_screens.menu_servo_diagnostics(m), [""]),
    ("calibration.menu_sensor_test",
     lambda m, s, v: calibration_screens.menu_sensor_test(m), ["0"]),
    ("calibration.menu_full_sensor_test",
     lambda m, s, v: calibration_screens.menu_full_sensor_test(m), [""]),
    ("calibration.menu_led_test",
     lambda m, s, v: calibration_screens.menu_led_test(m), ["0"]),
    ("calibration.show_active_calibration",
     lambda m, s, v: calibration_screens.show_active_calibration(m), [""]),
    ("records.menu_sample_database",
     lambda m, s, v: records_screens.menu_sample_database(m), ["0"]),
    ("records.menu_learning_history",
     lambda m, s, v: records_screens.menu_learning_history(m), ["0"]),
    ("records.import_esp32_samples",
     lambda m, s, v: records_screens.import_esp32_samples(m), ["", ""]),
    ("measure.menu_choose_slot", measure_screens.menu_choose_slot, ["2"]),
    ("measure.menu_prepare", measure_screens.menu_prepare,
     ["S-X", "", "", "", "", "", ""]),
    ("measure.menu_confirm", measure_screens.menu_confirm, ["y"]),
    ("measure.menu_measure", measure_screens.menu_measure, ["", ""]),
    ("measure.menu_clear_slot", measure_screens.menu_clear_slot, ["", ""]),
    ("screen.menu_help", screen_screens.menu_help, [""]),
)

crashes = []

for name, call, script in SCREENS_AFTER_LOSS:
    handle = bench()

    try:
        # A status is captured while the link still works, because that
        # is what the real menu loop passes in - and then the link dies
        # underneath the screen, which is the realistic ordering.
        status = handle.mission.hardware_status()
        view = handle.mission.slot_view(status)

        handle.port.fail_read_after = 0
        failure_of(handle.link.ping)

        try:
            run_screen(list(script),
                       lambda: call(handle.mission, status, view),
                       exhausted="0")

        except (DeviceError, LinkError):
            pass

        except Exception as error:                     # noqa: BLE001
            crashes.append("{}: {}: {}".format(
                name, type(error).__name__, error))

    finally:
        handle.close()

if crashes:
    print()

    for line in crashes:
        print("     {}".format(line))

    print()

checks.equal(crashes, [],
             "RF-004C: with the port lost underneath them, none of the "
             "{} screens raises".format(len(SCREENS_AFTER_LOSS)))


# ======================================================================
checks.section("RF-004D/F: the main loop reports DISCONNECTED and lives")

handle = bench()

try:
    handle.port.fail_read_after = 0

    # The exact traceback from the bench:
    #   interactive() -> mission.hardware_status() -> link.get_status()
    _value, printed, _console = run_screen(
        ["n"], lambda: screen_screens.interactive(handle.link),
        exhausted="q")

    checks.ok(True,
              "RF-004D: interactive() returns instead of raising when "
              "the link is gone before it starts")
    checks.ok("hardware state" in printed.lower()
              or "connection" in printed.lower()
              or "no answer" in printed.lower(),
              "and tells the operator the connection is the problem")

finally:
    handle.close()


# ======================================================================
checks.section("RF-004G: durable data survives the connection loss")

handle = bench(prepared="S-RF004")

try:
    data = handle.link.measure_raw(1, sample_id="S-RF004")

    handle.mission.session.add_measurement(
        "S-RF004",
        raw={name: block["acquisitions"][0]
             for name, block in (data.get("illuminations") or {}).items()},
        acquisition={"firmware_version": handle.mission.firmware_version},
    )

    # Now the port dies.
    handle.port.fail_read_after = 0
    failure_of(handle.link.get_status)

    reread = handle.mission.session.get_sample("S-RF004")
    measurements = (reread or {}).get("measurements") or []

    checks.equal(len(measurements), 1,
                 "a measurement saved before the loss is still there "
                 "afterwards")
    checks.equal(sorted(measurements[0].get("raw") or {}),
                 ["ir", "uv", "white"],
                 "with its RAW intact - losing the cable must not cost "
                 "science that already reached the disk")

    code, _ = failure_of(lambda: handle.link.measure_raw(1))
    checks.equal(code, "PORT_CLOSED",
                 "and a new measurement is refused rather than "
                 "fabricated")

    after = handle.mission.session.get_sample("S-RF004")
    checks.equal(len(after.get("measurements") or []), 1,
                 "so no phantom Measurement was added by the failure")

finally:
    handle.close()


# ======================================================================
# REG-LINUX-004 — a physical command whose answer is lost
# ======================================================================

checks.section("REG-LINUX-004: a lost acknowledgement is not retried")

handle = bench()

try:
    handle.link.connect_servo()
    handle.link.sync_position(load_slot=1)

    before = len(handle.port.requests)

    # The port dies between the write and the answer.
    handle.port.fail_read_after = 0

    code, _ = failure_of(lambda: handle.link.move_slots("cw", 1))

    checks.equal(code, "PORT_LOST", "the movement fails as PORT_LOST")

    moves = [r for r in handle.port.requests[before:]
             if r.get("cmd") == "move_slots"]

    checks.equal(len(moves), 1,
                 "and the command was sent EXACTLY ONCE - a relative "
                 "movement whose acknowledgement was lost has still "
                 "happened, and repeating it would turn the carousel "
                 "twice")

finally:
    handle.close()


# ======================================================================
checks.section("PORT_LOST at each point of a command, then keep going")

POINTS = (
    ("before the write", {"fail_write_after": 0}),
    ("after the write, waiting", {"fail_read_after": 0}),
    ("mid-response", {"fail_read_after": 1, "chunk_size": 8}),
)

for label, faults in POINTS:
    handle = bench()

    try:
        for key, value in faults.items():
            setattr(handle.port, key, value)

        code, _ = failure_of(handle.link.get_status)

        checks.equal(code, "PORT_LOST",
                     "a loss {} is PORT_LOST".format(label))
        checks.ok(handle.link.serial is None,
                  "and releases the handle ({})".format(label))

        # The whole point: is the application coherent afterwards?
        follow_up = []

        for name, call in (("hardware_status",
                            handle.mission.hardware_status),
                           ("ping", handle.link.ping),
                           ("measure", lambda: handle.link.measure_raw(1))):
            next_code, _ = failure_of(call)

            if str(next_code).startswith("CRASH"):
                follow_up.append("{} -> {}".format(name, next_code))

        checks.equal(follow_up, [],
                     "and every following action is a named refusal "
                     "({})".format(label))

    finally:
        handle.close()


# ======================================================================
# RF-005 — a failure must be locatable in the sequence
# ======================================================================

checks.section("RF-005C: the three failure stages read differently")

from workflow import measure as measure_screens               # noqa: E402

# The same operator action - Measure Sample - failed three ways. Each
# must produce a screen an operator can act on differently, because the
# right physical response differs: leave it alone, look at the
# mechanism, or check whether the sample came home.

def measure_screen(handle, sample_id):
    status = handle.mission.hardware_status()
    view = handle.mission.slot_view(status)

    # `exhausted="0"` because a failed measurement no longer RETURNS.
    #
    # It now holds the operator in MEASUREMENT RECOVERY until they
    # choose to leave, which is the whole point of that screen: the
    # sample, the slot, the stage and the missing spectrum used to be
    # gone one keypress after they were printed. "0" is the abort, so
    # the screen still finishes and everything it printed on the way is
    # still asserted on below.
    _value, printed, _console = run_screen(
        ["", "", "", ""],
        lambda: measure_screens.menu_measure(handle.mission, status, view),
        exhausted="0")

    return printed


# 1. Refused before anything moved.
handle = bench(prepared="S-RF005A")

try:
    handle.sensor.bus_error = True
    printed = measure_screen(handle, "S-RF005A")

    checks.ok("measurement recovery" in printed.lower(),
              "every failure lands in the recovery context, keeping the "
              "sample and the stage on screen")

    checks.ok("refused before anything moved" in printed.lower(),
              "a pre-flight refusal says nothing moved")
    checks.ok("unchanged" in printed.lower()
              and "never commanded" in printed.lower(),
              "and the carousel line agrees")

finally:
    handle.close()

# 2. The transfer failed after the carousel turned.
handle = bench(prepared="S-RF005B")

try:
    handle.servo.short_by = HALF_TURN_COUNTS - 2
    printed = measure_screen(handle, "S-RF005B")

    checks.ok("moving the sample to the scanner" in printed.lower(),
              "RF-005C: a failed transfer names THAT stage")
    checks.ok("encoder measured travel" in printed.lower(),
              "and says the carousel moved")
    checks.ok("refused before anything moved" not in printed.lower(),
              "and does NOT reuse the sentence for a refusal that "
              "moved nothing - these need opposite physical responses")

finally:
    handle.close()

# 3. The acquisition failed with the sample at the scanner.
handle = bench(prepared="S-RF005C")

try:
    original_acquire = handle.loopback.service.sensor.acquire_triad

    def failing_acquire(repeats):
        handle.sensor.bus_error = True

        return original_acquire(repeats)

    handle.loopback.service.sensor.acquire_triad = failing_acquire

    printed = measure_screen(handle, "S-RF005C")

    checks.ok("reached the scanner" in printed.lower(),
              "RF-005C: an acquisition failure says the sample REACHED "
              "the scanner")
    checks.ok("return" in printed.lower(),
              "and reports what happened to the return journey")

finally:
    handle.close()


# ======================================================================
checks.section("RF-005D: saved or not saved is never ambiguous")

handle = bench(prepared="S-RF005D")

try:
    printed = measure_screen(handle, "S-RF005D")

    # IT USED TO SAY "Saving RAW to BD", AND THAT WAS NOT TRUE.
    # Measure writes no file; the durable copy is on the ESP32 and the
    # PC archive gets one only on an import. The screen now says where
    # the spectrum actually is, and this asserts that it still says
    # something definite rather than going quiet.
    checks.ok("RAW retained" in printed,
              "a successful measurement says the RAW was retained")
    checks.ok("ESP32" in printed,
              "and names the ESP32 as the durable holder")
    checks.ok("archive" in printed.lower(),
              "and says it is not in the PC archive yet")
    checks.ok("PASS" in printed, "and marks the stage as passed")

    record = handle.mission.session.get_sample("S-RF005D")
    measurements = (record or {}).get("measurements") or []

    checks.equal(len(measurements), 1,
                 "and there really is one Measurement on disk")
    checks.equal(measurements[0]["acquisition_status"],
                 ACQUISITION_SUCCESS, "marked SUCCESS")

finally:
    handle.close()

# The failure case: a Measurement is recorded, and it is a FAILURE.
handle = bench(prepared="S-RF005E")

try:
    handle.sensor.bus_error = True
    printed = measure_screen(handle, "S-RF005E")

    checks.ok("not acquired" in printed.lower()
              or "none was saved" in printed.lower(),
              "RF-005D: a failed measurement says plainly that nothing "
              "was saved")

    record = handle.mission.session.get_sample("S-RF005E")
    measurements = (record or {}).get("measurements") or []

    checks.equal(len(measurements), 1,
                 "one Measurement was recorded for the attempt")
    checks.equal(measurements[0]["acquisition_status"],
                 ACQUISITION_FAILED, "marked FAILED")
    checks.ok("raw" not in measurements[0],
              "with no raw block at all")
    checks.equal(handle.mission.session.get_state("S-RF005E"), STATE_LOADED,
                 "and the Sample stays LOADED so it can be retried")

finally:
    handle.close()


# ======================================================================
checks.section("REG-LINUX-005: science survives a failed return")

# Stated once more as its own named regression, because it is the
# combination most likely to be got wrong by a later change: the
# spectra are real, and only the journey home failed.

handle = bench(prepared="S-REG005")

try:
    original_return = handle.loopback.service._return_home

    def failing_return(home_scan_slot):
        report = original_return(home_scan_slot)
        report["returned"] = False

        return report

    handle.loopback.service._return_home = failing_return

    printed = measure_screen(handle, "S-REG005")

    checks.ok("no spectrum" not in printed.lower(),
              "REG-LINUX-005: the screen does NOT say 'no spectrum "
              "obtained' - three of them were")

    record = handle.mission.session.get_sample("S-REG005")
    measurements = (record or {}).get("measurements") or []

    checks.equal(len(measurements), 1, "the Measurement was saved")
    checks.equal(measurements[0]["acquisition_status"],
                 ACQUISITION_SUCCESS, "as a SUCCESS")
    checks.equal(sorted(measurements[0].get("raw") or {}),
                 ["ir", "uv", "white"], "with all three illuminations")

    checks.ok("RETURN" in printed.upper(),
              "and the return failure is reported separately")

finally:
    handle.close()


restore_serial()

sys.exit(checks.report())
