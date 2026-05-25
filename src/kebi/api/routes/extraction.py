"""Extraction routes.

`POST /v1/extract` — canonical product-facing extraction endpoint
(ADR-073). The product repo calls this directly to save a place. The
agent (`/v1/chat`) is conversation-only and does not write to
`user_places`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from kebi.api.deps import get_extraction_service
from kebi.api.schemas.extract_place import (
    ExtractPlaceRequest,
    ExtractPlaceResponse,
)
from kebi.core.extraction.service import ExtractionService

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
    Whisper / vision can take 30–60 seconds. Per ADR-071, every picker
    candidate is persisted to `user_places` with `approved=False`; the
    user curates after the fact. Per ADR-093, the per-candidate
    evidence audit trail writes to an object-storage ledger and is no
    longer returned in the response.
    """
    return await service.run(raw_input=body.raw_input, user_id=body.user_id)
