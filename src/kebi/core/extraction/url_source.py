"""Single source of truth for mapping a URL to a `PlaceSource`."""

from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse

from kebi.core.places import PlaceSource

_TIKTOK_PHOTO_PATH_RE = re.compile(r"(/@[^/]+)/photo/", re.IGNORECASE)

# Hosts whose query strings are tracking noise (web_id, _t, share_app_id,
# utm_*, igshid, si, etc.). For these, dropping the query is safe and
# necessary for stable cache keying. youtu.be and the maps shortlinks
# are intentionally excluded — youtu.be's `?t=` is load-bearing, and
# maps.app.goo.gl is a shortlink whose canonical resolution is a
# follow-up. Everything else passes through unchanged so we don't
# accidentally strip semantically-required query params on hosts we
# haven't audited.
_CANONICAL_HOSTS: frozenset[str] = frozenset(
    {
        "tiktok.com",
        "www.tiktok.com",
        "m.tiktok.com",
        "vm.tiktok.com",
        "instagram.com",
        "www.instagram.com",
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
    }
)


def canonicalize_url(url: str | None) -> str | None:
    """Return a stable, pipeline-ready canonical form of `url`.

    Two transformations, both flavors of "canonical":

    1. **TikTok photo→video path rewrite.** TikTok photo-mode URLs use
       `/photo/<id>` in the path, which yt-dlp's extractor rejects with
       "Unsupported URL" — but the same post resolves cleanly under
       `/video/<id>`. Applied to any URL whose host contains
       `tiktok.com` (catches subdomain variants the canonical-host set
       below might miss).
    2. **Query + fragment strip, host lowercase, trailing-slash strip**
       for recognized hosts (TikTok, Instagram, YouTube — plus their
       `www.`, `m.`, and `vm.` variants). Two shares of the same TikTok
       with different `web_id` tracking params then collapse to the
       same canonical form — necessary for the extraction result cache
       (ADR-074) to actually hit.

    Unknown hosts pass through unchanged (conservative — universal
    tracking-param stripping is a follow-up). `None` round-trips as
    `None`.
    """
    if url is None:
        return None

    # TikTok photo→video rewrite. Applies before query-strip so the
    # regex matches against the original path.
    if "tiktok.com" in url.lower():
        url = _TIKTOK_PHOTO_PATH_RE.sub(r"\1/video/", url)

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in _CANONICAL_HOSTS:
        return url
    path = parsed.path.rstrip("/") or "/"
    return urlunparse(
        (
            parsed.scheme.lower() or "https",
            host,
            path,
            "",  # params
            "",  # query
            "",  # fragment
        )
    )


def source_from_url(url: str | None) -> PlaceSource | None:
    """Return the `PlaceSource` for a URL, or `None` for "no source".

    `None` is returned in two distinct cases:
    - `url is None` — caller passed nothing.
    - URL host doesn't map to any supported platform (e.g. a blog
      post, a generic short link, or an attacker host like
      `tiktok.com.evil.tld` whose hostname does not match the suffix
      `tiktok.com`). The service distinguishes the two by checking the
      original `url` value: a URL with `source is None` is an
      unsupported URL and gets rejected with a clear message before
      the cascade runs.

    Supported sources: TikTok, Instagram, YouTube, Google Maps.

    The actual host-suffix logic lives in
    `core/extraction/url_safety.source_from_url` — kept there so the
    same allowlist is reused by SSRF guards on the httpx / yt-dlp /
    Apify edges.
    """
    from kebi.core.extraction.url_safety import source_from_url as _delegate

    return _delegate(url)
