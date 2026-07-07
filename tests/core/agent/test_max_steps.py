"""Tests for max_steps routing + debug diagnostic steps (feature 028 M9)."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

from kebi.core.agent.graph import NODE_FALLBACK, fallback_node, should_continue
from kebi.core.agent.state import AgentState
from kebi.core.config import get_config


def _base_state(steps_taken: int = 0, error_count: int = 0) -> AgentState:
    return {  # type: ignore[return-value]
        "messages": [HumanMessage(content="hi"), AIMessage(content="ok")],
        "error_count": error_count,
        "steps_taken": steps_taken,
        "reasoning_steps": [],
        "user_id": "u1",
        "taste_profile_summary": "",
        "memory_summary": "",
        "location": None,
    }


def test_should_continue_routes_to_fallback_at_max_steps() -> None:
    max_steps = get_config().agent.max_steps
    state = _base_state(steps_taken=max_steps)
    assert should_continue(state) == NODE_FALLBACK


def test_should_continue_does_not_fallback_below_max_steps() -> None:
    max_steps = get_config().agent.max_steps
    state = _base_state(steps_taken=max_steps - 1)
    # last message has no tool_calls → routes to "end"
    result = should_continue(state)
    assert result != NODE_FALLBACK


def test_fallback_node_emits_max_steps_debug_step() -> None:
    """fallback_node emits a debug max_steps_detail step when steps exceeded."""
    max_steps = get_config().agent.max_steps
    state = _base_state(steps_taken=max_steps)
    result = fallback_node(state)
    debug_steps = [s for s in result["reasoning_steps"] if s.visibility == "debug"]
    assert any(s.step == "max_steps_detail" for s in debug_steps)


def test_fallback_node_emits_max_errors_debug_step() -> None:
    """fallback_node emits a debug max_errors_detail step when errors exceeded."""
    max_errors = get_config().agent.max_errors
    state = _base_state(error_count=max_errors)
    result = fallback_node(state)
    debug_steps = [s for s in result["reasoning_steps"] if s.visibility == "debug"]
    assert any(s.step == "max_errors_detail" for s in debug_steps)


def test_fallback_node_no_debug_step_on_generic_failure() -> None:
    """fallback_node does not emit debug step when neither limit was hit."""
    state = _base_state(steps_taken=0, error_count=0)
    result = fallback_node(state)
    debug_steps = [s for s in result["reasoning_steps"] if s.visibility == "debug"]
    assert len(debug_steps) == 0
