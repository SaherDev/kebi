"""Home screen route (/v1/home): context-aware greeting + suggestion chips."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from kebi.api.deps import (
    GatewayIdentity,
    get_home_service,
    require_gateway_identity,
)
from kebi.api.rate_limit import limiter
from kebi.api.schemas.home import HomeQuery, HomeResponse
from kebi.core.home import HomeService

router = APIRouter()


@router.get("/home", response_model=HomeResponse)
@limiter.limit("30/minute")
async def get_home(
    request: Request,
    identity: Annotated[GatewayIdentity, Depends(require_gateway_identity)],
    params: Annotated[HomeQuery, Query()],
    service: HomeService = Depends(get_home_service),  # noqa: B008
) -> HomeResponse:
    """The home screen's greeting + suggestion chips.

    Generates a short, context-aware greeting ("it's late, drunk food?") and a
    few tappable chips ("ramen, no line", "surprise me") from the caller's
    taste signal plus the client-supplied local context (location, local time,
    optional weather). Each chip's `text` is re-submitted to POST /v1/chat when
    tapped — a chip is a pre-written first message, not a separate action.

    The payload is Redis-cached per coarse context bucket, so most opens are a
    cache hit; a taste regeneration refreshes it. The path fails open: any
    geocoding, cache, or model error returns a neutral greeting + generic chips
    so the screen always renders (ADR-111).

    `user_id` comes only from the verified gateway identity (ADR-105); the
    server never originates location — `lat`/`lng` may be turned into a city
    name, nothing more.
    """
    suggestion = await service.generate(identity.user_id, params.to_context())
    return HomeResponse.from_suggestion(suggestion)
