"""Tests for UserPlacesService."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from totoro_ai.core.places_v2.models import (
    LocationContext,
    PlaceCore,
    PlaceObject,
    PlaceSource,
    SavedPlaceView,
    UserPlace,
)
from totoro_ai.core.places_v2.user_places_service import UserPlacesService


def _now() -> datetime:
    return datetime.now(UTC)


def _user_place(uid: str, place_id: str) -> UserPlace:
    return UserPlace(
        user_place_id=f"up-{place_id}",
        user_id=uid,
        place_id=place_id,
        source=PlaceSource.manual,
        saved_at=_now(),
    )


def _core(pid: str) -> PlaceCore:
    return PlaceCore(
        id=pid,
        provider_id=f"google:{pid}",
        place_name=f"Place {pid}",
        location=LocationContext(lat=13.7, address="Test St"),
    )


def _cached_object(pid: str) -> PlaceObject:
    return PlaceObject(
        id=pid,
        provider_id=f"google:{pid}",
        place_name=f"Place {pid}",
        rating=4.2,
        location=LocationContext(lat=13.7, address="Test St"),
    )


@pytest.fixture
def mock_search() -> MagicMock:
    search = MagicMock()
    search.get_by_ids = AsyncMock(return_value={})
    return search


class TestGetUserPlaces:
    async def test_empty_returns_empty(self, mock_search: MagicMock) -> None:
        places_repo = MagicMock(get_by_ids=AsyncMock(return_value=[]))
        user_places_repo = MagicMock(get_by_user=AsyncMock(return_value=[]))
        svc = UserPlacesService(
            places_repo=places_repo,
            user_places_repo=user_places_repo,
            search=mock_search,
        )
        result = await svc.get_user_places("u1")
        assert result == []

    async def test_cold_cache_returns_core_only_objects(
        self, mock_search: MagicMock
    ) -> None:
        up = _user_place("u1", "p1")
        core = _core("p1")

        places_repo = MagicMock(get_by_ids=AsyncMock(return_value=[core]))
        user_places_repo = MagicMock(get_by_user=AsyncMock(return_value=[up]))
        mock_search.get_by_ids = AsyncMock(return_value={})

        svc = UserPlacesService(
            places_repo=places_repo,
            user_places_repo=user_places_repo,
            search=mock_search,
        )
        result = await svc.get_user_places("u1")

        assert len(result) == 1
        assert isinstance(result[0], SavedPlaceView)
        assert result[0].place.place_name == "Place p1"
        assert result[0].place.rating is None  # no cache hit

    async def test_warm_cache_overlays_live_fields(
        self, mock_search: MagicMock
    ) -> None:
        up = _user_place("u1", "p1")
        core = _core("p1")
        cached = _cached_object("p1")

        places_repo = MagicMock(get_by_ids=AsyncMock(return_value=[core]))
        user_places_repo = MagicMock(get_by_user=AsyncMock(return_value=[up]))
        mock_search.get_by_ids = AsyncMock(return_value={"google:p1": cached})

        svc = UserPlacesService(
            places_repo=places_repo,
            user_places_repo=user_places_repo,
            search=mock_search,
        )
        result = await svc.get_user_places("u1")

        assert len(result) == 1
        assert result[0].place.rating == 4.2

    async def test_multiple_users_places(self, mock_search: MagicMock) -> None:
        ups = [_user_place("u1", "p1"), _user_place("u1", "p2")]
        cores = [_core("p1"), _core("p2")]

        places_repo = MagicMock(get_by_ids=AsyncMock(return_value=cores))
        user_places_repo = MagicMock(get_by_user=AsyncMock(return_value=ups))
        mock_search.get_by_ids = AsyncMock(return_value={})

        svc = UserPlacesService(
            places_repo=places_repo,
            user_places_repo=user_places_repo,
            search=mock_search,
        )
        result = await svc.get_user_places("u1")
        assert len(result) == 2


class TestUpdateStatus:
    async def test_updates_visited_flag(self, mock_search: MagicMock) -> None:
        up = _user_place("u1", "p1")
        updated = up.model_copy(update={"visited": True})

        places_repo = MagicMock()
        user_places_repo = MagicMock(
            get_by_user_place_id=AsyncMock(return_value=up),
            save_user_places=AsyncMock(return_value=[updated]),
        )
        svc = UserPlacesService(
            places_repo=places_repo,
            user_places_repo=user_places_repo,
            search=mock_search,
        )
        result = await svc.update_status("up-p1", visited=True)

        assert result.visited is True
        user_places_repo.save_user_places.assert_awaited_once()

    async def test_raises_when_not_found(self, mock_search: MagicMock) -> None:
        places_repo = MagicMock()
        user_places_repo = MagicMock(
            get_by_user_place_id=AsyncMock(return_value=None),
        )
        svc = UserPlacesService(
            places_repo=places_repo,
            user_places_repo=user_places_repo,
            search=mock_search,
        )
        with pytest.raises(ValueError, match="user_place_id not found"):
            await svc.update_status("missing-id", visited=True)
