"""Request/response schemas for POST /v1/signal endpoint.

Carries accept/reject feedback on a prior recommendation
(feature 022, ADR-060): recommendation_id + place_core_id (ADR-077
disambiguates this from user_place_id / provider_id) — and, since
location-kinds Step 6, keeping an AREA that was put forward, which is a
taste signal with no row behind it.

`SignalRequest` is a discriminated union on `signal_type`, so each signal
declares exactly the fields it needs rather than sharing one shape with
half of it optional.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field


class RecommendationSignalRequest(BaseModel):
    """Accept/reject signal for a prior recommendation.

    `user_id` is intentionally absent — the caller's identity arrives via
    the gateway header `X-Gateway-User-Id` and is verified by
    `require_gateway_identity`. The route passes it explicitly to the
    signal service.
    """

    signal_type: Literal["recommendation_accepted", "recommendation_rejected"] = Field(
        ..., description="Type of behavioral signal"
    )
    recommendation_id: str = Field(
        ..., description="ID of the recommendation being responded to"
    )
    place_core_id: str = Field(
        ..., description="places.id of the place the user acted on"
    )


class AreaSignalRequest(BaseModel):
    """The user kept an area kebi put forward (location-kinds Step 6).

    An area save is a **signal, not a row**. Saving "An Thuong" or "Hai Van
    Pass" records interest in that geography and nothing else — no library
    entry, no venue row. That is what closes the hole where a mountain pass
    could be saved as if it were a restaurant: the pass is an area, and an
    area has no venue-shaped save to make.

    `entity_key` is the area's key from the answer's card, in the same
    `build_geo_key` format the knowledge layer uses (`vn`, `vn/hoi-an`,
    `vn/da-nang/an-thuong`). An unknown key is a no-op, not an error — the
    signal is trusted from the product repo like its recommendation siblings
    (ADR-078), but only areas kebi actually resolved can train anything.
    """

    signal_type: Literal["area_saved"] = Field(
        ..., description="Type of behavioral signal"
    )
    entity_key: str = Field(..., description="entity_key of the area the user kept")
    recommendation_id: str | None = Field(
        None, description="ID of the answer the area was surfaced in, when known"
    )


SignalRequest = Annotated[
    RecommendationSignalRequest | AreaSignalRequest,
    Field(discriminator="signal_type"),
]


class SignalResponse(BaseModel):
    """Response body for POST /v1/signal."""

    status: str = Field(
        "accepted", description="Signal accepted and queued for processing"
    )
