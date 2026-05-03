"""PlacesRepo — sole writer/reader of the places_v2 DB table."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    Column,
    ColumnElement,
    DateTime,
    Float,
    MetaData,
    String,
    Table,
    and_,
    cast,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    LocationContext,
    PlaceCategory,
    PlaceCore,
    PlaceNameAlias,
    PlaceQuery,
    PlaceTag,
    SortField,
)

# ---------------------------------------------------------------------------
# Table reference — typed columns for native query building
# ---------------------------------------------------------------------------
_metadata = MetaData()
_PlacesV2Table = Table(
    "places_v2",
    _metadata,
    Column("id", String),
    Column("provider_id", String),
    Column("place_name", String),
    Column("place_name_aliases", JSONB),
    Column("category", String),
    Column("tags", JSONB),
    Column("location", JSONB),
    Column("created_at", DateTime(timezone=True)),
    Column("refreshed_at", DateTime(timezone=True)),
)
_t = _PlacesV2Table.c

# Allowlist of legal sort keys. Decouples the public sort literal from the
# underlying column/expression — values can become computed expressions
# (e.g. func.coalesce(...)) without breaking the API contract.
_SORT_COLUMNS: dict[SortField, ColumnElement[Any]] = {
    "created_at": _t.created_at,
    "refreshed_at": _t.refreshed_at,
    "place_name": _t.place_name,
    "category": _t.category,
}

# JSONB tag containment in find() encodes the PlaceTag.value field name as
# a literal SQL string. If the Pydantic field is renamed, the query would
# silently match zero rows. Fail loudly at import instead.
assert "value" in PlaceTag.model_fields, (
    "PlaceTag.value field renamed — update tag JSONB containment in find()"
)


class PlacesRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def get_by_ids(self, place_ids: list[str]) -> list[PlaceCore]:
        if not place_ids:
            return []
        stmt = select(_PlacesV2Table).where(_t.id.in_(place_ids))
        result = await self._session.execute(stmt)
        return [_row_to_core(row._mapping) for row in result]

    async def get_by_provider_ids(
        self, provider_ids: list[str]
    ) -> dict[str, PlaceCore]:
        if not provider_ids:
            return {}
        stmt = select(_PlacesV2Table).where(_t.provider_id.in_(provider_ids))
        result = await self._session.execute(stmt)
        return {
            row._mapping["provider_id"]: _row_to_core(row._mapping) for row in result
        }

    async def find(self, query: PlaceQuery, limit: int = 20) -> list[PlaceCore]:
        conditions: list[ColumnElement[bool]] = []

        if query.place_name:
            conditions.append(_t.place_name.ilike(f"%{query.place_name}%"))

        if query.category:
            conditions.append(_t.category == query.category.value)

        if query.tags:
            # AND semantics: every requested tag value must be present
            for tag_val in query.tags:
                conditions.append(
                    _t.tags.op("@>")(
                        cast(json.dumps([{"value": tag_val}]), JSONB)
                    )
                )

        loc = query.location
        if loc and loc.city:
            conditions.append(_t.location["city"].astext.ilike(f"%{loc.city}%"))
        if loc and loc.country:
            conditions.append(_t.location["country"].astext == loc.country)
        if loc and loc.neighborhood:
            conditions.append(
                _t.location["neighborhood"].astext.ilike(f"%{loc.neighborhood}%")
            )

        if (
            loc
            and loc.lat is not None
            and loc.lng is not None
            and loc.radius_m is not None
        ):
            geo_lat = cast(_t.location["lat"].astext, Float())
            geo_lng = cast(_t.location["lng"].astext, Float())
            query_box = func.earth_box(
                func.ll_to_earth(loc.lat, loc.lng), float(loc.radius_m)
            )
            conditions.extend(
                [
                    query_box.op("@>")(func.ll_to_earth(geo_lat, geo_lng)),
                    _t.location.isnot(None),
                    _t.location["lat"].astext.isnot(None),
                    _t.location["lng"].astext.isnot(None),
                ]
            )

        if query.created_after:
            conditions.append(_t.created_at >= query.created_after)
        if query.created_before:
            conditions.append(_t.created_at <= query.created_before)

        stmt = select(_PlacesV2Table)
        if conditions:
            stmt = stmt.where(and_(*conditions))

        sort_col = _SORT_COLUMNS[query.sort_by] if query.sort_by else _t.created_at
        stmt = stmt.order_by(sort_col.desc() if query.sort_desc else sort_col.asc())

        result = await self._session.execute(stmt.limit(limit))
        return [_row_to_core(row._mapping) for row in result]

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def upsert_places(self, cores: list[PlaceCore]) -> list[PlaceCore]:
        """Bulk write with no merge policy — overwrites mutable columns from input.

        The caller (PlaceUpsertService) is responsible for computing the final
        merged state via merge_place before calling this. The repo only persists.

        Requires provider_id on every row — the conflict target is the partial
        unique index on provider_id, and a NULL provider_id would silently
        create duplicate rows. RETURNING order is not guaranteed.
        """
        if not cores:
            return []

        missing = [c.place_name for c in cores if c.provider_id is None]
        if missing:
            raise ValueError(
                f"upsert_places requires provider_id on every row; "
                f"missing on {len(missing)} candidate(s): {missing}"
            )

        now = datetime.now(UTC)
        rows = [_core_to_dict(c, now) for c in cores]

        insert_stmt = pg_insert(_PlacesV2Table).values(rows)
        excl = insert_stmt.excluded

        # On conflict, overwrite every mutable column from the candidate.
        # id, provider_id, created_at are immutable post-insert.
        stmt = insert_stmt.on_conflict_do_update(
            index_elements=["provider_id"],
            index_where=_t.provider_id.isnot(None),
            set_={
                "place_name": excl.place_name,
                "place_name_aliases": excl.place_name_aliases,
                "category": excl.category,
                "tags": excl.tags,
                "location": excl.location,
                "refreshed_at": excl.refreshed_at,
            },
        ).returning(*_PlacesV2Table.c)

        result = await self._session.execute(stmt)
        await self._session.commit()
        return [_row_to_core(row._mapping) for row in result]

    async def wipe_stale_locations(self, cutoff: datetime) -> list[PlaceCore]:
        """Strip Google-derived location from rows last refreshed before cutoff.

        Sets ``location = NULL`` and ``refreshed_at = NULL`` so the row drops
        into the "needs refresh" branch of `find()` next time it's read. The
        ``location IS NOT NULL`` filter makes this idempotent — re-running
        the wipe doesn't touch already-cleared rows.

        Returns the wiped rows (with location/refreshed_at already cleared)
        so the caller can align cache state with the DB.
        """
        stmt = (
            update(_PlacesV2Table)
            .where(
                and_(
                    _t.location.isnot(None),
                    _t.refreshed_at < cutoff,
                )
            )
            .values(location=None, refreshed_at=None)
            .returning(*_PlacesV2Table.c)
        )
        result = await self._session.execute(stmt)
        await self._session.commit()
        return [_row_to_core(row._mapping) for row in result]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _core_to_dict(core: PlaceCore, now: datetime) -> dict[str, object]:
    loc = core.location
    return {
        "id": core.id or str(uuid4()),
        "provider_id": core.provider_id,
        "place_name": core.place_name,
        "place_name_aliases": [a.model_dump() for a in core.place_name_aliases]
        or None,
        "category": core.category.value if core.category else None,
        "tags": [t.model_dump() for t in core.tags] or None,
        "location": loc.model_dump(exclude_none=True) if loc else None,
        "created_at": core.created_at or now,
        "refreshed_at": core.refreshed_at
        or (now if loc and loc.lat is not None else None),
    }


def _row_to_core(row: object) -> PlaceCore:
    from collections.abc import Mapping

    m = dict(row) if isinstance(row, Mapping) else vars(row)
    tags = [PlaceTag.model_validate(t) for t in (m.get("tags") or [])]
    aliases = [
        PlaceNameAlias.model_validate(a)
        for a in (m.get("place_name_aliases") or [])
    ]
    loc_raw = m.get("location")
    location = LocationContext.model_validate(loc_raw) if loc_raw else None
    return PlaceCore(
        id=m.get("id"),
        provider_id=m.get("provider_id"),
        place_name=m["place_name"],
        place_name_aliases=aliases,
        category=PlaceCategory(m["category"]) if m.get("category") else None,
        tags=tags,
        location=location,
        created_at=m.get("created_at"),
        refreshed_at=m.get("refreshed_at"),
    )
