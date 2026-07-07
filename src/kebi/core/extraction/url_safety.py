"""SSRF defense for the extraction pipeline.

Threat: any URL the user supplies (`POST /v1/extract` `raw_input`) or
that a third-party scraper returns (Apify rehydration JSON, TikTok
carousel imageURL lists) flows into yt-dlp / ffmpeg / httpx. Without
guards, an attacker URL like `https://tiktok.com.attacker.tld/...` —
which substring-matches "tiktok.com" — would resolve to an attacker
host. Once resolved, that host can:

- Return arbitrary HLS / manifest content that exploits a yt-dlp parser
  CVE.
- Redirect via HTTP 302 to a private cloud-metadata endpoint
  (169.254.169.254) and exfiltrate IAM credentials.
- Serve attacker-controlled bytes that the downstream LLM ingests as
  "transcript" or "caption", chaining into prompt injection.

This module provides three checks. Use them at every entry point that
hands a URL to a subprocess, an HTTP client, or a third-party scraper.

- `source_from_url`: exact-suffix host match (replaces the previous
  substring match in `extraction/url_source.py`).
- `is_safe_public_host`: resolves DNS and refuses private, loopback,
  link-local, and cloud-metadata addresses.
- `safe_get`: an httpx wrapper that revalidates the host on every
  redirect hop, refusing to follow into disallowed CIDRs or off-allowlist
  hostnames.

DNS is resolved at validation time using the system resolver. Treat
this as defense-in-depth — there is still a small TOCTOU window between
the resolve call and the subsequent yt-dlp / httpx call. The mitigation
for that is running yt-dlp inside a network namespace with no route to
RFC1918 / 169.254.169.254; this module narrows the surface but does not
eliminate it.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from kebi.core.places import PlaceSource

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)


# Exact host suffixes per platform. A host matches a suffix iff
# `host == suffix or host.endswith("." + suffix)`. No substring search.
HOST_SUFFIXES: dict[PlaceSource, tuple[str, ...]] = {
    PlaceSource.tiktok: ("tiktok.com",),
    PlaceSource.instagram: ("instagram.com",),
    PlaceSource.youtube: ("youtube.com", "youtu.be"),
    PlaceSource.google_maps_list: (
        "maps.app.goo.gl",
        "goo.gl",
        "google.com",
        "maps.google.com",
    ),
}

# CDN hosts that may serve image / video bytes for the extraction
# pipeline. Anything not on this list (or the social-host list above)
# is refused at the boundary. Add suffixes here when a new provider
# / CDN is introduced; never widen via substring match.
CDN_ALLOWLIST: tuple[str, ...] = (
    "tiktokcdn.com",
    "tiktokcdn-us.com",
    "cdninstagram.com",
    "fbcdn.net",
    "googleusercontent.com",
    "ggpht.com",
    "ytimg.com",
)


def _host_matches_suffix(host: str, suffix: str) -> bool:
    host = host.lower()
    suffix = suffix.lower()
    return host == suffix or host.endswith("." + suffix)


def host_in_suffixes(host: str | None, suffixes: tuple[str, ...]) -> bool:
    """Exact-suffix host match. Empty / None host is never a match."""
    if not host:
        return False
    return any(_host_matches_suffix(host, s) for s in suffixes)


def source_from_url(url: str | None) -> PlaceSource | None:
    """Map a URL to a `PlaceSource` via exact-suffix host match.

    Returns `None` for `url is None`, malformed URLs, or hosts that
    don't match any known platform suffix. The caller treats a `None`
    source as an unsupported URL and rejects the request before any
    enricher runs.
    """
    if url is None:
        return None
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return None
    for src, suffixes in HOST_SUFFIXES.items():
        if any(_host_matches_suffix(host, s) for s in suffixes):
            return src
    return None


def is_safe_public_host(url: str) -> bool:
    """Resolve `url`'s host and refuse private/loopback/link-local IPs.

    Returns True iff *every* address the host resolves to is a public,
    routable IP. Any private (RFC1918), loopback (127.0.0.0/8, ::1),
    link-local (169.254.0.0/16 — cloud metadata!), or multicast
    address fails the check.

    Resolution uses the system resolver. There is a TOCTOU window
    between this call and the subsequent network operation — see the
    module docstring for the mitigation expectation.
    """
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    # Explicit deny on the cloud-metadata hostnames in case a resolver
    # serves a public-looking A record for them.
    if host in {"metadata.google.internal", "metadata.goog"}:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False
    return True


def url_is_allowed(url: str, allowed_suffixes: tuple[str, ...]) -> bool:
    """Combined host-suffix + public-address check.

    `url` must (a) parse to a host on one of `allowed_suffixes` and
    (b) resolve only to public IP addresses. Failing either is a hard
    refusal — callers should log and skip the URL.
    """
    host = (urlparse(url).hostname or "").lower()
    if not host_in_suffixes(host, allowed_suffixes):
        return False
    return is_safe_public_host(url)


async def safe_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    allowed_suffixes: tuple[str, ...],
    timeout: float | None = None,
    **kwargs: object,
) -> httpx.Response:
    """`client.get(url)` with allowlist + redirect-revalidation.

    Each hop is revalidated against `allowed_suffixes` and the public-IP
    check before the next request fires. A redirect to a private host
    raises `PermissionError` so the caller can log and skip the URL
    rather than treating a 30x as a normal flow.

    `follow_redirects` is forced False on the underlying client call —
    we follow manually so each hop is checked. `timeout` overrides the
    client default when set.
    """
    import httpx as _httpx  # local import keeps top-of-file lean

    next_url = url
    for _hop in range(5):  # max 5 redirects, matches httpx default
        if not url_is_allowed(next_url, allowed_suffixes):
            raise PermissionError(
                f"refusing to fetch disallowed or private host: {next_url!r}"
            )
        response = await client.get(
            next_url,
            follow_redirects=False,
            timeout=timeout if timeout is not None else _httpx.USE_CLIENT_DEFAULT,
            **kwargs,  # type: ignore[arg-type]
        )
        if response.is_redirect:
            loc = response.headers.get("location")
            if not loc:
                return response
            # Resolve relative redirects against the current URL.
            next_url = str(response.next_request.url) if response.next_request else loc
            continue
        return response
    raise PermissionError(f"too many redirects starting from {url!r}")
