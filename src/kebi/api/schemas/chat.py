"""Request and response schemas for POST /v1/chat endpoint."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

SignalTierHint = Literal["cold", "warming", "chip_selection", "active"]

ChatResponseType = Literal[
    "error",
    "agent",
]


class Location(BaseModel):
    """User's geographic location."""

    lat: float
    lng: float


class ChatRequest(BaseModel):
    """Request body for POST /v1/chat endpoint."""

    user_id: str
    message: str
    location: Location | None = None
    signal_tier: SignalTierHint | None = Field(
        default=None,
        description=(
            "Optional tier hint from the product repo (feature 023). Product "
            "reads GET /v1/user/context and forwards the tier. Retained for "
            "request-shape stability; no longer drives behavior now that the "
            "agent has no tools."
        ),
    )


class ChatResponse(BaseModel):
    """Response body for POST /v1/chat endpoint.

    type: One of "agent", "error". ADR-075 removed the recall and
          consult tools — the agent is now a zero-tool conversational
          Q&A surface, so "consult" and "recall" response types no
          longer exist (ADR-073 had already removed "extract-place" and
          "clarification"). The agent is the only dispatch path (ADR-065).
    message: Human-readable response text.
    data: Structured payload; null for error; on the "agent" path
          carries `{"reasoning_steps": [<ReasoningStep.model_dump>, ...]}`
          — only user-visible steps survive the serialization filter.
    tool_calls_used: Always 0 — the agent has no tools (ADR-075).
                     Retained for response-shape stability.
    """

    type: ChatResponseType
    message: str
    data: dict[str, Any] | None = None
    tool_calls_used: int = 0
