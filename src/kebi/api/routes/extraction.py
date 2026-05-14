"""Extraction routes.

`POST /v1/extract` — canonical product-facing extraction endpoint
(ADR-073). The product repo calls this directly to save a place. The
agent (`/v1/chat`) is conversation-only and does not write to
`user_places`.

`GET /v1/extraction/{request_id}` — reserved for future async use;
polls Redis status for a background extraction by request_id. No
product flow writes those keys today.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from kebi.api.deps import get_extraction_service, get_status_repo
from kebi.api.schemas.extract_place import (
    ExtractPlaceRequest,
    ExtractPlaceResponse,
)
from kebi.core.extraction.service import ExtractionService
from kebi.core.extraction.status_repository import ExtractionStatusRepository

router = APIRouter()


@router.post("/extract", status_code=200)
async def extract_place(
    body: ExtractPlaceRequest,
    service: ExtractionService = Depends(get_extraction_service),  # noqa: B008
) -> ExtractPlaceResponse:
    """Run the extraction pipeline and save the place(s) — canonical
    product-facing entry point (ADR-073).

    Synchronous: blocks until the pipeline completes. Latency profile —
    text inputs land in milliseconds; URL inputs that hit yt-dlp /
    Whisper / vision can take 30–60 seconds. The response carries the
    full per-candidate `evidence` trail so callers can see which
    producers contributed and which media (caption / transcript /
    frame / image / …) the extraction came from. Per ADR-071, every
    picker candidate is persisted to `user_places` with `approved=False`;
    the user curates after the fact.
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
