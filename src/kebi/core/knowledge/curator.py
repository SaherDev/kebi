"""Expert curator — the `curated_expert` knowledge writer (ADR-121).

A travel expert writes prose; one LLM call structures it into geo-scoped
claims, and each claim's area is resolved to a canonical `ResolvedGeo`
through the same free geocoder the rest of the repo uses — so a curated
claim keys identically to a harvested one and the two merge on the same
entity. The resolved claims hand off to the shared `KnowledgeWriter`.

v1 is geo-scoped (country / city / neighborhood): the curator has no place
to anchor a place-scoped claim to. Place-level curation would need resolving
a named venue to a catalog id and is deliberately out of scope here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from kebi.core.agent._trace_context import traced_call
from kebi.core.config import get_prompt
from kebi.core.knowledge.schemas import (
    ResolvedGeo,
    ReviewStatus,
    SourceType,
    StructuredClaim,
)

if TYPE_CHECKING:
    from kebi.core.places.nominatim_geocoding_client import NominatimGeocodingClient
    from kebi.providers.llm import InstructorClient

logger = logging.getLogger(__name__)


class LocationHint(BaseModel):
    """Optional anchor the expert supplies to disambiguate their prose.

    Used as the fallback geo when a claim's area can't be geocoded.
    `country_alpha2` is expected to be an ISO-3166 alpha-2 code; the writer
    validates it and drops the claim if it isn't.
    """

    model_config = ConfigDict(extra="forbid")

    country_alpha2: str | None = None
    city: str | None = None
    neighborhood: str | None = None


class _CuratedClaim(BaseModel):
    """One claim as the model emits it — area named for geocoding, not keyed."""

    model_config = ConfigDict(extra="forbid")

    scope: Literal["country", "city", "neighborhood"]
    entity_name: str
    area_query: str
    claim: str
    tags: list[str] = Field(default_factory=list)
    confidence: float


class _CuratorResponse(BaseModel):
    claims: list[_CuratedClaim] = Field(default_factory=list)


def _hint_geo(hint: LocationHint | None) -> ResolvedGeo | None:
    if hint is None or not hint.country_alpha2:
        return None
    return ResolvedGeo(
        country_code=hint.country_alpha2.strip().lower(),
        city=hint.city,
        neighborhood=hint.neighborhood,
    )


class KnowledgeCurator:
    """Structure expert prose into resolved geo-scoped claims.

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
        hint: LocationHint | None = None,
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
                        {"role": "system", "content": get_prompt("knowledge_curator")},
                        {"role": "user", "content": text},
                    ],
                )
            except Exception as exc:
                logger.warning("knowledge curation failed: %s", exc, exc_info=True)
                t.fail(exc)
                return []
            resolved = await self._resolve(cast(_CuratorResponse, response), hint)
            t.output = {"count": len(resolved)}
            return resolved

    async def _resolve(
        self, response: _CuratorResponse, hint: LocationHint | None
    ) -> list[StructuredClaim]:
        hint_geo = _hint_geo(hint)
        cache: dict[str, ResolvedGeo | None] = {}
        resolved: list[StructuredClaim] = []
        for raw in response.claims:
            geo = await self._resolve_area(raw.area_query, cache) or hint_geo
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
