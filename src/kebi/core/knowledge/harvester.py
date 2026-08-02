"""Content harvester — the `shared_content` knowledge writer (ADR-121/126).

A single LLM call over a share's already-gathered content (caption,
transcript, hashtags, title) plus the places extraction identified. It emits
world-entity facts, which are resolved into `StructuredClaim`s here — the
model references a place by index and names an entity, but never builds a
key. A claim is keyed to the entity it *names*, verified: when the name
matches its anchor place's own name/geo the anchor supplies the key, when a
city claim names a different city it is resolved by structured geocoding
with a round-trip name check, and a claim that can't be verified is dropped
rather than mis-keyed (ADR-126). The shared `KnowledgeWriter` persists the
result.

All exceptions are swallowed (returns `[]`): harvesting is best-effort
background enrichment and must never disturb the extraction it follows.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from kebi.core.agent._trace_context import traced_call
from kebi.core.areas.models import AreaContext
from kebi.core.config import get_prompt
from kebi.core.knowledge.schemas import (
    HarvestContent,
    HarvestPlace,
    NotedAreaRef,
    ResolvedGeo,
    ReviewStatus,
    SourceType,
    StructuredClaim,
    _slugify,
    slugs_match,
)
from kebi.core.knowledge.tags import render_claim_tag_vocabulary
from kebi.core.places import NON_VENUE_ROUTE

if TYPE_CHECKING:
    from kebi.core.areas import AreaService
    from kebi.providers.llm import InstructorClient

logger = logging.getLogger(__name__)


class _HarvestedClaim(BaseModel):
    """One claim as the model emits it — anchored to an entry of the
    indexed anchor list (venues and areas alike), never a key. Resolved
    into a `StructuredClaim` before it reaches the writer."""

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


class _Anchor(BaseModel):
    """One entry of the combined indexed anchor list the model cites by
    `place_index`. A venue anchor carries its catalog `place_id`; an area
    anchor (a noted non-venue resolved through the area service) carries
    only its verified geo — a `place`-scope claim citing it is dropped."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["venue", "area"]
    name: str
    geo: ResolvedGeo
    place_id: str | None = None


