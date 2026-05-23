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
    """The user's mobility capability (ADR-085, amending ADR-084).

    Owned by the NestJS product repo's `user_settings` (Constitution VI — kebi
    owns no user-settings table) and sent on every `/v1/chat` request, like
    `location`. kebi consumes it; it is never stored here.

    `available_modes` is the set of modes the user has access to and is
    willing to use — driving requires a licence, motorbike requires comfort
    and (often) a licence, cycling requires owning a bike. This is a stable
    per-user capability, NOT a per-city availability list.

    The resolver pairs this capability with the working location's city and
    density each turn to pick an `effective_mode`. A user capable of
    `[walking, driving, transit]` will likely resolve to `transit` in
    Manhattan and to `driving` in Koh Samui — same capability, different
    city. An explicit per-turn mode word in the message still overrides the
    inference, even if the named mode is not in `available_modes` (the user
    knows their situation this turn — e.g., a rental).

    `reach` shifts the resolved scope tier ±1 (compact / normal / far).
    """

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

    type: One of "agent", "error". The agent is the only dispatch path
          (ADR-065); "consult" / "recall" / "extract-place" /
          "clarification" types were removed by ADR-073/075.
    message: Human-readable response text.
    data: Structured payload; null for error. On the "agent" path:
          - `reasoning_steps`: list of user-visible `ReasoningStep`
            dumps (debug-visibility steps are filtered out).
          - `tool_results`: list of `{tool, tool_call_id, payload}`
            entries — one per tool call this turn. `payload` is the
            parsed `ConsultResult` (`candidates` with place + `source`
            + optional `reason` + `empty_reason`) so clients can render
            the structured list without re-parsing the prose.
    tool_calls_used: Number of tool calls the agent made this turn
                     (find_saved + suggest_places), surfaced for
                     rate-limit accounting on the NestJS side.
    """

    type: ChatResponseType
    message: str
    data: dict[str, Any] | None = None
    tool_calls_used: int = 0
