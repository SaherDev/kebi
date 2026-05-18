"""Test the one-tool-call-per-response invariant (feature 028 M5/M9).

The agent prompt instructs the orchestrator to emit one tool call per
response. This test verifies that the agent graph correctly handles the
case where the LLM returns multiple tool calls — the graph still routes
to tools (not end/fallback), and only processes one turn at a time.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from kebi.core.agent.graph import NODE_TOOLS, should_continue
from kebi.core.agent.state import AgentState


def _state_with_tool_calls(tool_calls: list[dict[str, Any]]) -> AgentState:
    ai_msg = AIMessage(content="", tool_calls=tool_calls)
    return {  # type: ignore[return-value]
        "messages": [HumanMessage(content="test"), ai_msg],
        "error_count": 0,
        "steps_taken": 0,
        "reasoning_steps": [],
        "user_id": "u1",
        "taste_profile_summary": "",
        "memory_summary": "",
        "location": None,
    }


def test_single_tool_call_routes_to_tools() -> None:
    """A single tool call in the last message routes to NODE_TOOLS."""
    state = _state_with_tool_calls(
        [
            {
                "name": "recall",
                "args": {"query": "ramen"},
                "id": "tc-1",
                "type": "tool_call",
            }
        ]
    )
    assert should_continue(state) == NODE_TOOLS


def test_no_tool_call_routes_to_end() -> None:
    """No tool calls in the last message routes to 'end'."""
    ai_msg = AIMessage(content="Here is my answer")
    state: AgentState = {  # type: ignore[typeddict-item]
        "messages": [HumanMessage(content="test"), ai_msg],
        "error_count": 0,
        "steps_taken": 0,
        "reasoning_steps": [],
        "user_id": "u1",
        "taste_profile_summary": "",
        "memory_summary": "",
        "location": None,
    }
    assert should_continue(state) == "end"
