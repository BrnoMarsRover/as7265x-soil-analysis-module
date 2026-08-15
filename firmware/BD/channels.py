"""
The 18 AS7265x channels.

Its own module, and one that imports nothing, because every other BD
module needs the channel list: putting it in sample_analysis.py made
aggregation, metrics and quality import the very module that imports
them.
"""

# In wavelength order. The letters are the AS7265x channel names; the
# gap between L and R is the sensor's own, not a mistake.
CHANNELS = (
    "A", "B", "C", "D", "E", "F",
    "G", "H", "I", "J", "K", "L",
    "R", "S", "T", "U", "V", "W",
)

WAVELENGTHS = {
    "A": 410, "B": 435, "C": 460, "D": 485, "E": 510, "F": 535,
    "G": 560, "H": 585, "I": 610, "J": 645, "K": 680, "L": 705,
    "R": 730, "S": 760, "T": 810, "U": 860, "V": 900, "W": 940,
}

# Which of the three internal detectors owns each channel. The seams
# between them are where a device mismatch shows up first.
CHANNEL_DEVICE = {
    "A": "uv", "B": "uv", "C": "uv", "D": "uv", "E": "uv", "F": "uv",
    "G": "visible", "H": "visible", "I": "visible",
    "J": "visible", "K": "visible", "L": "visible",
    "R": "nir", "S": "nir", "T": "nir",
    "U": "nir", "V": "nir", "W": "nir",
}


def channel_wavelengths():
    """Channel-to-nanometre map stored alongside every measurement."""
    return {channel: WAVELENGTHS[channel] for channel in CHANNELS}
