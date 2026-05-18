"""Signal service — validates and dispatches behavioral signal events."""

from __future__ import annotations

from typing import TYPE_CHECKING

from kebi.core.events.events import (
    RecommendationAccepted,
    RecommendationRejected,
)
from kebi.db.repositories.recommendation_repository import (
    SQLAlchemyRecommendationRepository,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from kebi.core.events.dispatcher import EventDispatcher


class RecommendationNotFoundError(Exception):
    """Raised when recommendation_id does not exist."""


# Signal types that require a valid recommendation_id in the DB.
_RECOMMENDATION_SIGNALS = frozenset(
    {
        "recommendation_accepted",
        "recommendation_rejected",
    }
)


class SignalService:
    """Validates and dispatches behavioral signal events.

    Owns the RecommendationRepository internally — the API layer never
    touches the repo directly (ADR-034 facade rule).

    Recommendation-scoped signals validate that recommendation_id exists.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        event_dispatcher: EventDispatcher,
    ) -> None:
        self._session_factory = session_factory
        self._event_dispatcher = event_dispatcher

    async def handle_signal(
        self,
        signal_type: str,
        user_id: str,
        recommendation_id: str | None = None,
        place_core_id: str | None = None,
    ) -> None:
        """Validate (if needed) and dispatch the signal event.

        Raises:
            RecommendationNotFoundError: if signal requires a recommendation_id
                and the ID is missing or does not exist in the DB.
        """
        if signal_type in _RECOMMENDATION_SIGNALS:
            if not recommendation_id:
                raise RecommendationNotFoundError("missing")
            async with self._session_factory() as session:
                repo = SQLAlchemyRecommendationRepository(session)
                if not await repo.exists(recommendation_id):
                    raise RecommendationNotFoundError(recommendation_id)

            event: RecommendationAccepted | RecommendationRejected
            if signal_type == "recommendation_accepted":
                event = RecommendationAccepted(
                    user_id=user_id,
                    recommendation_id=recommendation_id,
                    place_core_id=place_core_id or "",
                )
            else:
                event = RecommendationRejected(
                    user_id=user_id,
                    recommendation_id=recommendation_id,
                    place_core_id=place_core_id or "",
                )
            await self._event_dispatcher.dispatch(event)
