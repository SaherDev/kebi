"""Tests for the resolve_location node — start-of-turn location resolution."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import HumanMessage

from kebi.core.agent.graph import make_resolve_location_node
from kebi.core.agent.location import LocationResolution
from kebi.core.places.nominatim_geocoding_client import GeocodingError


def _resolver_llm(resolution: LocationResolution | Exception) -> MagicMock:
    """Fake resolver LLM whose structured runnable yields `resolution`."""
    llm = MagicMock()
    structured = MagicMock()

    async def _ainvoke(_messages: Any) -> LocationResolution:
        if isinstance(resolution, Exception):
            raise resolution
        return resolution

    structured.ainvoke = MagicMock(side_effect=_ainvoke)
    llm.with_structured_output = MagicMock(return_value=structured)
    return llm


def _geocoder(
    forward: tuple[float, float] | None = None,
    reverse: dict[str, str] | None = None,
) -> MagicMock:
    client = MagicMock()
    client.forward = AsyncMock(return_value=forward)
    client.reverse = AsyncMock(return_value=reverse)
    return client


def _state(
    message: str = "ramen in Tokyo",
    user_location: dict[str, Any] | None = None,
    working_location: dict[str, Any] | None = None,
) -> Any:
    return {
        "messages": [HumanMessage(content=message)],
        "user_location": user_location,
        "working_location": working_location,
        "reasoning_steps": [],
    }


async def test_explicit_query_is_forward_geocoded() -> None:
    resolution = LocationResolution(
        source="explicit_query", country="Japan", city="Tokyo", neighborhood="Shibuya"
    )
    node = make_resolve_location_node(
        _resolver_llm(resolution), _geocoder(forward=(35.66, 139.70))
    )
    update = await node(_state())
    wl = update["working_location"]
    assert wl["city"] == "Tokyo"
    assert wl["neighborhood"] == "Shibuya"
    assert wl["lat"] == 35.66
    assert update["location_clarification"] is None


async def test_carried_continuation_reuses_prior_working_location() -> None:
    """A continuation reuses the carried working location verbatim — no
    geocoding, and the LLM does not re-name the place."""
    resolution = LocationResolution(source="carried", is_shift=False)
    prior = {
        "country": "Japan",
        "city": "Tokyo",
        "neighborhood": "Shibuya",
        "lat": 35.66,
        "lng": 139.70,
    }
    geocoder = _geocoder()
    node = make_resolve_location_node(_resolver_llm(resolution), geocoder)
    update = await node(
        _state(message="and what else is there", working_location=prior)
    )
    assert update["working_location"] == prior
    geocoder.forward.assert_not_awaited()
    geocoder.reverse.assert_not_awaited()


async def test_carried_with_no_prior_falls_back_to_user_actual() -> None:
    resolution = LocationResolution(source="carried")
    geocoder = _geocoder(reverse={"country": "Thailand", "city": "Bangkok"})
    node = make_resolve_location_node(_resolver_llm(resolution), geocoder)
    update = await node(
        _state(message="what else", user_location={"lat": 13.73, "lng": 100.5})
    )
    assert update["working_location"]["city"] == "Bangkok"


async def test_shift_resolves_the_new_place() -> None:
    resolution = LocationResolution(
        source="explicit_query",
        country="Israel",
        city="Tel Aviv",
        neighborhood="Florentin",
        is_shift=True,
    )
    node = make_resolve_location_node(
        _resolver_llm(resolution), _geocoder(forward=(32.05, 34.77))
    )
    update = await node(
        _state(
            message="actually what about Tel Aviv",
            working_location={
                "country": "Japan",
                "city": "Tokyo",
                "neighborhood": "Shibuya",
                "lat": 35.66,
                "lng": 139.70,
            },
        )
    )
    assert update["working_location"]["city"] == "Tel Aviv"


async def test_ambiguous_triggers_clarification() -> None:
    resolution = LocationResolution(
        source="explicit_query",
        city="Cambridge",
        is_ambiguous=True,
        needs_clarification=True,
        clarification_reason="Cambridge UK or Massachusetts?",
    )
    node = make_resolve_location_node(_resolver_llm(resolution), _geocoder())
    update = await node(_state(message="what about Cambridge"))
    assert update["working_location"] is None
    assert "Cambridge UK or Massachusetts?" in update["location_clarification"]


async def test_explicit_query_missing_neighborhood_clarifies() -> None:
    """An explicitly named place must resolve all five fields."""
    resolution = LocationResolution(
        source="explicit_query", country="Japan", city="Tokyo"
    )
    node = make_resolve_location_node(
        _resolver_llm(resolution), _geocoder(forward=(35.6, 139.7))
    )
    update = await node(_state())
    assert update["working_location"] is None
    assert update["location_clarification"]


async def test_user_actual_reverse_geocodes_request_coords() -> None:
    """user_actual takes coords straight from the request and reverse-geocodes
    them — the LLM is never asked to transcribe the numbers. Neighborhood may
    be absent and that is accepted."""
    resolution = LocationResolution(source="user_actual")
    geocoder = _geocoder(reverse={"country": "Germany", "city": "Magdeburg"})
    node = make_resolve_location_node(_resolver_llm(resolution), geocoder)
    update = await node(
        _state(
            message="good places near me",
            user_location={"lat": 52.13, "lng": 11.64},
        )
    )
    wl = update["working_location"]
    assert wl is not None
    assert wl["city"] == "Magdeburg"
    assert wl["lat"] == 52.13
    assert wl["neighborhood"] is None
    assert update["location_clarification"] is None
    geocoder.reverse.assert_awaited_once_with(lat=52.13, lng=11.64)


async def test_user_actual_without_request_location_clarifies() -> None:
    """user_actual with no location in the request → ask the user."""
    resolution = LocationResolution(source="user_actual")
    node = make_resolve_location_node(_resolver_llm(resolution), _geocoder())
    update = await node(_state(message="good places near me", user_location=None))
    assert update["working_location"] is None
    assert update["location_clarification"]


async def test_geocode_failure_fails_toward_clarification() -> None:
    resolution = LocationResolution(
        source="explicit_query", country="Japan", city="Tokyo", neighborhood="Shibuya"
    )
    geocoder = MagicMock()
    geocoder.forward = AsyncMock(side_effect=GeocodingError("nominatim down"))
    node = make_resolve_location_node(_resolver_llm(resolution), geocoder)
    update = await node(_state())
    assert update["working_location"] is None
    assert update["location_clarification"]
    # A geocode failure is not an agent error — it must not bump error_count.
    assert "error_count" not in update


async def test_llm_failure_fails_toward_clarification() -> None:
    node = make_resolve_location_node(
        _resolver_llm(RuntimeError("llm unreachable")), _geocoder()
    )
    update = await node(_state())
    assert update["working_location"] is None
    assert update["location_clarification"]


async def test_emits_user_visible_reasoning_step() -> None:
    resolution = LocationResolution(
        source="explicit_query", country="Japan", city="Tokyo", neighborhood="Shibuya"
    )
    node = make_resolve_location_node(
        _resolver_llm(resolution), _geocoder(forward=(35.6, 139.7))
    )
    update = await node(_state())
    steps = update["reasoning_steps"]
    assert len(steps) == 1
    assert steps[0].step == "agent.location_resolved"
    assert steps[0].visibility == "user"
