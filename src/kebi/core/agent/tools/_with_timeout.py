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
- Appends a plain user-visible `ReasoningStep` plus a paired `debug`
  step carrying the technical cause, both under `source="agent"`
  (ADR-075 narrowed `source` and removed `tool_name`; we do not widen
  it back for one wrapper), and emits their SSE active→done frames.

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
from kebi.core.agent.stream_emit import emit_step_active, emit_step_done
from kebi.core.agent.tools._summaries import TITLES

logger = logging.getLogger(__name__)

# Which list field a tool's degraded (timeout / crash) payload empties.
# The consult place tools speak ConsultResult (`candidates`); `research`
# speaks ResearchResult (`notes`). A degraded payload must match the
# tool's own shape so clients and tests never see a foreign field.
_DEGRADED_EMPTY_FIELD = {"research": "notes"}


def tool_step_base_id(tool_name: str, state: AgentState) -> str:
    """Deterministic SSE-lifecycle id base for this tool call (ADR-102).

    Keyed by `tool_calls_used` read at entry — before the tool's `+1` write —
    so the tool body and `with_timeout` derive the same id independently,
    without threading it across the call boundary. Phase steps append
    `.{phase}`; the failure step appends `.failure`.
    """
    return f"{tool_name}#{state.get('tool_calls_used', 0)}"


def _degraded_command(
    tool_name: str,
    tool_call_id: str,
    state: AgentState,
    *,
    user_summary: str,
    detail: str,
) -> Command[Any]:
    """Build the Command emitted when a tool times out or raises.

    The user-visible step carries plain, non-technical copy; the technical
    cause (tool name, timeout, exception type) rides a paired `debug` step
    and the tool_result `error` payload — never the user summary.
    """
    payload = json.dumps(
        {
            _DEGRADED_EMPTY_FIELD.get(tool_name, "candidates"): [],
            "empty_reason": "error",
            "error": detail,
        }
    )
    tool_msg = ToolMessage(
        content=payload,
        tool_call_id=tool_call_id,
        name=tool_name,
    )
    base_id = tool_step_base_id(tool_name, state)
    debug_step = ReasoningStep(
        step=f"{tool_name}.failure_detail",
        summary=detail,
        source="agent",
        visibility="debug",
        duration_ms=0.0,
    )
    user_step = ReasoningStep(
        step=f"{tool_name}.failure",
        title=TITLES.get(tool_name, "searched"),
        summary=user_summary,
        source="agent",
        visibility="user",
        duration_ms=0.0,
    )
    # Self-contained active→done pairs (failure is terminal/instant), so the
    # lifecycle contract holds even when the tool raised before emitting any
    # phase frames. The user `.failure` step stays last in `reasoning_steps`.
    for step, step_id in (
        (debug_step, f"{base_id}.failure_detail"),
        (user_step, f"{base_id}.failure"),
    ):
        started = emit_step_active(
            step_id,
            step.step,
            title=step.title,
            source="agent",
            visibility=step.visibility,
        )
        emit_step_done(step_id, step, started=started)
    return Command(
        update={
            "messages": [tool_msg],
            "reasoning_steps": (state.get("reasoning_steps") or [])
            + [debug_step, user_step],
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
            user_summary="that search timed out",
            detail=f"{tool_name} timed out after {seconds}s",
        )
    except Exception as exc:
        logger.exception("Tool %s raised: %s", tool_name, exc)
        return _degraded_command(
            tool_name,
            tool_call_id,
            state,
            user_summary="couldn't finish that search",
            detail=f"{tool_name} raised {type(exc).__name__}: {str(exc)[:200]}",
        )
