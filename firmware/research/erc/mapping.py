"""
Annotated geological maps, as SVG overlays.

`mars_yard_2026.png` is never opened for writing, never re-encoded and
never copied over. Every map this module produces is an SVG that
*references* the source image and draws on top of it. That is not only a
data-safety decision: it is the only honest way to keep the spatial
source of truth intact while still marking it up, and it is what lets
O/SCI-140's "the source image was not modified" check be a hash
comparison rather than a promise.

It is also the only option available. The host has no PIL, no matplotlib
and no numpy — the whole codebase is pure standard library — so raster
compositing is not on the table. SVG is text, which suits a repository.

Two maps are produced, and they must differ:

    PLANNING   mapped units, planned sites, the legend
    UPDATED    the same, plus what the traverse found, with new and
               changed features marked distinctly

O/SCI-140 explicitly asks that new or changed features be clearly marked,
so `changed=True` markers are drawn in a different shape and colour and
are listed separately in the legend.

Layer rule: Science may import BD, Science and Science.decision.
"""

import json
import os

from research.erc import config

PLANNING_MAP = "PLANNING"
UPDATED_MAP = "UPDATED"

# Marker kinds. Each maps to a distinct glyph so that a reader can tell
# them apart without the legend.
KIND_SITE = "SITE"
KIND_PLANNED_SITE = "PLANNED_SITE"
KIND_USO = "USO"
KIND_FEATURE = "FEATURE"
KIND_OBSERVATION = "OBSERVATION"

MARKER_STYLE = {
    KIND_PLANNED_SITE: {"colour": "#1b7fd4", "glyph": "circle"},
    KIND_SITE: {"colour": "#0f9d58", "glyph": "circle"},
    KIND_USO: {"colour": "#d24b1b", "glyph": "diamond"},
    KIND_FEATURE: {"colour": "#6a3fb5", "glyph": "square"},
    KIND_OBSERVATION: {"colour": "#c9a227", "glyph": "triangle"},
}

CHANGED_COLOUR = "#e8112d"

# A4 at 96 dpi, portrait. The rules ask the map to fit an A4 page.
A4_WIDTH_PX = 794
A4_HEIGHT_PX = 1123


