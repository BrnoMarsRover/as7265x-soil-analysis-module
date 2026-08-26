"""
Does the suite actually notice when the software becomes wrong?

WHY THIS IS NOT A TEST SUITE

Every other file here asks "is the software right?". This one asks the
question behind it: "would we know if it were not?" A suite of 2000
passing checks that survives having its safety rules deleted is a suite
that proves nothing, and there is no way to find that out except by
breaking the software on purpose and watching.

    py mutation.py            every mutation
    py mutation.py stale      only mutations whose name matches

HOW IT WORKS, AND WHY IT IS SAFE

Each mutation is a textual edit to one production file. The file is
read, patched, written, the named suites are run, and the ORIGINAL text
is restored in a `finally` - so an interrupted run cannot leave the
repository modified. The last thing the script does is verify, by
hash, that every file it touched is byte-identical to how it started.

WHAT IS MUTATED

Only decisions that can cause an operational failure: a lost stale
frame, a movement that repeats, a position that stays valid after a
failure, a rollback that does not happen, a NaN that reaches a servo.
Cosmetic branches are not mutated - a surviving mutation has to MEAN
something or the number is noise.

READING THE RESULT

    KILLED     a suite failed. The suite protects that behaviour.
    SURVIVED   every suite still passed. THAT IS A HOLE - the software
               can be made wrong in that way and nothing notices.
    NOT APPLIED  the pattern was not found; the code has moved and the
               mutation needs rewriting.
"""

import hashlib
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIRMWARE = HERE.parent.parent

# Spelled as constants so no editing tool can mangle the escapes.
CRLF_BYTES = bytes([13, 10])
CRLF_TEXT = chr(13) + chr(10)
LF_TEXT = chr(10)

