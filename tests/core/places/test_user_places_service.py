"""Tests for UserPlacesService."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from kebi.core.places._cursor import LibraryCursor
from kebi.core.places.models import (
    LocationContext,
    PlaceCore,
    PlaceSource,
    SavedPlaceFilters,
    SavedPlaceView,
    UserPlace,
)
from kebi.core.places.user_places_service import (
    DuplicateUserPlaceError,
    UserPlacesService,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _user_place(uid: str, place_id: str, saved_at: datetime | None = None) -> UserPlace:
    return UserPlace(
        user_place_id=f"up-{place_id}",
        user_id=uid,
        place_id=place_id,
        source=PlaceSource.manual,
        saved_at=saved_at or _now(),
    )


def _core(pid: str) -> PlaceCore:
    return PlaceCore(
        id=pid,
        provider_id=f"google:{pid}",
        place_name=f"Place {pid}",
        location=LocationContext(lat=13.7, address="Test St"),
    )


def _view(uid: str, pid: str, saved_at: datetime | None = None) -> SavedPlaceView:
    return SavedPlaceView(place=_core(pid), user_data=_user_place(uid, pid, saved_at))


class TestBrowse:
    async def test_empty_returns_empty_page_and_no_cursor(self) -> None:
        repo = MagicMock(browse=AsyncMock(return_value=[]))
        svc = UserPlacesService(user_places_repo=repo)

        page, next_cursor = await svc.browse("u1", SavedPlaceFilters(), limit=10)

        assert page == []
        assert next_cursor is None
        # Over-fetches limit+1 to detect a next page; first page has no cursor.
        _, kwargs = repo.browse.call_args
        assert kwargs["limit"] == 11
        assert kwargs["cursor"] is None

    async def test_no_next_cursor_when_page_not_full(self) -> None:
        repo = MagicMock(browse=AsyncMock(return_value=[_view("u1", "p1")]))
        svc = UserPlacesService(user_places_repo=repo)

        page, next_cursor = await svc.browse("u1", SavedPlaceFilters(), limit=10)

        assert len(page) == 1
        assert next_cursor is None

    async def test_next_cursor_anchors_on_last_kept_row(self) -> None:
        # limit=2, repo returns 3 (limit+1) → there is another page.
        t = _now()
        repo = MagicMock(
            browse=AsyncMock(
                return_value=[
                    _view("u1", "p1", t),
                    _view("u1", "p2", t),
                    _view("u1", "p3", t),
                ]
            )
        )
        svc = UserPlacesService(user_places_repo=repo)

        page, next_cursor = await svc.browse("u1", SavedPlaceFilters(), limit=2)

        assert [v.place.id for v in page] == ["p1", "p2"]  # trimmed to limit
        assert next_cursor is not None
        # Cursor resumes after the last *kept* row, not the over-fetched one.
        assert LibraryCursor.decode(next_cursor) == LibraryCursor(t, "up-p2")

    async def test_incoming_cursor_decoded_and_passed_to_repo(self) -> None:
        repo = MagicMock(browse=AsyncMock(return_value=[]))
        svc = UserPlacesService(user_places_repo=repo)
        t = _now()
        token = LibraryCursor(t, "up-x").encode()

        await svc.browse("u1", SavedPlaceFilters(), limit=5, cursor=token)

        _, kwargs = repo.browse.call_args
        assert kwargs["cursor"] == LibraryCursor(t, "up-x")

    async def test_malformed_cursor_raises_value_error(self) -> None:
        repo = MagicMock(browse=AsyncMock(return_value=[]))
        svc = UserPlacesService(user_places_repo=repo)

        with pytest.raises(ValueError, match="invalid library cursor"):
            await svc.browse("u1", SavedPlaceFilters(), limit=5, cursor="@@bad@@")
        repo.browse.assert_not_called()


class TestUpdateStatus:
    async def test_updates_visited_flag(self) -> None:
        up = _user_place("u1", "p1")
        updated = up.model_copy(update={"visited": True})

        user_places_repo = MagicMock(
            get_by_user_place_id=AsyncMock(return_value=up),
            save_user_places=AsyncMock(return_value=[updated]),
        )
        svc = UserPlacesService(user_places_repo=user_places_repo)
        result = await svc.update_status("up-p1", visited=True)

        assert result.visited is True
        user_places_repo.save_user_places.assert_awaited_once()

    async def test_raises_when_not_found(self) -> None:
        user_places_repo = MagicMock(
            get_by_user_place_id=AsyncMock(return_value=None),
        )
        svc = UserPlacesService(user_places_repo=user_places_repo)
        with pytest.raises(ValueError, match="user_place_id not found"):
            await svc.update_status("missing-id", visited=True)


class TestSavePlaces:
    async def test_empty_returns_empty(self) -> None:
        user_places_repo = MagicMock(
            get_existing_place_ids=AsyncMock(return_value=set()),
            save_user_places=AsyncMock(return_value=[]),
        )
        svc = UserPlacesService(user_places_repo=user_places_repo)
        result = await svc.save_places(
            user_id="u1",
            places=[],
            source=PlaceSource.tiktok,
            source_ref="https://tiktok.com/x",
        )
        assert result == []
        user_places_repo.get_existing_place_ids.assert_not_called()
        user_places_repo.save_user_places.assert_not_called()

    async def test_builds_rows_with_approved_false_and_persists(self) -> None:
        cores = [_core("p1"), _core("p2")]
        user_places_repo = MagicMock(
            get_existing_place_ids=AsyncMock(return_value=set()),
            save_user_places=AsyncMock(side_effect=lambda rows: rows),
        )
        svc = UserPlacesService(user_places_repo=user_places_repo)

        result = await svc.save_places(
            user_id="u1",
            places=cores,
            source=PlaceSource.tiktok,
            source_ref="https://tiktok.com/x",
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
        assert all(r.source_ref == "https://tiktok.com/x" for r in result)
        # user_place_id is fresh per row
        assert len({r.user_place_id for r in result}) == 2

    async def test_rejects_core_without_id(self) -> None:
        bad = PlaceCore(place_name="No-id", provider_id="google:none")
        user_places_repo = MagicMock(
            get_existing_place_ids=AsyncMock(return_value=set()),
            save_user_places=AsyncMock(return_value=[]),
        )
        svc = UserPlacesService(user_places_repo=user_places_repo)
        with pytest.raises(ValueError, match="no id"):
            await svc.save_places(
                user_id="u1",
                places=[bad],
                source=PlaceSource.manual,
                source_ref=None,
            )
        user_places_repo.save_user_places.assert_not_called()

    async def test_source_labels_applied_per_place_id(self) -> None:
        cores = [_core("p1"), _core("p2")]
        user_places_repo = MagicMock(
            get_existing_place_ids=AsyncMock(return_value=set()),
            save_user_places=AsyncMock(side_effect=lambda rows: rows),
        )
        svc = UserPlacesService(user_places_repo=user_places_repo)

        result = await svc.save_places(
            user_id="u1",
            places=cores,
            source=PlaceSource.tiktok,
            source_ref="https://tiktok.com/x",
            source_labels={"p1": "Mirror Temple"},
        )

        by_pid = {r.place_id: r for r in result}
        assert by_pid["p1"].source_label == "Mirror Temple"
        # Absent from the map → NULL (per-place, not platform-wide).
        assert by_pid["p2"].source_label is None

    async def test_source_labels_default_none_back_compat(self) -> None:
        cores = [_core("p1")]
        user_places_repo = MagicMock(
            get_existing_place_ids=AsyncMock(return_value=set()),
            save_user_places=AsyncMock(side_effect=lambda rows: rows),
        )
        svc = UserPlacesService(user_places_repo=user_places_repo)
        result = await svc.save_places(
            user_id="u1",
            places=cores,
            source=PlaceSource.tiktok,
            source_ref="https://tiktok.com/x",
        )
        assert result[0].source_label is None

    async def test_duplicate_aborts_whole_batch(self) -> None:
        user_places_repo = MagicMock(
            get_existing_place_ids=AsyncMock(return_value={"p1"}),
            save_user_places=AsyncMock(return_value=[]),
        )
        svc = UserPlacesService(user_places_repo=user_places_repo)

        with pytest.raises(DuplicateUserPlaceError) as exc_info:
            await svc.save_places(
                user_id="u1",
                places=[_core("p1"), _core("p2")],
                source=PlaceSource.tiktok,
                source_ref="https://tiktok.com/x",
            )

        assert exc_info.value.conflicts == ["p1"]
        user_places_repo.save_user_places.assert_not_called()
