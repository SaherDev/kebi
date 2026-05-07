"""UserPlacesRepo — sole writer/reader of the user_places DB table."""

from __future__ import annotations

import logging

from sqlalchemy import Boolean, Column, DateTime, MetaData, String, Table, Text, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import PlaceSource, UserPlace

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Table reference — typed columns for native query building
# ---------------------------------------------------------------------------
_metadata = MetaData()
_UserPlacesTable = Table(
    "user_places",
    _metadata,
    Column("user_place_id", String),
    Column("user_id", String),
    Column("place_id", String),
    Column("approved", Boolean),
    Column("visited", Boolean),
    Column("liked", Boolean),
    Column("note", Text),
    Column("source", String),
    Column("source_url", Text),
    Column("saved_at", DateTime(timezone=True)),
    Column("visited_at", DateTime(timezone=True)),
)
_u = _UserPlacesTable.c


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

    async def get_by_user_place_id(self, user_place_id: str) -> UserPlace | None:
        stmt = select(_UserPlacesTable).where(_u.user_place_id == user_place_id)
        result = await self._session.execute(stmt)
        row = result.mappings().first()
        return _row_to_user_place(row) if row else None

    async def save_user_places(
        self, user_places: list[UserPlace]
    ) -> list[UserPlace]:
        """INSERT or UPDATE on user_place_id primary key."""
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

        result = await self._session.execute(stmt)
        await self._session.commit()
        return [_row_to_user_place(row._mapping) for row in result]


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
        "source_url": up.source_url,
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
        source_url=m.get("source_url"),
        saved_at=m["saved_at"],
        visited_at=m.get("visited_at"),
    )
