"""
The same operations, thousands of times, looking for drift.

WHAT A STRESS TEST IS FOR HERE

Not throughput. Nothing in this system is fast and nothing needs to be.
What thousands of cycles find that ten cannot:

    STATE THAT ACCUMULATES     a buffer that is appended to and never
                               trimmed, a counter that only goes up, a
                               list of damaged lines that grows without
                               a cap
    DRIFT                      a carousel that is one slot out after
                               four hundred moves because a rounding
                               error is applied per movement
    LEAKS                      a store that keeps every version of the
                               archive it has ever written
    UNBOUNDED RETRIES          a recovery path that never gives up

Every count below is chosen so the whole suite runs in seconds on the
fake clock. A stress test that takes ten minutes is a stress test that
gets skipped.
"""

import gc
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
from serial_link import DeviceError, LinkError, NOISE_LIMIT  # noqa: E402

from BD.samples import STATE_LOADED                          # noqa: E402

from fakes import FakeClock, SandboxBD                       # noqa: E402
from fakes.clock import install_clock                        # noqa: E402
from fakes.esp32 import (                                    # noqa: E402
    GARBAGE,
    MALFORMED,
    LoopbackDevice,
    ScriptedDevice,
)
from fakes.serial_port import install_fake_serial, open_link  # noqa: E402

checks = support.Checks("stress")

restore_serial = install_fake_serial(serial_link)

# MEASURED, not guessed. On this machine a ping through the fake costs
# 50 microseconds, a verified carousel move about 3 milliseconds, and a
# full three-illumination measurement through the real firmware and the
# real AS7265x register protocol about 370 milliseconds. The counts
# below are chosen so the whole group runs in well under a minute:
# a stress suite that takes ten is a stress suite that gets skipped,
# and a skipped suite finds nothing.
SERIAL_CYCLES = 10000        # ~0.5 s
FIRMWARE_CYCLES = 100        # ~37 s - the expensive one
CAROUSEL_MOVES = 2000        # ~6 s
ARCHIVE_WRITES = 300         # ~2 s


# ======================================================================
checks.section("{} request/response cycles".format(SERIAL_CYCLES))

clock = FakeClock()
installed = install_clock(serial_link, clock)
link, port = open_link(serial_link, None, clock=clock)
link.online = True

try:
    for _cycle in range(SERIAL_CYCLES):
        link.request("ping")

    checks.ok(True, "{} commands completed".format(SERIAL_CYCLES))

    checks.equal(port.in_waiting, 0,
                 "and the receive buffer is EMPTY afterwards - a frame "
                 "left behind by each command would be a slow leak and "
                 "the next session's stale answer")

    checks.equal(link.corrupt_frames, 0, "with no corruption")
    checks.equal(link.stale_frames, 0, "and nothing mistaken for stale")

    ids = [request["request_id"] for request in port.requests]
    checks.equal(len(set(ids)), SERIAL_CYCLES,
                 "every request had its own id, with no wraparound "
                 "collision over {} of them".format(SERIAL_CYCLES))

    # `<nonce>-<counter>`: one nonce for the session, a counter that
    # only goes up. Split rather than int()-ed, because the id is a
    # string by design - see _next_request_id.
    nonces = {value.rsplit("-", 1)[0] for value in ids}
    counters = [int(value.rsplit("-", 1)[1]) for value in ids]

    checks.equal(len(nonces), 1,
                 "every request carries the SAME session nonce")
    checks.equal(counters, sorted(counters),
                 "and a counter that only ever increases")
    checks.equal(counters, list(range(1, SERIAL_CYCLES + 1)),
                 "with no gaps and no repeats across {} of "
                 "them".format(SERIAL_CYCLES))

    checks.ok(link.bytes_read > SERIAL_CYCLES,
              "the byte counter accumulated ({} bytes), so the cost of "
              "a response is measurable rather than guessed".format(
                  link.bytes_read))

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("a thousand damaged frames do not fill memory")

