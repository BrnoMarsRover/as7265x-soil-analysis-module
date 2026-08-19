"""
The Mars Yard spatial model.

`mars_yard_2026.png` is the spatial source of truth. Everything this
module serves was transcribed from the coordinate tables printed on that
map into `data/mars_yard_points.json`; nothing here computes a position
from pixels, and nothing here invents one.

The distinction the software must never lose:

    starting location   where a run may begin          NOT a target
    navigation waypoint a surveyed nav position        candidate target
    landmark            a surveyed physical feature    candidate target
    deep sampling point another sub-task's location    NOT a target
    geological feature  an interpretation we drew      see plan.py
    scientific site     a target we chose              see sites.py
    sample              material actually measured     see BD/samples.py
    measurement         one acquisition of a sample    see Measurements/

Those are seven different things. Collapsing any two of them is how a
mission ends up reporting that it measured a start line.

Layer rule: Science may import BD, Measurements and DecisionModel.
"""

import json
import math

from Science import config

# A coordinate we actually have, versus one the source never supplied.
# There is no third state: a coordinate is either printed on the map or it
# does not exist, and "approximately here" is not a coordinate.
SOURCE_GROUNDED = "SOURCE_GROUNDED"
UNKNOWN = "UNKNOWN"

STARTING_LOCATION = "STARTING_LOCATION"
LANDMARK = "LANDMARK"
NAVIGATION_WAYPOINT = "NAVIGATION_WAYPOINT"
DEEP_SAMPLING_LOCATION = "DEEP_SAMPLING_LOCATION"

OBJECT_TYPES = (
    STARTING_LOCATION,
    LANDMARK,
    NAVIGATION_WAYPOINT,
    DEEP_SAMPLING_LOCATION,
)


