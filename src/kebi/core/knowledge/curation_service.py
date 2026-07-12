"""Curation service — structures expert prose and persists it (ADR-121/122).

A thin orchestration over the curator (prose → resolved claims) and the
source-agnostic `KnowledgeIngestion`, which stamps `curated_expert`
provenance from the curator itself. Kept separate from the route so
construction stays in the wiring layer and the route stays a thin adapter.
"""

from __future__ import annotations

from kebi.core.knowledge.curator import KnowledgeCurator, LocationHint
from kebi.core.knowledge.producer import KnowledgeIngestion
from kebi.core.knowledge.schemas import StructuredClaim


class KnowledgeCurationService:
    def __init__(
        self,
        curator: KnowledgeCurator,
        ingestion: KnowledgeIngestion,
    ) -> None:
        self._curator = curator
        self._ingestion = ingestion

    async def curate(
        self, *, text: str, hint: LocationHint | None, user_id: str
    ) -> list[StructuredClaim]:
        """Structure `text` into claims and persist them as `curated_expert`
        (global, `user_id=None`). Returns the claims that produced a new row
        (dedup collapses re-submissions). The caller's `user_id` is recorded
        only as provenance (`source_ref`), never as a claim scope."""
        structured = await self._curator.structure(text, hint, user_id=user_id)
        return await self._ingestion.ingest(
            self._curator,
            structured,
            source_ref=f"curator:{user_id}",
            user_id=None,
        )
