# drivers/
#
# Direct external-hardware communication, and nothing else.
#
# A module in here talks to one physical device over one bus:
#
#     as7265x.py            I2C, virtual registers, channel reads
#     st3215.py             UART packets, registers, encoder feedback
#     st3215_registers.py   the ST3215 wire protocol as pure data
#     servo_base.py         rotation vocabulary and the servo error base
#
# Drivers must not know that a Sample ID, a material database, a PC menu,
# a LOAD/SCAN workflow or any scientific analysis exists. They are
# imported BY control/, and they import nothing from it - that one-way
# edge is what keeps the layering honest and is enforced by
# Tests/test_architecture.py.
