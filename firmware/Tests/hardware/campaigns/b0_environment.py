"""
B0 - the bench, before anything is plugged in.

Nothing here opens a port. Enumeration reads what the operating system
already knows; the wiring check is a human looking at the module. B0 is
the layer that makes every later result attributable: which machine,
which Python, which commit, which profile, which physical device.

WHY DEVICE IDENTITY IS A TEST AND NOT A SETTING

`/dev/ttyUSB0` is an enumeration order, not a name. If a second USB
serial device appears - another board, a probe, an Arduino - the science
module moves and nothing visibly changes. HW-B0-004 exists to record the
STABLE identity of this board (by-id path, USB serial number, VID/PID)
so that every later campaign can prove it talked to the same instrument.
"""

import platform
import sys

from ..configuration import ports as ports_module
from ..configuration.profile import REPO_ROOT, production_values
from ..core.model import Automation, Requirement, Safety


CAMPAIGN = "B0"


def register(registry):
    registry.campaign(
        CAMPAIGN, layer="B0", title="Bench and environment inventory",
        purpose="Record what the bench IS, before anything is plugged "
                "in, so every later result can be attributed to a "
                "machine, a commit, a profile and a physical device.",
        gate_note="No prerequisites. B0 is the floor.",
    )

    registry.test(
        test_id="HW-B0-001", campaign=CAMPAIGN, layer="B0",
        requirements=("HW-REQ-ENV-001",),
        title="Host operating system, Python and dependencies",
        objective="Record the machine the campaign ran on, and prove "
                  "the Python and pyserial available are ones the "
                  "production client can use.",
        hardware_setup="None. Nothing is connected for this test.",
        preconditions="The repository is checked out and the operator "
                      "is at a shell on the machine that will drive the "
                      "campaign.",
        procedure=(
            "read the OS, kernel, architecture and hostname",
            "read the Python version and interpreter path",
            "import the production serial_link module and read the "
            "pyserial version it found",
            "check the Python version is at least 3.8",
        ),
        expected="Python 3.8 or newer, pyserial importable, and every "
                 "value recorded.",
        failure_criteria="pyserial missing - the production client "
                         "cannot run at all - or a Python older than "
                         "the production code assumes.",
        captures=("os", "kernel", "architecture", "hostname",
                  "python version", "interpreter path",
                  "pyserial version"),
        safety=Safety.READ_ONLY, automation=Automation.AUTOMATIC,
        requires=(),
        run=_host_environment,
        notes="Carries ENV-LINUX-001 in part: the deferred software "
              "item is running the software suite on freya-1-comp, "
              "which needs this environment and no hardware.",
    )

    registry.test(
        test_id="HW-B0-002", campaign=CAMPAIGN, layer="B0",
        requirements=("HW-REQ-ENV-002", "HW-REQ-ENV-003"),
        title="Repository revision and firmware configuration snapshot",
        objective="Tie the campaign to an exact commit and to the exact "
                  "production constants it is qualifying.",
        hardware_setup="None.",
        preconditions="The working tree is the one that will be "
                      "deployed to the board.",
        procedure=(
            "read the git commit and whether the tree is dirty",
            "read firmware/ESP32/config.py through the profile loader",
            "record firmware name, version and protocol version",
            "record the servo, sensor and carousel constants",
            "check the carousel geometry is self-consistent: the "
            "scan/load offset must be half the slot count",
        ),
        expected="A commit is recorded and the geometry is consistent.",
        failure_criteria="A dirty tree is recorded but not failed - it "
                         "is a warning on the evidence. An inconsistent "
                         "geometry IS a failure: the firmware's own "
                         "carousel checks that invariant and would "
                         "refuse to run.",
        captures=("git commit", "dirty flag", "firmware version",
                  "protocol version", "every production constant the "
                  "campaign depends on"),
        safety=Safety.READ_ONLY, automation=Automation.AUTOMATIC,
        requires=(),
        run=_repository_state,
    )

    registry.test(
        test_id="HW-B0-003", campaign=CAMPAIGN, layer="B0",
        requirements=("HW-REQ-ENV-004",),
        title="Bench profile validation",
        objective="Prove the profile that will select the device is "
                  "complete and internally consistent BEFORE a campaign "
                  "depends on it.",
        hardware_setup="None.",
        preconditions="A profile has been passed with --profile, or the "
                      "built-in default is in use.",
        procedure=(
            "validate every field of the profile",
            "check a device selector is present",
            "check the motion envelope does not exceed the production "
            "driver's per-leg limit",
            "check every iteration limit is a positive whole number",
            "record which values are CONFIGURED, which are ASSUMED",
        ),
        expected="No validation problems, and a selector that names "
                 "something more specific than a ttyUSBn.",
        failure_criteria="Any validation problem. A profile with no "
                         "selector fails here rather than at the moment "
                         "a campaign tries to open a device.",
        captures=("the whole profile", "its problems, if any",
                  "the production values it was merged with"),
        safety=Safety.READ_ONLY, automation=Automation.AUTOMATIC,
        requires=(),
        run=_profile_validation,
    )

    registry.test(
        test_id="HW-B0-004", campaign=CAMPAIGN, layer="B0",
        requirements=("HW-REQ-ENV-004", "HW-REQ-LINK-008"),
        title="Serial device inventory and stable identification",
        objective="Record every serial device the machine can see, and "
                  "establish a STABLE identity for the science module "
                  "that survives a replug.",
        hardware_setup="The ESP32 connected over USB. Nothing else "
                       "needs to be powered.",
        preconditions="The operator can see the module's USB cable.",
        procedure=(
            "enumerate every serial port the OS reports - this OPENS "
            "NOTHING",
            "parse VID, PID and serial number out of each hwid",
            "list /dev/serial/by-id if the machine has one",
            "resolve the profile's selector against the inventory",
            "check the resolution is unambiguous",
        ),
        expected="Exactly one device matches the profile's selector, "
                 "and it has a stable by-id path or a USB serial "
                 "number.",
        failure_criteria="No match, or more than one. Both are refusals "
                         "to guess: a campaign that opens the wrong "
                         "board reports a fault in an instrument that "
                         "was never under test.",
        captures=("every port with its description and hwid",
                  "the by-id entries", "the resolved device",
                  "how it was matched"),
        safety=Safety.READ_ONLY, automation=Automation.AUTOMATIC,
        requires=("link.enumerate",),
        run=_device_inventory,
        assumption="H-004",
        defect_prefix="HW-USB",
        notes="Replaces HW-100 in PHASE_B_CAMPAIGNS.md.",
    )

    registry.test(
        test_id="HW-B0-005", campaign=CAMPAIGN, layer="B0",
        requirements=("HW-REQ-ENV-005", "HW-REQ-ENV-009"),
        title="Wiring and power checklist",
        objective="Have a human confirm the physical assumptions every "
                  "later campaign is built on, one at a time.",
        hardware_setup="The complete module: ESP32, ST3215 on the "
                       "Waveshare bus board, AS7265x on I2C, carousel "
                       "mechanically attached, supply connected.",
        preconditions="The operator is at the bench and can see the "
                      "wiring.",
        procedure=(
            "confirm the ST3215 is wired to the configured TX and RX "
            "pins",
            "confirm the servo supply is connected and at the expected "
            "voltage",
            "confirm the AS7265x is on the configured SDA and SCL pins",
            "confirm the carousel is mechanically free to turn a full "
            "revolution",
            "confirm no sample is loaded",
            "record the operator's notes verbatim",
        ),
        expected="Every item confirmed by a human.",
        failure_criteria="Any item the operator will not confirm. A "
                         "campaign run over unverified wiring produces "
                         "failures that are wiring faults wearing a "
                         "firmware costume.",
        captures=("each confirmation, timestamped",
                  "the operator's free-text notes"),
        safety=Safety.READ_ONLY, automation=Automation.OPERATOR_ASSISTED,
        requires=(),
        run=_wiring_checklist,
        notes="The ST3215 ECHO_ONLY fault (only TX/RX swapped answers) "
              "is why the pin question is asked explicitly rather than "
              "assumed from config.py.",
    )

    registry.test(
        test_id="HW-B0-006", campaign=CAMPAIGN, layer="B0",
        requirements=("HW-REQ-ENV-006",),
        title="The physical unit under test is identified",
        objective="Record which module, ESP32, servo, sensor and "
                  "mechanical assembly this campaign is testing, so a "
                  "PASS can be attributed to an instrument rather than "
                  "to a project.",
        hardware_setup="The complete module in front of the operator, "
                       "with any asset tags or serial numbers legible.",
        preconditions="A bench profile exists.",
        procedure=(
            "read the unit identifiers the profile declares",
            "have the operator confirm each against the physical labels",
            "record the carousel assembly revision",
            "check the profile names at least the module",
        ),
        expected="Every declared identifier matches the hardware in "
                 "front of the operator, and the module is named.",
        failure_criteria="A profile that names no module, or an "
                         "identifier the operator cannot find on the "
                         "hardware. Either makes every later layer gate "
                         "unsound, because a prerequisite PASS could "
                         "have been earned on a different instrument.",
        captures=("every declared identifier",
                  "the operator's confirmation of each",
                  "the carousel assembly revision"),
        safety=Safety.READ_ONLY, automation=Automation.OPERATOR_ASSISTED,
        requires=("bench.unit_identified",),
        run=_unit_identity,
        defect_prefix="HW-USB",
    )

    registry.test(
        test_id="HW-B0-007", campaign=CAMPAIGN, layer="B0",
        requirements=("HW-REQ-ENV-007",),
        title="Measurement instruments and their calibration",
        objective="Record which instruments will produce the electrical "
                  "and thermal measurements, and when each was last "
                  "calibrated.",
        hardware_setup="Whatever multimeter, oscilloscope, logic "
                       "analyzer, thermal probe or current probe the "
                       "bench has.",
        preconditions="The profile declares the instruments.",
        procedure=(
            "read every instrument the profile declares",
            "check each carries a model, a serial number and a "
            "calibration date",
            "have the operator confirm the instrument in front of them "
            "is the one declared",
            "record which electrical tests are therefore possible",
        ),
        expected="Every declared instrument is identified and "
                 "calibrated, and the operator confirms it is the one "
                 "on the bench.",
        failure_criteria="An instrument whose calibration date is "
                         "missing or whose identity the operator cannot "
                         "confirm. A measurement from an unidentified "
                         "instrument is an anecdote with a number in "
                         "it.",
        captures=("each instrument's model, serial and calibration date",
                  "the operator's confirmation",
                  "which electrical tests are unblocked by them"),
        safety=Safety.READ_ONLY, automation=Automation.OPERATOR_ASSISTED,
        requires=(),
        run=_instrument_inventory,
        defect_prefix="HW-USB",
    )

    registry.test(
        test_id="HW-B0-008", campaign=CAMPAIGN, layer="B0",
        requirements=("HW-REQ-ENV-008", "HW-REQ-PWR-001"),
        title="Power topology and rail voltages at idle",
        objective="Record the bench's ACTUAL power topology and measure "
                  "every supply rail with the module idle.",
        hardware_setup="The module powered and idle. A multimeter with "
                       "safe access to the rails - no probing that "
                       "risks shorting anything.",
        preconditions="HW-B0-005 and HW-B0-007 passed.",
        procedure=(
            "have the operator walk the actual wiring and confirm the "
            "topology: regulated input, the 3V3 sensor rail, the "
            "external servo supply, and whether they share ground",
            "confirm whether servo current passes through the sensor "
            "PCB",
            "measure the regulated input voltage",
            "measure the 3V3 sensor rail",
            "measure the servo supply",
            "record every reading with the instrument that made it",
        ),
        expected="A recorded topology and three measured voltages.",
        failure_criteria="A topology the operator cannot confirm. The "
                         "VOLTAGES are characterization: no "
                         "schematic-derived limits are recorded in this "
                         "repository, so nothing here judges them - it "
                         "measures them so a later brownout has a "
                         "baseline to be compared against.",
        captures=("the confirmed topology",
                  "regulated input, sensor rail and servo supply "
                  "voltages", "the measuring instrument",
                  "whether grounds are shared",
                  "whether servo current crosses the sensor PCB"),
        safety=Safety.READ_ONLY, automation=Automation.OPERATOR_ASSISTED,
        requires=("bench.multimeter",),
        run=_power_topology,
        defect_prefix="HW-PWR",
        notes="Documentation/ARCHITECTURE.md states the intended "
              "topology. This records what the bench actually does, "
              "which is the only thing a current measurement can be "
              "interpreted against.",
    )

    registry.test(
        test_id="HW-B0-009", campaign=CAMPAIGN, layer="B0",
        requirements=("HW-REQ-ENV-010",),
        title="The repository does not contradict itself about the "
              "carousel",
        objective="Detect stale geometry claims across configuration, "
                  "documentation, plans and campaign definitions before "
                  "an operator follows one of them.",
        hardware_setup="None. This reads files.",
        preconditions="The repository is checked out.",
        procedure=(
            "read the authoritative geometry from "
            "firmware/ESP32/config.py",
            "scan the repository's documents for slot-count and "
            "slot-angle claims",
            "report every claim that contradicts the configuration, "
            "with its exact path and line",
            "check the hardware campaign's own documents agree",
        ),
        expected="No document contradicts the shipped configuration.",
        failure_criteria="Any contradiction. An operator following a "
                         "document that says 45 degrees per slot on a "
                         "mechanism configured for 90 will mis-set the "
                         "carousel and then blame the firmware. "
                         "Production files are out of this campaign's "
                         "scope, so a contradiction there is REPORTED "
                         "with its exact path, never silently edited.",
        captures=("the authoritative geometry",
                  "every contradicting claim with path and line",
                  "which files are in and out of scope to fix"),
        safety=Safety.READ_ONLY, automation=Automation.AUTOMATIC,
        requires=(),
        run=_document_consistency,
        defect_prefix="HW-USB",
    )

    registry.test(
        test_id="HW-B0-010", campaign=CAMPAIGN, layer="B0",
        requirements=("HW-REQ-PWR-004", "HW-REQ-SENSOR-015"),
        title="UART and I2C signal integrity at idle",
        objective="Observe the servo bus and the sensor bus with an "
                  "instrument, so a marginal signal is not left to "
                  "present itself later as an intermittent firmware "
                  "fault.",
        hardware_setup="An oscilloscope or logic analyzer with safe "
                       "access to the ST3215 data line and the I2C "
                       "pair. The module powered and idle.",
        preconditions="HW-B0-007 declared the instrument.",
        procedure=(
            "observe the UART idle level and confirm it is idle-high",
            "measure the observed baud rate against ST3215_BAUD",
            "observe the I2C SDA and SCL idle state and confirm both "
            "are high",
            "measure the I2C clock frequency against I2C_FREQ",
            "record rise and fall times if the instrument can",
        ),
        expected="Idle levels correct, and the observed rates match "
                 "the configured ones.",
        failure_criteria="An idle-low bus - missing pull-ups, a driver "
                         "holding the line - or a rate that does not "
                         "match the configuration. Edge timing is "
                         "characterization: no design limits for it are "
                         "recorded in this repository.",
        captures=("UART idle level and measured baud",
                  "I2C idle levels and measured clock",
                  "rise and fall times where available",
                  "the measuring instrument"),
        safety=Safety.READ_ONLY, automation=Automation.OPERATOR_ASSISTED,
        requires=("bench.oscilloscope",),
        run=_signal_integrity,
        defect_prefix="HW-PWR",
    )


