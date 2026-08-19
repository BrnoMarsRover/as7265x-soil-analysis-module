"""
ESP32: the protocol, the drivers and the carousel, on fake hardware.

Runs the REAL firmware modules on CPython against a fake AS7265x that
speaks the actual virtual-register protocol and a fake ST3215 that
speaks the actual packet format. Nothing production is reimplemented
here, so a driver bug shows up as a failing check rather than as a
passing check against a stub that shares the bug.

The two properties this suite exists for:

    THE PROTOCOL COMES UP WITHOUT WORKING PERIPHERALS. A missing
    sensor, an unpowered servo and an unsynchronized carousel must each
    leave ping, get_status and the diagnostics answering - because
    those are how an operator finds out which of them is the problem.

    NOTHING MOVES ON A GUESS. No servo at boot, no position until it is
    declared, no movement once position confidence is lost.

Run:  py test_esp32.py
"""

import json
import sys

import support

checks = support.Checks("esp32")


def build(**kwargs):
    return support.build_firmware(**kwargs)


def send(service, cmd, **payload):
    return support.command(service, cmd, **payload)


# ======================================================================
checks.section("protocol framing")

main, service, config, servo = build()

response = send(service, "ping")
checks.ok(response["ok"], "ping succeeds")
checks.equal(response["request_id"], "1", "request_id is echoed back")
checks.equal(response["cmd"], "ping", "the command is named in the answer")
checks.ok(response["data"]["pong"] is True, "pong is true")

response = service.dispatch({"request_id": "abc", "cmd": "ping"})
checks.equal(response["request_id"], "abc",
             "a non-numeric request_id survives unchanged")

response = service.dispatch({"cmd": "ping"})
checks.ok(response["ok"], "a missing request_id is not an error")
checks.equal(response["request_id"], None, "and comes back as null")

response = service.dispatch({"request_id": "1", "cmd": "no_such_command"})
checks.ok(not response["ok"], "an unknown command is refused")
checks.equal(response["error"]["code"], "UNKNOWN_COMMAND",
             "with UNKNOWN_COMMAND")
checks.ok("ping" in response["error"]["message"],
          "and the message lists what IS known")

response = service.dispatch(["not", "an", "object"])
checks.ok(not response["ok"], "a JSON array is refused")
checks.equal(response["error"]["code"], "INVALID_REQUEST",
             "with INVALID_REQUEST")

frames = []
support.capture_frames(service, frames)

service.process_line("{not json at all")
checks.equal(len(frames), 1, "malformed JSON still produces exactly one frame")
checks.equal(frames[0]["error"]["code"], "INVALID_JSON", "with INVALID_JSON")

frames.clear()
service.process_line("x" * (config.MAX_COMMAND_BYTES + 10))
checks.equal(frames[0]["error"]["code"], "COMMAND_TOO_LONG",
             "an oversized line is refused before it is parsed")

frames.clear()
service.process_line(json.dumps({"request_id": "9", "cmd": "ping"}))
checks.equal(len(frames), 1, "one request produces exactly one frame")
checks.equal(frames[0]["request_id"], "9", "and it is the answer to it")


# ======================================================================
checks.section("identity and geometry are reported by the firmware")

identity = send(service, "ping")["data"]

checks.equal(identity["firmware"], config.FIRMWARE_NAME, "firmware name")
checks.equal(identity["version"], config.FIRMWARE_VERSION, "firmware version")
checks.equal(identity["protocol_version"], config.PROTOCOL_VERSION,
             "protocol version")
checks.equal(identity["slot_count"], 4, "4 slots")
checks.close(identity["slot_angle_deg"], 90.0, "90 degrees per slot")
checks.close(identity["scanner_offset_deg"], 180.0, "180 degree offset")
checks.equal(identity["scanner_offset_slots"], 2, "which is 2 slots")
checks.ok("ST3215" in identity["servo"], "the servo is named")
checks.equal(identity["sensor"], "AS7265x", "the sensor is named")


