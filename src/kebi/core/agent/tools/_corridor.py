"""Route-shaped search scope, shared by the consult-family place tools (ADR-136).

A corridor turn resolves to an ordered route — origin plus the stops the user
named — and this module turns that route into the things a place search needs:

  - `waypoint_contexts` — the points to search around, in path order. Used by
    `discover_places`, which queries the provider directly per point.
  - `enclosing_context` — one coarse disc covering the whole route. Used by
    `suggest_places`, whose provider calls are one-per-proposed-name: the
    disc keeps that call count unchanged while letting a name anywhere along
    the route validate. Precision is not its job; `filter_and_order` is.
  - `filter_and_order` — drop what isn't near the route, order what's left
    origin → final destination. This is what makes an answer a *journey*
    rather than a bag of places.

**The length gate.** Venue stops are an honest answer up to roughly a long
day's drive. Across 600 km nothing is meaningfully "on the way" — the real
stops are cities, which consult cannot return until the roadmap's Step 6. So
an over-length leg contributes no interior sampling, and a route whose every
leg is over-length returns `route_too_long`: kebi says the trip is city-scale
and asks which stretch, instead of inventing five unrelated venues.

The geometry itself is deliberately scale-agnostic and lives in
`core/utils/geo.py`. Step 6 changes only *what* is searched at each waypoint,
never how the route is sampled, filtered, or ordered.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from kebi.core.agent.location import WorkingLocation
from kebi.core.places.models import LocationContext
from kebi.core.utils.geo import (
    Point,
    distance_to_path_m,
    enclosing_circle,
    leg_lengths_m,
    path_length_m,
    path_progress,
    sample_path_waypoints,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from kebi.core.config import CorridorConfig, MovementConfig

T = TypeVar("T")


def is_corridor(working: WorkingLocation | None) -> bool:
    """True when this turn is route-shaped and its route actually resolved."""
    return (
        working is not None
        and working.scope_shape == "corridor"
        and working.corridor is not None
        and len(working.corridor.stops) > 0
    )


def route_points(working: WorkingLocation) -> list[Point]:
    """The turn's route as a polyline, origin first.

    Caller has already checked `is_corridor`.
    """
    assert working.corridor is not None  # narrowed by is_corridor
    return working.corridor.points(working.lat, working.lng)


def half_width_m(working: WorkingLocation, movement_cfg: MovementConfig) -> float:
    """How far off the route a place still counts as "on the way", in metres.

    Scales with route length, because the route is a straight chord and the
    road is not: the longer the route, the further the real road bows away
    from the line. On the 84 km Da Nang → Hue drive the coastal road runs
    ~16 km off the chord, which is exactly where Lang Co — the stop everyone
    makes — sits. A flat tolerance drops it. The floor keeps a short urban
    corridor from collapsing to a hairline, the ceiling keeps a long one from
    becoming a country-wide search.

    The turn's own search radius still wins when it is *tighter*: the utility
    clamp (`_scope.py`) narrows the radius for an errand, and "an ATM on the
    way" should stay close to the road even on a long drive.
    """
    cfg = movement_cfg.corridor
    scaled = path_length_m(route_points(working)) * cfg.half_width_ratio
    bounded = min(max(scaled, cfg.min_half_width_m), cfg.max_half_width_m)
    return min(working.search_radius_m, bounded)


def oversized_legs(path: list[Point], cfg: CorridorConfig) -> frozenset[int]:
    """Indices of legs too long for venue stops to be a real answer."""
    return frozenset(
        i
        for i, length in enumerate(leg_lengths_m(path))
        if length > cfg.max_venue_route_m
    )


def is_route_too_long(working: WorkingLocation, movement_cfg: MovementConfig) -> bool:
    """True when no leg is short enough for venue stops to mean anything.

    Checked *before* any LLM or provider call: an all-oversized route spends
    nothing and answers honestly.
    """
    path = route_points(working)
    return len(oversized_legs(path, movement_cfg.corridor)) == len(path) - 1


def _context_at(
    point: Point, working: WorkingLocation, radius_m: float
) -> LocationContext:
    """A search context at `point`, carrying the turn's named geography.

    City/country/neighborhood ride along because the provider's text-search
    reranker reads them — they name the region, while lat/lng/radius bound it.
    """
    return LocationContext(
        lat=point[0],
        lng=point[1],
        radius_m=max(1, int(radius_m)),
        city=working.city,
        country=working.country,
        neighborhood=working.neighborhood,
    )


def waypoint_contexts(
    working: WorkingLocation, movement_cfg: MovementConfig
) -> list[LocationContext]:
    """Points to search around, in path order — one provider call each.

    Every stop the user named is always included; interior points fill the
    remaining budget, weighted to the longer legs and skipping over-length
    ones. This is the billed fan-out, capped by `corridor.max_waypoints`.
    """
    cfg = movement_cfg.corridor
    path = route_points(working)
    waypoints = sample_path_waypoints(
        path,
        spacing_m=cfg.waypoint_spacing_m,
        max_count=cfg.max_waypoints,
        min_count=cfg.min_waypoints,
        skip_legs=oversized_legs(path, cfg),
    )
    radius = half_width_m(working, movement_cfg)
    return [_context_at(p, working, radius) for p in waypoints]


def enclosing_context(
    working: WorkingLocation, movement_cfg: MovementConfig
) -> LocationContext:
    """One disc covering the whole route — a coarse single-call prefilter.

    Deliberately loose: `PlaceQuery(sort_by="distance")` turns this into a
    hard bounding rectangle at the provider, which lets a name anywhere along
    the route validate in ONE call instead of one per waypoint. The rectangle
    admits off-route slop by construction; `filter_and_order` removes it.
    """
    lat, lng, radius = enclosing_circle(
        route_points(working), half_width_m(working, movement_cfg)
    )
    return _context_at((lat, lng), working, radius)


def filter_and_order(
    items: list[T],
    working: WorkingLocation,
    movement_cfg: MovementConfig,
    *,
    coords: Callable[[T], tuple[float, float] | None],
) -> list[T]:
    """Keep what's near the route, ordered origin → final destination.

    Items whose coordinates are unknown (`coords` returns `None`) cannot be
    placed on the route at all: they are neither dropped as off-route nor
    given a false position, and land after the ordered ones. Everything else
    farther than the half-width from every leg is dropped — that is the whole
    point of the step, and it is why a route turn can now honestly return
    nothing where it used to return places clustered at the origin.
    """
    path = route_points(working)
    limit = half_width_m(working, movement_cfg)

    on_route: list[tuple[float, T]] = []
    unplaceable: list[T] = []
    for item in items:
        point = coords(item)
        if point is None:
            unplaceable.append(item)
            continue
        if distance_to_path_m(path, point[0], point[1]) > limit:
            continue
        on_route.append((path_progress(path, point[0], point[1]), item))

    on_route.sort(key=lambda pair: pair[0])
    return [item for _, item in on_route] + unplaceable


def place_coords(place: object) -> tuple[float, float] | None:
    """Coordinates of a `PlaceCore` / `PlaceObject`, or `None` when unknown.

    `PlaceCore.location` is nullable (Google-derived coordinates are wiped on
    the 30-day ToS cycle), so a row can legitimately reach here with no point.
    Shared by both place tools so "unknown position" means the same thing on
    each path.
    """
    location = getattr(place, "location", None)
    lat = getattr(location, "lat", None)
    lng = getattr(location, "lng", None)
    if lat is None or lng is None:
        return None
    return (float(lat), float(lng))


def route_summary(working: WorkingLocation) -> str:
    """Human phrasing of the route — "Da Nang → Hue → Hoi An, about 130 km".

    Shared by the namer prompt block and the agent's movement context so the
    two never drift into describing the same route differently.
    """
    assert working.corridor is not None  # narrowed by is_corridor
    names = [working.city, *(stop.name for stop in working.corridor.stops)]
    km = path_length_m(route_points(working)) / 1000.0
    return f"{' → '.join(names)}, about {km:.0f} km"
