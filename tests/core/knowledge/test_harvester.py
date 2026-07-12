"""Tests for KnowledgeHarvester — verified claim resolution (ADR-121/126)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from kebi.core.knowledge.harvester import (
    KnowledgeHarvester,
    _HarvestedClaim,
    _HarvesterResponse,
)
from kebi.core.knowledge.schemas import HarvestContent, HarvestPlace, ResolvedGeo
from kebi.core.places.nominatim_geocoding_client import GeocodeResult

_PLACE = HarvestPlace(
    place_id="p1",
    name="Fuji Ramen",
    geo=ResolvedGeo(country_code="jp", city="Tokyo", neighborhood="Shibuya"),
)
_CONTENT = HarvestContent(caption="late-night ramen in Shibuya", source_ref="u")


def _harvester(
    claims: list[_HarvestedClaim], geocoder: AsyncMock | None = None
) -> KnowledgeHarvester:
    client = AsyncMock()
    client.extract = AsyncMock(return_value=_HarvesterResponse(claims=claims))
    if geocoder is None:
        geocoder = AsyncMock()
        geocoder.search_structured = AsyncMock(return_value=None)
    return KnowledgeHarvester(client, geocoder)


def _claim(scope: str, entity_name: str, **overrides: object) -> _HarvestedClaim:
    fields: dict[str, object] = {
        "scope": scope,
        "place_index": 0,
        "entity_name": entity_name,
        "claim": "x",
        "confidence": 0.7,
    }
    fields.update(overrides)
    return _HarvestedClaim.model_validate(fields)


async def test_place_claim_keys_to_place_ref() -> None:
    h = _harvester(
        [_claim("place", "Fuji Ramen", claim="known for tonkotsu", tags=["ramen"])]
    )
    out = await h.harvest(_CONTENT, [_PLACE])
    assert len(out) == 1
    assert out[0].scope == "place"
    assert out[0].place_ref == "p1"
    assert out[0].geo is None


async def test_place_claim_tolerates_name_suffix_variance() -> None:
    h = _harvester([_claim("place", "Fuji Ramen Shibuya")])
    out = await h.harvest(_CONTENT, [_PLACE])
    assert out[0].place_ref == "p1"


async def test_place_claim_naming_different_entity_dropped() -> None:
    # The mislabeled-town case: a "place" claim about Hội An anchored to a
    # venue must not bolt the town's fact onto the venue's id.
    h = _harvester([_claim("place", "Hội An")])
    assert await h.harvest(_CONTENT, [_PLACE]) == []


async def test_neighborhood_claim_matching_anchor_takes_place_geo() -> None:
    h = _harvester(
        [
            _claim(
                "neighborhood",
                "Shibuya",
                claim="dense late-night dining district",
                tags=["nightlife"],
                confidence=0.6,
            )
        ]
    )
    out = await h.harvest(_CONTENT, [_PLACE])
    assert out[0].scope == "neighborhood"
    assert out[0].place_ref is None
    assert out[0].geo == _PLACE.geo


async def test_neighborhood_claim_naming_other_area_dropped() -> None:
    h = _harvester([_claim("neighborhood", "Golden Gai")])
    assert await h.harvest(_CONTENT, [_PLACE]) == []


async def test_city_claim_matching_anchor_skips_geocoder() -> None:
    geocoder = AsyncMock()
    geocoder.search_structured = AsyncMock()
    # Diacritic/script variance still counts as the anchor's own city.
    h = _harvester([_claim("city", "tōkyō")], geocoder)
    out = await h.harvest(_CONTENT, [_PLACE])
    assert out[0].geo == _PLACE.geo
    geocoder.search_structured.assert_not_called()


async def test_city_claim_naming_other_city_resolved_and_rekeyed() -> None:
    geocoder = AsyncMock()
    geocoder.search_structured = AsyncMock(
        return_value=GeocodeResult(lat=34.9, lng=135.7, country_code="jp", city="Kyoto")
    )
    h = _harvester([_claim("city", "Kyoto")], geocoder)
    out = await h.harvest(_CONTENT, [_PLACE])
    assert out[0].geo == ResolvedGeo(country_code="jp", city="Kyoto")
    geocoder.search_structured.assert_awaited_once_with(city="Kyoto", countrycodes="jp")


async def test_city_claim_failing_round_trip_dropped() -> None:
    # Top hit is some feature *containing* the name in a different city —
    # the returned city component doesn't match, so the claim drops.
    geocoder = AsyncMock()
    geocoder.search_structured = AsyncMock(
        return_value=GeocodeResult(lat=16.4, lng=107.5, country_code="vn", city="Huế")
    )
    h = _harvester([_claim("city", "Paris")], geocoder)
    assert await h.harvest(_CONTENT, [_PLACE]) == []


async def test_city_claim_unresolvable_dropped() -> None:
    h = _harvester([_claim("city", "Muine")])  # default geocoder returns None
    assert await h.harvest(_CONTENT, [_PLACE]) == []


async def test_city_claim_geocoder_error_dropped_not_raised() -> None:
    geocoder = AsyncMock()
    geocoder.search_structured = AsyncMock(side_effect=RuntimeError("down"))
    h = _harvester([_claim("city", "Kyoto")], geocoder)
    assert await h.harvest(_CONTENT, [_PLACE]) == []


async def test_country_claim_resolved_by_name() -> None:
    geocoder = AsyncMock()
    geocoder.search_structured = AsyncMock(
        return_value=GeocodeResult(
            lat=36.0, lng=138.0, country_code="jp", place_type="country"
        )
    )
    h = _harvester([_claim("country", "Japan")], geocoder)
    out = await h.harvest(_CONTENT, [_PLACE])
    assert out[0].geo == ResolvedGeo(country_code="jp")
    geocoder.search_structured.assert_awaited_once_with(country="Japan")


async def test_country_claim_matching_non_country_feature_dropped() -> None:
    geocoder = AsyncMock()
    geocoder.search_structured = AsyncMock(
        return_value=GeocodeResult(
            lat=35.6, lng=139.7, country_code="jp", place_type="restaurant"
        )
    )
    h = _harvester([_claim("country", "Japan Curry House")], geocoder)
    assert await h.harvest(_CONTENT, [_PLACE]) == []


async def test_repeated_entity_names_geocoded_once() -> None:
    geocoder = AsyncMock()
    geocoder.search_structured = AsyncMock(
        return_value=GeocodeResult(lat=34.9, lng=135.7, country_code="jp", city="Kyoto")
    )
    h = _harvester(
        [_claim("city", "Kyoto"), _claim("city", "Kyōto", claim="y")], geocoder
    )
    out = await h.harvest(_CONTENT, [_PLACE])
    assert len(out) == 2
    geocoder.search_structured.assert_awaited_once()


async def test_out_of_range_place_index_dropped() -> None:
    h = _harvester([_claim("place", "?", place_index=5, confidence=0.5)])
    assert await h.harvest(_CONTENT, [_PLACE]) == []


async def test_empty_content_short_circuits_without_llm() -> None:
    client = AsyncMock()
    client.extract = AsyncMock()
    h = KnowledgeHarvester(client, AsyncMock())
    out = await h.harvest(HarvestContent(), [_PLACE])
    assert out == []
    client.extract.assert_not_called()


async def test_no_places_short_circuits() -> None:
    client = AsyncMock()
    client.extract = AsyncMock()
    out = await KnowledgeHarvester(client, AsyncMock()).harvest(_CONTENT, [])
    assert out == []
    client.extract.assert_not_called()


async def test_llm_error_swallowed() -> None:
    client = AsyncMock()
    client.extract = AsyncMock(side_effect=RuntimeError("boom"))
    out = await KnowledgeHarvester(client, AsyncMock()).harvest(_CONTENT, [_PLACE])
    assert out == []
