"""Per-tool timeout guard for consult-family agent tools.

Re-introduced for the post-ADR-075 tool re-entry. The shape mirrors the
deleted `_timeout.py`: wrap a tool's actual work in `asyncio.wait_for`,
and on `TimeoutError` (or any unexpected exception) emit a degraded
`Command` that:

- Posts a `ToolMessage` matching the `tool_call_id` so the LangGraph
  tool_use ↔ tool_result pairing stays valid.
- Bumps `error_count` so `should_continue` can route to `fallback` when
  the per-turn budget is exceeded.
- Bumps `tool_calls_used` so the response surfaces the attempt for
  rate-limit accounting (success or failure counts).
- Appends one user-visible `ReasoningStep` under `source="agent"`
  (ADR-075 narrowed `source` and removed `tool_name`; we do not widen
  it back for one wrapper).

`AgentState.error_count`, `tool_calls_used`, and `reasoning_steps` all
use plain-overwrite reducers (FR-021) — the wrapper takes the state
snapshot in so it can compute the new full values, not deltas.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable
from typing import Any

from langchain_core.messages import ToolMessage
from langgraph.types import Command

from kebi.core.agent.reasoning import ReasoningStep
from kebi.core.agent.state import AgentState

logger = logging.getLogger(__name__)


def _degraded_command(
    tool_name: str,
    tool_call_id: str,
    state: AgentState,
    summary: str,
) -> Command[Any]:
    """Build the Command emitted when a tool times out or raises."""
    payload = json.dumps(
        {
            "candidates": [],
            "empty_reason": "error",
            "error": summary,
        }
    )
    tool_msg = ToolMessage(
        content=payload,
        tool_call_id=tool_call_id,
        name=tool_name,
    )
    step = ReasoningStep(
        step=f"{tool_name}.failure",
        summary=summary,
        source="agent",
        visibility="user",
        duration_ms=0.0,
    )
    return Command(
        update={
            "messages": [tool_msg],
            "reasoning_steps": (state.get("reasoning_steps") or []) + [step],
            "error_count": state.get("error_count", 0) + 1,
            "tool_calls_used": state.get("tool_calls_used", 0) + 1,
        }
    )


async def with_timeout(
    tool_name: str,
    tool_call_id: str,
    seconds: int,
    state: AgentState,
    coro: Awaitable[Command[Any]],
) -> Command[Any]:
    """Run `coro` under a per-tool budget. Degrade on timeout or exception.

    The caller passes a coroutine (not a callable) so the work begins
    only when this wrapper awaits it — keeping the `asyncio.wait_for`
    cancellation semantics intact.
    """
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except TimeoutError:
        logger.warning("Tool %s timed out after %ss", tool_name, seconds)
        return _degraded_command(
            tool_name,
            tool_call_id,
            state,
            f"Couldn't run {tool_name} this turn — search took longer than {seconds}s.",
        )
    except Exception as exc:
        logger.exception("Tool %s raised: %s", tool_name, exc)
        return _degraded_command(
            tool_name,
            tool_call_id,
            state,
            f"Couldn't run {tool_name} this turn — {type(exc).__name__}.",
        )
