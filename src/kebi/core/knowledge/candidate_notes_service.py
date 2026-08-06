"""Insider notes attached to the places an answer is about to name (ADR-137).

The knowledge layer already held claims per place and per area, but they only
reached the user through a deliberate `research` call. In a recommendation
turn the agent has a 5-call budget and spends it finding places, so the claims
never surfaced and answers read like any model's generic route advice.

This service closes that gap on the retrieval path instead of the routing
path: every place tool, having assembled its candidates, pulls the approved
claims for those exact place keys and for the turn's area in one batched read,
and hands them to the agent alongside the candidates. No extra tool call, no
extra LLM call — one indexed `entity_key IN (...)` query per tool call.

Reads are scoped like every other claims read: approved only (ADR-122), and
`user_id`-scoped so a caller sees global claims plus their own conversation
claims, never another user's.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from kebi.core.knowledge.research_models import ResearchNote
from kebi.core.knowledge.schemas import (
    KnowledgeClaim,
    build_geo_key,
    build_place_key,
    note_source_label,
)
from kebi.db.repositories.knowledge_claim_repository import KnowledgeClaimRepository

if TYPE_CHECKING:
    from kebi.core.agent.location import WorkingLocation


def _to_note(claim: KnowledgeClaim) -> ResearchNote:
    return ResearchNote(
        id=claim.id,
        text=claim.claim,
        tags=claim.tags,
        source=note_source_label(claim.source_type),
        confidence=claim.confidence,
        agree_count=claim.agree_count,
        disagree_count=claim.disagree_count,
    )


def _rank(claims: list[KnowledgeClaim], limit: int) -> list[ResearchNote]:
    """Strongest first — corroboration, then confidence, then recency.

    Agreement leads because a claim two people confirmed is worth more in an
    answer than a lone high-confidence guess; `place_notes_service` ranks by
    confidence alone because the Library shows the counts and lets the reader
    judge, while here the agent sees only the order.
    """
    ordered = sorted(
        claims,
        key=lambda c: (
            c.agree_count - c.disagree_count,
            c.confidence,
            c.created_at,
        ),
        reverse=True,
    )
    return [_to_note(c) for c in ordered[:limit]]


class CandidateNotesService:
    """Batched claim reader for a turn's candidates and its area."""

    def __init__(
        self,
        repo: KnowledgeClaimRepository,
        *,
        per_place_limit: int,
        area_limit: int,
    ) -> None:
        self._repo = repo
        self._per_place_limit = per_place_limit
        self._area_limit = area_limit

    async def notes_for_places(
        self, place_ids: list[str], user_id: str
    ) -> dict[str, list[ResearchNote]]:
        """Top notes per place id, in one read. Places with none are absent."""
        keys = {build_place_key(pid): pid for pid in place_ids if pid}
        if not keys:
            return {}
        claims = await self._repo.list_for_entities(
            list(keys), user_id=user_id, approved_only=True
        )
        grouped: dict[str, list[KnowledgeClaim]] = defaultdict(list)
        for claim in claims:
            grouped[claim.entity_key].append(claim)
        result: dict[str, list[ResearchNote]] = {}
        for key, place_id in keys.items():
            ranked = _rank(grouped.get(key, []), self._per_place_limit)
            if ranked:
                result[place_id] = ranked
        return result

    async def notes_for_area(
        self, working: WorkingLocation | None, user_id: str
    ) -> list[ResearchNote]:
        """Top notes for the turn's area — neighborhood, city, and country.

        All three levels are read together and ranked as one pool: a country
        fact ("cash still rules outside the cities") is as usable in an answer
        as a neighborhood one, and the agent, not this service, decides which
        earns a line. Returns empty when the location carries no country code
        (no code, no canonical key — see `build_geo_key`).
        """
        if working is None or not working.country_code:
            return []
        code = working.country_code
        keys = [code]
        if working.city:
            keys.append(build_geo_key(code, working.city))
            if working.neighborhood:
                keys.append(build_geo_key(code, working.city, working.neighborhood))
        claims = await self._repo.list_for_entities(
            keys, user_id=user_id, approved_only=True
        )
        return _rank(list(claims), self._area_limit)