# ======================================================================
# bodies
# ======================================================================

def _host_environment(ctx):
    ctx.record("host")

    details = {
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "node": platform.node(),
        "python": sys.version,
        "python_version": ".".join(map(str, sys.version_info[:3])),
        "executable": sys.executable,
    }

    ctx.check(sys.version_info >= (3, 8),
              "Python is 3.8 or newer",
              evidence={"version": details["python_version"]})

    pyserial = None
    import_error = None

    try:
        module = ctx.link.module
        serial_module = getattr(module, "serial", None)

        pyserial = getattr(serial_module, "__version__", None)

    except Exception as error:                         # pragma: no cover
        import_error = "{}: {}".format(type(error).__name__, error)

    details["pyserial"] = pyserial
    details["pyserial_import_error"] = import_error

    ctx.check(pyserial is not None,
              "pyserial is importable by the production serial owner",
              evidence={"version": pyserial, "error": import_error})

    ctx.record("host_details", **details)
    ctx.measure(stage="host", python=details["python_version"],
                pyserial=pyserial or "", platform=details["platform"])

    if details["system"] != "Linux":
        ctx.note(
            "This is not the Linux main computer. The module runs on "
            "freya-1-comp; a campaign run elsewhere is a bench "
            "rehearsal, and PORT_DENIED / dialout behaviour cannot be "
            "verified here.")


