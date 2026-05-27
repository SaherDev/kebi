"""POST /v1/signal — behavioral signal endpoint (replaces /v1/feedback).

Accepts recommendation_accepted / recommendation_rejected (feature 022,
ADR-060).

Route is a thin facade (ADR-034) — all dispatch lives in SignalService.
"""

from fastapi import APIRouter, Body, Depends, Request, status

from kebi.api.deps import (
    GatewayIdentity,
    get_signal_service,
    require_gateway_identity,
)
from kebi.api.rate_limit import limiter
from kebi.api.schemas.signal import (
    SignalRequest,
    SignalResponse,
)
from kebi.core.signal.service import SignalService

router = APIRouter()


@router.post(
    "/signal", response_model=SignalResponse, status_code=status.HTTP_202_ACCEPTED
)
@limiter.limit("60/minute")
async def post_signal(
    request: Request,
    body: SignalRequest = Body(...),  # noqa: B008
    identity: GatewayIdentity = Depends(require_gateway_identity),  # noqa: B008
    signal_service: SignalService = Depends(get_signal_service),  # noqa: B008
) -> SignalResponse:
    """Handle a recommendation accept/reject signal.

    Pydantic rejects unknown `signal_type` values with 422 automatically.
    The route dispatches to SignalService with the request's fields. The
    recommendation_id is trusted, not DB-validated (ADR-078).

    `user_id` is resolved from the verified gateway identity — never the
    request body — so a caller cannot poison another user's taste model.
    """
    await signal_service.handle_signal(
        signal_type=body.signal_type,
        user_id=identity.user_id,
        recommendation_id=body.recommendation_id,
        place_core_id=body.place_core_id,
    )
    return SignalResponse()
