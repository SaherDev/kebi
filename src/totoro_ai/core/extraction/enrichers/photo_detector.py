"""Level 1 — PhotoDetectorEnricher: detect Instagram/TikTok photo posts."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from totoro_ai.core.extraction.source_filtered_enricher import SourceFilteredEnricher
from totoro_ai.core.extraction.types import (
    Evidence,
    ExtractionContext,
    Medium,
    Producer,
)
from totoro_ai.core.places import PlaceSource

# Cap mirrors the design decision: Instagram carousels max at 10, TikTok
# photo posts can be much longer — 10 covers IG fully and trims long
# TikTok carousels to a sane vision-spend budget.
_MAX_IMAGE_URLS = 10


class PhotoDetectorEnricher(SourceFilteredEnricher):
    """Detects Instagram/TikTok photo posts and captures their image URLs.

    Runs its own `yt-dlp --dump-json` (separate from `YtDlpMetadataEnricher`)
    so detection can read fields the metadata enricher intentionally does
    not touch — `_type`, `entries`, `vcodec`, `thumbnails`. When a photo
    post is detected, sets `context.is_photo_post = True` and populates
    `context.image_urls` with up to `_MAX_IMAGE_URLS` image URLs in their
    natural order. Video posts and unrecognized payloads leave the context
    untouched.

    Does NOT catch exceptions — they propagate to `CircuitBreakerEnricher`.
    """

    def __init__(self) -> None:
        super().__init__(
            allowed_sources={PlaceSource.tiktok, PlaceSource.instagram}
        )

    async def _run(self, context: ExtractionContext) -> None:
        if context.is_photo_post or context.image_urls:
            return  # first-write-wins

        data = await self._fetch_metadata(context.url)  # type: ignore[arg-type]
        if data is None:
            return

        urls = _extract_image_urls(data)
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
            raise RuntimeError(
                f"yt-dlp exited with code {proc.returncode} for {url}"
            )

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
