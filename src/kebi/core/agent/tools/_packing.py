"""One place where a tool result becomes a graph update (ADR-139).

Splitting what the model reads from what the server keeps is easy to get
wrong tool-by-tool — forget it once and either the context balloons again or
the linker loses the ids it needs. So every place tool packs through here.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import ToolMessage

from kebi.core.agent.state import AgentState
from kebi.core.agent.tools._agent_view import consult_view
from kebi.core.agent.tools.consult_models import ConsultResult


def pack_consult_result(
    *,
    state: AgentState,
    tool_name: str,
    tool_call_id: str,
    result: ConsultResult,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Graph update carrying the lean view to the model, the full one to us.

    `extra` merges caller-owned slots (reasoning steps, counters) so a tool
    still builds exactly one update.
    """
    update: dict[str, Any] = {
        "messages": [
            ToolMessage(
                content=json.dumps(consult_view(result)),
                tool_call_id=tool_call_id,
                name=tool_name,
            )
        ],
        "tool_payloads": (state.get("tool_payloads") or [])
        + [
            {
                "tool": tool_name,
                "tool_call_id": tool_call_id,
                "payload": result.model_dump(mode="json"),
            }
        ],
    }
    update.update(extra or {})
    return update
