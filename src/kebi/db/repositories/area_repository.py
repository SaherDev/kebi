"""Area repository — Protocol + SQLAlchemy implementation (ADR-153).

Each method opens its own session via session_factory so it works in any
context (request path for reads, background profiler for the write). All
rows here are global — an area profile is user-independent — so there is
no scoping clause to enforce.
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kebi.core.areas.models import AreaChip, AreaProfile, NotableSubArea
from kebi.db.models import Area as AreaRow


def _to_profile(row: AreaRow) -> AreaProfile:
    return AreaProfile(
        geo_key=row.geo_key,
        name=row.name,
        level=row.level,
        icon=row.icon,
        summary=row.summary,
        best_for=[AreaChip.model_validate(c) for c in row.best_for or []],
        breadcrumb=list(row.breadcrumb or []),
        notable_sub_areas=[
            NotableSubArea.model_validate(s) for s in row.notable_sub_areas or []
        ],
        profiled_at=row.profiled_at,
    )


class AreaRepository(Protocol):
    async def get(self, geo_key: str) -> AreaProfile | None: ...

    async def get_many(self, geo_keys: list[str]) -> dict[str, AreaProfile]: ...

    async def upsert(self, profile: AreaProfile) -> AreaProfile: ...


class SQLAlchemyAreaRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, geo_key: str) -> AreaProfile | None:
        async with self._session_factory() as session:
            row = await session.get(AreaRow, geo_key)
        return _to_profile(row) if row else None

    async def get_many(self, geo_keys: list[str]) -> dict[str, AreaProfile]:
        """Profiles for the given keys, keyed back — absent keys just missing."""
        if not geo_keys:
            return {}
        async with self._session_factory() as session:
            stmt = select(AreaRow).where(AreaRow.geo_key.in_(geo_keys))
            rows = (await session.execute(stmt)).scalars().all()
        return {row.geo_key: _to_profile(row) for row in rows}

    async def upsert(self, profile: AreaProfile) -> AreaProfile:
        """Write one profiling pass's output, replacing any prior profile.

        Idempotent by key: two racers dressing the same area both write the
        same global facts, so last-write-wins is safe — the Redis lock in the
        profiler is spend control, not a correctness requirement.
        """
        values = {
            "geo_key": profile.geo_key,
            "name": profile.name,
            "level": profile.level,
            "icon": profile.icon,
            "summary": profile.summary,
            "best_for": [c.model_dump() for c in profile.best_for],
            "breadcrumb": list(profile.breadcrumb),
            "notable_sub_areas": [s.model_dump() for s in profile.notable_sub_areas],
        }
        stmt = (
            pg_insert(AreaRow)
            .values(**values)
            .on_conflict_do_update(index_elements=["geo_key"], set_=values)
            .returning(AreaRow)
        )
        async with self._session_factory() as session:
            row = (await session.execute(stmt)).scalar_one()
            await session.commit()
            return _to_profile(row)
