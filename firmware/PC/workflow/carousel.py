"""
Carousel and servo screens: setup, alignment, diagnostics, movement tests.

Option [0] lives here. Nothing turns until the operator has connected
the ST3215 and told the firmware which physical slot is at the loading
hole, because the mechanism has no index mark and software cannot
discover that by itself.

Fine alignment is a small physical correction and nothing more. It does
not renumber slots, does not redefine the origin and is never a hidden
whole-slot move - the logical geometry stays 4 slots at 90 degrees with
the scanner 180 degrees from the loader, whatever the actuator had to
be nudged by.
"""

from serial_link import DeviceError, LinkError

from workflow.display import (
    print_bus_scan,
    print_check,
    print_servo_block,
    report_failure,
    report_link_error,
    report_return_move,
)
from workflow.prompts import (
    RULE,
    ask,
    ask_float,
    ask_int,
    banner,
    choose,
    confirm,
    number,
    pause,
)

SLOT_COUNT = 4

import json
import sys
import textwrap

from workflow.display import print_servo_block, print_bus_scan


def menu_connect_servo(mission, allow_cancel=True):
    """
    Connect the ST3215 and confirm it answers.

    THE FIRST THING option [0] does, and a deliberate explicit step
    rather than something that happens on boot: until the UART is open
    and the servo has replied, the firmware refuses to move the carousel
    at all, because a carousel that turns without feedback is a carousel
    with no idea where it is.

    There is nothing to choose. This screen used to ask which of two
    actuators was installed; there is one, so it asks whether to connect
    it. Connecting always invalidates the carousel position on the
    ESP32 - the servo may have been unplugged, moved by hand or
    replaced while it was disconnected, and none of that is visible from
    here.
    """
    banner("CAROUSEL SERVO")

    print("Servo:   Waveshare ST3215 (serial bus, encoder feedback)")
    print("Link:    UART2, TX GPIO17 / RX GPIO16, 1 Mbps")
    print("Power:   external supply at the driver board")
    print()
    print("Connecting opens the UART and pings the servo. Nothing moves.")
    print()

    if allow_cancel:
        print("[1] Connect")
        print("[c] Cancel")
        print()

        selection = choose()

        if selection != "1":
            if selection and selection != "c":
                print("Unknown option.")

            else:
                print("Cancelled; carousel movement stays blocked.")

            return None

    print("Connecting...")
    sys.stdout.flush()

    try:
        data = mission.link.connect_servo()

    except (DeviceError, LinkError) as error:
        report_failure(error)
        print()
        print("Nothing is connected, so carousel movement stays blocked.")
        print()

        # SERVO_NOT_FOUND names four assumptions at once - the servo ID,
        # the baud rate, which wire is TX, and whether the servo has
        # power - and tests none of them. The scan tests all four, so it
        # is offered right here rather than left for the operator to
        # find in a submenu.
        if isinstance(error, DeviceError) and error.code in (
            "SERVO_NOT_FOUND", "SERVO_UART_TIMEOUT",
            "SERVO_CHECKSUM_ERROR", "SERVO_PROTOCOL_ERROR",
        ):
            print("The bus scan can tell you WHICH of those assumptions")
            print("is wrong: it pings every baud rate the ST3215")
            print("supports, in both pin orders, and moves nothing.")
            print()

            if confirm("Run the servo bus scan now?"):
                menu_servo_bus_scan(mission)

                return None

        pause()

        return None

    connection = data.get("connection") or {}

    print()
    print("{} connected.".format(
        connection.get("label", "ST3215")))
    print()
    print("The carousel position was invalidated by the connection -")
    print("align Slot 1 with the loading hole and confirm it.")
    print()
    pause()

    return "st3215"


def servo_connected(mission, status=None):
    """Whether the firmware currently holds a live link to the ST3215."""
    try:
        status = status or mission.hardware_status()

    except (DeviceError, LinkError):
        return False

    return bool((status.get("servo") or {}).get("connected"))


