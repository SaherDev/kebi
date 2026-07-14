"""Kebi-note service — records a saved-recommendation reason as a claim
(ADR-127).

A thin orchestration over the `KebiNoteProducer` (reason → resolved claim) and
the source-agnostic `KnowledgeIngestion`, which stamps `kebi_message`
provenance from the producer. Mirrors `KnowledgeCurationService`: construction
stays in the wiring layer and the route stays a thin adapter. The one
difference from the global writers is that this source is conversation-origin,
so it ingests **user-scoped** (`user_id=<caller>`), never global.
"""

from __future__ import annotations

from kebi.core.knowledge.kebi_note import KebiNoteProducer
from kebi.core.knowledge.producer import KnowledgeIngestion
from kebi.core.knowledge.schemas import StructuredClaim


class KebiNoteService:
    def __init__(
        self,
        producer: KebiNoteProducer,
        ingestion: KnowledgeIngestion,
    ) -> None:
        self._producer = producer
        self._ingestion = ingestion

    async def record(
        self,
        *,
        reason: str,
        place_id: str,
        place_name: str,
        user_id: str,
        recommendation_id: str,
    ) -> list[StructuredClaim]:
        """Persist `reason` as a user-scoped `kebi_message` claim on the place.

        Returns the claims that produced a new row — empty for a blank reason
        or a re-save (the writer's claim-text dedup collapses a repeat). The
        recommendation id is recorded as provenance (`source_ref`), never a
        share URL, so the note is never flagged "from what you shared"."""
        structured = self._producer.note(
            reason, place_id=place_id, place_name=place_name
        )
        return await self._ingestion.ingest(
            self._producer,
            structured,
            source_ref=f"kebi:rec:{recommendation_id}",
            user_id=user_id,
        )
