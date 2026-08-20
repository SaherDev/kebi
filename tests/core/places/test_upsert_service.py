"""Tests for PlaceUpsertService."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kebi.core.places.models import (
    LocationContext,
    PlaceCore,
    PlaceNameAlias,
    PlaceTag,
)
from kebi.core.places.upsert_service import PlaceUpsertService
from tests.geo_fakes import FakeGeoRegistry, make_area, make_city

_BALI = make_city("id", "Bali", pid="CityBali01")
_CANGGU = make_area(_BALI, "Canggu", pid="AreaCanggu01")


@pytest.fixture
def mock_repo() -> MagicMock:
    repo = MagicMock()
    repo.get_by_provider_ids = AsyncMock(return_value={})
    repo.upsert_places = AsyncMock(return_value=[])
    return repo


@pytest.fixture
def mock_embedding_service() -> MagicMock:
    svc = MagicMock()
    svc.embed_and_store = AsyncMock(return_value=None)
    return svc


@pytest.fixture
def service(
    mock_repo: MagicMock, mock_embedding_service: MagicMock
) -> PlaceUpsertService:
    return PlaceUpsertService(
        repo=mock_repo,
        embedding_service=mock_embedding_service,
        geo_registry=FakeGeoRegistry(_BALI, _CANGGU),
    )


class TestUpsertAndEmbed:
    async def test_empty_input_short_circuits(
        self,
        service: PlaceUpsertService,
        mock_repo: MagicMock,
        mock_embedding_service: MagicMock,
    ) -> None:
        result = await service.upsert_and_embed([])
        assert result == []
        mock_repo.get_by_provider_ids.assert_not_called()
        mock_repo.upsert_places.assert_not_called()
        mock_embedding_service.embed_and_store.assert_not_called()

    async def test_first_write_passes_candidate_through_and_embeds(
        self,
        service: PlaceUpsertService,
        mock_repo: MagicMock,
        mock_embedding_service: MagicMock,
    ) -> None:
        candidate = PlaceCore(place_name="Ramen Spot", provider_id="google:abc")
        persisted = PlaceCore(
            id="stored-id", place_name="Ramen Spot", provider_id="google:abc"
        )
        mock_repo.get_by_provider_ids.return_value = {}
        mock_repo.upsert_places.return_value = [persisted]

        result = await service.upsert_and_embed([candidate])

        mock_repo.get_by_provider_ids.assert_awaited_once_with(["google:abc"])
        mock_repo.upsert_places.assert_awaited_once()
        passed = mock_repo.upsert_places.call_args.args[0]
        assert passed[0].place_name == "Ramen Spot"
        mock_embedding_service.embed_and_store.assert_awaited_once_with([persisted])
        assert result == [persisted]

    async def test_skips_embed_when_repo_returns_empty(
        self,
        service: PlaceUpsertService,
        mock_repo: MagicMock,
        mock_embedding_service: MagicMock,
    ) -> None:
        candidate = PlaceCore(place_name="Ghost Spot", provider_id="google:xyz")
        mock_repo.get_by_provider_ids.return_value = {}
        mock_repo.upsert_places.return_value = []

        result = await service.upsert_and_embed([candidate])

        assert result == []
        mock_embedding_service.embed_and_store.assert_not_called()

    async def test_merges_existing_against_candidate(
        self,
        service: PlaceUpsertService,
        mock_repo: MagicMock,
        mock_embedding_service: MagicMock,
    ) -> None:
        existing = PlaceCore(
            id="uuid-A",
            provider_id="google:abc",
            place_name="Cafe Centro",
            tags=[PlaceTag(type="vibe", value="chill", source="google")],
        )
        candidate = PlaceCore(
            provider_id="google:abc",
            place_name="Café Centro",  # would be ignored — sticky
            place_name_aliases=[
                PlaceNameAlias(value="Cafe Centro Mission", source="tiktok")
            ],
            tags=[PlaceTag(type="cuisine", value="italian", source="user")],
        )
        mock_repo.get_by_provider_ids.return_value = {"google:abc": existing}
        mock_repo.upsert_places.side_effect = lambda cores: cores

        result = await service.upsert_and_embed([candidate])

        merged = result[0]
        assert merged.id == "uuid-A"
        assert merged.place_name == "Cafe Centro"
        assert {t.value for t in merged.tags} == {"chill", "italian"}
        assert {a.value for a in merged.place_name_aliases} == {"Cafe Centro Mission"}
        mock_embedding_service.embed_and_store.assert_awaited_once()


class TestGeoKeyResolution:
    """The write path is the mint point: each merged core's area identity is
    resolved through the geo registry before it reaches the repo."""

    async def test_resolved_location_stores_the_registry_key(
        self, service: PlaceUpsertService, mock_repo: MagicMock
    ) -> None:
        candidate = PlaceCore(
            place_name="Crate Café",
            provider_id="google:crate",
            location=LocationContext(
                city="Bali", neighborhood="Canggu", country_code="id"
            ),
        )
        mock_repo.upsert_places.side_effect = lambda cores: cores

        result = await service.upsert_and_embed([candidate])

        assert result[0].geo_key == _CANGGU.geo_key

    async def test_unresolved_city_stores_no_key(
        self, service: PlaceUpsertService, mock_repo: MagicMock
    ) -> None:
        """A location whose city the registry can't verify keys to nothing —
        the `elsewhere` bucket, coarser-but-correct; a country-level key is
        not an area anyone navigates to."""
        candidate = PlaceCore(
            place_name="Ghost Café",
            provider_id="google:ghost",
            geo_key="id/CityBali01/AreaCanggu01",  # stale — must not survive
            location=LocationContext(city="Atlantis", country_code="id"),
        )
        mock_repo.upsert_places.side_effect = lambda cores: cores

        result = await service.upsert_and_embed([candidate])

        assert result[0].geo_key is None

    async def test_missing_location_stores_no_key(
        self, service: PlaceUpsertService, mock_repo: MagicMock
    ) -> None:
        candidate = PlaceCore(place_name="Nowhere Bar", provider_id="google:nw")
        mock_repo.upsert_places.side_effect = lambda cores: cores

        result = await service.upsert_and_embed([candidate])

        assert result[0].geo_key is None

    async def test_registry_failure_keeps_the_carried_key(
        self, mock_repo: MagicMock, mock_embedding_service: MagicMock
    ) -> None:
        """Identity resolution must never cost the save: a refresh must not
        strip a good key over a transient registry failure."""
        registry = MagicMock(
            key_for_location=AsyncMock(side_effect=RuntimeError("geo down"))
        )
        service = PlaceUpsertService(
            repo=mock_repo,
            embedding_service=mock_embedding_service,
            geo_registry=registry,
        )
        candidate = PlaceCore(
            place_name="Crate Café",
            provider_id="google:crate",
            geo_key="id/CityBali01/AreaCanggu01",
            location=LocationContext(
                city="Bali", neighborhood="Canggu", country_code="id"
            ),
        )
        mock_repo.upsert_places.side_effect = lambda cores: cores

        result = await service.upsert_and_embed([candidate])

        assert result[0].geo_key == "id/CityBali01/AreaCanggu01"
