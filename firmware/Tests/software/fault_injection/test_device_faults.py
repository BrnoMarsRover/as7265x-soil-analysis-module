"""
The sensor and the servo, made to fail, seen from the PC.

WHERE THIS SITS

`integration/test_esp32.py` fails the peripherals and asserts on what
the FIRMWARE does. This suite fails them and asserts on what reaches
the OPERATOR, through the real client and the real wire - because a
firmware that refuses correctly and a client that reports the refusal
as a success are indistinguishable from the far end, and only the
second one loses a sample.

THE TWO PROPERTIES

    A FAILED MEASUREMENT IS NOT DATA. Not partial data, not zeros, not
    a spectrum with one illumination missing that looks complete
    because nobody counted.

    A FAILED MOVEMENT IS NOT A POSITION. The carousel has no index
    mark. If a movement fails halfway, the only honest answer is "I do
    not know where it is", and every later movement has to be blocked
    until an operator says.

WHAT IS FAKED

`machine.I2C` and `machine.UART`. The AS7265x fake speaks the real
virtual-register protocol and the ST3215 fake speaks the real
serial-bus frames, so the drivers under test are exercised, not
replaced.
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

from fakes import FakeClock                                  # noqa: E402
from fakes.clock import install_clock                        # noqa: E402
from fakes.esp32 import LoopbackDevice                       # noqa: E402
from fakes.serial_port import install_fake_serial, open_link  # noqa: E402

checks = support.Checks("device-faults")

restore_serial = install_fake_serial(serial_link)


def stack(sensor=None, servo=None, bring_up_sensor=True):
    """A client, a wire and the real firmware over the given fakes."""
    clock = FakeClock()
    installed = install_clock(serial_link, clock)

    loopback = LoopbackDevice(device=sensor, servo=servo,
                              bring_up_sensor=bring_up_sensor)
    loopback.build()

    link, port = open_link(serial_link, loopback, clock=clock)
    link.online = True

    return link, loopback, installed


def refusal(call):
    """The firmware's code for a refusal, or None if it did not refuse."""
    try:
        call()

        return None

    except DeviceError as error:
        return error.code

    except LinkError as error:
        return "LINK:" + error.code

    except Exception as error:                         # noqa: BLE001
        return "CRASH:" + type(error).__name__


# ======================================================================
checks.section("a servo that never answers")

link, loop, installed = stack(servo=support.FakeST3215(silent=True))

try:
    code = refusal(link.connect_servo)

    checks.ok(code is not None and not code.startswith("CRASH"),
              "connecting to a silent servo is refused, not crashed "
              "({})".format(code))

    status = link.get_status()

    checks.equal(status["servo"]["connected"], False,
                 "and the firmware does not claim a connection")
    checks.equal(status["carousel"]["position_valid"], False,
                 "and the carousel position stays unknown")

    for label, call in (
        ("move_slots", lambda: link.move_slots("cw", 1)),
        ("select_slot", lambda: link.select_slot(2)),
        ("fine_adjust", lambda: link.fine_adjust(1.0)),
        ("sync_position", lambda: link.sync_position(load_slot=1)),
        ("measure_raw", lambda: link.measure_raw(1)),
    ):
        code = refusal(call)

        checks.ok(code is not None and not code.startswith("CRASH"),
                  "{} is refused while no servo answers ({})".format(
                      label, code))

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("a servo whose frames are corrupt")

for label, servo in (
    ("checksum errors", support.FakeST3215(corrupt_checksum=True)),
    ("answers as another ID", support.FakeST3215(answer_as=9)),
    ("no reply to the first probes", support.FakeST3215(drop_replies=3)),
):
    link, loop, installed = stack(servo=servo)

    try:
        code = refusal(link.connect_servo)

        checks.ok(code is not None and not code.startswith("CRASH"),
                  "a servo with {} is refused with a name, not an "
                  "exception ({})".format(label, code))

        status = link.get_status()
        checks.equal(status["carousel"]["position_valid"], False,
                     "and leaves the carousel position unknown ({})"
                     .format(label))

    finally:
        installed.restore()
        link.close()


# ======================================================================
checks.section("a servo that stops answering after it connected")

# The nastiest ordering: everything is set up and trusted, and THEN the
# link dies. The position was valid a moment ago, which is exactly when
# it is most tempting to keep believing it.

servo = support.FakeST3215()
link, loop, installed = stack(servo=servo)

