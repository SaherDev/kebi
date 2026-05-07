"""Signal service — validates and dispatches behavioral signal events."""

from __future__ import annotations

from typing import TYPE_CHECKING

from kebi.core.events.events import (
    ChipConfirmed,
    RecommendationAccepted,
    RecommendationRejected,
)
from kebi.db.models import InteractionType
from kebi.db.repositories.recommendation_repository import (
    SQLAlchemyRecommendationRepository,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from kebi.api.schemas.signal import ChipConfirmMetadata
    from kebi.core.events.dispatcher import EventDispatcher
    from kebi.core.taste.service import TasteModelService


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
    chip_confirm signals write a CHIP_CONFIRM interaction row with metadata,
    merge chip statuses into taste_model.chips, and dispatch ChipConfirmed.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        event_dispatcher: EventDispatcher,
        taste_service: TasteModelService,
    ) -> None:
        self._session_factory = session_factory
        self._event_dispatcher = event_dispatcher
        self._taste_service = taste_service

    async def handle_signal(
        self,
        signal_type: str,
        user_id: str,
        recommendation_id: str | None = None,
        place_id: str | None = None,
        chip_metadata: ChipConfirmMetadata | None = None,
    ) -> None:
        """Validate (if needed) and dispatch the signal event.

        Raises:
            RecommendationNotFoundError: if signal requires a recommendation_id
                and the ID is missing or does not exist in the DB.
        """
        if signal_type == "chip_confirm":
            if chip_metadata is None:
                raise ValueError("chip_confirm signal requires chip_metadata")
            await self._handle_chip_confirm(user_id, chip_metadata)
            return

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
                    place_id=place_id or "",
                )
            else:
                event = RecommendationRejected(
                    user_id=user_id,
                    recommendation_id=recommendation_id,
                    place_id=place_id or "",
                )
            await self._event_dispatcher.dispatch(event)

    async def _handle_chip_confirm(
        self,
        user_id: str,
        metadata: ChipConfirmMetadata,
    ) -> None:
        """chip_confirm handler: log interaction, merge statuses, dispatch event.

        (1) Write one Interaction row with type=CHIP_CONFIRM, metadata=request.
        (2) Read current chips from taste_model.
        (3) Merge submitted statuses (preserving already-confirmed).
        (4) Persist the updated chip array via repo.merge_chip_statuses.
        (5) Dispatch ChipConfirmed so the regen background job can rewrite
            the taste profile summary.

        No deduplication (clarification Q3 Option A). Every submission
        writes its own row and dispatches its own event — the rewrite
        handler is idempotent on unchanged state.
        """
        from kebi.core.taste.chip_merge import merge_chip_statuses

        # 1. Log interaction with metadata.
        await self._taste_service._repo.log_interaction(
            user_id=user_id,
            interaction_type=InteractionType.CHIP_CONFIRM,
            place_id=None,
            metadata=metadata.model_dump(),
        )

        # 2. Read current chips.
        profile = await self._taste_service.get_taste_profile(user_id)
        existing_chips = profile.chips if profile else []

        # 3. Merge.
        updated_chips = merge_chip_statuses(existing_chips, metadata.chips)

        # 4. Persist. Skip if no taste_model row exists (cold user somehow
        #    submitted chip_confirm — edge case that shouldn't happen via
        #    the frontend-gated flow but we don't error out).
        if profile is not None:
            await self._taste_service._repo.merge_chip_statuses(
                user_id=user_id,
                updated_chips=[c.model_dump() for c in updated_chips],
            )

        # 5. Dispatch.
        await self._event_dispatcher.dispatch(ChipConfirmed(user_id=user_id))
