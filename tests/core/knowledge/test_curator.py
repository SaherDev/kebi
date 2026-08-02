"""Tests for KnowledgeCurator — prose structuring + area resolution (ADR-121)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from kebi.core.areas.models import AreaEntity
from kebi.core.knowledge.curator import (
    KnowledgeCurator,
    LocationHint,
    _CuratedClaim,
    _CuratorResponse,
)


def _entity(
    cc: str, name: str, entity_type: str = "city", key: str | None = None
) -> AreaEntity:
    return AreaEntity(
        entity_key=key or f"{cc}/{name.lower()}",
        entity_type=entity_type,  # type: ignore[arg-type]
        name=name,
        country_code=cc,
        lat=25.2,
        lng=55.2,
    )


def _curator(
    claims: list[_CuratedClaim],
    *,
    country: AreaEntity | None = None,
    city: AreaEntity | None = None,
):
    client = AsyncMock()
    client.extract = AsyncMock(return_value=_CuratorResponse(claims=claims))
    areas = AsyncMock()
    areas.resolve_country = AsyncMock(return_value=country)
    areas.resolve_city = AsyncMock(return_value=city)
    return KnowledgeCurator(client, areas), areas


_CLAIM = _CuratedClaim(
    scope="neighborhood",
    entity_name="Jumeirah",
    area_country="United Arab Emirates",
    area_city="Dubai",
    claim="beach clubs with sunset views",
    tags=["nightlife"],
    confidence=0.8,
)


async def test_resolves_structured_area_via_area_service() -> None:
    curator, areas = _curator(
        [_CLAIM],
        country=_entity("ae", "United Arab Emirates", "country", key="ae"),
        city=_entity("ae", "Dubai"),
    )
    out = await curator.structure("prose")
    assert len(out) == 1
    assert out[0].geo is not None
    assert out[0].geo.country_code == "ae"
    assert out[0].geo.city == "Dubai"
    areas.resolve_city.assert_awaited_once_with("Dubai", "ae")


async def test_falls_back_to_hint_when_area_refuses() -> None:
    curator, _ = _curator([_CLAIM])  # area service refuses everything
    out = await curator.structure(
        "prose", LocationHint(country_alpha2="AE", city="Dubai")
    )
    assert len(out) == 1
    assert out[0].geo.country_code == "ae"  # hint lowercased


async def test_hint_country_constrains_city_when_country_unnamed() -> None:
    claim = _CLAIM.model_copy(update={"area_country": None})
    curator, areas = _curator([claim], city=_entity("ae", "Dubai"))
    out = await curator.structure("prose", LocationHint(country_alpha2="AE"))
    assert out[0].geo.city == "Dubai"
    areas.resolve_city.assert_awaited_once_with("Dubai", "ae")


async def test_dropped_when_nothing_resolves_and_no_hint() -> None:
    curator, _ = _curator([_CLAIM])
    assert await curator.structure("prose") == []


async def test_country_only_claim_resolves_at_country_scope() -> None:
    claim = _CLAIM.model_copy(
        update={"scope": "country", "entity_name": "UAE", "area_city": None}
    )
    curator, _ = _curator(
        [claim], country=_entity("ae", "United Arab Emirates", "country", key="ae")
    )
    out = await curator.structure("prose")
    assert out[0].geo.country_code == "ae"
    assert out[0].geo.city is None


async def test_empty_text_short_circuits() -> None:
    client = AsyncMock()
    client.extract = AsyncMock()
    out = await KnowledgeCurator(client, AsyncMock()).structure("   ")
    assert out == []
    client.extract.assert_not_called()


async def test_llm_error_swallowed() -> None:
    client = AsyncMock()
    client.extract = AsyncMock(side_effect=RuntimeError("boom"))
    out = await KnowledgeCurator(client, AsyncMock()).structure("prose")
    assert out == []
