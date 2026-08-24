"""
Deterministic time.

WHY

`SerialLink._read_response` waits on `time.monotonic()` against a
deadline, and `CONNECT_TIMEOUT` is 15 seconds. A suite that tests the
timeout honestly with the real clock spends 15 real seconds per case,
which means the timeout cases get written once and then quietly deleted
the first time somebody is in a hurry.

With a fake clock the same test costs microseconds AND is exact: the
"answer arrives one millisecond before the deadline" case is a real
test rather than a race against the machine's load.

HOW

`install_clock` swaps the `time` module *as the module under test sees
it*. Nothing global is touched, so an unrelated import of `time`
elsewhere in the process is unaffected, and `restore()` puts the real
module back.
"""

import time as real_time


class FakeClock:
    """
    A clock that only moves when a test moves it.

    `sleep()` advances instead of waiting, which is what makes a
    retry loop with a 200 ms backoff testable at full speed.
    """

    def __init__(self, start=1000.0):
        self.now = float(start)
        self.slept = []
        self.sleeps = 0

    # -- the `time` module surface the production code uses ------------

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps += 1
        self.slept.append(seconds)
        self.now += float(seconds)

    # -- MicroPython's extensions, for firmware-side code --------------

    def ticks_ms(self):
        return int(self.now * 1000)

    def ticks_diff(self, a, b):
        return a - b

    def ticks_add(self, ticks, delta):
        return ticks + delta

    def sleep_ms(self, ms):
        self.sleep(ms / 1000.0)

    # -- what a test drives it with ------------------------------------

    def advance(self, seconds):
        self.now += float(seconds)

        return self.now

    @property
    def total_slept(self):
        return sum(self.slept)


class _Installed:
    """The handle `install_clock` returns; restores on close."""

    def __init__(self, module, clock, previous):
        self.module = module
        self.clock = clock
        self.previous = previous

    def restore(self):
        self.module.time = self.previous

    def __enter__(self):
        return self.clock

    def __exit__(self, exc_type, exc_value, traceback):
        self.restore()

        return False


def install_clock(module, clock=None, start=1000.0):
    """
    Give `module` a fake `time`, and hand back the clock.

    Used as a context manager:

        with install_clock(serial_link) as clock:
            ...
            clock.advance(11.0)

    or explicitly, with `.restore()` in a finally.
    """
    clock = clock or FakeClock(start)
    previous = getattr(module, "time", real_time)
    module.time = clock

    return _Installed(module, clock, previous)
