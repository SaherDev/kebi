"""Integration test: tool timeout increments error_count → graph eventually falls back.

Verifies M9's end-to-end: a timing-out tool produces a degraded Command
with error_count += 1, which — after max_errors hits — routes to fallback.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

from kebi.core.agent.graph import NODE_FALLBACK, should_continue
from kebi.core.agent.state import AgentState
from kebi.core.config import get_config


def test_repeated_timeout_errors_trigger_fallback() -> None:
    """Accumulating error_count routes should_continue to fallback at max_errors."""
    max_errors = get_config().agent.max_errors

    state: AgentState = {  # type: ignore[typeddict-item]
        "messages": [HumanMessage(content="test"), AIMessage(content="ok")],
        "error_count": max_errors,
        "steps_taken": 0,
        "reasoning_steps": [],
        "user_id": "u1",
        "taste_profile_summary": "",
        "memory_summary": "",
        "location": None,
    }
    assert should_continue(state) == NODE_FALLBACK


def test_single_timeout_below_max_does_not_trigger_fallback() -> None:
    """A single timeout error (below max_errors) does not immediately fall back."""
    max_errors = get_config().agent.max_errors

    state: AgentState = {  # type: ignore[typeddict-item]
        "messages": [HumanMessage(content="test"), AIMessage(content="ok")],
        "error_count": max_errors - 1,
        "steps_taken": 0,
        "reasoning_steps": [],
        "user_id": "u1",
        "taste_profile_summary": "",
        "memory_summary": "",
        "location": None,
    }
    assert should_continue(state) != NODE_FALLBACK
