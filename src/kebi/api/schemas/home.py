"""Request/response schemas for GET /v1/home (ADR-111).

The home screen's greeting + suggestion chips. The client supplies the local
context it natively holds (coordinates, local time, an optional coarse weather
hint); the server generates a greeting and a few tappable chips. Each chip's
`text` is re-submitted to POST /v1/chat on tap.

`user_id` is intentionally absent (ADR-105): the caller's identity arrives via
the gateway header and is verified by `require_gateway_identity`. The response
is an explicit projection of the internal `HomeSuggestion`, never the raw model.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from kebi.core.home import HomeContext, HomeSuggestion


class HomeQuery(BaseModel):
    """Query params for the home greeting endpoint — all optional.

    The server may turn `lat`/`lng` into a city name but never originates
    location; `local_time` drives the daypart (only the client knows the
    user's timezone); `weather` is a coarse free-text hint (e.g. "clear",
    "rain"), folded into a small band server-side. `extra="forbid"` rejects
    unknown params with a 422.
    """

    model_config = ConfigDict(extra="forbid")

    lat: float | None = Field(None, ge=-90, le=90)
    lng: float | None = Field(None, ge=-180, le=180)
    city: str | None = Field(
        None, description="Client-supplied city name; skips reverse-geocode."
    )
    local_time: datetime | None = Field(
        None, description="Device local time (ISO-8601); drives the daypart."
    )
    weather: str | None = Field(
        None, description="Coarse weather hint, e.g. 'clear' / 'rain'."
    )

    def to_context(self) -> HomeContext:
        return HomeContext(
            lat=self.lat,
            lng=self.lng,
            city=self.city,
            local_time=self.local_time,
            weather=self.weather,
        )


class HomeChip(BaseModel):
    """One suggestion chip — `text` is both displayed and re-submitted to chat."""

    text: str


class HomeResponse(BaseModel):
    """The home screen's greeting + chips."""

    greeting: str
    chips: list[HomeChip] = Field(default_factory=list)

    @classmethod
    def from_suggestion(cls, suggestion: HomeSuggestion) -> HomeResponse:
        return cls(
            greeting=suggestion.greeting,
            chips=[HomeChip(text=c.text) for c in suggestion.chips],
        )
