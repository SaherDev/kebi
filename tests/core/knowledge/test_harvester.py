"""Tests for KnowledgeHarvester — verified claim resolution (ADR-121/126)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from kebi.core.knowledge.harvester import (
    KnowledgeHarvester,
    _HarvestedClaim,
    _HarvesterResponse,
)
from kebi.core.knowledge.schemas import HarvestContent, HarvestPlace, ResolvedGeo
from tests.geo_fakes import FakeGeoRegistry, make_city, make_country

_PLACE = HarvestPlace(
    place_id="p1",
    name="Fuji Ramen",
    geo=ResolvedGeo(country_code="jp", city="Tokyo", neighborhood="Shibuya"),
)
_CONTENT = HarvestContent(caption="late-night ramen in Shibuya", source_ref="u")

_KYOTO = make_city("jp", "Kyoto")
_JAPAN = make_country("jp", "Japan")


def _harvester(
    claims: list[_HarvestedClaim], registry: FakeGeoRegistry | None = None
) -> KnowledgeHarvester:
    client = AsyncMock()
    client.extract = AsyncMock(return_value=_HarvesterResponse(claims=claims))
    return KnowledgeHarvester(client, registry or FakeGeoRegistry())


def _spy(registry: FakeGeoRegistry) -> FakeGeoRegistry:
    registry.key_for_location = AsyncMock(  # type: ignore[method-assign]
        wraps=registry.key_for_location
    )
    return registry


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


async def test_city_claim_matching_anchor_skips_registry() -> None:
    registry = _spy(FakeGeoRegistry(_KYOTO))
    # Diacritic/script variance still counts as the anchor's own city.
    h = _harvester([_claim("city", "tōkyō")], registry)
    out = await h.harvest(_CONTENT, [_PLACE])
    assert out[0].geo == _PLACE.geo
    registry.key_for_location.assert_not_awaited()  # type: ignore[union-attr]


async def test_city_claim_naming_other_city_resolved_and_rekeyed() -> None:
    registry = _spy(FakeGeoRegistry(_KYOTO))
    h = _harvester([_claim("city", "Kyoto")], registry)
    out = await h.harvest(_CONTENT, [_PLACE])
    assert out[0].geo == ResolvedGeo(country_code="jp", city="Kyoto")
    registry.key_for_location.assert_awaited_once_with(  # type: ignore[union-attr]
        "jp", "Kyoto", None, mint=True
    )


async def test_city_claim_unresolvable_dropped() -> None:
    # The registry resolves shallower than asked (no minted city row), which
    # covers both the old "geocoder found nothing" and "failed the round-trip
    # name check" drops — verification now lives inside the registry's mint.
    h = _harvester([_claim("city", "Muine")])
    assert await h.harvest(_CONTENT, [_PLACE]) == []


async def test_country_claim_resolved_by_name() -> None:
    h = _harvester([_claim("country", "Japan")], FakeGeoRegistry(_JAPAN))
    out = await h.harvest(_CONTENT, [_PLACE])
    assert out[0].geo == ResolvedGeo(country_code="jp")


async def test_country_claim_naming_no_known_country_dropped() -> None:
    # "Japan Curry House" is not a country the registry knows — never keyed
    # to jp just because the name contains one.
    h = _harvester([_claim("country", "Japan Curry House")], FakeGeoRegistry(_JAPAN))
    assert await h.harvest(_CONTENT, [_PLACE]) == []


async def test_variant_spellings_key_to_one_entity() -> None:
    # "Kyoto" and "Kyōto" collapse to the same registered city.
    h = _harvester(
        [_claim("city", "Kyoto"), _claim("city", "Kyōto", claim="y")],
        FakeGeoRegistry(_KYOTO),
    )
    out = await h.harvest(_CONTENT, [_PLACE])
    assert len(out) == 2
    assert out[0].geo == out[1].geo == ResolvedGeo(country_code="jp", city="Kyoto")


async def test_out_of_range_place_index_dropped() -> None:
    h = _harvester([_claim("place", "?", place_index=5, confidence=0.5)])
    assert await h.harvest(_CONTENT, [_PLACE]) == []


async def test_empty_content_short_circuits_without_llm() -> None:
    client = AsyncMock()
    client.extract = AsyncMock()
    h = KnowledgeHarvester(client, FakeGeoRegistry())
    out = await h.harvest(HarvestContent(), [_PLACE])
    assert out == []
    client.extract.assert_not_called()


async def test_no_places_short_circuits() -> None:
    client = AsyncMock()
    client.extract = AsyncMock()
    out = await KnowledgeHarvester(client, FakeGeoRegistry()).harvest(_CONTENT, [])
    assert out == []
    client.extract.assert_not_called()


async def test_llm_error_swallowed() -> None:
    client = AsyncMock()
    client.extract = AsyncMock(side_effect=RuntimeError("boom"))
    out = await KnowledgeHarvester(client, FakeGeoRegistry()).harvest(
        _CONTENT, [_PLACE]
    )
    assert out == []
