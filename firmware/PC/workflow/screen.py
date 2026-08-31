"""
The main screen and the top-level menu loop.

One screen, redrawn from live state every time round: the carousel
comes from get_status, the Sample states come from the archive. Nothing
is cached between iterations, so a screen that says a slot is loaded is
saying what BD and the firmware say right now.
"""

from serial_link import DeviceError, LinkError

# NOT `carousel`. Six places in this file bind `carousel` to the
# carousel STATE DICT from get_status, which shadowed the module import
# in every one of them - so `carousel.get(...)` read as a call into
# workflow/carousel.py while being a plain dict lookup. The screens this
# file needs are imported by name below.
from workflow import calibration, measure, records

# `ui_status`, for the same reason display.py does it: this module binds
# a local named `status` to the get_status dict in almost every function.
from workflow import status as ui_status

from workflow.display import (
    print_system_status,
    report_failure,
    report_link_error,
)
from workflow.prompts import (
    RULE,
    ask,
    banner,
    choose,
    confirm,
    pause,
)

SLOT_COUNT = 4

import json
import sys
import textwrap

from workflow.session import Mission

from workflow.measure import (
    menu_choose_slot,
    menu_clear_slot,
    menu_confirm,
    menu_measure,
    menu_prepare,
)

from workflow.carousel import (
    menu_fine_adjust,
    menu_initial_calibration,
    menu_resync,
    menu_servo_test,
)

from workflow.calibration import menu_sensor_test

from BD.samples import (
    STATE_EMPTY,
    STATE_LOADED,
    STATE_MEASURED,
    STATE_READY_TO_LOAD,
    StorageError,
)


# The Tools screen. A table rather than a chain of elif branches, so
# adding an entry is one line and the help text cannot drift away from
# the action it describes.
TOOLS_MENU = (
    ("1", "Sample Database", "View and manage saved Samples.",
     lambda mission, status, view: records.menu_sample_database(mission)),
    ("2", "System Status", "Show PC, database and hardware state.",
     lambda mission, status, view: (print_system_status(mission), pause())),
    ("3", "Re-sync Carousel", "Restore physical position tracking.",
     lambda mission, status, view: menu_resync(mission)),
    ("4", "Servo / Carousel Tools",
     "ST3215 diagnostics, movement tests and manual movement.",
     lambda mission, status, view: menu_servo_test(mission)),
    ("5", "Sensor Test / Calibration",
     "Test the sensor and analysis pipeline; create a full calibration.",
     lambda mission, status, view: calibration.menu_sensor_test(mission)),
    ("6", "Clear Physical Slot", "Free a physical carousel slot.",
     measure.menu_clear_slot),
    ("7", "Import ESP32 Acquisitions to the PC Archive",
     "COPY every acquisition held in the ESP32 buffer into the "
     "permanent PC archive. The device keeps its own copies.",
     lambda mission, status, view: records.import_esp32_samples(mission)),
    ("8", "Decision Learning History",
     "What the system has measured, what it concluded, and what the "
     "samples actually were.",
     lambda mission, status, view: records.menu_learning_history(mission)),
)


def menu_tools(mission, status, view):
    while True:
        banner("TOOLS / RECORDS")

        for key, label, description, _handler in TOOLS_MENU:
            print("[{}] {}".format(key, label))
            print("    {}".format(description))
            print()

        print("[0] Back")

        selection = choose()

        if selection == "0":
            return

        handler = None

        for key, _label, _description, action in TOOLS_MENU:
            if selection == key:
                handler = action

                break

        if handler is None:
            if selection:
                print("Unknown option.")

            continue

        try:
            handler(mission, status, view)

            # Slot and carousel state may have changed underneath us.
            status = mission.hardware_status()
            view = mission.slot_view(status)

        except LinkError as error:
            report_link_error(error)

        except TimeoutError as error:
            print()
            print("Timeout: {}".format(error))

        except KeyboardInterrupt:
            print()
            print("Cancelled.")


