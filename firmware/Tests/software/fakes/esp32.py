"""
Two device fakes, for two different questions.

LoopbackDevice - "does the whole stack work?"

    Runs the REAL ESP32 firmware in this process, behind a fake wire.
    A request written by the real `SerialLink` is parsed by the real
    `Protocol`, handled by the real `Carousel` and `Sensor`, and the
    real response comes back up through the real framing. The only
    fakes below the line are `machine.I2C`, `machine.UART` and
    `serial.Serial`.

    That the PC layer and the ESP32 firmware can share one interpreter
    is not obvious - both trees contain a module called `config` - but
    the ESP32's is top-level while BD's and Science's are inside
    packages, so the names do not collide. This is what makes a
    genuine end-to-end software test possible at all.

LoopbackDevice can also LIE, per command, so a real firmware answer can
be replaced by a malformed one at exactly one point in a long workflow.

ScriptedDevice - "what does the PC do when the answer is wrong?"

    No firmware at all. Answers come from a per-command script, and
    every entry may be one of the behaviours below. Cheap, and precise
    about failures the real firmware would never produce.

THE BEHAVIOURS, AND WHY EACH ONE IS IN THE LIST

    SUCCESS           the baseline
    DEVICE_ERROR      ok:false with a code - the firmware refusing
    TIMEOUT           no answer at all; the commonest real failure
    MALFORMED         a frame with our request_id, damaged in transit.
                      Distinct from TIMEOUT because the answer HAS
                      been and gone: waiting out the timeout is wrong
    TRUNCATED         JSON cut short mid-object
    GARBAGE           console noise that is not a frame
    REPL              the MicroPython >>> prompt
    ECHO              our own request bounced back. The REPL evaluates
                      an incoming JSON object and prints its repr, so
                      this frame carries OUR request_id and is the one
                      failure that can fake a healthy link
    WRONG_ID          an answer to somebody else's request
    NO_DATA           ok:true and no data at all
    EMPTY_DATA        ok:true with an empty data object
    OK_BUT_USELESS    ok:true whose data is missing the fields the
                      caller needs - "never trust a success flag alone"
    DISCONNECT        the port raises, mid-command
"""

import json
import sys
from pathlib import Path

_TESTS_DIR = next(
    p for p in Path(__file__).resolve().parents if p.name == "Tests"
)

if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

import support                                              # noqa: E402


SUCCESS = "SUCCESS"
DEVICE_ERROR = "DEVICE_ERROR"
TIMEOUT = "TIMEOUT"
MALFORMED = "MALFORMED"
TRUNCATED = "TRUNCATED"
GARBAGE = "GARBAGE"
REPL = "REPL"
ECHO = "ECHO"
WRONG_ID = "WRONG_ID"
NO_DATA = "NO_DATA"
EMPTY_DATA = "EMPTY_DATA"
OK_BUT_USELESS = "OK_BUT_USELESS"
DISCONNECT = "DISCONNECT"

BEHAVIOURS = (
    SUCCESS, DEVICE_ERROR, TIMEOUT, MALFORMED, TRUNCATED, GARBAGE,
    REPL, ECHO, WRONG_ID, NO_DATA, EMPTY_DATA, OK_BUT_USELESS,
    DISCONNECT,
)


