"""
One test per defect that ever reached the bench, or nearly did.

WHY THESE LIVE TOGETHER

Each of them is also covered by whichever suite is the natural home for
it. They are collected here as well, deliberately, because the value of
a regression test is not only that it runs - it is that somebody
reading it learns what went wrong, how it got past everything else, and
which line stops it coming back.

A defect with no test here is a defect that will be fixed twice.

THE FORMAT

    what broke, in one sentence
    how it got past the tests that existed
    what it cost, or would have cost
    the check
"""

import ast
import json
import sys
import tempfile
from pathlib import Path

_TESTS_DIR = next(
    p for p in Path(__file__).resolve().parents if p.name == "Tests"
)

if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

_SOFTWARE_DIR = _TESTS_DIR / "software"

if str(_SOFTWARE_DIR) not in sys.path:
    sys.path.insert(0, str(_SOFTWARE_DIR))

import support

support.add_project_root()
support.add_path("PC")
support.add_path("tools")

import serial_link                                          # noqa: E402
from serial_link import LinkError                            # noqa: E402

from fakes import FakeClock, SandboxBD, run_screen           # noqa: E402
from fakes.clock import install_clock                        # noqa: E402
from fakes.esp32 import LoopbackDevice                       # noqa: E402
from fakes.serial_port import install_fake_serial, open_link  # noqa: E402

checks = support.Checks("regressions")

FIRMWARE = support.FIRMWARE

restore_serial = install_fake_serial(serial_link)


def linked(device=None, **kwargs):
    clock = FakeClock()
    installed = install_clock(serial_link, clock)
    link, port = open_link(serial_link, device, clock=clock, **kwargs)
    link.online = True

    return link, port, clock, installed


# ======================================================================
checks.section("2026-08: sync_load_slot did not exist")

# WHAT BROKE
#   `mission.link.sync_load_slot(1)` in two branches of the carousel
#   screen. SerialLink has `sync_position(load_slot=...)`; there has
#   never been a `sync_load_slot`.
#
# HOW IT GOT PAST
#   Both branches are reachable only after a real ST3215 has answered.
#   855 checks passed, none of them executed the line.
#
# WHAT IT COST
#   AttributeError on the operator's first real carousel setup, after
#   everything it depends on had already worked.

from serial_link import SerialLink                            # noqa: E402

checks.ok(hasattr(SerialLink, "sync_position"),
          "SerialLink has sync_position")
checks.ok(not hasattr(SerialLink, "sync_load_slot"),
          "and no sync_load_slot, so the old spelling cannot come back "
          "unnoticed")

carousel_source = (FIRMWARE / "PC" / "workflow" / "carousel.py").read_text(
    encoding="utf-8")

checks.ok("sync_load_slot" not in carousel_source,
          "and the carousel screen does not name it")

# The general form: every link.<name> the PC layer uses must exist.
surface = set(dir(SerialLink)) | set(vars(SerialLink("PORT_TEST")))
used = []

for path in sorted((FIRMWARE / "PC").rglob("*.py")):
    if "__pycache__" in path.parts:
        continue

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "link"
                and node.attr not in surface):
            used.append("{}:{}: link.{}".format(
                path.relative_to(FIRMWARE).as_posix(), node.lineno,
                node.attr))

checks.equal(sorted(used), [],
             "and no other screen calls a command the link does not have")


# ======================================================================
checks.section("2026-08: a previous session's answer was accepted")

# WHAT BROKE
#   Request ids restarted at 1 every session, and open() deliberately
#   does not clear the receive buffer. A client that died with a
#   measure_raw in flight left the answer in the driver buffer; the
#   next client's first request had the same id and took it.
#
# HOW IT GOT PAST
#   Every test built a fresh fake port with an empty buffer.
#
# WHAT IT WOULD HAVE COST
#   The previous session's measurement returned as this session's
#   first result.

link, port, clock, installed = linked(link_kwargs={"timeout": 2.0})

