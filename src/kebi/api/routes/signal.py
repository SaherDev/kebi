"""POST /v1/signal — behavioral signal endpoint (replaces /v1/feedback).

Accepts recommendation_accepted / recommendation_rejected (feature 022,
ADR-060).

Route is a thin facade (ADR-034) — all dispatch lives in SignalService.
"""

from fastapi import APIRouter, Depends, status

from kebi.api.deps import get_signal_service
from kebi.api.schemas.signal import (
    SignalRequest,
    SignalResponse,
)
from kebi.core.signal.service import SignalService

router = APIRouter()


@router.post(
    "/signal", response_model=SignalResponse, status_code=status.HTTP_202_ACCEPTED
)
async def post_signal(
    request: SignalRequest,
    signal_service: SignalService = Depends(get_signal_service),  # noqa: B008
) -> SignalResponse:
    """Handle a recommendation accept/reject signal.

    Pydantic rejects unknown `signal_type` values with 422 automatically.
    The route dispatches to SignalService with the request's fields. The
    recommendation_id is trusted, not DB-validated (ADR-078).
    """
    await signal_service.handle_signal(
        signal_type=request.signal_type,
        user_id=request.user_id,
        recommendation_id=request.recommendation_id,
        place_core_id=request.place_core_id,
    )
    return SignalResponse()
