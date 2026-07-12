"""User-intent repository — Protocol + SQLAlchemy implementation (ADR-110).

Backs the home screen's "what you wanted" recall list. Each method opens its
own session via session_factory so it works in any context (request,
background event handler). Reads are newest-first and keyset-paged on
`(created_at, id)`; the opaque cursor is owned here so no other layer
re-implements the paging math (mirrors the Library cursor pattern).
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kebi.core.user.intent_models import IntentRecord
from kebi.db.models import UserIntent

_SEP = "|"


@dataclass(frozen=True)
class IntentCursor:
    """Keyset anchor for the recall list — the `(created_at, id)` of the last
    row of a page, under the fixed newest-first ordering.

    Opaque on the wire. `decode` raises `ValueError` on any malformed input
    (the API layer lets that surface through the shared `ValueError → 400`
    handler), matching the Library cursor's contract.
    """

    created_at: datetime
    intent_id: str

    def encode(self) -> str:
        raw = f"{self.created_at.isoformat()}{_SEP}{self.intent_id}".encode()
        return base64.urlsafe_b64encode(raw).decode()

    @classmethod
    def decode(cls, token: str) -> IntentCursor:
        try:
            raw = base64.urlsafe_b64decode(token.encode()).decode()
            ts_raw, sep, intent_id = raw.partition(_SEP)
            if not sep or not ts_raw or not intent_id:
                raise ValueError("missing field")
            return cls(datetime.fromisoformat(ts_raw), intent_id)
        except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
            raise ValueError(f"invalid intent cursor: {token!r}") from exc


class UserIntentRepository(Protocol):
    async def add(
        self, user_id: str, text: str, metadata: dict[str, Any] | None = None
    ) -> None: ...

    async def list_page(
        self, user_id: str, limit: int, cursor: str | None
    ) -> tuple[list[IntentRecord], str | None]: ...

    async def latest(self, user_id: str) -> IntentRecord | None: ...

    async def delete_by_user(self, user_id: str) -> None: ...


class SQLAlchemyUserIntentRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def add(
        self, user_id: str, text: str, metadata: dict[str, Any] | None = None
    ) -> None:
        async with self._session_factory() as session:
            session.add(
                UserIntent(
                    id=str(uuid4()),
                    user_id=user_id,
                    text=text,
                    metadata_=metadata,
                )
            )
            await session.commit()

    async def list_page(
        self, user_id: str, limit: int, cursor: str | None
    ) -> tuple[list[IntentRecord], str | None]:
        """One newest-first page plus the cursor for the next page (or None).

        Keyset on `(created_at DESC, id DESC)`; fetches `limit + 1` rows to
        decide whether a further page exists without a second count query.
        """
        async with self._session_factory() as session:
            stmt = select(UserIntent).where(UserIntent.user_id == user_id)
            if cursor is not None:
                anchor = IntentCursor.decode(cursor)
                stmt = stmt.where(
                    or_(
                        UserIntent.created_at < anchor.created_at,
                        and_(
                            UserIntent.created_at == anchor.created_at,
                            UserIntent.id < anchor.intent_id,
                        ),
                    )
                )
            stmt = stmt.order_by(
                UserIntent.created_at.desc(), UserIntent.id.desc()
            ).limit(limit + 1)
            rows = list((await session.execute(stmt)).scalars().all())

        has_more = len(rows) > limit
        page = rows[:limit]
        records = [
            IntentRecord(id=r.id, text=r.text, created_at=r.created_at) for r in page
        ]
        next_cursor = (
            IntentCursor(page[-1].created_at, page[-1].id).encode()
            if has_more
            else None
        )
        return records, next_cursor

    async def latest(self, user_id: str) -> IntentRecord | None:
        async with self._session_factory() as session:
            stmt = (
                select(UserIntent)
                .where(UserIntent.user_id == user_id)
                .order_by(UserIntent.created_at.desc(), UserIntent.id.desc())
                .limit(1)
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        return IntentRecord(id=row.id, text=row.text, created_at=row.created_at)

    async def delete_by_user(self, user_id: str) -> None:
        from sqlalchemy import delete

        async with self._session_factory() as session:
            await session.execute(
                delete(UserIntent).where(UserIntent.user_id == user_id)
            )
            await session.commit()
