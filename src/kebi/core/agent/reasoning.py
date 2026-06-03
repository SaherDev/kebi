"""ReasoningStep — one entry in the agent's reasoning trace (feature 027 M3, ADR-062).

ADR-075 removed the recall and consult tools, so no reasoning step is
ever tool-sourced anymore. `source` is now only `agent` or `fallback`
and the `tool_name` field is gone.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field


class ReasoningStep(BaseModel):
    """One entry in the agent's reasoning trace.

    Consumers filter by `visibility` to decide what lands in the JSON
    payload vs what stays in Langfuse/SSE debug.

    `duration_ms` is populated by the agent/fallback node from timestamp
    deltas. Non-null in persisted steps; a lingering `None` is a bug.

    SSE step-lifecycle (ADR-102): the same step is streamed twice, keyed
    by a stable `id` — an `active` frame when it starts (`summary` and
    `duration_ms` null → the frontend shows a skeleton) and a `done` frame
    when it completes (same `id`, `summary` + `duration_ms` filled). The
    `id` and `status` fields are stream-only lifecycle markers: they are
    `None` on every persisted step (the non-stream `POST /v1/chat` path
    returns steps that are implicitly complete) and set only by the
    `stream_emit` helpers as frames go out. `summary` is therefore
    `str | None` — null appears ONLY on an `active` wire frame, never on a
    persisted/`done` step.
    """

    step: str
    summary: str | None = None
    source: Literal["agent", "fallback"]
    visibility: Literal["user", "debug"] = "user"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    duration_ms: float | None = None
    # Stream-only lifecycle markers (None on persisted steps).
    id: str | None = None
    status: Literal["active", "done"] | None = None
