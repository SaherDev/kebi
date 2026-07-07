"""Schemas for the home greeting + chips (ADR-111).

`HomeContext` is the resolved input the service generates from; `HomeSuggestion`
(with `HomeChip`) is both the Instructor response model the LLM fills and the
cached/returned payload. These are internal domain models — the API projects
them to its own response DTO (ADR-105).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class HomeContext(BaseModel):
    """The local context the client supplies for greeting generation.

    All optional — the service degrades gracefully (a missing field becomes a
    neutral block in the prompt). The server may turn `lat`/`lng` into a city
    name but never originates location; `local_time` drives the daypart since
    only the client knows the user's timezone (ADR-111).
    """

    lat: float | None = None
    lng: float | None = None
    city: str | None = None
    local_time: datetime | None = None
    weather: str | None = None


class HomeChip(BaseModel):
    """One suggestion chip — displayed and, on tap, re-submitted to /v1/chat."""

    text: str = Field(min_length=1, max_length=40)


class HomeSuggestion(BaseModel):
    """The generated greeting + chips. Doubles as the Instructor response model.

    The chip-count envelope here is intentionally loose; the configured
    `chip_min`/`chip_max` are enforced by the prompt and trimmed by the
    service, so a config change doesn't require editing this schema.
    """

    greeting: str = Field(min_length=1, max_length=80)
    chips: list[HomeChip] = Field(min_length=1, max_length=8)
