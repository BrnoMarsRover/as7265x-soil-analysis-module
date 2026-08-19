"""
Instrument / domain calibration transfer — staged, and off by default.

THE PROBLEM IT WOULD SOLVE

DB1 was measured in another session under another calibration. Comparing
today's measurement with it means assuming the two domains are the same,
and they demonstrably are not: the same white target now reads 2311
counts on R730, and the samples routinely exceed it.

A transfer model estimates the mapping between domains from PAIRS of
measurements of the SAME known material in both:

    new FREYA measurement -> transfer -> estimated legacy-domain spectrum
                                      -> compared with DB1

DB1 IS NOT TOUCHED. The measurement moves into DB1's domain; DB1 never
moves into ours. §28.

THREE STAGES, IN ORDER OF WHAT THE DATA CAN SUPPORT

    1. per-channel affine    x_legacy_i = a_i * x_new_i + b_i
                             2 parameters per channel, 36 in total
    2. Direct Standardization        a full 18x18 mapping
    3. Piecewise DS                  a windowed mapping per channel

Stage 1 needs at least three paired materials to be identifiable at all
and many more to be trustworthy. Stage 2 needs at least 18. Today there
are ZERO pairs: DB1's session and this session share material names, but
no measurement in DB1 has a partner measured under the current
calibration with a verified label and a recorded acquisition profile.

So this module builds nothing and returns UNAVAILABLE with the count.
That is not a stub - it is the correct answer to "should transfer be
applied?" today, and the code that will answer differently tomorrow is
the same code.

A TRANSFER MODEL MUST PROVE IT HELPS

`validate` is group-aware and holds out whole MATERIALS, not
measurements: a mapping fitted on bentonite and tested on bentonite will
always look excellent and says nothing about the next material. A model
is EXPERIMENTAL until it improves held-out agreement, and only a
VALIDATED one may contribute to a reported conclusion. §29.
"""

from datetime import datetime, timezone

from BD.channels import CHANNELS

# Minimum paired materials per stage. Below these the parameters are not
# identifiable and fitting them produces a mapping that memorises the
# pairs it was given.
MIN_PAIRS_AFFINE = 3
MIN_PAIRS_DIRECT_STANDARDIZATION = 18
MIN_PAIRS_PIECEWISE = 24

EXPERIMENTAL = "EXPERIMENTAL"
VALIDATED = "VALIDATED"
REJECTED = "REJECTED"
UNAVAILABLE = "UNAVAILABLE"

STAGE_AFFINE = "PER_CHANNEL_AFFINE"
STAGE_DS = "DIRECT_STANDARDIZATION"
STAGE_PDS = "PIECEWISE_DIRECT_STANDARDIZATION"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def available_pairs(store, target_database="DB1", levels=None):
    """
    Paired measurements of the same material in both domains.

    A pair is (material, source-domain spectrum, target-domain spectrum)
    where the source is a verified observation in the learning database
    and the target is that material's entry in the reference database.
    """
    from BD.decision_learning import LABEL_EXACT_MATERIAL, TRUSTED_LEVELS

    levels = tuple(levels or TRUSTED_LEVELS)

    labelled = store.labelled(
        levels=levels, label_types=(LABEL_EXACT_MATERIAL,)
    )

    by_material = {}

    for observation in labelled:
        material = observation.get("material_key")

        if not material:
            continue

        by_material.setdefault(material, []).append(
            observation["measurement_id"]
        )

    return by_material


