"""Tests for per-tool timeout guard (feature 028 M9)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

from langchain_core.messages import ToolMessage
from langgraph.types import Command

from kebi.core.agent.tools._timeout import with_timeout


async def _fast_body() -> Command[Any]:
    return Command(update={"messages": [], "reasoning_steps": []})


async def _slow_body(delay: float) -> Command[Any]:
    await asyncio.sleep(delay)
    return Command(update={"messages": [], "reasoning_steps": []})


async def test_with_timeout_returns_normal_result_on_fast_body() -> None:
    """with_timeout returns the body's result when it completes within timeout."""
    state: dict[str, Any] = {"error_count": 0, "reasoning_steps": []}
    result = await with_timeout("recall", "tc-1", state, _fast_body())
    assert isinstance(result, Command)


async def test_with_timeout_increments_error_count_on_timeout() -> None:
    """with_timeout returns a degraded Command that increments error_count."""
    state: dict[str, Any] = {"error_count": 0, "reasoning_steps": []}
    # Use a coroutine that sleeps longer than our patched timeout
    slow_coro = _slow_body(999.0)

    # Patch config to use 0.01s timeout for the test
    from unittest.mock import MagicMock, patch

    mock_config = MagicMock()
    mock_config.agent.tool_timeouts_seconds.recall = 0  # 0s → immediate timeout

    with patch(  # noqa: SIM117
        "kebi.core.agent.tools._timeout.get_config",
        return_value=mock_config,
    ):
        result = await with_timeout("recall", "tc-2", state, slow_coro)

    assert isinstance(result, Command)
    assert result.update["error_count"] == 1


async def test_with_timeout_surfaces_user_visible_timeout_step() -> None:
    """Degraded Command from timeout includes a tool.summary user-visible step."""
    state: dict[str, Any] = {"error_count": 0, "reasoning_steps": []}

    from unittest.mock import MagicMock, patch

    mock_config = MagicMock()
    mock_config.agent.tool_timeouts_seconds.save = 0

    with patch(  # noqa: SIM117
        "kebi.core.agent.tools._timeout.get_config",
        return_value=mock_config,
    ):
        result = await with_timeout("save", "tc-3", state, _slow_body(999.0))

    steps = result.update.get("reasoning_steps", [])
    assert any(s.visibility == "user" and "timed out" in s.summary for s in steps)


async def test_with_timeout_includes_tool_message_on_timeout() -> None:
    """Degraded Command from timeout includes a ToolMessage with error payload."""
    state: dict[str, Any] = {"error_count": 0, "reasoning_steps": []}

    mock_config = MagicMock()
    mock_config.agent.tool_timeouts_seconds.consult = 0

    with patch(  # noqa: SIM117
        "kebi.core.agent.tools._timeout.get_config",
        return_value=mock_config,
    ):
        result = await with_timeout("consult", "tc-4", state, _slow_body(999.0))

    messages = result.update.get("messages", [])
    assert any(isinstance(m, ToolMessage) for m in messages)
    tool_msg = next(m for m in messages if isinstance(m, ToolMessage))
    assert "timeout" in tool_msg.content
    assert tool_msg.tool_call_id == "tc-4"


async def test_tool_timeout_tagged_correctly() -> None:
    """with_timeout emits a Langfuse span tagged error_type='tool_timeout'."""
    state: dict[str, Any] = {
        "error_count": 0,
        "reasoning_steps": [],
        "user_id": "u1",
    }
    mock_span = MagicMock()
    mock_tracer = MagicMock()
    mock_tracer.generation.return_value = mock_span
    mock_config = MagicMock()
    mock_config.agent.tool_timeouts_seconds.recall = 0

    with (
        patch(
            "kebi.core.agent.tools._timeout.get_config",
            return_value=mock_config,
        ),
        patch(
            "kebi.core.agent.tools._timeout.get_tracing_client",
            return_value=mock_tracer,
        ),
    ):
        await with_timeout("recall", "tc-5", state, _slow_body(999.0))

    mock_tracer.generation.assert_called_once_with("agent_tool", user_id="u1")
    call_kwargs = mock_span.end.call_args.kwargs
    assert call_kwargs["output"]["error_type"] == "tool_timeout"
    assert call_kwargs["output"]["tool"] == "recall"
    assert call_kwargs["level"] == "ERROR"