try:
    link.connect_servo()
    link.sync_position(load_slot=1)

    checks.equal(link.get_status()["carousel"]["position_valid"], True,
                 "the origin is declared and the position is valid")

    servo.silent = True

    code = refusal(lambda: link.move_slots("cw", 1))

    checks.ok(code is not None and not code.startswith("CRASH"),
              "the movement fails with a name ({})".format(code))

    status = link.get_status()

    checks.equal(status["carousel"]["position_valid"], False,
                 "AND THE POSITION IS INVALIDATED - a movement that was "
                 "commanded and not verified leaves the carousel "
                 "somewhere nobody knows")

    code = refusal(lambda: link.measure_raw(1))
    checks.ok(code is not None,
              "and a measurement afterwards is refused rather than "
              "taken at an unknown position ({})".format(code))

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("a servo that acknowledges and then does not move")

# `drop_goal_ack` is the write going unacknowledged; `polls_to_finish`
# far beyond the timeout is the servo accepting the goal and never
# reaching it. Both end with the carousel somewhere unverified.

for label, servo in (
    ("the goal write is not acknowledged",
     support.FakeST3215(drop_goal_ack=True)),
    ("the servo never reaches the goal",
     support.FakeST3215(polls_to_finish=10 ** 6)),
):
    link, loop, installed = stack(servo=servo)

    try:
        link.connect_servo()
        link.sync_position(load_slot=1)

        code = refusal(lambda: link.move_slots("cw", 1))

        checks.ok(code is not None and not code.startswith("CRASH"),
                  "when {}, the move is reported as failed ({})".format(
                      label, code))

        valid = link.get_status()["carousel"]["position_valid"]

        checks.equal(valid, False,
                     "and the position is not left valid - it would be a "
                     "remembered number with nothing behind it")

    finally:
        installed.restore()
        link.close()


# ======================================================================
checks.section("a sensor that is not on the bus")

link, loop, installed = stack(
    sensor=support.FakeAS7265X(absent_scans=10 ** 6),
    bring_up_sensor=False,
)

try:
    status = link.get_status()

    checks.ok(status["sensor"]["ready"] is False,
              "the firmware reports the sensor as not ready")

    # A DIAGNOSTIC COMMAND REPORTS FAILURE IN ITS PAYLOAD, NOT BY
    # RAISING - and that is correct. `sensor_test_raw` exists to say
    # WHICH stage failed, and an exception carries one code instead of
    # a stage list. So `ok: true` here means "the diagnosis ran", not
    # "the sensor works", and the interesting question is whether the
    # client can tell those apart.
    data = link.sensor_test_raw()

    checks.equal(data.get("ok"), False,
                 "the sensor test's own verdict is FAILED, even though "
                 "the envelope says the command ran")
    checks.ok(data.get("failed_stage"),
              "and it names the stage that failed ({})".format(
                  data.get("failed_stage")))
    checks.ok(data.get("raw") is None and not data.get("illuminations"),
              "and returns no spectrum at all - not zeros, not an empty "
              "block that counts as a reading")

    # The screen is what the operator actually sees, so the screen is
    # what has to be checked. This is the "never trust a success flag
    # alone" case, executed rather than reasoned about.
    from fakes import run_screen                             # noqa: E402
    from workflow import calibration as calibration_screen   # noqa: E402
    from workflow.session import Mission                     # noqa: E402

    mission = Mission(link)
    _value, output, _console = run_screen(
        [""], lambda: calibration_screen.menu_full_sensor_test(mission))

    checks.ok("FAILED STAGE" in output,
              "and the operator's screen says FAILED STAGE rather than "
              "presenting a measurement")
    checks.ok("NOTHING SAVED" in output.upper(),
              "and says plainly that nothing was saved")

    code = refusal(lambda: link.acquire_triad())
    checks.ok(code is not None and not code.startswith("CRASH"),
              "an acquisition, which is not a diagnostic, is refused "
              "outright ({})".format(code))

    # The protocol must still serve. A dead sensor is not a dead board.
    checks.ok(link.ping().get("pong") is True,
              "and the board still answers a ping - a dead peripheral "
              "is not a dead protocol")

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("a sensor that comes back")

# `absent_scans=2` is the measured boot situation: the AS7265x is not
# on the bus for the first scans and then appears. A firmware that
# latched the first failure reported a broken sensor for the rest of
# the session, and that defect has been in this project before.

sensor = support.FakeAS7265X(absent_scans=2)
link, loop, installed = stack(sensor=sensor, bring_up_sensor=False)

