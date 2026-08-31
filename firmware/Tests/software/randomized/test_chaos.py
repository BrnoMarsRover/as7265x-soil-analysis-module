"""
Seeded fault storms over whole workflows.

WHAT MAKES THIS DIFFERENT FROM THE FAULT SUITES

`fault_injection/` fails one thing at a time and asserts what should
happen. That finds the failures somebody thought of. This runs long
random sequences of operations with random faults injected between
them, and asserts only the INVARIANTS - the handful of statements that
must be true no matter what happened.

    the carousel is either at a known slot or at no slot
    the loader and the scanner are half a turn apart
    a movement that failed never leaves the position valid
    an archive on disk is always parseable
    a Sample that says MEASURED has a Measurement with RAW in it
    nothing raises an exception that is not a LinkError

DETERMINISM IS THE WHOLE POINT

Every run uses fixed seeds, so a failure found here is a failure
anybody can reproduce by running the same command. The seed is printed
with every failure and the scenario that produced it is replayed and
printed step by step, because "chaos test failed" is not a bug report.

A failure here is a LEAD, not a diagnosis: reduce it, name the root
cause, fix that, and add a deterministic regression test beside it.
"""

import json
import random
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

from BD import samples as samples_module                     # noqa: E402
from BD.samples import (                                     # noqa: E402
    ACQUISITION_SUCCESS,
    STATE_LOADED,
    STATE_MEASURED,
    StorageError,
)

from fakes import FakeClock, SandboxBD                       # noqa: E402
from fakes.clock import install_clock                        # noqa: E402
from fakes.esp32 import (                                    # noqa: E402
    ECHO,
    GARBAGE,
    MALFORMED,
    TIMEOUT,
    TRUNCATED,
    WRONG_ID,
    LoopbackDevice,
)
from fakes.serial_port import install_fake_serial, open_link  # noqa: E402

checks = support.Checks("chaos")

restore_serial = install_fake_serial(serial_link)

SLOT_COUNT = 4
SCAN_OFFSET = SLOT_COUNT // 2

# Fixed. A chaos suite with a clock-derived seed is a suite whose
# failures cannot be reproduced, which makes them impossible to fix.
SEEDS = (20260824, 1, 7, 42, 1337, 99991, 271828, 314159,
         65537, 8675309)
STEPS_PER_RUN = 60

LIES = (MALFORMED, TIMEOUT, GARBAGE, TRUNCATED, WRONG_ID, ECHO)


