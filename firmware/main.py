# main.py
# Automatic measurement loop for ESP32 + AS7265x + HS-5645MG
#
# MicroPython automatically runs this file after every power-on or reset.
#
# Sequence:
#   1. Wait for power rails and hardware to stabilize
#   2. Servo center -> dark measurement
#   3. Servo left   -> white reference measurement
#   4. Servo right  -> soil/sample measurement
#   5. Normalize and analyze
#   6. Repeat
#
# PowerShell commands
# -------------------
# Open PowerShell in the project folder:
#
#   PS ~\as7265x-soil-analyzer
#
# Upload all required files to ESP32:
#
#   py -m mpremote connect COM4 fs cp boot.py :boot.py
#   py -m mpremote connect COM4 fs cp main.py :main.py
#   py -m mpremote connect COM4 fs cp as7265x.py :as7265x.py
#   py -m mpremote connect COM4 fs cp config.py :config.py
#   py -m mpremote connect COM4 fs cp database.py :database.py
#   py -m mpremote connect COM4 fs cp database.json :database.json
#   py -m mpremote connect COM4 fs cp hs5645mg.py :hs5645mg.py
#
# Check files on ESP32:
#
#   py -m mpremote connect COM4 fs ls :
#
# Restart ESP32 and start this program automatically:
#
#   py -m mpremote connect COM4 reset
#
# Open REPL / serial output:
#
#   py -m mpremote connect COM4 repl
#
# Press Ctrl+C in REPL to stop the automatic loop.
#
# Save database files from ESP32 to PC:
#
#   py -m mpremote connect COM4 fs cp :references.json references.json
#   py -m mpremote connect COM4 fs cp :database.json database.json
#
# Important:
#   This file must be uploaded as :main.py.
#   auto_main.py and startup.py are no longer required.

import time
import config

from as7265x import AS7265X_Driver, SoilMeasurementSystem
from database import MaterialDatabase
from hs5645mg import MeasurementMovement


CHANNELS = [
    "A", "B", "C", "D", "E", "F",
    "G", "H", "I", "J", "K", "L",
    "R", "S", "T", "U", "V", "W"
]

WAVELENGTHS = {
    "A": 410,
    "B": 435,
    "C": 460,
    "D": 485,
    "E": 510,
    "F": 535,
    "G": 560,
    "H": 585,
    "I": 610,
    "J": 645,
    "K": 680,
    "L": 705,
    "R": 730,
    "S": 760,
    "T": 810,
    "U": 860,
    "V": 900,
    "W": 940
}


# Wait after boot before hardware initialization.
STARTUP_DELAY_SECONDS = 3

# Pause between complete measurement cycles.
LOOP_DELAY_SECONDS = 2

# Number of database results to display.
TOP_MATCHES = 5


def copy_reference(data):
    """Create a clean dictionary containing every AS7265x channel."""
    result = {}

    if data is None:
        data = {}

    for channel in CHANNELS:
        result[channel] = float(
            data.get(channel, 0.0)
        )

    return result


def move_to_center(movement):
    """Move servo to the center/dark position."""
    target_angle = config.SERVO_CENTER_ANGLE

    movement.motor.set_angle(target_angle)

    time.sleep(
        config.MECHANICAL_SETTLE_TIME
    )


def move_to_left(movement):
    """Move servo left to the white-reference position."""
    target_angle = (
        config.SERVO_CENTER_ANGLE
        - config.SERVO_SWING_DEGREES
    )

    movement.motor.set_angle(target_angle)

    time.sleep(
        config.MECHANICAL_SETTLE_TIME
    )


def move_to_right(movement):
    """Move servo right to the soil/sample position."""
    target_angle = (
        config.SERVO_CENTER_ANGLE
        + config.SERVO_SWING_DEGREES
    )

    movement.motor.set_angle(target_angle)

    time.sleep(
        config.MECHANICAL_SETTLE_TIME
    )


def print_channel_data(data, title):
    print("\n--- {} ---".format(title))

    print(
        "{:<3} | {:<4} | {}".format(
            "ch",
            "nm",
            "value"
        )
    )

    print("-" * 32)

    for channel in CHANNELS:
        wavelength = WAVELENGTHS[channel]
        value = data.get(channel, 0.0)

        print(
            "{:<3} | {:<4} | {:.4f}".format(
                channel,
                wavelength,
                value
            )
        )


