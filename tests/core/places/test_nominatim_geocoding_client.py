"""Tests for NominatimGeocodingClient — forward + reverse geocoding."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from kebi.core.places.nominatim_geocoding_client import (
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
    assert await client.forward(country="Thailand", city="Bangkok") == (13.75, 100.5)


async def test_forward_empty_result_returns_none() -> None:
    client, _ = _client([])
    assert await client.forward(country="Nowhere", city="Nowhere") is None


async def test_forward_sends_identifying_user_agent() -> None:
    client, http = _client([{"lat": "1", "lon": "2"}])
    await client.forward(country="Thailand", city="Bangkok", neighborhood="Thonglor")
    _, kwargs = http.get.call_args
    assert kwargs["headers"]["User-Agent"] == "kebi-test/1.0"


async def test_reverse_extracts_components_with_fallbacks() -> None:
    client, _ = _client(
        {"address": {"country": "Thailand", "town": "Pattaya", "suburb": "Jomtien"}}
    )
    result = await client.reverse(lat=12.9, lng=100.9)
    assert result == {
        "country": "Thailand",
        "city": "Pattaya",
        "neighborhood": "Jomtien",
    }


async def test_reverse_neighborhood_may_be_absent() -> None:
    client, _ = _client({"address": {"country": "Thailand", "city": "Bangkok"}})
    result = await client.reverse(lat=13.7, lng=100.5)
    assert result == {"country": "Thailand", "city": "Bangkok"}


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
