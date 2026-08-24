"""
A pySerial stand-in that can fail the way real ports fail.

WHAT IT MODELS THAT A NAIVE FAKE DOES NOT

    in_waiting        the link reads one byte and then whatever came
                      with it; a fake without in_waiting hides the
                      difference between a fast read and a slow one
    blocking reads    a real read() blocks for the port timeout when
                      nothing arrives. Returning b"" instantly turns
                      every timeout test into a busy spin whose result
                      depends on machine load
    chunking          a CP210x delivers a frame in 64-byte USB packets,
                      so a response arrives split at arbitrary points
    the write count   pySerial's write() returns how many bytes went
                      out, and short writes are a real failure

FAULTS IT CAN BE ASKED FOR

Every one of these was chosen because it is either measured on this
bench or a documented pySerial behaviour, not because it was easy:

    chunk_size            deliver responses in N-byte pieces
    latency               seconds of clock time each read costs
    line_ending           "\\n", "\\r\\n" or "\\r"
    drop_newline          send a frame with no terminator at all
    noise_before          bytes in front of the frame (LED inrush)
    noise_after           bytes behind it
    corrupt_leading       replace the first N bytes with rubbish, the
                          exact damage measured on this hardware
    duplicate             send every response twice
    stale                 bytes already in the buffer before we started
    fail_write_after      SerialException from the Nth write
    fail_read_after       SerialException from the Nth read
    short_write           write() reports fewer bytes than it took
    zero_write            write() reports 0 bytes
    close_on_write        the port closes itself mid-command
    disconnect_after      the device stops answering from command N
    swallow_after         responses stop being produced from command N
"""

import json


class FakeSerialException(Exception):
    """Stands in for serial.SerialException."""


class FakeSerialPort:
    """
    One fake port. `device` turns a request line into a response dict.

    `device` may be:
        None                    echoes a trivial ok:true answer
        a callable(dict) -> dict or None
        an object with .handle(dict) -> dict or None

    Returning None means "this command produces no answer at all",
    which is how a timeout is simulated without any sleeping.
    """

    # pySerial module-level constants a caller may reach for.
    EIGHTBITS = 8
    PARITY_NONE = "N"
    STOPBITS_ONE = 1

    def __init__(self, device=None, clock=None, **faults):
        self.device = device
        self.clock = clock

        self.port = None
        self.baudrate = None
        self.bytesize = None
        self.parity = None
        self.stopbits = None
        self.timeout = None
        self.exclusive = None

        self._dtr = True
        self._rts = True

        self.is_open = False
        self.opened_count = 0
        self.closed_count = 0
        self.buffer_resets = 0

        self.written = []
        self.requests = []

        self._out = []                      # queued response chunks

        # -- faults ----------------------------------------------------
        self.chunk_size = faults.pop("chunk_size", 0)
        self.latency = faults.pop("latency", 0.0)
        self.line_ending = faults.pop("line_ending", "\n")
        self.drop_newline = faults.pop("drop_newline", False)
        self.noise_before = faults.pop("noise_before", b"")
        self.noise_after = faults.pop("noise_after", b"")
        self.corrupt_leading = faults.pop("corrupt_leading", 0)
        self.duplicate = faults.pop("duplicate", False)
        self.fail_write_after = faults.pop("fail_write_after", None)
        self.fail_read_after = faults.pop("fail_read_after", None)
        self.short_write = faults.pop("short_write", False)
        self.zero_write = faults.pop("zero_write", False)
        self.close_on_write = faults.pop("close_on_write", None)
        self.disconnect_after = faults.pop("disconnect_after", None)
        self.swallow_after = faults.pop("swallow_after", None)

        stale = faults.pop("stale", None)

        if faults:
            raise TypeError("unknown fault(s): {}".format(sorted(faults)))

        self.write_count = 0
        self.read_count = 0

        if stale:
            self._enqueue(stale if isinstance(stale, bytes)
                          else stale.encode("utf-8"))

    # -- the pySerial surface ------------------------------------------

    @property
    def dtr(self):
        return self._dtr

    @dtr.setter
    def dtr(self, value):
        self._dtr = value

    @property
    def rts(self):
        return self._rts

    @rts.setter
    def rts(self, value):
        self._rts = value

    def open(self):
        self.is_open = True
        self.opened_count += 1

    def close(self):
        self.is_open = False
        self.closed_count += 1

    def reset_input_buffer(self):
        self._out = []
        self.buffer_resets += 1

    def flush(self):
        pass

    @property
    def in_waiting(self):
        return sum(len(chunk) for chunk in self._out)

    def write(self, data):
        self.write_count += 1

        if (self.fail_write_after is not None
                and self.write_count > self.fail_write_after):
            raise FakeSerialException(
                "write failed on {}: device disappeared".format(self.port))

        if (self.close_on_write is not None
                and self.write_count > self.close_on_write):
            self.close()

        self.written.append(data)

        answer = self._answer_for(data)

        if answer is not None:
            self._enqueue(answer)

            if self.duplicate:
                self._enqueue(answer)

        if self.zero_write:
            return 0

        if self.short_write:
            return max(0, len(data) - 1)

        return len(data)

    def read(self, count=1):
        self.read_count += 1

        if (self.fail_read_after is not None
                and self.read_count > self.fail_read_after):
            raise FakeSerialException(
                "read failed on {}: device disappeared".format(self.port))

        if not self._out:
            # A real port blocks for `timeout` and returns nothing.
            # Advancing the fake clock by exactly that is what makes a
            # PROTOCOL_TIMEOUT test terminate instead of spinning.
            self._spend(self.timeout if self.timeout else 0.01)

            return b""

        self._spend(self.latency)

        chunk = self._out.pop(0)

        if count is not None and count > 0 and len(chunk) > count:
            self._out.insert(0, chunk[count:])

            return chunk[:count]

        return chunk

    # -- internals ------------------------------------------------------

    def _spend(self, seconds):
        if self.clock is not None and seconds:
            self.clock.advance(seconds)

    def _enqueue(self, payload):
        """Split a response into the pieces a real bridge would deliver."""
        if self.chunk_size and self.chunk_size > 0:
            self._out.extend(
                payload[at:at + self.chunk_size]
                for at in range(0, len(payload), self.chunk_size)
            )

        else:
            self._out.append(payload)

    def _answer_for(self, data):
        """The bytes this request produces, or None for silence."""
        if (self.disconnect_after is not None
                and self.write_count > self.disconnect_after):
            return None

        if (self.swallow_after is not None
                and self.write_count > self.swallow_after):
            return None

        try:
            request = json.loads(data.decode("utf-8"))

        except (ValueError, UnicodeDecodeError):
            return None

        self.requests.append(request)

        response = self._device_answer(request)

        if response is None:
            return None

        terminator = b"" if self.drop_newline \
            else self.line_ending.encode("utf-8")

        if isinstance(response, bytes):
            # RAW BYTES STILL GET A TERMINATOR.
            #
            # Returning them bare made every "damaged frame" case time
            # out instead of being classified, because the read loop
            # only looks at COMPLETED lines - so a fake meant to test
            # MALFORMED_RESPONSE was testing PROTOCOL_TIMEOUT, and the
            # salvage and damage-counting paths were never reached at
            # all. A real board's console output ends in a newline;
            # so does this.
            return self.noise_before + response + terminator \
                + self.noise_after

        line = json.dumps(response)

        if self.corrupt_leading:
            damaged = (b"\xff" * self.corrupt_leading
                       + line.encode("utf-8")[self.corrupt_leading:])
            line_bytes = damaged

        else:
            line_bytes = line.encode("utf-8")

        return self.noise_before + line_bytes + terminator + self.noise_after

    def _device_answer(self, request):
        if self.device is None:
            return {
                "request_id": request.get("request_id"),
                "ok": True,
                "cmd": request.get("cmd"),
                "data": {"pong": True, "echoed": request.get("cmd")},
            }

        handle = getattr(self.device, "handle", None)

        if handle is not None:
            return handle(request)

        return self.device(request)