def menu_initial_calibration(mission):
    """
    Establish the carousel origin.

    Two steps, in this order:

        1. state which servo is installed
        2. align physical Slot 1 with the loading hole and confirm it

    The second step is the operator asserting the one fact no actuator can
    measure: which physical slot is called Slot 1. There is no limit
    switch, no Hall sensor and no index mark, so this is required after
    every power-up.

    The confirmation also captures the ST3215's encoder reading, so the
    logical model is tied to a real measurement rather than to a count
    that drifts.

    Neither path writes anything persistent to the servo. Changing the
    ST3215's stored configuration is a separate SERVICE operation under
    Tools.
    """
    try:
        status = mission.hardware_status()

    except (LinkError, TimeoutError) as error:
        report_failure(error)

        return False

    if not servo_connected(mission, status):
        if menu_connect_servo(mission) is None:
            return False

    while True:
        try:
            status = mission.hardware_status()

        except (LinkError, TimeoutError) as error:
            report_failure(error)

            return False

        servo = status.get("servo") or {}
        backend = servo.get("backend") or {}

        banner("CAROUSEL SETUP")

        print("Servo: {}".format(servo.get("label")))

        print("Communication: {}".format(
            "ONLINE" if backend.get("connected") else "NOT ANSWERING"
        ))
        print("Servo ID:      {}".format(backend.get("id")))
        print("Encoder:       {} counts ({} deg)".format(
            backend.get("position_counts"), backend.get("position_deg")
        ))
        print("Mode:          {}".format(backend.get("mode_name")))
        print("Voltage:       {} V".format(backend.get("voltage_v")))
        print("Temperature:   {} C".format(backend.get("temperature_c")))

        print()
        print("Goal:")
        print("Align physical Slot 1 exactly under the soil loading hole.")
        print()
        print("[1] Move one whole slot clockwise")
        print("[2] Move one whole slot counter-clockwise")
        print("[3] Fine alignment by degrees (+ = clockwise)")
        print("[4] STOP servo")

        print("[5] RELEASE torque, to turn the carousel by hand")
        print("[6] HOLD torque again")

        print()
        print("[7] SET CURRENT POSITION AS SLOT 1 / LOAD")
        print()
        print("[s] Change servo")

        print("[d] Diagnostics")

        print()
        print("[c] Cancel")

        selection = choose()

        try:
            if selection == "1":
                report_slot_move(mission.link.move_slots("cw", 1))

            elif selection == "2":
                report_slot_move(mission.link.move_slots("ccw", 1))

            elif selection == "3":
                menu_fine_adjust(mission)

            elif selection == "4":
                mission.link.servo_stop()
                print("Servo stopped.")

            elif selection == "5":
                if confirm("Release torque? The carousel will turn freely"):
                    mission.link.servo_torque(False)
                    print("Torque released. Turn the carousel by hand, then")
                    print("hold torque again before setting the origin.")

            elif selection == "6":
                mission.link.servo_torque(True)
                print("Torque enabled; the carousel is held again.")

            elif selection == "7":
                data = mission.link.sync_position(load_slot=1)
                carousel = data.get("carousel") or {}
                reference = carousel.get("reference") or {}
                origin = reference.get("origin") or {}

                print()
                print("Carousel setup complete.")
                print("Slot {} = LOADING position".format(
                    carousel.get("current_load_slot")
                ))
                print("Slot {} = SCANNER position".format(
                    carousel.get("current_scan_slot")
                ))

                if origin.get("feedback"):
                    print("Encoder origin: {} counts".format(
                        origin.get("origin_counts")
                    ))

                else:
                    print("Origin: your assertion - this servo has no "
                          "position sensor.")

                print()

                return True

            elif selection == "s":
                menu_connect_servo(mission)

            elif selection == "d":
                menu_servo_diagnostics(mission)

            elif selection == "c":
                print("Cancelled; carousel position unchanged.")

                return False

            elif selection:
                print("Unknown option.")

        except (LinkError, TimeoutError) as error:
            report_failure(error)


def report_slot_move(data):
    """What a whole-slot movement did, in whatever terms the backend has."""
    move = (data or {}).get("move") or {}
    servo = move.get("servo") or {}

    if not move.get("moved"):
        print("Nothing moved.")

        return

    print("Moved {} slot(s) {} ({:.0f} deg).".format(
        move.get("steps"), move.get("direction"), move.get("degrees") or 0
    ))

    if servo.get("verified"):
        print("  encoder:   {} -> {} counts".format(
            servo.get("start_position"), servo.get("actual_position")
        ))
        print("  error:     {} counts ({} deg), tolerance {}".format(
            servo.get("position_error"),
            servo.get("position_error_deg"),
            servo.get("tolerance_counts"),
        ))
        print("  elapsed:   {} ms".format(servo.get("elapsed_ms")))

    else:
        print("  commanded: {} ms per slot (timed, not verified)".format(
            servo.get("step_ms")
        ))


