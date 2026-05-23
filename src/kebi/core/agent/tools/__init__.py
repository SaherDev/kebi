"""Agent tool wrappers.

ADR-075 dropped the agent to zero tools. `find_saved` re-introduces
the first tool of the new consult family (`search_suggested` and
`discover_others` land in follow-ups and will slot into the same
`build_tools` signature). The factory takes its dependencies
explicitly so per-request DI (ADR-072) is preserved — services that
close over a request-scoped DB session must never be cached.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from kebi.core.agent.tools.find_saved_tool import build_find_saved_tool
from kebi.core.places.hybrid_search_service import HybridSearchService


def build_tools(hybrid_search: HybridSearchService) -> list[BaseTool]:
    """Build the agent's tool list, binding request-scoped services."""
    return [build_find_saved_tool(hybrid_search)]


__all__ = [
    "build_tools",
    "build_find_saved_tool",
]
