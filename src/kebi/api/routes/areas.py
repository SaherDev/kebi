"""GET /v1/areas/{area_id} — the area screen behind every area link (ADR-153).

Chat has minted `kebi://area/...` links since ADR-136, but an area tap had
nowhere to land: a venue link resolves to its catalog row, an area had no
row at all. This route is where the link lands. `area_id` is the encoded
geo key the shared codec minted into the URI; decoding it back is the whole
identity scheme — no lookup table, no new id space.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from kebi.api.deps import (
    GatewayIdentity,
    get_area_screen_service,
    get_event_dispatcher,
    require_gateway_identity,
)
from kebi.api.rate_limit import limiter
from kebi.api.schemas.areas import AreaScreenResponse
from kebi.core.areas import AreaScreenService, decode_area_id
from kebi.core.events.dispatcher import EventDispatcher
from kebi.core.events.events import AreaProfileRequested

router = APIRouter()


@router.get("/areas/{area_id}", response_model=AreaScreenResponse)
@limiter.limit("60/minute")
async def get_area(
    request: Request,
    area_id: str,
    identity: Annotated[GatewayIdentity, Depends(require_gateway_identity)],
    screen_service: AreaScreenService = Depends(get_area_screen_service),  # noqa: B008
    event_dispatcher: EventDispatcher = Depends(get_event_dispatcher),  # noqa: B008
) -> AreaScreenResponse:
    """One area as the client renders it, profiled or not.

    The global half (summary, chips, breadcrumb names) comes off the area's
    row; when no row exists yet this response returns the thin fallback and
    dispatches the profiler in the background so the next open is dressed —
    the same first-open contract as the place screen (ADR-152). The personal
    half (saved count, the "where you've saved" drill-down) is computed
    fresh for the verified caller on every open.

    404 when `area_id` does not decode to a canonical geo key — area links
    kebi minted always decode, so this means a stale or fabricated id.
    """
    try:
        geo_key = decode_area_id(area_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="area_not_found"
        ) from None
    screen = await screen_service.build_screen(geo_key, identity.user_id)
    if not screen.profiled:
        await event_dispatcher.dispatch(
            AreaProfileRequested(user_id=identity.user_id, geo_key=geo_key)
        )
    return AreaScreenResponse.from_screen(screen)