def print_reflectance_table(sensor):
    print("\n--- reflectance data ---")

    print(
        "{:<3} | {:<4} | {:<10} | {:<10} | "
        "{:<10} | {}".format(
            "ch",
            "nm",
            "dark",
            "white",
            "sample",
            "reflectance"
        )
    )

    print("-" * 76)

    for channel in CHANNELS:
        wavelength = WAVELENGTHS[channel]

        dark = sensor.dark_ref.get(
            channel,
            0.0
        )

        white = sensor.white_ref.get(
            channel,
            0.0
        )

        sample = sensor.sample_data.get(
            channel,
            0.0
        )

        reflectance = sensor.normalized_data.get(
            channel,
            0.0
        )

        print(
            "{:<3} | {:<4} | {:<10.4f} | "
            "{:<10.4f} | {:<10.4f} | {:.4f}".format(
                channel,
                wavelength,
                dark,
                white,
                sample,
                reflectance
            )
        )


def analyze(sensor, database):
    """
    Normalize the current dark, white and sample measurements
    and compare the result with database.json.
    """
    print("\n--- analysis ---")

    if not sensor.normalize():
        print("error: reflectance calculation failed")
        return False

    print_reflectance_table(sensor)

    print("\n--- database match results ---")

    if not database.data:
        print("database.json is empty or not loaded")
        return True

    matches = database.find_matches(
        sensor.normalized_data,
        top_n=TOP_MATCHES
    )

    for name, similarity in matches:
        print(
            "{}: {:.2f}% match".format(
                name,
                similarity
            )
        )

    return True


def run_measurement_cycle(
    sensor,
    database,
    movement,
    cycle_number
):
    """Run one complete automatic measurement cycle."""

    print("\n========================================")
    print(
        "automatic measurement cycle: {}".format(
            cycle_number
        )
    )
    print("========================================")

    # 1. Center position: dark reference
    print("\n[1/4] moving servo to center")

    move_to_center(movement)

    print("[1/4] measuring dark reference")

    dark_values = copy_reference(
        sensor.take_dark()
    )

    sensor.dark_ref = dark_values

    print_channel_data(
        dark_values,
        "dark reference"
    )

    # 2. Left position: white reference
    print(
        "\n[2/4] moving servo left "
        "to white reference"
    )

    move_to_left(movement)

    print("[2/4] measuring white reference")

    white_values = copy_reference(
        sensor.take_white()
    )

    sensor.white_ref = white_values

    print_channel_data(
        white_values,
        "white reference"
    )

    # 3. Right position: soil sample
    print(
        "\n[3/4] moving servo right "
        "to soil sample"
    )

    move_to_right(movement)

    print("[3/4] measuring soil sample")

    sample_values = copy_reference(
        sensor.take_sample()
    )

    sensor.sample_data = sample_values

    print_channel_data(
        sample_values,
        "soil sample"
    )

    # 4. Analysis
    print("\n[4/4] analyzing measurement")

    analyze(
        sensor,
        database
    )


def run_auto_loop():
    print(
        "\n--- AS7265x automatic soil analyzer ---"
    )

    print("center = dark")
    print("left   = white reference")
    print("right  = soil sample")
    print("Press Ctrl+C to stop.\n")

    movement = None

    try:
        sensor = SoilMeasurementSystem(
            AS7265X_Driver()
        )

        database = MaterialDatabase()

        movement = MeasurementMovement()

    except Exception as error:
        print("hardware initialization error:")
        print(error)
        return

    print("hardware initialized")

    print(
        "database items loaded: {}".format(
            len(database.data)
        )
    )

    cycle_number = 1

    try:
        while True:
            try:
                run_measurement_cycle(
                    sensor,
                    database,
                    movement,
                    cycle_number
                )

                cycle_number += 1

                print(
                    "\ncycle complete; "
                    "next cycle in {} seconds".format(
                        LOOP_DELAY_SECONDS
                    )
                )

                time.sleep(
                    LOOP_DELAY_SECONDS
                )

            except Exception as error:
                print("\nmeasurement cycle error:")
                print(error)

                print(
                    "retrying in {} seconds".format(
                        LOOP_DELAY_SECONDS
                    )
                )

                time.sleep(
                    LOOP_DELAY_SECONDS
                )

    except KeyboardInterrupt:
        print(
            "\nautomatic loop stopped by user"
        )

    finally:
        if movement is not None:
            try:
                print(
                    "returning servo to center"
                )

                move_to_center(movement)

            except Exception as error:
                print(
                    "could not return servo "
                    "to center:"
                )
                print(error)

            try:
                movement.shutdown()

                print("servo PWM released")

            except Exception as error:
                print(
                    "could not release servo PWM:"
                )
                print(error)


def main():
    print(
        "\nwaiting {} seconds for power rails..."
        .format(STARTUP_DELAY_SECONDS)
    )

    time.sleep(
        STARTUP_DELAY_SECONDS
    )

    run_auto_loop()


# MicroPython executes main.py automatically after boot/reset.
main()