class FakeSerialModule:
    """The pieces of `serial` that serial_link.py actually touches."""

    EIGHTBITS = 8
    PARITY_NONE = "N"
    STOPBITS_ONE = 1
    SerialException = FakeSerialException

    Serial = FakeSerialPort

    def __init__(self, factory):
        raise TypeError("FakeSerialModule is used as a module, not built")


def make_serial_module(factory):
    """A stand-in `serial` module whose Serial() returns `factory()`."""
    module = type("serial", (), {})

    module.EIGHTBITS = 8
    module.PARITY_NONE = "N"
    module.STOPBITS_ONE = 1
    module.SerialException = FakeSerialException
    module.Serial = staticmethod(lambda *a, **k: factory())

    return module


def open_link(serial_link_module, port, clock=None, link_kwargs=None,
              **faults):
    """
    An OPEN SerialLink talking to a fresh FakeSerialPort.

    Returns (link, fake_port). The real `open()` runs, so the DTR/RTS
    discipline and the failure classification are exercised rather than
    bypassed.
    """
    fake = FakeSerialPort(device=port, clock=clock, **faults)

    original = serial_link_module.serial
    serial_link_module.serial = make_serial_module(lambda: fake)

    try:
        link = serial_link_module.SerialLink(
            "PORT_TEST", **(link_kwargs or {}))
        link.open()

    finally:
        serial_link_module.serial = original

    # The link keeps the handle it opened; the module-level `serial` is
    # only needed again for exception matching, which the caller sets up
    # via install_fake_serial() when it wants transport errors.
    return link, fake


def install_fake_serial(serial_link_module):
    """
    Point serial_link at the fake `serial` module and hand back a
    restore callable. Needed whenever a test wants a transport
    exception to be recognized as `serial.SerialException`.
    """
    original = serial_link_module.serial
    serial_link_module.serial = FakeSerialModule

    def restore():
        serial_link_module.serial = original

    return restore
