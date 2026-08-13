"""Knowledge curation routes (/v1/knowledge/*) — ADR-121.

Lets a trusted expert push world knowledge into the claims store as
`curated_expert`, browse what they've added, and retract one claim. All of
it is global writes/reads, so the capability fails closed: the gateway must
assert `X-Gateway-Can-Curate`, or the request is forbidden — including
list/delete, so a revoked expert loses self-management along with writing.
Ownership inside the gate is the claim's `source_ref`, which names its
author; another curator's claims are indistinguishable from absent (404).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from kebi.api.deps import (
    GatewayIdentity,
    get_curator_claims_service,
    get_entity_search_service,
    get_knowledge_curation_service,
    require_gateway_identity,
)
from kebi.api.rate_limit import limiter
from kebi.api.schemas.knowledge import (
    CurateRequest,
    CurateResponse,
    EntitySearchQuery,
    EntitySearchResponse,
    MyClaimsQuery,
    MyClaimsResponse,
)
from kebi.core.areas.keys import decode_area_id
from kebi.core.knowledge.curation_service import (
    AnchorNotFoundError,
    CuratorClaimsService,
    KnowledgeCurationService,
)
from kebi.core.knowledge.entity_search_service import EntitySearchService

router = APIRouter()


def _require_curator(identity: GatewayIdentity) -> None:
    if not identity.can_curate:
        raise HTTPException(status_code=403, detail="curation_not_permitted")


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
    """Structure an expert's prose into claims and store them.

    Gated on the `can_curate` capability (gateway-asserted, fail-closed):
    curated claims are global, so an ordinary caller must not reach this. The
    claims land as `curated_expert` at a high confidence floor, keyed
    identically to harvested claims so the two layers merge on the same
    entity. `user_id` is recorded only as provenance — never as a scope.

    An anchor that doesn't resolve is a 404 before any LLM runs: an unknown
    `place_id`, or an `area_id` token this codec never minted. A valid but
    unprofiled area is NOT a 404 — area rows are lazy (ADR-153).
    """
    _require_curator(identity)

    anchor_place_id: str | None = None
    anchor_geo_key: str | None = None
    if body.anchor is not None:
        anchor_place_id = body.anchor.place_id
        if body.anchor.area_id is not None:
            try:
                anchor_geo_key = decode_area_id(body.anchor.area_id)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="anchor_not_found",
                ) from None

    try:
        written = await service.curate(
            text=body.text,
            user_id=identity.user_id,
            anchor_place_id=anchor_place_id,
            anchor_geo_key=anchor_geo_key,
        )
    except AnchorNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="anchor_not_found"
        ) from None
    return CurateResponse.from_written(written)


@router.get("/knowledge/claims", response_model=MyClaimsResponse)
@limiter.limit("60/minute")
async def list_my_claims(
    request: Request,
    identity: Annotated[GatewayIdentity, Depends(require_gateway_identity)],
    params: Annotated[MyClaimsQuery, Query()],
    service: CuratorClaimsService = Depends(get_curator_claims_service),  # noqa: B008
) -> MyClaimsResponse:
    """One newest-first page of the caller's own curated claims.

    Each claim carries a renderable, openable anchor: a place claim the
    catalog id venue links use, a geo claim the encoded area token area
    links use. `created_at` is a raw ISO-8601 instant; the client owns
    relative phrasing. This list — not the curate response — is the source
    of truth for "what you've added": a resubmission that deduped away
    still shows here, under its original id. A malformed `cursor` surfaces
    as a 400 via the shared `ValueError` handler.
    """
    _require_curator(identity)
    records, next_cursor = await service.list_mine(
        identity.user_id, params.limit, params.cursor
    )
    return MyClaimsResponse.from_page(records, next_cursor)


@router.delete("/knowledge/claims/{claim_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("60/minute")
async def retract_claim(
    request: Request,
    claim_id: str,
    identity: Annotated[GatewayIdentity, Depends(require_gateway_identity)],
    service: CuratorClaimsService = Depends(get_curator_claims_service),  # noqa: B008
) -> Response:
    """Retract one of the caller's own curated claims.

    Author-only: the claim's `source_ref` must name the caller. 404 covers
    both a claim that doesn't exist and one that isn't the caller's — the
    two are deliberately indistinguishable, so ids can't be probed.
    """
    _require_curator(identity)
    deleted = await service.retract(identity.user_id, claim_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="claim_not_found"
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/knowledge/entities", response_model=EntitySearchResponse)
@limiter.limit("120/minute")
async def search_entities(
    request: Request,
    identity: Annotated[GatewayIdentity, Depends(require_gateway_identity)],
    params: Annotated[EntitySearchQuery, Query()],
    service: EntitySearchService = Depends(get_entity_search_service),  # noqa: B008
) -> EntitySearchResponse:
    """Typeahead for the curation anchor chip — places and areas, one list.

    Deterministic (no LLM): catalog places via unscoped hybrid search, areas
    from the profiled `areas` table, and — only when the known corpus has no
    area hit — a verified-or-refuse geocode so a never-opened area can still
    be anchored ("Name, Country" pins a city; a bare name only a country).
    Every returned id is a valid curate anchor as-is, and every `area_id`
    opens on `GET /v1/areas/{id}` even before a row exists (lazy profiling,
    ADR-153). No matches is `{"results": []}`, never an error.
    """
    _require_curator(identity)
    results = await service.search(params.q, params.limit)
    return EntitySearchResponse.from_results(results)
