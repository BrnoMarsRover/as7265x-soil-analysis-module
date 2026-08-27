"""
The bench profile: what is plugged in, and what is only assumed.

THREE KINDS OF NUMBER, AND THEY ARE NOT INTERCHANGEABLE

    CONFIGURED   what firmware/ESP32/config.py ships. True by
                 definition: it is what the code will do.
    ASSUMED      what we believe the mechanism is. 4096 counts per
                 revolution, 180 degrees from loader to scanner. NOT
                 measured. H-002 exists because at least one of these
                 may be wrong.
    MEASURED     what a hardware test actually observed, with the run
                 that observed it.

`Provenance` keeps them apart in the profile, in the manifest and in
every report. The distinction is the entire reason this file exists:
`ST3215_COUNTS_PER_REV = 4096` in config.py is a CONFIGURED value and
also an ASSUMED physical fact, and a campaign that cannot tell those
apart cannot investigate H-002 at all.

PRODUCTION VALUES ARE IMPORTED, NEVER COPIED. `firmware/ESP32/config.py`
is loaded by path and read. A second copy of the tolerance in a test
profile would drift from the shipped one, and then the campaign would be
qualifying a number the firmware does not use.

NO DEFAULT PORT. `/dev/ttyUSB0` is not a device, it is a guess: Linux
renumbers on reconnect, and a campaign that opens the wrong board is
worse than one that refuses to start. The profile carries a SELECTOR -
by-id path, USB VID/PID, serial number - and the selector is resolved,
at run time, to exactly one device or to an error.
"""

import importlib.util
import json
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
HARDWARE_DIR = HERE.parent
TESTS_DIR = HARDWARE_DIR.parent
FIRMWARE_DIR = TESTS_DIR.parent
REPO_ROOT = FIRMWARE_DIR.parent

ESP32_CONFIG = FIRMWARE_DIR / "ESP32" / "config.py"

EXAMPLE_PROFILE = HERE / "profile.example.json"


class Provenance:
    CONFIGURED = "CONFIGURED"
    ASSUMED = "ASSUMED"
    MEASURED = "MEASURED"
    VERIFIED = "VERIFIED"

    ALL = (CONFIGURED, ASSUMED, MEASURED, VERIFIED)


class ProfileError(Exception):
    """The profile is unusable, and the message says which field."""


# ======================================================================
# the production configuration, read rather than duplicated
# ======================================================================

_PRODUCTION = None


def production_config():
    """
    `firmware/ESP32/config.py`, loaded by path, cached.

    Loaded under a unique module name because three different `config`
    modules exist in this repository (ESP32, Science, research/erc) and
    a plain `import config` would resolve to whichever happened to be on
    sys.path first. config.py itself imports nothing and defines only
    constants, so loading it is free of side effects - unlike sensor.py
    or servo.py, which import `machine` and cannot be loaded on CPython
    at all.
    """
    global _PRODUCTION

    if _PRODUCTION is not None:
        return _PRODUCTION

    if not ESP32_CONFIG.is_file():
        raise ProfileError(
            "the production firmware configuration is missing: {}. The "
            "hardware profile is derived from it and cannot be "
            "invented.".format(ESP32_CONFIG))

    spec = importlib.util.spec_from_file_location(
        "freya_production_config", ESP32_CONFIG)

    module = importlib.util.module_from_spec(spec)

    # Registered before exec so that a config which ever grows a
    # self-import still works, and removed from the ordinary import
    # namespace afterwards is not needed - the name is unique.
    sys.modules["freya_production_config"] = module

    try:
        spec.loader.exec_module(module)

    except Exception as error:
        raise ProfileError(
            "firmware/ESP32/config.py could not be read: {}: {}".format(
                type(error).__name__, error))

    _PRODUCTION = module

    return module


