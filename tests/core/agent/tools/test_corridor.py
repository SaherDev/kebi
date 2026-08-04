"""Tests for `_corridor` — route-shaped search scope (ADR-136).

Covers the three things the place tools ask of a route: where to search
(`waypoint_contexts` / `enclosing_context`), what counts as on it
(`filter_and_order`), and when venue stops stop being an honest answer
(`is_route_too_long`).
"""

from __future__ import annotations

from kebi.core.agent.location import CorridorPath, CorridorTarget, WorkingLocation
from kebi.core.agent.tools._corridor import (
    enclosing_context,
    filter_and_order,
    half_width_m,
    is_corridor,
    is_route_too_long,
    place_coords,
    route_summary,
    waypoint_contexts,
)
from kebi.core.config import MovementConfig
from kebi.core.places.models import LocationContext, PlaceCore
from kebi.core.utils.geo import haversine_m, path_progress

_CFG = MovementConfig()

_DA_NANG = (16.0544, 108.2022)
_HUE = (16.4637, 107.5909)
_HOI_AN = (15.8801, 108.3380)
_HANOI = (21.0278, 105.8342)
_SAIGON = (10.8231, 106.6297)
_LANG_CO = (16.2333, 108.0667)


def _working(
    *,
    stops: list[tuple[str, tuple[float, float]]] | None = None,
    radius_m: float = 9_000.0,
    origin: tuple[float, float] = _DA_NANG,
    city: str = "Da Nang",
) -> WorkingLocation:
    corridor = (
        CorridorPath(
            stops=[
                CorridorTarget(name=name, lat=point[0], lng=point[1])
                for name, point in stops
            ]
        )
        if stops
        else None
    )
    return WorkingLocation(
        country="Vietnam",
        city=city,
        lat=origin[0],
        lng=origin[1],
        country_code="vn",
        effective_mode="driving",
        scope_tier="city",
        scope_shape="corridor" if stops else "area",
        search_radius_m=radius_m,
        corridor=corridor,
    )


def _core(name: str, point: tuple[float, float] | None) -> PlaceCore:
    location = (
        LocationContext(lat=point[0], lng=point[1]) if point is not None else None
    )
    return PlaceCore(
        id=name,
        provider_id=f"google:{name}",
        place_name=name,
        location=location,
    )


class TestIsCorridor:
    def test_area_turn_is_not_a_corridor(self) -> None:
        assert is_corridor(_working()) is False

    def test_route_turn_is_a_corridor(self) -> None:
        assert is_corridor(_working(stops=[("Hue", _HUE)])) is True

    def test_missing_working_location_is_not_a_corridor(self) -> None:
        assert is_corridor(None) is False

    def test_corridor_shape_without_a_resolved_route_is_not_a_corridor(self) -> None:
        """Defensive: the shape says corridor but nothing resolved. The tools
        must fall back to the ordinary disc rather than crash on an empty
        path."""
        working = _working(stops=[("Hue", _HUE)]).model_copy(
            update={"corridor": CorridorPath(stops=[])}
        )
        assert is_corridor(working) is False


class TestHalfWidth:
    def test_uses_the_turn_radius_when_it_is_tight(self) -> None:
        working = _working(stops=[("Hue", _HUE)], radius_m=2_000.0)
        assert half_width_m(working, _CFG) == 2_000.0

    def test_config_ceiling_caps_a_sprawling_radius(self) -> None:
        """A metro-tier driving turn resolves past 100 km — unchecked, that
        would make "along the way" mean "anywhere in the country"."""
        working = _working(stops=[("Hue", _HUE)], radius_m=117_000.0)
        assert half_width_m(working, _CFG) == _CFG.corridor.max_half_width_m


class TestLengthGate:
    def test_a_day_drive_is_not_too_long(self) -> None:
        assert is_route_too_long(_working(stops=[("Hue", _HUE)]), _CFG) is False

    def test_a_country_crossing_is_too_long(self) -> None:
        working = _working(stops=[("Saigon", _SAIGON)], origin=_HANOI, city="Hanoi")
        assert is_route_too_long(working, _CFG) is True

    def test_one_short_leg_keeps_the_route_answerable(self) -> None:
        """Hanoi → Hue is city-scale, but Hue → Hoi An is a real drive. The
        gate is per leg, so the answerable stretch still gets answered."""
        working = _working(
            stops=[("Hue", _HUE), ("Hoi An", _HOI_AN)], origin=_HANOI, city="Hanoi"
        )
        assert is_route_too_long(working, _CFG) is False

    def test_oversized_leg_contributes_no_waypoints(self) -> None:
        working = _working(
            stops=[("Hue", _HUE), ("Hoi An", _HOI_AN)], origin=_HANOI, city="Hanoi"
        )
        contexts = waypoint_contexts(working, _CFG)
        cut = path_progress([_HANOI, _HUE, _HOI_AN], *_HUE)
        interior_on_long_leg = [
            c
            for c in contexts
            if path_progress([_HANOI, _HUE, _HOI_AN], c.lat, c.lng) < cut - 1e-9
            and (c.lat, c.lng) != _HANOI
        ]
        assert interior_on_long_leg == []


