"""The catalog floor: real nearby places when nothing better was found (ADR-140).

This is what `discover_places` used to be as a separate tool. Its whole job was
to stop an answer falling back to a fabricated tip ("head to <area> and you'll
find some") when the namer produced nothing usable — a safety property, and
one the model had to remember to invoke through a long conditional block in the
prompt. A rule the model must remember is a rule it can forget, so the
fall-through now runs automatically inside `suggest_places` instead.

Nothing about the search changed: same provider query, same walkable clamp for
errands, same hard-constraint filter. Only the trigger moved, from a routing
decision to a deterministic consequence of finding nothing.
"""

from __future__ import annotations

import logging

from kebi.core.agent.location import WorkingLocation
from kebi.core.agent.tools._hard_constraints import (
    hard_constraints_satisfied,
    split_constraints,
)
from kebi.core.agent.tools.consult_models import ConsultCandidate
from kebi.core.extraction.extraction_pipeline import SearchServiceFactory
from kebi.core.places.models import LocationContext, PlaceCategory, PlaceQuery

logger = logging.getLogger(__name__)


def _location_context(working: WorkingLocation) -> LocationContext:
    return LocationContext(
        lat=working.lat,
        lng=working.lng,
        radius_m=int(working.search_radius_m),
        neighborhood=working.neighborhood,
        city=working.city,
        country=working.country,
    )


async def catalog_candidates(
    *,
    places_search_factory: SearchServiceFactory,
    working: WorkingLocation,
    query: str,
    categories: list[PlaceCategory] | None,
    tags: list[str] | None,
    limit: int,
) -> list[ConsultCandidate]:
    """Whatever the catalog actually has nearby, as `source="discovered"`.

    Best-effort: a provider failure returns an empty list rather than raising,
    because this runs *after* the caller has already failed to find anything —
    turning that into an error would replace a thin answer with no answer.
    """
    if working.search_radius_m <= 0:
        return []
    place_query = PlaceQuery(
        place_names=[query] if query else None,
        categories=categories,
        tags=tags,
        location=_location_context(working),
    )
    try:
        async with places_search_factory() as svc:
            hits = await svc.find(place_query, limit=limit)
    except Exception as exc:
        logger.warning("catalog fallback lookup failed: %s", exc, exc_info=True)
        return []

    hard, _soft = split_constraints(tags or [])
    kept = [p for p in hits if hard_constraints_satisfied(p, hard)]
    return [
        ConsultCandidate(
            place=p.to_core() if hasattr(p, "to_core") else p,
            user_data=None,
            source="discovered",
            rrf_score=0.0,
        )
        for p in kept[:limit]
    ]
