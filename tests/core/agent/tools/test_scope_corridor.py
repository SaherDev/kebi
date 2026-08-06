"""Tests for `anchor_to_corridor` — retrieval along the way (ADR-137)."""

from __future__ import annotations

from typing import Any

from kebi.core.agent.location import CorridorTarget, WorkingLocation, resolve_radius
from kebi.core.agent.tools._scope import (
    anchor_to_corridor,
    clamp_to_walkable_for_utility,
)
from kebi.core.config import get_config
from kebi.core.places.models import PlaceCategory
from kebi.core.utils.geo import haversine_m


def _working(**kw: Any) -> WorkingLocation:
    base: dict[str, Any] = {
        "country": "Indonesia",
        "country_code": "id",
        "city": "Badung",
        "lat": -8.65,
        "lng": 115.13,
        "search_radius_m": 2000.0,
        "scope_shape": "area",
        "effective_mode": "driving",
    }
    base.update(kw)
    return WorkingLocation(**base)


_DESTINATION = CorridorTarget(name="Uluwatu", lat=-8.83, lng=115.09)


def _corridor_turn(**kw: Any) -> WorkingLocation:
    return _working(scope_shape="corridor", corridor=_DESTINATION, **kw)


def test_area_turn_is_untouched() -> None:
    working = _working()
    assert anchor_to_corridor(working) == working


def test_corridor_without_a_geocoded_destination_is_untouched() -> None:
    working = _working(scope_shape="corridor", corridor=None)
    assert anchor_to_corridor(working) == working


def test_anchor_moves_to_the_midpoint_of_the_route() -> None:
    out = anchor_to_corridor(_corridor_turn())
    assert out.lat == (-8.65 + -8.83) / 2
    assert out.lng == (115.13 + 115.09) / 2


def test_radius_covers_both_ends_of_the_route_plus_the_band() -> None:
    working = _corridor_turn()
    out = anchor_to_corridor(working)
    leg_m = haversine_m(working.lat, working.lng, _DESTINATION.lat, _DESTINATION.lng)
    assert out.search_radius_m == leg_m / 2 + working.search_radius_m
    # Both endpoints fall inside the new circle — that is what "along the
    # way" has to mean at minimum.
    for lat, lng in ((working.lat, working.lng), (_DESTINATION.lat, _DESTINATION.lng)):
        assert haversine_m(out.lat, out.lng, lat, lng) <= out.search_radius_m


def test_a_short_hop_stays_a_small_search() -> None:
    near = CorridorTarget(name="Berawa", lat=-8.66, lng=115.14)
    out = anchor_to_corridor(_working(scope_shape="corridor", corridor=near))
    assert out.search_radius_m < 4000


def test_utility_clamp_is_a_no_op_on_a_route() -> None:
    # "A pharmacy on the way to the airport" must not collapse to a walkable
    # circle around a random patch of road.
    working = anchor_to_corridor(_corridor_turn())
    clamped = clamp_to_walkable_for_utility(
        working, [PlaceCategory.pharmacy], get_config().movement
    )
    assert clamped == working


def test_utility_clamp_still_applies_off_a_route() -> None:
    working = _working(search_radius_m=20000.0)
    clamped = clamp_to_walkable_for_utility(
        working, [PlaceCategory.pharmacy], get_config().movement
    )
    assert clamped.search_radius_m < working.search_radius_m
    assert clamped.scope_tier == "walkable"


class TestRadiusCap:
    """The three radius factors multiply, so the wide end has to be bounded
    (ADR-143).

    Regression: metro x motorbike x sparse resolved to 172.8 km — wider than
    Bali is long — so every "nearby" search covered the whole island and a
    place an hour in the wrong direction ranked as if it were on the way.
    """

    def test_the_widest_combination_is_capped(self) -> None:
        cfg = get_config().movement
        radius = resolve_radius("driving", "metro", "far", "sparse", cfg)
        assert radius == cfg.max_radius_m

    def test_every_combination_stays_within_the_cap(self) -> None:
        cfg = get_config().movement
        for tier in ("walkable", "neighborhood", "city", "metro"):
            for mode in ("walking", "cycling", "transit", "motorbike", "driving"):
                for density in ("dense", "medium", "sparse"):
                    for reach in ("compact", "normal", "far"):
                        assert (
                            resolve_radius(mode, tier, reach, density, cfg)
                            <= cfg.max_radius_m
                        )

    def test_ordinary_scopes_are_untouched_by_the_cap(self) -> None:
        # The cap must bound the tail without flattening the useful range.
        cfg = get_config().movement
        walkable = resolve_radius("walking", "walkable", "normal", "dense", cfg)
        city = resolve_radius("motorbike", "city", "normal", "medium", cfg)
        assert walkable < city < cfg.max_radius_m

    def test_a_long_route_may_exceed_the_cap(self) -> None:
        """Corridor width is geometry, not a guess, so it is not capped.

        Clamping it would reintroduce the original bug from the other side:
        a search that cannot reach the far end of the route.
        """
        far = CorridorTarget(name="Far", lat=-9.9, lng=115.09)
        working = _working(
            scope_shape="corridor", corridor=far, search_radius_m=20000.0
        )
        out = anchor_to_corridor(working)
        assert out.search_radius_m > get_config().movement.max_radius_m
