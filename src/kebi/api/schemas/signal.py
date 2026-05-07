"""Request/response schemas for POST /v1/signal endpoint.

Discriminated union on `signal_type`:
- "recommendation_accepted" / "recommendation_rejected" — original shape
  (feature 022, ADR-060). Carries recommendation_id + place_id.
- "chip_confirm" — feature 023. Carries the user-submitted chip statuses;
  each chip carries its own `selection_round`, which the frontend copies
  verbatim from the value returned by `GET /v1/user/context`.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field


class RecommendationSignalRequest(BaseModel):
    """Accept/reject signal for a prior recommendation."""

    signal_type: Literal["recommendation_accepted", "recommendation_rejected"] = Field(
        ..., description="Type of behavioral signal"
    )
    user_id: str = Field(..., description="User identifier (from Clerk auth)")
    recommendation_id: str = Field(
        ..., description="ID of the recommendation being responded to"
    )
    place_id: str = Field(..., description="The place the user acted on")


class ChipConfirmChipItem(BaseModel):
    """One chip in a chip_confirm submission.

    Mirrors the shape the frontend receives from `GET /v1/user/context` —
    the frontend just echoes each chip back with an updated `status`. The
    `selection_round` field is the only round carrier; there is no outer
    `round` wrapper.
    """

    label: str
    signal_count: int = Field(..., ge=0)
    source_field: str
    source_value: str
    status: Literal["confirmed", "rejected"] = Field(
        ...,
        description=(
            "User-chosen status. 'pending' is not a valid submission — users "
            "submit a decision."
        ),
    )
    selection_round: str = Field(
        ...,
        min_length=1,
        description=(
            "Stage name the chip is anchored to — copied verbatim from the "
            "value returned by /v1/user/context on the same chip."
        ),
    )


class ChipConfirmMetadata(BaseModel):
    """Payload for a chip_confirm signal (feature 023)."""

    chips: list[ChipConfirmChipItem] = Field(..., min_length=1)


class ChipConfirmSignalRequest(BaseModel):
    """Explicit chip confirm/reject submission (feature 023)."""

    signal_type: Literal["chip_confirm"] = Field(
        ..., description="Discriminator value for chip-confirm submissions"
    )
    user_id: str = Field(..., description="User identifier (from Clerk auth)")
    metadata: ChipConfirmMetadata = Field(..., description="User-chosen chip statuses")


SignalRequest = Annotated[
    RecommendationSignalRequest | ChipConfirmSignalRequest,
    Field(discriminator="signal_type"),
]


class SignalResponse(BaseModel):
    """Response body for POST /v1/signal."""

    status: str = Field(
        "accepted", description="Signal accepted and queued for processing"
    )
