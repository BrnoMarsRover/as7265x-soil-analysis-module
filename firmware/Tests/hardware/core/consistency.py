"""
Does the repository still describe the carousel it actually has?

THE DEFECT THIS FINDS

The mechanism was once eight slots at 45 degrees. It is now four at 90.
`firmware/ESP32/config.py` was changed; several documents were not. An
operator following a document that says "Slot 5" or "45 degrees per
slot" will align the plate wrongly and then report a firmware fault -
and the firmware will be blameless.

WHAT IS AUTHORITATIVE, AND WHAT IS NOT

`firmware/ESP32/config.py` is authoritative because it is what the code
does. Every other statement in the repository is a claim ABOUT that, and
a claim that disagrees is stale.

SCOPE, AND WHY A CONTRADICTION IS REPORTED RATHER THAN FIXED

This campaign may only write under `firmware/Tests/hardware/`. A stale
claim in `README.md` is a real finding and is reported with its exact
path and line - but it is not edited here, because silently rewriting a
production document from a test is exactly the kind of unrequested
change that makes a test suite untrustworthy.

THE SCAN IS DELIBERATELY NARROW. It looks for slot counts, slot angles
and slot numbers, because those are the claims that put a plate in the
wrong place. It does not attempt to parse prose.
"""

import re
from pathlib import Path


# Documents worth scanning. Anything else in the repository is either
# generated, binary, or not about the mechanism.
SCANNED_SUFFIXES = (".md", ".txt")

# Directories that are never scanned: generated evidence, caches, and
# the Altium tree, which is CAD and not documentation.
SKIP_PARTS = ("__pycache__", "artifacts", ".git", "Hardware",
              "__Previews", "Photos", "data")

# Files inside this campaign's own scope - a contradiction here can and
# should be fixed by this campaign.
IN_SCOPE_PREFIX = "firmware/Tests/hardware/"


# "8 slots", "eight slots", "slot count of 8"
SLOT_COUNT = re.compile(
    r"\b(\d{1,2})\s*[- ]?\s*slots?\b", re.IGNORECASE)

# "45 degrees", "45°", "45 deg" near the word slot
SLOT_ANGLE = re.compile(
    r"\b(\d{1,3}(?:\.\d+)?)\s*(?:degrees?|deg\b|°)", re.IGNORECASE)

# "Slot 5", "slot 7"
SLOT_NUMBER = re.compile(r"\bslot\s+(\d{1,2})\b", re.IGNORECASE)


def authoritative_geometry():
    """The shipped geometry, read from production configuration."""
    from ..configuration.profile import production_values

    values = production_values()

    carousel = values["carousel"]
    servo = values["servo"]

    return {
        "slot_count": carousel["slot_count"],
        "slot_spacing_deg": carousel["slot_spacing_deg"],
        "half_turn_deg": carousel["half_turn_deg"],
        "scan_load_offset_slots": carousel["scan_load_offset_slots"],
        "counts_per_rev": servo["counts_per_rev"],
        "source": "firmware/ESP32/config.py",
    }


def _documents(repo_root):
    for path in sorted(Path(repo_root).rglob("*")):
        if not path.is_file():
            continue

        if path.suffix.lower() not in SCANNED_SUFFIXES:
            continue

        if any(part in SKIP_PARTS for part in path.parts):
            continue

        yield path


def scan(repo_root):
    """
    Every claim in the repository's documents that the configuration
    contradicts.

    Returns the authoritative geometry, the contradictions with exact
    paths and line numbers, and how many files were read - so a scan
    that found nothing because it read nothing is distinguishable from
    a clean one.
    """
    repo_root = Path(repo_root)

    geometry = authoritative_geometry()

    slot_count = geometry["slot_count"]
    spacing = float(geometry["slot_spacing_deg"])

    contradictions = []
    scanned = 0

    for path in _documents(repo_root):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")

        except OSError:                                # pragma: no cover
            continue

        scanned += 1

        relative = path.relative_to(repo_root).as_posix()

        in_scope = relative.startswith(IN_SCOPE_PREFIX)

        for number, line in enumerate(text.splitlines(), 1):
            lowered = line.lower()

            # A line that is explicitly ABOUT the stale model - saying
            # so - is not a contradiction. This scanner's own
            # documentation says "not eight slots" and must not report
            # itself.
            if "not eight" in lowered or "not 8 slots" in lowered:
                continue

            # A line that describes SEARCHING for the stale model is
            # not a claim that the stale model is current. HANDOFF.md
            # documents a grep for "8-slot" precisely so somebody
            # notices the boundary moved.
            if ("grep" in lowered or "searches for" in lowered
                    or "looks for" in lowered):
                continue

            if "45" in line and (
                    "not 45" in lowered or "instead of 45" in lowered):
                continue

            for match in SLOT_COUNT.finditer(line):
                claimed = int(match.group(1))

                if claimed == slot_count:
                    continue

                # "2 slots" is a distance, not a plate size, and the
                # scan/load offset really is 2.
                if claimed == geometry["scan_load_offset_slots"]:
                    continue

                contradictions.append({
                    "path": relative, "line": number,
                    "claim": line.strip()[:140],
                    "kind": "slot_count",
                    "claimed": claimed,
                    "expected": slot_count,
                    "in_scope": in_scope,
                })

            if "slot" in lowered:
                for match in SLOT_ANGLE.finditer(line):
                    claimed = float(match.group(1))

                    if claimed in (spacing, geometry["half_turn_deg"],
                                   360.0, 0.0):
                        continue

                    # Fine adjustment and tolerance angles are small and
                    # legitimately not the slot spacing.
                    if claimed < 20.0:
                        continue

                    contradictions.append({
                        "path": relative, "line": number,
                        "claim": line.strip()[:140],
                        "kind": "slot_angle",
                        "claimed": claimed,
                        "expected": spacing,
                        "in_scope": in_scope,
                    })

            for match in SLOT_NUMBER.finditer(line):
                claimed = int(match.group(1))

                if 1 <= claimed <= slot_count:
                    continue

                contradictions.append({
                    "path": relative, "line": number,
                    "claim": line.strip()[:140],
                    "kind": "slot_number",
                    "claimed": claimed,
                    "expected": "1..{}".format(slot_count),
                    "in_scope": in_scope,
                })

    return {
        "authoritative": geometry,
        "contradictions": contradictions,
        "files_scanned": scanned,
        "in_scope": len([c for c in contradictions if c["in_scope"]]),
        "out_of_scope": len([c for c in contradictions
                             if not c["in_scope"]]),
    }
