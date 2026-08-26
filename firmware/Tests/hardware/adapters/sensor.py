"""
The AS7265x, through the production command surface.

WHAT THE SHIPPED SYSTEM GIVES

    sensor_test_raw     initialize (optionally forced) and acquire,
                        with the I2C scan result reachable afterwards
                        through get_status -> sensor.last_scan
    acquire_block       one illumination, N times, every reading intact
    acquire_triad       WHITE then UV then IR - the production order
    led_test            the illumination sources, for the operator to
                        look at
    get_status          state, address, bus description, last scan,
                        settings, first init error, current error,
                        recovery count - and it touches NOTHING, so it
                        is safe to poll

THE ONE GAP: an on-demand I2C bus scan. `scan_bus` runs only inside
initialization, so the address list in `get_status` is from the last
init rather than from now. A test can still force one with
`sensor_test_raw(force_reinit=True)`, and that is what B6 does - but
"scan the bus without touching the sensor" is not available and the
test that wants it is registered BLOCKED.

DATA SHAPE IS THE POINT OF B7. 18 channels per illumination, three
illuminations, 54 features. This adapter's `validate_block` is where
missing channels, duplicates, wrong lengths, NaN and infinity are
caught - because a spectrum that is the wrong SHAPE is a communication
failure, and it must never reach Science to be discovered there.
"""

import math

from .base import Adapter, Capability, firmware_commands, pc_command_surface


# The 18 calibrated channels, in the firmware's wavelength order. This
# is a CONTRACT the test side checks the device against, which is why it
# is written out rather than imported: importing sensor.py needs the
# `machine` module, and a contract that is read from the thing it is
# checking is not a contract.
CHANNELS = ("A", "B", "C", "D", "E", "F",
            "G", "H", "I", "J", "K", "L",
            "R", "S", "T", "U", "V", "W")

ILLUMINATIONS = ("white", "uv", "ir")

FEATURES_PER_ILLUMINATION = len(CHANNELS)
TOTAL_FEATURES = FEATURES_PER_ILLUMINATION * len(ILLUMINATIONS)


