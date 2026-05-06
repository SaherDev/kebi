"""Extraction routes.

`POST /v1/extract` — direct extraction endpoint that bypasses the agent
(ADR-052 routes everything through `/v1/chat`; this is a debug/internal
hook for testing the pipeline in isolation, with the full Evidence
trail surfaced on every result item).

`GET /v1/extraction/{request_id}` — poll Redis status for a background
extraction by request_id.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from totoro_ai.api.deps import get_extraction_service, get_status_repo
from totoro_ai.api.schemas.extract_place import (
    ExtractPlaceRequest,
    ExtractPlaceResponse,
)
from totoro_ai.core.extraction.service import ExtractionService
from totoro_ai.core.extraction.status_repository import ExtractionStatusRepository

router = APIRouter()


@router.post("/extract", status_code=200)
async def extract_place(
    body: ExtractPlaceRequest,
    service: ExtractionService = Depends(get_extraction_service),  # noqa: B008
) -> ExtractPlaceResponse:
    """Run the extraction pipeline directly, bypassing the agent.

    Useful for debugging the cascade in isolation — the response carries
    the full per-candidate `evidence` trail so callers can see exactly
    which producers contributed and which media (caption / transcript /
    frame / image / …) the extraction came from.

    Production traffic still goes through `POST /v1/chat`; this route
    is for iteration on the extraction layer itself.
    """
    return await service.run(raw_input=body.raw_input, user_id=body.user_id)


@router.get("/extraction/{request_id}", status_code=200)
async def get_extraction_status(
    request_id: str,
    status_repo: ExtractionStatusRepository = Depends(get_status_repo),  # noqa: B008
) -> ExtractPlaceResponse:
    """Return the result of a background extraction keyed by request_id.

    Returns 404 if the key is not in Redis yet (still running or expired).
    """
    payload = await status_repo.read(request_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Extraction result not found")
    return ExtractPlaceResponse(**payload)