def menu_fine_adjust(mission):
    """Small mechanical correction. Does not change slot numbering."""
    banner("FINE CAROUSEL ALIGNMENT")
    print("Positive = clockwise, negative = counter-clockwise.")
    print()
    print("The correction is REMEMBERED: the next slot movement keeps it")
    print("instead of returning the carousel to the old theoretical")
    print("centre.")
    print()

    degrees = ask_float("Degrees", -15.0, 15.0)

    if degrees is None:
        print("Cancelled; carousel not moved.")

        return

    try:
        data = mission.link.fine_adjust(degrees)

    except (LinkError, TimeoutError) as error:
        report_failure(error)

        return

    adjustment = data.get("adjustment") or {}
    reference = (data.get("carousel") or {}).get("reference") or {}

    print()
    print("Requested: {:+.2f} deg".format(degrees))

    if adjustment.get("moved") is False:
        print(adjustment.get("message") or "Nothing moved.")

        return

    # Encoder terms. Every movement is commanded in counts and
    # verified by reading the encoder back, so there is nothing
    # else to report.
    print("Commanded: {} counts ({} deg)".format(
        adjustment.get("requested_counts"),
        adjustment.get("commanded_degrees"),
    ))
    print("Encoder:   {} -> {} counts".format(
        adjustment.get("start_position"),
        adjustment.get("actual_position"),
    ))
    print("Error:     {} counts ({} deg), tolerance {} counts".format(
        adjustment.get("position_error"),
        adjustment.get("position_error_deg"),
        adjustment.get("tolerance_counts"),
    ))

    print("Alignment offset now {} deg.".format(
        reference.get("alignment_offset_deg")
    ))

    if reference.get("drift_deg") is not None:
        print("Drift vs nominal: {} deg".format(reference.get("drift_deg")))


def menu_resync(mission):
    """Re-declare the origin after a reboot or a lost position."""
    banner("RE-SYNC CAROUSEL")

    if not servo_connected(mission):
        print("No servo is selected, so there is nothing to synchronize.")
        print("Run [0] Carousel Setup first.")
        print()
        pause()

        return

    print("Align physical Slot 1 with the soil loading hole, then confirm.")
    print()
    print("Nothing moves. On a servo with an encoder this also captures the")
    print("encoder reading as the carousel origin.")
    print()

    if not confirm("Is Slot 1 now under the loading hole?"):
        print("Cancelled; position tracking unchanged.")

        return

    try:
        data = mission.link.sync_position(load_slot=1)

    except (LinkError, TimeoutError) as error:
        report_failure(error)

        return

    carousel = data.get("carousel") or {}
    origin = ((carousel.get("reference") or {}).get("origin")) or {}

    print()
    print("Synchronized. Loader = Slot {}, scanner = Slot {}.".format(
        carousel.get("current_load_slot"),
        carousel.get("current_scan_slot"),
    ))

    if origin.get("feedback"):
        print("Encoder origin: {} counts.".format(origin.get("origin_counts")))


# ----------------------------------------------------------------------
# servo tools
# ----------------------------------------------------------------------


def menu_servo_test(mission):
    """Servo and carousel tools, adapted to whichever backend is active."""
    while True:
        try:
            status = mission.hardware_status()

        except (LinkError, TimeoutError) as error:
            report_failure(error)

            return

        servo = status.get("servo") or {}

        banner("SERVO / CAROUSEL TOOLS")

        print("Servo: {}".format(servo.get("label")))
        print()

        if not servo.get("selected"):
            print("No servo is selected. Carousel movement is blocked.")
            print()
            print("[s] Select the installed servo")
            print("[b] ST3215 bus scan - find out why one does not answer")
            print("[0] Back")

            selection = choose()

            if selection == "s":
                menu_connect_servo(mission)

            elif selection == "b":
                try:
                    menu_servo_bus_scan(mission)

                except (LinkError, TimeoutError) as error:
                    report_failure(error)

            elif selection == "0":
                return

            continue

        print("[1] One slot clockwise")
        print("[2] One slot counter-clockwise")
        print("[3] Fine adjustment by degrees")
        print("[4] STOP servo")
        print()
        print("[5] Diagnostics (moves nothing)")
        print("[6] Movement tests (turns the carousel)")
        print("[7] Calibration / settings")
        print()
        print("[s] Change servo")
        print("[b] Bus scan (moves nothing)")

        print("[e] SERVICE: write servo configuration to its EPROM")

        print("[t] Torque hold / release")

        print()
        print("[0] Back")

        selection = choose()

        try:
            if selection == "1":
                report_slot_move(mission.link.move_slots("cw", 1))

            elif selection == "2":
                report_slot_move(mission.link.move_slots("ccw", 1))

            elif selection == "3":
                menu_fine_adjust(mission)

            elif selection == "4":
                mission.link.servo_stop()
                print("Servo stopped.")
                print("The tracked position was dropped - re-sync before")
                print("the next measurement.")

            elif selection == "5":
                menu_servo_diagnostics(mission)

            elif selection == "6":
                menu_servo_movement_test(mission)

            elif selection == "7":
                menu_servo_calibration(mission)

            elif selection == "s":
                menu_connect_servo(mission)

            elif selection == "b":
                menu_servo_bus_scan(mission)

            elif selection == "e":
                menu_servo_configure(mission)

            elif selection == "t":
                menu_servo_torque(mission)

            elif selection == "0":
                return

            elif selection:
                print("Unknown option.")

        except (LinkError, TimeoutError) as error:
            report_failure(error)


