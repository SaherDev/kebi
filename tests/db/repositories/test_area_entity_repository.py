"""Tests for SQLAlchemyAreaEntityRepository — row↔record mapping and the
session-per-method discipline (mocked sessions, matching the suite's
convention for repository tests)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from kebi.core.areas.models import AreaEntity
from kebi.db.models import AreaEntity as AreaEntityRow
from kebi.db.models import AreaEntityType
from kebi.db.repositories.area_entity_repository import (
    SQLAlchemyAreaEntityRepository,
)


def _row() -> AreaEntityRow:
    row = AreaEntityRow(
        entity_key="vn/hoi-an",
        entity_type=AreaEntityType.CITY,
        name="Hoi An",
        aliases=["hoian"],
        country_code="vn",
        lat=15.88,
        lng=108.33,
        bbox=[15.8, 15.95, 108.2, 108.4],
        place_type="locality",
        parent_key="vn",
        provider_id="google:ChIJx",
        geo_refreshed_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    return row


def _session_factory(session: AsyncMock) -> MagicMock:
    ctx = AsyncMock()
    ctx.__aenter__.return_value = session
    ctx.__aexit__.return_value = None
    return MagicMock(return_value=ctx)


def _entity() -> AreaEntity:
    return AreaEntity(
        entity_key="vn/hoi-an",
        entity_type="city",
        name="Hoi An",
        aliases=["hoian"],
        country_code="vn",
        lat=15.88,
        lng=108.33,
        parent_key="vn",
    )


async def test_get_maps_row_to_record() -> None:
    session = AsyncMock()
    session.get = AsyncMock(return_value=_row())
    repo = SQLAlchemyAreaEntityRepository(_session_factory(session))
    record = await repo.get("vn/hoi-an")
    assert record is not None
    assert record.entity_key == "vn/hoi-an"
    assert record.entity_type == "city"
    assert record.aliases == ["hoian"]
    assert record.bbox == [15.8, 15.95, 108.2, 108.4]
    assert record.parent_key == "vn"


async def test_get_missing_returns_none() -> None:
    session = AsyncMock()
    session.get = AsyncMock(return_value=None)
    repo = SQLAlchemyAreaEntityRepository(_session_factory(session))
    assert await repo.get("xx/nowhere") is None


async def test_get_many_empty_short_circuits_without_query() -> None:
    session = AsyncMock()
    repo = SQLAlchemyAreaEntityRepository(_session_factory(session))
    assert await repo.get_many([]) == {}
    session.execute.assert_not_awaited()


async def test_get_many_maps_by_key() -> None:
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [_row()]
    session.execute = AsyncMock(return_value=result)
    repo = SQLAlchemyAreaEntityRepository(_session_factory(session))
    records = await repo.get_many(["vn/hoi-an", "vn/hue"])
    assert set(records) == {"vn/hoi-an"}


async def test_find_by_alias_returns_first_match() -> None:
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.first.return_value = _row()
    session.execute = AsyncMock(return_value=result)
    repo = SQLAlchemyAreaEntityRepository(_session_factory(session))
    record = await repo.find_by_alias("vn", "hoian")
    assert record is not None and record.entity_key == "vn/hoi-an"


async def test_upsert_executes_and_commits() -> None:
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.one.return_value = _row()
    session.execute = AsyncMock(return_value=result)
    repo = SQLAlchemyAreaEntityRepository(_session_factory(session))
    record = await repo.upsert(_entity())
    assert record.entity_key == "vn/hoi-an"
    session.execute.assert_awaited_once()
    session.commit.assert_awaited_once()


async def test_update_geometry_touches_only_geometry() -> None:
    session = AsyncMock()
    repo = SQLAlchemyAreaEntityRepository(_session_factory(session))
    now = datetime(2026, 8, 2, tzinfo=UTC)
    await repo.update_geometry(
        "vn/hoi-an", lat=15.9, lng=108.4, bbox=None, refreshed_at=now
    )
    session.execute.assert_awaited_once()
    session.commit.assert_awaited_once()
