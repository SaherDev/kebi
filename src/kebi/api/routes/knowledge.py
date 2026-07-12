"""Knowledge curation route (/v1/knowledge/curate) — ADR-121.

Lets a trusted expert push world knowledge into the claims store as
`curated_expert`. Global writes, so the capability fails closed: the gateway
must assert `X-Gateway-Can-Curate`, or the request is forbidden.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from kebi.api.deps import (
    GatewayIdentity,
    get_knowledge_curation_service,
    require_gateway_identity,
)
from kebi.api.rate_limit import limiter
from kebi.api.schemas.knowledge import CurateRequest, CurateResponse
from kebi.core.knowledge.curation_service import KnowledgeCurationService

router = APIRouter()


@router.post("/knowledge/curate", response_model=CurateResponse)
@limiter.limit("30/minute")
async def curate_knowledge(
    request: Request,
    body: CurateRequest,
    identity: Annotated[GatewayIdentity, Depends(require_gateway_identity)],
    service: KnowledgeCurationService = Depends(  # noqa: B008
        get_knowledge_curation_service
    ),
) -> CurateResponse:
    """Structure an expert's prose into geo-scoped claims and store them.

    Gated on the `can_curate` capability (gateway-asserted, fail-closed):
    curated claims are global, so an ordinary caller must not reach this. The
    claims land as `curated_expert` at a high confidence floor, keyed
    identically to harvested claims so the two layers merge on the same
    entity. `user_id` is recorded only as provenance — never as a scope.
    """
    if not identity.can_curate:
        raise HTTPException(status_code=403, detail="curation_not_permitted")
    written = await service.curate(
        text=body.text,
        hint=body.location_hint,
        user_id=identity.user_id,
    )
    return CurateResponse.from_written(written)
