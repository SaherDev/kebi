"""Area-shaped search scope, shared by the consult-family place tools (Step 6).

Sibling to `_corridor.py`. Where a corridor turn turns a route into points to
search around, this turns the **areas the agent named** into points to search
around — the same trick, a different shape.

This is ADR-140 applied to areas: the agent names the places and areas worth
going to, and kebi searches around what it suggests. For venues that already
held; here it holds for areas, which makes an area an *input* to retrieval
rather than only an output of it. The failure it exists to fix is an answer
that names Hue, Hoi An and the pass in prose and pins nothing at any of them,
because every search was anchored on a disc around the turn's origin.

Two rules keep it bounded and honest:

  - **The agent picks, config caps.** The agent may name as many areas as the
    answer needs — prose is free. Only the first `area_anchor.max_areas` of
    them are searched, because each anchor is a real fan-out. What gets
    dropped is logged, never silently truncated.
  - **An unusable extent degrades to a point, never to a guess.** The
    provider's viewport is not an extent (Google draws a ~0.4 km box around a
    ~20 km pass). `has_usable_extent` decides whether to believe it; when it
    can't be believed the anchor is the centroid with a radius from the area's
    kind. Kebi has no better geometry for a linear feature and does not invent
    one.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, NamedTuple, TypeVar

from kebi.core.agent.state import AgentState
from kebi.core.agent.tools._corridor import oversized_legs
from kebi.core.areas.models import (
    AreaEntity,
    AreaKind,
    extent_span_km,
    has_usable_extent,
)
from kebi.core.places.models import LocationContext
from kebi.core.utils.geo import (
    Point,
    distance_to_path_m,
    enclosing_circle,
    haversine_m,
    path_length_m,
    path_progress,
    sample_path_waypoints,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from kebi.core.config import MovementConfig

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Which radius tier an area of each kind is searched at when its own extent is
# unusable. Reuses the ADR-084 tiers rather than inventing a second set of
# distances: a neighborhood is searched like a neighborhood.
#
# A natural feature takes the city tier deliberately — a pass or a stretch of
# coast is long, and the walkable/neighborhood tiers would search one hairpin
# of it. A country or region takes the widest tier available; anchoring on a
# whole country is coarse by nature, and the agent naming one is asking for
# "somewhere in here", not for a precise disc.
_KIND_RADIUS_TIER: dict[AreaKind, str] = {
    "country": "metro",
    "region": "metro",
    "city": "city",
    "neighborhood": "neighborhood",
    "natural_feature": "city",
    "street": "walkable",
}


# How far a search may reach from an area's centre, per kind, whatever its
# stored extent says. **An administrative boundary is not a search radius.**
# Vietnam's 2025 province merger left `vn/da-nang` with a 160 km bbox — true
# geography, and the right thing to shade on a map, but "somewhere in Da Nang"
# does not mean "within 80 km of the city centre". Uncapped, that one disc
# swallowed an entire Da Nang → Hue ride and filed every stop under the
# origin, which is the failure area-anchoring exists to prevent.
#
# So extent and radius are separated on purpose: the bbox still travels to the
# client as `extent` because it is true, and the radius is bounded here
# because reach is a different question from footprint.
_KIND_MAX_RADIUS_M: dict[AreaKind, float] = {
    "country": 300_000.0,
    "region": 120_000.0,
    "city": 25_000.0,
    "neighborhood": 5_000.0,
    "natural_feature": 20_000.0,
    "street": 3_000.0,
}


def anchor_radius_m(entity: AreaEntity, movement_cfg: MovementConfig) -> float:
    """Search radius for this area, in metres.

    From the area's own extent when that extent is believable — half the
    longest side, so the disc covers the box without spilling far beyond it —
    and from the kind's tier otherwise. Either way it is capped by what a
    search of that kind should reach (`_KIND_MAX_RADIUS_M`).
    """
    tiers = movement_cfg.radius_tiers
    ceiling = _KIND_MAX_RADIUS_M[entity.entity_type]
    fallback = float(getattr(tiers, _KIND_RADIUS_TIER[entity.entity_type]))
    if not has_usable_extent(entity):
        return min(fallback, ceiling)
    span_km = extent_span_km(entity.bbox, entity.lat)
    if span_km is None:
        return min(fallback, ceiling)
    return max(1.0, min(span_km * 1000.0 / 2.0, ceiling))


def anchor_context(entity: AreaEntity, movement_cfg: MovementConfig) -> LocationContext:
    """One search context centred on an area.

    The area's own name rides in the `city` slot because that is the field the
    provider's text-search reranker reads as "which place is this about" — for
    a neighborhood, a pass or a street, the name of the thing is a better
    reranking signal than the city containing it.
    """
    return LocationContext(
        lat=entity.lat,
        lng=entity.lng,
        radius_m=max(1, int(anchor_radius_m(entity, movement_cfg))),
        city=entity.name,
        country=entity.country_code,
    )


def anchors_from_state(
    state: AgentState, area_keys: list[str] | None
) -> list[AreaEntity]:
    """The verified areas this turn is anchored on, in the agent's order.

    Only areas `suggest_areas` already verified this turn can be anchored on.
    A key the agent asks for that is not on state is dropped, not resolved:
    "search inside this area" has to stand on a verified footprint, and
    accepting an arbitrary key here would be a second, unverified door into
    the same retrieval.

    With no `area_keys` the tool anchors on every area the agent put forward,
    which is the common case — the agent named them precisely because they are
    what the answer is about.
    """
    entities: list[AreaEntity] = []
    for raw in state.get("area_anchors") or []:
        try:
            entities.append(AreaEntity.model_validate(raw))
        except Exception:  # noqa: BLE001 - a malformed anchor is skipped, never fatal
            logger.warning("skipping malformed area anchor on state")
    if not area_keys:
        return entities

    wanted = [key.strip() for key in area_keys if key.strip()]
    by_key = {e.entity_key: e for e in entities}
    picked = [by_key[key] for key in wanted if key in by_key]
    missing = [key for key in wanted if key not in by_key]
    if missing:
        logger.info("area_keys not verified this turn, ignored: %s", ", ".join(missing))
    return picked


def anchor_contexts(
    entities: list[AreaEntity], movement_cfg: MovementConfig, *, max_areas: int
) -> list[LocationContext]:
    """One context per area, capped — the billed fan-out for an area turn."""
    return [anchor_context(e, movement_cfg) for e in capped(entities, max_areas)]


def enclosing_anchor_context(
    entities: list[AreaEntity], movement_cfg: MovementConfig
) -> LocationContext:
    """One disc covering every anchored area — a coarse single-call prefilter.

    The area-shaped twin of `_corridor.enclosing_context`, and it exists for
    the same cost reason: `suggest_places` makes one provider call *per
    proposed name*, so anchoring per area would multiply that by the number of
    areas. A single loose disc keeps the call count identical while letting a
    name in any of the named areas validate. Precision is not its job —
    `attribute_to_area` is what makes the result area-shaped.
    """
    points = [(e.lat, e.lng) for e in entities]
    pad = max((anchor_radius_m(e, movement_cfg) for e in entities), default=0.0)
    lat, lng, radius = enclosing_circle(points, pad)
    return LocationContext(
        lat=lat,
        lng=lng,
        radius_m=max(1, int(radius)),
        country=entities[0].country_code if entities else None,
    )


def attribute_to_area(
    entities: list[AreaEntity],
    movement_cfg: MovementConfig,
    lat: float,
    lng: float,
) -> AreaEntity | None:
    """The anchored area a point belongs to — nearest whose radius contains it.

    None means the point is in none of them, which is a *drop*, not an
    unknown: the coarse prefilter disc admits slop by construction, and a
    place that is in no named area is exactly the "clustered around the
    origin" result this step exists to stop returning.
    """
    best: tuple[float, AreaEntity] | None = None
    for entity in entities:
        distance = haversine_m(entity.lat, entity.lng, lat, lng)
        if distance > anchor_radius_m(entity, movement_cfg):
            continue
        if best is None or distance < best[0]:
            best = (distance, entity)
    return best[1] if best is not None else None


# ---------------------------------------------------------------------------
# Journey geometry — the areas as a path, not just a set of discs.
#
# Whether there IS a path is the agent's call, never an inference from
# distance. It knows Hoi An → Hue is a ride people stop along, that An Thuong
# and Son Tra are two ends of one city with nothing between them worth
# pinning, and that Hanoi → Saigon is a flight. No geometry separates those
# three, and guessing produces confident nonsense in the middle case.
#
# Everything below reuses `_corridor.py` and `core/utils/geo.py` unchanged —
# the only new idea is that the polyline's vertices are anchor centroids
# instead of resolver-supplied stops.
# ---------------------------------------------------------------------------


def journey_from_state(state: AgentState) -> bool:
    """Did the agent say people travel between the areas it named this turn."""
    return bool(state.get("area_journey"))


def journey_path(anchors: list[AreaEntity]) -> list[Point]:
    """The anchor centroids as a polyline, in the order the agent named them."""
    return [(a.lat, a.lng) for a in anchors]


def between_key(origin: AreaEntity, destination: AreaEntity) -> str:
    """Group key for the stretch between two named areas."""
    return f"{origin.entity_key}>{destination.entity_key}"


def journey_half_width_m(
    anchors: list[AreaEntity], movement_cfg: MovementConfig
) -> float:
    """How far off the line a place still counts as "on the way", in metres.

    Same rule as a resolver corridor (`_corridor.half_width_m`) and for the
    same reason: the path is a straight chord and the road is not, so the
    tolerance scales with length. On the 84 km Da Nang → Hue drive the coastal
    road runs ~16 km off the chord, which is exactly where Lang Co sits.
    """
    cfg = movement_cfg.corridor
    scaled = path_length_m(journey_path(anchors)) * cfg.half_width_ratio
    return min(max(scaled, cfg.min_half_width_m), cfg.max_half_width_m)


def drivable_legs(
    anchors: list[AreaEntity], movement_cfg: MovementConfig
) -> frozenset[int]:
    """Indices of legs short enough for "what's on the way" to mean anything.

    Per leg, not per trip (ADR-138). A Hanoi → Saigon → Hoi An answer has one
    leg nobody drives and one that is the whole point of going; gating the
    trip as a whole would suppress the stops on the good leg, which is what
    `route_too_long` does today and why it is not reused here.
    """
    path = journey_path(anchors)
    oversized = oversized_legs(path, movement_cfg.corridor)
    return frozenset(i for i in range(max(0, len(path) - 1)) if i not in oversized)


def between_contexts(
    anchors: list[AreaEntity], movement_cfg: MovementConfig
) -> list[tuple[str, LocationContext]]:
    """Search points on the stretches between areas, keyed by which stretch.

    Interior points only: the areas themselves are already searched with their
    own radius, so a sample sitting inside one would just re-search it and
    spend a provider call to do so. Over-long legs contribute nothing —
    nothing is "on the way" across a distance nobody drives.
    """
    path = journey_path(anchors)
    if len(path) < 2:
        return []
    cfg = movement_cfg.corridor
    drivable = drivable_legs(anchors, movement_cfg)
    skip = frozenset(i for i in range(len(path) - 1) if i not in drivable)
    if len(skip) == len(path) - 1:
        return []

    radius = journey_half_width_m(anchors, movement_cfg)
    contexts: list[tuple[str, LocationContext]] = []
    for point in sample_path_waypoints(
        path,
        spacing_m=cfg.waypoint_spacing_m,
        max_count=cfg.max_waypoints,
        min_count=cfg.min_waypoints,
        skip_legs=skip,
    ):
        inside = any(
            haversine_m(a.lat, a.lng, point[0], point[1])
            <= anchor_radius_m(a, movement_cfg)
            for a in anchors
        )
        if inside:
            continue
        leg = _leg_index(path, point)
        if leg is None or leg not in drivable:
            continue
        contexts.append(
            (
                between_key(anchors[leg], anchors[leg + 1]),
                LocationContext(
                    lat=point[0],
                    lng=point[1],
                    radius_m=max(1, int(radius)),
                    country=anchors[0].country_code,
                ),
            )
        )
    return contexts


def _leg_index(path: list[Point], point: Point) -> int | None:
    """Which leg of the path a sampled point falls on, by nearest segment."""
    best: tuple[float, int] | None = None
    for i in range(len(path) - 1):
        distance = distance_to_path_m([path[i], path[i + 1]], point[0], point[1])
        if best is None or distance < best[0]:
            best = (distance, i)
    return best[1] if best is not None else None


class Placement(NamedTuple):
    """Where a result sits on a journey: a named area, a stretch, or nowhere."""

    on_journey: bool
    group_key: str | None
    area: AreaEntity | None
    progress: float


def place_on_journey(
    anchors: list[AreaEntity],
    movement_cfg: MovementConfig,
    lat: float,
    lng: float,
) -> Placement:
    """Locate a result against the journey — the journey-aware attribution.

    Three outcomes, and the third is the point: inside a named area it belongs
    to that area; off to the side of the line but within the half-width it is
    *on the way* and belongs between two areas; anywhere else it is dropped.
    Without that middle case a journey answer can only ever say "at Hoi An" or
    "at Hue" and never "stop here on the road", which is most of what makes a
    drive worth describing.
    """
    path = journey_path(anchors)
    progress = path_progress(path, lat, lng) if len(path) > 1 else 0.0

    # A named area wins over the road when the point is inside it. That
    # favours the endpoints, and on a bending route it favours them more than
    # is ideal — Hai Van Pass sits 18 km from Da Nang's centre and files under
    # Da Nang rather than under the ride.
    #
    # Tried and rejected: attributing by a *tight* radius (the kind's scope
    # tier) so the road keeps mid-route stops. It broke worse — post-merger
    # Vietnamese city rows carry PROVINCE centroids, so Hue's own Imperial
    # City is 16 km from the stored "Hue" point and a 7 km tier filed the
    # city's own landmarks as "on the way". Neither signal is trustworthy on
    # a route that bends: the chord misplaces progress and the centroid is
    # not the city. Between two imperfect answers this is the honest one,
    # because a stop filed under a real containing area is still true.
    area = attribute_to_area(anchors, movement_cfg, lat, lng)
    if area is not None:
        return Placement(True, area.entity_key, area, progress)

    if len(path) < 2:
        return Placement(False, None, None, progress)
    if distance_to_path_m(path, lat, lng) > journey_half_width_m(anchors, movement_cfg):
        return Placement(False, None, None, progress)

    leg = _leg_index(path, (lat, lng))
    if leg is None or leg not in drivable_legs(anchors, movement_cfg):
        return Placement(False, None, None, progress)
    return Placement(True, between_key(anchors[leg], anchors[leg + 1]), None, progress)


class SearchTarget(NamedTuple):
    """One place to search, and what part of the answer it belongs to.

    `area` is the named area when the target IS one, and None for a point on
    the road between two — which is why `group_key` exists separately: an
    on-the-way stop belongs to a stretch, not to either end of it.
    """

    group_key: str
    area: AreaEntity | None
    context: LocationContext


def search_targets(
    anchors: list[AreaEntity],
    movement_cfg: MovementConfig,
    *,
    journey: bool,
    max_areas: int,
) -> list[SearchTarget]:
    """Everywhere this turn searches: each named area, plus the road between.

    On a journey the between-stretches are interleaved in travel order, so the
    fan-out reads Hoi An → the road → Hue rather than the areas first and the
    road as an afterthought. When the agent did not flag travel, there is no
    road: the areas are independent and the answer is a set of places, not a
    drive.
    """
    picked = capped(anchors, max_areas)
    area_targets = [
        SearchTarget(a.entity_key, a, anchor_context(a, movement_cfg)) for a in picked
    ]
    if not journey or len(picked) < 2:
        return area_targets

    between = between_contexts(picked, movement_cfg)
    by_leg: dict[str, list[SearchTarget]] = {}
    for key, context in between:
        by_leg.setdefault(key, []).append(SearchTarget(key, None, context))

    interleaved: list[SearchTarget] = []
    for i, target in enumerate(area_targets):
        interleaved.append(target)
        if i + 1 < len(picked):
            interleaved.extend(by_leg.get(between_key(picked[i], picked[i + 1]), []))
    return interleaved


async def gather_targets(
    targets: list[SearchTarget],
    *,
    concurrency: int,
    runner: Callable[[SearchTarget], Awaitable[list[T]]],
    dedup_key: Callable[[T], str],
) -> list[tuple[SearchTarget, T]]:
    """Run one search per target concurrently; return results tagged by target.

    Where each result came from is carried out with it, because that is what
    the answer needs — "in Hoi An: …, on the way: …, in Hue: …" is a better
    answer than the same places in one undifferentiated list, and it is only
    knowable here.

    Dedup keeps the *first* target a place appeared under. Targets arrive in
    travel order, so a place near the edge of Hoi An that also falls on the
    road out of it is attributed to Hoi An — the earlier, more specific
    container — rather than appearing twice.

    A partial failure is survivable and deliberately survived: an answer
    missing one area still beats no answer. Only a total failure raises, which
    the caller reports as a provider error.
    """
    if not targets:
        return []
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _run(target: SearchTarget) -> list[T]:
        async with sem:
            return await runner(target)

    outcomes = await asyncio.gather(*(_run(t) for t in targets), return_exceptions=True)

    tagged: list[tuple[SearchTarget, T]] = []
    seen: set[str] = set()
    failures: list[BaseException] = []
    for target, outcome in zip(targets, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            logger.warning("search failed for %s: %s", target.group_key, outcome)
            failures.append(outcome)
            continue
        for item in outcome:
            key = dedup_key(item)
            if key in seen:
                continue
            seen.add(key)
            tagged.append((target, item))

    if failures and len(failures) == len(outcomes):
        raise failures[0]
    return tagged


def capped(entities: list[AreaEntity], max_areas: int) -> list[AreaEntity]:
    """The areas that get searched, in the order the agent named them.

    Order is the agent's, not a score's: it named them in the order it thinks
    they matter, and reordering here would quietly overrule that judgement.
    """
    limit = max(1, max_areas)
    if len(entities) <= limit:
        return entities
    dropped = [e.name for e in entities[limit:]]
    logger.info(
        "area anchors capped at %d; not searched: %s", limit, ", ".join(dropped)
    )
    return entities[:limit]
