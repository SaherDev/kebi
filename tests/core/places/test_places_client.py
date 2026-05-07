"""Tests for GooglePlacesClient — reverse_geocode + validate_places post-filter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from kebi.core.places.models import PlaceType
from kebi.core.places.places_client import (
    GooglePlacesClient,
    PlacesMatchQuality,
    PlacesMatchResult,
    _haversine_km,
    google_types_to_place_type,
)


def _make_client() -> GooglePlacesClient:
    """Build a GooglePlacesClient bypassing env-var lookup."""
    client = GooglePlacesClient.__new__(GooglePlacesClient)
    client.api_key = "fake-key"
    return client


# ---------------------------------------------------------------------------
# Haversine helper
# ---------------------------------------------------------------------------


def test_haversine_zero_distance_for_same_point() -> None:
    assert _haversine_km(52.12, 11.62, 52.12, 11.62) == 0.0


def test_haversine_magdeburg_to_london_is_roughly_900km() -> None:
    # Magdeburg (52.12, 11.62) to London (51.51, -0.13) ≈ 810-850km
    km = _haversine_km(52.12, 11.62, 51.51, -0.13)
    assert 700.0 < km < 1000.0


# ---------------------------------------------------------------------------
# reverse_geocode
# ---------------------------------------------------------------------------


def _fake_geocode_response(
    locality: str | None,
    admin_area: str | None,
    country: str | None,
    status: str = "OK",
) -> dict:
    components: list[dict] = []
    if locality:
        components.append({"long_name": locality, "types": ["locality", "political"]})
    if admin_area:
        components.append(
            {
                "long_name": admin_area,
                "types": ["administrative_area_level_1", "political"],
            }
        )
    if country:
        components.append({"long_name": country, "types": ["country", "political"]})
    return {
        "status": status,
        "results": [{"address_components": components}] if components else [],
    }


def _patch_httpx_get(json_payload: dict) -> MagicMock:
    """Mock chain for `async with httpx.AsyncClient() as c: c.get(...)`."""
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value=json_payload)

    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


async def test_reverse_geocode_returns_city_country_on_match() -> None:
    payload = _fake_geocode_response("Magdeburg", "Saxony-Anhalt", "Germany")
    client = _make_client()

    with patch(
        "kebi.core.places.places_client.httpx.AsyncClient",
        return_value=_patch_httpx_get(payload),
    ):
        label = await client.reverse_geocode(52.12, 11.62)

    assert label == "Magdeburg, Germany"


async def test_reverse_geocode_falls_back_to_admin_area_when_no_locality() -> None:
    """Rural coords often lack a locality — admin_area_level_1 is the fallback."""
    payload = _fake_geocode_response(None, "Bavaria", "Germany")
    client = _make_client()

    with patch(
        "kebi.core.places.places_client.httpx.AsyncClient",
        return_value=_patch_httpx_get(payload),
    ):
        label = await client.reverse_geocode(49.5, 11.0)

    assert label == "Bavaria, Germany"


async def test_reverse_geocode_returns_none_on_zero_results() -> None:
    payload = {"status": "ZERO_RESULTS", "results": []}
    client = _make_client()

    with patch(
        "kebi.core.places.places_client.httpx.AsyncClient",
        return_value=_patch_httpx_get(payload),
    ):
        label = await client.reverse_geocode(0.0, 0.0)

    assert label is None


async def test_reverse_geocode_returns_none_on_http_error() -> None:
    import httpx

    response = MagicMock()
    response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPError("boom")
    )
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    client = _make_client()
    with patch(
        "kebi.core.places.places_client.httpx.AsyncClient",
        return_value=mock_client,
    ):
        label = await client.reverse_geocode(52.12, 11.62)

    assert label is None


# ---------------------------------------------------------------------------
# validate_places post-filter
# ---------------------------------------------------------------------------


async def test_validate_places_drops_results_outside_50km_of_bias() -> None:
    """Google's locationbias is a soft hint — post-filter drops far results."""
    client = _make_client()

    # Mock validate_place to return 2 candidates: one near Magdeburg, one in London.
    async def _fake_validate(name: str, **_: object) -> PlacesMatchResult:
        if "kazoku" in name.lower():
            return PlacesMatchResult(
                match_quality=PlacesMatchQuality.EXACT,
                validated_name="Restaurant Kazoku",
                external_id="g:near",
                lat=52.12,
                lng=11.62,  # Magdeburg
                address="Otto-von-Guericke-Str, Magdeburg",
            )
        return PlacesMatchResult(
            match_quality=PlacesMatchQuality.EXACT,
            validated_name="Menya Ramen House",
            external_id="g:far",
            lat=51.51,
            lng=-0.13,  # London
            address="29 Museum St, London",
        )

    client.validate_place = AsyncMock(side_effect=_fake_validate)  # type: ignore[method-assign]

    results = await client.validate_places(
        ["Restaurant Kazoku", "Menya Ramen House"],
        location_bias={"lat": 52.12, "lng": 11.62},  # bias toward Magdeburg
    )

    # London result dropped (>800km away), Magdeburg result kept.
    assert len(results) == 1
    assert "Kazoku" in (results[0].place_name or "")


async def test_validate_places_no_filter_when_location_bias_absent() -> None:
    """Without location_bias, all validated results survive."""
    client = _make_client()

    async def _fake_validate(name: str, **_: object) -> PlacesMatchResult:
        return PlacesMatchResult(
            match_quality=PlacesMatchQuality.EXACT,
            validated_name=name,
            external_id=f"g:{name}",
            lat=52.12,
            lng=11.62,
            address=f"Address for {name}",
        )

    client.validate_place = AsyncMock(side_effect=_fake_validate)  # type: ignore[method-assign]

    results = await client.validate_places(["A", "B", "C"], location_bias=None)

    assert len(results) == 3


# ---------------------------------------------------------------------------
# google_types_to_place_type + validate_places place_type derivation
# ---------------------------------------------------------------------------


def test_google_types_restaurant_maps_to_food_and_drink() -> None:
    assert google_types_to_place_type(["restaurant", "food", "establishment"]) == (
        PlaceType.food_and_drink
    )


def test_google_types_lodging_maps_to_accommodation() -> None:
    assert (
        google_types_to_place_type(["lodging", "establishment"])
        == PlaceType.accommodation
    )


def test_google_types_unknown_falls_back_to_services() -> None:
    """No known type → defaults to services (with a coverage-gap log)."""
    assert google_types_to_place_type(["fictional_type"]) == PlaceType.services


def test_google_types_first_known_type_wins() -> None:
    """Order matters: a venue tagged restaurant + store lands as food."""
    assert (
        google_types_to_place_type(["restaurant", "store"])
        == PlaceType.food_and_drink
    )


async def test_validate_places_derives_place_type_from_google_types() -> None:
    """Validated PlaceObjects must carry the derived place_type, not a
    hardcoded `services` fallback (the wrench-icon bug)."""
    client = _make_client()

    async def _fake_validate(name: str, **_: object) -> PlacesMatchResult:
        return PlacesMatchResult(
            match_quality=PlacesMatchQuality.EXACT,
            validated_name=name,
            external_id=f"g:{name}",
            lat=52.12,
            lng=11.62,
            address=f"{name} address",
            place_types=["restaurant", "food", "establishment"],
        )

    client.validate_place = AsyncMock(side_effect=_fake_validate)  # type: ignore[method-assign]

    results = await client.validate_places(["Zum Domfelsen"], location_bias=None)

    assert len(results) == 1
    assert results[0].place_type == PlaceType.food_and_drink