def production_values():
    """Every production constant the hardware campaign depends on."""
    config = production_config()

    return {
        "firmware_name": config.FIRMWARE_NAME,
        "firmware_version": config.FIRMWARE_VERSION,
        "protocol_version": config.PROTOCOL_VERSION,
        "acquisition_protocol_version": config.ACQUISITION_PROTOCOL_VERSION,

        "sensor": {
            "i2c_bus": config.I2C_BUS,
            "i2c_sda_pin": config.I2C_SDA_PIN,
            "i2c_scl_pin": config.I2C_SCL_PIN,
            "i2c_frequency_hz": config.I2C_FREQ,
            "address": config.AS7265X_ADDRESS,
            "address_hex": "0x{:02X}".format(config.AS7265X_ADDRESS),
            "integration_cycles": config.SENSOR_INTEGRATION_CYCLES,
            "gain": config.SENSOR_GAIN,
            "measurement_mode": config.SENSOR_MEASUREMENT_MODE,
            "max_repeats": config.MAX_REPEATS,
            "sample_repeats": config.SAMPLE_REPEATS,
            "calibration_repeats": config.CALIBRATION_REPEATS,
            "illumination_settle_ms": config.ILLUMINATION_SETTLE_MS,
            "operation_budget_ms": config.SENSOR_OPERATION_BUDGET_MS,
            "init_attempts": config.SENSOR_INIT_ATTEMPTS,
        },

        "servo": {
            "uart_id": config.ST3215_UART_ID,
            "tx_pin": config.ST3215_TX_PIN,
            "rx_pin": config.ST3215_RX_PIN,
            "baud": config.ST3215_BAUD,
            "servo_id": config.ST3215_SERVO_ID,
            "counts_per_rev": config.ST3215_COUNTS_PER_REV,
            "counts_per_slot": config.ST3215_COUNTS_PER_SLOT,
            "half_turn_counts": config.ST3215_HALF_TURN_COUNTS,
            "mode": config.ST3215_MODE,
            "speed": config.ST3215_SPEED,
            "acceleration": config.ST3215_ACCELERATION,
            "position_tolerance": config.ST3215_POSITION_TOLERANCE,
            "settle_ms": config.ST3215_SETTLE_MS,
            "poll_interval_ms": config.ST3215_POLL_INTERVAL_MS,
            "move_timeout_ms": config.ST3215_MOVE_TIMEOUT_MS,
            "timeout_ms": config.ST3215_TIMEOUT_MS,
            "retries": config.ST3215_RETRIES,
        },

        "carousel": {
            "slot_count": config.CAROUSEL_SLOT_COUNT,
            "slot_spacing_deg": config.CAROUSEL_SLOT_GEOMETRY_DEG,
            "scan_load_offset_slots": config.CAROUSEL_SCAN_LOAD_OFFSET,
            "half_turn_deg": config.CAROUSEL_HALF_TURN_DEG,
            "forward_direction": config.CAROUSEL_FORWARD_DIRECTION,
            "max_fine_adjust_deg": config.MAX_FINE_ADJUST_DEG,
        },

        "protocol": {
            "max_command_bytes": config.MAX_COMMAND_BYTES,
        },
    }


# ======================================================================
# the profile
# ======================================================================

