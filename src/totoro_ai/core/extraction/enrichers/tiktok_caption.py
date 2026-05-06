"""TikTokCaptionEnricher — fetches the post caption via TikTok's oEmbed API."""

import httpx

from totoro_ai.core.extraction.source_filtered_enricher import SourceFilteredEnricher
from totoro_ai.core.extraction.types import (
    Evidence,
    ExtractionContext,
    Medium,
    Producer,
)
from totoro_ai.core.places import PlaceSource

_OEMBED_URL = "https://www.tiktok.com/oembed"
_TIMEOUT_SECONDS = 10.0  # TODO: move to config if oEmbed URL needs per-env override


class TikTokCaptionEnricher(SourceFilteredEnricher):
    """Fetches TikTok video caption via oEmbed API.

    Caption enricher: populates context.caption (first-write-wins).
    Does NOT catch exceptions — they propagate to CircuitBreakerEnricher.
    The base class's source-filter guard short-circuits anything other
    than `PlaceSource.tiktok` so non-TikTok URLs never hit the oEmbed
    endpoint and trip the circuit breaker on guaranteed failures.
    """

    def __init__(self) -> None:
        super().__init__(allowed_sources={PlaceSource.tiktok})

    async def _run(self, context: ExtractionContext) -> None:
        if context.caption is not None:
            return  # first-write-wins

        caption = await self._fetch_caption(context.url)  # type: ignore[arg-type]
        if caption and context.caption is None:
            context.caption = caption
            context.text_evidence.append(
                Evidence(
                    producer=Producer.TIKTOK_CAPTION,
                    medium=Medium.CAPTION,
                    snippet=caption[:200],
                )
            )
        if context.platform is None:
            context.platform = "tiktok"

    async def _fetch_caption(self, url: str) -> str | None:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                _OEMBED_URL,
                params={"url": url},
                timeout=_TIMEOUT_SECONDS,
            )
            response.raise_for_status()

        data = response.json()
        return data.get("title") or None
