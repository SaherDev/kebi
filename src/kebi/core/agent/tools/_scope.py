"""Search-scope helpers shared by the consult-family tools.

`clamp_to_walkable_for_utility` tightens a turn's search radius for practical
"near me" errands (ATM, pharmacy, supermarket, …). The location resolver runs
before the agent picks a tool, so it cannot know the intent is a utility errand
and may classify it at city/metro scope — yielding a search radius of several
kilometres. For an errand you walk to, that lets a prominent branch across town
out-rank the closest one. The tools, by contrast, know the category at call
time, so the clamp lives here and is deterministic.
"""

from __future__ import annotations

from kebi.core.agent.location import WorkingLocation, resolve_radius
from kebi.core.config import MovementConfig
from kebi.core.places.models import UTILITY_CATEGORIES, PlaceCategory


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
    """
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