DEFAULTS = {
    "name": "unnamed bench",
    "description": "",

    # Port selection. Every one of these is optional; at least one must
    # be present, and the resolver refuses an ambiguous match.
    "port": None,
    "port_by_id": None,
    "usb_vid": None,
    "usb_pid": None,
    "usb_serial": None,

    "baudrate": None,               # None -> production DEFAULT_BAUDRATE
    "command_timeout_s": None,
    "connect_timeout_s": None,
    "measure_timeout_s": None,

    # Bounded limits for anything repeated. Present so that an operator
    # can lower them on a bench with a fragile mechanism, and never
    # raise them past the framework's own ceiling without saying so.
    "limits": {
        "max_open_cycles": 2000,
        "max_requests": 20000,
        "max_measurements": 5000,
        "max_movements": 5000,
        "max_missions": 200,
    },

    # What the mechanism is BELIEVED to be. Overridable per bench,
    # because a second module may be geared differently, and every one
    # is stamped ASSUMED until a measurement says otherwise.
    "mechanism": {
        "gear_ratio_servo_to_carousel": 1.0,
        "loading_to_scanner_deg": 180.0,
        "slot_count": None,          # None -> production value
        "provenance": "ASSUMED",
    },

    # Motion envelope for the diagnostic campaigns. Never larger than
    # what the production driver will accept per leg.
    "motion": {
        "max_degrees_per_leg": 180.0,
        "safe_speed": None,          # None -> production ST3215_SPEED
        "safe_acceleration": None,
    },

    "illumination": {
        "max_hold_ms": 2000,
        "sources": ["white", "uv", "ir"],
    },

    # THE PHYSICAL UNIT UNDER TEST.
    #
    # A prerequisite PASS earned on one module says nothing about
    # another, so the layer gates are only sound if the module is
    # identified. Empty by default and empty is honest: an unnamed bench
    # cannot open a gate for a named one.
    "unit": {
        "module_id": None,
        "esp32_id": None,
        "servo_id_tag": None,
        "servo_model": "ST3215",
        "sensor_id_tag": None,
        "carousel_assembly_id": None,
        "carousel_assembly_revision": None,
    },

    # INSTRUMENTS. Every electrical and thermal test needs one, and an
    # unidentified uncalibrated instrument produces an anecdote. A test
    # whose instrument is absent here is BLOCKED, by name.
    #
    # Each entry: {"model": ..., "serial": ..., "calibrated": "YYYY-MM-DD"}
    "instruments": {
        "multimeter": None,
        "oscilloscope": None,
        "logic_analyzer": None,
        "thermal_probe": None,
        "current_probe": None,
    },

    # FIXTURES. Physical things a test needs that are not instruments.
    "fixtures": {
        # A bounded, documented mass that stands in for a soil sample.
        # NEVER a jam or stall fixture - see the safety note in B4.
        "representative_load": None,

        # A stable reflectance target for acquisition repeatability.
        "reference_target": None,

        # An independent angular reference - a protractor, an index
        # mark, a dial gauge - for the carousel rotation tests.
        "rotation_reference": None,

        # A camera or photodiode able to see the IR source. Without it
        # an IR observation can only be UNKNOWN.
        "ir_observer": None,

        # Whether the carousel plate can be detached from the output
        # shaft, which is what separates encoder error from coupling
        # slip in H-002.
        "carousel_detachable": None,
    },

    # THE TEST-SIDE DIAGNOSTIC FIRMWARE, when it has been deployed.
    # Deployment is manual and deliberate; this records that it
    # happened, so the run fingerprint can say which firmware answered.
    "diagnostic_firmware": {
        "deployed": False,
        "version": None,
        "sha256": None,
        "deployed_utc": None,
        "production_firmware_sha256": None,
    },

    # THE BENCH POWER TOPOLOGY, as it actually is.
    #
    # Documentation/ARCHITECTURE.md states the servo has its own supply
    # at the driver board and shares ground with the ESP32, and that
    # servo current must not pass through the sensor PCB. What THIS
    # bench does has to be written down before a current measurement
    # means anything.
    "power": {
        "topology_confirmed": False,
        "regulated_input_v": None,
        "sensor_rail_v": None,
        "servo_supply_v": None,
        "servo_supply_shared_ground": None,
        "servo_current_through_sensor_pcb": None,
    },

    # ACCEPTANCE THRESHOLDS, each with its provenance.
    #
    # Numbers like "4 counts" and "8 counts" were scattered through the
    # test bodies as bare literals. A bare literal is an acceptance
    # criterion nobody can question, so each one moved here and each one
    # says where it came from. A threshold whose source is NONE may only
    # produce CHARACTERIZATION - see core/requirements.py.
    "thresholds": {
        "direction_symmetry_counts": {
            "value": 4, "unit": "counts", "source": "NONE",
            "rationale": "A working figure, not a requirement. Until "
                         "HW-B4-001 has measured both directions there "
                         "is nothing authoritative to compare against.",
            "requirement_id": "HW-REQ-SERVO-014",
            "provenance": "ASSUMED",
        },
        "angle_growth_counts": {
            "value": 8, "unit": "counts", "source": "NONE",
            "rationale": "Whether error grows with angle is the "
                         "question; this bound only decides when to "
                         "raise a defect for a human to look at.",
            "requirement_id": "HW-REQ-SERVO-014",
            "provenance": "ASSUMED",
        },
        "endurance_drift_counts": {
            "value": 5, "unit": "counts", "source": "NONE",
            "rationale": "A trend detector, not a limit. The measured "
                         "first-to-last-decile difference is the "
                         "result.",
            "requirement_id": "HW-REQ-END-003",
            "provenance": "ASSUMED",
        },
        "move_timeout_headroom": {
            "value": 0.5, "unit": "fraction", "source": "DESIGN",
            "rationale": "A movement that routinely takes more than "
                         "half its timeout leaves no margin to "
                         "distinguish a slow movement from a stuck "
                         "one.",
            "requirement_id": "HW-REQ-SERVO-012",
            "provenance": "ASSUMED",
        },
        "status_latency_ms": {
            "value": 2000, "unit": "ms", "source": "DESIGN",
            "rationale": "get_status must answer at the same speed "
                         "whether the sensor is present or missing, so "
                         "it must be far below an acquisition.",
            "requirement_id": "HW-REQ-LINK-003",
            "provenance": "DESIGN",
        },
        "operator_angle_tolerance_deg": {
            "value": 5.0, "unit": "deg", "source": "NONE",
            "rationale": "What a human can read off a protractor. Not "
                         "a property of the mechanism.",
            "requirement_id": "HW-REQ-H002-001",
            "provenance": "ASSUMED",
        },
        "rotation_drift_counts": {
            "value": 20, "unit": "counts", "source": "NONE",
            "rationale": "An absolute bound that does NOT scale with "
                         "the rotation count. A 100-rotation run that "
                         "accepts 100x the one-rotation tolerance has "
                         "no acceptance criterion at all.",
            "requirement_id": "HW-REQ-CAR-005",
            "provenance": "ASSUMED",
        },
    },

    # THE MISSION the B12 rehearsal is judged against.
    "mission": {
        "expected_slots": None,      # None -> the production slot count
        "expected_sample_count": None,
        "required_slot_sequence": None,
        "time_budget_minutes": None,   # None -> characterization only
        "allowed_workarounds": 0,
    },

    "artifacts": None,               # None -> Tests/hardware/artifacts
    "operator": None,
    "notes": "",
}


