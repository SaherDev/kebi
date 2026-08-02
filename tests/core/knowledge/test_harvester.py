"""Tests for KnowledgeHarvester — verified claim resolution (ADR-121/126).

Round-trip verification itself lives in (and is tested with) the
AreaService; these tests fake it and assert the harvester's keying rules:
anchor-supplied keys, cross-anchor resolution through the service, and
drop-don't-mis-key on refusal.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from kebi.core.areas.models import AreaEntity
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


def _city_entity(name: str, cc: str) -> AreaEntity:
    return AreaEntity(
        entity_key=f"{cc}/{name.lower()}",
        entity_type="city",
        name=name,
        country_code=cc,
        lat=34.9,
        lng=135.7,
    )


def _country_entity(cc: str) -> AreaEntity:
    return AreaEntity(
        entity_key=cc,
        entity_type="country",
        name=cc.upper(),
        country_code=cc,
        lat=36.0,
        lng=138.0,
    )


def _areas(
    city: AreaEntity | None = None, country: AreaEntity | None = None
) -> AsyncMock:
    svc = AsyncMock()
    svc.resolve_city = AsyncMock(return_value=city)
    svc.resolve_country = AsyncMock(return_value=country)
    return svc


def _harvester(
    claims: list[_HarvestedClaim], areas: AsyncMock | None = None
) -> KnowledgeHarvester:
    client = AsyncMock()
    client.extract = AsyncMock(return_value=_HarvesterResponse(claims=claims))
    return KnowledgeHarvester(client, areas if areas is not None else _areas())


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


async def test_city_claim_matching_anchor_skips_area_service() -> None:
    areas = _areas()
    # Diacritic/script variance still counts as the anchor's own city.
    h = _harvester([_claim("city", "tōkyō")], areas)
    out = await h.harvest(_CONTENT, [_PLACE])
    assert out[0].geo == _PLACE.geo
    areas.resolve_city.assert_not_called()


async def test_city_claim_naming_other_city_resolved_and_rekeyed() -> None:
    areas = _areas(city=_city_entity("Kyoto", "jp"))
    h = _harvester([_claim("city", "Kyoto")], areas)
    out = await h.harvest(_CONTENT, [_PLACE])
    assert out[0].geo == ResolvedGeo(country_code="jp", city="Kyoto")
    areas.resolve_city.assert_awaited_once_with("Kyoto", "jp")


async def test_city_claim_refused_by_area_service_dropped() -> None:
    # The area service refuses unverifiable names (round-trip failure,
    # "Muine"-style misses) — the claim drops, never mis-keys.
    h = _harvester([_claim("city", "Muine")], _areas(city=None))
    assert await h.harvest(_CONTENT, [_PLACE]) == []


async def test_country_claim_resolved_by_name() -> None:
    areas = _areas(country=_country_entity("jp"))
    h = _harvester([_claim("country", "Japan")], areas)
    out = await h.harvest(_CONTENT, [_PLACE])
    assert out[0].geo == ResolvedGeo(country_code="jp")
    areas.resolve_country.assert_awaited_once_with("Japan")


async def test_country_claim_refused_dropped() -> None:
    # "Japan Curry House" matches no country feature — the service refuses.
    h = _harvester([_claim("country", "Japan Curry House")], _areas(country=None))
    assert await h.harvest(_CONTENT, [_PLACE]) == []


async def test_out_of_range_place_index_dropped() -> None:
    h = _harvester([_claim("place", "?", place_index=5, confidence=0.5)])
    assert await h.harvest(_CONTENT, [_PLACE]) == []


async def test_empty_content_short_circuits_without_llm() -> None:
    client = AsyncMock()
    client.extract = AsyncMock()
    h = KnowledgeHarvester(client, _areas())
    out = await h.harvest(HarvestContent(), [_PLACE])
    assert out == []
    client.extract.assert_not_called()


async def test_no_places_short_circuits() -> None:
    client = AsyncMock()
    client.extract = AsyncMock()
    out = await KnowledgeHarvester(client, _areas()).harvest(_CONTENT, [])
    assert out == []
    client.extract.assert_not_called()


async def test_llm_error_swallowed() -> None:
    client = AsyncMock()
    client.extract = AsyncMock(side_effect=RuntimeError("boom"))
    out = await KnowledgeHarvester(client, _areas()).harvest(_CONTENT, [_PLACE])
    assert out == []
