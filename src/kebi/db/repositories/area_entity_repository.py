"""Area-entity repository — Protocol + SQLAlchemy implementation.

Each method opens its own session via session_factory so it works in any
context (request, background event handler — the harvester resolves
areas off the request path). Upserts key on `entity_key`: a re-resolved
area merges its aliases and refreshes geometry rather than duplicating.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kebi.core.areas.models import AreaEntity
from kebi.db.models import AreaEntity as AreaEntityRow


def _to_record(row: AreaEntityRow) -> AreaEntity:
    return AreaEntity(
        entity_key=row.entity_key,
        entity_type=row.entity_type.value,
        name=row.name,
        aliases=list(row.aliases or []),
        country_code=row.country_code,
        lat=row.lat,
        lng=row.lng,
        bbox=list(row.bbox) if row.bbox else None,
        place_type=row.place_type,
        parent_key=row.parent_key,
        provider_id=row.provider_id,
        geo_refreshed_at=row.geo_refreshed_at,
    )


class AreaEntityRepository(Protocol):
    async def get(self, entity_key: str) -> AreaEntity | None: ...

    async def get_many(self, entity_keys: list[str]) -> dict[str, AreaEntity]: ...

    async def find_by_alias(
        self, country_code: str, alias_slug: str
    ) -> AreaEntity | None: ...

    async def upsert(self, entity: AreaEntity) -> AreaEntity: ...

    async def update_geometry(
        self,
        entity_key: str,
        *,
        lat: float,
        lng: float,
        bbox: list[float] | None,
        refreshed_at: datetime,
    ) -> None: ...


class SQLAlchemyAreaEntityRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, entity_key: str) -> AreaEntity | None:
        async with self._session_factory() as session:
            row = await session.get(AreaEntityRow, entity_key)
        return _to_record(row) if row else None

    async def get_many(self, entity_keys: list[str]) -> dict[str, AreaEntity]:
        if not entity_keys:
            return {}
        async with self._session_factory() as session:
            stmt = select(AreaEntityRow).where(
                AreaEntityRow.entity_key.in_(entity_keys)
            )
            rows = (await session.execute(stmt)).scalars().all()
        return {row.entity_key: _to_record(row) for row in rows}

    async def find_by_alias(
        self, country_code: str, alias_slug: str
    ) -> AreaEntity | None:
        """Look up a city/neighborhood by a previously-seen name variant.

        Alias slugs are stored per row in a JSONB array; the GIN index
        serves the containment test. Scoped to one country — alias slugs
        are only unique within it (ADR-125's collision bound).
        """
        async with self._session_factory() as session:
            stmt = (
                select(AreaEntityRow)
                .where(
                    AreaEntityRow.country_code == country_code,
                    AreaEntityRow.aliases.contains([alias_slug]),
                )
                .limit(1)
            )
            row = (await session.execute(stmt)).scalars().first()
        return _to_record(row) if row else None

    async def upsert(self, entity: AreaEntity) -> AreaEntity:
        """INSERT ON CONFLICT (entity_key): union aliases, refresh geometry.

        Alias union happens in SQL against the stored row so concurrent
        resolvers can't drop each other's variants.
        """
        async with self._session_factory() as session:
            stmt = pg_insert(AreaEntityRow).values(
                entity_key=entity.entity_key,
                entity_type=entity.entity_type,
                name=entity.name,
                aliases=entity.aliases,
                country_code=entity.country_code,
                lat=entity.lat,
                lng=entity.lng,
                bbox=entity.bbox,
                place_type=entity.place_type,
                parent_key=entity.parent_key,
                provider_id=entity.provider_id,
                geo_refreshed_at=entity.geo_refreshed_at,
            )
            upsert_stmt = stmt.on_conflict_do_update(
                index_elements=["entity_key"],
                set_={
                    "name": stmt.excluded.name,
                    # Set-union in SQL so concurrent resolvers can't drop
                    # each other's learned spellings.
                    "aliases": text(
                        "(SELECT COALESCE(jsonb_agg(DISTINCT elem), '[]'::jsonb) "
                        "FROM jsonb_array_elements("
                        "area_entities.aliases || excluded.aliases) AS elem)"
                    ),
                    "lat": stmt.excluded.lat,
                    "lng": stmt.excluded.lng,
                    "bbox": stmt.excluded.bbox,
                    "place_type": stmt.excluded.place_type,
                    "parent_key": stmt.excluded.parent_key,
                    "provider_id": stmt.excluded.provider_id,
                    "geo_refreshed_at": stmt.excluded.geo_refreshed_at,
                },
            ).returning(AreaEntityRow)
            row = (await session.execute(upsert_stmt)).scalars().one()
            record = _to_record(row)
            await session.commit()
        return record

    async def update_geometry(
        self,
        entity_key: str,
        *,
        lat: float,
        lng: float,
        bbox: list[float] | None,
        refreshed_at: datetime,
    ) -> None:
        """ToS geometry refresh — touches only the provider-content
        columns, never identity."""
        async with self._session_factory() as session:
            await session.execute(
                update(AreaEntityRow)
                .where(AreaEntityRow.entity_key == entity_key)
                .values(
                    lat=lat,
                    lng=lng,
                    bbox=bbox,
                    geo_refreshed_at=refreshed_at,
                )
            )
            await session.commit()
