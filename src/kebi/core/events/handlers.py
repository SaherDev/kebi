"""Event handlers for domain events (ADR-058 simplified).

One taste handler (`on_taste_signal`) covers all taste event types.
Per ADR-043, failures are logged and traced but never propagated.
"""

import logging
from typing import TYPE_CHECKING

from kebi.core.events.events import (
    DomainEvent,
    PlaceSaved,
    RecommendationAccepted,
    RecommendationRejected,
    RecommendationSaved,
    TurnCompleted,
)
from kebi.db.models import InteractionType
from kebi.providers.tracing import TracingClient, get_tracing_client

if TYPE_CHECKING:
    from kebi.core.memory.service import UserMemoryService
    from kebi.core.taste.service import TasteModelService

logger = logging.getLogger(__name__)

# Map event_type → InteractionType for single-place recommendation signals.
_TASTE_EVENT_MAP: dict[str, InteractionType] = {
    "recommendation_accepted": InteractionType.ACCEPTED,
    "recommendation_rejected": InteractionType.REJECTED,
    "recommendation_saved": InteractionType.SAVED_RECOMMENDATION,
}


class EventHandlers:
    """Container for event handler functions."""

    def __init__(
        self,
        taste_service: "TasteModelService",
        memory_service: "UserMemoryService",
        tracer: TracingClient | None = None,
    ) -> None:
        self.taste_service = taste_service
        self.memory_service = memory_service
        self._tracer = tracer or get_tracing_client()

    async def on_taste_signal(self, event: DomainEvent) -> None:
        """Unified handler for all taste-related events.

        Dispatches to handle_signal with the correct InteractionType.
        Handles PlaceSaved (multiple place_core_ids), RecommendationAccepted,
        RecommendationRejected, and RecommendationSaved.
        """
        try:
            # Build (signal_type, place_core_id) pairs from the event shape
            pairs: list[tuple[InteractionType, str]] = []
            if isinstance(event, PlaceSaved):
                pairs = [
                    (InteractionType.SAVE, pcid) for pcid in event.place_core_ids
                ]
            elif isinstance(
                event,
                RecommendationAccepted | RecommendationRejected | RecommendationSaved,
            ):
                pairs = [(_TASTE_EVENT_MAP[event.event_type], event.place_core_id)]

            for signal_type, place_core_id in pairs:
                await self.taste_service.handle_signal(
                    user_id=event.user_id,
                    signal_type=signal_type,
                    place_core_id=place_core_id,
                )

            self._tracer.capture_message(
                message=f"{event.event_type} handled",
                level="info",
                metadata={"event_id": event.event_id},
                user_id=event.user_id,
                session_id=event.user_id,
            )
        except Exception as exc:
            logger.error(
                "Failed to handle taste signal (%s): %s",
                event.event_type,
                exc,
                exc_info=True,
                extra={"user_id": event.user_id, "event_type": event.event_type},
            )
            self._tracer.capture_message(
                message=f"{event.event_type} handler error: {exc}",
                level="error",
                metadata={"event_id": event.event_id},
                user_id=event.user_id,
                session_id=event.user_id,
            )
            self._tracer.flush()

    async def on_turn_completed(self, event: TurnCompleted) -> None:
        """Hand the user message off to the memory service for buffered extraction.

        The service buffers per turn and only runs the LLM on every Nth
        message (memory.extraction.debounce_messages). All exceptions are
        caught here; ADR-043 forbids handler failures from surfacing.
        """
        try:
            await self.memory_service.extract_and_save_facts(
                user_id=event.user_id,
                user_message=event.user_message,
            )
            self._tracer.capture_message(
                message="TurnCompleted event handled",
                level="info",
                metadata={"event_id": event.event_id},
                user_id=event.user_id,
                session_id=event.user_id,
            )
        except Exception as exc:
            logger.error(
                "Failed to handle TurnCompleted: %s",
                exc,
                exc_info=True,
                extra={"user_id": event.user_id},
            )
            self._tracer.capture_message(
                message=f"TurnCompleted handler error: {exc}",
                level="error",
                metadata={"event_id": event.event_id},
                user_id=event.user_id,
                session_id=event.user_id,
            )
            self._tracer.flush()
