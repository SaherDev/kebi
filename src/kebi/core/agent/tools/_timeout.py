"""Per-tool asyncio.wait_for guard (feature 028 M9)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Any

from langchain_core.messages import ToolMessage
from langgraph.errors import GraphInterrupt, NodeInterrupt
from langgraph.types import Command

from kebi.core.agent.reasoning import ReasoningStep
from kebi.core.config import get_config
from kebi.providers.tracing import get_tracing_client

logger = logging.getLogger(__name__)


async def with_timeout(
    tool_name: str,
    tool_call_id: str,
    state: Mapping[str, Any],
    body: Any,  # coroutine
) -> Command[Any]:
    """Enforce per-tool timeout and catch service exceptions.

    On success, returns the Command from `body` unchanged.
    On asyncio.TimeoutError, returns a degraded Command that increments
    error_count and surfaces a user-visible `tool.summary` step.
    On any other Exception, logs the traceback and returns a similar
    degraded Command so the agent sees the error and the user trace
    records it. `NodeInterrupt` / `GraphInterrupt` are re-raised
    (they are control-flow signals for LangGraph checkpointing, not
    tool failures).

    Args:
        tool_name: Logical tool name matching ToolTimeoutsConfig fields.
        tool_call_id: LangGraph tool call identifier for ToolMessage.
        state: Current AgentState snapshot (read for error_count/reasoning_steps).
        body: Coroutine producing the normal Command result.
    """
    timeout: int = getattr(get_config().agent.tool_timeouts_seconds, tool_name)
    logger.debug("with_timeout entered: tool=%s budget=%ss", tool_name, timeout)
    try:
        cmd: Command[Any] = await asyncio.wait_for(body, timeout=float(timeout))
        return cmd
    except (NodeInterrupt, GraphInterrupt):
        raise
    except TimeoutError:
        logger.warning("tool %s timed out after %ss", tool_name, timeout)
        tracer = get_tracing_client()
        span = tracer.generation("agent_tool", user_id=state.get("user_id"))
        span.end(
            output={"error_type": "tool_timeout", "tool": tool_name},
            level="ERROR",
        )
        return _degraded_command(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            state=state,
            summary=f"{tool_name} timed out after {timeout}s — try again in a moment",
            tool_message_payload={"error": "timeout", "tool": tool_name},
        )
    except Exception as exc:
        logger.exception("tool %s crashed: %s", tool_name, exc)
        exc_type = type(exc).__name__
        exc_msg = str(exc)[:200]
        tracer = get_tracing_client()
        span = tracer.generation("agent_tool", user_id=state.get("user_id"))
        span.end(
            output={"error_type": "tool_crash", "tool": tool_name},
            level="ERROR",
        )
        return _degraded_command(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            state=state,
            summary=f"{tool_name} failed ({exc_type}) — try again or rephrase",
            tool_message_payload={
                "error": "exception",
                "tool": tool_name,
                "type": exc_type,
                "message": exc_msg,
            },
        )


def _degraded_command(
    *,
    tool_name: str,
    tool_call_id: str,
    state: Mapping[str, Any],
    summary: str,
    tool_message_payload: dict[str, Any],
) -> Command[Any]:
    step = ReasoningStep(
        step="tool.summary",
        summary=summary,
        source="tool",
        tool_name=tool_name,
        visibility="user",
    )
    return Command(
        update={
            "error_count": state.get("error_count", 0) + 1,
            "reasoning_steps": (state.get("reasoning_steps") or []) + [step],
            "messages": [
                ToolMessage(
                    content=json.dumps(tool_message_payload),
                    tool_call_id=tool_call_id,
                )
            ],
        }
    )


__all__ = ["with_timeout"]