# (name, file, find, replace, suites, why it matters)
#
# `find` must be unique in the file: a mutation that patches three
# places at once cannot be attributed to one behaviour.
MUTATIONS = (
    (
        "stale-frame: accept any matching id",
        "PC/serial_link.py",
        "        return answered is None or answered == cmd",
        "        return True",
        ("fault_injection/test_serial_faults.py",
         "regression/test_regressions.py"),
        "A previous session's answer becomes this session's result.",
    ),
    (
        "stale-frame: drop the session nonce",
        "PC/serial_link.py",
        "        self.session = os.urandom(3).hex()",
        '        self.session = "fixed"',
        ("contracts/test_request_identity.py",
         "regression/test_regressions.py"),
        "Every session produces the same ids, so a dead client's "
        "answers are indistinguishable from the next client's.",
    ),
    (
        "NaN: allow it onto the wire",
        "PC/serial_link.py",
        "                line = json.dumps(message, allow_nan=False) + \"\\n\"",
        "                line = json.dumps(message) + \"\\n\"",
        ("unit/test_prompts.py", "regression/test_regressions.py"),
        "A bare NaN is written to the serial wire as JSON.",
    ),
    (
        "NaN: accept it as a carousel angle",
        "PC/workflow/prompts.py",
        "        if value != value or value in (float(\"inf\"), float(\"-inf\")):",
        "        if False:",
        ("unit/test_prompts.py", "regression/test_regressions.py"),
        "NaN passes both range checks and becomes a servo goal.",
    ),
    (
        "closed link: raise RuntimeError again",
        "PC/serial_link.py",
        "            raise self._closed_error(cmd)",
        "            raise RuntimeError(\"Link is not open; call open() first.\")",
        ("regression/test_linux_bench.py",),
        "PORT_LOST then any screen kills the application.",
    ),
    (
        "movement: claim nothing moved",
        "ESP32/protocol.py",
        "            detail.setdefault(\"moved\", True)",
        "            detail[\"moved\"] = False",
        ("regression/test_linux_bench.py",),
        "A half turn that happened is reported as not having happened.",
    ),
    (
        "movement: report MOVED as NOT_STARTED",
        "ESP32/carousel.py",
        "        if motion.get(\"encoder_moved\"):",
        "        if False:",
        ("regression/test_linux_bench.py",),
        "The operator is told to leave a mechanism that has moved.",
    ),
    (
        "carousel: keep the position valid after a failure",
        "ESP32/carousel.py",
        "        self.invalidate_position(reason)",
        "        pass",
        ("state_machine/test_carousel_states.py",
         "fault_injection/test_device_faults.py"),
        "A failed movement leaves a position nobody verified.",
    ),
    (
        "servo: widen the tolerance to anything",
        "ESP32/servo.py",
        "        within = abs(error) <= config.ST3215_POSITION_TOLERANCE",
        "        within = True",
        ("regression/test_linux_bench.py",
         "state_machine/test_carousel_states.py"),
        "Every movement is declared successful wherever it stopped.",
    ),
    (
        "servo: break the circular error into a linear one",
        "ESP32/servo.py",
        "    return ((int(delta) + half) % counts_per_rev) - half",
        "    return int(delta)",
        ("regression/test_linux_bench.py",),
        "The 4095/0 seam becomes a 4095-count error.",
    ),
    (
        "samples: do not roll back after a failed write",
        "BD/samples.py",
        "            if self._durable is not None:",
        "            if False:",
        ("fault_injection/test_filesystem_faults.py",
         "state_machine/test_sample_lifecycle.py",
         "regression/test_regressions.py"),
        "A Sample the archive does not contain stays on screen.",
    ),
    (
        "samples: store RAW by reference",
        "BD/samples.py",
        "        record[\"raw\"] = copy.deepcopy(raw)",
        "        record[\"raw\"] = raw",
        ("fault_injection/test_filesystem_faults.py",
         "regression/test_regressions.py"),
        "The archive's copy of RAW is editable by any later caller.",
    ),
    (
        "samples: accept a SUCCESS with no RAW",
        "BD/samples.py",
        "        if not raw:",
        "        if False:",
        ("state_machine/test_sample_lifecycle.py",),
        "A successful measurement of nothing enters the record.",
    ),
    (
        "samples: stop validating the Sample ID",
        "BD/samples.py",
        "        if not (character.isalnum() or character in SAMPLE_ID_ALLOWED_EXTRA):",
        "        if False:",
        ("fault_injection/test_filesystem_faults.py",),
        "A path-shaped Sample ID reaches the filesystem.",
    ),
    (
        "samples: let a full disk escape as OSError",
        "BD/samples.py",
        "    except OSError as error:\n        raise StorageError(\n            \"Could not prepare a temporary file beside {}: {}\".format(",
        "    except ValueError as error:\n        raise StorageError(\n            \"Could not prepare a temporary file beside {}: {}\".format(",
        ("fault_injection/test_filesystem_faults.py",
         "regression/test_regressions.py"),
        "A full disk crashes the operator client.",
    ),
    (
        "science: let NaN through normalization",
        "Science/preprocessing.py",
        "        if not (_finite(sample.get(channel))",
        "        if False and not (_finite(sample.get(channel))",
        ("unit/test_numeric_edges.py",),
        "A NaN travels the pipeline looking like a reflectance.",
    ),
    (
        "science: return zero for an undefined channel",
        "Science/preprocessing.py",
        "        if denominator == 0:\n            result[channel] = None",
        "        if denominator == 0:\n            result[channel] = 0.0",
        ("unit/test_numeric_edges.py", "unit/test_science.py"),
        "An invented reflectance of zero enters every metric.",
    ),
    (
        "science: let cosine leave [-1, 1]",
        "Science/metrics.py",
        "    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))",
        "    return dot / (left_norm * right_norm)",
        ("unit/test_numeric_edges.py",
         "unit/test_science_properties.py"),
        "arccos of 1.0000000002 is NaN one line later.",
    ),
    (
        "sensor: trust the cached ready flag",
        "ESP32/sensor.py",
        "            if not probe:\n                return self.driver",
        "            return self.driver",
        ("regression/test_linux_bench.py",),
        "A sample is carried to the scanner on a dead sensor.",
    ),
    (
        "protocol: accept an unknown command",
        "ESP32/protocol.py",
        "            if method is None:",
        "            if False:",
        ("contracts/test_pc_firmware.py",),
        "A misspelled command is silently dispatched somewhere.",
    ),
    (
        "protocol: stop refusing an unsynchronised measurement",
        "ESP32/carousel.py",
        "        if not self.position_valid or self.current_scan_slot is None:\n            raise CarouselError(",
        "        if False:\n            raise CarouselError(",
        ("state_machine/test_carousel_states.py",
         "fault_injection/test_device_faults.py"),
        "Science is acquired at a position nobody declared.",
    ),
    (
        "display: format rank without a guard",
        "PC/workflow/display.py",
        "            rank_of(match),\n            str(match.get(\"material\"))[:32],",
        "            match.get(\"rank\"),\n            str(match.get(\"material\"))[:32],",
        ("unit/test_display_shapes.py", "integration/test_records.py"),
        "Opening a migrated record crashes the records browser.",
    ),
    (
        "link: retry a movement whose answer was damaged",
        "PC/serial_link.py",
        "    def move_slots(self, direction, slots=1):\n        return self.request(\"move_slots\", timeout=MOVE_TIMEOUT,",
        "    def move_slots(self, direction, slots=1):\n        return self.request(\"move_slots\", retries=2, timeout=MOVE_TIMEOUT,",
        ("fault_injection/test_serial_faults.py",),
        "The carousel turns twice for one instruction.",
    ),

    # ==================================================================
    # ROUND 2 - Phase A.3 section 82
    #
    # The first round mutated the decisions the campaign was built
    # around, and killed all 23. That is a strong result about a small
    # target. These go after the safety rules added or hardened SINCE,
    # and after three classes the first round did not reach at all: the
    # firmware's own validation, the host's frame-size policy, and the
    # honesty of the metrics.
    # ==================================================================

    (
        "frame-size: remove the unterminated-line cap",
        "PC/serial_link.py",
        "            if len(buffer) > MAX_FRAME_BYTES:",
        "            if False:",
        ("fault_injection/test_protocol_limits.py",),
        "A device that never sends a newline fills memory until the "
        "timeout - 2.08 MB per measure_raw, copied on every append.",
    ),
    (
        "frame-size: keep the cap but stop discarding",
        "PC/serial_link.py",
        "                buffer = \"\"\n\n            while \"\\n\" in buffer:",
        "                pass\n\n            while \"\\n\" in buffer:",
        ("fault_injection/test_protocol_limits.py",),
        "The overflow is counted and then the buffer grows anyway - "
        "the exact bug the first version of the fix had.",
    ),
    (
        "port-lost: catch SerialException only, as before",
        "PC/serial_link.py",
        "            except (serial.SerialException, OSError) as error:",
        "            except serial.SerialException as error:",
        ("linux/test_linux_runtime.py",),
        "in_waiting and flush raise a RAW OSError on Linux when the "
        "device node goes away, so the one failure this module exists "
        "to classify escapes as a traceback.",
    ),
    (
        "recursion: let a pathological frame exhaust the stack",
        "PC/serial_link.py",
        "        except (ValueError, RecursionError):\n            start = text.find(\"{\", start + 1)",
        "        except ValueError:\n            start = text.find(\"{\", start + 1)",
        ("fault_injection/test_protocol_limits.py",),
        "A line with ~17,000 opening brackets kills the client from "
        "console noise.",
    ),
    (
        "urandom: fall back to a fixed nonce",
        "PC/serial_link.py",
        "        except (OSError, NotImplementedError) as error:\n            raise RuntimeError(",
        "        except (OSError, NotImplementedError) as error:\n            self.session = \"000000\"\n            self._request_id = 0\n            return\n            raise RuntimeError(",
        ("fault_injection/test_resource_faults.py",),
        "Every session gets the same nonce again, silently - the "
        "cross-session collision the nonce exists to remove.",
    ),
    (
        "EOF: let a closed stdin spin the menus",
        "PC/workflow/prompts.py",
        "    return ask(prompt, eof_ends_session=True).strip().lower()",
        "    return ask(prompt).strip().lower()",
        ("process/test_lifecycle.py",),
        "Ctrl+D loops the main menu forever at full CPU, and the "
        "serial port is never released.",
    ),
    (
        "console: let an unencodable character kill a screen",
        "PC/rover_science_client.py",
        "        reconfigure = getattr(stream, \"reconfigure\", None)",
        "        reconfigure = None",
        ("linux/test_linux_runtime.py",),
        "A Czech note in a sample record crashes the records screen "
        "under LANG=C.",
    ),
    (
        "metrics: let a distance return infinity",
        "Science/metrics.py",
        "    return value if _finite(value) else None",
        "    return value",
        ("unit/test_science_properties.py",),
        "mae returns inf, which ranks, formats and reaches a metric "
        "table looking like a measurement.",
    ),
    (
        "startup-loop: drop StorageError from the startup screen",
        "PC/workflow/screen.py",
        "            except StorageError as error:\n                print()\n                print(\"Storage error: {} ({})\".format(\n                    error.message, error.code))",
        "            except LinkError:\n                pass",
        ("integration/test_screens_failing.py",),
        "A failed write reached from the startup screen kills the "
        "client, while the same screen reached from the main menu "
        "diagnoses it.",
    ),
    (
        "firmware: accept a command line of any length",
        "ESP32/protocol.py",
        "        if len(line) > config.MAX_COMMAND_BYTES:",
        "        if False:",
        ("fault_injection/test_firmware_faults.py",),
        "The device's only defence against a runaway host is removed.",
    ),
    (
        "firmware: let an internal error answer nothing",
        "ESP32/protocol.py",
        "            debug(\"internal error in '{}': {}\".format(cmd, error))",
        "            raise",
        ("fault_injection/test_firmware_faults.py",),
        "A programming defect becomes SILENCE, which the PC cannot "
        "tell apart from a dead board.",
    ),
    (
        "firmware: stop naming the exception type",
        "ESP32/protocol.py",
        "                    \"exception_type\": type(error).__name__,",
        "                    \"exception_type\": \"DeviceError\",",
        ("fault_injection/test_firmware_faults.py",),
        "A memory failure is filed as a device fault, and the operator "
        "chases the hardware.",
    ),
    (
        "firmware: let the chunked writer give up on a fragmented heap",
        "ESP32/protocol.py",
        "        except MemoryError:\n            for part in parts:\n                self._write(part)\n\n            return",
        "        except MemoryError:\n            return",
        ("fault_injection/test_firmware_faults.py",),
        "A response that cannot be joined is silently dropped instead "
        "of written in pieces.",
    ),
    # NOTE: "let a NaN through normalize" and "skip the rollback after
    # a failed write" were drafted here and then removed: round 1
    # already mutates both of those exact lines, at
    # "science: let NaN through normalization" and "samples: do not roll
    # back after a failed write". Two mutations of one line cannot be
    # attributed to one behaviour, which is the rule this table opens
    # with.
    # ==================================================================
    # ROUND 3 - the final closure review
    #
    # Two decisions from the section 26 priority list had no mutation:
    # what a RESET costs, and whether two feature spaces may be
    # compared. Both are on the list precisely because getting them
    # wrong produces a confident answer rather than a visible failure.
    # ==================================================================

    (
        "reset: let a fresh carousel start out synchronized",
        "ESP32/carousel.py",
        "        # Position tracking: unknown until the operator synchronizes.\n        self.position_valid = False",
        "        # Position tracking: unknown until the operator synchronizes.\n        self.position_valid = True",
        ("state_machine/test_reset_recovery.py",
         "state_machine/test_mission_model.py",
         "state_machine/test_carousel_states.py"),
        "A rebooted board claims to know where the carousel is. The "
        "mechanism may have been turned by hand while it was down, and "
        "a remembered number is indistinguishable from a measured one "
        "once it is on screen.",
    ),
    # WHY THIS MUTATES THE PIPELINE AND NOT `require_compatible`.
    #
    # The obvious target was `BD/channels.py::require_compatible`, the
    # validator that refuses to compare two feature spaces. Mutating it
    # SURVIVED - and the reason turned out to be that the function has
    # no callers anywhere in the tree.
    #
    # The protection is real, but it is by CONSTRUCTION rather than by
    # validation: `pipeline.analyze` selects the measured representation
    # from each database's own declared feature space, so a comparison
    # never crosses spaces to begin with. Removing an unused validator
    # cannot change behaviour, which made that mutation equivalent.
    #
    # This one mutates the decision that actually protects the property.
    (
        "science: feed a 54-feature library an 18-value spectrum",
        "Science/pipeline.py",
        "        elif handle.feature_space == AS7265X_54_MULTIILLUM:",
        "        elif False:",
        ("fault_injection/test_residual_handlers.py",
         "unit/test_science.py",
         "unit/test_science_properties.py"),
        "A 54-feature database is compared against the 18 white-light "
        "values by index. A DB1-shaped vector does not contain the UV "
        "and IR features that library expects, so the comparison "
        "invents data and returns a confident material name from it.",
    ),

    (
        "samples: leave the temporary file behind on failure",
        "BD/samples.py",
        "        try:\n            os.unlink(temporary)\n\n        except OSError:\n            pass",
        "        pass",
        ("fault_injection/test_resource_faults.py",),
        "Every failed save leaves a .samples-*.tmp beside the archive.",
    ),
)


