"""
Audit every command response against MicroPython's stricter JSON rules.

CPython's json accepts things MicroPython's does not (tuples, non-string
dict keys), so a response that serializes fine on the PC harness can
still explode on the board.
"""

import json
import os
import shutil
import sys
import tempfile

import test_firmware as fw

ALLOWED = (str, int, float, bool, type(None), dict, list)


def scan(value, path="response", problems=None):
    if problems is None:
        problems = []

    if isinstance(value, bool) or value is None:
        return problems

    if isinstance(value, (str, int, float)):
        return problems

    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                problems.append(
                    "{}: NON-STRING KEY {!r} ({})".format(
                        path, key, type(key).__name__
                    )
                )

            scan(item, "{}.{}".format(path, key), problems)

        return problems

    if isinstance(value, list):
        for index, item in enumerate(value):
            scan(item, "{}[{}]".format(path, index), problems)

        return problems

    if isinstance(value, tuple):
        problems.append(
            "{}: TUPLE (MicroPython json may reject)".format(path)
        )

        for index, item in enumerate(value):
            scan(item, "{}[{}]".format(path, index), problems)

        return problems

    problems.append(
        "{}: UNSUPPORTED {} -> {!r}".format(
            path, type(value).__name__, value
        )
    )

    return problems


def main():
    wd = tempfile.mkdtemp(prefix="freya_json_")
    for n in ("database.json", "references.json"):
        shutil.copy(os.path.join(fw.FIRMWARE, n), os.path.join(wd, n))
    os.chdir(wd)

    main_mod = fw.load_main_module()
    import config

    config.STARTUP_DELAY_SECONDS = 0
    config.SERVO_SETTLE_TIME = 0
    config.SCAN_SETTLE_TIME = 0
    config.NEXT_SLOT_CW_MS = 1
    config.NEXT_SLOT_CCW_MS = 2
    config.LOAD_TO_SCAN_CW_MS = 1
    config.SCAN_TO_LOAD_CCW_MS = 1
    config.SERVO_INTER_STEP_PAUSE_MS = 0

    sci = main_mod.ScienceModule()
    sci.boot()

    from as7265x import SoilMeasurementSystem

    drv = fw.FakeDriver({c: 123.456 for c in "ABCDEFGHIJKLRSTUVW"})
    sci.driver = drv
    sci.sensor = SoilMeasurementSystem(drv)
    sci.sensor_error = None
    sci.install_references()

    commands = [
        {"cmd": "ping"},
        {"cmd": "get_status"},
        {"cmd": "help"},
        {"cmd": "get_references"},
        {"cmd": "get_database_status"},
        {"cmd": "get_material_names"},
        {"cmd": "sync_position", "load_slot": 1},
        {"cmd": "get_carousel_status"},
        {"cmd": "select_slot", "slot": 2},
        {"cmd": "move_slots", "direction": "cw", "slots": 1},
        {"cmd": "fine_adjust", "degrees": 1.0},
        {"cmd": "prepare_load", "slot": 2, "sample_id": "JS01"},
        {"cmd": "confirm_loaded", "slot": 2},
        {"cmd": "measure_sample", "slot": 2},
        {"cmd": "get_slots"},
        {"cmd": "list_samples"},
        {"cmd": "get_sample", "sample_id": "JS01"},
        {"cmd": "clear_slot", "slot": 2},
        {"cmd": "measure_sample", "slot": 5},          # error path
        {"cmd": "nope"},                               # unknown command
    ]

    print("=" * 62)
    print(" JSON SAFETY AUDIT")
    print("=" * 62)

    total = 0
    worst = 0

    for request in commands:
        # select_slot needs a fresh selection each loop
        response = sci.dispatch_command(dict(request, request_id="1"))

        problems = scan(response)
        size = len(json.dumps(response))
        worst = max(worst, size)

        flag = "OK  " if not problems else "FAIL"
        print("  {} {:<22} {:>6} bytes".format(
            flag, request["cmd"], size
        ))

        for p in problems:
            print("       " + p)
            total += 1

    print()
    print("largest response: {} bytes".format(worst))
    print("{} problem(s)".format(total))

    shutil.rmtree(wd, ignore_errors=True)

    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
