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
from typing import TYPE_CHECKING

from kebi.core.knowledge.schemas import (
    ReviewStatus,
    SourceType,
    StructuredClaim,
    WrittenClaim,
    build_place_key,
)
from kebi.core.knowledge.tags import normalize_claim_tags
from kebi.db.repositories.knowledge_claim_repository import KnowledgeClaimRepository

if TYPE_CHECKING:
    from kebi.core.geo.protocols import GeoRegistryProtocol

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

    def __init__(
        self, repo: KnowledgeClaimRepository, geo_registry: GeoRegistryProtocol
    ) -> None:
        self._repo = repo
        self._geo_registry = geo_registry

    async def persist(
        self,
        claims: Iterable[StructuredClaim],
        *,
        source_type: SourceType,
        source_ref: str | None,
        user_id: str | None,
        confidence_floor: float,
        review_status: ReviewStatus = "approved",
    ) -> list[WrittenClaim]:
        """Write each claim under its canonical key; return the claims that
        produced a new row, each paired with its row id (dedup collapses
        re-runs, so a re-persist returns fewer). Unkeyable and accessibility
        claims are skipped silently."""
        written: list[WrittenClaim] = []
        for claim in claims:
            key = await self._build_key(claim)
            if key is None:
                logger.debug(
                    "knowledge_claim_dropped_no_key", extra={"scope": claim.scope}
                )
                continue
            if _asserts_accessibility(claim):
                logger.debug("knowledge_claim_dropped_accessibility")
                continue
            confidence = min(1.0, max(confidence_floor, claim.confidence))
            # Off-vocabulary tags are dropped, not stored: the tag index is
            # only useful if reader and writer share one bounded vocabulary.
            # (Accessibility was checked on the RAW tags above, on purpose.)
            stored_tags = normalize_claim_tags(claim.tags)
            claim_id = await self._repo.save(
                entity_type=claim.scope,
                entity_key=key,
                entity_name=claim.entity_name,
                claim=claim.claim,
                source_type=source_type,
                confidence=confidence,
                tags=stored_tags,
                source_ref=source_ref,
                user_id=user_id,
                review_status=review_status,
            )
            if claim_id is not None:
                # The returned claim carries what was STORED — echoing the
                # raw tags would show the caller values the index never kept.
                written.append(
                    WrittenClaim(
                        id=claim_id,
                        claim=claim.model_copy(update={"tags": stored_tags}),
                    )
                )
        return written

    async def _build_key(self, claim: StructuredClaim) -> str | None:
        """Canonical entity_key for a claim, or None when its scope lacks the
        parts the key needs (so it is dropped rather than mis-scoped).

        Geo scopes resolve through the registry with `mint=True` — a claim
        about an area the registry hasn't met is the write path meeting it.
        A key shallower than the claim's scope means the asked unit could not
        be verified: dropped, never keyed to the wrong depth.
        """
        if claim.scope == "place":
            return build_place_key(claim.place_ref) if claim.place_ref else None

        geo = claim.geo
        if geo is None or not geo.country_code:
            return None
        if claim.scope == "country":
            resolved = await self._geo_registry.key_for_location(
                geo.country_code, None, None
            )
            return resolved.geo_key if resolved else None
        if claim.scope == "city":
            if not geo.city:
                return None
            resolved = await self._geo_registry.key_for_location(
                geo.country_code, geo.city, None, mint=True
            )
            return resolved.geo_key if resolved and resolved.city else None
        if claim.scope == "neighborhood":
            if not geo.city or not geo.neighborhood:
                return None
            resolved = await self._geo_registry.key_for_location(
                geo.country_code, geo.city, geo.neighborhood, mint=True
            )
            return resolved.geo_key if resolved and resolved.area else None
        return None