class SensorAdapter(Adapter):
    """Acquisition, initialization and the shape of what comes back."""

    name = "sensor"

    def __init__(self, context, link):
        super().__init__(context)

        self.link = link

    # ------------------------------------------------------------------

    def _detect(self):
        surface = pc_command_surface()
        commands = firmware_commands()

        found = {}

        found["sensor.status"] = self.from_commands(
            "sensor.status", ["get_status"],
            "add get_status to firmware/ESP32/protocol.py",
            surface, ["get_status"],
        )

        found["sensor.init"] = self.from_commands(
            "sensor.init", ["sensor_test_raw"],
            "add sensor_test_raw to firmware/ESP32/protocol.py",
            surface, ["sensor_test_raw"],
        )

        found["sensor.acquire_block"] = self.from_commands(
            "sensor.acquire_block", ["acquire_block"],
            "add acquire_block to firmware/ESP32/protocol.py",
            surface, ["acquire_block"],
        )

        found["sensor.acquire_triad"] = self.from_commands(
            "sensor.acquire_triad", ["acquire_triad"],
            "add acquire_triad to firmware/ESP32/protocol.py",
            surface, ["acquire_triad"],
        )

        found["sensor.led_test"] = self.from_commands(
            "sensor.led_test", ["led_test"],
            "add led_test to firmware/ESP32/protocol.py",
            surface, ["led_test"],
        )

        found["sensor.i2c_scan_on_demand"] = Capability(
            "sensor.i2c_scan_on_demand", "i2c_scan" in commands,
            reason="the I2C bus is scanned only during sensor "
                   "initialization; get_status reports the LAST scan, "
                   "not a fresh one",
            recommendation=(
                "Add a read-only `i2c_scan` command to "
                "firmware/ESP32/protocol.py that calls the existing "
                "sensor.scan_bus(i2c) and returns the addresses found, "
                "without initializing or configuring anything. It is a "
                "few lines, it moves nothing and it would let a bench "
                "test tell 'the sensor is absent' from 'the bus is "
                "dead' without disturbing a sensor that is working. "
                "Until then HW-B6-001 is BLOCKED and HW-B6-002 covers "
                "the same ground through a forced re-initialization."),
        )

        return found

    # ------------------------------------------------------------------
    # operations
    # ------------------------------------------------------------------

    def status(self):
        """Sensor state without touching the sensor."""
        data = self.link.request("get_status", retries=2)["data"]

        return (data or {}).get("sensor") or {}

    def initialize(self, force=True, repeats=1):
        """
        Bring the sensor up, optionally from scratch, and read once.

        `force_reinit=True` is what makes the I2C scan run again, which
        is the only way to observe the bus from the PC side.
        """
        return self.link.request(
            "sensor_test_raw", timeout=self._measure_timeout(), retries=1,
            force_reinit=bool(force), repeats=int(repeats))

    def acquire(self, illumination, repeats):
        """One illumination, N readings, nothing aggregated on the device."""
        if illumination not in ILLUMINATIONS + ("dark",):
            raise ValueError(
                "illumination must be one of {}, got {!r}".format(
                    ", ".join(ILLUMINATIONS + ("dark",)), illumination))

        return self.link.request(
            "acquire_block", timeout=self._measure_timeout(), retries=1,
            illumination=illumination, repeats=int(repeats))

    def triad(self, repeats=None):
        """WHITE, UV, IR - the production order, in one command."""
        payload = {}

        if repeats is not None:
            payload["repeats"] = int(repeats)

        return self.link.request(
            "acquire_triad", timeout=self._measure_timeout(), retries=1,
            **payload)

    def led_test(self, hold_ms=400):
        self.context.require_hardware_mode("switch on the illumination")

        limit = self.context.profile.data["illumination"]["max_hold_ms"]

        hold = min(int(hold_ms), int(limit))

        return self.link.request(
            "led_test", timeout=self._measure_timeout(), hold_ms=hold)

    # ------------------------------------------------------------------
    # shape validation - the whole of B7's first half
    # ------------------------------------------------------------------

    @staticmethod
    def validate_spectrum(spectrum, where="spectrum"):
        """
        Every way one 18-channel reading can be wrong, as sentences.

        An empty list is a valid spectrum; anything else is a defect
        with a name. NaN and infinity are checked explicitly because
        JSON has no literal for either and a device that produces one
        has already done something unexpected on the way.
        """
        problems = []

        if not isinstance(spectrum, dict):
            return ["{}: expected an object of channel -> value, got "
                    "{}".format(where, type(spectrum).__name__)]

        missing = [c for c in CHANNELS if c not in spectrum]
        extra = [c for c in spectrum if c not in CHANNELS]

        if missing:
            problems.append("{}: missing channels {}".format(
                where, ", ".join(missing)))

        if extra:
            problems.append("{}: unexpected channels {}".format(
                where, ", ".join(map(str, extra))))

        if len(spectrum) != FEATURES_PER_ILLUMINATION and not (
                missing or extra):
            problems.append(                           # pragma: no cover
                "{}: {} channels, expected {}".format(
                    where, len(spectrum), FEATURES_PER_ILLUMINATION))

        for channel in CHANNELS:
            if channel not in spectrum:
                continue

            value = spectrum[channel]

            if isinstance(value, bool) or not isinstance(
                    value, (int, float)):
                problems.append("{}: channel {} is {!r}, not a "
                                "number".format(where, channel, value))

                continue

            if math.isnan(value):
                problems.append("{}: channel {} is NaN".format(
                    where, channel))

            elif math.isinf(value):
                problems.append("{}: channel {} is infinite".format(
                    where, channel))

        return problems

    @classmethod
    def validate_block(cls, block, expected_repeats=None,
                       illumination=None):
        """
        One `acquire_block` answer, checked end to end.

        Checks the envelope as well as the spectra: the illumination
        that came back must be the one asked for, and the number of
        acquisitions must be the number requested. A device that quietly
        returns two readings when three were asked for has failed, even
        though every individual reading is perfect.
        """
        problems = []

        if not isinstance(block, dict):
            return ["acquire_block returned {}, not an object".format(
                type(block).__name__)]

        if illumination is not None:
            answered = block.get("illumination")

            if answered != illumination:
                problems.append(
                    "asked for {} illumination, the answer says "
                    "{!r}".format(illumination, answered))

        acquisitions = block.get("acquisitions")

        if not isinstance(acquisitions, list):
            problems.append(
                "'acquisitions' is {}, expected a list".format(
                    type(acquisitions).__name__))

            return problems

        if expected_repeats is not None and len(
                acquisitions) != expected_repeats:
            problems.append(
                "{} acquisitions returned, {} were requested".format(
                    len(acquisitions), expected_repeats))

        for index, spectrum in enumerate(acquisitions):
            problems.extend(cls.validate_spectrum(
                spectrum, "acquisition {}".format(index + 1)))

        waits = block.get("data_ready_wait_ms")

        if isinstance(waits, list) and len(waits) != len(acquisitions):
            problems.append(
                "{} data_ready_wait_ms entries for {} "
                "acquisitions".format(len(waits), len(acquisitions)))

        return problems

    @classmethod
    def validate_triad(cls, report, expected_repeats=None):
        """
        The 54-feature answer: three illuminations, 18 channels each.

        Duplicate detection is here rather than per block: two
        illuminations returning byte-identical spectra is the signature
        of a lamp that never switched, and it is invisible when each
        block is checked on its own.
        """
        problems = []

        blocks = (report or {}).get("illuminations")

        if not isinstance(blocks, dict):
            return ["'illuminations' is {}, expected an object with "
                    "white, uv and ir".format(type(blocks).__name__)]

        for name in ILLUMINATIONS:
            if name not in blocks:
                problems.append("no {} block in the answer".format(name))

                continue

            problems.extend(cls.validate_block(
                blocks[name], expected_repeats, illumination=name))

        firsts = {}

        for name in ILLUMINATIONS:
            block = blocks.get(name) or {}
            acquisitions = block.get("acquisitions") or []

            if acquisitions and isinstance(acquisitions[0], dict):
                firsts[name] = tuple(
                    acquisitions[0].get(c) for c in CHANNELS)

        names = sorted(firsts)

        for index, first in enumerate(names):
            for second in names[index + 1:]:
                if firsts[first] == firsts[second]:
                    problems.append(
                        "the {} and {} spectra are identical to the last "
                        "digit, which is what a lamp that never switched "
                        "looks like".format(first, second))

        return problems

    @staticmethod
    def feature_count(report):
        """How many spectral features a triad answer actually carried."""
        blocks = (report or {}).get("illuminations") or {}

        total = 0

        for name in ILLUMINATIONS:
            acquisitions = (blocks.get(name) or {}).get("acquisitions")

            if isinstance(acquisitions, list) and acquisitions:
                first = acquisitions[0]

                if isinstance(first, dict):
                    total += len([c for c in CHANNELS if c in first])

        return total

    # ------------------------------------------------------------------

    def _measure_timeout(self):
        configured = self.context.profile.get("measure_timeout_s")

        if configured:
            return float(configured)

        return getattr(self.link.module, "MEASURE_TIMEOUT", 180.0)
