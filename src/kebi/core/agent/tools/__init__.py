"""Agent tool wrappers.

The recall and consult tools were removed by ADR-075 — the agent is now
a zero-tool conversational Q&A surface. `build_tools()` returns an empty
list, which `llm.bind_tools(tools)` accepts; the graph's tool node is
simply never reached. The save tool was removed earlier by ADR-073.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool


def build_tools() -> list[BaseTool]:
    """Return the agent's tool list — empty since ADR-075."""
    return []


__all__ = [
    "build_tools",
]
