"""TikTokPhotoEnricher — detect TikTok photo posts and capture all carousel slides.

Instagram has its own dedicated path (`InstagramApifyEnricher`) — yt-dlp
can't read Instagram without auth, so calling it for IG URLs would just
trip the circuit breaker. This enricher handles TikTok only; the TikTok
path uses yt-dlp for detection, then falls back to scraping the page's
rehydration JSON when yt-dlp returns ≤1 image (carousel posts).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from typing import Any

import httpx

from kebi.core.extraction.source_filtered_enricher import SourceFilteredEnricher
from kebi.core.extraction.types import (
    Evidence,
    ExtractionContext,
    Medium,
    Producer,
)
from kebi.core.places import PlaceSource

logger = logging.getLogger(__name__)

# Cap mirrors the design decision: Instagram carousels max at 10, TikTok
# photo posts can be much longer — 10 covers IG fully and trims long
# TikTok carousels to a sane vision-spend budget.
_MAX_IMAGE_URLS = 10

# yt-dlp doesn't enumerate TikTok carousel slides — it returns the cover
# thumbnail and the music track only. The full slide list lives in the
# page's `__UNIVERSAL_DATA_FOR_REHYDRATION__` JSON blob, which we parse
# directly. Used as a TikTok-only fallback when yt-dlp gave us ≤1 image.
_TIKTOK_REHYDRATION_RE = re.compile(
    r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"'
    r' type="application/json">(.*?)</script>',
    re.DOTALL,
)
_TIKTOK_PAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
_TIKTOK_PAGE_TIMEOUT_SECONDS = 15.0


class TikTokPhotoEnricher(SourceFilteredEnricher):
    """Detects TikTok photo posts and captures their image URLs.

    Runs its own `yt-dlp --dump-json` (separate from `YtDlpMetadataEnricher`)
    so detection can read fields the metadata enricher intentionally does
    not touch — `_type`, `entries`, `vcodec`, `thumbnails`. When a photo
    post is detected, sets `context.is_photo_post = True` and populates
    `context.image_urls` with up to `_MAX_IMAGE_URLS` image URLs in their
    natural order. For TikTok carousel posts (where yt-dlp only exposes
    the cover thumbnail) falls back to scraping the page's
    `__UNIVERSAL_DATA_FOR_REHYDRATION__` JSON for the full slide list.

    Gated to TikTok only — Instagram goes through `InstagramApifyEnricher`,
    which calls Apify and gets caption + hashtags + carousel slides in one
    request (yt-dlp can't auth against Instagram).

    Does NOT catch exceptions — they propagate to `CircuitBreakerEnricher`.
    """

    def __init__(self, *, http: httpx.AsyncClient) -> None:
        super().__init__(allowed_sources={PlaceSource.tiktok})
        self._http = http

    async def _run(self, context: ExtractionContext) -> None:
        if context.is_photo_post or context.image_urls:
            return  # first-write-wins

        data = await self._fetch_metadata(context.url)  # type: ignore[arg-type]
        if data is None:
            return

        urls = _extract_image_urls(data)

        # yt-dlp only exposes the cover thumb (single image) for TikTok
        # photo posts, never the full carousel. When we got ≤1 URL on
        # a TikTok photo post, fetch the page HTML and pull every slide
        # from the rehydration blob. Best-effort — failures fall back
        # to whatever yt-dlp gave us.
        if (
            len(urls) <= 1
            and data.get("_type") != "playlist"
            and _is_image_entry(data)
            and context.url is not None
        ):
            carousel = await _fetch_tiktok_carousel_urls(self._http, context.url)
            if len(carousel) > len(urls):
                urls = carousel

        if not urls:
            return

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

    async def _fetch_metadata(self, url: str) -> dict[str, Any] | None:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "yt_dlp",
            "--dump-json",
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()

        if proc.returncode != 0:
            raise RuntimeError(f"yt-dlp exited with code {proc.returncode} for {url}")

        # Photo posts may emit one JSON object per line (one per entry).
        # `--dump-json` on a playlist gives newline-delimited JSON; on a
        # single item, one object. Parse the first non-empty line as the
        # primary record — that's enough for top-level _type / entries
        # detection. Single-line playlists and single records both fit.
        text = stdout.decode("utf-8", errors="replace").strip()
        if not text:
            return None
        first_line = text.splitlines()[0]
        try:
            parsed: dict[str, Any] = json.loads(first_line)
        except json.JSONDecodeError:
            return None
        return parsed


def _is_image_entry(entry: dict[str, Any]) -> bool:
    """Return True when a yt-dlp entry has no video stream.

    Three positive signals: explicit `vcodec == "none"`; a `formats` list
    where every entry is non-video; or no enumerable formats but a
    `thumbnails` array (the typical shape for Instagram carousel /
    TikTok photo-mode entries that yt-dlp can't iterate as streams).
    """
    if entry.get("vcodec") == "none":
        return True
    formats = entry.get("formats") or []
    if formats and all(f.get("vcodec") == "none" for f in formats):
        return True
    return bool(not formats and entry.get("thumbnails"))


def _extract_image_url(
    entry: dict[str, Any], prefer_thumbnails: bool = False
) -> str | None:
    """Pick the best image URL from a yt-dlp entry.

    `prefer_thumbnails`: when True, use the `thumbnails` array even if
    `entry.url` is set. This is needed for non-playlist photo posts
    where yt-dlp's top-level `url` field points at the audio track
    (the only "format" available for photo-mode), not the image —
    only the `thumbnails` array carries the actual photo URL.
    """
    if not prefer_thumbnails:
        url = entry.get("url")
        if isinstance(url, str) and url:
            return url
    thumbs = entry.get("thumbnails") or []
    if thumbs:
        best = max(
            thumbs,
            key=lambda t: (t.get("height") or 0) * (t.get("width") or 0),
            default=None,
        )
        if best is not None:
            candidate = best.get("url")
            if isinstance(candidate, str) and candidate:
                return candidate
    if prefer_thumbnails:
        # Fallback: thumbnails missing — try entry.url anyway.
        url = entry.get("url")
        if isinstance(url, str) and url:
            return url
    return None


def _extract_image_urls(data: dict[str, Any]) -> list[str]:
    """Inspect a yt-dlp response and return image URLs if it's a photo post."""
    if data.get("_type") == "playlist":
        urls: list[str] = []
        for entry in data.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            if not _is_image_entry(entry):
                continue
            url = _extract_image_url(entry)
            if url:
                urls.append(url)
        return urls

    if _is_image_entry(data):
        # Single-record photo post (e.g. TikTok photo-mode with no
        # carousel). yt-dlp's top-level `url` is the AUDIO track that
        # plays over the photo — not the photo. Force thumbnails.
        url = _extract_image_url(data, prefer_thumbnails=True)
        if url:
            return [url]
    return []


async def _fetch_tiktok_carousel_urls(client: httpx.AsyncClient, url: str) -> list[str]:
    """Pull all carousel slide URLs from a TikTok photo-post page.

    yt-dlp doesn't expose carousel slides for TikTok — only the cover
    thumbnail. The full slide list lives in
    `__UNIVERSAL_DATA_FOR_REHYDRATION__` on the rendered HTML page,
    under `webapp.video-detail.itemInfo.itemStruct.imagePost.images`.
    Each entry's `imageURL.urlList` is an ordered list of CDN mirrors;
    the first is the canonical signed URL.

    Best-effort: any HTTP failure / missing JSON / shape change returns
    `[]` so the caller can fall back to yt-dlp's cover thumbnail.

    Host-validated against the TikTok suffix allowlist + public-IP
    check at every redirect hop (SSRF defense — the URL comes from
    upstream input that may be attacker-influenced).
    """
    from kebi.core.extraction.url_safety import HOST_SUFFIXES, safe_get
    from kebi.core.places import PlaceSource

    try:
        response = await safe_get(
            client,
            url,
            allowed_suffixes=HOST_SUFFIXES[PlaceSource.tiktok],
            headers=_TIKTOK_PAGE_HEADERS,
            timeout=_TIKTOK_PAGE_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except PermissionError as exc:
        logger.warning("tiktok_carousel_refused", extra={"url": url, "error": str(exc)})
        return []
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        logger.warning(
            "tiktok_carousel_fetch_failed",
            extra={"url": url, "error": str(exc)},
        )
        return []

    match = _TIKTOK_REHYDRATION_RE.search(response.text)
    if match is None:
        return []
    try:
        data: dict[str, Any] = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []

    item_struct = (
        data.get("__DEFAULT_SCOPE__", {})
        .get("webapp.video-detail", {})
        .get("itemInfo", {})
        .get("itemStruct", {})
    )
    if not isinstance(item_struct, dict):
        return []
    image_post = item_struct.get("imagePost") or {}
    images = image_post.get("images") or []
    if not isinstance(images, list):
        return []

    urls: list[str] = []
    for img in images:
        if not isinstance(img, dict):
            continue
        url_list = (img.get("imageURL") or {}).get("urlList") or []
        if isinstance(url_list, list) and url_list:
            first = url_list[0]
            if isinstance(first, str) and first:
                urls.append(first)
    return urls