# ======================================================================
checks.section("the protocol serves without any working peripheral")

absent_sensor = support.FakeAS7265X(absent_scans=10 ** 6)
main, cold, config, _servo = build(device=absent_sensor,
                                   bring_up_sensor=False)

checks.ok(send(cold, "ping")["ok"],
          "ping answers with no sensor and no servo")

status = send(cold, "get_status")
checks.ok(status["ok"], "get_status answers too")

data = status["data"]
checks.equal(data["sensor"]["state"], "NOT_INITIALIZED",
             "the sensor reports NOT_INITIALIZED before anything needs it")
checks.ok(not data["servo"]["connected"], "no servo is connected at boot")
checks.ok(not data["carousel"]["position_valid"],
          "the carousel position is not valid at boot")
checks.equal(len(data["commands"]), 25, "every command is registered")

# Reaching for the sensor fails, and leaves the protocol alone.
response = send(cold, "measure_raw", slot=1)
checks.ok(not response["ok"], "measuring without a synchronized carousel fails")

status = send(cold, "get_status")["data"]
checks.ok(send(cold, "ping")["ok"], "ping still answers after the failure")

test = send(cold, "sensor_test_raw")
checks.ok(test["ok"], "the sensor diagnostic itself answers")
checks.ok(not test["data"]["ok"],
          "and reports the sensor as unusable in its payload")
checks.equal(test["data"]["failed_stage"], "I2C_SCAN",
             "naming the stage that failed")
checks.equal(send(cold, "get_status")["data"]["sensor"]["state"],
             "UNAVAILABLE",
             "after a real attempt the sensor reads UNAVAILABLE, which is "
             "a different thing from NOT_INITIALIZED")

checks.ok(send(cold, "ping")["ok"],
          "and ping STILL answers, which is the whole point")


# ======================================================================
checks.section("nothing moves before the servo is connected")

main, service, config, fake_servo = build()

for command, payload in (
    ("select_slot", {"slot": 2}),
    ("move_slots", {"direction": "cw", "slots": 1}),
    ("sync_position", {"load_slot": 1}),
):
    response = send(service, command, **payload)
    checks.equal(response["error"]["code"], "SERVO_NOT_CONNECTED",
                 "{} is refused with SERVO_NOT_CONNECTED".format(command))

checks.equal(fake_servo.packets, [],
             "and not one packet reached the servo")

response = send(service, "connect_servo")
checks.ok(response["ok"], "connect_servo brings the link up")
checks.ok(send(service, "get_status")["data"]["servo"]["connected"],
          "and the status says so")

response = send(service, "disconnect_servo")
checks.ok(response["ok"], "disconnect_servo releases it")
checks.ok(not send(service, "get_status")["data"]["servo"]["connected"],
          "and movement is blocked again")


# ======================================================================
checks.section("ST3215 packet format")

sys.path.insert(0, str(support.ESP32_DIR))
support.purge_esp32_modules()

import servo as servo_module  # noqa: E402

checks.equal(servo_module.checksum([0x01, 0x02, 0x01]), 0xFB,
             "checksum is the one's complement of the byte sum")
checks.equal(servo_module.checksum([0xFF]), 0x00,
             "a sum of 0xFF checksums to 0x00")

packet = servo_module.build_packet(1, servo_module.INST_PING)
checks.equal(list(packet[:2]), [0xFF, 0xFF], "two header bytes")
checks.equal(packet[2], 1, "the servo id")
checks.equal(packet[3], 2, "length covers the instruction and the checksum")
checks.equal(packet[4], servo_module.INST_PING, "the instruction")
checks.equal(packet[-1], servo_module.checksum(list(packet[2:-1])),
             "and a valid trailing checksum")

read = servo_module.build_packet(
    1, servo_module.INST_READ, [servo_module.REG_PRESENT_POSITION, 2])
checks.equal(read[3], 4, "a two-parameter read has length 4")

