"""
ST3215 serial bus servo backend.

Three things are checked here, in order of how expensive they would be to
get wrong on hardware:

  1. THE WIRE PROTOCOL. Frame layout, checksum, register addresses and
     the sign conventions, each asserted against the official Waveshare
     memory table and the official SCServo reference implementation. A
     typo in a register address would move the wrong thing.

  2. VERIFIED MOVEMENT. Every movement reads the encoder before and
     after, and a movement that cannot be proved must raise rather than
     be assumed. The failure modes are injected, not hoped for.

  3. THE SUPPLIED REFERENCE DRIVER. st3215.c / st3215_regs.h were
     supplied as a cross-check. Where they agree with the official docs
     the agreement is asserted; where they disagree, the official
     behaviour is asserted and the divergence is named in the check
     description so it cannot be quietly "fixed" back.

Runs against support.FakeST3215, which speaks the real frame protocol -
the firmware builds real packets and parses real replies.
"""

import sys

import support
from support import Checks, FakeAS7265X, FakeST3215


def build(servo=None, select="st3215"):
    _main, module, config, fake = support.build_firmware(servo=servo)

    if select:
        response = support.command(module, "select_servo", servo=select)

        if not response["ok"]:
            return module, config, fake, response

    return module, config, fake, None


def main_tests():
    checks = Checks("ST3215 backend")

    # ==================================================================
    checks.section("1. frame protocol")

    support.load_esp32(FakeAS7265X(), FakeST3215())
    support.purge_esp32_modules()

    from drivers import st3215_registers as regs

    # 0xFF 0xFF ID LEN INST P... CKS, LEN = params + 2,
    # CKS = ~(ID + LEN + INST + sum(params)).
    ping = regs.build_packet(1, regs.INST_PING)

    checks.equal(
        list(ping), [0xFF, 0xFF, 0x01, 0x02, 0x01, 0xFB],
        "PING for servo 1 is FF FF 01 02 01 FB",
    )

    read_position = regs.build_packet(
        1, regs.INST_READ, bytes([regs.REG_PRESENT_POSITION, 2])
    )

    checks.equal(
        list(read_position),
        [0xFF, 0xFF, 0x01, 0x04, 0x02, 0x38, 0x02, 0xBE],
        "READ of 2 bytes at register 56 matches the reference frame",
    )

    checks.equal(
        regs.checksum(bytes([0x01, 0x02, 0x01])), 0xFB,
        "the checksum is the one's complement of the byte sum",
    )
    checks.equal(
        regs.checksum(bytes([0xFF, 0xFF])), 0x01,
        "and it wraps to one byte",
    )
    checks.equal(
        regs.build_packet(1, regs.INST_PING)[3], 2,
        "a frame with no parameters has length 2",
    )
    checks.equal(
        read_position[3], 4,
        "and a two-parameter frame has length 4",
    )

    # ==================================================================
    checks.section("2. register map, against the official memory table")

    # The supplied reference driver's st3215_regs.h lists the same
    # addresses, and its author reports verifying them on a real servo.
    # Both sources agree, so a mismatch here is a firmware typo.
    for name, address in (
        ("REG_ID", 5),
        ("REG_BAUD_RATE", 6),
        ("REG_RESPONSE_LEVEL", 8),
        ("REG_MIN_ANGLE_LIMIT", 9),
        ("REG_MAX_ANGLE_LIMIT", 11),
        ("REG_POSITION_CORRECTION", 31),
        ("REG_MODE", 33),
        ("REG_TORQUE_SWITCH", 40),
        ("REG_ACCELERATION", 41),
        ("REG_GOAL_POSITION", 42),
        ("REG_GOAL_TIME", 44),
        ("REG_GOAL_SPEED", 46),
        ("REG_TORQUE_LIMIT", 48),
        ("REG_LOCK", 55),
        ("REG_PRESENT_POSITION", 56),
        ("REG_PRESENT_SPEED", 58),
        ("REG_PRESENT_LOAD", 60),
        ("REG_PRESENT_VOLTAGE", 62),
        ("REG_PRESENT_TEMPERATURE", 63),
        ("REG_SERVO_STATUS", 65),
        ("REG_MOVING", 66),
        ("REG_PRESENT_CURRENT", 69),
    ):
        checks.equal(
            getattr(regs, name), address,
            "{} is memory table address {}".format(name, address),
        )

    checks.equal(
        regs.FEEDBACK_LENGTH, 15,
        "the feedback block is registers 56..70, read in one transaction",
    )
    checks.equal(regs.MODE_POSITION, 0, "mode 0 is position servo")
    checks.equal(regs.MODE_WHEEL, 1, "mode 1 is constant speed")
    checks.equal(regs.MODE_PWM, 2, "mode 2 is PWM open loop")
    checks.equal(regs.MODE_STEP, 3, "mode 3 is step servo")
    checks.equal(regs.BAUD_CODES[0], 1000000, "baud code 0 is 1 Mbps")
    checks.equal(regs.STEPS_PER_REV, 4096, "4096 steps per revolution")

    # The supplied driver names 128 "TORQUE_RELEASE_HERE" and calls it a
    # damped release. The official memory table and CalibrationOfs() both
    # say it rewrites the position offset so the shaft reads 2048. Acting
    # on the reference driver's description would silently move the
    # servo's zero while the operator thought they were releasing torque.
    checks.equal(
        regs.TORQUE_OFF, 0, "torque register 0 disables the output"
    )
    checks.equal(regs.TORQUE_ON, 1, "1 enables it")
    checks.equal(
        regs.TORQUE_SET_MIDDLE, 128,
        "and 128 is a MID-POINT CALIBRATION, not a release "
        "(supplied driver disagrees; official table wins)",
    )

    # The supplied driver maps bit 1 to "angle" and has no bit 4. The
    # official table gives the same bit order for three registers
    # (0x41 status, 0x13 unloading, 0x14 LED alarm), so it wins.
    checks.equal(
        regs.decode_status_flags(0x02), ["sensor"],
        "status bit 1 is SENSOR, not angle "
        "(supplied driver disagrees; official table wins)",
    )
    checks.equal(
        regs.decode_status_flags(0x10), ["angle"],
        "status bit 4 is ANGLE, which the supplied driver omits entirely",
    )
    checks.equal(
        regs.decode_status_flags(0x21), ["voltage", "overload"],
        "several alarm bits decode together",
    )
    checks.equal(regs.decode_status_flags(0), [], "and zero means healthy")

    # ==================================================================
    checks.section("3. value encodings")

    # Sign-magnitude, NOT two's complement. The supplied st3215_move()
    # takes an unsigned goal and so cannot express a negative step at
    # all - which in step servo mode means no reverse movement.
    checks.equal(regs.encode_signed(512), 512, "+512 encodes as 512")
    checks.equal(
        regs.encode_signed(-512), 512 | 0x8000,
        "-512 encodes as the magnitude with bit 15 set",
    )
    checks.equal(
        regs.decode_signed(512 | 0x8000), -512, "and decodes back",
    )
    checks.equal(
        regs.decode_signed(300 | (1 << 10), 10), -300,
        "load uses bit 10 as its sign bit, which both sources confirm",
    )
    checks.equal(
        regs.word_bytes(0x1234), bytes([0x34, 0x12]),
        "words go on the wire little-endian, as the memory table says",
    )
    checks.equal(
        regs.bytes_word(0x34, 0x12), 0x1234, "and come back the same way"
    )

    # Angular arithmetic on a seamless axis.
    checks.close(regs.counts_to_degrees(4096), 360.0, "4096 counts is 360 deg")
    checks.close(regs.counts_to_degrees(1024), 90.0, "1024 counts is 90 deg")
    checks.close(regs.counts_to_degrees(2048), 180.0, "2048 counts is 180 deg")
    checks.close(regs.counts_to_degrees(512), 45.0, "512 counts is 45 deg")
    checks.equal(regs.degrees_to_counts(0), 0, "0 deg is 0 counts")
    checks.equal(regs.degrees_to_counts(45), 512, "45 deg is 512 counts")
    checks.equal(regs.degrees_to_counts(90), 1024, "90 deg is 1024 counts")
    checks.equal(regs.degrees_to_counts(180), 2048, "180 deg is 2048 counts")
    checks.equal(regs.degrees_to_counts(360), 4096, "360 deg is a whole turn")
    checks.equal(regs.degrees_to_counts(-90), -1024, "and it keeps the sign")
    checks.equal(
        regs.degrees_to_counts(0.0879), 1,
        "one encoder count is about 0.088 deg",
    )

    checks.equal(regs.centred_error(16), 16, "a small difference is itself")
    checks.equal(
        regs.centred_error(-4080), 16,
        "and going the long way round is the same 16 counts",
    )
    checks.equal(
        regs.centred_error(4080), -16, "symmetrically the other way"
    )
    checks.equal(
        regs.centred_error(2048), -2048,
        "exactly half a turn resolves deterministically",
    )
    checks.equal(regs.wrap_counts(4096), 0, "4096 wraps to 0")
    checks.equal(regs.wrap_counts(-1), 4095, "-1 wraps to 4095")

    # ==================================================================
    checks.section("4. configuration is namespaced and official")

    import config

    checks.equal(config.ST3215_UART_ID, 2, "the servo is on UART2")
    checks.equal(config.ST3215_TX_PIN, 17, "servo TX is GPIO17")
    checks.equal(config.ST3215_RX_PIN, 16, "servo RX is GPIO16")
    checks.equal(config.ST3215_BAUD, 1000000, "at the factory 1 Mbps")
    checks.equal(config.ST3215_SERVO_ID, 1, "the factory servo ID is used")
    checks.equal(config.ST3215_COUNTS_PER_REV, 4096, "4096 counts per turn")
    checks.equal(
        config.ST3215_MODE, regs.MODE_STEP,
        "the carousel runs the servo in step servo mode",
    )
    checks.equal(
        config.ST3215_COUNTS_PER_SLOT, 1024,
        "one slot is 4096 / 4 = 1024 counts, derived not typed",
    )
    checks.equal(
        config.ST3215_HALF_TURN_COUNTS, 2048,
        "and half a turn is 2048",
    )
    checks.ok(
        config.ST3215_SPEED <= 3400,
        "the goal speed is inside the servo's 3400 steps/s limit",
    )
    checks.ok(
        config.ST3215_ACCELERATION <= 254,
        "and the acceleration is inside its 254 limit",
    )

    # ==================================================================
    checks.section("5. bring-up moves nothing")

    servo = FakeST3215(position=2048)
    module, config, fake, failure = build(servo)

    checks.ok(failure is None, "the ST3215 backend can be selected")
    checks.equal(fake.goals, [], "no goal position was written to select it")
    checks.equal(
        module.servos.servo_type, "st3215", "and it is the active backend"
    )

    backend = module.servos.servo

    checks.ok(backend.ready, "the backend reports itself ready")
    checks.equal(
        backend.read_position(), 2048, "the encoder is readable"
    )

    capabilities = backend.capabilities()

    checks.ok(capabilities["encoder"], "it advertises an encoder")
    checks.ok(
        capabilities["position_feedback"], "and position feedback"
    )
    checks.ok(
        capabilities["verified_movement"], "and verified movement"
    )
    checks.ok(capabilities["telemetry"], "and telemetry")
    checks.ok(
        not capabilities["timed_positioning"],
        "and explicitly NOT timed positioning",
    )

    # ==================================================================
    checks.section("6. verified movement")

    from drivers import st3215 as st3215_module

    result = backend.move_relative(1024)

    checks.equal(fake.goals, [1024], "one slot is commanded as 1024 counts")
    checks.equal(result["start_position"], 2048, "the movement starts at 2048")
    checks.equal(result["actual_position"], 3072, "and ends at 3072")
    checks.equal(result["position_error"], 0, "with no position error")
    checks.ok(result["within_tolerance"], "so it is within tolerance")
    checks.ok(result["settled"], "and the servo reported it had stopped")
    checks.ok(result["verified"], "the record says it was verified")
    checks.close(
        result["requested_degrees"], 90.0, "reported as 90 degrees"
    )

    # The goal block is written in ONE transaction starting at register
    # 41: acceleration, position, time, speed. Both the official
    # WritePosEx and the supplied st3215_move build exactly this frame.
    goal_writes = [
        write for write in fake.writes
        if write[0] == support.SERVO_REG_ACC
    ]

    checks.equal(len(goal_writes), 1, "one goal transaction was sent")
    checks.equal(
        len(goal_writes[0][1]), 7,
        "carrying all seven bytes: acc, position, time, speed",
    )
    checks.equal(
        goal_writes[0][1][0], config.ST3215_ACCELERATION,
        "the configured acceleration is used",
    )
    checks.equal(
        goal_writes[0][1][5] | (goal_writes[0][1][6] << 8),
        config.ST3215_SPEED,
        "and the configured speed",
    )
    checks.equal(
        goal_writes[0][1][3] | (goal_writes[0][1][4] << 8), 0,
        "goal time stays zero - it belongs to PWM mode only",
    )

    # ==================================================================
    checks.section("7. the encoder seam is a non-event")

    fake.position = 4000
    result = backend.move_relative(512)

    checks.equal(
        result["expected_position"], 416,
        "stepping +512 from 4000 crosses the seam to 416",
    )
    checks.equal(result["actual_position"], 416, "and the encoder agrees")
    checks.equal(
        result["position_error"], 0, "with no error across the boundary"
    )
    checks.equal(
        fake.goals[-1], 512,
        "the servo was asked for 512 counts, not 3584 the other way",
    )

    fake.position = 100
    result = backend.move_relative(-512)

    checks.equal(
        result["actual_position"], 3684,
        "stepping -512 from 100 wraps down through zero",
    )
    checks.equal(result["position_error"], 0, "and still verifies")
    checks.equal(fake.goals[-1], -512, "as a negative relative step")

    checks.raises(
        st3215_module.ST3215Error,
        lambda: backend.move_relative(2049),
        "a movement beyond half a turn is refused, not half-verified",
    )

    # ==================================================================
    checks.section("8. telemetry decoding")

    feedback = backend.read_feedback()

    checks.equal(feedback["voltage_v"], 7.4, "voltage is 0.1 V per count")
    checks.equal(feedback["temperature_c"], 32, "temperature is degrees C")
    checks.ok("load_permille" in feedback, "load is reported")
    checks.ok("current_ma" in feedback, "current is reported in mA")
    checks.ok("speed_steps_per_s" in feedback, "speed is reported")
    checks.equal(
        feedback["status_flags"], [], "no alarms on a healthy bus"
    )

    # A servo reporting an alarm is surfaced, not swallowed.
    fake.registers[support.SERVO_REG_STATUS] = 0x24

    feedback = backend.read_feedback()

    checks.equal(
        sorted(feedback["status_flags"]), ["overload", "temperature"],
        "an alarm byte is decoded into named conditions",
    )

    fake.registers[support.SERVO_REG_STATUS] = 0

    # ==================================================================
    checks.section("9. failures never look like success")

    # The mechanism stops short of the target.
    module, config, fake, _ = build(FakeST3215(position=2048, short_by=200))

    from drivers import st3215 as st3215_module

    checks.raises(
        st3215_module.ST3215PositionError,
        lambda: module.servos.servo.move_relative(1024),
        "stopping short raises a position mismatch",
    )

    # The servo never reports arrival.
    module, config, fake, _ = build(
        FakeST3215(position=2048, polls_to_finish=None)
    )

    from drivers import st3215 as st3215_module

    checks.raises(
        st3215_module.ST3215MoveTimeoutError,
        lambda: module.servos.servo.move_relative(1024),
        "a movement that never finishes times out",
    )

    # A relative goal must NEVER be resent: if the servo acted on it but
    # the acknowledgement was lost, a retry would step twice.
    module, config, fake, _ = build(
        FakeST3215(position=2048, drop_goal_ack=True)
    )

    from drivers import st3215 as st3215_module

    checks.raises(
        st3215_module.ST3215TimeoutError,
        lambda: module.servos.servo.move_relative(1024),
        "a lost acknowledgement on the goal write fails the movement",
    )
    checks.equal(
        len(fake.goals), 1,
        "and the relative goal was written exactly once, never retried",
    )

    # An ordinary transport glitch, on the other hand, IS retried.
    module, config, fake, _ = build(FakeST3215(position=2048, drop_replies=1))

    checks.equal(
        module.servos.servo.read_position(), 2048,
        "a single dropped reply is retried and the read succeeds",
    )
    checks.ok(
        module.servos.servo.stats["retry"] >= 1,
        "and the retry is counted, not hidden",
    )

    # ==================================================================
    checks.section("10. a broken bus is diagnosed, not guessed at")

    # Nothing on the bus at all.
    module, config, fake, failure = build(FakeST3215(silent=True))

    checks.ok(
        failure is not None,
        "selecting a servo that does not answer fails the selection",
    )
    checks.equal(
        failure["error"]["code"], "SERVO_NOT_FOUND",
        "with SERVO_NOT_FOUND rather than a generic error",
    )
    checks.equal(
        module.servos.servo_type, None,
        "and nothing is left connected",
    )

    response = support.command(module, "get_status")

    checks.ok(response["ok"], "get_status still answers without a servo")
    checks.ok(
        not response["data"]["servo"]["selected"],
        "reporting the servo as NOT SELECTED",
    )

    # A corrupted frame.
    module, config, fake, failure = build(FakeST3215(corrupt_checksum=True))

    checks.ok(failure is not None, "a corrupt bus fails selection too")
    checks.equal(
        failure["error"]["code"], "SERVO_CHECKSUM_ERROR",
        "and a bad checksum is named as such, not as a timeout",
    )

    # An ID clash: another servo answers.
    module, config, fake, failure = build(FakeST3215(answer_as=7))

    checks.ok(failure is not None, "an answer from the wrong ID fails")
    checks.equal(
        failure["error"]["code"], "SERVO_PROTOCOL_ERROR",
        "reported as a protocol error naming the foreign ID",
    )
    checks.ok(
        "7" in failure["error"]["message"],
        "and the message says which ID answered",
    )

    # The wrong operating mode.
    module, config, fake, _ = build(FakeST3215(mode=1))

    from drivers import st3215 as st3215_module

    checks.raises(
        st3215_module.ST3215ModeError,
        lambda: module.servos.servo.move_relative(1024),
        "the firmware refuses to move a servo in the wrong mode",
    )

    diagnostics = support.command(module, "servo_diagnostics")["data"]

    checks.ok(not diagnostics["ok"], "diagnostics fail on the wrong mode")
    checks.ok(not diagnostics["moved"], "and diagnostics never move anything")
    checks.equal(
        diagnostics["error"]["code"], "SERVO_MODE_ERROR",
        "with SERVO_MODE_ERROR",
    )

    # ==================================================================
    checks.section("11. diagnostics and EPROM service")

    module, config, fake, _ = build(FakeST3215(position=1000))

    diagnostics = support.command(module, "servo_diagnostics")

    checks.ok(diagnostics["ok"], "servo_diagnostics succeeds")
    checks.ok(diagnostics["data"]["ok"], "and reports a healthy link")
    checks.ok(not diagnostics["data"]["moved"], "without moving anything")
    checks.equal(
        [entry["step"] for entry in diagnostics["data"]["steps"]],
        ["uart", "ping", "id", "baud_code", "mode", "torque",
         "angle_limits", "feedback"],
        "every stage is reported separately, so a failure names itself",
    )
    checks.equal(
        diagnostics["data"]["baud_reported"], 1000000,
        "the servo's own baud setting is read back",
    )
    checks.equal(fake.goals, [], "diagnostics wrote no goal position")

    # EPROM writes are a SERVICE operation: confirmed, never routine.
    refused = support.command(module, "servo_configure")

    checks.ok(not refused["ok"], "servo_configure refuses without confirm")
    checks.equal(
        refused["error"]["code"], "CONFIRMATION_REQUIRED",
        "with CONFIRMATION_REQUIRED",
    )
    checks.equal(
        [w for w in fake.writes if w[0] == support.SERVO_REG_LOCK], [],
        "and touched no EPROM lock",
    )

    # Ordinary carousel synchronization must not write EPROM either.
    support.command(module, "sync_position", load_slot=1)

    checks.equal(
        [w for w in fake.writes if w[0] == support.SERVO_REG_LOCK], [],
        "carousel synchronization writes no persistent servo state",
    )

    configured = support.command(module, "servo_configure", confirm=True)

    checks.ok(configured["ok"], "servo_configure works when confirmed")
    checks.equal(
        configured["data"]["mode"], 3, "setting step servo mode"
    )

    lock_writes = [
        write for write in fake.writes if write[0] == support.SERVO_REG_LOCK
    ]

    checks.equal(
        [write[1][0] for write in lock_writes], [0, 1],
        "EPROM is unlocked, written, then locked again",
    )
    checks.equal(
        fake._get_word(support.SERVO_REG_MAX_ANGLE), 0,
        "and both angle limits are cleared, as step mode requires",
    )

    # ==================================================================
    checks.section("12. movement tests and torque")

    module, config, fake, _ = build(FakeST3215(position=2048))

    refused = support.command(module, "servo_test_move", kind="out_and_back")

    checks.ok(not refused["ok"], "servo_test_move refuses without confirm")
    checks.equal(
        refused["error"]["code"], "CONFIRMATION_REQUIRED",
        "with CONFIRMATION_REQUIRED",
    )
    checks.equal(fake.goals, [], "and nothing moved")

    support.command(module, "sync_position", load_slot=1)

    kinds = [
        name for name, _label in module.servos.servo.test_move_kinds()
    ]

    checks.ok(
        "wrap" in kinds,
        "the ST3215 offers the encoder boundary test",
    )
    checks.ok(
        "out_and_back" in kinds, "and the measurement-path test"
    )

    tested = support.command(
        module, "servo_test_move", kind="slot_out_and_back", confirm=True
    )

    checks.ok(tested["ok"], "the confirmed out-and-back test runs")
    checks.equal(tested["data"]["net_counts"], 0, "with no net travel")
    checks.equal(
        tested["data"]["closed_loop_error_counts"], 0,
        "and it closes exactly back on itself",
    )
    checks.ok(
        not tested["data"]["position_invalidated"],
        "so the tracked position survives a symmetrical VERIFIED test",
    )
    checks.ok(
        tested["data"]["carousel"]["position_valid"],
        "and really is still valid",
    )

    tested = support.command(
        module, "servo_test_move", kind="slot_forward", confirm=True
    )

    checks.ok(
        tested["data"]["position_invalidated"],
        "a one-way test does invalidate the position",
    )

    # The 4095 -> 0 boundary, at the level the operator runs it.
    fake.position = 4000
    support.command(module, "sync_position", load_slot=1)

    tested = support.command(module, "servo_test_move", kind="wrap",
                             confirm=True)

    checks.ok(tested["ok"], "the wrap-around test runs")

    for movement in tested["data"]["movements"]:
        checks.ok(
            abs(movement["requested_counts"]) <= 2048,
            "every leg of the wrap test stays inside half a turn",
        )

    checks.equal(
        tested["data"]["worst_position_error"], 0,
        "and crossing 4095 -> 0 lands exactly",
    )

    # Torque is explicit, and releasing it drops the position.
    support.command(module, "sync_position", load_slot=1)

    released = support.command(module, "servo_torque", enable=False)

    checks.ok(released["ok"], "torque can be released")
    checks.equal(fake.torque, 0, "the servo really is released")
    checks.ok(
        not released["data"]["carousel"]["position_valid"],
        "and a hand-turnable carousel has no trusted position",
    )
    checks.equal(
        [w for w in fake.writes if w[0] == support.SERVO_REG_TORQUE
         and w[1][0] == 128],
        [],
        "releasing torque writes 0, never 128 - 128 recalibrates the zero",
    )

    support.command(module, "servo_torque", enable=True)

    checks.equal(fake.torque, 1, "and it can be held again")

    # Stopping holds position but drops tracking.
    support.command(module, "sync_position", load_slot=1)

    stopped = support.command(module, "servo_stop")

    checks.ok(stopped["ok"], "servo_stop answers")
    checks.equal(fake.torque, 1, "the servo is still holding after a stop")
    checks.ok(
        not stopped["data"]["carousel"]["position_valid"],
        "but an aborted movement invalidates the position",
    )

    # ==================================================================
    checks.section("14. the bus scan finds what the error message cannot")

    class ScannableBus:
        """
        A bus that only answers at ONE baud rate, ID and pin order.

        That is the whole point of the scan: the operator has checked the
        wiring and measured the supply, so what is left is which of the
        four assumptions - ID, baud, pin order, servo power - is wrong.
        This fake lets each of them be wrong on its own.
        """

        def __init__(self, servo_id=1, baud=1000000, tx=17, rx=16,
                     echo=False, answers=True, corrupt=False):
            self.servo_id = servo_id
            self.baud = baud
            self.tx = tx
            self.rx = rx
            self.echo = echo
            self.answers = answers
            self.corrupt = corrupt

            self.link = None
            self.pending = bytearray()
            self.instructions = []

        def configure_link(self, uart_id, baudrate, tx, rx):
            self.link = (uart_id, baudrate, tx, rx)
            self.pending = bytearray()

        def _matches(self):
            if self.link is None:
                return False

            _uart_id, baudrate, tx, rx = self.link

            return (
                baudrate == self.baud
                and getattr(tx, "number", tx) == self.tx
                and getattr(rx, "number", rx) == self.rx
            )

        def write(self, data):
            data = bytes(data)

            self.instructions.append(data[4] if len(data) > 4 else None)

            if self.echo:
                self.pending.extend(data)

            if not self.answers or not self._matches():
                return len(data)

            if data[2] != self.servo_id:
                return len(data)

            body = bytes([self.servo_id, 2, 0])
            frame = b"\xff\xff" + body + bytes([regs.checksum(body)])

            if self.corrupt:
                frame = frame[:-1] + bytes([(frame[-1] + 1) & 0xFF])

            self.pending.extend(frame)

            return len(data)

        def read(self, count=None):
            if not self.pending:
                return b""

            take = len(self.pending) if count is None else min(
                count, len(self.pending)
            )
            chunk = bytes(self.pending[:take])
            del self.pending[:take]

            return chunk

        def any(self):
            return len(self.pending)

    def scan_with(bus, **kwargs):
        support.load_esp32(FakeAS7265X(), bus)
        support.purge_esp32_modules()

        from drivers import st3215 as driver

        return driver.bus_scan(**kwargs)

    # Everything as configured: found, and nothing to change.
    report = scan_with(ScannableBus())

    checks.equal(report["result"], "SERVO_FOUND", "a healthy bus is found")
    checks.ok(report["ok"], "and the scan reports ok")
    checks.ok(
        report["matches_config"],
        "and says the configuration already matches",
    )
    checks.equal(report["found"][0]["id"], 1, "at the configured ID")
    checks.equal(
        report["found"][0]["baud"], 1000000, "and the configured baud",
    )
    checks.ok(not report["moved"], "the scan moves nothing")

    # A ping is a read. Nothing else may ever be sent by a scan.
    bus = ScannableBus()
    scan_with(bus)

    checks.equal(
        set(bus.instructions), {regs.INST_PING},
        "the scan sends nothing but PING - no write, no goal position",
    )

    # Wrong baud rate: found anyway, and named.
    report = scan_with(ScannableBus(baud=115200))

    checks.equal(
        report["result"], "SERVO_FOUND", "a servo at another baud is found",
    )
    checks.ok(
        not report["matches_config"], "and flagged as not matching config",
    )
    checks.equal(
        report["found"][0]["baud"], 115200, "at the rate it actually uses",
    )
    checks.ok(
        any("ST3215_BAUD" in text for text in report["differences"]),
        "and the constant to change is named",
    )

    # TX and RX swapped: the one fault an operator cannot see by looking.
    report = scan_with(ScannableBus(tx=16, rx=17))

    checks.equal(
        report["result"], "SERVO_FOUND", "swapped wires still find it",
    )
    checks.ok(
        any("exchanged" in text for text in report["differences"]),
        "and the scan says the wires are exchanged",
    )

    # A servo whose ID was changed. The configured ID finds nothing; the
    # ID sweep is what turns "no answer" into "answers as ID 2".
    report = scan_with(ScannableBus(servo_id=2))

    checks.equal(
        report["result"], "SILENT_BUS",
        "pinging only the configured ID finds a renumbered servo nowhere",
    )

    report = scan_with(ScannableBus(servo_id=2), servo_ids=[1, 2, 3])

    checks.equal(
        report["result"], "SERVO_FOUND", "sweeping IDs finds it",
    )
    checks.equal(report["found"][0]["id"], 2, "and reports the real ID")
    checks.ok(
        any("ST3215_SERVO_ID" in text for text in report["differences"]),
        "naming the constant that has to change",
    )

    # A half-duplex adapter echoing us, with nothing on the bus.
    report = scan_with(ScannableBus(echo=True, answers=False))

    checks.equal(
        report["result"], "ECHO_ONLY",
        "an echo with no reply is diagnosed as a servo-side fault",
    )
    checks.ok(
        not report["ok"], "which is still a failure",
    )
    checks.ok(
        "power" in report["diagnosis"],
        "and points at servo power rather than at the wiring",
    )

    # Frames arriving with a bad checksum: grounding, not a servo fault.
    report = scan_with(ScannableBus(corrupt=True))

    checks.equal(
        report["result"], "CORRUPT_TRAFFIC",
        "checksum failures are diagnosed separately",
    )
    checks.ok(
        "ground" in report["diagnosis"],
        "and named as a grounding problem",
    )

    # Nothing at all - the case the operator started from.
    report = scan_with(ScannableBus(answers=False))

    checks.equal(
        report["result"], "SILENT_BUS", "a silent bus is diagnosed as such",
    )
    checks.equal(
        len(report["probes"]), 16,
        "after trying 8 baud rates in both pin orders",
    )
    checks.ok(
        "power" in report["diagnosis"] and "connector" in report["diagnosis"],
        "and sends the operator to the servo connector, not the supply",
    )

    # The scan must run with NOTHING selected: that is when it is needed.
    support.load_esp32(FakeAS7265X(), ScannableBus(answers=False))
    support.purge_esp32_modules()

    _main, module, _config, _fake = support.build_firmware(
        servo=ScannableBus(answers=False)
    )

    response = support.command(module, "servo_bus_scan")

    checks.ok(
        response["ok"],
        "servo_bus_scan runs with no servo selected - which is the point",
    )
    checks.equal(
        response["data"]["result"], "SILENT_BUS",
        "and returns the diagnosis",
    )
    checks.ok(
        not response["data"]["moved"], "having moved nothing",
    )

    # ==================================================================
    checks.section("13. the driver knows nothing about the carousel")

    source = (support.ESP32_DIR / "drivers" / "st3215.py").read_text(
        encoding="utf-8"
    )

    # "slot" is legitimate here: moving whole slots is part of the
    # actuator interface the carousel needs. What must NOT appear is
    # anything about samples, the loader, the scanner or the science.
    for token in ("sample_id", "Sample ID", "loader", "scanner",
                  "database", "spectrum", "AS7265"):
        checks.ok(
            token not in source,
            "the ST3215 driver never mentions '{}'".format(token),
        )

    for token in ("import carousel", "from control", "from protocol"):
        checks.ok(
            token not in source,
            "and never imports upward ('{}')".format(token),
        )

    return checks.report()


if __name__ == "__main__":
    sys.exit(main_tests())
