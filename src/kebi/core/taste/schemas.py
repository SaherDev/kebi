"""Pydantic schemas for taste model artifacts (ADR-058).

InteractionRow — typed shape for the interactions JOIN places query result.
SummaryLine / Chip — grounded LLM output items (share source_field/source_value).
TasteArtifacts — combined LLM output schema.
TasteProfile — read model returned by TasteModelService.get_taste_profile.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from kebi.core.places.models import PlaceAttributes

SignalTier = Literal["cold", "warming", "chip_selection", "active"]


class ChipStatus(str, Enum):
    """Lifecycle status of a taste chip (feature 023)."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


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


class Chip(BaseModel):
    """Short UI label grounded in signal_counts.

    status and selection_round are service-owned lifecycle fields added in
    feature 023. The regen LLM never emits them — defaults apply to legacy
    JSONB rows written before the feature shipped.

    query is LLM-emitted: a short natural-language message the user can tap
    to ask Kebi (e.g. "Best bars near me tonight"). Defaults to "" for
    legacy JSONB rows written before this field was added.
    """

    label: str = Field(min_length=1, max_length=30)
    source_field: str
    source_value: str
    signal_count: int
    query: str = Field(default="", max_length=120)
    status: ChipStatus = ChipStatus.PENDING
    selection_round: str | None = None


class TasteArtifacts(BaseModel):
    """Combined LLM output: summary lines + chips."""

    summary: list[SummaryLine] = Field(max_length=6)
    chips: list[Chip] = Field(max_length=8)


class TasteProfile(BaseModel):
    """Read model returned by TasteModelService.get_taste_profile."""

    taste_profile_summary: list[SummaryLine] = Field(default_factory=list)
    signal_counts: dict[str, Any] = Field(default_factory=dict)
    chips: list[Chip] = Field(default_factory=list)
    generated_from_log_count: int = 0


class ChipView(BaseModel):
    """User-facing chip shape returned by GET /v1/user/context (feature 023)."""

    label: str = Field(..., description="Short display label (e.g. 'Japanese')")
    source_field: str = Field(..., description="Field the chip was derived from")
    source_value: str = Field(..., description="Value of source_field")
    signal_count: int = Field(..., description="Number of signals for this chip")
    query: str = Field(
        default="",
        description="Natural-language message the user can tap to send (e.g. 'Best bars near me tonight').",  # noqa: E501
    )
    status: ChipStatus = Field(
        default=ChipStatus.PENDING,
        description="Lifecycle status: pending | confirmed | rejected",
    )
    selection_round: str | None = Field(
        default=None,
        description="Round name in which status was set, or null for pending chips.",
    )


class TasteContext(BaseModel):
    """Tier + chips derived from the taste model — the slice of
    GET /v1/user/context that TasteModelService owns. The route handler
    composes this with `saved_places_count` (from PlacesService) to build
    the final UserContext response.
    """

    signal_tier: SignalTier = Field(
        ...,
        description="Derived tier: cold | warming | chip_selection | active",
    )
    chips: list[ChipView] = Field(
        default_factory=list,
        description=(
            "Precomputed taste chips. Each chip's `selection_round` carries "
            "either the round the chip was decided in (confirmed/rejected) "
            "or — for still-pending chips — the round the user should submit "
            "the chip under (stamped server-side from the highest crossed "
            "stage). Null only at cold/warming tiers where no stage has been "
            "crossed yet."
        ),
    )


class UserContext(BaseModel):
    """Response shape for GET /v1/user/context (feature 023).

    Composed at the route: `saved_places_count` from PlacesService (places
    table) + `signal_tier` / `chips` from TasteModelService.get_taste_context.
    """

    saved_places_count: int = Field(
        ..., description="Total number of places the user has saved"
    )
    signal_tier: SignalTier = Field(
        ...,
        description="Derived tier: cold | warming | chip_selection | active",
    )
    chips: list[ChipView] = Field(
        default_factory=list,
        description=(
            "Precomputed taste chips. Each chip's `selection_round` carries "
            "either the round the chip was decided in (confirmed/rejected) "
            "or — for still-pending chips — the round the user should submit "
            "the chip under (stamped server-side from the highest crossed "
            "stage). Null only at cold/warming tiers where no stage has been "
            "crossed yet."
        ),
    )
