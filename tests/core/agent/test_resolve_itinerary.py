"""Resolving a multi-stop trip onto the working location (ADR-148).

Same harness shape as test_resolve_location.py, with a geocoder whose
`search` answers per stop — an itinerary geocodes each named stop, and the
shape must degrade to a plain area turn rather than fail when the stops
don't survive.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import HumanMessage

from kebi.core.agent.graph import make_resolve_location_node
from kebi.core.agent.location import LocationResolution
from kebi.core.places.nominatim_geocoding_client import GeocodeResult

_STOP_GEO: dict[str, GeocodeResult] = {
    "Hanoi, Vietnam": GeocodeResult(
        lat=21.03, lng=105.85, country="Vietnam", country_code="vn", city="Hanoi"
    ),
    "Hue, Vietnam": GeocodeResult(
        lat=16.46, lng=107.59, country="Vietnam", country_code="vn", city="Hue"
    ),
    "Hoi An, Vietnam": GeocodeResult(
        lat=15.88, lng=108.33, country="Vietnam", country_code="vn", city="Hoi An"
    ),
}


def _resolver_llm(resolution: LocationResolution) -> MagicMock:
    llm = MagicMock()
    structured = MagicMock()
    structured.ainvoke = AsyncMock(return_value=resolution)
    llm.with_structured_output = MagicMock(return_value=structured)
    return llm


def _geocoder(stops: dict[str, GeocodeResult] | None = None) -> MagicMock:
    client = MagicMock()
    client.forward = AsyncMock(
        return_value=GeocodeResult(
            lat=21.03, lng=105.85, country_code="vn", place_type="city"
        )
    )

    async def _search(*, query: str) -> GeocodeResult | None:
        return (stops or {}).get(query)

    client.search = AsyncMock(side_effect=_search)
    return client


def _itinerary_resolution(stops: list[str]) -> LocationResolution:
    return LocationResolution(
        source="explicit_query",
        country="Vietnam",
        city="Hanoi",
        scope_shape="itinerary",
        scope_tier="city",
        itinerary_stops=stops,
    )


def _state(message: str) -> Any:
    return {
        "messages": [HumanMessage(content=message)],
        "user_location": None,
        "working_location": None,
        "movement_profile": None,
        "trip_movement": None,
        "user_profile": None,
        "reasoning_steps": [],
    }


_TRIP = "I am doing Hanoi, then Hue, then Hoi An — what should I stop at?"


async def test_every_stop_is_geocoded_into_an_anchor() -> None:
    node = make_resolve_location_node(
        _resolver_llm(_itinerary_resolution(list(_STOP_GEO))),
        _geocoder(_STOP_GEO),
    )
    wl = (await node(_state(_TRIP)))["working_location"]
    assert wl["scope_shape"] == "itinerary"
    anchors = wl["itinerary"]
    # The label is the city half of the stop string — the agent quotes it.
    assert [a["name"] for a in anchors] == ["Hanoi", "Hue", "Hoi An"]
    assert [a["lat"] for a in anchors] == [21.03, 16.46, 15.88]
    # Area names + code ride along for per-stop area knowledge.
    assert anchors[1]["city"] == "Hue"
    assert anchors[1]["country_code"] == "vn"
    # The working point anchors at the first stop.
    assert wl["city"] == "Hanoi"


async def test_a_stop_that_fails_to_geocode_is_dropped_not_fatal() -> None:
    stops = dict(_STOP_GEO)
    stops.pop("Hue, Vietnam")
    node = make_resolve_location_node(
        _resolver_llm(_itinerary_resolution(list(_STOP_GEO))), _geocoder(stops)
    )
    wl = (await node(_state(_TRIP)))["working_location"]
    assert wl["scope_shape"] == "itinerary"
    assert [a["name"] for a in wl["itinerary"]] == ["Hanoi", "Hoi An"]


async def test_fewer_than_two_surviving_stops_degrades_to_an_area_turn() -> None:
    """A worse answer, never a failed one: with the trip un-geocodable the
    turn proceeds as a plain area search around the primary anchor."""
    node = make_resolve_location_node(
        _resolver_llm(_itinerary_resolution(list(_STOP_GEO))), _geocoder(None)
    )
    update = await node(_state(_TRIP))
    wl = update["working_location"]
    assert update["location_clarification"] is None
    assert wl["scope_shape"] == "area"
    assert wl["itinerary"] is None


async def test_the_stop_list_is_capped_by_config() -> None:
    from kebi.core.config import get_config

    max_stops = get_config().agent.itinerary.max_stops
    many = [f"Stop {i}, Nowhere" for i in range(max_stops + 3)]
    geocoder = _geocoder({})
    node = make_resolve_location_node(
        _resolver_llm(_itinerary_resolution(many)), geocoder
    )
    await node(_state(_TRIP))
    assert geocoder.search.await_count == max_stops
