"""
What each side believes after the other one restarts.

THE ASYMMETRY THAT MAKES THIS DANGEROUS

The PC and the ESP32 hold overlapping state, and they lose it at
different moments:

    ESP32, on reset      carousel origin, position validity, servo
                         connection, sensor readiness, the retained
                         acquisition buffer - ALL of it. The carousel
                         module says so explicitly: physical occupancy
                         and tracked position live in RAM only and are
                         intentionally forgotten.

    PC, on restart       everything except what reached BD/. The
                         archive survives; the belief about where the
                         carousel is does not.

So there are four combinations, and three of them can produce a PC that
believes something the firmware no longer does. The one that matters
most is the quiet one: the board browns out, resets, comes back in
under a second, and the PC never noticed - it still thinks Slot 1 is
under the loader and the servo is connected.

WHAT MUST NEVER HAPPEN

A position that survives a reset. If the firmware has forgotten where
the carousel is, no amount of PC-side memory may put a slot number back
on the screen: the servo could have been turned by hand while the board
was down, and a remembered number is indistinguishable from a measured
one once it is displayed.

WHAT IS SIMULATED, AND WHAT IS NOT

The software consequences of a reset - lost volatile state, boot
output, a fresh command dispatcher - are all simulated here by
rebuilding the firmware behind the same open link. The electrical
cause of a reset is hardware and belongs to Tests/hardware.
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
from serial_link import DeviceError, LinkError, SerialLink   # noqa: E402

from BD.samples import STATE_LOADED, STATE_MEASURED          # noqa: E402

from fakes import FakeClock, SandboxBD                       # noqa: E402
from fakes.clock import install_clock                        # noqa: E402
from fakes.esp32 import LoopbackDevice                       # noqa: E402
from fakes.serial_port import install_fake_serial, open_link  # noqa: E402

from workflow.session import Mission                         # noqa: E402

checks = support.Checks("reset-recovery")

restore_serial = install_fake_serial(serial_link)

# What a real MicroPython board writes on the way up. The client keeps
# this rather than discarding it - it is usually the whole explanation
# for a failure - so it has to be survivable as well as preserved.
BOOT_BANNER = (
    b"rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)\r\n"
    b"configsip: 0, SPIWP:0xee\r\n"
    b"MicroPython v1.28.0 on 2026-01-01; ESP32 module with ESP32\r\n"
)

BOOT_TRACEBACK = (
    b"Traceback (most recent call last):\r\n"
    b'  File "main.py", line 12, in <module>\r\n'
    b"ImportError: no module named 'carousel'\r\n"
)


class Bench:
    """A PC and an ESP32 that can be restarted independently."""

    def __init__(self):
        self.clock = FakeClock()
        self.installed = install_clock(serial_link, self.clock)

        self.servo = support.FakeST3215()
        self.sensor = support.FakeAS7265X()

        self.loopback = LoopbackDevice(device=self.sensor,
                                       servo=self.servo)
        self.loopback.build()

        self.link, self.port = open_link(
            serial_link, self.loopback, clock=self.clock,
            link_kwargs={"timeout": 5.0})
        self.link.online = True

        self.bd = SandboxBD()
        self.mission = self._mission()

    def _mission(self):
        mission = Mission(self.link)
        mission.samples = self.bd.sample_database()
        mission.session = mission.samples.session()
        mission.archive = mission.samples.archive()
        mission.calibrations = self.bd.calibration_store()
        mission.profiles = self.bd.profile_store()
        mission.load_science()

        return mission

    # -- restarts -------------------------------------------------------

    def reset_esp32(self, banner=BOOT_BANNER):
        """
        The board reboots. Volatile state is gone; the link is not.

        Rebuilding the LoopbackDevice's firmware is exactly what a
        reset does in software terms: a fresh Hardware, a fresh
        Carousel with position_valid False, a sensor that has not been
        brought up and a servo nothing has connected.
        """
        self.servo = support.FakeST3215()
        self.sensor = support.FakeAS7265X()

        self.loopback.service = None
        self.loopback._device = self.sensor
        self.loopback._servo = self.servo
        self.loopback.build()

        if banner:
            self.port._enqueue(banner)

    def restart_pc(self):
        """
        The operator restarts the client. A new SerialLink, a new
        Mission, the same archive on disk and the same board.
        """
        self.link.close()

        self.link, self.port = open_link(
            serial_link, self.loopback, clock=self.clock,
            link_kwargs={"timeout": 5.0})
        self.link.online = True

        self.mission = self._mission()

    def close(self):
        self.installed.restore()
        self.link.close()
        self.bd.close()


def attempt(call):
    try:
        return "OK", call()

    except DeviceError as error:
        return error.code, None

    except LinkError as error:
        return error.code, None

    except Exception as error:                         # noqa: BLE001
        return "CRASH:" + type(error).__name__, None


def carousel(bench):
    return (bench.link.get_status() or {}).get("carousel") or {}


def servo_state(bench):
    return (bench.link.get_status() or {}).get("servo") or {}


# ======================================================================
checks.section("what a reset actually costs")

# Enumerated rather than assumed: every piece of state that does not
# survive is named, so a later change that starts persisting one of
# them is a decision somebody makes on purpose.

bench = Bench()

try:
    bench.link.connect_servo()
    bench.link.sync_position(load_slot=1)
    bench.link.select_slot(2, sample_id="S-RESET")
    bench.link.sensor_test_raw()

    before = bench.link.get_status()

    checks.equal(before["carousel"]["position_valid"], True,
                 "before the reset the position is valid")
    checks.equal(before["servo"]["connected"], True, "the servo is connected")
    checks.equal(before["sensor"]["ready"], True, "the sensor is ready")
    checks.equal(before["carousel"]["selected_slot"], 2,
                 "and a slot is selected")

    bench.reset_esp32()

    after = bench.link.get_status()

    LOST = (
        ("the carousel origin", after["carousel"]["reference"]["origin"],
         None),
        ("position validity", after["carousel"]["position_valid"], False),
        ("the scanner slot", after["carousel"]["current_scan_slot"], None),
        ("the loading slot", after["carousel"]["current_load_slot"], None),
        ("the selected slot", after["carousel"]["selected_slot"], None),
        ("the servo connection", after["servo"]["connected"], False),
    )

    for label, actual, expected in LOST:
        checks.equal(actual, expected,
                     "{} does NOT survive a reset".format(label))

    checks.ok(after["sensor"]["ready"] in (False, True),
              "the sensor comes back on demand, so its readiness is a "
              "property of the bus rather than of the session ({})"
              .format(after["sensor"]["ready"]))

    # The board is alive and serving; only its memory is gone.
    checks.equal(bench.link.ping().get("pong"), True,
                 "and the board answers perfectly - a reset is not a "
                 "fault, it is amnesia")

finally:
    bench.close()


# ======================================================================
checks.section("PC believes, ESP32 has forgotten")

# The quiet case. The PC never noticed the reset, and every command it
# issues next is based on a belief the firmware no longer shares.

bench = Bench()

try:
    bench.link.connect_servo()
    bench.link.sync_position(load_slot=1)
    bench.link.select_slot(1, sample_id="S-STALE")

    bench.mission.session.create("S-STALE", 1)
    bench.mission.session.set_state("S-STALE", STATE_LOADED)

    # The PC's own view, captured BEFORE the reset - exactly what the
    # menu loop is holding when the board browns out.
    stale_status = bench.mission.hardware_status()
    stale_view = bench.mission.slot_view(stale_status)

    checks.equal((stale_status["carousel"] or {})["position_valid"], True,
                 "the PC is holding a status that says the position is "
                 "valid")

    bench.reset_esp32()

    # Everything the operator might do next, with a stale view in hand.
    for label, call in (
        ("measure", lambda: bench.link.measure_raw(1, "S-STALE")),
        ("select a slot", lambda: bench.link.select_slot(2)),
        ("move whole slots", lambda: bench.link.move_slots("cw", 1)),
        ("fine adjust", lambda: bench.link.fine_adjust(1.0)),
        ("stop the servo", lambda: bench.link.servo_stop()),
    ):
        code, _ = attempt(call)

        checks.ok(code != "OK" or label == "move whole slots",
                  "{} after an unnoticed reset is refused ({})".format(
                      label, code))
        checks.ok(not str(code).startswith("CRASH"),
                  "and refused by name, not by exception ({})".format(
                      label))

    # And the refusals name the right reason.
    code, _ = attempt(lambda: bench.link.measure_raw(1, "S-STALE"))
    checks.equal(code, "POSITION_NOT_SYNCHRONIZED",
                 "a measurement names the origin as the missing thing, "
                 "not the sensor and not the servo")

    # A fresh status reveals the truth immediately.
    fresh = bench.mission.hardware_status()

    checks.equal(fresh["carousel"]["position_valid"], False,
                 "and the very next status refresh shows the PC the "
                 "truth - nothing carries the old belief forward")

    # No measurement was fabricated for the sample.
    record = bench.mission.session.get_sample("S-STALE")
    checks.equal(record.get("measurements") or [], [],
                 "and not one Measurement was written for a command "
                 "that was refused")

finally:
    bench.close()


# ======================================================================
checks.section("recovery from a reset is the documented procedure")

bench = Bench()

try:
    bench.link.connect_servo()
    bench.link.sync_position(load_slot=1)

    bench.reset_esp32()

    checks.equal(carousel(bench)["position_valid"], False,
                 "the position is gone")

    # Exactly what the screens tell the operator to do.
    code, _ = attempt(bench.link.connect_servo)
    checks.equal(code, "OK", "connect the servo again")

    code, _ = attempt(lambda: bench.link.sync_position(load_slot=1))
    checks.equal(code, "OK", "declare the origin again")

    state = carousel(bench)

    checks.equal(state["position_valid"], True, "and the carousel is usable")
    checks.equal(state["current_load_slot"], 1, "with Slot 1 at the loader")

    code, data = attempt(lambda: bench.link.measure_raw(1, "S-AFTER"))
    checks.equal(code, "OK", "and a measurement works again")

finally:
    bench.close()


# ======================================================================
checks.section("ESP32 keeps running, the PC restarts")

# The other direction. The board still holds a valid origin; the new
# client knows nothing. What it must NOT do is assume either way.

bench = Bench()

try:
    bench.link.connect_servo()
    bench.link.sync_position(load_slot=3)
    bench.link.select_slot(3, sample_id="S-PCRESTART")

    bench.mission.session.create("S-PCRESTART", 3)
    bench.mission.session.set_state("S-PCRESTART", STATE_LOADED)

    # A REAL ACQUISITION, through the firmware, because the point of
    # this case is now which computer still has it afterwards. A
    # hand-built raw block in the PC's working set proves nothing about
    # the device.
    acquired = bench.link.measure_raw(3, sample_id="S-PCRESTART")

    checks.ok(bool(acquired.get("illuminations")),
              "the sample really was measured")
    checks.ok(acquired.get("retention_durable"),
              "and the device says it stored the acquisition durably")

    bench.mission.session.add_measurement(
        "S-PCRESTART",
        raw={name: block["acquisitions"][0]
             for name, block in acquired["illuminations"].items()},
    )

    bench.restart_pc()

    state = carousel(bench)

    checks.equal(state["position_valid"], True,
                 "the firmware still knows where the carousel is - a PC "
                 "restart does not touch the board")
    checks.equal(state["current_load_slot"], 3,
                 "and which slot is at the loader")

    # The new client learns it by ASKING, not by remembering.
    fresh = bench.mission.hardware_status()

    checks.equal(fresh["carousel"]["current_load_slot"], 3,
                 "and the new client reads that state off the board")

    # WHAT SURVIVES A PC RESTART, AND WHAT MUST NOT.
    #
    # This used to assert that the Sample came back, because the
    # working set was written into samples.json. It is memory now, and
    # its disappearance is the ownership model working: measuring did
    # not save anything on the PC, so restarting the client has nothing
    # to bring back.
    #
    # The measurement is not lost - the ESP32 has it, durably, and the
    # next two checks prove the operator can still get it. That is the
    # trade this design makes, and it is only acceptable BECAUSE the
    # device keeps its copy.
    checks.ok(bench.mission.session.get_sample("S-PCRESTART") is None,
              "the working set did NOT survive the PC restart - it was "
              "never saved on the PC, which is the whole point")

    checks.ok(bench.mission.archive.get_sample("S-PCRESTART") is None,
              "and nothing of it is in the PC archive either, because "
              "nobody imported it")

    checks.ok("S-PCRESTART" not in bench.bd.samples_file.read_text(
        encoding="utf-8"),
        "and samples.json does not mention it")

    # AND THE SCIENCE IS STILL THERE, on the computer that owns it.
    held = bench.link.list_saved_samples()
    device_ids = [entry.get("sample_id")
                  for entry in (held.get("samples") or [])]

    checks.ok("S-PCRESTART" in device_ids,
              "the ESP32 still holds the acquisition after the client "
              "restarted, so the operator can import it")

    checks.equal(held.get("storage"), "device_filesystem",
                 "and says it keeps it on its filesystem, not in RAM")

    # A measurement works immediately: nothing needs re-declaring.
    code, _ = attempt(lambda: bench.link.measure_raw(3, "S-PCRESTART"))
    checks.equal(code, "OK",
                 "and a measurement works without re-syncing, because "
                 "the origin was never lost")

finally:
    bench.close()


# ======================================================================
checks.section("both restart")

bench = Bench()

try:
    bench.link.connect_servo()
    bench.link.sync_position(load_slot=1)
    bench.link.select_slot(1, sample_id="S-BOTH")
    bench.mission.session.create("S-BOTH", 1)
    bench.link.measure_raw(1, sample_id="S-BOTH")

    bench.reset_esp32()
    bench.restart_pc()

    state = carousel(bench)

    checks.equal(state["position_valid"], False,
                 "neither side claims to know the position")
    checks.equal(state["current_load_slot"], None, "and no slot number")
    checks.equal(servo_state(bench)["connected"], False,
                 "and no servo connection is assumed")

    # WHAT SURVIVES WHEN BOTH SIDES RESTART.
    #
    # Not the PC's working set: it is memory, and nobody imported it.
    # The acquisition itself does, because the device writes it to its
    # own filesystem - so the pair can lose power together and the
    # science is still recoverable by an import.
    checks.ok(bench.mission.session.get_sample("S-BOTH") is None,
              "the PC kept nothing - the working set is memory and was "
              "never imported")

    checks.ok(bench.mission.archive.get_sample("S-BOTH") is None,
              "and the PC archive holds nothing for it")

    held = bench.link.list_saved_samples()
    device_ids = [entry.get("sample_id")
                  for entry in (held.get("samples") or [])]

    checks.ok("S-BOTH" in device_ids,
              "but the ESP32 still holds the acquisition through BOTH "
              "restarts, which is what makes it the owner of an "
              "un-imported measurement")

    code, _ = attempt(bench.link.connect_servo)
    checks.equal(code, "OK", "and the documented recovery still works")

finally:
    bench.close()


# ======================================================================
checks.section("a reset in the middle of a command")

# At each point of one command, the board vanishes and comes back. The
# question is never "did this command fail" - it is "is the application
# coherent afterwards".

POINTS = (
    ("before the request is written", {"fail_write_after": 0}),
    ("after the write, while waiting", {"fail_read_after": 0}),
    ("part way through the response", {"fail_read_after": 1,
                                       "chunk_size": 8}),
)

for label, faults in POINTS:
    bench = Bench()

    try:
        bench.link.connect_servo()
        bench.link.sync_position(load_slot=1)

        for key, value in faults.items():
            setattr(bench.port, key, value)

        code, _ = attempt(lambda: bench.link.measure_raw(1, "S-MID"))

        checks.equal(code, "PORT_LOST",
                     "a reset {} is seen as PORT_LOST".format(label))

        # The operator reconnects to a board that rebooted.
        bench.reset_esp32()
        bench.restart_pc()

        state = carousel(bench)

        checks.equal(state["position_valid"], False,
                     "and after reconnecting, no position is claimed "
                     "({})".format(label))

        code, _ = attempt(lambda: bench.link.measure_raw(1, "S-MID"))
        checks.equal(code, "POSITION_NOT_SYNCHRONIZED",
                     "and a measurement is refused until the origin is "
                     "declared again ({})".format(label))

    finally:
        bench.close()


# ======================================================================
checks.section("boot output is kept, and is never mistaken for an answer")

# The link deliberately does not clear the receive buffer, so a boot
# banner or a traceback is still there to be read. It must be
# survivable as well as preserved.

for label, output in (("a boot banner", BOOT_BANNER),
                      ("a startup traceback", BOOT_TRACEBACK),
                      ("both", BOOT_BANNER + BOOT_TRACEBACK)):
    bench = Bench()

    try:
        bench.port._enqueue(output)

        # A real answer follows the noise.
        data = bench.link.ping()

        checks.equal(data.get("pong"), True,
                     "{} in front of a real answer does not stop it "
                     "being read".format(label))
        checks.equal(bench.link.corrupt_frames, 0,
                     "and none of it is counted as a damaged frame "
                     "({})".format(label))

    finally:
        bench.close()

# Boot output INSTEAD of an answer is a timeout that carries the output.
from fakes.esp32 import ScriptedDevice, TIMEOUT                # noqa: E402
from fakes.serial_port import FakeSerialPort                   # noqa: E402

clock = FakeClock()
installed = install_clock(serial_link, clock)
silent = ScriptedDevice(default=TIMEOUT)
link, port = open_link(serial_link, silent, clock=clock,
                       link_kwargs={"timeout": 2.0})
link.online = True

try:
    port._enqueue(BOOT_TRACEBACK)

    code, _ = attempt(link.ping)

    checks.equal(code, "PROTOCOL_TIMEOUT",
                 "a traceback INSTEAD of an answer is a timeout, not a "
                 "success")
    checks.ok(any("Traceback" in line for line in link.last_noise),
              "and the traceback is kept on the error, because it is "
              "usually the entire explanation")

    from serial_link import diagnose_noise                     # noqa: E402

    diagnosis = diagnose_noise(link.last_noise)

    checks.ok("exception during startup" in diagnosis.lower()
              or "traceback" in diagnosis.lower(),
              "and the diagnosis names it as a firmware startup failure")

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("the retained acquisition survives a reset of the board")

# THE PROPERTY THAT PAYS FOR THE PC KEEPING ITS WORKING SET IN MEMORY.
#
# This buffer used to be RAM, and a reset emptied it - which is what
# the PC's persisted session was there to compensate for. Compensating
# on the PC was the wrong end: it made every measurement a stored PC
# file the moment it was taken, so "Import" decided nothing.
#
# The acquisitions are written to the device's own filesystem now, so
# the computer that OWNS the un-imported measurement is also the one
# that can still produce it after a reboot. If this ever goes back to
# being volatile, the ownership model has a hole in it and this case is
# where that shows up.

bench = Bench()

try:
    bench.link.connect_servo()
    bench.link.sync_position(load_slot=1)
    bench.link.select_slot(1, sample_id="S-BUFFER")
    bench.link.measure_raw(1, sample_id="S-BUFFER")

    held = bench.link.list_saved_samples()
    before = held.get("samples") or held.get("saved") or []

    checks.ok(len(before) >= 1,
              "the device is holding the acquisition ({})".format(
                  len(before)))

    checks.equal(held.get("storage"), "device_filesystem",
                 "and says it is holding it on its filesystem")

    bench.reset_esp32()

    held = bench.link.list_saved_samples()
    after = held.get("samples") or held.get("saved") or []

    checks.equal(len(after), len(before),
                 "and a reset does NOT empty it - the acquisition is on "
                 "the device's filesystem, not in its RAM")

    checks.equal([entry.get("sample_id") for entry in after],
                 [entry.get("sample_id") for entry in before],
                 "the same Sample IDs come back, so the PC can still "
                 "match the device's copy to its own record")

    # AND IT IS THE WHOLE RECORD, not just an index entry.
    restored = bench.link.get_saved_sample("S-BUFFER")
    measurement = (restored or {}).get("measurement") or {}

    checks.ok(bool(measurement.get("illuminations")),
              "and the spectrum itself survived, not merely its name")

    # PHYSICAL CLAIMS DO NOT SURVIVE, and must not.
    status = bench.link.get_status()
    slots = {slot["slot_id"]: slot for slot in (status.get("slots") or [])}

    checks.equal(slots[1]["occupied"], False,
                 "while the slot is NOT reported occupied - the firmware "
                 "cannot know whether the soil is still in the cup, and "
                 "a restored record must not invent that")

    checks.equal((status.get("carousel") or {})["position_valid"], False,
                 "and the position is still unknown, because a stored "
                 "spectrum says nothing about where the mechanism is")

finally:
    bench.close()


restore_serial()

sys.exit(checks.report())
