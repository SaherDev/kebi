"""Unit tests for should_continue routing (feature 027 M3, FR-026)."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

from kebi.core.agent.graph import (
    NODE_FALLBACK,
    NODE_TOOLS,
    should_continue,
)
from kebi.core.config import get_config


def _base_state(**overrides: object) -> dict:
    state = {
        "messages": [],
        "taste_profile_summary": "",
        "memory_summary": "",
        "user_id": "u1",
        "location": None,
        "reasoning_steps": [],
        "steps_taken": 0,
        "error_count": 0,
    }
    state.update(overrides)  # type: ignore[arg-type]
    return state


def test_routes_to_fallback_on_max_errors_reached() -> None:
    cfg = get_config().agent
    state = _base_state(error_count=cfg.max_errors)
    assert should_continue(state) == NODE_FALLBACK


def test_routes_to_fallback_on_max_errors_exceeded() -> None:
    cfg = get_config().agent
    state = _base_state(error_count=cfg.max_errors + 1)
    assert should_continue(state) == NODE_FALLBACK


def test_routes_to_fallback_on_max_steps_reached() -> None:
    cfg = get_config().agent
    state = _base_state(steps_taken=cfg.max_steps)
    assert should_continue(state) == NODE_FALLBACK


def test_routes_to_fallback_on_max_steps_exceeded() -> None:
    cfg = get_config().agent
    state = _base_state(steps_taken=cfg.max_steps + 1)
    assert should_continue(state) == NODE_FALLBACK


def test_errors_take_precedence_over_tool_calls() -> None:
    cfg = get_config().agent
    ai = AIMessage(
        content="",
        tool_calls=[{"id": "t1", "name": "recall", "args": {}}],
    )
    state = _base_state(error_count=cfg.max_errors, messages=[ai])
    assert should_continue(state) == NODE_FALLBACK


def test_routes_to_tools_when_last_ai_has_tool_calls() -> None:
    ai = AIMessage(
        content="",
        tool_calls=[{"id": "t1", "name": "recall", "args": {"query": "ramen"}}],
    )
    state = _base_state(messages=[HumanMessage(content="find ramen"), ai])
    assert should_continue(state) == NODE_TOOLS


def test_routes_to_end_when_last_ai_has_no_tool_calls() -> None:
    ai = AIMessage(content="Try tipping is not expected in Japan.")
    state = _base_state(messages=[HumanMessage(content="tipping?"), ai])
    assert should_continue(state) == "end"


def test_routes_to_end_when_messages_empty() -> None:
    state = _base_state(messages=[])
    assert should_continue(state) == "end"