def menu_servo_bus_scan(mission):
    """
    Find out WHY the servo does not answer, without guessing.

    Deliberately reachable with nothing selected: this is the tool for
    the moment select_servo has just failed, and requiring a working
    selection first would be circular.
    """
    # Whether there is anything to lose. Asked once, before the loop:
    # after the first scan the answer is always "no", and the warning
    # would be about a connection the scan itself has already taken.
    connected_at_entry = servo_connected(mission)

    while True:
        banner("SERVO BUS SCAN")

        print("Pings the servo bus and reports what answers. MOVES")
        print("NOTHING - a ping asks a servo to identify itself.")
        print()

        # "Moves nothing" is true and is not the whole story. The scan
        # reopens UART2 at every baud rate the ST3215 supports, so a
        # connected servo has to be released first and the carousel
        # origin is invalidated with it. Saying only "moves nothing"
        # sent operators into a scan that silently cost them the
        # alignment they had just done by hand.
        if connected_at_entry:
            print("IT DOES COST YOU THE CONNECTION. The scan reopens")
            print("UART2, so the servo is released and the carousel")
            print("position is invalidated - you will have to connect")
            print("and re-declare Slot 1 afterwards.")
            print()

            if not confirm("Scan anyway, losing the carousel origin?"):
                print("Cancelled; the servo and the origin are untouched.")

                return

            connected_at_entry = False

        print("[1] Quick scan")
        print("    The configured ID, at all 8 baud rates, in both pin")
        print("    orders. About 1 second.")
        print()
        print("[2] Full ID sweep")
        print("    Every ID from 0 to 253 at one baud rate, in both pin")
        print("    orders. Use when the quick scan hears an echo but no")
        print("    servo. About 20 seconds.")
        print()
        print("[0] Back")

        selection = choose()

        if selection == "0":
            return

        if selection == "1":
            ids, bauds = None, None

        elif selection == "2":
            print()
            print("Baud rate to sweep [1] 1000000  [2] 500000  [3] 115200")

            answer = choose("Baud")
            bauds = {
                "1": [1000000], "2": [500000], "3": [115200],
            }.get(answer, [1000000])

            ids = "all"

        else:
            if selection:
                print("Unknown option.")

            continue

        print()
        print("Scanning...")
        sys.stdout.flush()

        try:
            report = mission.link.servo_bus_scan(ids=ids, bauds=bauds)

        except (LinkError, TimeoutError) as error:
            report_failure(error)
            print()
            pause()

            continue

        print()
        print_bus_scan(report)
        print()
        pause()


