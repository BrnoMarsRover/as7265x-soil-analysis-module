"""
The main screen and the top-level menu loop.

One screen, redrawn from live state every time round: the carousel
comes from get_status, the Sample states come from the archive. Nothing
is cached between iterations, so a screen that says a slot is loaded is
saying what BD and the firmware say right now.
"""

from serial_link import DeviceError, LinkError

from workflow import calibration, carousel, measure, records
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
    ("7", "Sync ESP32 Acquisitions to BD",
     "Copy acquisitions held in the ESP32 buffer into the BD archive.",
     lambda mission, status, view: records.sync_esp32_samples(mission)),
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
  2. Prepare Sample          (creates the persistent record)
  3. Rover arm deposits soil
  4. Confirm Sample Loaded
  5. Measure Sample          (180 deg out, RAW, 180 deg back, saved)

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

  The module uses one fixed Dark and one fixed White reference stored
  in firmware/BD/data/calibration_legacy.json. They were accepted before the
  competition and are used for every sample. You are never asked to
  measure White or Dark.

      C = Sample - Dark
      R = (Sample - Dark) / (White - Dark)

WHERE THINGS LIVE

  ESP32   carousel + AS7265x, RAW spectra only
  BD      White/Dark, material database, all analysis
  PC      workflow, Sample records, this interface

  Saved samples: firmware/PC/data/

MEASURED IS NOT EMPTY

  The soil physically stays in the slot after a measurement. Use
  Tools -> Clear Physical Slot when it has been removed. That keeps the
  scientific record; Delete Sample is what removes it.

SYNC ESP32 SAMPLES TO PC

  The ESP32 holds the last raw acquisition per slot in RAM. If this
  program lost a result - a crash, a restart, a different laptop -
  Tools -> Sync ESP32 Samples to PC copies it into the archive. It never
  overwrites an existing Sample and never deletes the ESP32 copy."""


def menu_help(mission, status, view):
    banner("HELP")

    print(HELP_TEXT)
    print()
    pause()


# ======================================================================
# main screen
# ======================================================================


def action_labels(entry, carousel):
    """What the operator can actually do right now."""
    state = entry.get("state", STATE_EMPTY)
    phase = carousel.get("carousel_phase")

    labels = {"2": "", "3": "", "4": ""}

    if state == STATE_EMPTY:
        labels["2"] = "[AVAILABLE]"
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
            "[AVAILABLE]" if phase == "LOAD"
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

    print("Selected: Slot {} / {}".format(
        selected, entry.get("sample_id") or "----"
    ))
    print("State:    {}".format(entry.get("state", "-")))
    print("Position: {}".format(carousel.get("carousel_phase", "?")))

    # Which actuator is driving the carousel is always on screen, together
    # with whether it can verify its own movements. Those two facts change
    # what a measurement is worth.
    servo = status.get("servo") or {}
    capabilities = servo.get("capabilities") or {}

    print("Servo:    {}{}".format(
        servo.get("label", "NOT SELECTED"),
        " (encoder verified)" if capabilities.get("verified_movement")
        else " (timed, open loop)" if servo.get("selected") else "",
    ))

    print()
    print("Loader: Slot {}    Scanner: Slot {}".format(
        carousel.get("current_load_slot"),
        carousel.get("current_scan_slot"),
    ))
    print()

    for item in view:
        print("{}  {:<8} {}".format(
            item["slot_id"], item["sample_id"] or "----", item["state"]
        ))

    sensor = status.get("sensor") or {}

    if not sensor.get("ready"):
        print()
        print("Sensor: UNAVAILABLE - it will be retried automatically on "
              "the next measurement or sensor test.")

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


def print_startup_screen(servo_label=None):
    banner("FREYA SCIENCE MODULE")

    print("Carousel servo: {}".format(servo_label or "NOT SELECTED"))
    print("Carousel:       NOT CALIBRATED")
    print()
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

    mission = Mission(link)

    if mission.science_error:
        print("BD warning: {}".format(mission.science_error))

    while True:
        try:
            status = mission.hardware_status()

        except (LinkError, TimeoutError) as error:
            print()
            print("Could not read the hardware state: {}".format(error))

            if choose("Retry? [Y/n]").startswith("n"):
                return 1

            continue

        view = mission.slot_view(status)
        carousel = status.get("carousel") or {}

        if not carousel.get("position_valid"):
            print_startup_screen((status.get("servo") or {}).get("label"))

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
