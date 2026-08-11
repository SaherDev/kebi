"""Expert curator — the `curated_expert` knowledge writer (ADR-121).

A travel expert writes prose; one LLM call structures it into claims, and
each claim's area is resolved to a canonical `ResolvedGeo` through the same
free geocoder the rest of the repo uses — so a curated claim keys identically
to a harvested one and the two merge on the same entity. The resolved claims
hand off to the shared `KnowledgeWriter`.

A request may carry a `CurationAnchor` — the entity the prose was written
about, resolved by the caller before the LLM runs. A venue anchor is what
makes `place` scope expressible here: the model marks a claim as being about
the anchored venue and the anchor's catalog id becomes its `place_ref` — the
model itself never sees or invents an id. The anchor's geo is also the
fallback for geo-scoped claims whose area can't be geocoded from the prose.
An unanchored request remains geo-only: a `place` claim with no venue anchor
is dropped, never guessed at.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from kebi.core.agent._trace_context import traced_call
from kebi.core.config import get_prompt
from kebi.core.knowledge.schemas import (
    CurationAnchor,
    ResolvedGeo,
    ReviewStatus,
    SourceType,
    StructuredClaim,
)
from kebi.core.knowledge.tags import render_claim_tag_vocabulary

if TYPE_CHECKING:
    from kebi.core.places.nominatim_geocoding_client import NominatimGeocodingClient
    from kebi.providers.llm import InstructorClient

logger = logging.getLogger(__name__)


class _CuratedClaim(BaseModel):
    """One claim as the model emits it — area named for geocoding, not keyed.
    `area_query` is empty for a claim about the anchored venue (there is
    nothing to geocode; the anchor supplies the key)."""

    model_config = ConfigDict(extra="forbid")

    scope: Literal["place", "country", "city", "neighborhood"]
    entity_name: str
    area_query: str = ""
    claim: str
    tags: list[str] = Field(default_factory=list)
    confidence: float


class _CuratorResponse(BaseModel):
    claims: list[_CuratedClaim] = Field(default_factory=list)


def _render_anchor_line(anchor: CurationAnchor) -> str:
    """The dynamic anchor context the static addendum prompt refers to."""
    geo = anchor.geo
    where = ", ".join(
        part
        for part in (geo.neighborhood, geo.city, geo.country_code)
        if part and part != anchor.name
    )
    kind = "venue" if anchor.place_id else "area"
    suffix = f" (in {where})" if where else ""
    return f'Anchor: the {kind} "{anchor.name}"{suffix}.'


class KnowledgeCurator:
    """Structure expert prose into resolved claims.

    A `ClaimProducer` for the `curated_expert` source, self-describing its
    (higher) trust floor and review status.
    """

    source_type: SourceType = "curated_expert"

    def __init__(
        self,
        instructor_client: InstructorClient,
        geocoder: NominatimGeocodingClient,
        *,
        confidence_floor: float = 0.9,
        review_status: ReviewStatus = "approved",
    ) -> None:
        self._client = instructor_client
        self._geocoder = geocoder
        self.confidence_floor = confidence_floor
        self.review_status = review_status

    async def structure(
        self,
        text: str,
        anchor: CurationAnchor | None = None,
        *,
        user_id: str | None = None,
    ) -> list[StructuredClaim]:
        """Prose → resolved `StructuredClaim`s. Returns `[]` on any error."""
        if not text.strip():
            return []
        async with traced_call(
            "knowledge_curator.llm",
            "knowledge_curator",
            role="knowledge_curator",
            user_id=user_id,
            standalone=True,
        ) as t:
            try:
                response = await self._client.extract(
                    response_model=_CuratorResponse,
                    messages=[
                        {
                            "role": "system",
                            "content": self._system_prompt(anchor),
                        },
                        {"role": "user", "content": text},
                    ],
                )
            except Exception as exc:
                logger.warning("knowledge curation failed: %s", exc, exc_info=True)
                t.fail(exc)
                return []
            resolved = await self._resolve(cast(_CuratorResponse, response), anchor)
            t.output = {"count": len(resolved)}
            return resolved

    def _system_prompt(self, anchor: CurationAnchor | None) -> str:
        parts = [get_prompt("knowledge_curator")]
        if anchor is not None:
            parts.append(
                _render_anchor_line(anchor)
                + "\n\n"
                + get_prompt("knowledge_curator_anchor")
            )
        # Same rendered vocabulary as the harvester — one bounded tag list on
        # every write path, appended last on all of them.
        parts.append(render_claim_tag_vocabulary())
        return "\n\n".join(parts)

    async def _resolve(
        self, response: _CuratorResponse, anchor: CurationAnchor | None
    ) -> list[StructuredClaim]:
        anchor_geo = anchor.geo if anchor is not None else None
        cache: dict[str, ResolvedGeo | None] = {}
        resolved: list[StructuredClaim] = []
        for raw in response.claims:
            if raw.scope == "place":
                # Only the anchored venue is a legal place target; the model
                # never supplies the id (prompt rule, enforced here).
                if anchor is None or anchor.place_id is None:
                    logger.debug("curated_place_claim_dropped_no_venue_anchor")
                    continue
                resolved.append(
                    StructuredClaim(
                        scope="place",
                        entity_name=anchor.name,
                        claim=raw.claim,
                        tags=raw.tags,
                        confidence=raw.confidence,
                        place_ref=anchor.place_id,
                    )
                )
                continue
            geo = await self._resolve_area(raw.area_query, cache) or anchor_geo
            if geo is None:
                continue
            resolved.append(
                StructuredClaim(
                    scope=raw.scope,
                    entity_name=raw.entity_name,
                    claim=raw.claim,
                    tags=raw.tags,
                    confidence=raw.confidence,
                    geo=geo,
                )
            )
        return resolved

    async def _resolve_area(
        self, query: str, cache: dict[str, ResolvedGeo | None]
    ) -> ResolvedGeo | None:
        """Geocode one area string to a canonical geo, memoized per request
        (Nominatim is rate-limited). Returns None when it can't be resolved
        to at least a country code."""
        q = query.strip()
        if not q:
            return None
        if q in cache:
            return cache[q]
        geo: ResolvedGeo | None = None
        try:
            result = await self._geocoder.search(query=q)
        except Exception as exc:
            logger.warning("curator geocode failed for %r: %s", q, exc)
            result = None
        if result is not None and result.country_code:
            geo = ResolvedGeo(
                country_code=result.country_code,
                city=result.city,
                neighborhood=result.neighborhood,
            )
        cache[q] = geo
        return geo