def _render_content(content: HarvestContent, anchors: list[_Anchor]) -> str:
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
    for idx, anchor in enumerate(anchors):
        where = ", ".join(
            p
            for p in (
                anchor.geo.neighborhood,
                anchor.geo.city,
                anchor.geo.country_code,
            )
            if p
        )
        marker = " (area of interest, not a venue)" if anchor.kind == "area" else ""
        lines.append(
            f"  [{idx}] {anchor.name}{marker}" + (f" — {where}" if where else "")
        )
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
        area_service: AreaService,
        *,
        confidence_floor: float = 0.35,
        review_status: ReviewStatus = "approved",
    ) -> None:
        self._client = instructor_client
        self._areas = area_service
        self.confidence_floor = confidence_floor
        self.review_status = review_status

    async def harvest(
        self,
        content: HarvestContent,
        places: list[HarvestPlace],
        noted_areas: list[NotedAreaRef] | None = None,
        *,
        user_id: str | None = None,
    ) -> list[StructuredClaim]:
        """Mine the content into resolved claims. Returns `[]` on any error
        or when there is nothing to anchor to.

        Anchors are persisted places plus the resolved areas behind any
        noted non-venue names (Step 2's harvest-gap fix): a share whose
        every place was noted — a route video, a region roundup — still
        harvests, its claims keyed to the areas the interest belongs to.
        """
        if content.is_empty():
            return []
        anchors = [
            _Anchor(kind="venue", name=p.name, geo=p.geo, place_id=p.place_id)
            for p in places
        ]
        anchors.extend(await self._resolve_noted(noted_areas or []))
        if not anchors:
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
                            # Vocabulary rendered from code so the prompt and
                            # the writer's normalization can never drift.
                            "content": get_prompt("knowledge_harvester")
                            + "\n\n"
                            + render_claim_tag_vocabulary(),
                        },
                        {"role": "user", "content": _render_content(content, anchors)},
                    ],
                )
            except Exception as exc:
                logger.warning("knowledge harvest failed: %s", exc, exc_info=True)
                t.fail(exc)
                return []
            claims = await self._resolve(cast(_HarvesterResponse, response), anchors)
            t.output = {"count": len(claims)}
            return claims

    async def _resolve_noted(self, noted: list[NotedAreaRef]) -> list[_Anchor]:
        """Resolve noted non-venue names to area anchors, deduped by entity.

        The subject-vs-container rule lives in the area service: an area
        noted in its own right ("Hoi An") anchors itself; a route ("Ha
        Giang Loop") anchors its containing area — the route detection
        skips the doomed name-as-area probe. The route's own name stays
        the anchor's display name so the model can cite it; its claims
        key to the containing area's verified geo. Unresolvable names are
        skipped (drop-don't-mis-key, ADR-126)."""
        anchors: list[_Anchor] = []
        seen_keys: set[str] = set()
        for ref in noted:
            entity = await self._areas.resolve_noted_name(
                ref.name,
                AreaContext(
                    city=ref.city,
                    country=ref.country,
                    country_code=ref.country_code,
                ),
                probe_name=ref.reason != NON_VENUE_ROUTE,
            )
            if entity is None:
                logger.debug(
                    "harvest_noted_area_unresolvable", extra={"name": ref.name}
                )
                continue
            geo = ResolvedGeo(
                country_code=entity.country_code,
                city=entity.name if entity.entity_type == "city" else None,
            )
            if entity.entity_key in seen_keys:
                continue
            seen_keys.add(entity.entity_key)
            anchors.append(_Anchor(kind="area", name=ref.name, geo=geo))
        return anchors

    async def _resolve(
        self, response: _HarvesterResponse, anchors: list[_Anchor]
    ) -> list[StructuredClaim]:
        """Key each claim by the entity it names, verified (ADR-126).

        The anchor supplies the key only when the claim is *about* the
        anchor (its name, neighborhood, or city — slug-matched). A city
        claim naming a different city is resolved through the area service
        — store first, round-trip-verified geocode on miss, persisted as an
        entity — within the anchor's country; country claims are resolved
        by name. A `place`-scope claim citing an area anchor is dropped
        (an area has no catalog id to key on). Anything unverifiable is
        dropped — never keyed to the wrong entity.
        """
        resolved: list[StructuredClaim] = []
        for raw in response.claims:
            if not (0 <= raw.place_index < len(anchors)):
                continue
            anchor = anchors[raw.place_index]
            place_ref: str | None = None
            geo: ResolvedGeo | None = None
            scope = raw.scope
            if raw.scope == "place":
                if anchor.place_id is None:
                    logger.debug(
                        "harvest_claim_dropped_place_scope_on_area",
                        extra={"entity": raw.entity_name},
                    )
                    continue
                if raw.entity_name and not _venue_names_match(
                    raw.entity_name, anchor.name
                ):
                    logger.debug(
                        "harvest_claim_dropped_entity_mismatch",
                        extra={"scope": raw.scope, "entity": raw.entity_name},
                    )
                    continue
                place_ref = anchor.place_id
            elif raw.scope == "neighborhood":
                if not slugs_match(raw.entity_name, anchor.geo.neighborhood):
                    logger.debug(
                        "harvest_claim_dropped_entity_mismatch",
                        extra={"scope": raw.scope, "entity": raw.entity_name},
                    )
                    continue
                geo = anchor.geo
            elif raw.scope == "city":
                # An area anchor's own name ("Ha Giang Loop") counts as
                # its resolved area too — the claim the model states
                # about the route lands on the containing area's key.
                if slugs_match(raw.entity_name, anchor.geo.city) or (
                    anchor.kind == "area"
                    and slugs_match(raw.entity_name, anchor.name)
                ):
                    geo = anchor.geo
                    if geo.city is None:
                        # The anchor's containing area is a whole country
                        # (the share gave no city context) — the fact
                        # lands at the level the key can actually express.
                        scope = "country"
                elif anchor.geo.country_code:
                    entity = await self._areas.resolve_city(
                        raw.entity_name, anchor.geo.country_code
                    )
                    if entity is not None:
                        geo = ResolvedGeo(
                            country_code=entity.country_code, city=entity.name
                        )
                if geo is None:
                    logger.debug(
                        "harvest_claim_dropped_unresolvable",
                        extra={"scope": raw.scope, "entity": raw.entity_name},
                    )
                    continue
            else:  # country
                country = await self._areas.resolve_country(raw.entity_name)
                if country is not None:
                    geo = ResolvedGeo(country_code=country.country_code)
                if geo is None:
                    logger.debug(
                        "harvest_claim_dropped_unresolvable",
                        extra={"scope": raw.scope, "entity": raw.entity_name},
                    )
                    continue
            resolved.append(
                StructuredClaim(
                    scope=scope,
                    entity_name=raw.entity_name or anchor.name,
                    claim=raw.claim,
                    tags=raw.tags,
                    confidence=raw.confidence,
                    place_ref=place_ref,
                    geo=geo,
                )
            )
        return resolved


def _venue_names_match(entity_name: str, place_name: str) -> bool:
    """Loose match for venue names — slugs equal or one contains the other,
    tolerating suffix variance ("Fuji Ramen" vs "Fuji Ramen Shibuya") while
    still rejecting a different entity bolted onto the venue's id."""
    a, b = _slugify(entity_name), _slugify(place_name)
    if not a or not b:
        return True
    return a in b or b in a
