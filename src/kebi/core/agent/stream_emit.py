"""SSE step-lifecycle emission for the agent graph (ADR-102).

Single place that owns the LangGraph custom-stream writer and the active→done
lifecycle contract. Nodes and tools call these helpers at every reasoning-step
site; on the non-streaming `POST /v1/chat` path there is no writer so frame
emission no-ops — but the timing still runs, so both paths get real
`duration_ms` values.

Lifecycle contract: every step is emitted twice, keyed by a stable `id` — an
`active` frame when it starts (`summary`/`duration_ms` null) and a `done`
frame when it completes. Every `done` frame MUST be preceded by an `active`
frame with the same `id`. The reverse is allowed: an interrupted step (e.g. a
tool that times out mid-phase) may emit `active` with no `done` — the frontend
renders that as a step left in its skeleton state.

Timing is explicit and local — no shared state. `emit_step_active` returns a
monotonic start token; the caller hands it back to `emit_step_done`, which
measures the elapsed wall-clock and stamps `duration_ms` onto the step
(mutating it, so the same object the caller appends to `reasoning_steps`
carries the real latency, not the `0.0` placeholder the construction sites
default to). A step's `active` and `done` always run in one function scope, so
the token is just a local variable — correct under any number of concurrent
turns, with nothing to leak between users.

The wire shape is `ReasoningStep` itself (its stream-only `id`/`status`
markers set here): the JSON path and the stream carry the same step shape,
the stream just sends it as lifecycle-tagged chunks. The `id` is derived
deterministically from per-turn state counters at the call sites (e.g.
`f"{tool_name}#{tool_calls_used}"`).
"""

from __future__ import annotations

import time
from typing import Any, Literal

from langgraph.config import get_stream_writer

from kebi.core.agent.reasoning import ReasoningStep


def _writer() -> Any | None:
    """Return the active LangGraph stream writer, or None when not streaming."""
    try:
        return get_stream_writer()
    except RuntimeError:
        return None


def emit_step_active(
    step_id: str,
    step: str,
    *,
    source: Literal["agent", "fallback"],
    visibility: Literal["user", "debug"] = "user",
) -> float:
    """Mark a step as starting now and emit its `active` frame.

    Returns a start token to hand to `emit_step_done` for duration measurement.
    The token is returned even off-stream (no writer) so the JSON path still
    gets a real `duration_ms`.
    """
    started = time.perf_counter()
    writer = _writer()
    if writer is not None:
        frame = ReasoningStep(
            step=step,
            summary=None,
            source=source,
            visibility=visibility,
            duration_ms=None,
            id=step_id,
            status="active",
        )
        writer(frame.model_dump(mode="json"))
    return started


def emit_step_done(
    step_id: str,
    step: ReasoningStep,
    *,
    started: float | None = None,
) -> None:
    """Complete a step: stamp its measured duration, then emit the `done` frame.

    When `started` (the token from `emit_step_active`) is given, mutates
    `step.duration_ms` in place with the elapsed time, so the same object the
    caller appends to `reasoning_steps` carries the real latency on both the
    stream and the JSON path. Without a token the step's constructed
    `duration_ms` is left untouched.
    """
    if started is not None:
        step.duration_ms = (time.perf_counter() - started) * 1000.0
    writer = _writer()
    if writer is None:
        return
    frame = step.model_copy(update={"id": step_id, "status": "done"})
    writer(frame.model_dump(mode="json"))


__all__ = ["emit_step_active", "emit_step_done"]
