"""Tests for the per-turn tool-call cap routing + fallback message branch."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage

from kebi.core.agent.graph import NODE_FALLBACK, fallback_node, should_continue
from kebi.core.agent.state import AgentState
from kebi.core.config import get_config


def _base_state(
    tool_calls_used: int = 0,
    steps_taken: int = 0,
    error_count: int = 0,
) -> AgentState:
    return {  # type: ignore[return-value]
        "messages": [HumanMessage(content="hi"), AIMessage(content="ok")],
        "error_count": error_count,
        "steps_taken": steps_taken,
        "tool_calls_used": tool_calls_used,
        "reasoning_steps": [],
        "user_id": "u1",
        "taste_profile_summary": "",
        "memory_summary": "",
        "location": None,
    }


def test_should_continue_routes_to_fallback_at_max_tool_calls() -> None:
    cap = get_config().agent.max_tool_calls
    state = _base_state(tool_calls_used=cap)
    assert should_continue(state) == NODE_FALLBACK


def test_should_continue_does_not_fallback_below_max_tool_calls() -> None:
    cap = get_config().agent.max_tool_calls
    state = _base_state(tool_calls_used=cap - 1)
    # last message has no tool_calls → routes to "end"
    result = should_continue(state)
    assert result != NODE_FALLBACK


def test_tool_call_cap_takes_precedence_over_max_steps() -> None:
    """When both caps are hit, the tool-call cap routes first.

    Routing precedence matters because `fallback_node` keys its message
    branch on the same precedence — the tool-cap branch produces the
    dedicated "too vague" message, the max_steps branch produces the
    generic apology. If routing didn't agree with the message branch,
    the user would see one message while the debug step claimed
    another cause.
    """
    cfg = get_config().agent
    state = _base_state(
        tool_calls_used=cfg.max_tool_calls,
        steps_taken=cfg.max_steps,
    )
    assert should_continue(state) == NODE_FALLBACK


def test_fallback_node_emits_max_tool_calls_debug_step() -> None:
    cap = get_config().agent.max_tool_calls
    result = fallback_node(_base_state(tool_calls_used=cap))
    debug_steps = [s for s in result["reasoning_steps"] if s.visibility == "debug"]
    assert any(s.step == "max_tool_calls_detail" for s in debug_steps)


def test_fallback_node_uses_tool_cap_message_on_cap_hit() -> None:
    """Cap-hit turn shows the dedicated "too vague" message, not the
    generic apology used for max_steps / max_errors / catch-all."""
    cap = get_config().agent.max_tool_calls
    result = fallback_node(_base_state(tool_calls_used=cap))
    msgs = result["messages"]
    assert len(msgs) == 1
    assert isinstance(msgs[0], AIMessage)
    text = msgs[0].content.lower()
    assert "more detail" in text
    assert "something went wrong" not in text


def test_fallback_node_user_step_summary_mentions_tool_cap() -> None:
    cap = get_config().agent.max_tool_calls
    result = fallback_node(_base_state(tool_calls_used=cap))
    user_steps = [s for s in result["reasoning_steps"] if s.visibility == "user"]
    assert len(user_steps) == 1
    summary = user_steps[0].summary.lower()
    assert str(cap) in summary
    assert "tool" in summary


def test_fallback_node_emits_langfuse_span_with_error_type_max_tool_calls() -> None:
    cap = get_config().agent.max_tool_calls
    mock_span = MagicMock()
    mock_tracer = MagicMock()
    mock_tracer.generation.return_value = mock_span

    with patch(
        "kebi.core.agent.graph.get_tracing_client",
        return_value=mock_tracer,
    ):
        fallback_node(_base_state(tool_calls_used=cap))

    call_kwargs = mock_span.end.call_args.kwargs
    assert call_kwargs["output"]["error_type"] == "max_tool_calls"
    assert call_kwargs["level"] == "ERROR"


def test_tool_cap_branch_preempts_max_steps_branch_in_fallback_node() -> None:
    """When both caps are simultaneously hit, the cap-hit branch wins.

    Matches the routing precedence so debug attribution agrees with the
    user-visible message.
    """
    cfg = get_config().agent
    result = fallback_node(
        _base_state(
            tool_calls_used=cfg.max_tool_calls,
            steps_taken=cfg.max_steps,
        )
    )
    debug_steps = [s for s in result["reasoning_steps"] if s.visibility == "debug"]
    debug_names = [s.step for s in debug_steps]
    assert "max_tool_calls_detail" in debug_names
    assert "max_steps_detail" not in debug_names
