"""
The hardware boundary, faked - and nothing above it.

WHY THIS PACKAGE IS NOT CALLED `support`

`firmware/Tests/support.py` already owns that name and holds the ESP32
scaffolding: the `machine` stub, the fake AS7265x that speaks the real
virtual-register protocol, and the fake ST3215 that speaks the real
serial-bus frames. Two modules called `support` on one path is how a
suite ends up importing the wrong one, so this package is `fakes` and
imports the other rather than replacing it.

WHAT IS FAKED, AND WHERE THE LINE IS

    faked      serial.Serial, machine.UART, machine.I2C, the clock,
               the operator's keyboard, the directory records live in
    real       every line of SerialLink, protocol, carousel, servo,
               sensor, workflow, Science and BD above that line

The rule is one sentence: a fake replaces a WIRE, never a decision. A
test that fakes `Protocol.handle_measure_raw` proves the fake works.

CONTENTS

    clock        deterministic time, so a 15 s timeout costs no seconds
    serial_port  a pySerial stand-in with every failure mode measured
                 or plausible on this hardware
    esp32        two device fakes: one scripted, one that runs the REAL
                 firmware in-process behind a loopback wire
    console      scripted operator input and captured screen output
    storage      a throwaway BD/ so no test can touch the real one
"""

from fakes.clock import FakeClock, install_clock                 # noqa: F401
from fakes.console import (                                      # noqa: F401
    ScriptedConsole,
    answers,
    run_screen,
)
from fakes.esp32 import (                                        # noqa: F401
    BEHAVIOURS,
    LoopbackDevice,
    ScriptedDevice,
    loopback_link,
)
from fakes.serial_port import (                                  # noqa: F401
    FakeSerialModule,
    FakeSerialPort,
    open_link,
)
from fakes.storage import (                                      # noqa: F401
    SandboxBD,
    sandbox_mission,
)