checks.equal(servo_module.encode_signed(-100), 0x8064,
             "negative goals are sign-magnitude, not two's complement")
checks.equal(servo_module.encode_signed(100), 100, "positive goals are plain")
checks.equal(servo_module.decode_signed(0x8064), -100, "and decode back")
checks.equal(servo_module.decode_signed(100), 100, "both ways")

checks.equal(servo_module.word_bytes(0x1234), bytes([0x34, 0x12]),
             "16-bit registers are little-endian on the STS series")
checks.equal(servo_module.bytes_word(0x34, 0x12), 0x1234, "and read back")

flags = servo_module.decode_status_flags(0x11)
checks.equal(sorted(flags), ["angle", "voltage"],
             "the official alarm bit order is used")
checks.equal(servo_module.decode_status_flags(0), [],
             "no bits set means no alarms")

checks.close(servo_module.counts_to_degrees(1024), 90.0,
             "1024 counts is 90 degrees")
checks.close(servo_module.counts_to_degrees(2048), 180.0,
             "2048 counts is 180 degrees")
checks.equal(servo_module.degrees_to_counts(90.0), 1024,
             "and 90 degrees is 1024 counts")
checks.equal(servo_module.centred_error(4000, 4096), -96,
             "an error across the encoder seam is measured the short way")


# ======================================================================
checks.section("ST3215 transport faults are named, not swallowed")

main, service, config, fake = build(servo=support.FakeST3215(silent=True))

response = send(service, "connect_servo")
checks.ok(not response["ok"], "a servo that never answers refuses to connect")
checks.ok(response["error"]["code"].startswith("SERVO_"),
          "with a servo-specific code, not a generic failure")

main, service, config, fake = build(servo=support.FakeST3215(answer_as=7))
response = send(service, "connect_servo")
checks.ok(not response["ok"],
          "a reply from the wrong servo id is not this servo answering")

main, service, config, fake = build(servo=support.FakeST3215(corrupt_checksum=True))
response = send(service, "connect_servo")
checks.ok(not response["ok"], "a corrupted reply is refused")
checks.ok("CHECKSUM" in response["error"]["code"]
          or "PROTOCOL" in response["error"]["code"],
          "and named as a checksum or protocol fault")


# ======================================================================
checks.section("carousel geometry and position confidence")

main, service, config, fake = build()
send(service, "connect_servo")

status = send(service, "get_status")["data"]["carousel"]
checks.equal(status["slot_count"], 4, "the carousel has 4 slots")
checks.ok(not status["position_valid"],
          "and no trusted position until it is declared")

response = send(service, "sync_position", load_slot=1)
checks.ok(response["ok"], "sync_position declares the origin")

status = send(service, "get_status")["data"]["carousel"]
checks.ok(status["position_valid"], "the position is now trusted")
checks.equal(status["current_load_slot"], 1, "Slot 1 is at the loader")
checks.equal(status["current_scan_slot"], 3,
             "which puts Slot 3 at the scanner - 2 slots, 180 degrees")

for load_slot, scan_slot in ((1, 3), (2, 4), (3, 1), (4, 2)):
    send(service, "sync_position", load_slot=load_slot)
    status = send(service, "get_status")["data"]["carousel"]

    checks.equal(status["current_scan_slot"], scan_slot,
                 "loader {} means scanner {}".format(load_slot, scan_slot))

# The mapping is its own inverse, which is what a half-turn offset means.
send(service, "sync_position", scan_slot=3)
checks.equal(send(service, "get_status")["data"]["carousel"]
             ["current_load_slot"], 1,
             "declaring the scanner slot gives the same answer both ways")

for slot in (0, 5, 8, -1, "two", None):
    response = send(service, "select_slot", slot=slot)
    checks.ok(not response["ok"], "slot {!r} is refused".format(slot))

send(service, "sync_position", load_slot=1)
response = send(service, "select_slot", slot=2)
checks.ok(response["ok"], "slot 2 is selected")
checks.close(response["data"]["move"]["degrees"], 90.0,
             "one slot is 90 degrees of movement")

