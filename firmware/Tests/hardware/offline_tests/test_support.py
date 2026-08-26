"""
Statistics, the operator, the profile, the port resolver and the
adapters' shape validation.

Small pieces, each of which decides a verdict somewhere, and each of
which is wrong in a way that would be invisible in a hardware run.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

if str(HERE.parent.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent.parent))

from hardware.adapters.sensor import (CHANNELS,              # noqa: E402
                                      SensorAdapter)
from hardware.configuration import ports as ports_module     # noqa: E402
from hardware.configuration.profile import (Profile,         # noqa: E402
                                            ProfileError,
                                            production_values)
from hardware.core.model import Aborted, Blocked             # noqa: E402
from hardware.core.operator import Operator                  # noqa: E402
from hardware.core.analysis import (byte_order_interpretations,  # noqa: E402
                                      centred_error,
                                      counts_to_degrees,
                                      degrees_to_counts,
                                      failure_rate, outliers,
                                      percentile, summarize)
from hardware.offline_tests.harness import Checks, cli       # noqa: E402


class _Console:
    """A scripted stdin and a captured stdout, for the operator tests."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.written = []

    def readline(self):
        if not self.answers:
            return ""

        return self.answers.pop(0) + "\n"

    def write(self, text):
        self.written.append(text)

    def flush(self):
        pass


def _operator(answers):
    console = _Console(answers)

    return Operator(None, interactive=True, stream_in=console,
                    stream_out=console), console