try:
    link._request_id = 0
    port._enqueue(
        (json.dumps({"request_id": "1", "ok": True, "cmd": "measure_raw",
                     "data": {"stale": True}}) + "\n").encode("utf-8"))

    data = link.request("get_status")

    checks.ok(data.get("stale") is not True,
              "a leftover measure_raw answer is not returned as the "
              "answer to get_status")
    checks.equal(link.stale_frames, 1, "and the mismatch is counted")

finally:
    installed.restore()
    link.close()

link, port, clock, installed = linked()

try:
    checks.ok(str(link._request_id + 1) != "1",
              "and no session's first request is numbered 1 any more")

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("2026-08: NaN was a valid carousel angle")

# WHAT BROKE
#   `ask_float` accepted "nan". float("nan") parses, and NaN compares
#   False against every bound, so it passed both the minimum and the
#   maximum check and was returned as an angle.
#
# HOW IT GOT PAST
#   The range check looks correct. It is correct, for numbers.
#
# WHAT IT WOULD HAVE COST
#   json.dumps writes a bare `NaN`, which is not legal JSON and which
#   MicroPython refuses - so a servo goal of NaN, or a parse failure
#   reported against a command that looked ordinary.

from workflow import prompts                                 # noqa: E402

for text in ("nan", "NaN", "-nan", "inf", "-inf", "Infinity"):
    value, _output, _console = run_screen(
        [text, "1.0"],
        lambda: prompts.ask_float("Degrees", -15.0, 15.0))

    checks.close(value, 1.0,
                 "{!r} is refused at the keyboard and re-asked".format(
                     text))

link, port, clock, installed = linked()

try:
    for bad in (float("nan"), float("inf")):
        try:
            link.request("fine_adjust", degrees=bad)
            code = None

        except LinkError as error:
            code = error.code

        checks.equal(code, "INVALID_REQUEST",
                     "and the link refuses to serialize {} even if one "
                     "reaches it another way".format(bad))

    checks.equal(len(port.written), 0, "with nothing written to the wire")

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("2026-08: a full disk crashed the client")

# WHAT BROKE
#   `_write_json` created its temporary file and its parent directory
#   OUTSIDE the try that converts errors into StorageError. Every
#   screen catches StorageError; nothing catches OSError.
#
# HOW IT GOT PAST
#   No test had ever failed the filesystem.
#
# WHAT IT WOULD HAVE COST
#   A full SD card during a save takes down the operator client
#   mid-mission instead of saying "could not save".

from BD import samples as samples_module                     # noqa: E402
from BD.samples import SampleStore, StorageError             # noqa: E402

with SandboxBD() as bd:
    store = bd.sample_store()
    store.create("R001", 1)

    before = bd.samples_file.read_bytes()

    for target, exception in (("tempfile.mkstemp",
                               OSError(28, "No space left on device")),
                              ("os.replace",
                               OSError(13, "Permission denied"))):
        where, _dot, name = target.rpartition(".")
        module = getattr(samples_module, where)
        original = getattr(module, name)

        def raiser(*args, **kwargs):
            raise exception

        setattr(module, name, raiser)

        try:
            store.create("R002", 2)
            outcome = "ACCEPTED"

        except StorageError:
            outcome = "StorageError"

        except Exception as error:                     # noqa: BLE001
            outcome = type(error).__name__

        finally:
            setattr(module, name, original)

        checks.equal(outcome, "StorageError",
                     "{} failing produces a StorageError, which the "
                     "screens catch".format(target))

    checks.equal(bd.samples_file.read_bytes(), before,
                 "and the archive is unchanged after both")


# ======================================================================
checks.section("2026-08: a full disk lost a whole calibration")

# WHAT BROKE
#   The same shape as the Sample archive, in two more stores.
#   BD/calibrations.py and BD/acquisition_profiles.py wrote atomically
#   and correctly - and let an OSError escape. The calibration screen
#   called `mission.calibrations.save(document)` bare, twice.
#
# WHAT IT COST
#   A full disk while saving a freshly measured calibration took the
#   client down with a traceback. A calibration is a multi-minute
#   procedure with physical white and dark references placed in the
#   carousel by hand; restarting the client loses it.

from BD.calibrations import CalibrationError, CalibrationStore  # noqa: E402
from BD import calibrations as calibrations_module            # noqa: E402
from BD import acquisition_profiles as profiles_module        # noqa: E402
from BD.acquisition_profiles import (                         # noqa: E402
    AcquisitionProfileStore,
    ProfileError,
)

