"""Request and response schemas for POST /v1/chat endpoint."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from kebi.api.schemas.consult import Location

SignalTierHint = Literal["cold", "warming", "chip_selection", "active"]

ChatResponseType = Literal[
    "consult",
    "recall",
    "error",
    "agent",
]


class ChatRequest(BaseModel):
    """Request body for POST /v1/chat endpoint."""

    user_id: str
    message: str
    location: Location | None = None
    signal_tier: SignalTierHint | None = Field(
        default=None,
        description=(
            "Optional tier hint from the product repo (feature 023). Product "
            "reads GET /v1/user/context and forwards the tier so consult can "
            "apply tier-aware behavior (e.g. warming candidate-count blend) "
            "without a second DB read. When null, consult defaults to 'active'."
        ),
    )


class ChatResponse(BaseModel):
    """Response body for POST /v1/chat endpoint.

    type: One of "consult", "recall", "agent", "error". ADR-073
          removed "extract-place" and "clarification" — saves no
          longer flow through chat, and there is no remaining producer
          of GraphInterrupt to map onto clarification. The agent is the
          only dispatch path (ADR-065).
    message: Human-readable response text.
    data: Structured payload from downstream service; null for error;
          on the "agent" path carries
          `{"reasoning_steps": [<ReasoningStep.model_dump>, ...]}` —
          only user-visible steps survive the serialization filter.
    tool_calls_used: Count of tool invocations during this turn
                     (recall, consult). Read by NestJS to increment
                     the daily tool-call counter.
    """

    type: ChatResponseType
    message: str
    data: dict[str, Any] | None = None
    tool_calls_used: int = 0
