"""Agent tool wrappers.

The agent runs with a small, fixed tool surface bound per request so
each tool can close over request-scoped services (ADR-072). Four
consult-family tools today:

- `find_saved` — searches the user's saved places via
  `HybridSearchService`.
- `suggest_places` — proposes well-known places via an LLM namer and
  validates them against `PlacesSearchService`.
- the catalog floor — no longer a tool. `suggest_places` falls through
  to a plain provider search by itself when nothing was named or
  validated (ADR-140), so the safety property that stops a fabricated
  tip no longer depends on the model routing to a second tool.
- `research` — insider answers from the knowledge layer's claims store
  via `ResearchService`. Where the three place tools surface *where to
  go*, research answers *what a local knows* about a place or area.
- `find_known` — the two joined (ADR-138): places whose claims answer the
  question, retrieved *by* the fact rather than annotated with it.
- `web_search` — the outside world (ADR-145). The only tool that reads
  something kebi does not own, and the only path to a fact the store never
  held: event dates, schedules, prices, current conditions.

The place tools share one Pydantic arg schema (see
`_search_args.py`); `research` has its own (the area args name the
asked-about entity and are used, and `tags` speaks the claim
vocabulary). The agent picks between them on routing semantics.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from kebi.core.agent.tools.candidate_namer import CandidateNamerService
from kebi.core.agent.tools.find_known_tool import build_find_known_tool
from kebi.core.agent.tools.find_saved_tool import build_find_saved_tool
from kebi.core.agent.tools.research_tool import build_research_tool
from kebi.core.agent.tools.suggest_places_tool import build_suggest_places_tool
from kebi.core.agent.tools.web_search_tool import build_web_search_tool
from kebi.core.extraction.extraction_pipeline import SearchServiceFactory
from kebi.core.knowledge.candidate_notes_service import CandidateNotesService
from kebi.core.knowledge.known_places_service import KnownPlacesService
from kebi.core.knowledge.research_service import ResearchService
from kebi.core.places.hybrid_search_service import HybridSearchService
from kebi.core.web.service import WebKnowledgeService


def build_tools(
    hybrid_search: HybridSearchService,
    candidate_namer: CandidateNamerService,
    places_search_factory: SearchServiceFactory,
    research_service: ResearchService,
    *,
    candidate_notes: CandidateNotesService | None = None,
    known_places: KnownPlacesService | None = None,
    web_knowledge: WebKnowledgeService | None = None,
    discovery_enabled: bool = True,
) -> list[BaseTool]:
    """Build the agent's tool list, binding request-scoped services.

    `hybrid_search` closes over a request-scoped DB session (ADR-072) so
    it is constructed per request, not cached. `places_search_factory`
    is the per-task factory the extraction pipeline already uses — each
    call opens a fresh session, which is required because the
    `suggest_places` fan-out runs N parallel `find()` calls and a
    SQLAlchemy `AsyncSession` is not concurrency-safe.
    `candidate_namer` wraps the process-wide Instructor client and is
    safe to share — it is accepted explicitly here so the factory
    stays the single seam for the agent's collaborators.
    `research_service` closes over the request-scoped claims repository.
    `candidate_notes` is the same claims store read from the other side: it
    attaches kebi's insider notes to whatever the place tools return, so a
    recommendation turn layers what kebi knows without spending one of its
    five tool calls on `research` (ADR-137). Optional — omitted, the tools
    return candidates with empty `notes`.
    `known_places` backs `find_known` (ADR-138), the claims-driven retrieval
    path: it is ungated like `research`, since two indexed reads cost nothing
    external. Optional so a minimal graph can be built without it.
    `web_knowledge` backs `web_search` (ADR-145) — the only tool that reads
    something kebi does not own. Optional: omitted, the tool is simply not
    bound and the agent answers world questions from its own knowledge, which
    is the pre-ADR-145 behaviour.

    `discovery_enabled` is the plan-tier gate: `find_saved` (the user's
    own library, zero external cost) is always available; the two
    external-provider tool (`suggest_places`, which hits Google Places at
    real marginal cost) is withheld for tiers that
    do not pay for new-place discovery. `research` is ungated — a
    knowledge-layer read costs nothing external (at most one free
    Nominatim geocode), so every tier can ask what kebi knows.
    """
    tools: list[BaseTool] = [build_find_saved_tool(hybrid_search, candidate_notes)]
    if known_places is not None:
        tools.append(build_find_known_tool(known_places, candidate_notes))
    if discovery_enabled:
        tools.append(
            build_suggest_places_tool(
                candidate_namer, places_search_factory, candidate_notes
            )
        )

    tools.append(build_research_tool(research_service))
    if web_knowledge is not None:
        tools.append(build_web_search_tool(web_knowledge))
    return tools


__all__ = [
    "build_tools",
    "build_find_saved_tool",
    "build_suggest_places_tool",
    "build_research_tool",
    "build_find_known_tool",
    "build_web_search_tool",
    "CandidateNamerService",
]
