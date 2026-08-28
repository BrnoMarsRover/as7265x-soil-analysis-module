"""
The operator interface: compact, honest, and recoverable.

WHAT THIS SUITE IS FOR

Four defects reached the bench through the interface rather than
through the science, and all four are the same kind of mistake -
software describing a state it had not established.

    1. A movement failure asserted "THE CAROUSEL HAS MOVED" in the same
       message that reported the encoder measuring zero counts of
       travel. The firmware already knew better: motion_verdict reads
       encoder_moved and returns UNKNOWN for exactly that case, so both
       answers were printed, one paragraph apart.

    2. The same refusal said the same thing three times - once as the
       firmware's message, once as a `carousel:` line, once as an
       instruction. Eleven lines to convey two facts.

    3. A SERVO_POSITION_MISMATCH invalidates the carousel position, and
       the startup screen keyed on position_valid alone. So a fault
       with the servo answering normally throughout told the operator
       to connect a servo that had never disconnected, and never showed
       the instruction that would actually have helped.

    4. The sensor block read `sensor["boot_error"]`, a key no firmware
       has ever sent - AS7265x.status() calls it `first_init_error`.
       Both branches were dead, so every sensor initialisation failure
       displayed a bare "UNAVAILABLE" with the reason discarded.

WHAT IS ASSERTED, AND WHAT IS DELIBERATELY NOT

These check MEANING, not phrasing: that an unverified movement is not
described as a movement, that one fact appears once, that two
independent states stay independent. Pinning exact sentences would make
the wording unimprovable, which is how the eleven-line block survived
as long as it did.
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

import contextlib                                            # noqa: E402
import io                                                    # noqa: E402

from workflow import status as ui_status                     # noqa: E402

checks = support.Checks("operator-ui")


def rendered(*args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        ui_status.print_failure(*args, **kwargs)

    return out.getvalue()


# The bench failure, verbatim from the operator's terminal: a jog of one
# slot, 1024 counts commanded, encoder reading 2 before and 2 after.
MISMATCH = {
    "requested": {"direction": "cw", "slots": 1},
    "motion": "UNKNOWN",
    "moved": True,
    "motion_detail": {
        "commanded": True, "goal_written": True,
        "start_position": 2, "actual_position": 2,
        "expected_position": 1026, "travelled_counts": 0,
        "travelled_degrees": 0.0, "encoder_moved": False,
        "position_error": 1024, "tolerance_counts": 15, "settled": True,
    },
}

# The same command, where the encoder DID measure travel.
MOVED = {
    "requested": {"direction": "cw", "slots": 1},
    "motion": "MOVED",
    "moved": True,
    "motion_detail": {
        "commanded": True, "goal_written": True,
        "start_position": 2, "actual_position": 900,
        "expected_position": 1026, "travelled_counts": 898,
        "travelled_degrees": 78.9, "encoder_moved": True,
        "position_error": 126, "tolerance_counts": 15, "settled": True,
    },
}

NOT_STARTED = {"motion": "NOT_STARTED", "moved": False}


# ======================================================================
checks.section("a movement software cannot verify is not called a movement")

# WHAT BROKE
#   servo.py raised ST3215PositionError with "THE CAROUSEL HAS MOVED"
#   hardcoded into the message, regardless of what the encoder read.
#
# WHAT IT COST
#   The operator was told the carousel had moved, in capitals, by the
#   same sentence that reported zero counts of travel. Believing it
#   means treating the mechanism as displaced when it may be exactly
#   where it started - and the two call for opposite actions.

printed = rendered("SERVO_POSITION_MISMATCH", MISMATCH)
lowered = printed.lower()

checks.ok("position unknown" in lowered,
          "an unverified movement reports the position as UNKNOWN")
checks.ok("not verified" in lowered,
          "and says the movement was not verified")
checks.ok("carousel has moved" not in lowered
          and "the carousel moved" not in lowered,
          "and never claims the carousel moved - the encoder measured "
          "zero counts, so software has no evidence that it did")
checks.ok("encoder measured travel" not in lowered,
          "and does not borrow the encoder's evidence it does not have")

# The other direction must still work: when the encoder DID see travel,
# saying so is the whole point of RF-001D.
moved_text = rendered("SERVO_POSITION_MISMATCH", MOVED).lower()

checks.ok("encoder measured travel" in moved_text,
          "but a movement the encoder DID measure is reported as one")
checks.ok("position unknown" in moved_text,
          "with the position still untrusted, because it stopped short")

# And a refusal before anything was commanded may say so.
idle = rendered("SERVO_NOT_CONNECTED", NOT_STARTED).lower()

checks.ok("unchanged" in idle and "never commanded" in idle,
          "a refusal that never reached the servo says the carousel is "
          "unchanged")
checks.ok("position unknown" not in idle,
          "and does not invalidate a position that was never disturbed")


# ======================================================================
checks.section("one fact, said once")

# WHAT BROKE
#   report_link_error printed the firmware's message, then re-derived
#   the same verdict as a `carousel:` line, then an instruction. The
#   firmware's message ALSO carried a consequence paragraph appended by
#   Carousel._movement_failed. Four statements of two facts.

printed = rendered("SERVO_POSITION_MISMATCH", MISMATCH)
lowered = printed.lower()

checks.equal(lowered.count("position unknown"), 1,
             "the carousel verdict appears exactly once")
checks.equal(lowered.count("re-sync"), 1,
             "and the instruction to re-sync appears exactly once")

# The evidence is present and structured, so nothing is lost by being
# brief: this is the block that replaced the paragraphs.
for token in ("1026", "1024", "15"):
    checks.ok(token in printed,
              "the block still carries the {} figure".format(token))

checks.ok(len(printed.strip().splitlines()) <= 10,
          "and the whole failure fits in ten lines (was eleven lines of "
          "prose saying less)")

# A refusal carrying structured evidence must not ALSO print the
# firmware's sentence - that sentence is what the block is made of.
with_message = rendered(
    "SERVO_POSITION_MISMATCH", MISMATCH,
    message="Servo 1 was commanded 1024 counts, the encoder moved 0 "
            "counts, and it reports position 2.",
)

checks.ok("servo 1 was commanded" not in with_message.lower(),
          "a refusal with structured evidence does not repeat the "
          "firmware's prose on top of it")

# But a refusal with NO evidence still shows its message, or the screen
# would say nothing at all.
bare = rendered("PORT_CLOSED", {}, message="the serial port is closed")

checks.ok("serial port is closed" in bare.lower(),
          "a refusal with no structured evidence still prints its "
          "message")


# ======================================================================
checks.section("servo ONLINE and carousel UNKNOWN are different states")

# WHAT BROKE
#   print_startup_screen was reached on `not position_valid` and always
#   said "connect the ST3215 carousel servo". A position mismatch
#   invalidates the position with the servo answering perfectly, so the
#   operator was sent to reconnect working hardware.

# !! THESE FIXTURES WERE THE BUG. !!
#
# The checks below are the right checks - "the two recovery paths must
# stay distinct" is exactly the defect - and they passed for weeks while
# production was broken, because every fixture here said `selected` and
# `ServoLink.status()` has only ever sent `connected`. The reader agreed
# with the fixture, the fixture agreed with the reader, and neither had
# ever been compared to a board.
#
# They now use the firmware's own key. `test_operator_flow.py` asserts
# the key itself against a live loopback firmware, so this class of
# agreement cannot re-form.

ONLINE_UNSYNCED = {
    "servo": {"connected": True, "label": "Waveshare ST3215",
              "backend": {"connected": True, "id": 1}},
    "carousel": {"position_valid": False, "carousel_phase": "LOAD"},
    "sensor": {"state": "READY", "ready": True},
}

OFFLINE = {
    "servo": {"connected": False, "label": "NOT SELECTED", "backend": {}},
    "carousel": {"position_valid": False},
    "sensor": {"state": "READY", "ready": True},
}

NOT_ANSWERING = {
    "servo": {"connected": True, "label": "Waveshare ST3215",
              "backend": {"connected": False}},
    "carousel": {"position_valid": False},
    "sensor": {"state": "READY", "ready": True},
}

checks.equal(ui_status.servo_link(ONLINE_UNSYNCED), "ONLINE",
             "a servo that answers is ONLINE even with the carousel lost")
checks.equal(ui_status.servo_link(OFFLINE), "NOT SELECTED",
             "a servo that was never selected is NOT SELECTED")
checks.equal(ui_status.servo_link(NOT_ANSWERING), "NOT ANSWERING",
             "a selected servo that does not answer is NOT ANSWERING")

checks.equal(ui_status.carousel_label(ONLINE_UNSYNCED), "POSITION UNKNOWN",
             "an invalidated position outranks the reported phase - the "
             "firmware does not believe that phase either")

# THE TWO RECOVERY PATHS MUST STAY DISTINCT. This is the check that
# would have caught the defect.
action, needed = ui_status.recovery_action(ONLINE_UNSYNCED)

checks.ok(needed, "an unsynchronized carousel needs an action")
checks.ok("sync" in action.lower(),
          "and the action for an ONLINE servo is to RE-SYNC")
checks.ok("connect" not in action.lower(),
          "never to connect a servo that is already connected - this is "
          "the defect: a position mismatch is not a disconnection")

action, needed = ui_status.recovery_action(OFFLINE)

checks.ok(needed and "connect" in action.lower(),
          "a servo that is not selected DOES need connecting")

action, needed = ui_status.recovery_action(NOT_ANSWERING)

checks.ok(needed and "reconnect" in action.lower(),
          "and one that stopped answering needs reconnecting")

# A fully healthy system asks for nothing.
healthy = {
    "servo": {"connected": True, "backend": {"connected": True}},
    "carousel": {"position_valid": True, "carousel_phase": "LOAD"},
    "sensor": {"state": "READY", "ready": True},
}

action, needed = ui_status.recovery_action(healthy)

checks.ok(not needed and action is None,
          "a synchronized carousel on a live servo needs no recovery")
checks.equal(ui_status.carousel_label(healthy), "LOAD",
             "and reports the phase it is actually in")


# ======================================================================
checks.section("the startup screen names the right recovery")

from workflow import screen as screen_module                  # noqa: E402


def startup_for(status):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        screen_module.print_startup_screen(status)

    return out.getvalue()


online_screen = startup_for(ONLINE_UNSYNCED).lower()

checks.ok("re-sync" in online_screen,
          "with the servo answering, [0] offers a RE-SYNC")
checks.ok("connect the st3215" not in online_screen,
          "and does not tell the operator to connect a live servo")
checks.ok("position unknown" in online_screen,
          "while still saying the carousel position is not trusted")

offline_screen = startup_for(OFFLINE).lower()

checks.ok("connect" in offline_screen,
          "with no servo selected, it does offer to connect one")


# ======================================================================
checks.section("a dependent workflow is not offered on an unknown position")

# Measure Sample swings the carousel 180 degrees at a slot number that
# means nothing once the position is invalidated. The main loop routes
# away from this screen in that state, so this label is a second line -
# but "[AVAILABLE]" printed over POSITION UNKNOWN is the misleading
# confidence itself, and it must not rely on another function's routing.

LOADED = {"state": "LOADED"}

labels = screen_module.action_labels(
    LOADED, {"position_valid": True, "carousel_phase": "LOAD"})

checks.equal(labels["4"], "[AVAILABLE]",
             "a loaded sample at the loader on a synchronized carousel "
             "may be measured")

labels = screen_module.action_labels(
    LOADED, {"position_valid": False, "carousel_phase": "LOAD"})

checks.ok("LOCKED" in labels["4"],
          "but the same sample on an UNKNOWN position is locked")
checks.ok("re-sync" in labels["4"].lower(),
          "and the label names the recovery, not just the refusal")


# ======================================================================
checks.section("the sensor reports which kind of not-working it is")

# WHAT BROKE
#   `sensor["boot_error"]`. The firmware sends `first_init_error`, and
#   has for the whole of 6.0.0, so both branches were unreachable and
#   the reason a sensor failed was dropped on every screen.

ready = {"sensor": {"state": "READY", "ready": True}}
missing = {"sensor": {"state": "UNAVAILABLE", "ready": False,
                      "first_init_error": {"code": "AS7265X_NOT_FOUND",
                                           "message": "no device"}}}
fresh = {"sensor": {"state": "NOT_INITIALIZED", "ready": False}}

checks.equal(ui_status.sensor_label(ready), "READY",
             "a working sensor is READY")
checks.equal(ui_status.sensor_label(missing),
             "UNAVAILABLE | AS7265X_NOT_FOUND",
             "a failed sensor names the CODE that explains it - this is "
             "the field that was read under the wrong key")
checks.equal(ui_status.sensor_label(fresh), "NOT INITIALIZED",
             "a sensor nothing has asked for yet is NOT INITIALIZED, "
             "which is not a fault and must not read as one")

# A live error outranks the boot error: the current problem is the one
# the operator is standing in front of.
both = {"sensor": {"state": "UNAVAILABLE", "ready": False,
                   "first_init_error": {"code": "BOOT_CODE"},
                   "current_error": {"code": "LIVE_CODE"}}}

checks.ok("LIVE_CODE" in ui_status.sensor_label(both),
          "a current error is reported in preference to a boot error")

# Firmware too old to send `state` must still produce something honest.
legacy = {"sensor": {"ready": False}}

checks.equal(ui_status.sensor_label(legacy), "UNAVAILABLE",
             "firmware without a state field falls back to the boolean")


sys.exit(checks.report())
