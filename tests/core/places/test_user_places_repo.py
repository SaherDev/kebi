"""Tests for UserPlacesRepo session-state contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from kebi.core.places.models import PlaceSource, UserPlace
from kebi.core.places.user_places_repo import UserPlacesRepo


def _up() -> UserPlace:
    return UserPlace(
        user_place_id="up-1",
        user_id="u1",
        place_id="p1",
        source=PlaceSource.manual,
        saved_at=datetime.now(UTC),
    )


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
