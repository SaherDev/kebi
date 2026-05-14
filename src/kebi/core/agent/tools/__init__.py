"""Agent tool wrappers.

`build_tools(recall, consult)` returns the two @tool-decorated async
wrappers in stable order (recall → consult) for passing to
`llm.bind_tools(tools)` inside `build_graph(...)`. The save tool was
removed by ADR-073 — extraction is HTTP-only via `POST /v1/extract`.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from kebi.core.agent.tools.consult_tool import (
    ConsultToolInput,
    build_consult_tool,
)
from kebi.core.agent.tools.recall_tool import (
    RecallToolInput,
    build_recall_tool,
)
from kebi.core.consult.service import ConsultService
from kebi.core.recall.service import RecallService


def build_tools(
    recall: RecallService,
    consult: ConsultService,
) -> list[BaseTool]:
    """Return the two @tool callables in stable order."""
    return [
        build_recall_tool(recall),
        build_consult_tool(consult),
    ]


__all__ = [
    "ConsultToolInput",
    "RecallToolInput",
    "build_consult_tool",
    "build_recall_tool",
    "build_tools",
]
