"""Web-finding harvester — the flywheel's newest spoke (ADR-145).

A `ClaimProducer` for the `web_search` source. It reads the findings a turn's
search returned and keeps only the parts that will still be true next month:
the fee, the custom, the timing, the thing a local knows. Everything else —
the match result, the news, this weekend's line-up — is answered and
forgotten, because a claims store full of expired schedules is worse than one
without them.

Two rules make that separation safe rather than a matter of taste.

*Durability is judged, not assumed.* The prompt asks for lasting local facts
and the model marks anything dated; the dated ones never reach the writer. A
claim that was true on Tuesday and is asserted forever is exactly the failure
this whole layer exists to avoid.

*Keys are verified, never invented.* Same rule as ADR-126: a claim is keyed to
the area it names only when that name slug-matches the turn's own area or
survives a structured round-trip geocode. Unverifiable claims are dropped
rather than mis-keyed onto whatever city the conversation happened to be in.

Everything is best-effort. This runs after the answer is already on its way to
the user, so any failure returns `[]` and costs nothing but the enrichment.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from kebi.core.agent._trace_context import traced_call
from kebi.core.config import get_prompt
from kebi.core.knowledge.schemas import (
    EntityType,
    ResolvedGeo,
    ReviewStatus,
    SourceType,
    StructuredClaim,
    slugs_match,
)
from kebi.core.knowledge.tags import render_claim_tag_vocabulary
from kebi.core.web.models import WebSearchResult

if TYPE_CHECKING:
    from kebi.core.geo.protocols import GeoRegistryProtocol
    from kebi.providers.llm import InstructorClient

logger = logging.getLogger(__name__)


# Entry-rule vocabulary. A claim whose text matches any of these is dropped
# before it can be stored, no matter what the model said about it (ADR-154).
#
# The prompt already forbids these, and the prompt is not enough: entry rules
# read as stable to a model asking "will this be true in six months", so they
# get marked durable and stored. They are not stable — they change without
# notice and they differ by the traveller's passport, so a stored one is
# asserted later either after it stopped being true or to someone holding a
# different passport, and being wrong about one costs a person a flight. This
# is the same backstop reasoning as the accessibility guard in
# `places/tag_merge.py`: a prompt rule for a fact with real-world cost gets a
# deterministic check behind it.
#
# Tuned to over-drop. A false positive costs one area claim kebi would have
# stored; a false negative is the failure above. "customs" is deliberately
# absent — it collides with local custom / etiquette, which is exactly the
# kind of fact this harvester exists to keep.
_ENTRY_RULE_TERMS: tuple[str, ...] = (
    "visa",
    "passport",
    "immigration",
    "entry requirement",
    "entry permit",
    "residence permit",
    "work permit",
    "length of stay",
    "permitted stay",
    "border crossing",
    "port of entry",
    "arrival card",
    "departure card",
)


def is_entry_rule(claim: str) -> bool:
    """True when a claim states a visa / entry / immigration rule.

    Pure and substring-based so it is testable inline and cannot be skipped
    by a caller that forgot a flag — the same shape as `compute_confidence`.
    """
    text = claim.casefold()
    return any(term in text for term in _ENTRY_RULE_TERMS)


class _WebClaim(BaseModel):
    """One claim as the model emits it, before keys are resolved."""

    model_config = ConfigDict(extra="forbid")

    # No `place` scope. A finding names venues in prose but carries no catalog
    # id, and resolving a name to a place would mean a paid provider lookup on
    # a background path — so web knowledge keys to areas only. A venue fact
    # still reaches the answer through the turn's findings; it just does not
    # become a claim.
    scope: Literal["neighborhood", "city", "country"]
    entity_name: str
    claim: str
    tags: list[str] = Field(default_factory=list)
    confidence: float
    # The model's own call on whether this expires. Dated facts are answered
    # in the turn and dropped here.
    durable: bool


class _WebHarvestResponse(BaseModel):
    claims: list[_WebClaim] = Field(default_factory=list)


def _render_findings(result: WebSearchResult) -> str:
    lines = [f"Search: {result.query}", ""]
    where = ", ".join(
        p for p in (result.neighborhood, result.city, result.country_code) if p
    )
    if where:
        lines.append(f"The user was asking about: {where}")
        lines.append("")
    lines.append("Findings:")
    for idx, finding in enumerate(result.findings):
        attribution = " / ".join(p for p in (finding.source, finding.age) if p)
        suffix = f" [{attribution}]" if attribution else ""
        lines.append(f"  [{idx}] {finding.text}{suffix}")
    return "\n".join(lines)


class WebKnowledgeHarvester:
    """One cheap LLM pass over a turn's findings → durable area claims."""

    source_type: SourceType = "web_search"

    def __init__(
        self,
        instructor_client: InstructorClient,
        geo_registry: GeoRegistryProtocol,
        *,
        confidence_floor: float = 0.25,
        review_status: ReviewStatus = "approved",
    ) -> None:
        self._client = instructor_client
        self._registry = geo_registry
        self.confidence_floor = confidence_floor
        self.review_status = review_status

    async def harvest(
        self,
        result: WebSearchResult,
        *,
        user_id: str | None = None,
    ) -> list[StructuredClaim]:
        """Mine durable local facts out of a search result."""
        if not result.findings:
            return []
        async with traced_call(
            "web_knowledge_harvester.llm",
            "web_knowledge_harvester",
            role="web_harvester",
            user_id=user_id,
            standalone=True,
        ) as t:
            try:
                extraction = await self._client.extract(
                    response_model=_WebHarvestResponse,
                    messages=[
                        {
                            "role": "system",
                            # Rendered from code, so the writer's tag
                            # normalisation and the prompt can never drift.
                            "content": get_prompt("web_harvester")
                            + "\n\n"
                            + render_claim_tag_vocabulary(),
                        },
                        {"role": "user", "content": _render_findings(result)},
                    ],
                )
            except Exception as exc:
                logger.warning("web harvest failed: %s", exc, exc_info=True)
                t.fail(exc)
                return []
            t.usage = extraction.usage
            t.attempts = extraction.attempts
            claims = await self._resolve(
                cast(_WebHarvestResponse, extraction.data), result
            )
            t.output = {"count": len(claims)}
            return claims

    async def _resolve(
        self, response: _WebHarvestResponse, result: WebSearchResult
    ) -> list[StructuredClaim]:
        """Key each claim to the entity it names, verified (ADR-126)."""
        country_code = result.country_code
        resolved: list[StructuredClaim] = []
        for raw in response.claims:
            if not raw.durable:
                logger.debug(
                    "web_claim_dropped_dated", extra={"entity": raw.entity_name}
                )
                continue
            if is_entry_rule(raw.claim):
                # Logged at info, not debug: the model was told not to emit
                # these at all, so one arriving means the prompt rule slipped
                # and the backstop is the only thing holding.
                logger.info(
                    "web_claim_dropped_entry_rule", extra={"entity": raw.entity_name}
                )
                continue
            entity = await self._resolve_entity(raw, result, country_code)
            if entity is None:
                logger.debug(
                    "web_claim_dropped_unresolvable",
                    extra={"scope": raw.scope, "entity": raw.entity_name},
                )
                continue
            scope, geo = entity
            resolved.append(
                StructuredClaim(
                    scope=scope,
                    entity_name=raw.entity_name,
                    claim=raw.claim,
                    tags=raw.tags,
                    confidence=raw.confidence,
                    geo=geo,
                )
            )
        return resolved

    async def _resolve_entity(
        self,
        raw: _WebClaim,
        result: WebSearchResult,
        country_code: str | None,
    ) -> tuple[EntityType, ResolvedGeo] | None:
        try:
            return await self._resolve_entity_inner(raw, result, country_code)
        except Exception:
            # A registry failure (DB down, not a lookup miss) drops this one
            # claim rather than failing the whole harvest.
            logger.warning("web_harvest_geo_resolve_failed", exc_info=True)
            return None

    async def _resolve_entity_inner(
        self,
        raw: _WebClaim,
        result: WebSearchResult,
        country_code: str | None,
    ) -> tuple[EntityType, ResolvedGeo] | None:
        """The claim's real scope and key, or None if it cannot be verified.

        The scope is *returned*, not taken from the model, because the two
        disagree in a way that silently destroyed every claim: the prompt's
        taxonomy calls any populated area a "city", so a fact about Canggu
        comes back scoped `city` — while the turn resolved Canggu as the
        neighbourhood of Badung. Trusting the label meant name-matching
        "Canggu" against "Badung", failing, falling through to a geocode that
        cannot verify Canggu as a city either, and dropping the claim.

        So the turn's own resolved geography wins. Match the name against the
        area at either level and take the scope that match implies; the
        model's label only decides what to do when neither matches.
        """
        if raw.scope == "country":
            row = await self._registry.resolve_country(raw.entity_name)
            if row is None:
                return None
            return ("country", ResolvedGeo(country_code=row.country_code))
        if not country_code:
            return None
        if slugs_match(raw.entity_name, result.neighborhood):
            return (
                "neighborhood",
                ResolvedGeo(
                    country_code=country_code,
                    city=result.city,
                    neighborhood=result.neighborhood,
                ),
            )
        if slugs_match(raw.entity_name, result.city):
            return (
                "city",
                ResolvedGeo(country_code=country_code, city=result.city),
            )
        if raw.scope == "neighborhood":
            # A neighbourhood naming a different area than the turn's own
            # resolves through the registry inside the turn's city — verified
            # or dropped, never free-texted (ADR-126).
            if not result.city:
                return None
            resolved = await self._registry.key_for_location(
                country_code, result.city, raw.entity_name, mint=True
            )
            if resolved is None or resolved.area is None:
                return None
            return (
                "neighborhood",
                ResolvedGeo(
                    country_code=country_code,
                    city=result.city,
                    neighborhood=resolved.area.name,
                ),
            )
        resolved = await self._registry.key_for_location(
            country_code, raw.entity_name, None, mint=True
        )
        if resolved is None or resolved.city is None:
            return None
        return (
            "city",
            ResolvedGeo(
                country_code=resolved.city.country_code, city=resolved.city.name
            ),
        )