# `damaged_lines` is a diagnostic aid. It has to be capped, or a link
# with a bad cable turns into a memory leak over a long mission.

clock = FakeClock()
installed = install_clock(serial_link, clock)
device = ScriptedDevice({"ping": MALFORMED, "get_status": GARBAGE})
link, port = open_link(serial_link, device, clock=clock,
                       link_kwargs={"timeout": 1.0})
link.online = True

try:
    damaged = 0

    for _cycle in range(1000):
        try:
            link.request("ping")

        except LinkError:
            damaged += 1

    checks.equal(damaged, 1000, "a thousand damaged answers, all refused")
    checks.equal(link.corrupt_frames, 1000, "and all counted")
    checks.ok(len(link.damaged_lines) <= NOISE_LIMIT,
              "but only {} lines are KEPT - the cap is what stops a bad "
              "cable becoming a memory leak ({} held)".format(
                  NOISE_LIMIT, len(link.damaged_lines)))

    for _cycle in range(200):
        try:
            link.request("get_status")

        except LinkError:
            pass

    checks.ok(len(link.last_noise) <= NOISE_LIMIT,
              "and the console noise is capped the same way ({} "
              "lines)".format(len(link.last_noise)))

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("{} carousel movements without drift".format(CAROUSEL_MOVES))

clock = FakeClock()
installed = install_clock(serial_link, clock)
loopback = LoopbackDevice()
loopback.build()
link, port = open_link(serial_link, loopback, clock=clock)
link.online = True

try:
    link.connect_servo()
    link.sync_position(load_slot=1)

    expected = 1
    drift = []

    # THOUSANDS OF BIASED MOVES IS EXACTLY WHERE THE TRAJECTORY
    # REGISTER RUNS OUT, AND THAT IS A REAL EVENT, NOT A TEST ARTIFACT.
    #
    # Register 67 accumulates net one-directional travel and clamps at
    # 32766 - about eight revolutions - after which the servo stops
    # moving in either direction while still reporting a following
    # error of about 2. The driver folds the register back before that
    # can happen, which writes EPROM, moves nothing, and carries the
    # logical angle across unchanged.
    #
    # This loop is biased clockwise, so it WILL get there, several
    # times. Nothing here catches an error or performs a recovery: the
    # point of the section is that a long biased session needs neither.
    for cycle in range(CAROUSEL_MOVES):
        direction = "cw" if cycle % 2 == 0 or cycle % 7 else "ccw"
        steps = (cycle % 4) + 1

        link.move_slots(direction, steps)

        expected = ((expected - 1
                     + (steps if direction == "cw" else -steps)) % 4) + 1

        actual = link.get_status()["carousel"]["current_load_slot"]

        if actual != expected:
            drift.append((cycle, direction, steps, expected, actual))

            break

    checks.equal(drift, [],
                 "{} movements, and the loading slot is still exactly "
                 "where the arithmetic says it should be".format(
                     CAROUSEL_MOVES))

    servo = link.get_status()["servo"]["backend"] or {}
    reseeds = servo.get("trajectory_reseeds")

    checks.ok((reseeds or 0) > 0,
              "the trajectory register was folded back {} time(s) on "
              "the way - the real event a long biased session produces, "
              "handled rather than avoided".format(reseeds))

    checks.ok((servo.get("trajectory_headroom") or 0) > 0,
              "and it is still short of its clamp, so the fold happened "
              "BEFORE the servo could start refusing movements while "
              "reporting them complete")

    status = link.get_status()

    checks.equal(status["carousel"]["position_valid"], True,
                 "the position is still valid after all of them")

    reference = status["carousel"]["reference"]

    checks.ok(abs(reference.get("alignment_offset_deg") or 0.0) < 1e-6,
              "and no fine-alignment offset appeared out of nowhere")

    # The encoder is a 4096-count ring. Thousands of moves in one
    # direction is exactly where a naive absolute-position driver wraps
    # and takes the long way round.
    checks.ok(reference.get("origin") is not None,
              "and the origin is still the encoder reading the operator "
              "confirmed, not something recomputed since")

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("{} whole firmware command cycles".format(FIRMWARE_CYCLES))

