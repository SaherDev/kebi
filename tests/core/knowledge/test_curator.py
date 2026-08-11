"""Tests for KnowledgeCurator — prose structuring + geo resolution (ADR-121)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from kebi.core.knowledge.curator import (
    KnowledgeCurator,
    _CuratedClaim,
    _CuratorResponse,
)
from kebi.core.knowledge.schemas import CurationAnchor, ResolvedGeo
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

_PLACE_CLAIM = _CuratedClaim(
    scope="place",
    entity_name="Beach Club X",
    claim="cash only at the bar",
    tags=["money"],
    confidence=0.9,
)

_VENUE_ANCHOR = CurationAnchor(
    place_id="place-123",
    name="Beach Club X",
    geo=ResolvedGeo(country_code="ae", city="Dubai", neighborhood="Jumeirah"),
)

_AREA_ANCHOR = CurationAnchor(
    place_id=None,
    name="Jumeirah",
    geo=ResolvedGeo(country_code="ae", city="Dubai", neighborhood="Jumeirah"),
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


async def test_falls_back_to_anchor_geo_when_geocode_fails() -> None:
    curator, _ = _curator([_CLAIM], geocode=None)  # geocoder finds nothing
    out = await curator.structure("prose", _AREA_ANCHOR)
    assert len(out) == 1
    assert out[0].geo.country_code == "ae"
    assert out[0].geo.city == "Dubai"


async def test_dropped_when_no_geocode_and_no_anchor() -> None:
    curator, _ = _curator([_CLAIM], geocode=None)
    assert await curator.structure("prose") == []


async def test_place_claim_keyed_to_venue_anchor() -> None:
    curator, geocoder = _curator([_PLACE_CLAIM], geocode=None)
    out = await curator.structure("prose", _VENUE_ANCHOR)
    assert len(out) == 1
    assert out[0].scope == "place"
    assert out[0].place_ref == "place-123"
    assert out[0].entity_name == "Beach Club X"
    geocoder.search.assert_not_awaited()  # nothing to geocode for the venue


async def test_place_claim_dropped_without_venue_anchor() -> None:
    curator, _ = _curator([_PLACE_CLAIM], geocode=None)
    assert await curator.structure("prose") == []


async def test_place_claim_dropped_under_area_anchor() -> None:
    curator, _ = _curator([_PLACE_CLAIM], geocode=None)
    assert await curator.structure("prose", _AREA_ANCHOR) == []


async def test_claim_naming_anchor_takes_anchor_geo_over_geocode() -> None:
    # Live failure this pins: geocoding "Canggu" free-text returned its
    # district (city="North Kuta", no neighborhood) — a successful-but-wrong
    # answer that beat the fallback and then dropped at the writer. A claim
    # about the anchor itself must key to the anchor, never re-geocode.
    wrong = GeocodeResult(lat=-8.6, lng=115.1, country_code="id", city="North Kuta")
    claim = _CuratedClaim(
        scope="neighborhood",
        entity_name="Canggu",
        area_query="Canggu, Bali, Indonesia",
        claim="rent a scooter; taxis are scarce",
        tags=["transport"],
        confidence=0.9,
    )
    anchor = CurationAnchor(
        place_id=None,
        name="Canggu",
        geo=ResolvedGeo(country_code="id", city="bali", neighborhood="canggu"),
    )
    curator, geocoder = _curator([claim], wrong)
    out = await curator.structure("prose", anchor)
    assert len(out) == 1
    assert out[0].geo.city == "bali"
    assert out[0].geo.neighborhood == "canggu"
    geocoder.search.assert_not_awaited()


async def test_empty_area_query_under_anchor_means_here() -> None:
    claim = _CuratedClaim(
        scope="neighborhood",
        entity_name="Canggu",
        area_query="",
        claim="gojek pickups get blocked from gated streets",
        confidence=0.8,
    )
    curator, geocoder = _curator([claim], geocode=None)
    out = await curator.structure("prose", _AREA_ANCHOR)
    assert len(out) == 1
    assert out[0].geo == _AREA_ANCHOR.geo
    geocoder.search.assert_not_awaited()


async def test_geo_spillover_under_venue_anchor() -> None:
    # An area-level remark in venue-anchored prose still lands geo-scoped,
    # falling back to the venue's own location when the geocoder has nothing.
    curator, _ = _curator([_PLACE_CLAIM, _CLAIM], geocode=None)
    out = await curator.structure("prose", _VENUE_ANCHOR)
    assert [c.scope for c in out] == ["place", "neighborhood"]
    assert out[1].geo.country_code == "ae"


async def test_anchor_line_rendered_into_system_prompt() -> None:
    curator, _ = _curator([], geocode=None)
    await curator.structure("prose", _VENUE_ANCHOR)
    system = curator._client.extract.await_args.kwargs["messages"][0]["content"]
    assert 'the venue "Beach Club X"' in system
    assert "ANCHORED REQUEST" in system


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
