"""
What the BENCH has, as opposed to what the firmware can be asked.

WHY FIXTURES ARE CAPABILITIES

The other adapters detect what the shipped software can do. This one
detects what the physical bench has: a multimeter, an oscilloscope, a
thermal probe, a reference target, an angular reference, something that
can see infrared, a carousel plate that can be detached.

Those decide just as much as a firmware command does. An electrical test
with no multimeter is not a test that fails - it is a test that cannot
start, which is BLOCKED, and saying so in `--list` is more useful than
discovering it at the bench with the module already powered.

DECLARED, NOT DETECTED. Nothing here probes anything. A multimeter is
present because the profile says so, with its model, serial number and
calibration date - and an instrument with no calibration date is refused
by the profile validator, because a measurement from an uncalibrated
instrument is an anecdote with a number in it.

THE DEFAULT PROFILE HAS NONE OF THEM, and that is the honest default: a
bench nobody has described has no instruments.
"""

from .base import Adapter, Capability


# Fixture name -> (capability, what it is for, what to put in the
# profile). The recommendation is the whole value of a BLOCKED result,
# so each one names the exact field.
INSTRUMENTS = {
    "multimeter": (
        "bench.multimeter",
        "measuring supply rails, droop and current",
        'instruments.multimeter = {"model": ..., "serial": ..., '
        '"calibrated": "YYYY-MM-DD"}'),
    "oscilloscope": (
        "bench.oscilloscope",
        "observing UART and I2C signal integrity, and droop transients",
        'instruments.oscilloscope = {"model": ..., "serial": ..., '
        '"calibrated": "YYYY-MM-DD"}'),
    "logic_analyzer": (
        "bench.logic_analyzer",
        "decoding the servo bus and the I2C transactions",
        'instruments.logic_analyzer = {"model": ..., "serial": ..., '
        '"calibrated": "YYYY-MM-DD"}'),
    "thermal_probe": (
        "bench.thermal_probe",
        "measuring component temperatures after endurance",
        'instruments.thermal_probe = {"model": ..., "serial": ..., '
        '"calibrated": "YYYY-MM-DD"}'),
    "current_probe": (
        "bench.current_probe",
        "measuring motion and illumination current without breaking "
        "the supply",
        'instruments.current_probe = {"model": ..., "serial": ..., '
        '"calibrated": "YYYY-MM-DD"}'),
}

FIXTURES = {
    "representative_load": (
        "bench.representative_load",
        "measuring the mechanism as it will actually be operated",
        'fixtures.representative_load = "<a bounded, documented mass>"'),
    "reference_target": (
        "bench.reference_target",
        "acquisition repeatability against an unchanging surface",
        'fixtures.reference_target = "<a stable reflectance target>"'),
    "rotation_reference": (
        "bench.rotation_reference",
        "an angular reference INDEPENDENT of the encoder under test",
        'fixtures.rotation_reference = "<protractor, index mark or '
        'dial gauge>"'),
    "ir_observer": (
        "bench.ir_observer",
        "confirming the IR source lit, which the eye cannot do",
        'fixtures.ir_observer = "<a camera or photodiode that sees '
        'IR>"'),
    "carousel_detachable": (
        "bench.carousel_detachable",
        "separating encoder error from coupling slip in H-002",
        'fixtures.carousel_detachable = true, once the plate can be '
        'removed from the output shaft'),
}


class BenchAdapter(Adapter):
    """The physical bench, as the profile describes it."""

    name = "bench"

    def _detect(self):
        profile = self.context.profile

        found = {}

        for key, (name, purpose, field) in sorted(INSTRUMENTS.items()):
            declared = profile.instrument(key)

            found[name] = Capability(
                name, bool(declared),
                reason=(
                    "{} {}".format(
                        (declared or {}).get("model", "?"),
                        (declared or {}).get("serial", ""))
                    if declared else
                    "the profile declares no {} - needed for {}".format(
                        key, purpose)),
                recommendation=(
                    "" if declared else
                    "Add it to the bench profile: {}. An instrument "
                    "with no calibration date is refused by the profile "
                    "validator.".format(field)),
                detail={"declared": declared, "purpose": purpose},
            )

        for key, (name, purpose, field) in sorted(FIXTURES.items()):
            declared = profile.fixture(key)

            found[name] = Capability(
                name, bool(declared),
                reason=(
                    "the profile declares it: {}".format(declared)
                    if declared else
                    "the profile declares no {} - needed for {}".format(
                        key, purpose)),
                recommendation=(
                    "" if declared else
                    "Add it to the bench profile: {}".format(field)),
                detail={"declared": declared, "purpose": purpose},
            )

        # The unit under test. Not a fixture, but the same shape of
        # question: a campaign whose module is unnamed cannot attribute
        # a PASS to anything.
        found["bench.unit_identified"] = Capability(
            "bench.unit_identified", profile.unit_identified(),
            reason=(
                "the profile identifies module {}".format(
                    profile.unit().get("module_id"))
                if profile.unit_identified() else
                "the profile does not name the physical module under "
                "test"),
            recommendation=(
                "" if profile.unit_identified() else
                'Set unit.module_id in the bench profile, plus whichever '
                'of esp32_id, servo_id_tag, sensor_id_tag and '
                'carousel_assembly_id apply. A prerequisite PASS earned '
                'on one module is not evidence about another, so the '
                'layer gates are only sound once the module is named.'),
            detail={"unit": profile.unit()},
        )

        # The bench power topology, confirmed by a human against the
        # actual wiring rather than against the architecture document.
        power = profile.data.get("power") or {}

        found["bench.power_topology"] = Capability(
            "bench.power_topology", bool(power.get("topology_confirmed")),
            reason=(
                "the profile records a confirmed power topology"
                if power.get("topology_confirmed") else
                "the bench power topology has not been confirmed"),
            recommendation=(
                "" if power.get("topology_confirmed") else
                'Walk the actual wiring and set power.topology_confirmed '
                'true, with regulated_input_v, sensor_rail_v, '
                'servo_supply_v, servo_supply_shared_ground and '
                'servo_current_through_sensor_pcb. '
                'Documentation/ARCHITECTURE.md says what the design '
                'intends; this records what the bench does.'),
            detail={"power": power},
        )

        return found

    # ------------------------------------------------------------------

    def require_instrument(self, key):
        """
        The declared instrument, or a Blocked naming the profile field.

        Called from a test body rather than from the capability gate
        when the test needs the instrument's identity in its evidence -
        which every electrical measurement does.
        """
        from ..core.model import Blocked

        declared = self.context.profile.instrument(key)

        if not declared:
            name, purpose, field = INSTRUMENTS[key]

            raise Blocked(
                "this bench declares no {}, which is needed for "
                "{}".format(key, purpose),
                capability=name,
                recommendation="Add it to the bench profile: {}".format(
                    field))

        return dict(declared)
