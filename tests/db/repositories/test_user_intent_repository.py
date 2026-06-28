"""Tests for SQLAlchemyUserIntentRepository + the IntentCursor (ADR-110)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.sql import Delete

from kebi.db.models import UserIntent
from kebi.db.repositories.user_intent_repository import (
    IntentCursor,
    SQLAlchemyUserIntentRepository,
)


def _mock_session_factory() -> tuple[MagicMock, AsyncMock]:
    session = AsyncMock()
    session.add = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    ctx = AsyncMock()
    ctx.__aenter__.return_value = session
    ctx.__aexit__.return_value = None
    factory = MagicMock(return_value=ctx)
    return factory, session


def _row(id_: str, text: str, when: datetime) -> SimpleNamespace:
    return SimpleNamespace(id=id_, text=text, created_at=when)


def _scalars_result(rows: list[SimpleNamespace]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    return result


# ---- IntentCursor -----------------------------------------------------------


def test_cursor_round_trip() -> None:
    when = datetime(2026, 6, 28, 21, 41, tzinfo=UTC)
    token = IntentCursor(when, "intent-1").encode()
    decoded = IntentCursor.decode(token)
    assert decoded.created_at == when
    assert decoded.intent_id == "intent-1"


@pytest.mark.parametrize("bad", ["@@", "", "bm90LWJhc2U2ND8="])
def test_cursor_decode_malformed_raises_value_error(bad: str) -> None:
    with pytest.raises(ValueError):
        IntentCursor.decode(bad)


# ---- add / latest / delete --------------------------------------------------


async def test_add_persists_user_intent() -> None:
    factory, session = _mock_session_factory()
    repo = SQLAlchemyUserIntentRepository(factory)

    await repo.add("user_abc", "coffee, quiet, nowhere i've been")

    session.add.assert_called_once()
    intent = session.add.call_args[0][0]
    assert isinstance(intent, UserIntent)
    assert intent.user_id == "user_abc"
    assert intent.text == "coffee, quiet, nowhere i've been"
    session.commit.assert_awaited_once()


async def test_latest_returns_record_or_none() -> None:
    factory, session = _mock_session_factory()
    when = datetime(2026, 6, 28, tzinfo=UTC)
    result = MagicMock()
    result.scalar_one_or_none.return_value = _row("i1", "drinks", when)
    session.execute = AsyncMock(return_value=result)
    repo = SQLAlchemyUserIntentRepository(factory)

    latest = await repo.latest("user_abc")
    assert latest is not None
    assert latest.id == "i1"
    assert latest.text == "drinks"


async def test_latest_none_when_no_rows() -> None:
    factory, session = _mock_session_factory()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result)
    repo = SQLAlchemyUserIntentRepository(factory)

    assert await repo.latest("user_abc") is None


async def test_delete_by_user_issues_delete_and_commits() -> None:
    factory, session = _mock_session_factory()
    repo = SQLAlchemyUserIntentRepository(factory)

    await repo.delete_by_user("user_abc")

    stmt = session.execute.await_args.args[0]
    assert isinstance(stmt, Delete)
    assert stmt.table.name == "user_intents"
    session.commit.assert_awaited_once()


# ---- list_page (keyset) -----------------------------------------------------


async def test_list_page_no_more_returns_null_cursor() -> None:
    factory, session = _mock_session_factory()
    rows = [
        _row("i2", "b", datetime(2026, 6, 28, 9, tzinfo=UTC)),
        _row("i1", "a", datetime(2026, 6, 27, 9, tzinfo=UTC)),
    ]
    session.execute = AsyncMock(return_value=_scalars_result(rows))
    repo = SQLAlchemyUserIntentRepository(factory)

    records, next_cursor = await repo.list_page("user_abc", limit=2, cursor=None)

    assert [r.id for r in records] == ["i2", "i1"]
    assert next_cursor is None


async def test_list_page_more_returns_cursor_and_trims_to_limit() -> None:
    factory, session = _mock_session_factory()
    # limit=2 but the repo fetches limit+1; a 3rd row signals "more".
    rows = [
        _row("i3", "c", datetime(2026, 6, 28, 9, tzinfo=UTC)),
        _row("i2", "b", datetime(2026, 6, 27, 9, tzinfo=UTC)),
        _row("i1", "a", datetime(2026, 6, 26, 9, tzinfo=UTC)),
    ]
    session.execute = AsyncMock(return_value=_scalars_result(rows))
    repo = SQLAlchemyUserIntentRepository(factory)

    records, next_cursor = await repo.list_page("user_abc", limit=2, cursor=None)

    assert [r.id for r in records] == ["i3", "i2"]
    assert next_cursor is not None
    # Cursor anchors on the last returned row (i2), not the peeked extra (i1).
    decoded = IntentCursor.decode(next_cursor)
    assert decoded.intent_id == "i2"
