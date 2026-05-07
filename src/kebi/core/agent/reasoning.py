"""ReasoningStep — one entry in the agent's reasoning trace (feature 027 M3, ADR-062).

Re-exported by `api/schemas/consult.py` so `ConsultResponse.reasoning_steps`
continues to type-check under the richer schema (FR-024).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ReasoningStep(BaseModel):
    """One entry in the agent's reasoning trace.

    Consumers filter by `visibility` to decide what lands in the JSON
    payload vs what stays in Langfuse/SSE debug. See the M5 catalog in
    `docs/plans/2026-04-21-agent-tool-migration.md` for step names.

    Invariant: `tool_name` is set iff `source == "tool"`. A tool step
    without a tool_name is a bug; a non-tool step with a tool_name is
    a bug. Enforced by the model_validator below.

    `duration_ms` is populated either by the service (when it measured
    the underlying operation directly) or by the wrapper's emit closure
    from timestamp deltas (`core/agent/tools/_emit.py`). Non-null in
    persisted steps; a lingering `None` is a bug.
    """

    step: str
    summary: str
    source: Literal["tool", "agent", "fallback"]
    tool_name: Literal["recall", "save", "consult"] | None = None
    visibility: Literal["user", "debug"] = "user"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    duration_ms: float | None = None

    @model_validator(mode="after")
    def _source_tool_name_consistency(self) -> ReasoningStep:
        if self.source == "tool" and self.tool_name is None:
            raise ValueError(
                f"ReasoningStep(source='tool') requires tool_name; got None "
                f"(step={self.step!r})"
            )
        if self.source != "tool" and self.tool_name is not None:
            raise ValueError(
                f"ReasoningStep(source={self.source!r}) forbids tool_name; "
                f"got {self.tool_name!r} (step={self.step!r})"
            )
        return self
