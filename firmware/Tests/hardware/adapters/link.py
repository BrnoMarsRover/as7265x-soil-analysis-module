"""
The PC to ESP32 transport, as the tests see it.

NO SECOND DRIVER. Every byte still goes through
`firmware/PC/serial_link.py`: the same framing, the same request ids,
the same timeouts and the same ten error codes the operator client uses.
A hardware test that talks to the board its own way qualifies the test,
not the product.

WHAT THIS ADDS ON TOP

    timing          every request is measured, so a latency
                    distribution is a by-product rather than a
                    separate campaign
    raw evidence    the transport counters - corrupt frames, salvaged
                    frames, stale frames, oversized lines, bytes read -
                    are snapshotted before and after every request, so
                    a result carries what the wire did, not only what
                    the answer said
    normalization   LinkError and DeviceError become AdapterError with
                    the original type, code and message intact

WHAT IT REFUSES TO DO

    open a port outside EXECUTE mode. `require_link()` raises if the
    run is a dry run or a self-test. That is the mechanical guarantee
    behind "--dry-run never initializes a transport".
"""

import time

from .base import (Adapter, AdapterError, Capability, firmware_commands,
                   load_serial_link_module, pc_command_surface)


class LinkAdapter(Adapter):
    """One open port for a whole campaign, with every request timed."""

    name = "link"

    def __init__(self, context):
        super().__init__(context)

        self._module = None
        self._link = None
        self._device = None

        self.transactions = []

    # ------------------------------------------------------------------
    # capability detection - no hardware
    # ------------------------------------------------------------------

    def _detect(self):
        surface = pc_command_surface()
        commands = firmware_commands()

        found = {}

        found["link.open"] = Capability(
            "link.open", "open" in surface and "close" in surface,
            reason="SerialLink.open/close",
        )

        found["link.ping"] = self.from_commands(
            "link.ping", ["ping"],
            "add a ping command to firmware/ESP32/protocol.py",
            surface, ["ping"],
        )

        found["link.status"] = self.from_commands(
            "link.status", ["get_status"],
            "add get_status to firmware/ESP32/protocol.py",
            surface, ["get_status"],
        )

        found["link.hard_reset"] = Capability(
            "link.hard_reset", "hard_reset" in surface,
            reason="SerialLink.hard_reset - an RTS pulse with DTR low, "
                   "measured to reach the application and not the "
                   "bootloader",
            recommendation="",
        )

        found["link.enumerate"] = Capability(
            "link.enumerate", "available_ports" in surface,
            reason="SerialLink.available_ports enumerates without "
                   "opening anything",
        )

        found["link.counters"] = Capability(
            "link.counters", True,
            reason="corrupt_frames, salvaged_frames, stale_frames, "
                   "oversized_lines and bytes_read are public attributes "
                   "of SerialLink",
        )

        # Byte-level TX/RX capture is NOT available: the production
        # reader consumes the stream and keeps only damaged lines. This
        # is a real gap and it is reported as one rather than worked
        # around with a second reader on the same port, which would
        # steal bytes from the production path.
        found["link.raw_stream"] = Capability(
            "link.raw_stream", False,
            reason="SerialLink does not expose the raw byte stream; it "
                   "keeps `damaged_lines` and a bounded `last_noise` "
                   "buffer only. A second reader on the same port would "
                   "consume bytes the production reader needs.",
            recommendation="If full wire capture is ever needed, do it "
                           "OUTSIDE the framework with a hardware line "
                           "tap or `interceptty`, or add an opt-in "
                           "`tee` hook to SerialLink._read_response "
                           "that copies every received line to a "
                           "caller-supplied sink.",
        )

        found["link.commands"] = Capability(
            "link.commands", bool(commands),
            reason="{} firmware commands parsed from protocol.py".format(
                len(commands)),
            detail={"commands": sorted(commands)},
        )

        return found

    # ------------------------------------------------------------------
    # the transport
    # ------------------------------------------------------------------

    @property
    def module(self):
        """The production serial_link module. Importing opens nothing."""
        if self._module is None:
            self._module = load_serial_link_module()

        return self._module

    def enumerate_ports(self):
        """
        What the OS reports. Opens nothing.

        Still refused outside EXECUTE: enumeration is harmless, but a
        dry run that starts inspecting the bench blurs the line the
        whole framework is built on.
        """
        self.context.require_hardware_mode("enumerate serial ports")

        return list(self.module.SerialLink.available_ports())

    def require_link(self, reason="talk to the ESP32"):
        """
        The open link, opening it on first use.

        Raises unless the run is EXECUTE and the operator confirmed the
        hardware. This is the choke point: no test can reach a serial
        port without passing through here.
        """
        self.context.require_hardware_mode(reason)

        if self._link is not None:
            return self._link

        device = self.context.device()
        profile = self.context.profile

        module = self.module

        kwargs = {"port": device}

        if profile.get("baudrate"):
            kwargs["baudrate"] = profile.get("baudrate")

        if profile.get("command_timeout_s"):
            kwargs["timeout"] = profile.get("command_timeout_s")

        if profile.get("connect_timeout_s"):
            kwargs["connect_timeout"] = profile.get("connect_timeout_s")

        try:
            link = module.SerialLink(**kwargs)

        except RuntimeError as error:
            # pyserial missing, or the OS refusing random bytes. Both
            # are environment faults, not device faults.
            raise AdapterError(
                "the serial client could not be constructed: {}".format(
                    error),
                code="ENVIRONMENT", original=error,
            )

        self.context.event("link_open_attempt", device=device,
                           baudrate=link.baudrate)

        try:
            link.open()

        except Exception as error:
            raise self._normalize(error, "open {}".format(device))

        self._link = link
        self._device = device

        self.context.event("link_open", device=device,
                           baudrate=link.baudrate)

        return link

    def wait_online(self, timeout=None):
        link = self.require_link("wait for the module to answer")

        try:
            link.wait_online(timeout=timeout)

        except Exception as error:
            raise self._normalize(error, "wait_online")

        return True

    def is_open(self):
        return self._link is not None and self._link.serial is not None

    def close(self, reason="campaign finished"):
        """
        Release the port. Never raises, and says whether it worked.

        A close that fails is recorded rather than swallowed: if the
        device has already vanished, "the port was released" is not a
        statement the framework may make.
        """
        if self._link is None:
            return {"closed": True, "was_open": False}

        try:
            self._link.close(reason=reason)
            result = {"closed": True, "was_open": True, "reason": reason}

        except Exception as error:                     # pragma: no cover
            result = {
                "closed": False,
                "was_open": True,
                "error_type": type(error).__name__,
                "error": str(error),
            }

        self._link = None

        self.context.event("link_close", **result)

        return result

    # ------------------------------------------------------------------
    # requests
    # ------------------------------------------------------------------

    def counters(self):
        """The transport's own health counters, right now."""
        link = self._link

        if link is None:
            return {}

        return {
            "corrupt_frames": link.corrupt_frames,
            "salvaged_frames": link.salvaged_frames,
            "stale_frames": link.stale_frames,
            "oversized_lines": link.oversized_lines,
            "bytes_read": link.bytes_read,
        }

    def request(self, command, timeout=None, retries=0, **payload):
        """
        One command, timed, with the transport counters either side.

        Returns a transaction record rather than only the answer,
        because a hardware result needs to carry what it cost as well as
        what it said.
        """
        link = self.require_link("send {}".format(command))

        before = self.counters()
        started = time.perf_counter()

        record = {
            "command": command,
            "payload": dict(payload),
            "timeout": timeout,
            "retries": retries,
            "started_monotonic": started,
        }

        try:
            data = link.request(command, timeout=timeout, retries=retries,
                                **payload)

            record["ok"] = True
            record["data"] = data

        except Exception as error:
            record["ok"] = False
            record["error"] = self._error_fields(error)

            normalized = self._normalize(error, command)

        else:
            normalized = None

        record["elapsed_s"] = round(time.perf_counter() - started, 6)
        record["elapsed_ms"] = round(record["elapsed_s"] * 1000.0, 3)

        after = self.counters()

        record["counters_before"] = before
        record["counters_after"] = after
        record["counters_delta"] = {
            key: after.get(key, 0) - before.get(key, 0)
            for key in after
        }

        # The damaged frames themselves, when the link kept any. This is
        # the evidence that distinguishes a CP210x packet artefact from
        # firmware that builds bad JSON.
        if link.damaged_lines:
            record["damaged_lines"] = list(link.damaged_lines)[-3:]

        self.transactions.append(record)
        self.context.event("transaction", **_loggable(record))

        if normalized is not None:
            normalized.data["transaction"] = _loggable(record)

            raise normalized

        return record

    def data(self, command, **kwargs):
        """`request` when only the answer is wanted."""
        return self.request(command, **kwargs)["data"]

    # ------------------------------------------------------------------

    def _error_fields(self, error):
        fields = {
            "type": type(error).__name__,
            "message": str(error),
        }

        for attribute in ("code", "data"):
            if hasattr(error, attribute):
                fields[attribute] = getattr(error, attribute)

        return fields

    def _normalize(self, error, what):
        """
        Production exception -> AdapterError, with everything preserved.

        `LinkError` and `DeviceError` already carry a code an operator
        can act on; that code becomes the AdapterError's code so the
        classification survives, and the original object is kept so a
        test that wants to check `isinstance` still can.
        """
        module = self._module

        code = getattr(error, "code", None)

        if module is not None and isinstance(error, module.DeviceError):
            kind = "DEVICE"

        elif module is not None and isinstance(error, module.LinkError):
            kind = "LINK"

        else:
            kind = "UNEXPECTED"

        return AdapterError(
            "{} failed: {}".format(what, error),
            code=code or kind,
            original=error,
            data={"kind": kind, "what": what,
                  "device_data": getattr(error, "data", None)},
        )


def _loggable(record):
    """
    A transaction, trimmed for the event log.

    The full 54-channel spectrum belongs in measurements.csv and in the
    test's own evidence, not repeated in every event line - events.jsonl
    has to stay readable when an endurance run has written 20,000 of
    them.
    """
    trimmed = dict(record)

    data = trimmed.get("data")

    if isinstance(data, dict):
        summary = {}

        for key, value in data.items():
            if isinstance(value, (list, tuple)) and len(value) > 8:
                summary[key] = "<{} entries>".format(len(value))

            elif isinstance(value, dict) and len(value) > 12:
                summary[key] = "<{} keys>".format(len(value))

            else:
                summary[key] = value

        trimmed["data"] = summary

    trimmed.pop("started_monotonic", None)

    return trimmed