def run():
    checks = Checks("hardware/offline_tests/test_support.py")

    # ------------------------------------------------------------------
    checks.section("statistics")

    values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 100]

    distribution = summarize(values)

    checks.equal(distribution["n"], 10, "n counts every value")
    checks.equal(distribution["min"], 1.0, "min is the smallest")
    checks.equal(distribution["max"], 100.0, "max is the largest")
    checks.equal(distribution["median"], 5.5, "median is the middle")
    checks.equal(distribution["worst_abs"], 100.0,
                 "worst_abs is the largest magnitude")

    checks.ok(distribution["p95"] in values,
              "a nearest-rank p95 is a value that really occurred")

    checks.ok(distribution["p99"] in values,
              "a nearest-rank p99 is a value that really occurred")

    checks.equal(summarize([]), None,
                 "an empty series is None, not a distribution of zeros")

    with_none = summarize([1.0, None, 3.0])

    checks.equal(with_none["n"], 2,
                 "a None reading is dropped rather than counted as zero")

    checks.equal(with_none["dropped"], 1,
                 "and the number dropped is reported")

    checks.equal(summarize([5.0])["sd"], 0.0,
                 "one sample has a standard deviation of zero, not an "
                 "exception")

    checks.equal(percentile([], 0.95), None,
                 "a percentile of nothing is None")

    checks.equal(percentile([1.0], 0.99), 1.0,
                 "a percentile of one value is that value")

    checks.section("failure rates never round an intermittent fault away")

    rate = failure_rate([True] * 99 + [False])

    checks.equal(rate["all_passed"], False,
                 "99 passes and one failure is not all_passed")

    checks.equal(rate["first_failure_iteration"], 100,
                 "the first failing iteration is reported")

    checks.equal(rate["failed"], 1, "the failure is counted")

    early = failure_rate([False] + [True] * 99)

    checks.equal(early["first_failure_iteration"], 1,
                 "a failure at the start is distinguished from one at "
                 "the end")

    checks.equal(failure_rate([])["all_passed"], False,
                 "an empty run has not passed - it has not run")

    checks.equal(failure_rate([True, True])["all_passed"], True,
                 "an all-passing run is all_passed")

    checks.section("outliers are flagged, never removed")

    # Twenty steady readings and one wild one. A shorter series would
    # not flag it, and that is correct rather than a defect: with nine
    # samples the outlier inflates the standard deviation enough to hide
    # inside three of them, which is exactly why three sigma on a tiny
    # sample is a weak claim and why nothing here ever DROPS an outlier.
    flagged = outliers([1] * 20 + [50])

    checks.ok(any(o["index"] == 20 for o in flagged),
              "a far outlier in a long enough series is flagged with "
              "its index")

    checks.equal(outliers([1] * 8 + [50]), [],
                 "the same outlier in a short series is not claimed - "
                 "three sigma on nine samples proves little")

    checks.equal(outliers([1, 1]), [],
                 "too few values to judge produces no claim")

    checks.equal(outliers([2, 2, 2, 2]), [],
                 "a series with no spread has no outliers")

    checks.section("encoder arithmetic")

    checks.equal(counts_to_degrees(2048, 4096), 180.0,
                 "half a revolution is 180 degrees")

    checks.equal(degrees_to_counts(180.0, 4096), 2048,
                 "180 degrees is half a revolution")

    checks.equal(degrees_to_counts(45.0, 4096), 512,
                 "45 degrees is a quarter of a half turn")

    checks.equal(centred_error(4095 - 0, 4096), -1,
                 "the circular difference has no seam at 4095/0")

    checks.equal(centred_error(1, 4096), 1,
                 "a small forward difference is itself")

    checks.equal(centred_error(2048, 4096), 2048,
                 "exactly half a revolution stays positive")

    checks.equal(centred_error(2049, 4096), -2047,
                 "just past half a revolution is the short way back")

    checks.equal(centred_error(None, 4096), None,
                 "an unknown delta stays unknown")

    hint = byte_order_interpretations(8)

    checks.equal(hint["as_read"], 8, "the value as read is kept")
    checks.equal(hint["byte_swapped"], 2048,
                 "8 byte-swapped is 2048 - the H-002 hint that costs "
                 "nothing to print")

    checks.ok("diagnostic only" in hint["note"],
              "the hint says in words that it is not a measurement")

    # ------------------------------------------------------------------
    checks.section("the operator: validated, timestamped, refusable")

    operator, _console = _operator(["y"])

    checks.equal(operator.confirm("T", "yes?"), True, "y is yes")

    operator, _console = _operator(["n"])

    checks.equal(operator.confirm("T", "no?"), False, "n is no")

    operator, console = _operator(["maybe", "banana", "y"])

    checks.equal(operator.confirm("T", "eventually?"), True,
                 "an unparseable answer is re-asked, not guessed")

    checks.ok(any("answer y or n" in w for w in console.written),
              "and the operator is told what is acceptable")

    operator, _console = _operator([""])

    checks.equal(operator.confirm("T", "default?", default=True), True,
                 "a bare Enter takes an explicit default when one is "
                 "offered")

    operator, _console = _operator(["", "y"])

    checks.equal(operator.confirm("T", "no default"), True,
                 "with no default a bare Enter re-asks rather than "
                 "agreeing")

    operator, _console = _operator(["abort"])

    checks.raises(Aborted, lambda: operator.confirm("T", "abort?"),
                  "ABORT at a confirmation aborts the run")

    operator, _console = _operator([])

    checks.raises(Aborted, lambda: operator.confirm("T", "eof?"),
                  "end of input is an abort, never an empty answer")

    operator, _console = _operator(["180"])

    checks.equal(operator.number("T", "angle?"), 180.0,
                 "a number is parsed")

    operator, _console = _operator(["180,5"])

    checks.equal(operator.number("T", "angle?"), 180.5,
                 "a comma decimal separator is accepted - this is a "
                 "Czech bench")

    operator, _console = _operator(["unknown"])

    checks.equal(operator.number("T", "angle?"), None,
                 "UNKNOWN is a real answer and becomes None, never zero")

    operator, console = _operator(["9999", "180"])

    checks.equal(operator.number("T", "angle?", maximum=360), 180.0,
                 "a value outside the range is refused and re-asked")

    operator, console = _operator(["about 180ish", "180"])

    checks.equal(operator.number("T", "angle?"), 180.0,
                 "'about 180ish' is not an angle")

    operator, _console = _operator(["ccw"])

    checks.equal(operator.direction("T"), "CCW",
                 "a direction is normalized to upper case")

    operator, _console = _operator(["sideways", "NO_MOTION"])

    checks.equal(operator.direction("T"), "NO_MOTION",
                 "an unknown direction is refused; NO_MOTION is a real "
                 "answer")

    checks.section("non-interactive refuses rather than assuming")

    silent = Operator(None, interactive=False)

    for call, description in (
            (lambda: silent.confirm("T", "?"), "a confirmation"),
            (lambda: silent.number("T", "?"), "a measurement"),
            (lambda: silent.choice("T", "?", ("A", "B")), "a choice"),
            (lambda: silent.instruct("T", "do something"),
             "a physical instruction")):
        checks.raises(Blocked, call,
                      "{} is BLOCKED in non-interactive mode".format(
                          description))

    # ------------------------------------------------------------------
    checks.section("the profile reads production values, never copies")

    production = production_values()

    checks.equal(production["sensor"]["address"], 0x49,
                 "the AS7265x address comes from config.py")

    checks.equal(production["servo"]["tx_pin"], 17,
                 "the ST3215 TX pin comes from config.py")

    checks.equal(production["servo"]["rx_pin"], 16,
                 "the ST3215 RX pin comes from config.py")

    checks.equal(production["servo"]["counts_per_rev"], 4096,
                 "the encoder range comes from config.py")

    checks.equal(production["servo"]["position_tolerance"], 15,
                 "the position tolerance comes from config.py and is "
                 "NOT changed by this framework")

    checks.equal(production["carousel"]["slot_count"], 4,
                 "the slot count is read from the firmware - this bench "
                 "has four slots, not eight")

    checks.equal(production["carousel"]["slot_spacing_deg"], 90.0,
                 "and the spacing follows from it")

    checks.equal(production["carousel"]["half_turn_deg"], 180.0,
                 "the loader-to-scanner transfer is a half turn")

    checks.section("profile validation")

    checks.ok(not Profile({"port": "/dev/ttyUSB0"}).problems,
              "a profile with an explicit port validates")

    checks.ok(Profile({}).problems,
              "a profile with no selector at all does not validate")

    checks.ok(any("selector" in p for p in Profile({}).problems),
              "and the problem names the missing selector")

    checks.ok(Profile({"port": "x",
                       "motion": {"max_degrees_per_leg": 400}}).problems,
              "a motion envelope beyond half a revolution is refused - "
              "the driver cannot verify it")

    checks.ok(Profile({"port": "x",
                       "limits": {"max_open_cycles": 0}}).problems,
              "a zero iteration limit is refused")

    checks.ok(Profile({"port": "x",
                       "limits": {"max_open_cycles": -1}}).problems,
              "a negative iteration limit is refused")

    checks.ok(Profile({"port": "x",
                       "mechanism": {
                           "gear_ratio_servo_to_carousel": 0}}).problems,
              "a zero gear ratio is refused")

    checks.ok(Profile({"port": "x",
                       "mechanism": {
                           "provenance": "GUESSED"}}).problems,
              "an unknown provenance is refused - CONFIGURED, ASSUMED, "
              "MEASURED and VERIFIED are the only kinds")

    checks.ok(Profile({"port": "x",
                       "illumination": {
                           "sources": ["red"]}}).problems,
              "'red' is not an illumination source on this module - the "
              "hardware has white, uv and ir")

    checks.ok(Profile({"port": "x", "baudrate": -1}).problems,
              "a negative baud rate is refused")

    checks.ok(Profile({"usb_vid": "0x10C4", "usb_pid": "0xEA60"}).valid,
              "a VID/PID pair is a valid selector")

    checks.ok(Profile({"port": "x", "usb_vid": "not-a-word"}).problems,
              "a VID that is not a 16-bit value is refused")

    checks.raises(ProfileError,
                  lambda: Profile.load("/no/such/profile.json"),
                  "a missing profile file is refused with a message")

    example = Path(__file__).resolve().parents[1] / "configuration"

    loaded = Profile.load(example / "profile.example.json")

    checks.ok(loaded.valid,
              "the shipped example profile validates")

    checks.equal(loaded.slot_count, 4,
                 "the example profile takes the slot count from the "
                 "firmware rather than repeating it")

    checks.section("the port resolver refuses to guess")

    ports = [
        {"port": "/dev/ttyUSB0", "description": "CP2102",
         "hwid": "USB VID:PID=10C4:EA60 SER=AAAA"},
        {"port": "/dev/ttyUSB1", "description": "CP2102",
         "hwid": "USB VID:PID=10C4:EA60 SER=BBBB"},
    ]

    identity = ports_module.parse_hwid(ports[0]["hwid"])

    checks.equal(identity["vid"], 0x10C4, "the VID is parsed")
    checks.equal(identity["pid"], 0xEA60, "the PID is parsed")
    checks.equal(identity["serial"], "AAAA", "the serial number is parsed")

    checks.equal(ports_module.parse_hwid(None), {},
                 "an absent hwid parses to nothing rather than raising")

    checks.equal(ports_module.parse_hwid("nonsense"), {},
                 "an unparseable hwid parses to nothing")

    checks.raises(
        ports_module.PortError,
        lambda: ports_module.resolve(
            {"usb_vid": 0x10C4, "usb_pid": 0xEA60}, ports),
        "two boards of the same model is AMBIGUOUS, not a choice")

    resolved = ports_module.resolve({"usb_serial": "BBBB"}, ports)

    checks.equal(resolved["device"], "/dev/ttyUSB1",
                 "a USB serial number selects one board")

    checks.raises(
        ports_module.PortError,
        lambda: ports_module.resolve({"usb_serial": "CCCC"}, ports),
        "a selector matching nothing is an error, not a fallback")

    checks.raises(
        ports_module.PortError,
        lambda: ports_module.resolve({"port": "/dev/ttyUSB9"}, ports),
        "an explicit port the OS does not report is an error")

    # ------------------------------------------------------------------
    checks.section("the 54-feature contract")

    good = {channel: 1.0 for channel in CHANNELS}

    checks.equal(SensorAdapter.validate_spectrum(good), [],
                 "a complete 18-channel spectrum has no problems")

    missing = dict(good)
    missing.pop("A")

    checks.ok(any("missing" in p
                  for p in SensorAdapter.validate_spectrum(missing)),
              "a missing channel is named")

    extra = dict(good)
    extra["Z"] = 1.0

    checks.ok(any("unexpected" in p
                  for p in SensorAdapter.validate_spectrum(extra)),
              "an unexpected channel is named")

    nan = dict(good)
    nan["B"] = float("nan")

    checks.ok(any("NaN" in p
                  for p in SensorAdapter.validate_spectrum(nan)),
              "a NaN is caught")

    infinite = dict(good)
    infinite["C"] = float("inf")

    checks.ok(any("infinite" in p
                  for p in SensorAdapter.validate_spectrum(infinite)),
              "an infinity is caught")

    text = dict(good)
    text["D"] = "1.0"

    checks.ok(any("not a number" in p
                  for p in SensorAdapter.validate_spectrum(text)),
              "a string where a number belongs is caught")

    checks.ok(SensorAdapter.validate_spectrum([]),
              "a list where an object belongs is caught")

    block = {"illumination": "white", "acquisitions": [good, good],
             "data_ready_wait_ms": [400, 400]}

    checks.equal(SensorAdapter.validate_block(block, 2, "white"), [],
                 "a well-formed block has no problems")

    checks.ok(SensorAdapter.validate_block(block, 3, "white"),
              "two acquisitions where three were requested is a problem")

    checks.ok(SensorAdapter.validate_block(block, 2, "uv"),
              "a block answering with the wrong illumination is a "
              "problem")

    short_waits = dict(block)
    short_waits["data_ready_wait_ms"] = [400]

    checks.ok(SensorAdapter.validate_block(short_waits, 2, "white"),
              "a wait list shorter than the acquisition list is a "
              "problem")

    triad = {"illuminations": {
        name: {"illumination": name,
               "acquisitions": [
                   {c: 1.0 + offset for c in CHANNELS}],
               "data_ready_wait_ms": [400]}
        for offset, name in enumerate(("white", "uv", "ir"))}}

    checks.equal(SensorAdapter.validate_triad(triad, 1), [],
                 "a well-formed triad has no problems")

    checks.equal(SensorAdapter.feature_count(triad), 54,
                 "a complete triad carries 54 features")

    identical = {"illuminations": {
        name: {"illumination": name, "acquisitions": [dict(good)],
               "data_ready_wait_ms": [400]}
        for name in ("white", "uv", "ir")}}

    problems = SensorAdapter.validate_triad(identical, 1)

    checks.ok(any("identical" in p for p in problems),
              "three identical spectra are caught - that is what a lamp "
              "that never switched looks like")

    incomplete = {"illuminations": {
        "white": triad["illuminations"]["white"]}}

    checks.ok(SensorAdapter.validate_triad(incomplete, 1),
              "a triad missing two illuminations is caught")

    checks.equal(SensorAdapter.feature_count(incomplete), 18,
                 "and its feature count says so")

    return checks.report()


if __name__ == "__main__":
    sys.exit(cli(run, __doc__))
