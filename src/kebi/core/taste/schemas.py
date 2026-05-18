"""Pydantic schemas for taste model artifacts (ADR-058).

InteractionRow — typed shape for the interactions JOIN places query result.
SummaryLine — grounded LLM output items.
TasteArtifacts — combined LLM output schema.
TasteProfile — read model returned by TasteModelService.get_taste_profile.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from kebi.core.places.models import PlaceAttributes


class InteractionRow(BaseModel):
    """Typed shape for the interactions JOIN places query result.

    Fields mirror PlaceObject Tier 1 columns. Repository hydrates
    this from the JSONB column via PlaceAttributes(**row.attributes).
    """

    type: str
    place_type: str
    subcategory: str | None = None
    source: str | None = None
    tags: list[str] = Field(default_factory=list)
    attributes: PlaceAttributes = Field(default_factory=PlaceAttributes)


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
