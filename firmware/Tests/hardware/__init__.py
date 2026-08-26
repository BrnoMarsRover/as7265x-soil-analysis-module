"""
Real-hardware verification for the Freya science module.

DELIBERATELY UNREACHABLE FROM `run_all.py`. The software campaign runs
against fakes and cannot command an actuator; this one drives a real
CP2102, a real ESP32, a real ST3215 and a real AS7265x, and it turns a
carousel. Two entry points, on purpose:

    Tests/run_all.py                    software only, safe by reflex
    Tests/hardware/run_hardware_tests.py    the real instrument

IMPORTING THIS PACKAGE TOUCHES NO HARDWARE. Every adapter is lazy, the
capability detection is static, and the only code path that opens a
serial port is behind an explicit `--confirm-hardware`.

NOTHING HERE HAS BEEN EXECUTED AGAINST HARDWARE. Every test in the
catalogue is NOT_RUN or READY_FOR_HARDWARE. See README.md.
"""

__version__ = "1.0.0"
