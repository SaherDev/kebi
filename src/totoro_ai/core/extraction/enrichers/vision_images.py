"""Level 2 — VisionImagesEnricher: extract places from photo-post images."""

from __future__ import annotations

import asyncio
import logging

import httpx

from totoro_ai.core.extraction.source_filtered_enricher import SourceFilteredEnricher
from totoro_ai.core.extraction.types import (
    ExtractionContext,
    KnownPlace,
    Medium,
    Producer,
)
from totoro_ai.core.places import PlaceSource
from totoro_ai.providers.llm import VisionExtractorProtocol

logger = logging.getLogger(__name__)

_PER_REQUEST_TIMEOUT_SECONDS = 5.0
_TOTAL_TIMEOUT_SECONDS = 15.0
_MAX_IMAGES = 10


class VisionImagesEnricher(SourceFilteredEnricher):
    """Downloads photo-post images and extracts place names via vision LLM.

    Sibling to `VisionFramesEnricher`. Photo posts are simpler than
    videos: `PhotoDetectorEnricher` already extracted signed CDN URLs
    via yt-dlp metadata; we just `httpx.get` each one in parallel and
    feed the bytes to `VisionExtractorProtocol`. We don't pipe through
    `yt-dlp -o -` here because for photo-mode posts yt-dlp would
    download the audio track (the only "format") rather than the
    image — a different code path from frames-mode video extraction.
    The signed query-string auth on TikTok/Instagram photo URLs makes
    direct httpx GETs work without cookies.

    Names-only producer: appends each extracted name to
    `context.known_places` as a `KnownPlace(VISION_IMAGES, IMAGE)`
    entry. The deep-level finalizer (`LLMNEREnricher`) reads
    known_places and emits one structured candidate per name with
    inferred `place_type` / `subcategory` / `cuisine` and stamps the
    full evidence trail.
    """

    def __init__(self, vision_extractor: VisionExtractorProtocol) -> None:
        super().__init__(
            allowed_sources={PlaceSource.tiktok, PlaceSource.instagram}
        )
        self._vision_extractor = vision_extractor

    async def _run(self, context: ExtractionContext) -> None:
        if not context.is_photo_post or not context.image_urls:
            return

        try:
            await asyncio.wait_for(
                self._fetch_and_extract(context),
                timeout=_TOTAL_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "VisionImagesEnricher timed out for url=%s", context.url
            )
        except Exception as exc:
            logger.warning(
                "VisionImagesEnricher failed for url=%s: %s", context.url, exc
            )

    async def _fetch_and_extract(self, context: ExtractionContext) -> None:
        urls = context.image_urls[:_MAX_IMAGES]
        async with httpx.AsyncClient(
            timeout=_PER_REQUEST_TIMEOUT_SECONDS, follow_redirects=True
        ) as client:
            results = await asyncio.gather(
                *(self._download(client, u) for u in urls),
                return_exceptions=True,
            )
        images: list[bytes] = [r for r in results if isinstance(r, bytes) and r]
        if not images:
            return

        names = await self._vision_extractor.extract_place_names(images)
        for name in names:
            if name:
                context.known_places.append(
                    KnownPlace(
                        name=name,
                        producer=Producer.VISION_IMAGES,
                        medium=Medium.IMAGE,
                        snippet=name,
                    )
                )

    async def _download(
        self, client: httpx.AsyncClient, url: str
    ) -> bytes | None:
        try:
            response = await client.get(url)
            response.raise_for_status()
            return response.content
        except Exception as exc:
            logger.debug(
                "VisionImagesEnricher download failed for %s: %s", url, exc
            )
            return None