# ======================================================================
# help
# ======================================================================

HELP_TEXT = """NORMAL COMPETITION WORKFLOW

  0. Initial Carousel Calibration - once, Slot 1 under the loading hole
  1. Choose Sample / Slot
  2. Prepare Sample          (opens the record for this run)
  3. Rover arm deposits soil
  4. Confirm Sample Loaded
  5. Measure Sample          (180 deg out, RAW, 180 deg back)
  6. Import it, when you want to keep it - see WHERE THINGS LIVE

CAROUSEL

  4 slots, 90 degrees apart. The scanner sits 180 degrees - two slots -
  from the loading hole:

      Loader Slot 1  ->  Scanner Slot 3
      Loader Slot 2  ->  Scanner Slot 4
      Loader Slot 3  ->  Scanner Slot 1
      Loader Slot 4  ->  Scanner Slot 2

  Measure Sample swings the slot out to the scanner and back again, so a
  successful measurement ends with the sample exactly where it started.

  The carousel is driven by an ST3215 serial bus servo with a 4096-count
  absolute encoder, so every movement is commanded in counts and then
  checked against the encoder before the software believes it. One slot
  is 1024 counts; the loader/scanner sweep is 2048. A movement that
  cannot be verified is reported as a failure and the tracked position is
  dropped - the software never assumes a movement worked.

  Initial Carousel Calibration is still needed after every power-up. The
  encoder knows exactly where the servo is, but nothing can tell it which
  physical slot you call Slot 1.

CALIBRATION

  The module uses one fixed Dark and one fixed White reference, stored
  as the protected LEGACY record inside the calibration database. They
  were accepted before the competition and are used for every
  comparison against DB1. You are never asked to measure them.

      C = Sample - Dark
      R = (Sample - Dark) / (White - Dark)

WHERE THINGS LIVE

  ESP32   carousel + AS7265x, RAW spectra only
  BD      White/Dark, material database, all analysis
  PC      workflow, Sample records, this interface

IF YOU CLOSE THIS PROGRAM RIGHT NOW, WHERE IS s01?

  Three stores, and only two of them survive that.

  ESP32       the device's own store, one acquisition per slot, on the
              ESP32's filesystem. SURVIVES a board reset, a power cut
              and a restart of this program. Only a delete removes it.

  PC session  this run's working set, in THIS PROGRAM'S MEMORY.
              Prepare and Measure write here. It is NOT a file: closing
              this program discards it. The measurement is not lost -
              the device still has it.

  PC archive  the permanent record, in firmware/BD/samples/samples.json.
              The ONLY PC store that survives closing this program, and
              nothing arrives in it except by an explicit import.

  Measuring a sample does NOT save it on this computer. That is
  deliberate: what gets kept is your decision, not a side effect.

IMPORTING - HOW A MEASUREMENT BECOMES A KEPT RECORD

  Tools -> [7] Import ESP32 Acquisitions to the PC Archive copies every
  acquisition the device is holding into the permanent PC archive. It
  never overwrites an existing Sample and never deletes the ESP32 copy.

  Tools -> [1] Sample Database does the same one sample at a time, and
  is also where every delete lives. Every delete there names exactly
  one store, because "delete the sample" is not a sentence this system
  can act on.

MEASURED IS NOT EMPTY

  The soil physically stays in the slot after a measurement. Use
  Tools -> Clear Physical Slot when it has been removed. That frees the
  mechanism ONLY: the measurement taken from the slot is kept, on the
  device and in this run. Removing a measurement is a delete in the
  Sample Database, and it names its store."""


def menu_help(mission, status, view):
    banner("HELP")

    print(HELP_TEXT)
    print()
    pause()


# ======================================================================
# main screen
# ======================================================================


