"""Tests for UserPlacesService."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from kebi.core.places.models import (
    LocationContext,
    PlaceCore,
    PlaceSource,
    SavedPlaceView,
    UserPlace,
)
from kebi.core.places.user_places_service import (
    DuplicateUserPlaceError,
    UserPlacesService,
)


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


class TestSavePlaces:
    async def test_empty_returns_empty(self) -> None:
        places_repo = MagicMock()
        user_places_repo = MagicMock(
            get_existing_place_ids=AsyncMock(return_value=set()),
            save_user_places=AsyncMock(return_value=[]),
        )
        svc = UserPlacesService(
            places_repo=places_repo, user_places_repo=user_places_repo
        )
        result = await svc.save_places(
            user_id="u1",
            places=[],
            source=PlaceSource.tiktok,
            source_url="https://tiktok.com/x",
        )
        assert result == []
        user_places_repo.get_existing_place_ids.assert_not_called()
        user_places_repo.save_user_places.assert_not_called()

    async def test_builds_rows_with_approved_false_and_persists(self) -> None:
        cores = [_core("p1"), _core("p2")]
        places_repo = MagicMock()
        user_places_repo = MagicMock(
            get_existing_place_ids=AsyncMock(return_value=set()),
            save_user_places=AsyncMock(side_effect=lambda rows: rows),
        )
        svc = UserPlacesService(
            places_repo=places_repo, user_places_repo=user_places_repo
        )

        result = await svc.save_places(
            user_id="u1",
            places=cores,
            source=PlaceSource.tiktok,
            source_url="https://tiktok.com/x",
        )

        user_places_repo.get_existing_place_ids.assert_awaited_once_with(
            "u1", ["p1", "p2"]
        )
        assert len(result) == 2
        assert {r.place_id for r in result} == {"p1", "p2"}
        assert all(r.user_id == "u1" for r in result)
        assert all(r.approved is False for r in result)
        assert all(r.visited is False for r in result)
        assert all(r.liked is None for r in result)
        assert all(r.note is None for r in result)
        assert all(r.source == PlaceSource.tiktok for r in result)
        assert all(r.source_url == "https://tiktok.com/x" for r in result)
        # user_place_id is fresh per row
        assert len({r.user_place_id for r in result}) == 2

    async def test_rejects_core_without_id(self) -> None:
        bad = PlaceCore(place_name="No-id", provider_id="google:none")
        places_repo = MagicMock()
        user_places_repo = MagicMock(
            get_existing_place_ids=AsyncMock(return_value=set()),
            save_user_places=AsyncMock(return_value=[]),
        )
        svc = UserPlacesService(
            places_repo=places_repo, user_places_repo=user_places_repo
        )
        with pytest.raises(ValueError, match="no id"):
            await svc.save_places(
                user_id="u1",
                places=[bad],
                source=PlaceSource.manual,
                source_url=None,
            )
        user_places_repo.save_user_places.assert_not_called()

    async def test_source_labels_applied_per_place_id(self) -> None:
        cores = [_core("p1"), _core("p2")]
        places_repo = MagicMock()
        user_places_repo = MagicMock(
            get_existing_place_ids=AsyncMock(return_value=set()),
            save_user_places=AsyncMock(side_effect=lambda rows: rows),
        )
        svc = UserPlacesService(
            places_repo=places_repo, user_places_repo=user_places_repo
        )

        result = await svc.save_places(
            user_id="u1",
            places=cores,
            source=PlaceSource.tiktok,
            source_url="https://tiktok.com/x",
            source_labels={"p1": "Mirror Temple"},
        )

        by_pid = {r.place_id: r for r in result}
        assert by_pid["p1"].source_label == "Mirror Temple"
        # Absent from the map → NULL (per-place, not platform-wide).
        assert by_pid["p2"].source_label is None

    async def test_source_labels_default_none_back_compat(self) -> None:
        cores = [_core("p1")]
        places_repo = MagicMock()
        user_places_repo = MagicMock(
            get_existing_place_ids=AsyncMock(return_value=set()),
            save_user_places=AsyncMock(side_effect=lambda rows: rows),
        )
        svc = UserPlacesService(
            places_repo=places_repo, user_places_repo=user_places_repo
        )
        result = await svc.save_places(
            user_id="u1",
            places=cores,
            source=PlaceSource.tiktok,
            source_url="https://tiktok.com/x",
        )
        assert result[0].source_label is None

    async def test_duplicate_aborts_whole_batch(self) -> None:
        places_repo = MagicMock()
        user_places_repo = MagicMock(
            get_existing_place_ids=AsyncMock(return_value={"p1"}),
            save_user_places=AsyncMock(return_value=[]),
        )
        svc = UserPlacesService(
            places_repo=places_repo, user_places_repo=user_places_repo
        )

        with pytest.raises(DuplicateUserPlaceError) as exc_info:
            await svc.save_places(
                user_id="u1",
                places=[_core("p1"), _core("p2")],
                source=PlaceSource.tiktok,
                source_url="https://tiktok.com/x",
            )

        assert exc_info.value.conflicts == ["p1"]
        user_places_repo.save_user_places.assert_not_called()
