"""Agent tool wrappers.

The agent runs with a small, fixed tool surface bound per request so
each tool can close over request-scoped services (ADR-072). Today there
are two consult-family tools:

- `find_saved` — searches the user's own saved places via
  `HybridSearchService`.
- `suggest_places` — proposes well-known places via an LLM namer and
  validates them against `PlacesSearchService`.

Both share the same Pydantic arg schema (see `_search_args.py`); the
agent picks between them on routing semantics, not on parameter shape.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from kebi.core.agent.tools.candidate_namer import CandidateNamerService
from kebi.core.agent.tools.find_saved_tool import build_find_saved_tool
from kebi.core.agent.tools.suggest_places_tool import build_suggest_places_tool
from kebi.core.extraction.extraction_pipeline import SearchServiceFactory
from kebi.core.places.hybrid_search_service import HybridSearchService


def build_tools(
    hybrid_search: HybridSearchService,
    candidate_namer: CandidateNamerService,
    places_search_factory: SearchServiceFactory,
) -> list[BaseTool]:
    """Build the agent's tool list, binding request-scoped services.

    `hybrid_search` closes over a request-scoped DB session (ADR-072) so
    it is constructed per request, not cached. `places_search_factory`
    is the per-task factory the extraction pipeline already uses — each
    call opens a fresh session, which is required because the
    `suggest_places` fan-out runs N parallel `find()` calls and a
    SQLAlchemy `AsyncSession` is not concurrency-safe. `candidate_namer`
    wraps the process-wide Instructor client and is safe to share — it
    is accepted explicitly here so the factory stays the single seam
    for the agent's collaborators.
    """
    return [
        build_find_saved_tool(hybrid_search),
        build_suggest_places_tool(candidate_namer, places_search_factory),
    ]


__all__ = [
    "build_tools",
    "build_find_saved_tool",
    "build_suggest_places_tool",
    "CandidateNamerService",
]
