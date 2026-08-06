"""Search-scope helpers shared by the consult-family tools.

Two deterministic adjustments the location resolver cannot make, because both
depend on things only known at tool-call time:

`clamp_to_walkable_for_utility` tightens a turn's search radius for practical
"near me" errands (ATM, pharmacy, supermarket, …). The location resolver runs
before the agent picks a tool, so it cannot know the intent is a utility errand
and may classify it at city/metro scope — yielding a search radius of several
kilometres. For an errand you walk to, that lets a prominent branch across town
out-rank the closest one. The tools, by contrast, know the category at call
time, so the clamp lives here and is deterministic.

`anchor_to_corridor` makes retrieval work *along the way*. The resolver
classifies a route turn as `scope_shape="corridor"` and geocodes where the
user is headed, but every downstream search is still a point plus a radius —
so without this the search circles the origin and an answer about the trip
only ever knows about places next to where the trip starts.

`itinerary_segments` is the multi-stop generalisation (ADR-148): an
itinerary turn fans one search out into a disc per stop plus a corridor
circle per leg, so the user's saves, kebi's claims, and taste picks can
surface for every part of the trip — including a city on the route the user
never named.
"""

from __future__ import annotations

import logging
from typing import NamedTuple

from kebi.core.agent.location import WorkingLocation, resolve_radius
from kebi.core.config import MovementConfig
from kebi.core.places.models import UTILITY_CATEGORIES, PlaceCategory
from kebi.core.utils.geo import haversine_m

logger = logging.getLogger(__name__)


def anchor_to_corridor(working: WorkingLocation) -> WorkingLocation:
    """Re-anchor a corridor turn's search onto the whole route.

    The circle is moved to the midpoint of origin → destination and widened to
    the smallest radius that still covers both ends plus the band the user
    would deviate off-route (their already-resolved `search_radius_m`). That
    is the minimal enclosing circle of the route with its band, so nothing is
    invented and nothing arbitrary is capped: a 4 km hop across town stays a
    small search, a 60 km drive legitimately spans the drive.

    A circle is a coarse stand-in for a real corridor — it admits places
    beside the midpoint that aren't on the road — but it is strictly better
    than the origin-only circle it replaces, and the agent still ranks by
    what's actually on the way. Returns `working` unchanged for any turn that
    isn't a corridor or whose destination never geocoded.
    """
    corridor = working.corridor
    if working.scope_shape != "corridor" or corridor is None:
        return working
    logger.info(
        "anchoring search on the route to %s (was %.0fm around origin)",
        corridor.name,
        working.search_radius_m,
    )
    leg_m = haversine_m(working.lat, working.lng, corridor.lat, corridor.lng)
    return working.model_copy(
        update={
            "lat": (working.lat + corridor.lat) / 2,
            "lng": (working.lng + corridor.lng) / 2,
            "search_radius_m": leg_m / 2 + working.search_radius_m,
        }
    )


class ItinerarySegment(NamedTuple):
    """One searchable piece of a multi-stop trip.

    `label` is what the candidate carries back to the agent ("Hue", or
    "on the way between Hue and Hoi An") — user-facing words, since the
    agent quotes them when pinning a pick to a part of the trip.
    `on_the_way` marks a leg: a hit here that isn't in any stop's own disc
    is the "add Da Nang" signal — somewhere the user never named, on their
    route, with a reason to stop.
    """

    label: str
    on_the_way: bool
    working: WorkingLocation


def itinerary_segments(working: WorkingLocation) -> list[ItinerarySegment]:
    """Fan an itinerary turn out into per-stop discs and per-leg corridors.

    Stops come first (in trip order), then legs between consecutive stops —
    so a fan-out that dedupes in order attributes a place to its city, and
    only a hit *outside* every stop's disc stays labelled as on the way.

    Each stop keeps the turn's resolved `search_radius_m` (the resolver
    classifies an itinerary at city tier, so that radius is the city disc).
    Each leg reuses the corridor math above: midpoint of the two stops,
    widened to cover both ends plus the stop radius as the off-route band.
    Same coarse-circle trade-off as `anchor_to_corridor`, same defence: the
    agent still ranks by what is actually on the way.

    Returns `[]` for any turn that isn't an itinerary or that resolved
    fewer than two anchors — callers fall back to the single-search path.
    """
    anchors = working.itinerary or []
    if working.scope_shape != "itinerary" or len(anchors) < 2:
        return []

    base = working.model_copy(
        update={
            "scope_shape": "area",
            "corridor": None,
            "itinerary": None,
            "neighborhood": None,
            "neighborhood_icon": None,
        }
    )
    # Each stop disc carries the stop's OWN area names + country code from
    # its geocode — anything name-keyed downstream (area claims via
    # `build_geo_key`) must resolve to Hue's knowledge for the Hue stop, not
    # the trip's first city. A leg has no area of its own, so it keeps the
    # primary names only for display and nothing name-keyed runs against it.
    segments = [
        ItinerarySegment(
            label=anchor.name,
            on_the_way=False,
            working=base.model_copy(
                update={
                    "lat": anchor.lat,
                    "lng": anchor.lng,
                    "city": anchor.city or anchor.name,
                    "country": anchor.country or base.country,
                    "country_code": anchor.country_code,
                }
            ),
        )
        for anchor in anchors
    ]
    for a, b in zip(anchors, anchors[1:], strict=False):
        leg_m = haversine_m(a.lat, a.lng, b.lat, b.lng)
        segments.append(
            ItinerarySegment(
                label=f"on the way between {a.name} and {b.name}",
                on_the_way=True,
                working=base.model_copy(
                    update={
                        "lat": (a.lat + b.lat) / 2,
                        "lng": (a.lng + b.lng) / 2,
                        "search_radius_m": leg_m / 2 + working.search_radius_m,
                    }
                ),
            )
        )
    return segments


def clamp_to_walkable_for_utility(
    working: WorkingLocation,
    categories: list[PlaceCategory] | None,
    movement_cfg: MovementConfig,
) -> WorkingLocation:
    """Tighten the search radius to walkable scope for a utility errand.

    Returns `working` unchanged unless `categories` intersects
    `UTILITY_CATEGORIES`. When it does, the radius is recomputed at the
    `walkable` tier — keeping the turn's effective mode and location density,
    with no reach shift — by reusing the same `resolve_radius` formula the
    resolver uses, so there is no second source of truth and no hardcoded
    metres. The clamp only ever tightens (`min`); a turn already resolved to a
    smaller radius is left alone.

    A corridor turn is exempt: "a pharmacy on the way to the airport" is an
    errand, but clamping it to walking distance around the route's midpoint
    would search a random patch of road. On a route, the route wins.
    """
    if working.scope_shape == "corridor":
        return working
    if not categories or UTILITY_CATEGORIES.isdisjoint(categories):
        return working

    walkable_radius = resolve_radius(
        working.effective_mode,
        "walkable",
        "normal",
        working.density,
        movement_cfg,
    )
    new_radius = min(working.search_radius_m, walkable_radius)
    return working.model_copy(
        update={"search_radius_m": new_radius, "scope_tier": "walkable"}
    )
