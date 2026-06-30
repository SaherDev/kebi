"""Agent tool wrappers.

The agent runs with a small, fixed tool surface bound per request so
each tool can close over request-scoped services (ADR-072). Three
consult-family tools today:

- `find_saved` — searches the user's saved places via
  `HybridSearchService`.
- `suggest_places` — proposes well-known places via an LLM namer and
  validates them against `PlacesSearchService`.
- `discover_places` — provider-driven nearby/area search via
  `PlacesSearchService` (no LLM, no saved-collection lookup). The
  fall-through floor: used when the other two tools came back empty in
  the same turn. Utility errands (pharmacy, ATM, gas station, etc.) now
  route to `suggest_places` first — the namer proposes the trusted
  brand/chain and the provider resolves the nearest branch — so
  `discover_places` only handles the generic nearest match when no
  brand validated nearby.

All three share the same Pydantic arg schema (see `_search_args.py`);
the agent picks between them on routing semantics, not on parameter
shape.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from kebi.core.agent.tools.candidate_namer import CandidateNamerService
from kebi.core.agent.tools.discover_places_tool import (
    build_discover_places_tool,
)
from kebi.core.agent.tools.find_saved_tool import build_find_saved_tool
from kebi.core.agent.tools.suggest_places_tool import build_suggest_places_tool
from kebi.core.extraction.extraction_pipeline import SearchServiceFactory
from kebi.core.places.hybrid_search_service import HybridSearchService


def build_tools(
    hybrid_search: HybridSearchService,
    candidate_namer: CandidateNamerService,
    places_search_factory: SearchServiceFactory,
    *,
    discovery_enabled: bool = True,
) -> list[BaseTool]:
    """Build the agent's tool list, binding request-scoped services.

    `hybrid_search` closes over a request-scoped DB session (ADR-072) so
    it is constructed per request, not cached. `places_search_factory`
    is the per-task factory the extraction pipeline already uses — each
    call opens a fresh session, which is required because the
    `suggest_places` fan-out runs N parallel `find()` calls and a
    SQLAlchemy `AsyncSession` is not concurrency-safe. `discover_places`
    only makes one `find()` call per invocation but reuses the same
    factory to keep session ownership uniform across the trio.
    `candidate_namer` wraps the process-wide Instructor client and is
    safe to share — it is accepted explicitly here so the factory
    stays the single seam for the agent's collaborators.

    `discovery_enabled` is the plan-tier gate: `find_saved` (the user's
    own library, zero external cost) is always available; the two
    external-provider tools (`suggest_places`, `discover_places`, which
    hit Google Places at real marginal cost) are withheld for tiers that
    do not pay for new-place discovery.
    """
    tools: list[BaseTool] = [build_find_saved_tool(hybrid_search)]
    if discovery_enabled:
        tools.append(build_suggest_places_tool(candidate_namer, places_search_factory))
        tools.append(build_discover_places_tool(places_search_factory))
    return tools


__all__ = [
    "build_tools",
    "build_find_saved_tool",
    "build_suggest_places_tool",
    "build_discover_places_tool",
    "CandidateNamerService",
]
