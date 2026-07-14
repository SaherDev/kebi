"""Kebi-note producer — the `kebi_message` knowledge writer (ADR-127).

When a user saves a place kebi recommended, the reason the card showed is a
fact kebi is asserting about that place. Rather than parking it on the save as
a per-user note, it is written into the knowledge layer as a `kebi_message`
claim, so it surfaces through the same insider-notes path as harvested and
curated knowledge.

Like the harvester and curator, this is a `ClaimProducer` (ADR-123): it emits
`StructuredClaim`s and declares its own provenance (source, trust floor, review
state), and hands off to the shared `KnowledgeWriter` via `KnowledgeIngestion`.
Unlike them there is no LLM — the reason text *is* the claim and the place is
already resolved to a catalog id, so no structuring or geocoding is needed. It
is a *conversation-origin* source, so its claims are user-scoped (only ever
read back for the same user), stamped by the ingestion caller's `user_id`.
"""

from __future__ import annotations

from kebi.core.knowledge.schemas import ReviewStatus, SourceType, StructuredClaim


class KebiNoteProducer:
    """Wrap a saved-recommendation reason into a place-scoped claim.

    A `ClaimProducer` for the `kebi_message` source, self-describing its trust
    floor and review status (both from config, so gating is a config change).
    """

    source_type: SourceType = "kebi_message"

    def __init__(
        self,
        *,
        confidence_floor: float,
        review_status: ReviewStatus = "approved",
    ) -> None:
        self.confidence_floor = confidence_floor
        self.review_status = review_status

    def note(
        self, reason: str, *, place_id: str, place_name: str
    ) -> list[StructuredClaim]:
        """A reason → a single place-scoped `StructuredClaim`, or `[]` when the
        reason is blank. The place is already a catalog id, so the writer keys
        it directly (`place:<id>`) — no resolution step. Confidence is the
        source floor: there is no model estimate to beat it."""
        text = reason.strip()
        if not text:
            return []
        return [
            StructuredClaim(
                scope="place",
                entity_name=place_name,
                claim=text,
                tags=[],
                confidence=self.confidence_floor,
                place_ref=place_id,
            )
        ]