def action_labels(entry, carousel):
    """
    What the operator can actually do right now.

    MEASURING NEEDS A TRUSTED POSITION. Measure Sample swings the
    carousel 180 degrees and back; with the position invalidated the
    firmware does not know which slot is at the loader, so the movement
    would be aimed at a slot number that means nothing.

    The main loop routes to the startup screen when the position is
    invalid, so this is a second line rather than the only one - but a
    label that reads [AVAILABLE] over an unknown position is exactly
    the misleading confidence §5 is about, and it must not depend on
    another function's routing to stay honest.
    """
    state = entry.get("state", STATE_EMPTY)
    phase = carousel.get("carousel_phase")
    synchronized = bool(carousel.get("position_valid"))

    labels = {"2": "", "3": "", "4": ""}

    if state == STATE_EMPTY:
        # [AVAILABLE] ONLY IF THE CUP IS ACTUALLY FREE. With no live
        # record this slot reads EMPTY, and the board may still be
        # reporting soil in it from a run this client never saw -
        # offering Prepare there invites a second sample into an
        # occupied cup. `menu_prepare` refuses it; the label must not
        # promise otherwise first.
        if ui_status.slot_is_free(entry):
            labels["2"] = "[AVAILABLE]"

        else:
            labels["2"] = "[LOCKED - the module reports soil in this slot]"

        labels["3"] = "[LOCKED - no sample prepared]"
        labels["4"] = "[LOCKED - no sample prepared]"

    elif state == STATE_READY_TO_LOAD:
        labels["2"] = "[DONE]"
        labels["3"] = "[AVAILABLE]"
        labels["4"] = "[LOCKED - sample not confirmed]"

    elif state == STATE_LOADED:
        labels["2"] = "[DONE]"
        labels["3"] = "[DONE]"
        labels["4"] = (
            "[LOCKED - carousel position unknown, re-sync]"
            if not synchronized
            else "[AVAILABLE]" if phase == "LOAD"
            else "[LOCKED - sample not at loading hole]"
        )

    elif state == STATE_MEASURED:
        labels["2"] = "[DONE]"
        labels["3"] = "[DONE]"
        labels["4"] = "[DONE - MEASURED]"

    return labels


def print_main_screen(mission, status, view):
    carousel = status.get("carousel") or {}
    selected = carousel.get("selected_slot")
    entry = mission.entry_for(view, selected) if selected else {}

    banner("FREYA SCIENCE MODULE")

    # THE FOUR SUBSYSTEMS, THEN THE SELECTION. §7.
    #
    # What an operator needs from this screen in two seconds is whether
    # each layer is working and where the carousel is - not the servo's
    # part number and its feedback type, which is what stood here and is
    # telemetry that belongs in setup. Deep servo detail is one keypress
    # away in Carousel Setup and Diagnostics; it is not duplicated here.
    ui_status.print_fields((
        ("System", ui_status.ONLINE if mission.link.online
         else ui_status.UNREACHABLE),
        ("Sensor", ui_status.sensor_label(status)),
        ("Servo", ui_status.servo_link(status)),
        ("Carousel", ui_status.carousel_label(status)),
    ))

    print()
    print(ui_status.field("Selected", "Slot {} / {} / {}".format(
        selected,
        entry.get("sample_id") or "----",
        entry.get("state", "-"),
    )))

    print()
    print("Loader: Slot {}    Scanner: Slot {}".format(
        carousel.get("current_load_slot"),
        carousel.get("current_scan_slot"),
    ))
    print()

    for item in view:
        print("{}  {:<8} {}".format(
            item["slot_id"], item["sample_id"] or "----",
            ui_status.slot_state_label(item),
        ))

    if mission.science_error:
        print()
        print("BD: {}".format(mission.science_error))

    if mission.active_calibration is None:
        print()
        print("NO ACTIVE CALIBRATION - UV/IR reflectance is not available.")
        print("Tools -> Sensor Test -> [7] to select a stored calibration,")
        print("or [3] to make one. The material library is unaffected.")

    labels = action_labels(entry, carousel)

    print()
    print("[1] Choose Sample / Slot")
    print("[2] Prepare Sample {}".format(labels["2"]))
    print("[3] Confirm Sample Loaded {}".format(labels["3"]))
    print("[4] Measure Sample {}".format(labels["4"]))
    print("[5] Fine Carousel Alignment")
    print()
    print("[t] Tools / Records")
    print("[h] Help")
    print("[q] Exit")


