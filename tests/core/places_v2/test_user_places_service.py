"""Tests for UserPlacesService."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from kebi.core.places_v2.models import (
    LocationContext,
    PlaceCore,
    PlaceSource,
    SavedPlaceView,
    UserPlace,
)
from kebi.core.places_v2.user_places_service import UserPlacesService


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


class TestGetUserPlaces:
    async def test_empty_returns_empty(self) -> None:
        places_repo = MagicMock(get_by_ids=AsyncMock(return_value=[]))
        user_places_repo = MagicMock(get_by_user=AsyncMock(return_value=[]))
        svc = UserPlacesService(
            places_repo=places_repo,
            user_places_repo=user_places_repo,
        )
        result = await svc.get_user_places("u1")
        assert result == []

    async def test_returns_core_only_views(self) -> None:
        up = _user_place("u1", "p1")
        core = _core("p1")

        places_repo = MagicMock(get_by_ids=AsyncMock(return_value=[core]))
        user_places_repo = MagicMock(get_by_user=AsyncMock(return_value=[up]))

        svc = UserPlacesService(
            places_repo=places_repo,
            user_places_repo=user_places_repo,
        )
        result = await svc.get_user_places("u1")

        assert len(result) == 1
        assert isinstance(result[0], SavedPlaceView)
        assert isinstance(result[0].place, PlaceCore)
        assert result[0].place.place_name == "Place p1"

    async def test_multiple_users_places(self) -> None:
        ups = [_user_place("u1", "p1"), _user_place("u1", "p2")]
        cores = [_core("p1"), _core("p2")]

        places_repo = MagicMock(get_by_ids=AsyncMock(return_value=cores))
        user_places_repo = MagicMock(get_by_user=AsyncMock(return_value=ups))

        svc = UserPlacesService(
            places_repo=places_repo,
            user_places_repo=user_places_repo,
        )
        result = await svc.get_user_places("u1")
        assert len(result) == 2


class TestUpdateStatus:
    async def test_updates_visited_flag(self) -> None:
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
        )
        result = await svc.update_status("up-p1", visited=True)

        assert result.visited is True
        user_places_repo.save_user_places.assert_awaited_once()

    async def test_raises_when_not_found(self) -> None:
        places_repo = MagicMock()
        user_places_repo = MagicMock(
            get_by_user_place_id=AsyncMock(return_value=None),
        )
        svc = UserPlacesService(
            places_repo=places_repo,
            user_places_repo=user_places_repo,
        )
        with pytest.raises(ValueError, match="user_place_id not found"):
            await svc.update_status("missing-id", visited=True)