class MarsYardError(Exception):
    """The spatial model is missing, malformed or self-contradictory."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class YardPoint:
    """One surveyed object, with the provenance of its coordinate."""

    def __init__(self, entry):
        self.point_id = entry["point_id"]
        self.type = entry["type"]
        self.label = entry.get("label", self.point_id)

        self.x_m = entry.get("x_m")
        self.y_m = entry.get("y_m")
        self.h_m = entry.get("h_m")

        self.coordinate_status = entry.get("coordinate_status", UNKNOWN)
        self.zone_association = entry.get("zone_association")
        self.zone_status = entry.get("zone_status")

        self.sub_task = entry.get("sub_task")
        self.excluded_from_scientific_exploration = entry.get(
            "excluded_from_scientific_exploration", False
        )
        self.exclusion_reason = entry.get("exclusion_reason")

    @property
    def has_coordinate(self):
        return (
            self.coordinate_status == SOURCE_GROUNDED
            and self.x_m is not None
            and self.y_m is not None
        )

    def distance_to(self, other):
        """
        Horizontal separation in metres.

        Horizontal on purpose: H spans 0.74 m across the whole yard while
        X and Y span tens of metres, so a 3-D distance would be the 2-D
        distance with noise added. Height is kept and reported, just not
        mixed into separation.
        """
        if not (self.has_coordinate and other.has_coordinate):
            raise MarsYardError(
                "NO_COORDINATE",
                "cannot measure between {} and {}: at least one has no "
                "source-grounded coordinate".format(
                    self.point_id, other.point_id
                ),
            )

        return math.hypot(self.x_m - other.x_m, self.y_m - other.y_m)

    def as_dict(self):
        return {
            "point_id": self.point_id,
            "type": self.type,
            "label": self.label,
            "x_m": self.x_m,
            "y_m": self.y_m,
            "h_m": self.h_m,
            "coordinate_status": self.coordinate_status,
            "coordinate_frame": None,   # filled in by MarsYard.point_record
            "zone_association": self.zone_association,
        }

    def __repr__(self):
        return "<YardPoint {} {} ({}, {})>".format(
            self.point_id, self.type, self.x_m, self.y_m
        )


class MarsYard:
    """The surveyed objects of the 2026 yard, loaded once."""

    def __init__(self, path=None):
        self.path = path or config.MARS_YARD_POINTS_FILE
        self.document = self._read()

        self.frame = self.document.get("coordinate_frame") or {}
        self.registration = self.document.get("image_registration") or {}
        self.object_type_descriptions = self.document.get("object_types") or {}
        self.zone_source = self.document.get("zone_source") or {}

        self.points = {}

        for entry in self.document.get("points") or []:
            point = YardPoint(entry)

            if point.point_id in self.points:
                raise MarsYardError(
                    "DUPLICATE_POINT",
                    "point id {} appears more than once".format(
                        point.point_id
                    ),
                )

            if point.type not in OBJECT_TYPES:
                raise MarsYardError(
                    "UNKNOWN_TYPE",
                    "{} declares unknown object type {}".format(
                        point.point_id, point.type
                    ),
                )

            self.points[point.point_id] = point

        if not self.points:
            raise MarsYardError(
                "EMPTY", "the spatial model contains no points"
            )

    def _read(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return json.load(handle)

        except OSError as error:
            raise MarsYardError(
                "MISSING",
                "the Mars Yard spatial model could not be read from {}: "
                "{}".format(self.path, error),
            )

        except ValueError as error:
            raise MarsYardError(
                "MALFORMED",
                "the Mars Yard spatial model at {} is not valid JSON: "
                "{}".format(self.path, error),
            )

    # ------------------------------------------------------------------
    # lookup
    # ------------------------------------------------------------------

    def __getitem__(self, point_id):
        try:
            return self.points[point_id]

        except KeyError:
            raise MarsYardError(
                "NO_SUCH_POINT",
                "{} is not a surveyed object on this map".format(point_id),
            )

    def get(self, point_id):
        return self.points.get(point_id)

    def of_type(self, *types):
        """Points of the given object types, in map order."""
        return [
            point for point in self.points.values()
            if point.type in types
        ]

    def starting_locations(self):
        return self.of_type(STARTING_LOCATION)

    def landmarks(self):
        return self.of_type(LANDMARK)

    def waypoints(self):
        return self.of_type(NAVIGATION_WAYPOINT)

    def site_candidates(self):
        """
        Objects a scientific site may legitimately be placed on.

        Start locations are excluded because they mark where a run
        begins, not what is worth measuring. The deep sampling point is
        excluded because its location is organiser-defined and the rules
        forbid using that sub-task's material as Scientific Exploration
        evidence.
        """
        return [
            point for point in self.of_type(*config.SITE_ELIGIBLE_TYPES)
            if point.has_coordinate
            and not point.excluded_from_scientific_exploration
        ]

    # ------------------------------------------------------------------
    # geometry
    # ------------------------------------------------------------------

    def distance(self, a, b):
        return self[a].distance_to(self[b])

    def extent(self):
        """Bounding box of every source-grounded point, in metres."""
        located = [p for p in self.points.values() if p.has_coordinate]

        return {
            "min_x_m": min(p.x_m for p in located),
            "max_x_m": max(p.x_m for p in located),
            "min_y_m": min(p.y_m for p in located),
            "max_y_m": max(p.y_m for p in located),
            "min_h_m": min(p.h_m for p in located if p.h_m is not None),
            "max_h_m": max(p.h_m for p in located if p.h_m is not None),
            "point_count": len(located),
        }

    def to_pixels(self, x_m, y_m):
        """
        Map metres to source-image pixels, for annotation only.

        The transform is an ESTIMATE registered from plotted markers, and
        it is declared as such in the data file. It exists so an overlay
        can be drawn on top of the image; it never feeds a coordinate
        back into the model.
        """
        if not self.registration:
            raise MarsYardError(
                "NO_REGISTRATION",
                "the spatial model declares no image registration, so "
                "metres cannot be placed on the image",
            )

        origin_x, origin_y = self.registration["origin_px"]
        scale = self.registration["scale_px_per_m"]

        return (origin_x + scale * x_m, origin_y - scale * y_m)

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------

    def point_record(self, point_id):
        """A point as it should be embedded in a traceable record."""
        point = self[point_id]
        record = point.as_dict()
        record["coordinate_frame"] = self.frame.get("frame_id")
        record["source"] = self.document.get("generated_from")

        return record

    def counts(self):
        counts = {kind: 0 for kind in OBJECT_TYPES}

        for point in self.points.values():
            counts[point.type] += 1

        return counts

    def validate(self):
        """
        Problems that would make the model unusable or dishonest.

        Returns a list of strings; empty means usable.
        """
        problems = []

        for point in self.points.values():
            if point.coordinate_status == SOURCE_GROUNDED:
                for axis in ("x_m", "y_m"):
                    if getattr(point, axis) is None:
                        problems.append(
                            "{} claims a source-grounded coordinate but "
                            "{} is null".format(point.point_id, axis)
                        )

            elif point.coordinate_status != UNKNOWN:
                problems.append(
                    "{} declares coordinate status {}, which is neither "
                    "{} nor {}".format(
                        point.point_id, point.coordinate_status,
                        SOURCE_GROUNDED, UNKNOWN,
                    )
                )

        if not self.frame.get("frame_id"):
            problems.append("the coordinate frame has no frame_id")

        # A geodetic reference we do not have must stay absent. If one
        # ever appears without the source supplying it, that is an
        # invention and the model should refuse to be trusted.
        if self.frame.get("geodetic_reference") is not None:
            problems.append(
                "a geodetic reference is present, but the source map "
                "supplies none - this would be an invented transformation"
            )

        if not self.site_candidates():
            problems.append("no object is eligible to carry a science site")

        return problems

    def status(self):
        counts = self.counts()

        return {
            "document_id": self.document.get("document_id"),
            "schema_version": self.document.get("schema_version"),
            "source": self.document.get("generated_from"),
            "coordinate_frame": self.frame.get("frame_id"),
            "frame_kind": self.frame.get("kind"),
            "units": self.frame.get("units"),
            "geodetic_status": self.frame.get("geodetic_status"),
            "counts": counts,
            "total_points": sum(counts.values()),
            "site_candidates": len(self.site_candidates()),
            "registration_status": self.registration.get("status"),
            "problems": self.validate(),
        }


def load(path=None):
    """Load the spatial model, raising MarsYardError if it is unusable."""
    return MarsYard(path)
