"""Tests for EntityGeoResolver + slugs_match — verified geo lookup (ADR-126)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from kebi.core.knowledge.geo_resolve import EntityGeoResolver, slugs_match
from kebi.core.knowledge.schemas import ResolvedGeo
from kebi.core.places.nominatim_geocoding_client import GeocodeResult


def _resolver(result: GeocodeResult | None) -> tuple[EntityGeoResolver, AsyncMock]:
    geocoder = AsyncMock()
    geocoder.search_structured = AsyncMock(return_value=result)
    return EntityGeoResolver(geocoder), geocoder


def test_slugs_match_folds_diacritics_and_scripts() -> None:
    assert slugs_match("Hội An", "Hoi An")
    assert slugs_match("Đà Nẵng", "da nang")
    assert slugs_match("Tōkyō", "Tokyo")
    assert not slugs_match("Hội An", "Đà Nẵng")
    assert not slugs_match("", "Hoi An")
    assert not slugs_match(None, "Hoi An")


async def test_resolve_city_verified_round_trip() -> None:
    resolver, geocoder = _resolver(
        GeocodeResult(lat=15.88, lng=108.33, country_code="vn", city="Hoi An")
    )
    geo = await resolver.resolve_city("Hội An", "vn")
    assert geo == ResolvedGeo(country_code="vn", city="Hoi An")
    geocoder.search_structured.assert_awaited_once_with(
        city="Hội An", countrycodes="vn"
    )


async def test_resolve_city_rejects_round_trip_mismatch() -> None:
    # Nominatim's top hit for "Paris" in vn is a feature in Huế — the city
    # component that would form the key doesn't match the asked-for name.
    resolver, _ = _resolver(
        GeocodeResult(lat=16.46, lng=107.59, country_code="vn", city="Huế")
    )
    assert await resolver.resolve_city("Paris", "vn") is None


async def test_resolve_city_requires_city_component() -> None:
    # "Mui Ne" resolves to a suburb with no city — unkeyable, so None.
    resolver, _ = _resolver(GeocodeResult(lat=10.9, lng=108.2, country_code="vn"))
    assert await resolver.resolve_city("Mui Ne", "vn") is None


async def test_resolve_city_no_result_returns_none() -> None:
    resolver, _ = _resolver(None)
    assert await resolver.resolve_city("Muine", "vn") is None


async def test_resolve_country_requires_country_feature() -> None:
    resolver, geocoder = _resolver(
        GeocodeResult(lat=16.0, lng=106.0, country_code="vn", place_type="country")
    )
    assert await resolver.resolve_country("Vietnam") == ResolvedGeo(country_code="vn")
    geocoder.search_structured.assert_awaited_once_with(country="Vietnam")


async def test_resolve_country_rejects_non_country_feature() -> None:
    resolver, _ = _resolver(
        GeocodeResult(lat=1.0, lng=2.0, country_code="jp", place_type="restaurant")
    )
    assert await resolver.resolve_country("Japan Curry House") is None


async def test_lookups_memoized_across_spellings() -> None:
    resolver, geocoder = _resolver(
        GeocodeResult(lat=15.88, lng=108.33, country_code="vn", city="Hoi An")
    )
    first = await resolver.resolve_city("Hội An", "vn")
    second = await resolver.resolve_city("Hoi An", "vn")  # same slug → cached
    assert first == second
    geocoder.search_structured.assert_awaited_once()


async def test_failed_lookup_memoized() -> None:
    resolver, geocoder = _resolver(None)
    assert await resolver.resolve_city("Muine", "vn") is None
    assert await resolver.resolve_city("Muine", "vn") is None
    geocoder.search_structured.assert_awaited_once()


async def test_geocoder_exception_swallowed() -> None:
    geocoder = AsyncMock()
    geocoder.search_structured = AsyncMock(side_effect=RuntimeError("down"))
    resolver = EntityGeoResolver(geocoder)
    assert await resolver.resolve_city("Kyoto", "jp") is None
    assert await resolver.resolve_country("Japan") is None
