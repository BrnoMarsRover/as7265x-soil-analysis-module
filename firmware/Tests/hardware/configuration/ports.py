"""
Turning a selector into exactly one device, or into an error.

THE DEFECT THIS PREVENTS

`/dev/ttyUSB0` is not a name, it is a race. Linux hands out ttyUSBn in
enumeration order, so a second USB serial device - a second rover board,
a debug probe, an Arduino somebody left plugged in - moves the science
module to ttyUSB1 without anything visibly changing. A campaign that
defaults to ttyUSB0 then opens the wrong device, and the failure it
reports is a failure of an instrument that was never under test.

So there is no default, and an ambiguous selector is an error rather
than a choice. Two matches means the operator has to say which one; the
resolver will not pick.

ENUMERATION IS NOT OPENING. `list_ports.comports()` reads what the OS
already knows and opens nothing, which is why B0 can inventory the bench
without touching a device. The framework still only calls it in a real
run: --list, --describe and --dry-run never reach this module.
"""

from pathlib import Path


class PortError(Exception):
    """No device, too many devices, or a selector that names neither."""

    def __init__(self, code, message, candidates=None):
        super().__init__(message)

        self.code = code
        self.message = message
        self.candidates = candidates or []


def available_ports(serial_link_module):
    """
    Every serial port the OS reports, through the production enumerator.

    Deliberately routed through `SerialLink.available_ports` rather than
    importing pyserial here: one owner of the serial library, which is
    the rule the PC layer already follows.
    """
    return list(serial_link_module.SerialLink.available_ports())


def by_id_directory():
    """/dev/serial/by-id, if this is a Linux machine that has one."""
    path = Path("/dev/serial/by-id")

    return path if path.is_dir() else None


def by_id_entries():
    """
    The stable names Linux gives USB serial devices, with their targets.

    These survive replug and renumbering, which is exactly what ttyUSBn
    does not, so a bench profile should name one of these.
    """
    directory = by_id_directory()

    if directory is None:
        return []

    entries = []

    for link in sorted(directory.iterdir()):
        try:
            target = link.resolve()

        except OSError:                                # pragma: no cover
            target = None

        entries.append({
            "by_id": str(link),
            "device": str(target) if target else None,
        })

    return entries


def describe(entry):
    return "{}  {}  [{}]".format(
        entry.get("port"), entry.get("description") or "",
        entry.get("hwid") or "")


def parse_hwid(hwid):
    """
    VID, PID and serial number out of pyserial's hwid string.

    The format is stable enough across platforms to parse but not
    stable enough to trust blindly, so every field is optional and a
    string that does not parse yields an empty dict rather than an
    exception.
    """
    found = {}

    if not hwid:
        return found

    text = str(hwid)

    for token in text.replace(",", " ").split():
        if token.upper().startswith("VID:PID="):
            pair = token.split("=", 1)[1]

            if ":" in pair:
                vid, pid = pair.split(":", 1)

                found["vid"] = _hex(vid)
                found["pid"] = _hex(pid)

        elif token.upper().startswith("SER="):
            found["serial"] = token.split("=", 1)[1]

    return found


def _hex(text):
    try:
        return int(str(text), 16)

    except (TypeError, ValueError):
        return None


def resolve(selector, ports, by_id=None):
    """
    One device, or a PortError that says why not.

    Order of preference, most specific first:

        an explicit device path      the operator named it; trust them
        a by-id path                 stable across replug
        a USB serial number          unique per board
        VID/PID                      unique per model, not per board

    VID/PID matching more than one device is an ERROR, not a choice: two
    CP2102 boards on one bench is exactly the situation where picking
    the first would silently test the wrong instrument.
    """
    by_id = by_id if by_id is not None else []

    explicit = selector.get("port")

    if explicit:
        known = [p for p in ports if p.get("port") == explicit]

        if not known and ports:
            raise PortError(
                "PORT_NOT_PRESENT",
                "the profile names {} but the operating system does not "
                "report it. The board may not be plugged in, or it "
                "enumerated under a different name.".format(explicit),
                candidates=[describe(p) for p in ports],
            )

        return {
            "device": explicit,
            "matched_by": "explicit port",
            "detail": known[0] if known else None,
        }

    wanted_by_id = selector.get("port_by_id")

    if wanted_by_id:
        matches = [e for e in by_id
                   if e["by_id"] == wanted_by_id
                   or Path(e["by_id"]).name == wanted_by_id]

        if not matches:
            raise PortError(
                "BY_ID_NOT_FOUND",
                "the profile names by-id {} and nothing under "
                "/dev/serial/by-id matches it.".format(wanted_by_id),
                candidates=[e["by_id"] for e in by_id],
            )

        if len(matches) > 1:                           # pragma: no cover
            raise PortError(
                "BY_ID_AMBIGUOUS",
                "{} matches {} entries.".format(
                    wanted_by_id, len(matches)),
                candidates=[e["by_id"] for e in matches],
            )

        entry = matches[0]

        return {
            "device": entry["device"] or entry["by_id"],
            "matched_by": "by-id",
            "detail": entry,
        }

    wanted_serial = selector.get("usb_serial")
    wanted_vid = selector.get("usb_vid")
    wanted_pid = selector.get("usb_pid")

    matches = []

    for port in ports:
        identity = parse_hwid(port.get("hwid"))

        if wanted_serial:
            if identity.get("serial") == wanted_serial:
                matches.append((port, identity, "usb serial number"))

            continue

        if wanted_vid is not None and identity.get("vid") != wanted_vid:
            continue

        if wanted_pid is not None and identity.get("pid") != wanted_pid:
            continue

        if wanted_vid is None and wanted_pid is None:
            continue

        matches.append((port, identity, "usb vid/pid"))

    if not matches:
        raise PortError(
            "NO_MATCHING_DEVICE",
            "no serial device matches the profile's selector. Nothing "
            "will be opened: a campaign that guesses at a port can test "
            "the wrong instrument.",
            candidates=[describe(p) for p in ports],
        )

    if len(matches) > 1:
        raise PortError(
            "AMBIGUOUS_DEVICE",
            "{} devices match the profile's selector. Name the exact "
            "device with --port, or narrow the profile with a USB serial "
            "number. The framework will not choose for you.".format(
                len(matches)),
            candidates=[describe(p) for p, _i, _w in matches],
        )

    port, identity, how = matches[0]

    return {
        "device": port.get("port"),
        "matched_by": how,
        "detail": {"port": port, "identity": identity},
    }
