"""
DB1 — historical measured database tests.

DB1 is the only database in this system whose numbers came off this
instrument. Everything else is recalculated, modelled or projected. These
tests guard the properties that make it trustworthy:

    it contains the whole historical session, not a subset;
    every number can be traced to the verbatim source snapshot;
    the reflectance can be re-derived from the raw measurements;
    nothing was clipped, filled in or quietly corrected;
    the anomalies in the source are represented rather than smoothed.

The last one matters most. A database that looks clean because someone
tidied the awkward parts away is less useful than one that says exactly
where it is uncertain.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import support
from support import Checks

support.add_project_root()

from BD.channels import CHANNELS, WAVELENGTHS  # noqa: E402

DATA = support.REPO / "BD" / "data"
SOURCE = DATA / "DB1_source.txt"
MATERIALS = DATA / "DB1.json"
BUILDER = support.REPO / "research" / "build_db1.py"

# The 23 materials of the historical session, as supplied.
EXPECTED_MATERIALS = {
    "Activated Carbon", "Aluminum Sulfate", "Ascorbic Acid", "Bentonite",
    "Borax (Sodium Tetraborate)", "Calcium Carbonate (Chalk)",
    "Citric Acid", "Copper(II) Sulfate", "Epsom Salt (Magnesium Sulfate)",
    "Green Clay (Illite)", "Iron(II) Sulfate", "Iron(II,III) Oxide Black",
    "Iron(III) Oxide Red", "Kaolin (White Clay)", "Magnesium Carbonate",
    "Magnesium Oxide", "Pink Clay", "Potassium Nitrate", "Red Clay",
    "Sodium Bicarbonate", "Sulfur Powder", "Talc", "Tartaric Acid",
}


def main_tests():
    checks = Checks("DB1 historical database")

    document = json.loads(MATERIALS.read_text(encoding="utf-8"))
    manifest = document
    materials = document["materials"]

    # ==================================================================
    checks.section("1. the whole historical session is present")

    checks.equal(len(materials), 23, "exactly 23 materials")
    checks.equal(
        set(materials), EXPECTED_MATERIALS,
        "the material set matches the supplied session exactly",
    )

    # The specific regression this database exists to fix: the committed
    # legacy DB1.json had 22 materials and Copper(II) Sulfate was
    # reported as unauditable.
    checks.ok(
        "Copper(II) Sulfate" in materials,
        "Copper(II) Sulfate is present - it was absent from the committed "
        "legacy database.json",
    )
    checks.equal(
        materials["Copper(II) Sulfate"]["canonical_name"], "Chalcanthite",
        "and carries its canonical mineral identity",
    )

    identifiers = [entry["material_id"] for entry in materials.values()]
    checks.equal(
        len(set(identifiers)), len(identifiers), "material IDs are unique"
    )

    # "Bentonit" in the committed database is the same material, spelled
    # differently. Confirmed numerically at build time; recorded as alias.
    checks.ok(
        "Bentonit" in materials["Bentonite"]["aliases"],
        "the committed 'Bentonit' spelling resolves as an alias of "
        "Bentonite rather than looking like a 24th material",
    )

    # ==================================================================
    checks.section("2. channels and wavelengths")

    for name, entry in sorted(materials.items()):
        channels = list(entry["channels"])

        if channels != list(CHANNELS):
            checks.ok(False, "{}: 18 channels in wavelength order".format(name))
            break
    else:
        checks.ok(True, "every material has the 18 channels in order")

    wavelength_ok = all(
        entry["channels"][channel]["wavelength_nm"] == WAVELENGTHS[channel]
        for entry in materials.values()
        for channel in CHANNELS
    )
    checks.ok(wavelength_ok, "every wavelength matches the channel schema")

    checks.equal(
        list(materials), sorted(materials),
        "materials are stored in deterministic sorted order",
    )

    # ==================================================================
    checks.section("3. reflectance is reproducible from the raw data")

    # The whole point of keeping raw Sample/Dark/White: the derived value
    # must be checkable, not taken on faith.
    worst = 0.0
    worst_where = None

    for name, entry in materials.items():
        for channel in CHANNELS:
            cell = entry["channels"][channel]
            denominator = cell["white_reference"] - cell["dark_reference"]

            recomputed = (
                cell["raw_sample"] - cell["dark_reference"]
            ) / denominator

            residual = abs(recomputed - cell["reflectance_as_supplied"])

            if residual > worst:
                worst, worst_where = residual, "{} {}".format(name, channel)

    checks.ok(
        worst <= 5e-5,
        "every supplied reflectance is reproduced by "
        "(Sample-Dark)/(White-Dark) to within source rounding "
        "(worst {:.2e} at {})".format(worst, worst_where),
    )

    # A wrong-but-plausible implementation would omit the Dark term from
    # the denominator. That is only detectable on D485, the one channel
    # with a non-zero Dark - so test it explicitly.
    d_cell = materials["Iron(III) Oxide Red"]["channels"]["D"]

    checks.close(
        d_cell["dark_reference"], 3.4855,
        "D485 is the one channel with a non-zero Dark",
    )

    without_dark_in_denominator = (
        d_cell["raw_sample"] - d_cell["dark_reference"]
    ) / d_cell["white_reference"]

    checks.ok(
        abs(without_dark_in_denominator
            - d_cell["reflectance_as_supplied"]) > 1e-4,
        "the denominator genuinely subtracts Dark - omitting it would "
        "give a different answer on D485",
    )

    # ==================================================================
    checks.section("4. nothing was clipped, filled or corrected")

    above_one = {
        name: [
            channel for channel in CHANNELS
            if entry["channels"][channel]["reflectance_as_supplied"] > 1.0
        ]
        for name, entry in materials.items()
    }
    above_one = {name: ch for name, ch in above_one.items() if ch}

    checks.equal(
        sorted(above_one), ["Citric Acid", "Magnesium Carbonate", "Talc"],
        "the three materials that exceed reflectance 1.0 are preserved, "
        "not clipped",
    )
    checks.close(
        materials["Magnesium Carbonate"]["channels"]["F"][
            "reflectance_as_supplied"],
        1.2003,
        "the largest value in the session survives intact at 1.2003",
    )
    checks.ok(
        all("REFLECTANCE_ABOVE_ONE" in materials[name]["quality_flags"]
            for name in above_one),
        "and each is flagged rather than silently accepted",
    )

    # Iron(II,III) Oxide Black reads exactly zero on K680 and L705.
    black = materials["Iron(II,III) Oxide Black"]["channels"]

    checks.close(black["K"]["raw_sample"], 0.0, "K680 raw Sample is 0.0000")
    checks.close(black["L"]["raw_sample"], 0.0, "L705 raw Sample is 0.0000")
    checks.ok(
        "ZERO_RAW_SAMPLE" in materials["Iron(II,III) Oxide Black"][
            "quality_flags"],
        "a zero raw reading is flagged, not treated as a missing value",
    )

    # ==================================================================
    checks.section("5. the W940 Dark anomaly is represented")

    # The standalone Dark table lists A..V. W940 appears only inside the
    # material tables. That distinction must survive into the database,
    # because a future reader must not believe W940 Dark was measured
    # directly when it was inferred from agreement across 23 tables.
    shared = document["shared_references"]

    checks.equal(
        shared["dark_inferred_channels"], ["W"],
        "W is recorded as the channel whose Dark was inferred",
    )
    checks.ok(
        "W" not in shared["dark"],
        "W is absent from the shared Dark block, matching the source",
    )

    provenance = {
        materials[name]["channels"]["W"]["dark_provenance"]
        for name in materials
    }
    checks.equal(
        provenance, {"INFERRED_FROM_MATERIAL_TABLES"},
        "every material marks its W940 Dark as inferred",
    )

    other = {
        materials[name]["channels"]["A"]["dark_provenance"]
        for name in materials
    }
    checks.equal(
        other, {"STANDALONE_DARK_TABLE"},
        "while channels that ARE in the Dark table say so",
    )

    # ==================================================================
    checks.section("6. acquisition settings are unknown, not invented")

    settings = materials["Talc"]["acquisition_settings"]

    for field in ("gain", "integration_cycles", "measurement_mode",
                  "geometry"):
        checks.ok(
            str(settings[field]).startswith("UNKNOWN"),
            "{} is UNKNOWN - the historical session did not record "
            "it".format(field),
        )

    # ==================================================================
    checks.section("7. provenance is verifiable")

    actual_hash = hashlib.sha256(SOURCE.read_bytes()).hexdigest()

    checks.equal(
        document["source"]["sha256"], actual_hash,
        "the recorded hash matches the source snapshot on disk",
    )
    checks.equal(
        document["source"]["sha256"], actual_hash,
        "and the database records the hash of what it was built from",
    )
    checks.equal(
        document["measurement_type"], "MEASURED",
        "DB1 is labelled MEASURED - the only database that may be",
    )
    checks.equal(
        document["reflectance_equation"],
        "R = (Sample - Dark) / (White - Dark)",
        "the equation used is recorded with the data",
    )
    checks.equal(
        document["audit"]["errors"], [],
        "the build recorded no structural errors",
    )
    checks.ok(
        len(document["audit"]["anomalies"]) >= 3,
        "and did record the source anomalies rather than hiding them",
    )

    # ==================================================================
    checks.section("8. the build is deterministic")

    before = MATERIALS.read_text(encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(BUILDER)],
        capture_output=True, text=True, cwd=str(support.REPO.parent),
    )

    checks.equal(result.returncode, 0, "the builder re-runs cleanly")

    after = MATERIALS.read_text(encoding="utf-8")

    # generated_at is a timestamp and legitimately differs; everything
    # else must be byte-identical on a rebuild from unchanged input.
    def strip_timestamp(text):
        document = json.loads(text)
        document.pop("generated_at", None)
        return json.dumps(document, sort_keys=True)

    checks.equal(
        strip_timestamp(before), strip_timestamp(after),
        "rebuilding from the same source reproduces the same database",
    )

    return checks.report()


if __name__ == "__main__":
    sys.exit(main_tests())
