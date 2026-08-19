# protocol/transport.py
# Newline-delimited JSON over the USB serial console.
#
#     one command  = one JSON object followed by "\n"  on sys.stdin
#     one response = one JSON object followed by "\n"  on sys.stdout
#
# carried over the development board's CP2102 bridge, on the same cable
# that powers the ESP32.
#
# stdout IS the protocol stream. Anything printed there lands in the
# middle of the JSON the PC is parsing, which is why the firmware prints
# nothing outside a frame and why diagnostics go through a debug helper
# gated by config.DEBUG.
#
# This module owns the wire format and nothing else: it does not know
# what a command means, only how to read one and how to answer with
# exactly one well-formed frame.
#
# It is emphatically NOT the servo link. The ST3215 talks over UART2,
# which is a separate hardware peripheral owned by its driver. The two
# channels never share a byte.

import json
import sys
import time

import config


def debug(*parts):
    """Diagnostic output, silent unless config.DEBUG is switched on by hand."""
    if config.DEBUG:
        print(*parts)


def read_command():
    """Block for one line; None means stdin reported end of input."""
    line = sys.stdin.readline()

    if not line:
        return None

    return line.strip()


def make_json_safe(value, depth=0):
    """
    Coerce a payload into JSON-encodable primitives.

    Numbers stay numbers - spectral values must never be stringified.
    Anything genuinely unsupported (an exception, a driver, a UART) is
    replaced by its type name rather than breaking the response.
    """
    if depth > 12:
        return "<nested too deeply>"

    if value is None or isinstance(value, bool):
        return value

    if isinstance(value, (int, float, str)):
        return value

    if isinstance(value, dict):
        safe = {}

        for key in value:
            # MicroPython's json refuses non-string keys outright.
            safe_key = key if isinstance(key, str) else str(key)
            safe[safe_key] = make_json_safe(value[key], depth + 1)

        return safe

    if isinstance(value, (list, tuple)):
        return [make_json_safe(item, depth + 1) for item in value]

    return "<{}>".format(type(value).__name__)


def _ascii_only(text):
    """
    Escape non-ASCII as \\uXXXX.

    Keeps one byte per character, which is what makes the partial-write
    accounting in _write_all exact. \\uXXXX is valid JSON.
    """
    for character in text:
        if ord(character) > 127:
            break
    else:
        return text

    out = []

    for character in text:
        if ord(character) > 127:
            out.append("\\u{:04x}".format(ord(character)))
        else:
            out.append(character)

    return "".join(out)


def _write_all(text):
    """
    Write the whole string, however many calls that takes.

    sys.stdout on the USB console is non-blocking: one write() of a
    multi-kilobyte response does NOT necessarily send everything, and
    the unwritten tail is silently dropped - leaving the PC waiting
    forever for a line terminator. Honour the return value.
    """
    total = len(text)
    sent = 0

    while sent < total:
        chunk = text[sent:sent + config.STDOUT_CHUNK_BYTES]

        written = sys.stdout.write(chunk)

        if written is None:
            # Some ports return None and always write everything.
            written = len(chunk)

        if written <= 0:
            # Buffer full; give the USB stack a moment to drain.
            time.sleep_ms(2)

            continue

        sent += written

    try:
        sys.stdout.flush()

    except AttributeError:
        # Not every MicroPython build exposes flush() on the console.
        pass


def send_json(payload):
    """
    The single exit point for every response.

    Serialization completes IN FULL before a byte is written, so a value
    that cannot be encoded can never leave half an object on the wire.
    """
    try:
        text = json.dumps(payload)

    except (TypeError, ValueError):
        try:
            text = json.dumps(make_json_safe(payload))

        except Exception as error:
            text = json.dumps({
                "request_id": payload.get("request_id")
                if isinstance(payload, dict) else None,
                "ok": False,
                "error": {
                    "code": "JSON_SERIALIZATION_ERROR",
                    "message": "Response contains a non-serializable "
                               "value.",
                    "exception_type": type(error).__name__,
                    "exception_message": str(error),
                },
            })

    # The leading newline is a guard, not decoration: it closes anything
    # already sitting on the console so this frame gets a line of its
    # own. See config.RESPONSE_GUARD_NEWLINE.
    prefix = "\n" if config.RESPONSE_GUARD_NEWLINE else ""

    _write_all(prefix + _ascii_only(text) + "\n")