def _repository_state(ctx):
    from ..core.evidence import git_revision

    ctx.record("repository")

    revision = git_revision(REPO_ROOT)

    ctx.check(bool(revision.get("commit")),
              "the repository revision is known",
              evidence=revision)

    if revision.get("dirty"):
        ctx.note("The working tree has uncommitted changes. The "
                 "evidence records the commit, but the code that ran is "
                 "not exactly that commit.")

    values = production_values()

    carousel = values["carousel"]
    servo = values["servo"]

    ctx.check(
        carousel["scan_load_offset_slots"] * 2 == carousel["slot_count"],
        "the loader/scanner offset is half the slot count, so the "
        "mapping is its own inverse",
        evidence=carousel,
    )

    ctx.check(
        servo["counts_per_slot"] * carousel["slot_count"]
        == servo["counts_per_rev"],
        "counts per slot times slot count equals counts per revolution",
        evidence={"counts_per_slot": servo["counts_per_slot"],
                  "slot_count": carousel["slot_count"],
                  "counts_per_rev": servo["counts_per_rev"]},
    )

    ctx.check(
        servo["half_turn_counts"] * 2 == servo["counts_per_rev"],
        "the half-turn constant is half a revolution",
        evidence={"half_turn_counts": servo["half_turn_counts"]},
    )

    ctx.record("production_configuration", **values)

    ctx.note(
        "CONFIGURED, not measured: {} counts per revolution, {} slots, "
        "{} degrees between slots, {} degrees loader to scanner. H-002 "
        "and H-005 exist because the relationship between those numbers "
        "and the mechanism is unproven.".format(
            servo["counts_per_rev"], carousel["slot_count"],
            carousel["slot_spacing_deg"], carousel["half_turn_deg"]))


