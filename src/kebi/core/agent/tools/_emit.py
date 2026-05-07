"""Shared emit helpers for tool wrappers (feature 028 M5).

Every tool wrapper uses the same fan-out pattern: build a closure that
appends debug-visibility `ReasoningStep`s to a local `collected` list and
forwards each step to `langgraph.config.get_stream_writer()` when a
streaming caller is attached, then — after the service returns — append
a user-visible `tool.summary` via `append_summary`.

Centralizing these two helpers means Langfuse spans, metric counters, or
default step-field values change in exactly one place and affect all
three wrappers.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Literal

from langgraph.config import get_stream_writer

from kebi.core.agent.reasoning import ReasoningStep
from kebi.core.emit import EmitFn

logger = logging.getLogger(__name__)

ToolName = Literal["recall", "save", "consult"]


def _get_writer_safe() -> Any:
    """Return the active stream writer or None outside a runnable context.

    `langgraph.config.get_stream_writer()` raises `RuntimeError` when called
    outside graph execution. Unit tests call the tool's body directly
    (without a graph), so we degrade to `None` silently. During graph
    execution the real writer (or None when no caller is streaming) is
    returned.
    """
    try:
        return get_stream_writer()
    except RuntimeError:
        return None


def build_emit_closure(
    tool_name: ToolName,
) -> tuple[list[ReasoningStep], EmitFn]:
    """Return `(collected, emit)` for a tool wrapper.

    `emit(step, summary, duration_ms=None)`:
      - Appends a debug-visibility `ReasoningStep` to `collected`.
      - When `duration_ms` is `None`, computes elapsed from timestamp delta
        (time since previous emit on this closure, or since closure build
        time for the first emit). When the caller passes it explicitly,
        the supplied value is used verbatim.
      - Forwards to `get_stream_writer()` for live SSE fan-out when a
        streaming caller is attached; silent no-op otherwise.
    """
    collected: list[ReasoningStep] = []
    last_ts = datetime.now(UTC)
    writer = _get_writer_safe()

    def emit(step: str, summary: str, duration_ms: float | None = None) -> None:
        nonlocal last_ts
        now = datetime.now(UTC)
        if duration_ms is None:
            duration_ms = (now - last_ts).total_seconds() * 1000.0
        rs = ReasoningStep(
            step=step,
            summary=summary,
            source="tool",
            tool_name=tool_name,
            visibility="debug",
            timestamp=now,
            duration_ms=duration_ms,
        )
        collected.append(rs)
        if writer is not None:
            writer(rs.model_dump())
        last_ts = now

    return collected, emit


def append_summary(
    collected: list[ReasoningStep],
    tool_name: ToolName,
    summary: str,
) -> None:
    """Append the wrapper's user-visible `tool.summary` step to `collected`.

    `duration_ms` reflects the total tool-invocation elapsed — from the
    first emit in `collected` to `now`. When `collected` is empty (no
    debug emits preceded the summary), `duration_ms` is `0.0`.
    """
    now = datetime.now(UTC)
    start = collected[0].timestamp if collected else now
    rs = ReasoningStep(
        step="tool.summary",
        summary=summary,
        source="tool",
        tool_name=tool_name,
        visibility="user",
        timestamp=now,
        duration_ms=(now - start).total_seconds() * 1000.0,
    )
    collected.append(rs)
    writer = _get_writer_safe()
    if writer is not None:
        writer(rs.model_dump())
    logger.debug(
        "tool.summary appended: tool=%s summary=%r collected_len=%d",
        tool_name,
        summary,
        len(collected),
    )


__all__ = ["ToolName", "build_emit_closure", "append_summary"]
