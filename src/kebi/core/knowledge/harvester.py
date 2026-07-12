"""Content harvester — the `shared_content` knowledge writer (ADR-121).

A single LLM call over a share's already-gathered content (caption,
transcript, hashtags, title) plus the places extraction identified. It emits
world-entity facts, which are resolved into `StructuredClaim`s here — the
model references a place by index and names an entity, but never builds a
key; the catalog id and resolved geo come from the `HarvestPlace` list, so a
model can never invent a scope key. The shared `KnowledgeWriter` persists the
result.

All exceptions are swallowed (returns `[]`): harvesting is best-effort
background enrichment and must never disturb the extraction it follows.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from kebi.core.agent._trace_context import traced_call
from kebi.core.config import get_prompt
from kebi.core.knowledge.schemas import (
    HarvestContent,
    HarvestPlace,
    ReviewStatus,
    SourceType,
    StructuredClaim,
)

if TYPE_CHECKING:
    from kebi.providers.llm import InstructorClient

logger = logging.getLogger(__name__)


class _HarvestedClaim(BaseModel):
    """One claim as the model emits it — anchored to a place by index, not a
    key. Resolved into a `StructuredClaim` before it reaches the writer."""

    model_config = ConfigDict(extra="forbid")

    scope: Literal["place", "neighborhood", "city", "country"]
    place_index: int
    entity_name: str
    claim: str
    tags: list[str] = Field(default_factory=list)
    confidence: float


class _HarvesterResponse(BaseModel):
    """Top-level response — Instructor needs a class, not `list[...]`."""

    claims: list[_HarvestedClaim] = Field(default_factory=list)


def _render_content(content: HarvestContent, places: list[HarvestPlace]) -> str:
    lines: list[str] = []
    if content.platform:
        lines.append(f"Platform: {content.platform}")
    if content.title:
        lines.append(f"Title: {content.title}")
    if content.caption:
        lines.append(f"Caption: {content.caption}")
    if content.transcript:
        lines.append(f"Transcript: {content.transcript}")
    if content.hashtags:
        lines.append("Hashtags: " + " ".join(content.hashtags))
    if content.location_tag:
        lines.append(f"Location tag: {content.location_tag}")
    lines.append("")
    lines.append("Identified places:")
    for idx, place in enumerate(places):
        where = ", ".join(
            p
            for p in (place.geo.neighborhood, place.geo.city, place.geo.country_code)
            if p
        )
        lines.append(f"  [{idx}] {place.name}" + (f" — {where}" if where else ""))
    return "\n".join(lines)


class KnowledgeHarvester:
    """One LLM pass over a share's content → resolved `StructuredClaim`s.

    A `ClaimProducer` for the `shared_content` source: it declares its own
    provenance (trust floor, review status), so ingestion stamps claims
    without knowing anything harvest-specific.
    """

    source_type: SourceType = "shared_content"

    def __init__(
        self,
        instructor_client: InstructorClient,
        *,
        confidence_floor: float = 0.35,
        review_status: ReviewStatus = "approved",
    ) -> None:
        self._client = instructor_client
        self.confidence_floor = confidence_floor
        self.review_status = review_status

    async def harvest(
        self,
        content: HarvestContent,
        places: list[HarvestPlace],
        *,
        user_id: str | None = None,
    ) -> list[StructuredClaim]:
        """Mine the content into resolved claims. Returns `[]` on any error
        or when there is nothing to anchor to."""
        if not places or content.is_empty():
            return []
        async with traced_call(
            "knowledge_harvester.llm",
            "knowledge_harvester",
            role="knowledge_harvester",
            user_id=user_id,
            standalone=True,
        ) as t:
            try:
                response = await self._client.extract(
                    response_model=_HarvesterResponse,
                    messages=[
                        {
                            "role": "system",
                            "content": get_prompt("knowledge_harvester"),
                        },
                        {"role": "user", "content": _render_content(content, places)},
                    ],
                )
            except Exception as exc:
                logger.warning("knowledge harvest failed: %s", exc, exc_info=True)
                t.fail(exc)
                return []
            claims = _resolve(cast(_HarvesterResponse, response), places)
            t.output = {"count": len(claims)}
            return claims


def _resolve(
    response: _HarvesterResponse, places: list[HarvestPlace]
) -> list[StructuredClaim]:
    """Map model output onto catalog ids + resolved geo. Claims that
    reference an out-of-range place are dropped."""
    resolved: list[StructuredClaim] = []
    for raw in response.claims:
        if not (0 <= raw.place_index < len(places)):
            continue
        place = places[raw.place_index]
        resolved.append(
            StructuredClaim(
                scope=raw.scope,
                entity_name=raw.entity_name or place.name,
                claim=raw.claim,
                tags=raw.tags,
                confidence=raw.confidence,
                place_ref=place.place_id if raw.scope == "place" else None,
                geo=None if raw.scope == "place" else place.geo,
            )
        )
    return resolved