def _profile_validation(ctx):
    profile = ctx.profile

    ctx.record("profile", path=str(profile.path) if profile.path else None)

    ctx.check(profile.valid, "the bench profile validates",
              evidence={"problems": profile.problems})

    selector = profile.selector()

    ctx.check(any(selector.values()),
              "the profile names a device selector",
              evidence=selector)

    specific = bool(selector.get("port_by_id") or selector.get("usb_serial"))

    ctx.check(specific or bool(selector.get("port")),
              "the selector is specific enough to identify one board",
              evidence=selector)

    if not specific:
        ctx.note(
            "The selector relies on an explicit device path or on "
            "VID/PID. Both are weaker than a by-id path or a USB serial "
            "number: Linux renumbers ttyUSBn on replug, and VID/PID is "
            "the same for every CP2102 on the bench.")

    limits = profile.limits

    ctx.check(all(isinstance(v, int) and v > 0 for v in limits.values()),
              "every iteration limit is a positive whole number",
              evidence=limits)

    ctx.record("profile_snapshot", **profile.as_dict())


def _device_inventory(ctx):
    ctx.require("link.enumerate")

    ctx.record("enumerate")

    ports = ctx.link.enumerate_ports()

    identified = []

    for port in ports:
        identity = ports_module.parse_hwid(port.get("hwid"))

        entry = dict(port)
        entry["identity"] = identity

        identified.append(entry)

        ctx.measure(
            stage="inventory", port=port.get("port"),
            description=port.get("description") or "",
            hwid=port.get("hwid") or "",
            vid=identity.get("vid"), pid=identity.get("pid"),
            serial=identity.get("serial") or "",
        )

    by_id = ports_module.by_id_entries()

    ctx.record("inventory", ports=identified, by_id=by_id,
               port_count=len(ports))

    ctx.check(bool(ports),
              "the operating system reports at least one serial device",
              evidence={"count": len(ports)})

    try:
        resolved = ports_module.resolve(
            ctx.profile.selector(), ports, by_id)

    except ports_module.PortError as error:
        ctx.check(False,
                  "the profile's selector resolves to exactly one device",
                  evidence={"code": error.code, "message": error.message,
                            "candidates": error.candidates})

        ctx.defect(
            title="the bench profile does not identify exactly one device",
            observed=error.message,
            expected="exactly one serial device matching the selector",
            reproduction=("run HW-B0-004 with this profile",),
            suspected_layer="bench configuration",
            evidence={"code": error.code,
                      "candidates": error.candidates,
                      "selector": ctx.profile.selector()},
        )

        return

    ctx.check(True, "the profile's selector resolves to exactly one device",
              evidence=resolved)

    # Stability is a property of the DEVICE, not of how the profile
    # happened to find it. An explicit --port resolves without going
    # near a hwid, and the first version of this check called that
    # unstable - which would have told an operator to fix a profile that
    # was already naming a perfectly identifiable board.
    device = resolved.get("device")

    matched = next(
        (entry for entry in identified if entry.get("port") == device),
        None)

    serial_number = ((matched or {}).get("identity") or {}).get("serial")

    named_by_id = any(entry.get("device") == device
                      or entry.get("by_id") == device
                      for entry in by_id)

    stable = bool(serial_number) or named_by_id

    ctx.check(stable,
              "the resolved device has a stable identity that survives "
              "a replug",
              evidence={"device": device,
                        "usb_serial_number": serial_number,
                        "has_by_id_path": named_by_id,
                        "by_id_entries": len(by_id),
                        "matched_by": resolved.get("matched_by")})

    if not stable:
        ctx.note(
            "This device has no USB serial number and no "
            "/dev/serial/by-id path, so it can only be named by a "
            "ttyUSBn that Linux may renumber on the next replug. Record "
            "that in the run notes: a later campaign that opens the "
            "wrong board would look like an instrument fault.")


