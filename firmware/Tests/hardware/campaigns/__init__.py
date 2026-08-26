"""
B0 to B12: the layer pyramid, in the order it must be climbed.

    B0   bench and environment inventory
    B1   Main PC <-> ESP32 communication
    B2   direct ST3215 communication
    B3   H-002: encoder versus physical movement
    B4   servo repeatability and tolerance characterization
    B5   the physical carousel
    B6   direct AS7265x testing
    B7   sensor endurance, data integrity and illumination
    B8   carousel and measurement integration
    B9   Linux / USB / reset / failure recovery
    B10  the full operator workflow
    B11  endurance
    B12  competition mission rehearsal

Each module registers its campaign and its tests into the registry it is
handed. Importing them builds definitions out of data and function
objects; nothing is executed and no adapter is constructed.

THE GATES BETWEEN THEM ARE DECLARED, NOT ASSUMED. B5 lists B3 as a
prerequisite because a carousel result is meaningless while the encoder
and the mechanism disagree; B12 lists B8 and B10 because a mission
rehearsal is the last test to run, never the first.
"""

from . import (b0_environment, b1_link, b2_servo_comms, b3_h002,
               b4_servo_characterization, b5_carousel, b6_sensor,
               b7_sensor_integrity, b8_integration, b9_recovery,
               b10_workflow, b11_endurance, b12_mission)


MODULES = (
    b0_environment,
    b1_link,
    b2_servo_comms,
    b3_h002,
    b4_servo_characterization,
    b5_carousel,
    b6_sensor,
    b7_sensor_integrity,
    b8_integration,
    b9_recovery,
    b10_workflow,
    b11_endurance,
    b12_mission,
)


def load_all(registry):
    """Register every campaign, in layer order."""
    for module in MODULES:
        module.register(registry)

    return registry
