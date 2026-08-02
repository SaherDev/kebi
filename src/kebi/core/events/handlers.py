"""Event handlers for domain events (ADR-058 simplified).

One taste handler (`on_taste_signal`) covers all taste event types.
Per ADR-043, failures are logged and traced but never propagated.
"""

import logging
from collections.abc import Awaitable
from typing import TYPE_CHECKING

from kebi.core.events.events import (
    ContentHarvestRequested,
    DomainEvent,
    LibraryStateChanged,
    PlaceSaved,
    RecommendationAccepted,
    RecommendationRejected,
    RecommendationSaved,
    TurnCompleted,
)
from kebi.db.models import InteractionType
from kebi.providers.tracing import TracingClient, get_tracing_client

if TYPE_CHECKING:
    from kebi.core.knowledge.harvest_bucket import HarvestBucketReader
    from kebi.core.knowledge.harvester import KnowledgeHarvester
    from kebi.core.knowledge.producer import KnowledgeIngestion
    from kebi.core.memory.service import UserMemoryService
    from kebi.core.taste.service import TasteModelService
    from kebi.core.user.intent_service import UserIntentService

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
        intent_service: "UserIntentService",
        tracer: TracingClient | None = None,
        *,
        harvest_reader: "HarvestBucketReader | None" = None,
        harvester: "KnowledgeHarvester | None" = None,
        ingestion: "KnowledgeIngestion | None" = None,
    ) -> None:
        self.taste_service = taste_service
        self.memory_service = memory_service
        self.intent_service = intent_service
        self._tracer = tracer or get_tracing_client()
        self._harvest_reader = harvest_reader
        self._harvester = harvester
        self._ingestion = ingestion

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
                pairs = [(InteractionType.SAVE, pcid) for pcid in event.place_core_ids]
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

    async def on_content_harvest_requested(
        self, event: ContentHarvestRequested
    ) -> None:
        """Second pass over a saved share's content (ADR-121).

        Reads the durable snapshot back from the bucket, mines it into
        world-entity claims, and writes them global (`user_id=None`) as
        `shared_content`. Best-effort: any failure is swallowed and traced
        (ADR-043) — the place is already saved. No-op if the writer stack
        wasn't wired (defensive) or the snapshot is gone.
        """
        try:
            if (
                self._harvest_reader is None
                or self._harvester is None
                or self._ingestion is None
            ):
                return
            snapshot = await self._harvest_reader.get(event.harvest_key)
            if snapshot is None:
                return
            claims = await self._harvester.harvest(
                snapshot.content,
                snapshot.places,
                snapshot.noted_areas,
                user_id=event.user_id,
            )
            written = await self._ingestion.ingest(
                self._harvester,
                claims,
                source_ref=event.source_ref,
                user_id=None,
            )
            self._tracer.capture_message(
                message=f"content_harvest wrote {len(written)} claim(s)",
                level="info",
                metadata={"event_id": event.event_id, "harvest_key": event.harvest_key},
                user_id=event.user_id,
                session_id=event.user_id,
            )
        except Exception as exc:
            logger.error(
                "Failed content harvest: %s",
                exc,
                exc_info=True,
                extra={"user_id": event.user_id, "harvest_key": event.harvest_key},
            )
            self._tracer.capture_message(
                message=f"content_harvest error: {exc}",
                level="error",
                metadata={"event_id": event.event_id},
                user_id=event.user_id,
                session_id=event.user_id,
            )
            self._tracer.flush()

    async def on_library_state_changed(self, event: LibraryStateChanged) -> None:
        """Retrain taste when a saved place's pills change (ADR-115).

        Schedules a debounced regen without writing an interaction row — the
        pills are read as current snapshot state at regen time. Failures are
        swallowed and traced (ADR-043): a pill toggle must never fail the PATCH.
        """
        try:
            self.taste_service.schedule_regen(event.user_id)
            self._tracer.capture_message(
                message=f"{event.event_type} handled",
                level="info",
                metadata={"event_id": event.event_id},
                user_id=event.user_id,
                session_id=event.user_id,
            )
        except Exception as exc:
            logger.error(
                "Failed to schedule regen on library change: %s",
                exc,
                exc_info=True,
                extra={"user_id": event.user_id},
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
        """Run the per-turn background side-effects: buffered memory extraction
        and recall-list persistence.

        The two are guarded independently so one failing never blocks the
        other. Memory extraction buffers per turn and runs the LLM every Nth
        message (memory.extraction.debounce_messages); intent persistence
        records the turn when it is intent-bearing (ADR-110). All exceptions
        are caught here; ADR-043 forbids handler failures from surfacing.
        """
        await self._run_guarded(
            "memory extraction",
            event,
            self.memory_service.extract_and_save_facts(
                user_id=event.user_id,
                user_message=event.user_message,
            ),
        )
        await self._run_guarded(
            "intent recording",
            event,
            self.intent_service.record_intent(
                event.user_id,
                event.user_message,
                surfaced=event.surfaced_places,
            ),
        )

    async def _run_guarded(
        self,
        label: str,
        event: TurnCompleted,
        coro: Awaitable[None],
    ) -> None:
        """Await one turn side-effect, swallowing+tracing any failure (ADR-043)."""
        try:
            await coro
            self._tracer.capture_message(
                message=f"TurnCompleted {label} handled",
                level="info",
                metadata={"event_id": event.event_id},
                user_id=event.user_id,
                session_id=event.user_id,
            )
        except Exception as exc:
            logger.error(
                "Failed TurnCompleted %s: %s",
                label,
                exc,
                exc_info=True,
                extra={"user_id": event.user_id},
            )
            self._tracer.capture_message(
                message=f"TurnCompleted {label} error: {exc}",
                level="error",
                metadata={"event_id": event.event_id},
                user_id=event.user_id,
                session_id=event.user_id,
            )
            self._tracer.flush()
