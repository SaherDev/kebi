"""InstagramPostEnricher — caption + hashtags + carousel images via Apify.

Instagram's public HTML returns a login wall for unauthenticated
requests, so yt-dlp / oEmbed / direct scraping all fail. We delegate
to the Apify `apify/instagram-post-scraper` actor, which uses a
managed login pool and returns full post data: caption, hashtags,
location, owner, and the **complete list of carousel slide URLs**.

Populates in one shot:
- `context.caption`         — post caption (with hashtags appended in body)
- `context.hashtags`        — pre-parsed `#tag` tokens (without the `#`)
- `context.location_tag`    — Instagram's tagged location (e.g. "Amsterdam Netherland")
- `context.is_photo_post`   — True for `Sidecar` (carousel) or `Image` posts
- `context.image_urls`      — every carousel slide, capped to fit vision budget

Gated to `PlaceSource.instagram`. Skips silently when `APIFY_TOKEN`
is not configured. Exceptions propagate to the surrounding
`CircuitBreakerEnricher` so a degraded Apify doesn't keep retrying
on every request.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from kebi.core.agent._trace_context import traced_call
from kebi.core.config import get_config, get_env
from kebi.core.extraction.source_filtered_enricher import SourceFilteredEnricher
from kebi.core.extraction.types import (
    Evidence,
    ExtractionContext,
    Medium,
    Producer,
)
from kebi.core.places import PlaceSource

logger = logging.getLogger(__name__)

_APIFY_ENDPOINT = (
    "https://api.apify.com/v2/acts/"
    "apify~instagram-post-scraper/run-sync-get-dataset-items"
)
_DEFAULT_TIMEOUT_SECONDS = 90.0
# Mirrors TikTokPhotoEnricher's cap — 10 covers IG carousels (max 10 slides)
# and trims long TikTok carousels to a sane vision-spend budget.
_MAX_IMAGE_URLS = 10
# Apify Instagram post types that should drive `is_photo_post = True`.
_PHOTO_POST_TYPES: frozenset[str] = frozenset({"Sidecar", "Image"})


class InstagramPostEnricher(SourceFilteredEnricher):
    """Pulls Instagram post data via the Apify scraper actor.

    Gated to `PlaceSource.instagram`. Single Apify call populates
    caption, hashtags, location, and the full carousel slide list —
    no other Instagram producer in the pipeline can do this without
    authentication (yt-dlp returns "no csrf token", direct HTML
    fetch hits the login wall).
    """

    def __init__(
        self,
        *,
        http: httpx.AsyncClient,
        token: str | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(allowed_sources={PlaceSource.instagram})
        # Lazy-resolve the token so tests can construct the enricher
        # without touching the env, but production callers can pass it
        # explicitly if they want.
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._http = http

    def _resolve_token(self) -> str | None:
        return self._token or get_env().APIFY_TOKEN

    async def _run(self, context: ExtractionContext) -> None:
        token = self._resolve_token()
        if not token:
            logger.info(
                "InstagramPostEnricher skipped — APIFY_TOKEN not configured (url=%s)",
                context.url,
            )
            return

        post = await self._fetch_post(context.url, token, context.user_id)  # type: ignore[arg-type]
        if post is None:
            return

        # Caption — first-write-wins so we don't trample a more accurate
        # source if one ran first (none currently do for Instagram).
        caption = post.get("caption")
        if isinstance(caption, str) and caption.strip() and not context.caption:
            context.caption = caption.strip()
            context.text_evidence.append(
                Evidence(
                    producer=Producer.INSTAGRAM_POST,
                    medium=Medium.CAPTION,
                    snippet=context.caption[:200],
                )
            )

        # Hashtags — Apify returns them pre-parsed, no `#` prefix.
        hashtags = post.get("hashtags")
        if isinstance(hashtags, list) and not context.hashtags:
            context.hashtags = [str(h) for h in hashtags if isinstance(h, str)]

        # Location — Instagram's tagged location (e.g. "Amsterdam Netherland").
        location_name = post.get("locationName")
        if (
            isinstance(location_name, str)
            and location_name.strip()
            and not context.location_tag
        ):
            context.location_tag = location_name.strip()
            context.text_evidence.append(
                Evidence(
                    producer=Producer.INSTAGRAM_POST,
                    medium=Medium.LOCATION_TAG,
                    snippet=context.location_tag[:200],
                )
            )

        # Carousel slides + photo-post detection.
        post_type = post.get("type")
        if post_type in _PHOTO_POST_TYPES and not context.image_urls:
            urls = _extract_image_urls(post)
            if urls:
                context.is_photo_post = True
                context.image_urls = urls[:_MAX_IMAGE_URLS]
                context.text_evidence.append(
                    Evidence(
                        producer=Producer.PHOTO_DETECTOR,
                        medium=Medium.IMAGE,
                        snippet=None,
                        metadata=(("image_count", len(context.image_urls)),),
                    )
                )

    async def _fetch_post(
        self, url: str, token: str, user_id: str | None = None
    ) -> dict[str, Any] | None:
        body = {"username": [url], "resultsLimit": 1}
        async with traced_call(
            "apify.instagram_post",
            "extraction",
            user_id=user_id,
            extra={
                "actor": "apify/instagram-post-scraper",
                "input_url": url,
            },
        ) as t:
            response = await self._http.post(
                _APIFY_ENDPOINT,
                params={"token": token},
                json=body,
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            items: list[dict[str, Any]] = (
                data if isinstance(data, list) else []
            )
            item_count = int(
                response.headers.get("x-apify-pagination-total", len(items))
            )
            t.cost_usd = get_config().pricing.external.apify.cost_for(
                "instagram_post", item_count
            )
            t.output = {"item_count": item_count}
            if not items:
                return None
            first = items[0]
            return first if isinstance(first, dict) else None


def _extract_image_urls(post: dict[str, Any]) -> list[str]:
    """Return carousel slide URLs in display order.

    Prefers `childPosts[*].displayUrl` (most structured for Sidecar),
    falls back to `images[]` (flat list, also Sidecar-only), then
    `displayUrl` (single-image fallback for `Image` posts). Each
    branch returns the first source that produced any URL — we don't
    union, since the same slides appear in multiple keys.
    """
    children = post.get("childPosts") or []
    if isinstance(children, list):
        from_children: list[str] = []
        for c in children:
            if not isinstance(c, dict):
                continue
            url = c.get("displayUrl")
            if isinstance(url, str) and url:
                from_children.append(url)
        if from_children:
            return from_children

    images = post.get("images") or []
    if isinstance(images, list):
        flat = [u for u in images if isinstance(u, str) and u]
        if flat:
            return flat

    display = post.get("displayUrl")
    if isinstance(display, str) and display:
        return [display]

    return []
