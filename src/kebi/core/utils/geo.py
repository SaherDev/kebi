"""Geographic utilities — distance calculations and location operations.

Two families live here. The point helpers (`haversine_m`, `bounding_box`)
back the disc-shaped searches every ordinary turn runs. The **path** helpers
below back route-shaped turns (ADR-136): a route is an ordered polyline
`[(lat, lng), ...]` running origin → … → final destination, and the search
needs three things from it — where to sample, how far off-route a hit is, and
how far along the route it sits.

The path helpers reuse the same equirectangular degree-scaling approximation
`bounding_box` already relies on (a degree of latitude is ~111.32 km; a degree
of longitude shrinks by cos(latitude)). Deliberately one projection for the
module, not two: at corridor scale the error is far below the search
half-width, and a second projection would be a second source of truth.
"""

import math

# Metres per degree of latitude (WGS-84 mean). Longitude scales by cos(lat).
_M_PER_DEG_LAT = 111_320.0

# A path point, `(lat, lng)`. Paths are ordered origin-first.
Point = tuple[float, float]


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """
    Calculate the great-circle distance between two points on Earth.

    Uses the Haversine formula. Returns distance in metres.

    Args:
        lat1: Latitude of first point (degrees)
        lng1: Longitude of first point (degrees)
        lat2: Latitude of second point (degrees)
        lng2: Longitude of second point (degrees)

    Returns:
        Distance in metres

    """
    # Earth's radius in metres
    R = 6_371_000

    # Convert to radians
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lng2 - lng1)

    # Haversine formula
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    c = 2 * math.asin(math.sqrt(a))

    return R * c


def bounding_box(
    lat: float, lng: float, radius_m: float
) -> tuple[float, float, float, float]:
    """Axis-aligned box that circumscribes the circle of `radius_m` at (lat, lng).

    Returns `(low_lat, low_lng, high_lat, high_lng)`. A degree of latitude is
    ~111.32 km everywhere; a degree of longitude shrinks by cos(latitude). The
    box's corners reach ~1.41× the radius, so it fully contains the circle —
    used as a *hard* `locationRestriction.rectangle` for the place provider,
    which (for text search) accepts a rectangle but not a circle.

    `cos(lat)` is floored to a small positive value so the longitude span stays
    finite at the poles rather than dividing by zero.
    """
    dlat = radius_m / _M_PER_DEG_LAT
    cos_lat = max(math.cos(math.radians(lat)), 0.01)
    dlng = radius_m / (_M_PER_DEG_LAT * cos_lat)
    return (lat - dlat, lng - dlng, lat + dlat, lng + dlng)


# --- Path geometry (ADR-136) -----------------------------------------------


def _cos_ref(path: list[Point]) -> float:
    """Longitude scale factor for the path, from its mean latitude.

    One reference latitude for the whole path keeps the local projection
    self-consistent, so a point's off-route distance and its progress are
    measured in the same plane. Floored like `bounding_box` so the scale
    stays finite near the poles.
    """
    if not path:
        return 1.0
    mean_lat = sum(p[0] for p in path) / len(path)
    return max(math.cos(math.radians(mean_lat)), 0.01)


def _xy(point: Point, cos_ref: float) -> tuple[float, float]:
    """Project `(lat, lng)` to local metres `(x, y)` at the path's scale."""
    lat, lng = point
    return (lng * _M_PER_DEG_LAT * cos_ref, lat * _M_PER_DEG_LAT)


def _project_on_segment(
    a: Point, b: Point, p: Point, cos_ref: float
) -> tuple[float, float]:
    """Project `p` onto segment `a→b`. Returns `(t, distance_m)`.

    `t` is the clamped position along the segment (`0.0` at `a`, `1.0` at
    `b`), so a point beyond either end projects onto that end rather than
    off the segment. A zero-length segment yields `t = 0.0` and the plain
    point distance — never a division by zero.
    """
    ax, ay = _xy(a, cos_ref)
    bx, by = _xy(b, cos_ref)
    px, py = _xy(p, cos_ref)
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    t = (
        0.0
        if denom <= 0.0
        else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
    )
    cx, cy = ax + t * dx, ay + t * dy
    return t, math.hypot(px - cx, py - cy)


def leg_lengths_m(path: list[Point]) -> list[float]:
    """Great-circle length of each consecutive leg of `path`, in metres."""
    return [
        haversine_m(a[0], a[1], b[0], b[1])
        for a, b in zip(path, path[1:], strict=False)
    ]


def path_length_m(path: list[Point]) -> float:
    """Total length of the polyline, in metres. Zero for a 0/1-point path."""
    return sum(leg_lengths_m(path))


def distance_to_path_m(path: list[Point], lat: float, lng: float) -> float:
    """Shortest distance from `(lat, lng)` to the polyline, in metres.

    This is the off-route test: a hit farther than the corridor half-width
    from every leg is not "on the way", however close it sits to an endpoint.
    A single-point path degrades to the plain point distance.
    """
    if not path:
        return math.inf
    if len(path) == 1:
        return haversine_m(path[0][0], path[0][1], lat, lng)
    cos_ref = _cos_ref(path)
    return min(
        _project_on_segment(a, b, (lat, lng), cos_ref)[1]
        for a, b in zip(path, path[1:], strict=False)
    )