class Profile:
    """One bench, described well enough to reproduce a result on it."""

    def __init__(self, data=None, path=None):
        merged = _deep_merge(DEFAULTS, data or {})

        self.path = Path(path) if path else None
        self.data = merged
        self.production = production_values()

        self.problems = []
        self._validate()

    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path):
        path = Path(path)

        if not path.is_file():
            raise ProfileError("no such profile: {}".format(path))

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))

        except (OSError, ValueError) as error:
            raise ProfileError("{} could not be read: {}".format(
                path, error))

        if not isinstance(raw, dict):
            raise ProfileError(
                "{} must contain a JSON object, not a {}".format(
                    path, type(raw).__name__))

        return cls(raw, path=path)

    @classmethod
    def default(cls):
        """
        A profile with no bench in it.

        Enough to list, describe and dry-run; not enough to execute,
        because it names no device. That asymmetry is the point.
        """
        return cls({})

    # ------------------------------------------------------------------

    def _validate(self):
        problems = []
        data = self.data

        selectors = [data.get(key) for key in
                     ("port", "port_by_id", "usb_serial")]

        if not any(selectors) and not (data.get("usb_vid")
                                       and data.get("usb_pid")):
            problems.append(
                "no device selector: set one of 'port', 'port_by_id', "
                "'usb_serial', or both 'usb_vid' and 'usb_pid'. There is "
                "no default port and there will not be one.")

        for key in ("usb_vid", "usb_pid"):
            value = data.get(key)

            if value is not None and not _is_hex_word(value):
                problems.append(
                    "{} must be a 16-bit value such as \"0x10C4\" or "
                    "4292, got {!r}".format(key, value))

        for key in ("baudrate", "command_timeout_s", "connect_timeout_s",
                    "measure_timeout_s"):
            value = data.get(key)

            if value is not None and (
                    not isinstance(value, (int, float)) or value <= 0):
                problems.append(
                    "{} must be a positive number, got {!r}".format(
                        key, value))

        limits = data.get("limits") or {}

        for key, value in sorted(limits.items()):
            if not isinstance(value, int) or value < 1:
                problems.append(
                    "limits.{} must be a positive whole number, got "
                    "{!r}".format(key, value))

        mechanism = data.get("mechanism") or {}
        ratio = mechanism.get("gear_ratio_servo_to_carousel")

        if not isinstance(ratio, (int, float)) or ratio <= 0:
            problems.append(
                "mechanism.gear_ratio_servo_to_carousel must be a "
                "positive number, got {!r}".format(ratio))

        if mechanism.get("provenance") not in Provenance.ALL:
            problems.append(
                "mechanism.provenance must be one of {}, got {!r}".format(
                    ", ".join(Provenance.ALL), mechanism.get("provenance")))

        motion = data.get("motion") or {}
        per_leg = motion.get("max_degrees_per_leg")

        if not isinstance(per_leg, (int, float)) or not 0 < per_leg <= 180:
            problems.append(
                "motion.max_degrees_per_leg must be greater than 0 and at "
                "most 180 - the production driver refuses a single "
                "verified movement larger than half a revolution - got "
                "{!r}".format(per_leg))

        illumination = data.get("illumination") or {}
        sources = illumination.get("sources") or []
        known = {"white", "uv", "ir"}

        unknown = [s for s in sources if s not in known]

        if unknown:
            problems.append(
                "illumination.sources may only contain {}, got {}".format(
                    ", ".join(sorted(known)), ", ".join(map(str, unknown))))

        hold = illumination.get("max_hold_ms")

        if not isinstance(hold, int) or not 0 < hold <= 10000:
            problems.append(
                "illumination.max_hold_ms must be between 1 and 10000, "
                "got {!r}".format(hold))

        problems.extend(self._validate_thresholds())
        problems.extend(self._validate_instruments())
        problems.extend(self._validate_mission())

        self.problems = problems

    def _validate_thresholds(self):
        """
        Every threshold must carry its provenance.

        A threshold that is a bare number is exactly what this section
        exists to abolish, so one missing field here is a profile
        error rather than a silent default.
        """
        problems = []

        required = ("value", "unit", "source", "rationale",
                    "requirement_id", "provenance")

        for name, entry in sorted(
                (self.data.get("thresholds") or {}).items()):
            if not isinstance(entry, dict):
                problems.append(
                    "thresholds.{} must be an object carrying value, "
                    "unit, source, rationale, requirement_id and "
                    "provenance".format(name))

                continue

            missing = [field for field in required if field not in entry]

            if missing:
                problems.append(
                    "thresholds.{} is missing {}".format(
                        name, ", ".join(missing)))

            if not isinstance(entry.get("value"), (int, float)):
                problems.append(
                    "thresholds.{}.value must be a number".format(name))

            source = entry.get("source")

            if source is not None and source not in (
                    "NONE", "PRODUCTION_CONFIG", "PROTOCOL", "DESIGN",
                    "DATASHEET", "MEASURED_BASELINE"):
                problems.append(
                    "thresholds.{}.source is {!r}, which is not one of "
                    "the recognised provenances".format(name, source))

        return problems

    def _validate_instruments(self):
        problems = []

        for name, entry in sorted(
                (self.data.get("instruments") or {}).items()):
            if entry is None:
                continue

            if not isinstance(entry, dict):
                problems.append(
                    "instruments.{} must be an object with model, "
                    "serial and calibrated".format(name))

                continue

            for field in ("model", "serial", "calibrated"):
                if not entry.get(field):
                    problems.append(
                        "instruments.{} is declared but has no "
                        "{}".format(name, field))

        return problems

    def _validate_mission(self):
        problems = []

        mission = self.data.get("mission") or {}

        count = mission.get("expected_sample_count")

        if count is not None:
            if isinstance(count, bool) or not isinstance(count, int):
                problems.append(
                    "mission.expected_sample_count must be a whole "
                    "number")

            elif count < 1:
                problems.append(
                    "mission.expected_sample_count must be at least 1 - "
                    "a mission with zero samples is not a mission")

        budget = mission.get("time_budget_minutes")

        if budget is not None and (
                not isinstance(budget, (int, float)) or budget <= 0):
            problems.append(
                "mission.time_budget_minutes must be a positive number "
                "or null")

        allowed = mission.get("allowed_workarounds")

        if allowed not in (0, None):
            problems.append(
                "mission.allowed_workarounds must be 0 - a workaround "
                "is a defect that has not been written down yet")

        return problems

    @property
    def valid(self):
        return not self.problems

    def require_valid(self):
        if self.problems:
            raise ProfileError(
                "the hardware profile {} is not usable:\n  - {}".format(
                    self.path or "(built-in default)",
                    "\n  - ".join(self.problems)))

        return self

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------

    def get(self, key, default=None):
        return self.data.get(key, default)

    @property
    def name(self):
        return self.data.get("name")

    @property
    def limits(self):
        return dict(self.data.get("limits") or {})

    @property
    def slot_count(self):
        override = (self.data.get("mechanism") or {}).get("slot_count")

        if override:
            return int(override)

        return int(self.production["carousel"]["slot_count"])

    @property
    def counts_per_rev(self):
        return int(self.production["servo"]["counts_per_rev"])

    @property
    def gear_ratio(self):
        return float(
            (self.data.get("mechanism") or {})
            ["gear_ratio_servo_to_carousel"]
        )

    # ------------------------------------------------------------------
    # the bench: unit, instruments, fixtures, thresholds
    # ------------------------------------------------------------------

    def unit(self):
        return dict(self.data.get("unit") or {})

    def unit_identified(self):
        """
        Whether this bench names the physical module it is testing.

        Not a formality. The layer gate opens on a prior PASS, and a
        PASS earned on another module is not evidence about this one.
        """
        unit = self.unit()

        return bool(unit.get("module_id"))

    def instrument(self, name):
        """One declared instrument, or None."""
        return (self.data.get("instruments") or {}).get(name)

    def fixture(self, name):
        return (self.data.get("fixtures") or {}).get(name)

    def threshold(self, name):
        """
        One acceptance threshold with its provenance.

        Raises rather than returning a default: a test asking for a
        threshold that does not exist is a test about to invent one.
        """
        thresholds = self.data.get("thresholds") or {}

        if name not in thresholds:
            raise ProfileError(
                "no threshold called {!r} in the profile. A test may "
                "not invent an acceptance criterion; add it to "
                "configuration/profile.py with its source and "
                "rationale.".format(name))

        return dict(thresholds[name])

    def threshold_value(self, name):
        return self.threshold(name)["value"]

    def threshold_is_authoritative(self, name):
        """
        Whether a PASS/FAIL may be drawn against this threshold.

        A threshold whose source is NONE is a working figure somebody
        chose. Judging hardware against it produces a verdict that
        cannot survive being questioned, so the test characterizes
        instead.
        """
        return self.threshold(name).get("source") not in (None, "NONE")

    def mission(self):
        mission = dict(self.data.get("mission") or {})

        if not mission.get("expected_slots"):
            mission["expected_slots"] = self.slot_count

        return mission

    def diagnostic_firmware(self):
        return dict(self.data.get("diagnostic_firmware") or {})

    @property
    def artifacts_dir(self):
        configured = self.data.get("artifacts")

        if configured:
            return Path(configured).expanduser()

        return HARDWARE_DIR / "artifacts"

    def selector(self):
        """The device selector, as the resolver wants it."""
        return {
            "port": self.data.get("port"),
            "port_by_id": self.data.get("port_by_id"),
            "usb_vid": _as_word(self.data.get("usb_vid")),
            "usb_pid": _as_word(self.data.get("usb_pid")),
            "usb_serial": self.data.get("usb_serial"),
        }

    def as_dict(self):
        """
        The snapshot that goes into the run manifest.

        Includes the production values, because a result read a year
        later has to be interpretable against the configuration it was
        produced under - not against whatever config.py says then.
        """
        return {
            "path": str(self.path) if self.path else None,
            "name": self.name,
            "valid": self.valid,
            "problems": list(self.problems),
            "profile": self.data,
            "production_configuration": self.production,
            "provenance_note":
                "Values under 'production_configuration' are CONFIGURED: "
                "they are what the firmware does. Values under "
                "'profile.mechanism' are ASSUMED unless their provenance "
                "says otherwise. Nothing here is MEASURED until a "
                "hardware test measures it.",
        }


# ======================================================================
# helpers
# ======================================================================

def _deep_merge(base, overlay):
    merged = {}

    for key, value in base.items():
        if isinstance(value, dict):
            merged[key] = _deep_merge(value, (overlay or {}).get(key) or {})

        else:
            merged[key] = (overlay or {}).get(key, value)

    for key, value in (overlay or {}).items():
        if key not in merged:
            merged[key] = value

    return merged


def _is_hex_word(value):
    return _as_word(value) is not None


def _as_word(value):
    """Accept 0x10C4, "10C4", "0x10C4" and 4292; reject anything else."""
    if value is None:
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, int):
        return value if 0 <= value <= 0xFFFF else None

    if isinstance(value, str):
        text = value.strip()

        try:
            number = int(text, 16 if text.lower().startswith("0x") else 10)

        except ValueError:
            try:
                number = int(text, 16)

            except ValueError:
                return None

        return number if 0 <= number <= 0xFFFF else None

    return None
