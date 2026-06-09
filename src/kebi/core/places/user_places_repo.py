"""UserPlacesRepo — sole writer/reader of the user_places DB table."""

from __future__ import annotations

import logging

from sqlalchemy import and_, delete, literal, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ._cursor import LibraryCursor
from ._place_filters import (
    _p,
    _PlacesTable,
    _up,
    _UserPlacesTable,
    build_filter_conditions,
    row_to_place_core,
)
from .models import (
    PlaceSource,
    SavedPlaceFilters,
    SavedPlaceView,
    UserPlace,
)

logger = logging.getLogger(__name__)

# user_places columns — the canonical table ref + filter/row helpers live in
# `_place_filters` (shared with hybrid_search_repo). `_u` is the short alias
# this module's existing queries use.
_u = _up


class UserPlacesRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_user(self, user_id: str) -> list[UserPlace]:
        stmt = (
            select(_UserPlacesTable)
            .where(_u.user_id == user_id)
            .order_by(_u.saved_at.desc())
        )
        result = await self._session.execute(stmt)
        return [_row_to_user_place(row._mapping) for row in result]

    async def browse(
        self,
        user_id: str,
        filters: SavedPlaceFilters,
        limit: int,
        cursor: LibraryCursor | None = None,
    ) -> list[SavedPlaceView]:
        """Browse the user's saved places ⋈ catalog, filtered + keyset-paged.

        Ordered newest-first (`saved_at DESC, user_place_id DESC`). The
        `user_place_id` tie-break is load-bearing: `save_places` stamps one
        `saved_at` for an entire import batch, so paging on `saved_at` alone
        would skip or repeat rows at a page boundary. `cursor` is the keyset
        anchor of the previous page's last row; rows strictly *after* it (in
        the DESC order) are returned.
        """
        conditions = [_up.user_id == user_id, *build_filter_conditions(filters)]
        if cursor is not None:
            conditions.append(
                tuple_(_up.saved_at, _up.user_place_id)
                < tuple_(literal(cursor.saved_at), literal(cursor.user_place_id))
            )

        stmt = (
            select(
                _p.id,
                _p.provider_id,
                _p.place_name,
                _p.place_name_aliases,
                _p.categories,
                _p.tags,
                _p.location,
                _p.created_at,
                _p.refreshed_at,
                _up.user_place_id,
                _up.user_id,
                _up.place_id,
                _up.approved,
                _up.visited,
                _up.liked,
                _up.note,
                _up.source,
                _up.source_ref,
                _up.source_label,
                _up.saved_at,
                _up.visited_at,
            )
            .select_from(_PlacesTable.join(_UserPlacesTable, _up.place_id == _p.id))
            .where(and_(*conditions))
            .order_by(_up.saved_at.desc(), _up.user_place_id.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [
            SavedPlaceView(
                place=row_to_place_core(row._mapping),
                user_data=_row_to_user_place(row._mapping),
            )
            for row in result
        ]

    async def get_by_user_place_id(self, user_place_id: str) -> UserPlace | None:
        stmt = select(_UserPlacesTable).where(_u.user_place_id == user_place_id)
        result = await self._session.execute(stmt)
        row = result.mappings().first()
        return _row_to_user_place(row) if row else None

    async def get_existing_place_ids(
        self, user_id: str, place_ids: list[str]
    ) -> set[str]:
        """Return the subset of `place_ids` already saved by `user_id`.

        Targeted overlap query — avoids pulling the user's entire saved
        list just to check duplicates on a small incoming batch.
        """
        if not place_ids:
            return set()
        stmt = select(_u.place_id).where(
            _u.user_id == user_id, _u.place_id.in_(place_ids)
        )
        result = await self._session.execute(stmt)
        return {row[0] for row in result}

    async def save_user_places(self, user_places: list[UserPlace]) -> list[UserPlace]:
        """INSERT or UPDATE on user_place_id primary key.

        Rolls back the session on any execute error before re-raising —
        asyncpg leaves the transaction in an aborted state on errors
        like a foreign-key violation, and every subsequent statement on
        the same connection fails with `InFailedSQLTransactionError`
        until rollback. ADR-074's stale-cache fallback path retries on
        the same request-scoped session, so this is load-bearing.
        """
        if not user_places:
            return []

        rows = [_user_place_to_dict(up) for up in user_places]
        insert_stmt = pg_insert(_UserPlacesTable).values(rows)
        excl = insert_stmt.excluded

        stmt = insert_stmt.on_conflict_do_update(
            index_elements=["user_place_id"],
            set_={
                "approved": excl.approved,
                "visited": excl.visited,
                "liked": excl.liked,
                "note": excl.note,
                "visited_at": excl.visited_at,
            },
        ).returning(*_UserPlacesTable.c)

        try:
            result = await self._session.execute(stmt)
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        return [_row_to_user_place(row._mapping) for row in result]

    async def delete_by_user(self, user_id: str) -> int:
        """Hard-delete every `user_places` row for `user_id`. Returns the
        number of rows removed.

        Idempotent — deleting for a user with no saved places is a 0-row
        no-op. Unlike `save_user_places`, this does NOT commit: the caller
        owns the transaction so the account-erase sweep can wipe
        `user_places` atomically alongside the other AI tables.
        """
        stmt = delete(_UserPlacesTable).where(_u.user_id == user_id)
        result = await self._session.execute(stmt)
        return result.rowcount or 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user_place_to_dict(up: UserPlace) -> dict[str, object]:
    return {
        "user_place_id": up.user_place_id,
        "user_id": up.user_id,
        "place_id": up.place_id,
        "approved": up.approved,
        "visited": up.visited,
        "liked": up.liked,
        "note": up.note,
        "source": up.source.value,
        "source_ref": up.source_ref,
        "source_label": up.source_label,
        "saved_at": up.saved_at,
        "visited_at": up.visited_at,
    }


def _row_to_user_place(row: object) -> UserPlace:
    from collections.abc import Mapping

    m = dict(row) if isinstance(row, Mapping) else vars(row)
    return UserPlace(
        user_place_id=m["user_place_id"],
        user_id=m["user_id"],
        place_id=m["place_id"],
        approved=bool(m.get("approved", True)),
        visited=bool(m.get("visited", False)),
        liked=m.get("liked"),
        note=m.get("note"),
        source=PlaceSource(m["source"]),
        source_ref=m.get("source_ref"),
        source_label=m.get("source_label"),
        saved_at=m["saved_at"],
        visited_at=m.get("visited_at"),
    )
