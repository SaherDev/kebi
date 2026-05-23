"""Tests for the resolve_location node — start-of-turn location resolution."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import HumanMessage

from kebi.core.agent.graph import _CORRIDOR_ASK, make_resolve_location_node
from kebi.core.agent.location import LocationResolution, resolve_radius
from kebi.core.config import get_config
from kebi.core.places.nominatim_geocoding_client import GeocodeResult, GeocodingError


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
    reverse: dict[str, Any] | None = None,
    search: tuple[float, float] | None = None,
    place_type: str | None = None,
    bbox: list[float] | None = None,
) -> MagicMock:
    """Fake geocoder. `forward`/`search` take `(lat, lng)` tuples and
    `reverse` a `{country, city, ...}` dict — wrapped into `GeocodeResult`s.
    `place_type` sets the density signal on every result."""
    client = MagicMock()
    client.forward = AsyncMock(
        return_value=(
            GeocodeResult(
                lat=forward[0], lng=forward[1], place_type=place_type, bbox=bbox
            )
            if forward is not None
            else None
        )
    )
    client.search = AsyncMock(
        return_value=(
            GeocodeResult(
                lat=search[0], lng=search[1], place_type=place_type, bbox=bbox
            )
            if search is not None
            else None
        )
    )
    client.reverse = AsyncMock(
        return_value=(
            GeocodeResult(
                lat=reverse.get("lat", 0.0),
                lng=reverse.get("lng", 0.0),
                country=reverse.get("country"),
                city=reverse.get("city"),
                neighborhood=reverse.get("neighborhood"),
                place_type=reverse.get("place_type", place_type),
                bbox=bbox,
            )
            if reverse is not None
            else None
        )
    )
    return client


def _state(
    message: str = "ramen in Tokyo",
    user_location: dict[str, Any] | None = None,
    working_location: dict[str, Any] | None = None,
    movement_profile: dict[str, Any] | None = None,
) -> Any:
    return {
        "messages": [HumanMessage(content=message)],
        "user_location": user_location,
        "working_location": working_location,
        "movement_profile": movement_profile,
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
    wl = update["working_location"]
    # The point is reused verbatim — no geocoding, no LLM re-naming.
    assert {k: wl[k] for k in prior} == prior
    # Scope (ADR-084) is still resolved fresh every turn, even on a carry.
    assert wl["search_radius_m"] > 0
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
        country="Thailand",
        city="Bangkok",
        neighborhood="Thonglor",
        is_shift=True,
    )
    node = make_resolve_location_node(
        _resolver_llm(resolution), _geocoder(forward=(13.73, 100.58))
    )
    update = await node(
        _state(
            message="actually what about Bangkok",
            working_location={
                "country": "Japan",
                "city": "Tokyo",
                "neighborhood": "Shibuya",
                "lat": 35.66,
                "lng": 139.70,
            },
        )
    )
    assert update["working_location"]["city"] == "Bangkok"


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


async def test_explicit_query_bare_city_resolves_without_neighborhood() -> None:
    """A user naming just a city ("what about Chiang Mai") resolves on
    country + city — neighborhood is optional, like user_actual. Demanding a
    neighborhood would force a needless "confirm the city?" clarification."""
    resolution = LocationResolution(
        source="explicit_query", country="Thailand", city="Chiang Mai"
    )
    node = make_resolve_location_node(
        _resolver_llm(resolution), _geocoder(forward=(18.79, 98.99))
    )
    update = await node(_state(message="actually what about Chiang Mai"))
    wl = update["working_location"]
    assert wl is not None
    assert wl["city"] == "Chiang Mai"
    assert wl["neighborhood"] is None
    assert update["location_clarification"] is None


async def test_explicit_query_missing_city_clarifies() -> None:
    """With no usable city the location cannot be pinned — ask the user."""
    resolution = LocationResolution(source="explicit_query", country="Thailand")
    node = make_resolve_location_node(
        _resolver_llm(resolution), _geocoder(forward=(13.7, 100.5))
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


# --- Movement / search scope (ADR-084) -------------------------------------


async def test_scope_is_resolved_onto_the_working_location() -> None:
    """A resolved turn carries effective_mode, tier, shape and a radius."""
    resolution = LocationResolution(
        source="explicit_query",
        country="Japan",
        city="Tokyo",
        neighborhood="Shibuya",
        scope_tier="walkable",
        scope_shape="area",
        effective_mode="walking",
    )
    node = make_resolve_location_node(
        _resolver_llm(resolution), _geocoder(forward=(35.66, 139.70))
    )
    wl = (await node(_state()))["working_location"]
    assert wl["scope_tier"] == "walkable"
    assert wl["scope_shape"] == "area"
    assert wl["effective_mode"] == "walking"
    assert wl["search_radius_m"] > 0


async def test_day_trip_to_a_different_city_lands_shift_tier_and_mode() -> None:
    """Cross-interaction: a single resolution shifts the point to a new city,
    classifies a metro tier, and infers the driving mode — all together."""
    resolution = LocationResolution(
        source="explicit_query",
        country="Thailand",
        city="Pattaya",
        neighborhood="Central Pattaya",
        is_shift=True,
        scope_tier="metro",
        effective_mode="driving",
    )
    node = make_resolve_location_node(
        _resolver_llm(resolution), _geocoder(forward=(12.93, 100.88))
    )
    update = await node(
        _state(
            message="actually let's do a day trip to Pattaya",
            working_location={
                "country": "Thailand",
                "city": "Bangkok",
                "neighborhood": "Thonglor",
                "lat": 13.73,
                "lng": 100.58,
            },
        )
    )
    wl = update["working_location"]
    assert wl["city"] == "Pattaya"  # shift re-anchored the point
    assert wl["scope_tier"] == "metro"
    assert wl["effective_mode"] == "driving"
    assert wl["search_radius_m"] == resolve_radius(
        "driving", "metro", "normal", "medium", get_config().movement
    )


async def test_resolver_effective_mode_is_honored() -> None:
    """The node trusts the resolver's effective_mode — an explicit per-turn
    mode wins even when it is outside the profile's available_modes."""
    resolution = LocationResolution(
        source="explicit_query",
        country="Thailand",
        city="Bangkok",
        neighborhood="Thonglor",
        effective_mode="driving",
    )
    node = make_resolve_location_node(
        _resolver_llm(resolution), _geocoder(forward=(13.73, 100.58))
    )
    update = await node(
        _state(
            message="what if I drive there",
            movement_profile={
                "available_modes": ["walking", "transit"],
                "reach": "normal",
            },
        )
    )
    assert update["working_location"]["effective_mode"] == "driving"


