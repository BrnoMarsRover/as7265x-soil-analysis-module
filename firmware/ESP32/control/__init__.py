# control/
#
# Hardware subsystem logic: what the device does, expressed in terms the
# instrument cares about rather than in registers and pulse widths.
#
#     servo_manager.py   which actuator is fitted, and its lifecycle
#     carousel.py        slots, geometry, movement planning, position
#
# control/ imports drivers/. The reverse is forbidden: a driver that
# reached back into carousel geometry would no longer be reusable, and
# swapping actuators would stop being a local change.
