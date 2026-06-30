"""Extraction routes.

`POST /v1/extract` — canonical product-facing extraction endpoint
(ADR-073). The product repo calls this directly to save a place. The
agent (`/v1/chat`) is conversation-only and does not write to
`user_places`.

Note: no `from __future__ import annotations` here — slowapi's
`@limiter.limit` + FastAPI's per-body type-adapter rebuild fails to
resolve forward refs through the wrapper.
"""

from fastapi import APIRouter, Body, Depends, Request

from kebi.api.deps import (
    GatewayIdentity,
    get_extraction_service,
    require_gateway_identity,
)
from kebi.api.rate_limit import limiter
from kebi.api.schemas.extract_place import (
    ExtractPlaceRequest,
    ExtractPlaceResponse,
)
from kebi.core.extraction.service import ExtractionService

router = APIRouter()


@router.post("/extract", status_code=200)
@limiter.limit("10/minute")
async def extract_place(
    request: Request,
    body: ExtractPlaceRequest = Body(...),  # noqa: B008
    identity: GatewayIdentity = Depends(require_gateway_identity),  # noqa: B008
    service: ExtractionService = Depends(get_extraction_service),  # noqa: B008
) -> ExtractPlaceResponse:
    """Run the extraction pipeline and save the place(s) — canonical
    product-facing entry point (ADR-073).

    Synchronous: blocks until the pipeline completes. Latency profile —
    text inputs land in milliseconds; URL inputs that hit yt-dlp /
    Whisper / vision can take 30–60 seconds. Per ADR-071, every picker
    candidate is persisted to `user_places` with `approved=False`; the
    user curates after the fact. Per ADR-093, the per-candidate
    evidence audit trail writes to an object-storage ledger and is no
    longer returned in the response.

    `user_id` is resolved from the verified gateway identity — not a
    body field — so a caller cannot link a place to someone else's
    account.

    The caller's plan-tier `save_limit` (forwarded by the gateway) is
    enforced before the pipeline runs: a user with a full library gets a
    terminal `status="failed"`, `failure_reason="save_limit_reached"`
    response and spends no extraction quota.
    """
    return await service.run(
        raw_input=body.raw_input,
        user_id=identity.user_id,
        save_limit=identity.save_limit,
    )
