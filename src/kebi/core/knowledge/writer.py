"""Shared write path into the knowledge-claims store (ADR-120).

Both writers — the content harvester (`shared_content`) and the expert
curator (`curated_expert`) — resolve their LLM output into `StructuredClaim`s
and hand them here. This class owns the one thing they must do identically:
turn a resolved claim into a canonical `entity_key` and persist it. The
writers differ only in the provenance they pass (`source_type`, `source_ref`,
`confidence_floor`); everything below the hand-off is one code path.

Two claims never reach the store: one whose scope lacks the geo parts its key
needs (dropped, not mis-keyed), and one that asserts accessibility — an
unverified accessibility claim is real-world harm, so it is forbidden in the
prompts and dropped here as a backstop (ADR-118).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from kebi.core.knowledge.schemas import (
    ReviewStatus,
    SourceType,
    StructuredClaim,
    build_geo_key,
    build_place_key,
)
from kebi.db.repositories.knowledge_claim_repository import KnowledgeClaimRepository

logger = logging.getLogger(__name__)

# Substrings that mark a claim as an accessibility assertion. Deliberately
# broad — the cost of dropping a borderline non-accessibility claim is low;
# the cost of persisting a false accessibility claim is real (ADR-118).
_ACCESSIBILITY_MARKERS = (
    "accessib",
    "wheelchair",
    "step-free",
    "step free",
    "disabled access",
    "disability access",
    "handicap",
    "ada compliant",
)


def _asserts_accessibility(claim: StructuredClaim) -> bool:
    haystack = " ".join([claim.claim, *claim.tags]).lower()
    return any(marker in haystack for marker in _ACCESSIBILITY_MARKERS)


class KnowledgeWriter:
    """Persists resolved claims under a canonical entity key.

    Mechanical and source-agnostic: the caller (a `ClaimProducer` via
    `KnowledgeIngestion`) supplies the provenance — `source_type`,
    `confidence_floor`, and `review_status` (ADR-122). This class only builds
    keys, drops the undroppable, floors confidence, and writes.
    """

    def __init__(self, repo: KnowledgeClaimRepository) -> None:
        self._repo = repo

    async def persist(
        self,
        claims: Iterable[StructuredClaim],
        *,
        source_type: SourceType,
        source_ref: str | None,
        user_id: str | None,
        confidence_floor: float,
        review_status: ReviewStatus = "approved",
    ) -> list[StructuredClaim]:
        """Write each claim under its canonical key; return the claims that
        produced a new row (dedup collapses re-runs, so a re-persist returns
        fewer). Unkeyable and accessibility claims are skipped silently."""
        written: list[StructuredClaim] = []
        for claim in claims:
            key = _build_key(claim)
            if key is None:
                logger.debug(
                    "knowledge_claim_dropped_no_key", extra={"scope": claim.scope}
                )
                continue
            if _asserts_accessibility(claim):
                logger.debug("knowledge_claim_dropped_accessibility")
                continue
            confidence = min(1.0, max(confidence_floor, claim.confidence))
            created = await self._repo.save(
                entity_type=claim.scope,
                entity_key=key,
                entity_name=claim.entity_name,
                claim=claim.claim,
                source_type=source_type,
                confidence=confidence,
                tags=claim.tags,
                source_ref=source_ref,
                user_id=user_id,
                review_status=review_status,
            )
            if created:
                written.append(claim)
        return written


def _build_key(claim: StructuredClaim) -> str | None:
    """Canonical entity_key for a claim, or None when its scope lacks the
    parts the key needs (so it is dropped rather than mis-scoped)."""
    if claim.scope == "place":
        return build_place_key(claim.place_ref) if claim.place_ref else None

    geo = claim.geo
    if geo is None or not geo.country_code:
        return None
    try:
        if claim.scope == "country":
            return build_geo_key(geo.country_code)
        if claim.scope == "city":
            return build_geo_key(geo.country_code, geo.city) if geo.city else None
        if claim.scope == "neighborhood":
            if not geo.city or not geo.neighborhood:
                return None
            return build_geo_key(geo.country_code, geo.city, geo.neighborhood)
    except ValueError:
        # country_code that isn't a valid alpha-2 — drop rather than raise.
        return None
    return None