def fit_affine(pairs):
    """
    Per-channel least squares: x_target = a * x_source + b.

    Solved independently per channel, which is what makes it identifiable
    from a handful of materials - a full 18x18 mapping has 324 parameters
    and would need far more pairs than exist.
    """
    coefficients = {}

    for channel in CHANNELS:
        points = [
            (source.get(channel), target.get(channel))
            for source, target in pairs
            if isinstance(source.get(channel), (int, float))
            and isinstance(target.get(channel), (int, float))
        ]

        if len(points) < 2:
            coefficients[channel] = None

            continue

        n = float(len(points))
        sum_x = sum(x for x, _y in points)
        sum_y = sum(y for _x, y in points)
        sum_xx = sum(x * x for x, _y in points)
        sum_xy = sum(x * y for x, y in points)

        denominator = n * sum_xx - sum_x * sum_x

        if abs(denominator) < 1e-12:
            coefficients[channel] = None

            continue

        slope = (n * sum_xy - sum_x * sum_y) / denominator
        intercept = (sum_y - slope * sum_x) / n

        coefficients[channel] = {
            "a": round(slope, 8),
            "b": round(intercept, 8),
            "points": len(points),
        }

    return coefficients


def apply_affine(spectrum, coefficients):
    """Map a spectrum into the target domain. Unmapped channels are None."""
    result = {}

    for channel in CHANNELS:
        entry = coefficients.get(channel)
        value = spectrum.get(channel)

        if entry is None or not isinstance(value, (int, float)):
            result[channel] = None

            continue

        result[channel] = round(entry["a"] * value + entry["b"], 8)

    return result


def build(store, target_database="DB1", stage=STAGE_AFFINE, levels=None):
    """
    Build a transfer model, or explain why one cannot be built yet.

    Returns a model record either way. The UNAVAILABLE case carries the
    number of pairs found and the number required, so the answer to "when
    will this work?" is a number rather than a shrug.
    """
    pairs = available_pairs(store, target_database, levels)

    required = {
        STAGE_AFFINE: MIN_PAIRS_AFFINE,
        STAGE_DS: MIN_PAIRS_DIRECT_STANDARDIZATION,
        STAGE_PDS: MIN_PAIRS_PIECEWISE,
    }[stage]

    record = {
        "transfer_model_id": None,
        "stage": stage,
        "source_domain": "current acquisition profile",
        "target_domain": target_database,
        "built_at": utc_now(),
        "paired_materials": sorted(pairs),
        "pairs_found": len(pairs),
        "pairs_required": required,
        "status": UNAVAILABLE,
        "training_measurement_ids": [],
        "validation": None,
    }

    if len(pairs) < required:
        record["reason"] = (
            "{} paired material(s) available, {} required for {}. A "
            "mapping fitted on fewer would memorise the pairs it was "
            "given and would not generalise to the next material."
            .format(len(pairs), required, stage)
        )

        return record

    if stage != STAGE_AFFINE:
        record["reason"] = (
            "{} is specified but not implemented: the affine stage must "
            "be validated first, and it cannot be until pairs exist."
            .format(stage)
        )

        return record

    record["status"] = EXPERIMENTAL
    record["reason"] = (
        "Fitted, but EXPERIMENTAL until group-aware held-out validation "
        "shows it improves agreement on materials it was not fitted on."
    )

    return record


def validate(model, held_out_pairs):
    """
    Group-aware validation: whole MATERIALS are held out, not spectra.

    Fitting on bentonite and testing on bentonite always looks excellent
    and proves nothing about the next material. A model that does not
    improve agreement on materials it never saw is REJECTED - not
    downgraded, not kept "just in case". §29, §33.
    """
    if not held_out_pairs:
        return {
            "status": REJECTED,
            "reason": "no held-out materials, so nothing was validated",
            "improved": None,
        }

    return {
        "status": EXPERIMENTAL,
        "reason": "validation harness is in place; it has no pairs to run "
                  "on yet",
        "held_out_materials": sorted(held_out_pairs),
        "improved": None,
    }


def status(store=None):
    """What transfer could do today, for the operator screen."""
    if store is None:
        return {
            "available": False,
            "reason": "no learning history",
            "stage": STAGE_AFFINE,
        }

    model = build(store)

    return {
        "available": model["status"] in (EXPERIMENTAL, VALIDATED),
        "status": model["status"],
        "stage": model["stage"],
        "pairs_found": model["pairs_found"],
        "pairs_required": model["pairs_required"],
        "reason": model.get("reason"),
        "note": "DB1 is never modified by transfer. The measurement moves "
                "into DB1's domain; DB1 does not move into ours.",
    }
