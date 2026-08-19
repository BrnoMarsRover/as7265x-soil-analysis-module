# protocol/router.py
# Command dispatch, error envelopes, and the serving loop.
#
# One request in, exactly one response frame out - always. A command that
# fails, a command that does not exist, a payload that is not JSON and an
# unexpected internal fault all produce a well-formed answer, because the
# PC blocks waiting for a line terminator and a silent failure looks
# identical to a dead board.
#
# The router knows nothing about hardware. It is handed a table of
# handlers by main.py and translates between JSON frames and calls.

import json
import time

import config

from protocol import transport
from protocol.transport import debug, send_json


class CommandError(Exception):
    """
    Rejected command carrying a machine-readable code for the PC.

    super().__init__ - MicroPython has no unbound Exception.__init__.
    """

    def __init__(self, code, message, data=None):
        super().__init__(message)

        self.code = str(code)
        self.message = str(message)
        self.data = data


class Router:
    """
    Maps command names to handlers and answers every frame exactly once.

    `error_types` is a list of (exception class, converter) pairs, so the
    domain exceptions - a carousel fault, a servo fault, a sensor fault -
    each turn into their own error envelope without the router needing to
    import any of them directly.
    """

    def __init__(self, handlers=None, error_types=()):
        self.handlers = dict(handlers or {})
        self.error_types = tuple(error_types)

    def register(self, handlers):
        """Add a group of commands. Duplicate names are a programming error."""
        for name in handlers:
            if name in self.handlers:
                raise ValueError(
                    "command '{}' is already registered".format(name)
                )

            self.handlers[name] = handlers[name]

        return self

    def command_names(self):
        return sorted(self.handlers.keys())

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------

    def dispatch(self, request):
        """Run one parsed request and return the response object."""
        request_id = None
        cmd = None

        try:
            if not isinstance(request, dict):
                raise CommandError(
                    "INVALID_REQUEST", "Command must be a JSON object."
                )

            request_id = request.get("request_id")
            cmd = request.get("cmd")

            handler = self.handlers.get(cmd)

            if handler is None:
                raise CommandError(
                    "UNKNOWN_COMMAND",
                    "Unknown command '{}'. Known commands: {}.".format(
                        cmd, ", ".join(self.command_names())
                    ),
                )

            return {
                "request_id": request_id,
                "ok": True,
                "cmd": cmd,
                "data": handler(request),
            }

        except CommandError as error:
            response = {
                "request_id": request_id,
                "ok": False,
                "cmd": cmd,
                "error": {"code": error.code, "message": error.message},
            }

            if error.data is not None:
                response["data"] = error.data

            return response

        except Exception as error:
            for error_type, convert in self.error_types:
                if isinstance(error, error_type):
                    response = {
                        "request_id": request_id,
                        "ok": False,
                        "cmd": cmd,
                    }
                    response.update(convert(error))

                    return response

            # An unexpected fault must still produce a well-formed answer,
            # otherwise the PC blocks waiting for a reply.
            # type(error).__name__ - never type(error) itself, which is a
            # class object and cannot be serialized.
            debug("internal error in '{}': {}".format(cmd, error))

            return {
                "request_id": request_id,
                "ok": False,
                "cmd": cmd,
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": str(error),
                    "exception_type": type(error).__name__,
                    "exception_message": str(error),
                },
            }

    def process_line(self, line):
        """Parse one received line and answer it with exactly one frame."""
        if len(line) > config.MAX_COMMAND_BYTES:
            send_json({
                "request_id": None,
                "ok": False,
                "error": {
                    "code": "COMMAND_TOO_LONG",
                    "message": "Command exceeded {} bytes.".format(
                        config.MAX_COMMAND_BYTES
                    ),
                },
            })

            return

        try:
            request = json.loads(line)

        except (ValueError, TypeError):
            send_json({
                "request_id": None,
                "ok": False,
                "error": {
                    "code": "INVALID_JSON",
                    "message": "Command is not valid JSON.",
                },
            })

            return

        send_json(self.dispatch(request))

    def serve_forever(self):
        """
        Wait for commands and execute them, one at a time, forever.

        There is no automatic activity of any kind: the module does
        nothing at all until the main computer asks for something.
        """
        while True:
            line = transport.read_command()

            if not line:
                # Empty line or end of input. Pause briefly so a closed
                # console cannot turn this into a busy loop.
                time.sleep_ms(config.IDLE_DELAY_MS)

                continue

            try:
                self.process_line(line)

            except Exception as error:
                # process_line answers its own errors; reaching here means
                # the transport itself failed. Keep serving rather than
                # dropping to the REPL.
                debug("command loop error:", error)