def _wiring_checklist(ctx):
    values = production_values()

    servo = values["servo"]
    sensor = values["sensor"]

    ctx.confirm_observation(
        "Is the ST3215 data line wired to GPIO{} (TX) and GPIO{} (RX) "
        "as config.py expects".format(servo["tx_pin"], servo["rx_pin"]))

    ctx.confirm_observation(
        "Is the ST3215 supply connected and at the voltage the servo "
        "expects")

    ctx.confirm_observation(
        "Is the AS7265x on I2C bus {} with SDA on GPIO{} and SCL on "
        "GPIO{}".format(sensor["i2c_bus"], sensor["i2c_sda_pin"],
                        sensor["i2c_scl_pin"]))

    ctx.confirm_observation(
        "Is the carousel mechanically free to turn a complete "
        "revolution with nothing obstructing it")

    ctx.confirm_observation(
        "Is the carousel EMPTY - no soil, no sample in any slot")

    ctx.confirm_observation(
        "Can you see a reference mark on the carousel that lets you "
        "judge how far it has turned")

    ctx.operator_note("Anything unusual about the bench today")


def _unit_identity(ctx):
    ctx.require("bench.unit_identified")

    unit = ctx.profile.unit()

    ctx.record("unit", **unit)

    ctx.check(bool(unit.get("module_id")),
              "the profile names the module under test",
              evidence=unit)

    for field, question in (
            ("module_id",
             "Is the module in front of you the one labelled {}"),
            ("esp32_id",
             "Is the ESP32 the one labelled {}"),
            ("servo_id_tag",
             "Is the servo the one labelled {}"),
            ("sensor_id_tag",
             "Is the AS7265x the one labelled {}"),
            ("carousel_assembly_id",
             "Is the carousel assembly the one labelled {}")):
        value = unit.get(field)

        if not value:
            # An undeclared identifier is not a failure - not every
            # bench labels every part - but it is recorded as absent
            # rather than quietly skipped.
            ctx.note(
                "the profile declares no {}; that part of the unit "
                "identity is unrecorded".format(field))

            continue

        ctx.confirm_observation(question.format(value))

    revision = unit.get("carousel_assembly_revision")

    ctx.measure(stage="unit", module_id=unit.get("module_id") or "",
                esp32_id=unit.get("esp32_id") or "",
                servo_id_tag=unit.get("servo_id_tag") or "",
                sensor_id_tag=unit.get("sensor_id_tag") or "",
                carousel_assembly_id=unit.get(
                    "carousel_assembly_id") or "",
                carousel_assembly_revision=revision or "")

    if not revision:
        ctx.note(
            "No carousel assembly revision is recorded. If the "
            "mechanism is rebuilt or re-coupled, every H-002 and B5 "
            "result earned before the change stops applying, and "
            "without a revision nobody can tell which side of the "
            "change a result came from.")


