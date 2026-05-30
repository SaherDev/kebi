"""Tests for `clamp_to_walkable_for_utility` — utility-errand radius clamp."""

from __future__ import annotations

from kebi.core.agent.location import WorkingLocation
from kebi.core.agent.tools._scope import clamp_to_walkable_for_utility
from kebi.core.config import get_config
from kebi.core.places.models import PlaceCategory


def _city_working(radius_m: float = 9800.0) -> WorkingLocation:
    """Tokyo at city scope — transit, dense — the case that surfaced the bug."""
    return WorkingLocation(
        country="Japan",
        city="Tokyo",
        lat=35.6762,
        lng=139.6503,
        density="dense",
        effective_mode="transit",
        scope_tier="city",
        scope_shape="area",
        search_radius_m=radius_m,
    )


def _movement():
    return get_config().movement


def test_utility_category_clamps_to_walkable() -> None:
    out = clamp_to_walkable_for_utility(
        _city_working(), [PlaceCategory.atm], _movement()
    )
    # walkable(1000) × transit(2.0) × dense(0.7) = 1400; tier follows.
    assert out.search_radius_m == 1400.0
    assert out.scope_tier == "walkable"


def test_non_utility_category_unchanged() -> None:
    working = _city_working()
    out = clamp_to_walkable_for_utility(
        working, [PlaceCategory.restaurant], _movement()
    )
    assert out.search_radius_m == working.search_radius_m
    assert out.scope_tier == "city"


def test_no_categories_unchanged() -> None:
    working = _city_working()
    out = clamp_to_walkable_for_utility(working, None, _movement())
    assert out.search_radius_m == working.search_radius_m


def test_mixed_categories_clamp_when_any_is_utility() -> None:
    out = clamp_to_walkable_for_utility(
        _city_working(),
        [PlaceCategory.restaurant, PlaceCategory.pharmacy],
        _movement(),
    )
    assert out.search_radius_m == 1400.0


def test_clamp_never_widens() -> None:
    # Already tighter than the walkable radius — left alone (min).
    working = _city_working(radius_m=500.0)
    out = clamp_to_walkable_for_utility(
        working, [PlaceCategory.atm], _movement()
    )
    assert out.search_radius_m == 500.0
