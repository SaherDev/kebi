"""Level 2 — VisionImagesEnricher: extract places from photo-post images."""

from __future__ import annotations

import asyncio
import logging
import sys

from totoro_ai.core.extraction.source_filtered_enricher import SourceFilteredEnricher
from totoro_ai.core.extraction.types import ExtractionContext
from totoro_ai.core.places import PlaceSource
from totoro_ai.providers.llm import VisionExtractorProtocol

logger = logging.getLogger(__name__)

_TOTAL_TIMEOUT_SECONDS = 15.0
_MAX_IMAGES = 10


class VisionImagesEnricher(SourceFilteredEnricher):
    """Downloads photo-post images via yt-dlp and extracts place names via vision LLM.

    Sibling to `VisionFramesEnricher`. Where frames-mode pipes a video
    stream from yt-dlp through ffmpeg, this enricher invokes
    `yt-dlp --playlist-items N -o - <url>` once per image (1..N in
    parallel) and captures each image's bytes from stdout. We don't
    httpx-GET the URLs that `PhotoDetectorEnricher` captured — those are
    often signed CDN links that 403 without yt-dlp's cookie/header
    handling, the same reason `VisionFramesEnricher` pipes through
    yt-dlp instead of fetching the resolved URL directly. Nothing is
    written to disk.

    Names-only producer: appends each extracted name to
    `context.known_places` and lets the deep-level finalizer
    (`LLMNEREnricher`) emit one structured `CandidatePlace` per name
    with `place_type` / `subcategory` / `cuisine` inferred from the
    name itself. Same path used by `GoogleMapsListEnricher`.
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
        assert context.url is not None  # SourceFilteredEnricher guarantees
        count = min(len(context.image_urls), _MAX_IMAGES)
        results = await asyncio.gather(
            *(self._capture_image(context.url, i) for i in range(1, count + 1)),
            return_exceptions=True,
        )
        images: list[bytes] = [r for r in results if isinstance(r, bytes) and r]
        if not images:
            return

        names = await self._vision_extractor.extract_place_names(images)
        for name in names:
            if name:
                context.known_places.append(name)

    async def _capture_image(self, url: str, item: int) -> bytes | None:
        """Fetch the Nth image of the post via yt-dlp -o - (stdout, no file).

        `--playlist-items` is 1-indexed in yt-dlp. For single-image posts,
        `1` selects the only entry; for carousels, `1..N` selects each
        slide in order.
        """
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "yt_dlp",
            "--playlist-items",
            str(item),
            "-o",
            "-",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        return stdout or None
