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
    PlaceProfileRequested,
    PlaceSaved,
    RecommendationSaved,
    TurnCompleted,
    WebFindingsHarvestRequested,
)
from kebi.core.web.models import WebSearchResult
from kebi.db.models import InteractionType
from kebi.providers.tracing import TracingClient, get_tracing_client

if TYPE_CHECKING:
    from kebi.core.knowledge.harvest_bucket import HarvestBucketReader
    from kebi.core.knowledge.harvester import KnowledgeHarvester
    from kebi.core.knowledge.producer import KnowledgeIngestion
    from kebi.core.knowledge.web_harvester import WebKnowledgeHarvester
    from kebi.core.memory.service import UserMemoryService
    from kebi.core.places.profile_service import PlaceProfileService
    from kebi.core.taste.service import TasteModelService
    from kebi.core.user.intent_service import UserIntentService

logger = logging.getLogger(__name__)


def _web_source_ref(result: WebSearchResult) -> str:
    """Provenance for a web-mined claim: the query that surfaced it.

    Not the URL of any one finding. A claim is normally synthesised from
    several snippets, so pinning it to one page would be a citation that does
    not hold; the query is the honest, reproducible pointer to how kebi came
    to believe it.
    """
    return f"web_search:{result.query}"[:500]


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
        web_harvester: "WebKnowledgeHarvester | None" = None,
        profile_service: "PlaceProfileService | None" = None,
    ) -> None:
        self.taste_service = taste_service
        self.memory_service = memory_service
        self.intent_service = intent_service
        self._tracer = tracer or get_tracing_client()
        self._harvest_reader = harvest_reader
        self._harvester = harvester
        self._ingestion = ingestion
        self._web_harvester = web_harvester
        self._profile_service = profile_service

    async def on_taste_signal(self, event: DomainEvent) -> None:
        """Unified handler for all taste-related events.

        Dispatches to handle_signal with the correct InteractionType.
        Handles PlaceSaved (multiple place_core_ids) and RecommendationSaved.
        The accept/reject events are gone with their UI (ADR-151); their
        interaction types remain readable for historical rows.
        """
        try:
            # Build (signal_type, place_core_id) pairs from the event shape
            pairs: list[tuple[InteractionType, str]] = []
            if isinstance(event, PlaceSaved):
                pairs = [(InteractionType.SAVE, pcid) for pcid in event.place_core_ids]
            elif isinstance(event, RecommendationSaved):
                pairs = [(InteractionType.SAVED_RECOMMENDATION, event.place_core_id)]

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

    async def on_place_profile_requested(self, event: PlaceProfileRequested) -> None:
        """Profile a thin catalog row in the background (ADR-152).

        Best-effort like every handler here (ADR-043): the user already has
        their (thin) place screen; a failure leaves the row as it was and
        the next open retries. The service holds the dedup lock and the
        no-longer-thin re-check, so this stays a thin dispatch.
        """
        try:
            if self._profile_service is None:
                return
            updated = await self._profile_service.profile_place(event.place_id)
            if updated is not None:
                self._tracer.capture_message(
                    message=f"place_profile wrote {len(updated.tags)} tag(s)",
                    level="info",
                    metadata={"event_id": event.event_id, "place_id": event.place_id},
                    user_id=event.user_id,
                    session_id=event.user_id,
                )
        except Exception as exc:
            logger.error(
                "Failed place profile: %s",
                exc,
                exc_info=True,
                extra={"place_id": event.place_id},
            )
            self._tracer.capture_message(
                message=f"place_profile error: {exc}",
                level="error",
                metadata={"event_id": event.event_id, "place_id": event.place_id},
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
                snapshot.content, snapshot.places, user_id=event.user_id
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

    async def on_web_findings_harvest_requested(
        self, event: WebFindingsHarvestRequested
    ) -> None:
        """Mine a turn's web findings into durable area claims (ADR-145).

        This is the half of web search that pays for itself. The lookup
        already happened and the answer is already gone; what is left is
        deciding which of those findings were lasting local facts and writing
        those to the claims store, so the next person asking gets them from
        `find_known` for free instead of from a paid search.

        Best-effort and silent, exactly like the content harvest: the user has
        their answer, and nothing here is allowed to disturb a turn that
        already succeeded. A missing writer stack, an unparseable payload, or
        an LLM failure all no-op.
        """
        try:
            if self._web_harvester is None or self._ingestion is None:
                return
            result = WebSearchResult.model_validate(event.result)
            claims = await self._web_harvester.harvest(result, user_id=event.user_id)
            if not claims:
                return
            written = await self._ingestion.ingest(
                self._web_harvester,
                claims,
                source_ref=_web_source_ref(result),
                # Global, not user-scoped: a fact about an area belongs to
                # everyone who asks about that area.
                user_id=None,
            )
            self._tracer.capture_message(
                message=f"web_harvest wrote {len(written)} claim(s)",
                level="info",
                metadata={"event_id": event.event_id},
                user_id=event.user_id,
                session_id=event.user_id,
            )
        except Exception as exc:
            logger.error(
                "Failed web findings harvest: %s",
                exc,
                exc_info=True,
                extra={"user_id": event.user_id},
            )
            self._tracer.capture_message(
                message=f"web_harvest error: {exc}",
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
