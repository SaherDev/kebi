"""HybridSearchService — embed the query and delegate to HybridSearchRepo.

Thin layer. The repo speaks SQL and needs a query vector; the service
owns the embedder and produces it. Mirrors the repo's signature so both
scoped (user_id given) and unscoped (user_id is None) modes pass through.

The embedder is called with ``input_type="query"`` — distinct from the
``"document"`` calls in the embedding pipeline that produced the stored
vectors. Voyage trains query and document encoders asymmetrically; using
the wrong type degrades retrieval quality measurably.
"""

from __future__ import annotations

import logging

from .models import HybridSearchFilters, HybridSearchHit
from .protocols import EmbedderProtocol, HybridSearchRepoProtocol

logger = logging.getLogger(__name__)


class HybridSearchService:
    def __init__(
        self,
        repo: HybridSearchRepoProtocol,
        embedder: EmbedderProtocol,
    ) -> None:
        self._repo = repo
        self._embedder = embedder

    async def search(
        self,
        user_id: str | None,
        query: str,
        filters: HybridSearchFilters | None = None,
        limit: int = 20,
        rrf_k: int = 60,
        candidate_multiplier: int = 4,
    ) -> list[HybridSearchHit]:
        """Embed the query and run hybrid retrieval.

        Empty / whitespace-only queries short-circuit to ``[]`` — the
        embedder would either error or return a zero vector that
        kNN-matches nothing meaningful, and websearch_to_tsquery on an
        empty string yields an empty tsquery anyway.
        """
        cleaned = query.strip()
        if not cleaned:
            return []

        vectors = await self._embedder.embed([cleaned], input_type="query")
        query_vector = vectors[0]

        return await self._repo.search(
            user_id=user_id,
            query=cleaned,
            query_vector=query_vector,
            filters=filters,
            limit=limit,
            rrf_k=rrf_k,
            candidate_multiplier=candidate_multiplier,
        )
