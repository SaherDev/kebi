"""Research reader service — the retrieval funnel over the claims store.

The knowledge layer's first agent-facing reader (the repository reserved
`approved_only=True` for exactly this). Four stages, each narrowing cheaply
before the next; no stage searches the whole store and no stage spends a
model call — the orchestrator already in the loop does the final semantic
judgment for free:

A. **Resolve** the asked-about entity (staged, verified-or-refuse — §resolver).
   A shaky entity never proceeds to retrieval.
B. **Entity-bounded read** by exact keys only (ADR-120): the resolved scope,
   its descendants (ranked-and-capped, never dumped raw), and its ancestors
   (a broader entity's claims are reachable). Every read is
   `approved_only=True` (ADR-122) and user-scoped (global + the caller's own
   claims). All reads hit the `(entity_type, entity_key)` index.
C. **In-memory relevance rank** — a pure, config-weighted score over the
   already-bounded candidate set (tag match on the controlled vocabulary,
   lexical text overlap, writer trust, proximity to the asked scope). A
   prioritizer, not the banned store-level semantic search.
D. **Honest empties** — `no_claims` (nothing known here) or `no_topic_match`
   (know the place, not this angle) with a clarification for the agent to
   ask; otherwise the top-N notes, coarse-labeled, never raw provenance.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from anyascii import anyascii
from pydantic import BaseModel, ConfigDict

from kebi.core.knowledge.research_models import ResearchNote, ResearchResult
from kebi.core.knowledge.research_resolver import (
    ResearchEntityResolver,
    ResolvedEntity,
)
from kebi.core.knowledge.schemas import KnowledgeClaim, note_source_label
from kebi.core.knowledge.tags import _fold

if TYPE_CHECKING:
    from kebi.core.agent.location import WorkingLocation
    from kebi.db.repositories.knowledge_claim_repository import (
        KnowledgeClaimRepository,
    )

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Question scaffolding that carries no topic signal. Broad-question words
# ("what should I know", "any tips", "general info") must not read as a
# topic, or "tell me about X" phrasings would trip the no_topic_match
# floor. Deliberately conservative beyond that — a missed stopword only
# adds a harmless token.
_STOPWORDS = frozenset(
    "the a an in at on of for to and or is are was it its this that "
    "what whats which how where when who tell me my you your about "
    "there here near around good best any some with should would could "
    "know things stuff anything something everything advice tips tip "
    "info information worth like want need get".split()
)


def _tokens(text: str) -> set[str]:
    """Topic tokens of a query or claim: ASCII-folded lowercase words,
    stopwords and one-to-two-letter noise dropped."""
    return {
        t
        for t in _TOKEN_RE.findall(anyascii(text).lower())
        if len(t) >= 3 and t not in _STOPWORDS
    }


class ResearchRankingWeights(BaseModel):
    """Stage-C weights — pure data, injected from config so ranking is
    tunable without a release (same shape as `compute_confidence`)."""

    model_config = ConfigDict(frozen=True)

    w_tag: float = 0.5
    w_text: float = 0.3
    w_trust: float = 0.2
    w_prox: float = 0.1


class ResearchService:
    """Answers a research question with entity-scoped insider notes."""

    def __init__(
        self,
        repo: KnowledgeClaimRepository,
        resolver: ResearchEntityResolver,
        *,
        default_limit: int,
        max_limit: int,
        notes_limit: int,
        weights: ResearchRankingWeights,
        topic_relevance_floor: float,
    ) -> None:
        self._repo = repo
        self._resolver = resolver
        self._default_limit = default_limit
        self._max_limit = max_limit
        self._notes_limit = notes_limit
        self._weights = weights
        self._topic_relevance_floor = topic_relevance_floor

    async def research(
        self,
        *,
        query: str,
        tags: list[str] | None = None,
        city: str | None = None,
        country: str | None = None,
        neighborhood: str | None = None,
        working_location: WorkingLocation | None = None,
        user_id: str,
        limit: int | None = None,
    ) -> ResearchResult:
        # Stage A — resolve the asked-about entity, or stop with a clarify.
        entity = await self._resolver.resolve(
            city=city,
            country=country,
            neighborhood=neighborhood,
            working_location=working_location,
        )
        if entity.needs_clarification or not entity.entity_key:
            return ResearchResult(
                empty_reason=entity.empty_reason or "unresolved",
                clarification=entity.clarification_reason,
            )

        # Stage B — entity-bounded read (exact keys, approved, user-scoped).
        candidates = await self._read(entity, user_id)
        if not candidates:
            return ResearchResult(
                entity_name=entity.entity_name,
                entity_key=entity.entity_key,
                empty_reason="no_claims",
                clarification=(
                    f"kebi has no insider notes about {entity.entity_name} yet"
                ),
            )

        # Stage C — in-memory relevance rank over the bounded set.
        wanted_tags = {_fold(t) for t in (tags or []) if t.strip()}
        query_tokens = _tokens(query)
        scored = sorted(
            (
                (self._score(claim, prox, wanted_tags, query_tokens), claim)
                for claim, prox in candidates
            ),
            key=lambda pair: pair[0].score,
            reverse=True,
        )

        # Stage D — honest empties, else the top-N as notes. The topic floor
        # applies to the relevance component only, and only when the question
        # actually carries topic signal — a broad "tell me about X" ranks on
        # trust/proximity alone and is never a topic mismatch.
        if (wanted_tags or query_tokens) and all(
            s.relevance < self._topic_relevance_floor for s, _ in scored
        ):
            return ResearchResult(
                entity_name=entity.entity_name,
                entity_key=entity.entity_key,
                empty_reason="no_topic_match",
                clarification=(
                    f"kebi knows {entity.entity_name}, but nothing on this "
                    "specific topic yet"
                ),
            )
        top = scored[: self._effective_limit(limit)]
        return ResearchResult(
            entity_name=entity.entity_name,
            entity_key=entity.entity_key,
            notes=[_to_note(claim) for _, claim in top],
        )

    # ---- Stage B ----------------------------------------------------------

    async def _read(
        self, entity: ResolvedEntity, user_id: str
    ) -> list[tuple[KnowledgeClaim, int]]:
        """Read the resolved scope's claims with hierarchy: a neighborhood
        inherits its ancestors, a city adds its neighborhoods (descend) plus
        its country, a country descends into its cities (DECIDED — most
        claims are city-scoped per ADR-124, a strict country-key read would
        answer almost nothing). Each claim carries its proximity: depth
        distance from the asked scope, so Stage C ranks the specific over
        the ambient in both directions."""
        key = entity.entity_key
        assert key is not None  # guarded by the caller
        if entity.entity_type == "neighborhood":
            claims = await self._repo.list_for_entities(
                _ancestry(key), user_id=user_id, approved_only=True
            )
        elif entity.entity_type == "city":
            under = await self._repo.list_under_prefix(
                key, user_id=user_id, approved_only=True
            )
            above = await self._repo.list_for_entities(
                _ancestry(key)[1:], user_id=user_id, approved_only=True
            )
            claims = [*under, *above]
        else:  # country
            claims = await self._repo.list_under_prefix(
                key, user_id=user_id, approved_only=True
            )
        own_depth = key.count("/")
        return [
            (claim, abs(claim.entity_key.count("/") - own_depth)) for claim in claims
        ]

    # ---- Stage C ----------------------------------------------------------

    def _score(
        self,
        claim: KnowledgeClaim,
        proximity: int,
        wanted_tags: set[str],
        query_tokens: set[str],
    ) -> _Score:
        w = self._weights
        relevance = w.w_tag * _tag_overlap(
            claim.tags, wanted_tags, query_tokens
        ) + w.w_text * _text_overlap(claim.claim, query_tokens)
        score = relevance + w.w_trust * claim.confidence - w.w_prox * proximity
        return _Score(score=score, relevance=relevance)

    def _effective_limit(self, limit: int | None) -> int:
        requested = limit if limit is not None and limit >= 1 else self._default_limit
        return min(requested, self._max_limit, self._notes_limit)


class _Score(BaseModel):
    model_config = ConfigDict(frozen=True)

    score: float
    relevance: float


def _ancestry(key: str) -> list[str]:
    """A geo key plus its ancestors, most specific first:
    "vn/da-nang/my-khe" → ["vn/da-nang/my-khe", "vn/da-nang", "vn"]."""
    parts = key.split("/")
    return ["/".join(parts[: i + 1]) for i in range(len(parts) - 1, -1, -1)]


def _tag_overlap(
    claim_tags: list[str], wanted_tags: set[str], query_tokens: set[str]
) -> float:
    """Fraction of the claim's tags the question asked for — exact match on
    the controlled vocabulary (folded), or a tag whose word parts appear in
    the query ("atm fees in…" hits `no_fee_atm`)."""
    if not claim_tags:
        return 0.0
    hits = 0
    for tag in claim_tags:
        folded = _fold(tag)
        if folded in wanted_tags or set(folded.split("_")) & query_tokens:
            hits += 1
    return hits / len(claim_tags)


def _text_overlap(claim_text: str, query_tokens: set[str]) -> float:
    """Fraction of the query's topic tokens the claim's own text covers."""
    if not query_tokens:
        return 0.0
    return len(_tokens(claim_text) & query_tokens) / len(query_tokens)


def _to_note(claim: KnowledgeClaim) -> ResearchNote:
    return ResearchNote(
        id=claim.id,
        text=claim.claim,
        tags=list(claim.tags),
        source=note_source_label(claim.source_type),
        confidence=claim.confidence,
        agree_count=claim.agree_count,
        disagree_count=claim.disagree_count,
    )
