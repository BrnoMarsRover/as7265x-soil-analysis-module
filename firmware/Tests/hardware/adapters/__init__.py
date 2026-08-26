"""
Adapters around the production interfaces.

One rule holds across all of them: the test side never speaks protocol.
A test asks the servo adapter to move; it does not build a
`servo_test_move` payload, and it does not know that `MOVE_TIMEOUT` is
60 seconds. That keeps the 60-odd test definitions readable and means a
protocol change costs one edit here rather than sixty there.

Constructing any adapter opens nothing. Detection of what the production
system can do is static - the PC method surface by introspection, the
firmware command table by parsing `protocol.py` with `ast` - so
`--list` can print a BLOCKED reason on a machine with no board attached.
"""

from .base import Adapter, AdapterError, Capability
from .carousel import CarouselAdapter
from .link import LinkAdapter
from .sensor import SensorAdapter
from .servo import ServoAdapter
from .workflow import WorkflowAdapter

__all__ = [
    "Adapter", "AdapterError", "Capability",
    "CarouselAdapter", "LinkAdapter", "SensorAdapter", "ServoAdapter",
    "WorkflowAdapter",
]