def _instrument_inventory(ctx):
    from ..adapters.bench import INSTRUMENTS

    declared = {}

    for key in sorted(INSTRUMENTS):
        instrument = ctx.profile.instrument(key)

        if instrument:
            declared[key] = instrument

    ctx.record("instruments", declared=declared,
               absent=[k for k in sorted(INSTRUMENTS)
                       if k not in declared])

    ctx.check(bool(declared),
              "the profile declares at least one measuring instrument",
              evidence={"declared": sorted(declared)})

    if not declared:
        ctx.note(
            "No instruments are declared, so every electrical and "
            "thermal test in this campaign will report BLOCKED with "
            "the profile field it needs. That is the correct outcome "
            "for a bench with no instruments, not a defect.")

    for key in sorted(declared):
        instrument = declared[key]

        for field in ("model", "serial", "calibrated"):
            ctx.check(bool(instrument.get(field)),
                      "the {} declares its {}".format(key, field),
                      evidence={"instrument": instrument})

        ctx.confirm_observation(
            "Is the {} on the bench a {}, serial {}".format(
                key, instrument.get("model"), instrument.get("serial")))

        ctx.measure(stage="instrument", instrument=key,
                    model=instrument.get("model") or "",
                    serial=instrument.get("serial") or "",
                    calibrated=instrument.get("calibrated") or "")