class TestWaypointContexts:
    def test_every_named_stop_is_searched(self) -> None:
        working = _working(stops=[("Hue", _HUE), ("Hoi An", _HOI_AN)])
        points = [(c.lat, c.lng) for c in waypoint_contexts(working, _CFG)]
        assert _DA_NANG in points
        assert _HUE in points
        assert _HOI_AN in points

    def test_fan_out_respects_the_billed_cap(self) -> None:
        working = _working(stops=[("Hue", _HUE)])
        assert len(waypoint_contexts(working, _CFG)) <= _CFG.corridor.max_waypoints

    def test_each_context_carries_the_half_width_and_named_geography(self) -> None:
        working = _working(stops=[("Hue", _HUE)], radius_m=6_000.0)
        for context in waypoint_contexts(working, _CFG):
            assert context.radius_m == 6_000
            assert context.city == "Da Nang"
            assert context.country == "Vietnam"

    def test_waypoints_come_back_in_path_order(self) -> None:
        working = _working(stops=[("Hue", _HUE)])
        contexts = waypoint_contexts(working, _CFG)
        progress = [path_progress([_DA_NANG, _HUE], c.lat, c.lng) for c in contexts]
        assert progress == sorted(progress)


class TestEnclosingContext:
    def test_circle_covers_both_endpoints(self) -> None:
        working = _working(stops=[("Hue", _HUE)])
        context = enclosing_context(working, _CFG)
        for point in (_DA_NANG, _HUE):
            assert haversine_m(context.lat, context.lng, *point) <= context.radius_m

    def test_circle_is_one_context_not_many(self) -> None:
        """The whole point: name validation stays one provider call per name,
        however long the route is."""
        working = _working(stops=[("Hue", _HUE), ("Hoi An", _HOI_AN)])
        assert isinstance(enclosing_context(working, _CFG), LocationContext)


class TestFilterAndOrder:
    def test_orders_results_from_origin_to_destination(self) -> None:
        working = _working(stops=[("Hue", _HUE)])
        places = [_core("hue", _HUE), _core("lang-co", _LANG_CO), _core("dn", _DA_NANG)]
        ordered = filter_and_order(places, working, _CFG, coords=place_coords)
        assert [p.place_name for p in ordered] == ["dn", "lang-co", "hue"]

    def test_drops_a_place_well_off_the_route(self) -> None:
        """Hoi An is south of Da Nang — the wrong direction for a Hue trip.
        Before this step it would have ranked fine on a disc around the
        origin."""
        working = _working(stops=[("Hue", _HUE)])
        places = [_core("hoi-an", _HOI_AN), _core("lang-co", _LANG_CO)]
        ordered = filter_and_order(places, working, _CFG, coords=place_coords)
        assert [p.place_name for p in ordered] == ["lang-co"]

    def test_a_place_on_the_second_leg_survives_and_sorts_last(self) -> None:
        working = _working(stops=[("Hue", _HUE), ("Hoi An", _HOI_AN)])
        places = [_core("hoi-an", _HOI_AN), _core("dn", _DA_NANG)]
        ordered = filter_and_order(places, working, _CFG, coords=place_coords)
        assert [p.place_name for p in ordered] == ["dn", "hoi-an"]

    def test_place_with_unknown_coordinates_survives_at_the_end(self) -> None:
        """Coordinates are wiped on the 30-day ToS cycle, so a saved row can
        legitimately have none. It cannot be placed on the route — but it is
        not evidence of being off it either."""
        working = _working(stops=[("Hue", _HUE)])
        places = [_core("unknown", None), _core("lang-co", _LANG_CO)]
        ordered = filter_and_order(places, working, _CFG, coords=place_coords)
        assert [p.place_name for p in ordered] == ["lang-co", "unknown"]

    def test_everything_off_route_returns_empty(self) -> None:
        """The honest empty: better than places clustered at the origin."""
        working = _working(stops=[("Hue", _HUE)])
        ordered = filter_and_order(
            [_core("hoi-an", _HOI_AN)], working, _CFG, coords=place_coords
        )
        assert ordered == []


class TestPlaceCoords:
    def test_reads_coordinates_from_the_location_block(self) -> None:
        assert place_coords(_core("x", _HUE)) == _HUE

    def test_missing_location_is_none(self) -> None:
        assert place_coords(_core("x", None)) is None


class TestRouteSummary:
    def test_names_every_stop_in_order_with_a_distance(self) -> None:
        working = _working(stops=[("Hue", _HUE), ("Hoi An", _HOI_AN)])
        summary = route_summary(working)
        assert summary.startswith("Da Nang → Hue → Hoi An")
        assert "km" in summary