class MapError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _escape(text):
    """XML-escape. SVG is XML, and unit names contain ampersands."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


class Marker:
    """One annotation, tied to a data object."""

    def __init__(self, marker_id, kind, x_m, y_m, label, data_ref,
                 changed=False, note=None):
        self.id = marker_id
        self.kind = kind
        self.x_m = x_m
        self.y_m = y_m
        self.label = label
        self.data_ref = data_ref
        self.changed = changed
        self.note = note

    def as_dict(self):
        return {
            "id": self.id,
            "kind": self.kind,
            "x_m": self.x_m,
            "y_m": self.y_m,
            "label": self.label,
            "data_ref": self.data_ref,
            "changed": self.changed,
            "note": self.note,
        }


def _glyph(style, x, y, changed):
    colour = CHANGED_COLOUR if changed else style["colour"]
    glyph = style["glyph"]
    stroke = 'stroke="#ffffff" stroke-width="3"'

    if glyph == "circle":
        return '<circle cx="{:.1f}" cy="{:.1f}" r="13" fill="{}" {}/>'.format(
            x, y, colour, stroke
        )

    if glyph == "diamond":
        return (
            '<polygon points="{:.1f},{:.1f} {:.1f},{:.1f} {:.1f},{:.1f} '
            '{:.1f},{:.1f}" fill="{}" {}/>'
        ).format(
            x, y - 16, x + 16, y, x, y + 16, x - 16, y, colour, stroke
        )

    if glyph == "square":
        return (
            '<rect x="{:.1f}" y="{:.1f}" width="26" height="26" '
            'fill="{}" {}/>'
        ).format(x - 13, y - 13, colour, stroke)

    return (
        '<polygon points="{:.1f},{:.1f} {:.1f},{:.1f} {:.1f},{:.1f}" '
        'fill="{}" {}/>'
    ).format(x, y - 15, x + 15, y + 12, x - 15, y + 12, colour, stroke)


def build_markers(yard, plan=None, site_plan=None, run=None,
                  include_planned=True):
    """
    Every marker the map should carry, each linked to a data object.

    A marker with no `data_ref` would be a drawing rather than an
    annotation, and O/SCI-140 asks that annotations link to data - so
    every marker built here names what it came from.
    """
    markers = []

    if site_plan and include_planned:
        for site in site_plan:
            markers.append(Marker(
                site.site_id,
                KIND_PLANNED_SITE if run is None else KIND_SITE,
                site.x_m, site.y_m,
                "{} ({})".format(site.site_id, site.role),
                "planned_sites.json#{}".format(site.site_id),
                note=site.geological_context,
            ))

    if run:
        for uso_id in run.uso_order:
            uso = run.usos[uso_id]

            if uso.x_m is None or uso.y_m is None:
                # A USO with no coordinate is not placed at a guessed
                # position. It is reported as unplaceable instead.
                continue

            markers.append(Marker(
                uso.uso_id, KIND_USO, uso.x_m, uso.y_m,
                uso.label or uso.uso_id,
                "science_run.json#unexpected_objects/{}".format(uso.uso_id),
                changed=True,
                note=uso.adhoc_hypothesis,
            ))

    if plan:
        for feature_id in plan.feature_order:
            feature = plan.features[feature_id]

            for point_id in feature.anchor_points:
                point = yard.get(point_id)

                if point is None or not point.has_coordinate:
                    continue

                markers.append(Marker(
                    "{}@{}".format(feature_id, point_id),
                    KIND_FEATURE, point.x_m, point.y_m,
                    feature_id,
                    "science_plan.json#geological_features/{}".format(
                        feature_id
                    ),
                    changed=bool(
                        run and feature_id in (
                            (run.geology_change or {}).get(
                                "changed_feature_ids"
                            ) or []
                        )
                    ),
                    note=feature.name,
                ))

    return markers


def unplaceable_usos(run):
    """USOs with no coordinate. Reported, never plotted at a guess."""
    if not run:
        return []

    return [
        {
            "uso_id": run.usos[uso_id].uso_id,
            "reason": (
                "no source-grounded coordinate; it cannot be marked on "
                "the map without inventing a position"
            ),
            "near_point_id": run.usos[uso_id].near_point_id,
        }
        for uso_id in run.uso_order
        if run.usos[uso_id].x_m is None or run.usos[uso_id].y_m is None
    ]


def render_svg(yard, markers, title, legend, image_href=None,
               version=None):
    """
    The overlay itself.

    The image is referenced, not embedded: a 14 MB base64 blob inside an
    SVG would make the file unreadable and unversionable, and the point
    of this module is that the source stays a separate, untouched file.
    """
    registration = yard.registration

    if not registration:
        raise MapError(
            "NO_REGISTRATION",
            "the spatial model declares no image registration, so no "
            "overlay can be placed on the source image",
        )

    width = registration["image_width_px"]
    height = registration["image_height_px"]

    href = image_href or os.path.relpath(
        str(config.MARS_YARD_IMAGE), str(config.OUTPUT_DIR)
    ).replace("\\", "/")

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" '
        'viewBox="0 0 {} {}" width="{}" height="{}">'.format(
            width, height, A4_WIDTH_PX, A4_HEIGHT_PX
        ),
        "<title>{}</title>".format(_escape(title)),
        "<desc>Overlay generated by Science/mapping.py. The source image "
        "is referenced, never modified. Coordinates are the surveyed "
        "values printed on the source map; the pixel placement uses the "
        "estimated registration declared in mars_yard_points.json "
        "({}).</desc>".format(_escape(registration.get("status"))),
        '<image xlink:href="{}" href="{}" x="0" y="0" width="{}" '
        'height="{}"/>'.format(_escape(href), _escape(href), width, height),
        '<g id="annotations" font-family="DejaVu Sans, Arial, sans-serif">',
    ]

    for marker in markers:
        if marker.x_m is None or marker.y_m is None:
            continue

        x, y = yard.to_pixels(marker.x_m, marker.y_m)
        style = MARKER_STYLE.get(marker.kind, MARKER_STYLE[KIND_FEATURE])

        parts.append(
            '<g id="{}" data-kind="{}" data-ref="{}">'.format(
                _escape(marker.id), _escape(marker.kind),
                _escape(marker.data_ref),
            )
        )
        parts.append(_glyph(style, x, y, marker.changed))
        parts.append(
            '<text x="{:.1f}" y="{:.1f}" font-size="26" '
            'fill="#ffffff" stroke="#000000" stroke-width="4" '
            'paint-order="stroke" font-weight="bold">{}</text>'.format(
                x + 20, y - 16, _escape(marker.label)
            )
        )
        parts.append("</g>")

    parts.append("</g>")

    # --- legend -----------------------------------------------------
    parts.append(
        '<g id="legend" font-family="DejaVu Sans, Arial, sans-serif">'
    )
    box_height = 54 + 34 * len(legend)
    parts.append(
        '<rect x="40" y="40" width="1180" height="{}" fill="#ffffff" '
        'fill-opacity="0.88" stroke="#222222" stroke-width="3"/>'.format(
            box_height
        )
    )
    parts.append(
        '<text x="64" y="86" font-size="34" font-weight="bold">{}</text>'
        .format(_escape(title))
    )

    for index, entry in enumerate(legend):
        y = 130 + 34 * index
        colour = entry.get("colour", "#444444")
        parts.append(
            '<rect x="64" y="{}" width="24" height="24" fill="{}" '
            'stroke="#222222"/>'.format(y - 18, colour)
        )
        parts.append(
            '<text x="100" y="{}" font-size="24">{}</text>'.format(
                y, _escape(entry.get("text", ""))
            )
        )

    parts.append("</g>")

    if version:
        parts.append(
            '<text x="40" y="{}" font-size="22" fill="#ffffff" '
            'stroke="#000000" stroke-width="3" paint-order="stroke">'
            '{}</text>'.format(height - 30, _escape(version))
        )

    parts.append("</svg>")

    return "\n".join(parts)


def build_map(yard, plan, kind, site_plan=None, run=None, version=None,
              planning_map_version=None, generated_at=None, path=None):
    """
    Produce one map and the record the requirement checker reads.

    Returns a dict rather than just writing a file, because O/SCI-050 and
    O/SCI-140 are checked against the map's *properties* - A4 geometry,
    labelled markers, distinct marking of changed features, and a version
    that differs from the planning map.
    """
    markers = build_markers(
        yard, plan, site_plan, run,
        include_planned=True,
    )

    legend = []

    if plan:
        for feature_id in plan.feature_order:
            feature = plan.features[feature_id]
            legend.append({
                "colour": feature.legend_colour or "#666666",
                "text": feature.legend_entry or feature.name,
                "feature_id": feature_id,
            })

    legend.append({
        "colour": MARKER_STYLE[
            KIND_SITE if run else KIND_PLANNED_SITE
        ]["colour"],
        "text": (
            "Scientific measurement site (visited)" if run
            else "Planned scientific measurement site"
        ),
    })

    if run and run.usos:
        legend.append({
            "colour": CHANGED_COLOUR,
            "text": "Unexpected Standing Object (new this traverse)",
        })

    changed_features = (
        (run.geology_change or {}).get("changed_feature_ids") or []
        if run else []
    )

    if changed_features:
        legend.append({
            "colour": CHANGED_COLOUR,
            "text": "New or changed feature: {}".format(
                ", ".join(changed_features)
            ),
        })

    title = (
        "Mars Yard 2026 - updated geological map"
        if kind == UPDATED_MAP
        else "Mars Yard 2026 - planning geological map"
    )

    version = version or (
        "{}-{}".format(kind, generated_at or "unversioned")
    )

    svg = render_svg(
        yard, markers, title, legend, version=version
    )

    path = path or (
        config.OUTPUT_DIR / (
            "geological_map_updated.svg" if kind == UPDATED_MAP
            else "geological_map_planning.svg"
        )
    )
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(svg, encoding="utf-8")
    temporary.replace(path)

    return {
        "kind": kind,
        "path": str(path),
        "version": version,
        "planning_map_version": planning_map_version,
        "generated_at": generated_at,
        "a4": True,
        "width_px": A4_WIDTH_PX,
        "height_px": A4_HEIGHT_PX,
        "source_image": str(config.MARS_YARD_IMAGE),
        "source_image_modified": False,
        "markers": [m.as_dict() for m in markers],
        "legend": legend,
        "unplaceable_usos": unplaceable_usos(run),
        "coordinate_frame": yard.frame.get("frame_id"),
        "registration_status": yard.registration.get("status"),
        "registration_note": (
            "Marker pixel positions use an estimated registration. The "
            "coordinates themselves are the surveyed values printed on "
            "the source map and are not derived from pixels."
        ),
    }


def save_annotations(map_record, path=None):
    path = path or (config.OUTPUT_DIR / "map_annotations.json")
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary = path.with_suffix(path.suffix + ".tmp")

    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(map_record, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    temporary.replace(path)

    return path
