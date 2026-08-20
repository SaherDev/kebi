"""Tests for KnowledgeCurationService — anchor resolution + hand-off."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from kebi.core.knowledge.curation_service import (
    AnchorNotFoundError,
    CuratorClaimsService,
    KnowledgeCurationService,
    curator_source_ref,
)
from kebi.core.places.models import LocationContext, PlaceCore
from tests.geo_fakes import FakeGeoRegistry, make_area, make_city

_BALI = make_city("id", "Bali", pid="CityBali01")
_CANGGU = make_area(_BALI, "Canggu", pid="AreaCanggu01")


def _service(
    *,
    place: PlaceCore | None = None,
    area_profile=None,
    registry: FakeGeoRegistry | None = None,
) -> tuple[KnowledgeCurationService, AsyncMock, AsyncMock]:
    curator = AsyncMock()
    curator.structure = AsyncMock(return_value=[])
    ingestion = AsyncMock()
    ingestion.ingest = AsyncMock(return_value=[])
    places_repo = AsyncMock()
    places_repo.get_by_ids = AsyncMock(return_value=[place] if place else [])
    area_repo = AsyncMock()
    area_repo.get = AsyncMock(return_value=area_profile)
    svc = KnowledgeCurationService(
        curator=curator,
        ingestion=ingestion,
        places_repo=places_repo,
        area_repo=area_repo,
        geo_registry=registry or FakeGeoRegistry(_BALI, _CANGGU),
    )
    return svc, curator, ingestion


async def test_unanchored_passes_none() -> None:
    svc, curator, _ = _service()
    await svc.curate(text="prose", user_id="user_x")
    anchor = curator.structure.await_args.args[1]
    assert anchor is None


async def test_place_anchor_resolved_from_catalog() -> None:
    place = PlaceCore(
        id="p1",
        place_name="Beach Club X",
        location=LocationContext(
            country_code="ae", city="Dubai", neighborhood="Jumeirah"
        ),
    )
    svc, curator, _ = _service(place=place)
    await svc.curate(text="prose", user_id="user_x", anchor_place_id="p1")
    anchor = curator.structure.await_args.args[1]
    assert anchor.place_id == "p1"
    assert anchor.name == "Beach Club X"
    assert anchor.geo.country_code == "ae"
    assert anchor.geo.city == "Dubai"


async def test_unknown_place_anchor_raises_before_llm() -> None:
    svc, curator, _ = _service(place=None)
    with pytest.raises(AnchorNotFoundError):
        await svc.curate(text="prose", user_id="user_x", anchor_place_id="ghost")
    curator.structure.assert_not_awaited()


async def test_area_anchor_named_and_geo_built_from_registry_rows() -> None:
    # Key segments are provider ids, so geo parts and the display name come
    # from the rows behind them — never from the key text. No profiled row:
    # the leaf registry row's display name is the fallback.
    svc, curator, _ = _service(area_profile=None)
    await svc.curate(text="prose", user_id="user_x", anchor_geo_key=_CANGGU.geo_key)
    anchor = curator.structure.await_args.args[1]
    assert anchor.place_id is None
    assert anchor.name == "Canggu"
    assert anchor.geo.country_code == "id"
    assert anchor.geo.city == "Bali"
    assert anchor.geo.neighborhood == "Canggu"


async def test_area_anchor_prefers_profiled_name() -> None:
    class _Profile:
        name = "Canggu (Bali)"

    svc, curator, _ = _service(area_profile=_Profile())
    await svc.curate(text="prose", user_id="user_x", anchor_geo_key=_CANGGU.geo_key)
    anchor = curator.structure.await_args.args[1]
    assert anchor.name == "Canggu (Bali)"


async def test_country_only_anchor_falls_back_to_the_code() -> None:
    # A bare country key needs no registry row; the code is the display.
    svc, curator, _ = _service(registry=FakeGeoRegistry())
    await svc.curate(text="prose", user_id="user_x", anchor_geo_key="id")
    anchor = curator.structure.await_args.args[1]
    assert anchor.name == "ID"
    assert anchor.geo.country_code == "id"
    assert anchor.geo.city is None


async def test_unregistered_area_key_raises_before_llm() -> None:
    # A non-country segment with no registry row cannot anchor — the key
    # text alone proves nothing (AnchorNotFoundError, not a guessed geo).
    svc, curator, _ = _service()
    with pytest.raises(AnchorNotFoundError):
        await svc.curate(
            text="prose",
            user_id="user_x",
            anchor_geo_key="id/CityBali01/AreaGhost99",
        )
    curator.structure.assert_not_awaited()


async def test_provenance_is_caller_scoped_but_claims_global() -> None:
    svc, _, ingestion = _service()
    await svc.curate(text="prose", user_id="user_x")
    kwargs = ingestion.ingest.await_args.kwargs
    assert kwargs["source_ref"] == "curator:user_x"
    assert kwargs["user_id"] is None


async def test_claims_service_scopes_by_source_ref() -> None:
    repo = AsyncMock()
    repo.list_by_source_ref = AsyncMock(return_value=([], None))
    repo.delete_owned = AsyncMock(return_value=True)
    svc = CuratorClaimsService(repo)

    await svc.list_mine("user_x", 20, None)
    repo.list_by_source_ref.assert_awaited_once_with("curator:user_x", 20, None)

    assert await svc.retract("user_x", "c1") is True
    repo.delete_owned.assert_awaited_once_with("c1", "curator:user_x")

    # One helper defines the ownership key everywhere.
    assert curator_source_ref("user_x") == "curator:user_x"
