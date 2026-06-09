"""Tests for UserPlacesRepo session-state contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql as pg_dialect

from kebi.core.places._cursor import LibraryCursor
from kebi.core.places.models import (
    PlaceCategory,
    PlaceSource,
    SavedPlaceFilters,
    SavedPlaceView,
    UserPlace,
)
from kebi.core.places.user_places_repo import UserPlacesRepo


def _up() -> UserPlace:
    return UserPlace(
        user_place_id="up-1",
        user_id="u1",
        place_id="p1",
        source=PlaceSource.manual,
        saved_at=datetime.now(UTC),
    )


class _FakeRow:
    def __init__(self, mapping: dict[str, Any]) -> None:
        self._mapping = mapping


def _row_mapping(pid: str, uid: str, saved_at: datetime) -> dict[str, Any]:
    return {
        # places columns
        "id": pid,
        "provider_id": f"google:{pid}",
        "place_name": f"Place {pid}",
        "place_name_aliases": None,
        "categories": ["cafe"],
        "tags": None,
        "location": {"city": "Bangkok"},
        "created_at": saved_at,
        "refreshed_at": None,
        # user_places columns
        "user_place_id": f"up-{pid}",
        "user_id": uid,
        "place_id": pid,
        "approved": False,
        "visited": False,
        "liked": None,
        "note": None,
        "source": "manual",
        "source_ref": None,
        "source_label": None,
        "saved_at": saved_at,
        "visited_at": None,
    }


def _compiled(session: MagicMock) -> str:
    """The SQL the repo handed to session.execute, rendered for assertions."""
    stmt = session.execute.await_args.args[0]
    return str(stmt.compile(dialect=pg_dialect.dialect()))


@pytest.mark.asyncio
async def test_browse_orders_newest_first_with_tiebreak() -> None:
    session = MagicMock()
    session.execute = AsyncMock(return_value=[])
    repo = UserPlacesRepo(session=session)

    await repo.browse("u1", SavedPlaceFilters(), limit=20)

    sql = _compiled(session)
    assert "ORDER BY user_places.saved_at DESC, user_places.user_place_id DESC" in sql
    assert "LIMIT" in sql
    # No keyset predicate without a cursor.
    assert "user_places.saved_at, user_places.user_place_id) <" not in sql


@pytest.mark.asyncio
async def test_browse_cursor_adds_keyset_predicate() -> None:
    session = MagicMock()
    session.execute = AsyncMock(return_value=[])
    repo = UserPlacesRepo(session=session)

    cursor = LibraryCursor(datetime(2026, 6, 9, tzinfo=UTC), "up-50")
    await repo.browse("u1", SavedPlaceFilters(), limit=20, cursor=cursor)

    sql = _compiled(session)
    # Row-value keyset comparison over the same (saved_at, user_place_id) key.
    assert "(user_places.saved_at, user_places.user_place_id) < (" in sql


@pytest.mark.asyncio
async def test_browse_applies_place_and_user_filters() -> None:
    session = MagicMock()
    session.execute = AsyncMock(return_value=[])
    repo = UserPlacesRepo(session=session)

    await repo.browse(
        "u1",
        SavedPlaceFilters(
            categories=[PlaceCategory.cafe],
            visited=False,
            source=PlaceSource.tiktok,
        ),
        limit=20,
    )

    sql = _compiled(session)
    assert "user_places.user_id =" in sql
    assert "&&" in sql  # category array-overlap
    assert "user_places.visited =" in sql
    assert "user_places.source =" in sql


@pytest.mark.asyncio
async def test_browse_maps_rows_to_saved_place_views() -> None:
    t = datetime(2026, 6, 9, 8, 0, tzinfo=UTC)
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=[
            _FakeRow(_row_mapping("p1", "u1", t)),
            _FakeRow(_row_mapping("p2", "u1", t)),
        ]
    )
    repo = UserPlacesRepo(session=session)

    views = await repo.browse("u1", SavedPlaceFilters(), limit=20)

    assert [type(v) for v in views] == [SavedPlaceView, SavedPlaceView]
    assert [v.place.id for v in views] == ["p1", "p2"]
    assert views[0].place.place_name == "Place p1"
    assert views[0].user_data.user_place_id == "up-p1"
    assert views[0].user_data.source == PlaceSource.manual


@pytest.mark.asyncio
async def test_save_user_places_rolls_back_on_execute_error() -> None:
    """asyncpg leaves the transaction aborted on errors like an FK
    violation; subsequent statements on the same session then fail
    with `InFailedSQLTransactionError`. ADR-074's stale-cache
    fallback retries on the same request-scoped session, so this
    rollback is load-bearing."""
    session = MagicMock()
    boom = RuntimeError("FK violation simulated")
    session.execute = AsyncMock(side_effect=boom)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    repo = UserPlacesRepo(session=session)

    with pytest.raises(RuntimeError, match="FK violation simulated"):
        await repo.save_user_places([_up()])

    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_user_places_empty_is_noop() -> None:
    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    repo = UserPlacesRepo(session=session)
    assert await repo.save_user_places([]) == []

    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()
