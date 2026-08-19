# boot.py
#
# Runs before main.py on every reset, and deliberately does nothing.
#
# MicroPython executes this file first, which makes it the most
# tempting place in the firmware to put a sensor scan, a servo ping or
# a "ready" message. All three would be wrong:
#
#   - stdout IS the protocol stream, so any text printed here lands in
#     front of the first JSON frame the PC tries to parse
#   - a peripheral touched here is touched before anything can report
#     that it failed
#   - a retry loop here delays the protocol by however long it runs
#
# Hardware is brought up on demand, by main.py's runtime state. This
# file stays empty on purpose.