def _power_topology(ctx):
    ctx.require("bench.multimeter")

    meter = ctx.bench.require_instrument("multimeter")

    ctx.record("instrument", **meter)

    ctx.instruct(
        "Walk the actual wiring before answering the next questions. "
        "Do not answer them from the architecture document - this test "
        "records what THIS bench does.")

    shared_ground = ctx.ask(
        "Do the ESP32 and the external servo supply share a common "
        "ground")

    ctx.check(bool(shared_ground),
              "the ESP32 and the servo supply share a ground",
              evidence={"operator_answer": shared_ground},
              kind="OPERATOR")

    through_pcb = ctx.ask(
        "Does servo current pass through the sensor PCB")

    ctx.check(not through_pcb,
              "servo current does NOT pass through the sensor PCB",
              evidence={"operator_answer": through_pcb},
              kind="OPERATOR")

    readings = {}

    for key, question, minimum, maximum in (
            ("regulated_input_v",
             "Measure the regulated input voltage", 0.0, 30.0),
            ("sensor_rail_v",
             "Measure the +3V3 sensor rail", 0.0, 10.0),
            ("servo_supply_v",
             "Measure the external servo supply", 0.0, 30.0)):
        value = ctx.ask_number(question, minimum=minimum,
                               maximum=maximum, unit="V")

        readings[key] = value

        if value is None:
            # A rail nobody could measure is a gap in the baseline, and
            # the whole point of this test is the baseline.
            ctx.result.record_missing_required(
                "the {} was not measured".format(key),
                evidence={"question": question})

        ctx.measure(stage="rail", rail=key, volts=value,
                    instrument=meter.get("model") or "")

    ctx.record("power", shared_ground=bool(shared_ground),
               servo_current_through_sensor_pcb=bool(through_pcb),
               **readings)

    ctx.operator_note("Describe the power topology in your own words")

    ctx.characterize(
        "supply rails measured at idle: {}. No schematic-derived limits "
        "are recorded in this repository, so these are a baseline for a "
        "later droop or brownout to be compared against, not a "
        "judgement.".format(
            ", ".join("{} = {}".format(k, v)
                      for k, v in sorted(readings.items()))))


