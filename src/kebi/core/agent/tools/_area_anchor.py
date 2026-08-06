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
from typing import TYPE_CHECKING, TypeVar

from kebi.core.agent.state import AgentState
from kebi.core.areas.models import (
    AreaEntity,
    AreaKind,
    extent_span_km,
    has_usable_extent,
)
from kebi.core.places.models import LocationContext
from kebi.core.utils.geo import enclosing_circle, haversine_m

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


def anchor_radius_m(entity: AreaEntity, movement_cfg: MovementConfig) -> float:
    """Search radius for this area, in metres.

    From the area's own extent when that extent is believable — half the
    longest side, so the disc covers the box without spilling far beyond it —
    and from the kind's tier otherwise.
    """
    tiers = movement_cfg.radius_tiers
    fallback = float(getattr(tiers, _KIND_RADIUS_TIER[entity.entity_type]))
    if not has_usable_extent(entity):
        return fallback
    span_km = extent_span_km(entity.bbox, entity.lat)
    if span_km is None:
        return fallback
    return max(1.0, span_km * 1000.0 / 2.0)


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


async def gather_per_area(
    anchors: list[AreaEntity],
    movement_cfg: MovementConfig,
    *,
    max_areas: int,
    concurrency: int,
    runner: Callable[[AreaEntity, LocationContext], Awaitable[list[T]]],
    dedup_key: Callable[[T], str],
) -> list[tuple[AreaEntity, T]]:
    """Run one search per area concurrently; return results tagged by area.

    The area each result came from is carried out with it, because that is
    what the answer needs — "in Hoi An: …, in Hue: …" is a better answer than
    the same places in one undifferentiated list, and it is only knowable
    here.

    Dedup keeps the *first* area a place appeared under, so a venue between
    two named areas is attributed to the one the agent named first rather
    than appearing twice.

    A partial failure is survivable and deliberately survived: an answer
    missing one area still beats no answer. Only a total failure raises, which
    the caller reports as a provider error.
    """
    picked = capped(anchors, max_areas)
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _run(entity: AreaEntity) -> list[T]:
        async with sem:
            return await runner(entity, anchor_context(entity, movement_cfg))

    outcomes = await asyncio.gather(
        *(_run(entity) for entity in picked), return_exceptions=True
    )

    tagged: list[tuple[AreaEntity, T]] = []
    seen: set[str] = set()
    failures: list[BaseException] = []
    for entity, outcome in zip(picked, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            logger.warning("area search failed for %s: %s", entity.name, outcome)
            failures.append(outcome)
            continue
        for item in outcome:
            key = dedup_key(item)
            if key in seen:
                continue
            seen.add(key)
            tagged.append((entity, item))

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
