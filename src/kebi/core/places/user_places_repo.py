"""UserPlacesRepo — sole writer/reader of the user_places DB table."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, delete, func, literal, select, tuple_, update
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
    LibrarySort,
    PlaceSource,
    SavedPlaceFilters,
    SavedPlaceView,
    UserPlace,
    UserPlaceStatusUpdate,
)

logger = logging.getLogger(__name__)

# user_places columns — the canonical table ref + filter/row helpers live in
# `_place_filters` (shared with hybrid_search_repo). `_u` is the short alias
# this module's existing queries use.
_u = _up


@dataclass(frozen=True)
class _SortSpec:
    """How one `LibrarySort` maps to SQL: the primary order key, its
    direction, and how to read a cursor anchor back into a typed value.

    The same `primary` expression drives both the ORDER BY and the keyset
    comparison, so they can never drift. `user_place_id` is the implicit,
    always-present tie-break (saves in one import batch share a `saved_at`,
    and place names are not unique). Both keys sort the same direction, so a
    row-value comparison resumes paging: `<` for descending, `>` for
    ascending.
    """

    primary: Any
    descending: bool
    parse_anchor: Callable[[str], Any]


# `func.lower(place_name)` is built once and reused for ORDER BY and keyset —
# case-insensitive A–Z; the cursor stores the already-lowered name to match.
_SORT_SPECS: dict[LibrarySort, _SortSpec] = {
    LibrarySort.recent: _SortSpec(
        primary=_up.saved_at, descending=True, parse_anchor=datetime.fromisoformat
    ),
    LibrarySort.name: _SortSpec(
        primary=func.lower(_p.place_name), descending=False, parse_anchor=lambda s: s
    ),
}


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

    async def pill_state(
        self, user_id: str
    ) -> list[tuple[str, bool, bool, bool | None]]:
        """Lightweight taste-relevant projection: (place_id, approved, visited,
        liked) for every save the user holds.

        Feeds the taste regen stale-guard's pill fingerprint — a change to any
        of these columns must trigger a re-aggregation, but `note`/`source`/
        timestamps do not affect taste, so they are deliberately excluded.
        """
        stmt = select(_u.place_id, _u.approved, _u.visited, _u.liked).where(
            _u.user_id == user_id
        )
        result = await self._session.execute(stmt)
        return [(r.place_id, r.approved, r.visited, r.liked) for r in result]

    async def browse(
        self,
        user_id: str,
        filters: SavedPlaceFilters,
        limit: int,
        cursor: LibraryCursor | None = None,
        sort: LibrarySort = LibrarySort.recent,
    ) -> list[SavedPlaceView]:
        """Browse the user's saved places ⋈ catalog, filtered + keyset-paged.

        `sort` chooses the order: `recent` (newest-saved first, the default)
        or `name` (case-insensitive A–Z). Either way `user_place_id` is the
        load-bearing tie-break: `save_places` stamps one `saved_at` for a whole
        import batch and place names are not unique, so paging on the primary
        key alone would skip or repeat rows at a page boundary. `cursor` is the
        keyset anchor of the previous page's last row; rows strictly *after* it
        (in the active order) are returned. A cursor minted under a different
        sort is rejected (`ValueError`) — switching the toggle restarts paging.
        """
        spec = _SORT_SPECS[sort]
        key = tuple_(spec.primary, _up.user_place_id)

        conditions = [_up.user_id == user_id, *build_filter_conditions(filters)]
        if cursor is not None:
            if cursor.sort is not sort:
                raise ValueError(
                    f"cursor sort {cursor.sort.value!r} does not match requested "
                    f"sort {sort.value!r}; restart paging without the cursor"
                )
            bound = tuple_(
                literal(spec.parse_anchor(cursor.anchor)),
                literal(cursor.user_place_id),
            )
            conditions.append(key < bound if spec.descending else key > bound)

        def _dir(col: Any) -> Any:
            return col.desc() if spec.descending else col.asc()

        stmt = (
            select(
                _p.id,
                _p.provider_id,
                _p.place_name,
                _p.place_name_aliases,
                _p.categories,
                _p.tags,
                _p.icon,
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
            .order_by(_dir(spec.primary), _dir(_up.user_place_id))
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

    async def count_by_user(self, user_id: str) -> int:
        """Count every save the user holds — unfiltered grand total.

        Deliberately ignores `SavedPlaceFilters` and the keyset cursor: this
        is the whole-library size that drives the screen's hero count, the
        same on every page regardless of the filters narrowing the rows. No
        join to `places` — a save with a missing catalog row still counts as
        one of the user's saves.
        """
        stmt = (
            select(func.count())
            .select_from(_UserPlacesTable)
            .where(_u.user_id == user_id)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()

    async def count_filtered(self, user_id: str, filters: SavedPlaceFilters) -> int:
        """How many saves match `filters` across the whole library.

        The counterpart to `count_by_user`: that one is the unfiltered hero
        number, this one is "3 of 84"'s left-hand side. Same predicate and
        same join as `browse` — deliberately, because a count that disagrees
        with the rows it describes is worse than no count. The join means a
        save whose catalog row is missing is excluded here exactly as it is
        from `browse`, so the number can never exceed what paging can reach.

        Ignores the cursor: this counts the whole result set, not a page.

        When the predicate narrows nothing — no search, no filters — this is
        the unfiltered total by definition, so it delegates rather than
        issuing a second aggregate for the same answer. Asking
        `build_filter_conditions` whether it produced any SQL keeps that
        judgement in one place: a filter that builds no condition (an empty
        category list, a whitespace-only needle) is correctly a no-op here
        too, with no second list of "what counts as set" to drift.
        """
        filter_conditions = build_filter_conditions(filters)
        if not filter_conditions:
            return await self.count_by_user(user_id)

        conditions = [_up.user_id == user_id, *filter_conditions]
        stmt = (
            select(func.count())
            .select_from(_PlacesTable.join(_UserPlacesTable, _up.place_id == _p.id))
            .where(and_(*conditions))
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()

    async def update_fields(
        self, user_place_id: str, user_id: str, changes: UserPlaceStatusUpdate
    ) -> UserPlace | None:
        """Update the set fields of a single saved place the caller owns.

        Ownership IS the predicate, same as `delete_one`: the row is matched
        on `(user_place_id, user_id)`, so a save owned by another user matches
        nothing and the update is a no-op returning None — no read-then-write
        window and no existence leak. Only the explicitly-set fields of
        `changes` are written (`model_dump(exclude_unset=True)`); an unset
        field is left as-is, an explicit `None` clears the column. Returns the
        updated row, or None when nothing matched (absent or not theirs).

        Owns its transaction (commits here), like the other route-driven
        writes; rolls back on error so a failed statement does not strand the
        session for any retry on the same connection.
        """
        values = changes.model_dump(exclude_unset=True)
        if not values:
            # Nothing to write — an empty UPDATE is invalid SQL. The API
            # boundary already rejects an empty patch (422); this guards the
            # data layer if a caller reaches it another way.
            existing = await self._get_owned(user_place_id, user_id)
            return existing

        stmt = (
            update(_UserPlacesTable)
            .where(_u.user_place_id == user_place_id, _u.user_id == user_id)
            .values(**values)
            .returning(*_UserPlacesTable.c)
        )
        try:
            result = await self._session.execute(stmt)
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        row = result.mappings().first()
        return _row_to_user_place(row) if row else None

    async def _get_owned(self, user_place_id: str, user_id: str) -> UserPlace | None:
        stmt = select(_UserPlacesTable).where(
            _u.user_place_id == user_place_id, _u.user_id == user_id
        )
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

    async def get_by_user_and_place(
        self, user_id: str, place_id: str
    ) -> UserPlace | None:
        """Return the caller's existing save for `place_id`, or None.

        Single-row lookup keyed on `(user_id, place_id)` — the idempotency
        read behind `save_one`: a re-tap of "save it" finds the existing row
        and returns it rather than inserting a duplicate.
        """
        stmt = select(_UserPlacesTable).where(
            _u.user_id == user_id, _u.place_id == place_id
        )
        result = await self._session.execute(stmt)
        row = result.mappings().first()
        return _row_to_user_place(row) if row else None

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

    async def delete_one(self, user_place_id: str, user_id: str) -> int:
        """Hard-delete a single saved place the caller owns. Returns the
        number of rows removed (1 on success, 0 if absent or not theirs).

        Ownership IS the predicate: the `user_id` clause means a caller can
        only ever delete their own save — a valid `user_place_id` belonging
        to another user matches nothing and removes 0 rows (no IDOR, no
        existence leak). Single statement — no read-then-check, so no
        TOCTOU window. Owns its transaction (commits here): unlike
        `delete_by_user`, this is driven by a single-row route, not the
        multi-table account-erase sweep.
        """
        stmt = delete(_UserPlacesTable).where(
            _u.user_place_id == user_place_id,
            _u.user_id == user_id,
        )
        try:
            result = await self._session.execute(stmt)
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        return result.rowcount or 0

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
