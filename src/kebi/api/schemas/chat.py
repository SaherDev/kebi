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
    """Request body for POST /v1/chat endpoint.

    `user_id` is intentionally absent — the caller's identity is verified
    by the gateway and forwarded as `X-Gateway-User-Id`; routes resolve
    it via `require_gateway_identity` and pass it explicitly to the
    service layer (see ADR for gateway auth).

    `message` is length-capped to bound LLM token cost on adversarial
    inputs and keep the per-user memory buffer from ballooning. 4000
    chars covers typical conversational and voice-transcribed inputs
    with headroom; longer payloads are rejected with 422.
    """

    message: str = Field(min_length=1, max_length=4000)
    location: Location | None = None
    movement_profile: MovementProfile | None = None
    # The caller's local wall-clock time, ISO-8601 (e.g.
    # "2026-08-06T21:40:00+08:00"). Client-supplied for the same reason
    # `location` is: only the device knows the user's actual clock, and a
    # server clock in another timezone would confidently answer for the
    # wrong day. Day of week is load-bearing — "tonight is Luigi's night"
    # requires knowing it is Monday (ADR-138). Omitted, the agent reasons
    # without a schedule rather than guessing one.
    local_time: str | None = Field(default=None, max_length=40)


class ChatResponse(BaseModel):
    """Response body for POST /v1/chat endpoint.

    type: One of "agent", "error". The agent is the only dispatch path
          (ADR-065); "consult" / "recall" / "extract-place" /
          "clarification" types were removed by ADR-073/075.
    message: Human-readable response text. On the "agent" path the entity
          names in it are already wrapped as markdown links to
          `kebi://{kind}/{key}` URIs (ADR-136) — chat renders text plus
          taps, and nothing else.
    data: Structured payload; null for error. On the "agent" path:
          - `reasoning_steps`: list of user-visible `ReasoningStep`
            dumps (debug-visibility steps are filtered out).
          - `entities`: list of `ChatEntity` dumps — one per link in the
            message, resolving its `kind` / `key` / `uri` / canonical
            `name` so the client's link handler never parses the URI.
            Per-tool payloads are deliberately absent: a new tool changes
            what the agent says, never what the client draws.
    tool_calls_used: Number of tool calls the agent made this turn
                     (find_saved + suggest_places), surfaced for
                     rate-limit accounting on the NestJS side.
    """

    type: ChatResponseType
    message: str
    data: dict[str, Any] | None = None
    tool_calls_used: int = 0