def menu_servo_diagnostics(mission):
    """Read-only backend check, stage by stage."""
    banner("SERVO DIAGNOSTICS")

    print("Checking the active servo. Nothing will move.")
    print()
    sys.stdout.flush()

    try:
        report = mission.link.servo_diagnostics()

    except (LinkError, TimeoutError) as error:
        report_failure(error)
        print()
        pause()

        return

    for entry in report.get("steps") or []:
        print_check(entry.get("step"), entry.get("ok"))

        value = entry.get("value")

        if entry.get("ok") and isinstance(value, str):
            print("      {}".format(value))

        if not entry.get("ok"):
            error = entry.get("error") or {}

            if error:
                print("      {}: {}".format(
                    error.get("code"), error.get("message")
                ))

            elif isinstance(value, str):
                print("      {}".format(value))

    print()

    if report.get("ok"):
        print("{} OK.".format(report.get("label")))

    else:
        error = report.get("error") or {}

        print("{} FAILED: {}".format(
            report.get("label"), error.get("code")
        ))
        print("  {}".format(error.get("message")))

        if report.get("uart_id") is not None:
            print()
            print("Check, in this order:")
            print("  1. external servo power supply at the driver board")
            print("  2. ESP32 GND to driver board GND (common reference)")
            print("  3. GPIO17 -> driver TX, GPIO16 -> driver RX")
            print("  4. servo ID and baud rate")

    if report.get("baud_matches") is False:
        print()
        print("The servo reports {} baud but the firmware opens {}.".format(
            report.get("baud_reported"), report.get("baud")
        ))

    print()
    pause()


def menu_servo_configure(mission):
    """SERVICE: write the operating mode into the servo's own memory."""
    banner("SERVICE: SERVO CONFIGURATION")

    print("This writes the servo's EPROM: the operating mode and both")
    print("angle limits. It is needed ONCE per servo, and it survives a")
    print("power cycle.")
    print()
    print("It is NOT part of carousel setup. Ordinary calibration")
    print("establishes a runtime origin and touches no persistent servo")
    print("state; this command is the only thing that does.")
    print()
    print("Step servo mode is what the carousel needs: every movement is a")
    print("relative number of encoder counts, so the carousel can turn")
    print("indefinitely and never takes the long way round the 4095/0")
    print("encoder boundary.")
    print()
    print("EPROM has a finite write life - do not run this routinely.")
    print()

    if not confirm("Write the servo EPROM now?"):
        print("Cancelled; the servo was not changed.")

        return

    try:
        result = mission.link.servo_configure(confirm=True)

    except (LinkError, TimeoutError) as error:
        report_failure(error)

        return

    print()
    print("Mode is now {} ({}).".format(
        result.get("mode_name"), result.get("mode")
    ))
    print("Angle limits: {} .. {}".format(
        result.get("min_angle_limit"), result.get("max_angle_limit")
    ))
    print()
    pause()


def menu_servo_torque(mission):
    """Hold or release the servo, explicitly."""
    banner("SERVO TORQUE")

    print("Holding torque keeps the carousel exactly where the last")
    print("movement left it, which is what a measurement depends on.")
    print()
    print("[1] HOLD  (enable torque)")
    print("[2] RELEASE (turn the carousel by hand; drops the tracked")
    print("    position)")
    print("[0] Back")

    selection = choose()

    try:
        if selection == "1":
            mission.link.servo_torque(True)
            print("Torque enabled.")

        elif selection == "2":
            if confirm("Release torque? The carousel will turn freely"):
                mission.link.servo_torque(False)
                print("Torque released. Re-sync the carousel afterwards.")

    except (LinkError, TimeoutError) as error:
        report_failure(error)


# ST3215 settings, shown read-only. See menu_servo_calibration.


def print_st3215_settings(calibration):
    values = calibration["current"]
    units = calibration.get("units") or {}

    print("ST3215 settings (read only):")
    print()
    print("  Speed:              {} steps/s".format(
        values["speed_steps_per_s"]
    ))
    print("  Acceleration:       {}".format(values["acceleration"]))
    print("  Position tolerance: {} counts".format(
        values["position_tolerance_counts"]
    ))
    print("  Settle:             {} ms".format(values["settle_ms"]))
    print("  Poll interval:      {} ms".format(values["poll_interval_ms"]))
    print("  Move timeout:       {} ms".format(values["move_timeout_ms"]))
    print()

    for key in ("speed_steps_per_s", "acceleration",
                "position_tolerance_counts"):
        if units.get(key):
            print("  {:<26} {}".format(key + ":", units[key]))

    print()
    print(calibration.get("note") or "")


