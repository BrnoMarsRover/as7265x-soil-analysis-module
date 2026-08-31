"""
The operator, driving the REAL client loop.

WHY THIS EXISTS

Every screen in this project already had tests. They called a render
function with a status dictionary the test had written itself, and
asserted on what came back. Two defects lived through all of it,
because both live BETWEEN the functions that were tested:

    `workflow/status.servo_link` read `servo["selected"]`, a key the
    firmware has never sent. Every test that exercised it built its own
    status dict containing `selected`, so the reader and the fixture
    agreed and neither agreed with the board. In production the
    function returned NOT SELECTED unconditionally - over an ST3215
    that was answering normally.

    A failed measurement printed a correct, compact failure block and
    then RETURNED, and the main loop's next iteration saw
    `position_valid == False` and redrew the context-free startup
    screen. Nothing rendered anything wrong. The operator simply lost
    the sample, the slot, the stage and the failure.

So this harness drives `screen.interactive()` itself - the real menu
loop, the real prompts, the real dispatch, the real error handling -
with scripted keystrokes, against the real firmware behind a fake wire.

WHAT IS FAKE, AND ONLY THIS

    the serial PORT             `fakes.serial_port`
    the AS7265x and ST3215      `support.FakeAS7265X` / `FakeST3215`
    the BD tree                 `SandboxBD`, so no test can touch BD/
    the keyboard                `fakes.console.ScriptedConsole`

Everything from `SerialLink.request()` inward, and everything from the
menu loop outward, is production code. The firmware that answers is the
real `protocol.py` running the real handlers, so a fault injected in
the fake servo produces the real firmware's real error.
"""

import contextlib
import io
import sys
from pathlib import Path

_TESTS_DIR = next(
    p for p in Path(__file__).resolve().parents if p.name == "Tests"
)

if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

import support                                                # noqa: E402

support.add_project_root()
support.add_path("PC")

from fakes.console import ScriptedConsole                     # noqa: E402
from fakes.esp32 import LoopbackDevice, loopback_link         # noqa: E402
from fakes.serial_port import install_fake_serial             # noqa: E402
from fakes.storage import SandboxBD, sandbox_mission          # noqa: E402


class Transcript:
    """Everything one scripted operator session produced."""

    def __init__(self, text, console, exit_code, error=None):
        self.text = text
        self.console = console
        self.exit_code = exit_code
        self.error = error

    @property
    def prompts(self):
        return list(self.console.prompts)

    @property
    def unused(self):
        """Answers the script never got asked for."""
        return self.console.unused

    def shows(self, *fragments):
        """True when every fragment appears somewhere in the output."""
        return all(fragment in self.text for fragment in fragments)

    def screens(self, title):
        """How many times a screen with this title was drawn."""
        return self.text.count(title)

    def after(self, marker):
        """Everything printed after the LAST occurrence of a marker."""
        at = self.text.rfind(marker)

        return "" if at == -1 else self.text[at:]

    def before(self, marker):
        at = self.text.find(marker)

        return self.text if at == -1 else self.text[:at]


class OperatorBench:
    """
    A client, a board and a keyboard.

    Not a context manager by accident: the sandbox holds a real
    temporary directory and the fake serial module is installed
    globally, so both have to be given back.
    """

    def __init__(self, servo=None, device=None, sensor=None,
                 link_kwargs=None, **faults):
        self._restore = install_fake_serial(__import__("serial_link"))

        loopback = device or LoopbackDevice(
            servo=servo or support.FakeST3215(),
            device=sensor,
        )

        self.link, self.port, self.loopback = loopback_link(
            __import__("serial_link"), device=loopback,
            link_kwargs=link_kwargs or {"timeout": 5.0}, **faults)

        self.bd = SandboxBD()
        self.mission, _ = sandbox_mission(self.link, bd=self.bd)

    # -- setting the bench up ------------------------------------------

    def bring_up(self, load_slot=1):
        """Connect the servo and synchronize, as Carousel Setup does."""
        self.link.connect_servo()
        self.link.sync_position(load_slot=load_slot)

        return self

    def loaded_sample(self, sample_id="s0007", slot=1):
        """A sample prepared, confirmed and selected - ready to measure."""
        from BD.samples import STATE_LOADED

        self.mission.session.create(sample_id, slot)
        self.mission.session.set_state(sample_id, STATE_LOADED)
        self.link.select_slot(slot, sample_id=sample_id)

        return self

    def status(self):
        return self.link.get_status()

    # -- driving the real client ---------------------------------------

    def run(self, script, exhausted="QUIT", loop_guard=400):
        """
        Drive the REAL `screen.interactive` loop with scripted keys.

        `Mission` is redirected to the sandboxed one for the duration
        and put back afterwards. That is the ONLY seam: the loop, the
        screens, the prompts, the dispatch table and every error
        handler are the production objects.
        """
        from workflow import screen

        console = ScriptedConsole(script, exhausted=exhausted,
                                  loop_guard=loop_guard)

        original_mission = screen.Mission
        original_input = __import__("builtins").input

        screen.Mission = lambda _link: self.mission
        __import__("builtins").input = console

        error = None
        code = None

        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                try:
                    code = screen.interactive(self.link)

                except BaseException as exc:            # noqa: BLE001
                    # NOT swallowed into a pass: a loop that raises is
                    # the finding. It is captured so the transcript up
                    # to the failure is still readable.
                    error = exc

        finally:
            screen.Mission = original_mission
            __import__("builtins").input = original_input

        return Transcript(out.getvalue(), console, code, error)

    # -- lifecycle ------------------------------------------------------

    def close(self):
        try:
            self.link.close()

        finally:
            try:
                self.bd.close()

            finally:
                self._restore()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

        return False


def stuck_encoder(short_by=2048, **kwargs):
    """
    A servo that accepts the goal and whose encoder does not move.

    This is the H-002 signature as the bench actually produced it: the
    goal is written, the driver polls, the movement reports finished,
    and the encoder reads what it read before. The REAL firmware then
    raises the REAL ST3215PositionError, so the error the client
    handles is the firmware's own rather than one a test invented.
    """
    return support.FakeST3215(short_by=short_by, **kwargs)


class ServoStuckAfter(support.FakeST3215):
    """
    A servo that obeys the first N movements and then stops moving.

    For the measurement transaction this is the case that matters most
    and is hardest to reach any other way: the OUTBOUND half turn
    succeeds, WHITE/UV/IR are acquired, and the RETURN fails. Real
    spectra then exist while the carousel position does not - two facts
    that must not be collapsed into one verdict.
    """

    def __init__(self, obey=1, stuck_by=2048, **kwargs):
        super().__init__(**kwargs)

        self.obey = int(obey)
        self.stuck_by = int(stuck_by)
        self.move_count = 0

    def _start_move(self, goal):
        self.move_count += 1

        if self.move_count > self.obey:
            # Accept the goal, poll normally, arrive nowhere.
            self.short_by = self.stuck_by

        return super()._start_move(goal)


def stuck_on_return(**kwargs):
    """Outbound works; the 180 degree return does not."""
    return ServoStuckAfter(obey=1, **kwargs)