def _render(behaviour, request, payload=None, error=None):
    """
    Turn a behaviour into what the wire carries.

    Returns a dict (framed normally by the port), raw bytes (framed
    exactly as given), or None for silence.
    """
    request_id = request.get("request_id")
    cmd = request.get("cmd")

    if behaviour == SUCCESS:
        return {"request_id": request_id, "ok": True, "cmd": cmd,
                "data": payload if payload is not None else {}}

    if behaviour == DEVICE_ERROR:
        error = error or {"code": "REFUSED",
                          "message": "The firmware refused the command."}

        return {"request_id": request_id, "ok": False, "cmd": cmd,
                "error": error}

    if behaviour == TIMEOUT:
        return None

    if behaviour == MALFORMED:
        # Carries the fingerprint of a response - request_id and ok -
        # and will not parse. That combination is what tells the link
        # the answer already came and was mangled.
        return ('{"request_id": "' + str(request_id)
                + '", "ok": true, "cmd": "' + str(cmd)
                + '", "data": {BROKEN').encode("utf-8")

    if behaviour == TRUNCATED:
        full = json.dumps({"request_id": request_id, "ok": True,
                           "cmd": cmd, "data": {"value": 1}})

        return full[:len(full) // 2].encode("utf-8")

    if behaviour == GARBAGE:
        return b"rst:0x1 (POWERON_RESET),boot:0x13 (SPI_FAST_FLASH_BOOT)"

    if behaviour == REPL:
        return b">>> "

    if behaviour == ECHO:
        # The request itself, with no "ok" key. Only a response has one.
        return json.dumps(request).encode("utf-8")

    if behaviour == WRONG_ID:
        return {"request_id": "not-{}".format(request_id), "ok": True,
                "cmd": cmd, "data": {}}

    if behaviour == NO_DATA:
        return {"request_id": request_id, "ok": True, "cmd": cmd}

    if behaviour == EMPTY_DATA:
        return {"request_id": request_id, "ok": True, "cmd": cmd,
                "data": {}}

    if behaviour == OK_BUT_USELESS:
        return {"request_id": request_id, "ok": True, "cmd": cmd,
                "data": {"unrelated": "field"}}

    if behaviour == DISCONNECT:
        return None

    raise ValueError("unknown behaviour {!r}".format(behaviour))


class ScriptedDevice:
    """
    Answers from a script, with no firmware behind it.

    `plan` maps a command name to either one behaviour or a list of
    behaviours consumed in order, so "the second MOVE disconnects" is
    one line:

        ScriptedDevice({"move_slots": [SUCCESS, DISCONNECT, SUCCESS]})

    `payloads` supplies the data object for SUCCESS answers.
    """

    def __init__(self, plan=None, payloads=None, default=SUCCESS):
        self.plan = dict(plan or {})
        self.payloads = dict(payloads or {})
        self.default = default

        self.seen = []
        self.counts = {}

    def behaviour_for(self, cmd):
        entry = self.plan.get(cmd, self.default)

        if isinstance(entry, (list, tuple)):
            index = self.counts.get(cmd, 0)

            if index < len(entry):
                return entry[index]

            return entry[-1] if entry else self.default

        return entry

    def handle(self, request):
        cmd = request.get("cmd")

        self.seen.append(cmd)
        behaviour = self.behaviour_for(cmd)
        self.counts[cmd] = self.counts.get(cmd, 0) + 1

        return _render(behaviour, request, self.payloads.get(cmd))


class LoopbackDevice:
    """
    The real firmware, one function call away from the real client.

    `build()` is lazy so a suite that never uses the loopback does not
    pay for loading the ESP32 tree.

    `lie` maps a command to a behaviour that REPLACES the firmware's
    real answer - the firmware still runs and still changes state,
    which is exactly the "the move happened, the acknowledgement was
    lost" case.
    """

    def __init__(self, lie=None, bring_up_sensor=True, device=None,
                 servo=None, retained_dir=None):
        self.lie = dict(lie or {})
        self.bring_up_sensor = bring_up_sensor
        self._device = device
        self._servo = servo

        # THE DEVICE'S FILESYSTEM, WHICH OUTLIVES ITS FIRMWARE.
        #
        # Setting `service = None` and calling build() again is how a
        # reset is modelled, and a reset does not erase flash. Holding
        # the directory here means the rebuilt firmware comes up on the
        # same filesystem it went down on - so a test can prove that
        # retained acquisitions survive a reboot, and would notice if
        # they stopped.
        self.retained_dir = retained_dir

        self.main = None
        self.service = None
        self.config = None
        self.fake_servo = None
        self.fake_sensor = None

        self.seen = []
        self.counts = {}
        self.responses = []

    def build(self):
        if self.service is not None:
            return self

        (self.main, self.service, self.config,
         self.fake_servo) = support.build_firmware(
            device=self._device, servo=self._servo,
            bring_up_sensor=self.bring_up_sensor,
            retained_dir=self.retained_dir,
        )

        # Whatever directory the first build was given, every later one
        # gets the same.
        self.retained_dir = self.config.RETAINED_DIR

        self.fake_sensor = self.service.fake_sensor

        return self

    def behaviour_for(self, cmd):
        entry = self.lie.get(cmd)

        if entry is None:
            return None

        if isinstance(entry, (list, tuple)):
            index = self.counts.get(cmd, 0)

            if index < len(entry):
                return entry[index]

            return entry[-1] if entry else None

        return entry

    def handle(self, request):
        self.build()

        cmd = request.get("cmd")
        self.seen.append(cmd)

        behaviour = self.behaviour_for(cmd)
        self.counts[cmd] = self.counts.get(cmd, 0) + 1

        # The firmware runs FIRST even when we are about to lie about
        # the answer: a lost acknowledgement does not undo the movement
        # that produced it.
        response = self.service.dispatch(request)
        self.responses.append(response)

        if behaviour is None or behaviour == SUCCESS:
            return response

        return _render(behaviour, request, response.get("data"))


def loopback_link(serial_link_module, device=None, clock=None,
                  link_kwargs=None, **faults):
    """
    An open SerialLink whose other end is the real firmware.

    Returns (link, fake_port, loopback). Everything between the
    client's `request()` and the firmware's handler is production code.
    """
    from fakes.serial_port import open_link

    loopback = device if isinstance(device, LoopbackDevice) \
        else LoopbackDevice()
    loopback.build()

    link, fake = open_link(
        serial_link_module, loopback, clock=clock,
        link_kwargs=link_kwargs, **faults)

    link.online = True

    return link, fake, loopback
