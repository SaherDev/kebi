"""Tests for KnowledgeCurator — prose structuring + geo resolution (ADR-121)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from kebi.core.knowledge.curator import (
    KnowledgeCurator,
    LocationHint,
    _CuratedClaim,
    _CuratorResponse,
)
from kebi.core.places.nominatim_geocoding_client import GeocodeResult


def _curator(claims: list[_CuratedClaim], geocode: GeocodeResult | None):
    client = AsyncMock()
    client.extract = AsyncMock(return_value=_CuratorResponse(claims=claims))
    geocoder = AsyncMock()
    geocoder.search = AsyncMock(return_value=geocode)
    return KnowledgeCurator(client, geocoder), geocoder


_CLAIM = _CuratedClaim(
    scope="neighborhood",
    entity_name="Jumeirah",
    area_query="Jumeirah, Dubai, United Arab Emirates",
    claim="beach clubs with sunset views",
    tags=["nightlife"],
    confidence=0.8,
)


async def test_resolves_area_via_geocoder() -> None:
    geocode = GeocodeResult(
        lat=25.2, lng=55.2, country_code="ae", city="Dubai", neighborhood="Jumeirah"
    )
    curator, _ = _curator([_CLAIM], geocode)
    out = await curator.structure("prose")
    assert len(out) == 1
    assert out[0].geo is not None
    assert out[0].geo.country_code == "ae"
    assert out[0].geo.city == "Dubai"


async def test_falls_back_to_hint_when_geocode_fails() -> None:
    curator, _ = _curator([_CLAIM], geocode=None)  # geocoder finds nothing
    out = await curator.structure(
        "prose", LocationHint(country_alpha2="AE", city="Dubai")
    )
    assert len(out) == 1
    assert out[0].geo.country_code == "ae"  # hint lowercased


async def test_dropped_when_no_geocode_and_no_hint() -> None:
    curator, _ = _curator([_CLAIM], geocode=None)
    assert await curator.structure("prose") == []


async def test_distinct_area_geocoded_once() -> None:
    geocode = GeocodeResult(lat=25.2, lng=55.2, country_code="ae", city="Dubai")
    curator, geocoder = _curator([_CLAIM, _CLAIM], geocode)
    await curator.structure("prose")
    geocoder.search.assert_awaited_once()  # memoized per identical area_query


async def test_empty_text_short_circuits() -> None:
    client = AsyncMock()
    client.extract = AsyncMock()
    geocoder = AsyncMock()
    out = await KnowledgeCurator(client, geocoder).structure("   ")
    assert out == []
    client.extract.assert_not_called()


async def test_llm_error_swallowed() -> None:
    client = AsyncMock()
    client.extract = AsyncMock(side_effect=RuntimeError("boom"))
    geocoder = AsyncMock()
    out = await KnowledgeCurator(client, geocoder).structure("prose")
    assert out == []
