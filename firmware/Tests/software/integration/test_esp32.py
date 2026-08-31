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
from pathlib import Path

# The shared scaffolding lives in firmware/Tests/. Walking up to it by
# name means this suite runs from any working directory and does not
# care how deep under Tests/software/ it sits.
_TESTS_DIR = next(
    p for p in Path(__file__).resolve().parents if p.name == "Tests"
)

if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))


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
checks.equal(len(data["commands"]), 26, "every command is registered")

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

# EVERY read-only servo command, against a connected driver.
#
# get_servo_calibration used to call servo.capabilities(), a method
# deleted with the multi-servo capability table. Nothing here exercised
# the command, so the suite stayed green while the command raised
# AttributeError against every real ST3215 on the bench. A handler that
# is never called by a test is a handler that is not tested.
for command in ("get_servo_calibration", "servo_diagnostics"):
    response = send(service, command)

    checks.ok(response["ok"],
              "{} answers against a connected servo".format(command))
    checks.ok(isinstance(response.get("data"), dict),
              "{} returns a data object".format(command))

calibration = send(service, "get_servo_calibration")["data"]

checks.equal(calibration["servo_type"], "st3215",
             "get_servo_calibration names the actuator")
checks.ok("capabilities" not in calibration,
          "and carries no capability table - that machinery is gone")
checks.ok("current" in calibration,
          "but does carry the tunables it exists to report")

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
    1, servo_module.INST_READ, [servo_module.REG_POSITION_FEEDBACK, 2])
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


# ======================================================================
checks.section("the 18-channel read does not repeat itself")
#
# CHANNEL_LAYOUT is ordered by wavelength, which groups the channels six
# at a time by the device that owns them. Selecting the device before
# every channel therefore asked for a device that was already selected
# fifteen times out of eighteen - 56 ms of bus traffic per acquisition,
# measured on the sensor.

main, service, config, _servo = build()
fake = service.fake_sensor

fake.device_selects = 0
fake.channel_reads = 0

spectrum = service.sensor.driver.read_all_channels()

import sensor as sensor_module

checks.equal(len(spectrum), 18, "all 18 channels are read")
checks.equal(sorted(spectrum), sorted(sensor_module.CHANNELS),
             "and they are exactly the channels the layout declares")
checks.equal(fake.device_selects, 3,
             "one device select per internal device, not one per channel")
checks.equal(fake.channel_reads, 18 * 4,
             "and every channel is still read as four register bytes")


# ======================================================================
checks.section("large responses survive a fragmented heap")
#
# REGRESSION. json.dumps allocates its result as ONE contiguous block,
# and MicroPython's collector never moves anything: measured on the
# board, an acquisition leaves 94784 bytes free with a largest block of
# 8 kB. The first triad after boot returned and every later one came
# back RESPONSE_TOO_LARGE after spending its full 24 s reading the
# sensor - the measurement existed and was discarded for want of a
# buffer. send_json now splits the payload instead of retrying the
# allocation that just failed.

main, service, config, fake = build()

import protocol as protocol_module

CHANNELS = ("A", "B", "C", "D", "E", "F", "G", "H", "I",
            "J", "K", "L", "R", "S", "T", "U", "V", "W")


def triad_shaped(repeats):
    """A payload the shape and size of a real WHITE/UV/IR response."""
    blocks = {}

    for offset, name in enumerate(("white", "uv", "ir")):
        blocks[name] = {
            "illumination": name,
            "repeats": repeats,
            "acquisitions": [
                {c: 1000.0 + offset + index + n * 1.25
                 for n, c in enumerate(CHANNELS)}
                for index in range(repeats)
            ],
            "data_ready_wait_ms": [123] * repeats,
        }

    return {"request_id": "1", "ok": True, "cmd": "acquire_triad",
            "data": {"illuminations": blocks, "repeats": repeats}}


payload = triad_shaped(5)

written = []
sink = protocol_module._ChunkSink(write=written.append, limit=10 ** 9)
protocol_module._emit_json(payload, sink)
sink.done()

checks.equal(json.loads("".join(written)), payload,
             "a response emitted through the sink parses back unchanged")
