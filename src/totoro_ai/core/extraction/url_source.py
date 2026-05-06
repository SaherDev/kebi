"""Single source of truth for mapping a URL to a `PlaceSource`."""

from __future__ import annotations

import re

from totoro_ai.core.places import PlaceSource

_TIKTOK_PHOTO_PATH_RE = re.compile(r"(/@[^/]+)/photo/", re.IGNORECASE)


def normalize_url(url: str | None) -> str | None:
    """Canonicalize platform URLs into the form yt-dlp can extract.

    TikTok photo-mode URLs use `/photo/<id>` in the path, which yt-dlp's
    extractor (as of 2026.03.17) rejects with "Unsupported URL" — but
    the same post resolves cleanly under `/video/<id>`. Rewrite once at
    the pipeline entry point so every downstream enricher
    (tiktok_photo, vision_images, video_metadata, …) hits a URL
    yt-dlp accepts.

    Returns the input unchanged for non-TikTok URLs, non-photo TikTok
    URLs, and `None`.
    """
    if url is None:
        return None
    if "tiktok.com" not in url.lower():
        return url
    return _TIKTOK_PHOTO_PATH_RE.sub(r"\1/video/", url)


def source_from_url(url: str | None) -> PlaceSource | None:
    """Return the `PlaceSource` for a URL, or `None` for "no source".

    `None` is returned in two distinct cases:
    - `url is None` — caller passed nothing.
    - URL host doesn't map to any supported platform (e.g. a blog
      post, a generic short link). The service distinguishes the two
      by checking the original `url` value: a URL with `source is None`
      is an unsupported URL and gets rejected with a clear message
      before the cascade runs.

    Supported sources: TikTok, Instagram, YouTube, Google Maps.
    """
    if url is None:
        return None
    lowered = url.lower()
    if "tiktok.com" in lowered:
        return PlaceSource.tiktok
    if "instagram.com" in lowered:
        return PlaceSource.instagram
    if "youtube.com" in lowered or "youtu.be" in lowered:
        return PlaceSource.youtube
    if (
        "maps.app.goo.gl" in lowered
        or "goo.gl/maps" in lowered
        or "google.com/maps" in lowered
        or "maps.google.com" in lowered
    ):
        return PlaceSource.google_maps
    return None
