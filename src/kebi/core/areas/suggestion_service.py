"""Area suggestion service — verify the areas the agent named (Step 6).

The service behind `suggest_areas`. It takes the names the orchestrator
proposed, resolves each one to a verified area entity, and attaches the
knowledge layer's claims about it.

Two things it deliberately is not:

  - **It is not a ranker.** The agent named these areas in the order it thinks
    they matter, and it holds the query, the taste summary and the
    conversation. Re-sorting here would overrule the model best placed to
    judge, which is the mistake ADR-140 was written against. What this service
    adds is what the agent cannot know: whether the area is real, where it is,
    how big it is, and what kebi has accumulated about it.
  - **It is not a search.** No provider place call, no LLM. A resolve is a
    store read, and only a store miss costs a geocode. That is what makes it
    affordable to run before the place tools rather than instead of them.

Refusals are returned, not swallowed: a name that does not verify comes back in
`refused` so the agent can say so. A refused name is never substituted with
something nearby — that rule is the whole reason a route name cannot become a
stored place under a city's coordinates.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from kebi.core.areas.models import AreaEntity, AreaSummary
from kebi.core.knowledge.schemas import PlaceNote, _slugify, note_rank_key

if TYPE_CHECKING:
    from kebi.core.agent.location import WorkingLocation
    from kebi.core.areas.service import AreaService
    from kebi.db.repositories.knowledge_claim_repository import (
        KnowledgeClaimRepository,
    )

logger = logging.getLogger(__name__)


class AreaSuggestion(BaseModel):
    """One verified area, with its evidence and its entity."""

    model_config = ConfigDict(frozen=True)

    entity: AreaEntity
    summary: AreaSummary


class AreaSuggestionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    suggestions: list[AreaSuggestion] = []
    refused: list[str] = []


class AreaSuggestionService:
    def __init__(
        self,
        areas: AreaService,
        claims: KnowledgeClaimRepository,
        *,
        max_names: int,
        notes_limit: int,
    ) -> None:
        self._areas = areas
        self._claims = claims
        self._max_names = max_names
        self._notes_limit = notes_limit

    async def suggest(
        self,
        *,
        names: list[str],
        user_id: str,
        country: str | None = None,
        city: str | None = None,
        working_location: WorkingLocation | None = None,
    ) -> AreaSuggestionResult:
        """Resolve each named area, in the order given, with its claims.

        `country` is required to resolve anything — the resolver is
        country-scoped by design (ADR-126), which is what stops "Hoi An" from
        matching a restaurant of that name three countries away. It comes from
        the agent's own arg when the question named a country, and otherwise
        from the turn's working location.
        """
        asked = self._clean(names)
        if not asked:
            return AreaSuggestionResult()

        country_code = await self._country_code(country, working_location)
        if country_code is None:
            # Nothing to scope resolution to. Every name is a refusal rather
            # than a guess: an unscoped geocode is exactly the free-text
            # lookup ADR-126 removed.
            return AreaSuggestionResult(refused=asked)

        city_hint = city or (working_location.city if working_location else None)
        entities = await asyncio.gather(
            *(self._resolve(name, country_code, city_hint=city_hint) for name in asked)
        )

        resolved = [(name, e) for name, e in zip(asked, entities, strict=True) if e]
        refused = [name for name, e in zip(asked, entities, strict=True) if e is None]

        notes = await self._notes([e for _, e in resolved], user_id)
        return AreaSuggestionResult(
            suggestions=[
                AreaSuggestion(
                    entity=entity,
                    summary=AreaSummary.from_entity(
                        entity, notes=notes.get(entity.entity_key, [])
                    ),
                )
                for _, entity in resolved
            ],
            refused=refused,
        )

    async def known_areas(self, names: list[str]) -> dict[str, AreaSummary]:
        """Which of these names kebi already knows to be areas, by input name.

        Store-only — no geocode, no provider call, one indexed read. It exists
        for the venue path, which cannot tell geography from a venue on type
        alone: the provider holds two records for Hai Van Pass, and the one
        typed `historical_landmark` is indistinguishable from any restaurant.
        The entity store settles it, and settles it the right way round — the
        answer is not a guard that blocks the pass (that would block Lang Co
        Beach too, which is one of the best stops on the same road) but a
        correction of *kind*: it is an area, so it becomes an area card
        instead of a savable venue row.
        """
        by_slug = {_slugify(name): name for name in names if name.strip()}
        if not by_slug:
            return {}
        found = await self._areas.find_known_by_slug(list(by_slug))
        return {
            by_slug[slug]: AreaSummary.from_entity(entity)
            for slug, entity in found.items()
            if slug in by_slug
        }

    # ---- internals --------------------------------------------------------

    def _clean(self, names: list[str]) -> list[str]:
        """Trim, drop blanks, dedupe case-insensitively, cap at `max_names`."""
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in names:
            name = raw.strip()
            key = name.casefold()
            if not name or key in seen:
                continue
            seen.add(key)
            cleaned.append(name)
        if len(cleaned) > self._max_names:
            logger.info(
                "suggest_areas: %d names asked, resolving the first %d",
                len(cleaned),
                self._max_names,
            )
        return cleaned[: self._max_names]

    async def _country_code(
        self, country: str | None, working: WorkingLocation | None
    ) -> str | None:
        if country:
            entity = await self._areas.resolve_country(country)
            if entity is not None:
                return entity.country_code
        if working is None:
            return None
        if working.country_code:
            return working.country_code.strip().lower()
        entity = await self._areas.resolve_country(working.country)
        return entity.country_code if entity is not None else None

    async def _resolve(
        self, name: str, country_code: str, *, city_hint: str | None
    ) -> AreaEntity | None:
        try:
            return await self._areas.resolve_area(
                name, country_code, city_hint=city_hint
            )
        except Exception as exc:  # noqa: BLE001 - one bad name never kills the turn
            logger.warning("area resolution failed for %r: %s", name, exc)
            return None

    def _record_thin_coverage(self, grouped: dict[str, list[PlaceNote]]) -> None:
        """Log which resolved areas kebi knows little or nothing about.

        The minimal piece of the roadmap's Step 7 that belongs here. Step 6
        can only rank areas as well as the knowledge layer describes them, and
        Step 7 opens by *measuring* coverage — but measuring it from a
        speculative sweep of world geography would answer the wrong question.
        Every turn already names exactly the entities that matter: the ones
        users ask about. Recording thin coverage at the point of resolution
        turns real usage into Step 7's work-list, at the cost of a log line.

        Deliberately not an enrichment trigger: writing new claims needs a
        producer that does not exist yet, and inventing one here would be
        Step 7 done badly rather than Step 6 done. What this leaves behind is
        the input to that decision.
        """
        thin = [key for key, notes in grouped.items() if not notes]
        if thin:
            logger.info(
                "area knowledge thin or absent for: %s", ", ".join(sorted(thin))
            )

    async def _notes(
        self, entities: list[AreaEntity], user_id: str
    ) -> dict[str, list[PlaceNote]]:
        """Claims for the resolved areas, one batched read, strongest first."""
        if not entities:
            return {}
        keys = [e.entity_key for e in entities]
        claims = await self._claims.list_for_entities(
            keys, user_id=user_id, approved_only=True
        )
        grouped: dict[str, list[PlaceNote]] = {key: [] for key in keys}
        for claim in sorted(claims, key=note_rank_key, reverse=True):
            bucket = grouped.get(claim.entity_key)
            if bucket is None or len(bucket) >= self._notes_limit:
                continue
            bucket.append(
                PlaceNote(
                    id=claim.id,
                    text=claim.claim,
                    tags=claim.tags,
                    source_type=claim.source_type,
                    agree_count=claim.agree_count,
                    disagree_count=claim.disagree_count,
                )
            )
        self._record_thin_coverage(grouped)
        return grouped