checks.equal(len(written), 1,
             "a healthy heap pays ONE dumps call and is never split - the "
             "fast path must not regress into piecework")

# The same object, forced down every branch: this is the deep-splitting
# path a badly fragmented heap actually takes.
class _NoRoom(dict):
    """A dict json.dumps cannot serialize whole."""


def _explode(obj):
    if isinstance(obj, dict):
        return _NoRoom({k: _explode(v) for k, v in obj.items()})

    if isinstance(obj, list):
        return [_explode(v) for v in obj]

    return obj


real_dumps = protocol_module.json.dumps
depth_calls = []


def _dumps_that_runs_out(obj, **kwargs):
    """Refuse every container, exactly as a heap with no large hole does."""
    if isinstance(obj, (dict, list, tuple)) and obj:
        depth_calls.append(1)

        raise MemoryError("no contiguous block")

    return real_dumps(obj, **kwargs)


protocol_module.json.dumps = _dumps_that_runs_out

deep = []
deep_sink = protocol_module._ChunkSink(write=deep.append, limit=256)

try:
    protocol_module._emit_json(payload, deep_sink)
    deep_sink.done()

finally:
    protocol_module.json.dumps = real_dumps

checks.ok(bool(depth_calls),
          "the fragmented-heap path was really exercised")
checks.equal(json.loads("".join(deep)), payload,
             "and a fully split response still parses to the same object")
checks.ok(len(deep) > 1,
          "a heap with no large hole yields many small pieces")
checks.ok(max(len(piece) for piece in deep) <= 256 + 64,
          "and no piece is much larger than one stdout chunk: largest "
          "was {}".format(max(len(piece) for piece in deep)))

# End to end through the real exit point: nothing may reach the console
# until serialization has finished, and what does reach it must be one
# parseable frame.
written = []
real_write_all = protocol_module._write_all
protocol_module._write_all = lambda text: written.append(text)
protocol_module.json.dumps = _dumps_that_runs_out

try:
    protocol_module.send_json(payload)

finally:
    protocol_module._write_all = real_write_all
    protocol_module.json.dumps = real_dumps

frame = "".join(written).strip()

checks.equal(json.loads(frame), payload,
             "send_json delivers the whole response when no large block "
             "can be allocated")
checks.ok(written[0] == "\n",
          "the guard newline still leads the frame")
checks.ok(written[-1] == "\n",
          "and the frame is still newline terminated")

# REGRESSION: a sink that runs out of room must not make the emitter
# serialize the same subtree twice. The first implementation guarded
# `sink.add(json.dumps(obj))` as one statement, so a MemoryError from
# the SINK - raised after the text was already stored - was read as "this
# subtree would not serialize" and the whole subtree was emitted again on
# top of the copy already held. On the board that turned a tight heap
# into an exhausted one.
class _SinkThatFillsUp(protocol_module._ChunkSink):
    def __init__(self, fail_after):
        protocol_module._ChunkSink.__init__(self, write=lambda text: None,
                                            limit=10 ** 9)
        self.fail_after = fail_after
        self.adds = 0

    def add(self, text):
        self.adds += 1

        if self.adds == self.fail_after:
            raise MemoryError("sink is full")

        protocol_module._ChunkSink.add(self, text)


sink = _SinkThatFillsUp(fail_after=1)
raised = None

try:
    protocol_module._emit_json(payload, sink)

except MemoryError as error:
    raised = error

checks.ok(raised is not None,
          "a sink that cannot store the text reports it, instead of "
          "silently re-serializing the same object")
checks.equal(sink.adds, 1,
             "and the emitter stops rather than emitting a second copy")

# Compact separators: same object, fewer bytes on a 115200 wire.
wide = json.dumps(payload)
tight = json.dumps(payload, separators=protocol_module._COMPACT)

checks.equal(json.loads(tight), json.loads(wide),
             "compact separators do not change the response")
checks.ok(len(tight) < len(wide),
          "and make it smaller: {} bytes against {}".format(
              len(tight), len(wide)))


sys.exit(checks.report())