for module, error_type, label in (
    (calibrations_module, CalibrationError, "the calibration library"),
    (profiles_module, ProfileError, "the acquisition profiles"),
):
    directory = Path(tempfile.mkdtemp(prefix="freya-reg-"))
    original = module.os.replace

    def refuse(*args, **kwargs):
        raise OSError(28, "No space left on device")

    module.os.replace = refuse

    try:
        module._write_json(directory / "thing.json", {"a": 1})
        outcome = "ACCEPTED"

    except error_type:
        outcome = error_type.__name__

    except Exception as error:                         # noqa: BLE001
        outcome = type(error).__name__

    finally:
        module.os.replace = original

    checks.equal(outcome, error_type.__name__,
                 "a full disk while writing {} raises {}, which its "
                 "callers catch".format(label, error_type.__name__))

    leftovers = [p.name for p in directory.iterdir()
                 if p.name.endswith(".tmp")]

    checks.equal(leftovers, [],
                 "and leaves no .tmp file behind ({})".format(label))

# And no screen calls save() somewhere the failure cannot be caught.
#
# Parsed, not grepped: a substring search reports `report_save`'s own
# guarded call and its docstring, which is how a check like this ends
# up deleted for crying wolf. What matters is whether the call sits
# inside a `try`.

calibration_screen = FIRMWARE / "PC" / "workflow" / "calibration.py"
tree = ast.parse(calibration_screen.read_text(encoding="utf-8"),
                 filename=str(calibration_screen))

guarded_lines = set()

for node in ast.walk(tree):
    if isinstance(node, ast.Try):
        for child in ast.walk(node):
            if hasattr(child, "lineno"):
                guarded_lines.add(child.lineno)

unguarded = []

for node in ast.walk(tree):
    if (isinstance(node, ast.Call)
            and getattr(node.func, "attr", None) == "save"
            and getattr(node.func.value, "attr", None) == "calibrations"
            and node.lineno not in guarded_lines):
        unguarded.append("calibration.py:{}".format(node.lineno))

checks.equal(unguarded, [],
             "and every calibrations.save() in the screens is inside a "
             "try - a failure there used to raise straight past the "
             "menu loop, which catches LinkError and nothing else")


# ======================================================================
checks.section("2026-08: a failed save left a phantom Sample in memory")

# WHAT BROKE
#   Every mutator appended to `self.data` and then called `_write()`.
#   A failed write raised, and left the record in memory.
#
# HOW IT GOT PAST
#   Nothing had ever failed a write, so nothing had ever looked at what
#   the store believed afterwards.
#
# WHAT IT WOULD HAVE COST
#   The operator is told the save failed, and the screen still lists
#   the Sample. On exit it is gone.

with SandboxBD() as bd:
    store = bd.sample_store()
    store.create("R001", 1)

    original = samples_module.os.replace

    def refuse(*args, **kwargs):
        raise OSError(28, "No space left on device")

    samples_module.os.replace = refuse

    try:
        store.create("PHANTOM", 2)

    except StorageError:
        pass

    finally:
        samples_module.os.replace = original

    checks.ok(not store.has_sample("PHANTOM"),
              "the store in memory does not hold a Sample whose save "
              "failed")
    checks.ok(not bd.sample_store().has_sample("PHANTOM"),
              "and neither does the disk")
    checks.ok(store.has_sample("R001"),
              "while everything that WAS saved is still there")


# ======================================================================
checks.section("2026-08: RAW was stored by reference")

# WHAT BROKE
#   `record["raw"] = raw` kept the caller's dictionary. Anything that
#   touched it afterwards edited the archive's copy of what the
#   instrument reported.
#
# WHAT IT WOULD HAVE COST
#   The one number in the project that must be exactly what came off
#   the sensor, quietly editable by any later caller.

