"""POST /v1/signal — behavioral signal endpoint (replaces /v1/feedback).

Accepts a discriminated union on `signal_type`:
- recommendation_accepted / recommendation_rejected (feature 022, ADR-060)
- chip_confirm (feature 023)

Route is a thin facade (ADR-034) — all dispatch lives in SignalService.
"""

from fastapi import APIRouter, Body, Depends, HTTPException, status

from kebi.api.deps import get_signal_service
from kebi.api.schemas.signal import (
    ChipConfirmSignalRequest,
    RecommendationSignalRequest,
    SignalRequest,
    SignalResponse,
)
from kebi.core.signal.service import RecommendationNotFoundError, SignalService

router = APIRouter()


@router.post(
    "/signal", response_model=SignalResponse, status_code=status.HTTP_202_ACCEPTED
)
async def post_signal(
    request: SignalRequest = Body(..., discriminator="signal_type"),  # noqa: B008
    signal_service: SignalService = Depends(get_signal_service),  # noqa: B008
) -> SignalResponse:
    """Handle behavioral signals — recommendation accept/reject or chip_confirm.

    Pydantic handles discriminator-based variant dispatch; unknown values
    produce 422 automatically. The route dispatches to SignalService with
    the variant's load-bearing fields.
    """
    try:
        if isinstance(request, ChipConfirmSignalRequest):
            await signal_service.handle_signal(
                signal_type=request.signal_type,
                user_id=request.user_id,
                chip_metadata=request.metadata,
            )
        elif isinstance(request, RecommendationSignalRequest):
            await signal_service.handle_signal(
                signal_type=request.signal_type,
                user_id=request.user_id,
                recommendation_id=request.recommendation_id,
                place_id=request.place_id,
            )
    except RecommendationNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Recommendation not found",
        ) from None

    return SignalResponse()
