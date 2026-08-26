"""
The bench profile and the device selector.

    profile.py   what is plugged in, what is configured, what is only
                 assumed - and the refusal to confuse the three
    ports.py     turning a selector into exactly one device, or into an
                 error that names the candidates

Neither opens a port. `ports.resolve` is given a list of devices; the
enumeration that produces that list lives behind the hardware gate in
`adapters/link.py`.
"""

from .profile import (Profile, ProfileError, Provenance, EXAMPLE_PROFILE,
                      production_values)
from .ports import PortError, resolve

__all__ = [
    "Profile", "ProfileError", "Provenance", "EXAMPLE_PROFILE",
    "production_values", "PortError", "resolve",
]