async def test_no_resolver_mode_falls_back_to_first_capability() -> None:
    """When the resolver leaves effective_mode empty (no explicit signal —
    e.g. "tired of walking" is scope language, not a mode word), the system
    falls back to the first listed capability (ADR-085)."""
    resolution = LocationResolution(
        source="explicit_query",
        country="Thailand",
        city="Bangkok",
        neighborhood="Thonglor",
        effective_mode=None,
    )
    node = make_resolve_location_node(
        _resolver_llm(resolution), _geocoder(forward=(13.73, 100.58))
    )
    update = await node(
        _state(
            message="somewhere further, I'm tired of walking",
            movement_profile={
                "available_modes": ["transit", "walking"],
                "reach": "normal",
            },
        )
    )
    # First capability in the list wins as the deterministic fallback.
    assert update["working_location"]["effective_mode"] == "transit"


async def test_no_profile_uses_config_fallback_first_capability() -> None:
    resolution = LocationResolution(
        source="explicit_query",
        country="Thailand",
        city="Bangkok",
        neighborhood="Thonglor",
        effective_mode=None,
    )
    node = make_resolve_location_node(
        _resolver_llm(resolution), _geocoder(forward=(13.73, 100.58))
    )
    update = await node(_state(movement_profile=None))
    assert (
        update["working_location"]["effective_mode"]
        == get_config().movement.fallback.available_modes[0]
    )


async def test_corridor_destination_is_eagerly_geocoded() -> None:
    resolution = LocationResolution(
        source="user_actual",
        scope_shape="corridor",
        corridor_destination="Suvarnabhumi Airport",
    )
    geocoder = _geocoder(
        reverse={"country": "Thailand", "city": "Bangkok"},
        search=(13.69, 100.75),
    )
    node = make_resolve_location_node(_resolver_llm(resolution), geocoder)
    update = await node(
        _state(
            message="a coffee place on my way to the airport",
            user_location={"lat": 13.75, "lng": 100.50},
        )
    )
    wl = update["working_location"]
    assert wl["scope_shape"] == "corridor"
    assert wl["corridor"]["name"] == "Suvarnabhumi Airport"
    assert wl["corridor"]["lat"] == 13.69
    assert update["location_clarification"] is None


async def test_corridor_unresolvable_destination_asks_not_area() -> None:
    """An ungeocodable corridor destination ("home") asks the user — it must
    NOT silently degrade to an area search around the current point."""
    resolution = LocationResolution(
        source="user_actual",
        scope_shape="corridor",
        corridor_destination="home",
    )
    geocoder = _geocoder(
        reverse={"country": "Thailand", "city": "Bangkok"},
        search=None,  # Nominatim finds no match for "home"
    )
    node = make_resolve_location_node(_resolver_llm(resolution), geocoder)
    update = await node(
        _state(
            message="coffee on my way home",
            user_location={"lat": 13.75, "lng": 100.50},
        )
    )
    assert update["working_location"] is None
    assert update["location_clarification"] == _CORRIDOR_ASK


async def test_corridor_with_no_destination_asks() -> None:
    resolution = LocationResolution(
        source="user_actual",
        scope_shape="corridor",
        corridor_destination=None,
    )
    geocoder = _geocoder(reverse={"country": "Thailand", "city": "Bangkok"})
    node = make_resolve_location_node(_resolver_llm(resolution), geocoder)
    update = await node(
        _state(message="coffee on my way", user_location={"lat": 13.75, "lng": 100.50})
    )
    assert update["working_location"] is None
    assert update["location_clarification"] == _CORRIDOR_ASK


async def test_density_from_geocoder_place_type_scales_the_radius() -> None:
    """The geocoder's place type sets density on the working location, which
    scales the radius — a sparse town reaches further than a dense city for
    the same walkable turn."""
    resolution = LocationResolution(
        source="user_actual", scope_tier="walkable", effective_mode="walking"
    )
    dense = await make_resolve_location_node(
        _resolver_llm(resolution),
        _geocoder(
            reverse={"country": "Thailand", "city": "Bangkok"}, place_type="city"
        ),
    )(_state(message="near me", user_location={"lat": 13.75, "lng": 100.50}))
    sparse = await make_resolve_location_node(
        _resolver_llm(resolution),
        _geocoder(reverse={"country": "Thailand", "city": "Pai"}, place_type="village"),
    )(_state(message="near me", user_location={"lat": 19.36, "lng": 98.44}))
    assert dense["working_location"]["density"] == "dense"
    assert sparse["working_location"]["density"] == "sparse"
    assert (
        sparse["working_location"]["search_radius_m"]
        > dense["working_location"]["search_radius_m"]
    )