def menu_servo_calibration(mission):
    """
    Servo settings. Read only.

    There is nothing to calibrate. The ST3215 commands every movement in
    encoder counts and verifies it by reading the encoder back, so its
    limits are engineering values that belong in config.py under version
    control rather than numbers trimmed at runtime.

    This screen is read-only. An earlier revision made it editable
    because the actuator it also drove was
    open-loop and its timings had to be measured on the real mechanism.
    That backend is gone, and with it every pulse width, settle time and
    correction factor this screen existed to tune.
    """
    banner("SERVO SETTINGS")

    try:
        calibration = mission.link.get_servo_calibration()

    except (LinkError, TimeoutError) as error:
        report_failure(error)
        print()
        pause()

        return

    if calibration.get("editable"):
        # The firmware says its settings are runtime-editable. No servo
        # this client drives has that any more, so rather than silently
        # showing a read-only screen, say what happened.
        print("The firmware reports editable calibration, which no servo")
        print("this client supports should have. Check that the firmware")
        print("and this client are the same version.")
        print()

    print_st3215_settings(calibration)
    print()
    pause()


def report_move_test(result):
    """Per-leg results, in whatever terms the backend can report."""
    print()

    if result.get("verified_movement"):
        print("  Legs:          {} x {} = {}".format(
            result.get("repeat"), result.get("legs"), result.get("leg_count")
        ))
        print("  Net travel:    {} counts ({} deg)".format(
            result.get("net_counts"), result.get("net_degrees")
        ))
        print("  Encoder:       {} -> {}".format(
            result.get("start_position"), result.get("end_position")
        ))
        print("  Worst error:   {} counts (tolerance {})".format(
            result.get("worst_position_error"), result.get("tolerance_counts")
        ))

        if result.get("closed_loop_error_counts") is not None:
            print("  Closing error: {} counts ({} deg)".format(
                result.get("closed_loop_error_counts"),
                result.get("closed_loop_error_deg"),
            ))

        print()

        for index, movement in enumerate(result.get("movements") or []):
            print("  leg {}: {:+5} counts, {} -> {}, error {:+} counts, "
                  "{} ms".format(
                      index + 1,
                      movement.get("requested_counts"),
                      movement.get("start_position"),
                      movement.get("actual_position"),
                      movement.get("position_error"),
                      movement.get("elapsed_ms"),
                  ))

    print()
    print("Movement complete.")

    if result.get("position_invalidated"):
        print()
        print("Carousel position tracking was invalidated - re-sync before")
        print("normal operation.")


def menu_servo_movement_test(mission):
    """
    Operator-confirmed movement tests, offered per backend.

    Run the diagnostics first: there is no point turning a mechanism whose
    servo is not answering.
    """
    while True:
        try:
            status = mission.hardware_status()

        except (LinkError, TimeoutError) as error:
            report_failure(error)

            return

        servo = status.get("servo") or {}
        kinds = [
            (entry["kind"], entry["label"])
            for entry in (servo.get("test_move_kinds") or [])
        ]

        banner("SERVO MOVEMENT TEST")

        print("Servo: {}".format(servo.get("label")))
        print()
        print("THE CAROUSEL WILL TURN. Check the mechanism is clear.")
        print()

        print("Every movement is commanded in encoder counts and")
        print("verified from the encoder afterwards, so what you get")
        print("back is what the servo measured.")

        print()

        for index, (kind, label) in enumerate(kinds, start=1):
            print("[{}] {}".format(index, label))

        print()
        print("[0] Back")

        selection = choose()

        if selection == "0":
            return

        chosen = None

        for index, entry in enumerate(kinds, start=1):
            if selection == str(index):
                chosen = entry

        if chosen is None:
            if selection:
                print("Unknown option.")

            continue

        kind, label = chosen
        degrees = None
        hold_ms = None

        if kind == "degrees":
            degrees = ask_float("Degrees (+ = clockwise)", -180.0, 180.0)

            if degrees is None:
                continue

        if kind == "neutral":
            seconds = ask_int("Hold for how many seconds", 1, 30, default=5)

            if seconds is None:
                continue

            hold_ms = seconds * 1000

        repeat = ask_int("Repeat how many times", 1, 8, default=1)

        if repeat is None:
            continue

        print()
        print("{} x {}".format(label, repeat))

        if not confirm("Move the carousel now?"):
            print("Cancelled; nothing moved.")

            continue

        sys.stdout.flush()

        try:
            result = mission.link.servo_test_move(
                kind, repeat=repeat, degrees=degrees, hold_ms=hold_ms,
                confirm=True,
            )

        except (LinkError, TimeoutError) as error:
            report_failure(error)

            continue

        report_move_test(result)

        if kind in ("slot_out_and_back", "out_and_back"):
            print()
            print("A symmetrical test should close on itself. This is the")
            print("repeatability figure that matters for Measure Sample.")

        print()
        pause()


# ======================================================================
# screens: the sample workflow
# ======================================================================
