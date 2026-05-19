"""Pydantic schemas for taste model artifacts (ADR-077).

RawInteraction — minimal interaction row read from the DB (no place JOIN).
InteractionRow — places-vocabulary row, built in the service from a
    resolved PlaceCore + the per-user save source.
SummaryLine — grounded LLM output items.
TasteArtifacts — combined LLM output schema.
TasteProfile — read model returned by TasteModelService.get_taste_profile.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RawInteraction(BaseModel):
    """One interactions row, place data not yet resolved.

    The repository returns these (type + place_core_id only); the service
    resolves place_core_id against the places catalog and builds the
    richer InteractionRow. `place_core_id` is the `places.id` value
    stored in the `interactions.place_id` column (the column name is
    unchanged; only the field disambiguates it from user_place_id /
    provider_id).
    """

    type: str
    place_core_id: str | None = None


class InteractionRow(BaseModel):
    """places-vocabulary interaction row (ADR-077).

    Built by core/taste/mapping.place_to_interaction_row from a resolved
    PlaceCore plus the per-user save source. Typed tag dimensions mirror
    places TagType; `categories` are flat PlaceCategory values.
    """

    type: str
    categories: list[str] = Field(default_factory=list)
    cuisine: list[str] = Field(default_factory=list)
    dietary: list[str] = Field(default_factory=list)
    feature: list[str] = Field(default_factory=list)
    atmosphere: list[str] = Field(default_factory=list)
    service: list[str] = Field(default_factory=list)
    price: str | None = None  # last price tag wins (single-value semantics)
    accessibility: list[str] = Field(default_factory=list)
    time: list[str] = Field(default_factory=list)
    season: list[str] = Field(default_factory=list)
    neighborhood: str | None = None
    city: str | None = None
    country: str | None = None
    source: str | None = None  # UserPlace.source, save-only at aggregation


class SummaryLine(BaseModel):
    """One line of the taste_profile_summary — grounded in signal_counts."""

    text: str = Field(min_length=1, max_length=200)
    signal_count: int
    source_field: str
    source_value: str | None = None


class TasteArtifacts(BaseModel):
    """Combined LLM output: summary lines."""

    summary: list[SummaryLine] = Field(max_length=6)


class TasteProfile(BaseModel):
    """Read model returned by TasteModelService.get_taste_profile."""

    taste_profile_summary: list[SummaryLine] = Field(default_factory=list)
    signal_counts: dict[str, Any] = Field(default_factory=dict)
    generated_from_log_count: int = 0
