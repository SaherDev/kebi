"""Request and response schemas for POST /v1/chat endpoint."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

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

    `source` says whether a human ever chose these modes (ADR-155). It
    defaults to `default` because that is the truth for every user today:
    the product repo seeds a new settings row with a neutral capability and
    injects it on every turn, and there is no screen yet on which a user
    could say otherwise. Without the flag a seeded guess is indistinguishable
    on the wire from a deliberate answer, and kebi believed the guess — it
    asserted walking range to people who drive and never asked, because a
    profile being present *was* the "resolved" signal. Clients that add a
    settings screen send `source: "user"` for the rows a user actually
    touched; until then, the honest default is the one that makes kebi ask.
    """

    available_modes: list[MovementMode] = Field(min_length=1)
    reach: Reach = "normal"
    source: Literal["user", "default"] = "default"


class UserProfile(BaseModel):
    """What the user said about themselves — the "about me" block (ADR-154).

    Owned by the NestJS product repo's `user_settings` and sent on every
    `/v1/chat` request, like `location` and `movement_profile`. kebi consumes
    it; it is never stored here (Constitution VI).

    Every field is optional: a user who filled in nothing is a user with no
    profile, not an error. The three fields are deliberately the ones kebi
    cannot learn on its own — a name is never inferable, a passport country
    never shows up in saved places, and a brand-new user has no behavior to
    read. Anything kebi *can* learn (how far someone will travel, what
    cuisines they lean toward) is left to the taste model, because a
    self-reported answer there is the user's aspirational self and would
    compete with observed behavior.

    `about` is free prose and therefore untrusted input: it reaches the model
    inside a `trust="low"` block, never as instruction. It is a soft prior
    that observed behavior overrides — with one exception the renderer makes
    explicit, since a stated dietary or religious restriction is a constraint
    rather than a preference.
    """

    # Not the user's legal name — what they want to be called. Short cap: a
    # display name, never a paragraph smuggled through a small field.
    call_me: str | None = Field(default=None, max_length=40)
    # ISO 3166-1 alpha-2. A code rather than free text because this is the
    # side of an entry-rule question that must be exact ("UAE" / "Emirates" /
    # "United Arab Emirates" are one country and three strings).
    home_country: str | None = Field(default=None, pattern=r"^[A-Za-z]{2}$")
    # One short paragraph in the user's own words. Capped because it is
    # per-turn prompt weight on every single request, and because a long
    # self-description is a worse prior than a short one, not a better one.
    about: str | None = Field(default=None, max_length=300)

    @field_validator("home_country")
    @classmethod
    def _upper_country(cls, v: str | None) -> str | None:
        """Normalize the country code so `ae` and `AE` are one value."""
        return v.upper() if v else v

    @field_validator("call_me", "about")
    @classmethod
    def _blank_to_none(cls, v: str | None) -> str | None:
        """Treat a whitespace-only field as absent.

        A cleared text input arrives as `""` from the client, and an empty
        string rendered into the prompt reads as "they told us something"
        when they told us nothing.
        """
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None


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
    # The user's "about me" block (ADR-154). Client-supplied for the same
    # reason `movement_profile` is: it lives in the product repo's
    # `user_settings` and kebi owns no user-settings table. Omitted, the
    # agent simply knows nothing about the person beyond their behavior.
    user_profile: UserProfile | None = None
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
