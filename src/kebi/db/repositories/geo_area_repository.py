"""Geo registry repository — Protocol + SQLAlchemy implementation.

Each method opens its own session via session_factory so the registry works
in any context: request-path resolution, background minting, and the one-off
identity migration. All rows are global — a geographic unit is the same unit
for everyone.
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kebi.core.geo.models import GeoArea
from kebi.db.models import GeoAreaAliasRow, GeoAreaRow


def _to_model(row: GeoAreaRow) -> GeoArea:
    return GeoArea(
        place_id=row.place_id,
        provider=row.provider,
        country_code=row.country_code,
        slot=row.slot,
        kind=row.kind,
        name=row.name,
        colloquial_name=row.colloquial_name,
        groups_into=row.groups_into,
        split_of=row.split_of,
        city_place_id=row.city_place_id,
        geo_key=row.geo_key,
        legacy_key=row.legacy_key,
        lat=row.lat,
        lng=row.lng,
        viewport=list(row.viewport) if row.viewport else None,
        ambiguous=row.ambiguous,
    )


class GeoAreaRepository(Protocol):
    async def get(self, place_id: str) -> GeoArea | None: ...

    async def get_many(self, place_ids: list[str]) -> dict[str, GeoArea]: ...

    async def get_by_key(self, geo_key: str) -> GeoArea | None: ...

    async def get_by_keys(self, geo_keys: list[str]) -> dict[str, GeoArea]: ...

    async def get_by_legacy_key(self, legacy_key: str) -> GeoArea | None: ...

    async def get_splits(self, place_id: str) -> list[GeoArea]: ...

    async def lookup_alias(
        self, country_code: str, city_place_id: str, slug: str
    ) -> GeoArea | None: ...

    async def lookup_country_alias(self, slug: str) -> GeoArea | None: ...

    async def upsert(self, area: GeoArea) -> GeoArea: ...

    async def add_alias(
        self, country_code: str, city_place_id: str, slug: str, place_id: str
    ) -> None: ...

    async def set_ambiguous(self, place_id: str) -> None: ...

    async def set_legacy_key(self, place_id: str, legacy_key: str) -> None: ...


class SQLAlchemyGeoAreaRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, place_id: str) -> GeoArea | None:
        async with self._session_factory() as session:
            row = await session.get(GeoAreaRow, place_id)
        return _to_model(row) if row else None

    async def get_many(self, place_ids: list[str]) -> dict[str, GeoArea]:
        if not place_ids:
            return {}
        async with self._session_factory() as session:
            stmt = select(GeoAreaRow).where(GeoAreaRow.place_id.in_(place_ids))
            rows = (await session.execute(stmt)).scalars().all()
        return {row.place_id: _to_model(row) for row in rows}

    async def get_by_key(self, geo_key: str) -> GeoArea | None:
        async with self._session_factory() as session:
            stmt = select(GeoAreaRow).where(GeoAreaRow.geo_key == geo_key)
            row = (await session.execute(stmt)).scalar_one_or_none()
        return _to_model(row) if row else None

    async def get_by_keys(self, geo_keys: list[str]) -> dict[str, GeoArea]:
        if not geo_keys:
            return {}
        async with self._session_factory() as session:
            stmt = select(GeoAreaRow).where(GeoAreaRow.geo_key.in_(geo_keys))
            rows = (await session.execute(stmt)).scalars().all()
        return {row.geo_key: _to_model(row) for row in rows}

    async def get_by_legacy_key(self, legacy_key: str) -> GeoArea | None:
        """The row an old slug key maps to, or None.

        The column holds each row's primary legacy key; the `legacy`-scoped
        alias rows hold the additional variants (one area used to key under
        several slug spellings — that was the whole disease). Both are
        checked so every token minted before the id migration keeps
        resolving.
        """
        async with self._session_factory() as session:
            stmt = select(GeoAreaRow).where(GeoAreaRow.legacy_key == legacy_key)
            row = (await session.execute(stmt)).scalars().first()
            if row is None:
                alias_stmt = (
                    select(GeoAreaRow)
                    .join(
                        GeoAreaAliasRow,
                        GeoAreaAliasRow.place_id == GeoAreaRow.place_id,
                    )
                    .where(
                        GeoAreaAliasRow.city_place_id == "legacy",
                        GeoAreaAliasRow.slug == legacy_key,
                    )
                )
                row = (await session.execute(alias_stmt)).scalars().first()
        return _to_model(row) if row else None

    async def get_splits(self, place_id: str) -> list[GeoArea]:
        async with self._session_factory() as session:
            stmt = select(GeoAreaRow).where(GeoAreaRow.split_of == place_id)
            rows = (await session.execute(stmt)).scalars().all()
        return [_to_model(row) for row in rows]

    async def lookup_alias(
        self, country_code: str, city_place_id: str, slug: str
    ) -> GeoArea | None:
        async with self._session_factory() as session:
            stmt = (
                select(GeoAreaRow)
                .join(
                    GeoAreaAliasRow,
                    GeoAreaAliasRow.place_id == GeoAreaRow.place_id,
                )
                .where(
                    GeoAreaAliasRow.country_code == country_code,
                    GeoAreaAliasRow.city_place_id == city_place_id,
                    GeoAreaAliasRow.slug == slug,
                )
            )
            row = (await session.execute(stmt)).scalars().first()
        return _to_model(row) if row else None

    async def lookup_country_alias(self, slug: str) -> GeoArea | None:
        """Country-name lookup — the one alias scope queried without a
        country code, because the code is what's being resolved. The scope
        marker is the literal `country` (a place_id can never collide with
        it: ids are provider-issued and longer)."""
        async with self._session_factory() as session:
            stmt = (
                select(GeoAreaRow)
                .join(
                    GeoAreaAliasRow,
                    GeoAreaAliasRow.place_id == GeoAreaRow.place_id,
                )
                .where(
                    GeoAreaAliasRow.city_place_id == "country",
                    GeoAreaAliasRow.slug == slug,
                )
            )
            row = (await session.execute(stmt)).scalars().first()
        return _to_model(row) if row else None

    async def upsert(self, area: GeoArea) -> GeoArea:
        """Write one minted unit; idempotent by id.

        Two racers minting the same unit write the same provider facts, so
        last-write-wins is safe. `legacy_key` is deliberately not overwritten
        with None — a re-mint after the migration must not orphan old tokens.
        """
        values = {
            "place_id": area.place_id,
            "provider": area.provider,
            "country_code": area.country_code,
            "slot": area.slot,
            "kind": area.kind,
            "name": area.name,
            "colloquial_name": area.colloquial_name,
            "groups_into": area.groups_into,
            "split_of": area.split_of,
            "city_place_id": area.city_place_id,
            "geo_key": area.geo_key,
            "lat": area.lat,
            "lng": area.lng,
            "viewport": area.viewport,
            "ambiguous": area.ambiguous,
        }
        if area.legacy_key is not None:
            values["legacy_key"] = area.legacy_key
        update_values = {k: v for k, v in values.items() if k != "place_id"}
        stmt = (
            pg_insert(GeoAreaRow)
            .values(**values)
            .on_conflict_do_update(index_elements=["place_id"], set_=update_values)
            .returning(GeoAreaRow)
        )
        async with self._session_factory() as session:
            row = (await session.execute(stmt)).scalar_one()
            await session.commit()
            return _to_model(row)

    async def add_alias(
        self, country_code: str, city_place_id: str, slug: str, place_id: str
    ) -> None:
        """Record one verified way this unit has been asked for.

        First-write-wins on conflict: an alias that already resolves must not
        be silently repointed by a later, differently-verified ask — identity
        flips go through an explicit correction, not an upsert race.
        """
        stmt = (
            pg_insert(GeoAreaAliasRow)
            .values(
                country_code=country_code,
                city_place_id=city_place_id,
                slug=slug,
                place_id=place_id,
            )
            .on_conflict_do_nothing(
                index_elements=["country_code", "city_place_id", "slug"]
            )
        )
        async with self._session_factory() as session:
            await session.execute(stmt)
            await session.commit()

    async def set_ambiguous(self, place_id: str) -> None:
        async with self._session_factory() as session:
            row = await session.get(GeoAreaRow, place_id)
            if row is not None:
                row.ambiguous = True
                await session.commit()

    async def set_legacy_key(self, place_id: str, legacy_key: str) -> None:
        async with self._session_factory() as session:
            row = await session.get(GeoAreaRow, place_id)
            if row is not None:
                row.legacy_key = legacy_key
                await session.commit()