with SandboxBD() as bd:
    store = bd.sample_store()
    store.create("R001", 1)
    store.set_state("R001", "LOADED")

    raw = {"white": {"A": 1.0, "B": 2.0}}
    store.add_measurement("R001", raw=raw)

    raw["white"]["A"] = 999.0
    raw["white"]["INJECTED"] = 1.0
    raw["EXTRA_BLOCK"] = {"A": 1.0}

    stored = bd.sample_store().get_sample("R001")["measurements"][0]["raw"]

    checks.close(stored["white"]["A"], 1.0,
                 "editing the caller's dict does not change stored RAW")
    checks.ok("INJECTED" not in stored["white"],
              "nor add a channel to it")
    checks.ok("EXTRA_BLOCK" not in stored,
              "nor a whole illumination block")


# ======================================================================
checks.section("2026-08: --help rewrote a reference database")

# WHAT BROKE
#   `analyse_discriminability.py` had no argument parser. It ignored
#   every flag, including --help, ran the full analysis and rewrote
#   BD/DB3/DB3.json.
#
# HOW IT GOT PAST
#   Nothing ran the research entry points at all.
#
# WHAT IT COST
#   A protected reference library rewritten by the command an engineer
#   types when they do not know what a program does.

import hashlib                                               # noqa: E402
import subprocess                                            # noqa: E402

protected = sorted((FIRMWARE / "BD").rglob("*.json"))
before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}

script = FIRMWARE / "research" / "analyse_discriminability.py"

result = subprocess.run(
    [sys.executable, str(script), "--help"],
    capture_output=True, text=True, cwd=str(FIRMWARE.parent), timeout=120)

checks.equal(result.returncode, 0, "--help exits cleanly")
checks.ok("usage" in result.stdout.lower(),
          "and prints usage instead of doing the work")

after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}

checks.equal([p.name for p in protected if before[p] != after[p]], [],
             "and not one protected database changed")

result = subprocess.run(
    [sys.executable, str(script), "--not-a-real-flag"],
    capture_output=True, text=True, cwd=str(FIRMWARE.parent), timeout=120)

checks.ok(result.returncode != 0,
          "an unknown flag is REFUSED rather than ignored - being "
          "ignored is what made --help dangerous")

after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}

checks.equal([p.name for p in protected if before[p] != after[p]], [],
             "and that changed nothing either")


# ======================================================================
checks.section("2026-08: three names that did not exist")

# WHAT BROKE
#   spectral_features.first_derivative  (NameError)
#   from Science.decision import class_models  (ImportError)
#   research.erc.config.EXPECTED_GAIN  (AttributeError, swallowed)
#
# HOW THEY GOT PAST
#   The first two are in branches nothing executed. The third was
#   inside a try/except that caught the AttributeError and left the
#   configuration snapshot recording None for all three settings it
#   exists to pin down.

from research.training import dataset_builder                # noqa: E402
from research.training import validate as validate_module    # noqa: E402
from research.erc import prerun                             # noqa: E402

checks.ok(hasattr(dataset_builder, "features"),
          "dataset_builder imports Science.features by its real name")

builder_source = (FIRMWARE / "research" / "training"
                  / "dataset_builder.py").read_text(encoding="utf-8")

checks.ok("spectral_features" not in builder_source,
          "and never names spectral_features")

checks.ok(hasattr(validate_module, "class_models"),
          "validate.py imports class_models from Science, where it "
          "lives - the module used to fail on import")

payload = prerun.configuration_payload()

for key in ("expected_measurement_mode", "expected_integration_cycles",
            "expected_gain"):
    checks.ok(payload.get(key) is not None,
              "the configuration snapshot records {} instead of None"
              .format(key))

from Science import config as science_config                 # noqa: E402

checks.equal(payload["expected_gain"], science_config.EXPECTED_GAIN,
             "and it is the value Science declares, not a guess")


# ======================================================================
checks.section("2026-08: the bus scan cost the origin, silently")

# WHAT BROKE
#   A servo bus scan reopens UART2 at eight baud rates, so it releases
#   a connected servo and invalidates the carousel position. The
#   firmware does that correctly and reports it in `released_servo`.
#   The screen dropped the field and said only "MOVES NOTHING".
#
# WHAT IT COST
#   An operator who ran a scan from Tools mid-session lost the origin
#   they had aligned by hand, with nothing on screen to say why.