def run_suite(relative):
    path = HERE / relative

    if not path.is_file():
        return None

    result = subprocess.run(
        [sys.executable, str(path)],
        capture_output=True, text=True, cwd=str(path.parent),
    )

    return result.returncode == 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    pattern = argv[0] if argv else None

    selected = [m for m in MUTATIONS
                if pattern is None or pattern.lower() in m[0].lower()]

    if not selected:
        print("No mutation matches {!r}.".format(pattern))

        return 2

    print("=" * 72)
    print("MUTATION VERIFICATION - can the suite tell right from wrong?")
    print("=" * 72)
    print()

    touched = {}

    for _name, relative, _f, _r, _s, _w in selected:
        path = FIRMWARE / relative

        if path not in touched:
            touched[path] = hashlib.sha256(path.read_bytes()).hexdigest()

    killed = []
    survived = []
    not_applied = []

    for name, relative, find, replace, suites, why in selected:
        path = FIRMWARE / relative

        # BINARY, not text. `read_text`/`write_text` translate line
        # endings on Windows, so a file stored with LF came back as
        # CRLF and the restore changed every line of it. The content
        # was right and the bytes were not - which the hash check at
        # the end caught, and which is exactly why that check exists.
        raw = path.read_bytes()

        # Normalise for MATCHING, restore the original BYTES.
        #
        # The working copy may hold CRLF while every `find` pattern
        # in this file is written with a bare newline, so a
        # multi-line pattern silently stops matching and the
        # mutation reports NOT APPLIED - which reads like a
        # maintenance note and is in fact an unverified behaviour.
        crlf = CRLF_BYTES in raw
        original = raw.decode("utf-8").replace(CRLF_TEXT, LF_TEXT)

        print("  {:<48}".format(name[:48]), end="", flush=True)

        if find not in original:
            print(" NOT APPLIED (pattern not found)")
            not_applied.append((name, why))

            continue

        if original.count(find) > 1:
            print(" NOT APPLIED (pattern is not unique)")
            not_applied.append((name, why))

            continue

        try:
            mutated = original.replace(find, replace, 1)

            if crlf:
                mutated = mutated.replace(LF_TEXT, CRLF_TEXT)

            path.write_bytes(mutated.encode("utf-8"))

            outcomes = [run_suite(suite) for suite in suites]
            detected = any(result is False for result in outcomes)

        finally:
            # ALWAYS, including on Ctrl+C. A mutation left in the tree
            # is a corrupted repository.
            path.write_bytes(raw)

        if detected:
            print(" KILLED")
            killed.append(name)

        else:
            print(" SURVIVED  <-- HOLE")
            survived.append((name, why, suites))

    print()
    print("=" * 72)
    print("  attempted    {}".format(len(selected)))
    print("  killed       {}".format(len(killed)))
    print("  survived     {}".format(len(survived)))
    print("  not applied  {}".format(len(not_applied)))
    print()

    if survived:
        print("SURVIVING MUTATIONS - each is a behaviour nothing checks:")
        print()

        for name, why, suites in survived:
            print("  {}".format(name))
            print("      consequence: {}".format(why))
            print("      ran: {}".format(", ".join(suites)))

        print()

    if not_applied:
        print("NOT APPLIED - the code moved; rewrite these:")

        for name, _why in not_applied:
            print("  {}".format(name))

        print()

    # The repository must be exactly as it was.
    damaged = [
        str(path.relative_to(FIRMWARE))
        for path, digest in touched.items()
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest
    ]

    if damaged:
        print("!! FILES NOT RESTORED: {}".format(", ".join(damaged)))
        print("   Restore them with git before doing anything else.")

        return 2

    print("  every mutated file restored, byte for byte ({} files)".format(
        len(touched)))

    return 1 if (survived or not_applied) else 0


if __name__ == "__main__":
    sys.exit(main())
