"""
Projecting external laboratory spectra into the AS7265x bands.

An external library measures reflectance as a near-continuous function of
wavelength. The AS7265x measures 18 numbers. Turning the first into the
second is a modelling step, and this module exists to make that step
explicit, reproducible and honest about its assumptions.

THE MODEL
---------
Each band is the source reflectance weighted by that band's spectral
response and normalised by the response itself:

    band_i = integral( R(lam) * S_i(lam) dlam )
             / integral( S_i(lam) dlam )

Nearest-wavelength sampling — just reading R(410), R(435), ... — is NOT
used as the default. It ignores the bandwidth entirely, so a narrow
absorption feature that the real detector would average away instead
lands at full depth or is missed completely, depending on luck.

WHAT S_i(lam) IS, AND WHAT IT IS NOT
------------------------------------
The correct S_i is the manufacturer's measured spectral response curve
for each of the 18 channels. Those curves are not present in this
repository, and no attempt is made to pretend otherwise.

Until they are obtained, the model uses a Gaussian centred on the nominal
channel wavelength with the documented nominal FWHM. Every record
produced this way is stamped:

    projection_method = "GAUSSIAN_APPROXIMATION"
    approximate       = true

so that no downstream consumer can mistake a modelled vector for a
measured one, and so that the whole database can be regenerated the day
real response curves arrive — by changing the response function and
re-running, not by editing numbers.

NO EXTRAPOLATION
----------------
A band whose response extends past the ends of the source spectrum is
NOT estimated. Inventing reflectance outside the measured range is
exactly the kind of fabrication this project forbids. Such bands are
reported as uncovered and the record is marked PARTIAL or rejected.
"""

import math
import sys
from pathlib import Path

FIRMWARE_ROOT = Path(__file__).resolve().parent.parent

if str(FIRMWARE_ROOT) not in sys.path:
    sys.path.insert(0, str(FIRMWARE_ROOT))

from BD.channels import CHANNELS, WAVELENGTHS  # noqa: E402

# Nominal full width at half maximum of an AS7265x channel, in nm.
#
# SOURCE STATUS: ams OSRAM documents the AS7265x channels as approximately
# 20 nm FWHM. This is the NOMINAL figure from the device documentation,
# not a per-device measurement and not a full response curve.
#
# research/SPECTRAL_SOURCES.md records what was searched for and what is
# still missing. If real curves are obtained, replace `channel_response`
# below - nothing else needs to change.
NOMINAL_FWHM_NM = 20.0

# Gaussian sigma from FWHM.
FWHM_TO_SIGMA = 1.0 / (2.0 * math.sqrt(2.0 * math.log(2.0)))

# Integrate each band over +/- this many sigma. Beyond ~3 sigma the
# Gaussian contributes under 0.3% and demanding source coverage there
# would reject otherwise usable spectra.
INTEGRATION_HALF_WIDTH_SIGMA = 3.0

# Integration step in nm. Fine enough that halving it does not move a
# projected band by more than 1e-6 - verified by test.
INTEGRATION_STEP_NM = 0.5

PROJECTION_MODEL_VERSION = "gaussian-nominal-fwhm-v1"

# Fraction of a band's response weight that must fall inside the source
# spectrum's measured range for that band to be reported at all.
MIN_BAND_COVERAGE = 0.98


class ProjectionError(Exception):
    """A spectrum cannot be projected into the AS7265x bands."""

    def __init__(self, code, message, details=None):
        super().__init__(message)

        self.code = code
        self.message = message
        self.details = details or {}


def channel_response(channel, wavelength):
    """
    Spectral response of one AS7265x channel at one wavelength.

    APPROXIMATION. Gaussian on the nominal centre with nominal FWHM.
    Replace this function with the manufacturer's measured curves when
    they are available; it is the single point the model depends on.
    """
    centre = WAVELENGTHS[channel]
    sigma = NOMINAL_FWHM_NM * FWHM_TO_SIGMA

    return math.exp(-0.5 * ((wavelength - centre) / sigma) ** 2)


def band_window(channel):
    """The wavelength interval a band's response meaningfully covers."""
    centre = WAVELENGTHS[channel]
    sigma = NOMINAL_FWHM_NM * FWHM_TO_SIGMA
    half = INTEGRATION_HALF_WIDTH_SIGMA * sigma

    return centre - half, centre + half


def interpolate(wavelengths, reflectances, target):
    """
    Linear interpolation inside the measured range.

    Returns None outside it. There is deliberately no extrapolation: a
    reflectance the source never measured is not available at any price.
    """
    if target < wavelengths[0] or target > wavelengths[-1]:
        return None

    low, high = 0, len(wavelengths) - 1

    while high - low > 1:
        middle = (low + high) // 2

        if wavelengths[middle] <= target:
            low = middle
        else:
            high = middle

    span = wavelengths[high] - wavelengths[low]

    if span <= 0:
        return reflectances[low]

    weight = (target - wavelengths[low]) / span

    return reflectances[low] * (1 - weight) + reflectances[high] * weight


