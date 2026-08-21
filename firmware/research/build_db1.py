"""
Build DB1 — the historical measured database — from its source snapshot.

    py firmware/research/build_db1.py
    py firmware/research/build_db1.py --audit-only

Reads  firmware/BD/data/DB1_source.txt   (verbatim, never edited)
Writes firmware/BD/data/DB1.json         (everything: metadata, audit, materials)

DB1 is the only database whose numbers came off this instrument. The audit
runs first and always; nothing is written if the source fails a structural
check, because a database built from data nobody looked at is worse than
no database.

DELIBERATELY NOT DONE HERE
  * No reflectance is clipped to [0,1]. Values above 1.0 are real and flagged.
  * No missing value is interpolated.
  * No acquisition setting is invented - the historical session did not
    record gain, integration time or geometry, so they are UNKNOWN.
  * The supplied reflectance is kept AS SUPPLIED beside a recomputed one.
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

FIRMWARE_ROOT = Path(__file__).resolve().parent.parent

if str(FIRMWARE_ROOT) not in sys.path:
    sys.path.insert(0, str(FIRMWARE_ROOT))

from BD import config                                    # noqa: E402
from BD.channels import AS7265X_18, CHANNELS, WAVELENGTHS  # noqa: E402
from research.material_identity import identity           # noqa: E402

SOURCE = config.DB1_SOURCE
OUTPUT = config.DB1_FILE

DATABASE_ID = "DB1"
DATABASE_VERSION = "measured18-v1"
SCHEMA_VERSION = 2
LEGACY_CALIBRATION_ID = "FREYA_COMPETITION_2026_CAL_V1"

# Source rounding is 4 decimal places, so a recomputed reflectance may
# legitimately differ by up to half a unit in the last place. Beyond
# RESIDUAL_ALERT a disagreement is not rounding - it is a different number.
ROUNDING_TOLERANCE = 5e-5
RESIDUAL_ALERT = 1e-3

ROW = re.compile(
    r"^([A-Z])\s*\|\s*(\d+)\s*\|\s*"
    r"([-\d.]+)\s*\|\s*([-\d.]+)\s*\|\s*([-\d.]+)\s*\|\s*([-\d.]+)\s*$"
)
REFERENCE_ROW = re.compile(r"^([A-Z])\s*\|\s*(\d+)\s*\|\s*([-\d.]+)\s*$")

NOISE = (
    "---", "ch  |", "CH  |", "Dark Measurement", "White Measurement",
    "===", "SOURCE SNAPSHOT", "Supplied by", "This file", "formatting",
    "produced and", "DB1 is generated", "firmware/BD", "this file is",
    "correction is", "record the", "Known anomalies",
)

# ----------------------------------------------------------------------
# Canonical identity of the 23 materials.
#
# Reference knowledge ABOUT the substances - names, formulae, classes,
# and the Czech labels printed on the containers - never used to alter a
# measured value.
#
# It used to be a dict in this file. DB2 measures the SAME 23 physical
# containers and needs the same identities, and two copies of a name
# table drift apart the first time one of them is corrected. So it
# moved to research/material_identity.py and both builders read it
# from there.
# ----------------------------------------------------------------------


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ----------------------------------------------------------------------
# parsing
# ----------------------------------------------------------------------

def parse_source(text):
    """
    Pull the two reference blocks and every material table out of the
    snapshot.

    Deliberately tolerant of the source's formatting irregularities - a
    stray header line, headers before or after the material name, marker
    lines - because those irregularities are preserved on purpose and the
    parser must cope with the real file rather than an idealised one.
    """
    dark_reference = {}
    white_reference = {}
    materials = {}

    section = None
    current = None
    in_preamble = True

    for raw_line in text.split("\n"):
        line = raw_line.strip()

        if not line:
            continue

        if in_preamble:
            if line.startswith("Dark Measurement"):
                in_preamble = False
            else:
                continue

        if line.startswith("Dark Measurement"):
            section, current = "dark", None
            continue

        if line.startswith("White Measurement"):
            section, current = "white", None
            continue

        reference_match = REFERENCE_ROW.match(line)

        if reference_match and section in ("dark", "white"):
            channel, nm, value = reference_match.groups()
            target = dark_reference if section == "dark" else white_reference
            target[channel] = float(value)
            continue

        row_match = ROW.match(line)

        if row_match:
            if current is None:
                raise ValueError(
                    "data row outside any material block: {!r}".format(line)
                )

            channel, nm, dark, white, sample, reflectance = row_match.groups()

            current["rows"].append({
                "channel": channel,
                "nm": int(nm),
                "dark": float(dark),
                "white": float(white),
                "sample": float(sample),
                "reflectance_as_supplied": float(reflectance),
            })
            continue

        if any(line.startswith(prefix) for prefix in NOISE):
            continue

        if line.startswith(("1.", "2.", "3.", "4.")):
            continue

        name = line.rstrip(":").strip()

        if name:
            section = "material"
            current = {"name": name, "rows": []}
            materials[name] = current

    return dark_reference, white_reference, materials


# ----------------------------------------------------------------------
# audit
# ----------------------------------------------------------------------

def audit(dark_reference, white_reference, materials):
    """
    Returns (findings, structural_ok). Only STRUCTURAL failures block the
    build; anomalies that are genuine properties of the historical data
    are recorded and carried into the database.
    """
    findings = []
    structural_ok = True

    def note(severity, code, message, **detail):
        findings.append({
            "severity": severity, "code": code,
            "message": message, "detail": detail,
        })

    expected = list(CHANNELS)

    note("INFO", "MATERIAL_COUNT",
         "{} materials parsed".format(len(materials)),
         count=len(materials), materials=sorted(materials))

    if len(materials) != 23:
        note("ERROR", "MATERIAL_COUNT_UNEXPECTED",
             "expected 23 historical materials, parsed {}".format(
                 len(materials)))
        structural_ok = False

    seen = {}
    for name in materials:
        seen.setdefault(name.lower().replace(" ", ""), []).append(name)

    for _key, names in seen.items():
        if len(names) > 1:
            note("ERROR", "DUPLICATE_MATERIAL",
                 "materials collide after normalisation: {}".format(names))
            structural_ok = False

    missing_dark = [c for c in expected if c not in dark_reference]

    if missing_dark:
        note("ANOMALY", "DARK_REFERENCE_INCOMPLETE",
             "the standalone Dark table is missing {}; every material table "
             "nevertheless carries it. The value is INFERRED from repeated "
             "material-table evidence, not read from the Dark block."
             .format(",".join(missing_dark)),
             missing=missing_dark,
             resolution="inferred_from_material_tables")

    missing_white = [c for c in expected if c not in white_reference]

    if missing_white:
        note("ERROR", "WHITE_REFERENCE_INCOMPLETE",
             "White reference missing {}".format(",".join(missing_white)))
        structural_ok = False

    dark_disagreements = []
    white_disagreements = []
    residual_report = {}
    above_one = {}
    zero_samples = {}
    inferred_support = {}
    worst_residual = 0.0

    for name, material in sorted(materials.items()):
        rows = material["rows"]

        if [row["channel"] for row in rows] != expected:
            note("ERROR", "CHANNEL_SET_MISMATCH",
                 "{}: channels are not the 18 in wavelength order".format(name))
            structural_ok = False
            continue

        for row in rows:
            channel = row["channel"]

            if row["nm"] != WAVELENGTHS[channel]:
                note("ERROR", "WAVELENGTH_MISMATCH",
                     "{} {}: source says {} nm, schema says {} nm".format(
                         name, channel, row["nm"], WAVELENGTHS[channel]))
                structural_ok = False

            for field in ("dark", "white", "sample",
                          "reflectance_as_supplied"):
                value = row[field]

                if value != value or value in (
                    float("inf"), float("-inf")
                ):
                    note("ERROR", "NON_FINITE",
                         "{} {} {} is not finite".format(name, channel, field))
                    structural_ok = False

            if channel in dark_reference:
                if abs(row["dark"] - dark_reference[channel]) > 1e-9:
                    dark_disagreements.append("{} {}".format(name, channel))
            else:
                inferred_support.setdefault(channel, set()).add(row["dark"])

            if channel in white_reference:
                if abs(row["white"] - white_reference[channel]) > 1e-9:
                    white_disagreements.append("{} {}".format(name, channel))

            denominator = row["white"] - row["dark"]

            if denominator == 0:
                note("ERROR", "ZERO_DENOMINATOR",
                     "{} {}: White equals Dark".format(name, channel))
                structural_ok = False
                continue

            recomputed = (row["sample"] - row["dark"]) / denominator
            residual = abs(recomputed - row["reflectance_as_supplied"])

            row["reflectance_recomputed"] = recomputed
            row["reflectance_residual"] = residual
            worst_residual = max(worst_residual, residual)

            if residual > RESIDUAL_ALERT:
                residual_report.setdefault(name, []).append({
                    "channel": channel,
                    "supplied": row["reflectance_as_supplied"],
                    "recomputed": round(recomputed, 6),
                    "residual": round(residual, 6),
                })

            if row["reflectance_as_supplied"] > 1.0:
                above_one.setdefault(name, []).append(channel)

            if row["sample"] == 0.0:
                zero_samples.setdefault(name, []).append(channel)

    if dark_disagreements:
        note("ERROR", "DARK_INCONSISTENT",
             "material tables disagree with the standalone Dark reference",
             examples=dark_disagreements[:10])
        structural_ok = False
    else:
        note("INFO", "DARK_CONSISTENT",
             "every material table agrees with the standalone Dark reference")

    if white_disagreements:
        note("ERROR", "WHITE_INCONSISTENT",
             "material tables disagree with the standalone White reference",
             examples=white_disagreements[:10])
        structural_ok = False
    else:
        note("INFO", "WHITE_CONSISTENT",
             "every material table agrees with the standalone White reference")

    for channel, values in sorted(inferred_support.items()):
        note("ANOMALY", "DARK_VALUE_INFERRED",
             "{} Dark is absent from the standalone table; all {} material "
             "tables agree on {}".format(
                 channel, len(materials), sorted(values)),
             channel=channel, distinct_values=sorted(values),
             agreement="unanimous" if len(values) == 1 else "CONFLICTING")

        if len(values) != 1:
            structural_ok = False

    if residual_report:
        note("WARNING", "REFLECTANCE_RESIDUAL",
             "{} material(s) disagree with (Sample-Dark)/(White-Dark) by "
             "more than {}".format(len(residual_report), RESIDUAL_ALERT),
             materials=residual_report)
    else:
        note("INFO", "REFLECTANCE_REPRODUCED",
             "every supplied reflectance is reproduced by "
             "(Sample-Dark)/(White-Dark); worst residual {:.2e}".format(
                 worst_residual))

    if above_one:
        note("ANOMALY", "REFLECTANCE_ABOVE_ONE",
             "{} material(s) exceed reflectance 1.0. NOT clipped - a sample "
             "brighter than the white reference is real information about "
             "geometry or scattering.".format(len(above_one)),
             materials=above_one)

    if zero_samples:
        note("ANOMALY", "ZERO_RAW_SAMPLE",
             "channels whose raw Sample is exactly 0.0000 - at or below the "
             "detector floor, preserved rather than treated as missing",
             materials=zero_samples)

    return findings, structural_ok


# ----------------------------------------------------------------------
# build
# ----------------------------------------------------------------------

def build(dark_reference, white_reference, materials, findings, source_hash):
    inferred = [c for c in CHANNELS if c not in dark_reference]
    records = {}

    for name, material in sorted(materials.items()):
        rows = {row["channel"]: row for row in material["rows"]}
        record = identity(name)

        flags = sorted({
            flag for channel in CHANNELS for flag in (
                (["REFLECTANCE_ABOVE_ONE"]
                 if rows[channel]["reflectance_as_supplied"] > 1.0 else [])
                + (["ZERO_RAW_SAMPLE"]
                   if rows[channel]["sample"] == 0.0 else [])
            )
        })

        records[name] = {
            "material_id": record["material_id"],
            "display_name": record["display_name"],
            "name_en": record["name_en"],
            "name_cs": record["name_cs"],
            "canonical_name": record["canonical_name"],
            "chemical_formula": record["chemical_formula"],
            "material_class": record["material_class"],
            "aliases": record["aliases"],

            "measurement_type": "MEASURED",
            "calibration_id": LEGACY_CALIBRATION_ID,

            "acquisition_settings": {
                "gain": "UNKNOWN",
                "integration_cycles": "UNKNOWN",
                "measurement_mode": "UNKNOWN",
                "led_current": "UNKNOWN",
                "illumination": "UNKNOWN_SINGLE_SOURCE",
                "geometry": "UNKNOWN",
                "distance_mm": "UNKNOWN",
                "repeats": "UNKNOWN",
                "note": "The historical session did not record acquisition "
                        "settings. These are UNKNOWN, not defaults.",
            },

            "channels": {
                channel: {
                    "wavelength_nm": rows[channel]["nm"],
                    "raw_sample": rows[channel]["sample"],
                    "dark_reference": rows[channel]["dark"],
                    "white_reference": rows[channel]["white"],
                    "reflectance_as_supplied":
                        rows[channel]["reflectance_as_supplied"],
                    "reflectance_recomputed": round(
                        rows[channel]["reflectance_recomputed"], 6),
                    "reflectance_residual": round(
                        rows[channel]["reflectance_residual"], 8),
                    "dark_provenance": (
                        "INFERRED_FROM_MATERIAL_TABLES"
                        if channel in inferred else "STANDALONE_DARK_TABLE"
                    ),
                }
                for channel in CHANNELS
            },

            "quality_flags": flags,
        }

    return {
        "database_id": DATABASE_ID,
        "database_version": DATABASE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "measurement_type": "MEASURED",
        "feature_space": AS7265X_18,
        "instrument": "AS7265x on the Freya science module",
        "calibration_id": LEGACY_CALIBRATION_ID,
        "generated_at": datetime.now(timezone.utc).isoformat(),

        "description": "The historical measurement session: 23 materials "
                       "measured on this instrument under a single "
                       "illumination, with one shared Dark and White.",

        "source": {
            "file": SOURCE.name,
            "sha256": source_hash,
            "supplied_by": "project author",
            "supplied_on": "2026-08-15",
            "rebuild": "py firmware/research/build_db1.py",
        },

        "reflectance_equation": "R = (Sample - Dark) / (White - Dark)",
        "clipping": "NONE - values above 1.0 and at 0.0 are preserved",

        "shared_references": {
            "dark": dict(dark_reference),
            "white": dict(white_reference),
            "dark_inferred_channels": inferred,
            "note": "W940 is absent from the standalone Dark table. Its "
                    "value comes from the material tables, where all 23 "
                    "agree on 0.0000. Recorded as inferred, not measured.",
        },

        "audit": {
            "errors": [f for f in findings if f["severity"] == "ERROR"],
            "warnings": [f for f in findings if f["severity"] == "WARNING"],
            "anomalies": [f for f in findings if f["severity"] == "ANOMALY"],
            "info": [f for f in findings if f["severity"] == "INFO"],
        },

        "material_count": len(records),
        "materials": records,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit-only", action="store_true")
    args = parser.parse_args()

    text = SOURCE.read_text(encoding="utf-8")
    source_hash = sha256(SOURCE)

    dark_reference, white_reference, materials = parse_source(text)
    findings, structural_ok = audit(dark_reference, white_reference, materials)

    print("=" * 70)
    print("DB1 AUDIT")
    print("=" * 70)
    print("source    : {}".format(SOURCE.name))
    print("sha256    : {}".format(source_hash))
    print("materials : {}".format(len(materials)))
    print()

    for finding in findings:
        print("[{:<7}] {}".format(finding["severity"], finding["code"]))
        print("          {}".format(finding["message"]))

    counts = {
        severity: len([f for f in findings if f["severity"] == severity])
        for severity in ("ERROR", "WARNING", "ANOMALY")
    }

    print()
    print("errors {ERROR} | warnings {WARNING} | anomalies {ANOMALY}".format(
        **counts))

    if not structural_ok:
        print()
        print("STRUCTURAL FAILURE - DB1 NOT BUILT")

        return 1

    if args.audit_only:
        return 0

    document = build(
        dark_reference, white_reference, materials, findings, source_hash
    )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(document, indent=2), encoding="utf-8")

    print()
    print("DB1 built: {} materials -> {}".format(
        document["material_count"], OUTPUT.name))

    return 0


if __name__ == "__main__":
    sys.exit(main())
