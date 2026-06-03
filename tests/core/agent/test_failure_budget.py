"""Tests for error_count failure budget in the agent graph (feature 028 M9)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from langchain_core.messages import AIMessage, HumanMessage

from kebi.core.agent.graph import NODE_FALLBACK, should_continue
from kebi.core.agent.state import AgentState


async def test_should_continue_routes_to_fallback_on_max_errors() -> None:
    """should_continue returns NODE_FALLBACK when error_count >= max_errors."""
    from kebi.core.config import get_config

    max_errors = get_config().agent.max_errors
    state: AgentState = {
        "messages": [HumanMessage(content="hi"), AIMessage(content="ok")],
        "error_count": max_errors,
        "steps_taken": 0,
        "reasoning_steps": [],
        "user_id": "u1",
        "taste_profile_summary": "",
        "memory_summary": "",
        "location": None,
    }
    result = should_continue(state)
    assert result == NODE_FALLBACK


async def test_error_count_increments_on_agent_node_failure(
    monkeypatch: Any,
) -> None:
    """agent_node increments error_count after exhausting LLM retries,
    appends an AIMessage explaining the connection error, and emits a
    user-visible reasoning step."""
    from kebi.core.agent import graph as graph_module
    from kebi.core.agent.graph import make_agent_node

    # Keep the test fast — skip backoff sleeps.
    monkeypatch.setattr(graph_module, "_LLM_BACKOFF_BASE_SECONDS", 0)

    failing_llm = MagicMock()
    failing_llm.bind_tools = MagicMock(return_value=failing_llm)
    call_count = {"n": 0}

    async def _fail(_messages: Any) -> AIMessage:
        call_count["n"] += 1
        raise RuntimeError("LLM failed")

    failing_llm.ainvoke = MagicMock(side_effect=_fail)

    node = make_agent_node(failing_llm, [])
    state: AgentState = {
        "messages": [HumanMessage(content="test")],
        "error_count": 0,
        "steps_taken": 0,
        "reasoning_steps": [],
        "user_id": "u1",
        "taste_profile_summary": "",
        "memory_summary": "",
        "location": None,
    }
    result = await node(state)
    assert result["error_count"] == 1
    assert result["steps_taken"] == 1
    assert call_count["n"] == graph_module._LLM_MAX_ATTEMPTS
    assert len(result["messages"]) == 1
    error_message = result["messages"][0]
    assert isinstance(error_message, AIMessage)
    assert isinstance(error_message.content, str)
    assert "connection issue" in error_message.content.lower()
    user_steps = [s for s in result["reasoning_steps"] if s.visibility == "user"]
    assert len(user_steps) == 1
    # Plain user-facing copy — no exception class name leaked into the summary.
    assert "connection issue" in user_steps[0].summary.lower()


async def test_llm_retry_recovers_on_second_attempt(monkeypatch: Any) -> None:
    """First LLM call fails, second succeeds — agent_node returns the
    successful AIMessage without incrementing error_count."""
    from kebi.core.agent import graph as graph_module
    from kebi.core.agent.graph import make_agent_node

    monkeypatch.setattr(graph_module, "_LLM_BACKOFF_BASE_SECONDS", 0)

    flaky_llm = MagicMock()
    flaky_llm.bind_tools = MagicMock(return_value=flaky_llm)
    call_count = {"n": 0}
    success_msg = AIMessage(content="ok")

    async def _flaky(_messages: Any) -> AIMessage:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("transient")
        return success_msg

    flaky_llm.ainvoke = MagicMock(side_effect=_flaky)

    node = make_agent_node(flaky_llm, [])
    state: AgentState = {
        "messages": [HumanMessage(content="test")],
        "error_count": 0,
        "steps_taken": 0,
        "reasoning_steps": [],
        "user_id": "u1",
        "taste_profile_summary": "",
        "memory_summary": "",
        "location": None,
    }
    result = await node(state)
    assert call_count["n"] == 2
    # Happy path only writes messages/steps_taken/reasoning_steps — error_count
    # is untouched so the state-merge leaves the prior value (0).
    assert "error_count" not in result
    assert result["messages"][0] is success_msg


async def test_fallback_node_emits_user_visible_step() -> None:
    """fallback_node always emits a user-visible 'fallback' ReasoningStep."""
    from kebi.core.agent.graph import fallback_node

    state: AgentState = {
        "messages": [],
        "error_count": 0,
        "steps_taken": 0,
        "reasoning_steps": [],
        "user_id": "u1",
        "taste_profile_summary": "",
        "memory_summary": "",
        "location": None,
    }
    result = fallback_node(state)
    user_steps = [s for s in result["reasoning_steps"] if s.visibility == "user"]
    assert len(user_steps) == 1
    assert user_steps[0].step == "fallback"
