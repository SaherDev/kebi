"""Tests for UserPlacesRepo session-state contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql as pg_dialect

from kebi.core.places._cursor import LibraryCursor
from kebi.core.places.models import (
    LibrarySort,
    PlaceCategory,
    PlaceSource,
    SavedPlaceFilters,
    SavedPlaceView,
    UserPlace,
    UserPlaceStatusUpdate,
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

    cursor = LibraryCursor(
        LibrarySort.recent, datetime(2026, 6, 9, tzinfo=UTC).isoformat(), "up-50"
    )
    await repo.browse("u1", SavedPlaceFilters(), limit=20, cursor=cursor)

    sql = _compiled(session)
    # Row-value keyset comparison over the same (saved_at, user_place_id) key.
    assert "(user_places.saved_at, user_places.user_place_id) < (" in sql


@pytest.mark.asyncio
async def test_browse_name_sort_orders_case_insensitively_asc() -> None:
    session = MagicMock()
    session.execute = AsyncMock(return_value=[])
    repo = UserPlacesRepo(session=session)

    await repo.browse("u1", SavedPlaceFilters(), limit=20, sort=LibrarySort.name)

    sql = _compiled(session)
    # lower(place_name) ASC, with the user_place_id tie-break ASC.
    assert "ORDER BY lower(places.place_name) ASC, user_places.user_place_id ASC" in sql


@pytest.mark.asyncio
async def test_browse_name_sort_keyset_uses_lowered_name_and_gt() -> None:
    session = MagicMock()
    session.execute = AsyncMock(return_value=[])
    repo = UserPlacesRepo(session=session)

    cursor = LibraryCursor(LibrarySort.name, "café x", "up-50")
    await repo.browse(
        "u1", SavedPlaceFilters(), limit=20, cursor=cursor, sort=LibrarySort.name
    )

    sql = _compiled(session)
    # Ascending sort → strictly-greater row-value comparison over the same
    # (lower(place_name), user_place_id) key the ORDER BY uses.
    assert "(lower(places.place_name), user_places.user_place_id) > (" in sql


@pytest.mark.asyncio
async def test_browse_rejects_cursor_from_a_different_sort() -> None:
    """A cursor minted under `recent` cannot be replayed under `name` — the
    anchor type and comparison differ, so paging must restart."""
    session = MagicMock()
    session.execute = AsyncMock(return_value=[])
    repo = UserPlacesRepo(session=session)

    recent_cursor = LibraryCursor(
        LibrarySort.recent, datetime(2026, 6, 9, tzinfo=UTC).isoformat(), "up-1"
    )
    with pytest.raises(ValueError, match="does not match requested sort"):
        await repo.browse(
            "u1",
            SavedPlaceFilters(),
            limit=20,
            cursor=recent_cursor,
            sort=LibrarySort.name,
        )


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


@pytest.mark.asyncio
async def test_delete_one_scopes_on_both_user_place_id_and_user_id() -> None:
    """The DELETE must filter on user_id too — ownership IS the predicate.
    Without it, any caller could delete any row by id (IDOR)."""
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(rowcount=1))
    session.commit = AsyncMock()
    repo = UserPlacesRepo(session=session)

    await repo.delete_one("up-1", "u1")

    sql = _compiled(session)
    assert sql.startswith("DELETE FROM user_places")
    assert "user_places.user_place_id =" in sql
    assert "user_places.user_id =" in sql


@pytest.mark.asyncio
async def test_delete_one_returns_rowcount_and_commits() -> None:
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(rowcount=1))
    session.commit = AsyncMock()
    repo = UserPlacesRepo(session=session)

    assert await repo.delete_one("up-1", "u1") == 1
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_one_absent_or_not_owned_returns_zero() -> None:
    """No matching row (absent, or owned by another user) → 0 rows, still
    a clean commit. The caller maps this to an idempotent 204."""
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(rowcount=0))
    session.commit = AsyncMock()
    repo = UserPlacesRepo(session=session)

    assert await repo.delete_one("up-missing", "u1") == 0
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_one_rolls_back_on_execute_error() -> None:
    session = MagicMock()
    session.execute = AsyncMock(side_effect=RuntimeError("boom"))
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    repo = UserPlacesRepo(session=session)

    with pytest.raises(RuntimeError, match="boom"):
        await repo.delete_one("up-1", "u1")

    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()


def _exec_returning(mapping: dict[str, Any] | None) -> MagicMock:
    """A session.execute return whose .mappings().first() yields `mapping`."""
    result = MagicMock()
    result.mappings.return_value.first.return_value = mapping
    return result


@pytest.mark.asyncio
async def test_update_fields_scopes_on_both_ids_and_writes_only_set_fields() -> None:
    """The UPDATE is owner-scoped (user_id in WHERE) and touches only the
    fields the caller set — `liked` here, not `visited`/`approved`/`note`."""
    t = datetime(2026, 6, 9, tzinfo=UTC)
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=_exec_returning(_row_mapping("p1", "u1", t))
    )
    session.commit = AsyncMock()
    repo = UserPlacesRepo(session=session)

    await repo.update_fields("up-1", "u1", UserPlaceStatusUpdate(liked=True))

    sql = _compiled(session)
    assert sql.startswith("UPDATE user_places SET")
    assert "liked=" in sql
    assert "user_places.user_place_id =" in sql
    assert "user_places.user_id =" in sql
    # Unset fields are not in the SET clause.
    assert "visited=" not in sql
    assert "approved=" not in sql


@pytest.mark.asyncio
async def test_update_fields_explicit_null_is_written() -> None:
    """An explicit None clears the column — `note` appears in SET even
    though its value is null (set ≠ omitted)."""
    t = datetime(2026, 6, 9, tzinfo=UTC)
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=_exec_returning(_row_mapping("p1", "u1", t))
    )
    session.commit = AsyncMock()
    repo = UserPlacesRepo(session=session)

    await repo.update_fields("up-1", "u1", UserPlaceStatusUpdate(note=None))

    sql = _compiled(session)
    assert "note=" in sql


@pytest.mark.asyncio
async def test_update_fields_returns_updated_row_and_commits() -> None:
    t = datetime(2026, 6, 9, tzinfo=UTC)
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=_exec_returning(_row_mapping("p1", "u1", t))
    )
    session.commit = AsyncMock()
    repo = UserPlacesRepo(session=session)

    result = await repo.update_fields("up-1", "u1", UserPlaceStatusUpdate(visited=True))

    assert result is not None
    assert result.user_place_id == "up-p1"
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_fields_absent_or_not_owned_returns_none() -> None:
    """RETURNING yields no row (absent, or owned by another user) → None."""
    session = MagicMock()
    session.execute = AsyncMock(return_value=_exec_returning(None))
    session.commit = AsyncMock()
    repo = UserPlacesRepo(session=session)

    result = await repo.update_fields(
        "up-missing", "u1", UserPlaceStatusUpdate(visited=True)
    )

    assert result is None


@pytest.mark.asyncio
async def test_update_fields_empty_changes_is_read_only_noop() -> None:
    """No fields set → no UPDATE issued (would be invalid SQL); falls back
    to an owner-scoped read and commits nothing."""
    t = datetime(2026, 6, 9, tzinfo=UTC)
    session = MagicMock()
    session.execute = AsyncMock(
        return_value=_exec_returning(_row_mapping("p1", "u1", t))
    )
    session.commit = AsyncMock()
    repo = UserPlacesRepo(session=session)

    result = await repo.update_fields("up-1", "u1", UserPlaceStatusUpdate())

    sql = _compiled(session)
    assert sql.startswith("SELECT")
    assert result is not None
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_fields_rolls_back_on_execute_error() -> None:
    session = MagicMock()
    session.execute = AsyncMock(side_effect=RuntimeError("boom"))
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    repo = UserPlacesRepo(session=session)

    with pytest.raises(RuntimeError, match="boom"):
        await repo.update_fields("up-1", "u1", UserPlaceStatusUpdate(visited=True))

    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()