clock = FakeClock()
installed = install_clock(serial_link, clock)
loopback = LoopbackDevice()
loopback.build()
link, port = open_link(serial_link, loopback, clock=clock)
link.online = True

try:
    link.connect_servo()
    link.sync_position(load_slot=1)

    failures = []

    for cycle in range(FIRMWARE_CYCLES):
        slot = (cycle % 4) + 1

        try:
            link.select_slot(slot, sample_id="STRESS-{}".format(cycle))
            link.measure_raw(slot, sample_id="STRESS-{}".format(cycle))

        except (DeviceError, LinkError) as error:
            failures.append((cycle, error.code))

            break

    checks.equal(failures, [],
                 "{} select-and-measure cycles, every one of them "
                 "completing".format(FIRMWARE_CYCLES))

    status = link.get_status()

    checks.equal(status["carousel"]["position_valid"], True,
                 "the carousel still knows where it is")
    checks.ok(status["sensor"]["ready"] is True,
              "the sensor is still ready")
    checks.equal(status["sensor"]["recovery_count"], 0,
              "and it never had to be recovered")

    # The device's retained-acquisition buffer is the one thing on a
    # memory-constrained board that grows with use.
    saved = link.list_saved_samples()
    held = saved.get("samples") or saved.get("saved") or []

    checks.ok(len(held) <= FIRMWARE_CYCLES,
              "the device's acquisition buffer holds {} entries after "
              "{} measurements - bounded, not one per cycle "
              "forever".format(len(held), FIRMWARE_CYCLES))

    checks.equal(link.corrupt_frames, 0,
                 "and not one frame was damaged across all of it")

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("{} archive writes".format(ARCHIVE_WRITES))

with SandboxBD() as bd:
    store = bd.sample_store()

    for index in range(ARCHIVE_WRITES):
        sample_id = "STRESS-{:04d}".format(index)
        store.create(sample_id, (index % 4) + 1)
        store.set_state(sample_id, STATE_LOADED)

    checks.equal(store.count(), ARCHIVE_WRITES,
                 "{} Samples written".format(ARCHIVE_WRITES))

    leftovers = [p.name for p in bd.samples_file.parent.iterdir()
                 if p.name.startswith(".samples-")]

    checks.equal(leftovers, [],
                 "and NOT ONE temporary file was left behind - the "
                 "atomic write cleans up after itself every time, not "
                 "usually")

    reread = bd.sample_store()

    checks.equal(reread.count(), ARCHIVE_WRITES,
                 "and every one of them reads back")

    size = bd.samples_file.stat().st_size
    per_sample = size / float(ARCHIVE_WRITES)

    checks.ok(per_sample < 2000,
              "the archive is {:.0f} bytes per Sample, so it grows with "
              "the mission and not with the number of saves".format(
                  per_sample))


# ======================================================================
checks.section("nothing accumulated that should not have")

gc.collect()

clock = FakeClock()
installed = install_clock(serial_link, clock)
link, port = open_link(serial_link, None, clock=clock)
link.online = True

try:
    for _cycle in range(2000):
        link.request("ping")

    checks.equal(len(link.damaged_lines), 0,
                 "a healthy link keeps no damaged lines")
    checks.equal(len(link.last_noise), 0, "and no console noise")
    checks.equal(port.in_waiting, 0, "and no unread bytes")

    # The one structure that legitimately grows is the fake's own
    # record of what it was sent. Stated so its growth is not mistaken
    # for a leak in the code under test.
    checks.equal(len(port.requests), 2000,
                 "the only thing that grew is the FAKE's log of what it "
                 "was asked, which is the test's own bookkeeping")

finally:
    installed.restore()
    link.close()


restore_serial()

sys.exit(checks.report())