def path_progress(path: list[Point], lat: float, lng: float) -> float:
    """How far along the polyline `(lat, lng)` sits, normalised `0.0..1.0`.

    Measured as cumulative distance to the projection on the *nearest* leg,
    over the total path length — so ordering by this value runs a result set
    origin → final destination, across legs. A degenerate path (no length)
    returns `0.0`: everything ties, and the caller's sort stays stable.
    """
    lengths = leg_lengths_m(path)
    total = sum(lengths)
    if total <= 0.0:
        return 0.0
    cos_ref = _cos_ref(path)
    best_distance = math.inf
    best_travelled = 0.0
    travelled = 0.0
    for i, (a, b) in enumerate(zip(path, path[1:], strict=False)):
        t, distance = _project_on_segment(a, b, (lat, lng), cos_ref)
        if distance < best_distance:
            best_distance = distance
            best_travelled = travelled + t * lengths[i]
        travelled += lengths[i]
    return best_travelled / total


def interpolate_point(a: Point, b: Point, t: float) -> Point:
    """The point `t` of the way from `a` to `b` (linear in degrees)."""
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def enclosing_circle(path: list[Point], pad_m: float) -> tuple[float, float, float]:
    """Smallest bounding circle-ish `(lat, lng, radius_m)` covering `path` + pad.

    Centre is the bounding-box centre and the radius reaches the farthest path
    point plus `pad_m`, so the circle contains the whole route and its
    half-width. Used as a single coarse prefilter for the place provider —
    precision comes from `distance_to_path_m` afterwards, not from this.
    """
    if not path:
        return (0.0, 0.0, max(pad_m, 0.0))
    lats = [p[0] for p in path]
    lngs = [p[1] for p in path]
    centre_lat = (min(lats) + max(lats)) / 2.0
    centre_lng = (min(lngs) + max(lngs)) / 2.0
    reach = max(haversine_m(centre_lat, centre_lng, p[0], p[1]) for p in path)
    return (centre_lat, centre_lng, reach + max(pad_m, 0.0))


def sample_path_waypoints(
    path: list[Point],
    *,
    spacing_m: float,
    max_count: int,
    min_count: int = 2,
    skip_legs: frozenset[int] = frozenset(),
) -> list[Point]:
    """Points to search around, in path order.

    Every point of `path` is always sampled — those are the stops the user
    named, and dropping one to fit a budget would answer a question they
    didn't ask. `max_count` therefore caps the *interior* sampling: when the
    named stops already meet it, the stops are the whole answer.

    Interior points are allocated **proportional to leg length**, so a long
    leg gets more of the budget than a short one, and legs listed in
    `skip_legs` get none (the length gate — nothing is meaningfully "on the
    way" across a leg that spans a country). Spacing is a target, not a
    guarantee: `spacing_m` sets how many points a leg would like, the budget
    decides how many it gets.
    """
    if len(path) < 2:
        return list(path)

    lengths = leg_lengths_m(path)
    budget = max(0, max(max_count, min_count) - len(path))
    eligible = [
        i for i, length in enumerate(lengths) if i not in skip_legs and length > 0.0
    ]
    wanted = {
        i: max(0, math.ceil(lengths[i] / spacing_m) - 1) if spacing_m > 0 else 0
        for i in eligible
    }
    allocation = _allocate_interior(wanted, lengths, budget)

    waypoints: list[Point] = []
    for i, (a, b) in enumerate(zip(path, path[1:], strict=False)):
        waypoints.append(a)
        count = allocation.get(i, 0)
        for k in range(1, count + 1):
            waypoints.append(interpolate_point(a, b, k / (count + 1)))
    waypoints.append(path[-1])
    return waypoints


def _allocate_interior(
    wanted: dict[int, int], lengths: list[float], budget: int
) -> dict[int, int]:
    """Fit `wanted` interior points per leg into `budget`, longest legs first.

    Under budget, every leg gets what it asked for. Over budget, the share is
    proportional to leg length with the remainder going to the longest legs —
    so squeezing a multi-leg route thins the short hops before the long haul.
    """
    total_wanted = sum(wanted.values())
    if budget <= 0:
        return {}
    if total_wanted <= budget:
        return dict(wanted)

    eligible_length = sum(lengths[i] for i in wanted)
    if eligible_length <= 0.0:
        return {}

    exact = {i: budget * lengths[i] / eligible_length for i in wanted}
    allocation = {i: min(wanted[i], int(value)) for i, value in exact.items()}
    remaining = budget - sum(allocation.values())
    # Largest fractional remainder first, ties broken by the longer leg.
    for i in sorted(
        exact, key=lambda i: (exact[i] - int(exact[i]), lengths[i]), reverse=True
    ):
        if remaining <= 0:
            break
        if allocation[i] < wanted[i]:
            allocation[i] += 1
            remaining -= 1
    return allocation
