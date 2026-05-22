"""Request and response schemas for POST /v1/chat endpoint."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from kebi.core.agent.location import MovementMode, Reach

ChatResponseType = Literal[
    "error",
    "agent",
]


class Location(BaseModel):
    """User's geographic location."""

    lat: float
    lng: float


class MovementProfile(BaseModel):
    """The user's profile-level mobility setting (ADR-084).

    Owned by the NestJS product repo's `user_settings` (Constitution VI — kebi
    owns no user-settings table) and sent on every `/v1/chat` request, like
    `location`. kebi consumes it; it is never stored here. It is the *default*
    the per-turn resolver starts from — request context can resolve a
    different effective mode/scope for any given turn without changing it.

    `available_modes` constrains *inferred* mode picks (the resolver will not
    infer `driving` for a user who lists only `[walking, transit]`); an
    explicit per-turn mode word can still override it. `reach` shifts the
    resolved scope tier ±1.
    """

    default_mode: MovementMode
    available_modes: list[MovementMode] = Field(min_length=1)
    reach: Reach = "normal"


class ChatRequest(BaseModel):
    """Request body for POST /v1/chat endpoint."""

    user_id: str
    message: str
    location: Location | None = None
    movement_profile: MovementProfile | None = None


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