def validate_source(wavelengths, reflectances):
    """Structural checks before anything is modelled."""
    if len(wavelengths) != len(reflectances):
        raise ProjectionError(
            "SOURCE_LENGTH_MISMATCH",
            "{} wavelengths but {} reflectance values".format(
                len(wavelengths), len(reflectances)
            ),
        )

    if len(wavelengths) < 2:
        raise ProjectionError(
            "SOURCE_TOO_SHORT",
            "a spectrum needs at least two points",
        )

    for index in range(1, len(wavelengths)):
        if wavelengths[index] <= wavelengths[index - 1]:
            raise ProjectionError(
                "SOURCE_NOT_MONOTONIC",
                "wavelengths must strictly increase; {} follows {}".format(
                    wavelengths[index], wavelengths[index - 1]
                ),
            )

    for value in reflectances:
        if value != value or value in (float("inf"), float("-inf")):
            raise ProjectionError(
                "SOURCE_NON_FINITE",
                "the spectrum contains a non-finite reflectance",
            )


def project(wavelengths, reflectances):
    """
    Project one source spectrum into the 18 AS7265x bands.

    Returns (bands, report). `bands` maps channel -> projected
    reflectance, omitting any band the source does not cover well enough.
    `report` records coverage per band so the omission is never silent.
    """
    validate_source(wavelengths, reflectances)

    source_min, source_max = wavelengths[0], wavelengths[-1]

    bands = {}
    coverage = {}

    for channel in CHANNELS:
        start, end = band_window(channel)

        weighted_sum = 0.0
        weight_total = 0.0
        weight_covered = 0.0

        steps = int((end - start) / INTEGRATION_STEP_NM) + 1

        for step in range(steps):
            wavelength = start + step * INTEGRATION_STEP_NM
            response = channel_response(channel, wavelength)
            weight_total += response

            value = interpolate(wavelengths, reflectances, wavelength)

            if value is None:
                continue

            weight_covered += response
            weighted_sum += response * value

        fraction = weight_covered / weight_total if weight_total else 0.0
        coverage[channel] = round(fraction, 4)

        if fraction >= MIN_BAND_COVERAGE and weight_covered > 0:
            bands[channel] = weighted_sum / weight_covered

    covered = len(bands)

    if covered == len(CHANNELS):
        status = "FULL_18CH"
    elif covered == 0:
        status = "REJECTED_INSUFFICIENT_COVERAGE"
    else:
        status = "PARTIAL"

    return bands, {
        "coverage_status": status,
        "bands_covered": covered,
        "bands_expected": len(CHANNELS),
        "uncovered_bands": [
            channel for channel in CHANNELS if channel not in bands
        ],
        "band_coverage_fraction": coverage,
        "source_range_nm": [source_min, source_max],
        "source_points": len(wavelengths),
        "projection_method": "GAUSSIAN_APPROXIMATION",
        "projection_model_version": PROJECTION_MODEL_VERSION,
        "response_source": "NOMINAL_CENTRE_AND_FWHM",
        "nominal_fwhm_nm": NOMINAL_FWHM_NM,
        "integration_step_nm": INTEGRATION_STEP_NM,
        "approximate": True,
        "approximation_note": (
            "Band response modelled as a Gaussian on the nominal centre "
            "wavelength with nominal FWHM. This is NOT the manufacturer's "
            "measured spectral response. Replace channel_response() and "
            "regenerate when real curves are available."
        ),
        "extrapolation": "NONE - bands outside the source range are "
                         "omitted, never estimated",
    }


def build_record(material, wavelengths, reflectances, provenance):
    """
    One DB3 material record: projected bands plus mandatory provenance.

    A reference record without a traceable source is indistinguishable
    from an invention, so provenance is required, not optional.
    """
    required = ("source_dataset", "source_record_id", "license")
    missing = [field for field in required if not provenance.get(field)]

    if missing:
        raise ProjectionError(
            "PROVENANCE_INCOMPLETE",
            "a DB3 record needs {}; missing {}".format(
                ", ".join(required), ", ".join(missing)
            ),
            {"missing": missing, "material": material},
        )

    bands, report = project(wavelengths, reflectances)

    if report["coverage_status"] == "REJECTED_INSUFFICIENT_COVERAGE":
        raise ProjectionError(
            "SOURCE_COVERAGE_INSUFFICIENT",
            "{} covers {}-{} nm, which does not reach the AS7265x "
            "bands".format(
                material, report["source_range_nm"][0],
                report["source_range_nm"][1]
            ),
            report,
        )

    return {
        "measurement_type": "REFERENCE_PROJECTED",
        "display_name": material,
        "channels": {
            channel: {
                "wavelength_nm": WAVELENGTHS[channel],
                "reflectance_as_supplied": round(value, 6),
            }
            for channel, value in bands.items()
        },
        "projection": report,
        "provenance": dict(provenance),
        "quality_flags": (
            ["PARTIAL_COVERAGE"]
            if report["coverage_status"] == "PARTIAL" else []
        ),
    }
