"""The source-agnostic ingestion seam for the knowledge layer (ADR-120/122).

Every origin of world knowledge — content harvested from a share, an expert's
curation, and later conversational claims — is a `ClaimProducer`: it emits
`StructuredClaim`s and declares its own provenance (which source it is, how
much to trust a fresh claim, and what review state it lands in). One
`KnowledgeIngestion` path persists whatever a producer emits, so adding a new
source is a new adapter that conforms to this Protocol — never an edit to the
write path. How each producer *makes* its claims (mining video content vs
structuring prose) is deliberately outside this Protocol: those inputs
genuinely differ, and only the emitted shape and provenance are shared.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from kebi.core.knowledge.schemas import ReviewStatus, SourceType, StructuredClaim
from kebi.core.knowledge.writer import KnowledgeWriter


@runtime_checkable
class ClaimProducer(Protocol):
    """A source of knowledge claims, self-describing its provenance.

    Implementers also expose their own `produce`-style method with a
    source-specific signature (e.g. `harvest(content, places)`,
    `structure(text, hint)`); that call is made by the source's own caller.
    What this Protocol standardizes is the provenance the ingestion path
    stamps and the fact that the output is `StructuredClaim`s.
    """

    @property
    def source_type(self) -> SourceType: ...

    @property
    def confidence_floor(self) -> float: ...

    @property
    def review_status(self) -> ReviewStatus: ...


class KnowledgeIngestion:
    """Persists a producer's claims under that producer's provenance."""

    def __init__(self, writer: KnowledgeWriter) -> None:
        self._writer = writer

    async def ingest(
        self,
        producer: ClaimProducer,
        claims: Iterable[StructuredClaim],
        *,
        source_ref: str | None,
        user_id: str | None = None,
    ) -> list[StructuredClaim]:
        """Write `claims` stamped with `producer`'s provenance; return the
        claims that produced a new row. `user_id` is None for global sources
        (harvest, curation) and set only for conversation-scoped ones."""
        return await self._writer.persist(
            claims,
            source_type=producer.source_type,
            source_ref=source_ref,
            user_id=user_id,
            confidence_floor=producer.confidence_floor,
            review_status=producer.review_status,
        )
