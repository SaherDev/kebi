"""fallback_node emission tests (feature 027 M3, FR-027; M9 updates debug steps)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage

from kebi.core.agent.graph import fallback_node
from kebi.core.agent.reasoning import ReasoningStep
from kebi.core.config import get_config


def _base_state(**overrides: object) -> dict:
    state = {
        "messages": [],
        "taste_profile_summary": "",
        "memory_summary": "",
        "user_id": "u1",
        "location": None,
        "last_recall_results": None,
        "reasoning_steps": [],
        "steps_taken": 0,
        "error_count": 0,
    }
    state.update(overrides)  # type: ignore[arg-type]
    return state


def test_fallback_node_composes_graceful_ai_message() -> None:
    cfg = get_config().agent
    update = fallback_node(_base_state(steps_taken=cfg.max_steps))
    assert "messages" in update
    msgs = update["messages"]
    assert len(msgs) == 1
    assert isinstance(msgs[0], AIMessage)
    assert "something went wrong" in msgs[0].content.lower()


def test_fallback_node_emits_user_visible_step_on_max_steps() -> None:
    """One user-visible 'fallback' step is emitted on max_steps (M9 may add debug)."""
    cfg = get_config().agent
    update = fallback_node(_base_state(steps_taken=cfg.max_steps))
    steps = update["reasoning_steps"]
    user_steps = [s for s in steps if s.visibility == "user"]
    assert len(user_steps) == 1
    step = user_steps[0]
    assert isinstance(step, ReasoningStep)
    assert step.step == "fallback"
    assert step.source == "fallback"
    assert step.tool_name is None
    assert str(cfg.max_steps) in step.summary


def test_fallback_node_emits_user_visible_step_on_max_errors() -> None:
    cfg = get_config().agent
    update = fallback_node(_base_state(error_count=cfg.max_errors))
    user_steps = [s for s in update["reasoning_steps"] if s.visibility == "user"]
    assert len(user_steps) == 1
    step = user_steps[0]
    assert step.step == "fallback"
    assert step.source == "fallback"
    assert "errors" in step.summary.lower()


def test_fallback_node_preserves_existing_reasoning_steps() -> None:
    cfg = get_config().agent
    existing = [
        ReasoningStep(
            step="agent.tool_decision",
            summary="starting",
            source="agent",
            visibility="user",
        ),
    ]
    update = fallback_node(
        _base_state(steps_taken=cfg.max_steps, reasoning_steps=existing)
    )
    steps = update["reasoning_steps"]
    step_names = [s.step for s in steps]
    assert "agent.tool_decision" in step_names
    assert "fallback" in step_names


def test_fallback_node_emits_debug_diagnostics_on_m9() -> None:
    """M9: max_steps_detail debug step is present when steps budget exceeded."""
    cfg = get_config().agent
    update = fallback_node(_base_state(steps_taken=cfg.max_steps))
    debug_steps = [s for s in update["reasoning_steps"] if s.visibility == "debug"]
    assert any(s.step == "max_steps_detail" for s in debug_steps)


def test_fallback_emits_langfuse_span_with_error_type_max_errors() -> None:
    """fallback_node emits a Langfuse span tagged error_type='max_errors'."""
    cfg = get_config().agent
    mock_span = MagicMock()
    mock_tracer = MagicMock()
    mock_tracer.generation.return_value = mock_span

    with patch(
        "kebi.core.agent.graph.get_tracing_client",
        return_value=mock_tracer,
    ):
        fallback_node(_base_state(error_count=cfg.max_errors))

    mock_tracer.generation.assert_called_once_with(
        "agent_fallback", user_id="u1"
    )
    mock_span.end.assert_called_once()
    call_kwargs = mock_span.end.call_args.kwargs
    assert call_kwargs["output"]["error_type"] == "max_errors"
    assert call_kwargs["level"] == "ERROR"


def test_fallback_emits_langfuse_span_with_error_type_max_steps() -> None:
    """fallback_node emits a Langfuse span tagged error_type='max_steps'."""
    cfg = get_config().agent
    mock_span = MagicMock()
    mock_tracer = MagicMock()
    mock_tracer.generation.return_value = mock_span

    with patch(
        "kebi.core.agent.graph.get_tracing_client",
        return_value=mock_tracer,
    ):
        fallback_node(_base_state(steps_taken=cfg.max_steps))

    call_kwargs = mock_span.end.call_args.kwargs
    assert call_kwargs["output"]["error_type"] == "max_steps"
    assert call_kwargs["level"] == "ERROR"
