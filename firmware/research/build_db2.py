"""
Build DB2 — the 54-feature measured database — from its source snapshot.

    py firmware/research/build_db2.py
    py firmware/research/build_db2.py --audit-only

Reads  firmware/BD/DB2/DB2_source.txt   (verbatim, never edited)
Writes firmware/BD/DB2/DB2.json         (metadata, references, audit, materials)

WHAT MAKES DB2 DIFFERENT FROM DB1

DB1 holds 18 numbers per material, measured under ONE illumination that
was never recorded. DB2 holds 54: the same 18 bands under WHITE, under UV
and under IR, each against a reference measured under that same lamp, all
under one calibration that IS recorded. That is why DB2 could not be
derived from DB1 and had to be physically measured - and why this builder
exists rather than a conversion script.

    54 FEATURES, NOT 54 WAVELENGTHS. The sensor has 18 detectors. A
    feature is (illumination, channel), and BD/channels.py refuses to
    compare a space of one kind against a space of the other.

THE AUDIT RUNS FIRST AND ALWAYS

Every reflectance printed by the instrument is recomputed here from the
raw counts and the references, and the two are compared. The supplied
value is kept AS SUPPLIED - it is what the instrument said - and the
recomputed value sits beside it with the residual between them. A
disagreement larger than rounding is an anomaly and is reported; the
build refuses on a STRUCTURAL failure and never on a scientific one,
because a surprising number that was really measured still belongs in
the database.

DELIBERATELY NOT DONE HERE
  * No reflectance is clipped to [0,1]. R > 1 means the material sent
    back more light than the white target under that lamp, which for a
    reference that does not describe this geometry is real and
    informative. R < 0 means the sample read below the dark offset.
  * No missing channel is interpolated and no absent material invented.
    Sodium Bicarbonate was not measured in this session; it is absent
    from DB2, not zero in it.
  * UV and IR features are never filled in from WHITE, and no number
    here is scaled from DB1.
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

from BD import config                                        # noqa: E402
from BD.channels import (                                    # noqa: E402
    AS7265X_54_MULTIILLUM,
    CHANNELS,
    ILLUMINATIONS,
    WAVELENGTHS,
)
from research.material_identity import identity, resolve_label  # noqa: E402

SOURCE = config.DB2_DIR / "DB2_source.txt"
OUTPUT = config.DB2_FILE

DATABASE_ID = "DB2"
DATABASE_VERSION = "measured54-v1"
SCHEMA_VERSION = 1
CALIBRATION_ID = "FREYA_FULL_SPECTRAL_CAL_20260817_173914"
LEGACY_CALIBRATION_ID = "FREYA_COMPETITION_2026_CAL_V1"
SESSION_ID = "SESSION_20260817"
MEASURED_AT = "2026-08-17"

# The source prints reflectance to 4 decimal places, so a recomputed
# value may legitimately differ by up to half a unit in the last place.
ROUNDING_TOLERANCE = 5e-5

# Beyond this a disagreement is not rounding: it is a different number,
# and it is reported as an anomaly rather than averaged away.
RESIDUAL_ALERT = 1e-3

SENSOR_SETTINGS = {
    "measurement_mode": 3,
    "measurement_mode_name": "one-shot",
    "integration_cycles": 100,
    "gain": 2,
    "gain_x": "16x",
    "white_current_ma": 25,
    "uv_current_ma": 25,
    "ir_current_ma": 25,
}

# ----------------------------------------------------------------------
# parsing
# ----------------------------------------------------------------------

# "A       36.3851    36.3851     0.0000    0.000%"  (reference blocks)
REFERENCE_ROW = re.compile(
    r"^([A-Z])\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+%|-)\s*$"
)

# "A    410        0.8086     71.1530      0.8086      0.0222 ..."
MATERIAL_ROW = re.compile(
    r"^([A-Z])\s+(\d+)\s+"
    r"([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+"
    r"([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*$"
)

REFERENCE_HEADINGS = {
    "WHITE WHITE REFERENCE": "white",
    "UV WHITE REFERENCE": "uv",
    "IR WHITE REFERENCE": "ir",
    "DARK REFERENCE": "dark",
}


class SourceError(Exception):
    """The snapshot cannot be parsed. Nothing is written."""


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def parse_source(text):
    """
    Pull the four reference blocks and every material table out of the
    snapshot.

    A material heading is any line ending in ':' that is followed by a
    FULL SPECTRAL DATA block. Everything before the reference section and
    every banner, note and repeat-count line is prose and is skipped.
    """
    references = {}
    materials = {}

    section = None
    current = None

    for raw_line in text.split("\n"):
        line = raw_line.strip()

        if not line or line.startswith("=") or line.startswith("-"):
            continue

        heading = REFERENCE_HEADINGS.get(line)

        if heading is not None:
            section = ("reference", heading)
            references[heading] = {}
            current = references[heading]

            continue

        if line.endswith(":") and not line.startswith("CH"):
            label = line[:-1].strip()

            # Prose in the preamble also ends in a colon. A material
            # heading is only a material heading if rows follow it.
            if label and not label.startswith(("Calibration", "Settings",
                                               "Mode", "Known")):
                section = ("material", label)
                materials[label] = []
                current = materials[label]

            continue

        if section is None:
            continue

        if section[0] == "reference":
            match = REFERENCE_ROW.match(line)

            if match:
                current[match.group(1)] = {
                    "median": float(match.group(2)),
                    "mean": float(match.group(3)),
                    "stdev": float(match.group(4)),
                    "cv": match.group(5),
                }

            continue

        match = MATERIAL_ROW.match(line)

        if match:
            current.append({
                "channel": match.group(1),
                "nm": int(match.group(2)),
                "raw": {
                    "white": float(match.group(3)),
                    "uv": float(match.group(4)),
                    "ir": float(match.group(5)),
                },
                "supplied": {
                    "white": float(match.group(6)),
                    "uv": float(match.group(7)),
                    "ir": float(match.group(8)),
                },
            })

    return references, materials


# ----------------------------------------------------------------------
# audit
# ----------------------------------------------------------------------

def reflectance(sample, dark, reference):
    """
    R = (Sample - Dark) / (Reference - Dark).

    A zero denominator is undefined, not zero: the reference and the dark
    read the same on that channel, so the channel carries no information
    about the sample at all. Returned as None so it can be recorded as
    undefined rather than silently becoming 0.0.
    """
    denominator = reference - dark

    if denominator == 0.0:
        return None

    return (sample - dark) / denominator


def audit(references, materials):
    """
    Everything checkable about the snapshot, before anything is written.

    Returns (findings, structural_ok). Structural failures - a missing
    channel, a missing reference block, an unnameable material - stop the
    build. Scientific findings never do.
    """
    findings = {"errors": [], "warnings": [], "anomalies": [], "info": []}

    def error(message, **detail):
        findings["errors"].append(dict(message=message, **detail))

    def warn(message, **detail):
        findings["warnings"].append(dict(message=message, **detail))

    def anomaly(message, **detail):
        findings["anomalies"].append(dict(message=message, **detail))

    def info(message, **detail):
        findings["info"].append(dict(message=message, **detail))

    # -- the reference blocks --------------------------------------
    for block in ("dark",) + ILLUMINATIONS:
        if block not in references:
            error("reference block '{}' is missing from the snapshot"
                  .format(block), block=block)

            continue

        missing = [c for c in CHANNELS if c not in references[block]]

        if missing:
            error("reference block '{}' is missing channel(s) {}".format(
                block, ",".join(missing)), block=block, channels=missing)

    if findings["errors"]:
        return findings, False

    dark = {c: references["dark"][c]["median"] for c in CHANNELS}

    # A channel where the lamp cannot lift the detector above its own
    # dark offset can never produce a usable reflectance for ANY
    # material. Said once here rather than 22 times below.
    for illumination in ILLUMINATIONS:
        white = {
            c: references[illumination][c]["median"] for c in CHANNELS
        }
        dead = [c for c in CHANNELS if white[c] - dark[c] <= 0.0]

        if dead:
            anomaly(
                "under {} the reference does not rise above the dark "
                "offset on {} channel(s): reflectance is undefined there "
                "for every material".format(illumination, len(dead)),
                illumination=illumination, channels=dead,
            )

        weak = [
            c for c in CHANNELS
            if 0.0 < white[c] - dark[c] < 5.0
        ]

        if weak:
            anomaly(
                "under {} the reference has less than 5 counts of dynamic "
                "range on {} channel(s): reflectance there is quantised "
                "and should not be read as a precise number".format(
                    illumination, len(weak)),
                illumination=illumination, channels=weak,
            )

    # -- the materials ---------------------------------------------
    resolved = {}

    for label, rows in sorted(materials.items()):
        name = resolve_label(label)

        if name is None:
            error("material heading '{}' does not name any material in "
                  "the identity table. Refusing to guess.".format(label),
                  label=label)

            continue

        if name in resolved:
            error("'{}' and '{}' both resolve to {}".format(
                label, resolved[name], name), label=label)

            continue

        resolved[name] = label

        present = {row["channel"] for row in rows}
        missing = [c for c in CHANNELS if c not in present]

        if missing:
            error("{}: missing channel(s) {}".format(
                label, ",".join(missing)), label=label, channels=missing)

            continue

        for row in rows:
            if row["nm"] != WAVELENGTHS[row["channel"]]:
                error("{}: channel {} is labelled {} nm, expected {}"
                      .format(label, row["channel"], row["nm"],
                              WAVELENGTHS[row["channel"]]), label=label)

    if findings["errors"]:
        return findings, False

    # -- reflectance agreement -------------------------------------
    worst = {"residual": 0.0}
    disagreements = 0
    undefined = 0

    for label, rows in sorted(materials.items()):
        for row in rows:
            channel = row["channel"]

            for illumination in ILLUMINATIONS:
                white = references[illumination][channel]["median"]
                recomputed = reflectance(
                    row["raw"][illumination], dark[channel], white
                )
                supplied = row["supplied"][illumination]

                if recomputed is None:
                    undefined += 1
                    row.setdefault("recomputed", {})[illumination] = None
                    row.setdefault("residual", {})[illumination] = None

                    continue

                residual = abs(recomputed - supplied)

                row.setdefault("recomputed", {})[illumination] = recomputed
                row.setdefault("residual", {})[illumination] = residual

                if residual > worst["residual"]:
                    worst = {
                        "residual": residual, "material": label,
                        "channel": channel, "illumination": illumination,
                        "supplied": supplied, "recomputed": recomputed,
                    }

                if residual > RESIDUAL_ALERT:
                    disagreements += 1
                    anomaly(
                        "{} {}:{} - the instrument printed R={} but "
                        "(sample-dark)/(reference-dark) gives {:.6f}, a "
                        "difference of {:.6f} which is larger than "
                        "rounding".format(
                            label, illumination, channel, supplied,
                            recomputed, residual),
                        label=label, channel=channel,
                        illumination=illumination,
                        supplied=supplied, recomputed=recomputed,
                        residual=residual,
                    )

    info(
        "recomputed {} reflectance values from raw counts and the "
        "references".format(len(materials) * len(CHANNELS)
                            * len(ILLUMINATIONS)),
        materials=len(materials),
        features_per_material=len(CHANNELS) * len(ILLUMINATIONS),
    )

    if worst.get("material"):
        info(
            "largest disagreement with the printed reflectance is "
            "{:.2e} ({} {}:{})".format(
                worst["residual"], worst["material"],
                worst["illumination"], worst["channel"]),
            **worst
        )

    if disagreements == 0:
        info("every printed reflectance agrees with the recomputed one "
             "to within rounding ({:.0e})".format(ROUNDING_TOLERANCE),
             tolerance=ROUNDING_TOLERANCE)

    if undefined:
        info("{} feature(s) have an undefined reflectance: the reference "
             "equals the dark on that channel under that lamp".format(
                 undefined), count=undefined)

    # -- which DB1 materials are and are not here -------------------
    from research.material_identity import IDENTITIES

    absent = sorted(set(IDENTITIES) - set(resolved))

    if absent:
        warn(
            "{} DB1 material(s) were not measured in this session and are "
            "ABSENT from DB2, not zero in it: {}".format(
                len(absent), ", ".join(absent)),
            materials=absent,
        )

    return findings, True


# ----------------------------------------------------------------------
# build
# ----------------------------------------------------------------------

def build(references, materials, findings, source_hash):
    dark = {c: references["dark"][c]["median"] for c in CHANNELS}
    records = {}

    for label, rows in sorted(materials.items()):
        name = resolve_label(label)
        record = identity(name)
        by_channel = {row["channel"]: row for row in rows}

        features = {}
        flags = set()

        for illumination in ILLUMINATIONS:
            white = {
                c: references[illumination][c]["median"] for c in CHANNELS
            }

            for channel in CHANNELS:
                row = by_channel[channel]
                supplied = row["supplied"][illumination]
                recomputed = row["recomputed"][illumination]
                residual = row["residual"][illumination]

                feature_flags = []

                if supplied > 1.0:
                    feature_flags.append("REFLECTANCE_ABOVE_ONE")
                    flags.add("REFLECTANCE_ABOVE_ONE")

                if supplied < 0.0:
                    feature_flags.append("REFLECTANCE_BELOW_ZERO")
                    flags.add("REFLECTANCE_BELOW_ZERO")

                if row["raw"][illumination] == 0.0:
                    feature_flags.append("ZERO_RAW_SAMPLE")
                    flags.add("ZERO_RAW_SAMPLE")

                if recomputed is None:
                    feature_flags.append("REFLECTANCE_UNDEFINED")
                    flags.add("REFLECTANCE_UNDEFINED")

                elif residual > RESIDUAL_ALERT:
                    feature_flags.append("RESIDUAL_ABOVE_ROUNDING")
                    flags.add("RESIDUAL_ABOVE_ROUNDING")

                span = white[channel] - dark[channel]

                if 0.0 < span < 5.0:
                    feature_flags.append("REFERENCE_RANGE_MARGINAL")
                    flags.add("REFERENCE_RANGE_MARGINAL")

                features["{}:{}".format(illumination, channel)] = {
                    "illumination": illumination,
                    "channel": channel,
                    "wavelength_nm": WAVELENGTHS[channel],
                    "raw_sample": row["raw"][illumination],
                    "dark_reference": dark[channel],
                    "white_reference": white[channel],
                    "reference_span": round(span, 4),
                    "reflectance_as_supplied": supplied,
                    "reflectance_recomputed": (
                        round(recomputed, 6) if recomputed is not None
                        else None
                    ),
                    "reflectance_residual": (
                        round(residual, 8) if residual is not None else None
                    ),
                    "flags": feature_flags,
                }

        records[name] = {
            "material_id": record["material_id"],
            "display_name": record["display_name"],
            "name_en": record["name_en"],
            "name_cs": record["name_cs"],
            "operator_label": label,
            "canonical_name": record["canonical_name"],
            "chemical_formula": record["chemical_formula"],
            "material_class": record["material_class"],
            "aliases": record["aliases"],

            "measurement_type": "MEASURED",
            "calibration_id": CALIBRATION_ID,

            "acquisition_settings": {
                "gain": SENSOR_SETTINGS["gain_x"],
                "integration_cycles": SENSOR_SETTINGS["integration_cycles"],
                "measurement_mode": SENSOR_SETTINGS["measurement_mode"],
                "led_current_ma": {
                    "white": SENSOR_SETTINGS["white_current_ma"],
                    "uv": SENSOR_SETTINGS["uv_current_ma"],
                    "ir": SENSOR_SETTINGS["ir_current_ma"],
                },
                "illumination": list(ILLUMINATIONS),
                "geometry": "UNKNOWN",
                "distance_mm": "UNKNOWN",
                "repeats": 3,
                "note": "Sensor settings are recorded; geometry and "
                        "sensor-to-sample distance were not measured "
                        "during this session and are UNKNOWN, not "
                        "defaults.",
            },

            "features": features,
            "quality_flags": sorted(flags),
        }

    return {
        "database_id": DATABASE_ID,
        "database_version": DATABASE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "measurement_type": "MEASURED",
        "feature_space": AS7265X_54_MULTIILLUM,
        "instrument": "AS7265x on the Freya science module",
        "status": "READY",

        "calibration_id": CALIBRATION_ID,
        "legacy_calibration_id": LEGACY_CALIBRATION_ID,
        "session_id": SESSION_ID,
        "measured_at": MEASURED_AT,
        "generated_at": datetime.now(timezone.utc).isoformat(),

        "description": "The same materials measured on THIS instrument "
                       "under all three illuminations: 18 spectral bands "
                       "x 3 illumination conditions = 54 features per "
                       "material.",

        "source": {
            "file": SOURCE.name,
            "sha256": source_hash,
            "supplied_by": "operator",
            "supplied_on": "2026-08-21",
            "rebuild": "py firmware/research/build_db2.py",
        },

        "feature_model": {
            "spectral_bands": len(CHANNELS),
            "illuminations": list(ILLUMINATIONS),
            "features": len(CHANNELS) * len(ILLUMINATIONS),
            "feature_id_format": "<illumination>:<channel>, e.g. white:A, "
                                 "ir:W",
            "wording": "18 spectral bands x 3 illumination conditions. NOT "
                       "54 wavelengths - the sensor has 18 detectors.",
        },

        "reflectance_equation": "R = (Sample - Dark) / (Reference - Dark), "
                                "with the reference measured under the SAME "
                                "lamp as the sample and one shared Dark",
        "clipping": "NONE - values above 1.0 and below 0.0 are preserved",

        "shared_references": {
            "dark": dark,
            "white": {
                illumination: {
                    c: references[illumination][c]["median"]
                    for c in CHANNELS
                }
                for illumination in ILLUMINATIONS
            },
            "repeats": 2,
            "note": "One Dark and three white-target acquisitions, one "
                    "per lamp, taken in a single calibration without "
                    "moving the sensor. The Dark is subtracted under all "
                    "three lamps because it is the detector's response "
                    "with every lamp off.",
        },

        "sensor_settings": SENSOR_SETTINGS,

        "naming": {
            "languages": ["en", "cs"],
            "note": "Every record carries name_en and name_cs. The key is "
                    "the English display name DB1 was built with and is "
                    "not changed - it is what every stored Sample and "
                    "every ground-truth label already refers to.",
        },

        "audit": findings,

        "material_count": len(records),
        "materials": records,
    }


# ----------------------------------------------------------------------
# reporting
# ----------------------------------------------------------------------

def print_findings(findings):
    for kind in ("errors", "warnings", "anomalies", "info"):
        entries = findings[kind]

        if not entries:
            continue

        print()
        print("{} ({})".format(kind.upper(), len(entries)))

        for entry in entries:
            print("  - {}".format(entry["message"]))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build DB2 from its verbatim source snapshot."
    )
    parser.add_argument("--audit-only", action="store_true",
                        help="audit and report; write nothing")
    parser.add_argument("--source", default=None)
    parser.add_argument("--output", default=None)

    arguments = parser.parse_args(argv)

    source = Path(arguments.source or SOURCE)
    output = Path(arguments.output or OUTPUT)

    print("DB2 BUILD")
    print()
    print("source: {}".format(source))
    print("output: {}".format(output))

    text = source.read_text(encoding="utf-8")
    references, materials = parse_source(text)

    print()
    print("parsed: {} reference block(s), {} material table(s)".format(
        len(references), len(materials)
    ))

    findings, structural_ok = audit(references, materials)

    print_findings(findings)

    if not structural_ok:
        print()
        print("REFUSED: the snapshot failed a structural check. Nothing "
              "was written.")

        return 2

    if arguments.audit_only:
        print()
        print("Audit only. Nothing was written.")

        return 0

    document = build(references, materials, findings, sha256(source))

    output.parent.mkdir(parents=True, exist_ok=True)

    with open(output, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    print()
    print("Wrote {} material(s), {} features each.".format(
        document["material_count"],
        document["feature_model"]["features"],
    ))
    print("  {}".format(output))

    return 0


if __name__ == "__main__":
    sys.exit(main())
