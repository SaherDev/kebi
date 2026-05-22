"""Tests for NominatimGeocodingClient — forward + reverse + search geocoding."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from kebi.core.places.nominatim_geocoding_client import (
    GeocodeResult,
    GeocodingError,
    NominatimGeocodingClient,
)


def _client(json_payload: Any) -> tuple[NominatimGeocodingClient, MagicMock]:
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value=json_payload)
    http = MagicMock(spec=httpx.AsyncClient)
    http.get = AsyncMock(return_value=response)
    client = NominatimGeocodingClient(http=http, user_agent="kebi-test/1.0")
    return client, http


async def test_forward_parses_lat_lon() -> None:
    client, _ = _client([{"lat": "13.75", "lon": "100.5"}])
    result = await client.forward(country="Thailand", city="Bangkok")
    assert result is not None
    assert (result.lat, result.lng) == (13.75, 100.5)


async def test_forward_empty_result_returns_none() -> None:
    client, _ = _client([])
    assert await client.forward(country="Nowhere", city="Nowhere") is None


async def test_forward_sends_identifying_user_agent() -> None:
    client, http = _client([{"lat": "1", "lon": "2"}])
    await client.forward(country="Thailand", city="Bangkok", neighborhood="Thonglor")
    _, kwargs = http.get.call_args
    assert kwargs["headers"]["User-Agent"] == "kebi-test/1.0"


async def test_requests_english_place_names() -> None:
    """Every Nominatim call forces `accept-language=en` — otherwise names
    come back in the local script ("กรุงเทพมหานคร" rather than "Bangkok")."""
    client, http = _client([{"lat": "1", "lon": "2"}])
    await client.search(query="Bangkok")
    _, kwargs = http.get.call_args
    assert kwargs["params"]["accept-language"] == "en"


async def test_search_carries_place_type_and_bbox() -> None:
    """A search result reuses the place type (density proxy) and bounding box
    from the same Nominatim response — no extra call."""
    client, _ = _client(
        [
            {
                "lat": "13.75",
                "lon": "100.5",
                "addresstype": "city",
                "boundingbox": ["13.5", "14.0", "100.3", "100.9"],
            }
        ]
    )
    result = await client.search(query="Bangkok, Thailand")
    assert result == GeocodeResult(
        lat=13.75,
        lng=100.5,
        place_type="city",
        bbox=[13.5, 14.0, 100.3, 100.9],
    )


async def test_search_malformed_bbox_is_dropped() -> None:
    client, _ = _client([{"lat": "1", "lon": "2", "boundingbox": ["bad"]}])
    result = await client.search(query="x")
    assert result is not None
    assert result.bbox is None


async def test_reverse_extracts_components_with_fallbacks() -> None:
    client, _ = _client(
        {"address": {"country": "Thailand", "town": "Pattaya", "suburb": "Jomtien"}}
    )
    result = await client.reverse(lat=12.9, lng=100.9)
    assert result is not None
    assert result.country == "Thailand"
    assert result.city == "Pattaya"
    assert result.neighborhood == "Jomtien"
    # City name came from the `town` key → density proxy is "town".
    assert result.place_type == "town"


async def test_reverse_place_type_reflects_city_key() -> None:
    client, _ = _client({"address": {"country": "Thailand", "city": "Bangkok"}})
    result = await client.reverse(lat=13.7, lng=100.5)
    assert result is not None
    assert result.place_type == "city"


async def test_reverse_neighborhood_may_be_absent() -> None:
    client, _ = _client({"address": {"country": "Thailand", "city": "Bangkok"}})
    result = await client.reverse(lat=13.7, lng=100.5)
    assert result is not None
    assert result.neighborhood is None


async def test_reverse_missing_city_returns_none() -> None:
    client, _ = _client({"address": {"country": "Thailand"}})
    assert await client.reverse(lat=13.7, lng=100.5) is None


async def test_http_error_raises_geocoding_error() -> None:
    request = httpx.Request("GET", "https://nominatim.openstreetmap.org/search")
    err = httpx.HTTPStatusError(
        "rate limited",
        request=request,
        response=httpx.Response(429, request=request),
    )
    response = MagicMock()
    response.raise_for_status = MagicMock(side_effect=err)
    http = MagicMock(spec=httpx.AsyncClient)
    http.get = AsyncMock(return_value=response)
    client = NominatimGeocodingClient(http=http, user_agent="kebi-test/1.0")
    with pytest.raises(GeocodingError):
        await client.forward(country="Thailand", city="Bangkok")