import contextlib                                            # noqa: E402
import io                                                    # noqa: E402

from workflow.display import print_bus_scan                  # noqa: E402

clock = FakeClock()
installed = install_clock(serial_link, clock)
loopback = LoopbackDevice()
loopback.build()
link, port = open_link(serial_link, loopback, clock=clock)
link.online = True

try:
    link.connect_servo()
    link.sync_position(load_slot=1)

    report = link.servo_bus_scan()

    checks.ok(report.get("released_servo"),
              "the firmware reports the release in the response")

    with contextlib.redirect_stdout(io.StringIO()) as out:
        print_bus_scan(report)

    printed = out.getvalue()

    checks.ok("RELEASED" in printed.upper(),
              "and the screen the operator reads says so")
    checks.ok("re-declare" in printed.lower() or "re-sync" in printed.lower(),
              "and tells them the origin must be declared again")

    checks.equal(link.get_status()["carousel"]["position_valid"], False,
                 "which is true: the position really is invalid")

finally:
    installed.restore()
    link.close()


# ======================================================================
checks.section("2026-08: Linux failures had no names")

# WHAT BROKE
#   pySerial's open() failures were classified by their Windows
#   exception text only.
#
# WHAT IT COST
#   On the operator's Linux machine, a missing /dev/ttyUSB0 and an
#   account outside the dialout group both arrived as
#   PORT_OPEN_FAILED, a code that names no action at all.

CASES = (
    ("[Errno 2] could not open port /dev/ttyUSB0: [Errno 2] No such "
     "file or directory: '/dev/ttyUSB0'", "PORT_NOT_FOUND"),
    ("[Errno 13] could not open port /dev/ttyUSB0: [Errno 13] "
     "Permission denied: '/dev/ttyUSB0'", "PORT_DENIED"),
    ("Could not exclusively lock port /dev/ttyUSB0: [Errno 11] "
     "Resource temporarily unavailable", "PORT_BUSY"),
)

for text, expected in CASES:
    error = serial_link._classify_open_failure("/dev/ttyUSB0",
                                               Exception(text))

    checks.equal(error.code, expected,
                 "a POSIX failure is named {}".format(expected))

denied = serial_link._classify_open_failure(
    "/dev/ttyUSB0",
    Exception("[Errno 13] Permission denied: '/dev/ttyUSB0'"))

checks.ok("dialout" in denied.message and "usermod" in denied.message,
          "and PORT_DENIED gives the command that fixes it")


# ======================================================================
checks.section("2026-08: a moved file broke its own path arithmetic")

# WHAT BROKE
#   `hardware_validation.py` computed FIRMWARE_DIR as `parent.parent`.
#   Moving it one directory deeper made that Tests/ instead of
#   firmware/, and it stopped finding the code it validates.
#
# HOW IT GOT PAST
#   It was found by moving the file, in this campaign. Hop counts are
#   correct until somebody moves the file, and then they are silently
#   wrong.

hardware_source = (FIRMWARE / "Tests" / "hardware"
                   / "hardware_validation.py").read_text(encoding="utf-8")

checks.ok('p.name == "firmware"' in hardware_source,
          "hardware_validation resolves firmware/ by NAME, not by "
          "counting parent hops")

checks.ok("FIRMWARE_DIR = TESTS_DIR.parent" not in hardware_source,
          "and the hop count that broke when it moved is gone")

# The same discipline everywhere a suite resolves the tree. The pattern
# is assembled rather than written out, so this file does not report
# itself.
HOPS = ".".join(["parent"] * 3)

hop_counted = []

for path in sorted((FIRMWARE / "Tests").rglob("*.py")):
    if "__pycache__" in path.parts:
        continue

    if path.resolve() == Path(__file__).resolve():
        continue

    if HOPS in path.read_text(encoding="utf-8-sig"):
        hop_counted.append(path.relative_to(FIRMWARE).as_posix())

checks.equal(hop_counted, [],
             "and no suite locates the tree by counting three parents "
             "either - every one of them walks up to a directory by "
             "name, so moving a file cannot silently repoint it")


restore_serial()

sys.exit(checks.report())
