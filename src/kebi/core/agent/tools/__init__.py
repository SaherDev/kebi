"""Agent tool wrappers (feature 028 M5).

`build_tools(recall, extraction, consult)` returns the three @tool-decorated
async wrappers in stable order (recall → save → consult) for passing to
`llm.bind_tools(tools)` inside `build_graph(...)`.
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
from kebi.core.agent.tools.save_tool import SaveToolInput, build_save_tool
from kebi.core.consult.service import ConsultService
from kebi.core.extraction.service import ExtractionService
from kebi.core.recall.service import RecallService


def build_tools(
    recall: RecallService,
    extraction: ExtractionService,
    consult: ConsultService,
) -> list[BaseTool]:
    """Return the three @tool callables in stable order."""
    return [
        build_recall_tool(recall),
        build_save_tool(extraction),
        build_consult_tool(consult),
    ]


__all__ = [
    "ConsultToolInput",
    "RecallToolInput",
    "SaveToolInput",
    "build_consult_tool",
    "build_recall_tool",
    "build_save_tool",
    "build_tools",
]
