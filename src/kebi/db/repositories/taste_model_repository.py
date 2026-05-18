"""Taste model repository — Protocol + SQLAlchemy implementation (ADR-058).

Each method opens its own session via session_factory so it works in any
context (request, background task, debouncer).
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kebi.core.places.models import PlaceAttributes
from kebi.core.taste.schemas import InteractionRow
from kebi.db.models import Interaction, InteractionType, Place, TasteModel


class TasteModelRepository(Protocol):
    async def get_by_user_id(self, user_id: str) -> TasteModel | None: ...

    async def upsert_regen(
        self,
        user_id: str,
        signal_counts: dict[str, Any],
        summary: list[dict[str, Any]],
        log_count: int,
    ) -> None: ...

    async def log_interaction(
        self,
        user_id: str,
        interaction_type: InteractionType,
        place_id: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...

    async def get_interactions_with_places(
        self, user_id: str
    ) -> list[InteractionRow]: ...

    async def count_interactions(self, user_id: str) -> int: ...


class SQLAlchemyTasteModelRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_by_user_id(self, user_id: str) -> TasteModel | None:
        async with self._session_factory() as session:
            stmt = select(TasteModel).where(TasteModel.user_id == user_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def upsert_regen(
        self,
        user_id: str,
        signal_counts: dict[str, Any],
        summary: list[dict[str, Any]],
        log_count: int,
    ) -> None:
        async with self._session_factory() as session:
            stmt = (
                pg_insert(TasteModel)
                .values(
                    user_id=user_id,
                    signal_counts=signal_counts,
                    taste_profile_summary=summary,
                    generated_at=func.now(),
                    generated_from_log_count=log_count,
                )
                .on_conflict_do_update(
                    index_elements=["user_id"],
                    set_={
                        "signal_counts": signal_counts,
                        "taste_profile_summary": summary,
                        "generated_at": func.now(),
                        "generated_from_log_count": log_count,
                    },
                )
            )
            await session.execute(stmt)
            await session.commit()

    async def log_interaction(
        self,
        user_id: str,
        interaction_type: InteractionType,
        place_id: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        async with self._session_factory() as session:
            interaction = Interaction(
                user_id=user_id,
                type=interaction_type,
                place_id=place_id,
                metadata_=metadata,
            )
            session.add(interaction)
            await session.commit()

    async def get_interactions_with_places(self, user_id: str) -> list[InteractionRow]:
        async with self._session_factory() as session:
            stmt = (
                select(
                    Interaction.type,
                    Place.place_type,
                    Place.subcategory,
                    Place.source,
                    Place.tags,
                    Place.attributes,
                )
                .join(Place, Interaction.place_id == Place.id)
                .where(Interaction.user_id == user_id)
                .order_by(Interaction.created_at)
            )
            result = await session.execute(stmt)
            rows: list[InteractionRow] = []
            for row in result:
                attrs = PlaceAttributes(**(row.attributes or {}))
                rows.append(
                    InteractionRow(
                        type=(
                            row.type.value if hasattr(row.type, "value") else row.type
                        ),
                        place_type=row.place_type,
                        subcategory=row.subcategory,
                        source=row.source,
                        tags=row.tags or [],
                        attributes=attrs,
                    )
                )
            return rows

    async def count_interactions(self, user_id: str) -> int:
        async with self._session_factory() as session:
            stmt = (
                select(func.count())
                .select_from(Interaction)
                .where(Interaction.user_id == user_id)
            )
            result = await session.execute(stmt)
            return result.scalar_one()
