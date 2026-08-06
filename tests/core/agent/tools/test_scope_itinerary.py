"""Tests for `itinerary_segments` — multi-stop trip fan-out geometry (ADR-148)."""

from __future__ import annotations

from typing import Any

from kebi.core.agent.location import ItineraryAnchor, WorkingLocation
from kebi.core.agent.tools._scope import itinerary_segments
from kebi.core.utils.geo import haversine_m

_HANOI = ItineraryAnchor(
    name="Hanoi",
    lat=21.03,
    lng=105.85,
    city="Hanoi",
    country="Vietnam",
    country_code="vn",
)
_HUE = ItineraryAnchor(
    name="Hue",
    lat=16.46,
    lng=107.59,
    city="Hue",
    country="Vietnam",
    country_code="vn",
)
_HOI_AN = ItineraryAnchor(
    name="Hoi An",
    lat=15.88,
    lng=108.33,
    city="Hoi An",
    country="Vietnam",
    country_code="vn",
)


def _working(**kw: Any) -> WorkingLocation:
    base: dict[str, Any] = {
        "country": "Vietnam",
        "country_code": "vn",
        "city": "Hanoi",
        "neighborhood": "Old Quarter",
        "lat": 21.03,
        "lng": 105.85,
        "search_radius_m": 7000.0,
        "scope_shape": "itinerary",
        "scope_tier": "city",
        "effective_mode": "driving",
        "itinerary": [_HANOI, _HUE, _HOI_AN],
    }
    base.update(kw)
    return WorkingLocation(**base)


def test_an_area_turn_yields_no_segments() -> None:
    assert itinerary_segments(_working(scope_shape="area")) == []


def test_fewer_than_two_anchors_yields_no_segments() -> None:
    assert itinerary_segments(_working(itinerary=[_HANOI])) == []
    assert itinerary_segments(_working(itinerary=None)) == []


def test_stops_come_first_in_trip_order_then_legs() -> None:
    labels = [s.label for s in itinerary_segments(_working())]
    assert labels == [
        "Hanoi",
        "Hue",
        "Hoi An",
        "on the way between Hanoi and Hue",
        "on the way between Hue and Hoi An",
    ]


def test_only_legs_are_marked_on_the_way() -> None:
    segments = itinerary_segments(_working())
    assert [s.on_the_way for s in segments] == [False, False, False, True, True]


def test_a_stop_searches_its_own_disc_with_its_own_names() -> None:
    hue = itinerary_segments(_working())[1].working
    assert (hue.lat, hue.lng) == (_HUE.lat, _HUE.lng)
    assert hue.city == "Hue"
    assert hue.country == "Vietnam"
    assert hue.country_code == "vn"
    # The primary anchor's neighbourhood must not leak into another stop —
    # area claims for "Old Quarter, Hue" would be nonsense.
    assert hue.neighborhood is None
    # City-tier disc: the turn's resolved radius, unchanged.
    assert hue.search_radius_m == 7000.0


def test_a_leg_covers_both_ends_plus_the_stop_band() -> None:
    leg = itinerary_segments(_working())[4].working  # Hue → Hoi An
    assert leg.lat == (_HUE.lat + _HOI_AN.lat) / 2
    assert leg.lng == (_HUE.lng + _HOI_AN.lng) / 2
    for anchor in (_HUE, _HOI_AN):
        assert (
            haversine_m(leg.lat, leg.lng, anchor.lat, anchor.lng) <= leg.search_radius_m
        )


def test_the_leg_between_hue_and_hoi_an_contains_da_nang() -> None:
    """The whole point of the leg circle: a city on the route the user never
    named falls inside it, so their saves there can surface as on the way."""
    leg = itinerary_segments(_working())[4].working
    da_nang = (16.07, 108.22)
    assert haversine_m(leg.lat, leg.lng, *da_nang) <= leg.search_radius_m


def test_segment_workings_are_not_themselves_itineraries() -> None:
    # A segment fed back through the fan-out must be a plain area search —
    # otherwise the tools would recurse.
    for segment in itinerary_segments(_working()):
        assert segment.working.scope_shape == "area"
        assert segment.working.itinerary is None
        assert itinerary_segments(segment.working) == []


def test_an_anchor_without_a_geocoded_city_falls_back_to_its_name() -> None:
    bare = ItineraryAnchor(name="Hue", lat=16.46, lng=107.59)
    segments = itinerary_segments(_working(itinerary=[_HANOI, bare]))
    assert segments[1].working.city == "Hue"
    # No code means that stop contributes no name-keyed area knowledge —
    # explicit None, never the primary stop's code.
    assert segments[1].working.country_code is None
