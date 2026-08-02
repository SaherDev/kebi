"""Tests for GoogleGeocoder — Geocoding API response mapping."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kebi.providers.geocoding import GeocodingError, GoogleGeocoder


def _response(payload: dict[str, Any]) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload)
    return resp


def _geocoder(payload: dict[str, Any]) -> tuple[GoogleGeocoder, MagicMock]:
    http = MagicMock()
    http.get = AsyncMock(return_value=_response(payload))
    return GoogleGeocoder(api_key="k", http=http), http


_HOI_AN = {
    "status": "OK",
    "results": [
        {
            "place_id": "ChIJhoian",
            "types": ["locality", "political"],
            "address_components": [
                {
                    "long_name": "Hoi An",
                    "short_name": "Hoi An",
                    "types": ["locality", "political"],
                },
                {
                    "long_name": "Quảng Nam",
                    "short_name": "Quảng Nam",
                    "types": ["administrative_area_level_1", "political"],
                },
                {
                    "long_name": "Vietnam",
                    "short_name": "VN",
                    "types": ["country", "political"],
                },
            ],
            "geometry": {
                "location": {"lat": 15.88, "lng": 108.33},
                "bounds": {
                    "southwest": {"lat": 15.8, "lng": 108.2},
                    "northeast": {"lat": 15.95, "lng": 108.4},
                },
            },
        }
    ],
}


async def test_forward_maps_components_geometry_and_identity() -> None:
    geocoder, http = _geocoder(_HOI_AN)
    result = await geocoder.search_area(query="Hoi An", region_code="vn")
    assert result is not None
    assert result.name == "Hoi An"
    assert result.city == "Hoi An"
    assert result.country == "Vietnam"
    assert result.country_code == "vn"
    assert result.place_type == "locality"
    assert result.lat == 15.88 and result.lng == 108.33
    # bounds → [min_lat, max_lat, min_lng, max_lng]
    assert result.bbox == [15.8, 15.95, 108.2, 108.4]
    assert result.provider_id == "google:ChIJhoian"
    # Region constraint travels as a hard components filter.
    params = http.get.await_args.kwargs["params"]
    assert params["components"] == "country:VN"
    assert params["language"] == "en"


async def test_forward_without_region_omits_components_filter() -> None:
    geocoder, http = _geocoder(_HOI_AN)
    await geocoder.search_area(query="Hoi An")
    assert "components" not in http.get.await_args.kwargs["params"]


async def test_zero_results_returns_none_not_error() -> None:
    geocoder, _ = _geocoder({"status": "ZERO_RESULTS", "results": []})
    assert await geocoder.search_area(query="nowhere") is None


async def test_request_denied_raises_geocoding_error() -> None:
    geocoder, _ = _geocoder({"status": "REQUEST_DENIED", "results": []})
    with pytest.raises(GeocodingError):
        await geocoder.search_area(query="Hoi An")


async def test_place_id_lookup_strips_namespace_prefix() -> None:
    geocoder, http = _geocoder(_HOI_AN)
    result = await geocoder.geocode_place_id("google:ChIJhoian")
    assert result is not None
    assert http.get.await_args.kwargs["params"]["place_id"] == "ChIJhoian"


async def test_reverse_maps_legacy_components_and_echoes_input_coords() -> None:
    geocoder, _ = _geocoder(
        {
            "status": "OK",
            "results": [
                {
                    "place_id": "ChIJaddr",
                    "types": ["street_address"],
                    "address_components": [
                        {
                            "long_name": "123",
                            "short_name": "123",
                            "types": ["street_number"],
                        },
                        {
                            "long_name": "Shibuya",
                            "short_name": "Shibuya",
                            "types": ["sublocality_level_1", "political"],
                        },
                        {
                            "long_name": "Tokyo",
                            "short_name": "Tokyo",
                            "types": ["locality", "political"],
                        },
                        {
                            "long_name": "Japan",
                            "short_name": "JP",
                            "types": ["country", "political"],
                        },
                    ],
                    "geometry": {"location": {"lat": 35.001, "lng": 139.001}},
                }
            ],
        }
    )
    result = await geocoder.reverse(lat=35.66, lng=139.70)
    assert result is not None
    # The caller's GPS is canonical — never the matched address's coords.
    assert result.lat == 35.66 and result.lng == 139.70
    assert result.city == "Tokyo"
    assert result.neighborhood == "Shibuya"
    assert result.country == "Japan"
    assert result.country_code == "jp"
    # Google has no settlement-size signal — no density proxy on reverse.
    assert result.place_type is None


async def test_reverse_without_city_returns_none() -> None:
    geocoder, _ = _geocoder(
        {
            "status": "OK",
            "results": [
                {
                    "place_id": "x",
                    "types": ["plus_code"],
                    "address_components": [
                        {
                            "long_name": "Vietnam",
                            "short_name": "VN",
                            "types": ["country", "political"],
                        }
                    ],
                    "geometry": {"location": {"lat": 1, "lng": 1}},
                }
            ],
        }
    )
    assert await geocoder.reverse(lat=1.0, lng=1.0) is None


async def test_municipality_city_falls_back_to_admin_level_1() -> None:
    """ADR-119 ranking survives the legacy casing: Đà Nẵng arrives as
    administrative_area_level_1 with no locality — it still becomes city."""
    geocoder, _ = _geocoder(
        {
            "status": "OK",
            "results": [
                {
                    "place_id": "ChIJdn",
                    "types": ["administrative_area_level_1", "political"],
                    "address_components": [
                        {
                            "long_name": "Da Nang",
                            "short_name": "Da Nang",
                            "types": ["administrative_area_level_1", "political"],
                        },
                        {
                            "long_name": "Vietnam",
                            "short_name": "VN",
                            "types": ["country", "political"],
                        },
                    ],
                    "geometry": {"location": {"lat": 16.05, "lng": 108.2}},
                }
            ],
        }
    )
    result = await geocoder.search_area(query="Da Nang", region_code="vn")
    assert result is not None
    assert result.city == "Da Nang"
    assert result.place_type == "administrative_area_level_1"
