"""Tests for KnowledgeHarvester — resolution of model output (ADR-121)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from kebi.core.knowledge.harvester import (
    KnowledgeHarvester,
    _HarvestedClaim,
    _HarvesterResponse,
)
from kebi.core.knowledge.schemas import HarvestContent, HarvestPlace, ResolvedGeo

_PLACE = HarvestPlace(
    place_id="p1",
    name="Fuji Ramen",
    geo=ResolvedGeo(country_code="jp", city="Tokyo", neighborhood="Shibuya"),
)
_CONTENT = HarvestContent(caption="late-night ramen in Shibuya", source_ref="u")


def _harvester(claims: list[_HarvestedClaim]) -> KnowledgeHarvester:
    client = AsyncMock()
    client.extract = AsyncMock(return_value=_HarvesterResponse(claims=claims))
    return KnowledgeHarvester(client)


async def test_place_claim_keys_to_place_ref() -> None:
    h = _harvester(
        [
            _HarvestedClaim(
                scope="place",
                place_index=0,
                entity_name="Fuji Ramen",
                claim="known for tonkotsu",
                tags=["ramen"],
                confidence=0.7,
            )
        ]
    )
    out = await h.harvest(_CONTENT, [_PLACE])
    assert len(out) == 1
    assert out[0].scope == "place"
    assert out[0].place_ref == "p1"
    assert out[0].geo is None


async def test_geo_claim_takes_place_geo() -> None:
    h = _harvester(
        [
            _HarvestedClaim(
                scope="neighborhood",
                place_index=0,
                entity_name="Shibuya",
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


async def test_out_of_range_place_index_dropped() -> None:
    h = _harvester(
        [
            _HarvestedClaim(
                scope="place",
                place_index=5,
                entity_name="?",
                claim="x",
                confidence=0.5,
            )
        ]
    )
    assert await h.harvest(_CONTENT, [_PLACE]) == []


async def test_empty_content_short_circuits_without_llm() -> None:
    client = AsyncMock()
    client.extract = AsyncMock()
    h = KnowledgeHarvester(client)
    out = await h.harvest(HarvestContent(), [_PLACE])
    assert out == []
    client.extract.assert_not_called()


async def test_no_places_short_circuits() -> None:
    client = AsyncMock()
    client.extract = AsyncMock()
    out = await KnowledgeHarvester(client).harvest(_CONTENT, [])
    assert out == []
    client.extract.assert_not_called()


async def test_llm_error_swallowed() -> None:
    client = AsyncMock()
    client.extract = AsyncMock(side_effect=RuntimeError("boom"))
    out = await KnowledgeHarvester(client).harvest(_CONTENT, [_PLACE])
    assert out == []
