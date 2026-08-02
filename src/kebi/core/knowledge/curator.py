"""Expert curator — the `curated_expert` knowledge writer (ADR-121).

A travel expert writes prose; one LLM call structures it into geo-scoped
claims, each naming its area as structured components (country, city)
rather than a free-text query — free-text geocoding of bare names is
banned (ADR-126). The components resolve through the shared
`AreaService` (store first, round-trip-verified geocode on miss), so a
curated claim keys identically to a harvested one and the two merge on
the same entity. An unresolvable area falls back to the expert's
location hint, else the claim is dropped — never mis-keyed. The
resolved claims hand off to the shared `KnowledgeWriter`.

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
from kebi.core.knowledge.tags import render_claim_tag_vocabulary

if TYPE_CHECKING:
    from kebi.core.areas import AreaService
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
    """One claim as the model emits it — area named as structured
    components (never keyed, never a free-text query)."""

    model_config = ConfigDict(extra="forbid")

    scope: Literal["country", "city", "neighborhood"]
    entity_name: str
    area_country: str | None = None
    area_city: str | None = None
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
        area_service: AreaService,
        *,
        confidence_floor: float = 0.9,
        review_status: ReviewStatus = "approved",
    ) -> None:
        self._client = instructor_client
        self._areas = area_service
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
                        {
                            "role": "system",
                            # Same rendered vocabulary as the harvester —
                            # one bounded tag list on every write path.
                            "content": get_prompt("knowledge_curator")
                            + "\n\n"
                            + render_claim_tag_vocabulary(),
                        },
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
        resolved: list[StructuredClaim] = []
        for raw in response.claims:
            geo = await self._resolve_area(raw, hint) or hint_geo
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
        self, raw: _CuratedClaim, hint: LocationHint | None
    ) -> ResolvedGeo | None:
        """Resolve a claim's structured area components through the area
        service (which memoizes per instance and refuses unverifiable
        names). Returns None when nothing resolves to at least a country
        code — the caller then tries the expert's hint, else drops."""
        country_code: str | None = None
        if raw.area_country:
            country = await self._areas.resolve_country(raw.area_country)
            if country is not None:
                country_code = country.country_code
        if country_code is None and hint is not None and hint.country_alpha2:
            country_code = hint.country_alpha2.strip().lower()
        if raw.area_city:
            if not country_code:
                return None
            entity = await self._areas.resolve_city(raw.area_city, country_code)
            if entity is None:
                # Named a city that won't verify — let the hint (which may
                # carry the full geo) take over rather than degrading a
                # city-scoped claim to a country key the writer would drop.
                return None
            return ResolvedGeo(country_code=entity.country_code, city=entity.name)
        if country_code:
            return ResolvedGeo(country_code=country_code)
        return None