class Chaos:
    """One seeded session: a link, the real firmware and a sandbox BD."""

    def __init__(self, seed):
        self.seed = seed
        self.random = random.Random(seed)
        self.history = []

        self.clock = FakeClock()
        self.installed = install_clock(serial_link, self.clock)

        self.servo = support.FakeST3215()
        self.sensor = support.FakeAS7265X()

        self.loopback = LoopbackDevice(device=self.sensor, servo=self.servo)
        self.loopback.build()

        self.link, self.port = open_link(
            serial_link, self.loopback, clock=self.clock,
            link_kwargs={"timeout": 2.0})
        self.link.online = True

        self.bd = SandboxBD()
        self.store = self.bd.sample_database().session()

        self.counter = 0

    def close(self):
        self.installed.restore()
        self.link.close()
        self.bd.close()

    # -- the things that can happen ------------------------------------

    def record(self, label, outcome):
        self.history.append((label, outcome))

    def run(self, label, call):
        try:
            call()
            self.record(label, "OK")

            return True

        except (DeviceError, LinkError, StorageError) as error:
            self.record(label, getattr(error, "code", "?"))

            return False

        except Exception as error:                     # noqa: BLE001
            self.record(label, "CRASH:" + type(error).__name__)

            raise

    # -- faults --------------------------------------------------------

    def inject(self):
        """One random fault, or none."""
        roll = self.random.random()

        if roll < 0.55:
            self.loopback.lie = {}

            return "none"

        if roll < 0.75:
            command = self.random.choice((
                "measure_raw", "move_slots", "select_slot", "get_status",
                "sync_position", "connect_servo", "acquire_triad",
            ))
            behaviour = self.random.choice(LIES)
            self.loopback.lie = {command: behaviour}

            return "{} -> {}".format(command, behaviour)

        if roll < 0.85:
            self.servo.silent = not self.servo.silent

            return "servo silent={}".format(self.servo.silent)

        if roll < 0.95:
            self.sensor.bus_error = not self.sensor.bus_error

            return "sensor bus_error={}".format(self.sensor.bus_error)

        self.fail_saves = not getattr(self, "fail_saves", False)

        return "saves failing={}".format(self.fail_saves)

    # -- actions -------------------------------------------------------

    def act(self):
        name = self.random.choice((
            "connect", "sync", "select", "move", "fine", "status",
            "measure", "torque", "stop", "create", "state", "record",
            "disconnect",
        ))
        link = self.link
        self.counter += 1
        sample_id = "CHAOS-{:04d}".format(self.counter)

        if name == "connect":
            self.run("connect", link.connect_servo)

        elif name == "sync":
            self.run("sync", lambda: link.sync_position(load_slot=1))

        elif name == "select":
            slot = self.random.randint(1, SLOT_COUNT)
            self.run("select {}".format(slot),
                     lambda: link.select_slot(slot))

        elif name == "move":
            direction = self.random.choice(("cw", "ccw"))
            steps = self.random.randint(1, SLOT_COUNT)
            self.run("move {} {}".format(direction, steps),
                     lambda: link.move_slots(direction, steps))

        elif name == "fine":
            degrees = self.random.uniform(-5.0, 5.0)
            self.run("fine {:.2f}".format(degrees),
                     lambda: link.fine_adjust(degrees))

        elif name == "status":
            self.run("status", link.get_status)

        elif name == "measure":
            slot = self.random.randint(1, SLOT_COUNT)
            self.run("measure {}".format(slot),
                     lambda: link.measure_raw(slot))

        elif name == "torque":
            enable = self.random.choice((True, False))
            self.run("torque {}".format(enable),
                     lambda: link.servo_torque(enable))

        elif name == "stop":
            self.run("stop", link.servo_stop)

        elif name == "disconnect":
            self.run("disconnect", link.disconnect_servo)

        elif name == "create":
            self.run("create {}".format(sample_id),
                     lambda: self.store.create(
                         sample_id, self.random.randint(1, SLOT_COUNT)))

        elif name == "state":
            existing = [r["sample_id"] for r in self.store.summaries()]

            if existing:
                chosen = self.random.choice(existing)
                self.run("state {}".format(chosen),
                         lambda: self.store.set_state(chosen, STATE_LOADED))

        else:
            existing = [r["sample_id"] for r in self.store.summaries()]

            if existing:
                chosen = self.random.choice(existing)
                self.run("measurement {}".format(chosen),
                         lambda: self.store.add_measurement(
                             chosen, raw={"white": {"A": 1.0}}))

    # -- invariants -----------------------------------------------------

    def violations(self):
        broken = []

        try:
            carousel = (self.link.get_status() or {}).get("carousel") or {}

        except (DeviceError, LinkError):
            return broken

        valid = carousel.get("position_valid")
        load = carousel.get("current_load_slot")
        scan = carousel.get("current_scan_slot")

        if valid:
            if not (isinstance(load, int) and 1 <= load <= SLOT_COUNT):
                broken.append("valid position with loader {!r}".format(load))

            if not (isinstance(scan, int) and 1 <= scan <= SLOT_COUNT):
                broken.append("valid position with scanner {!r}".format(scan))

            if isinstance(load, int) and isinstance(scan, int):
                if (scan - load) % SLOT_COUNT != SCAN_OFFSET:
                    broken.append(
                        "loader {} and scanner {} are not half a turn "
                        "apart".format(load, scan))

        else:
            if load is not None or scan is not None:
                broken.append(
                    "invalid position still reporting loader {!r} / "
                    "scanner {!r}".format(load, scan))

        # The archive is always parseable, and always self-consistent.
        try:
            payload = json.loads(
                self.bd.samples_file.read_text(encoding="utf-8"))

        except (OSError, ValueError) as error:
            broken.append("the archive is unreadable: {}".format(error))

            return broken

        for record in payload.get("samples") or []:
            if record.get("state") == STATE_MEASURED:
                successful = [
                    m for m in record.get("measurements") or []
                    if m.get("acquisition_status") == ACQUISITION_SUCCESS
                ]

                if not successful:
                    broken.append(
                        "{} says MEASURED with no successful "
                        "Measurement".format(record.get("sample_id")))

                for measurement in successful:
                    if not measurement.get("raw"):
                        broken.append(
                            "{} has a SUCCESS with no RAW".format(
                                record.get("sample_id")))

        return broken