def _document_consistency(ctx):
    from ..core import consistency

    report = consistency.scan(REPO_ROOT)

    ctx.record("document_consistency", **report)

    ctx.check(report["authoritative"]["slot_count"] is not None,
              "the authoritative geometry was read from config.py",
              evidence=report["authoritative"])

    in_scope = [c for c in report["contradictions"] if c["in_scope"]]
    out_of_scope = [c for c in report["contradictions"]
                    if not c["in_scope"]]

    for entry in report["contradictions"]:
        ctx.measure(stage="contradiction", path=entry["path"],
                    line=entry["line"], claim=entry["claim"],
                    expected=entry["expected"],
                    in_scope=entry["in_scope"])

    ctx.check(not in_scope,
              "no document inside this campaign's scope contradicts the "
              "shipped configuration",
              evidence={"contradictions": in_scope})

    if out_of_scope:
        ctx.note(
            "{} contradiction(s) are in PRODUCTION files, which this "
            "campaign may not edit. They are reported with their exact "
            "path and line so somebody who owns those files can fix "
            "them: {}".format(
                len(out_of_scope),
                "; ".join("{}:{} says {}".format(
                    c["path"], c["line"], c["claim"])
                    for c in out_of_scope[:6])))

        ctx.defect(
            title="repository documents contradict the shipped carousel "
                  "geometry",
            observed="; ".join(
                "{}:{}: {}".format(c["path"], c["line"], c["claim"])
                for c in out_of_scope[:10]),
            expected="every document describes {} slots at {} degrees, "
                     "as firmware/ESP32/config.py ships".format(
                         report["authoritative"]["slot_count"],
                         report["authoritative"]["slot_spacing_deg"]),
            reproduction=("run HW-B0-009",),
            suspected_layer="documentation, not hardware",
            evidence={"contradictions": out_of_scope},
        )


def _signal_integrity(ctx):
    ctx.require("bench.oscilloscope")

    scope = ctx.bench.require_instrument("oscilloscope")

    ctx.record("instrument", **scope)

    servo = ctx.profile.production["servo"]
    sensor = ctx.profile.production["sensor"]

    ctx.instruct(
        "Connect the instrument to the ST3215 data line. Do not probe "
        "anything you cannot reach safely, and do not short adjacent "
        "pins.")

    uart_idle = ctx.observe(
        "With the bus idle, is the ST3215 data line HIGH or LOW",
        ("HIGH", "LOW", "UNKNOWN"))

    ctx.check(uart_idle == "HIGH",
              "the servo UART idles high",
              evidence={"observed": uart_idle}, kind="OPERATOR")

    measured_baud = ctx.ask_number(
        "Measure the bit rate on that line during a transaction "
        "(UNKNOWN if the instrument cannot)",
        minimum=0, maximum=5000000, unit="baud")

    if measured_baud is None:
        ctx.result.record_missing_required(
            "the servo bus bit rate was not measured",
            evidence={"configured": servo["baud"]})

    else:
        ctx.check(
            abs(measured_baud - servo["baud"]) <= servo["baud"] * 0.05,
            "the measured bit rate is within 5% of the configured "
            "{}".format(servo["baud"]),
            evidence={"measured": measured_baud,
                      "configured": servo["baud"]})

    ctx.instruct("Move the instrument to the I2C SDA and SCL lines.")

    for line in ("SDA", "SCL"):
        state = ctx.observe(
            "With the bus idle, is I2C {} HIGH or LOW".format(line),
            ("HIGH", "LOW", "UNKNOWN"))

        ctx.check(state == "HIGH",
                  "I2C {} idles high - the pull-ups are "
                  "present".format(line),
                  evidence={"observed": state}, kind="OPERATOR")

    clock = ctx.ask_number(
        "Measure the I2C clock frequency during a transaction "
        "(UNKNOWN if the instrument cannot)",
        minimum=0, maximum=1000000, unit="Hz")

    if clock is not None:
        ctx.check(
            abs(clock - sensor["i2c_frequency_hz"])
            <= sensor["i2c_frequency_hz"] * 0.2,
            "the measured I2C clock is within 20% of the configured "
            "{} Hz".format(sensor["i2c_frequency_hz"]),
            evidence={"measured": clock,
                      "configured": sensor["i2c_frequency_hz"]})

    rise = ctx.ask_number(
        "Measure the I2C rise time, if the instrument can (UNKNOWN "
        "otherwise)", minimum=0, maximum=100000, unit="ns")

    fall = ctx.ask_number(
        "Measure the I2C fall time, if the instrument can (UNKNOWN "
        "otherwise)", minimum=0, maximum=100000, unit="ns")

    ctx.measure(stage="signal_integrity", uart_idle=uart_idle,
                measured_baud=measured_baud,
                i2c_clock_hz=clock, i2c_rise_ns=rise, i2c_fall_ns=fall,
                instrument=scope.get("model") or "")

    if rise is not None or fall is not None:
        ctx.characterize(
            "I2C edge timing measured (rise {} ns, fall {} ns). No "
            "design limits for edge timing are recorded in this "
            "repository, so these are a baseline, not a "
            "judgement.".format(rise, fall))
