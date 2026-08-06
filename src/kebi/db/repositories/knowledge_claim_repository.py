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

from sqlalchemy import (
    ColumnElement,
    Float,
    and_,
    cast,
    func,
    or_,
    select,
    true,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kebi.core.knowledge.schemas import (
    EntityType,
    KnowledgeClaim,
    ReviewStatus,
    SourceType,
)
from kebi.core.places.places_repo import PLACES_TABLE as _PlacesTable
from kebi.db.models import KnowledgeClaim as KnowledgeClaimRow
from kebi.db.models import KnowledgeEntityType, KnowledgeReviewStatus


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
        review_status=row.review_status.value,
        reviewed_by=row.reviewed_by,
        reviewed_at=row.reviewed_at,
        agree_count=row.agree_count,
        disagree_count=row.disagree_count,
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


def _approved_clause(approved_only: bool) -> ColumnElement[bool]:
    """Restrict to live (approved) claims when the reader asks (ADR-122).

    Off by default — no reader gates on review state yet; the future research
    tool passes `approved_only=True` so pending/rejected claims never surface.
    """
    if not approved_only:
        return true()
    return KnowledgeClaimRow.review_status == KnowledgeReviewStatus.APPROVED


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
        review_status: ReviewStatus = "approved",
    ) -> bool: ...

    async def list_for_entity(
        self,
        entity_key: str,
        user_id: str | None = None,
        approved_only: bool = False,
    ) -> list[KnowledgeClaim]: ...

    async def list_for_entities(
        self,
        entity_keys: list[str],
        user_id: str | None = None,
        approved_only: bool = False,
    ) -> list[KnowledgeClaim]: ...

    async def list_under_prefix(
        self,
        prefix: str,
        user_id: str | None = None,
        approved_only: bool = False,
    ) -> list[KnowledgeClaim]: ...

    async def list_place_claims_in_area(
        self,
        *,
        lat: float,
        lng: float,
        radius_m: float,
        user_id: str | None = None,
        approved_only: bool = True,
        limit: int = 200,
    ) -> list[tuple[str, KnowledgeClaim]]: ...


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
        review_status: ReviewStatus = "approved",
    ) -> bool:
        """Persist a claim via INSERT ON CONFLICT DO NOTHING.

        Returns True if a new row was written, False if the claim already
        existed under the dedup key (entity_key, claim, source_type, user_id).
        `review_status` (ADR-122) is set at write; `reviewed_by`/`reviewed_at`
        stay NULL until an actual review moves the claim.
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
                    review_status=review_status,
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
        self,
        entity_key: str,
        user_id: str | None = None,
        approved_only: bool = False,
    ) -> list[KnowledgeClaim]:
        async with self._session_factory() as session:
            stmt = select(KnowledgeClaimRow).where(
                and_(
                    KnowledgeClaimRow.entity_key == entity_key,
                    _scope_clause(user_id),
                    _approved_clause(approved_only),
                )
            )
            rows = (await session.execute(stmt)).scalars().all()
        return [_to_record(row) for row in rows]

    async def list_for_entities(
        self,
        entity_keys: list[str],
        user_id: str | None = None,
        approved_only: bool = False,
    ) -> list[KnowledgeClaim]:
        """Batch of `list_for_entity` — every claim for any of `entity_keys`
        in one `entity_key IN (...)` query, same scoping/approved rules. Lets a
        page of places (each keyed `place:<id>`) resolve its notes in one round
        trip instead of N. Returns `[]` for an empty key list."""
        if not entity_keys:
            return []
        async with self._session_factory() as session:
            stmt = select(KnowledgeClaimRow).where(
                and_(
                    KnowledgeClaimRow.entity_key.in_(entity_keys),
                    _scope_clause(user_id),
                    _approved_clause(approved_only),
                )
            )
            rows = (await session.execute(stmt)).scalars().all()
        return [_to_record(row) for row in rows]

    async def list_under_prefix(
        self,
        prefix: str,
        user_id: str | None = None,
        approved_only: bool = False,
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
                    _approved_clause(approved_only),
                )
            )
            rows = (await session.execute(stmt)).scalars().all()
        return [_to_record(row) for row in rows]

    async def list_place_claims_in_area(
        self,
        *,
        lat: float,
        lng: float,
        radius_m: float,
        user_id: str | None = None,
        approved_only: bool = True,
        limit: int = 200,
    ) -> list[tuple[str, KnowledgeClaim]]:
        """Place-scoped claims whose place sits inside a geofence (ADR-138).

        The inverse of every other read here: instead of "what does kebi know
        about this place", it answers "which places around here does kebi know
        anything about" — the query that lets a claim *surface* a venue rather
        than only annotate one already retrieved.

        Place claims are keyed `place:<id>`, so they carry no geo of their own
        and cannot be found by a prefix scan over geo keys. The join to
        `places` supplies the geography, reusing the same `earth_box`
        predicate as the catalog's own radius search so one turn's geofence
        means the same thing on both sides.

        Returns `(place_id, claim)` pairs, strongest claim first, so the
        caller can group per place without a second read.
        """
        place_key = func.concat("place:", _PlacesTable.c.id)
        geo_lat = cast(_PlacesTable.c.location["lat"].astext, Float())
        geo_lng = cast(_PlacesTable.c.location["lng"].astext, Float())
        query_box = func.earth_box(func.ll_to_earth(lat, lng), float(radius_m))

        stmt = (
            select(KnowledgeClaimRow, _PlacesTable.c.id.label("place_id"))
            .join(_PlacesTable, KnowledgeClaimRow.entity_key == place_key)
            .where(
                and_(
                    KnowledgeClaimRow.entity_type == KnowledgeEntityType.PLACE,
                    _scope_clause(user_id),
                    _approved_clause(approved_only),
                    _PlacesTable.c.location.isnot(None),
                    _PlacesTable.c.location["lat"].astext.isnot(None),
                    _PlacesTable.c.location["lng"].astext.isnot(None),
                    query_box.op("@>")(func.ll_to_earth(geo_lat, geo_lng)),
                )
            )
            .order_by(
                KnowledgeClaimRow.confidence.desc(),
                KnowledgeClaimRow.created_at.desc(),
            )
            .limit(limit)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            return [(row.place_id, _to_record(row[0])) for row in result.all()]
