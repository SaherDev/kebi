"""Knowledge-claim repository — Protocol + SQLAlchemy implementation (ADR-120).

Each method opens its own session via session_factory so it works in any
context (request, background event handler). Reads enforce the claim-scoping
rule at the query: a global claim (user_id NULL) is always visible; a
user-scoped claim is only visible to that same user, never leaked across
callers.
"""

from __future__ import annotations

from typing import Protocol
from uuid import uuid4

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kebi.core.knowledge.schemas import EntityType, KnowledgeClaim, SourceType
from kebi.db.models import KnowledgeClaim as KnowledgeClaimRow


def _to_record(row: KnowledgeClaimRow) -> KnowledgeClaim:
    return KnowledgeClaim(
        id=row.id,
        entity_type=row.entity_type.value,
        entity_key=row.entity_key,
        entity_name=row.entity_name,
        claim=row.claim,
        tags=list(row.tags or []),
        source_type=row.source_type.value,
        source_ref=row.source_ref,
        confidence=row.confidence,
        user_id=row.user_id,
        created_at=row.created_at,
    )


def _scope_clause(user_id: str | None) -> ColumnElement[bool]:
    """Global rows, plus this user's own rows when a user_id is given."""
    if user_id is None:
        return KnowledgeClaimRow.user_id.is_(None)
    return or_(
        KnowledgeClaimRow.user_id.is_(None),
        KnowledgeClaimRow.user_id == user_id,
    )


class KnowledgeClaimRepository(Protocol):
    async def save(
        self,
        entity_type: EntityType,
        entity_key: str,
        entity_name: str,
        claim: str,
        source_type: SourceType,
        confidence: float,
        tags: list[str] | None = None,
        source_ref: str | None = None,
        user_id: str | None = None,
    ) -> bool: ...

    async def list_for_entity(
        self, entity_key: str, user_id: str | None = None
    ) -> list[KnowledgeClaim]: ...

    async def list_under_prefix(
        self, prefix: str, user_id: str | None = None
    ) -> list[KnowledgeClaim]: ...


class SQLAlchemyKnowledgeClaimRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save(
        self,
        entity_type: EntityType,
        entity_key: str,
        entity_name: str,
        claim: str,
        source_type: SourceType,
        confidence: float,
        tags: list[str] | None = None,
        source_ref: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        """Persist a claim via INSERT ON CONFLICT DO NOTHING.

        Returns True if a new row was written, False if the claim already
        existed under the dedup key (entity_key, claim, source_type, user_id).
        """
        async with self._session_factory() as session:
            stmt = (
                pg_insert(KnowledgeClaimRow)
                .values(
                    id=str(uuid4()),
                    entity_type=entity_type,
                    entity_key=entity_key,
                    entity_name=entity_name,
                    claim=claim,
                    tags=tags or [],
                    source_type=source_type,
                    source_ref=source_ref,
                    confidence=confidence,
                    user_id=user_id,
                )
                .on_conflict_do_nothing(
                    index_elements=["entity_key", "claim", "source_type", "user_id"],
                )
                .returning(KnowledgeClaimRow.id)
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.first() is not None

    async def list_for_entity(
        self, entity_key: str, user_id: str | None = None
    ) -> list[KnowledgeClaim]:
        async with self._session_factory() as session:
            stmt = select(KnowledgeClaimRow).where(
                and_(
                    KnowledgeClaimRow.entity_key == entity_key,
                    _scope_clause(user_id),
                )
            )
            rows = (await session.execute(stmt)).scalars().all()
        return [_to_record(row) for row in rows]

    async def list_under_prefix(
        self, prefix: str, user_id: str | None = None
    ) -> list[KnowledgeClaim]:
        """Geo prefix scan — e.g. prefix "ae/dubai" also matches "ae/dubai/jumeirah"."""
        async with self._session_factory() as session:
            stmt = select(KnowledgeClaimRow).where(
                and_(
                    or_(
                        KnowledgeClaimRow.entity_key == prefix,
                        KnowledgeClaimRow.entity_key.like(f"{prefix}/%"),
                    ),
                    _scope_clause(user_id),
                )
            )
            rows = (await session.execute(stmt)).scalars().all()
        return [_to_record(row) for row in rows]
