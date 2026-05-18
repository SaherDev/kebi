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
    """

    step: str
    summary: str
    source: Literal["agent", "fallback"]
    visibility: Literal["user", "debug"] = "user"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    duration_ms: float | None = None