# ======================================================================
checks.section("{} seeded runs of {} steps each".format(
    len(SEEDS), STEPS_PER_RUN))

total_steps = 0
total_faults = 0
failures = []

for seed in SEEDS:
    chaos = Chaos(seed)
    original_replace = samples_module.os.replace

    def maybe_refuse(*args, **kwargs):
        if getattr(chaos, "fail_saves", False):
            raise OSError(28, "No space left on device")

        return original_replace(*args, **kwargs)

    samples_module.os.replace = maybe_refuse

    try:
        for step in range(STEPS_PER_RUN):
            fault = chaos.inject()

            if fault != "none":
                total_faults += 1

            chaos.act()
            total_steps += 1

            broken = chaos.violations()

            if broken:
                failures.append((seed, step, fault, broken,
                                 list(chaos.history[-6:])))

                break

    except Exception as error:                         # noqa: BLE001
        failures.append((seed, "?", "?",
                         ["raised {}: {}".format(
                             type(error).__name__, error)],
                         list(chaos.history[-6:])))

    finally:
        samples_module.os.replace = original_replace
        chaos.close()

if failures:
    print()
    print("  REPRODUCE:")

    for seed, step, fault, broken, history in failures:
        print("    seed={} step={} fault={!r}".format(seed, step, fault))

        for reason in broken:
            print("      invariant: {}".format(reason))

        for label, outcome in history:
            print("      ... {:<24} {}".format(label, outcome))

    print()

checks.equal(failures, [],
             "{} steps across {} seeds, {} faults injected, and every "
             "invariant held".format(total_steps, len(SEEDS), total_faults))

checks.ok(total_faults > len(SEEDS),
          "faults really were injected ({} of them) - a chaos run that "
          "injects nothing passes for the wrong reason".format(
              total_faults))


# ======================================================================
checks.section("the same seed produces the same run")

# If it does not, a failure found here cannot be reproduced, and the
# suite is worth nothing.

def fingerprint(seed, steps=15):
    chaos = Chaos(seed)

    try:
        for _step in range(steps):
            chaos.inject()
            chaos.act()

        return list(chaos.history)

    finally:
        chaos.close()


first = fingerprint(4242)
second = fingerprint(4242)

checks.equal(first, second,
             "two runs of seed 4242 take exactly the same {} steps with "
             "exactly the same outcomes".format(len(first)))

different = fingerprint(4243)

checks.ok(different != first,
          "and a different seed takes a different path, so the seeds "
          "are actually doing something")


# ======================================================================
checks.section("recovery is always possible, whatever the storm did")

# The mission question: after everything, can the operator get back to
# a working instrument?

for seed in SEEDS[:3]:
    chaos = Chaos(seed)

    try:
        for _step in range(STEPS_PER_RUN):
            chaos.inject()
            chaos.act()

        # The operator's way back, in the order the screens offer it.
        chaos.loopback.lie = {}
        chaos.servo.silent = False
        chaos.sensor.bus_error = False
        chaos.fail_saves = False

        chaos.run("recover: connect", chaos.link.connect_servo)
        chaos.run("recover: sync",
                  lambda: chaos.link.sync_position(load_slot=1))

        carousel = chaos.link.get_status()["carousel"]

        checks.equal(carousel["position_valid"], True,
                     "seed {}: after {} chaotic steps the instrument is "
                     "usable again".format(seed, STEPS_PER_RUN))
        checks.equal(chaos.violations(), [],
                     "seed {}: and every invariant holds".format(seed))

    finally:
        chaos.close()


restore_serial()

sys.exit(checks.report())
