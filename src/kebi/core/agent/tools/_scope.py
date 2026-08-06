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
"""

from __future__ import annotations

import logging

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
