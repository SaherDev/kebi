"""Repository abstractions for user memory persistence (ADR-038).

Each method opens its own session via session_factory so it works in any
context (request, background task, debouncer).
"""

from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class UserMemoryRepository(Protocol):
    """Protocol for user memory persistence (ADR-038)."""

    async def save(
        self,
        user_id: str,
        memory: str,
        source: str,
        confidence: float,
    ) -> None: ...

    async def load(self, user_id: str) -> list[str]: ...


class NullUserMemoryRepository:
    """No-op implementation for testing (ADR-038)."""

    async def save(
        self,
        user_id: str,
        memory: str,
        source: str,
        confidence: float,
    ) -> None:
        pass

    async def load(self, user_id: str) -> list[str]:
        return []


class SQLAlchemyUserMemoryRepository:
    """SQLAlchemy async implementation with deduplication (ADR-038).

    Takes session_factory — each method opens/commits/closes its own session.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save(
        self,
        user_id: str,
        memory: str,
        source: str,
        confidence: float,
    ) -> None:
        """Persist a personal fact using INSERT ON CONFLICT DO NOTHING."""
        from uuid import uuid4

        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from kebi.db.models import UserMemory

        async with self._session_factory() as session:
            stmt = (
                pg_insert(UserMemory)
                .values(
                    id=str(uuid4()),
                    user_id=user_id,
                    memory=memory,
                    source=source,
                    confidence=confidence,
                )
                .on_conflict_do_nothing(
                    index_elements=["user_id", "memory"],
                )
            )
            await session.execute(stmt)
            await session.commit()

    async def load(self, user_id: str) -> list[str]:
        """Load all stored memory strings for user_id, ordered by created_at ASC."""
        from kebi.db.models import UserMemory

        try:
            async with self._session_factory() as session:
                stmt = (
                    select(UserMemory.memory)
                    .where(UserMemory.user_id == user_id)
                    .order_by(UserMemory.created_at.asc())
                )
                result = await session.execute(stmt)
                return [row[0] for row in result.all()]
        except Exception:
            return []