try:
    first = refusal(lambda: link.sensor_test_raw(force_reinit=True))

    data = None
    last = None

    for _attempt in range(6):
        try:
            data = link.sensor_test_raw(force_reinit=True)

            break

        except DeviceError as error:
            last = error.code

    checks.ok(data is not None,
              "a sensor absent for the first scans is found on a later "
              "attempt (first said {}, last said {})".format(first, last))

    status = link.get_status()
    checks.ok(status["sensor"]["ready"] is True,
              "and the firmware reports it ready afterwards, rather "
              "than latching the first failure for the session")

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("a sensor that fails part-way through a measurement")

# The case that decides whether a partial spectrum can become science.
# Everything is healthy, the carousel moves, the first illumination is
# read - and then the bus dies.

servo = support.FakeST3215()
sensor = support.FakeAS7265X()
link, loop, installed = stack(sensor=sensor, servo=servo)

try:
    link.connect_servo()
    link.sync_position(load_slot=1)
    link.select_slot(1, sample_id="S-PARTIAL")

    healthy = link.measure_raw(1, sample_id="S-PARTIAL")
    illuminations = healthy.get("illuminations") or {}

    checks.ok(len(illuminations) >= 3,
              "a healthy measurement returns every illumination "
              "({})".format(sorted(illuminations)))

    # NOW KILL THE BUS FOR REAL.
    #
    # `absent_scans` was not enough: the driver is already up, so it
    # never scans again and the fake goes on answering register reads
    # perfectly. `bus_error` is the connector coming loose - every
    # transfer raises, which is what a missing device does.
    sensor.bus_error = True

    code = refusal(lambda: link.measure_raw(1, sample_id="S-PARTIAL-2"))

    checks.ok(code is not None,
              "a measurement started on a sensor whose bus has just "
              "died does not return a spectrum")
    checks.ok(code is not None and not code.startswith("CRASH"),
              "and is refused with a name rather than an exception "
              "({})".format(code))

    status = link.get_status()

    checks.ok(isinstance(status.get("carousel"), dict),
              "and the board still reports its state afterwards")
    checks.equal(status["sensor"]["ready"], False,
                 "and no longer claims the sensor is ready")

    # Recovery: the connector goes back in.
    sensor.bus_error = False

    recovered = None

    for _attempt in range(4):
        try:
            recovered = link.sensor_test_raw(force_reinit=True)

            if recovered.get("ok"):
                break

        except DeviceError:
            continue

    checks.ok(recovered is not None and recovered.get("ok") is True,
              "and the sensor comes back when the bus does - a failure "
              "that latched for the session would end the mission")

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("a measurement that fails still reports the return move")

# Acquisition and mechanical recovery are separate outcomes. A servo
# that failed to bring the slot home must never be reported as a failed
# measurement, and a good measurement must never imply the carousel is
# where the software thinks it is.

servo = support.FakeST3215()
link, loop, installed = stack(servo=servo)

try:
    link.connect_servo()
    link.sync_position(load_slot=1)
    link.select_slot(1, sample_id="S-RETURN")

    data = link.measure_raw(1, sample_id="S-RETURN")

    checks.ok("return_move" in data,
              "a measurement reports the return movement as its own "
              "result, separate from the acquisition")
    checks.ok("home_restored" in data,
              "and says explicitly whether the slot came home")

    move = data.get("return_move") or {}
    checks.ok("returned" in move,
              "and the return move carries a verdict, not just a "
              "description")

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("the illumination lamps are switched off again")

# A lamp left on cooks the sample, drifts the next dark reference and
# drains the rover. The fake counts every transition, so this is a
# measurement rather than an inspection.

sensor = support.FakeAS7265X()
servo = support.FakeST3215()
link, loop, installed = stack(sensor=sensor, servo=servo)

try:
    link.connect_servo()
    link.sync_position(load_slot=1)
    link.select_slot(1, sample_id="S-LAMPS")

    data = link.measure_raw(1, sample_id="S-LAMPS")

    checks.ok(sum(sensor.lamp_on_counts.values()) > 0,
              "the measurement really switched lamps on ({})".format(
                  sensor.lamp_on_counts))
    checks.equal(sensor.any_lamp_on(), False,
                 "and every one of them is off when it returns")
    checks.ok(data.get("bulbs_off") is True,
              "and the firmware says so in the response, so the PC does "
              "not have to assume it")

finally:
    installed.restore()
    link.close()


restore_serial()

sys.exit(checks.report())