send(service, "sync_position", load_slot=1)
send(service, "select_slot", slot=1)
before = send(service, "get_status")["data"]["carousel"]

response = send(service, "servo_torque", enable=False)
checks.ok(response["ok"], "torque can be released")
checks.ok(not send(service, "get_status")["data"]["carousel"]
          ["position_valid"],
          "releasing torque invalidates the position - a carousel that "
          "can be turned by hand has no position the firmware can vouch for")

response = send(service, "select_slot", slot=2)
checks.ok(not response["ok"],
          "and movement is refused until it is re-synchronized")


# ======================================================================
checks.section("fine alignment does not renumber slots")

main, service, config, fake = build()
send(service, "connect_servo")
send(service, "sync_position", load_slot=1)
send(service, "select_slot", slot=1)

before = send(service, "get_status")["data"]["carousel"]

response = send(service, "fine_adjust", degrees=3.0)
checks.ok(response["ok"], "a small fine adjustment is accepted")

after = send(service, "get_status")["data"]["carousel"]
checks.equal(after["selected_slot"], before["selected_slot"],
             "the selected slot is unchanged")
checks.equal(after["current_load_slot"], before["current_load_slot"],
             "the loader slot is unchanged")
checks.equal(after["current_scan_slot"], before["current_scan_slot"],
             "the scanner slot is unchanged")
checks.equal(after["slot_count"], 4, "and the geometry is still 4 slots")

response = send(service, "fine_adjust",
                degrees=config.MAX_FINE_ADJUST_DEG + 1)
checks.ok(not response["ok"],
          "an adjustment larger than the limit is refused - fine alignment "
          "is never a hidden whole-slot move")


# ======================================================================
checks.section("acquisition")

main, service, config, fake = build()
send(service, "connect_servo")
send(service, "sync_position", load_slot=1)
send(service, "select_slot", slot=1)

response = send(service, "measure_raw", slot=1)
checks.ok(response["ok"], "measure_raw succeeds")

data = response["data"]
checks.equal(sorted(data["illuminations"]), ["ir", "uv", "white"],
             "all three illuminations are acquired")

for name, block in data["illuminations"].items():
    first = (block.get("acquisitions") or [{}])[0]

    checks.equal(len(first), 18,
                 "{} carries 18 channels".format(name))

checks.equal(data["slot_id"], 1, "the slot is reported")
checks.ok(data["home_restored"],
          "and the sample is returned to the loading position")

status = send(service, "get_status")["data"]["carousel"]
checks.equal(status["carousel_phase"], "LOAD",
             "the carousel ends where it started")

# A sensor that fails must not leave the sample at the scanner.
main, service, config, fake = build(
    device=support.FakeAS7265X(data_ready=False))
send(service, "connect_servo")
send(service, "sync_position", load_slot=1)
send(service, "select_slot", slot=1)

response = send(service, "measure_raw", slot=1)
checks.ok(not response["ok"], "an acquisition failure is reported")
checks.ok("return_move" in (response.get("data") or {})
          or response["data"].get("moved") is False,
          "and the answer says what happened to the mechanism")

# Refusing early is better than moving and then refusing.
main, service, config, fake = build(
    device=support.FakeAS7265X(absent_scans=10 ** 6), bring_up_sensor=False)
send(service, "connect_servo")
send(service, "sync_position", load_slot=1)
send(service, "select_slot", slot=1)

before = send(service, "get_status")["data"]["carousel"]["carousel_phase"]
response = send(service, "measure_raw", slot=1)
after = send(service, "get_status")["data"]["carousel"]["carousel_phase"]

checks.ok(not response["ok"], "measuring with no sensor fails")
checks.equal(after, before,
             "and the carousel never moved - the sensor is checked BEFORE "
             "a sample is swung to the scanner")


sys.exit(checks.report())
