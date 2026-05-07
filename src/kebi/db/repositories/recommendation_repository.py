"""Repository pattern for Recommendation model (ADR-019, ADR-060).

Provides Protocol abstraction, a no-op stub, and the real SQLAlchemy
implementation for persisting recommendation records.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from kebi.db.models import Recommendation

logger = logging.getLogger(__name__)


class RecommendationRepository(Protocol):
    """Protocol for persisting recommendation records."""

    async def save(self, recommendation: Recommendation) -> None:
        """Persist a recommendation record."""
        ...

    async def exists(self, recommendation_id: str) -> bool:
        """Check if a recommendation with the given ID exists."""
        ...


class NullRecommendationRepository:
    """No-op implementation — used until the real DB impl is wired in."""

    async def save(self, recommendation: Recommendation) -> None:
        """No-op save — silently discards the record."""
        logger.debug("NullRecommendationRepository.save() called — no-op")

    async def exists(self, recommendation_id: str) -> bool:
        """Always returns True in no-op mode."""
        return True


class SQLAlchemyRecommendationRepository:
    """SQLAlchemy async implementation of RecommendationRepository."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, recommendation: Recommendation) -> None:
        """Persist a recommendation record via SQLAlchemy session."""
        self._session.add(recommendation)
        await self._session.commit()

    async def exists(self, recommendation_id: str) -> bool:
        """Check if a recommendation exists by ID."""
        from sqlalchemy import literal, select

        from kebi.db.models import Recommendation

        stmt = select(literal(1)).where(Recommendation.id == recommendation_id).limit(1)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None
