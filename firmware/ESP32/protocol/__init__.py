# protocol/
#
# The ESP32 <-> PC command protocol: newline-delimited JSON carried over
# the USB serial console.
#
#     transport.py          reading one command, writing one safe frame
#     router.py             dispatch, error envelopes, the serving loop
#     carousel_commands.py  servo selection and carousel movement
#     sensor_commands.py    acquisition and illumination
#     sample_commands.py    retained acquisitions held in RAM
#
# This layer owns the wire format and the command surface. It does not
# own hardware behaviour: every handler is a thin translation between a
# JSON request and a call into control/.
#
# Nothing in here touches UART2 - that belongs to the ST3215 driver. The
# two serial channels are entirely separate peripherals.