def print_startup_screen(status=None):
    """
    The screen shown while the carousel position is not trusted.

    A SERVO THAT IS ANSWERING IS NEVER ASKED TO BE CONNECTED. §5.

    This screen printed "connect the ST3215 carousel servo" whenever
    `position_valid` was false, and position is invalidated by a failed
    movement as well as by a missing servo. So a SERVO_POSITION_MISMATCH
    - which is a mechanism or feedback fault, with the servo answering
    normally throughout - dropped the operator here and told them to
    connect hardware that had never disconnected. The real instruction,
    re-sync, was nowhere on the screen.

    The two states are now distinguished by what the firmware reports,
    and `[0]` is labelled with whichever one is actually true.
    """
    banner("FREYA SCIENCE MODULE")

    online = ui_status.servo_online(status)

    ui_status.print_fields((
        ("Servo", ui_status.servo_link(status)),
        ("Sensor", ui_status.sensor_label(status) if status else None),
        ("Carousel", ui_status.POSITION_UNKNOWN),
    ))

    print()

    if online:
        # The servo is fine. The only missing fact is which physical
        # slot is Slot 1, and only a person can supply it.
        print("The servo is answering; the carousel position is not")
        print("trusted. Align physical Slot 1 with the loading hole and")
        print("confirm it. Nothing moves until you do.")
        print()
        print("[0] Re-sync Carousel")

    else:
        print("Before working with samples:")
        print("  1. connect the ST3215 carousel servo")
        print("  2. align physical Slot 1 with the soil loading hole")
        print()
        print("[0] Carousel Setup")

    print("[t] Tools / Records")
    print("[h] Help")
    print("[q] Exit")


MAIN_ACTIONS = {
    "1": menu_choose_slot,
    "2": menu_prepare,
    "3": menu_confirm,
    "4": menu_measure,
    "5": lambda mission, status, view: menu_fine_adjust(mission),
    "t": menu_tools,
    "h": menu_help,
}


