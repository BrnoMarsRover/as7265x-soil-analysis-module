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
from ..core.model import Automation, Safety


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
