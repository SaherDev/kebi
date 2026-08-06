"""`suggest_places` stop re-anchoring on a multi-stop trip (ADR-148)."""

from __future__ import annotations

from typing import Any

from kebi.core.agent.location import ItineraryAnchor, WorkingLocation
from kebi.core.agent.tools.suggest_places_tool import _anchor_to_itinerary_stop

_ANCHORS = [
    ItineraryAnchor(
        name="Hanoi",
        lat=21.03,
        lng=105.85,
        city="Hanoi",
        country="Vietnam",
        country_code="vn",
    ),
    ItineraryAnchor(
        name="Da Nang",
        lat=16.07,
        lng=108.22,
        city="Da Nang",
        country="Vietnam",
        country_code="vn",
    ),
]


def _working(**kw: Any) -> WorkingLocation:
    base: dict[str, Any] = {
        "country": "Vietnam",
        "country_code": "vn",
        "city": "Hanoi",
        "lat": 21.03,
        "lng": 105.85,
        "search_radius_m": 7000.0,
        "scope_shape": "itinerary",
        "scope_tier": "city",
        "effective_mode": "driving",
        "itinerary": _ANCHORS,
    }
    base.update(kw)
    return WorkingLocation(**base)


def test_a_named_stop_re_anchors_provider_verification() -> None:
    """ "43 Factory" in Da Nang must be verified against Da Nang's disc, not
    the trip's first city — or every mid-route taste pick gets dropped."""
    out = _anchor_to_itinerary_stop(_working(), "da nang")
    assert (out.lat, out.lng) == (16.07, 108.22)
    assert out.city == "Da Nang"
    assert out.scope_shape == "area"
    assert out.itinerary is None


def test_a_city_that_is_not_a_stop_keeps_the_primary_anchor() -> None:
    # The agent does not get to pick geography freely (ADR-083) — only a
    # resolved stop re-anchors.
    working = _working()
    assert _anchor_to_itinerary_stop(working, "Bangkok") == working


def test_no_override_and_non_itinerary_turns_are_untouched() -> None:
    working = _working()
    assert _anchor_to_itinerary_stop(working, None) == working
    area = _working(scope_shape="area", itinerary=None)
    assert _anchor_to_itinerary_stop(area, "Da Nang") == area
