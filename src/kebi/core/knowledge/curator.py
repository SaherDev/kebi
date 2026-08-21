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
    slugs_match,
)
from kebi.core.knowledge.tags import render_claim_tag_vocabulary

if TYPE_CHECKING:
    from kebi.core.geo.protocols import GeoRegistryProtocol
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


def _names_anchor(raw: _CuratedClaim, anchor: CurationAnchor | None) -> bool:
    """True when a geo claim is about the anchored entity (or a geo level the
    anchor already sits in), so the anchor's verified geo keys it directly.
    An empty area_query under an anchor means "here" — also the anchor."""
    if anchor is None:
        return False
    if not raw.area_query.strip():
        return True
    geo = anchor.geo
    return (
        slugs_match(raw.entity_name, anchor.name)
        or slugs_match(raw.entity_name, geo.neighborhood)
        or slugs_match(raw.entity_name, geo.city)
    )


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
        geo_registry: GeoRegistryProtocol,
        *,
        confidence_floor: float = 0.9,
        review_status: ReviewStatus = "approved",
    ) -> None:
        self._client = instructor_client
        self._registry = geo_registry
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
                extraction = await self._client.extract(
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
            t.usage = extraction.usage
            t.attempts = extraction.attempts
            resolved = await self._resolve(
                cast(_CuratorResponse, extraction.data), anchor
            )
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
            if _names_anchor(raw, anchor):
                # The anchor supplies the key for claims about itself —
                # re-resolving the anchor's own name risks an incomplete or
                # differently-keyed answer that would split the entity's
                # claims (the harvester's own rule, ADR-126). Only a claim
                # about a *different* area earns a registry resolve.
                geo = anchor_geo
            else:
                geo = (
                    await self._resolve_area(raw.area_query, anchor_geo, cache)
                    or anchor_geo
                )
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
        self,
        query: str,
        anchor_geo: ResolvedGeo | None,
        cache: dict[str, ResolvedGeo | None],
    ) -> ResolvedGeo | None:
        """Resolve one area string through the registry, memoized per request.

        Constrained by the anchor's country — the free-text global geocode
        this replaces was the write path's weakest link (ADR-126's "Muine
        resolves to Italy" class). Tried most-specific first: an area inside
        the anchor's city, then a city in the anchor's country, then a
        country name. None when nothing verifies — the claim falls back to
        the anchor's own geography rather than a guessed key.
        """
        q = query.strip()
        if not q:
            return None
        if q in cache:
            return cache[q]
        name = q.split(",")[0].strip() or q
        geo: ResolvedGeo | None = None
        try:
            cc = anchor_geo.country_code if anchor_geo else None
            if cc and anchor_geo and anchor_geo.city:
                resolved = await self._registry.key_for_location(
                    cc, anchor_geo.city, name, mint=True
                )
                if resolved is not None and resolved.area is not None:
                    geo = ResolvedGeo(
                        country_code=cc,
                        city=anchor_geo.city,
                        neighborhood=resolved.area.name,
                    )
            if geo is None and cc:
                resolved = await self._registry.key_for_location(
                    cc, name, None, mint=True
                )
                if resolved is not None and resolved.city is not None:
                    geo = ResolvedGeo(
                        country_code=resolved.city.country_code,
                        city=resolved.city.name,
                    )
            if geo is None:
                row = await self._registry.resolve_country(name)
                if row is not None:
                    geo = ResolvedGeo(country_code=row.country_code)
        except Exception as exc:
            logger.warning("curator area resolve failed for %r: %s", q, exc)
            geo = None
        cache[q] = geo
        return geo
