"""Request/response schemas for POST /v1/signal endpoint.

Carries accept/reject feedback on a prior recommendation
(feature 022, ADR-060): recommendation_id + place_id.
"""

from __future__ import annotations

from typing import Literal

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


SignalRequest = RecommendationSignalRequest


class SignalResponse(BaseModel):
    """Response body for POST /v1/signal."""

    status: str = Field(
        "accepted", description="Signal accepted and queued for processing"
    )