def interactive(link):
    """Calibrate the carousel once, then run the sample loop."""
    print("Connecting to the science module on {}...".format(link.port))

    try:
        link.wait_online()

    except (LinkError, TimeoutError) as error:
        print("No answer from the science module: {}".format(error))
        print()
        print("Check that the USB cable is connected, that the port is "
              "correct, and that no other program (REPL, mpremote, serial "
              "monitor) is holding the port open.")

        return 1

    print("Connection: ONLINE")

    # BUILD IDENTITY, ON THE LINE THE OPERATOR ALREADY READS.
    #
    # `new PC code + old ESP32 firmware` is the failure this prevents:
    # it presents as dead keys and missing commands, which look like
    # hardware faults and cost bench time. The firmware bumps
    # PROTOCOL_VERSION whenever the command surface changes, so a
    # mismatch here is proof the device is not running this source.
    print("Firmware:   {} {}  (protocol {})".format(
        link.firmware_name or "?",
        link.firmware_version or "?",
        link.device_protocol_version
        if link.device_protocol_version is not None else "?",
    ))

    if link.protocol_mismatch:
        print()
        print("!! PROTOCOL MISMATCH: this client expects {}, the board "
              "reports {}.".format(link.protocol_mismatch["expected"],
                                   link.protocol_mismatch["device"]))
        print("   The device is not running this source. Re-deploy, or")
        print("   expect missing commands and absent response fields:")
        print("     py firmware/tools/device.py deploy --port {} --clean"
              .format(link.port))
        print()

    mission = Mission(link)

    if mission.science_error:
        print("BD warning: {}".format(mission.science_error))

    while True:
        try:
            status = mission.hardware_status()

        except (LinkError, TimeoutError) as error:
            print()
            print("Could not read the hardware state: {}".format(error))

            # A CLOSED LINK CANNOT BE RETRIED, SO DO NOT OFFER IT.
            #
            # PORT_LOST closes the link and clears the serial handle.
            # Nothing in this loop re-opens it, so every "Retry?" after
            # that raises PORT_CLOSED again - forever, with a prompt
            # that defaults to Yes. An operator whose USB cable fell out
            # pressed Enter, saw the same line, and had every reason to
            # think the client had hung. The cable being plugged back in
            # does not help either: the software link stays closed.
            #
            # The client is NOT re-opened automatically here on purpose.
            # Opening the port resets the ESP32, which drops the servo
            # connection and invalidates the carousel position - a
            # physical consequence that must not follow from a
            # keypress the operator meant as "try again".
            if getattr(error, "code", None) == "PORT_CLOSED":
                print()
                print("The connection is closed and this client cannot "
                      "re-open it.")
                print("Reconnect the module, then start the client "
                      "again:")
                print("  py firmware/PC/rover_science_client.py --port {}"
                      .format(mission.link.port))
                print()
                print("Re-opening the port resets the board, so the "
                      "carousel position")
                print("will need re-syncing after you reconnect.")

                return 1

            if choose("Retry? [Y/n]").startswith("n"):
                return 1

            continue

        view = mission.slot_view(status)
        carousel = status.get("carousel") or {}

        if not carousel.get("position_valid"):
            # The whole status dict, so the screen can tell a missing
            # servo from an unverified movement. `menu_initial_calibration`
            # behind [0] already connects only when nothing is connected,
            # so an online servo is never re-opened - it goes straight to
            # the alignment controls, which is the re-sync path. §5.
            print_startup_screen(status)

            selection = choose()

            if selection == "q":
                return 0

            try:
                if selection == "0":
                    menu_initial_calibration(mission)

                elif selection == "t":
                    menu_tools(mission, status, view)

                elif selection == "h":
                    menu_help(mission, status, view)

                elif selection:
                    print("Unknown option.")

            except LinkError as error:
                report_link_error(error)

            except TimeoutError as error:
                print("Timeout: {}".format(error))

            # THE SAME SET AS THE MAIN LOOP BELOW, and it has to be.
            #
            # These two loops dispatch to OVERLAPPING screens - `t`
            # reaches the whole tools menu from here, and the main
            # screen reaches it too - but they were not catching the
            # same things: the main loop handled StorageError and this
            # one did not. A tools screen that let a failed write
            # through would therefore be a diagnosed message from one
            # screen and a dead client from the other, decided by
            # whether the carousel happened to be synchronized.
            #
            # No screen reachable from here does propagate one today.
            # That is a fact about the current screens rather than a
            # property of this loop, and it is not a fact worth
            # depending on.
            except StorageError as error:
                print()
                print("Storage error: {} ({})".format(
                    error.message, error.code))

            except KeyboardInterrupt:
                print()
                print("Cancelled.")

            continue

        print_main_screen(mission, status, view)

        selection = choose()

        if selection == "q":
            return 0

        handler = MAIN_ACTIONS.get(selection)

        if handler is None:
            if selection:
                print("Unknown option.")

            continue

        try:
            handler(mission, status, view)

        except LinkError as error:
            report_link_error(error)

        except TimeoutError as error:
            print()
            print("Timeout: {}".format(error))

        except StorageError as error:
            print()
            print("Storage error: {} ({})".format(error.message, error.code))

        except KeyboardInterrupt:
            print()
            print("Cancelled.")


# ======================================================================
# entry point
# ======================================================================
