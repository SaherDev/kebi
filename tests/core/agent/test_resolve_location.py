"""Tests for the resolve_location node — start-of-turn location resolution."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import HumanMessage

from kebi.core.agent.graph import _CORRIDOR_ASK, make_resolve_location_node
from kebi.core.agent.location import LocationResolution, resolve_radius
from kebi.core.agent.state import LOCATION_INHERIT
from kebi.core.areas.models import AreaEntity
from kebi.core.config import get_config
from kebi.providers.geocoding import GeocodeResult, GeocodingError


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
    reverse: dict[str, Any] | None = None,
    search: tuple[float, float] | None = None,
    place_type: str | None = None,
    bbox: list[float] | None = None,
) -> MagicMock:
    """Fake geocoder boundary. `search` (a `(lat, lng)` tuple) feeds
    `search_area` — the coords-only path (corridor POI fallback,
    neighborhood refinement); `reverse` a `{country, city, ...}` dict."""
    client = MagicMock()
    client.search_area = AsyncMock(
        return_value=(
            GeocodeResult(
                lat=search[0], lng=search[1], place_type=place_type, bbox=bbox
            )
            if search is not None
            else None
        )
    )
    client.geocode_place_id = AsyncMock(return_value=None)
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


def _area_service(
    forward: tuple[float, float] | None = None,
    place_type: str | None = None,
    bbox: list[float] | None = None,
    country_code: str = "xx",
) -> MagicMock:
    """Fake AreaService. With `forward` set, any named country/city resolves
    to an entity whose centroid is the tuple; without it, resolution refuses
    (returns None) — the round-trip-failed case."""
    svc = MagicMock()

    def _country(name: str) -> AreaEntity | None:
        if forward is None:
            return None
        return AreaEntity(
            entity_key=country_code,
            entity_type="country",
            name=name,
            country_code=country_code,
            lat=forward[0],
            lng=forward[1],
        )

    def _city(name: str, cc: str) -> AreaEntity | None:
        if forward is None:
            return None
        return AreaEntity(
            entity_key=f"{cc}/{name.lower().replace(' ', '-')}",
            entity_type="city",
            name=name,
            country_code=cc,
            lat=forward[0],
            lng=forward[1],
            place_type=place_type,
            bbox=bbox,
        )

    svc.resolve_country = AsyncMock(side_effect=_country)
    svc.resolve_city = AsyncMock(side_effect=_city)
    return svc


def _node(
    resolution: LocationResolution | Exception,
    geocoder: MagicMock | None = None,
    area_service: MagicMock | None = None,
) -> Any:
    return make_resolve_location_node(
        _resolver_llm(resolution),
        geocoder if geocoder is not None else _geocoder(),
        area_service if area_service is not None else _area_service(),
    )


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


async def test_explicit_query_resolves_through_area_service() -> None:
    resolution = LocationResolution(
        source="explicit_query", country="Japan", city="Tokyo", neighborhood="Shibuya"
    )
    node = _node(resolution, area_service=_area_service(forward=(35.66, 139.70)))
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
    areas = _area_service()
    node = _node(resolution, geocoder, areas)
    update = await node(
        _state(message="and what else is there", working_location=prior)
    )
    wl = update["working_location"]
    # The point is reused verbatim — no geocoding, no LLM re-naming.
    assert {k: wl[k] for k in prior} == prior
    # Scope (ADR-084) is still resolved fresh every turn, even on a carry.
    assert wl["search_radius_m"] > 0
    geocoder.reverse.assert_not_awaited()
    areas.resolve_city.assert_not_awaited()


async def test_carried_with_no_prior_falls_back_to_user_actual() -> None:
    resolution = LocationResolution(source="carried")
    geocoder = _geocoder(reverse={"country": "Thailand", "city": "Bangkok"})
    node = _node(resolution, geocoder)
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
    node = _node(resolution, area_service=_area_service(forward=(13.73, 100.58)))
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


async def test_explicit_query_wins_over_far_user_actual() -> None:
    """Far-away GPS does not override an explicitly named place.

    Regression for a real bug where a lowercased named neighborhood +
    far GPS resolved to user_actual instead of shifting: the resolver's
    classification must win over the user's current physical
    coordinates regardless of the distance gap. The travelled-branch
    is only for messages that name no place.
    """
    resolution = LocationResolution(
        source="explicit_query",
        country="Thailand",
        city="Chiang Mai",
        neighborhood="Old Town",
        is_shift=True,
    )
    node = _node(resolution, area_service=_area_service(forward=(18.79, 98.99)))
    update = await node(
        _state(
            message="I'm in old town chiang mai next week — famous food spots?",
            # ~5000 km away from Chiang Mai — simulates the GPS-mismatch.
            user_location={"lat": 35.6762, "lng": 139.6503},
        )
    )
    wl = update["working_location"]
    assert wl["country"] == "Thailand"
    assert wl["city"] == "Chiang Mai"
    assert wl["neighborhood"] == "Old Town"
    # Coords come from the named place's resolved entity, NOT from the
    # user's actual location — locking the rule that explicit_query
    # never falls back to GPS.
    assert wl["lat"] == 18.79
    assert wl["lng"] == 98.99
    assert update["location_clarification"] is None


async def test_ambiguous_triggers_clarification() -> None:
    resolution = LocationResolution(
        source="explicit_query",
        city="Cambridge",
        is_ambiguous=True,
        needs_clarification=True,
        clarification_reason="Cambridge UK or Massachusetts?",
    )
    node = _node(resolution)
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
    node = _node(resolution, area_service=_area_service(forward=(18.79, 98.99)))
    update = await node(_state(message="actually what about Chiang Mai"))
    wl = update["working_location"]
    assert wl is not None
    assert wl["city"] == "Chiang Mai"
    assert wl["neighborhood"] is None
    assert update["location_clarification"] is None


async def test_explicit_query_missing_city_clarifies() -> None:
    """With no usable city the location cannot be pinned — ask the user."""
    resolution = LocationResolution(source="explicit_query", country="Thailand")
    node = _node(resolution, area_service=_area_service(forward=(13.7, 100.5)))
    update = await node(_state())
    assert update["working_location"] is None
    assert update["location_clarification"]


async def test_explicit_query_unverifiable_city_clarifies() -> None:
    """A named city the area service refuses (round-trip failure) asks the
    user instead of silently resolving to a merely similar feature."""
    resolution = LocationResolution(
        source="explicit_query", country="Thailand", city="Bangk0k Cty"
    )
    node = _node(resolution, area_service=_area_service(forward=None))
    update = await node(_state())
    assert update["working_location"] is None
    assert update["location_clarification"]


async def test_user_actual_reverse_geocodes_request_coords() -> None:
    """user_actual takes coords straight from the request and reverse-geocodes
    them — the LLM is never asked to transcribe the numbers. Neighborhood may
    be absent and that is accepted."""
    resolution = LocationResolution(source="user_actual")
    geocoder = _geocoder(reverse={"country": "Germany", "city": "Magdeburg"})
    node = _node(resolution, geocoder)
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


async def test_inherit_sentinel_on_fresh_thread_resolves_not_crashes() -> None:
    """Regression: on a brand-new thread LangGraph stores the first
    `working_location` value as-is, so the raw `LOCATION_INHERIT` sentinel
    string (not a dict) is present when the resolver runs. It must be treated
    as 'no carried location' rather than have `.get()` called on it — the bug
    was an `AttributeError: 'str' object has no attribute 'get'` that fell the
    whole turn through to a clarification."""
    resolution = LocationResolution(source="user_actual")
    geocoder = _geocoder(reverse={"country": "Japan", "city": "Tokyo"})
    node = _node(resolution, geocoder)
    update = await node(
        _state(
            message="any atm near me?",
            user_location={"lat": 35.6762, "lng": 139.6503},
            working_location=LOCATION_INHERIT,  # raw sentinel string, not a dict
        )
    )
    wl = update["working_location"]
    assert wl is not None
    assert wl["city"] == "Tokyo"
    assert wl["lat"] == 35.6762
    assert update["location_clarification"] is None


async def test_user_actual_without_request_location_clarifies() -> None:
    """user_actual with no location in the request → ask the user."""
    resolution = LocationResolution(source="user_actual")
    node = _node(resolution)
    update = await node(_state(message="good places near me", user_location=None))
    assert update["working_location"] is None
    assert update["location_clarification"]


async def test_geocode_failure_fails_toward_clarification() -> None:
    resolution = LocationResolution(
        source="explicit_query", country="Japan", city="Tokyo", neighborhood="Shibuya"
    )
    areas = MagicMock()
    areas.resolve_country = AsyncMock(side_effect=GeocodingError("provider down"))
    node = _node(resolution, area_service=areas)
    update = await node(_state())
    assert update["working_location"] is None
    assert update["location_clarification"]
    # A geocode failure is not an agent error — it must not bump error_count.
    assert "error_count" not in update


async def test_llm_failure_fails_toward_clarification() -> None:
    node = _node(_resolver_llm_error())
    update = await node(_state())
    assert update["working_location"] is None
    assert update["location_clarification"]


def _resolver_llm_error() -> Exception:
    return RuntimeError("llm unreachable")


async def test_emits_user_visible_reasoning_step() -> None:
    resolution = LocationResolution(
        source="explicit_query", country="Japan", city="Tokyo", neighborhood="Shibuya"
    )
    node = _node(resolution, area_service=_area_service(forward=(35.6, 139.7)))
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
    node = _node(resolution, area_service=_area_service(forward=(35.66, 139.70)))
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
    node = _node(resolution, area_service=_area_service(forward=(12.93, 100.88)))
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
    node = _node(resolution, area_service=_area_service(forward=(13.73, 100.58)))
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
    node = _node(resolution, area_service=_area_service(forward=(13.73, 100.58)))
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
    node = _node(resolution, area_service=_area_service(forward=(13.73, 100.58)))
    update = await node(_state(movement_profile=None))
    assert (
        update["working_location"]["effective_mode"]
        == get_config().movement.fallback.available_modes[0]
    )


async def test_corridor_poi_destination_falls_back_to_coords_lookup() -> None:
    """A POI destination (an airport) refuses the area round-trip and falls
    back to one coords-only geocode — an endpoint coordinate, not an
    identity, so nothing is persisted."""
    resolution = LocationResolution(
        source="user_actual",
        scope_shape="corridor",
        corridor_destinations=["Suvarnabhumi Airport"],
    )
    geocoder = _geocoder(
        reverse={"country": "Thailand", "city": "Bangkok"},
        search=(13.69, 100.75),
    )
    node = _node(resolution, geocoder, _area_service(forward=None))
    update = await node(
        _state(
            message="a coffee place on my way to the airport",
            user_location={"lat": 13.75, "lng": 100.50},
        )
    )
    wl = update["working_location"]
    assert wl["scope_shape"] == "corridor"
    assert wl["corridor"]["stops"][0]["name"] == "Suvarnabhumi Airport"
    assert wl["corridor"]["stops"][0]["lat"] == 13.69
    assert update["location_clarification"] is None


async def test_corridor_area_destination_resolves_through_the_store() -> None:
    """A destination that IS an area ("Hue") resolves through the area
    service — verified within the working country and persisted, so both
    corridor endpoints are stored entities."""
    resolution = LocationResolution(
        source="user_actual",
        scope_shape="corridor",
        corridor_destinations=["Hue"],
    )
    geocoder = _geocoder(
        reverse={
            "country": "Vietnam",
            "city": "Da Nang",
            "country_code": "vn",
        },
    )
    geocoder.reverse.return_value = GeocodeResult(
        lat=16.05, lng=108.20, country="Vietnam", city="Da Nang", country_code="vn"
    )
    areas = _area_service(forward=(16.46, 107.59), country_code="vn")
    node = _node(resolution, geocoder, areas)
    update = await node(
        _state(
            message="stops on the way to Hue",
            user_location={"lat": 16.05, "lng": 108.20},
        )
    )
    wl = update["working_location"]
    assert wl["corridor"]["stops"][0]["name"] == "Hue"
    assert wl["corridor"]["stops"][0]["lat"] == 16.46
    areas.resolve_city.assert_awaited_with("Hue", "vn")
    # The coords-only fallback never ran — the area path answered.
    geocoder.search_area.assert_not_awaited()


async def test_corridor_unresolvable_destination_asks_not_area() -> None:
    """A corridor destination that resolves nowhere ("home") asks the user —
    it must NOT silently degrade to an area search around the current
    point."""
    resolution = LocationResolution(
        source="user_actual",
        scope_shape="corridor",
        corridor_destinations=["home"],
    )
    geocoder = _geocoder(
        reverse={"country": "Thailand", "city": "Bangkok"},
        search=None,  # the provider finds no match for "home"
    )
    node = _node(resolution, geocoder, _area_service(forward=None))
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
        corridor_destinations=[],
    )
    geocoder = _geocoder(reverse={"country": "Thailand", "city": "Bangkok"})
    node = _node(resolution, geocoder)
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
    dense = await _node(
        resolution,
        _geocoder(
            reverse={"country": "Thailand", "city": "Bangkok"}, place_type="city"
        ),
    )(_state(message="near me", user_location={"lat": 13.75, "lng": 100.50}))
    sparse = await _node(
        resolution,
        _geocoder(reverse={"country": "Thailand", "city": "Pai"}, place_type="village"),
    )(_state(message="near me", user_location={"lat": 19.36, "lng": 98.44}))
    assert dense["working_location"]["density"] == "dense"
    assert sparse["working_location"]["density"] == "sparse"
    assert (
        sparse["working_location"]["search_radius_m"]
        > dense["working_location"]["search_radius_m"]
    )


# ---------------------------------------------------------------------------
# Multi-stop routes (ADR-136)
# ---------------------------------------------------------------------------


def _multi_area_service(coords: dict[str, tuple[float, float]]) -> MagicMock:
    """AreaService that resolves only the named cities it was given.

    Anything else refuses, which is how an unresolvable stop reaches the
    all-or-nothing check.
    """
    svc = MagicMock()

    def _city(name: str, cc: str) -> AreaEntity | None:
        point = coords.get(name)
        if point is None:
            return None
        return AreaEntity(
            entity_key=f"{cc}/{name.lower().replace(' ', '-')}",
            entity_type="city",
            name=name,
            country_code=cc,
            lat=point[0],
            lng=point[1],
        )

    svc.resolve_country = AsyncMock(return_value=None)
    svc.resolve_city = AsyncMock(side_effect=_city)
    return svc


def _vietnam_geocoder() -> MagicMock:
    geocoder = _geocoder(reverse={"country": "Vietnam", "city": "Hanoi"})
    geocoder.reverse.return_value = GeocodeResult(
        lat=21.03, lng=105.83, country="Vietnam", city="Hanoi", country_code="vn"
    )
    return geocoder


async def test_multi_stop_route_resolves_every_stop_in_order() -> None:
    """A chain is one route with several stops — and the order the user said
    them IS the order of the journey."""
    resolution = LocationResolution(
        source="user_actual",
        scope_shape="corridor",
        corridor_destinations=["Hue", "Hoi An"],
    )
    areas = _multi_area_service({"Hue": (16.46, 107.59), "Hoi An": (15.88, 108.33)})
    node = _node(resolution, _vietnam_geocoder(), areas)
    update = await node(
        _state(
            message="Hanoi, then Hue, then Hoi An",
            user_location={"lat": 21.03, "lng": 105.83},
        )
    )
    stops = update["working_location"]["corridor"]["stops"]
    assert [s["name"] for s in stops] == ["Hue", "Hoi An"]
    assert stops[0]["lat"] == 16.46
    assert update["location_clarification"] is None


async def test_one_unresolvable_stop_asks_rather_than_dropping_it() -> None:
    """All-or-nothing: answering a route that quietly lost a stop answers a
    different question than the one the user asked."""
    resolution = LocationResolution(
        source="user_actual",
        scope_shape="corridor",
        corridor_destinations=["Hue", "somewhere imaginary"],
    )
    areas = _multi_area_service({"Hue": (16.46, 107.59)})
    geocoder = _vietnam_geocoder()
    geocoder.search_area = AsyncMock(return_value=None)
    node = _node(resolution, geocoder, areas)
    update = await node(
        _state(
            message="Hanoi, then Hue, then somewhere imaginary",
            user_location={"lat": 21.03, "lng": 105.83},
        )
    )
    assert update["working_location"] is None
    assert update["location_clarification"] == _CORRIDOR_ASK


async def test_stop_count_is_capped(monkeypatch: Any) -> None:
    """A runaway resolver list cannot fan out into unbounded geocodes."""
    resolution = LocationResolution(
        source="user_actual",
        scope_shape="corridor",
        corridor_destinations=[f"City {i}" for i in range(12)],
    )
    areas = _multi_area_service({f"City {i}": (16.0 + i, 107.0) for i in range(12)})
    node = _node(resolution, _vietnam_geocoder(), areas)
    update = await node(
        _state(message="a long list", user_location={"lat": 21.03, "lng": 105.83})
    )
    stops = update["working_location"]["corridor"]["stops"]
    assert len(stops) == get_config().movement.corridor.max_stops


async def test_corridor_endpoint_prefers_the_country_scoped_lookup() -> None:
    """The Saigon bug: qualifying a destination by the ORIGIN city misdirects
    the geocoder. "Saigon, Hanoi, Vietnam" comes back as a plus-code
    establishment on the edge of Hanoi — a 10 km route instead of a 1,100 km
    one, which then silently passes the length gate. Country scope first."""
    resolution = LocationResolution(
        source="user_actual",
        scope_shape="corridor",
        corridor_destinations=["Saigon"],
    )
    geocoder = _vietnam_geocoder()

    async def _search(*, query: str, region_code: str | None = None) -> Any:
        if "Hanoi" in query:  # the origin-qualified query — the wrong answer
            return GeocodeResult(
                lat=20.99, lng=105.78, name="XQXP+Q2X", place_type="establishment"
            )
        return GeocodeResult(
            lat=10.82, lng=106.63, name="Ho Chi Minh City", place_type="locality"
        )

    geocoder.search_area = AsyncMock(side_effect=_search)
    # The area store refuses: "Saigon" doesn't round-trip to "Ho Chi Minh City".
    node = _node(resolution, geocoder, _multi_area_service({}))
    update = await node(
        _state(
            message="road trip from Hanoi to Saigon",
            user_location={"lat": 21.03, "lng": 105.83},
        )
    )
    stop = update["working_location"]["corridor"]["stops"][0]
    assert stop["lat"] == 10.82


async def test_corridor_endpoint_falls_back_to_city_scope_for_a_local_poi() -> None:
    """A generic POI name only means something locally: "the airport, Vietnam"
    resolves to the country itself, so the city-qualified query must still
    run when the country-scoped one is not a settlement."""
    resolution = LocationResolution(
        source="user_actual",
        scope_shape="corridor",
        corridor_destinations=["the airport"],
    )
    geocoder = _vietnam_geocoder()

    async def _search(*, query: str, region_code: str | None = None) -> Any:
        if "Hanoi" in query:
            return GeocodeResult(
                lat=21.22, lng=105.80, name="Noi Bai Airport", place_type="airport"
            )
        return GeocodeResult(
            lat=14.06, lng=108.28, name="Vietnam", place_type="country"
        )

    geocoder.search_area = AsyncMock(side_effect=_search)
    node = _node(resolution, geocoder, _multi_area_service({}))
    update = await node(
        _state(
            message="coffee on the way to the airport",
            user_location={"lat": 21.03, "lng": 105.83},
        )
    )
    stop = update["working_location"]["corridor"]["stops"][0]
    assert stop["lat"] == 21.22
